package api

// routes/scripts.py: saved scripts, Python or R code kept as an object of a folder, with
// their versions. Ported: every route but the schedule (croniter reads the cron: the route
// stays the Python gateway's) and, while OpenLineage is on, the change (a rename tells the
// catalog).

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

func init() {
	register("scripts", func(d *Deps) []server.Route {
		plain := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		body := func(method, pattern string, h scBodyHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: scWithBody(d, pattern, h)}
		}
		return []server.Route{
			plain(http.MethodGet, "/scripts", listScripts),
			plain(http.MethodGet, "/projects/{project_id}/scripts", listProjectScripts),
			plain(http.MethodGet, "/scripts/{script_id}", getScript),
			body(http.MethodPost, "/projects/{project_id}/scripts", createScript),
			body(http.MethodPatch, "/scripts/{script_id}", updateScript),
			plain(http.MethodPost, "/scripts/{script_id}/duplicate", duplicateScript),
			plain(http.MethodDelete, "/scripts/{script_id}", deleteScript),
			plain(http.MethodGet, "/scripts/{script_id}/versions", listScriptVersions),
			plain(http.MethodGet, "/scripts/{script_id}/versions/{version}", getScriptVersion),
			plain(http.MethodPost, "/scripts/{script_id}/versions/{version}/restore", restoreScriptVersion),
			plain(http.MethodPost, "/scripts/{script_id}/run", runScriptNow),
			plain(http.MethodGet, "/scripts/{script_id}/runs", listScriptRuns),
			plain(http.MethodGet, "/scripts/{script_id}/runs/{run_id}/preview", previewScriptOutput),
		}
	})
}

// ── shapes ────────────────────────────────────────────────────────────────────

type scriptOut struct {
	ID              int64   `json:"id"`
	Name            string  `json:"name"`
	Description     string  `json:"description"`
	ProjectID       int64   `json:"project_id"`
	EnvironmentID   int64   `json:"environment_id"`
	EnvironmentName *string `json:"environment_name"`
	Language        *string `json:"language"`
	CurrentVersion  int64   `json:"current_version"`
	RunSchedule     *string `json:"run_schedule"`
	RunScheduledBy  *int64  `json:"run_scheduled_by"`
	NextRunAt       *string `json:"next_run_at"`
	CreatedAt       string  `json:"created_at"`
	UpdatedAt       string  `json:"updated_at"`
	Editable        bool    `json:"editable"`
	Runnable        bool    `json:"runnable"`
	LastRunStatus   *string `json:"last_run_status"`
	LastRunAt       *string `json:"last_run_at"`
}

type scriptInput struct {
	DatasourceID int64    `json:"datasource_id"`
	Alias        string   `json:"alias"`
	Name         *string  `json:"name"`
	Folder       *string  `json:"folder"`
	ProjectID    *int64   `json:"project_id"`
	Columns      []string `json:"columns"`
	State        string   `json:"state"`
}

type scriptDetail struct {
	scriptOut
	Content pyjson.Object `json:"content"`
	Inputs  []scriptInput `json:"inputs"`
}

// scriptsOn is routes/scripts.py _on: the scripting environments can be built here.
func scriptsOn() bool { return scriptcfg.Get().DisabledReason() == "" }

// scEnvironment is a script_environments row, the columns the routes read.
type scEnvironment struct {
	ID                int64
	Name, Description string
	Language          string
	Enabled           bool
	CurrentVersionID  *int64
	CPURequest        string
	CPULimit          string
	MemoryRequest     string
	MemoryLimit       string
	MaxTimeoutSeconds int64
	UpdatedAt         time.Time
}

const scEnvColumns = `id, name, description, language, enabled, current_version_id, cpu_request, cpu_limit, memory_request,
	memory_limit, max_timeout_seconds, updated_at`

func scScanEnv(row pgx.Row) (*scEnvironment, error) {
	var e scEnvironment
	if err := row.Scan(&e.ID, &e.Name, &e.Description, &e.Language, &e.Enabled, &e.CurrentVersionID, &e.CPURequest, &e.CPULimit,
		&e.MemoryRequest, &e.MemoryLimit, &e.MaxTimeoutSeconds, &e.UpdatedAt); err != nil {
		return nil, err
	}
	return &e, nil
}

// scGetEnv is session.get(ScriptEnvironment, id): nil when there is none.
func scGetEnv(ctx context.Context, q perm.Querier, id int64) (*scEnvironment, error) {
	e, err := scScanEnv(q.QueryRow(ctx, `SELECT `+scEnvColumns+` FROM script_environments WHERE id = $1`, id))
	if scNoRows(err) {
		return nil, nil
	}
	return e, err
}

func (e *scEnvironment) checks() savedscripts.Environment {
	return savedscripts.Environment{ID: e.ID, Name: e.Name, Language: e.Language, MaxTimeout: e.MaxTimeoutSeconds}
}

func scriptOutOf(ctx context.Context, c *perm.Checker, q perm.Querier, s *savedscripts.Script, env *scEnvironment) (scriptOut, error) {
	out := scriptOut{ID: s.ID, Name: s.Name, Description: s.Description, ProjectID: s.ProjectID, EnvironmentID: s.EnvironmentID,
		CurrentVersion: s.CurrentVersion, RunSchedule: s.RunSchedule, RunScheduledBy: s.RunScheduledBy,
		NextRunAt: isoPtr(s.NextRunAt), CreatedAt: pyjson.Isoformat(s.CreatedAt), UpdatedAt: pyjson.Isoformat(s.UpdatedAt)}
	if env != nil {
		out.EnvironmentName, out.Language = &env.Name, &env.Language
	}
	var err error
	if out.Editable, err = c.Has(ctx, s.ProjectID, perm.Edit); err != nil {
		return out, err
	}
	out.Runnable, err = c.Has(ctx, s.ProjectID, perm.Run)
	return out, err
}

// scriptInputs is _inputs: each declared input as this reader may know it.
func scriptInputs(ctx context.Context, c *perm.Checker, q perm.Querier, content pyjson.Object) ([]scriptInput, error) {
	out := []scriptInput{}
	list, _ := scGet(content, "inputs").([]any)
	var paths map[int64]string
	for _, it := range list {
		item, _ := it.(pyjson.Object)
		a := ""
		if v := scGet(item, "alias"); sqlscripts.Truthy(v) {
			a = pyjson.Str(v)
		}
		id, isInt := scGet(item, "datasource_id").(int64)
		var ds *sqlscripts.Datasource
		if isInt {
			var err error
			ds, err = sqlscripts.ScanDatasource(q.QueryRow(ctx, `SELECT `+sqlscripts.DatasourceColumns+` FROM datasources WHERE id = $1`, id))
			if scNoRows(err) {
				ds = nil
			} else if err != nil {
				return nil, err
			}
		}
		if ds == nil {
			out = append(out, scriptInput{DatasourceID: id, Alias: a, Columns: []string{}, State: "missing"})
			continue
		}
		ok, err := c.Has(ctx, ds.ProjectID, perm.View)
		if err != nil {
			return nil, err
		}
		if !ok {
			out = append(out, scriptInput{DatasourceID: ds.ID, Alias: a, Columns: []string{}, State: "hidden"})
			continue
		}
		if paths == nil {
			folders, err := sqlscripts.Folders(ctx, q)
			if err != nil {
				return nil, err
			}
			paths = sqlscripts.Paths(folders, " / ")
		}
		cols, err := scColumnNames(ds.Columns)
		if err != nil {
			return nil, err
		}
		state := "ok"
		if ds.Key == "" {
			state = "empty"
		}
		folder, project, name := paths[ds.ProjectID], ds.ProjectID, ds.Name
		out = append(out, scriptInput{DatasourceID: ds.ID, Alias: a, Name: &name, Folder: &folder, ProjectID: &project,
			Columns: cols, State: state})
	}
	return out, nil
}

// scColumnNames is saved_script_runs._columns: the names of a datasource's columns. A name
// that is not a text fails the answer's validation (a 500 in Python).
func scColumnNames(raw string) ([]string, error) {
	if raw == "" {
		raw = "[]"
	}
	out := []string{}
	v, err := pyjson.Loads(raw)
	if err != nil {
		return out, nil
	}
	switch list := v.(type) {
	case []any:
		for _, c := range list {
			o, ok := c.(pyjson.Object)
			if !ok {
				continue
			}
			n, ok := scGet(o, "name").(string)
			if !ok {
				return nil, fmt.Errorf("a column's name is not a text: %s", raw)
			}
			out = append(out, n)
		}
	case pyjson.Object, string:
	default:
		return nil, fmt.Errorf("the columns are not iterable: %s", raw)
	}
	return out, nil
}

func scriptDetailOf(ctx context.Context, c *perm.Checker, q perm.Querier, s *savedscripts.Script) (*scriptDetail, error) {
	content, err := savedscripts.ContentOf(ctx, q, s, 0)
	if err != nil {
		return nil, err
	}
	env, err := scGetEnv(ctx, q, s.EnvironmentID)
	if err != nil {
		return nil, err
	}
	out, err := scriptOutOf(ctx, c, q, s, env)
	if err != nil {
		return nil, err
	}
	inputs, err := scriptInputs(ctx, c, q, content)
	if err != nil {
		return nil, err
	}
	return &scriptDetail{scriptOut: out, Content: content, Inputs: inputs}, nil
}

func scriptSendDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, id int64, status int) {
	ctx := r.Context()
	s, err := savedscripts.Get(ctx, d.DB, id)
	if err != nil || s == nil {
		fail(d, w, "scripts: reading the script again", fmt.Errorf("script %d: %v", id, err))
		return
	}
	det, err := scriptDetailOf(ctx, perm.For(d.DB, &p.User), d.DB, s)
	if err != nil {
		fail(d, w, "scripts: the script's detail", err)
		return
	}
	server.JSON(w, status, det)
}

func scriptVisible(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, p *auth.Principal, id int64) (*savedscripts.Script, bool) {
	s, refusal, err := savedscripts.GetVisible(r.Context(), c, d.DB, p.User.ID, id)
	if err != nil {
		fail(d, w, "scripts: reading the script", err)
		return nil, false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return nil, false
	}
	return s, true
}

// scriptNeedOn is _need_on.
func scriptNeedOn(w http.ResponseWriter) bool {
	if !scriptsOn() {
		detail(w, http.StatusConflict, "Scripts need the scripting environments (Kubernetes)")
		return false
	}
	return true
}

// ── lists ─────────────────────────────────────────────────────────────────────

// scriptsMany is _many: the scripts with their environments and their latest own run.
func scriptsMany(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, p *auth.Principal, projects []int64) {
	ctx := r.Context()
	rows, err := savedscripts.Visible(ctx, c, d.DB, p.User.ID, projects)
	if err != nil {
		fail(d, w, "scripts: the list", err)
		return
	}
	out := make([]scriptOut, 0, len(rows))
	for i := range rows {
		env, err := scGetEnv(ctx, d.DB, rows[i].EnvironmentID)
		if err != nil {
			fail(d, w, "scripts: an environment", err)
			return
		}
		o, err := scriptOutOf(ctx, c, d.DB, &rows[i], env)
		if err != nil {
			fail(d, w, "scripts: a script of the list", err)
			return
		}
		// the latest of each script's own runs
		var status string
		var at time.Time
		err = d.DB.QueryRow(ctx, `SELECT status, started_at FROM runs WHERE id = (SELECT max(id) FROM runs
			WHERE kind = 'orchestration' AND saved_script_id = $1)`, rows[i].ID).Scan(&status, &at)
		if err == nil {
			o.LastRunStatus, o.LastRunAt = &status, isoPtr(&at)
		} else if !scNoRows(err) {
			fail(d, w, "scripts: the latest run", err)
			return
		}
		out = append(out, o)
	}
	server.JSON(w, http.StatusOK, out)
}

// listScripts is GET /scripts (list_scripts).
func listScripts(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if !scriptsOn() {
		server.JSON(w, http.StatusOK, []any{})
		return
	}
	scriptsMany(d, w, r, perm.For(d.DB, &p.User), p, nil)
}

// listProjectScripts is GET /projects/{project_id}/scripts (list_project_scripts).
func listProjectScripts(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, ids[0])
	if err != nil {
		fail(d, w, "scripts: the folder", err)
		return
	}
	if !exists {
		detail(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, ids[0], perm.View) {
		return
	}
	if !scriptsOn() {
		server.JSON(w, http.StatusOK, []any{})
		return
	}
	scriptsMany(d, w, r, c, p, []int64{ids[0]})
}

// getScript is GET /scripts/{script_id} (get_script).
func getScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id")
	if !ok {
		return
	}
	if !scriptsOn() {
		detail(w, http.StatusNotFound, "Script not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	det, err := scriptDetailOf(r.Context(), c, d.DB, s)
	if err != nil {
		fail(d, w, "scripts: the script's detail", err)
		return
	}
	server.JSON(w, http.StatusOK, det)
}

// ── create, change, copy, delete ──────────────────────────────────────────────

// scriptEnvironment is _environment: one this person may use, or the 422.
func scriptEnvironment(ctx context.Context, c *perm.Checker, q perm.Querier, userID, id int64) (*scEnvironment, *auth.Refusal, error) {
	env, err := scGetEnv(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	if env != nil {
		ok, err := scriptnodes.CanUse(ctx, c, q, userID, env.ID)
		if err != nil {
			return nil, nil, err
		}
		if ok {
			return env, nil, nil
		}
	}
	return nil, &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "Choose a scripting environment you may use"}, nil
}

// scObjectName is _name of a versioned object: stripped, not empty, free in the folder.
func scObjectName(ctx context.Context, q perm.Querier, table, noun, what string, project int64, name string, exclude int64) (string, *auth.Refusal, error) {
	name = pyd.Strip(name)
	if name == "" {
		return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("The %s needs a name", noun)}, nil
	}
	var clash int64
	err := q.QueryRow(ctx, fmt.Sprintf(`SELECT id FROM %s WHERE project_id = $1 AND name = $2`, table), project, name).Scan(&clash)
	if err != nil && !scNoRows(err) {
		return "", nil, err
	}
	if err == nil && clash != exclude {
		return "", &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a %s '%s' in this folder", what, name)}, nil
	}
	return name, nil, nil
}

// scAnswer writes a refusal or a failure; true when there was one.
func scAnswer(d *Deps, w http.ResponseWriter, what string, refusal *auth.Refusal, err error) bool {
	if err != nil {
		fail(d, w, what, err)
		return true
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return true
	}
	return false
}

// createScript is POST /projects/{project_id}/scripts (create_script).
func createScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	pid := params.Path("project_id", r.PathValue("project_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.ScriptCreate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptNeedOn(w) {
		return
	}
	project, ok := pid.(int64)
	if !ok {
		project = -1
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, project)
	if err != nil {
		fail(d, w, "scripts: the folder", err)
		return
	}
	if !exists {
		detail(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, project, perm.Edit) {
		return
	}
	envID, _ := in.Get("environment_id").(int64)
	env, refusal, err := scriptEnvironment(ctx, c, d.DB, p.User.ID, envID)
	if scAnswer(d, w, "scripts: the environment", refusal, err) {
		return
	}
	name, refusal, err := scObjectName(ctx, d.DB, "saved_scripts", "script", "script", project, in.Get("name").(string), 0)
	if scAnswer(d, w, "scripts: the name", refusal, err) {
		return
	}
	var content any = savedscripts.EmptyContent()
	if raw := scFromPyd(in.Get("content")); sqlscripts.Truthy(raw) {
		content = raw
	}
	checked, refusal, err := savedscripts.CheckContent(ctx, c, d.DB, name, content, env.checks(), nil)
	if scAnswer(d, w, "scripts: the content", refusal, err) {
		return
	}
	var id int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id,
			current_version, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 0, $6, $6) RETURNING id`,
			name, pyd.Strip(in.Get("description").(string)), project, p.User.ID, env.ID, now).Scan(&id); err != nil {
			return err
		}
		v, err := savedscripts.AddVersion(ctx, tx, "saved_scripts", "saved_script_versions", "script_id", id, checked, "created", p.User.ID)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE saved_scripts SET current_version = $1 WHERE id = $2`, v, id)
		return err
	})
	if err != nil {
		fail(d, w, "scripts: creating the script", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.create", TargetType: scPtrStr("saved_script"),
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: project}, {Key: "environment", Value: env.Name}}})
	scriptSendDetail(d, w, r, p, id, http.StatusCreated)
}

// scriptOffElsewhere is _schedules_off_elsewhere: the flows and groups that run the script,
// scheduled by someone else, switched off in `tx`. Returns what to record.
func scriptOffElsewhere(ctx context.Context, tx pgx.Tx, user *auth.User, s *savedscripts.Script, name string, reasons []string) ([]scOffRecord, error) {
	if len(reasons) == 0 {
		return nil, nil
	}
	flows, groups, err := savedscripts.Using(ctx, tx, s.ID)
	if err != nil {
		return nil, err
	}
	var off []scOffRecord
	for _, part := range []struct {
		list         []savedscripts.Scheduled
		action, kind string
		table        string
	}{{flows, "flow.schedule", "flow", "flows"}, {groups, "flow_group.schedule", "flow_group", "flow_groups"}} {
		done := map[int64]bool{}
		for _, o := range part.list {
			if done[o.ID] || o.RunSchedule == nil || *o.RunSchedule == "" || o.RunScheduledBy == nil || *o.RunScheduledBy == user.ID {
				continue
			}
			done[o.ID] = true
			why := pyjson.Object{{Key: "turned_off", Value: true}, {Key: "author", Value: *o.RunScheduledBy},
				{Key: "reason", Value: fmt.Sprintf("the saved script '%s' it runs changed: %s", name, strings.Join(reasons, "; "))}}
			if _, err := tx.Exec(ctx, fmt.Sprintf(`UPDATE %s SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL
				WHERE id = $1`, part.table), o.ID); err != nil {
				return nil, err
			}
			off = append(off, scOffRecord{part.action, part.kind, o.ID, o.Name, why})
		}
	}
	return off, nil
}

type scOffRecord struct {
	action, kind string
	id           int64
	name         string
	why          pyjson.Object
}

func scRecordOffElsewhere(ctx context.Context, d *Deps, user *auth.User, off []scOffRecord) {
	for _, o := range off {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: o.action, TargetType: scPtrStr(o.kind),
			TargetID: scPtrInt(o.id), TargetLabel: scPtrStr(o.name), Detail: o.why})
	}
}

// updateScript is PATCH /scripts/{script_id} (update_script).
func updateScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	if scOpenLineageOn() {
		scForward(d, w, r)
		return
	}
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.ScriptUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptNeedOn(w) {
		return
	}
	scriptID, ok := sid.(int64)
	if !ok {
		scriptID = -1
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, scriptID)
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	changes := pyjson.Object{}
	target := s.ProjectID
	if v := in.Get("project_id"); v != nil {
		dest, ok := v.(int64)
		if !ok {
			dest = -1
		}
		if dest != s.ProjectID {
			exists, err := scFolderExists(ctx, d.DB, dest)
			if err != nil {
				fail(d, w, "scripts: the folder", err)
				return
			}
			if !exists {
				detail(w, http.StatusNotFound, "Destination folder not found")
				return
			}
			if !scEnsure(d, w, ctx, c, dest, perm.Edit) {
				return
			}
			target = dest
		}
	}
	wanted := s.Name
	if v := in.Get("name"); v != nil {
		wanted = v.(string)
	}
	name, refusal, err := scObjectName(ctx, d.DB, "saved_scripts", "script", "script", target, wanted, s.ID)
	if scAnswer(d, w, "scripts: the name", refusal, err) {
		return
	}
	if name != s.Name {
		changes = append(changes, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: s.Name}, {Key: "to", Value: name}}})
	}
	if target != s.ProjectID {
		changes = append(changes, pyjson.KV{Key: "project_id", Value: pyjson.Object{{Key: "from", Value: s.ProjectID}, {Key: "to", Value: target}}})
	}
	env, err := scGetEnv(ctx, d.DB, s.EnvironmentID)
	if err != nil {
		fail(d, w, "scripts: the environment", err)
		return
	}
	envChanged := false
	if v := in.Get("environment_id"); v != nil {
		if id, _ := v.(int64); id != s.EnvironmentID {
			env, refusal, err = scriptEnvironment(ctx, c, d.DB, p.User.ID, id)
			if scAnswer(d, w, "scripts: the environment", refusal, err) {
				return
			}
			changes = append(changes, pyjson.KV{Key: "environment", Value: env.Name})
			envChanged = true
		}
	}
	if env == nil {
		detail(w, http.StatusUnprocessableEntity, "Its scripting environment no longer exists: choose another")
		return
	}
	current, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		fail(d, w, "scripts: the content", err)
		return
	}
	var newContent pyjson.Object
	raw := in.Get("content")
	if raw != nil || envChanged {
		var content any = current
		if raw != nil {
			content = scFromPyd(raw)
		}
		checked, refusal, err := savedscripts.CheckContent(ctx, c, d.DB, name, content, env.checks(), current)
		if scAnswer(d, w, "scripts: the content", refusal, err) {
			return
		}
		if !pyjson.Equal(checked, current) {
			newContent = checked
		}
	}
	var reasons []string
	if newContent != nil {
		reasons = savedscripts.Widens(current, newContent)
	}
	if env.ID != s.EnvironmentID {
		reasons = append(reasons, "it runs in another environment")
	}
	if target != s.ProjectID {
		reasons = append(reasons, "it was moved to another folder")
	}
	mine := scScheduleOff(s.RunSchedule, s.RunScheduledBy, &p.User, reasons)
	var elsewhereReasons []string
	for _, x := range reasons {
		if !strings.Contains(x, "moved") {
			elsewhereReasons = append(elsewhereReasons, x)
		}
	}
	description, descChanged := s.Description, false
	if v := in.Get("description"); v != nil && pyd.Strip(v.(string)) != s.Description {
		description, descChanged = pyd.Strip(v.(string)), true
	}
	if len(changes) == 0 && newContent == nil && !descChanged {
		// nothing to save; a schedule switched off in the session is not committed either
		scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
		return
	}
	var off []scOffRecord
	var version int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if off, err = scriptOffElsewhere(ctx, tx, &p.User, s, name, elsewhereReasons); err != nil {
			return err
		}
		current := s.CurrentVersion
		if newContent != nil {
			if version, err = savedscripts.AddVersion(ctx, tx, "saved_scripts", "saved_script_versions", "script_id", s.ID, newContent, "edited", p.User.ID); err != nil {
				return err
			}
			current = version
		}
		schedule, by, next := s.RunSchedule, s.RunScheduledBy, s.NextRunAt
		if mine != nil {
			schedule, by, next = nil, nil, nil
		}
		_, err = tx.Exec(ctx, `UPDATE saved_scripts SET name = $1, project_id = $2, environment_id = $3, description = $4,
			current_version = $5, run_schedule = $6, run_scheduled_by = $7, next_run_at = $8, updated_at = $9 WHERE id = $10`,
			name, target, env.ID, description, current, schedule, by, next, time.Now().UTC(), s.ID)
		return err
	})
	if err != nil {
		fail(d, w, "scripts: changing the script", err)
		return
	}
	if descChanged {
		changes = append(changes, pyjson.KV{Key: "description", Value: true})
	}
	if newContent != nil {
		changes = append(changes, pyjson.KV{Key: "version", Value: version})
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.update", TargetType: scPtrStr("saved_script"),
		TargetID: scPtrInt(s.ID), TargetLabel: &name, Detail: changes})
	if mine != nil {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.schedule", TargetType: scPtrStr("saved_script"),
			TargetID: scPtrInt(s.ID), TargetLabel: &name, Detail: mine})
	}
	scRecordOffElsewhere(ctx, d, &p.User, off)
	scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
}

// duplicateScript is POST /scripts/{script_id}/duplicate (duplicate_script).
func duplicateScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id")
	if !ok || !scriptNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	src, ok := scriptVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, src.ProjectID, perm.Edit) {
		return
	}
	var id int64
	var name string
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if name, err = copynames.Free(ctx, tx, "saved_scripts", src.ProjectID, src.Name); err != nil {
			return err
		}
		content, err := savedscripts.ContentOf(ctx, tx, src, 0)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id,
			current_version, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, 0, $6, $6) RETURNING id`,
			name, src.Description, src.ProjectID, p.User.ID, src.EnvironmentID, now).Scan(&id); err != nil {
			return err
		}
		v, err := savedscripts.AddVersion(ctx, tx, "saved_scripts", "saved_script_versions", "script_id", id, content, "created", p.User.ID)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE saved_scripts SET current_version = $1 WHERE id = $2`, v, id)
		return err
	})
	if copynames.Clash(err) {
		detail(w, http.StatusConflict, copynames.ClashDetail)
		return
	}
	if err != nil {
		fail(d, w, "scripts: the copy", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.create", TargetType: scPtrStr("saved_script"),
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "duplicate_of", Value: src.ID}}})
	scriptSendDetail(d, w, r, p, id, http.StatusCreated)
}

// deleteScript is DELETE /scripts/{script_id} (delete_script): its runs stay in the
// history, no longer attached to it.
func deleteScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	var status int
	var why any
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(74610003, $1)`, s.ID); err != nil {
			return err
		}
		var live int64
		err := tx.QueryRow(ctx, `SELECT id FROM runs WHERE saved_script_id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`, s.ID).Scan(&live)
		if err == nil {
			status, why = http.StatusConflict, "The script is queued or running: delete it once it has finished"
			return scRefused
		}
		if !scNoRows(err) {
			return err
		}
		flows, groups, err := savedscripts.References(ctx, tx, s.ID)
		if err != nil {
			return err
		}
		if len(flows) > 0 || len(groups) > 0 {
			var names []string
			for _, f := range flows {
				names = append(names, "flow «"+f+"»")
			}
			for _, g := range groups {
				names = append(names, "flow group «"+g+"»")
			}
			shown := names
			if len(shown) > 10 {
				shown = shown[:10]
			}
			more := ""
			if len(names) > 10 {
				more = " …"
			}
			status, why = http.StatusConflict, pyjson.Object{
				{Key: "message", Value: "The script is used by " + strings.Join(shown, ", ") + more + ": remove it from there first"},
				{Key: "flows", Value: flows}, {Key: "groups", Value: groups}}
			return scRefused
		}
		if _, err := tx.Exec(ctx, `UPDATE runs SET saved_script_id = NULL WHERE saved_script_id = $1`, s.ID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM saved_script_versions WHERE script_id = $1`, s.ID); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM saved_scripts WHERE id = $1`, s.ID)
		return err
	})
	if status != 0 {
		scRefuseWith(w, status, why)
		return
	}
	if err != nil {
		fail(d, w, "scripts: deleting the script", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.delete", TargetType: scPtrStr("saved_script"),
		TargetID: scPtrInt(ids[0]), TargetLabel: scPtrStr(s.Name)})
	returnsNone(w)
}

// ── versions ──────────────────────────────────────────────────────────────────

// listScriptVersions is GET /scripts/{script_id}/versions (list_versions).
func listScriptVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id")
	if !ok {
		return
	}
	s, ok := scriptVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	out, err := scVersionsOf(r.Context(), d.DB, "saved_script_versions", "script_id", s.ID, s.CurrentVersion)
	if err != nil {
		fail(d, w, "scripts: the versions", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// getScriptVersion is GET /scripts/{script_id}/versions/{version} (get_version): 0 is the
// current one.
func getScriptVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	s, ok := scriptVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	exists, _, err := savedscripts.VersionRow(ctx, d.DB, "saved_script_versions", "script_id", s.ID, s.CurrentVersion, ids[1])
	if err != nil {
		fail(d, w, "scripts: the version", err)
		return
	}
	if !exists {
		detail(w, http.StatusNotFound, "Version not found")
		return
	}
	content, err := savedscripts.ContentOf(ctx, d.DB, s, ids[1])
	if err != nil {
		fail(d, w, "scripts: the version's content", err)
		return
	}
	server.JSON(w, http.StatusOK, content)
}

// restoreScriptVersion is POST /scripts/{script_id}/versions/{version}/restore (restore_version).
func restoreScriptVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id", "version")
	if !ok || !scriptNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	version := ids[1]
	exists, _, err := savedscripts.VersionRow(ctx, d.DB, "saved_script_versions", "script_id", s.ID, s.CurrentVersion, version)
	if err != nil {
		fail(d, w, "scripts: the version", err)
		return
	}
	if !exists {
		detail(w, http.StatusNotFound, "Version not found")
		return
	}
	if version == s.CurrentVersion {
		scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
		return
	}
	env, err := scGetEnv(ctx, d.DB, s.EnvironmentID)
	if err != nil {
		fail(d, w, "scripts: the environment", err)
		return
	}
	if env == nil {
		detail(w, http.StatusUnprocessableEntity, "Its scripting environment no longer exists: choose another")
		return
	}
	old, err := savedscripts.ContentOf(ctx, d.DB, s, version)
	if err != nil {
		fail(d, w, "scripts: the version's content", err)
		return
	}
	current, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		fail(d, w, "scripts: the content", err)
		return
	}
	content, refusal, err := savedscripts.CheckContent(ctx, c, d.DB, s.Name, old, env.checks(), current)
	if scAnswer(d, w, "scripts: the content", refusal, err) {
		return
	}
	widened := savedscripts.Widens(current, content)
	mine := scScheduleOff(s.RunSchedule, s.RunScheduledBy, &p.User, widened)
	var off []scOffRecord
	var newVersion int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if off, err = scriptOffElsewhere(ctx, tx, &p.User, s, s.Name, widened); err != nil {
			return err
		}
		if newVersion, err = savedscripts.AddVersion(ctx, tx, "saved_scripts", "saved_script_versions", "script_id", s.ID, content,
			fmt.Sprintf("restored from v%d", version), p.User.ID); err != nil {
			return err
		}
		schedule, by, next := s.RunSchedule, s.RunScheduledBy, s.NextRunAt
		if mine != nil {
			schedule, by, next = nil, nil, nil
		}
		_, err = tx.Exec(ctx, `UPDATE saved_scripts SET current_version = $1, run_schedule = $2, run_scheduled_by = $3,
			next_run_at = $4, updated_at = $5 WHERE id = $6`, newVersion, schedule, by, next, time.Now().UTC(), s.ID)
		return err
	})
	if err != nil {
		fail(d, w, "scripts: restoring the version", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.restore", TargetType: scPtrStr("saved_script"),
		TargetID: scPtrInt(s.ID), TargetLabel: scPtrStr(s.Name),
		Detail: pyjson.Object{{Key: "from", Value: version}, {Key: "version", Value: newVersion}}})
	if mine != nil {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.schedule", TargetType: scPtrStr("saved_script"),
			TargetID: scPtrInt(s.ID), TargetLabel: scPtrStr(s.Name), Detail: mine})
	}
	scRecordOffElsewhere(ctx, d, &p.User, off)
	scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
}

// ── running ───────────────────────────────────────────────────────────────────

// runScriptNow is POST /scripts/{script_id}/run (run_script_now): a run of the current
// version queued with the caller's authority; an orchestrator takes it from the queue.
func runScriptNow(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "script_id")
	if !ok || !scriptNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Run) {
		return
	}
	content, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		fail(d, w, "scripts: the content", err)
		return
	}
	if !sqlscripts.Truthy(scGet(content, "files")) {
		detail(w, http.StatusUnprocessableEntity, "The script has no code yet: write it and save it first")
		return
	}
	var status int
	var why string
	var runID int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(74610003, $1)`, s.ID); err != nil {
			return err
		}
		var one int64
		if err := tx.QueryRow(ctx, `SELECT id FROM saved_scripts WHERE id = $1`, s.ID).Scan(&one); scNoRows(err) {
			status, why = http.StatusNotFound, "Script not found" // deleted while waiting in line
			return scRefused
		} else if err != nil {
			return err
		}
		err := tx.QueryRow(ctx, `SELECT id FROM runs WHERE kind = 'orchestration' AND saved_script_id = $1
			AND status IN ('PENDING', 'STARTED') LIMIT 1`, s.ID).Scan(&one)
		if err == nil {
			status, why = http.StatusConflict, "The script is already queued or running: it can be run again once it has finished"
			return scRefused
		}
		if !scNoRows(err) {
			return err
		}
		return tx.QueryRow(ctx, `INSERT INTO runs (kind, saved_script_id, task_id, status, launched_by, trigger_type, engine_mode,
			input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ('orchestration', $1, '', 'PENDING', $2, 'manual', 'production', '', '', '', '', false, '[]', $3) RETURNING id`,
			s.ID, p.User.ID, time.Now().UTC()).Scan(&runID)
	})
	if status != 0 {
		detail(w, status, why)
		return
	}
	if err != nil {
		fail(d, w, "scripts: queueing the run", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.run", TargetType: scPtrStr("saved_script"),
		TargetID: scPtrInt(s.ID), TargetLabel: scPtrStr(s.Name), Detail: pyjson.Object{{Key: "run_id", Value: runID},
			{Key: "trigger", Value: "manual"}, {Key: "version", Value: s.CurrentVersion}}})
	server.JSON(w, http.StatusAccepted, pyjson.Object{{Key: "status", Value: "started"}, {Key: "script_id", Value: s.ID},
		{Key: "run_id", Value: runID}})
}

type scriptRunOut struct {
	RunID         int64   `json:"run_id"`
	Status        string  `json:"status"`
	TriggerType   string  `json:"trigger_type"`
	StartedAt     *string `json:"started_at"`
	FinishedAt    *string `json:"finished_at"`
	Error         *string `json:"error"`
	LaunchedBy    *string `json:"launched_by"`
	ScriptRunID   *int64  `json:"script_run_id"`
	ScriptVersion any     `json:"script_version"`
	Attempts      any     `json:"attempts"`
	ExitCode      any     `json:"exit_code"`
	Log           any     `json:"log"`
	LogExpired    bool    `json:"log_expired"`
	Rows          *int64  `json:"rows"`
	Output        bool    `json:"output"`
	Columns       any     `json:"columns"`
	DatasourceID  *int64  `json:"datasource_id"`
}

// listScriptRuns is GET /scripts/{script_id}/runs (list_script_runs): newest first.
func listScriptRuns(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	lim := params.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(100), 20)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	scriptID, ok := sid.(int64)
	if !ok {
		scriptID = -1
	}
	ctx := r.Context()
	s, ok := scriptVisible(d, w, r, perm.For(d.DB, &p.User), p, scriptID)
	if !ok {
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT h.id, h.status, h.trigger_type, h.started_at, h.finished_at, h.error, u.email
		FROM runs h LEFT JOIN users u ON u.id = h.launched_by
		WHERE h.saved_script_id = $1 AND h.kind = 'orchestration' ORDER BY h.started_at DESC, h.id DESC LIMIT $2`, s.ID, lim.(int64))
	if err != nil {
		fail(d, w, "scripts: the runs", err)
		return
	}
	out := []scriptRunOut{}
	for rows.Next() {
		var o scriptRunOut
		var started time.Time
		var finished *time.Time
		if err := rows.Scan(&o.RunID, &o.Status, &o.TriggerType, &started, &finished, &o.Error, &o.LaunchedBy); err != nil {
			rows.Close()
			fail(d, w, "scripts: a run", err)
			return
		}
		o.StartedAt, o.FinishedAt, o.Log, o.Columns = isoPtr(&started), isoPtr(finished), "", []any{}
		out = append(out, o)
	}
	rows.Close()
	for i := range out {
		var childID int64
		var status string
		var script *string
		var rowsWritten, dsID *int64
		var outputKey string
		// children by parent: the last one read wins, as a dict built from the rows
		crow, err := d.DB.Query(ctx, `SELECT id, status, script, rows_written, datasource_id, output_key FROM runs
			WHERE parent_run_id = $1 AND kind = 'script'`, out[i].RunID)
		if err != nil {
			fail(d, w, "scripts: a run's child", err)
			return
		}
		found := false
		for crow.Next() {
			if err := crow.Scan(&childID, &status, &script, &rowsWritten, &dsID, &outputKey); err != nil {
				crow.Close()
				fail(d, w, "scripts: a run's child", err)
				return
			}
			found = true
		}
		crow.Close()
		if !found {
			continue
		}
		info := pyjson.Object{}
		text := "{}"
		if script != nil && *script != "" {
			text = *script
		}
		if v, err := pyjson.Loads(text); err == nil {
			if o, ok := v.(pyjson.Object); ok {
				info = o
			} else {
				fail(d, w, "scripts: a run's script is not an object", fmt.Errorf("run %d", childID))
				return
			}
		}
		o := &out[i]
		o.ScriptRunID, o.ScriptVersion = scPtrInt(childID), scGet(info, "script_version")
		o.Attempts, o.ExitCode = scGet(info, "attempts"), scGet(info, "exit_code")
		if l := scGet(info, "log"); sqlscripts.Truthy(l) {
			o.Log = l
		}
		o.LogExpired = sqlscripts.Truthy(scGet(info, "log_expired"))
		o.Rows, o.DatasourceID = rowsWritten, dsID
		if cols := scGet(info, "columns"); sqlscripts.Truthy(cols) {
			o.Columns = cols
		}
		o.Output = status == "SUCCESS" && outputKey != ""
	}
	server.JSON(w, http.StatusOK, out)
}

// previewScriptOutput is GET /scripts/{script_id}/runs/{run_id}/preview: the first rows of
// what a run of the script wrote at TABULARIA_OUTPUT.
func previewScriptOutput(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	rid := params.Path("run_id", r.PathValue("run_id"), pyd.Int())
	lim := params.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(1000), 100)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	scriptID, ok := sid.(int64)
	if !ok {
		scriptID = -1
	}
	runID, ok := rid.(int64)
	if !ok {
		runID = -1
	}
	ctx := r.Context()
	s, ok := scriptVisible(d, w, r, perm.For(d.DB, &p.User), p, scriptID)
	if !ok {
		return
	}
	var status, bucket, key string
	err := d.DB.QueryRow(ctx, `SELECT status, output_bucket, output_key FROM runs WHERE parent_run_id = $1 AND kind = 'script'
		AND saved_script_id = $2`, runID, s.ID).Scan(&status, &bucket, &key)
	if err != nil && !scNoRows(err) {
		fail(d, w, "scripts: the run", err)
		return
	}
	if scNoRows(err) || status != "SUCCESS" || key == "" {
		detail(w, http.StatusNotFound, "This run left no output")
		return
	}
	resp, raw, err := d.Engine.JSON(ctx, http.MethodPost, "/tasks/preview", pyjson.Object{{Key: "bucket", Value: bucket},
		{Key: "input_key", Value: key}, {Key: "operations", Value: []any{}}, {Key: "limit", Value: lim}, {Key: "no_cache", Value: true}})
	if err != nil {
		fail(d, w, "scripts: the engine", err)
		return
	}
	if resp.StatusCode >= 400 {
		why, ok := scEngineDetail(raw)
		if !ok {
			fail(d, w, "scripts: the engine's refusal is not an object", nil)
			return
		}
		detail(w, http.StatusBadGateway, "The output could not be read: "+scClip(why, 500))
		return
	}
	scRelay(w, raw)
}
