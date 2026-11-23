package bayesgraph

import (
	"bytes"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// FuzzDecode feeds the gateway's whole entry (Decode, then the validator) with arbitrary bytes. Its
// seeds are the corpus and the four families: they run in every go test. The 10-minute run of lot M3:
//
//	go test -run='^$' -fuzz=FuzzDecode -fuzztime=10m -parallel 2 ./internal/bayesgraph/
func FuzzDecode(f *testing.F) {
	for _, b := range corpusInputs(f) {
		f.Add(b)
	}
	for _, s := range []string{regression, logistic, ar2, locallevel, example, cpNested, ``, `{}`, `null`, `{"format_version":1}`} {
		f.Add([]byte(s))
	}
	lim := bayescatalog.GetLimits()
	f.Fuzz(func(t *testing.T, data []byte) {
		g, fs := Decode(data)
		if len(data) > lim.MaxGraphBytes {
			if g != nil || len(fs) != 1 || fs[0].Code != "graph.too_big" {
				t.Fatalf("%d bytes are over max_graph_bytes: %+v", len(data), fs)
			}
			return
		}
		if g == nil {
			if len(fs) == 0 {
				t.Fatal("no graph and no finding")
			}
			return
		}
		if len(g.Nodes) > lim.MaxNodes || len(g.Plates) > lim.MaxPlates {
			t.Fatalf("%d nodes and %d plates are over the limits", len(g.Nodes), len(g.Plates))
		}
		type result struct {
			fs    []Finding
			model *Model
		}
		done := make(chan result, 1)
		go func() {
			fs := Validate(g)
			done <- result{fs, BuildModel(g)}
		}()
		var res result
		select {
		case res = <-done:
		case <-time.After(20 * time.Second):
			t.Fatalf("Validate does not end on %q", data)
		}
		sorted := append([]Finding(nil), res.fs...)
		SortFindings(sorted)
		if a, _ := json.Marshal(sorted); !bytes.Equal(a, mustJSON(res.fs)) {
			t.Fatalf("the findings are not sorted:\n%s", codes(res.fs))
		}
		if res.model == nil || !bytes.Equal(mustJSON(res.model.Findings), mustJSON(res.fs)) {
			t.Fatal("BuildModel and Validate disagree")
		}
		for _, f := range res.fs {
			if f.Severity != Error && f.Severity != Warning {
				t.Fatalf("severity %q of %s", f.Severity, f.Code)
			}
		}
		// a canonical form that can be written is a document that reads back to the same bytes
		c1, err := Canonical(g)
		if err != nil {
			return
		}
		g2, fs2 := Decode(c1)
		if g2 == nil {
			t.Fatalf("the canonical form does not decode: %+v\n%s", fs2, c1)
		}
		if c2, err := Canonical(g2); err != nil || !bytes.Equal(c1, c2) {
			t.Fatalf("the canonical form is not stable:\n%s\n%s", c1, c2)
		}
	})
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprint(err))
	}
	return b
}
