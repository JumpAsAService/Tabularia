package modelcatalog

import (
	"bytes"
	"encoding/json"
	"fmt"
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
	if got := Models(); !reflect.DeepEqual(got, []string{"linear", "logistic", "kaplan_meier", "cox", "parametric_survival", "arima", "xgboost", "catboost", "clv", "bg_bb", "ab_test", "ab_plan"}) {
		t.Fatalf("models: %v", got)
	}
	groups := map[string]string{}
	for _, g := range Groups() {
		if want := map[bool]string{true: "python", false: "r"}[g.ID == "boosting"]; g.Language != want {
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
	if Limit("max_chart_points") != 2000 || Limit("max_terms") != 200 || Limit("max_cells") != 50_000_000 ||
		Limit("max_quantiles") != 9 || Limit("min_customers") != 20 || Limit("min_repeaters") != 5 || Limit("max_opportunities") != 1000 ||
		Limit("max_clv_steps") != 520 || Limit("max_top_customers") != 20 || Limit("palive_grid_frequency") != 10 || Limit("shap_cost_budget") != 20_000_000_000 || Limit("max_model_file_bytes") != 64<<20 ||
		Limit("max_variants") != 20 || Limit("max_looks") != 1000 || Limit("max_secondary_metrics") != 10 || Limit("posterior_points") != 200 || Limit("power_points") != 50 {
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
		{"parametric_survival", map[string]any{"time": "t", "event": "e", "event_value": "1"}, "ph_aft"},
		{"parametric_survival", map[string]any{"time": "t", "event": "e", "event_value": "1", "distribution": "exponential"}, "ph_aft"},
		{"parametric_survival", map[string]any{"time": "t", "event": "e", "event_value": "1", "distribution": "gengamma"}, "aft"},
		{"parametric_survival", map[string]any{"time": "t", "event": "e", "event_value": "1", "distribution": "gompertz"}, "ph"},
		{"parametric_survival", map[string]any{"time": "t", "event": "e", "event_value": "1", "distribution": "auto"}, "auto"},
		// the variant is made by a column being given: the spend part
		{"clv", map[string]any{"customer": "id", "frequency": "x", "recency": "t_x", "age": "T", "time_unit": "week"}, "purchases"},
		{"clv", map[string]any{"customer": "id", "frequency": "x", "recency": "t_x", "age": "T", "time_unit": "week", "spend": "m"}, "value"},
		{"bg_bb", map[string]any{"customer": "id", "frequency": "x", "recency": "t_x", "opportunities": "n", "period_unit": "year"}, "purchases"},
		{"bg_bb", map[string]any{"customer": "id", "frequency": "x", "recency": "t_x", "opportunities": "n", "period_unit": "year", "spend": "m"}, "value"},
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

// The check is idempotent: what it answers, given back, is answered again as it is, in the shape
// and in the values (an ARIMA was once refused for its own default). The same test is in the
// engine (backend/tests/test_models_catalog.py), the form (frontend/tests) and the assistant.

func asJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func checkTwice(t *testing.T, what, model string, params map[string]any) {
	t.Helper()
	first, err := Check(model, params)
	if err != nil {
		return // a combination the check refuses: nothing to repeat
	}
	second, err := Check(model, first)
	if err != nil {
		t.Errorf("%s: %s refuses its own answer: %v\n answer %s", what, model, err, asJSON(t, first))
		return
	}
	if a, b := asJSON(t, first), asJSON(t, second); a != b {
		t.Errorf("%s: %s: checking twice changes the answer:\n once  %s\n twice %s", what, model, a, b)
	}
}

// sampleParams are the parameters a model needs, with a column or a level each.
func sampleParams(m *Model) map[string]any {
	p := map[string]any{}
	for i, k := range m.Keys {
		if !m.Needs[k] {
			continue
		}
		kind := data.kinds[m.KindOf[k]]
		switch kind.Control {
		case "column":
			p[k] = fmt.Sprintf("col%d", i)
		case "columns":
			p[k] = []any{fmt.Sprintf("col%d", i)}
		case "level":
			p[k] = "yes"
		case "choice":
			p[k] = kind.Choices[0]
		}
	}
	return p
}

func TestCheckingTwiceIsCheckingOnce(t *testing.T) {
	for _, id := range Models() {
		m := data.byID[id]
		base := sampleParams(m)
		checkTwice(t, "the defaults", id, base)
		// every choice of every choosing key, alone and two by two: the variants
		type pick struct {
			key string
			val any
		}
		var picks []pick
		for _, k := range m.Keys {
			if kind := data.kinds[m.KindOf[k]]; kind.Control == "choice" && !m.Needs[k] {
				for _, c := range kind.Choices {
					picks = append(picks, pick{k, c})
				}
			}
			if data.kinds[m.KindOf[k]].Control == "boolean" {
				picks = append(picks, pick{k, true}, pick{k, false})
			}
		}
		with := func(ps ...pick) map[string]any {
			out := map[string]any{}
			for k, v := range base {
				out[k] = v
			}
			for _, p := range ps {
				out[p.key] = p.val
			}
			return out
		}
		for i, a := range picks {
			checkTwice(t, fmt.Sprintf("%s=%v", a.key, a.val), id, with(a))
			for _, b := range picks[i+1:] {
				if a.key != b.key {
					checkTwice(t, fmt.Sprintf("%s=%v %s=%v", a.key, a.val, b.key, b.val), id, with(a, b))
				}
			}
		}
	}
}

func TestEverySharedCaseThatPassesIsItsOwnAnswerToo(t *testing.T) {
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
		} `json:"cases"`
	}
	if err := json.Unmarshal(b, &file); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, c := range file.Cases {
		if c.Checked == nil {
			continue
		}
		n++
		checkTwice(t, c.What, c.Model, c.Params)
		again, err := Check(c.Model, c.Checked)
		if err != nil || asJSON(t, again) != asJSON(t, c.Checked) {
			t.Errorf("%s: the shared answer given back: %v\n got  %s\n want %s", c.What, err, asJSON(t, again), asJSON(t, c.Checked))
		}
	}
	if n < 15 {
		t.Errorf("only %d cases that pass", n)
	}
}

func TestAnEmptyListOfColumnsStaysAnEmptyList(t *testing.T) {
	got, err := Check("arima", map[string]any{"time": "t", "value": "v", "frequency": "month", "exog": []any{}})
	if err != nil {
		t.Fatal(err)
	}
	if enc := asJSON(t, got["exog"]); enc != "[]" {
		t.Errorf("exog: %s, want []", enc)
	}
}

// level_numbers: a number for each value of a column (the experiments' expected shares), read as the engine reads it.
func TestLevelNumbersAreReadAsTheEngineReadsThem(t *testing.T) {
	base := map[string]any{"variant": "arm", "control": "A", "metric_type": "binary", "metric": "converted"}
	with := func(shares any) map[string]any {
		p := map[string]any{}
		for k, v := range base {
			p[k] = v
		}
		p["expected_shares"] = shares
		return p
	}
	got, err := Check("ab_test", with(map[string]any{"B": 1.0, "A": 3.0}))
	if err != nil || !reflect.DeepEqual(got["expected_shares"], map[string]any{"A": 3.0, "B": 1.0}) {
		t.Errorf("shares: %v %v", got["expected_shares"], err)
	}
	if got, err := Check("ab_test", with(map[string]any{})); err != nil || len(got["expected_shares"].(map[string]any)) != 0 {
		t.Errorf("an empty object is none: %v %v", got["expected_shares"], err)
	}
	for shares, want := range map[string]any{
		"B is no number": map[string]any{"A": 1.0, "B": "half"},
		"B is zero":      map[string]any{"A": 1.0, "B": 0.0},
		"B is negative":  map[string]any{"B": -1.0, "A": 1.0},
	} {
		_, err := Check("ab_test", with(want))
		if err == nil || err.Error() != "ab_test: 'expected_shares.B' must be a number above 0" {
			t.Errorf("%s: %v", shares, err)
		}
	}
	for _, bad := range []any{[]any{1.0, 1.0}, 0.5, "A:1", map[string]any{"": 1.0}} {
		_, err := Check("ab_test", with(bad))
		if err == nil || err.Error() != "ab_test: 'expected_shares' must give a number to each value of the column, as {value: number}" {
			t.Errorf("%v: %v", bad, err)
		}
	}
	// the columns of the node are the variant, the metric…: the shares name no column
	checked, _ := Check("ab_test", with(map[string]any{"A": 1.0, "B": 1.0}))
	if cols := Columns("ab_test", checked); !reflect.DeepEqual(cols, []string{"arm", "converted"}) {
		t.Errorf("columns: %v", cols)
	}
}

// card: false (the experiments' nodes) makes no model card: no registration, no Predict.
func TestTheExperimentsNodesMakeNoCard(t *testing.T) {
	for model, want := range map[string]bool{"ab_test": false, "ab_plan": false, "linear": true, "xgboost": true, "clv": true, "nope": false} {
		if HasCard(model) != want {
			t.Errorf("HasCard(%s) = %v", model, !want)
		}
	}
	for _, m := range Models() {
		for _, v := range data.byID[m].Variants {
			if cols := PredictColumns(m, v.ID, map[string]any{}); len(cols) > 0 && !HasCard(m) {
				t.Errorf("%s: no card and Predict columns %v", m, cols)
			}
		}
	}
	if GroupOf("ab_test") != "experiments" || GroupOf("ab_plan") != "experiments" {
		t.Error("the group of the experiments' nodes")
	}
	// the planning node's variants are the metric's type, its output the planning grid
	if v := VariantOf("ab_plan", map[string]any{"metric_type": "ratio"}); v == nil || v.ID != "ratio" || len(v.Outputs["plan"]) != 10 {
		t.Errorf("ab_plan ratio: %+v", v)
	}
	if v := VariantOf("ab_test", map[string]any{"metric_type": "binary"}); v == nil || v.ID != "binary" || len(v.Outputs["variants"]) != 27 {
		t.Errorf("ab_test binary: %+v", v)
	}
}
