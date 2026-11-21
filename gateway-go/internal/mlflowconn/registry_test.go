package mlflowconn_test

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
)

// The reading side of the client, against the fake server: what a Predict node lists and reads.

func TestTheRegistryIsRead(t *testing.T) {
	s := mlflowtest.New(t)
	s.Authorization = "Bearer tok"
	tags := map[string]string{"tabularia.model": "linear"}
	files := map[string][]byte{"model.json": []byte(`{"format": "tabularia-model"}`), "MLmodel": []byte("flavors:\n  tabularia:\n")}
	run1, v1 := s.Register("Pasta", "price", tags, map[string]float64{"r_squared": 0.8}, files, tags)
	s.Register("Pasta", "by-hand", map[string]string{}, nil, nil, map[string]string{})
	s.Register("Other", "churn", tags, nil, files, tags)
	tr := mlflowconn.NewTracker(mlflowconn.Config{URI: s.URL, Auth: "bearer", Secret: "tok", Timeout: 5 * time.Second})
	ctx := context.Background()

	exps, err := tr.Experiments(ctx)
	if err != nil || len(exps) != 2 || exps[0].Name != "Other" || exps[1].Name != "Pasta" {
		t.Fatalf("experiments %+v %v", exps, err)
	}
	id, err := tr.ExperimentID(ctx, "Pasta")
	if err != nil || id != exps[1].ID {
		t.Fatalf("id %q %v", id, err)
	}
	var nf *mlflowconn.NotFound
	if _, err := tr.ExperimentID(ctx, "Nope"); !errors.As(err, &nf) {
		t.Errorf("a missing experiment: %v", err)
	}
	runs, err := tr.TabulariaRuns(ctx, id)
	if err != nil || len(runs) != 1 || runs[0].ID != run1 || runs[0].Metrics["r_squared"] != 0.8 {
		t.Fatalf("Tabularia's runs of the experiment %+v %v", runs, err)
	}
	versions, err := tr.VersionsOfRuns(ctx, []string{run1, "x') OR (name = 'y"})
	if err != nil || len(versions) != 1 || versions[0].Name != "price" || versions[0].Version != v1 || versions[0].Created == 0 {
		t.Fatalf("versions %+v %v", versions, err)
	}
	of, err := tr.VersionsOf(ctx, "price")
	if err != nil || len(of) != 1 {
		t.Errorf("versions of a model %+v %v", of, err)
	}
	// a name with quotes or a backslash: between the quotes it does not hold, else by LIKE and kept
	// only when it is exactly that name
	for _, name := range []string{"Customer's CLV", `the "best" model`, `o'neil "x"`, `ends with \`, `a\'b`} {
		s.Register("Pasta", name, tags, nil, files, tags)
		s.Register("Pasta", strings.NewReplacer("'", "x", `"`, "x", `\`, "x").Replace(name), tags, nil, files, tags) // a name alike
		if of, err := tr.VersionsOf(ctx, name); err != nil || len(of) != 1 || of[0].Name != name {
			t.Errorf("the versions of %q: %+v %v", name, of, err)
		}
	}
	v, err := tr.Version(ctx, "price", v1)
	if err != nil || v.RunID != run1 || v.Tags["tabularia.model"] != "linear" || v.Number() != 1 {
		t.Fatalf("version %+v %v", v, err)
	}
	if _, err := tr.Version(ctx, "price", "9"); !errors.As(err, &nf) {
		t.Errorf("a missing version: %v", err)
	}
	where := mlflowconn.ArtifactPath(v.Source)
	card, err := tr.Download(ctx, where, "model.json", 1<<20)
	if err != nil || string(card) != `{"format": "tabularia-model"}` {
		t.Errorf("card %q %v", card, err)
	}
	var tl *mlflowconn.TooLarge
	if _, err := tr.Download(ctx, where, "model.json", 5); !errors.As(err, &tl) {
		t.Errorf("too large: %v", err)
	}
	if _, err := tr.Download(ctx, where, "native.pkl", 1<<20); !errors.As(err, &nf) {
		t.Errorf("a missing file: %v", err)
	}
	// only reads, with the credentials
	for _, p := range s.Paths() {
		if strings.HasPrefix(p, "PUT ") || strings.Contains(p, "/create") || strings.Contains(p, "/update") || strings.Contains(p, "log-batch") {
			t.Errorf("a write: %s", p)
		}
	}
	bad := mlflowconn.NewTracker(mlflowconn.Config{URI: s.URL, Auth: "bearer", Secret: "wrong", Timeout: 5 * time.Second})
	var me *mlflowconn.Error
	if _, err := bad.Experiments(ctx); !errors.As(err, &me) || me.Code != "unauthorized" || strings.Contains(err.Error(), mlflowtest.Leak) {
		t.Errorf("wrong token: %v", err)
	}
	if _, err := bad.Download(ctx, where, "model.json", 1<<20); !errors.As(err, &me) || me.Code != "unauthorized" {
		t.Errorf("a download with a wrong token: %v", err)
	}
	s.Fail = func(r *http.Request) int { return http.StatusServiceUnavailable }
	if _, err := tr.Version(ctx, "price", v1); !errors.As(err, &me) || strings.Contains(err.Error(), mlflowtest.Leak) {
		t.Errorf("a failing server: %v", err)
	}
}

// The same names on a real MLflow server (TEST_MLFLOW_URL, e.g. ghcr.io/mlflow/mlflow:v2.22.0 run
// with `mlflow server --serve-artifacts`): what «latest» reads finds each of them.
func TestQuotedNamesAreFoundOnARealMLflow(t *testing.T) {
	uri := os.Getenv("TEST_MLFLOW_URL")
	if uri == "" {
		t.Skip("TEST_MLFLOW_URL not set")
	}
	ctx := context.Background()
	tr := mlflowconn.NewTracker(mlflowconn.Config{URI: uri, Auth: "none", VerifyTLS: true})
	stamp := time.Now().Format("150405.000000")
	exp, _, err := tr.Experiment(ctx, "names-"+stamp)
	if err != nil {
		t.Fatal(err)
	}
	run, err := tr.CreateRun(ctx, exp, "r", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"Customer's CLV", `the "best" model`, `o'neil "x"`, `ends with \`, `a\'b`, "plain"} {
		name += " " + stamp
		alike := strings.NewReplacer("'", "x", `"`, "x", `\`, "x").Replace(name)
		for i, n := range []string{name, alike} {
			if i == 1 && alike == name {
				continue
			}
			if _, err := tr.Register(ctx, n, strings.TrimRight(run.ArtifactURI, "/")+"/model", run.ID, nil); err != nil {
				t.Fatalf("register %q: %v", n, err)
			}
		}
		if of, err := tr.VersionsOf(ctx, name); err != nil || len(of) != 1 || of[0].Name != name || of[0].Version != "1" {
			t.Errorf("the versions of %q: %+v %v", name, of, err)
		}
	}
}

// A version's run status and the deletion of a version, against the fake and a real MLflow server
// (TEST_MLFLOW_URL): what the model step does after a registration whose answer was lost, and what
// Predict reads before it applies a version.
func TestARunsStatusIsReadAndAVersionDeleted(t *testing.T) {
	servers := map[string]string{"fake": mlflowtest.New(t).URL}
	if uri := os.Getenv("TEST_MLFLOW_URL"); uri != "" {
		servers["real"] = uri
	}
	for what, uri := range servers {
		t.Run(what, func(t *testing.T) {
			ctx := context.Background()
			tr := mlflowconn.NewTracker(mlflowconn.Config{URI: uri, Auth: "none", VerifyTLS: true})
			stamp := time.Now().Format("150405.000000")
			exp, _, err := tr.Experiment(ctx, "status-"+stamp)
			if err != nil {
				t.Fatal(err)
			}
			run, err := tr.CreateRun(ctx, exp, "r", time.Now())
			if err != nil {
				t.Fatal(err)
			}
			if s, err := tr.RunStatus(ctx, run.ID); err != nil || s != "RUNNING" {
				t.Errorf("a running run: %q %v", s, err)
			}
			if err := tr.EndRun(ctx, run.ID, false, time.Now()); err != nil {
				t.Fatal(err)
			}
			if err := tr.EndRun(ctx, run.ID, true, time.Now()); err != nil {
				t.Fatal(err)
			}
			if s, err := tr.RunStatus(ctx, run.ID); err != nil || s != "FAILED" {
				t.Errorf("a run set FAILED after FINISHED: %q %v", s, err)
			}
			var nf *mlflowconn.NotFound
			if _, err := tr.RunStatus(ctx, "0123456789abcdef0123456789abcdef"); !errors.As(err, &nf) {
				t.Errorf("a run that is not there: %v", err)
			}
			name := "deleted " + stamp
			v, err := tr.Register(ctx, name, strings.TrimRight(run.ArtifactURI, "/")+"/model", run.ID, nil)
			if err != nil {
				t.Fatal(err)
			}
			if of, err := tr.VersionsOfRuns(ctx, []string{run.ID}); err != nil || len(of) != 1 || of[0].Version != v {
				t.Fatalf("the run's versions: %+v %v", of, err)
			}
			if err := tr.DeleteVersion(ctx, name, v); err != nil {
				t.Fatal(err)
			}
			if of, err := tr.VersionsOf(ctx, name); err != nil || len(of) != 0 {
				t.Errorf("after the deletion: %+v %v", of, err)
			}
			if err := tr.DeleteVersion(ctx, name, v); err != nil {
				t.Errorf("a version already gone: %v", err)
			}
		})
	}
}
