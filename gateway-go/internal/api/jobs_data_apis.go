package api

// The data APIs' reconcile loop (docs/specs/data-apis.md, «Runtime»): every 30 seconds, in the
// orchestrator, the Kubernetes objects of every data API are kept in line with the database in
// DATA_API__NAMESPACE (internal/dataapis/cluster.go makes them): an active one has its pods, a
// paused one its objects with no pods, a deleted one (or one whose creator's account went, the
// rows cascading) none. Objects labelled for a data API the database no longer has are removed.
// The pod token is made here, at the first round after an activation (the route forgets the
// previous one): the Secret gets it, the database its sha-256, and the pods restart with it.
// What the cluster says of each data API and of its pods is kept for the page
// (data_api_status, data_api_pods), and the usage past DATA_API__USAGE_RETENTION_DAYS goes.
//
// One round at a time across the orchestrators: the session advisory lock 7402150, held for
// the round on a connection of its own. Where the data APIs cannot be (no Kubernetes) the loop
// returns at once; with the feature switched off it removes what is left.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
)

const (
	daLock  = 7_402_150
	daEvery = 30 * time.Second
	// a size of the storage asked again after this, for the same data
	daSizeKeep = time.Hour
)

// the kinds of objects a data API has, in the order they are applied
var daKinds = []kube.Kind{kube.Secrets, kube.Services, kube.NetworkPolicies, kube.Deployments, kube.Ingresses,
	kube.PodDisruptionBudgets, kube.HorizontalPodAutoscaler}

func init() {
	RegisterJob("data_apis_loop", func(d *Deps) jobs.Job {
		r := newDataAPIReconciler(d, kubeCluster{namespace: d.Config.DataAPI.Pods.Namespace})
		return jobs.Job{Order: 13, Loop: r.loop}
	})
}

// dapiCluster is the namespace of the data APIs, as the loop sees it (a fake one in the tests).
type dapiCluster interface {
	Apply(ctx context.Context, kind kube.Kind, name string, obj kube.Obj) (kube.Obj, error)
	Delete(ctx context.Context, kind kube.Kind, name string) error
	List(ctx context.Context, kind kube.Kind, selector string) ([]kube.Obj, error)
}

type kubeCluster struct{ namespace string }

func (c kubeCluster) Apply(ctx context.Context, kind kube.Kind, name string, obj kube.Obj) (kube.Obj, error) {
	return kube.Apply(ctx, kubeAPI(), c.namespace, kind, name, dataapis.FieldManager, obj)
}

func (c kubeCluster) Delete(ctx context.Context, kind kube.Kind, name string) error {
	return kube.Delete(ctx, kubeAPI(), c.namespace, kind, name)
}

func (c kubeCluster) List(ctx context.Context, kind kube.Kind, selector string) ([]kube.Obj, error) {
	return kube.ListObjects(ctx, kubeAPI(), c.namespace, kind, selector)
}

type dataAPIReconciler struct {
	d       *Deps
	cluster dapiCluster
	// the size of a data file, by its fingerprint (another file, another run: another one)
	mu    sync.Mutex
	sizes map[string]daSize
	// the disk each data API was last given: kept when a size cannot be had
	disks map[int64]int64
	// the last warning, not repeated every round
	warned   string
	warnedAt time.Time
}

type daSize struct {
	bytes int64
	at    time.Time
}

func newDataAPIReconciler(d *Deps, c dapiCluster) *dataAPIReconciler {
	return &dataAPIReconciler{d: d, cluster: c, sizes: map[string]daSize{}, disks: map[int64]int64{}}
}

func (r *dataAPIReconciler) cfg() config.DataAPIPods { return r.d.Config.DataAPI.Pods }

func (r *dataAPIReconciler) loop(ctx context.Context) {
	if dataAPIsOff(r.d) == "kubernetes" || r.cfg().Namespace == "" {
		return
	}
	for ctx.Err() == nil {
		if err := r.round(ctx); err != nil && !errors.Is(err, context.Canceled) {
			r.warn(fmt.Sprintf("data apis: the cluster not reconciled (%v), again in %ds", err, int(daEvery/time.Second)))
		}
		if !jobs.Sleep(ctx, daEvery) {
			break
		}
	}
}

// warn logs a warning, the same one at most every ten minutes
func (r *dataAPIReconciler) warn(text string) {
	if text == r.warned && time.Since(r.warnedAt) < 10*time.Minute {
		return
	}
	r.warned, r.warnedAt = text, time.Now()
	r.d.Log.Warn(text)
}

// daRow is what the loop reads of a data API.
type daRow struct {
	api       *dataapis.API
	tokenHash string
}

// round is one pass: the objects of every data API in line, the orphans removed, the status
// kept, the old usage deleted.
func (r *dataAPIReconciler) round(ctx context.Context) error {
	conn, err := r.d.DB.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	var mine bool
	if err := conn.QueryRow(ctx, `SELECT pg_try_advisory_lock($1)`, int64(daLock)).Scan(&mine); err != nil {
		return err
	}
	if !mine {
		return nil
	}
	defer func() { _, _ = conn.Exec(context.WithoutCancel(ctx), `SELECT pg_advisory_unlock($1)`, int64(daLock)) }()
	c := r.cfg()
	// what the cluster has: every object of every data API of the release
	have := map[string][]kube.Obj{}
	for _, kind := range daKinds {
		list, err := r.cluster.List(ctx, kind, dataapis.Selector(c.Release))
		if err != nil {
			return fmt.Errorf("listing the %s: %w", kind.Plural, err)
		}
		have[kind.Plural] = list
	}
	// what the database wants: the active and paused data APIs (none with the feature off)
	wanted := map[int64]*daRow{}
	if dataAPIsOn(r.d) {
		rows, err := r.d.DB.Query(ctx, `SELECT id, coalesce(pod_token_hash, '') FROM data_apis WHERE state IN ($1, $2) ORDER BY id`,
			dataapis.Active, dataapis.Paused)
		if err != nil {
			return err
		}
		hashes := map[int64]string{}
		for rows.Next() {
			var id int64
			var hash string
			if err := rows.Scan(&id, &hash); err != nil {
				rows.Close()
				return err
			}
			hashes[id] = hash
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		for id, hash := range hashes {
			a, err := dataapis.Get(ctx, r.d.DB, id)
			if err != nil {
				return err
			}
			if a != nil {
				wanted[id] = &daRow{api: a, tokenHash: hash}
			}
		}
	}
	// the objects of data APIs that are no longer wanted: deleted, drafts, the feature off
	for _, kind := range daKinds {
		for _, obj := range have[kind.Plural] {
			labels, _ := kube.At(obj, "metadata", "labels").(kube.Obj)
			id := dataapis.ParseID(labels)
			if id > 0 && wanted[id] != nil {
				continue
			}
			name := kube.Str(obj, "metadata", "name")
			if err := r.cluster.Delete(ctx, kind, name); err != nil {
				r.warn(fmt.Sprintf("data apis: the %s %s not removed (%v)", strings.TrimSuffix(kind.Plural, "s"), name, err))
				continue
			}
			r.d.Log.Info("data apis: removed", "kind", kind.Plural, "name", name, "data_api", id)
		}
	}
	if _, err := r.d.DB.Exec(ctx, `DELETE FROM data_api_status s WHERE NOT EXISTS (SELECT 1 FROM data_apis a
		WHERE a.id = s.data_api_id AND a.state IN ($1, $2))`, dataapis.Active, dataapis.Paused); err != nil {
		return err
	}
	if _, err := r.d.DB.Exec(ctx, `DELETE FROM data_api_pods p WHERE NOT EXISTS (SELECT 1 FROM data_apis a
		WHERE a.id = p.data_api_id AND a.state IN ($1, $2))`, dataapis.Active, dataapis.Paused); err != nil {
		return err
	}
	// the pods, for the status
	pods, err := r.cluster.List(ctx, kube.Pods, dataapis.Selector(c.Release))
	if err != nil {
		return fmt.Errorf("listing the pods: %w", err)
	}
	podsOf := map[int64][]dataapis.PodCluster{}
	for _, p := range pods {
		labels, _ := kube.At(p, "metadata", "labels").(kube.Obj)
		if id := dataapis.ParseID(labels); id > 0 {
			podsOf[id] = append(podsOf[id], podCluster(p))
		}
	}
	ids := make([]int64, 0, len(wanted))
	for id := range wanted {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	for _, id := range ids {
		status, err := r.one(ctx, wanted[id], have)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return err
			}
			status.Error = err.Error()
			r.warn(fmt.Sprintf("data apis: data API %d not reconciled (%v)", id, err))
		}
		if status.Error == "" {
			status.Error = podsTrouble(podsOf[id])
		}
		if err := dataapis.SetClusterStatus(ctx, r.d.DB, id, status); err != nil {
			return err
		}
		if err := dataapis.SetPodsCluster(ctx, r.d.DB, id, podsOf[id]); err != nil {
			return err
		}
	}
	// the usage past its retention
	days := max(r.d.Config.DataAPI.UsageRetentionDays, 1)
	if n, err := dataapis.DeleteUsageBefore(ctx, r.d.DB, dataapis.Now().Add(-time.Duration(days)*24*time.Hour)); err != nil {
		return err
	} else if n > 0 {
		r.d.Log.Info("data apis: old usage deleted", "rows", n, "retention_days", days)
	}
	return nil
}

func named(list []kube.Obj, name string) kube.Obj {
	for _, o := range list {
		if kube.Str(o, "metadata", "name") == name {
			return o
		}
	}
	return nil
}

// one keeps the objects of one data API in line: what the loop saw of its Deployment, and the
// first thing the cluster refused.
func (r *dataAPIReconciler) one(ctx context.Context, row *daRow, have map[string][]kube.Obj) (dataapis.ClusterStatus, error) {
	c := r.cfg()
	a := row.api
	name := dataapis.Name(c.Release, a.ID)
	status := dataapis.ClusterStatus{}
	if c.Image == "" {
		return status, errors.New("no image for the pods: DATA_API__IMAGE is not set")
	}
	if c.GatewayURL == "" {
		return status, errors.New("no address of the gateway for the pods: DATA_API__GATEWAY_URL is not set")
	}
	disk, err := r.disk(ctx, a)
	if err != nil {
		return status, err
	}
	status.Disk = dataapis.Quantity(disk)
	// the pod token: the Secret's, while it is the one the database knows; a new one otherwise
	// (the first round after an activation, or a Secret gone)
	hash := row.tokenHash
	held := ""
	if secret := named(have[kube.Secrets.Plural], name); secret != nil {
		if b, err := base64.StdEncoding.DecodeString(kube.Str(secret, "data", dataapis.SecretKey)); err == nil {
			held = string(b)
		}
	}
	if hash == "" || held == "" || !dataapiwire.SameHash(dataapiwire.Hash(held), hash) {
		token, err := dataapis.RotatePodToken(ctx, r.d.DB, a.ID)
		if err != nil {
			return status, err
		}
		if _, err := r.cluster.Apply(ctx, kube.Secrets, name, dataapis.Secret(c, a.ID, token)); err != nil {
			return status, fmt.Errorf("the Secret: %w", err)
		}
		hash = dataapiwire.Hash(token)
		r.d.Log.Info("data apis: a new pod token", "data_api", a.ID)
	}
	want := dataapis.Want{API: a, TokenHash: hash, Disk: disk, PollSeconds: r.d.Config.DataAPI.PollSeconds}
	if _, err := r.cluster.Apply(ctx, kube.Services, name, dataapis.Service(c, a.ID)); err != nil {
		return status, fmt.Errorf("the Service: %w", err)
	}
	if err := r.applyOrDelete(ctx, have, kube.NetworkPolicies, name, dataapis.NetworkPolicy(c, a.ID)); err != nil {
		return status, fmt.Errorf("the NetworkPolicy: %w", err)
	}
	dep, err := r.cluster.Apply(ctx, kube.Deployments, name, dataapis.Deployment(c, want))
	if err != nil {
		return status, fmt.Errorf("the Deployment: %w", err)
	}
	status.Wanted = int(kube.Int(dep, "spec", "replicas"))
	status.Ready = int(kube.Int(dep, "status", "readyReplicas"))
	status.Updated = int(kube.Int(dep, "status", "updatedReplicas"))
	status.Error = deploymentTrouble(dep)
	if _, err := r.cluster.Apply(ctx, kube.Ingresses, name, dataapis.Ingress(c, a)); err != nil {
		return status, fmt.Errorf("the Ingress: %w", err)
	}
	if err := r.applyOrDelete(ctx, have, kube.PodDisruptionBudgets, name, dataapis.PodDisruptionBudget(c, want)); err != nil {
		return status, fmt.Errorf("the PodDisruptionBudget: %w", err)
	}
	if err := r.applyOrDelete(ctx, have, kube.HorizontalPodAutoscaler, name, dataapis.HorizontalPodAutoscaler(c, want)); err != nil {
		return status, fmt.Errorf("the HorizontalPodAutoscaler: %w", err)
	}
	return status, nil
}

// applyOrDelete applies the object, or removes it when there is none to have (nil)
func (r *dataAPIReconciler) applyOrDelete(ctx context.Context, have map[string][]kube.Obj, kind kube.Kind, name string, obj kube.Obj) error {
	if obj == nil {
		if named(have[kind.Plural], name) == nil {
			return nil
		}
		return r.cluster.Delete(ctx, kind, name)
	}
	_, err := r.cluster.Apply(ctx, kind, name, obj)
	return err
}

// disk is what each pod of the data API gets for its data files, from the size of its
// datasources' parquets (dataapis.Disk); the last one given when a size cannot be had.
func (r *dataAPIReconciler) disk(ctx context.Context, a *dataapis.API) (int64, error) {
	res, err := dataapis.Resources(ctx, r.d.DB, a.ID)
	if err != nil {
		return 0, err
	}
	ids := []int64{}
	for _, x := range res {
		ids = append(ids, x.Source.ID)
	}
	files, err := dataapis.DataFiles(ctx, r.d.DB, ids)
	if err != nil {
		return 0, err
	}
	maxDisk, _ := config.MemoryBytes(r.cfg().MaxDisk)
	sizes := []int64{}
	seen := map[int64]bool{}
	for _, x := range res {
		f, ok := files[x.Source.ID]
		if !ok || f.Key == "" || seen[x.Source.ID] {
			continue
		}
		seen[x.Source.ID] = true
		size, err := r.size(ctx, f)
		if err != nil {
			if last, ok := r.disks[a.ID]; ok {
				return last, nil
			}
			r.warn(fmt.Sprintf("data apis: the size of a data file of data API %d not had (%v)", a.ID, err))
			continue
		}
		sizes = append(sizes, size)
	}
	disk := dataapis.Disk(sizes, maxDisk)
	r.disks[a.ID] = disk
	return disk, nil
}

// size is a data file's size in bytes, asked of the engine (the storage's HEAD) once per file
// and hour; 0 when it is not there
func (r *dataAPIReconciler) size(ctx context.Context, f dataapis.DataFile) (int64, error) {
	key := f.Fingerprint()
	r.mu.Lock()
	known, ok := r.sizes[key]
	r.mu.Unlock()
	if ok && time.Since(known.at) < daSizeKeep {
		return known.bytes, nil
	}
	_, size, missing, err := dataAPIObject(ctx, r.d, f.Bucket, f.Key, 60)
	if err != nil {
		return 0, err
	}
	if missing {
		size = 0
	}
	r.mu.Lock()
	r.sizes[key] = daSize{bytes: size, at: time.Now()}
	r.mu.Unlock()
	return size, nil
}

// deploymentTrouble is what the Deployment's conditions say is wrong ("" when nothing): a pod
// the cluster refused (a quota, the namespace's Pod Security), a rollout that does not progress.
func deploymentTrouble(dep kube.Obj) string {
	conditions, _ := kube.At(dep, "status", "conditions").([]any)
	for _, raw := range conditions {
		cond, _ := raw.(kube.Obj)
		kind, state := kube.Str(cond, "type"), kube.Str(cond, "status")
		if (kind == "ReplicaFailure" && state == "True") || (kind == "Progressing" && state == "False") {
			return strings.TrimSpace(kube.Str(cond, "reason") + ": " + kube.Str(cond, "message"))
		}
	}
	return ""
}

// podsTrouble is the first thing wrong with the pods ("" when nothing): one that cannot be
// scheduled, that cannot pull its image, that keeps failing, that was evicted.
func podsTrouble(pods []dataapis.PodCluster) string {
	for _, p := range pods {
		if p.Terminating || p.Reason == "" || p.Reason == "ContainerCreating" || p.Reason == "PodInitializing" {
			continue
		}
		if p.Message != "" {
			return p.Name + ": " + p.Reason + ": " + p.Message
		}
		return p.Name + ": " + p.Reason
	}
	return ""
}

// podCluster is what the page shows of a pod as the cluster has it.
func podCluster(p kube.Obj) dataapis.PodCluster {
	out := dataapis.PodCluster{Name: kube.Str(p, "metadata", "name"), Phase: kube.Str(p, "status", "phase"),
		Node: kube.Str(p, "spec", "nodeName"), Terminating: kube.At(p, "metadata", "deletionTimestamp") != nil}
	if t, err := time.Parse(time.RFC3339, kube.Str(p, "status", "startTime")); err == nil {
		out.Started = &t
	}
	if reason := kube.Str(p, "status", "reason"); reason != "" { // Evicted: the disk, the node's memory
		out.Reason, out.Message = reason, kube.Str(p, "status", "message")
	}
	conditions, _ := kube.At(p, "status", "conditions").([]any)
	for _, raw := range conditions {
		cond, _ := raw.(kube.Obj)
		if kube.Str(cond, "type") == "PodScheduled" && kube.Str(cond, "status") == "False" && out.Reason == "" {
			out.Reason, out.Message = kube.Str(cond, "reason"), kube.Str(cond, "message")
		}
	}
	statuses, _ := kube.At(p, "status", "containerStatuses").([]any)
	for _, raw := range statuses {
		cs, _ := raw.(kube.Obj)
		out.Ready = cs["ready"] == true
		out.Restarts = kube.Int(cs, "restartCount")
		if w := kube.Str(cs, "state", "waiting", "reason"); w != "" && out.Reason == "" {
			out.Reason, out.Message = w, kube.Str(cs, "state", "waiting", "message")
		}
		if last := kube.Str(cs, "lastState", "terminated", "reason"); last != "" && out.Restarts > 0 {
			exit := kube.Int(cs, "lastState", "terminated", "exitCode")
			what := fmt.Sprintf("last ended: %s (exit %d)", last, exit)
			if out.Message == "" {
				out.Message = what
			} else {
				out.Message += "; " + what
			}
		}
	}
	if len(out.Message) > dataapiwire.MaxStatusText {
		out.Message = out.Message[:dataapiwire.MaxStatusText] + "…"
	}
	return out
}

// dataAPIObject asks the engine for a short-lived link to an object of the storage and its size
// (the gateway holds no storage credentials: the engine signs it, as for the notebooks);
// missing when the object is not there.
func dataAPIObject(ctx context.Context, d *Deps, bucket, key string, seconds int) (link string, size int64, missing bool, err error) {
	if d.Engine == nil {
		return "", 0, false, errors.New("no engine")
	}
	query := "bucket=" + url.QueryEscape(bucket) + "&key=" + url.QueryEscape(key) + "&expires=" + strconv.Itoa(seconds)
	res, raw, err := d.Engine.JSON(ctx, http.MethodGet, "/files/presigned?"+query, nil)
	if err != nil {
		return "", 0, false, err
	}
	if res.StatusCode == http.StatusNotFound {
		return "", 0, true, nil
	}
	if res.StatusCode != http.StatusOK {
		return "", 0, false, fmt.Errorf("the engine answered %d", res.StatusCode)
	}
	var out struct {
		URL  string `json:"url"`
		Size int64  `json:"size"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || out.URL == "" {
		return "", 0, false, fmt.Errorf("the engine's link: %v", err)
	}
	return out.URL, out.Size, false, nil
}
