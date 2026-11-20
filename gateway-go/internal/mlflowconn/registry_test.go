package mlflowconn_test

import (
	"context"
	"errors"
	"net/http"
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
	if _, err := tr.VersionsOf(ctx, "o'neil"); !errors.As(err, &nf) {
		t.Errorf("a name with a quote: %v", err)
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
