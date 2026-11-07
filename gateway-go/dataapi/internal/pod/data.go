package pod

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"

	_ "github.com/duckdb/duckdb-go/v2" // the «duckdb» driver of database/sql

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Table is a resource's data, prepared: a DuckDB file with only the columns that go out,
// sorted by the first filterable column, opened read-only. A request holds it (acquire) while
// it reads; a newer one takes its place atomically, and the old one is closed and removed
// once the last request reading it is done.
type Table struct {
	Resource string
	// the data's version, as the answers give it
	Version string
	// the data and the layout of the file: what a cursor is bound to
	Generation string
	Columns    []Column
	byName     map[string]Column
	// the column the file is sorted by, "" when none
	FileOrder string

	db   *sql.DB
	path string

	// the prepared statements, by their text: a request's SQL has few shapes (which filters,
	// how many values, which columns), and preparing again at every request was most of a
	// lookup's time
	stmtMu sync.Mutex
	stmts  map[string]*sql.Stmt

	mu      sync.Mutex
	refs    int
	retired bool
}

// Column is the prepared column of this name.
func (t *Table) Column(name string) (Column, bool) {
	c, ok := t.byName[name]
	return c, ok
}

func (t *Table) acquire() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.retired {
		return false
	}
	t.refs++
	return true
}

func (t *Table) release() {
	t.mu.Lock()
	t.refs--
	done := t.retired && t.refs == 0
	t.mu.Unlock()
	if done {
		t.close()
	}
}

// retire takes the table out of service: closed now, or when the last reader is done.
func (t *Table) retire() {
	t.mu.Lock()
	t.retired = true
	done := t.refs == 0
	t.mu.Unlock()
	if done {
		t.close()
	}
}

// MaxStatements is how many shapes of query a table keeps prepared; past them a query is
// prepared for its request only.
const MaxStatements = 512

// Query runs a query of the table, prepared once for its text.
func (t *Table) Query(ctx context.Context, text string, args ...any) (*sql.Rows, error) {
	t.stmtMu.Lock()
	st := t.stmts[text]
	if st == nil && len(t.stmts) < MaxStatements {
		var err error
		if st, err = t.db.PrepareContext(ctx, text); err != nil {
			t.stmtMu.Unlock()
			return nil, err
		}
		if t.stmts == nil {
			t.stmts = map[string]*sql.Stmt{}
		}
		t.stmts[text] = st
	}
	t.stmtMu.Unlock()
	if st == nil {
		return t.db.QueryContext(ctx, text, args...)
	}
	return st.QueryContext(ctx, args...)
}

func (t *Table) close() {
	t.stmtMu.Lock()
	for _, st := range t.stmts {
		_ = st.Close()
	}
	t.stmts = nil
	t.stmtMu.Unlock()
	_ = t.db.Close()
	_ = os.Remove(t.path)
	_ = os.Remove(t.path + ".wal")
}

// the names the pod gives its files in the data directory
var ownFile = regexp.MustCompile(`^r[0-9]+\.(duckdb|duckdb\.wal|parquet|parquet\.part)$`)

// RemoveLeftovers removes what a previous run left in the data directory: the pod's own
// files and its spill directory, nothing else.
func RemoveLeftovers(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if ownFile.MatchString(e.Name()) || (e.Name() == "tmp" && e.IsDir()) {
			_ = os.RemoveAll(filepath.Join(dir, e.Name()))
		}
	}
}

// Layout is what of a resource's definition shapes its file: the columns and the order. A
// change of either prepares the file again, even for the same data.
func Layout(r dataapiwire.Resource) string {
	h := sha256.New()
	for _, c := range r.Columns {
		h.Write([]byte(c + "\x00"))
	}
	h.Write([]byte("\x01" + fileOrder(r)))
	return hex.EncodeToString(h.Sum(nil))[:8]
}

// Generation is what a cursor of this resource's answers is bound to.
func Generation(r dataapiwire.Resource) string { return r.Fingerprint + "." + Layout(r) }

// fileOrder is the column the prepared file is sorted by: the first filterable one, the key a
// CRM asks by
func fileOrder(r dataapiwire.Resource) string {
	if len(r.Filters) > 0 {
		return r.Filters[0].Column
	}
	return ""
}

// ident quotes an identifier for DuckDB. The identifiers are the definition's column names,
// which the gateway checked against the datasource; no text of a request is ever written in
// the SQL.
func ident(name string) string { return `"` + strings.ReplaceAll(name, `"`, `""`) + `"` }

// literal quotes a string for DuckDB: only the pod's own file paths are written so
func literal(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// Preparer turns a resource's parquet into a Table.
type Preparer struct {
	Dir  string
	HTTP *http.Client
}

// Budgeted is the memory and the threads of DuckDB: preparing, then serving.
type Budgeted struct {
	PrepareMB, PrepareThreads int
	ServeMB, ServeThreads     int
}

// Prepare downloads the resource's parquet from its link and prepares it as file `name`
// (a name of the pod's own: never one from the manifest).
func (p *Preparer) Prepare(ctx context.Context, res dataapiwire.Resource, name string, b Budgeted) (*Table, error) {
	if err := os.MkdirAll(filepath.Join(p.Dir, "tmp"), 0o700); err != nil {
		return nil, err
	}
	parquet := filepath.Join(p.Dir, name+".parquet")
	file := filepath.Join(p.Dir, name+".duckdb")
	defer os.Remove(parquet)
	if err := p.download(ctx, res.URL, parquet); err != nil {
		return nil, fmt.Errorf("downloading the data: %w", err)
	}
	if err := p.build(ctx, res, parquet, file, b); err != nil {
		_ = os.Remove(file)
		_ = os.Remove(file + ".wal")
		return nil, fmt.Errorf("preparing the data: %w", err)
	}
	t, err := Open(file, res, b.ServeMB, b.ServeThreads)
	if err != nil {
		_ = os.Remove(file)
		return nil, err
	}
	return t, nil
}

func (p *Preparer) download(ctx context.Context, link, into string) error {
	if link == "" {
		return errors.New("no link")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, link, nil)
	if err != nil {
		return err
	}
	res, err := p.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("the storage answered %d", res.StatusCode)
	}
	part := into + ".part"
	f, err := os.OpenFile(part, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	n, err := io.Copy(f, res.Body)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil && res.ContentLength >= 0 && n != res.ContentLength {
		err = fmt.Errorf("%d bytes of %d", n, res.ContentLength)
	}
	if err != nil {
		_ = os.Remove(part)
		return err
	}
	return os.Rename(part, into)
}

// the settings every DuckDB of the pod opens with: no extension installed or loaded from the
// network
const quietDuckDB = "autoinstall_known_extensions=false&autoload_known_extensions=false"

func (p *Preparer) build(ctx context.Context, res dataapiwire.Resource, parquet, file string, b Budgeted) error {
	_ = os.Remove(file)
	_ = os.Remove(file + ".wal")
	dsn := fmt.Sprintf("%s?threads=%d&memory_limit=%dMB&temp_directory=%s&%s", file, max(b.PrepareThreads, 1), max(b.PrepareMB, 64),
		filepath.Join(p.Dir, "tmp"), quietDuckDB)
	db, err := sql.Open("duckdb", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	cols := make([]string, len(res.Columns))
	for i, c := range res.Columns {
		cols[i] = ident(c)
	}
	query := "CREATE TABLE serve AS SELECT " + strings.Join(cols, ", ") + " FROM read_parquet(" + literal(parquet) + ")"
	if order := fileOrder(res); order != "" {
		query += " ORDER BY " + ident(order) + " ASC NULLS LAST"
	}
	if _, err := db.ExecContext(ctx, query); err != nil {
		return err
	}
	_, err = db.ExecContext(ctx, "CHECKPOINT")
	return err
}

// Open opens a prepared file read-only, with no access to anything but it.
func Open(file string, res dataapiwire.Resource, memoryMB, threads int) (*Table, error) {
	dsn := fmt.Sprintf("%s?access_mode=read_only&threads=%d&memory_limit=%dMB&enable_external_access=false&preserve_insertion_order=true&%s", file,
		max(threads, 1), max(memoryMB, 32), quietDuckDB)
	db, err := sql.Open("duckdb", dsn)
	if err != nil {
		return nil, err
	}
	// the connections are the requests read side by side
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(16)
	t := &Table{Resource: res.Name, Version: res.Version, Generation: Generation(res), FileOrder: fileOrder(res), db: db,
		path: file, byName: map[string]Column{}}
	rows, err := db.Query(`SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'serve' ORDER BY column_index`)
	if err != nil {
		db.Close()
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var name, typ string
		if err := rows.Scan(&name, &typ); err != nil {
			db.Close()
			return nil, err
		}
		c := NewColumn(name, typ)
		t.Columns = append(t.Columns, c)
		t.byName[name] = c
	}
	if err := rows.Err(); err != nil {
		db.Close()
		return nil, err
	}
	if len(t.Columns) != len(res.Columns) {
		db.Close()
		return nil, fmt.Errorf("the prepared file has %d columns, not %d", len(t.Columns), len(res.Columns))
	}
	return t, nil
}
