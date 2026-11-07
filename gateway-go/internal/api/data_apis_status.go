package api

// GET /data-apis/{api_id}/status: how a data API's pods are, for its page (docs/specs/
// data-apis.md, «The object»): what the reconcile loop last saw in the cluster (the pods wanted
// and ready, the disk each has, what the cluster refused), each pod as the cluster sees it and
// as it reports itself (ready, preparing, in error; the data version each resource serves), the
// version each resource's datasource has now, and the traffic of the last hours. VIEW on the
// folder, as the page; no key, no person's data.

import (
	"net/http"
	"strconv"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("data_apis_status", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodGet, Pattern: "/data-apis/{api_id}/status", Handler: server.Authed(d.Auth,
			"/data-apis/{api_id}/status", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { dataAPIStatus(d, w, r, p) })}}
	})
}

// The conditions the page gives a pod.
const (
	daPodReady     = "ready"     // it answers
	daPodPreparing = "preparing" // it is preparing the data before it answers
	daPodStarting  = "starting"  // scheduled, pulling its image, not yet reporting
	daPodError     = "error"     // the cluster or the pod says something is wrong
	daPodStopping  = "stopping"  // a rollout, a scale-in, a pause
)

type dataAPIPodOut struct {
	Name        string  `json:"name"`
	Condition   string  `json:"condition"`
	Phase       string  `json:"phase"`
	Ready       bool    `json:"ready"`
	Restarts    int64   `json:"restarts"`
	Node        string  `json:"node"`
	Started     *string `json:"started"`
	Reason      string  `json:"reason"`
	Message     string  `json:"message"`
	Terminating bool    `json:"terminating"`
	// the pod's own report: when it last sent one, the state of its manifest, its last error
	// and what it serves
	ReportedAt *string                      `json:"reported_at"`
	State      string                       `json:"state"`
	Error      string                       `json:"error"`
	Resources  []dataapiwire.ResourceStatus `json:"resources"`
}

type dataAPIServedOut struct {
	Name string `json:"name"`
	// the version of the data its datasource has now, "" when it has none
	Latest string `json:"latest"`
	// the pods that answer with it
	Serving int `json:"serving"`
}

type dataAPITrafficOut struct {
	Hours         int                     `json:"hours"`
	BucketMinutes int                     `json:"bucket_minutes"`
	Points        []dataapis.UsagePoint   `json:"points"`
	Totals        dataAPITrafficTotalsOut `json:"totals"`
}

type dataAPITrafficTotalsOut struct {
	Requests  int64 `json:"requests"`
	Rows      int64 `json:"rows"`
	Errors    int64 `json:"errors"`
	Throttled int64 `json:"throttled"`
}

type dataAPIStatusOut struct {
	State string `json:"state"`
	// when the reconcile loop last looked; nil: not yet (just activated, or a draft)
	CheckedAt *string            `json:"checked_at"`
	Wanted    int                `json:"wanted"`
	Ready     int                `json:"ready"`
	Updated   int                `json:"updated"`
	Disk      string             `json:"disk"`
	Error     string             `json:"error"`
	Pods      []dataAPIPodOut    `json:"pods"`
	Resources []dataAPIServedOut `json:"resources"`
	Traffic   dataAPITrafficOut  `json:"traffic"`
	Poll      int                `json:"poll_seconds"`
	Path      string             `json:"path"`
	// the host the Ingress publishes it on, "" for the app's own
	Host string `json:"host"`
}

// how far back the traffic goes, and in what buckets
var dataAPITrafficSpans = map[int]time.Duration{1: 5 * time.Minute, 24: time.Hour, 168: 6 * time.Hour}

func dataAPIStatus(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return
	}
	hours := 24
	if h, err := strconv.Atoi(r.URL.Query().Get("hours")); err == nil {
		if _, known := dataAPITrafficSpans[h]; known {
			hours = h
		}
	}
	ctx := r.Context()
	cluster, checked, pods, err := dataapis.Status(ctx, d.DB, a.ID)
	if err != nil {
		fail(d, w, "data apis: the status", err)
		return
	}
	out := dataAPIStatusOut{State: a.State, CheckedAt: isoPtr(checked), Pods: []dataAPIPodOut{}, Resources: []dataAPIServedOut{},
		Poll: d.Config.DataAPI.PollSeconds, Path: "/data/" + a.Slug + "/", Host: d.Config.DataAPI.Pods.IngressHost}
	if cluster != nil {
		out.Wanted, out.Ready, out.Updated, out.Disk, out.Error = cluster.Wanted, cluster.Ready, cluster.Updated, cluster.Disk, cluster.Error
	}
	// a report is fresh for two polls and a margin: a pod that no longer reports says nothing
	fresh := time.Duration(2*max(d.Config.DataAPI.PollSeconds, 5)+10) * time.Second
	now := dataapis.Now()
	served := map[string]map[string]int{} // resource → version → pods
	for _, row := range pods {
		if row.Cluster == nil && (row.ReportedAt == nil || now.Sub(*row.ReportedAt) > fresh) {
			continue // a pod the cluster no longer has
		}
		po := dataAPIPodOut{Name: row.Pod, Resources: []dataapiwire.ResourceStatus{}}
		if c := row.Cluster; c != nil {
			po.Phase, po.Ready, po.Restarts, po.Node, po.Started = c.Phase, c.Ready, c.Restarts, c.Node, isoPtr(c.Started)
			po.Reason, po.Message, po.Terminating = c.Reason, c.Message, c.Terminating
		}
		reporting := row.Report != nil && row.ReportedAt != nil && now.Sub(*row.ReportedAt) <= fresh
		if reporting {
			rep := row.Report
			po.ReportedAt, po.State, po.Error = isoPtr(row.ReportedAt), rep.State, rep.Error
			po.Resources = append(po.Resources, rep.Resources...)
			if row.Cluster == nil {
				po.Ready = rep.Ready
			}
			for _, res := range rep.Resources {
				if res.Version == "" || !po.Ready {
					continue
				}
				if served[res.Name] == nil {
					served[res.Name] = map[string]int{}
				}
				served[res.Name][res.Version]++
			}
		}
		po.Condition = podCondition(po, reporting)
		out.Pods = append(out.Pods, po)
	}
	def, err := dataapis.CurrentDefinition(ctx, d.DB, a)
	if err != nil {
		fail(d, w, "data apis: the definition", err)
		return
	}
	ids := []int64{}
	for _, res := range def.Resources {
		ids = append(ids, res.Source.ID)
	}
	files, err := dataapis.DataFiles(ctx, d.DB, ids)
	if err != nil {
		fail(d, w, "data apis: the data files", err)
		return
	}
	for _, res := range def.Resources {
		so := dataAPIServedOut{Name: res.Name}
		if f, ok := files[res.Source.ID]; ok && f.Key != "" {
			so.Latest = f.Version()
			so.Serving = served[res.Name][so.Latest]
		}
		out.Resources = append(out.Resources, so)
	}
	bucket := dataAPITrafficSpans[hours]
	points, err := dataapis.UsageSeries(ctx, d.DB, a.ID, now.Add(-time.Duration(hours)*time.Hour).Truncate(bucket), bucket)
	if err != nil {
		fail(d, w, "data apis: the traffic", err)
		return
	}
	out.Traffic = dataAPITrafficOut{Hours: hours, BucketMinutes: int(bucket / time.Minute), Points: points}
	for _, pt := range points {
		t := &out.Traffic.Totals
		t.Requests, t.Rows, t.Errors, t.Throttled = t.Requests+pt.Requests, t.Rows+pt.Rows, t.Errors+pt.Errors, t.Throttled+pt.Throttled
	}
	w.Header().Set("Cache-Control", "no-store")
	server.JSON(w, http.StatusOK, out)
}

// podCondition is one word for the page: ready, preparing, starting, error, stopping.
func podCondition(p dataAPIPodOut, reporting bool) string {
	switch {
	case p.Terminating:
		return daPodStopping
	case p.Reason != "" && p.Reason != "ContainerCreating" && p.Reason != "PodInitializing":
		return daPodError
	case p.Ready:
		return daPodReady
	case reporting && p.Error != "":
		return daPodError
	case reporting:
		return daPodPreparing
	}
	return daPodStarting
}
