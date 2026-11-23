package dbtexport

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/statcatalog"
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

// A hypothesis test has no SQL to hand to dbt: every operation of the catalogue is refused by
// its name, on every target, wherever it sits in the flow, with the engine's code and words.
func TestAHypothesisTestIsNotExported(t *testing.T) {
	model := func(operations string) []any {
		v, err := pyjson.Loads(`{"name": "m", "operations": ` + operations + `}`)
		if err != nil {
			t.Fatal(err)
		}
		o := v.(pyjson.Object)
		return []any{&o}
	}
	filter := `{"type": "filter", "params": {"column": "a", "operator": "is_null"}}`
	if len(statcatalog.Operations()) == 0 {
		t.Fatal("the catalogue has no test")
	}
	for _, op := range statcatalog.Operations() {
		test := `{"type": "` + op + `", "params": {"value": "v", "group": "g"}}`
		for name, operations := range map[string]string{
			"last step":       `[` + filter + `, ` + test + `]`,
			"in the middle":   `[` + test + `, ` + filter + `]`,
			"a join's branch": `[{"type": "join", "params": {"on": ["a"], "right": {"source": {"key": "k"}, "operations": [` + test + `]}}}]`,
		} {
			for _, target := range []string{"duckdb", "native", "clickhouse"} {
				e := knownRefusal(model(operations), target)
				if e == nil || e.Code != "op_not_exportable" || e.Params.Value("op") != op {
					t.Fatalf("%s, %s, %s: %#v", op, name, target, e)
				}
				if e.Message != "operation '"+op+"' cannot be exported to dbt" {
					t.Errorf("%s: %q", op, e.Message)
				}
				if got := ErrorMessage(e, "en"); got != "The operation «"+op+"» cannot be exported to dbt." {
					t.Errorf("%s: %q", op, got)
				}
				if got := ErrorMessage(e, "it"); got != "L'operazione «"+op+"» non si esporta in dbt." {
					t.Errorf("%s: %q", op, got)
				}
			}
		}
		// a loop is said first, as before: its sentence tells what to do with the flow
		if e := knownRefusal(model(`[{"type": "foreach", "params": {}}, `+test+`]`), "duckdb"); e == nil || e.Code != "foreach_not_exportable" {
			t.Errorf("%s after a foreach: %#v", op, e)
		}
	}
	// two tests: the first of the flow is the one named
	if ops := statcatalog.Operations(); len(ops) > 1 {
		both := `[{"type": "` + ops[1] + `", "params": {}}, {"type": "` + ops[0] + `", "params": {}}]`
		if e := knownRefusal(model(both), "duckdb"); e == nil || e.Params.Value("op") != ops[1] {
			t.Errorf("two tests: %#v", e)
		}
	}
	// what only looks like one is not the catalogue's business, and the flows of before pass
	for _, operations := range []string{`[` + filter + `]`, `[{"type": "stat_nope", "params": {}}]`, `[]`} {
		if e := knownRefusal(model(operations), "duckdb"); e != nil {
			t.Errorf("%s: %#v", operations, e)
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

// A file loaded in the editor has its rows in its node: over SeedMaxRows the plan offered the
// targets and the download then failed (the engine counts the rows); now both refuse it, naming
// the file.
func TestAFileSeedOverTheCapIsRefusedByItsNode(t *testing.T) {
	for rows, refused := range map[string]bool{"50000": false, "50001": true, "50001.0": true, "null": false} {
		p := newProject(context.Background(), emptyCatalog{}, "b", "clickhouse", nil, nil)
		def := `{"nodes": [{"id": "a", "type": "source", "data": {"parquetKey": "files/x.parquet", "filename": "orders big.csv", "rows": ` + rows + `}}]}`
		p.context(&Flow{ID: 1, Name: "f", Definition: def})
		err := pyval.Catch(func() { p.seed(nil, "files/x.parquet") })
		if !refused {
			if err != nil {
				t.Errorf("rows %s: %v", rows, err)
			}
			continue
		}
		e, ok := err.(*Error)
		if !ok || e.Code != "seed_too_large" || e.Params.Value("source") != "orders big.csv" || e.Params.Value("rows") != "50,001" {
			t.Errorf("rows %s: %#v", rows, err)
		}
	}
}

// emptyCatalog is a database with no rows at all.
type emptyCatalog struct{}

type noRow struct{}

func (noRow) Scan(...any) error { return pgx.ErrNoRows }

func (emptyCatalog) QueryRow(context.Context, string, ...any) pgx.Row { return noRow{} }
func (emptyCatalog) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, pgx.ErrNoRows
}

// A model node runs on the cluster, not in dbt (docs/specs/statistical-models.md, «What
// exists and is reused»): an Output that reads it refuses the export, one that does not
// leaves it out with a note.
func TestAModelIsNotExported(t *testing.T) {
	for _, typ := range []string{"model", "model_predict", "bayes_model"} {
		def, err := pyjson.Loads(`{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
			{"id": "m", "type": "` + typ + `", "data": {"label": "Elasticity"}},
			{"id": "o", "type": "output", "data": {"name": "coefficients"}}, {"id": "o2", "type": "output", "data": {"name": "rows"}}],
			"edges": [{"source": "s", "target": "m"}, {"source": "m", "target": "o"}, {"source": "s", "target": "o2"}]}`)
		if err != nil {
			t.Fatal(err)
		}
		p := &Project{}
		err = pyval.Catch(func() { p.withoutScript(&Flow{Name: "Pricing"}, def, nil) })
		e, ok := err.(*Error)
		if !ok || e.Code != "model_not_exportable" || e.Params.Value("model") != "Elasticity" || e.Params.Value("output") != "coefficients" {
			t.Fatalf("%s: %#v", typ, err)
		}
		if got := ErrorMessage(e, "en"); got == "" || got == e.Message {
			t.Errorf("%s: the sentence %q", typ, got)
		}
		// only the output that does not read it: exported, the model left out with a note
		p = &Project{}
		if err := pyval.Catch(func() { p.withoutScript(&Flow{Name: "Pricing"}, def, []any{"o2"}) }); err != nil {
			t.Fatalf("%s: %v", typ, err)
		}
	}
}
