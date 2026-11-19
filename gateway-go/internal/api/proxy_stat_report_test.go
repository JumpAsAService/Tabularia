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

// the chain a preview of a statistics step sends: a development sample the editor marked, a
// branch, the test
const statReportChain = `[{"type": "limit", "params": {"n": 100000, "_dev_sample": true}},
	{"type": "join", "params": {"on": ["città"], "right": {"source": {"bucket": "data-prep", "key": "datasets/sr-nobody-2.parquet"},
		"operations": [{"type": "sample", "params": {"fraction": 0.1, "seed": 42, "_dev_sample": true}}]}}},
	{"type": "stat_t_two_sample", "params": {"value": "amount", "group": "channel", "groups": ["web", "shop"], "alpha": 0.05}}]`

// What is not the report of a statistics step is refused before anything is read, and
// before the engine: a chain that does not end with a test, no chain, a format that is not
// a report's.
func TestStatReportRefusesWhatIsNotAStatisticsStepsReport(t *testing.T) {
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
	source := `"bucket": "data-prep", "input_key": "datasets/sr.parquet"`
	for body, want := range map[string]string{
		`[]`:                                 "must be a JSON object",
		`{` + source + `}`:                   "needs its chain",
		`{` + source + `, "operations": []}`: "needs its chain",
		`{` + source + `, "operations": "stat_t_two_sample"}`:                                "needs its chain",
		`{` + source + `, "operations": [{"type": "filter", "params": {}}]}`:                 "is not a statistical test",
		`{` + source + `, "operations": [{"type": "stat_no_such_test", "params": {}}]}`:      "is not a statistical test",
		`{` + source + `, "operations": ["stat_t_two_sample"]}`:                              "is not a statistical test",
		`{` + source + `, "operations": [{"type": "stat_t_two_sample"}, {"type": "limit"}]}`: "is not a statistical test",
		`{` + source + `, "operations": [{"type": "stat_t_two_sample"}], "format": "html"}`:  "svg (the pages on screen), pdf or xlsx",
		`{` + source + `, "operations": [{"type": "stat_t_two_sample"}], "format": "csv"}`:   "svg (the pages on screen), pdf or xlsx",
		`{` + source + `, "operations": [{"type": "stat_t_two_sample"}], "format": ["pdf"]}`: "svg (the pages on screen), pdf or xlsx",
	} {
		w := httptest.NewRecorder()
		d.proxyStatReport(w, httptest.NewRequest(http.MethodPost, "/tasks/stat-report", strings.NewReader(body)), p)
		if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), want) {
			t.Errorf("%s: status %d (%s), want 422 with «%s»", body, w.Code, w.Body.String(), want)
		}
	}
	if reached != 0 {
		t.Fatalf("a refused request reached the engine %d times", reached)
	}
}

// The report of a step is drawn for whoever may download the step's rows, and nobody else:
// a chain that reads what the person may not read — in the main chain or in a branch — or
// another bucket never gets to the engine; the chain reaches the engine as it was sent, the
// result is the person's, and nothing but the chain and how to draw it travels (no
// definition from a browser).
func TestStatReportIsCheckedAsADownloadAndForwardsTheChainAsItCame(t *testing.T) {
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
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, r.Method+" "+r.URL.Path+" "+string(b))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"task_id": "0b0e1c1e-6a52-4c11-9d0c-3f6f0f1d2a3b", "status": "submitted", "message": "…", "filename": "Welch's t test — amount by channel.json"}`))
	}))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, "a test fernet key, not of anything")
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	d := &Deps{DB: db, Config: cfg, Log: log, Engine: e, Panel: settings.New(cfg, false, log)}
	user := func(email string, admin bool) auth.User {
		var id int64
		if err := db.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, $2, false, false, now()) RETURNING id`, email, admin).Scan(&id); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = $1`, id)
			_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = $1`, id)
		})
		return auth.User{ID: id, Email: email, IsActive: true, IsSuperuser: admin}
	}
	reader := &auth.Principal{User: user("sr-reader@x.test", false)}
	admin := &auth.Principal{User: user("sr-admin@x.test", true)}
	call := func(p *auth.Principal, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		d.proxyStatReport(w, httptest.NewRequest(http.MethodPost, "/tasks/stat-report", strings.NewReader(body)), p)
		return w
	}
	body := `{"engine": "duckdb", "bucket": "data-prep", "input_key": "datasets/sr-nobody.parquet", "operations": ` + statReportChain + `,
		"format": "pdf", "locale": "it", "filename": "Canali a confronto", "step": "Canali",
		"definition": {"v": 1}, "owner": "user:1", "wait_seconds": 3000, "datasets": {"result": {"rows": []}}}`
	if w := call(reader, body); w.Code != http.StatusForbidden || strings.Contains(w.Body.String(), "bucket not allowed") {
		t.Fatalf("a source the person may not read: status %d (%s)", w.Code, w.Body.String())
	}
	// (an administrator reads any bucket, as for a preview: the pin is for everyone else)
	other := strings.Replace(body, `"bucket": "data-prep", "input_key"`, `"bucket": "elsewhere", "input_key"`, 1)
	if w := call(reader, other); w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "bucket not allowed: 'elsewhere'") {
		t.Fatalf("another bucket: status %d (%s)", w.Code, w.Body.String())
	}
	branch := strings.Replace(body, `"source": {"bucket": "data-prep"`, `"source": {"bucket": "elsewhere"`, 1)
	if w := call(reader, branch); w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "bucket not allowed: 'elsewhere'") {
		t.Fatalf("another bucket in a branch: status %d (%s)", w.Code, w.Body.String())
	}
	if len(got) != 0 {
		t.Fatalf("a refused report reached the engine: %q", got)
	}

	w := call(admin, body)
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"task_id"`) || !strings.Contains(w.Body.String(), `"filename"`) {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if len(got) != 1 || !strings.HasPrefix(got[0], "POST /reports/stat ") {
		t.Fatalf("the engine was asked %q, want one POST /reports/stat", got)
	}
	var sent map[string]any
	if err := json.Unmarshal([]byte(strings.TrimPrefix(got[0], "POST /reports/stat ")), &sent); err != nil {
		t.Fatal(err)
	}
	var chain any
	if err := json.Unmarshal([]byte(statReportChain), &chain); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(sent["operations"], chain) {
		t.Fatalf("the chain reached the engine changed:\n%v\nwant\n%v", sent["operations"], chain)
	}
	want := map[string]any{"engine": "duckdb", "bucket": "data-prep", "input_key": "datasets/sr-nobody.parquet",
		"operations": chain, "format": "pdf", "locale": "it", "filename": "Canali a confronto", "step": "Canali",
		"owner": fmt.Sprintf("user:%d", admin.User.ID), "wait_seconds": float64(0)}
	if !reflect.DeepEqual(sent, want) {
		t.Fatalf("the engine was sent\n%v\nwant\n%v", sent, want)
	}
	// the download is audited, as a step's CSV is
	var audited int
	if err := db.QueryRow(ctx, `SELECT count(*) FROM audit_logs WHERE actor_id = $1 AND action = 'export.download'`, admin.User.ID).Scan(&audited); err != nil {
		t.Fatal(err)
	}
	if audited != 1 {
		t.Fatalf("%d audit lines for the report drawn, want 1", audited)
	}

	// the defaults: the pages on screen, in English for a language Tabularia does not speak;
	// code (an API token) waits for its place among the person's exports
	got = nil
	cfg.Flows.ReportsAPIWaitSeconds = 900
	token := &auth.Principal{User: admin.User, TokenName: "ci"}
	minimal := `{"bucket": "data-prep", "input_key": "datasets/sr-nobody.parquet", "locale": "pt",
		"operations": [{"type": "stat_t_two_sample", "params": {"value": "amount", "group": "channel"}}], "engine": null}`
	if w := call(token, minimal); w.Code != http.StatusOK {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	sent = nil
	if err := json.Unmarshal([]byte(strings.TrimPrefix(got[0], "POST /reports/stat ")), &sent); err != nil {
		t.Fatal(err)
	}
	if sent["format"] != "svg" || sent["locale"] != "en" || sent["wait_seconds"] != float64(900) {
		t.Fatalf("defaults: %v", sent)
	}
	if _, has := sent["engine"]; has {
		t.Fatalf("a null engine travelled: %v", sent)
	}
}
