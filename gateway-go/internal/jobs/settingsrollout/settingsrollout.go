// Package settingsrollout is services/settings_rollout.py rollout_loop: on Kubernetes, the
// pods of the components that read a changed setting only at start restarted one at a
// time, when an administrator asks (Administration → Settings, «Roll out»). It evicts the
// oldest pod started before the request, waits until the component's pods are all ready
// again, evicts the next, component after component, the orchestrator last. It returns at
// once off Kubernetes or without the chart's settingsRollout.
//
// One step at a time across the processes: the transaction's advisory lock 7402121, asked
// once, as the Python loop asks it.
package settingsrollout

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	stepEvery = 5 * time.Second // STEP_EVERY
	lockKey   = 7_402_121       // LOCK
	maxOutput = 15 * 60         // MAX_OUTPUT: a pod on its way out longer than this no longer holds the rollout
)

// order is settings_rollout.ORDER: the orchestrator, which runs the rollout, last.
var order = []string{"worker", "preview-worker", "beat", "engine", "gateway", "orchestrator"}

func init() {
	api.RegisterJob("settings_rollout_loop", func(d *api.Deps) jobs.Job {
		r := &rollout{db: d.DB, log: d.Log, panel: d.Panel, cfg: d.Config.Jobs.Kube, kube: kube.FromEnv(),
			api: scriptcfg.Load(config.FromOS()).Kubernetes}
		return jobs.Job{Order: 12, Loop: r.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_step: what it did ("evicted <component>: <pod>", …)
			"settings_rollout": func(ctx context.Context) (any, error) {
				if r.mode() != "kubernetes" {
					return nil, nil
				}
				did, err := r.oneStep(ctx)
				if did == "" {
					return nil, err
				}
				return did, err
			},
		}}
	})
}

type rollout struct {
	db    *pgxpool.Pool
	log   *slog.Logger
	panel *settings.Panel
	cfg   config.KubeJobsConfig
	kube  kube.Settings
	api   scriptcfg.Kubernetes
}

// mode is settings_rollout.mode: kubernetes (the panel restarts the pods), manual (on
// Kubernetes without the chart's right), compose.
func (r *rollout) mode() string {
	if !r.kube.OnKubernetes() {
		return "compose"
	}
	_, err := os.Stat(r.kube.TokenFile)
	if r.cfg.Rollout && r.cfg.Release != "" && err == nil {
		return "kubernetes"
	}
	return "manual"
}

func (r *rollout) kubeAPI() scriptcfg.Kubernetes {
	k := r.api
	k.TimeoutSeconds = r.panel.KubernetesTimeout()
	return k
}

// namespace is cluster_view.release_namespace.
func (r *rollout) namespace() string {
	if r.cfg.Namespace != "" {
		return r.cfg.Namespace
	}
	raw, err := os.ReadFile(r.cfg.NamespaceFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// parseStamp is datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(utc) without
// its zone; false when it is no moment.
func parseStamp(v any) (time.Time, bool) {
	raw, ok := v.(string)
	if !ok {
		return time.Time{}, false
	}
	raw = strings.Replace(raw, "Z", "+00:00", -1)
	for _, layout := range []string{"2006-01-02T15:04:05.999999999-07:00", "2006-01-02T15:04:05.999999999",
		"2006-01-02 15:04:05.999999999-07:00", "2006-01-02 15:04:05.999999999", "2006-01-02T15:04-07:00", "2006-01-02T15:04",
		"2006-01-02"} {
		if t, err := time.Parse(layout, raw); err == nil {
			return t.UTC(), true
		}
	}
	return time.Time{}, false
}

func metadata(pod any) pyjson.Object { return kube.Dict(kube.Dict(pod).Value("metadata")) }

// created is _created: nil when the pod has no moment of creation.
func created(pod any) (time.Time, bool) { return parseStamp(metadata(pod).Value("creationTimestamp")) }

// leaving is _leaving: on its way out, and not for so long that its node must be gone.
func leaving(pod any) bool {
	raw := metadata(pod).Value("deletionTimestamp")
	if !pyjson.Truthy(raw) {
		return false
	}
	since, ok := parseStamp(raw)
	if !ok {
		return true
	}
	return jobs.Now().Sub(since).Seconds() < maxOutput
}

// ready is _ready.
func ready(pod any) bool {
	if pyjson.Truthy(metadata(pod).Value("deletionTimestamp")) {
		return false
	}
	status := kube.Dict(kube.Dict(pod).Value("status"))
	if status.Value("phase") != "Running" {
		return false
	}
	for _, c := range kube.List(status.Value("conditions")) {
		o := kube.Dict(c)
		if o.Value("type") == "Ready" && o.Value("status") == "True" {
			return true
		}
	}
	return false
}

// name is _name.
func name(pod any) string { return kube.Text(metadata(pod).Value("name")) }

// pods is settings_rollout.pods: the component's pods of the release, but the ended ones.
func (r *rollout) pods(ctx context.Context, component string) ([]any, error) {
	selector := "app.kubernetes.io/instance=" + r.cfg.Release + ",app.kubernetes.io/component=" + component
	if r.cfg.AppName != "" {
		selector += ",app.kubernetes.io/name=" + r.cfg.AppName
	}
	out, err := kube.Call(ctx, r.kubeAPI(), http.MethodGet, "/api/v1/namespaces/"+r.namespace()+"/pods?labelSelector="+kube.Quote(selector), nil, false)
	if err != nil {
		return nil, err
	}
	var list []any
	for _, p := range kube.List(kube.Dict(out).Value("items")) {
		phase := kube.Dict(kube.Dict(p).Value("status")).Value("phase")
		if phase != "Succeeded" && phase != "Failed" {
			list = append(list, p)
		}
	}
	return list, nil
}

// old is settings_rollout.old: the pods started before `since`, oldest first, not already on
// their way out.
func old(pods []any, since time.Time) []any {
	type one struct {
		pod any
		at  time.Time // the zero time is datetime.min
	}
	var out []one
	for _, p := range pods {
		if pyjson.Truthy(metadata(p).Value("deletionTimestamp")) {
			continue
		}
		at, _ := created(p)
		if at.Before(since) {
			out = append(out, one{p, at})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].at.Before(out[j].at) })
	list := make([]any, len(out))
	for i, o := range out {
		list[i] = o.pod
	}
	return list
}

// evict is settings_rollout.evict: true if evicted (or already gone), false if a
// PodDisruptionBudget says not now.
func (r *rollout) evict(ctx context.Context, pod string) (bool, error) {
	ns := r.namespace()
	_, err := kube.Call(ctx, r.kubeAPI(), http.MethodPost, "/api/v1/namespaces/"+ns+"/pods/"+pod+"/eviction", pyjson.Object{
		{Key: "apiVersion", Value: "policy/v1"}, {Key: "kind", Value: "Eviction"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: pod}, {Key: "namespace", Value: ns}}}}, false)
	switch kube.Status(err) {
	case 404:
		return true, nil
	case 429:
		return false, nil
	}
	return err == nil, err
}

// oneStep is one_step: evicts one pod, or closes a finished request, or waits; what it did
// ("" when nothing).
func (r *rollout) oneStep(ctx context.Context) (string, error) {
	me := os.Getenv("HOSTNAME")
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(lockKey)).Scan(&got); err != nil {
		return "", err
	}
	if !got {
		return "", nil
	}
	requested := map[string]time.Time{}
	rows, err := tx.Query(ctx, `SELECT component, requested_at FROM settings_rollouts WHERE requested_at IS NOT NULL`)
	if err != nil {
		return "", err
	}
	for rows.Next() {
		var c string
		var at time.Time
		if err := rows.Scan(&c, &at); err != nil {
			rows.Close()
			return "", err
		}
		requested[c] = at
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return "", err
	}
	for _, c := range order {
		at, ok := requested[c]
		if !ok {
			continue
		}
		all, err := r.pods(ctx, c)
		if err != nil {
			return "", err
		}
		var pl []any
		for _, p := range all {
			if !pyjson.Truthy(metadata(p).Value("deletionTimestamp")) || leaving(p) {
				pl = append(pl, p)
			}
		}
		stale := old(pl, at)
		staleNames := map[string]bool{}
		for _, p := range stale {
			staleNames[name(p)] = true
		}
		// a pod on its way out, or a new one not ready yet: wait. A new pod that never gets
		// ready stops the rollout there, with the old pods still serving
		var busy []string
		for _, p := range pl {
			if !ready(p) && (pyjson.Truthy(metadata(p).Value("deletionTimestamp")) || !staleNames[name(p)]) {
				busy = append(busy, name(p))
			}
		}
		if len(busy) > 0 {
			return "waiting " + c + ": " + strings.Join(busy, ", "), tx.Commit(ctx)
		}
		if len(stale) == 0 {
			// closed only if nobody asked again meanwhile (a new request, not under the lock)
			if _, err := tx.Exec(ctx, `UPDATE settings_rollouts SET requested_at = NULL WHERE component = $1 AND requested_at = $2`,
				c, at); err != nil {
				return "", err
			}
			if err := tx.Commit(ctx); err != nil {
				return "", err
			}
			r.log.Info("settings: rollout of " + c + " done")
			return "done " + c, nil
		}
		// an old pod that is not ready first (it serves nothing), itself last: the next
		// orchestrator closes the request
		var target any
		for _, p := range stale {
			if !ready(p) {
				target = p
				break
			}
		}
		if target == nil {
			target = stale[0]
			for _, p := range stale {
				if name(p) != me {
					target = p
					break
				}
			}
		}
		pod := name(target)
		evicted, err := r.evict(ctx, pod)
		if err != nil {
			return "", err
		}
		if !evicted {
			return "blocked " + c + ": " + pod, tx.Commit(ctx)
		}
		r.log.Info(fmt.Sprintf("settings: rollout of %s, pod %s evicted (%d left)", c, pod, len(stale)-1))
		return "evicted " + c + ": " + pod, tx.Commit(ctx)
	}
	return "", tx.Commit(ctx)
}

// loop is rollout_loop.
func (r *rollout) loop(ctx context.Context) {
	if r.mode() != "kubernetes" {
		return
	}
	for ctx.Err() == nil {
		if _, err := r.oneStep(ctx); err != nil && !errors.Is(err, context.Canceled) {
			// the API or the database down: at the next step
			r.log.Warn(fmt.Sprintf("settings: rollout step failed (%v), again in %ss", err, "5.0"))
		}
		if !jobs.Sleep(ctx, stepEvery) {
			break
		}
	}
}
