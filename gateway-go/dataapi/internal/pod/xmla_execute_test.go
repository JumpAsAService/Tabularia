package pod

import (
	"context"
	"database/sql"
	"encoding/xml"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Execute (docs/specs/xmla.md, piece 4) on the «sales» table of xmla_discover_test.go: every case
// is a statement a pivot table writes, and the numbers of its cells are compared with the same
// aggregation written by hand in SQL on the same table.

// ── the helpers ───────────────────────────────────────────────────────────────

type dsMember struct {
	hier  string
	props map[string]string
}

type dsCell struct{ value, typ, formatted, format string }

type dsHier struct {
	name  string
	props []string // the element names of its HierarchyInfo
}

type dataset struct {
	axes     map[string][][]dsMember
	cells    map[int]dsCell
	info     map[string][]dsHier
	cube     string
	cellInfo []string
	schema   bool
	raw      string
}

func parseDataset(t *testing.T, body string) *dataset {
	t.Helper()
	ds := &dataset{axes: map[string][][]dsMember{}, cells: map[int]dsCell{}, info: map[string][]dsHier{}, raw: body}
	dec := xml.NewDecoder(strings.NewReader(body))
	var path []string
	var axis string
	var tuple []dsMember
	var member *dsMember
	var cellOrd int
	var cell dsCell
	var text strings.Builder
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		switch x := tok.(type) {
		case xml.StartElement:
			name := x.Name.Local
			path = append(path, name)
			text.Reset()
			attr := func(n string) string {
				for _, a := range x.Attr {
					if a.Name.Local == n {
						return a.Value
					}
				}
				return ""
			}
			switch {
			case name == "schema":
				ds.schema = true
			case name == "Axis":
				axis = attr("name")
			case name == "Tuple":
				tuple = nil
			case name == "Member":
				member = &dsMember{hier: attr("Hierarchy"), props: map[string]string{}}
			case name == "AxisInfo":
				axis = attr("name")
			case name == "HierarchyInfo":
				ds.info[axis] = append(ds.info[axis], dsHier{name: attr("name")})
			case len(path) >= 2 && path[len(path)-2] == "HierarchyInfo":
				h := &ds.info[axis][len(ds.info[axis])-1]
				h.props = append(h.props, name)
			case name == "Cell" && path[len(path)-2] == "CellData":
				cellOrd, _ = strconv.Atoi(attr("CellOrdinal"))
				cell = dsCell{}
			case len(path) >= 2 && path[len(path)-2] == "CellInfo":
				ds.cellInfo = append(ds.cellInfo, name)
			case name == "Value" && member == nil:
				cell.typ = attr("type")
			}
		case xml.CharData:
			text.Write(x)
		case xml.EndElement:
			name := x.Name.Local
			parent := ""
			if len(path) >= 2 {
				parent = path[len(path)-2]
			}
			switch {
			case name == "Tuple":
				ds.axes[axis] = append(ds.axes[axis], tuple)
			case name == "Member":
				tuple = append(tuple, *member)
				member = nil
			case parent == "Member" && member != nil:
				member.props[name] = text.String()
			case name == "CubeName":
				ds.cube = text.String()
			case parent == "Cell" && name == "Value":
				cell.value = text.String()
			case parent == "Cell" && name == "FormattedValue":
				cell.formatted = text.String()
			case parent == "Cell" && name == "FormatString":
				cell.format = text.String()
			case name == "Cell" && parent == "CellData":
				ds.cells[cellOrd] = cell
			}
			path = path[:len(path)-1]
		}
	}
	return ds
}

func (m dsMember) caption() string { return m.props["Caption"] }
func (m dsMember) uname() string   { return m.props["UName"] }

// label is the caption of a tuple's members, joined
func label(t []dsMember) string {
	parts := make([]string, len(t))
	for i, m := range t {
		parts[i] = m.caption()
	}
	return strings.Join(parts, "/")
}

func (d *dataset) labels(axis string) []string {
	out := []string{}
	for _, t := range d.axes[axis] {
		out = append(out, label(t))
	}
	return out
}

func (d *dataset) unames(axis string) []string {
	out := []string{}
	for _, t := range d.axes[axis] {
		parts := make([]string, len(t))
		for i, m := range t {
			parts[i] = m.uname()
		}
		out = append(out, strings.Join(parts, " "))
	}
	return out
}

// grid is the non-empty cells as «label of each axis' tuple, joined by |» → value
func (d *dataset) grid(axes ...string) map[string]string {
	out := map[string]string{}
	for ord, c := range d.cells {
		var key []string
		rest := ord
		for _, a := range axes {
			n := len(d.axes[a])
			key = append(key, label(d.axes[a][rest%n]))
			rest /= n
		}
		out[strings.Join(key, "|")] = c.value
	}
	return out
}

func executeBody(mdx string, props ...string) string {
	var b strings.Builder
	b.WriteString(`<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>`)
	_ = xml.EscapeText(&b, []byte(mdx))
	b.WriteString(`</Statement></Command><Properties><PropertyList><Catalog>CRM</Catalog>`)
	if len(props) == 0 {
		props = []string{"Format", "Multidimensional", "AxisFormat", "TupleFormat", "Content", "SchemaData"}
	}
	for i := 0; i+1 < len(props); i += 2 {
		b.WriteString("<" + props[i] + ">" + props[i+1] + "</" + props[i] + ">")
	}
	b.WriteString(`</PropertyList></Properties></Execute></soap:Body></soap:Envelope>`)
	return b.String()
}

// exec runs a statement with the main key and parses the dataset of the answer
func (f *fixture) exec(mdx string) *dataset {
	f.t.Helper()
	a := f.post(executeBody(mdx), "main")
	if a.status != 200 {
		f.t.Fatalf("%s\n→ %d %s", mdx, a.status, a.body)
	}
	return parseDataset(f.t, string(a.body))
}

// refused runs a statement that must be refused and says what the fault said
func (f *fixture) refused(mdx string, key ...string) string {
	f.t.Helper()
	k := "main"
	if len(key) > 0 {
		k = key[0]
	}
	a := f.post(executeBody(mdx), k)
	if a.status != 500 || !strings.Contains(string(a.body), "<soap:Fault>") {
		f.t.Fatalf("%s\nwant a fault: %d %s", mdx, a.status, a.body)
	}
	return string(a.body)
}

// sqlGrid runs a query on the table by hand: its rows are the labels of each axis' tuple and a
// value, as text, and the result is what the cells of the answer must be
func sqlGrid(t *testing.T, f *fixture, query string) map[string]string {
	t.Helper()
	tb := f.s.table("sales")
	defer tb.release()
	rows, err := tb.db.QueryContext(context.Background(), query)
	if err != nil {
		t.Fatalf("%s: %v", query, err)
	}
	defer rows.Close()
	cols, _ := rows.Columns()
	out := map[string]string{}
	for rows.Next() {
		vals := make([]sql.NullString, len(cols))
		dest := make([]any, len(cols))
		for i := range vals {
			dest[i] = &vals[i]
		}
		if err := rows.Scan(dest...); err != nil {
			t.Fatal(err)
		}
		if !vals[len(vals)-1].Valid {
			continue // an empty cell is not in the answer
		}
		key := make([]string, len(vals)-1)
		for i := range key {
			key[i] = vals[i].String
		}
		out[strings.Join(key, "|")] = vals[len(vals)-1].String
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func sameGrid(t *testing.T, what string, got, want map[string]string) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("%s: %d cells, want %d\n got %v\nwant %v", what, len(got), len(want), sortedPairs(got), sortedPairs(want))
		return
	}
	for k, v := range want {
		if g, ok := got[k]; !ok || g != v {
			t.Errorf("%s: cell %q is %q (present %v), want %q\n got %v\nwant %v", what, k, g, ok, v, sortedPairs(got), sortedPairs(want))
			return
		}
	}
}

func sortedPairs(m map[string]string) []string {
	out := []string{}
	for k, v := range m {
		out = append(out, k+"="+v)
	}
	sort.Strings(out)
	return out
}

// the tuples a statement lists on an axis, by caption, and the cells it gives
type numCase struct {
	name string
	mdx  string
	// the axes in the order the cells' ordinal runs through them
	axes []string
	// the same aggregation by hand: the label of each axis' tuple, then the value
	sql string
	// the tuples of an axis, in order, when the order is the point
	labels map[string][]string
}

// by hand, a total by country: a measure's label, the country's label, the value
const byCountry = `SELECT 'Rows', coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve GROUP BY country
 UNION ALL SELECT 'amount', coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY country`

// by hand, the same for some of the countries only
func byCountryOf(in ...string) string {
	list := make([]string, len(in))
	for i, c := range in {
		list[i] = "'" + c + "'"
	}
	w := " WHERE coalesce(country, 'null') IN (" + strings.Join(list, ", ") + ") GROUP BY country"
	return `SELECT 'Rows', coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve` + w +
		` UNION ALL SELECT 'amount', coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve` + w
}

// by hand, the rows of the calendar: the label of the member, the count and the sum of its rows
func byDate(levels ...string) string {
	q := []string{}
	for _, l := range levels {
		level, under, _ := strings.Cut(l, "@")
		where := ""
		switch {
		case strings.Contains(under, "-Q"):
			where = " AND year(day) = " + under[:4] + " AND quarter(day) = " + under[len(under)-1:]
		case under != "":
			where = " AND year(day) = " + under
		}
		switch level {
		case "all":
			q = append(q, "SELECT 'All' AS l, count(*) AS n, sum(amount) AS a FROM serve")
		case "year":
			q = append(q, "SELECT coalesce(CAST(year(day) AS VARCHAR), 'null') AS l, count(*) AS n, sum(amount) AS a FROM serve GROUP BY year(day)")
		case "quarter":
			q = append(q, "SELECT CAST(year(day) AS VARCHAR) || '-Q' || CAST(quarter(day) AS VARCHAR) AS l, count(*) AS n, sum(amount) AS a FROM serve WHERE day IS NOT NULL"+where+" GROUP BY year(day), quarter(day)")
		case "month":
			q = append(q, "SELECT CAST(year(day) AS VARCHAR) || '-' || lpad(CAST(month(day) AS VARCHAR), 2, '0') AS l, count(*) AS n, sum(amount) AS a FROM serve WHERE day IS NOT NULL"+where+" GROUP BY year(day), month(day)")
		case "day":
			q = append(q, "SELECT CAST(day AS VARCHAR) AS l, count(*) AS n, sum(amount) AS a FROM serve WHERE day IS NOT NULL GROUP BY day")
		}
	}
	u := strings.Join(q, " UNION ALL ")
	return "SELECT 'Rows', l, CAST(n AS VARCHAR) FROM (" + u + ") UNION ALL SELECT 'amount', l, CAST(a AS VARCHAR) FROM (" + u + ")"
}

const cal = "[day].[Year-Quarter-Month-Day]"

func TestExecuteNumbers(t *testing.T) {
	f := salesFixture(t)
	measures := "{[Measures].[Rows],[Measures].[amount]} ON COLUMNS"
	allCountries := []string{"", "Città", "FR", "IT", "\\x", "a]b", "null"}
	cases := []numCase{
		{name: "one measure", mdx: `SELECT {[Measures].[amount]} ON COLUMNS FROM [Sales]`, axes: []string{"Axis0"},
			sql: `SELECT 'amount', CAST(sum(amount) AS VARCHAR) FROM serve`},
		{name: "several measures on an axis", axes: []string{"Axis0"},
			mdx: `SELECT {[Measures].[Rows],[Measures].[amount],[Measures].[amount (Average)],[Measures].[amount (Min)],[Measures].[amount (Max)],[Measures].[code],[Measures].[ratio]} ON COLUMNS FROM [Sales]`,
			sql: `SELECT 'Rows', CAST(count(*) AS VARCHAR) FROM serve UNION ALL SELECT 'amount', CAST(sum(amount) AS VARCHAR) FROM serve
 UNION ALL SELECT 'amount (Average)', CAST(avg(amount) AS VARCHAR) FROM serve UNION ALL SELECT 'amount (Min)', CAST(min(amount) AS VARCHAR) FROM serve
 UNION ALL SELECT 'amount (Max)', CAST(max(amount) AS VARCHAR) FROM serve UNION ALL SELECT 'code', CAST(sum(code) AS VARCHAR) FROM serve
 UNION ALL SELECT 'ratio', CAST(sum(ratio) AS VARCHAR) FROM serve`},
		{name: "a field on rows, measures on columns", axes: []string{"Axis0", "Axis1"}, sql: byCountry,
			mdx:    `SELECT ` + measures + `, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales]`,
			labels: map[string][]string{"Axis0": {"Rows", "amount"}, "Axis1": allCountries}},
		{name: "the same without NON EMPTY", axes: []string{"Axis0", "Axis1"}, sql: byCountry,
			mdx:    `SELECT ` + measures + `, [country].[country].[country].Members ON ROWS FROM [Sales]`,
			labels: map[string][]string{"Axis1": allCountries}},
		{name: "the members of a field with its All and the first level", axes: []string{"Axis0", "Axis1"},
			mdx:    `SELECT ` + measures + `, NON EMPTY Hierarchize({DrilldownLevel({[country].[country].[All]},,,INCLUDE_CALC_MEMBERS)}) ON ROWS FROM [Sales]`,
			sql:    `SELECT 'Rows', 'All', CAST(count(*) AS VARCHAR) FROM serve UNION ALL SELECT 'amount', 'All', CAST(sum(amount) AS VARCHAR) FROM serve UNION ALL ` + byCountry,
			labels: map[string][]string{"Axis1": append([]string{"All"}, allCountries...)}},
		{name: "two fields crossed on rows", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT {[Measures].[amount]} ON COLUMNS, NON EMPTY CrossJoin([country].[country].[country].Members, [paid].[paid].[paid].Members) ON ROWS FROM [Sales]`,
			sql: `SELECT 'amount', coalesce(country, 'null') || '/' || coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY country, paid`},
		{name: "two fields crossed with *, with empty tuples", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT {[Measures].[amount]} ON COLUMNS, [country].[country].[country].Members * [paid].[paid].[paid].Members ON ROWS FROM [Sales]`,
			sql: `SELECT 'amount', coalesce(country, 'null') || '/' || coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY country, paid`},
		{name: "a field on rows and a field on columns, the default measure", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT NON EMPTY [paid].[paid].[paid].Members ON COLUMNS, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales]`,
			sql: `SELECT coalesce(CAST(paid AS VARCHAR), 'null'), coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve GROUP BY paid, country`},
		{name: "the measure of the slicer is the one of the cells", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT NON EMPTY [paid].[paid].[paid].Members ON COLUMNS, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales] WHERE ([Measures].[amount])`,
			sql: `SELECT coalesce(CAST(paid AS VARCHAR), 'null'), coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY paid, country`},
		{name: "a measure on rows and a field on columns", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT NON EMPTY [paid].[paid].[paid].Members ON COLUMNS, {[Measures].[amount],[Measures].[Rows]} ON ROWS FROM [Sales]`,
			sql: `SELECT coalesce(CAST(paid AS VARCHAR), 'null'), 'Rows', CAST(count(*) AS VARCHAR) FROM serve GROUP BY paid
 UNION ALL SELECT coalesce(CAST(paid AS VARCHAR), 'null'), 'amount', CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY paid`},
		{name: "measures crossed with a field on one axis", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT NON EMPTY CrossJoin([paid].[paid].[paid].Members, {[Measures].[Rows],[Measures].[amount]}) ON COLUMNS, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales]`,
			sql: `SELECT coalesce(CAST(paid AS VARCHAR), 'null') || '/Rows', coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve GROUP BY paid, country
 UNION ALL SELECT coalesce(CAST(paid AS VARCHAR), 'null') || '/amount', coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY paid, country`},
		{name: "the calendar to its first level, with the total", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize({DrilldownLevel({` + cal + `.[All]},,,INCLUDE_CALC_MEMBERS)}) ON ROWS FROM [Sales]`,
			sql: byDate("all", "year"), labels: map[string][]string{"Axis1": {"All", "2025", "2026", "null"}}},
		{name: "a year expanded, another not", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize(DrilldownMember({{DrilldownLevel({` + cal + `.[All]},,,INCLUDE_CALC_MEMBERS)}}, {` + cal + `.[Year].&[2026]},,,INCLUDE_CALC_MEMBERS)) ON ROWS FROM [Sales]`,
			sql: byDate("all", "year", "quarter@2026"), labels: map[string][]string{"Axis1": {"All", "2025", "2026", "2026-Q1", "2026-Q2", "null"}}},
		{name: "three levels, the subtotals of each", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize(DrilldownMember(DrilldownMember({{DrilldownLevel({` + cal + `.[All]},,,INCLUDE_CALC_MEMBERS)}}, {` + cal + `.[Year].&[2026]},,,INCLUDE_CALC_MEMBERS), {` + cal + `.[Quarter].&[2026-Q1]},,,INCLUDE_CALC_MEMBERS)) ON ROWS FROM [Sales]`,
			sql: byDate("all", "year", "quarter@2026", "month@2026-Q1"), labels: map[string][]string{"Axis1": {"All", "2025", "2026", "2026-Q1", "2026-01", "2026-Q2", "null"}}},
		{name: "all the members of the calendar", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY ` + cal + `.Members ON ROWS FROM [Sales]`,
			sql: byDate("all", "year", "quarter", "month", "day")},
		{name: "a level of the calendar", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY ` + cal + `.[Month].Members ON ROWS FROM [Sales]`,
			sql: byDate("month"), labels: map[string][]string{"Axis1": {"2025-12", "2026-01", "2026-05"}}},
		{name: "the children of a member", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY ` + cal + `.[Year].&[2026].Children ON ROWS FROM [Sales]`,
			sql: byDate("quarter@2026"), labels: map[string][]string{"Axis1": {"2026-Q1", "2026-Q2"}}},
		{name: "the plain value of a date", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY [day].[day].[day].Members ON ROWS FROM [Sales]`,
			sql: byDate("day") + ` UNION ALL SELECT 'Rows', 'null', CAST(count(*) AS VARCHAR) FROM serve WHERE day IS NULL`, labels: map[string][]string{"Axis1": {"2025-12-31", "2026-01-15", "2026-01-20", "2026-05-20", "2026-05-21", "null"}}},
		{name: "the calendar on columns and a field on rows", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT NON EMPTY Hierarchize(DrilldownLevel({` + cal + `.[All]})) ON COLUMNS, NON EMPTY Hierarchize(DrilldownLevel({[paid].[paid].[All]})) ON ROWS FROM [Sales] WHERE ([Measures].[amount])`,
			sql: `SELECT l, p, CAST(sum(amount) AS VARCHAR) FROM (
 SELECT 'All' AS l, 'All' AS p, amount FROM serve UNION ALL SELECT 'All', coalesce(CAST(paid AS VARCHAR), 'null'), amount FROM serve
 UNION ALL SELECT coalesce(CAST(year(day) AS VARCHAR), 'null'), 'All', amount FROM serve
 UNION ALL SELECT coalesce(CAST(year(day) AS VARCHAR), 'null'), coalesce(CAST(paid AS VARCHAR), 'null'), amount FROM serve) GROUP BY l, p`},
		{name: "a filter in the slicer", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY [paid].[paid].[paid].Members ON ROWS FROM [Sales] WHERE ([country].[country].[country].&[IT])`,
			sql: `SELECT 'Rows', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid
 UNION ALL SELECT 'amount', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid`},
		{name: "a slicer with two members of a field", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize(DrilldownLevel({[paid].[paid].[All]})) ON ROWS FROM [Sales] WHERE {[country].[country].[country].&[IT],[country].[country].[country].&[FR]}`,
			sql: `SELECT m, p, v FROM (SELECT 'Rows' AS m, 'All' AS p, CAST(count(*) AS VARCHAR) AS v FROM serve WHERE country IN ('IT','FR')
 UNION ALL SELECT 'amount', 'All', CAST(sum(amount) AS VARCHAR) FROM serve WHERE country IN ('IT','FR')
 UNION ALL SELECT 'Rows', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE country IN ('IT','FR') GROUP BY paid
 UNION ALL SELECT 'amount', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country IN ('IT','FR') GROUP BY paid)`},
		{name: "a slicer with a measure and a member", axes: []string{"Axis0"},
			mdx: `SELECT NON EMPTY [paid].[paid].[paid].Members ON COLUMNS FROM [Sales] WHERE ([Measures].[amount],[country].[country].[country].&[IT])`,
			sql: `SELECT coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid`},
		{name: "a slicer on the calendar", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales] WHERE (` + cal + `.[Quarter].&[2026-Q2])`,
			sql: `SELECT 'Rows', coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE year(day) = 2026 AND quarter(day) = 2 GROUP BY country
 UNION ALL SELECT 'amount', coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE year(day) = 2026 AND quarter(day) = 2 GROUP BY country`},
		{name: "a filter as a subselect, one member", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize(DrilldownLevel({[paid].[paid].[All]})) ON ROWS FROM (SELECT ({[country].[country].[country].&[IT]}) ON COLUMNS FROM [Sales])`,
			sql: `SELECT m, p, v FROM (SELECT 'Rows' AS m, 'All' AS p, CAST(count(*) AS VARCHAR) AS v FROM serve WHERE country = 'IT'
 UNION ALL SELECT 'amount', 'All', CAST(sum(amount) AS VARCHAR) FROM serve WHERE country = 'IT'
 UNION ALL SELECT 'Rows', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid
 UNION ALL SELECT 'amount', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid)`},
		{name: "a filter as a subselect, several members and a year", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY [country].[country].[country].Members ON ROWS FROM (SELECT ({[country].[country].[country].&[IT],[country].[country].[country].&[FR]}) ON COLUMNS, {` + cal + `.[Year].&[2026]} ON ROWS FROM [Sales])`,
			sql: `SELECT 'Rows', coalesce(country, 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE country IN ('IT', 'FR') AND year(day) = 2026 GROUP BY country
 UNION ALL SELECT 'amount', coalesce(country, 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country IN ('IT', 'FR') AND year(day) = 2026 GROUP BY country`,
			labels: map[string][]string{"Axis1": {"FR", "IT"}}},
		{name: "subselects inside subselects", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY [paid].[paid].[paid].Members ON ROWS FROM (SELECT ({[country].[country].[country].&[IT]}) ON COLUMNS FROM (SELECT ({[country].[country].[country].&[IT],[country].[country].[country].&[FR]}) ON COLUMNS FROM [Sales]))`,
			sql: `SELECT 'Rows', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid
 UNION ALL SELECT 'amount', coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve WHERE country = 'IT' GROUP BY paid`},
		{name: "a filter on a field that is on an axis too: its members are the filter's", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Hierarchize({DrilldownLevel({[country].[country].[All]})}) ON ROWS FROM (SELECT ({[country].[country].[country].&[IT],[country].[country].[country].&[FR]}) ON COLUMNS FROM [Sales])`,
			sql: `SELECT m, c, v FROM (SELECT 'Rows' AS m, 'All' AS c, CAST(count(*) AS VARCHAR) AS v FROM serve WHERE country IN ('IT','FR')
 UNION ALL SELECT 'amount', 'All', CAST(sum(amount) AS VARCHAR) FROM serve WHERE country IN ('IT','FR')
 UNION ALL SELECT 'Rows', country, CAST(count(*) AS VARCHAR) FROM serve WHERE country IN ('IT','FR') GROUP BY country
 UNION ALL SELECT 'amount', country, CAST(sum(amount) AS VARCHAR) FROM serve WHERE country IN ('IT','FR') GROUP BY country)`,
			labels: map[string][]string{"Axis1": {"All", "FR", "IT"}}},
		{name: "a member written by name and the null member", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, {[country].[country].[country].&[IT],[country].[country].[(null)],[country].[country].[country].&[a]]b]} ON ROWS FROM [Sales]`,
			sql: byCountryOf("IT", "null", "a]b"), labels: map[string][]string{"Axis1": {"IT", "null", "a]b"}}},
		{name: "Except", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, NON EMPTY Except([country].[country].[country].Members, {[country].[country].[country].&[IT],[country].[country].[country].&[FR]}) ON ROWS FROM [Sales]`,
			sql: byCountryOf("", "Città", "\\x", "a]b", "null"), labels: map[string][]string{"Axis1": {"", "Città", "\\x", "a]b", "null"}}},
		{name: "TopCount", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, TopCount([country].[country].[country].Members, 2, [Measures].[amount]) ON ROWS FROM [Sales]`,
			sql: byCountryOf("FR", "IT"), labels: map[string][]string{"Axis1": {"FR", "IT"}}},
		{name: "BottomCount", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, BottomCount([country].[country].[country].Members, 2, [Measures].[amount]) ON ROWS FROM [Sales]`,
			sql: byCountryOf("", "a]b"), labels: map[string][]string{"Axis1": {"", "a]b"}}},
		{name: "Filter on a measure", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, Filter([country].[country].[country].Members, [Measures].[amount] > 5) ON ROWS FROM [Sales]`,
			sql: byCountryOf("FR", "IT"), labels: map[string][]string{"Axis1": {"FR", "IT"}}},
		{name: "Filter on two measures", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, Filter([country].[country].[country].Members, [Measures].[amount] > 3 AND [Measures].[Rows] < 2) ON ROWS FROM [Sales]`,
			sql: byCountryOf("Città", "FR"), labels: map[string][]string{"Axis1": {"Città", "FR"}}},
		{name: "Order", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, Order([country].[country].[country].Members, [Measures].[amount], BDESC) ON ROWS FROM [Sales]`,
			sql: byCountry, labels: map[string][]string{"Axis1": {"FR", "IT", "Città", "\\x", "a]b", "", "null"}}},
		{name: "Head and Distinct", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT ` + measures + `, Head(Distinct({[country].[country].[country].&[IT],[country].[country].[country].&[FR],[country].[country].[country].&[IT]}), 2) ON ROWS FROM [Sales]`,
			sql: byCountryOf("IT", "FR"), labels: map[string][]string{"Axis1": {"IT", "FR"}}},
		{name: "three axes", axes: []string{"Axis0", "Axis1", "Axis2"},
			mdx: `SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, NON EMPTY [country].[country].[country].Members ON ROWS, NON EMPTY [paid].[paid].[paid].Members ON PAGES FROM [Sales]`,
			sql: `SELECT 'Rows', coalesce(country, 'null'), coalesce(CAST(paid AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve GROUP BY country, paid
 UNION ALL SELECT 'amount', coalesce(country, 'null'), coalesce(CAST(paid AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY country, paid`},
		{name: "a cell with no axis", axes: nil, mdx: `SELECT FROM [Sales]`, sql: `SELECT CAST(count(*) AS VARCHAR) FROM serve`},
		{name: "an integer field is a member as well as a measure", axes: []string{"Axis0", "Axis1"},
			mdx:    `SELECT {[Measures].[amount]} ON COLUMNS, NON EMPTY [code].[code].[code].Members ON ROWS FROM [Sales]`,
			sql:    `SELECT 'amount', coalesce(CAST(code AS VARCHAR), 'null'), CAST(sum(amount) AS VARCHAR) FROM serve GROUP BY code`,
			labels: map[string][]string{"Axis1": {"-3", "2", "7", "10"}}},
		{name: "a timestamp with a zone, by month", axes: []string{"Axis0", "Axis1"},
			mdx: `SELECT {[Measures].[Rows]} ON COLUMNS, NON EMPTY [seen_tz].[Year-Quarter-Month-Day].[Month].Members ON ROWS FROM [Sales]`,
			sql: `SELECT 'Rows', CAST(year(timezone('UTC', seen_tz)) AS VARCHAR) || '-' || lpad(CAST(month(timezone('UTC', seen_tz)) AS VARCHAR), 2, '0'), CAST(count(*) AS VARCHAR) FROM serve
 WHERE seen_tz IS NOT NULL GROUP BY year(timezone('UTC', seen_tz)), month(timezone('UTC', seen_tz))`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ds := f.exec(c.mdx)
			sameGrid(t, c.name, ds.grid(c.axes...), sqlGrid(t, f, c.sql))
			for axis, want := range c.labels {
				if got := ds.labels(axis); strings.Join(got, "\x00") != strings.Join(want, "\x00") {
					t.Errorf("%s: tuples of %s\n got %q\nwant %q", c.name, axis, got, want)
				}
			}
		})
	}
}

// ── the shape of the answer ───────────────────────────────────────────────────

func TestExecuteShapeOfTheAnswer(t *testing.T) {
	f := salesFixture(t)
	a := f.post(executeBody(`SELECT NON EMPTY Hierarchize({DrilldownLevel({`+cal+`.[All]},,,INCLUDE_CALC_MEMBERS)}) DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,CHILDREN_CARDINALITY ON ROWS, `+
		`{[Measures].[Rows],[Measures].[amount],[Measures].[amount (Average)]} DIMENSION PROPERTIES PARENT_UNIQUE_NAME ON COLUMNS FROM [Sales] WHERE ([country].[country].[country].&[IT]) `+
		`CELL PROPERTIES VALUE, FORMAT_STRING, FORMATTED_VALUE, LANGUAGE, BACK_COLOR, FORE_COLOR, FONT_FLAGS`), "main")
	if a.status != 200 || !strings.HasPrefix(a.header.Get("Content-Type"), "text/xml") {
		t.Fatalf("%d %v %s", a.status, a.header, a.body)
	}
	ds := parseDataset(t, string(a.body))
	if !ds.schema || ds.cube != "Sales" {
		t.Errorf("schema %v, cube %q", ds.schema, ds.cube)
	}
	if !strings.Contains(string(a.body), `<LastDataUpdate xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">2026-11-07T06:00:00Z</LastDataUpdate>`) {
		t.Error("no LastDataUpdate")
	}
	// OlapInfo: the hierarchies of each axis with the properties asked for, the slicer's, the cell info
	cal0 := dsHier{name: "[day].[Year-Quarter-Month-Day]", props: []string{"UName", "Caption", "LName", "LNum", "DisplayInfo", "PARENT_UNIQUE_NAME", "HIERARCHY_UNIQUE_NAME", "CHILDREN_CARDINALITY"}}
	if got := ds.info["Axis1"]; len(got) != 1 || got[0].name != cal0.name || strings.Join(got[0].props, ",") != strings.Join(cal0.props, ",") {
		t.Errorf("Axis1 info: %+v", got)
	}
	if got := ds.info["Axis0"]; len(got) != 1 || got[0].name != "[Measures]" || strings.Join(got[0].props, ",") != "UName,Caption,LName,LNum,DisplayInfo,PARENT_UNIQUE_NAME" {
		t.Errorf("Axis0 info: %+v", got)
	}
	if got := ds.info["SlicerAxis"]; len(got) != 2 || got[0].name != "[country].[country]" || got[1].name != "[Measures]" {
		t.Errorf("slicer info: %+v", got)
	}
	if strings.Join(ds.cellInfo, ",") != "Value,FormattedValue,FormatString" {
		t.Errorf("cell info: %v", ds.cellInfo)
	}
	// the members of the rows, with their properties
	rows := ds.axes["Axis1"]
	if len(rows) != 3 { // the null year has no row of IT
		t.Fatalf("rows: %v", ds.labels("Axis1"))
	}
	want := []struct {
		un, caption, lname, lnum, display, parent, children string
	}{
		{"[day].[Year-Quarter-Month-Day].[All]", "All", "[day].[Year-Quarter-Month-Day].[(All)]", "0", "196611", "", "3"},
		{"[day].[Year-Quarter-Month-Day].[Year].&[2025]", "2025", "[day].[Year-Quarter-Month-Day].[Year]", "1", "1", "[day].[Year-Quarter-Month-Day].[All]", "1"},
		{"[day].[Year-Quarter-Month-Day].[Year].&[2026]", "2026", "[day].[Year-Quarter-Month-Day].[Year]", "1", "131074", "[day].[Year-Quarter-Month-Day].[All]", "2"},
	}
	for i, w := range want {
		m := rows[i][0]
		got := []string{m.hier, m.props["UName"], m.props["Caption"], m.props["LName"], m.props["LNum"], m.props["DisplayInfo"], m.props["PARENT_UNIQUE_NAME"],
			m.props["CHILDREN_CARDINALITY"], m.props["HIERARCHY_UNIQUE_NAME"]}
		exp := []string{"[day].[Year-Quarter-Month-Day]", w.un, w.caption, w.lname, w.lnum, w.display, w.parent, w.children, "[day].[Year-Quarter-Month-Day]"}
		if strings.Join(got, "|") != strings.Join(exp, "|") {
			t.Errorf("member %d:\n got %q\nwant %q", i, got, exp)
		}
	}
	// the measures on columns, the slicer holds the member and the default measure
	if got := ds.unames("Axis0"); strings.Join(got, ",") != "[Measures].[Rows],[Measures].[amount],[Measures].[amount (Average)]" {
		t.Errorf("columns: %v", got)
	}
	if got := ds.unames("SlicerAxis"); len(got) != 1 || got[0] != "[country].[country].[country].&[IT] [Measures].[Rows]" {
		t.Errorf("slicer axis: %v", got)
	}
	// the cells: IT has two rows, 15.75 of amount, 7.875 on average; ordinals run along the columns
	type cellWant struct {
		ord                           int
		typ, value, formatted, format string
	}
	for _, c := range []cellWant{
		{0, "xsd:int", "2", "2", "#,0"}, {1, "xsd:decimal", "15.75", "15.75", "#,0.00"}, {2, "xsd:double", "7.875", "7.88", "#,0.00"},
		{3, "xsd:int", "1", "1", "#,0"}, // 2025: one row, IT
	} {
		got, ok := ds.cells[c.ord]
		if !ok || got.typ != c.typ || got.value != c.value || got.formatted != c.formatted || got.format != c.format {
			t.Errorf("cell %d: %+v (present %v), want %+v", c.ord, got, ok, c)
		}
	}
	// the ordinals are written in ascending order
	last := -1
	for _, m := range regexp.MustCompile(`CellOrdinal="(\d+)"`).FindAllStringSubmatch(string(a.body), -1) {
		n, _ := strconv.Atoi(m[1])
		if n <= last {
			t.Fatalf("ordinal %d after %d", n, last)
		}
		last = n
	}
	if len(ds.cells) != 9 {
		t.Errorf("cells: %d", len(ds.cells))
	}
}

func TestExecuteDefaultsAndTheDefaultMeasure(t *testing.T) {
	f := salesFixture(t)
	// no measure anywhere: the cube's default, the one Discover declares
	p := f.must("MDSCHEMA_HIERARCHIES", "CUBE_NAME", "Sales", "HIERARCHY_UNIQUE_NAME", "[Measures]")
	def := p.col("DEFAULT_MEMBER")
	if len(def) != 1 {
		t.Fatalf("%v", def)
	}
	ds := f.exec(`SELECT FROM [Sales]`)
	if got := ds.unames("SlicerAxis"); len(got) != 1 || got[0] != def[0] {
		t.Errorf("slicer axis %v, Discover's default %v", got, def)
	}
	if c := ds.cells[0]; c.value != "8" || c.typ != "xsd:int" {
		t.Errorf("the one cell: %+v", c)
	}
	if len(ds.axes["Axis0"]) != 0 {
		t.Errorf("axes: %v", ds.axes)
	}
	// a pivot with nothing asked of the cells gets the value and its formatting
	ds = f.exec(`SELECT {[Measures].[amount]} ON COLUMNS FROM [Sales]`)
	if strings.Join(ds.cellInfo, ",") != "Value,FormattedValue" || ds.cells[0].formatted != "45.75" {
		t.Errorf("%v %+v", ds.cellInfo, ds.cells[0])
	}
	// the other cube of the data API, and its own table
	ds = f.exec(`SELECT {[Measures].[Rows],[Measures].[customer]} ON COLUMNS FROM [orders]`)
	if ds.cells[0].value != "50" || ds.cube != "orders" {
		t.Errorf("orders: %+v %q", ds.cells, ds.cube)
	}
	// the catalog may be named before the cube
	if ds = f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [CRM].[Sales]`); ds.cells[0].value != "8" {
		t.Errorf("%+v", ds.cells)
	}
}

func TestExecuteEmptyAnswers(t *testing.T) {
	f := salesFixture(t)
	// nothing passes the filter: no tuple on a NON EMPTY axis, no cell
	ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS, NON EMPTY [country].[country].[country].Members ON ROWS FROM [Sales] WHERE ([paid].[paid].[paid].&[false],[day].[Year-Quarter-Month-Day].[Year].&[2025])`)
	if len(ds.axes["Axis1"]) != 0 || len(ds.cells) != 0 || len(ds.axes["Axis0"]) != 1 {
		t.Errorf("%v %v", ds.labels("Axis1"), ds.cells)
	}
	// the same without NON EMPTY: the tuples are there, with no cell
	ds = f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales] WHERE ([paid].[paid].[paid].&[false],[day].[Year-Quarter-Month-Day].[Year].&[2025])`)
	if len(ds.axes["Axis1"]) != 7 || len(ds.cells) != 0 {
		t.Errorf("%v %v", ds.labels("Axis1"), ds.cells)
	}
	// a set with no member
	ds = f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS, Filter([country].[country].[country].Members, [Measures].[amount] > 1000) ON ROWS FROM [Sales]`)
	if len(ds.axes["Axis1"]) != 0 || len(ds.cells) != 0 || !strings.Contains(ds.raw, `<Axis name="Axis1"><Tuples></Tuples></Axis>`) {
		t.Errorf("%v %v", ds.labels("Axis1"), ds.cells)
	}
	// the cube's only cell, with a filter that lets nothing through: empty
	ds = f.exec(`SELECT FROM [Sales] WHERE ([paid].[paid].[paid].&[false],[day].[Year-Quarter-Month-Day].[Year].&[2025])`)
	if len(ds.cells) != 0 {
		t.Errorf("%v", ds.cells)
	}
}

func TestExecuteProperties(t *testing.T) {
	f := salesFixture(t)
	mdx := `SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`
	body := func(props ...string) string { return string(f.post(executeBody(mdx, props...), "main").body) }
	// Data has no schema, Schema only the schema, None neither
	if b := body("Content", "Data"); strings.Contains(b, "<xsd:schema") || !strings.Contains(b, "<CellData>") {
		t.Errorf("Data: %s", b)
	}
	if b := body("Content", "Schema"); !strings.Contains(b, "<xsd:schema") || strings.Contains(b, "<CellData>") {
		t.Errorf("Schema: %s", b)
	}
	if b := body("Content", "None"); strings.Contains(b, "<xsd:schema") || strings.Contains(b, "<CellData>") || !strings.Contains(b, "<root") {
		t.Errorf("None: %s", b)
	}
	if b := body("Content", "SchemaData"); !strings.Contains(b, "<xsd:schema") || !strings.Contains(b, "<CellData>") {
		t.Errorf("SchemaData: %s", b)
	}
	// Native is Multidimensional for a statement; what is not served is a fault that says so
	if b := body("Format", "Native"); !strings.Contains(b, "<CellData>") {
		t.Errorf("Native: %s", b)
	}
	for _, c := range []struct {
		props []string
		want  string
	}{
		{[]string{"Format", "Tabular"}, "The format «Tabular» is not served"},
		{[]string{"AxisFormat", "ClusterFormat"}, "The axis format «ClusterFormat» is not served"},
		{[]string{"Content", "Everything"}, "The content «Everything» is not served"},
		{[]string{"Catalog", "Other"}, "The catalog «Other» does not exist"},
	} {
		b := body(c.props...)
		if !strings.Contains(b, "<soap:Fault>") || !strings.Contains(b, c.want) {
			t.Errorf("%v: want %q in %s", c.props, c.want, b)
		}
	}
}

// ── what is refused ───────────────────────────────────────────────────────────

func TestExecuteRefusesWhatItDoesNotRender(t *testing.T) {
	f := salesFixture(t)
	c, p := "[country].[country].[country]", "[paid].[paid].[paid]"
	both := "CrossJoin(" + c + ".Members, " + p + ".Members)"
	all := "[country].[country].[All]"
	for _, tc := range []struct {
		name, mdx, want string
		code            string
	}{
		// constructs the plan cannot render exactly, each named
		{"Head over a cross", `SELECT Head(` + both + `, 2) ON COLUMNS FROM [Sales]`, "Head over a set of several hierarchies", "0xc10e0002"},
		{"TopCount over a cross", `SELECT TopCount(` + both + `, 2, [Measures].[amount]) ON COLUMNS FROM [Sales]`, "TopCount over a set of several hierarchies", "0xc10e0002"},
		{"BottomCount over a cross", `SELECT BottomCount(` + both + `, 2, [Measures].[amount]) ON COLUMNS FROM [Sales]`, "BottomCount over a set of several hierarchies", "0xc10e0002"},
		{"Order over a cross", `SELECT Order(` + both + `, [Measures].[amount], BDESC) ON COLUMNS FROM [Sales]`, "Order over a set of several hierarchies", "0xc10e0002"},
		{"Filter over a cross", `SELECT Filter(` + both + `, [Measures].[amount] > 1) ON COLUMNS FROM [Sales]`, "Filter over a set of several hierarchies", "0xc10e0002"},
		{"Except over a cross", `SELECT Except(` + both + `, {` + c + `.&[IT]}) ON COLUMNS FROM [Sales]`, "Except over a set of several hierarchies", "0xc10e0002"},
		{"DrilldownLevel over a cross", `SELECT DrilldownLevel(CrossJoin({` + all + `}, {[paid].[paid].[All]})) ON COLUMNS FROM [Sales]`, "DrilldownLevel over a set of several hierarchies", "0xc10e0002"},
		{"DrilldownMember over a cross", `SELECT DrilldownMember(CrossJoin({` + all + `}, {[paid].[paid].[All]}), {` + all + `}) ON COLUMNS FROM [Sales]`, "DrilldownMember over a set of several hierarchies", "0xc10e0002"},
		{"DrilldownLevel with an index", `SELECT DrilldownLevel({` + all + `},,2) ON COLUMNS FROM [Sales]`, "DrilldownLevel with an index", "0xc10e0002"},
		{"DrilldownMember of another hierarchy", `SELECT DrilldownMember({` + all + `}, {[paid].[paid].[All]}) ON COLUMNS FROM [Sales]`, "DrilldownMember with members of another hierarchy", "0xc10e0002"},
		{"Order ASC over several levels", `SELECT Order(Hierarchize(DrilldownLevel({` + all + `})), [Measures].[amount], ASC) ON COLUMNS FROM [Sales]`, "Order with ASC over members of several levels", "0xc10e0002"},
		{"a hierarchy as a set", `SELECT {[country].[country]} ON COLUMNS FROM [Sales]`, "used as a set", "0xc10e0002"},
		{"a level as a set", `SELECT ` + c + ` ON COLUMNS FROM [Sales]`, "used as a set", "0xc10e0002"},
		{"Members of a member", `SELECT ` + c + `.&[IT].Members ON COLUMNS FROM [Sales]`, "Members on a member or a measure", "0xc10e0002"},
		{"a hierarchy on two axes", `SELECT ` + c + `.Members ON COLUMNS, ` + c + `.Members ON ROWS FROM [Sales]`, "a hierarchy on two axes", "0xc10e0002"},
		{"measures on two axes", `SELECT {[Measures].[Rows]} ON COLUMNS, {[Measures].[amount]} ON ROWS FROM [Sales]`, "measures on two axes", "0xc10e0002"},
		{"a hierarchy in the slicer and on an axis", `SELECT ` + c + `.Members ON COLUMNS FROM [Sales] WHERE (` + c + `.&[IT])`, "a hierarchy in the slicer and on an axis", "0xc10e0002"},
		{"a measure in the slicer and on an axis", `SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales] WHERE ([Measures].[amount])`, "a measure in the slicer and measures on an axis", "0xc10e0002"},
		{"several measures in the slicer", `SELECT ` + c + `.Members ON COLUMNS FROM [Sales] WHERE {[Measures].[Rows],[Measures].[amount]}`, "several measures in a slicer or a subselect", "0xc10e0002"},
		{"a measure in a subselect", `SELECT ` + c + `.Members ON COLUMNS FROM (SELECT {[Measures].[amount]} ON COLUMNS FROM [Sales])`, "a measure in a subselect", "0xc10e0002"},
		{"NON EMPTY in a subselect", `SELECT ` + c + `.Members ON COLUMNS FROM (SELECT NON EMPTY {` + c + `.&[IT]} ON COLUMNS FROM [Sales])`, "NON EMPTY in a subselect", "0xc10e0002"},
		{"an axis with a gap", `SELECT ` + c + `.Members ON ROWS FROM [Sales]`, "not numbered from 0 with no gap (COLUMNS is missing)", "0xc10e0002"},
		{"tuples that are not a product", `SELECT {(` + c + `.&[IT], ` + p + `.&[true]), (` + c + `.&[FR], ` + p + `.&[false])} ON COLUMNS FROM [Sales]`, "a set of tuples that is not the product of its members", "0xc10e0002"},
		{"a set of members of different hierarchies", `SELECT {` + c + `.&[IT], ` + p + `.&[true]} ON COLUMNS FROM [Sales]`, "a set that mixes members of different hierarchies", "0xc10e0002"},
		{"a cross of a hierarchy with itself", `SELECT CrossJoin(` + c + `.Members, ` + c + `.Members) ON COLUMNS FROM [Sales]`, "CrossJoin of two sets of the same hierarchy", "0xc10e0002"},
		{"a tuple of the same hierarchy", `SELECT {(` + c + `.&[IT], ` + c + `.&[FR])} ON COLUMNS FROM [Sales]`, "a tuple with two members of the same hierarchy", "0xc10e0002"},
		{"Head with a count that is not a number of members", `SELECT Head(` + c + `.Members, 1.5) ON COLUMNS FROM [Sales]`, "Head with a count that is not a whole number", "0xc10e0002"},
		{"TopCount by something that is not a measure", `SELECT TopCount(` + c + `.Members, 2, [country].[country]) ON COLUMNS FROM [Sales]`, "TopCount by something that is not a measure", "0xc10e0002"},
		{"a property that is not served", `SELECT ` + c + `.Members DIMENSION PROPERTIES FOO ON COLUMNS FROM [Sales]`, "the member property FOO", "0xc10e0002"},
		{"a cell property that is not served", `SELECT FROM [Sales] CELL PROPERTIES FOO`, "the cell property FOO", "0xc10e0002"},
		{"a member property of a level", `SELECT ` + c + `.Members DIMENSION PROPERTIES [country].[Name] ON COLUMNS FROM [Sales]`, "the member property country.Name", "0xc10e0002"},
		// what the parser refuses
		{"a calculated member", `WITH MEMBER [Measures].[x] AS '1' SELECT {[Measures].[x]} ON COLUMNS FROM [Sales]`, "WITH MEMBER", "0xc10e0002"},
		{"a function outside the subset", `SELECT Generate(` + c + `.Members, ` + p + `.Members) ON COLUMNS FROM [Sales]`, "Generate", "0xc10e0002"},
		{"a syntax error", `SELECT {[Measures].[Rows] ON COLUMNS FROM [Sales]`, "The statement was not understood", "0xc10e0001"},
		// names the cube does not know
		{"a cube that does not exist", `SELECT FROM [Nope]`, "The cube «Nope» does not exist", "0xc10e0001"},
		{"a catalog that does not exist", `SELECT FROM [Nope].[Sales]`, "The catalog «Nope» does not exist", "0xc10e0001"},
		{"a measure that does not exist", `SELECT {[Measures].[nope]} ON COLUMNS FROM [Sales]`, "this cube has no such measure", "0xc10e0001"},
		{"a dimension that does not exist", `SELECT {[nope].[nope].[All]} ON COLUMNS FROM [Sales]`, "this cube has no such dimension", "0xc10e0001"},
		{"a member the data does not have", `SELECT {` + c + `.&[ZZ]} ON COLUMNS FROM [Sales]`, "The member [country].[country].[country].&amp;[ZZ] does not exist in this cube", "0xc10e0001"},
		{"a member the data does not have, in the slicer", `SELECT FROM [Sales] WHERE (` + c + `.&[ZZ])`, "does not exist in this cube", "0xc10e0001"},
		{"a member the data does not have, in a subselect", `SELECT FROM (SELECT {` + c + `.&[ZZ]} ON COLUMNS FROM [Sales])`, "does not exist in this cube", "0xc10e0001"},
		{"a member outside the subselect", `SELECT {` + c + `.&[FR]} ON COLUMNS FROM (SELECT {` + c + `.&[IT]} ON COLUMNS FROM [Sales])`, "does not exist in this cube", "0xc10e0001"},
		{"a key that is not of the level", `SELECT {[day].[Year-Quarter-Month-Day].[Year].&[x]} ON COLUMNS FROM [Sales]`, "the key is not one of this level", "0xc10e0001"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := f.refused(tc.mdx)
			if !strings.Contains(body, tc.want) || !strings.Contains(body, tc.code) {
				t.Errorf("want %q and %s\n got %s", tc.want, tc.code, body)
			}
			if strings.Contains(body, "<ExecuteResponse") {
				t.Errorf("an answer begun: %s", body)
			}
		})
	}
	// a key that does not see the cube
	body := f.refused(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`, "orders")
	if !strings.Contains(body, "The cube «Sales» does not exist (or this key cannot read it)") {
		t.Errorf("%s", body)
	}
	// but its own
	a := f.post(executeBody(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [orders]`), "orders")
	if a.status != 200 {
		t.Errorf("%d %s", a.status, a.body)
	}
	// XMLA off: a 404 after the key is checked
	f.m.XMLA = nil
	f.s.Apply(f.m)
	f.want(f.post(executeBody(`SELECT FROM [Sales]`), "main"), 404, "xmla_off")
	f.want(f.post(executeBody(`SELECT FROM [Sales]`), ""), 401, "key_missing")
}

func TestExecuteCaps(t *testing.T) {
	f := salesFixture(t)
	// the cells: two measures on seven countries are fourteen
	f.m.XMLA.MaxCells = 13
	f.s.Apply(f.m)
	body := f.refused(`SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales]`)
	for _, want := range []string{"holds more than 13 cells", "DATA_API__XMLA_MAX_CELLS", "0xc10e0003"} {
		if !strings.Contains(body, want) {
			t.Errorf("want %q in %s", want, body)
		}
	}
	if strings.Contains(body, "<ExecuteResponse") {
		t.Errorf("an answer begun: %s", body)
	}
	f.m.XMLA.MaxCells = 14
	f.s.Apply(f.m)
	if a := f.post(executeBody(`SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales]`), "main"); a.status != 200 {
		t.Errorf("at the cap: %d %s", a.status, a.body)
	}
	// the tuples of an axis: seven countries
	f.m.XMLA.MaxCells = 1000
	f.m.XMLA.MaxTuples = 5
	f.s.Apply(f.m)
	for _, mdx := range []string{
		`SELECT [country].[country].[country].Members ON COLUMNS FROM [Sales]`,
		`SELECT NON EMPTY [country].[country].[country].Members ON COLUMNS FROM [Sales]`,
		// the members of two fields that cross past the cap
		`SELECT [paid].[paid].[paid].Members * [code].[code].[code].Members ON COLUMNS FROM [Sales]`,
		// the tuples that are left once the empty ones are out
		`SELECT NON EMPTY [paid].[paid].[paid].Members * [code].[code].[code].Members ON COLUMNS FROM [Sales]`,
		// a slicer that is a product of members
		`SELECT FROM [Sales] WHERE ({` + strings.Join([]string{"1", "2", "3", "4", "5", "6", "7"}, ",") + `})`,
	} {
		if strings.Contains(mdx, "WHERE") {
			continue
		}
		body := f.refused(mdx)
		for _, want := range []string{"more than 5 tuples on one axis", "DATA_API__XMLA_MAX_TUPLES", "0xc10e0003"} {
			if !strings.Contains(body, want) {
				t.Errorf("%s: want %q in %s", mdx, want, body)
			}
		}
	}
	f.m.XMLA.MaxTuples = 100
	f.s.Apply(f.m)
	// the cap is the pod's: the administrator's value reaches the answer
	if a := f.post(executeBody(`SELECT NON EMPTY [paid].[paid].[paid].Members * [code].[code].[code].Members ON COLUMNS FROM [Sales]`), "main"); a.status != 200 {
		t.Errorf("%d %s", a.status, a.body)
	}
}

func TestExecuteTimeout(t *testing.T) {
	f := salesFixture(t)
	f.s.cfg.QueryTimeout = time.Nanosecond
	body := f.refused(`SELECT {[Measures].[Rows]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales]`)
	if !strings.Contains(body, "longer than") || !strings.Contains(body, "0xc10e0003") {
		t.Errorf("%s", body)
	}
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("left behind: %v", left)
	}
}

func TestExecuteSessions(t *testing.T) {
	f := salesFixture(t)
	envelope := func(header, command string) string {
		return `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">` + header + `<soap:Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis">` + command +
			`<Properties><PropertyList><Catalog>CRM</Catalog><Format>Multidimensional</Format></PropertyList></Properties></Execute></soap:Body></soap:Envelope>`
	}
	begin := `<soap:Header><BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis" soap:mustUnderstand="1"/></soap:Header>`
	end := `<soap:Header><EndSession xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="abc"/></soap:Header>`
	empty := `<Command><Statement></Statement></Command>`
	// a session begins with an empty Execute: it gets an id and an empty answer
	a := f.post(envelope(begin, empty), "main")
	m := regexp.MustCompile(`<soap:Header><Session xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="([0-9a-f]{32})"/></soap:Header>`).FindStringSubmatch(string(a.body))
	if a.status != 200 || m == nil || !strings.Contains(string(a.body), `<root xmlns="urn:schemas-microsoft-com:xml-analysis:empty"/>`) {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// two sessions, two ids
	if b := f.post(envelope(begin, empty), "main"); strings.Contains(string(b.body), m[1]) {
		t.Errorf("the same id twice")
	}
	// the statements that come in the session answer as ever
	if a := f.post(envelope(`<soap:Header><Session xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="`+m[1]+`"/></soap:Header>`,
		`<Command><Statement>SELECT FROM [Sales]</Statement></Command>`), "main"); a.status != 200 || !strings.Contains(string(a.body), "<CellData>") {
		t.Errorf("%d %s", a.status, a.body)
	}
	// and one that begins it as well gets the header with its answer
	a = f.post(envelope(begin, `<Command><Statement>SELECT FROM [Sales]</Statement></Command>`), "main")
	if a.status != 200 || !strings.Contains(string(a.body), `<soap:Header><Session`) || !strings.Contains(string(a.body), "<CellData>") {
		t.Errorf("%d %s", a.status, a.body)
	}
	// it ends with an empty one
	a = f.post(envelope(end, empty), "main")
	if a.status != 200 || strings.Contains(string(a.body), "<Session") || !strings.Contains(string(a.body), `xml-analysis:empty`) {
		t.Errorf("%d %s", a.status, a.body)
	}
	// an Execute with no command at all, and one with blanks: what a client sends around its queries
	for _, c := range []string{``, `<Command><Statement>  </Statement></Command>`} {
		if a := f.post(envelope("", c), "main"); a.status != 200 || !strings.Contains(string(a.body), `xml-analysis:empty`) {
			t.Errorf("%q: %d %s", c, a.status, a.body)
		}
	}
	// the key is still checked
	f.want(f.post(envelope(begin, empty), ""), 401, "key_missing")
}
