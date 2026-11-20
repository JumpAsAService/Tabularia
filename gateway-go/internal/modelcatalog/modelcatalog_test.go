package modelcatalog

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

// The copy is the engine's snapshot (backend/app/engine/models/catalog.json), byte for byte:
// write it again with `python3 .github/scripts/model_catalog.py` when the catalogue changes.
func TestTheCopyIsTheEnginesSnapshot(t *testing.T) {
	want, err := os.ReadFile("../../../backend/app/engine/models/catalog.json")
	if err != nil {
		t.Skip("the engine's sources are not beside the gateway's: ", err)
	}
	if !bytes.Equal(raw, want) {
		t.Fatal("internal/modelcatalog/catalog.json differs from backend/app/engine/models/catalog.json: run python3 .github/scripts/model_catalog.py")
	}
}

// The engine and the gateway answer the same cases with the same words.
func TestTheSharedCases(t *testing.T) {
	b, err := os.ReadFile("../../../backend/tests/fixtures/models/params_cases.json")
	if err != nil {
		t.Skip("the engine's sources are not beside the gateway's: ", err)
	}
	var file struct {
		Cases []struct {
			What    string         `json:"what"`
			Model   string         `json:"model"`
			Params  map[string]any `json:"params"`
			Checked map[string]any `json:"checked"`
			Error   string         `json:"error"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(b, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) < 40 {
		t.Fatalf("only %d cases", len(file.Cases))
	}
	for _, c := range file.Cases {
		got, err := Check(c.Model, c.Params)
		if c.Error != "" {
			if err == nil || err.Error() != c.Error {
				t.Errorf("%s: got %v, want %q", c.What, err, c.Error)
			}
			continue
		}
		if err != nil {
			t.Errorf("%s: refused: %v", c.What, err)
			continue
		}
		// what the gateway answers, as JSON, is what the engine answers
		var normal map[string]any
		enc, _ := json.Marshal(got)
		_ = json.Unmarshal(enc, &normal)
		if !reflect.DeepEqual(normal, c.Checked) {
			t.Errorf("%s:\n got %v\nwant %v", c.What, normal, c.Checked)
		}
	}
}

func TestGroupsModelsAndVariants(t *testing.T) {
	if got := Models(); !reflect.DeepEqual(got, []string{"linear", "logistic", "kaplan_meier", "cox", "arima"}) {
		t.Fatalf("models: %v", got)
	}
	groups := map[string]string{}
	for _, g := range Groups() {
		if g.Language != "r" {
			t.Errorf("%s: language %q", g.ID, g.Language)
		}
		for _, m := range g.Models {
			groups[m] = g.ID
		}
	}
	for _, m := range Models() {
		if GroupOf(m) == "" || GroupOf(m) != groups[m] {
			t.Errorf("%s: group %q, listed by %q", m, GroupOf(m), groups[m])
		}
	}
	if GroupOf("nope") != "" || KindOf("linear", "penalty") != "penalty" || KindOf("linear", "nope") != "" {
		t.Error("GroupOf or KindOf")
	}
	if Limit("max_chart_points") != 2000 || Limit("max_terms") != 200 {
		t.Error("limits")
	}
	cases := []struct {
		model  string
		params map[string]any
		want   string
	}{
		{"linear", map[string]any{"outcome": "y", "predictors": []any{"x"}}, "unpenalized"},
		{"linear", map[string]any{"outcome": "y", "predictors": []any{"x"}, "penalty": "lasso"}, "penalized"},
		{"logistic", map[string]any{"outcome": "y", "event_value": "1", "predictors": []any{"x"}, "penalty": "ridge"}, "penalized"},
		{"kaplan_meier", map[string]any{"time": "t", "event": "e", "event_value": "1"}, "default"},
		{"cox", map[string]any{"time": "t", "event": "e", "event_value": "1", "predictors": []any{"x"}}, "default"},
		{"arima", map[string]any{"time": "t", "value": "v", "frequency": "month"}, "manual"},
		{"arima", map[string]any{"time": "t", "value": "v", "frequency": "month", "orders": "auto"}, "auto"},
	}
	for _, c := range cases {
		checked, err := Check(c.model, c.params)
		if err != nil {
			t.Fatalf("%s: %v", c.model, err)
		}
		v := VariantOf(c.model, checked)
		if v == nil || v.ID != c.want {
			t.Errorf("%s %v: variant %v, want %s", c.model, c.params, v, c.want)
			continue
		}
		if _, ok := v.Sections["model"]; !ok || len(v.Outputs) == 0 {
			t.Errorf("%s/%s: no model section or no outputs", c.model, v.ID)
		}
	}
	if VariantOf("nope", nil) != nil {
		t.Error("a variant of no model")
	}
	pen := VariantOf("linear", map[string]any{"penalty": "lasso"})
	for _, s := range pen.Sections["coef"].Numbers {
		if s == "p_value" || s == "std_error" {
			t.Errorf("a penalized coefficient has %s", s)
		}
	}
}

// What a caller gets is its own: changing it does not change the catalogue.
func TestTheListsAreCopies(t *testing.T) {
	g := Groups()
	g[0].Models[0] = "changed"
	r := Raw()
	r[0] = 'x'
	if Groups()[0].Models[0] == "changed" || Raw()[0] == 'x' {
		t.Error("a caller changed the catalogue")
	}
}

func TestABrokenSnapshotIsRefused(t *testing.T) {
	for _, broken := range []string{"{", `{"groups": [], "models": []}`,
		`{"groups": [{"id": "g", "models": []}], "kinds": {}, "models": [{"id": "a", "group": "g", "family": "f", "label": "l", "needs": {}, "takes": {}, "defaults": {}, "applies": {}, "requires": [], "forbids": [], "disjoint": [], "variants": [{"id": "v", "when": {}}]}]}`,
		`{"groups": [{"id": "g", "models": ["a"]}], "kinds": {}, "models": [{"id": "a", "group": "g", "family": "f", "label": "l", "needs": {"x": "nope"}, "takes": {}, "defaults": {}, "applies": {}, "requires": [], "forbids": [], "disjoint": [], "variants": [{"id": "v", "when": {}}]}]}`,
	} {
		if _, err := parse([]byte(broken)); err == nil {
			t.Errorf("accepted: %s", broken)
		}
	}
}

func TestColumnsAreWhatTheModelReads(t *testing.T) {
	checked, err := Check("linear", map[string]any{"outcome": "y", "predictors": []any{"x", "c"}, "weights": "w", "se": "cluster", "cluster": "c"})
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(Columns("linear", checked), ","); got != "y,x,c,w" {
		t.Errorf("linear: %s", got)
	}
	checked, err = Check("arima", map[string]any{"value": "sales", "time": "month", "frequency": "month", "exog": []any{"price"}})
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(Columns("arima", checked), ","); got != "month,sales,price" {
		t.Errorf("arima, the time first: %s", got)
	}
	if Columns("nope", nil) != nil {
		t.Error("no model, no columns")
	}
}
