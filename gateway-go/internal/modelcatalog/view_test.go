package modelcatalog

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// a catalogue with a made-up model of the group `bayes`: its group, its limit, a shape and a kind of its own, a shape and a kind
// it shares with another model, and a shape called «bayes» that another group's model owns (the experiments' one).
const fakeCatalogue = `{
  "format_version": 1,
  "limits": {"max_terms": 200, "bayes_trace_points": 200, "zmax": 1, "bayes_rank_bins": 20},
  "kinds": {"column": {"control": "column"}, "draws_count": {"control": "integer"}, "alpha": {"control": "number"}},
  "section_shapes": {"model": {"level": "null"}, "bayes": {"level": "required"}, "quantity": {"level": "required"}, "point": {"level": "required"}},
  "output_types": {"results": {}, "draws": {}},
  "groups": [
    {"id": "regression", "language": "r", "label": "x", "models": ["linear"]},
    {"id": "bayes", "language": "python", "label": "y", "models": ["bayes_model"]},
    {"id": "experiments", "language": "r", "label": "z", "models": ["ab"]}
  ],
  "models": [
    {"id": "linear", "group": "regression", "takes": {"alpha": "alpha", "by": "column"}, "variants": [{"sections": {"model": {}, "point": {}}}]},
    {"id": "bayes_model", "group": "bayes", "takes": {"draws": "draws_count", "alpha": "alpha", "col": "column"},
      "variants": [{"sections": {"model": {}, "quantity": {}, "point": {}}}], "output": "draws"},
    {"id": "ab", "group": "experiments", "variants": [{"sections": {"bayes": {}}}]}
  ],
  "predict": {"takes": {}}
}`

const fakeSchema = `{
  "title": "card",
  "required": ["format", "bayes"],
  "properties": {
    "format": {"type": "string"},
    "bayes": {"type": "object"},
    "arima": {"type": "object"}
  },
  "allOf": [
    {"if": {"properties": {"model": {"enum": ["arima"]}}}, "then": {"required": ["arima"]}},
    {"if": {"properties": {"model": {"const": "bayes_model"}}}, "then": {"required": ["bayes"]}},
    {"if": {"properties": {"group": {"const": "bayes"}}, "required": ["group"]}, "then": {"required": ["bayes"]}}
  ]
}
`

// the catalogue as it is shipped holds the group `bayes` (its entry, its limits, its shapes, its kinds, its outputs): the group gets it
// whole, and every other group gets the catalogue as it was before the entry, byte for byte (recorded in testdata/before-bayes.json)
func TestTheOtherGroupsViewIsTheCatalogueBeforeTheEntry(t *testing.T) {
	var before struct {
		Catalog struct {
			Bytes  int    `json:"bytes"`
			SHA256 string `json:"sha256"`
		} `json:"catalog_compact"`
	}
	b, err := os.ReadFile("testdata/before-bayes.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &before); err != nil {
		t.Fatal(err)
	}
	for _, group := range []string{"regression", "survival", "timeseries", "boosting", "customer_lifetime", "experiments", "foundation_forecast"} {
		view := CompactFor(group)
		sum := sha256.Sum256(view)
		if len(view) != before.Catalog.Bytes || hex.EncodeToString(sum[:]) != before.Catalog.SHA256 {
			t.Errorf("%s: the view is %d bytes, sha256 %x: not the catalogue before the entry (%d bytes, %s)", group, len(view), sum, before.Catalog.Bytes, before.Catalog.SHA256)
		}
	}
	if !bytes.Equal(CompactFor(BayesGroup), Compact()) {
		t.Error("the group bayes did not receive the catalogue whole")
	}
	whole := string(Compact())
	for _, want := range []string{`"id":"bayes_model"`, `"bayes_max_pair_quantities"`, `"bayes_mode"`, `"output_bayes"`, `"prior_check":{`, `"replicated":{`} {
		if !strings.Contains(whole, want) {
			t.Errorf("the catalogue as shipped lacks %s", want)
		}
	}
	// and the other groups' view holds none of the group's
	view := string(CompactFor("regression"))
	for _, gone := range []string{`"bayes_model"`, `"bayes_`, `output_bayes`, `"prior_check"`, `"replicated"`, `"sampler":{"level"`} {
		if strings.Contains(view, gone) {
			t.Errorf("the view still holds %s", gone)
		}
	}
	// what another group's model writes with the same word stays: a reading block «summary» is no section «summary» of the shapes
	if !strings.Contains(view, `"reading":{"level":"required"`) || !strings.Contains(view, `"bayes":{"level":"required"`) {
		t.Error("the view lost a shape another group's model has")
	}
}

func TestAnotherGroupsViewHoldsNothingOfTheGroupBayes(t *testing.T) {
	view := compactFor([]byte(fakeCatalogue), "regression")
	var got map[string]any
	if err := json.Unmarshal(view, &got); err != nil {
		t.Fatal(err)
	}
	text := string(view)
	for _, gone := range []string{`"bayes_model"`, `"bayes_trace_points"`, `"bayes_rank_bins"`, `"draws_count"`, `"quantity"`, `"python"`, `"draws":{}`} {
		if strings.Contains(text, gone) {
			t.Errorf("the view still holds %s: %s", gone, text)
		}
	}
	// what is shared, or another group's, stays, in the file's order
	for _, kept := range []string{`"max_terms":200,"zmax":1`, `"column":{`, `"alpha":{`, `"model":{"level":"null"},"bayes":{"level":"required"},"point"`,
		`"regression"`, `"experiments"`, `"linear"`, `"ab"`} {
		if !strings.Contains(text, kept) {
			t.Errorf("the view lost %s: %s", kept, text)
		}
	}
	if !strings.Contains(text, `"output_types":{"results":{}}`) && !strings.Contains(text, `"output_types":{}`) {
		t.Errorf("output_types: %s", text)
	}
	if strings.Index(text, `"regression"`) > strings.Index(text, `"experiments"`) {
		t.Error("the order of the groups changed")
	}
}

func TestEveryOtherGroupGetsTheSameViewAndBayesGetsTheWholeCatalogue(t *testing.T) {
	cat := []byte(fakeCatalogue)
	if !bytes.Equal(compactFor(cat, "regression"), compactFor(cat, "experiments")) {
		t.Error("two groups other than bayes got different views")
	}
	whole := compactFor(cat, BayesGroup)
	if !bytes.Equal(whole, compactOf(cat)) {
		t.Error("the group bayes did not receive the catalogue whole")
	}
	for _, want := range []string{`"bayes_model"`, `"bayes_trace_points"`, `"draws_count"`, `"quantity"`} {
		if !strings.Contains(string(whole), want) {
			t.Errorf("the group bayes lost %s", want)
		}
	}
}

func TestTheSchemaView(t *testing.T) {
	cat := []byte(fakeCatalogue)
	view := string(schemaFor([]byte(fakeSchema), cat, "survival"))
	var parsed map[string]any
	if err := json.Unmarshal([]byte(view), &parsed); err != nil {
		t.Fatalf("%v\n%s", err, view)
	}
	if strings.Contains(view, "bayes") {
		t.Errorf("the schema's view still names the group: %s", view)
	}
	if !strings.Contains(view, `"arima"`) || len(parsed["allOf"].([]any)) != 1 || !strings.HasSuffix(view, "}\n") {
		t.Errorf("the view lost what is another's: %s", view)
	}
	if strings.Index(view, `"format"`) > strings.Index(view, `"arima"`) {
		t.Error("the order changed")
	}
	if got := string(schemaFor([]byte(fakeSchema), cat, BayesGroup)); got != fakeSchema {
		t.Error("the group bayes did not receive the schema whole")
	}
	// the shipped schema holds no such object: it is returned as it is
	shipped := []byte(`{"properties": {"a": {}}}`)
	if got := schemaFor(shipped, cat, "survival"); !bytes.Equal(got, shipped) {
		t.Error("a schema with nothing of the group changed")
	}
}
