package openlineage

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The values below are Python's (uuid.uuid5, re.sub, urllib.parse).
func TestUUIDsArePythons(t *testing.T) {
	if got := (Settings{Namespace: "tabularia"}).RunUUID(42); got != "9b2819a9-7c60-5cdf-9098-26ef20d74dba" {
		t.Fatal(got)
	}
	if got := (Settings{Namespace: "ns"}).NotebookRunUUID("nb-3-app-abc123"); got != "36a35198-73bd-5931-9911-eebff53c188b" {
		t.Fatal(got)
	}
}

func TestErrorTextHidesTheAddresses(t *testing.T) {
	if got := errorText("anna.rossi@acme.example and żółw@ex.pl x@y"); got != "<address> and <address> x@y" {
		t.Fatal(got)
	}
}

func TestQuoteIsPythons(t *testing.T) {
	if got := quote("/a b/ü:@&=+$,~._-"); got != "%2Fa%20b%2F%C3%BC%3A%40%26%3D%2B%24%2C~._-" {
		t.Fatal(got)
	}
}

func TestURLSplit(t *testing.T) {
	u, err := urlsplit("https://API.Ex.test:0443/v2/?k=1")
	if err != nil || u.scheme != "https" || u.hostname != "api.ex.test" || *u.port != 443 || u.path != "/v2/" {
		t.Fatal(u, err)
	}
	u, err = urlsplit("http://[::1]:80/x")
	if err != nil || u.hostname != "::1" || *u.port != 80 || u.path != "/x" {
		t.Fatal(u, err)
	}
	if _, err := urlsplit("http://h:x/"); err == nil {
		t.Fatal("a port that is not a number")
	}
}

func TestRunEventsGetTheClientsTags(t *testing.T) {
	e := pyjson.Object{{Key: "eventType", Value: "START"}, {Key: "run", Value: pyjson.Object{{Key: "runId", Value: "x"},
		{Key: "facets", Value: pyjson.Object{}}}}}
	out, _ := pyjson.Dumps(withClientTags(e), pyjson.Options{SortKeys: true})
	if !strings.Contains(out, `"tags": [{"key": "openlineage_client_version", "source": "OPENLINEAGE_CLIENT", "value": "1.53.0"}]`) {
		t.Fatal(out)
	}
	if facets := e.Value("run").(pyjson.Object).Value("facets").(pyjson.Object); len(facets) != 0 {
		t.Fatal("the event itself is not changed")
	}
	job := pyjson.Object{{Key: "job", Value: pyjson.Object{}}}
	if out, _ := pyjson.Dumps(withClientTags(job), pyjson.Options{}); strings.Contains(out, "tags") {
		t.Fatal("a JobEvent has no run")
	}
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestTheQueueSendsInOrderAndWaitIdleEmptiesIt(t *testing.T) {
	var got []string
	done := make(chan struct{}, 10)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, string(b))
		done <- struct{}{}
	}))
	defer srv.Close()
	file := filepath.Join(t.TempDir(), "ol.jsonl")
	e := New(Settings{URL: srv.URL, Endpoint: "api/v1/lineage", File: file, Namespace: "n", Timeout: 5}, nil, nil, func() float64 { return 2 }, quiet())
	for _, s := range []string{"a", "b"} {
		e.enqueue(item{label: s, ev: []pyjson.Object{{{Key: "n", Value: s}}}})
	}
	e.WaitIdle(5 * time.Second)
	if len(got) != 2 || got[0] != `{"n": "a"}` || got[1] != `{"n": "b"}` {
		t.Fatal(got)
	}
	raw, _ := os.ReadFile(file)
	if string(raw) != "{\"n\": \"a\"}\n{\"n\": \"b\"}\n" {
		t.Fatal(string(raw))
	}
	if e.Pending() != 0 {
		t.Fatal("pending")
	}
}

func TestAFullQueueDrops(t *testing.T) {
	block := make(chan struct{})
	var n atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		<-block
	}))
	defer srv.Close()
	e := New(Settings{URL: srv.URL, Endpoint: "x", Namespace: "n", Timeout: 30}, nil, nil, nil, quiet())
	sent := 0
	for i := 0; i < QueueMax+5; i++ {
		if e.enqueue(item{label: "r", ev: []pyjson.Object{{{Key: "i", Value: int64(i)}}}}) {
			sent++
		}
	}
	if sent < QueueMax || sent > QueueMax+1 || e.dropped == 0 {
		t.Fatal(sent, e.dropped)
	}
	close(block)
	e.WaitIdle(10 * time.Second)
}

func TestOffDoesNothing(t *testing.T) {
	var e *Emitter
	e.RunClosed(context.Background(), nil, 1)
	e.Renamed(context.Background(), nil, Names{{Kind: KindFlow, ID: 1, Name: "x"}})
	if names, err := e.CatalogNames(context.Background(), nil, Ref{Kind: KindFlow}); names != nil || err != nil {
		t.Fatal(names, err)
	}
	e.WaitIdle(time.Second)
	off := New(Settings{Namespace: "n"}, nil, nil, nil, quiet())
	if off.Enabled() {
		t.Fatal("on without a destination")
	}
}

func TestDisabledSendsNothing(t *testing.T) {
	n, err := Settings{URL: "http://127.0.0.1:1", Disabled: true}.Emit(context.Background(), []pyjson.Object{{}, {}})
	if n != 2 || err != nil {
		t.Fatal(n, err)
	}
}

func TestBothTransportsFailing(t *testing.T) {
	_, err := Settings{URL: "http://127.0.0.1:1", Endpoint: "x", File: "/nonexistent/dir/f", Timeout: 1}.Emit(context.Background(), []pyjson.Object{{}})
	if err == nil || !strings.HasPrefix(err.Error(), "None of the transports successfully emitted the event") {
		t.Fatal(err)
	}
}

func TestMarquezAPI(t *testing.T) {
	if got := marquezAPI(Settings{URL: "http://m:5000/", Endpoint: "/api/v1/lineage/"}); got != "http://m:5000/api/v1" {
		t.Fatal(got)
	}
	if got := marquezAPI(Settings{URL: "http://m:5000", Endpoint: "events"}); got != "" {
		t.Fatal(got)
	}
}
