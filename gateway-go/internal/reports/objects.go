package reports

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// What a dataset's source names, read with the reader's rights (services/reports.py `_connection`,
// `_datasource`, `_view`, `_script`) and the few services/sql_scripts.py helpers they use.

// Unreadable is sql_scripts.Unreadable: what a person may not read, said as what does not
// exist: a 422 like any other; a dashboard tells it apart.
type Unreadable struct{ Message string }

func (u *Unreadable) Error() string { return u.Message }

// Refusal is an HTTPException of these services: a status and a detail (a text, or an object).
type Refusal struct {
	Status int
	Detail any
}

func (r *Refusal) Error() string { return fmt.Sprint(r.Detail) }

// Text is _text: the detail's message.
func Text(err error) string {
	var u *Unreadable
	if errors.As(err, &u) {
		return u.Message
	}
	var r *Refusal
	if errors.As(err, &r) {
		if o, ok := r.Detail.(pyjson.Object); ok {
			m, _ := o.Get("message")
			return pystr.Str(m)
		}
		return pystr.Str(r.Detail)
	}
	return err.Error()
}

func unreadable(what string) error { return &Unreadable{Message: "it reads " + what} }

// Queryable is sql_scripts.QUERYABLE.
var Queryable = map[string]bool{"postgresql": true, "mysql": true, "mariadb": true, "clickhouse": true, "trino": true,
	"oracle": true, "sqlserver": true}

// Connection is a `connections` row.
type Connection struct {
	ID                int64
	Name              string
	ProjectID         int64
	DBType            string
	Host              string
	Port              *int64
	Username          string
	PasswordEncrypted string
	Database          string
	DBSchema          string
	Extra             string
}

// Datasource is a `datasources` row, the columns a render reads.
type Datasource struct {
	ID        int64
	Name      string
	ProjectID int64
	Bucket    string
	Key       string
	Columns   string
}

// View is a `saved_views` row.
type View struct {
	ID           int64
	ProjectID    int64
	DatasourceID int64
	Spec         string
}

// Script is a `sql_scripts` row.
type Script struct {
	ID             int64
	ProjectID      int64
	CurrentVersion int64
}

func one(err error) (bool, error) {
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// QueryableConnection is sql_scripts.queryable_connection.
func QueryableConnection(ctx context.Context, q perm.Querier, id int64) (*Connection, error) {
	var c Connection
	var extra *string
	ok, err := one(q.QueryRow(ctx, `SELECT id, name, project_id, db_type, host, port, username, password_encrypted, database,
		db_schema, extra FROM connections WHERE id = $1`, id).
		Scan(&c.ID, &c.Name, &c.ProjectID, &c.DBType, &c.Host, &c.Port, &c.Username, &c.PasswordEncrypted, &c.Database, &c.DBSchema, &extra))
	if extra != nil {
		c.Extra = *extra
	}
	if err != nil || !ok || !Queryable[c.DBType] {
		return nil, err
	}
	return &c, nil
}

// UsableConnection is _connection: a connection `user` may query, CONNECT on its folder.
func UsableConnection(ctx context.Context, q perm.Querier, c *perm.Checker, id int64) (*Connection, error) {
	conn, err := QueryableConnection(ctx, q, id)
	if err != nil {
		return nil, err
	}
	if conn != nil {
		ok, err := c.Has(ctx, conn.ProjectID, perm.Connect)
		if err != nil {
			return nil, err
		}
		if ok {
			return conn, nil
		}
	}
	return nil, unreadable(fmt.Sprintf("a database connection (%d) that does not exist or that you cannot use", id))
}

// GetDatasource is session.get(Datasource, id).
func GetDatasource(ctx context.Context, q perm.Querier, id int64) (*Datasource, error) {
	var d Datasource
	ok, err := one(q.QueryRow(ctx, `SELECT id, name, project_id, bucket, key, columns FROM datasources WHERE id = $1`, id).
		Scan(&d.ID, &d.Name, &d.ProjectID, &d.Bucket, &d.Key, &d.Columns))
	if err != nil || !ok {
		return nil, err
	}
	return &d, nil
}

// ReadableDatasource is _datasource: VIEW on its folder.
func ReadableDatasource(ctx context.Context, q perm.Querier, c *perm.Checker, id int64) (*Datasource, error) {
	d, err := GetDatasource(ctx, q, id)
	if err != nil {
		return nil, err
	}
	if d != nil {
		ok, err := c.Has(ctx, d.ProjectID, perm.View)
		if err != nil {
			return nil, err
		}
		if ok {
			return d, nil
		}
	}
	return nil, unreadable(fmt.Sprintf("a datasource (%d) that does not exist or that you cannot read", id))
}

// ReadableView is _view: VIEW on its folder and on its datasource's.
func ReadableView(ctx context.Context, q perm.Querier, c *perm.Checker, id int64) (*View, *Datasource, error) {
	var v View
	found, err := one(q.QueryRow(ctx, `SELECT id, project_id, datasource_id, spec FROM saved_views WHERE id = $1`, id).
		Scan(&v.ID, &v.ProjectID, &v.DatasourceID, &v.Spec))
	if err != nil {
		return nil, nil, err
	}
	if found {
		ok, err := c.Has(ctx, v.ProjectID, perm.View)
		if err != nil {
			return nil, nil, err
		}
		if ok {
			d, err := ReadableDatasource(ctx, q, c, v.DatasourceID)
			if err != nil {
				return nil, nil, err
			}
			return &v, d, nil
		}
	}
	return nil, nil, unreadable(fmt.Sprintf("a saved view (%d) that does not exist or that you cannot read", id))
}

// GetScript is session.get(SqlScript, id).
func GetScript(ctx context.Context, q perm.Querier, id int64) (*Script, error) {
	var s Script
	ok, err := one(q.QueryRow(ctx, `SELECT id, project_id, current_version FROM sql_scripts WHERE id = $1`, id).
		Scan(&s.ID, &s.ProjectID, &s.CurrentVersion))
	if err != nil || !ok {
		return nil, err
	}
	return &s, nil
}

// ReadableScript is _script: sql_scripts.can_see (VIEW on its folder; administrators every one).
func ReadableScript(ctx context.Context, q perm.Querier, c *perm.Checker, id int64) (*Script, error) {
	s, err := GetScript(ctx, q, id)
	if err != nil {
		return nil, err
	}
	if s != nil {
		admin, err := c.IsAdmin(ctx)
		if err != nil {
			return nil, err
		}
		ok := admin
		if !ok {
			if ok, err = c.Has(ctx, s.ProjectID, perm.View); err != nil {
				return nil, err
			}
		}
		if ok {
			return s, nil
		}
	}
	return nil, unreadable(fmt.Sprintf("a SQL script (%d) that does not exist or that you cannot read", id))
}

// ScriptContent is sql_scripts.content_of: the current version's content over empty_content().
func ScriptContent(ctx context.Context, q perm.Querier, s *Script) (pyjson.Object, error) {
	content := pyjson.Object{{Key: "sql", Value: ""}, {Key: "engine", Value: nil}, {Key: "connection_id", Value: nil}, {Key: "output", Value: nil}}
	if s == nil {
		return content, nil
	}
	var text string
	ok, err := one(q.QueryRow(ctx, `SELECT content FROM sql_script_versions WHERE script_id = $1 AND version = $2 LIMIT 1`,
		s.ID, s.CurrentVersion).Scan(&text))
	if err != nil || !ok {
		return content, err
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return content, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, ErrInternal // {**empty_content(), **json.loads(...)} of a non-dict
	}
	for _, kv := range o {
		content = content.Set(kv.Key, kv.Value)
	}
	return content, nil
}

// ErrInternal is where the Python code fails by itself: a 500.
var ErrInternal = errors.New("the Python code fails here")

// StripComments is sql_scripts.strip_comments (sqlscripts.StripComments): the SQL without its
// comments, quoted text left alone.
func StripComments(sql string) string { return sqlscripts.StripComments(sql) }
