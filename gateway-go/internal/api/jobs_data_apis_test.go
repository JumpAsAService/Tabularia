package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
)

// The reconcile loop on a real database, against a namespace kept in memory: the objects an
// activation makes, a pause, a scale, autoscaling, a deletion, the orphans, the pod token, the
// status the page reads and the usage that goes.

// fakeNamespace holds objects as the API server would, roughly: a Secret's stringData becomes
// its data, a Deployment reports its pods ready.
type fakeNamespace struct {
	mu      sync.Mutex
	objects map[string]map[string]kube.Obj // plural → name → object
	pods    []kube.Obj
	applied int
	refuse  map[string]error // plural → the error every apply of it gets
}

func newFakeNamespace() *fakeNamespace {
	return &fakeNamespace{objects: map[string]map[string]kube.Obj{}, refuse: map[string]error{}}
}

func (n *fakeNamespace) Apply(_ context.Context, kind kube.Kind, name string, obj kube.Obj) (kube.Obj, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	if err := n.refuse[kind.Plural]; err != nil {
		return nil, err
	}
	raw, _ := json.Marshal(obj)
	var stored kube.Obj
	_ = json.Unmarshal(raw, &stored)
	if sd, ok := stored["stringData"].(kube.Obj); ok {
		data := kube.Obj{}
		for k, v := range sd {
			data[k] = base64.StdEncoding.EncodeToString([]byte(v.(string)))
		}
		stored["data"] = data
		delete(stored, "stringData")
	}
	if kind == kube.Deployments {
		replicas := kube.Int(stored, "spec", "replicas")
		if _, set := kube.At(stored, "spec").(kube.Obj)["replicas"]; !set { // autoscaled: the HPA's minimum
			replicas = 1
			stored["spec"].(kube.Obj)["replicas"] = float64(1)
		}
		stored["status"] = kube.Obj{"readyReplicas": float64(replicas), "updatedReplicas": float64(replicas)}
	}
	if n.objects[kind.Plural] == nil {
		n.objects[kind.Plural] = map[string]kube.Obj{}
	}
	n.objects[kind.Plural][name] = stored
	n.applied++
	return stored, nil
}

func (n *fakeNamespace) Delete(_ context.Context, kind kube.Kind, name string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	delete(n.objects[kind.Plural], name)
	return nil
}

func matches(labels kube.Obj, selector string) bool {
	for _, term := range strings.Split(selector, ",") {
		k, v, _ := strings.Cut(term, "=")
		if labels[k] != v {
			return false
		}
	}
	return true
}

func (n *fakeNamespace) List(_ context.Context, kind kube.Kind, selector string) ([]kube.Obj, error) {
	n.mu.Lock()
	defer n.mu.Unlock()
	source := []kube.Obj{}
	if kind == kube.Pods {
		source = n.pods
	} else {
		for _, o := range n.objects[kind.Plural] {
			source = append(source, o)
		}
	}
	out := []kube.Obj{}
	for _, o := range source {
		labels, _ := kube.At(o, "metadata", "labels").(kube.Obj)
		if matches(labels, selector) {
			out = append(out, o)
		}
	}
	return out, nil
}

func (n *fakeNamespace) get(plural, name string) kube.Obj {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.objects[plural][name]
}

// of are the kinds the namespace has objects of for this data API
func (n *fakeNamespace) of(release string, id int64) []string {
	n.mu.Lock()
	defer n.mu.Unlock()
	name := dataapis.Name(release, id)
	out := []string{}
	for _, kind := range daKinds {
		if n.objects[kind.Plural][name] != nil {
			out = append(out, kind.Plural)
		}
	}
	return out
}

func dapiPodsConfig() config.DataAPIPods {
	return config.DataAPIPods{Namespace: "tabularia-scripts", Release: "dapitest", Image: "registry/tabularia-data-api:test",
		ImagePullPolicy: "IfNotPresent", GatewayURL: "http://gateway:8000", MaxDisk: "20Gi", NetworkPolicy: true,
		IngressFrom: []any{map[string]any{"podSelector": map[string]any{}}}}
}

func TestDataAPIReconcile(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	f.d.Config.DataAPI.Pods = dapiPodsConfig()
	f.d.Config.DataAPI.PollSeconds = 20
	f.d.Config.DataAPI.UsageRetentionDays = 30
	ns := newFakeNamespace()
	r := newDataAPIReconciler(f.d, ns)
	round := func() {
		t.Helper()
		if err := r.round(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	rel := f.d.Config.DataAPI.Pods.Release
	_, _ = f.db.Exec(f.ctx, `UPDATE datasources SET bucket = 'tabularia', key = 'datasets/customers.parquet' WHERE id = $1`, f.customers)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	name := dataapis.Name(rel, a.ID)
	// a draft has nothing in the cluster
	round()
	if got := ns.of(rel, a.ID); len(got) != 0 {
		t.Fatalf("a draft: %v", got)
	}
	// activated: its objects, one pod, no budget nor autoscaler; a pod token the database knows
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	round()
	if got := strings.Join(ns.of(rel, a.ID), ","); got != "secrets,services,networkpolicies,deployments,ingresses" {
		t.Fatalf("active: %s", got)
	}
	tokenOf := func() string {
		raw, _ := base64.StdEncoding.DecodeString(kube.Str(ns.get("secrets", name), "data", dataapis.SecretKey))
		return string(raw)
	}
	token := tokenOf()
	if !dataapiwire.WellFormed(token, dataapiwire.PodTokenPrefix) {
		t.Fatalf("%q", token)
	}
	if m := f.manifest(token, a.ID); m.State != "active" || len(m.Resources) != 1 {
		t.Fatalf("%+v", m)
	}
	dep := ns.get("deployments", name)
	mark := kube.Str(dep, "spec", "template", "metadata", "annotations", dataapis.AnnotationToken)
	if kube.Int(dep, "spec", "replicas") != 1 || mark != dataapiwire.Hash(token)[:16] {
		t.Fatalf("%v", dep)
	}
	// the disk from the parquet's size (the fake engine: 250 MB): 6 × 250 + 256 MB → 2Gi
	if disk := kube.At(dep, "spec", "template", "spec", "volumes").([]any)[0].(kube.Obj)["emptyDir"].(kube.Obj)["sizeLimit"]; disk != "2Gi" {
		t.Errorf("disk %v", disk)
	}
	if ing := ns.get("ingresses", name); kube.At(ing, "spec", "rules").([]any)[0].(kube.Obj)["http"].(kube.Obj)["paths"].([]any)[0].(kube.Obj)["path"] != "/data/"+a.Slug+"/" {
		t.Errorf("%v", ing)
	}
	// a second round changes nothing: the same token, no restart
	round()
	if tokenOf() != token {
		t.Fatal("the token changed without an activation")
	}
	// the status the page reads, with a pod the cluster has and its report
	ns.mu.Lock()
	ns.pods = []kube.Obj{{"metadata": kube.Obj{"name": name + "-abc", "labels": kube.Obj{dataapis.LabelInstance: rel,
		dataapis.LabelComponent: dataapis.Component, dataapis.LabelID: fmt.Sprint(a.ID)}},
		"spec": kube.Obj{"nodeName": "worker2"},
		"status": kube.Obj{"phase": "Running", "startTime": "2026-11-07T10:00:00Z", "containerStatuses": []any{kube.Obj{"ready": true, "restartCount": float64(1),
			"lastState": kube.Obj{"terminated": kube.Obj{"reason": "OOMKilled", "exitCode": float64(137)}}}}}}}
	ns.mu.Unlock()
	round()
	version := f.manifest(token, a.ID).Resources[0].Version
	report, _ := json.Marshal(dataapiwire.Usage{Pod: name + "-abc", Minutes: []dataapiwire.UsageMinute{},
		Status: &dataapiwire.PodStatus{Ready: true, State: "active", Resources: []dataapiwire.ResourceStatus{
			{Name: "customers", Version: version, Bytes: 1234}}}})
	if w := f.pod(token, podUsage, http.MethodPost, a.ID, report); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	st := decode[dataAPIStatusOut](t, f.call(f.viewer, dataAPIStatus, http.MethodGet, nil, path))
	if st.State != "active" || st.CheckedAt == nil || st.Wanted != 1 || st.Ready != 1 || st.Disk != "2Gi" || st.Error != "" ||
		len(st.Pods) != 1 || st.Pods[0].Condition != "ready" || st.Pods[0].Restarts != 1 || st.Pods[0].Node != "worker2" ||
		!strings.Contains(st.Pods[0].Message, "OOMKilled (exit 137)") || len(st.Pods[0].Resources) != 1 ||
		len(st.Resources) != 1 || st.Resources[0].Latest != version || st.Resources[0].Serving != 1 || st.Path != "/data/"+a.Slug+"/" {
		t.Fatalf("%+v", st)
	}
	// a pod that cannot pull its image: in error, and the data API says why
	ns.mu.Lock()
	ns.pods[0]["status"].(kube.Obj)["containerStatuses"] = []any{kube.Obj{"ready": false, "restartCount": float64(0),
		"state": kube.Obj{"waiting": kube.Obj{"reason": "ImagePullBackOff", "message": "Back-off pulling image"}}}}
	ns.mu.Unlock()
	round()
	st = decode[dataAPIStatusOut](t, f.call(f.viewer, dataAPIStatus, http.MethodGet, nil, path))
	if st.Pods[0].Condition != "error" || !strings.Contains(st.Error, "ImagePullBackOff") {
		t.Fatalf("%+v", st)
	}
	// someone without VIEW does not see it
	if w := f.call(f.outsider, dataAPIStatus, http.MethodGet, nil, path); w.Code != http.StatusNotFound {
		t.Errorf("%d", w.Code)
	}
	// two replicas: a budget; autoscaling: an autoscaler and no count of pods
	w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"runtime": map[string]any{"replicas": 2, "cpu": "250m",
		"memory": "256Mi"}}, path)
	if w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	round()
	if got := strings.Join(ns.of(rel, a.ID), ","); !strings.Contains(got, "poddisruptionbudgets") || strings.Contains(got, "horizontal") ||
		kube.Int(ns.get("deployments", name), "spec", "replicas") != 2 {
		t.Fatalf("two replicas: %s", got)
	}
	f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"runtime": map[string]any{"autoscale": true, "min_replicas": 1,
		"max_replicas": 3, "cpu": "250m", "memory": "256Mi"}}, path)
	round()
	if got := strings.Join(ns.of(rel, a.ID), ","); strings.Contains(got, "poddisruptionbudgets") || !strings.Contains(got, "horizontal") {
		t.Fatalf("autoscaling: %s", got)
	}
	// paused: the objects stay, with no pods, no budget, no autoscaler; the token stays
	if w := f.call(f.creator, pauseDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d", w.Code)
	}
	round()
	if got := strings.Join(ns.of(rel, a.ID), ","); got != "secrets,services,networkpolicies,deployments,ingresses" ||
		kube.Int(ns.get("deployments", name), "spec", "replicas") != 0 || tokenOf() != token {
		t.Fatalf("paused: %s", got)
	}
	// activated again: a new token, the previous one refused at once
	f.call(f.creator, activateDataAPI, http.MethodPost, nil, path)
	if w := f.pod(token, podManifest, http.MethodGet, a.ID, nil); w.Code != http.StatusUnauthorized {
		t.Errorf("the previous token after an activation: %d", w.Code)
	}
	round()
	if again := tokenOf(); again == token || f.manifest(again, a.ID).State != "active" ||
		kube.Str(ns.get("deployments", name), "spec", "template", "metadata", "annotations", dataapis.AnnotationToken) == mark {
		t.Fatal("no new token at the activation")
	}
	token = tokenOf()
	// the Secret gone by hand: a new token, the pods with it
	_ = ns.Delete(f.ctx, kube.Secrets, name)
	round()
	if tokenOf() == token || tokenOf() == "" {
		t.Fatal("no new Secret")
	}
	// what the cluster refuses is said on the page, the rest goes on
	ns.refuse["ingresses"] = &kube.Error{Status: 403, Message: "Kubernetes answered 403: ingresses is forbidden"}
	round()
	st = decode[dataAPIStatusOut](t, f.call(f.viewer, dataAPIStatus, http.MethodGet, nil, path))
	if !strings.Contains(st.Error, "the Ingress") || !strings.Contains(st.Error, "403") {
		t.Errorf("%+v", st.Error)
	}
	delete(ns.refuse, "ingresses")
	// an orphan: objects of a data API the database does not have
	orphan := dataapis.Secret(f.d.Config.DataAPI.Pods, 987654321, "tdp_orphan")
	_, _ = ns.Apply(f.ctx, kube.Secrets, dataapis.Name(rel, 987654321), orphan)
	_, _ = ns.Apply(f.ctx, kube.Services, dataapis.Name(rel, 987654321), dataapis.Service(f.d.Config.DataAPI.Pods, 987654321))
	// another release's object, which is not this loop's
	otherCfg := f.d.Config.DataAPI.Pods
	otherCfg.Release = "someone-else"
	_, _ = ns.Apply(f.ctx, kube.Secrets, dataapis.Name("someone-else", a.ID), dataapis.Secret(otherCfg, a.ID, "tdp_x"))
	round()
	if got := ns.of(rel, 987654321); len(got) != 0 {
		t.Errorf("orphans left: %v", got)
	}
	if ns.get("secrets", dataapis.Name("someone-else", a.ID)) == nil {
		t.Error("another release's Secret removed")
	}
	// deleted: its objects go, and what was kept of its pods
	if w := f.call(f.creator, deleteDataAPI, http.MethodDelete, nil, path); w.Code != http.StatusNoContent && w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	round()
	if got := ns.of(rel, a.ID); len(got) != 0 {
		t.Errorf("deleted, still: %v", got)
	}
	var n int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM data_api_pods WHERE data_api_id = $1`, a.ID).Scan(&n)
	if n != 0 {
		t.Errorf("%d pods kept of a deleted data API", n)
	}
}

// A creator whose account goes takes their data APIs with them (the rows cascade): the loop
// removes their objects; and with the feature switched off, every object goes.
func TestDataAPIReconcileCascadeAndOff(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	f.d.Config.DataAPI.Pods = dapiPodsConfig()
	f.d.Config.DataAPI.UsageRetentionDays = 30
	ns := newFakeNamespace()
	r := newDataAPIReconciler(f.d, ns)
	rel := f.d.Config.DataAPI.Pods.Release
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	b, _ := f.create(f.manager, f.folder, map[string]any{"name": "ERP", "slug": f.slug("erp"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	for _, x := range []dapiDetail{a, b} {
		if w := f.call(f.admin, activateDataAPI, http.MethodPost, nil, map[string]string{"api_id": dapiID(x.ID)}); w.Code != http.StatusOK {
			t.Fatalf("%d %s", w.Code, w.Body.String())
		}
	}
	if err := r.round(f.ctx); err != nil {
		t.Fatal(err)
	}
	if len(ns.of(rel, a.ID)) == 0 || len(ns.of(rel, b.ID)) == 0 {
		t.Fatal("not made")
	}
	_, _ = f.db.Exec(f.ctx, `DELETE FROM audit_logs WHERE actor_id = $1`, f.creator.ID)
	_, _ = f.db.Exec(f.ctx, `DELETE FROM permissions WHERE user_id = $1`, f.creator.ID)
	if _, err := f.db.Exec(f.ctx, `DELETE FROM users WHERE id = $1`, f.creator.ID); err != nil {
		t.Fatal(err)
	}
	if err := r.round(f.ctx); err != nil {
		t.Fatal(err)
	}
	if got := ns.of(rel, a.ID); len(got) != 0 {
		t.Errorf("the creator gone, still: %v", got)
	}
	if len(ns.of(rel, b.ID)) == 0 {
		t.Error("another creator's data API removed")
	}
	f.d.Config.DataAPI.Enabled = false
	if err := r.round(f.ctx); err != nil {
		t.Fatal(err)
	}
	if got := ns.of(rel, b.ID); len(got) != 0 {
		t.Errorf("switched off, still: %v", got)
	}
	f.d.Config.DataAPI.Enabled = true
}

// The usage past its retention goes at the next round; the rest stays.
func TestDataAPIUsageRetention(t *testing.T) {
	f := newDapiFixture(t)
	dapiFakeEngine(t, f)
	f.d.Config.DataAPI.Pods = dapiPodsConfig()
	f.d.Config.DataAPI.UsageRetentionDays = 3
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	expires := time.Now().Add(30 * 24 * time.Hour).UTC().Format(time.RFC3339)
	key := decode[dapiKeyAnswer](t, f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "CRM", "expires_at": expires},
		map[string]string{"api_id": dapiID(a.ID)})).Key
	now := time.Now().UTC().Truncate(time.Minute)
	for _, age := range []time.Duration{time.Minute, 2 * 24 * time.Hour, 4 * 24 * time.Hour, 40 * 24 * time.Hour} {
		if _, err := f.db.Exec(f.ctx, `INSERT INTO data_api_usage (data_api_id, key_id, minute, requests, rows, errors, throttled)
			VALUES ($1, $2, $3, 5, 50, 1, 0)`, a.ID, key.ID, now.Add(-age)); err != nil {
			t.Fatal(err)
		}
	}
	if err := newDataAPIReconciler(f.d, newFakeNamespace()).round(f.ctx); err != nil {
		t.Fatal(err)
	}
	var n int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM data_api_usage WHERE data_api_id = $1`, a.ID).Scan(&n)
	if n != 2 {
		t.Errorf("%d rows kept, want 2", n)
	}
	// the page's traffic: the last day, by hour
	st := decode[dataAPIStatusOut](t, f.call(f.viewer, dataAPIStatus, http.MethodGet, nil, map[string]string{"api_id": dapiID(a.ID)}))
	if st.Traffic.Hours != 24 || st.Traffic.BucketMinutes != 60 || st.Traffic.Totals.Requests != 5 || st.Traffic.Totals.Rows != 50 ||
		len(st.Traffic.Points) != 1 || st.Pods == nil || len(st.Pods) != 0 {
		t.Errorf("%+v", st)
	}
}

// A pod's report: kept for its own data API only, within the most pods, its texts cut.
func TestDataAPIPodReports(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	report := dataapiwire.PodStatus{Ready: false, State: "active", Error: strings.Repeat("x", 2000),
		Resources: []dataapiwire.ResourceStatus{{Name: "customers", Preparing: "2026-11-07T10:00:00Z"}}}
	for _, bad := range []string{"", "Not A Pod", strings.Repeat("a", 300), "pod/../x"} {
		if ok, err := dataapis.RecordPodReport(f.ctx, f.db, a.ID, bad, report); ok || err != nil {
			t.Errorf("%q: %v %v", bad, ok, err)
		}
	}
	for i := 0; i < dataapis.MaxPodRows+3; i++ {
		ok, err := dataapis.RecordPodReport(f.ctx, f.db, a.ID, fmt.Sprintf("crm-%d", i), report)
		if err != nil || ok != (i < dataapis.MaxPodRows) {
			t.Fatalf("%d: %v %v", i, ok, err)
		}
	}
	// a pod already there still updates its row
	report.Ready = true
	if ok, _ := dataapis.RecordPodReport(f.ctx, f.db, a.ID, "crm-0", report); !ok {
		t.Error("an existing pod refused")
	}
	_, _, pods, err := dataapis.Status(f.ctx, f.db, a.ID)
	if err != nil || len(pods) != dataapis.MaxPodRows || pods[0].Report == nil || !pods[0].Report.Ready ||
		len([]rune(pods[0].Report.Error)) != dataapiwire.MaxStatusText+1 || pods[0].Cluster != nil {
		t.Fatalf("%d %v", len(pods), err)
	}
	// the loop's listing is the cluster's word: the pods it does not have go
	if err := dataapis.SetPodsCluster(f.ctx, f.db, a.ID, []dataapis.PodCluster{{Name: "crm-0", Phase: "Running", Ready: true}}); err != nil {
		t.Fatal(err)
	}
	if _, _, pods, _ = dataapis.Status(f.ctx, f.db, a.ID); len(pods) != 1 || pods[0].Cluster == nil || pods[0].Report == nil {
		t.Fatalf("%+v", pods)
	}
}
