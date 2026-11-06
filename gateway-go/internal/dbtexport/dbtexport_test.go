package dbtexport

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// testdata/python_cases.json is what services/dbt_export.py, dbt_messages.py and
// routes/flows.py answer for these inputs (made with the gateway's image).
type pythonCases struct {
	Slug, Within, Folder, Jinja, CD map[string]string
	Translate                       [][]any
}

func loadCases(t *testing.T) pythonCases {
	raw, err := os.ReadFile("testdata/python_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var c pythonCases
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestNamesAsPython(t *testing.T) {
	c := loadCases(t)
	for in, want := range c.Slug {
		if got := Slug(in); got != want {
			t.Errorf("Slug(%q) = %q, want %q", in, got, want)
		}
	}
	for in, want := range c.Within {
		if got := within(in, 23); got != want {
			t.Errorf("within(%q) = %q, want %q", in, got, want)
		}
	}
	for in, want := range c.Folder {
		if got := DefaultFolder(in); got != want {
			t.Errorf("DefaultFolder(%q) = %q, want %q", in, got, want)
		}
	}
	for in, want := range c.Jinja {
		if got := jinjaString(in); got != want {
			t.Errorf("jinjaString(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTranslateAsPython(t *testing.T) {
	for _, tc := range loadCases(t).Translate {
		raw, _ := json.Marshal(tc[1])
		params, _ := pyjson.Loads(string(raw))
		got, err := Translate(tc[0], params, tc[2].(string), tc[3].(string))
		if err != nil {
			t.Fatal(err)
		}
		want := tc[4]
		if want == nil {
			want = ""
		}
		if got != want {
			t.Errorf("Translate(%v, %v, %v) = %q, want %q", tc[0], tc[1], tc[2], got, want)
		}
	}
}

func TestIncompleteStep(t *testing.T) {
	for in, want := range map[string]string{
		`{"type": "filter", "params": {"column": "a", "operator": "between", "value": [1]}}`: "filter_value_missing",
		`{"type": "filter", "params": {"column": "a", "operator": "is_null"}}`:               "",
		`{"type": "join", "params": {"right": {}, "left_on": ["a"], "right_on": "b"}}`:       "",
		`{"type": "join", "params": {"right": 1, "left_on": ["a", "c"], "right_on": "b"}}`:   "step_invalid",
		`{"type": "join", "params": {"right": 1, "how": "cross"}}`:                           "",
		`{"type": "group_by", "params": {"by": ["a"], "aggregations": [{"func": "sum"}]}}`:   "step_invalid",
		`{"type": "select", "params": {"columns": []}}`:                                      "step_invalid",
		`{"type": "nope", "params": 5}`:                                                      "",
		`{"type": "pivot", "params": {"index": ["a"], "values": "v"}}`:                       "",
		`{"type": "pivot", "params": {"on": ["a"], "index": [], "values": "v"}}`:             "",
		`{"type": "pivot", "params": {"index": [], "on": [], "values": "v"}}`:                "step_invalid",
		`{"type": "pivot", "params": {"index": ["a"], "on": ["b"]}}`:                         "step_invalid",
	} {
		op, _ := pyjson.Loads(in)
		got := ""
		if e := incompleteStep(op); e != nil {
			got = e.Code
		}
		if got != want {
			t.Errorf("%s: %q, want %q", in, got, want)
		}
	}
}

func TestThousandsAndRebuildable(t *testing.T) {
	if thousands(1234567) != "1,234,567" || thousands(50000) != "50,000" || thousands(12) != "12" {
		t.Error("thousands")
	}
	if !rebuildable("orders") || rebuildable("ùàòìéèùàòìéèùàòìéèùàòìéèùàòì") {
		t.Error("rebuildable")
	}
}
