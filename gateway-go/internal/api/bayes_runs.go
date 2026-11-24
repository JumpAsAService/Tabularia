package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The runs the editor's assistant may read (docs/specs/bayes-assistants.md, «Reading a run»). The editor has no run: a run belongs
// to a node of a flow that pins the model. Two routes, both under the report's rights (VIEW on the flow's folder and the flow
// opens for the person; whoever may not gets a 404, as if the run were not there):
//
//	GET /bayes-models/{model_id}/runs                    the nodes that pin the model, each with its latest sampling run
//	GET /bayes-models/{model_id}/runs/{run_id}/facts     that run's sampler facts and each node's extremes
//
// What leaves is the rule of the whole file: **the sections `sampler` and `quantity` of the run's results and no other** (never
// `summary`, `data`, `model`, a chart's rows or a draw), asked of the engine by name and kept to numbers. The prior predictive check
// is no sampling run: it is left out of the list and refused by the facts. A route of the Go gateway only.

func init() {
	register("bayes_runs", func(d *Deps) []server.Route {
		route := func(pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: http.MethodGet, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route("/bayes-models/{model_id}/runs", listBayesModelRuns),
			route("/bayes-models/{model_id}/runs/{run_id}/facts", getBayesRunFacts),
		}
	})
}

// bayesRunSections are the only sections of a run's results the assistant's route asks the engine for.
var bayesRunSections = []any{"sampler", "quantity"}

// bayesRunNodes is the most nodes' extremes one answer holds (the agent reads as many).
const bayesRunNodes = 400

// bayesSamplerKeys are the sampler's facts the answer carries (the row `all` of the section), as numbers.
var bayesSamplerKeys = []string{"divergences", "treedepth_hits", "treedepth_max", "e_bfmi", "stepsize", "accept_stat_mean",
	"n_leapfrog_mean", "seconds_warmup", "seconds_sampling", "draws"}

// bayesRun is a finished run of a Bayesian node as the routes read it: what the run kept (info is the run's `script`).
type bayesRun struct {
	id         int64
	info       pyjson.Object
	bucket     string
	resultsKey string
	finishedAt *time.Time
}

// version is the saved model's version the run used (`bayes_model.version`), 0 when the run did not keep it.
func (r *bayesRun) version() int64 {
	v, _ := pyjson.Obj(r.info.Value("bayes_model")).Value("version").(int64)
	return v
}

func (r *bayesRun) trial() bool {
	t, _ := r.info.Value("trial").(bool)
	return t
}

// sampling: the run is of this model and is no prior predictive check.
func (r *bayesRun) sampling(modelID int64) bool {
	if id, _ := pyjson.Obj(r.info.Value("bayes_model")).Value("id").(int64); id != modelID {
		return false
	}
	return r.info.Value("check") != bayesCheckPrior
}

// latestBayesRun is the node's latest finished sampling run of the model with results kept, nil when it has none.
func latestBayesRun(ctx context.Context, d *Deps, flowID int64, nodeID any, modelID int64) (*bayesRun, error) {
	key, _ := pyjson.Dumps(pyjson.Object{{Key: "node_id", Value: nodeID}}, pyjson.Options{EnsureASCII: true})
	rows, err := d.DB.Query(ctx, `SELECT id, script, output_bucket, model_results_key, finished_at FROM runs
		WHERE flow_id = $1 AND kind = 'model' AND status = 'SUCCESS' AND model_results_key IS NOT NULL AND model_results_key <> ''
		AND starts_with(script, $2) ORDER BY id DESC LIMIT 50`, flowID, key[:len(key)-1]+",")
	if err != nil {
		return nil, bayesRunsErr(err)
	}
	defer rows.Close()
	for rows.Next() {
		run, err := scanBayesRun(rows.Scan)
		if err != nil {
			return nil, err
		}
		if run != nil && run.sampling(modelID) {
			return run, nil
		}
	}
	return nil, bayesRunsErr(rows.Err())
}

// bayesRunsErr: a database without the models' migration (0044) has no results, so no run.
func bayesRunsErr(err error) error {
	if isUndefinedColumn(err) {
		return nil
	}
	return err
}

func scanBayesRun(scan func(...any) error) (*bayesRun, error) {
	var run bayesRun
	var script, bucket, key *string
	if err := scan(&run.id, &script, &bucket, &key, &run.finishedAt); err != nil {
		return nil, err
	}
	v, err := pyjson.Loads(str0(script))
	if err != nil {
		return nil, nil
	}
	if run.info = pyjson.Obj(v); run.info == nil {
		return nil, nil
	}
	run.bucket, run.resultsKey = str0(bucket), str0(key)
	return &run, nil
}

// bayesFlowOpens is the person's right to the flow's runs, the report's: VIEW on its folder and the flow opens for them. The
// definition is returned for the node's label.
func bayesFlowOpens(q *request, flowID int64) (string, bool, error) {
	var project int64
	var definition string
	err := q.d.DB.QueryRow(q.ctx, `SELECT project_id, definition FROM flows WHERE id = $1`, flowID).Scan(&project, &definition)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return "", false, nil
		}
		return "", false, err
	}
	ok, err := q.c.Has(q.ctx, project, perm.View)
	if err != nil || !ok {
		return "", false, err
	}
	ref, err := q.acc.EnsureCanOpen(q.ctx, definition)
	if err != nil || ref != nil {
		return "", false, err
	}
	return definition, true, nil
}

type bayesRunListed struct {
	FlowID        int64      `json:"flow_id"`
	FlowName      string     `json:"flow_name"`
	NodeID        string     `json:"node_id"`
	NodeLabel     string     `json:"node_label"`
	PinnedVersion int64      `json:"pinned_version"`
	RunID         *int64     `json:"run_id"`
	Version       *int64     `json:"version"`
	Trial         bool       `json:"trial"`
	FinishedAt    *time.Time `json:"finished_at"`
}

// listBayesModelRuns is GET /bayes-models/{model_id}/runs: the nodes of flows the person may open that pin the model, each with
// its latest sampling run (`run_id` null when it has none: a node never run, or only checked). A flow the person may not open is
// counted in `hidden`, not named.
func listBayesModelRuns(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	m, _, ok := bayesVisible(d, w, r, q.c, p, ids[0])
	if !ok {
		return
	}
	uses, err := bayesmodels.Using(q.ctx, d.DB, m.ID)
	if err != nil {
		q.fail(err)
		return
	}
	out := struct {
		Runs   []bayesRunListed `json:"runs"`
		Hidden int              `json:"hidden"`
	}{Runs: []bayesRunListed{}}
	definitions := map[int64]string{}
	opens := map[int64]bool{}
	hidden := map[int64]bool{}
	for _, u := range uses {
		ok, seen := opens[u.FlowID]
		if !seen {
			def, can, err := bayesFlowOpens(q, u.FlowID)
			if err != nil {
				q.fail(err)
				return
			}
			opens[u.FlowID], definitions[u.FlowID], ok = can, def, can
		}
		if !ok {
			hidden[u.FlowID] = true
			continue
		}
		item := bayesRunListed{FlowID: u.FlowID, FlowName: u.FlowName, NodeID: u.NodeID, PinnedVersion: u.Version}
		if node := modelNode(definitions[u.FlowID], u.NodeID); node != nil {
			item.NodeLabel, _ = pyjson.Obj(pyjson.Or(node.Value("data"), pyjson.Object{})).Value("label").(string)
		}
		run, err := latestBayesRun(q.ctx, d, u.FlowID, u.NodeID, m.ID)
		if err != nil {
			q.fail(err)
			return
		}
		if run != nil {
			v := run.version()
			item.RunID, item.Version, item.Trial, item.FinishedAt = &run.id, &v, run.trial(), run.finishedAt
		}
		out.Runs = append(out.Runs, item)
	}
	out.Hidden = len(hidden)
	server.JSON(w, http.StatusOK, out)
}

type bayesRunNode struct {
	Node       string   `json:"node"`
	RhatMax    *float64 `json:"rhat_max,omitempty"`
	ESSBulkMin *float64 `json:"ess_bulk_min,omitempty"`
	ESSTailMin *float64 `json:"ess_tail_min,omitempty"`
}

// bayesRunFacts is the `run` the assistant's request carries: only the version, the sampler's numbers and each node's extremes.
// `node` is the graph's node id and nothing else (the extremes over all the node's elements).
type bayesRunFacts struct {
	RunID   int64              `json:"run_id"`
	FlowID  int64              `json:"flow_id"`
	NodeID  string             `json:"node_id"`
	Trial   bool               `json:"trial"`
	Version int64              `json:"version"`
	Sampler map[string]float64 `json:"sampler"`
	Nodes   []bayesRunNode     `json:"nodes"`
}

// getBayesRunFacts is GET /bayes-models/{model_id}/runs/{run_id}/facts. 404 for a model the person may not see, a run that is not
// a finished sampling run of a node of this model in a flow they may open, or results no longer kept.
func getBayesRunFacts(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id", "run_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	m, _, ok := bayesVisible(d, w, r, q.c, p, ids[0])
	if !ok {
		return
	}
	notFound := func() { detail(w, http.StatusNotFound, "This model has no such run to read") }
	var flowID int64
	var script, bucket, key, status *string
	var finished *time.Time
	err := d.DB.QueryRow(q.ctx, `SELECT flow_id, script, output_bucket, model_results_key, status, finished_at FROM runs
		WHERE id = $1 AND kind = 'model'`, ids[1]).Scan(&flowID, &script, &bucket, &key, &status, &finished)
	if errors.Is(err, pgx.ErrNoRows) || isUndefinedColumn(err) {
		notFound()
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	run := &bayesRun{id: ids[1], bucket: str0(bucket), resultsKey: str0(key), finishedAt: finished}
	if v, err := pyjson.Loads(str0(script)); err == nil {
		run.info = pyjson.Obj(v)
	}
	if run.info == nil || str0(status) != "SUCCESS" || run.resultsKey == "" || !run.sampling(m.ID) {
		notFound()
		return
	}
	definition, can, err := bayesFlowOpens(q, flowID)
	if err != nil {
		q.fail(err)
		return
	}
	nodeID, hasNode := run.info.Get("node_id")
	if !can || !hasNode || modelNode(definition, nodeID) == nil {
		notFound()
		return
	}
	if run.bucket == "" {
		run.bucket = d.Config.EngineBucket
	}
	rows, status404, err := d.bayesRunRows(q.ctx, run)
	switch {
	case err != nil:
		q.fail(err)
		return
	case status404 != 0:
		detail(w, status404, "The results of this run could not be read: they are gone, or the engine refused them")
		return
	}
	node, _ := nodeID.(string)
	facts := bayesRunFactsOf(rows, d.bayesRunNodeOf(q.ctx, m, run.version()))
	facts.RunID, facts.FlowID, facts.NodeID, facts.Trial, facts.Version = run.id, flowID, node, run.trial(), run.version()
	server.JSON(w, http.StatusOK, facts)
}

// bayesRunRows reads the sections `sampler` and `quantity` of the run's results, kept by the engine, and no other. The second
// result is the status to answer when the engine does not give them (0: it did).
func (d *Deps) bayesRunRows(ctx context.Context, run *bayesRun) ([]resultRow, int, error) {
	cctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/reports/model/rows", pyjson.Object{{Key: "bucket", Value: run.bucket},
		{Key: "results_key", Value: run.resultsKey}, {Key: "model", Value: bayescontract.Model},
		{Key: "params", Value: pyjson.Or(run.info.Value("params"), pyjson.Object{})}, {Key: "sections", Value: bayesRunSections}})
	if err != nil {
		return nil, 0, err
	}
	if res.StatusCode >= 400 {
		d.Log.Warn("bayes run rows refused by the engine", "status", res.StatusCode, "run", run.id, "detail", engineDetail(raw))
		return nil, http.StatusNotFound, nil
	}
	var got struct {
		Rows []resultRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		return nil, 0, err
	}
	return got.Rows, 0, nil
}

// bayesRunNodeOf is the Stan identifier -> the graph's node id of the version the run used, from the mapping stored with it. A
// version that is not there, or does not read, gives an empty table: then no node is named.
func (d *Deps) bayesRunNodeOf(ctx context.Context, m *bayesmodels.Model, version int64) map[string]string {
	out := map[string]string{}
	v, err := bayesmodels.GetVersion(ctx, d.DB, m, version)
	if err != nil || v == nil {
		return out
	}
	var mapping []bayescontract.MappingEntry
	if json.Unmarshal([]byte(v.Mapping), &mapping) != nil {
		return out
	}
	for _, e := range mapping {
		if e.NodeID != nil && *e.NodeID != "" {
			out[e.Stan] = *e.NodeID
		}
	}
	return out
}

// bayesRunFactsOf keeps of the rows the sampler's numbers of the row `all` (the chains counted by their own rows) and each node's
// extremes, in the order of the results, whatever else the engine sent. `nodeOf` turns the identifier a quantity's rows carry into
// the graph's node id: the extremes of every identifier of a node are one (the largest R-hat, the smallest ESS), and a level that
// names no node of the mapping (a unit's label, a value of a group column) is dropped, never sent: levels are the person's data.
func bayesRunFactsOf(rows []resultRow, nodeOf map[string]string) *bayesRunFacts {
	f := &bayesRunFacts{Sampler: map[string]float64{}, Nodes: []bayesRunNode{}}
	wanted := map[string]bool{}
	for _, k := range bayesSamplerKeys {
		wanted[k] = true
	}
	chains := map[string]bool{}
	at := map[string]int{}
	for _, r := range rows {
		if r.Level == nil {
			continue
		}
		switch r.Section {
		case "sampler":
			if *r.Level != "all" {
				chains[*r.Level] = true
			} else if r.Value != nil && wanted[r.Stat] {
				f.Sampler[r.Stat] = *r.Value
			}
		case "quantity":
			if r.Value == nil || (r.Stat != "rhat_max" && r.Stat != "ess_bulk_min" && r.Stat != "ess_tail_min") {
				continue
			}
			node, mapped := nodeOf[*r.Level]
			if !mapped {
				continue
			}
			i, seen := at[node]
			if !seen {
				if len(f.Nodes) >= bayesRunNodes {
					continue
				}
				i = len(f.Nodes)
				at[node] = i
				f.Nodes = append(f.Nodes, bayesRunNode{Node: node})
			}
			v := *r.Value
			switch r.Stat {
			case "rhat_max":
				if f.Nodes[i].RhatMax == nil || v > *f.Nodes[i].RhatMax {
					f.Nodes[i].RhatMax = &v
				}
			case "ess_bulk_min":
				if f.Nodes[i].ESSBulkMin == nil || v < *f.Nodes[i].ESSBulkMin {
					f.Nodes[i].ESSBulkMin = &v
				}
			default:
				if f.Nodes[i].ESSTailMin == nil || v < *f.Nodes[i].ESSTailMin {
					f.Nodes[i].ESSTailMin = &v
				}
			}
		}
	}
	if len(chains) > 0 {
		f.Sampler["chains"] = float64(len(chains))
	}
	return f
}
