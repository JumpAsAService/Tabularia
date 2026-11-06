package reportdef

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// TestAsPythonChecks compares every case of testdata/cases.json with what
// services/report_definition.py answered (testdata/golden.py made both).
func TestAsPythonChecks(t *testing.T) {
	var cases []string
	var golden []map[string]any
	read := func(name string, v any) {
		raw, err := os.ReadFile("testdata/" + name)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(raw, v); err != nil {
			t.Fatal(err)
		}
	}
	read("cases.json", &cases)
	read("golden.json", &golden)
	failed := 0
	for i, text := range cases {
		def, err := pyjson.Loads(text)
		if err != nil {
			t.Fatal(err)
		}
		want := golden[i]
		checked, err := Check(def)
		var got map[string]any
		switch {
		case err == nil:
			s, _ := pyjson.Dumps(checked, pyjson.Options{EnsureASCII: true})
			got = map[string]any{"ok": s}
		case err == ErrInternal:
			got = map[string]any{"error": want["error"]}
		default:
			p, _ := AsProblem(err)
			orig, _ := pyjson.Loads(text)
			d, _ := pyjson.Dumps(p.Dict(orig), pyjson.Options{})
			var dm any
			json.Unmarshal([]byte(d), &dm)
			got = map[string]any{"problem": dm, "where": p.Where}
		}
		g, _ := json.Marshal(got)
		w, _ := json.Marshal(want)
		if string(g) != string(w) {
			failed++
			if failed <= 25 {
				t.Errorf("case %d %s\n got  %s\n want %s", i, clip(text), g, w)
			}
		}
	}
	if failed > 0 {
		t.Errorf("%d of %d cases differ", failed, len(cases))
	}
}

func clip(s string) string {
	if len(s) > 300 {
		return s[:300] + "…"
	}
	return s
}
