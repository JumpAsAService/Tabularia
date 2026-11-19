package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// What is not the report of a model's run is refused before anything is read, and before
// the engine: no flow, node or run, a format that is not a report's.
func TestModelReportRefusesWhatIsNotARunsReport(t *testing.T) {
	reached := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached++ }))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	// no database: a request refused for its shape never gets to the rights
	d := &Deps{Config: &config.Config{EngineBucket: "data-prep"}, Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Engine: e}
	p := &auth.Principal{User: auth.User{ID: 7}}
	for body, want := range map[string]string{
		`[]`:                              "must be a JSON object",
		`{}`:                              "needs its flow, its node and its run",
		`{"flow_id": 1, "node_id": "n7"}`: "needs its flow, its node and its run",
		`{"flow_id": "1", "node_id": "n7", "run_id": 2}`:                  "needs its flow, its node and its run",
		`{"flow_id": 1, "node_id": null, "run_id": 2}`:                    "needs its flow, its node and its run",
		`{"flow_id": 1, "node_id": "n7", "run_id": 2.5}`:                  "needs its flow, its node and its run",
		`{"flow_id": 1, "node_id": "n7", "run_id": 2, "format": "html"}`:  "svg (the pages on screen), pdf or xlsx",
		`{"flow_id": 1, "node_id": "n7", "run_id": 2, "format": ["pdf"]}`: "svg (the pages on screen), pdf or xlsx",
	} {
		w := httptest.NewRecorder()
		d.proxyModelReport(w, httptest.NewRequest(http.MethodPost, "/tasks/model-report", strings.NewReader(body)), p)
		if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), want) {
			t.Errorf("%s: status %d (%s), want 422 with «%s»", body, w.Code, w.Body.String(), want)
		}
	}
	if reached != 0 {
		t.Fatalf("a refused request reached the engine %d times", reached)
	}
}

// The report of a run is drawn for whoever may open the flow, and nobody else; the run must
// be one of that node's and have its results; what reaches the engine is the gateway's —
// where the results are, the model and the parameters the run kept, the node's name, the
// samples — and nothing of the browser's but the format and the language.
func TestModelReportReadsTheRunsResultsForWhoeverMayOpenTheFlow(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var migrated bool
	if err := db.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM information_schema.columns
		WHERE table_name = 'runs' AND column_name = 'model_results_key')`).Scan(&migrated); err != nil {
		t.Fatal(err)
	}
	if !migrated {
		t.Skip("the models' migration (0044) is not applied: runs.model_results_key does not exist")
	}
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, r.Method+" "+r.URL.Path+" "+string(b))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"task_id": "0b0e1c1e-6a52-4c11-9d0c-3f6f0f1d2a3b", "status": "submitted", "message": "…", "filename": "Linear regression.json"}`))
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	d := &Deps{DB: db, Config: cfg, Log: log, Engine: e, Panel: settings.New(cfg, false, log)}
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := db.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	user := func(email string, admin bool) auth.User {
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, $2, false, false, now()) RETURNING id`, email, admin)
		t.Cleanup(func() {
			_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = $1`, id)
			_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = $1`, id)
		})
		return auth.User{ID: id, Email: email, IsActive: true, IsSuperuser: admin}
	}
	stranger := &auth.Principal{User: user("mr-stranger@x.test", false)}
	admin := &auth.Principal{User: user("mr-admin@x.test", true)}
	folder := one(`INSERT INTO projects (name, description, created_at) VALUES ('mr-folder', '', now()) RETURNING id`)
	definition := `{"nodes": [{"id": "n7", "type": "model", "data": {"label": "Prices", "model": "linear",
		"params": {"outcome": "quantity", "predictors": ["price"]}}}, {"id": "n8", "type": "operation", "data": {}}], "edges": []}`
	flow := one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('mr flow', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, folder, admin.User.ID, definition)
	t.Cleanup(func() {
		_, _ = db.Exec(ctx, `DELETE FROM runs WHERE flow_id = $1`, flow)
		_, _ = db.Exec(ctx, `DELETE FROM flows WHERE id = $1`, flow)
		_, _ = db.Exec(ctx, `DELETE FROM projects WHERE id = $1`, folder)
	})
	run := func(node, status, key string) int64 {
		script := `{"node_id": "` + node + `", "model": "linear", "params": {"outcome": "quantity", "predictors": ["price", "channel"]},
			"sample": [{"first": 100000}], "trial": true}`
		return one(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
			publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, script, model_results_key)
			VALUES ('model', $1, '', $2, $3, 'trial', '', 'data-prep', '', '', false, '[]', now(), now(), $4, $5) RETURNING id`,
			flow, status, admin.User.ID, script, key)
	}
	done := run("n7", "SUCCESS", "runs/1/n7/results.parquet")
	failed := run("n7", "FAILURE", "")
	another := run("n9", "SUCCESS", "runs/1/n9/results.parquet")
	call := func(p *auth.Principal, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		d.proxyModelReport(w, httptest.NewRequest(http.MethodPost, "/tasks/model-report", strings.NewReader(body)), p)
		return w
	}
	ask := func(run int64, more string) string {
		return fmt.Sprintf(`{"flow_id": %d, "node_id": "n7", "run_id": %d%s}`, flow, run, more)
	}
	if w := call(stranger, ask(done, "")); w.Code != http.StatusForbidden && w.Code != http.StatusNotFound {
		t.Fatalf("someone who cannot open the flow: status %d (%s)", w.Code, w.Body.String())
	}
	for _, c := range []struct {
		body, want string
	}{
		{ask(failed, ""), "did not finish, or it failed"},
		{ask(another, ""), "not one of that node's"},
		{fmt.Sprintf(`{"flow_id": %d, "node_id": "n8", "run_id": %d}`, flow, done), "no model node with that id"},
	} {
		if w := call(admin, c.body); w.Code != http.StatusNotFound || !strings.Contains(w.Body.String(), c.want) {
			t.Fatalf("%s: status %d (%s), want 404 with «%s»", c.body, w.Code, w.Body.String(), c.want)
		}
	}
	if len(got) != 0 {
		t.Fatalf("a refused report reached the engine: %q", got)
	}
	w := call(admin, ask(done, `, "format": "pdf", "locale": "it", "bucket": "elsewhere", "results_key": "x", "model": "cox", "params": {}`))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"task_id"`) {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if len(got) != 1 || !strings.HasPrefix(got[0], "POST /reports/model ") {
		t.Fatalf("the engine was asked %q, want one POST /reports/model", got)
	}
	var sent map[string]any
	if err := json.Unmarshal([]byte(strings.TrimPrefix(got[0], "POST /reports/model ")), &sent); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"bucket": "data-prep", "results_key": "runs/1/n7/results.parquet", "model": "linear",
		"params": map[string]any{"outcome": "quantity", "predictors": []any{"price", "channel"}}, "format": "pdf", "locale": "it",
		"step": "Prices", "sample": []any{map[string]any{"first": float64(100000)}},
		"owner": fmt.Sprintf("user:%d", admin.User.ID), "wait_seconds": float64(0)}
	if !reflect.DeepEqual(sent, want) {
		t.Fatalf("the engine was sent\n%v\nwant\n%v", sent, want)
	}
}
