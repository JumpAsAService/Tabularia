package pod

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// What Execute leaves behind and what it writes where: the temporary tables, the text of the SQL,
// the statements kept (docs/specs/xmla.md, «Execution» and «The MDX that is understood»).

// leftTables lists the temporary tables of Execute that any connection of the table's engine
// still holds: as many connections are asked as can be opened, the idle ones among them.
func leftTables(t *testing.T, f *fixture) []string {
	t.Helper()
	tb := f.s.table("sales")
	defer tb.release()
	ctx := context.Background()
	var conns []*sql.Conn
	defer func() {
		for _, c := range conns {
			c.Close()
		}
	}()
	var left []string
	for i := 0; i < 8; i++ {
		c, err := tb.conn(ctx)
		if err != nil {
			t.Fatal(err)
		}
		conns = append(conns, c)
		rows, err := c.QueryContext(ctx, `SELECT table_name FROM duckdb_tables() WHERE temporary AND table_name LIKE 'xmla\_%' ESCAPE '\'`)
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			var n string
			_ = rows.Scan(&n)
			left = append(left, n)
		}
		rows.Close()
	}
	return left
}

// recorder is a connection that notes the statements it is given
type recorder struct {
	xmla.Runner
	texts []string
	args  [][]any
	after func(n int) // called after the n-th statement that changes something
	execs int
	conn  *sql.Conn
}

// release gives the connection back: the pool is small, and a test that keeps them runs out
func (r *recorder) release() {
	if r.conn != nil {
		r.conn.Close()
		r.conn = nil
	}
}

func (r *recorder) ExecContext(ctx context.Context, q string, args ...any) (sql.Result, error) {
	r.texts, r.args = append(r.texts, q), append(r.args, args)
	res, err := r.Runner.ExecContext(ctx, q, args...)
	r.execs++
	if r.after != nil {
		r.after(r.execs)
	}
	return res, err
}

func (r *recorder) QueryContext(ctx context.Context, q string, args ...any) (*sql.Rows, error) {
	r.texts, r.args = append(r.texts, q), append(r.args, args)
	return r.Runner.QueryContext(ctx, q, args...)
}

// direct runs Execute on the sales table, bypassing HTTP, through a recording connection
func direct(t *testing.T, f *fixture, ctx context.Context, mdx string, edit func(*recorder)) (*xmla.Result, *recorder, error) {
	t.Helper()
	return directWith(t, f, ctx, mdx, edit, xmla.ExecLimits{MaxCells: 1000, MaxTuples: 100})
}

// directWith is direct with the caps of the test's own, and the properties of the request if given.
func directWith(t *testing.T, f *fixture, ctx context.Context, mdx string, edit func(*recorder), lim xmla.ExecLimits, props ...string) (*xmla.Result, *recorder, error) {
	t.Helper()
	return directRes(t, f, "sales", ctx, mdx, edit, lim, props...)
}

// directOn is direct on another resource of the fixture.
func directOn(t *testing.T, f *fixture, resource string, ctx context.Context, mdx string, lim xmla.ExecLimits) (*xmla.Result, *recorder, error) {
	t.Helper()
	return directRes(t, f, resource, ctx, mdx, nil, lim)
}

func directRes(t *testing.T, f *fixture, resource string, ctx context.Context, mdx string, edit func(*recorder), lim xmla.ExecLimits, props ...string) (*xmla.Result, *recorder, error) {
	t.Helper()
	tb := f.s.table(resource)
	t.Cleanup(tb.release)
	res := f.s.snap.Load().resources[resource]
	cat := &xmla.Catalog{Name: "CRM", Cubes: []*xmlamodel.Cube{cubeOf(res, tb)}}
	rec := &recorder{}
	src := xmla.Source{Dialect: xmla.DuckDB{}, Table: serveTable, Open: func(*xmlamodel.Cube) (xmla.Runner, error) {
		c, err := tb.conn(context.Background())
		if err != nil {
			return nil, err
		}
		rec.conn = c
		t.Cleanup(rec.release)
		rec.Runner = c
		if edit != nil {
			edit(rec)
		}
		return rec, nil
	}}
	req, err := xmla.ReadRequest(strings.NewReader(executeBody(mdx, props...)))
	if err != nil {
		t.Fatal(err)
	}
	r, err := xmla.Execute(ctx, src, req, cat, lim)
	return r, rec, err
}

type failingWriter struct{ after int }

func (w *failingWriter) Write(p []byte) (int, error) {
	if w.after -= len(p); w.after < 0 {
		return 0, errors.New("the client went away")
	}
	return len(p), nil
}

func TestExecuteLeavesNoTableBehind(t *testing.T) {
	f := salesFixture(t)
	const mdx = `SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, NON EMPTY Hierarchize(DrilldownLevel({[country].[country].[All]})) ON ROWS FROM [Sales]`
	// an answer over HTTP
	if a := f.post(executeBody(mdx), "main"); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("after an answer: %v", left)
	}
	// a fault that comes after the tables were made: the cap of cells
	f.m.XMLA.MaxCells = 3
	f.s.Apply(f.m)
	f.refused(mdx)
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("after a cap: %v", left)
	}
	f.m.XMLA.MaxCells = 1000
	f.s.Apply(f.m)
	// through Execute, to see the tables while the result lives
	r, rec, err := direct(t, f, context.Background(), mdx, nil)
	if err != nil {
		t.Fatal(err)
	}
	// the tables are the connection's: look at them through it
	rows, err := rec.Runner.QueryContext(context.Background(), `SELECT count(*) FROM duckdb_tables() WHERE temporary AND table_name LIKE 'xmla\_%' ESCAPE '\'`)
	if err != nil {
		t.Fatal(err)
	}
	var n int
	if rows.Next() {
		_ = rows.Scan(&n)
	}
	rows.Close()
	if n < 4 {
		t.Errorf("while the result is open: %d tables (they should be there)", n)
	}
	var out strings.Builder
	if err := r.Write(context.Background(), &out, ""); err != nil {
		t.Fatal(err)
	}
	r.Close()
	rec.release()
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("after Close: %v", left)
	}
	r.Close() // a second one is harmless
	if len(rec.texts) == 0 {
		t.Error("nothing recorded")
	}
	// a client that goes away in the middle of the writing
	r, rec, err = direct(t, f, context.Background(), mdx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Write(context.Background(), &failingWriter{after: 100}, ""); !errors.Is(err, xmla.ErrWrite) {
		t.Errorf("write: %v", err)
	}
	r.Close()
	rec.release()
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("after a client that left: %v", left)
	}
	// the context cancelled after the n-th statement: the rest cannot run, and the tables go
	for n := 1; n <= 7; n++ {
		ctx, cancel := context.WithCancel(context.Background())
		r, rec, err := direct(t, f, ctx, mdx, func(rec *recorder) {
			rec.after = func(i int) {
				if i == n {
					cancel()
				}
			}
		})
		if err == nil {
			r.Close()
		} else if !strings.Contains(err.Error(), "cancel") {
			t.Errorf("cancelled after %d statements: %v", n, err)
		}
		cancel()
		rec.release()
		if left := leftTables(t, f); len(left) != 0 {
			t.Errorf("cancelled after %d statements: %v", n, left)
		}
	}
	// a context that is cancelled before it starts
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if r, rec, err := direct(t, f, ctx, mdx, nil); err == nil {
		r.Close()
		t.Error("a cancelled context gave an answer")
	} else {
		rec.release()
	}
	if left := leftTables(t, f); len(left) != 0 {
		t.Errorf("after a cancelled request: %v", left)
	}
}

func TestExecuteWritesNoValueOfTheMDXInTheSQL(t *testing.T) {
	f := salesFixture(t)
	mdx := `SELECT NON EMPTY {[Measures].[Rows],[Measures].[amount]} ON COLUMNS, NON EMPTY Hierarchize(DrilldownMember({{DrilldownLevel({[country].[country].[All]})}}, {[country].[country].[All]})) ON ROWS ` +
		`FROM (SELECT {[country].[country].[country].&[Città],[country].[country].[country].&[a]]b]} ON COLUMNS, {[day].[Year-Quarter-Month-Day].[Year].&[2026]} ON ROWS FROM [Sales]) ` +
		`WHERE ([paid].[paid].[paid].&[true])`
	r, rec, err := direct(t, f, context.Background(), mdx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer rec.release()
	text := strings.Join(rec.texts, "\n")
	for _, v := range []string{"Città", "a]b", "2026", "true", "[", "&"} {
		if strings.Contains(text, v) {
			t.Errorf("%q is in the text of the SQL:\n%s", v, text)
		}
	}
	// they are bound
	bound := fmt.Sprint(rec.args)
	for _, v := range []string{"Città", "a]b", "2026", "true"} {
		if !strings.Contains(bound, v) {
			t.Errorf("%q is not among the bound values: %s", v, bound)
		}
	}
	// a key that is SQL is a key like another: a member the data does not have
	evil := "x'; " + "DR" + "OP TABLE serve; --"
	_, rec, err = direct(t, f, context.Background(), `SELECT {[country].[country].[country].&[`+evil+`]} ON COLUMNS FROM [Sales]`, nil)
	rec.release()
	var fault *xmla.Fault
	if !errors.As(err, &fault) || !strings.Contains(fault.Message, "does not exist") {
		t.Fatalf("%v", err)
	}
	for _, q := range rec.texts {
		if strings.Contains(q, "DR"+"OP") || strings.Contains(q, "x'") {
			t.Errorf("the key is in the SQL: %s", q)
		}
	}
	if ds := f.exec(`SELECT {[Measures].[Rows]} ON COLUMNS FROM [Sales]`); ds.cells[0].value != "8" {
		t.Errorf("the table: %+v", ds.cells)
	}
	// nothing of the values in the logs
	f.refused(`SELECT {[country].[country].[country].&[SECRETVALUE]} ON COLUMNS FROM [Sales]`)
	f.refused(`SELECT {[country].[country].[country].&[SECRETVALUE]} ON COLUMNS FROM [Nope]`)
	if strings.Contains(f.logs.String(), "SECRETVALUE") {
		t.Errorf("a value in the log: %s", f.logs.String())
	}
}

func TestExecuteKeepsRefusedStatementsWhenAllowed(t *testing.T) {
	f := salesFixture(t)
	file := filepath.Join(f.dir, "data", refusedFile)
	read := func(key string) answer { return f.get("/data/crm/xmla/refused", key) }
	// the switch is off: nothing is kept, and the way to read it says so
	f.refused(`SELECT {[country].[country].[country].&[SECRETVALUE]} ON COLUMNS FROM [Sales]`)
	if _, err := os.Stat(file); err == nil {
		t.Error("a statement kept with the switch off")
	}
	f.want(read("main"), 404, "not_found")
	// on: what the parser and the plan refused is kept
	f.m.XMLA.KeepRefused = true
	f.s.Apply(f.m)
	f.refused(`WITH MEMBER [Measures].[x] AS '1' SELECT {[Measures].[x]} ON COLUMNS FROM [Sales]`)
	f.refused(`SELECT Head(CrossJoin([country].[country].[country].Members, [paid].[paid].[paid].Members), 2) ON COLUMNS FROM [Sales]`)
	f.refused(`SELECT {[country].[country].[country].&[SECRETVALUE]} ON COLUMNS FROM [Sales]`)
	// not kept: what is answered, and what a cap stops
	f.exec(`SELECT FROM [Sales]`)
	f.m.XMLA.MaxCells = 1
	f.s.Apply(f.m)
	f.refused(`SELECT {[Measures].[Rows],[Measures].[amount]} ON COLUMNS FROM [Sales]`)
	f.m.XMLA.MaxCells = 1000
	f.s.Apply(f.m)
	a := read("main")
	if a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	var got struct {
		Refused []struct {
			At, Statement, Reason string
			Truncated             bool
		}
		KeptHours int `json:"kept_hours"`
	}
	if err := json.Unmarshal(a.body, &got); err != nil {
		t.Fatal(err)
	}
	if len(got.Refused) != 3 || got.KeptHours != 24 {
		t.Fatalf("%+v", got)
	}
	if !strings.Contains(got.Refused[0].Statement, "SECRETVALUE") || !strings.Contains(got.Refused[0].Reason, "does not exist") ||
		!strings.HasPrefix(got.Refused[1].Statement, "SELECT Head(") || !strings.Contains(got.Refused[1].Reason, "Head over a set of several hierarchies") ||
		!strings.HasPrefix(got.Refused[2].Statement, "WITH MEMBER") || !strings.Contains(got.Refused[2].Reason, "WITH MEMBER") {
		t.Errorf("%+v", got.Refused)
	}
	// read with a key that is not limited to some resources
	f.want(read("orders"), 403, "key_not_allowed")
	f.want(read(""), 401, "key_missing")
	// the values are in the file and not in the logs
	if strings.Contains(f.logs.String(), "SECRETVALUE") {
		t.Errorf("a value in the log: %s", f.logs.String())
	}
	// a day, and they are gone
	f.now = f.now.Add(25 * time.Hour)
	f.s.Apply(f.m)
	if a := read("main"); !strings.Contains(string(a.body), `"refused":[]`) {
		t.Errorf("%s", a.body)
	}
	// XMLA off for the data API: not even the list
	f.m.XMLA = nil
	f.s.Apply(f.m)
	f.want(read("main"), 404, "xmla_off")
}

func TestRefusedStoreIsSmall(t *testing.T) {
	dir := t.TempDir()
	st := &refusedStore{path: filepath.Join(dir, refusedFile)}
	now := time.Date(2026, 11, 25, 9, 0, 0, 0, time.UTC)
	// a statement is cut at its cap, on a character
	st.add(now, strings.Repeat("é", maxRefusedBytes), "why")
	got := st.list(now)
	if len(got) != 1 || !got[0].Truncated || len(got[0].Statement) > maxRefusedBytes || !utf8.ValidString(got[0].Statement) {
		t.Fatalf("%+v", got)
	}
	// only so many are kept, the latest
	for i := 0; i < maxRefused+20; i++ {
		st.add(now.Add(time.Duration(i)*time.Second), fmt.Sprintf("SELECT %d", i), "why")
	}
	got = st.list(now.Add(time.Hour))
	if len(got) != maxRefused || got[len(got)-1].Statement != fmt.Sprintf("SELECT %d", maxRefused+19) || got[0].Statement != "SELECT 20" {
		t.Fatalf("%d %q … %q", len(got), got[0].Statement, got[len(got)-1].Statement)
	}
	// the file is bounded
	if fi, err := os.Stat(st.path); err != nil || fi.Size() > maxRefused*(maxRefusedBytes+2048) {
		t.Errorf("%v %v", err, fi)
	}
	// a day later they are gone, and a file that does not read is an empty one
	if got := st.list(now.Add(25 * time.Hour)); len(got) != 0 {
		t.Errorf("%d", len(got))
	}
	if err := os.WriteFile(st.path, []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := st.list(now); len(got) != 0 {
		t.Errorf("%d", len(got))
	}
	st.add(now, "SELECT 1", "why")
	if got := st.list(now); len(got) != 1 {
		t.Errorf("%d", len(got))
	}
}
