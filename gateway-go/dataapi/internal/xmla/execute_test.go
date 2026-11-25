package xmla

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla/mdx"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// The parts of Execute that need no engine: the text of the SQL a plan becomes, through the
// dialect, with its bound values; the ordering of members; the values of the cells and how they are
// written. The numbers are the pod's tests (xmla_execute_test.go in the pod), against DuckDB.

func testCube() *xmlamodel.Cube {
	return xmlamodel.Build("Sales", "sales", []xmlamodel.Column{
		{Name: "country", Kind: dataapiwire.KindString}, {Name: "code", Kind: dataapiwire.KindInt},
		{Name: "amount", Kind: dataapiwire.KindDecimal, Width: 12, Scale: 2}, {Name: "day", Kind: dataapiwire.KindDate}})
}

func mem(l *xmlamodel.Level, key string) xmlamodel.Member {
	return xmlamodel.Member{Level: l, Kind: xmlamodel.MemberKey, Key: key}
}

// a second dialect, to see what a plan's SQL looks like when only the dialect changes: numbered
// markers, other quotes, another way to write the date parts
type numbered struct{ DuckDB }

func (numbered) Quote(name string) string { return "`" + name + "`" }
func (numbered) Placeholder(n int) string { return fmt.Sprintf("$%d", n) }
func (numbered) Year(x string) string     { return "EXTRACT(YEAR FROM " + x + ")" }
func (numbered) Quarter(x string) string  { return "EXTRACT(QUARTER FROM " + x + ")" }
func (numbered) Month(x string) string    { return "EXTRACT(MONTH FROM " + x + ")" }
func (numbered) Text(x string) string     { return "CAST(" + x + " AS TEXT)" }
func (numbered) Date(x string) string     { return "DATE(" + x + ")" }
func (numbered) Limit(n int) string       { return fmt.Sprintf("FETCH FIRST %d ROWS ONLY", n) }
func (numbered) CreateTable(name, query string) string {
	return "CREATE TEMPORARY TABLE " + name + " AS " + query
}

func TestPlanBecomesSQLAndValues(t *testing.T) {
	cube := testCube()
	country := cube.Hierarchy("[country].[country]")
	cal := cube.Hierarchy("[day].[Year-Quarter-Month-Day]")
	code := cube.Hierarchy("[code].[code]")
	amount := cube.Measure("amount")
	rows := cube.Measures[0]
	filters := []hfilter{
		{country, []xmlamodel.Member{mem(country.Levels[1], "IT"), mem(country.Levels[1], "FR")}},
		{cal, []xmlamodel.Member{mem(cal.Levels[2], "2026-Q1"), mem(cal.Levels[1], "2025"), {Level: cal.Levels[1], Kind: xmlamodel.MemberNull}}},
		{code, []xmlamodel.Member{mem(code.Levels[1], "7"), mem(code.Levels[1], "10")}},
	}
	// the totals of every level in one pass: a field at All and its level, the calendar at three levels
	g := &gen{d: DuckDB{}, table: "serve"}
	q, err := g.aggSelect([]hUse{{country, []int{0, 1}}, {cal, []int{0, 1, 3}}}, []*xmlamodel.Measure{rows, amount}, " WHERE x", false)
	if err != nil {
		t.Fatal(err)
	}
	want := `SELECT CASE GROUPING(g0) WHEN 1 THEN 0 WHEN 0 THEN 1 END AS lv0, CASE GROUPING(g0) WHEN 1 THEN 'a' WHEN 0 THEN COALESCE(('v' || CAST(g0 AS VARCHAR)), 'n') END AS k0, ` +
		`CASE GROUPING(g1, g2) WHEN 3 THEN 0 WHEN 1 THEN 1 WHEN 0 THEN 3 END AS lv1, ` +
		`CASE GROUPING(g1, g2) WHEN 3 THEN 'a' WHEN 1 THEN COALESCE(('v' || lpad(CAST(g1 AS VARCHAR), 4, '0')), 'n') ` +
		`WHEN 0 THEN COALESCE(('v' || (lpad(CAST(g1 AS VARCHAR), 4, '0') || '-' || lpad(CAST(g2 AS VARCHAR), 2, '0'))), 'n') END AS k1, ` +
		`NULLIF(count(*), 0) AS m0, sum(c0) AS m1 ` +
		`FROM (SELECT "country" AS g0, year("day") AS g1, month("day") AS g2, "amount" AS c0 FROM "serve" WHERE x) ` +
		`GROUP BY GROUPING SETS ((), (g1), (g1, g2), (g0), (g0, g1), (g0, g1, g2))`
	if q != want {
		t.Errorf("DuckDB:\n got %s\nwant %s", q, want)
	}
	// the same plan through another dialect: the identifiers and the date parts follow it, the shape does not
	g2 := &gen{d: numbered{}, table: "serve"}
	q2, err := g2.aggSelect([]hUse{{country, []int{0, 1}}, {cal, []int{0, 1, 3}}}, []*xmlamodel.Measure{rows, amount}, " WHERE x", false)
	if err != nil {
		t.Fatal(err)
	}
	for _, w := range []string{"`country` AS g0", "EXTRACT(YEAR FROM `day`) AS g1", "EXTRACT(MONTH FROM `day`) AS g2", "FROM `serve`", "CAST(g0 AS TEXT)",
		"GROUP BY GROUPING SETS ((), (g1), (g1, g2), (g0), (g0, g1), (g0, g1, g2))"} {
		if !strings.Contains(q2, w) {
			t.Errorf("the other dialect: %q is not in\n%s", w, q2)
		}
	}
	if strings.Contains(q2, `"`) || strings.Contains(q2, "year(") {
		t.Errorf("DuckDB's own text in the other dialect:\n%s", q2)
	}
	// the filters: a value of the MDX is a bound value, in the order of the text
	for _, tc := range []struct {
		d     Dialect
		where string
		args  []any
	}{
		{DuckDB{}, ` WHERE (CAST("country" AS VARCHAR) IN (?, ?)) AND ((year("day") = ? AND quarter("day") = ?) OR (year("day") = ?) OR ("day" IS NULL)) AND ("code" IN (?, ?))`,
			[]any{"IT", "FR", 2026, 1, 2025, int64(7), int64(10)}},
		{numbered{}, ` WHERE (CAST(` + "`country`" + ` AS TEXT) IN ($1, $2)) AND ((EXTRACT(YEAR FROM ` + "`day`" + `) = $3 AND EXTRACT(QUARTER FROM ` + "`day`" + `) = $4) OR (EXTRACT(YEAR FROM ` + "`day`" + `) = $5) OR (` + "`day`" + ` IS NULL)) AND (` + "`code`" + ` IN ($6, $7))`,
			[]any{"IT", "FR", 2026, 1, 2025, int64(7), int64(10)}},
	} {
		b := &qb{d: tc.d}
		gg := &gen{d: tc.d, table: "serve"}
		got := gg.where(b, nil, filters)
		if got != tc.where || fmt.Sprint(b.args) != fmt.Sprint(tc.args) {
			t.Errorf("%T:\n got %s %v\nwant %s %v", tc.d, got, b.args, tc.where, tc.args)
		}
	}
	// a key that is SQL stays out of the text
	evil := "x'; DROP TABLE serve; --"
	b := &qb{d: DuckDB{}}
	got := g.where(b, nil, []hfilter{{country, []xmlamodel.Member{mem(country.Levels[1], evil), mem(country.Levels[1], "b")}}}, []hfilter{{code, []xmlamodel.Member{mem(code.Levels[1], evil)}}})
	if strings.Contains(got, "DROP") || strings.Contains(got, "x'") || len(b.args) != 3 || b.args[0] != evil {
		t.Errorf("%s %v", got, b.args)
	}
	// a filter with no member lets nothing through, one with the All member everything
	b = &qb{d: DuckDB{}}
	if got := g.where(b, nil, []hfilter{{}}); got != " WHERE 1 = 0" {
		t.Errorf("%q", got)
	}
	if got := g.where(b, nil, []hfilter{{country, []xmlamodel.Member{country.All()}}}); got != "" {
		t.Errorf("%q", got)
	}
	// the tuples of a level of the calendar, below members: what the planner reads
	rec := &recordRunner{}
	p := &planner{ctx: context.Background(), run: rec, g: g, cube: cube, max: 10,
		universe: []hfilter{{country, []xmlamodel.Member{mem(country.Levels[1], "IT")}}}}
	_, _ = p.scan(cal.Levels[2], []xmlamodel.Member{mem(cal.Levels[1], "2026")}, 10)
	wantScan := `SELECT (lpad(CAST(g0 AS VARCHAR), 4, '0') || '-Q' || CAST(g1 AS VARCHAR)), count(DISTINCT ch) FROM (SELECT year("day") AS g0, quarter("day") AS g1, ` +
		`month("day") AS ch FROM "serve" WHERE ((CAST("country" AS VARCHAR) = ?)) AND ((year("day") = ?)) AND "day" IS NOT NULL) GROUP BY g0, g1 ORDER BY g0 ASC NULLS LAST, g1 ASC NULLS LAST LIMIT 11`
	if len(rec.texts) != 1 || rec.texts[0] != wantScan || fmt.Sprint(rec.args[0]) != "[IT 2026]" {
		t.Errorf("scan:\n got %v %v\nwant %s", rec.texts, rec.args, wantScan)
	}
}

type recordRunner struct {
	texts []string
	args  [][]any
}

func (r *recordRunner) ExecContext(ctx context.Context, q string, args ...any) (sql.Result, error) {
	r.texts, r.args = append(r.texts, q), append(r.args, args)
	return nil, errors.New("not here")
}

func (r *recordRunner) QueryContext(ctx context.Context, q string, args ...any) (*sql.Rows, error) {
	r.texts, r.args = append(r.texts, q), append(r.args, args)
	return nil, errors.New("not here")
}

func TestTooManyLevelsAreRefused(t *testing.T) {
	cube := testCube()
	g := &gen{d: DuckDB{}, table: "serve"}
	var uses []hUse
	for _, d := range cube.Dimensions {
		uses = append(uses, hUse{d.Hierarchies[0], []int{0, 1}})
		if d.Time {
			uses[len(uses)-1].levels = []int{0, 1, 2, 3, 4}
		}
	}
	// 2 × 2 × 2 × 5 = 40 sets: fine; the levels of a few fields multiplied are not
	if _, err := g.aggSelect(uses, cube.Measures[:1], "", false); err != nil {
		t.Fatal(err)
	}
	// 5 × 5 × 5 = 125 sets are past the cap of 64 (each is a pass over the rows): a fault that names
	// the cap and its value, a limit and not a refusal of the statement
	many := []hUse{}
	for i := 0; i < 3; i++ {
		many = append(many, hUse{cube.Hierarchy("[day].[Year-Quarter-Month-Day]"), []int{0, 1, 2, 3, 4}})
	}
	_, err := g.aggSelect(many, cube.Measures[:1], "", false)
	var f *Fault
	if !errors.As(err, &f) || f.Refused || f.Code != CodeLimit || !strings.Contains(f.Message, "more than 64 grouping sets") {
		t.Errorf("%v", err)
	}
	// 4 × 4 × 4 = 64 is at the cap, and fits
	for i := range many {
		many[i].levels = []int{0, 1, 2, 3}
	}
	if _, err := g.aggSelect(many, cube.Measures[:1], "", false); err != nil {
		t.Errorf("at the cap: %v", err)
	}
}

func TestOrderOfMembers(t *testing.T) {
	cube := testCube()
	country := cube.Hierarchy("[country].[country]")
	cal := cube.Hierarchy("[day].[Year-Quarter-Month-Day]")
	code := cube.Hierarchy("[code].[code]")
	null := xmlamodel.Member{Level: country.Levels[1], Kind: xmlamodel.MemberNull}
	// values in their own order, a null last: integers by value, not by text
	if keyCmp(mem(code.Levels[1], "-3"), mem(code.Levels[1], "10")) >= 0 || keyCmp(mem(code.Levels[1], "9"), mem(code.Levels[1], "10")) >= 0 ||
		keyCmp(mem(country.Levels[1], "a"), null) >= 0 || keyCmp(null, mem(country.Levels[1], "a")) <= 0 || keyCmp(null, null) != 0 {
		t.Error("values")
	}
	// the years of a calendar by value, even past four digits
	if keyCmp(mem(cal.Levels[1], "9999"), mem(cal.Levels[1], "10000")) >= 0 {
		t.Error("years")
	}
	// a parent before its children, the children of a parent together, whatever the order they came in
	pt := &part{h: cal, items: []item{
		{m: mem(cal.Levels[3], "2026-02")}, {m: mem(cal.Levels[2], "2026-Q2")}, {m: cal.All()}, {m: mem(cal.Levels[2], "2025-Q4")},
		{m: mem(cal.Levels[1], "2026")}, {m: mem(cal.Levels[3], "2026-01")}, {m: mem(cal.Levels[1], "2025")}, {m: mem(cal.Levels[2], "2026-Q1")},
	}}
	hierarchize(pt, false)
	var got []string
	for _, it := range pt.items {
		got = append(got, it.m.Caption())
	}
	if want := "All 2025 2025-Q4 2026 2026-Q1 2026-01 2026-02 2026-Q2"; strings.Join(got, " ") != want {
		t.Errorf("\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	hierarchize(pt, true)
	got = nil
	for _, it := range pt.items {
		got = append(got, it.m.Caption())
	}
	if want := "2025-Q4 2025 2026-01 2026-02 2026-Q1 2026-Q2 2026 All"; strings.Join(got, " ") != want {
		t.Errorf("POST:\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	// ordering by value: stable, the empty ones last whichever way
	f := func(v float64) *float64 { return &v }
	items := func() *part {
		return &part{h: country, items: []item{{m: mem(country.Levels[1], "a")}, {m: mem(country.Levels[1], "b")}, {m: mem(country.Levels[1], "c")},
			{m: mem(country.Levels[1], "d")}, {m: mem(country.Levels[1], "e")}}}
	}
	vals := [][]*float64{{f(2)}, {nil}, {f(5)}, {f(2)}, {f(1)}}
	for _, tc := range []struct {
		desc bool
		want string
	}{{true, "c a d e b"}, {false, "e a d c b"}} {
		p := items()
		orderBy(p, vals, 0, tc.desc)
		var got []string
		for _, it := range p.items {
			got = append(got, it.m.Key)
		}
		if strings.Join(got, " ") != tc.want {
			t.Errorf("desc=%v: %v, want %s", tc.desc, got, tc.want)
		}
	}
}

func TestFilterConditionReadsEmptyAsZero(t *testing.T) {
	st, err := mdx.Parse(`SELECT Filter({[a].[b].[c]}, [Measures].[x] > 1 AND [Measures].[y] <= 0 OR [Measures].[x] = 7) ON COLUMNS FROM [c]`)
	if err != nil {
		t.Fatal(err)
	}
	cond := st.Axes[0].Set.(*mdx.Call).Args[1]
	index := map[*mdx.Ref]int{}
	var walk func(e mdx.Expr)
	n := 0
	walk = func(e mdx.Expr) {
		switch c := e.(type) {
		case *mdx.Compare:
			index[c.Left] = n
			n++
		case *mdx.Logic:
			for _, x := range c.Terms {
				walk(x)
			}
		}
	}
	walk(cond)
	f := func(v float64) *float64 { return &v }
	for _, tc := range []struct {
		vals []*float64
		want bool
	}{
		{[]*float64{f(2), nil, f(0)}, true},   // x > 1 and y empty (0) <= 0
		{[]*float64{f(2), f(3), f(0)}, false}, // y > 0
		{[]*float64{nil, nil, f(7)}, true},    // the OR: x = 7 is the third comparison
		{[]*float64{f(0), nil, f(7)}, true},   // the OR
		{[]*float64{nil, nil, nil}, false},
	} {
		if got := holds(cond, tc.vals, index); got != tc.want {
			t.Errorf("%v: %v, want %v", tc.vals, got, tc.want)
		}
	}
}

func TestTuplesThatAreAProduct(t *testing.T) {
	cube := testCube()
	country := cube.Hierarchy("[country].[country]")
	code := cube.Hierarchy("[code].[code]")
	tup := func(c, n string) *setVal {
		return &setVal{parts: []*part{{h: country, items: []item{{m: mem(country.Levels[1], c)}}}, {h: code, items: []item{{m: mem(code.Levels[1], n)}}}}}
	}
	// the product in the order of the product: the first hierarchy varies slowest
	v, err := union([]*setVal{tup("IT", "1"), tup("IT", "2"), tup("FR", "1"), tup("FR", "2")})
	if err != nil || len(v.parts) != 2 || len(v.parts[0].items) != 2 || len(v.parts[1].items) != 2 {
		t.Fatalf("%v %+v", err, v)
	}
	// the same members in another order, a hole, a repeat: not rendered
	for name, in := range map[string][]*setVal{
		"another order": {tup("IT", "1"), tup("FR", "1"), tup("IT", "2"), tup("FR", "2")},
		"a hole":        {tup("IT", "1"), tup("IT", "2"), tup("FR", "1")},
		"a repeat":      {tup("IT", "1"), tup("IT", "1")},
	} {
		if _, err := union(in); err == nil || !strings.Contains(err.Error(), "product") {
			t.Errorf("%s: %v", name, err)
		}
	}
	// a set that is a single tuple, or of one hierarchy, is as it is
	if v, err := union([]*setVal{tup("IT", "1")}); err != nil || len(v.parts) != 2 {
		t.Errorf("%v", err)
	}
	// nothing and something
	if v, err := union([]*setVal{{}, tup("IT", "1"), {}}); err != nil || v.size() != 1 {
		t.Errorf("%v", err)
	}
	if v, err := union(nil); err != nil || !v.empty() {
		t.Errorf("%v", err)
	}
}

func TestCellValues(t *testing.T) {
	cube := testCube()
	amount, rows, avg := cube.Measure("amount"), cube.Measures[0], cube.Measure("amount (Average)")
	code := cube.Measure("code")
	for _, tc := range []struct {
		m             *xmlamodel.Measure
		in, typ, want string
		formatted     string
	}{
		{rows, "8", "xsd:int", "8", "8"},
		{rows, "2147483647", "xsd:int", "2147483647", "2,147,483,647"},
		{rows, "2147483648", "xsd:long", "2147483648", "2,147,483,648"},
		{code, "-1234567", "xsd:int", "-1234567", "-1,234,567"},
		{code, "99999999999999999999999", "xsd:decimal", "99999999999999999999999", "99,999,999,999,999,999,999,999"},
		{amount, "1234567.50", "xsd:decimal", "1234567.50", "1,234,567.50"},
		{amount, "-0.05", "xsd:decimal", "-0.05", "-0.05"},
		{avg, "7.875", "xsd:double", "7.875", "7.88"},
		{avg, "1234.5", "xsd:double", "1234.5", "1,234.50"},
		{avg, "1e-05", "xsd:double", "1e-05", "0.00"},
		{avg, "inf", "xsd:double", "INF", "INF"},
		{avg, "-inf", "xsd:double", "-INF", "-INF"},
		{avg, "nan", "xsd:double", "NaN", "NaN"},
	} {
		typ, text := valueOf(tc.m, tc.in)
		if typ != tc.typ || text != tc.want {
			t.Errorf("%s %q: %s %q, want %s %q", tc.m.Name, tc.in, typ, text, tc.typ, tc.want)
		}
		if got := formatNumber(tc.m, text); got != tc.formatted {
			t.Errorf("%s %q formatted %q, want %q", tc.m.Name, tc.in, got, tc.formatted)
		}
	}
}

func TestExecuteProperties(t *testing.T) {
	cat := &Catalog{Name: "CRM"}
	ask := func(props map[string]string) (execProps, error) {
		return checkExecProps(&Request{Properties: props}, cat)
	}
	if p, err := ask(nil); err != nil || p.content != "SchemaData" {
		t.Errorf("%+v %v", p, err)
	}
	for _, c := range []string{"data", "SCHEMA", "None"} {
		if p, err := ask(map[string]string{"Content": c}); err != nil || !strings.EqualFold(p.content, c) {
			t.Errorf("%s: %+v %v", c, p, err)
		}
	}
	for _, p := range []map[string]string{{"Format": "Tabular"}, {"AxisFormat": "ClusterFormat"}, {"Content": "x"}, {"Catalog": "other"}} {
		_, err := ask(p)
		var f *Fault
		if !errors.As(err, &f) || f.Refused {
			t.Errorf("%v: %v", p, err)
		}
	}
	if _, err := ask(map[string]string{"Format": "multidimensional", "AxisFormat": "tupleformat", "Catalog": "crm"}); err != nil {
		t.Error(err)
	}
}

func TestParserErrorsBecomeFaults(t *testing.T) {
	for _, tc := range []struct {
		mdx  string
		code uint32
	}{
		{`SELECT {[a]} ON COLUMNS`, CodeNotServed}, // no FROM
		{`WITH MEMBER [Measures].[x] AS '1' SELECT FROM [c]`, CodeNotServed},
		{`SELECT {[a ON COLUMNS FROM [c]`, CodeBadRequest},
		{``, CodeBadRequest},
	} {
		_, err := mdx.Parse(tc.mdx)
		f := parseFault(err)
		if f.Code != tc.code || !f.Refused || !strings.Contains(f.Message, "was not understood") {
			t.Errorf("%q: %+v", tc.mdx, f)
		}
	}
}

func TestOrdinalsDoNotOverflow(t *testing.T) {
	if n, ok := product(10_000, 10_000, 10_000, 10_000); !ok || n != 1e16 {
		t.Errorf("%d %v", n, ok)
	}
	if _, ok := product(10_000, 10_000, 10_000, 10_000, 10_000, 10_000); ok {
		t.Error("past 2^62")
	}
	if n, ok := product(5, 0, 1<<40); !ok || n != 0 {
		t.Errorf("%d %v", n, ok)
	}
}

func TestEmptyExecuteAnswer(t *testing.T) {
	var b strings.Builder
	if err := WriteEmptyExecute(&b, ""); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(b.String(), `<ExecuteResponse xmlns="`+NSXMLA+`"><return><root xmlns="`+NSEmpty+`"/></return></ExecuteResponse>`) || strings.Contains(b.String(), "Header") {
		t.Errorf("%s", b.String())
	}
	b.Reset()
	_ = WriteEmptyExecute(&b, `ab"c`)
	if !strings.Contains(b.String(), `<soap:Header><Session xmlns="`+NSXMLA+`" SessionId="ab&#34;c"/></soap:Header>`) {
		t.Errorf("%s", b.String())
	}
}
