package pod

import (
	"database/sql"
	"encoding/xml"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// Discover (docs/specs/xmla.md, piece 2) on real DuckDB tables: a small table of every type of
// column, asked over HTTP as a pivot table would.

// the table: eight rows, a null, an empty text, a bracket and a backslash among the texts, a null
// number, boolean and date, dates over two years and three quarters, and a timestamp with a zone
// that is not UTC
const salesSQL = `COPY (SELECT * FROM (VALUES
 (1, 'IT',       7,    10.50::DECIMAL(12,2), 2.5, true,  DATE '2025-12-31', TIMESTAMP '2025-12-31 23:59:59', TIMESTAMPTZ '2025-12-31 23:59:59+00', TIME '08:00:00', 100::UBIGINT, 'u1', [1]),
 (2, 'FR',       10,   20.00, 1.0, false, DATE '2026-01-15', TIMESTAMP '2026-01-15 08:30:00', TIMESTAMPTZ '2026-01-15 23:30:00-05', TIME '09:30:00', 200::UBIGINT, 'u2', [2]),
 (3, 'IT',       7,     5.25, 3.5, true,  DATE '2026-01-20', TIMESTAMP '2026-01-20 08:30:00.25', TIMESTAMPTZ '2026-01-20 08:00:00+00', TIME '08:00:00', 100::UBIGINT, 'u1', [3]),
 (4, NULL,       NULL, NULL,  NULL, NULL, NULL,              NULL,                              NULL,                                  NULL,           NULL,         NULL, NULL),
 (5, '',         2,     1.00, 0.5, true,  DATE '2026-05-20', TIMESTAMP '2026-05-20 12:00:00', TIMESTAMPTZ '2026-05-20 12:00:00+00', TIME '12:00:00', 300::UBIGINT, 'u3', [5]),
 (6, 'a]b',      2,     2.00, 0.5, false, DATE '2026-05-21', TIMESTAMP '2026-05-21 12:00:00', TIMESTAMPTZ '2026-05-21 12:00:00+00', TIME '12:00:00', 300::UBIGINT, 'u3', [6]),
 (7, '\x',       10,    3.00, 0.5, false, DATE '2026-05-21', TIMESTAMP '2026-05-21 13:00:00', TIMESTAMPTZ '2026-05-21 13:00:00+00', TIME '13:00:00', 400::UBIGINT, 'u4', [7]),
 (8, 'Città',    -3,    4.00, 0.5, true,  DATE '2026-05-21', TIMESTAMP '2026-05-21 14:00:00', TIMESTAMPTZ '2026-05-21 14:00:00+00', TIME '14:00:00', 400::UBIGINT, 'u4', [8])
) t(id, country, code, amount, ratio, paid, day, seen, seen_tz, clock, big, ref, tags)) TO '%s' (FORMAT parquet)`

var salesColumns = []string{"id", "country", "code", "amount", "ratio", "paid", "day", "seen", "seen_tz", "clock", "big", "ref", "tags"}

func writeSales(t *testing.T, path string) {
	t.Helper()
	db, err := sql.Open("duckdb", "")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(fmt.Sprintf(salesSQL, path)); err != nil {
		t.Fatal(err)
	}
}

// salesFixture is the fixture with XMLA on, a third resource «sales» (cube Sales) and all of it prepared.
func salesFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t)
	writeSales(t, filepath.Join(f.dir, "storage", "sales.parquet"))
	f.m.Resources = append(f.m.Resources, dataapiwire.Resource{Name: "sales", Cube: "Sales", Available: true, Version: "2026-11-07T06:00:00Z",
		Fingerprint: "s1", URL: f.files.URL + "/sales.parquet", Columns: salesColumns, Filters: []dataapiwire.Filter{}, MaxPage: 10})
	f.m.XMLA = &dataapiwire.XMLA{MaxCells: 1000, MaxTuples: 100, MaxMembers: 500}
	f.load()
	return f
}

// the request of a Discover, as a pivot table writes it
func discoverBody(rowset string, restrictions map[string]string, order ...string) string {
	var b strings.Builder
	b.WriteString(`<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><Discover xmlns="urn:schemas-microsoft-com:xml-analysis">` +
		`<RequestType>` + rowset + `</RequestType><Restrictions><RestrictionList>`)
	for _, k := range order {
		b.WriteString("<" + k + ">")
		xml.EscapeText(&b, []byte(restrictions[k]))
		b.WriteString("</" + k + ">")
	}
	b.WriteString(`</RestrictionList></Restrictions><Properties><PropertyList><Catalog>CRM</Catalog><Content>SchemaData</Content><Format>Tabular</Format></PropertyList></Properties></Discover></soap:Body></soap:Envelope>`)
	return b.String()
}

// ask is a Discover with restrictions written in a fixed order (sorted by name)
func (f *fixture) ask(key, rowset string, restrictions ...string) (*parsed, answer) {
	f.t.Helper()
	m := map[string]string{}
	var order []string
	for i := 0; i+1 < len(restrictions); i += 2 {
		m[restrictions[i]] = restrictions[i+1]
		order = append(order, restrictions[i])
	}
	a := f.post(discoverBody(rowset, m, order...), key)
	if a.status != 200 {
		return nil, a
	}
	return parseRowset(f.t, string(a.body)), a
}

func (f *fixture) must(rowset string, restrictions ...string) *parsed {
	f.t.Helper()
	p, a := f.ask("main", rowset, restrictions...)
	if p == nil {
		f.t.Fatalf("%s %v: %d %s", rowset, restrictions, a.status, a.body)
	}
	return p
}

// parsed is a rowset answer: the columns of its schema and its rows as name → text
type parsed struct {
	cols []string
	rows []map[string]string
}

func parseRowset(t *testing.T, body string) *parsed {
	t.Helper()
	p := &parsed{}
	dec := xml.NewDecoder(strings.NewReader(body))
	var cur map[string]string
	var name string
	var text strings.Builder
	depth := 0
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		switch x := tok.(type) {
		case xml.StartElement:
			switch {
			case x.Name.Space == "http://www.w3.org/2001/XMLSchema" && x.Name.Local == "element":
				for _, a := range x.Attr {
					if a.Name.Local == "field" {
						p.cols = append(p.cols, a.Value)
					}
				}
			case x.Name.Space == xmla.NSRowset && x.Name.Local == "row":
				cur, depth = map[string]string{}, 0
			case cur != nil:
				depth++
				if depth == 1 {
					name = x.Name.Local
					text.Reset()
				}
			}
		case xml.CharData:
			if cur != nil && depth == 1 {
				text.Write(x)
			}
		case xml.EndElement:
			switch {
			case cur != nil && depth == 0 && x.Name.Local == "row":
				p.rows = append(p.rows, cur)
				cur = nil
			case cur != nil:
				if depth == 1 {
					cur[name] += text.String()
				}
				depth--
			}
		}
	}
	return p
}

func (p *parsed) col(name string) []string {
	out := []string{}
	for _, r := range p.rows {
		out = append(out, r[name])
	}
	return out
}

func same(t *testing.T, what string, got []string, want ...string) {
	t.Helper()
	if want == nil {
		want = []string{}
	}
	if strings.Join(got, "\x1f") != strings.Join(want, "\x1f") {
		t.Errorf("%s\n got %q\nwant %q", what, got, want)
	}
}

func TestDiscoverCubesFromTheTables(t *testing.T) {
	f := salesFixture(t)
	// a cube per resource, named by its caption or its name
	same(t, "cubes", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "orders", "Sales")
	cubes := f.must("MDSCHEMA_CUBES")
	if cubes.rows[2]["LAST_DATA_UPDATE"] != "2026-11-07T06:00:00Z" || cubes.rows[2]["CATALOG_NAME"] != "CRM" {
		t.Errorf("sales row %v", cubes.rows[2])
	}
	same(t, "catalogs", f.must("DBSCHEMA_CATALOGS").col("CATALOG_NAME"), "CRM")
	// measures: Rows, then a group of four for each number (the sum named as the column)
	same(t, "sales measures", f.must("MDSCHEMA_MEASURES", "CUBE_NAME", "Sales").col("MEASURE_UNIQUE_NAME"),
		"[Measures].[Rows]",
		"[Measures].[id]", "[Measures].[id (Average)]", "[Measures].[id (Min)]", "[Measures].[id (Max)]",
		"[Measures].[code]", "[Measures].[code (Average)]", "[Measures].[code (Min)]", "[Measures].[code (Max)]",
		"[Measures].[amount]", "[Measures].[amount (Average)]", "[Measures].[amount (Min)]", "[Measures].[amount (Max)]",
		"[Measures].[ratio]", "[Measures].[ratio (Average)]", "[Measures].[ratio (Min)]", "[Measures].[ratio (Max)]",
		"[Measures].[big]", "[Measures].[big (Average)]", "[Measures].[big (Min)]", "[Measures].[big (Max)]")
	amount := f.must("MDSCHEMA_MEASURES", "CUBE_NAME", "Sales", "MEASURE_UNIQUE_NAME", "[Measures].[amount]").rows[0]
	if amount["DATA_TYPE"] != "131" || amount["NUMERIC_PRECISION"] != "12" || amount["NUMERIC_SCALE"] != "2" || amount["DEFAULT_FORMAT_STRING"] != "#,0.00" || amount["MEASURE_DISPLAY_FOLDER"] != "amount" {
		t.Errorf("amount %v", amount)
	}
	// dimensions: integers, text, booleans, dates, timestamps, times and uuids; a list stays out; a float is a measure only
	same(t, "sales dimensions", f.must("MDSCHEMA_DIMENSIONS", "CUBE_NAME", "Sales").col("DIMENSION_UNIQUE_NAME"),
		"[Measures]", "[id]", "[country]", "[code]", "[paid]", "[day]", "[seen]", "[seen_tz]", "[clock]", "[big]", "[ref]")
	same(t, "dimension types", f.must("MDSCHEMA_DIMENSIONS", "CUBE_NAME", "Sales").col("DIMENSION_TYPE"), "2", "3", "3", "3", "3", "1", "1", "1", "3", "3", "3")
	same(t, "orders dimensions", f.must("MDSCHEMA_DIMENSIONS", "CUBE_NAME", "orders").col("DIMENSION_UNIQUE_NAME"), "[Measures]", "[order_id]", "[customer]")
	// customers has an odd column name with a quote in it, and a list
	cust := f.must("MDSCHEMA_DIMENSIONS", "CUBE_NAME", "customers").col("DIMENSION_UNIQUE_NAME")
	if !contains(cust, `[odd "col"]`) || contains(cust, "[tags]") || contains(cust, "[score]") || contains(cust, "[ratio]") {
		t.Errorf("customers dimensions %v", cust)
	}
	same(t, "hierarchies of a date", f.must("MDSCHEMA_HIERARCHIES", "CUBE_NAME", "Sales", "DIMENSION_UNIQUE_NAME", "[day]").col("HIERARCHY_UNIQUE_NAME"),
		"[day].[Year-Quarter-Month-Day]", "[day].[day]")
	// every rowset is well formed with the columns of the standard
	for _, name := range xmla.Rowsets() {
		var restrictions []string
		if name == xmla.RowsetMembers {
			restrictions = []string{"CUBE_NAME", "Sales"} // all the members of the three cubes are more than the cap
		}
		p := f.must(name, restrictions...)
		var want, got []string
		for _, c := range xmla.Columns(name) {
			want = append(want, c.Name)
		}
		for _, c := range p.cols {
			// the Name and Type inside the restrictions of a schema rowset
			if name != xmla.RowsetSchemaRowsets || (c != "Name" && c != "Type") {
				got = append(got, c)
			}
		}
		same(t, name+" columns", got, want...)
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

func TestDiscoverMembersOfEveryKindOfColumn(t *testing.T) {
	f := salesFixture(t)
	level := func(dim, hier, lvl string) *parsed {
		return f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "["+dim+"].["+hier+"].["+lvl+"]")
	}
	// text: ordered, the empty text and the backslash written as keys, a bracket doubled, the null last and a member of its own
	c := level("country", "country", "country")
	same(t, "country", c.col("MEMBER_UNIQUE_NAME"),
		`[country].[country].[country].&[\]`, `[country].[country].[country].&[Città]`, `[country].[country].[country].&[FR]`, `[country].[country].[country].&[IT]`,
		`[country].[country].[country].&[\\x]`, `[country].[country].[country].&[a]]b]`, `[country].[country].[(null)]`)
	same(t, "country captions", c.col("MEMBER_CAPTION"), "", "Città", "FR", "IT", `\x`, "a]b", "null")
	same(t, "country keys", c.col("MEMBER_KEY")[:6], "", "Città", "FR", "IT", `\x`, "a]b")
	// integers: in numeric order, a negative one first, the null last
	same(t, "code", level("code", "code", "code").col("MEMBER_CAPTION"), "-3", "2", "7", "10", "null")
	same(t, "code keys", level("code", "code", "code").col("MEMBER_UNIQUE_NAME"),
		"[code].[code].[code].&[-3]", "[code].[code].[code].&[2]", "[code].[code].[code].&[7]", "[code].[code].[code].&[10]", "[code].[code].[(null)]")
	// booleans
	same(t, "paid", level("paid", "paid", "paid").col("MEMBER_UNIQUE_NAME"), "[paid].[paid].[paid].&[false]", "[paid].[paid].[paid].&[true]", "[paid].[paid].[(null)]")
	// a huge integer, a time, a uuid-like text
	same(t, "big", level("big", "big", "big").col("MEMBER_CAPTION"), "100", "200", "300", "400", "null")
	same(t, "clock", level("clock", "clock", "clock").col("MEMBER_CAPTION"), "08:00:00", "09:30:00", "12:00:00", "13:00:00", "14:00:00", "null")
	same(t, "ref", level("ref", "ref", "ref").col("MEMBER_CAPTION"), "u1", "u2", "u3", "u4", "null")
	// the All member and its cardinality: the five countries, the empty one, and the null
	all := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "HIERARCHY_UNIQUE_NAME", "[country].[country]", "LEVEL_NUMBER", "0")
	if len(all.rows) != 1 || all.rows[0]["CHILDREN_CARDINALITY"] != "7" || all.rows[0]["MEMBER_TYPE"] != "2" {
		t.Errorf("All: %v", all.rows)
	}
	allCode := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "HIERARCHY_UNIQUE_NAME", "[code].[code]", "LEVEL_NUMBER", "0")
	if allCode.rows[0]["CHILDREN_CARDINALITY"] != "5" {
		t.Errorf("All of code: %v", allCode.rows[0])
	}
	// the calendar: years (the null among them), quarters, months and days, with their children
	cal := "[day].[Year-Quarter-Month-Day]"
	y := level("day", "Year-Quarter-Month-Day", "Year")
	same(t, "years", y.col("MEMBER_UNIQUE_NAME"), cal+".[Year].&[2025]", cal+".[Year].&[2026]", cal+".[(null)]")
	same(t, "year children", y.col("CHILDREN_CARDINALITY"), "1", "2", "0")
	q := level("day", "Year-Quarter-Month-Day", "Quarter")
	same(t, "quarters", q.col("MEMBER_CAPTION"), "2025-Q4", "2026-Q1", "2026-Q2")
	same(t, "quarter children", q.col("CHILDREN_CARDINALITY"), "1", "1", "1")
	same(t, "quarter parents", q.col("PARENT_UNIQUE_NAME"), cal+".[Year].&[2025]", cal+".[Year].&[2026]", cal+".[Year].&[2026]")
	m := level("day", "Year-Quarter-Month-Day", "Month")
	same(t, "months", m.col("MEMBER_CAPTION"), "2025-12", "2026-01", "2026-05")
	same(t, "month children", m.col("CHILDREN_CARDINALITY"), "1", "2", "2")
	d := level("day", "Year-Quarter-Month-Day", "Day")
	same(t, "days", d.col("MEMBER_CAPTION"), "2025-12-31", "2026-01-15", "2026-01-20", "2026-05-20", "2026-05-21")
	same(t, "day parents", d.col("PARENT_UNIQUE_NAME"), cal+".[Month].&[2025-12]", cal+".[Month].&[2026-01]", cal+".[Month].&[2026-01]", cal+".[Month].&[2026-05]", cal+".[Month].&[2026-05]")
	same(t, "plain date", level("day", "day", "day").col("MEMBER_UNIQUE_NAME"), "[day].[day].[day].&[2025-12-31]", "[day].[day].[day].&[2026-01-15]",
		"[day].[day].[day].&[2026-01-20]", "[day].[day].[day].&[2026-05-20]", "[day].[day].[day].&[2026-05-21]", "[day].[day].[(null)]")
	same(t, "year of the All", f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "HIERARCHY_UNIQUE_NAME", cal, "LEVEL_NUMBER", "0").col("CHILDREN_CARDINALITY"), "3")
	// a timestamp: the seconds, the fraction when there is one; its calendar reads the date part
	same(t, "plain timestamp", level("seen", "seen", "seen").col("MEMBER_CAPTION"), "2025-12-31 23:59:59", "2026-01-15 08:30:00", "2026-01-20 08:30:00.25",
		"2026-05-20 12:00:00", "2026-05-21 12:00:00", "2026-05-21 13:00:00", "2026-05-21 14:00:00", "null")
	same(t, "timestamp days", level("seen", "Year-Quarter-Month-Day", "Day").col("MEMBER_CAPTION"), "2025-12-31", "2026-01-15", "2026-01-20", "2026-05-20", "2026-05-21")
	// a timestamp with a time zone is read in UTC: 23:30 at -05:00 on the 15th is the 16th, 04:30
	same(t, "tz days", level("seen_tz", "Year-Quarter-Month-Day", "Day").col("MEMBER_CAPTION"), "2025-12-31", "2026-01-16", "2026-01-20", "2026-05-20", "2026-05-21")
	same(t, "tz plain", level("seen_tz", "seen_tz", "seen_tz").col("MEMBER_CAPTION")[:3], "2025-12-31 23:59:59", "2026-01-16 04:30:00", "2026-01-20 08:00:00")
}

func TestDiscoverTreeOpsOnTheData(t *testing.T) {
	f := salesFixture(t)
	cal := "[day].[Year-Quarter-Month-Day]"
	tree := func(member, op string) []string {
		t.Helper()
		args := []string{"CUBE_NAME", "Sales", "MEMBER_UNIQUE_NAME", member}
		if op != "" {
			args = append(args, "TREE_OP", op)
		}
		return f.must("MDSCHEMA_MEMBERS", args...).col("MEMBER_UNIQUE_NAME")
	}
	y26 := cal + ".[Year].&[2026]"
	same(t, "self", tree(y26, ""), y26)
	same(t, "children", tree(y26, "1"), cal+".[Quarter].&[2026-Q1]", cal+".[Quarter].&[2026-Q2]")
	same(t, "siblings", tree(y26, "2"), cal+".[Year].&[2025]", cal+".[(null)]")
	same(t, "parent", tree(y26, "4"), cal+".[All]")
	same(t, "descendants", tree(y26, "16"), cal+".[Quarter].&[2026-Q1]", cal+".[Quarter].&[2026-Q2]", cal+".[Month].&[2026-01]", cal+".[Month].&[2026-05]",
		cal+".[Day].&[2026-01-15]", cal+".[Day].&[2026-01-20]", cal+".[Day].&[2026-05-20]", cal+".[Day].&[2026-05-21]")
	same(t, "ancestors of a day", tree(cal+".[Day].&[2026-05-20]", "32"), cal+".[All]", y26, cal+".[Quarter].&[2026-Q2]", cal+".[Month].&[2026-05]")
	same(t, "siblings of a day", tree(cal+".[Day].&[2026-01-15]", "2"), cal+".[Day].&[2026-01-20]")
	same(t, "children of All", tree(cal+".[All]", "1"), cal+".[Year].&[2025]", y26, cal+".[(null)]")
	// a member the data does not have: nothing, and nothing around it either
	same(t, "absent year", tree(cal+".[Year].&[1999]", "63"))
	same(t, "absent text", tree("[country].[country].[country].&[ES]", "63"))
	same(t, "case of a key counts", tree("[country].[country].[country].&[it]", "8"))
	same(t, "absent day", tree(cal+".[Day].&[2026-02-30]", "8"))
	same(t, "a day that is not in the data", tree(cal+".[Day].&[2026-03-01]", "8"))
	// keys that are written specially find their rows
	same(t, "the empty text", tree(`[country].[country].[country].&[\]`, "8"), `[country].[country].[country].&[\]`)
	same(t, "a backslash", tree(`[country].[country].[country].&[\\x]`, "8"), `[country].[country].[country].&[\\x]`)
	same(t, "a bracket", tree(`[country].[country].[country].&[a]]b]`, "8"), `[country].[country].[country].&[a]]b]`)
	same(t, "an accent", tree(`[country].[country].[country].&[Città]`, "8"), `[country].[country].[country].&[Città]`)
	same(t, "null text", tree("[country].[country].[(null)]", "8"), "[country].[country].[(null)]")
	same(t, "null date", tree(cal+".[(null)]", "8"), cal+".[(null)]")
	same(t, "an integer", tree("[code].[code].[code].&[10]", "8"), "[code].[code].[code].&[10]")
	same(t, "an integer, written another way", tree("[code].[code].[code].&[+10]", "8"), "[code].[code].[code].&[10]")
	same(t, "a boolean", tree("[paid].[paid].[paid].&[true]", "8"), "[paid].[paid].[paid].&[true]")
	same(t, "a day", tree("[day].[day].[day].&[2026-01-15]", "8"), "[day].[day].[day].&[2026-01-15]")
	same(t, "a timestamp", tree("[seen].[seen].[seen].&[2026-01-20 08:30:00.25]", "8"), "[seen].[seen].[seen].&[2026-01-20 08:30:00.25]")
	same(t, "a timestamp with a T", tree("[seen].[seen].[seen].&[2026-01-20T08:30:00.25]", "8"), "[seen].[seen].[seen].&[2026-01-20 08:30:00.25]")
	same(t, "a timestamp with a zone", tree("[seen_tz].[seen_tz].[seen_tz].&[2026-01-16 04:30:00]", "8"), "[seen_tz].[seen_tz].[seen_tz].&[2026-01-16 04:30:00]")
	same(t, "a time", tree("[clock].[clock].[clock].&[09:30:00]", "8"), "[clock].[clock].[clock].&[09:30:00]")
	same(t, "a huge integer", tree("[big].[big].[big].&[300]", "8"), "[big].[big].[big].&[300]")
	// a name with a quote in it is a column of customers
	same(t, "a quote in a column", f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "customers", "MEMBER_UNIQUE_NAME", `[odd "col"].[odd "col"].[(null)]`).col("MEMBER_UNIQUE_NAME"))
	// no value of the data in the log
	if strings.Contains(f.logs.String(), "Città") {
		t.Error("a value of the data in the log")
	}
}

func TestDiscoverMembersCap(t *testing.T) {
	f := salesFixture(t)
	f.m.XMLA.MaxMembers = 50
	f.s.Apply(f.m)
	// customers.name has 1000 values: the cap of 50 is named, with its value, and no part of the list is sent
	a := f.post(discoverBody("MDSCHEMA_MEMBERS", map[string]string{"CUBE_NAME": "customers", "LEVEL_UNIQUE_NAME": "[name].[name].[name]"}, "CUBE_NAME", "LEVEL_UNIQUE_NAME"), "main")
	if a.status != 500 || !strings.Contains(string(a.body), "<soap:Fault>") || !strings.Contains(string(a.body), "more than 50 members") ||
		!strings.Contains(string(a.body), "DATA_API__XMLA_MAX_MEMBERS") || strings.Contains(string(a.body), "name 1") || strings.Contains(string(a.body), "<row>") {
		t.Fatalf("%d %s", a.status, a.body)
	}
	if !strings.Contains(f.logs.String(), `"code":"xmla_limit"`) {
		t.Error("the log says which cap")
	}
	// at the cap exactly it goes through
	f.m.XMLA.MaxMembers = 8
	f.s.Apply(f.m)
	if p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "[country].[country].[country]"); len(p.rows) != 7 {
		t.Errorf("%d rows", len(p.rows))
	}
	f.m.XMLA.MaxMembers = 6
	f.s.Apply(f.m)
	a = f.post(discoverBody("MDSCHEMA_MEMBERS", map[string]string{"CUBE_NAME": "Sales", "LEVEL_UNIQUE_NAME": "[country].[country].[country]"}, "CUBE_NAME", "LEVEL_UNIQUE_NAME"), "main")
	if a.status != 500 || !strings.Contains(string(a.body), "more than 6 members") {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// the children of one member are still reachable under the cap
	f.m.XMLA.MaxMembers = 4
	f.s.Apply(f.m)
	if p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "MEMBER_UNIQUE_NAME", "[day].[Year-Quarter-Month-Day].[Year].&[2026]", "TREE_OP", "1"); len(p.rows) != 2 {
		t.Errorf("%d rows", len(p.rows))
	}
}

func TestDiscoverAKeyForSomeResources(t *testing.T) {
	f := salesFixture(t)
	// «orders» sees the cube of orders and none other, in every rowset
	for _, name := range xmla.Rowsets() {
		for _, restrictions := range [][]string{nil, {"CUBE_NAME", "Sales"}, {"CUBE_NAME", "customers"}} {
			p, a := f.ask("orders", name, restrictions...)
			if p == nil {
				t.Fatalf("%s: %d %s", name, a.status, a.body)
			}
			body := string(a.body)
			if strings.Contains(body, "Sales") && name != xmla.RowsetKeywords || strings.Contains(body, "customers") || strings.Contains(body, "[country]") {
				t.Errorf("%s %v: the key sees what it may not: %s", name, restrictions, body)
			}
			if restrictions != nil && xmla.NeedsCubes(name) && len(p.rows) != 0 {
				t.Errorf("%s %v: %d rows of a cube it may not read", name, restrictions, len(p.rows))
			}
		}
	}
	same(t, "its cubes", func() []string { p, _ := f.ask("orders", "MDSCHEMA_CUBES"); return p.col("CUBE_NAME") }(), "orders")
	// asking for a member of a cube it may not read: nothing
	p, _ := f.ask("orders", "MDSCHEMA_MEMBERS", "MEMBER_UNIQUE_NAME", "[country].[country].[All]", "TREE_OP", "1")
	same(t, "members of another cube", p.col("MEMBER_UNIQUE_NAME"))
	p, _ = f.ask("orders", "MDSCHEMA_MEMBERS", "CUBE_NAME", "orders", "LEVEL_UNIQUE_NAME", "[order_id].[order_id].[order_id]")
	if len(p.rows) != 50 && f.m.XMLA.MaxMembers < 50 {
		t.Logf("orders members: %d", len(p.rows))
	}
	// the main key sees all three; a key not allowed an address or revoked still gets nothing
	same(t, "main", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "orders", "Sales")
	f.want(f.post(discoverBody("MDSCHEMA_CUBES", nil), "revoked"), 401, "key_revoked")
	f.want(f.post(discoverBody("MDSCHEMA_CUBES", nil), ""), 401, "key_missing")
}

func TestDiscoverWhileTheXMLAIsOff(t *testing.T) {
	f := salesFixture(t)
	f.m.XMLA = nil
	f.s.Apply(f.m)
	f.want(f.post(discoverBody("MDSCHEMA_CUBES", nil), "main"), 404, "xmla_off")
	f.want(f.post(discoverBody("MDSCHEMA_MEMBERS", nil), "main"), 404, "xmla_off")
	f.m.XMLA = &dataapiwire.XMLA{MaxCells: 1000, MaxTuples: 100, MaxMembers: 50}
	f.s.Apply(f.m)
	if p, a := f.ask("main", "MDSCHEMA_CUBES"); p == nil {
		t.Fatalf("%d %s", a.status, a.body)
	}
}

func TestDiscoverFollowsTheManifest(t *testing.T) {
	f := salesFixture(t)
	same(t, "before", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "orders", "Sales")
	// the caption of the cube changes with the next manifest, at once
	for i := range f.m.Resources {
		if f.m.Resources[i].Name == "sales" {
			f.m.Resources[i].Cube = "Revenue"
		}
	}
	f.s.Apply(f.m)
	same(t, "renamed", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "orders", "Revenue")
	same(t, "its measures", f.must("MDSCHEMA_MEASUREGROUPS", "CUBE_NAME", "Revenue").col("MEASUREGROUP_NAME"), "Revenue")
	same(t, "the old name", f.must("MDSCHEMA_MEASUREGROUPS", "CUBE_NAME", "Sales").col("MEASUREGROUP_NAME"))
	// a new version of the data: another table, with other rows, replaces the first once it is prepared
	db, _ := sql.Open("duckdb", "")
	if _, err := db.Exec(fmt.Sprintf(`COPY (SELECT 1 AS id, 'ES' AS country) TO '%s' (FORMAT parquet)`, filepath.Join(f.dir, "storage", "sales2.parquet"))); err != nil {
		t.Fatal(err)
	}
	db.Close()
	for i := range f.m.Resources {
		if f.m.Resources[i].Name == "sales" {
			r := &f.m.Resources[i]
			r.URL, r.Fingerprint, r.Version, r.Columns = f.files.URL+"/sales2.parquet", "s2", "2026-11-08T06:00:00Z", []string{"id", "country"}
		}
	}
	f.s.Apply(f.m)
	// not prepared yet: the old table answers, with the new caption
	same(t, "old data", f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Revenue", "LEVEL_UNIQUE_NAME", "[country].[country].[country]").col("MEMBER_CAPTION")[:2], "", "Città")
	f.s.PrepareAll(t.Context())
	same(t, "new data", f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Revenue", "LEVEL_UNIQUE_NAME", "[country].[country].[country]").col("MEMBER_CAPTION"), "ES")
	same(t, "new columns", f.must("MDSCHEMA_DIMENSIONS", "CUBE_NAME", "Revenue").col("DIMENSION_UNIQUE_NAME"), "[Measures]", "[id]", "[country]")
	if got := f.must("MDSCHEMA_CUBES").rows[2]["LAST_DATA_UPDATE"]; got != "2026-11-08T06:00:00Z" {
		t.Errorf("update %s", got)
	}
	// a resource that has no table yet: the list of cubes is not given short
	f.m.Resources = append(f.m.Resources, dataapiwire.Resource{Name: "later", Available: true, Version: "v", Fingerprint: "x", URL: f.files.URL + "/missing.parquet",
		Columns: []string{"id"}, Filters: []dataapiwire.Filter{}, MaxPage: 10})
	f.s.Apply(f.m)
	a := f.post(discoverBody("MDSCHEMA_CUBES", nil), "main")
	if a.status != 500 || !strings.Contains(string(a.body), "«later» is still loading") {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// a rowset that needs no cube is answered meanwhile
	if p, a := f.ask("main", "DISCOVER_PROPERTIES"); p == nil {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// and one that has nothing to describe (no data, or the creator lost the data) is left out
	f.m.Resources[len(f.m.Resources)-1].Unavailable = dataapiwire.UnavailableNoData
	f.s.Apply(f.m)
	same(t, "without it", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "orders", "Revenue")
	for i := range f.m.Resources {
		if f.m.Resources[i].Name == "orders" {
			f.m.Resources[i].Unavailable = dataapiwire.UnavailableRights
		}
	}
	f.s.Apply(f.m)
	same(t, "without the one the creator may not read", f.must("MDSCHEMA_CUBES").col("CUBE_NAME"), "customers", "Revenue")
}

func TestDiscoverUnknownRowsetIsLoggedByNameOnly(t *testing.T) {
	f := salesFixture(t)
	a := f.post(discoverBody("MDSCHEMA_ACTIONS", map[string]string{"CUBE_NAME": "TOPSECRETVALUE"}, "CUBE_NAME"), "main")
	if a.status != 500 || !strings.Contains(string(a.body), "«MDSCHEMA_ACTIONS» is not served by this interface") {
		t.Fatalf("%d %s", a.status, a.body)
	}
	logs := f.logs.String()
	if !strings.Contains(logs, `"rowset":"MDSCHEMA_ACTIONS"`) || strings.Contains(logs, "TOPSECRETVALUE") {
		t.Errorf("log: %s", logs)
	}
	if !strings.Contains(logs, `"code":"xmla_rowset_unknown"`) {
		t.Errorf("log: %s", logs)
	}
}

func TestDiscoverNoValuesInTheLog(t *testing.T) {
	f := salesFixture(t)
	f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "MEMBER_UNIQUE_NAME", "[country].[country].[country].&[Città]", "TREE_OP", "8")
	f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "[country].[country].[country]")
	logs := f.logs.String()
	for _, secret := range []string{"Città", "FR", "[country]"} {
		if strings.Contains(logs, secret) {
			t.Errorf("%q in the log", secret)
		}
	}
	if !strings.Contains(logs, `"rows":7`) {
		t.Error("the rows are counted")
	}
}

// the model in the pod: the same names that a client reads in Discover read back to the pieces
func TestDiscoverNamesReadBack(t *testing.T) {
	f := salesFixture(t)
	snap := f.s.snap.Load()
	tb := f.s.table("sales")
	defer tb.release()
	cube := cubeOf(snap.resources["sales"], tb)
	for _, row := range f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales").rows {
		ref, err := cube.ResolveName(row["MEMBER_UNIQUE_NAME"])
		if err != nil || ref.Kind != xmlamodel.RefMember && ref.Kind != xmlamodel.RefMeasure {
			t.Errorf("%s: %v", row["MEMBER_UNIQUE_NAME"], err)
		}
	}
	for _, row := range f.must("MDSCHEMA_LEVELS", "CUBE_NAME", "Sales").rows {
		if _, err := cube.ResolveName(row["LEVEL_UNIQUE_NAME"]); err != nil && !strings.HasPrefix(row["LEVEL_UNIQUE_NAME"], "[Measures]") {
			t.Errorf("%s: %v", row["LEVEL_UNIQUE_NAME"], err)
		}
	}
}
