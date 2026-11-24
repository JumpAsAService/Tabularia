package api

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// The runs the Bayesian editor's assistant may read (lot A4): the list of a model's nodes with their latest sampling run, a run's
// facts, the report's rights, and the engine asked for the sections `sampler` and `quantity` and no other.

func TestBayesRunRoutesAreRegistered(t *testing.T) {
	routes := map[string]bool{}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		routes[r.Method+" "+r.Pattern] = true
	}
	for _, want := range []string{"GET /bayes-models/{model_id}/runs", "GET /bayes-models/{model_id}/runs/{run_id}/facts"} {
		if !routes[want] {
			t.Errorf("missing route %s", want)
		}
	}
}

// the rows of a Bayesian run's results as the engine keeps them; the fake sends the summary and the data too, as a careless engine would
const bayesRunEngineRows = `{"rows": [
 {"section": "summary", "level": "mu", "level_2": null, "stat": "mean", "value": 41.5, "text": null, "x0": null},
 {"section": "data", "level": "y", "level_2": null, "stat": "mean", "value": 99.5, "text": null, "x0": null},
 {"section": "sampler", "level": "1", "level_2": null, "stat": "divergences", "value": 1, "text": null, "x0": null},
 {"section": "sampler", "level": "2", "level_2": null, "stat": "divergences", "value": 2, "text": null, "x0": null},
 {"section": "sampler", "level": "all", "level_2": null, "stat": "divergences", "value": 3, "text": null, "x0": null},
 {"section": "sampler", "level": "all", "level_2": null, "stat": "e_bfmi", "value": 0.31, "text": null, "x0": null},
 {"section": "sampler", "level": "all", "level_2": null, "stat": "draws", "value": 1000, "text": null, "x0": null},
 {"section": "sampler", "level": "all", "level_2": null, "stat": "seconds_sampling", "value": 12.5, "text": null, "x0": null},
 {"section": "sampler", "level": "all", "level_2": null, "stat": "secret_stat", "value": 7, "text": null, "x0": null},
 {"section": "quantity", "level": "beta_1", "level_2": null, "stat": "rhat_max", "value": 1.02, "text": null, "x0": null},
 {"section": "quantity", "level": "beta_1", "level_2": null, "stat": "ess_bulk_min", "value": 410, "text": null, "x0": null},
 {"section": "quantity", "level": "beta_1", "level_2": null, "stat": "ess_tail_min", "value": null, "text": null, "x0": null},
 {"section": "quantity", "level": "beta_1", "level_2": null, "stat": "mean", "value": 5, "text": null, "x0": null},
 {"section": "quantity", "level": "a_g", "level_2": null, "stat": "rhat_max", "value": 1.08, "text": null, "x0": null},
 {"section": "quantity", "level": "a_g", "level_2": null, "stat": "ess_bulk_min", "value": 120, "text": null, "x0": null},
 {"section": "quantity", "level": "a_g_raw", "level_2": null, "stat": "rhat_max", "value": 1.2, "text": null, "x0": null},
 {"section": "quantity", "level": "a_g_raw", "level_2": null, "stat": "ess_bulk_min", "value": 300, "text": null, "x0": null},
 {"section": "quantity", "level": "a_g[Milano]", "level_2": "Milano", "stat": "rhat_max", "value": 1.5, "text": null, "x0": null},
 {"section": "quantity", "level": "Milano", "level_2": null, "stat": "rhat_max", "value": 1.6, "text": null, "x0": null}
]}`

func TestBayesRunRoutes(t *testing.T) {
	w := newBayesWorld(t)
	var mu sync.Mutex
	var asked []string
	srv := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		asked = append(asked, r.URL.Path+" "+string(b))
		mu.Unlock()
		rw.Header().Set("Content-Type", "application/json")
		_, _ = rw.Write([]byte(bayesRunEngineRows))
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	w.d.Engine, w.d.Config, w.d.Panel = e, cfg, settings.New(cfg, false, log)
	var migrated bool
	if err := w.db.QueryRow(w.ctx, `SELECT EXISTS (SELECT 1 FROM information_schema.columns
		WHERE table_name = 'runs' AND column_name = 'model_results_key')`).Scan(&migrated); err != nil || !migrated {
		t.Skip("the models' migration (0044) is not applied")
	}

	model := w.create("Runs model", w.folder, w.editor)
	otherModel := w.create("Runs other model", w.folder, w.editor)
	secret := w.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bmruns-secret-%d", time.Now().UnixNano()))
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM runs WHERE flow_id IN (SELECT id FROM flows WHERE project_id = $1)`, secret)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM flows WHERE project_id = $1`, secret)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM permissions WHERE project_id = $1`, secret)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM projects WHERE id = $1`, secret)
	})
	observer := &auth.User{}
	{
		email := fmt.Sprintf("bmr-observer-%d@x.test", time.Now().UnixNano())
		id := w.one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, 'observer', 'x', true, false, true, false, now()) RETURNING id`, email)
		*observer = auth.User{ID: id, Email: email, IsActive: true, IsObserver: true}
		w.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'view') RETURNING id`, w.folder, id)
		t.Cleanup(func() {
			_, _ = w.db.Exec(w.ctx, `DELETE FROM permissions WHERE user_id = $1`, id)
			_, _ = w.db.Exec(w.ctx, `DELETE FROM audit_logs WHERE actor_id = $1`, id)
			_, _ = w.db.Exec(w.ctx, `DELETE FROM users WHERE id = $1`, id)
		})
	}
	flow := func(name string, project int64) int64 {
		def := fmt.Sprintf(`{"nodes": [{"id": "bm1", "type": "bayes_model", "data": {"label": "Prices «%s»", "bayesModelId": %d, "bayesModelVersion": 2}},
			{"id": "bm2", "type": "bayes_model", "data": {"label": "Never run", "bayesModelId": %d}}], "edges": []}`, name, model.ID, model.ID)
		return w.one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, 'duckdb', now(), now()) RETURNING id`, name, project, w.editor.ID, def)
	}
	open, closed := flow("runs open", w.folder), flow("runs closed", secret)
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM runs WHERE flow_id = $1`, open)
	})
	run := func(flowID int64, node string, modelID int64, version int, status, key string, extra string) int64 {
		script := fmt.Sprintf(`{"node_id": "%s", "model": "bayes_model", "params": {"sampler": {"chains": 2}}, "bayes_model": {"id": %d, "version": %d, "name": "x"}%s}`,
			node, modelID, version, extra)
		return w.one(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
			publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, script, model_results_key)
			VALUES ('model', $1, '', $2, $3, 'manual', '', 'data-prep', '', '', false, '[]', now(), now(), $4, $5) RETURNING id`,
			flowID, status, w.editor.ID, script, key)
	}
	old := run(open, "bm1", model.ID, 7, "SUCCESS", "out/old.parquet", `, "trial": false`)
	sampling := run(open, "bm1", model.ID, 1, "SUCCESS", "out/new.parquet", `, "trial": true`)
	_ = run(open, "bm1", model.ID, 2, "SUCCESS", "out/prior.parquet", `, "trial": true, "check": "prior_predictive"`) // the latest run, but a prior check
	_ = run(open, "bm1", model.ID, 2, "FAILURE", "", `, "trial": false`)
	foreign := run(open, "bm1", otherModel.ID, 2, "SUCCESS", "out/foreign.parquet", ``)
	closedRun := run(closed, "bm1", model.ID, 2, "SUCCESS", "out/closed.parquet", ``)

	// the stored mapping of version 1: a vector node n2, and a plate's node n3 that the program writes as two identifiers
	if _, err := w.db.Exec(w.ctx, `UPDATE bayes_model_versions SET mapping = $1 WHERE model_id = $2 AND version = 1`,
		`[{"stan":"beta_1","block":"parameters","node_id":"n2","kind":"x"},{"stan":"a_g","block":"parameters","node_id":"n3","kind":"x"},`+
			`{"stan":"a_g_raw","block":"parameters","node_id":"n3","kind":"x"},{"stan":"Sigma","block":"parameters","node_id":null,"kind":"x"}]`, model.ID); err != nil {
		t.Fatal(err)
	}
	type listOut struct {
		Runs []struct {
			FlowID    int64  `json:"flow_id"`
			NodeID    string `json:"node_id"`
			NodeLabel string `json:"node_label"`
			RunID     *int64 `json:"run_id"`
			Version   *int64 `json:"version"`
			Trial     bool   `json:"trial"`
		} `json:"runs"`
		Hidden int `json:"hidden"`
	}
	mid := map[string]string{"model_id": bmID(model.ID)}
	list := func(u *auth.User) (*httptest.ResponseRecorder, listOut) {
		rec := w.call(listBayesModelRuns, u, bayesCall{method: "GET", path: "/x", values: mid})
		var out listOut
		if rec.Code == 200 {
			if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
				t.Fatal(err)
			}
		}
		return rec, out
	}

	// the list: the nodes the person may open, each with its latest SAMPLING run (the prior check, the failure and another model's run pass by)
	for _, u := range []*auth.User{w.editor, w.viewer, w.admin, observer} {
		rec, out := list(u)
		w.want(rec, 200)
		if len(out.Runs) != 2 && u != w.admin {
			t.Fatalf("%s: %d nodes listed: %s", u.FullName, len(out.Runs), rec.Body)
		}
		if u == w.admin { // an administrator opens every flow
			if out.Hidden != 0 || len(out.Runs) != 4 {
				t.Errorf("admin: %d nodes, %d hidden", len(out.Runs), out.Hidden)
			}
			continue
		}
		first, second := out.Runs[0], out.Runs[1]
		if first.FlowID != open || first.NodeID != "bm1" || first.RunID == nil || *first.RunID != sampling || *first.Version != 1 || !first.Trial ||
			first.NodeLabel != "Prices «runs open»" {
			t.Errorf("%s: first %+v", u.FullName, first)
		}
		if second.NodeID != "bm2" || second.RunID != nil || second.Version != nil {
			t.Errorf("%s: a node never run: %+v", u.FullName, second)
		}
		if out.Hidden != 1 {
			t.Errorf("%s: %d hidden, want the closed flow", u.FullName, out.Hidden)
		}
	}
	if rec, _ := list(w.stranger); rec.Code != 404 { // no right on the model's folder: the model is not there for them
		t.Errorf("stranger: %d %s", rec.Code, rec.Body)
	}
	if rec := w.call(listBayesModelRuns, w.editor, bayesCall{method: "GET", path: "/x", values: map[string]string{"model_id": "999999"}}); rec.Code != 404 {
		t.Errorf("a model that does not exist: %d", rec.Code)
	}

	// the facts
	facts := func(u *auth.User, modelID, runID int64) *httptest.ResponseRecorder {
		return w.call(getBayesRunFacts, u, bayesCall{method: "GET", path: "/x", values: map[string]string{"model_id": bmID(modelID), "run_id": bmID(runID)}})
	}
	for _, u := range []*auth.User{w.editor, w.viewer, observer, w.admin} {
		rec := facts(u, model.ID, sampling)
		w.want(rec, 200)
		var got struct {
			RunID   int64              `json:"run_id"`
			Version int64              `json:"version"`
			Trial   bool               `json:"trial"`
			Sampler map[string]float64 `json:"sampler"`
			Nodes   []map[string]any   `json:"nodes"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		if got.RunID != sampling || got.Version != 1 || !got.Trial || len(got.Nodes) != 2 {
			t.Errorf("%s: %+v", u.FullName, got)
		}
		want := map[string]float64{"divergences": 3, "e_bfmi": 0.31, "draws": 1000, "seconds_sampling": 12.5, "chains": 2}
		if len(got.Sampler) != len(want) {
			t.Errorf("%s: sampler %v, want %v", u.FullName, got.Sampler, want)
		}
		for k, v := range want {
			if got.Sampler[k] != v {
				t.Errorf("%s: sampler %s = %v, want %v", u.FullName, k, got.Sampler[k], v)
			}
		}
		n, vec := got.Nodes[0], got.Nodes[1]
		if n["node"] != "n2" || n["rhat_max"] != 1.02 || n["ess_bulk_min"] != 410.0 || len(n) != 3 { // the extreme that is null is no key; no mean
			t.Errorf("%s: node %v", u.FullName, n)
		}
		// a vector node on a group plate: one entry, the node's id, the worst over all its identifiers; no level, no unit
		if vec["node"] != "n3" || vec["rhat_max"] != 1.2 || vec["ess_bulk_min"] != 120.0 || len(vec) != 3 {
			t.Errorf("%s: the plate's node %v", u.FullName, vec)
		}
		// nothing of the summary or the data, in any form
		for _, leak := range []string{"41.5", "99.5", "summary", "secret_stat", "mean", "Milano", "a_g", "beta_1", "1.5", "1.6"} {
			if strings.Contains(rec.Body.String(), leak) {
				t.Errorf("%s: the answer holds «%s»: %s", u.FullName, leak, rec.Body)
			}
		}
	}
	// what the engine was asked: the sections sampler and quantity, and no other, with where the results are
	mu.Lock()
	if len(asked) == 0 {
		t.Fatal("the engine was not asked")
	}
	for _, a := range asked {
		if !strings.HasPrefix(a, "/reports/model/rows ") || !strings.Contains(a, `"sections":["sampler","quantity"]`) ||
			!strings.Contains(a, `"results_key":"out/new.parquet"`) || !strings.Contains(a, `"bucket":"data-prep"`) {
			t.Errorf("the engine was asked: %s", a)
		}
		for _, no := range []string{"summary", "data", "ppc", "hist", "curve"} {
			if strings.Contains(a, `"`+no+`"`) {
				t.Errorf("the engine was asked for «%s»: %s", no, a)
			}
		}
	}
	before := len(asked)
	mu.Unlock()

	// 404, never 403, and the engine is not reached: no right on the flow, another model's run, a prior check, a failed run, a run that is not there
	prior := w.one(`SELECT id FROM runs WHERE flow_id = $1 AND model_results_key = 'out/prior.parquet'`, open)
	failed := w.one(`SELECT id FROM runs WHERE flow_id = $1 AND status = 'FAILURE'`, open)
	for name, rec := range map[string]*httptest.ResponseRecorder{
		"a stranger":      facts(w.stranger, model.ID, sampling),
		"a closed flow":   facts(w.editor, model.ID, closedRun),
		"another model":   facts(w.editor, model.ID, foreign),
		"under the other": facts(w.editor, otherModel.ID, sampling),
		"a prior check":   facts(w.editor, model.ID, prior),
		"a failed run":    facts(w.editor, model.ID, failed),
		"no such run":     facts(w.editor, model.ID, 999999),
		"no such model":   facts(w.editor, 999999, sampling),
	} {
		if rec.Code != 404 {
			t.Errorf("%s: %d, want 404: %s", name, rec.Code, rec.Body)
		}
	}
	if rec := facts(w.editor, model.ID, old); rec.Code != 200 { // an older run of the model is read all the same, and its version said
		t.Errorf("an older run: %d", rec.Code)
	} else if !strings.Contains(rec.Body.String(), `"version":7`) {
		t.Errorf("an older run's version: %s", rec.Body)
	}
	mu.Lock()
	if n := len(asked) - before; n != 1 { // only the older run reached the engine
		t.Errorf("refused requests reached the engine: %d requests after the refusals", n)
	}
	mu.Unlock()

	// the group `bayes` is not usable by the person: the model is not theirs to see
	w.group(false, false)
	if rec, _ := list(w.editor); rec.Code == 200 {
		t.Errorf("the list with the group off: %d", rec.Code)
	}
	if rec := facts(w.editor, model.ID, sampling); rec.Code == 200 {
		t.Errorf("the facts with the group off: %d", rec.Code)
	}
}
