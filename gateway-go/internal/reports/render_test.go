package reports

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// A saved view's pivot reaches the engine as the Viewer sends it: the row keys, the
// columns to spread or both (the engine's rule), never neither.
func TestViewOperationsPivotWithOneSide(t *testing.T) {
	for in, want := range map[string]string{
		`{"pivotOn": true, "pivot": {"index": ["a"], "on": [""], "values": "v", "func": "sum"}}`: `[{"type": "pivot", "params": {"index": ["a"], "on": [], "values": "v", "func": "sum"}}]`,
		`{"pivotOn": true, "pivot": {"index": [""], "on": ["b"], "values": "v"}}`:                `[{"type": "pivot", "params": {"index": [], "on": ["b"], "values": "v", "func": "sum"}}]`,
		`{"pivotOn": true, "pivot": {"index": [""], "on": [""], "values": "v"}}`:                 `[]`,
		`{"pivotOn": true, "pivot": {"index": ["a"], "on": ["b"], "values": ""}}`:                `[]`,
		`{"pivotOn": false, "pivot": {"index": ["a"], "on": ["b"], "values": "v"}}`:              `[]`,
	} {
		spec, _ := pyjson.Loads(in)
		got, _ := pyjson.Dumps(ViewOperations(spec.(pyjson.Object), &Datasource{}), pyjson.Options{})
		if got != want {
			t.Errorf("%s:\n got %s\nwant %s", in, got, want)
		}
	}
}
