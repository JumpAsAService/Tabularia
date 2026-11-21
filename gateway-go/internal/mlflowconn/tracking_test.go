package mlflowconn_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"runtime"
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

// A key is cut without the «…» a value gets (MLflow refuses it in a key); a batch the server refuses
// for a key or a value says what MLflow takes, never the server's own words.
func TestAKeyIsCutPlainAndARefusedBatchSaysWhy(t *testing.T) {
	long := strings.Repeat("k", 300)
	b := mlflowconn.Batches([]mlflowconn.Tag{{Key: long, Value: "v"}}, []mlflowconn.Metric{{Key: long}}, []mlflowconn.Tag{{Key: long, Value: "v"}})
	for _, key := range []string{b[0].Params[0].Key, b[0].Metrics[0].Key, b[0].Tags[0].Key} {
		if key != long[:250] {
			t.Errorf("a key cut: %d characters, %q at its end", len([]rune(key)), key[len(key)-3:])
		}
	}
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	run, err := tr.CreateRun(context.Background(), "1", "r", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	err = tr.LogBatch(context.Background(), run.ID, []mlflowconn.Tag{{Key: "a//b", Value: "1"}}, nil, nil)
	var e *mlflowconn.Error
	if !errors.As(err, &e) || e.ErrorCode != "INVALID_PARAMETER_VALUE" || strings.Contains(err.Error(), mlflowtest.Leak) ||
		!strings.HasPrefix(err.Error(), "the tracking server refused a key or a value, and so the whole batch (INVALID_PARAMETER_VALUE, HTTP 400): "+
			"MLflow takes in a key only letters, digits") || !strings.HasSuffix(err.Error(), "shorter values, logging the parameters, metrics and tags") {
		t.Errorf("a refused batch: %v", err)
	}
	if len(s.OnlyRun().Params) != 0 {
		t.Error("a refused batch logged something")
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

// ── a model file as a stream (lot X5) ────────────────────────────────────────

func TestABinaryFileGoesUpAndComesBackByteForByte(t *testing.T) {
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	ctx := context.Background()
	binary := append([]byte{0x00, 0xff, 0xfe, '\r', '\n', 0x1a}, mlflowtest.Pattern(5000)...) // not UTF-8
	if err := tr.Upload(ctx, "1/run1/artifacts", "model/catboost.cbm", bytes.NewReader(binary), int64(len(binary))); err != nil {
		t.Fatal(err)
	}
	if got := s.ContentType["1/run1/artifacts/model/catboost.cbm"]; got != "application/octet-stream" {
		t.Errorf("content type %q", got)
	}
	rc, length, err := tr.Open(ctx, "1/run1/artifacts", "model/catboost.cbm", 1<<20)
	if err != nil || length != int64(len(binary)) {
		t.Fatalf("open: %v %d", err, length)
	}
	got, err := io.ReadAll(rc)
	rc.Close()
	if err != nil || !bytes.Equal(got, binary) {
		t.Fatalf("read back: %v, %d bytes", err, len(got))
	}
	// Download (the small files' way) reads the same bytes
	if b, err := tr.Download(ctx, "1/run1/artifacts", "model/catboost.cbm", 1<<20); err != nil || !bytes.Equal(b, binary) {
		t.Errorf("download: %v", err)
	}
}

func TestOpenSaysMissingAndTooLarge(t *testing.T) {
	s := mlflowtest.New(t)
	tr := tracker(s, "none", "")
	ctx := context.Background()
	if _, _, err := tr.Open(ctx, "1/run1/artifacts", "model/xgboost.json", 1<<20); err == nil {
		t.Fatal("a missing file opened")
	} else if nf := new(mlflowconn.NotFound); !errors.As(err, &nf) {
		t.Errorf("missing: %T %v", err, err)
	}
	s.Artifacts["1/run1/artifacts/model/xgboost.json"] = bytes.Repeat([]byte("x"), 1000)
	// the server says its length: refused before a byte is read
	_, _, err := tr.Open(ctx, "1/run1/artifacts", "model/xgboost.json", 999)
	if tl := new(mlflowconn.TooLarge); !errors.As(err, &tl) || tl.Limit != 999 {
		t.Errorf("over the limit by its length: %v", err)
	}
	// exactly the limit is fine
	rc, _, err := tr.Open(ctx, "1/run1/artifacts", "model/xgboost.json", 1000)
	if err != nil {
		t.Fatal(err)
	}
	if b, err := io.ReadAll(rc); err != nil || len(b) != 1000 {
		t.Errorf("exactly the limit: %v %d", err, len(b))
	}
	rc.Close()
	// no length given: the read itself stops past the limit
	s.NoLength = true
	rc, length, err := tr.Open(ctx, "1/run1/artifacts", "model/xgboost.json", 999)
	if err != nil || length != -1 {
		t.Fatalf("no length: %v %d", err, length)
	}
	defer rc.Close()
	n, err := io.Copy(io.Discard, rc)
	if tl := new(mlflowconn.TooLarge); !errors.As(err, &tl) || n > 1000 {
		t.Errorf("past the limit while reading: %d bytes, %v", n, err)
	}
}

func TestAnUnreadableOpenIsSaidWithoutTheSecret(t *testing.T) {
	s := mlflowtest.New(t)
	s.Authorization = "Bearer right"
	_, _, err := tracker(s, "bearer", "wrong").Open(context.Background(), "1/run1/artifacts", "model/xgboost.json", 1<<20)
	if err == nil || !strings.Contains(err.Error(), "401") || strings.Contains(err.Error(), mlflowtest.Leak) || strings.Contains(err.Error(), "wrong") {
		t.Errorf("%v", err)
	}
}

// A model file of a few megabytes passes through Open without being held: made up by the fake
// while it is sent, hashed and discarded by the reader. What the process allocates stays far
// below the file's size (reading it whole would allocate at least its size, twice with the
// doubling of a growing buffer).
func TestAModelFileIsStreamedNotHeld(t *testing.T) {
	const size = 24 << 20
	s := mlflowtest.New(t)
	s.Stream("1/run1/artifacts/model/xgboost.json", size)
	tr := tracker(s, "none", "")
	want := mlflowtest.PatternSHA256(size)
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	rc, length, err := tr.Open(context.Background(), "1/run1/artifacts", "model/xgboost.json", 64<<20)
	if err != nil || length != size {
		t.Fatalf("open: %v %d", err, length)
	}
	h := sha256.New()
	n, err := io.Copy(h, rc) // io.Copy's own buffer is 32 KiB
	rc.Close()
	runtime.ReadMemStats(&after)
	if err != nil || n != size || hex.EncodeToString(h.Sum(nil)) != want {
		t.Fatalf("read: %v, %d bytes, %x", err, n, h.Sum(nil))
	}
	t.Logf("allocated while streaming %d MiB: %d KiB", size>>20, (after.TotalAlloc-before.TotalAlloc)>>10)
	// the fake's own chunks and the HTTP buffers are in this count too (same process)
	if grown := after.TotalAlloc - before.TotalAlloc; grown > size/3 {
		t.Errorf("%d bytes allocated to stream %d: the file was held", grown, size)
	}
}
