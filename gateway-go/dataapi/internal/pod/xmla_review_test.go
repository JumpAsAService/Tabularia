package pod

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/duckdb/duckdb-go/v2"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// What an independent review of the XMLA interface found (docs/specs/xmla.md, «As built»): the
// caps that were not met in time, the places a statement waits for, what is kept on the disk and
// what is told to someone who has no key.

// ── the development switch of Basic over plain HTTP ───────────────────────────

func TestXMLAInsecureBasicSwitch(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	plain := basic("x", f.keys["main"])
	// off, as it is by default: a key in clear text over plain HTTP is refused
	f.want(f.post(discoverProperties, "", plain), 403, "https_required")
	if strings.Contains(f.logs.String(), "INSECURE_BASIC") {
		t.Error("a warning with the switch off")
	}
	// on: the manifest says so, and Basic is accepted over plain HTTP (and the other keys still work)
	f.m.XMLA.InsecureBasic = true
	f.s.Apply(f.m)
	if a := f.post(discoverProperties, "", plain); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	if a := f.post(discoverProperties, "main"); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// a wrong key is still a wrong key
	f.want(f.post(discoverProperties, "", basic("x", "tdk_nope")), 401, "key_invalid")
	// one warning, however many manifests come in
	f.s.Apply(f.m)
	f.s.Apply(f.m)
	if n := strings.Count(f.logs.String(), "DATA_API__XMLA_INSECURE_BASIC is on"); n != 1 {
		t.Errorf("%d warnings: %s", n, f.logs.String())
	}
	// switched off again: refused as ever
	f.m.XMLA.InsecureBasic = false
	f.s.Apply(f.m)
	f.want(f.post(discoverProperties, "", plain), 403, "https_required")
}

// ── the turn of the members, the places to write ─────────────────────────────

func fill(ch chan struct{}) func() {
	for i := 0; i < cap(ch); i++ {
		ch <- struct{}{}
	}
	return func() {
		for len(ch) > 0 {
			<-ch
		}
	}
}

// The members' turn is taken whatever the case of the rowset's name: a client could not go round
// the gate by writing it in lower case.
func TestDiscoverMembersTakeTheirTurnWhateverTheCaseOfTheName(t *testing.T) {
	f := salesFixture(t)
	free := fill(f.s.gate)
	defer free()
	f.s.cfg.QueryTimeout = 200 * time.Millisecond
	for _, name := range []string{"MDSCHEMA_MEMBERS", "mdschema_members", "MdSchema_Members"} {
		a := f.post(discoverBody(name, map[string]string{"CUBE_NAME": "Sales"}, "CUBE_NAME"), "main")
		if a.status != 500 || !strings.Contains(string(a.body), "waited longer than") || !strings.Contains(string(a.body), "its turn") {
			t.Errorf("%s: %d %s", name, a.status, a.body)
		}
	}
	// the rowsets that read nothing take no turn
	if a := f.post(discoverProperties, "main"); a.status != 200 {
		t.Errorf("%d %s", a.status, a.body)
	}
	free()
	if p := f.must("mdschema_members", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "[country].[country].[country]"); len(p.rows) != 7 {
		t.Errorf("%d rows", len(p.rows))
	}
}

// A few places only to compute and write an Execute answer: each holds a connection of the table's
// pool, and a slow client could take them all. The wait is within the timeout and is a clear fault.
func TestExecuteHasAFewPlacesToWrite(t *testing.T) {
	f := salesFixture(t)
	if cap(f.s.xmlaPlaces) >= 16 || cap(f.s.xmlaPlaces) < 1 {
		t.Fatalf("%d places: fewer than the pool's connections (16)", cap(f.s.xmlaPlaces))
	}
	free := fill(f.s.xmlaPlaces)
	defer free()
	f.s.cfg.QueryTimeout = 200 * time.Millisecond
	body := f.refused(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`)
	for _, want := range []string{"waited longer than", "a place to write its answer", "DATA_API__QUERY_SECONDS", "0xc10e0003"} {
		if !strings.Contains(body, want) {
			t.Errorf("want %q in %s", want, body)
		}
	}
	if !strings.Contains(f.logs.String(), `"code":"xmla_limit"`) {
		t.Error("the log does not say it was a limit")
	}
	// the rest of the interface is not held up: Discover reads, and the REST interface answers
	if p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "[country].[country].[country]"); len(p.rows) != 7 {
		t.Errorf("%d rows", len(p.rows))
	}
	if a := f.get("/data/crm/customers?_limit=1", "main"); a.status != 200 {
		t.Errorf("REST: %d %s", a.status, a.body)
	}
	// a place freed, and the statement is answered; and every answer gives its place back
	<-f.s.xmlaPlaces
	f.s.cfg.QueryTimeout = 10 * time.Second
	if a := f.post(executeBody(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`), "main"); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	if len(f.s.xmlaPlaces) != cap(f.s.xmlaPlaces)-1 {
		t.Errorf("places taken: %d", len(f.s.xmlaPlaces))
	}
	free()
	f.refused(`SELECT {[country].[country].[country].&[NOBODY]} ON COLUMNS FROM [Sales]`)
	if len(f.s.xmlaPlaces) != 0 {
		t.Errorf("a refused statement kept its place: %d", len(f.s.xmlaPlaces))
	}
}

// A client that went away while it waited is not a cap reached.
func TestAWaitThatEndsWithTheClientIsNotALimit(t *testing.T) {
	f := salesFixture(t)
	free := fill(f.s.gate)
	defer free()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	gone := func(r *http.Request) { *r = *r.WithContext(ctx) }
	f.post(executeBody(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`), "main", gone)
	f.post(discoverBody("MDSCHEMA_MEMBERS", map[string]string{"CUBE_NAME": "Sales"}, "CUBE_NAME"), "main", gone)
	logs := f.logs.String()
	if strings.Contains(logs, `"code":"xmla_limit"`) || strings.Count(logs, `"code":"xmla_cancelled"`) != 2 {
		t.Errorf("%s", logs)
	}
}

// ── bytes ─────────────────────────────────────────────────────────────────────

func TestDiscoverAnswerInBytesHasACap(t *testing.T) {
	f := salesFixture(t)
	f.m.XMLA.MaxMembers = 5000
	f.s.Apply(f.m)
	f.s.xmlaMaxBytes = 64 << 10
	// 1,000 members, each a row of a kilobyte: past the cap, and the cap is named with its value
	a := f.post(discoverBody("MDSCHEMA_MEMBERS", map[string]string{"CUBE_NAME": "customers", "LEVEL_UNIQUE_NAME": "[name].[name].[name]"}, "CUBE_NAME", "LEVEL_UNIQUE_NAME"), "main")
	if a.status != 500 || !strings.Contains(string(a.body), "larger than 65536 bytes") || strings.Contains(string(a.body), "<row>") {
		t.Fatalf("%d %.300s", a.status, a.body)
	}
	if !strings.Contains(f.logs.String(), `"code":"xmla_limit"`) {
		t.Error("the log does not say it was a limit")
	}
	// a small one is answered under the same cap
	if p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Sales", "LEVEL_UNIQUE_NAME", "[country].[country].[country]"); len(p.rows) != 7 {
		t.Errorf("%d rows", len(p.rows))
	}
}

func TestPlanningKeysInBytesHaveACap(t *testing.T) {
	f := salesFixture(t)
	const mdx = `SELECT [country].[country].[country].Members ON COLUMNS FROM [Sales]`
	_, rec, err := directWith(t, f, context.Background(), mdx, nil, xmla.ExecLimits{MaxCells: 1000, MaxTuples: 100, MaxKeyBytes: 5})
	rec.release()
	var fault *xmla.Fault
	if err == nil || !asFault(err, &fault) || fault.Code != xmla.CodeLimit || !strings.Contains(fault.Message, "more than 5 bytes of keys") {
		t.Fatalf("%v", err)
	}
	r, rec, err := directWith(t, f, context.Background(), mdx, nil, xmla.ExecLimits{MaxCells: 1000, MaxTuples: 100, MaxKeyBytes: 5000})
	if err != nil {
		t.Fatal(err)
	}
	r.Close()
	rec.release()
}

func asFault(err error, f **xmla.Fault) bool {
	x, ok := err.(*xmla.Fault)
	*f = x
	return ok
}

// ── sets that add up, levels read whole ──────────────────────────────────────

// scans are the queries of the planning that read members (not the creation of the tables).
func scans(rec *recorder) []string {
	var out []string
	for _, q := range rec.texts {
		if strings.Contains(q, "GROUP BY") && !strings.HasPrefix(q, "CREATE") {
			out = append(out, q)
		}
	}
	return out
}

func TestASetCapIsOnTheSumOfItsElementsAndStopsAtOnce(t *testing.T) {
	f := salesFixture(t)
	lim := xmla.ExecLimits{MaxCells: 1000, MaxTuples: 8}
	code := `[code].[code].[code].Members`
	// four elements of five members: the second makes ten, and the others are never read
	_, rec, err := directWith(t, f, context.Background(), `SELECT {`+code+`, `+code+`, `+code+`, `+code+`} ON COLUMNS FROM [Sales]`, nil, lim)
	rec.release()
	var fault *xmla.Fault
	if err == nil || !asFault(err, &fault) || fault.Code != xmla.CodeLimit || !strings.Contains(fault.Message, "more than 8 tuples") {
		t.Fatalf("%v", err)
	}
	if n := len(scans(rec)); n != 2 {
		t.Errorf("%d members read: the set kept on after its cap", n)
	}
	// the same for the arguments of a CrossJoin
	lim.MaxTuples = 7
	_, rec, err = directWith(t, f, context.Background(), `SELECT CrossJoin([paid].[paid].[paid].Members, `+code+`, [country].[country].[country].Members) ON COLUMNS FROM [Sales]`, nil, lim)
	rec.release()
	if err == nil || !asFault(err, &fault) || !strings.Contains(fault.Message, "more than 7 tuples") {
		t.Fatalf("%v", err)
	}
	if n := len(scans(rec)); n != 2 {
		t.Errorf("%d members read: the CrossJoin kept on after its cap", n)
	}
}

// A drilldown from All is what a pivot sends when a field is dragged: the members read are never
// more than the cap of tuples (and one), not a million.
func TestDrilldownLevelReadsNoMoreThanTheCapOfTuples(t *testing.T) {
	f := salesFixture(t)
	_, rec, err := directWith(t, f, context.Background(), `SELECT DrilldownLevel({[country].[country].[All]}) ON COLUMNS FROM [Sales]`, nil,
		xmla.ExecLimits{MaxCells: 1000, MaxTuples: 5})
	rec.release()
	var fault *xmla.Fault
	if err == nil || !asFault(err, &fault) || !strings.Contains(fault.Message, "more than 5 tuples") {
		t.Fatalf("%v", err)
	}
	all := scans(rec)
	if len(all) == 0 {
		t.Fatal("nothing read")
	}
	for _, q := range all {
		if !strings.Contains(q, "LIMIT 6") {
			t.Errorf("a read with another limit than the cap and one: %s", q)
		}
	}
}

// A filter of more than a batch of members is checked a batch at a time: a query never names more
// than a batch, and no query reads the whole level.
func TestAFilterOfManyMembersIsReadInBatches(t *testing.T) {
	f := salesFixture(t)
	tb := f.s.table("customers")
	defer tb.release()
	rows, err := tb.db.Query(`SELECT name FROM serve ORDER BY id LIMIT 25`)
	if err != nil {
		t.Fatal(err)
	}
	var members []string
	for rows.Next() {
		var n string
		_ = rows.Scan(&n)
		members = append(members, "[name].[name].[name].&["+n+"]")
	}
	rows.Close()
	mdx := `SELECT {[Measures].[Rows]} ON COLUMNS FROM (SELECT {` + strings.Join(members, ",") + `} ON COLUMNS FROM [customers])`
	r, rec, err := directOn(t, f, "customers", context.Background(), mdx, xmla.ExecLimits{MaxCells: 1000, MaxTuples: 100, MemberBatch: 10})
	if err != nil {
		t.Fatal(err)
	}
	var out strings.Builder
	if err := r.Write(context.Background(), &out, ""); err != nil {
		t.Fatal(err)
	}
	r.Close()
	rec.release()
	ds := parseDataset(t, out.String())
	if c := ds.cells[0]; c.value != "25" {
		t.Errorf("the rows of the 25 members: %+v", ds.cells)
	}
	reads := 0
	for i, q := range rec.texts {
		if !strings.Contains(q, "GROUP BY") || strings.HasPrefix(q, "CREATE") || strings.Contains(q, "GROUPING") {
			continue
		}
		reads++
		if !strings.Contains(q, " WHERE ") || len(rec.args[i]) > 10 {
			t.Errorf("a read that names %d members or none: %s", len(rec.args[i]), q)
		}
	}
	if reads != 3 {
		t.Errorf("%d reads of members, want 3 batches (10, 10, 5)", reads)
	}
}

// What TopCount, Order and Filter read is the values of the members they are given: the rows of
// the others are not aggregated.
func TestTopCountAggregatesOnlyItsMembers(t *testing.T) {
	f := salesFixture(t)
	r, rec, err := direct(t, f, context.Background(), `SELECT {[Measures].[amount]} ON COLUMNS, TopCount({[country].[country].[country].&[IT],[country].[country].[country].&[FR]}, 1, [Measures].[amount]) ON ROWS FROM [Sales]`, nil)
	if err != nil {
		t.Fatal(err)
	}
	var out strings.Builder
	_ = r.Write(context.Background(), &out, "")
	r.Close()
	rec.release()
	ds := parseDataset(t, out.String())
	if got := ds.labels("Axis1"); len(got) != 1 || got[0] != "FR" {
		t.Errorf("%v", got)
	}
	found := false
	for i, q := range rec.texts {
		if strings.Contains(q, "GROUPING") && !strings.HasPrefix(q, "CREATE") {
			found = true
			if !strings.Contains(q, " WHERE ") || len(rec.args[i]) != 2 || fmt.Sprint(rec.args[i]) != "[IT FR]" {
				t.Errorf("the values of the members: %s %v", q, rec.args[i])
			}
		}
	}
	if !found {
		t.Error("no read of the values")
	}
}

// ── what the aggregation reads ───────────────────────────────────────────────

func aggregation(rec *recorder) (string, []any) {
	for i, q := range rec.texts {
		if strings.HasPrefix(q, "CREATE") && strings.Contains(q, "_agg AS") {
			return q, rec.args[i]
		}
	}
	return "", nil
}

func TestExecuteReadsOnlyTheRowsOfTheMembersOnItsAxes(t *testing.T) {
	f := salesFixture(t)
	run := func(mdx string) (*dataset, string, []any) {
		t.Helper()
		r, rec, err := direct(t, f, context.Background(), mdx, nil)
		if err != nil {
			t.Fatal(err)
		}
		var out strings.Builder
		if err := r.Write(context.Background(), &out, ""); err != nil {
			t.Fatal(err)
		}
		r.Close()
		rec.release()
		q, args := aggregation(rec)
		return parseDataset(t, out.String()), q, args
	}
	// a customer by a product: the rows of the two members, not of every pair
	ds, q, args := run(`SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, {[country].[country].[country].&[IT]} * {[paid].[paid].[paid].&[true]} ON ROWS FROM [Sales]`)
	if !strings.Contains(q, " WHERE ") || fmt.Sprint(args) != "[IT true]" {
		t.Errorf("the aggregation reads the whole table: %s %v", q, args)
	}
	sameGrid(t, "the rows of two members", ds.grid("Axis0", "Axis1"), map[string]string{"Rows|IT/true": "2", "amount|IT/true": "15.75"})
	// All on the axis: the total of the table, not of the members listed (and no condition on it)
	ds, q, args = run(`SELECT {[Measures].[Rows]} ON COLUMNS, {[country].[country].[All], [country].[country].[country].&[IT]} ON ROWS FROM [Sales]`)
	if strings.Contains(q, " WHERE ") || len(args) != 0 {
		t.Errorf("a part with the All member is narrowed: %s %v", q, args)
	}
	sameGrid(t, "All with a member", ds.grid("Axis0", "Axis1"), map[string]string{"Rows|All": "8", "Rows|IT": "2"})
	// one part narrowed and one with All: only the first is narrowed, and its total is the right one
	ds, q, args = run(`SELECT {[Measures].[Rows]} ON COLUMNS, {[country].[country].[country].&[IT]} * {[paid].[paid].[All], [paid].[paid].[paid].&[true]} ON ROWS FROM [Sales]`)
	if fmt.Sprint(args) != "[IT]" {
		t.Errorf("%s %v", q, args)
	}
	sameGrid(t, "a member and a total", ds.grid("Axis0", "Axis1"), map[string]string{"Rows|IT/All": "2", "Rows|IT/true": "2"})
	// a calendar part, with members of two levels
	ds, q, args = run(`SELECT {[Measures].[Rows]} ON COLUMNS, {[day].[Year-Quarter-Month-Day].[Year].&[2025], [day].[Year-Quarter-Month-Day].[Quarter].&[2026-Q1]} ON ROWS FROM [Sales]`)
	if !strings.Contains(q, " WHERE ") {
		t.Errorf("%s %v", q, args)
	}
	sameGrid(t, "calendar", ds.grid("Axis0", "Axis1"), map[string]string{"Rows|2025": "1", "Rows|2026-Q1": "2"})
}

// The cells are counted before any row is read when no axis drops its empty tuples.
func TestExecuteMeetsTheCapOfCellsBeforeAggregating(t *testing.T) {
	f := salesFixture(t)
	_, rec, err := directWith(t, f, context.Background(), `SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales]`, nil,
		xmla.ExecLimits{MaxCells: 13, MaxTuples: 100})
	rec.release()
	var fault *xmla.Fault
	if err == nil || !asFault(err, &fault) || !strings.Contains(fault.Message, "more than 13 cells") {
		t.Fatalf("%v", err)
	}
	for _, q := range rec.texts {
		if strings.Contains(q, "_agg") {
			t.Errorf("an aggregation was made before the cap was met: %s", q)
		}
	}
}

// Nine levels crossed make a grouping set for each combination: past the cap the fault names it.
func TestTooManyGroupingSetsAreAFaultThatNamesTheCap(t *testing.T) {
	f := salesFixture(t)
	drill := func(h string, n int) string {
		s := `{[` + h + `].[Year-Quarter-Month-Day].[All]}`
		for i := 0; i < n; i++ {
			s = `DrilldownLevel(` + s + `)`
		}
		return s
	}
	cross := func(n int) string {
		return `SELECT {[Measures].[Rows]} ON COLUMNS, NON EMPTY CrossJoin(` + drill("day", n) + `, ` + drill("seen", n) + `, ` + drill("seen_tz", n) + `) ON ROWS FROM [Sales]`
	}
	// 5 levels each: 125 sets
	body := f.refused(cross(4))
	for _, want := range []string{"more than 64 grouping sets", "0xc10e0003"} {
		if !strings.Contains(body, want) {
			t.Errorf("want %q in %s", want, body)
		}
	}
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("left behind: %v", left)
	}
	// 3 levels each: 27 sets, under the cap, are answered (the cap itself is tried in the xmla package:
	// this test's table has 30 MB of memory, and 64 hash tables of an aggregation do not fit in it)
	if a := f.post(executeBody(cross(2)), "main"); a.status != 200 {
		t.Errorf("under the cap: %d %.300s %s", a.status, a.body, f.logs.String())
	}
}

// A temporary table is DuckDB's own: held in its memory and spilled to the temp directory that sits
// by the pod's data file, in the pod's data directory, past the memory limit.
func TestTemporaryTablesSpillToTheDataDirectory(t *testing.T) {
	f := salesFixture(t)
	tb := f.s.table("sales")
	defer tb.release()
	ctx := context.Background()
	c, err := tb.conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var dir, limit string
	if err := c.QueryRowContext(ctx, "SELECT current_setting('temp_directory')").Scan(&dir); err != nil {
		t.Fatal(err)
	}
	if err := c.QueryRowContext(ctx, "SELECT current_setting('memory_limit')").Scan(&limit); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(dir, filepath.Join(f.dir, "data")+string(filepath.Separator)) {
		t.Errorf("the temp directory %q is not in the pod's data directory", dir)
	}
	// well past the memory limit of the table (tens of MB)
	if _, err := c.ExecContext(ctx, `CREATE TEMP TABLE big AS SELECT i, repeat('x', 100) || i AS s FROM range(0, 1500000) t(i)`); err != nil {
		t.Fatalf("a temporary table past the memory limit %s: %v", limit, err)
	}
	var n int
	if err := c.QueryRowContext(ctx, `SELECT count(*) FROM big`).Scan(&n); err != nil || n != 1500000 {
		t.Errorf("%d %v", n, err)
	}
	_, _ = c.ExecContext(ctx, `DROP TABLE big`)
}

// ── a panic, the engine's errors, the content that holds no data ─────────────

func TestAPanicInExecuteDropsTheTables(t *testing.T) {
	f := salesFixture(t)
	const mdx = `SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, NON EMPTY Hierarchize(DrilldownLevel({[country].[country].[All]})) ON ROWS FROM [Sales]`
	for n := 1; n <= 6; n++ {
		r, rec, err := direct(t, f, context.Background(), mdx, func(rec *recorder) {
			rec.after = func(i int) {
				if i == n {
					panic("something nobody foresaw: Città")
				}
			}
		})
		var fault *xmla.Fault
		if err == nil || !asFault(err, &fault) || fault.Code != xmla.CodeInternal || fault.Class != "panic:string" || strings.Contains(fault.Message, "Città") {
			t.Fatalf("panic after %d statements: %v %+v", n, err, r)
		}
		rec.release()
		if left := leftTables(t, f); len(left) != 0 {
			t.Errorf("panic after %d statements left %v", n, left)
		}
	}
}

func TestEngineClassIsTheKindAndNotTheText(t *testing.T) {
	err := fmt.Errorf("wrapped: %w", &duckdb.Error{Type: duckdb.ErrorTypeOutOfMemory, Msg: "Out of Memory Error on 'Città di Roma'"})
	if got := engineClass(err); got != fmt.Sprintf("duckdb:%d", duckdb.ErrorTypeOutOfMemory) || strings.Contains(got, "Città") {
		t.Errorf("%q", got)
	}
	if got := engineClass(fmt.Errorf("plain")); got != "" {
		t.Errorf("%q", got)
	}
	// and it is the class that reaches the log of a fault that is ours to tell
	f := newFixture(t)
	f.withXMLA()
	r := httptest.NewRequest(http.MethodPost, "/data/crm/xmla", nil)
	f.s.noteFault(r, f.s.snap.Load(), &xmla.Request{Statement: "SELECT 1"}, &xmla.Fault{Code: xmla.CodeInternal, Message: "The statement could not be computed: try again", Class: "duckdb:12"})
	if !strings.Contains(f.logs.String(), `"class":"duckdb:12"`) {
		t.Errorf("%s", f.logs.String())
	}
}

func TestContentWithNoDataComputesNothing(t *testing.T) {
	f := salesFixture(t)
	for _, content := range []string{"Schema", "None"} {
		r, rec, err := directWith(t, f, context.Background(), `SELECT {[Measures].[Rows]} ON COLUMNS, [country].[country].[country].Members ON ROWS FROM [Sales]`, nil,
			xmla.ExecLimits{MaxCells: 1000, MaxTuples: 100}, "Format", "Multidimensional", "Content", content)
		if err != nil {
			t.Fatal(err)
		}
		if rec.conn != nil || len(rec.texts) != 0 {
			t.Errorf("%s: a connection taken or a query made: %v", content, rec.texts)
		}
		var out strings.Builder
		if err := r.Write(context.Background(), &out, ""); err != nil || strings.Contains(out.String(), "<CellData>") || strings.Contains(out.String(), "<Axes>") {
			t.Errorf("%s: %v %s", content, err, out.String())
		}
		if content == "Schema" && !strings.Contains(out.String(), "xsd:schema") {
			t.Errorf("no schema: %s", out.String())
		}
		r.Close()
		rec.release()
	}
	// a statement that does not parse is still refused, whatever the content
	if _, rec, err := directWith(t, f, context.Background(), `SELECT {`, nil, xmla.ExecLimits{}, "Content", "None"); err == nil {
		t.Error("a bad statement was answered")
	} else {
		rec.release()
	}
	// over HTTP
	a := f.post(executeBody(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`, "Format", "Multidimensional", "Content", "None"), "main")
	if a.status != 200 || strings.Contains(string(a.body), "<CellData>") {
		t.Errorf("%d %s", a.status, a.body)
	}
}

// ── what is kept on the disk ─────────────────────────────────────────────────

func TestRefusedStatementsLeaveTheDiskWhenTheirDayIsOverAndWhenTheSwitchIsOff(t *testing.T) {
	dir := t.TempDir()
	st := &refusedStore{path: filepath.Join(dir, refusedFile)}
	t0 := time.Date(2026, 11, 25, 9, 0, 0, 0, time.UTC)
	onDisk := func() int {
		raw, err := os.ReadFile(st.path)
		if err != nil {
			return 0
		}
		var all []refusedEntry
		_ = json.Unmarshal(raw, &all)
		return len(all)
	}
	st.add(t0, "SELECT 1", "why")
	st.add(t0.Add(time.Hour), "SELECT 2", "why")
	if onDisk() != 2 {
		t.Fatalf("%d", onDisk())
	}
	// a write takes the old ones off the disk: both are more than a day old
	st.add(t0.Add(25*time.Hour+time.Minute), "SELECT 3", "why")
	if raw, _ := os.ReadFile(st.path); onDisk() != 1 || strings.Contains(string(raw), "SELECT 1") || strings.Contains(string(raw), "SELECT 2") {
		t.Errorf("after a write: %d on the disk: %s", onDisk(), raw)
	}
	st.add(t0.Add(26*time.Hour), "SELECT 4", "why")
	if onDisk() != 2 {
		t.Errorf("%d on the disk", onDisk())
	}
	// a read takes them off too, and the last to go takes the file with it
	if got := st.list(t0.Add(49*time.Hour + 30*time.Minute)); len(got) != 1 || got[0].Statement != "SELECT 4" {
		t.Fatalf("%+v", got)
	}
	if raw, _ := os.ReadFile(st.path); strings.Contains(string(raw), "SELECT 3") {
		t.Errorf("an old statement is still in the file after a read: %s", raw)
	}
	if got := st.list(t0.Add(51 * time.Hour)); len(got) != 0 {
		t.Fatalf("%+v", got)
	}
	if _, err := os.Stat(st.path); err == nil {
		t.Error("an empty file is left")
	}
	// the switch turned off: the file goes, with what a half-made write may have left
	f := salesFixture(t)
	file := filepath.Join(f.dir, "data", refusedFile)
	f.m.XMLA.KeepRefused = true
	f.s.Apply(f.m)
	f.refused(`SELECT {[country].[country].[country].&[NOBODY]} ON COLUMNS FROM [Sales]`)
	if _, err := os.Stat(file); err != nil {
		t.Fatalf("nothing kept with the switch on: %v", err)
	}
	f.s.Apply(f.m)
	if _, err := os.Stat(file); err != nil {
		t.Errorf("a manifest that keeps them took the file: %v", err)
	}
	f.m.XMLA.KeepRefused = false
	f.s.Apply(f.m)
	if _, err := os.Stat(file); err == nil {
		t.Error("the file is left with the switch off")
	}
	// and the data API with XMLA off altogether
	f.m.XMLA.KeepRefused = true
	f.s.Apply(f.m)
	f.refused(`SELECT {[country].[country].[country].&[NOBODY]} ON COLUMNS FROM [Sales]`)
	f.m.XMLA = nil
	f.s.Apply(f.m)
	if _, err := os.Stat(file); err == nil {
		t.Error("the file is left with XMLA off")
	}
}

// ── what the model and the pod agree on ──────────────────────────────────────

// The columns the cube is made of in the pod are those of the data the datasource's types give:
// the model's kind of every DuckDB type a datasource can have is the REST interface's.
func TestTheCubeAndTheRESTInterfaceReadAColumnAlike(t *testing.T) {
	for _, typ := range []string{"TINYINT", "BIGINT", "UBIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL(12,2)", "BOOLEAN", "VARCHAR", "DATE", "TIMESTAMP",
		"TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS", "TIMESTAMP WITH TIME ZONE", "TIME", "TIME_NS", "UUID", "BLOB", "BIGINT[]", "STRUCT(a BIGINT)", "INTERVAL", "JSON", "ENUM('a', 'b')"} {
		col := NewColumn("c", typ)
		cube := cubeOf(&dataapiwire.Resource{Name: "r"}, &Table{Columns: []Column{col}})
		inCube := len(cube.Dimensions) > 0 || len(cube.Measures) > 1
		if inCube != (col.Kind != KindOther) {
			t.Errorf("%s: kind %s, in the cube %v", typ, col.Kind, inCube)
		}
	}
}

// ── hierarchies and filters together ─────────────────────────────────────────

func TestTwoHierarchiesOfOneDateOnDifferentAxes(t *testing.T) {
	f := salesFixture(t)
	ds := f.exec(`SELECT {[day].[Year-Quarter-Month-Day].[Year].Members} ON COLUMNS, {[day].[day].[day].Members} ON ROWS FROM [Sales]`)
	want := sqlGrid(t, f, `SELECT coalesce(CAST(year(day) AS VARCHAR), 'null'), coalesce(CAST(day AS VARCHAR), 'null'), CAST(count(*) AS VARCHAR) FROM serve GROUP BY year(day), day`)
	sameGrid(t, "year by day", ds.grid("Axis0", "Axis1"), want)
}

func TestASlicerAndASubselectOnTheSameHierarchy(t *testing.T) {
	f := salesFixture(t)
	// the slicer is one of the members of the subselect: its rows
	ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM (SELECT {[country].[country].[country].&[IT],[country].[country].[country].&[FR]} ON COLUMNS FROM [Sales]) WHERE ([country].[country].[country].&[IT])`)
	sameGrid(t, "slicer inside", ds.grid("Axis0"), map[string]string{"Rows": "2"})
	// a slicer outside the subselect's members: a member the rows left do not have is a fault (spec: the
	// interpretations to check with the real client)
	body := f.refused(`SELECT {[Measures].[Rows]} ON COLUMNS FROM (SELECT {[country].[country].[country].&[IT]} ON COLUMNS FROM [Sales]) WHERE ([country].[country].[country].&[FR])`)
	if !strings.Contains(body, "does not exist") {
		t.Errorf("%s", body)
	}
	// and on an axis, with the slicer on another hierarchy of the subselect's field
	ds = f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS, {[paid].[paid].[paid].Members} ON ROWS FROM (SELECT {[country].[country].[country].&[IT],[country].[country].[country].&[FR]} ON COLUMNS FROM [Sales]) WHERE ([code].[code].[code].&[7])`)
	sameGrid(t, "axis under a subselect and a slicer", ds.grid("Axis0", "Axis1"), map[string]string{"Rows|true": "2"})
}

// ── Discover lists what Execute takes, and what XML cannot hold is not lost ──

const oddSQL = `COPY (SELECT * FROM (VALUES
 (1, 'a' || chr(1) || 'b', TIMESTAMP_NS '2026-01-20 08:30:00.123456789', 5),
 (2, 'a' || chr(2) || 'b', TIMESTAMP_NS '2026-01-20 08:30:00.123456790', 7),
 (3, 'ab',                  TIMESTAMP_NS '2026-01-20 08:30:00.123456789', 9),
 (4, 'x' || chr(31),        TIMESTAMP_NS '2026-02-01 00:00:00',           NULL)
) t(id, note, ts, n)) TO '%s' (FORMAT parquet)`

func oddFixture(t *testing.T) *fixture {
	t.Helper()
	f := salesFixture(t)
	path := filepath.Join(f.dir, "storage", "odd.parquet")
	db, err := sql.Open("duckdb", "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(fmt.Sprintf(oddSQL, path)); err != nil {
		t.Fatal(err)
	}
	db.Close()
	f.m.Resources = append(f.m.Resources, dataapiwire.Resource{Name: "odd", Cube: "Odd", Available: true, Version: "2026-11-07T06:00:00Z",
		Fingerprint: "odd1", URL: f.files.URL + "/odd.parquet", Columns: []string{"id", "note", "ts", "n"}, Filters: []dataapiwire.Filter{}, MaxPage: 10})
	f.load()
	return f
}

// Every member Discover lists is a name Execute takes: a slicer on it gives the rows it has. The
// two compare a key the same way, a timestamp of nanoseconds included.
func TestEveryMemberDiscoverListsIsOneExecuteTakes(t *testing.T) {
	f := oddFixture(t)
	f.m.XMLA.MaxMembers = 5000
	f.s.Apply(f.m)
	for _, cube := range []string{"Sales", "Odd"} {
		p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", cube)
		taken := 0
		for i, un := range p.col("MEMBER_UNIQUE_NAME") {
			if p.rows[i]["MEMBER_TYPE"] != "1" {
				continue // the measures and the All members
			}
			ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [` + cube + `] WHERE (` + un + `)`)
			if c, ok := ds.cells[0]; !ok || c.value == "" || c.value == "0" {
				t.Errorf("%s: %s has no rows: %+v", cube, un, ds.cells)
			}
			taken++
		}
		if taken < 10 {
			t.Errorf("%s: only %d members tried", cube, taken)
		}
	}
	// the nanoseconds in particular: two of the four rows are one member, the third is its neighbour
	ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Odd] WHERE ([ts].[ts].[ts].&[2026-01-20 08:30:00.123456789])`)
	sameGrid(t, "a timestamp of nanoseconds", ds.grid("Axis0"), map[string]string{"Rows": "2"})
}

// Two values that differ only by a character XML cannot hold are two members with two names, and
// each name is a member that is found.
func TestValuesThatDifferByACharacterXMLCannotHoldAreTwoMembers(t *testing.T) {
	f := oddFixture(t)
	p := f.must("MDSCHEMA_MEMBERS", "CUBE_NAME", "Odd", "LEVEL_UNIQUE_NAME", "[note].[note].[note]")
	names := p.col("MEMBER_UNIQUE_NAME")
	if len(names) != 4 {
		t.Fatalf("%d members: %v", len(names), names)
	}
	seen := map[string]bool{}
	for _, n := range names {
		if seen[n] {
			t.Errorf("%q twice", n)
		}
		seen[n] = true
		ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Odd] WHERE (` + n + `)`)
		sameGrid(t, n, ds.grid("Axis0"), map[string]string{"Rows": "1"})
	}
	// the member a client writes with the escape of the name, as it was given
	a := f.post(executeBody(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Odd] WHERE ([note].[note].[note].&[\#a\u{1}b])`), "main")
	if a.status != 200 || !strings.Contains(string(a.body), `<Value xsi:type="xsd:int">1</Value>`) {
		t.Errorf("%d %s", a.status, a.body)
	}
}
