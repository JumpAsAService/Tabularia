package bayescontract_test

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
)

// The entry `bayes_model` of the models' catalogue (backend/app/engine/models/catalog.py, lot Q1 of docs/specs/bayes-report.md) says
// what this package declares: the group, the modes and outputs, the sections of both variants with their numbers and texts, the
// columns of the outputs and their types, the shapes of the sections, the limits with their defaults. The two are written by hand in
// two languages; this test is what keeps them one.

type entry struct {
	Groups []struct {
		ID       string   `json:"id"`
		Language string   `json:"language"`
		Models   []string `json:"models"`
	} `json:"groups"`
	Models []struct {
		ID       string            `json:"id"`
		Group    string            `json:"group"`
		Card     bool              `json:"card"`
		Needs    map[string]string `json:"needs"`
		Takes    map[string]string `json:"takes"`
		Defaults map[string]any    `json:"defaults"`
		Variants []struct {
			ID       string                           `json:"id"`
			When     map[string]any                   `json:"when"`
			Sections map[string]bayescontract.Section `json:"sections"`
			Charts   []any                            `json:"charts"`
			Outputs  map[string][]string              `json:"outputs"`
		} `json:"variants"`
	} `json:"models"`
	Kinds map[string]struct {
		Control string   `json:"control"`
		Choices []string `json:"choices"`
	} `json:"kinds"`
	Limits      map[string]int64               `json:"limits"`
	Shapes      map[string]bayescontract.Shape `json:"section_shapes"`
	OutputTypes map[string]map[string]string   `json:"output_types"`
	Predict     json.RawMessage                `json:"predict"`
}

func loadEntry(t *testing.T) (entry, int) {
	t.Helper()
	var c entry
	if err := json.Unmarshal(modelcatalog.Raw(), &c); err != nil {
		t.Fatal(err)
	}
	for i, m := range c.Models {
		if m.ID == bayescontract.Model {
			return c, i
		}
	}
	t.Fatal("the catalogue has no model bayes_model")
	return c, -1
}

// nonNull: the catalogue writes an empty list where the declaration has nil (the engine reads the lists of a section as lists).
func nonNull(s bayescontract.Section) bayescontract.Section {
	if s.Numbers == nil {
		s.Numbers = []string{}
	}
	if s.Texts == nil {
		s.Texts = []string{}
	}
	return s
}

func TestTheCatalogueEntryIsTheDeclaration(t *testing.T) {
	c, at := loadEntry(t)
	m := c.Models[at]
	if m.Group != bayescontract.Group || !m.Card || len(m.Needs) != 0 || !reflect.DeepEqual(m.Takes, map[string]string{"mode": "bayes_mode", "output": "output_bayes"}) {
		t.Errorf("the model: group %q, card %v, needs %v, takes %v", m.Group, m.Card, m.Needs, m.Takes)
	}
	found := false
	for _, g := range c.Groups {
		if g.ID == bayescontract.Group {
			found = true
			if g.Language != "python" || !reflect.DeepEqual(g.Models, []string{bayescontract.Model}) {
				t.Errorf("the group: %+v", g)
			}
		}
	}
	if !found {
		t.Error("the catalogue has no group bayes")
	}
	// the modes the node takes are the runner's two sampling modes, and the outputs the contract's, the default first
	if got := c.Kinds["bayes_mode"].Choices; !reflect.DeepEqual(got, []string{bayescontract.ModeSample, bayescontract.ModePriorPredictive}) {
		t.Errorf("modes: %v", got)
	}
	if got := c.Kinds["output_bayes"].Choices; !reflect.DeepEqual(got, bayescontract.Outputs) {
		t.Errorf("outputs: %v, the contract's %v", got, bayescontract.Outputs)
	}
	if m.Defaults["mode"] != bayescontract.ModeSample || m.Defaults["output"] != bayescontract.OutputSummary {
		t.Errorf("defaults: %v", m.Defaults)
	}
	// the variants and their sections, section by section
	if len(m.Variants) != 2 {
		t.Fatalf("variants: %d", len(m.Variants))
	}
	for _, v := range m.Variants {
		want, ok := bayescontract.Variants[v.ID]
		if !ok || v.When["mode"] != v.ID {
			t.Errorf("the variant %q: when %v", v.ID, v.When)
			continue
		}
		if len(v.Sections) != len(want) {
			t.Errorf("%s: %d sections, the declaration's %d", v.ID, len(v.Sections), len(want))
		}
		for name, section := range want {
			got, ok := v.Sections[name]
			if !ok {
				t.Errorf("%s: no section %s", v.ID, name)
				continue
			}
			if got.Levels != nil {
				t.Errorf("%s.%s: levels %v (any, from the model and the data)", v.ID, name, got.Levels)
			}
			if !reflect.DeepEqual(nonNull(got), nonNull(section)) {
				t.Errorf("%s.%s: the catalogue says %+v, the declaration %+v", v.ID, name, got, section)
			}
			if _, ok := c.Shapes[name]; !ok && name != "reading" {
				t.Errorf("section %s has no shape", name)
			}
		}
	}
	// the outputs: the columns of the sample mode's three, in the declaration's order and of its types; none for the prior predictive
	sample, prior := m.Variants[0], m.Variants[1]
	if sample.ID != bayescontract.ModeSample || prior.ID != bayescontract.ModePriorPredictive || len(prior.Outputs) != 0 {
		t.Errorf("variants: %s, %s, the prior predictive's outputs %v", sample.ID, prior.ID, prior.Outputs)
	}
	for output, columns := range bayescontract.OutputColumns {
		var names []string
		for _, col := range columns {
			names = append(names, col.Name)
			if got := c.OutputTypes[output][col.Name]; got != col.Type {
				t.Errorf("%s.%s: the catalogue says %q, the declaration %q", output, col.Name, got, col.Type)
			}
		}
		if !reflect.DeepEqual(sample.Outputs[output], names) {
			t.Errorf("%s: the catalogue says %v, the declaration %v", output, sample.Outputs[output], names)
		}
		if len(c.OutputTypes[output]) != len(columns) {
			t.Errorf("%s: %d typed columns, the declaration's %d", output, len(c.OutputTypes[output]), len(columns))
		}
	}
	if len(sample.Outputs) != len(bayescontract.OutputColumns) {
		t.Errorf("the sample mode hands on %d outputs", len(sample.Outputs))
	}
}

func TestTheCatalogueShapesAndLimitsAreTheDeclarations(t *testing.T) {
	c, _ := loadEntry(t)
	for name, shape := range bayescontract.Shapes {
		got, ok := c.Shapes[name]
		if !ok || !reflect.DeepEqual(got, shape) {
			t.Errorf("the shape of %s: the catalogue says %+v, the declaration %+v", name, got, shape)
		}
	}
	// the limits of the catalogue (where: catalogue) are the group's `bayes_` keys, with the declaration's defaults, and nothing else
	for _, l := range bayescontract.Limits {
		got, in := c.Limits[l.Key]
		if l.Where == "catalogue" {
			if !in || got != l.Default || !strings.HasPrefix(l.Key, "bayes_") {
				t.Errorf("the limit %s: the catalogue says %d (%v), the declaration %d", l.Key, got, in, l.Default)
			}
		} else if in {
			t.Errorf("the limit %s is the %s's, not the catalogue's", l.Key, l.Where)
		}
	}
	for key := range c.Limits {
		if strings.HasPrefix(key, "bayes_") && bayescontract.LimitDefault(key) == 0 {
			t.Errorf("the catalogue has a limit %s the declaration does not", key)
		}
	}
	// the ten columns, bounded by construction under the engine's limit
	if most := bayescontract.MaxResultRows(200); most >= c.Limits["max_result_rows"] {
		t.Errorf("at most %d rows, the engine's limit %d", most, c.Limits["max_result_rows"])
	}
	// Predict does not name it
	if strings.Contains(string(c.Predict), bayescontract.Model) {
		t.Error("predict names bayes_model")
	}
}
