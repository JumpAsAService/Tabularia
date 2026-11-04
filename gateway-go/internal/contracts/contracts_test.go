package contracts

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

type golden struct {
	Documents []struct{ Doc, Ok, Errors string }
	ODCS      []struct{ Columns, Document, YAML string } `json:"odcs"`
}

func load(t *testing.T) golden {
	raw, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// pythonRe stands in for the AI service's re.compile in the tests: the patterns of the golden
// cases, whose verdicts Python gave.
func pythonRe(_ context.Context, patterns []string) ([]bool, error) {
	out := make([]bool, len(patterns))
	for i, p := range patterns {
		out[i] = p != "(" && len(p) <= 500
		if p == "(?<=x)y" { // Python takes a look-behind, Go's regexp does not
			out[i] = true
		} else if _, err := regexp.Compile(p); err != nil {
			out[i] = false
		}
	}
	return out, nil
}

// TestValidateDocumentIsPythons compares ValidateDocument with contracts.validate_document
// (testdata/golden.json, written by testdata/gen_golden.py).
func TestValidateDocumentIsPythons(t *testing.T) {
	for _, c := range load(t).Documents {
		v, err := pyd.LoadsStr(c.Doc)
		if err != nil {
			t.Fatal(err)
		}
		doc, ok := v.(*pyd.Object)
		if !ok {
			t.Fatal(c.Doc)
		}
		out, err := ValidateDocument(context.Background(), doc, pythonRe)
		var invalid *Invalid
		switch {
		case errors.As(err, &invalid):
			got, _ := pyjson.Dumps(invalid.Errors, pyjson.Options{EnsureASCII: true})
			if got != c.Errors {
				t.Errorf("%s\n  go     %s\n  python %s %s", c.Doc, got, c.Errors, c.Ok)
			}
		case err != nil:
			t.Fatal(err)
		default:
			got, _ := pyjson.Dumps(PyjsonOf(out), pyjson.Options{SortKeys: true})
			if got != c.Ok {
				t.Errorf("%s\n  go     %s\n  python %s %s", c.Doc, got, c.Ok, c.Errors)
			}
		}
	}
}

func TestNoPatternNoCall(t *testing.T) {
	doc, _ := pyd.LoadsStr(`{"rules": [{"kind": "not_null", "severity": "error", "column": "a"}]}`)
	called := false
	_, err := ValidateDocument(context.Background(), doc.(*pyd.Object), func(context.Context, []string) ([]bool, error) {
		called = true
		return nil, errors.New("down")
	})
	if err != nil || called {
		t.Fatalf("err %v, called %v", err, called)
	}
	doc, _ = pyd.LoadsStr(`{"rules": [{"kind": "pattern", "severity": "error", "column": "a", "regex": "x"}]}`)
	if _, err := ValidateDocument(context.Background(), doc.(*pyd.Object), func(context.Context, []string) ([]bool, error) {
		return nil, errors.New("down")
	}); err == nil || errors.As(err, new(*Invalid)) {
		t.Fatalf("a service down is an error, not a refusal: %v", err)
	}
}

// TestODCSIsPythons compares ToODCS and DumpYAML with odcs.to_odcs and dump_yaml.
func TestODCSIsPythons(t *testing.T) {
	g := load(t)
	cols, _ := pyd.LoadsStr(g.ODCS[0].Columns)
	doc, _ := pyd.LoadsStr(g.ODCS[0].Document)
	descs := pyd.NewObject()
	descs.Set("id", "the key")
	descs.Set("code", "")
	at := mustTime(t, "2026-01-02T03:04:05.000678Z")
	out, err := ToODCS(Input{DatasourceID: 7, Name: "Sales", Description: "the datasource", Columns: cols.([]any),
		ColumnDescriptions: descs, Document: doc.(*pyd.Object), Version: 3, Enabled: false, CreatedAt: &at})
	if err != nil {
		t.Fatal(err)
	}
	if y, err := DumpYAML(out); err != nil || y != g.ODCS[0].YAML {
		t.Errorf("%v\n--- go\n%s\n--- python\n%s", err, y, g.ODCS[0].YAML)
	}
	empty := pyd.NewObject()
	empty.Set("rules", []any{})
	out, err = ToODCS(Input{DatasourceID: 8, Name: "Empty", Columns: []any{}, ColumnDescriptions: pyd.NewObject(), Document: empty,
		Version: 1, Enabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if y, err := DumpYAML(out); err != nil || y != g.ODCS[1].YAML {
		t.Errorf("%v\n--- go\n%s\n--- python\n%s", err, y, g.ODCS[1].YAML)
	}
}

func mustTime(t *testing.T, s string) time.Time {
	v, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}
