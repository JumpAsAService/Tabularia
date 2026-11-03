// Package sqlscripts is services/sql_scripts.py and the parts of services/sql_script_runs.py
// the routes read: who sees a SQL script, what its versions hold, the connections and the
// datasources it may name. Reading a SQL (sqlglot: `resolve`, `named_datasources`) stays in
// the Python gateway: the routes that need it are forwarded there.
package sqlscripts

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const (
	MaxSQLChars = 100_000
	MaxRows     = 1000 // the engine's preview cap
	DefaultRows = 200
)

// Queryable are the connections a SQL script can run on: the databases.
var Queryable = map[string]bool{"postgresql": true, "mysql": true, "mariadb": true, "clickhouse": true,
	"trino": true, "oracle": true, "sqlserver": true}

// KnownEngines is engine_policy.KNOWN_ENGINES.
var KnownEngines = map[string]bool{"polars": true, "duckdb": true, "chdb": true, "clickhouse": true, "bigquery": true}

// Querier is a pool, a connection or a transaction.
type Querier = perm.Querier

// Script is a row of sql_scripts.
type Script struct {
	ID             int64
	Name           string
	Description    string
	ProjectID      int64
	OwnerID        *int64
	CurrentVersion int64
	RunSchedule    *string
	RunScheduledBy *int64
	NextRunAt      *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

const scriptColumns = `id, name, description, project_id, owner_id, current_version, run_schedule, run_scheduled_by,
	next_run_at, created_at, updated_at`

func scanScript(row pgx.Row) (*Script, error) {
	var s Script
	err := row.Scan(&s.ID, &s.Name, &s.Description, &s.ProjectID, &s.OwnerID, &s.CurrentVersion, &s.RunSchedule,
		&s.RunScheduledBy, &s.NextRunAt, &s.CreatedAt, &s.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return &s, nil
}

// Get is session.get(SqlScript, id): nil when there is none.
func Get(ctx context.Context, q Querier, id int64) (*Script, error) {
	s, err := scanScript(q.QueryRow(ctx, `SELECT `+scriptColumns+` FROM sql_scripts WHERE id = $1`, id))
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	return s, err
}

// CanSee is can_see.
func CanSee(ctx context.Context, c *perm.Checker, s *Script) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	return c.Has(ctx, s.ProjectID, perm.View)
}

// GetVisible is get_visible: the script, or the 404 of one that does not exist.
func GetVisible(ctx context.Context, c *perm.Checker, q Querier, id int64) (*Script, *auth.Refusal, error) {
	s, err := Get(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	if s != nil {
		ok, err := CanSee(ctx, c, s)
		if err != nil {
			return nil, nil, err
		}
		if ok {
			return s, nil, nil
		}
	}
	return nil, &auth.Refusal{Status: http.StatusNotFound, Detail: "SQL script not found"}, nil
}

// Visible is visible_query run: the scripts this person sees (in `projects` when not nil),
// by name.
func Visible(ctx context.Context, c *perm.Checker, q Querier, projects []int64) ([]Script, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	var ids []int64
	filter := true
	if !admin {
		readable, err := c.Readable(ctx)
		if err != nil {
			return nil, err
		}
		if projects != nil {
			only := map[int64]bool{}
			for _, p := range projects {
				if readable[p] {
					only[p] = true
				}
			}
			readable = only
		}
		if len(readable) == 0 {
			return nil, nil
		}
		ids = perm.Sorted(readable)
	} else if projects != nil {
		ids = projects
	} else {
		filter = false
	}
	sql := `SELECT ` + scriptColumns + ` FROM sql_scripts`
	var args []any
	if filter {
		sql += ` WHERE project_id = ANY($1)`
		args = append(args, ids)
	}
	rows, err := q.Query(ctx, sql+` ORDER BY name`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Script{}
	for rows.Next() {
		s, err := scanScript(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *s)
	}
	return out, rows.Err()
}

// ── what a script holds ───────────────────────────────────────────────────────

// EmptyContent is empty_content.
func EmptyContent() pyjson.Object {
	return pyjson.Object{{Key: "sql", Value: ""}, {Key: "engine", Value: nil}, {Key: "connection_id", Value: nil},
		{Key: "output", Value: nil}}
}

// errNotAnObject: `{**empty_content(), **json.loads(…)}` of a JSON that is no object is a
// TypeError in Python, a 500.
var errNotAnObject = fmt.Errorf("sql_scripts: a version's content is not a JSON object")

// Parsed is _parsed: the version's content over the empty one; unreadable JSON is empty.
func Parsed(content *string) (pyjson.Object, error) {
	out := EmptyContent()
	if content == nil {
		return out, nil
	}
	v, err := pyjson.Loads(*content)
	if err != nil {
		return out, nil // ValueError
	}
	obj, ok := v.(pyjson.Object)
	if !ok {
		return nil, errNotAnObject
	}
	for _, kv := range obj {
		out = out.Set(kv.Key, kv.Value)
	}
	return out, nil
}

// VersionRow is version_row: whether the version exists, and its content; nil = the current.
func VersionRow(ctx context.Context, q Querier, s *Script, version *int64) (bool, *string, error) {
	v := s.CurrentVersion
	if version != nil {
		v = *version
	}
	var content string
	err := q.QueryRow(ctx, `SELECT content FROM sql_script_versions WHERE script_id = $1 AND version = $2`,
		s.ID, v).Scan(&content)
	if err == pgx.ErrNoRows {
		return false, nil, nil
	}
	if err != nil {
		return false, nil, err
	}
	return true, &content, nil
}

// ContentOf is content_of.
func ContentOf(ctx context.Context, q Querier, s *Script, version *int64) (pyjson.Object, error) {
	_, content, err := VersionRow(ctx, q, s, version)
	if err != nil {
		return nil, err
	}
	return Parsed(content)
}

// CurrentContents is current_contents: each script's current version, read at once.
func CurrentContents(ctx context.Context, q Querier, scripts []Script) (map[int64]pyjson.Object, error) {
	out := map[int64]pyjson.Object{}
	if len(scripts) == 0 {
		return out, nil
	}
	ids := make([]int64, len(scripts))
	for i, s := range scripts {
		ids[i] = s.ID
	}
	rows, err := q.Query(ctx, `SELECT v.script_id, v.content FROM sql_script_versions v JOIN sql_scripts s
		ON s.id = v.script_id AND s.current_version = v.version WHERE s.id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	byScript := map[int64]*string{}
	for rows.Next() {
		var id int64
		var content string
		if err := rows.Scan(&id, &content); err != nil {
			rows.Close()
			return nil, err
		}
		byScript[id] = &content
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, s := range scripts {
		c, err := Parsed(byScript[s.ID])
		if err != nil {
			return nil, err
		}
		out[s.ID] = c
	}
	return out, nil
}

// AddVersion is add_version: the next version, which becomes the current one, with the
// script's row locked until the commit. Returns the new version's number.
func AddVersion(ctx context.Context, tx pgx.Tx, s *Script, content pyjson.Object, note string, userID int64) (int64, error) {
	if _, err := tx.Exec(ctx, `SELECT id FROM sql_scripts WHERE id = $1 FOR UPDATE`, s.ID); err != nil {
		return 0, err
	}
	var last int64
	err := tx.QueryRow(ctx, `SELECT version FROM sql_script_versions WHERE script_id = $1 ORDER BY version DESC LIMIT 1`,
		s.ID).Scan(&last)
	if err != nil && err != pgx.ErrNoRows {
		return 0, err
	}
	text, err := pyjson.Dumps(content, pyjson.Options{EnsureASCII: true})
	if err != nil {
		return 0, err
	}
	version := last + 1
	if _, err := tx.Exec(ctx, `INSERT INTO sql_script_versions (script_id, version, content, note, created_at, created_by)
		VALUES ($1, $2, $3, $4, $5, $6)`, s.ID, version, text, note, time.Now().UTC(), userID); err != nil {
		return 0, err
	}
	s.CurrentVersion = version
	return version, nil
}

// ── connections ───────────────────────────────────────────────────────────────

// Connection is the part of a connections row the scripts read.
type Connection struct {
	ID        int64
	Name      string
	ProjectID int64
	DBType    string
}

// QueryableConnection is queryable_connection: nil when it is gone or no database.
func QueryableConnection(ctx context.Context, q Querier, id int64) (*Connection, error) {
	var c Connection
	err := q.QueryRow(ctx, `SELECT id, name, project_id, db_type FROM connections WHERE id = $1`, id).
		Scan(&c.ID, &c.Name, &c.ProjectID, &c.DBType)
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !Queryable[c.DBType] {
		return nil, nil
	}
	return &c, nil
}

// UsableConnection is usable_connection: CONNECT on its folder.
func UsableConnection(ctx context.Context, c *perm.Checker, q Querier, id int64) (*Connection, *auth.Refusal, error) {
	conn, err := QueryableConnection(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	if conn == nil {
		return nil, &auth.Refusal{Status: http.StatusNotFound, Detail: "The script's database connection no longer exists: choose another"}, nil
	}
	ok, err := c.Has(ctx, conn.ProjectID, perm.Connect)
	if err != nil {
		return nil, nil, err
	}
	if !ok {
		return nil, &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("Running SQL on «%s» needs the CONNECT permission "+
			"on its folder: ask whoever manages it", conn.Name)}, nil
	}
	return conn, nil, nil
}

// ── folders and datasources ───────────────────────────────────────────────────

// Folder is a row of projects, its name and its parent.
type Folder struct {
	Name   string
	Parent *int64
}

// Folders is permissions._all_projects.
func Folders(ctx context.Context, q Querier) (map[int64]Folder, error) {
	rows, err := q.Query(ctx, `SELECT id, name, parent_id FROM projects`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]Folder{}
	for rows.Next() {
		var id int64
		var f Folder
		if err := rows.Scan(&id, &f.Name, &f.Parent); err != nil {
			return nil, err
		}
		out[id] = f
	}
	return out, rows.Err()
}

// Paths is folder_paths: every folder's whole path joined by `sep` («Vendite / Nord»).
func Paths(folders map[int64]Folder, sep string) map[int64]string {
	parents := make(map[int64]*int64, len(folders))
	for id, f := range folders {
		parents[id] = f.Parent
	}
	out := make(map[int64]string, len(folders))
	for id := range folders {
		chain := perm.Ancestors(parents, id)
		names := make([]string, 0, len(chain))
		for i := len(chain) - 1; i >= 0; i-- {
			if f, ok := folders[chain[i]]; ok {
				names = append(names, f.Name)
			}
		}
		out[id] = strings.Join(names, sep)
	}
	return out
}

// Datasource is the part of a datasources row the scripts read.
type Datasource struct {
	ID          int64
	Name        string
	Description string
	ProjectID   int64
	Bucket      string
	Key         string
	Rows        *int64
	Columns     string
	Kind        string
	UpdatedAt   time.Time
}

// DatasourceColumns is the column list of the datasource reads.
const DatasourceColumns = `id, name, description, project_id, bucket, key, rows, columns, kind, updated_at`

// ScanDatasource reads DatasourceColumns.
func ScanDatasource(row pgx.Row) (*Datasource, error) {
	var d Datasource
	if err := row.Scan(&d.ID, &d.Name, &d.Description, &d.ProjectID, &d.Bucket, &d.Key, &d.Rows, &d.Columns, &d.Kind,
		&d.UpdatedAt); err != nil {
		return nil, err
	}
	return &d, nil
}

// ReadableDatasources is readable_datasources: in the folders the person may read, by name.
func ReadableDatasources(ctx context.Context, c *perm.Checker, q Querier) ([]Datasource, error) {
	readable, err := c.Readable(ctx)
	if err != nil || len(readable) == 0 {
		return nil, err
	}
	rows, err := q.Query(ctx, `SELECT `+DatasourceColumns+` FROM datasources WHERE project_id = ANY($1) ORDER BY name`,
		perm.Sorted(readable))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Datasource
	for rows.Next() {
		d, err := ScanDatasource(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *d)
	}
	return out, rows.Err()
}

// ── the datasource it writes (services/sql_script_runs.py) ────────────────────

// OutputOf is sql_script_runs.output_of: {"name", "project_id"} as stored, or nil.
func OutputOf(content pyjson.Object) pyjson.Object {
	v, _ := content.Get("output")
	out, ok := v.(pyjson.Object)
	if !ok {
		return nil
	}
	name, _ := out.Get("name")
	if !Truthy(name) {
		return nil
	}
	if _, ok := OutputProject(out); !ok {
		return nil
	}
	return out
}

// OutputProject is the output's project_id, when it is an int (a bool is one, in Python).
func OutputProject(out pyjson.Object) (int64, bool) {
	p, _ := out.Get("project_id")
	switch x := p.(type) {
	case int64:
		return x, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

// TargetOf is sql_script_runs.target_of: the datasource already under the output's name.
func TargetOf(ctx context.Context, q Querier, out pyjson.Object) (*Datasource, error) {
	project, _ := OutputProject(out)
	name, _ := out.Get("name")
	n, ok := name.(string)
	if !ok {
		return nil, nil
	}
	d, err := ScanDatasource(q.QueryRow(ctx, `SELECT `+DatasourceColumns+` FROM datasources
		WHERE project_id = $1 AND name = $2`, project, n))
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	return d, err
}

// Truthy is Python's bool() of a JSON value.
func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int64:
		return x != 0
	case float64:
		return x != 0
	case pyjson.Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}
