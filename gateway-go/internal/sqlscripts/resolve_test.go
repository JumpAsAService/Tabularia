package sqlscripts

import (
	"errors"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
)

// what sqlglot answers for a table: its offsets in characters, as the AI service gives them
func at(text, written string) (*int, *int) {
	i := strings.Index(text, written)
	start := len([]rune(text[:i]))
	end := start + len([]rune(written)) - 1
	return &start, &end
}

func table(text, written, name, folder string, aliased bool) aisvc.Table {
	s, e := at(text, written)
	return aisvc.Table{Name: name, Folder: folder, Aliased: aliased, Start: s, End: e}
}

var parents = map[int64]Folder{1: {Name: "Vendite"}, 2: {Name: "Nord", Parent: ptr(1)}, 3: {Name: "Acquisti"}, 4: {Name: "Nord", Parent: ptr(3)}}

func ptr(i int64) *int64 { return &i }

func ds(id, project int64, name string) Datasource {
	return Datasource{ID: id, Name: name, ProjectID: project, Bucket: "b", Key: "k/" + name}
}

func TestPlaceWritesTheAliasesWhereTheNamesAre(t *testing.T) {
	text := `WITH x AS (SELECT 1) SELECT * FROM "Città Nord" JOIN Vendite.ordini o ON 1 = 1, x`
	parsed := &aisvc.Parsed{Statements: 1, Query: true, CTEs: []string{"x"}, Tables: []aisvc.Table{
		table(text, "Città Nord", "Città Nord", "", false),
		table(text, "Vendite.ordini", "ordini", "Vendite", true),
		{Name: "x", Start: ptr2(len([]rune(text)) - 1), End: ptr2(len([]rune(text)) - 1)},
	}}
	got, err := place(text, "duckdb", parsed, []Datasource{ds(7, 2, "città nord"), ds(9, 1, "Ordini")}, parents)
	if err != nil {
		t.Fatal(err)
	}
	want := `WITH x AS (SELECT 1) SELECT * FROM ds_7 AS "Città Nord" JOIN ds_9 o ON 1 = 1, x`
	if got.Query != want {
		t.Errorf("%s\nwant %s", got.Query, want)
	}
	if len(got.Tables) != 2 || got.Tables[0].Alias != "ds_7" || got.Tables[1].Alias != "ds_9" {
		t.Errorf("%+v", got.Tables)
	}
	chain := CatalogChain(got)
	if first, _ := chain.Get("input_key"); first != "k/città nord" {
		t.Errorf("the first datasource is the source: %v", first)
	}
}

func ptr2(i int) *int { return &i }

func TestPlaceRefusesAsResolveDoes(t *testing.T) {
	text := "SELECT * FROM t"
	one := func(tb aisvc.Table) *aisvc.Parsed {
		return &aisvc.Parsed{Statements: 1, Query: true, Tables: []aisvc.Table{tb}}
	}
	cases := []struct {
		name     string
		parsed   *aisvc.Parsed
		readable []Datasource
		want     string
		unread   bool
	}{
		{"catalog", one(aisvc.Table{Name: "t", Folder: "s", Catalog: true, Written: "db.s.t"}), nil,
			"«db.s.t»: name a datasource by its name, or by its folder and its name", false},
		{"nowhere", one(table(text, "t", "t", "", false)), nil, "There is no datasource «t» you can read", true},
		{"another folder", one(table(text, "t", "t", "Acquisti", false)), []Datasource{ds(1, 1, "t")},
			"There is no datasource «Acquisti.t» you can read", true},
		{"two folders", one(table(text, "t", "t", "", false)), []Datasource{ds(1, 3, "t"), ds(2, 1, "t")},
			`«t» is in more than one folder (Acquisti, Vendite): write it as "Acquisti"."t"`, false},
		{"two folders of one name", one(table(text, "t", "t", "Nord", false)), []Datasource{ds(1, 4, "t"), ds(2, 2, "t")},
			"«Nord.t» is in more than one folder called «Nord» (Acquisti / Nord, Vendite / Nord): rename one of the folders, or one of the datasources", false},
		{"no data", one(table(text, "t", "t", "", false)), []Datasource{{ID: 1, Name: "t", ProjectID: 1}},
			"The datasource «t» has no data yet", false},
		{"no place", one(aisvc.Table{Name: "t"}), []Datasource{ds(1, 1, "t")},
			"«t» could not be placed in the SQL: write its name in quotes", false},
	}
	for _, c := range cases {
		_, err := place(text, "duckdb", c.parsed, c.readable, parents)
		var r *Refusal
		var u *Unreadable
		switch {
		case errors.As(err, &u):
			if !c.unread || u.Message != c.want {
				t.Errorf("%s: unreadable %q", c.name, u.Message)
			}
		case errors.As(err, &r):
			if c.unread || r.Message != c.want {
				t.Errorf("%s: refused %q\nwant %q", c.name, r.Message, c.want)
			}
		default:
			t.Errorf("%s: %v", c.name, err)
		}
	}
}

// two of a name in the same folder list: the one written with the same case wins
func TestPlaceTakesTheExactName(t *testing.T) {
	text := "SELECT * FROM `Sales`"
	parsed := &aisvc.Parsed{Statements: 1, Query: true, Tables: []aisvc.Table{table(text, "Sales", "Sales", "", false)}}
	got, err := place(text, "bigquery", parsed, []Datasource{ds(1, 1, "sales"), ds(2, 3, "Sales")}, parents)
	if err != nil {
		t.Fatal(err)
	}
	if got.Query != "SELECT * FROM ds_2 AS `Sales`" {
		t.Error(got.Query)
	}
}

func TestQuoteAndTheFirstLine(t *testing.T) {
	if Quote("a`b\\", "bigquery") != "`a\\`b\\\\`" || Quote(`a"b`, "postgres") != `"a""b"` {
		t.Error("quote")
	}
	for in, want := range map[string]string{"one": "one", "a\nb": "a", "a b": "a", "a\x1cb": "a", "\nb": ""} {
		if got, ok := firstLine(in); !ok || got != want {
			t.Errorf("%q → %q", in, got)
		}
	}
	if _, ok := firstLine(""); ok {
		t.Error("no line at all fails in Python")
	}
	if DialectOf("") != "postgres" || DialectOf("chdb") != "clickhouse" || DialectOf("spark") != "postgres" {
		t.Error("dialects")
	}
}
