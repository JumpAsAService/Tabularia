package xmla

import (
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// A fake of the data: the values of each column in a few rows, so that the rowsets and TREE_OP are
// tested without DuckDB (the pod's tests read a real table).
type fake struct {
	cols map[string][]*string // by column; nil is a null
	asks int
}

func p(s string) *string { return &s }

// the table: country, code, day
func newFake() *fake {
	return &fake{cols: map[string][]*string{
		"country": {p("IT"), p("FR"), p("IT"), nil, p(""), p("a]b")},
		"code":    {p("7"), p("10"), p("7"), nil, p("2"), p("2")},
		"day":     {p("2025-12-31"), p("2026-01-15"), p("2026-01-20"), p("2026-05-20"), nil, p("2026-05-21")},
	}}
}

func (f *fake) values(d *xmlamodel.Dimension) []*string { return f.cols[d.Column] }

func (f *fake) Members(ctx context.Context, cube *xmlamodel.Cube, l *xmlamodel.Level, under *xmlamodel.Member, fn func(xmlamodel.Member, int) error) error {
	f.asks++
	if err := ctx.Err(); err != nil {
		return err
	}
	d := l.Hierarchy.Dimension
	type group struct {
		key      string
		null     bool
		children map[string]bool
	}
	groups := map[string]*group{}
	for _, v := range f.values(d) {
		if v == nil {
			if l.Number > 1 && l.Part != xmlamodel.PartValue {
				continue
			}
			g := groups["\x00null"]
			if g == nil {
				g = &group{null: true, children: map[string]bool{}}
				groups["\x00null"] = g
			}
			continue
		}
		var key string
		var child string
		if l.Part == xmlamodel.PartValue {
			key = *v
		} else {
			var y, m, dd int
			fmt.Sscanf(*v, "%d-%d-%d", &y, &m, &dd)
			parts := xmlamodel.Parts{Year: y, Month: m, Day: dd, Quarter: (m-1)/3 + 1}
			key = parts.Key(l.Part)
			if under != nil {
				anc := parts.Key(under.Level.Part)
				if anc != under.Key {
					continue
				}
			}
			if l.Number < 4 {
				child = parts.Key(l.Part + 1)
			}
		}
		g := groups[key]
		if g == nil {
			g = &group{key: key, children: map[string]bool{}}
			groups[key] = g
		}
		if child != "" {
			g.children[child] = true
		}
	}
	var all []*group
	for _, g := range groups {
		all = append(all, g)
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].null != all[j].null {
			return all[j].null
		}
		return all[i].key < all[j].key
	})
	for _, g := range all {
		m := xmlamodel.Member{Level: l, Kind: xmlamodel.MemberKey, Key: g.key}
		if g.null {
			m.Kind = xmlamodel.MemberNull
		}
		if err := fn(m, len(g.children)); err != nil {
			return err
		}
	}
	return nil
}

func (f *fake) Member(ctx context.Context, cube *xmlamodel.Cube, m xmlamodel.Member) (int, bool, error) {
	if m.Kind == xmlamodel.MemberAll {
		n := 0
		err := f.Members(ctx, cube, m.Hierarchy().Levels[1], nil, func(xmlamodel.Member, int) error { n++; return nil })
		return n, true, err
	}
	var under *xmlamodel.Member
	if p, ok := m.Parent(); ok && p.Kind != xmlamodel.MemberAll {
		under = &p
	}
	var found bool
	var children int
	err := f.Members(ctx, cube, m.Level, under, func(x xmlamodel.Member, c int) error {
		if x.UniqueName() == m.UniqueName() {
			found, children = true, c
		}
		return nil
	})
	return children, found, err
}

func testCatalog() *Catalog {
	cube := xmlamodel.Build("Sales", "sales", []xmlamodel.Column{
		{Name: "country", Kind: dataapiwire.KindString}, {Name: "code", Kind: dataapiwire.KindInt}, {Name: "day", Kind: dataapiwire.KindDate},
		{Name: "amount", Kind: dataapiwire.KindDecimal, Width: 12, Scale: 2}, {Name: "tags", Kind: dataapiwire.KindOther}})
	cube.Updated = "2026-11-07T08:00:00Z"
	other := xmlamodel.Build("Orders", "orders", []xmlamodel.Column{{Name: "id", Kind: dataapiwire.KindInt}})
	return &Catalog{Name: "CRM", Cubes: []*xmlamodel.Cube{cube, other}}
}

// ── reading an answer ─────────────────────────────────────────────────────────

type answer struct {
	cols []Column
	rows []map[string]string
	raw  string
}

// read parses a Discover answer: the columns of its schema, in order, with their types, and the
// rows as name → text (an array column keeps its inner XML).
func read(t *testing.T, body string) answer {
	t.Helper()
	a := answer{raw: body}
	dec := xml.NewDecoder(strings.NewReader(body))
	inRow := false
	var cur map[string]string
	var depth int
	var name string
	var text strings.Builder
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		switch x := tok.(type) {
		case xml.StartElement:
			switch {
			case x.Name.Local == "element" && x.Name.Space == "http://www.w3.org/2001/XMLSchema":
				var n, typ, field string
				for _, at := range x.Attr {
					switch at.Name.Local {
					case "name":
						n = at.Value
					case "type":
						typ = at.Value
					case "field":
						field = at.Value
					}
				}
				if field != "" {
					a.cols = append(a.cols, Column{Name: n, Type: typ})
				}
			case x.Name.Local == "row" && x.Name.Space == NSRowset:
				inRow, cur, depth = true, map[string]string{}, 0
			case inRow:
				depth++
				if depth == 1 {
					name = x.Name.Local
					text.Reset()
				} else {
					text.WriteString("<" + x.Name.Local + ">")
				}
			}
		case xml.CharData:
			if inRow && depth >= 1 {
				text.Write(x)
			}
		case xml.EndElement:
			if inRow && x.Name.Local == "row" && depth == 0 {
				a.rows = append(a.rows, cur)
				inRow = false
			} else if inRow {
				if depth == 1 {
					val := text.String()
					if strings.Contains(val, "<") {
						val = "<" + name + ">" + val + "</" + name + ">"
					}
					if prev, ok := cur[name]; ok && strings.HasPrefix(prev, "<") {
						cur[name] = prev + val
					} else {
						cur[name] = val
					}
				} else {
					text.WriteString("</" + x.Name.Local + ">")
				}
				depth--
			}
		}
	}
	return a
}

func (a answer) col(name string) []string {
	var out []string
	for _, r := range a.rows {
		out = append(out, r[name])
	}
	return out
}

func (a answer) names() []string {
	var out []string
	for _, c := range a.cols {
		out = append(out, c.Name)
	}
	return out
}

func discover(t *testing.T, rowset string, r map[string][]string, data Data, lim ...Limits) (answer, error) {
	t.Helper()
	l := Limits{MaxMembers: 1000}
	if len(lim) > 0 {
		l = lim[0]
	}
	if data == nil {
		data = newFake()
	}
	var out bytes.Buffer
	n, err := Discover(context.Background(), &out, &Request{Method: MethodDiscover, RequestType: rowset, Restrictions: r}, testCatalog(), data, l)
	if err != nil {
		if out.Len() != 0 {
			t.Errorf("%s: a fault after %d bytes were written", rowset, out.Len())
		}
		return answer{}, err
	}
	a := read(t, out.String())
	if n != len(a.rows) {
		t.Errorf("%s: %d rows reported, %d in the answer", rowset, n, len(a.rows))
	}
	return a, nil
}

func must(t *testing.T, rowset string, r map[string][]string) answer {
	t.Helper()
	a, err := discover(t, rowset, r, nil)
	if err != nil {
		t.Fatalf("%s: %v", rowset, err)
	}
	return a
}

func eqs(t *testing.T, what string, got []string, want ...string) {
	t.Helper()
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("%s\n got %q\nwant %q", what, got, want)
	}
}

// ── the columns of every rowset, as the standard writes them ──────────────────

func TestColumnsOfEveryRowset(t *testing.T) {
	want := map[string]string{
		RowsetCatalogs: "CATALOG_NAME:xsd:string DESCRIPTION:xsd:string ROLES:xsd:string DATE_MODIFIED:xsd:dateTime",
		RowsetCubes: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string CUBE_TYPE:xsd:string CUBE_GUID:xsd:string CREATED_ON:xsd:dateTime " +
			"LAST_SCHEMA_UPDATE:xsd:dateTime SCHEMA_UPDATED_BY:xsd:string LAST_DATA_UPDATE:xsd:dateTime DATA_UPDATED_BY:xsd:string DESCRIPTION:xsd:string " +
			"IS_DRILLTHROUGH_ENABLED:xsd:boolean IS_LINKABLE:xsd:boolean IS_WRITE_ENABLED:xsd:boolean IS_SQL_ENABLED:xsd:boolean CUBE_CAPTION:xsd:string " +
			"BASE_CUBE_NAME:xsd:string CUBE_SOURCE:xsd:unsignedShort",
		RowsetMeasureGroups: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string MEASUREGROUP_NAME:xsd:string DESCRIPTION:xsd:string " +
			"IS_WRITE_ENABLED:xsd:boolean MEASUREGROUP_CAPTION:xsd:string",
		RowsetMeasures: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string MEASURE_NAME:xsd:string MEASURE_UNIQUE_NAME:xsd:string " +
			"MEASURE_CAPTION:xsd:string MEASURE_GUID:xsd:string MEASURE_AGGREGATOR:xsd:int DATA_TYPE:xsd:unsignedShort NUMERIC_PRECISION:xsd:unsignedShort " +
			"NUMERIC_SCALE:xsd:short MEASURE_UNITS:xsd:string DESCRIPTION:xsd:string EXPRESSION:xsd:string MEASURE_IS_VISIBLE:xsd:boolean LEVELS_LIST:xsd:string " +
			"MEASURE_NAME_SQL_COLUMN_NAME:xsd:string MEASURE_UNQUALIFIED_CAPTION:xsd:string MEASUREGROUP_NAME:xsd:string MEASURE_DISPLAY_FOLDER:xsd:string " +
			"DEFAULT_FORMAT_STRING:xsd:string",
		RowsetDimensions: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string DIMENSION_NAME:xsd:string DIMENSION_UNIQUE_NAME:xsd:string " +
			"DIMENSION_GUID:xsd:string DIMENSION_CAPTION:xsd:string DIMENSION_ORDINAL:xsd:unsignedInt DIMENSION_TYPE:xsd:short DIMENSION_CARDINALITY:xsd:unsignedInt " +
			"DEFAULT_HIERARCHY:xsd:string DESCRIPTION:xsd:string IS_VIRTUAL:xsd:boolean IS_READWRITE:xsd:boolean DIMENSION_UNIQUE_SETTINGS:xsd:int " +
			"DIMENSION_MASTER_UNIQUE_NAME:xsd:string DIMENSION_IS_VISIBLE:xsd:boolean",
		RowsetHierarchies: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string DIMENSION_UNIQUE_NAME:xsd:string HIERARCHY_NAME:xsd:string " +
			"HIERARCHY_UNIQUE_NAME:xsd:string HIERARCHY_GUID:xsd:string HIERARCHY_CAPTION:xsd:string DIMENSION_TYPE:xsd:short HIERARCHY_CARDINALITY:xsd:unsignedInt " +
			"DEFAULT_MEMBER:xsd:string ALL_MEMBER:xsd:string DESCRIPTION:xsd:string STRUCTURE:xsd:short IS_VIRTUAL:xsd:boolean IS_READWRITE:xsd:boolean " +
			"DIMENSION_UNIQUE_SETTINGS:xsd:int DIMENSION_IS_VISIBLE:xsd:boolean HIERARCHY_ORDINAL:xsd:unsignedInt DIMENSION_IS_SHARED:xsd:boolean " +
			"HIERARCHY_IS_VISIBLE:xsd:boolean HIERARCHY_ORIGIN:xsd:unsignedShort HIERARCHY_DISPLAY_FOLDER:xsd:string INSTANCE_SELECTION:xsd:unsignedShort",
		RowsetLevels: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string DIMENSION_UNIQUE_NAME:xsd:string HIERARCHY_UNIQUE_NAME:xsd:string " +
			"LEVEL_NAME:xsd:string LEVEL_UNIQUE_NAME:xsd:string LEVEL_GUID:xsd:string LEVEL_CAPTION:xsd:string LEVEL_NUMBER:xsd:unsignedInt " +
			"LEVEL_CARDINALITY:xsd:unsignedInt LEVEL_TYPE:xsd:int CUSTOM_ROLLUP_SETTING:xsd:int LEVEL_UNIQUE_SETTINGS:xsd:int LEVEL_IS_VISIBLE:xsd:boolean " +
			"DESCRIPTION:xsd:string LEVEL_ORIGIN:xsd:unsignedShort",
		RowsetMembers: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string DIMENSION_UNIQUE_NAME:xsd:string HIERARCHY_UNIQUE_NAME:xsd:string " +
			"LEVEL_UNIQUE_NAME:xsd:string LEVEL_NUMBER:xsd:unsignedInt MEMBER_ORDINAL:xsd:unsignedInt MEMBER_NAME:xsd:string MEMBER_UNIQUE_NAME:xsd:string " +
			"MEMBER_TYPE:xsd:int MEMBER_GUID:xsd:string MEMBER_CAPTION:xsd:string CHILDREN_CARDINALITY:xsd:unsignedInt PARENT_LEVEL:xsd:unsignedInt " +
			"PARENT_UNIQUE_NAME:xsd:string PARENT_COUNT:xsd:unsignedInt DESCRIPTION:xsd:string MEMBER_KEY:xsd:string",
		RowsetProps: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string DIMENSION_UNIQUE_NAME:xsd:string HIERARCHY_UNIQUE_NAME:xsd:string " +
			"LEVEL_UNIQUE_NAME:xsd:string MEMBER_UNIQUE_NAME:xsd:string PROPERTY_TYPE:xsd:short PROPERTY_NAME:xsd:string PROPERTY_CAPTION:xsd:string " +
			"DATA_TYPE:xsd:unsignedShort PROPERTY_CONTENT_TYPE:xsd:short DESCRIPTION:xsd:string",
		RowsetSets: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string SET_NAME:xsd:string SCOPE:xsd:int DESCRIPTION:xsd:string " +
			"EXPRESSION:xsd:string DIMENSIONS:xsd:string SET_CAPTION:xsd:string SET_DISPLAY_FOLDER:xsd:string SET_EVALUATION_CONTEXT:xsd:string",
		RowsetKPIs: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string MEASUREGROUP_NAME:xsd:string KPI_NAME:xsd:string KPI_CAPTION:xsd:string " +
			"KPI_DESCRIPTION:xsd:string KPI_DISPLAY_FOLDER:xsd:string KPI_VALUE:xsd:string KPI_GOAL:xsd:string KPI_STATUS:xsd:string KPI_TREND:xsd:string " +
			"KPI_STATUS_GRAPHIC:xsd:string KPI_TREND_GRAPHIC:xsd:string KPI_WEIGHT:xsd:string KPI_CURRENT_TIME_MEMBER:xsd:string KPI_PARENT_KPI_NAME:xsd:string SCOPE:xsd:int",
		RowsetMGDimensions: "CATALOG_NAME:xsd:string SCHEMA_NAME:xsd:string CUBE_NAME:xsd:string MEASUREGROUP_NAME:xsd:string MEASUREGROUP_CARDINALITY:xsd:string " +
			"DIMENSION_UNIQUE_NAME:xsd:string DIMENSION_CARDINALITY:xsd:string DIMENSION_IS_VISIBLE:xsd:boolean DIMENSION_IS_FACT_DIMENSION:xsd:boolean DIMENSION_PATH: DESCRIPTION:xsd:string",
		RowsetFunctions: "FUNCTION_NAME:xsd:string DESCRIPTION:xsd:string PARAMETER_LIST:xsd:string RETURN_TYPE:xsd:int ORIGIN:xsd:int INTERFACE_NAME:xsd:string " +
			"LIBRARY_NAME:xsd:string CAPTION:xsd:string",
		RowsetSchemaRowsets: "SchemaName:xsd:string SchemaGuid:uuid Restrictions: Name:xsd:string Type:xsd:string Description:xsd:string RestrictionsMask:xsd:unsignedLong",
		RowsetDatasources: "DataSourceName:xsd:string DataSourceDescription:xsd:string URL:xsd:string DataSourceInfo:xsd:string ProviderName:xsd:string " +
			"ProviderType:xsd:string AuthenticationMode:xsd:string",
		RowsetEnumerators: "EnumName:xsd:string EnumDescription:xsd:string EnumType:xsd:string ElementName:xsd:string ElementDescription:xsd:string ElementValue:xsd:string",
		RowsetKeywords:    "Keyword:xsd:string",
		RowsetLiterals:    "LiteralName:xsd:string LiteralValue:xsd:string LiteralInvalidChars:xsd:string LiteralInvalidStartingChars:xsd:string LiteralMaxLength:xsd:int",
		RowsetProperties:  "PropertyName:xsd:string PropertyDescription:xsd:string PropertyType:xsd:string PropertyAccessType:xsd:string IsRequired:xsd:boolean Value:xsd:string",
	}
	if len(want) != len(Rowsets()) {
		t.Errorf("%d rowsets described, %d served", len(want), len(Rowsets()))
	}
	for _, name := range Rowsets() {
		// the answer to a request with no restrictions, and with the data it needs, has the columns in this order
		a := must(t, name, nil)
		var got []string
		for _, c := range a.cols {
			got = append(got, c.Name+":"+c.Type)
		}
		if w := want[name]; strings.Join(got, " ") != w {
			t.Errorf("%s\n got %s\nwant %s", name, strings.Join(got, " "), w)
		}
		// the declared columns are the written ones
		inner := 0
		if name == RowsetSchemaRowsets {
			inner = 2 // the Name and Type of a restriction
		}
		if len(Columns(name)) != len(a.cols)-inner {
			t.Errorf("%s: Columns says %d", name, len(Columns(name)))
		}
		if !Served(name) || !Served(strings.ToLower(name)) {
			t.Errorf("%s is served", name)
		}
	}
}

func TestEveryAnswerIsWellFormedXML(t *testing.T) {
	for _, name := range Rowsets() {
		var out bytes.Buffer
		_, err := Discover(context.Background(), &out, &Request{Method: MethodDiscover, RequestType: name}, testCatalog(), newFake(), Limits{MaxMembers: 1000})
		if err != nil {
			t.Fatal(name, err)
		}
		dec := xml.NewDecoder(&out)
		for {
			if _, err := dec.Token(); err != nil {
				if err.Error() != "EOF" {
					t.Errorf("%s: %v", name, err)
				}
				break
			}
		}
	}
}

// ── the rowsets ───────────────────────────────────────────────────────────────

func TestCatalogsAndCubes(t *testing.T) {
	a := must(t, RowsetCatalogs, nil)
	eqs(t, "catalogs", a.col("CATALOG_NAME"), "CRM")
	a = must(t, RowsetCatalogs, map[string][]string{"CATALOG_NAME": {"crm"}})
	eqs(t, "catalog by name, case apart", a.col("CATALOG_NAME"), "CRM")
	a = must(t, RowsetCatalogs, map[string][]string{"CATALOG_NAME": {"other"}})
	eqs(t, "another catalog", a.col("CATALOG_NAME"))

	a = must(t, RowsetCubes, nil)
	eqs(t, "cubes", a.col("CUBE_NAME"), "Sales", "Orders")
	r := a.rows[0]
	if r["CATALOG_NAME"] != "CRM" || r["CUBE_TYPE"] != "CUBE" || r["CUBE_CAPTION"] != "Sales" || r["CUBE_SOURCE"] != "1" || r["LAST_DATA_UPDATE"] != "2026-11-07T08:00:00Z" ||
		r["IS_WRITE_ENABLED"] != "false" || r["IS_DRILLTHROUGH_ENABLED"] != "false" {
		t.Errorf("cube row %v", r)
	}
	if _, has := a.rows[1]["LAST_DATA_UPDATE"]; has {
		t.Error("a cube with no known update says none")
	}
	eqs(t, "cube restricted", must(t, RowsetCubes, map[string][]string{"CUBE_NAME": {"orders"}}).col("CUBE_NAME"), "Orders")
	eqs(t, "two cubes", must(t, RowsetCubes, map[string][]string{"CUBE_NAME": {"orders", "Sales"}}).col("CUBE_NAME"), "Sales", "Orders")
	eqs(t, "no such cube", must(t, RowsetCubes, map[string][]string{"CUBE_NAME": {"nope"}}).col("CUBE_NAME"))
	eqs(t, "cube source cube", must(t, RowsetCubes, map[string][]string{"CUBE_SOURCE": {"1"}}).col("CUBE_NAME"), "Sales", "Orders")
	eqs(t, "cube source cube or dimension", must(t, RowsetCubes, map[string][]string{"CUBE_SOURCE": {"3"}}).col("CUBE_NAME"), "Sales", "Orders")
	eqs(t, "cube source dimension", must(t, RowsetCubes, map[string][]string{"CUBE_SOURCE": {"2"}}).col("CUBE_NAME"))
	eqs(t, "a schema", must(t, RowsetCubes, map[string][]string{"SCHEMA_NAME": {"dbo"}}).col("CUBE_NAME"))
	eqs(t, "another catalog", must(t, RowsetCubes, map[string][]string{"CATALOG_NAME": {"x"}}).col("CUBE_NAME"))
	// an empty restriction is none
	eqs(t, "empty restriction", must(t, RowsetCubes, map[string][]string{"CUBE_NAME": {""}}).col("CUBE_NAME"), "Sales", "Orders")

	a = must(t, RowsetMeasureGroups, nil)
	eqs(t, "measure groups", a.col("MEASUREGROUP_NAME"), "Sales", "Orders")
	eqs(t, "measure group restricted", must(t, RowsetMeasureGroups, map[string][]string{"CUBE_NAME": {"Sales"}}).col("CUBE_NAME"), "Sales")
	eqs(t, "measure group by name", must(t, RowsetMeasureGroups, map[string][]string{"MEASUREGROUP_NAME": {"Orders"}}).col("CUBE_NAME"), "Orders")
}

func TestMeasures(t *testing.T) {
	a := must(t, RowsetMeasures, map[string][]string{"CUBE_NAME": {"Sales"}})
	eqs(t, "measures", a.col("MEASURE_UNIQUE_NAME"), "[Measures].[Rows]", "[Measures].[code]", "[Measures].[code (Average)]", "[Measures].[code (Min)]",
		"[Measures].[code (Max)]", "[Measures].[amount]", "[Measures].[amount (Average)]", "[Measures].[amount (Min)]", "[Measures].[amount (Max)]")
	by := map[string]map[string]string{}
	for _, r := range a.rows {
		by[r["MEASURE_NAME"]] = r
	}
	rows, amount, avg := by["Rows"], by["amount"], by["amount (Average)"]
	if rows["MEASURE_AGGREGATOR"] != "2" || rows["DATA_TYPE"] != "20" || rows["MEASURE_DISPLAY_FOLDER"] != "" || rows["DEFAULT_FORMAT_STRING"] != "#,0" || rows["MEASURE_IS_VISIBLE"] != "true" {
		t.Errorf("Rows %v", rows)
	}
	if amount["MEASURE_AGGREGATOR"] != "1" || amount["DATA_TYPE"] != "131" || amount["NUMERIC_PRECISION"] != "12" || amount["NUMERIC_SCALE"] != "2" ||
		amount["MEASURE_DISPLAY_FOLDER"] != "amount" || amount["DEFAULT_FORMAT_STRING"] != "#,0.00" || amount["MEASUREGROUP_NAME"] != "Sales" || amount["MEASURE_CAPTION"] != "amount" {
		t.Errorf("amount %v", amount)
	}
	if avg["MEASURE_AGGREGATOR"] != "5" || avg["DATA_TYPE"] != "5" || avg["MEASURE_DISPLAY_FOLDER"] != "amount" {
		t.Errorf("average %v", avg)
	}
	if by["amount (Min)"]["MEASURE_AGGREGATOR"] != "3" || by["amount (Max)"]["MEASURE_AGGREGATOR"] != "4" || by["code"]["DATA_TYPE"] != "20" {
		t.Error("min, max, integer")
	}
	eqs(t, "by unique name", must(t, RowsetMeasures, map[string][]string{"CUBE_NAME": {"Sales"}, "MEASURE_UNIQUE_NAME": {"[measures].[AMOUNT]"}}).col("MEASURE_NAME"), "amount")
	eqs(t, "by name", must(t, RowsetMeasures, map[string][]string{"MEASURE_NAME": {"Rows"}}).col("CUBE_NAME"), "Sales", "Orders")
	eqs(t, "none", must(t, RowsetMeasures, map[string][]string{"MEASURE_NAME": {"nope"}}).col("CUBE_NAME"))
	eqs(t, "visibility: visible", must(t, RowsetMeasures, map[string][]string{"CUBE_NAME": {"Orders"}, "MEASURE_VISIBILITY": {"1"}}).col("MEASURE_NAME"), "Rows", "id", "id (Average)", "id (Min)", "id (Max)")
	eqs(t, "visibility: hidden", must(t, RowsetMeasures, map[string][]string{"MEASURE_VISIBILITY": {"2"}}).col("MEASURE_NAME"))
}

func TestDimensionsHierarchiesAndLevels(t *testing.T) {
	sales := map[string][]string{"CUBE_NAME": {"Sales"}}
	a := must(t, RowsetDimensions, sales)
	eqs(t, "dimensions", a.col("DIMENSION_UNIQUE_NAME"), "[Measures]", "[country]", "[code]", "[day]")
	eqs(t, "dimension types", a.col("DIMENSION_TYPE"), "2", "3", "3", "1")
	eqs(t, "dimension ordinals", a.col("DIMENSION_ORDINAL"), "0", "1", "2", "3")
	eqs(t, "default hierarchies", a.col("DEFAULT_HIERARCHY"), "[Measures]", "[country].[country]", "[code].[code]", "[day].[Year-Quarter-Month-Day]")
	if a.rows[0]["DIMENSION_CARDINALITY"] != "9" {
		t.Errorf("the measures' cardinality %v", a.rows[0])
	}
	eqs(t, "a dimension", must(t, RowsetDimensions, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[DAY]"}}).col("DIMENSION_NAME"), "day")
	eqs(t, "the measures'", must(t, RowsetDimensions, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[Measures]"}}).col("DIMENSION_NAME"), "Measures")
	eqs(t, "dimension by name", must(t, RowsetDimensions, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_NAME": {"country"}}).col("DIMENSION_NAME"), "country")

	a = must(t, RowsetHierarchies, sales)
	eqs(t, "hierarchies", a.col("HIERARCHY_UNIQUE_NAME"), "[Measures]", "[country].[country]", "[code].[code]", "[day].[Year-Quarter-Month-Day]", "[day].[day]")
	eqs(t, "default members", a.col("DEFAULT_MEMBER"), "[Measures].[Rows]", "[country].[country].[All]", "[code].[code].[All]",
		"[day].[Year-Quarter-Month-Day].[All]", "[day].[day].[All]")
	if a.rows[0]["ALL_MEMBER"] != "" || a.rows[3]["ALL_MEMBER"] != "[day].[Year-Quarter-Month-Day].[All]" {
		t.Error("only a hierarchy of data has an All member")
	}
	eqs(t, "hierarchy origins", a.col("HIERARCHY_ORIGIN"), "1", "2", "2", "1", "2")
	eqs(t, "hierarchy ordinals", a.col("HIERARCHY_ORDINAL"), "0", "0", "0", "0", "1")
	eqs(t, "of a dimension", must(t, RowsetHierarchies, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[day]"}}).col("HIERARCHY_NAME"),
		"Year-Quarter-Month-Day", "day")
	eqs(t, "one hierarchy", must(t, RowsetHierarchies, map[string][]string{"CUBE_NAME": {"Sales"}, "HIERARCHY_UNIQUE_NAME": {"[day].[day]"}}).col("HIERARCHY_NAME"), "day")
	eqs(t, "user hierarchies only", must(t, RowsetHierarchies, map[string][]string{"CUBE_NAME": {"Sales"}, "HIERARCHY_ORIGIN": {"1"}}).col("HIERARCHY_UNIQUE_NAME"),
		"[Measures]", "[day].[Year-Quarter-Month-Day]")

	a = must(t, RowsetLevels, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[day]"}})
	eqs(t, "levels", a.col("LEVEL_UNIQUE_NAME"), "[day].[Year-Quarter-Month-Day].[(All)]", "[day].[Year-Quarter-Month-Day].[Year]", "[day].[Year-Quarter-Month-Day].[Quarter]",
		"[day].[Year-Quarter-Month-Day].[Month]", "[day].[Year-Quarter-Month-Day].[Day]", "[day].[day].[(All)]", "[day].[day].[day]")
	eqs(t, "level numbers", a.col("LEVEL_NUMBER"), "0", "1", "2", "3", "4", "0", "1")
	eqs(t, "level types", a.col("LEVEL_TYPE"), "1", "20", "68", "132", "516", "1", "0")
	a = must(t, RowsetLevels, map[string][]string{"CUBE_NAME": {"Sales"}, "HIERARCHY_UNIQUE_NAME": {"[Measures]"}})
	eqs(t, "the measures' level", a.col("LEVEL_UNIQUE_NAME"), "[Measures].[MeasuresLevel]")
	eqs(t, "the measures' level by its long name", must(t, RowsetLevels, map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[Measures].[Measures].[MeasuresLevel]"}}).col("LEVEL_NAME"), "MeasuresLevel")
	eqs(t, "a level by name", must(t, RowsetLevels, map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_NAME": {"Month"}}).col("LEVEL_UNIQUE_NAME"), "[day].[Year-Quarter-Month-Day].[Month]")
	eqs(t, "levels of country", must(t, RowsetLevels, map[string][]string{"CUBE_NAME": {"Sales"}, "HIERARCHY_UNIQUE_NAME": {"[country].[country]"}}).col("LEVEL_NAME"), "(All)", "country")

	a = must(t, RowsetMGDimensions, sales)
	eqs(t, "measure group dimensions", a.col("DIMENSION_UNIQUE_NAME"), "[country]", "[code]", "[day]")
	if a.rows[0]["DIMENSION_PATH"] != "<DIMENSION_PATH><MeasureGroupDimension>[country]</MeasureGroupDimension></DIMENSION_PATH>" || a.rows[0]["MEASUREGROUP_NAME"] != "Sales" {
		t.Errorf("mgd row %v", a.rows[0])
	}
	eqs(t, "one", must(t, RowsetMGDimensions, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[code]"}}).col("DIMENSION_UNIQUE_NAME"), "[code]")
}

func TestEmptyRowsets(t *testing.T) {
	for _, name := range []string{RowsetSets, RowsetKPIs, RowsetProps} {
		a := must(t, name, nil)
		if len(a.rows) != 0 || len(a.cols) == 0 {
			t.Errorf("%s: %d rows, %d columns", name, len(a.rows), len(a.cols))
		}
		a = must(t, name, map[string][]string{"CUBE_NAME": {"Sales"}})
		if len(a.rows) != 0 {
			t.Errorf("%s with a restriction", name)
		}
	}
}

func TestServerRowsets(t *testing.T) {
	a := must(t, RowsetSchemaRowsets, nil)
	if got := a.col("SchemaName"); len(got) != len(Rowsets()) {
		t.Fatalf("%d schema rowsets, %d served", len(got), len(Rowsets()))
	}
	for _, r := range a.rows {
		if r["Description"] == "" || r["RestrictionsMask"] == "" {
			t.Errorf("%s: %v", r["SchemaName"], r)
		}
	}
	one := must(t, RowsetSchemaRowsets, map[string][]string{"SchemaName": {"mdschema_members"}}).rows
	if len(one) != 1 {
		t.Fatal("one rowset")
	}
	r := one[0]
	if r["SchemaGuid"] != "C8B522DE-5CF3-11CE-ADE5-00AA0044773D" || r["RestrictionsMask"] != "8191" {
		t.Errorf("members: %v", r)
	}
	for _, want := range []string{"<Restrictions><Name>CUBE_NAME</Name><Type>xsd:string</Type></Restrictions>", "<Name>TREE_OP</Name><Type>xsd:int</Type>",
		"<Name>MEMBER_UNIQUE_NAME</Name>", "<Name>LEVEL_NUMBER</Name><Type>xsd:unsignedInt</Type>"} {
		if !strings.Contains(r["Restrictions"], want) {
			t.Errorf("restrictions lack %s: %s", want, r["Restrictions"])
		}
	}
	// the restrictions each rowset says it takes are the ones it reads
	for _, n := range []string{"CATALOG_NAME", "CUBE_NAME", "DIMENSION_UNIQUE_NAME", "HIERARCHY_UNIQUE_NAME", "LEVEL_UNIQUE_NAME", "MEMBER_UNIQUE_NAME", "TREE_OP"} {
		found := false
		for _, x := range RestrictionsOf(RowsetMembers) {
			found = found || x.Name == n
		}
		if !found {
			t.Errorf("MDSCHEMA_MEMBERS does not list %s", n)
		}
	}

	a = must(t, RowsetDatasources, nil)
	if len(a.rows) != 1 || a.rows[0]["ProviderName"] != "Tabularia" || a.rows[0]["ProviderType"] != "MDP" {
		t.Errorf("datasources %v", a.rows)
	}
	if len(must(t, RowsetDatasources, map[string][]string{"ProviderName": {"Other"}}).rows) != 0 {
		t.Error("datasource restricted")
	}
	a = must(t, RowsetEnumerators, nil)
	if len(a.rows) < 10 {
		t.Errorf("%d enumerators", len(a.rows))
	}
	eqs(t, "an enumerator", must(t, RowsetEnumerators, map[string][]string{"EnumName": {"Format"}}).col("ElementName"), "Tabular", "Multidimensional", "Native")
	a = must(t, RowsetKeywords, nil)
	if !contains(a.col("Keyword"), "SELECT") || !contains(a.col("Keyword"), "WHERE") {
		t.Error("keywords")
	}
	eqs(t, "a keyword", must(t, RowsetKeywords, map[string][]string{"Keyword": {"select"}}).col("Keyword"), "SELECT")
	a = must(t, RowsetLiterals, nil)
	if len(a.rows) == 0 || a.rows[0]["LiteralName"] != "DBLITERAL_QUOTE_PREFIX" || a.rows[0]["LiteralValue"] != "[" {
		t.Errorf("literals %v", a.rows)
	}
	eqs(t, "a literal", must(t, RowsetLiterals, map[string][]string{"LiteralName": {"DBLITERAL_QUOTE_SUFFIX"}}).col("LiteralValue"), "]")
	a = must(t, RowsetFunctions, nil)
	if !contains(a.col("FUNCTION_NAME"), "CrossJoin") || !contains(a.col("FUNCTION_NAME"), "DrilldownMember") || len(a.rows) != len(functionList) {
		t.Errorf("functions %v", a.col("FUNCTION_NAME"))
	}
	eqs(t, "a function", must(t, RowsetFunctions, map[string][]string{"FUNCTION_NAME": {"head"}}).col("FUNCTION_NAME"), "Head")
	a = must(t, RowsetProperties, map[string][]string{"PropertyName": {"Catalog"}})
	eqs(t, "a property", a.col("PropertyName"), "Catalog")
	if a.rows[0]["Value"] != "CRM" {
		t.Error("the catalog's value")
	}
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

func TestUnknownRowset(t *testing.T) {
	for _, name := range []string{"DISCOVER_NOPE", "MDSCHEMA_ACTIONS", "DBSCHEMA_TABLES", "mdschema_cubes_extra", "<x>&"} {
		var out bytes.Buffer
		_, err := Discover(context.Background(), &out, &Request{Method: MethodDiscover, RequestType: name}, testCatalog(), newFake(), Limits{})
		var f *Fault
		if !errors.As(err, &f) || f.Code != CodeNotServed || !strings.Contains(f.Message, name) || out.Len() != 0 {
			t.Errorf("%s: %v (%d bytes)", name, err, out.Len())
		}
		if Served(name) {
			t.Errorf("%s served", name)
		}
	}
	// a long or odd name is cut and cleaned before it is written back
	long := strings.Repeat("A", 500) + "\x01\n"
	_, err := Discover(context.Background(), &bytes.Buffer{}, &Request{Method: MethodDiscover, RequestType: long}, testCatalog(), newFake(), Limits{})
	if err == nil || len(err.Error()) > 200 || strings.ContainsAny(err.Error(), "\x01\n") {
		t.Errorf("long name: %v", err)
	}
	var out bytes.Buffer
	if err := WriteFault(&out, err.(*Fault)); err != nil || !strings.Contains(out.String(), "faultstring") {
		t.Error("the fault is written")
	}
}

func TestACatalogWithNoCubes(t *testing.T) {
	for _, name := range Rowsets() {
		var out bytes.Buffer
		n, err := Discover(context.Background(), &out, &Request{Method: MethodDiscover, RequestType: name}, &Catalog{Name: "CRM"}, newFake(), Limits{})
		if err != nil {
			t.Fatal(name, err)
		}
		switch name {
		case RowsetCubes, RowsetMeasureGroups, RowsetMeasures, RowsetDimensions, RowsetHierarchies, RowsetLevels, RowsetMembers, RowsetMGDimensions:
			if n != 0 {
				t.Errorf("%s: %d rows with no cube", name, n)
			}
		}
	}
}

// ── members ───────────────────────────────────────────────────────────────────

func membersOf(t *testing.T, r map[string][]string) answer {
	t.Helper()
	r["CUBE_NAME"] = []string{"Sales"}
	return must(t, RowsetMembers, r)
}

func TestMembersOfALevel(t *testing.T) {
	a := membersOf(t, map[string][]string{"LEVEL_UNIQUE_NAME": {"[country].[country].[country]"}})
	// ordered by value, the empty text and a bracket among them, the null last
	eqs(t, "country", a.col("MEMBER_UNIQUE_NAME"), "[country].[country].[country].&[\\]", "[country].[country].[country].&[FR]", "[country].[country].[country].&[IT]",
		"[country].[country].[country].&[a]]b]", "[country].[country].[(null)]")
	eqs(t, "captions", a.col("MEMBER_CAPTION"), "", "FR", "IT", "a]b", "null")
	eqs(t, "types", a.col("MEMBER_TYPE"), "1", "1", "1", "1", "1")
	eqs(t, "level numbers", a.col("LEVEL_NUMBER"), "1", "1", "1", "1", "1")
	eqs(t, "children", a.col("CHILDREN_CARDINALITY"), "0", "0", "0", "0", "0")
	eqs(t, "parents", a.col("PARENT_UNIQUE_NAME"), "[country].[country].[All]", "[country].[country].[All]", "[country].[country].[All]", "[country].[country].[All]", "[country].[country].[All]")
	eqs(t, "ordinals", a.col("MEMBER_ORDINAL"), "0", "1", "2", "3", "4")
	if r := a.rows[2]; r["DIMENSION_UNIQUE_NAME"] != "[country]" || r["HIERARCHY_UNIQUE_NAME"] != "[country].[country]" || r["LEVEL_UNIQUE_NAME"] != "[country].[country].[country]" ||
		r["MEMBER_NAME"] != "IT" || r["MEMBER_KEY"] != "IT" || r["PARENT_LEVEL"] != "0" || r["PARENT_COUNT"] != "1" || r["CATALOG_NAME"] != "CRM" || r["CUBE_NAME"] != "Sales" {
		t.Errorf("IT: %v", r)
	}
	// integers, in numeric order, the null last
	a = membersOf(t, map[string][]string{"HIERARCHY_UNIQUE_NAME": {"[code].[code]"}, "LEVEL_NUMBER": {"1"}})
	eqs(t, "code", a.col("MEMBER_CAPTION"), "10", "2", "7", "null") // the fake sorts as text: the pod's test has the numeric order
	// the All member
	a = membersOf(t, map[string][]string{"HIERARCHY_UNIQUE_NAME": {"[country].[country]"}, "LEVEL_NUMBER": {"0"}})
	eqs(t, "all", a.col("MEMBER_UNIQUE_NAME"), "[country].[country].[All]")
	if r := a.rows[0]; r["MEMBER_TYPE"] != "2" || r["CHILDREN_CARDINALITY"] != "5" || r["PARENT_UNIQUE_NAME"] != "" || r["PARENT_COUNT"] != "0" || r["MEMBER_CAPTION"] != "All" || r["LEVEL_UNIQUE_NAME"] != "[country].[country].[(All)]" {
		t.Errorf("All: %v", r)
	}
	// a whole hierarchy: All, then its members
	a = membersOf(t, map[string][]string{"HIERARCHY_UNIQUE_NAME": {"[country].[country]"}})
	if len(a.rows) != 6 || a.rows[0]["MEMBER_CAPTION"] != "All" {
		t.Errorf("the hierarchy: %v", a.col("MEMBER_UNIQUE_NAME"))
	}
	// the measures are the members of their hierarchy
	a = membersOf(t, map[string][]string{"DIMENSION_UNIQUE_NAME": {"[Measures]"}})
	if len(a.rows) != 9 || a.rows[0]["MEMBER_UNIQUE_NAME"] != "[Measures].[Rows]" || a.rows[0]["MEMBER_TYPE"] != "3" || a.rows[0]["LEVEL_UNIQUE_NAME"] != "[Measures].[MeasuresLevel]" {
		t.Errorf("measures: %v", a.rows)
	}
	// by caption, by name, by type
	eqs(t, "by caption", membersOf(t, map[string][]string{"HIERARCHY_UNIQUE_NAME": {"[country].[country]"}, "MEMBER_CAPTION": {"it"}}).col("MEMBER_UNIQUE_NAME"), "[country].[country].[country].&[IT]")
	eqs(t, "by name", membersOf(t, map[string][]string{"DIMENSION_UNIQUE_NAME": {"[country]"}, "MEMBER_NAME": {"FR"}}).col("MEMBER_CAPTION"), "FR")
	eqs(t, "by type: all", membersOf(t, map[string][]string{"MEMBER_TYPE": {"2"}, "DIMENSION_UNIQUE_NAME": {"[country]"}}).col("MEMBER_CAPTION"), "All")
	// without a cube, every cube answers
	a = must(t, RowsetMembers, map[string][]string{"DIMENSION_UNIQUE_NAME": {"[id]"}})
	eqs(t, "other cube", a.col("CUBE_NAME"), "Orders")
}

func TestMembersOfTheCalendar(t *testing.T) {
	cal := "[day].[Year-Quarter-Month-Day]"
	for level, want := range map[string][]string{
		"Year":    {"2025", "2026", "null"},
		"Quarter": {"2025-Q4", "2026-Q1", "2026-Q2"},
		"Month":   {"2025-12", "2026-01", "2026-05"},
		"Day":     {"2025-12-31", "2026-01-15", "2026-01-20", "2026-05-20", "2026-05-21"},
	} {
		a := membersOf(t, map[string][]string{"LEVEL_UNIQUE_NAME": {cal + ".[" + level + "]"}})
		eqs(t, level, a.col("MEMBER_CAPTION"), want...)
	}
	a := membersOf(t, map[string][]string{"LEVEL_UNIQUE_NAME": {cal + ".[Year]"}})
	eqs(t, "year children", a.col("CHILDREN_CARDINALITY"), "1", "2", "0")
	eqs(t, "year unique names", a.col("MEMBER_UNIQUE_NAME"), cal+".[Year].&[2025]", cal+".[Year].&[2026]", cal+".[(null)]")
	a = membersOf(t, map[string][]string{"LEVEL_UNIQUE_NAME": {cal + ".[Month]"}})
	eqs(t, "month parents", a.col("PARENT_UNIQUE_NAME"), cal+".[Quarter].&[2025-Q4]", cal+".[Quarter].&[2026-Q1]", cal+".[Quarter].&[2026-Q2]")
	eqs(t, "month parent level", a.col("PARENT_LEVEL"), "2", "2", "2")
	eqs(t, "month children", a.col("CHILDREN_CARDINALITY"), "1", "2", "2")
	// the plain value of the date is a second hierarchy
	a = membersOf(t, map[string][]string{"LEVEL_UNIQUE_NAME": {"[day].[day].[day]"}})
	eqs(t, "plain", a.col("MEMBER_UNIQUE_NAME"), "[day].[day].[day].&[2025-12-31]", "[day].[day].[day].&[2026-01-15]", "[day].[day].[day].&[2026-01-20]",
		"[day].[day].[day].&[2026-05-20]", "[day].[day].[day].&[2026-05-21]", "[day].[day].[(null)]")
}

func TestTreeOps(t *testing.T) {
	cal := "[day].[Year-Quarter-Month-Day]"
	q1 := cal + ".[Quarter].&[2026-Q1]"
	ask := func(member string, op string) []string {
		r := map[string][]string{"MEMBER_UNIQUE_NAME": {member}}
		if op != "" {
			r["TREE_OP"] = []string{op}
		}
		return membersOf(t, r).col("MEMBER_UNIQUE_NAME")
	}
	eqs(t, "no TREE_OP is the member", ask(q1, ""), q1)
	eqs(t, "self", ask(q1, "8"), q1)
	eqs(t, "children", ask(q1, "1"), cal+".[Month].&[2026-01]")
	eqs(t, "parent", ask(q1, "4"), cal+".[Year].&[2026]")
	eqs(t, "siblings", ask(q1, "2"), cal+".[Quarter].&[2026-Q2]")
	eqs(t, "ancestors", ask(q1, "32"), cal+".[All]", cal+".[Year].&[2026]")
	eqs(t, "descendants", ask(q1, "16"), cal+".[Month].&[2026-01]", cal+".[Day].&[2026-01-15]", cal+".[Day].&[2026-01-20]")
	eqs(t, "self and children", ask(q1, "9"), q1, cal+".[Month].&[2026-01]")
	eqs(t, "children and descendants once", ask(q1, "17"), cal+".[Month].&[2026-01]", cal+".[Day].&[2026-01-15]", cal+".[Day].&[2026-01-20]")
	eqs(t, "parent and ancestors once", ask(q1, "36"), cal+".[All]", cal+".[Year].&[2026]")
	eqs(t, "everything", ask(q1, "63"), cal+".[All]", cal+".[Year].&[2026]", q1, cal+".[Quarter].&[2026-Q2]", cal+".[Month].&[2026-01]", cal+".[Day].&[2026-01-15]", cal+".[Day].&[2026-01-20]")
	// All: its children are the first level, it has no parent or siblings
	eqs(t, "children of All", ask(cal+".[All]", "1"), cal+".[Year].&[2025]", cal+".[Year].&[2026]", cal+".[(null)]")
	eqs(t, "parent of All", ask(cal+".[All]", "4"))
	eqs(t, "siblings of All", ask(cal+".[All]", "2"))
	eqs(t, "ancestors of All", ask(cal+".[All]", "32"))
	if got := ask(cal+".[All]", "16"); len(got) != 3+3+3+5 {
		t.Errorf("descendants of All: %d: %v", len(got), got)
	}
	// a value: its parent is All; its siblings are the others, a null among them
	eqs(t, "value parent", ask("[country].[country].[country].&[IT]", "4"), "[country].[country].[All]")
	eqs(t, "value siblings", ask("[country].[country].[country].&[IT]", "2"), "[country].[country].[country].&[\\]", "[country].[country].[country].&[FR]",
		"[country].[country].[country].&[a]]b]", "[country].[country].[(null)]")
	eqs(t, "value children", ask("[country].[country].[country].&[IT]", "1"))
	// the null
	eqs(t, "null self", ask("[country].[country].[(null)]", ""), "[country].[country].[(null)]")
	eqs(t, "null parent", ask("[country].[country].[(null)]", "4"), "[country].[country].[All]")
	eqs(t, "null children", ask("[country].[country].[(null)]", "17"))
	eqs(t, "null siblings", ask("[country].[country].[(null)]", "2"), "[country].[country].[country].&[\\]", "[country].[country].[country].&[FR]",
		"[country].[country].[country].&[IT]", "[country].[country].[country].&[a]]b]")
	// a member the data does not have, and names that are not members: nothing
	eqs(t, "absent", ask("[country].[country].[country].&[ES]", "63"))
	eqs(t, "absent date", ask(cal+".[Day].&[2026-02-30]", "63"))
	eqs(t, "not a member", ask("[country].[country].[country]", "63"))
	eqs(t, "nonsense", ask("not a name", "63"))
	eqs(t, "another cube's", ask("[id].[id].[id].&[1]", "63"))
	// two members at once, once each
	got := membersOf(t, map[string][]string{"MEMBER_UNIQUE_NAME": {q1, q1, cal + ".[Quarter].&[2026-Q2]"}, "TREE_OP": {"8"}}).col("MEMBER_UNIQUE_NAME")
	eqs(t, "several members", got, q1, cal+".[Quarter].&[2026-Q2]")
	// the restrictions on the hierarchy and level still apply
	eqs(t, "restricted level", membersOf(t, map[string][]string{"MEMBER_UNIQUE_NAME": {q1}, "TREE_OP": {"63"}, "LEVEL_NUMBER": {"3"}}).col("MEMBER_UNIQUE_NAME"), cal+".[Month].&[2026-01]")
	eqs(t, "restricted hierarchy", membersOf(t, map[string][]string{"MEMBER_UNIQUE_NAME": {q1}, "TREE_OP": {"63"}, "HIERARCHY_UNIQUE_NAME": {"[day].[day]"}}).col("MEMBER_UNIQUE_NAME"))
	// measures
	eqs(t, "a measure", ask("[Measures].[amount]", ""), "[Measures].[amount]")
	if got := ask("[Measures].[amount]", "2"); len(got) != 8 {
		t.Errorf("measure siblings: %v", got)
	}
	eqs(t, "a measure's children", ask("[Measures].[amount]", "1"))
	// a TREE_OP that is not a number is refused
	for _, bad := range []string{"x", "-1", "64"} {
		if _, err := discover(t, RowsetMembers, map[string][]string{"CUBE_NAME": {"Sales"}, "MEMBER_UNIQUE_NAME": {q1}, "TREE_OP": {bad}}, nil); err == nil {
			t.Errorf("TREE_OP %s", bad)
		}
	}
}

func TestTheMembersCap(t *testing.T) {
	r := map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[day].[Year-Quarter-Month-Day].[Day]"}}
	// five days: a cap of five lets them through, a cap of four does not
	a, err := discover(t, RowsetMembers, r, nil, Limits{MaxMembers: 5})
	if err != nil || len(a.rows) != 5 {
		t.Fatalf("%v %d", err, len(a.rows))
	}
	_, err = discover(t, RowsetMembers, r, nil, Limits{MaxMembers: 4})
	var f *Fault
	if !errors.As(err, &f) || f.Code != CodeLimit || !strings.Contains(f.Message, "4 members") || !strings.Contains(f.Message, "DATA_API__XMLA_MAX_MEMBERS") {
		t.Fatalf("%v", err)
	}
	// the cap counts what is answered: a member that is filtered out is not counted
	r["MEMBER_CAPTION"] = []string{"2026-05-20"}
	if a, err := discover(t, RowsetMembers, r, nil, Limits{MaxMembers: 1}); err != nil || len(a.rows) != 1 {
		t.Errorf("filtered: %v", err)
	}
	// several cubes and levels count together
	if _, err := discover(t, RowsetMembers, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[country]"}}, nil, Limits{MaxMembers: 5}); err == nil {
		t.Error("the All member and five countries are six")
	}
	// a cap of the measures
	if _, err := discover(t, RowsetMembers, map[string][]string{"CUBE_NAME": {"Sales"}, "DIMENSION_UNIQUE_NAME": {"[Measures]"}}, nil, Limits{MaxMembers: 8}); err == nil {
		t.Error("nine measures over a cap of eight")
	}
	// the data is read no more than needed once the cap is passed
	fk := newFake()
	_, err = discover(t, RowsetMembers, map[string][]string{"CUBE_NAME": {"Sales"}}, fk, Limits{MaxMembers: 3})
	if err == nil || fk.asks > 3 {
		t.Errorf("read on after the cap: %d reads, %v", fk.asks, err)
	}
	// the cap is the default when none is given
	if a, err := discover(t, RowsetMembers, r, nil, Limits{}); err != nil || len(a.rows) != 1 {
		t.Errorf("default cap: %v", err)
	}
}

type failing struct{ err error }

func (f failing) Members(context.Context, *xmlamodel.Cube, *xmlamodel.Level, *xmlamodel.Member, func(xmlamodel.Member, int) error) error {
	return f.err
}
func (f failing) Member(context.Context, *xmlamodel.Cube, xmlamodel.Member) (int, bool, error) {
	return 0, false, f.err
}

func TestAReadThatFails(t *testing.T) {
	r := map[string][]string{"CUBE_NAME": {"Sales"}, "LEVEL_UNIQUE_NAME": {"[country].[country].[country]"}}
	_, err := discover(t, RowsetMembers, r, failing{errors.New("disk on fire at /var/data/secret")})
	var f *Fault
	if !errors.As(err, &f) || f.Code != CodeInternal || strings.Contains(f.Message, "secret") {
		t.Errorf("an internal error is not shown: %v", err)
	}
	_, err = discover(t, RowsetMembers, r, failing{context.DeadlineExceeded})
	if !errors.As(err, &f) || f.Code != CodeLimit {
		t.Errorf("a timeout: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = Discover(ctx, &bytes.Buffer{}, &Request{Method: MethodDiscover, RequestType: RowsetMembers, Restrictions: r}, testCatalog(), newFake(), Limits{})
	if err == nil {
		t.Error("a cancelled request")
	}
}
