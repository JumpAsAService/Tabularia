package pod

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The pod against a gateway: it asks for the manifest with its token, prepares the data, and
// sends what it counted with the next poll.
func TestRunAgainstAGateway(t *testing.T) {
	f := newFixture(t)
	f.m.PollSeconds = 1
	token, _ := dataapiwire.Random(dataapiwire.PodTokenPrefix)
	var mu sync.Mutex
	var reports []dataapiwire.Usage
	manifests := 0
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(401)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		switch r.URL.Path {
		case "/internal/data-apis/1/manifest":
			manifests++
			_ = json.NewEncoder(w).Encode(f.m)
		case "/internal/data-apis/1/usage":
			var u dataapiwire.Usage
			_ = json.NewDecoder(r.Body).Decode(&u)
			reports = append(reports, u)
			_, _ = w.Write([]byte(`{"kept": 1}`))
		default:
			w.WriteHeader(404)
		}
	}))
	defer gw.Close()
	f.s.gateway = &Gateway{URL: gw.URL, ID: 1, Token: token, HTTP: gw.Client()}
	f.s.now = time.Now // real time: the polls go by
	f.s.limiter.now = time.Now
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { f.s.Run(ctx); close(done) }()
	deadline := time.Now().Add(20 * time.Second)
	for !f.s.Ready() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !f.s.Ready() {
		t.Fatalf("not ready: %s", f.logs.String())
	}
	f.want(f.get("/data/crm/customers?id=3", "main"), 200, "")
	for time.Now().Before(deadline) {
		mu.Lock()
		counted := false
		for _, u := range reports {
			counted = counted || len(u.Minutes) > 0
		}
		mu.Unlock()
		if counted {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	cancel()
	<-done
	mu.Lock()
	all, n := reports, manifests
	mu.Unlock()
	// a report at every poll; the counts in the first one after the request
	var got []dataapiwire.Usage
	for _, u := range all {
		if len(u.Minutes) > 0 {
			got = append(got, u)
		}
	}
	if n < 2 || len(got) == 0 || got[0].Pod != "test" || got[0].Minutes[0].KeyID != 1 || got[0].Minutes[0].Rows != 1 {
		t.Fatalf("%d manifests, %+v", n, got)
	}
	// a report as soon as the data are prepared, not a poll later: ready, the version each
	// resource serves
	last := got[len(got)-1].Status
	if last == nil || !last.Ready || last.State != dataapiwire.StateActive || last.ManifestAt == nil || len(last.Resources) != 2 ||
		last.Resources[0].Name != "customers" || last.Resources[0].Version != f.m.Resources[0].Version ||
		last.Resources[0].Bytes <= 0 || last.Resources[0].Preparing != "" || last.Error != "" {
		t.Fatalf("status %+v", last)
	}
	// a wrong token: the pod keeps what it had and says so
	f.s.gateway.Token = "tdp_wrong"
	if _, err := f.s.gateway.Manifest(context.Background()); err == nil || !strings.Contains(err.Error(), "401") {
		t.Error(err)
	}
	// another API's manifest is not taken
	f.s.gateway.Token, f.s.gateway.ID = token, 2
	if _, err := f.s.gateway.Manifest(context.Background()); err == nil {
		t.Error("another API's manifest")
	}
}

func TestBudget(t *testing.T) {
	c := Config{MemoryMB: 614, Threads: 2}
	if serve, prep, threads := c.Budget(1); serve != 204 || prep != 409 || threads != 2 {
		t.Error(serve, prep, threads)
	}
	if serve, prep, threads := c.Budget(4); serve != 51 || prep != 409 || threads != 2 {
		t.Error(serve, prep, threads)
	}
	// a small pod sorts with one thread, the only way it can
	c = Config{MemoryMB: 300, Threads: 4}
	if _, prep, threads := c.Budget(1); prep != 200 || threads != 1 {
		t.Error(prep, threads)
	}
}

func TestConfig(t *testing.T) {
	env := map[string]string{"DATA_API__ID": "7", "DATA_API__GATEWAY_URL": "http://gw:8000/", "DATA_API__POD_TOKEN": "tdp_x",
		"DATA_API__FORWARDED_ALLOW_IPS": "10.0.0.0/8, 192.168.0.1", "DATA_API__THREADS": "3", "DATA_API__MEMORY_LIMIT_MB": "300"}
	c, err := LoadConfig(func(k string) string { return env[k] })
	if err != nil || c.APIID != 7 || c.GatewayURL != "http://gw:8000" || c.Poll != 20*time.Second || c.DataDir != "/data" ||
		c.Port != 8000 || c.Threads != 3 || c.MemoryMB != 300 || len(c.ForwardedAllowIPs) != 2 || c.ManifestMaxAge != 15*time.Minute {
		t.Fatalf("%+v %v", c, err)
	}
	_, err = LoadConfig(func(k string) string { return map[string]string{"DATA_API__THREADS": "0"}[k] })
	if err == nil || !strings.Contains(err.Error(), "DATA_API__ID") || !strings.Contains(err.Error(), "DATA_API__POD_TOKEN") ||
		!strings.Contains(err.Error(), "DATA_API__THREADS") {
		t.Error(err)
	}
}

func TestRemoveLeftovers(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"r1.duckdb", "r1.duckdb.wal", "r12.parquet.part", "keep.txt", "r1.duckdb.bak"} {
		if err := os.WriteFile(filepath.Join(dir, name), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_ = os.MkdirAll(filepath.Join(dir, "tmp", "x"), 0o700)
	RemoveLeftovers(dir)
	left, _ := os.ReadDir(dir)
	names := []string{}
	for _, e := range left {
		names = append(names, e.Name())
	}
	if strings.Join(names, ",") != "keep.txt,r1.duckdb.bak" {
		t.Error(names)
	}
}

// The status says what went wrong: a manifest not had, a version that does not prepare.
func TestStatusSaysWhatWentWrong(t *testing.T) {
	f := newFixture(t)
	if st := f.s.Status(); st.Ready || st.State != "" || st.ManifestAt != nil || len(st.Resources) != 0 {
		t.Fatalf("before the first manifest: %+v", st)
	}
	f.load()
	st := f.s.Status()
	if !st.Ready || st.State != "active" || len(st.Resources) != 2 || st.Resources[0].Version == "" || st.Error != "" {
		t.Fatalf("%+v", st)
	}
	f.s.mu.Lock()
	f.s.manifestErr, f.s.manifestAt = "the manifest: the gateway answered 401 to the manifest", time.Now()
	f.s.failed["customers/"+Generation(f.m.Resources[0])] = failure{error: strings.Repeat("é", 400)}
	f.s.mu.Unlock()
	st = f.s.Status()
	if !strings.Contains(st.Error, "401") || st.ErrorAt == nil || !strings.HasSuffix(st.Resources[0].Error, "…") ||
		len(st.Resources[0].Error) > dataapiwire.MaxStatusText+len("…") {
		t.Fatalf("%+v", st)
	}
}

// The status goes as soon as the data are prepared: the page does not wait a poll to know.
func TestReportAfterPreparing(t *testing.T) {
	f := newFixture(t)
	f.m.PollSeconds = 600
	token, _ := dataapiwire.Random(dataapiwire.PodTokenPrefix)
	var mu sync.Mutex
	var reports []dataapiwire.Usage
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch r.URL.Path {
		case "/internal/data-apis/1/manifest":
			_ = json.NewEncoder(w).Encode(f.m)
		case "/internal/data-apis/1/usage":
			var u dataapiwire.Usage
			_ = json.NewDecoder(r.Body).Decode(&u)
			reports = append(reports, u)
		}
	}))
	defer gw.Close()
	f.s.gateway = &Gateway{URL: gw.URL, ID: 1, Token: token, HTTP: gw.Client()}
	f.s.now = time.Now
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { f.s.Run(ctx); close(done) }()
	deadline := time.Now().Add(20 * time.Second)
	ready := false
	for !ready && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
		mu.Lock()
		for _, u := range reports {
			ready = ready || (u.Status != nil && u.Status.Ready)
		}
		mu.Unlock()
	}
	cancel()
	<-done
	if !ready {
		t.Fatalf("no ready report within the poll of 600 s: %d reports", len(reports))
	}
}

// No more queries run at once than the pod has CPUs: the others wait their turn, within the
// request's time.
func TestQueriesTakeTurns(t *testing.T) {
	f := newFixture(t)
	f.load()
	if cap(f.s.gate) != 2 {
		t.Fatalf("gate of %d for 2 threads", cap(f.s.gate))
	}
	// both turns taken: a request waits, and past its time answers 503
	f.s.gate <- struct{}{}
	f.s.gate <- struct{}{}
	f.s.cfg.QueryTimeout = 200 * time.Millisecond
	started := time.Now()
	f.want(f.get("/data/crm/customers?id=3", "main"), 503, "query_timeout")
	if time.Since(started) < 150*time.Millisecond {
		t.Error("did not wait its turn")
	}
	<-f.s.gate
	f.want(f.get("/data/crm/customers?id=3", "main"), 200, "")
	<-f.s.gate
}
