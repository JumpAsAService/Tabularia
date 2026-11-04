package api

// routes/sql_scripts.py run_sql and export_sql, with services/sql_scripts.py run, export and
// _run_on_connection: the SQL in the editor run (its first rows) or exported (a csv or xlsx file
// the person follows through /tasks/exports), on the catalog or on a database connection. On the
// catalog the SQL is read by sqlglot in the AI service (sqlscripts.Resolve). Every run and every
// export is audited with its SQL, whatever its outcome.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

func init() {
	register("sql_scripts_run", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/sql-scripts/run", Handler: scWithBody(d, "/sql-scripts/run", runSQLScriptText)},
			{Method: http.MethodPost, Pattern: "/sql-scripts/export", Handler: scWithBody(d, "/sql-scripts/export", exportSQLScriptText)},
		}
	})
}

// sqlRun is what a run needs from its request.
type sqlRun struct {
	d      *Deps
	p      *auth.Principal
	c      *perm.Checker
	script *sqlscripts.Script // nil: a draft
	sql    string
}

// sqlAnswer writes an error of a run or an export: a refusal, the engine not answering (main.py's
// 503), a 500.
func (s *sqlRun) answerErr(w http.ResponseWriter, err error) {
	var ref *reports.Refusal
	var u *reports.Unreadable
	var sr *sqlscripts.Refusal
	var su *sqlscripts.Unreadable
	var down *reports.EngineDown
	switch {
	case errors.As(err, &ref):
		server.JSON(w, ref.Status, map[string]any{"detail": ref.Detail})
	case errors.As(err, &u):
		detail(w, http.StatusUnprocessableEntity, u.Message)
	case errors.As(err, &sr):
		detail(w, http.StatusUnprocessableEntity, sr.Message)
	case errors.As(err, &su):
		detail(w, http.StatusUnprocessableEntity, su.Message)
	case errors.As(err, &down):
		detail(w, http.StatusServiceUnavailable, "The engine is not answering: try again in a moment")
	default:
		fail(s.d, w, "sql_scripts: a run", err)
	}
}

// audit is _audit_run.
func (s *sqlRun) audit(ctx context.Context, action, outcome string, det pyjson.Object) {
	e := audit.Event{Actor: &s.p.User, Action: action, Outcome: outcome, TargetType: scPtrStr("sql_script"), TargetLabel: scPtrStr(""),
		Detail: det}
	if s.script != nil {
		e.TargetID, e.TargetLabel = scPtrInt(s.script.ID), scPtrStr(s.script.Name)
	}
	audit.Record(ctx, s.d.DB, s.d.Log, e)
}

// sqlRunBegin is what run_sql and export_sql do before the service: the script, when one is named,
// must be visible.
func sqlRunBegin(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, scriptID any) (*sqlRun, bool) {
	s := &sqlRun{d: d, p: p, c: perm.For(d.DB, &p.User)}
	if scriptID != nil {
		script, ok := sqlVisibleScript(d, w, r, s.c, idOf(scriptID))
		if !ok {
			return nil, false
		}
		s.script = script
	}
	return s, true
}

func (s *sqlRun) resolver() *reports.Resolver {
	return &reports.Resolver{Q: s.d.DB, C: s.c, Engine: s.d.Engine, Log: s.d.Log, SQL: s.d.SQL}
}

// catalog is pick_engine then resolve: the engine, the datasources the SQL names and its text.
func (s *sqlRun) catalog(ctx context.Context, engineV any) (string, *sqlscripts.Resolved, error) {
	engineName, err := s.resolver().PickEngine(ctx, engineV)
	if err != nil {
		return "", nil, err
	}
	resolved, err := sqlscripts.Resolve(ctx, s.d.DB, s.c, s.d.SQL, s.sql, engineName)
	if err != nil {
		return "", nil, err
	}
	if len(resolved.Tables) == 0 {
		return "", nil, &sqlscripts.Refusal{Message: "The query reads no datasource: name one in FROM"}
	}
	return engineName, resolved, nil
}

// connection is usable_connection, then the SQL must say something.
func (s *sqlRun) connection(ctx context.Context, w http.ResponseWriter, id any) (*reports.Connection, *sqlscripts.Connection, bool) {
	conn, refusal, err := sqlscripts.UsableConnection(ctx, s.c, s.d.DB, idOf(id))
	if err != nil {
		fail(s.d, w, "sql_scripts: the connection", err)
		return nil, nil, false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return nil, nil, false
	}
	// strip_comments(sql or "").strip().rstrip(";").strip()
	if pystr.Strip(strings.TrimRight(pystr.Strip(sqlscripts.StripComments(s.sql)), ";")) == "" {
		detail(w, http.StatusUnprocessableEntity, "The query is empty")
		return nil, nil, false
	}
	full, err := reports.QueryableConnection(ctx, s.d.DB, conn.ID)
	if err != nil || full == nil {
		fail(s.d, w, "sql_scripts: the connection", fmt.Errorf("connection %d: %v", conn.ID, err))
		return nil, nil, false
	}
	return full, conn, true
}

func sqlRunDatasources(resolved *sqlscripts.Resolved) []any {
	out := []any{}
	for _, n := range resolved.Tables {
		out = append(out, pyjson.Object{{Key: "id", Value: n.Datasource.ID}, {Key: "name", Value: n.Datasource.Name}})
	}
	return out
}

// sqlRunWordChar is a character of Python's \w in a str pattern.
func sqlRunWordChar(r rune) bool { return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r) }

// sqlRunReplaceWord is re.sub(rf"\b{word}\b", name, text) for a word of word characters.
func sqlRunReplaceWord(text, word, name string) string {
	var b strings.Builder
	for {
		i := strings.Index(text, word)
		if i < 0 {
			b.WriteString(text)
			return b.String()
		}
		before, _ := utf8.DecodeLastRuneInString(text[:i])
		after, _ := utf8.DecodeRuneInString(text[i+len(word):])
		if (i == 0 || !sqlRunWordChar(before)) && (i+len(word) == len(text) || !sqlRunWordChar(after)) {
			b.WriteString(text[:i])
			b.WriteString(name)
		} else {
			b.WriteString(text[:i+len(word)])
		}
		text = text[i+len(word):]
	}
}

var sqlRunPrefix = regexp.MustCompile(`^sql: `)

// sqlRunEngineMessage is _engine_message: the engine's refusal in the words of a SQL script, the
// aliases back to the names, and no talk of the `sql` node's input.
func sqlRunEngineMessage(detail string, resolved *sqlscripts.Resolved) string {
	for _, n := range resolved.Tables {
		detail = sqlRunReplaceWord(detail, n.Alias, n.Datasource.Name)
	}
	detail = sqlRunPrefix.ReplaceAllString(detail, "")
	detail = strings.ReplaceAll(detail, "the query can read only the input of the node (`self` or `input`). ", "the query can read only datasources. ")
	return strings.ReplaceAll(detail, ": to combine other data use a Join or Union node.", ".")
}

// sqlRunLen is len(res.get(key) or []) of what the engine answered; false where Python fails.
func sqlRunLen(v any) (int, bool) {
	switch x := v.(type) {
	case nil:
		return 0, true
	case []any:
		return len(x), true
	case pyjson.Object:
		return len(x), true
	case string:
		return pystr.Len(x), true
	case bool:
		if !x {
			return 0, true
		}
	case int64:
		if x == 0 {
			return 0, true
		}
	case float64:
		if x == 0 {
			return 0, true
		}
	}
	return 0, false
}

// sqlRunOrEmpty is `v or []`.
func sqlRunOrEmpty(v any) any {
	if pystr.Truthy(v) {
		return v
	}
	return []any{}
}

// runSQLScriptText is POST /sql-scripts/run (run_sql, sql_scripts.run): the first rows of the SQL in the
// editor (saved or not), on the catalog or on a connection.
func runSQLScriptText(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	in := params.Body(body, gatewaymodels.RunRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	s, ok := sqlRunBegin(d, w, r, p, in.Get("script_id"))
	if !ok {
		return
	}
	ctx := r.Context()
	s.sql = in.Get("sql").(string)
	limit := in.Get("limit").(int64)
	slot := fmt.Sprintf("u%d:sql:draft", p.User.ID)
	if s.script != nil {
		slot = fmt.Sprintf("u%d:sql:%d", p.User.ID, s.script.ID)
	}
	var (
		path      string
		req       pyjson.Object
		auditOf   func(rows int) pyjson.Object
		engineOut any
		resolved  *sqlscripts.Resolved
		connOut   any
	)
	if cid := in.Get("connection_id"); cid != nil {
		full, conn, ok := s.connection(ctx, w, cid)
		if !ok {
			return
		}
		path = "/db/query"
		req = pyjson.Object{{Key: "connection", Value: reports.ConnectionPayload(full)}, {Key: "sql", Value: s.sql},
			{Key: "limit", Value: limit}, {Key: "slot", Value: slot}}
		auditOf = func(rows int) pyjson.Object {
			return pyjson.Object{{Key: "sql", Value: pystr.Cut(s.sql, 2000)}, {Key: "rows", Value: int64(rows)},
				{Key: "connection", Value: pyjson.Object{{Key: "id", Value: conn.ID}, {Key: "name", Value: conn.Name}}}}
		}
		connOut = pyjson.Object{{Key: "id", Value: conn.ID}, {Key: "name", Value: conn.Name}, {Key: "db_type", Value: conn.DBType}}
	} else {
		engineName, res, err := s.catalog(ctx, in.Get("engine"))
		if err != nil {
			s.answerErr(w, err)
			return
		}
		resolved, engineOut = res, engineName
		path = "/tasks/preview"
		req = append(sqlscripts.CatalogChain(res), pyjson.KV{Key: "limit", Value: limit}, pyjson.KV{Key: "no_cache", Value: true},
			pyjson.KV{Key: "engine", Value: engineName}, pyjson.KV{Key: "slot", Value: slot})
		auditOf = func(rows int) pyjson.Object {
			return pyjson.Object{{Key: "sql", Value: pystr.Cut(s.sql, 2000)}, {Key: "engine", Value: engineName},
				{Key: "rows", Value: int64(rows)}, {Key: "datasources", Value: sqlRunDatasources(res)}}
		}
	}
	started := time.Now()
	outcome, rows := "failure", 0
	answer, err := func() (pyjson.Object, error) {
		cctx, cancel := context.WithTimeout(ctx, 300*time.Second)
		defer cancel()
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, path, req)
		if err != nil {
			return nil, &reports.EngineDown{Err: err}
		}
		if res.StatusCode >= 400 {
			message := func(m string) string { return m }
			if resolved != nil {
				message = func(m string) string { return sqlRunEngineMessage(m, resolved) }
			}
			return nil, reports.EngineFailure(res.StatusCode, raw, message)
		}
		v, err := pyjson.Loads(string(raw))
		if err != nil {
			return nil, err
		}
		o, isObj := v.(pyjson.Object)
		if !isObj {
			return nil, reports.ErrInternal // res.get of something else
		}
		rowsV, _ := o.Get("rows")
		n, ok := sqlRunLen(rowsV)
		if !ok {
			return nil, reports.ErrInternal
		}
		outcome, rows = "success", n
		return o, nil
	}()
	action := "sql_script.run"
	s.audit(ctx, action, outcome, auditOf(rows))
	if err != nil {
		s.answerErr(w, err)
		return
	}
	columns, _ := answer.Get("columns")
	rowsV, _ := answer.Get("rows")
	truncated, _ := answer.Get("truncated")
	datasources := []any{}
	if resolved != nil {
		datasources = sqlRunDatasources(resolved)
	}
	writeRaw(w, http.StatusOK, pyjson.Object{{Key: "columns", Value: sqlRunOrEmpty(columns)}, {Key: "rows", Value: sqlRunOrEmpty(rowsV)},
		{Key: "row_count", Value: int64(rows)}, {Key: "truncated", Value: pystr.Truthy(truncated)}, {Key: "engine", Value: engineOut},
		{Key: "elapsed_ms", Value: int64(time.Since(started) / time.Millisecond)}, {Key: "datasources", Value: datasources},
		{Key: "connection", Value: connOut}})
}

// exportSQLScriptText is POST /sql-scripts/export (export_sql, sql_scripts.export): the whole result as a
// csv or xlsx file, written by a worker as the Viewer's export, followed by its owner.
func exportSQLScriptText(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	in := params.Body(body, gatewaymodels.ExportRequest)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	s, ok := sqlRunBegin(d, w, r, p, in.Get("script_id"))
	if !ok {
		return
	}
	ctx := r.Context()
	s.sql = in.Get("sql").(string)
	format := in.Get("format").(string)
	name := ""
	if s.script != nil {
		name = s.script.Name
	}
	filename := reports.FilenameOr(name, format, "sql-script")
	var path string
	var req, det pyjson.Object
	if cid := in.Get("connection_id"); cid != nil {
		full, conn, ok := s.connection(ctx, w, cid)
		if !ok {
			return
		}
		path = "/db/exports"
		req = pyjson.Object{{Key: "connection", Value: reports.ConnectionPayload(full)}, {Key: "sql", Value: s.sql}}
		det = pyjson.Object{{Key: "connection", Value: pyjson.Object{{Key: "id", Value: conn.ID}, {Key: "name", Value: conn.Name}}}}
	} else {
		engineName, resolved, err := s.catalog(ctx, in.Get("engine"))
		if err != nil {
			s.answerErr(w, err)
			return
		}
		path = "/tasks/exports"
		req = append(sqlscripts.CatalogChain(resolved), pyjson.KV{Key: "engine", Value: engineName})
		det = pyjson.Object{{Key: "engine", Value: engineName}, {Key: "datasources", Value: sqlRunDatasources(resolved)}}
	}
	// export_wait(request): code (an API token) waits its turn on the export queue
	wait := 0
	if p.TokenName != "" {
		wait = d.Config.ReportsAPIWaitSeconds
	}
	req = append(req, pyjson.KV{Key: "format", Value: format}, pyjson.KV{Key: "filename", Value: filename},
		pyjson.KV{Key: "owner", Value: fmt.Sprintf("user:%d", p.User.ID)}, pyjson.KV{Key: "wait_seconds", Value: int64(wait)})
	outcome := "failure"
	var raw []byte
	err := func() error {
		cctx, cancel := context.WithTimeout(ctx, time.Duration(60+wait)*time.Second)
		defer cancel()
		res, body, err := d.Engine.JSON(cctx, http.MethodPost, path, req)
		if err != nil {
			return &reports.EngineDown{Err: err}
		}
		if res.StatusCode >= 400 {
			return reports.EngineFailure(res.StatusCode, body, func(m string) string { return m })
		}
		outcome, raw = "success", body
		return nil
	}()
	s.audit(ctx, "sql_script.export", outcome, append(pyjson.Object{{Key: "sql", Value: pystr.Cut(s.sql, 2000)},
		{Key: "format", Value: format}}, det...))
	if err != nil {
		s.answerErr(w, err)
		return
	}
	v, err := pyjson.Loads(string(raw))
	o, isObj := v.(pyjson.Object)
	task, has := o.Get("task_id")
	if err != nil || !isObj || !has { // resp.json()["task_id"]
		fail(d, w, "sql_scripts: the export's answer", fmt.Errorf("%.200s", raw))
		return
	}
	writeRaw(w, http.StatusOK, pyjson.Object{{Key: "task_id", Value: task}, {Key: "filename", Value: filename}})
}
