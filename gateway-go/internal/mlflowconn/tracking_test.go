package mlflowconn_test

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
)

// The tracking client against a fake MLflow server: the experiment found or created, the
// batches split by MLflow's limits, the artifacts through the proxy, the version registered,
// and every failure said without the server's text or the secret.

func tracker(s *mlflowtest.Server, auth, secret string) *mlflowconn.Tracker {
	return mlflowconn.NewTracker(mlflowconn.Config{URI: s.URL + "/", Auth: auth, User: "ann", Secret: secret, VerifyTLS: true,
		Timeout: 2 * time.Second})
}

func TestTheExperimentIsFoundOrCreated(t *testing.T) {
	s := mlflowtest.New(t)
	s.Experiments["Pasta/price"] = "7"
	tr := tracker(s, "none", "")
	ctx := context.Background()
	if id, created, err := tr.Experiment(ctx, "Pasta/price"); err != nil || id != "7" || created {
		t.Fatalf("found: %q %v %v", id, created, err)
	}
	id, created, err := tr.Experiment(ctx, "New one")
	if err != nil || !created || id == "" || s.ExpTags["New one"]["tabularia.created"] != "true" {
		t.Fatalf("created: %q %v %v %v", id, created, err, s.ExpTags)
	}
	s.Deleted["Pasta/price"] = true
	_, _, err = tr.Experiment(ctx, "Pasta/price")
	var del *mlflowconn.Deleted
	if !errors.As(err, &del) || !strings.Contains(err.Error(), "trash") {
		t.Fatalf("in the trash: %v", err)
	}
}

func TestAnExperimentMadeMeanwhileIsFoundAgain(t *testing.T) {
	s := mlflowtest.New(t)
	s.Fail = func(r *http.Request) int {
		if r.URL.Path == "/api/2.0/mlflow/experiments/create" { // another run made it after this one looked
			s.Experiments["Race"] = "3"
		}
		return 0
	}
	id, created, err := tracker(s, "none", "").Experiment(context.Background(), "Race")
	if err != nil || id != "3" || created {
		t.Fatalf("%q %v %v", id, created, err)
	}
}

func TestBatchesKeepMLflowsLimits(t *testing.T) {
	var params, tags []mlflowconn.Tag
	var metrics []mlflowconn.Metric
	for i := range 250 {
		params = append(params, mlflowconn.Tag{Key: fmt.Sprintf("p%d", i), Value: strings.Repeat("v", 300)})
	}
	for i := range 150 {
		tags = append(tags, mlflowconn.Tag{Key: fmt.Sprintf("t%d", i), Value: "x"})
	}
	for i := range 2500 {
		metrics = append(metrics, mlflowconn.Metric{Key: fmt.Sprintf("m%d", i), Value: float64(i)})
	}
	batches := mlflowconn.Batches(params, metrics, tags)
	np, nm, nt := 0, 0, 0
	for _, b := range batches {
		if len(b.Params) > 100 || len(b.Tags) > 100 || len(b.Params)+len(b.Metrics)+len(b.Tags) > 1000 {
			t.Fatalf("a batch over the limits: %d %d %d", len(b.Params), len(b.Metrics), len(b.Tags))
		}
		for _, p := range b.Params {
			if r := []rune(p.Value); len(r) != 250 || r[249] != '…' {
				t.Fatalf("a long value is cut to 250 with «…»: %d", len(r))
			}
		}
		np, nm, nt = np+len(b.Params), nm+len(b.Metrics), nt+len(b.Tags)
	}
	if np != 250 || nm != 2500 || nt != 150 || len(batches) != 3 {
		t.Fatalf("%d batches: %d params, %d metrics, %d tags", len(batches), np, nm, nt)
	}
	if mlflowconn.Batches(nil, nil, nil) != nil {
		t.Error("nothing to log: no call")
	}
	// against the fake, which refuses a batch over the limits
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	run, err := tr.CreateRun(context.Background(), "1", "flow · node · date", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if err := tr.LogBatch(context.Background(), run.ID, params, metrics, tags); err != nil {
		t.Fatal(err)
	}
	r := s.OnlyRun()
	if len(r.Params) != 250 || len(r.Metrics) != 2500 || len(r.Tags) != 150 || len(r.Batches) != 3 {
		t.Fatalf("logged %d params, %d metrics, %d tags in %d calls", len(r.Params), len(r.Metrics), len(r.Tags), len(r.Batches))
	}
}

func TestArtifactsGoThroughTheProxy(t *testing.T) {
	for uri, want := range map[string]string{
		"mlflow-artifacts:/1/abc/artifacts":               "1/abc/artifacts",
		"mlflow-artifacts://mlflow:5000/1/abc/artifacts/": "1/abc/artifacts",
		"s3://bucket/1/abc/artifacts":                     "",
		"/mlruns/1/abc/artifacts":                         "",
		"file:///mlruns/1/abc":                            "",
	} {
		if got := mlflowconn.ArtifactPath(uri); got != want {
			t.Errorf("%s: %q, want %q", uri, got, want)
		}
	}
	s := mlflowtest.New(t)
	s.Authorization = "Bearer tok"
	tr := tracker(s, "bearer", "tok")
	run, err := tr.CreateRun(context.Background(), "1", "r", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	where := mlflowconn.ArtifactPath(run.ArtifactURI)
	if err := tr.Upload(context.Background(), where, "model/model.json", strings.NewReader(`{"format":"tabularia-model"}`), 28); err != nil {
		t.Fatal(err)
	}
	if string(s.Artifacts["1/"+run.ID+"/artifacts/model/model.json"]) != `{"format":"tabularia-model"}` {
		t.Fatalf("%v", s.Artifacts)
	}
	// a server without the proxy: the upload says it is not offered
	s.NoProxy = true
	err = tr.Upload(context.Background(), where, "model/MLmodel", strings.NewReader("x"), 1)
	if !mlflowconn.Unsupported(err) {
		t.Fatalf("no proxy: %v", err)
	}
}

func TestTheModelIsRegistered(t *testing.T) {
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	tags := []mlflowconn.Tag{{Key: "tabularia.model", Value: "linear"}, {Key: "tabularia.card_sha256", Value: "ab"}}
	v, err := tr.Register(context.Background(), "price-elasticity", "mlflow-artifacts:/1/run1/artifacts/model", "run1", tags)
	if err != nil || v != "1" {
		t.Fatalf("%q %v", v, err)
	}
	// the registered model exists now: a second version
	v, err = tr.Register(context.Background(), "price-elasticity", "mlflow-artifacts:/1/run2/artifacts/model", "run2", tags)
	if err != nil || v != "2" {
		t.Fatalf("%q %v", v, err)
	}
	got := s.Models["price-elasticity"][1]
	if got.Source != "mlflow-artifacts:/1/run2/artifacts/model" || got.RunID != "run2" || got.Tags["tabularia.model"] != "linear" {
		t.Fatalf("%+v", got)
	}
	s.NoRegistry = true
	_, err = tr.Register(context.Background(), "other", "x", "run1", nil)
	if !mlflowconn.Unsupported(err) {
		t.Fatalf("no registry: %v", err)
	}
}

func TestFailuresAreSaidWithoutTheServersTextOrTheSecret(t *testing.T) {
	s := mlflowtest.New(t)
	s.Authorization = "Basic YW5uOnJpZ2h0" // ann:right
	_, err := tracker(s, "basic", "wrong-s3cret").CreateRun(context.Background(), "1", "r", time.Now())
	var e *mlflowconn.Error
	if !errors.As(err, &e) || e.Code != "unauthorized" || e.Status != 401 || !strings.Contains(err.Error(), "401") {
		t.Fatalf("401: %v", err)
	}
	for _, bad := range []string{mlflowtest.Leak, "wrong-s3cret"} {
		if strings.Contains(err.Error(), bad) {
			t.Fatalf("%q in %q", bad, err)
		}
	}
	s.Authorization = ""
	s.Fail = func(*http.Request) int { return http.StatusForbidden }
	_, err = tracker(s, "bearer", "tok-s3cret").CreateRun(context.Background(), "1", "r", time.Now())
	if !errors.As(err, &e) || e.Code != "forbidden" || strings.Contains(err.Error(), mlflowtest.Leak) || strings.Contains(err.Error(), "tok-s3cret") {
		t.Fatalf("403: %v", err)
	}
	if err.Error() != "the connection's credentials are not allowed to do this on the tracking server (403), creating the run" {
		t.Fatalf("the sentence: %q", err)
	}
	s.Fail = func(*http.Request) int { return http.StatusInternalServerError }
	_, err = tracker(s, "none", "").CreateRun(context.Background(), "1", "r", time.Now())
	if err == nil || err.Error() != "the tracking server answered PERMISSION_DENIED (HTTP 500), creating the run" {
		t.Fatalf("500: %v", err)
	}
}

func TestATimeoutIsSaid(t *testing.T) {
	s := mlflowtest.New(t)
	s.Delay = 2 * time.Second
	tr := mlflowconn.NewTracker(mlflowconn.Config{URI: s.URL, VerifyTLS: true, Timeout: 100 * time.Millisecond})
	start := time.Now()
	_, _, err := tr.Experiment(context.Background(), "x")
	var e *mlflowconn.Error
	if !errors.As(err, &e) || e.Code != "timeout" || time.Since(start) > time.Second {
		t.Fatalf("%v after %s", err, time.Since(start))
	}
	if err.Error() != "the tracking server did not answer in time, finding the experiment «x»" {
		t.Fatalf("%q", err)
	}
}

func TestTheRunEnds(t *testing.T) {
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	run, _ := tr.CreateRun(context.Background(), "1", "r", time.Now())
	if err := tr.EndRun(context.Background(), run.ID, true, time.Now()); err != nil {
		t.Fatal(err)
	}
	if r := s.OnlyRun(); r.Status != "FAILED" || !r.Ended {
		t.Fatalf("%+v", r)
	}
}
