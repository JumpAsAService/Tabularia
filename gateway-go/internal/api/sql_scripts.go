package api

// routes/sql_scripts.py: SQL kept as an object of a folder, with its versions. Ported: the
// lists, the catalog, a script and its versions, a copy, deleting it, restoring a version.
// The runs on the catalog read the SQL with sqlglot (services/sql_scripts.resolve), which the
// port leaves to the Python gateway: «Run», «Export», «Refresh now» and the schedule stay
// there, with the routes whose bodies wait for the shared validation.

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
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

func init() {
	register("sql_scripts", func(d *Deps) []server.Route {
		route := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route(http.MethodGet, "/sql-scripts", listSQLScripts),
			route(http.MethodGet, "/projects/{project_id}/sql-scripts", listProjectSQLScripts),
			route(http.MethodGet, "/sql-scripts/catalog", sqlCatalog),
			route(http.MethodGet, "/sql-scripts/{script_id}", getSQLScript),
			route(http.MethodPost, "/sql-scripts/{script_id}/duplicate", duplicateSQLScript),
			route(http.MethodDelete, "/sql-scripts/{script_id}", deleteSQLScript),
			route(http.MethodGet, "/sql-scripts/{script_id}/versions", listSQLScriptVersions),
			route(http.MethodGet, "/sql-scripts/{script_id}/versions/{version}", getSQLScriptVersion),
			route(http.MethodPost, "/sql-scripts/{script_id}/versions/{version}/restore", restoreSQLScriptVersion),
		}
	})
}

// ── shapes ────────────────────────────────────────────────────────────────────

type sqlConnectionRef struct {
	ID     int64  `json:"id"`
	Name   string `json:"name"` // empty when the connection no longer exists
	DBType string `json:"db_type"`
	Usable bool   `json:"usable"` // CONNECT on its folder: this person can run the script
}

type sqlScriptOut struct {
	ID             int64             `json:"id"`
	Name           string            `json:"name"`
	Description    string            `json:"description"`
	ProjectID      int64             `json:"project_id"`
	CurrentVersion int64             `json:"current_version"`
	Engine         any               `json:"engine"`
	Connection     *sqlConnectionRef `json:"connection"`
	Output         any               `json:"output"`
	CreatedAt      string            `json:"created_at"`
	UpdatedAt      string            `json:"updated_at"`
	Editable       bool              `json:"editable"`
	Runnable       bool              `json:"runnable"`
	RunSchedule    *string           `json:"run_schedule"`
	RunScheduledBy *int64            `json:"run_scheduled_by"`
	NextRunAt      *string           `json:"next_run_at"`
}

type sqlDatasourceRef struct {
	ID        int64   `json:"id"`
	Name      string  `json:"name"`
	ProjectID int64   `json:"project_id"`
	Rows      *int64  `json:"rows"`
	UpdatedAt *string `json:"updated_at"`
}

type sqlScriptDetail struct {
	sqlScriptOut
	Content    pyjson.Object     `json:"content"`
	Datasource *sqlDatasourceRef `json:"datasource"`
}

type sqlCatalogColumn struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

type sqlCatalogItem struct {
	ID      int64              `json:"id"`
	Name    string             `json:"name"`
	Folder  string             `json:"folder"`
	Path    string             `json:"path"`
	Rows    *int64             `json:"rows"`
	Columns []sqlCatalogColumn `json:"columns"`
	HasData bool               `json:"has_data"`
}

type scVersionOut struct {
	Version   int64   `json:"version"`
	Note      string  `json:"note"`
	CreatedAt string  `json:"created_at"`
	CreatedBy *string `json:"created_by"`
	Current   bool    `json:"current"`
}

// ── helpers ───────────────────────────────────────────────────────────────────

// connectionRefOf is _connection_ref.
func connectionRefOf(ctx context.Context, c *perm.Checker, q perm.Querier, id any) (*sqlConnectionRef, error) {
	if id == nil {
		return nil, nil
	}
	cid, _ := id.(int64)
	conn, err := sqlscripts.QueryableConnection(ctx, q, cid)
	if err != nil {
		return nil, err
	}
	if conn == nil {
		return &sqlConnectionRef{ID: cid}, nil
	}
	usable, err := c.Has(ctx, conn.ProjectID, perm.Connect)
	if err != nil {
		return nil, err
	}
	return &sqlConnectionRef{ID: conn.ID, Name: conn.Name, DBType: conn.DBType, Usable: usable}, nil
}

// sqlOutOf is _out.
func sqlOutOf(ctx context.Context, c *perm.Checker, q perm.Querier, s *sqlscripts.Script, content pyjson.Object) (sqlScriptOut, error) {
	engine, _ := content.Get("engine")
	connID, _ := content.Get("connection_id")
	conn, err := connectionRefOf(ctx, c, q, connID)
	if err != nil {
		return sqlScriptOut{}, err
	}
	var output any
	if o := sqlscripts.OutputOf(content); o != nil {
		output = o
	}
	editable, err := c.Has(ctx, s.ProjectID, perm.Edit)
	if err != nil {
		return sqlScriptOut{}, err
	}
	runnable, err := c.Has(ctx, s.ProjectID, perm.Run)
	if err != nil {
		return sqlScriptOut{}, err
	}
	return sqlScriptOut{ID: s.ID, Name: s.Name, Description: s.Description, ProjectID: s.ProjectID,
		CurrentVersion: s.CurrentVersion, Engine: engine, Connection: conn, Output: output,
		CreatedAt: pyjson.Isoformat(s.CreatedAt), UpdatedAt: pyjson.Isoformat(s.UpdatedAt), Editable: editable,
		Runnable: runnable, RunSchedule: s.RunSchedule, RunScheduledBy: s.RunScheduledBy, NextRunAt: isoPtr(s.NextRunAt)}, nil
}

// sqlDetailOf is _detail.
func sqlDetailOf(ctx context.Context, c *perm.Checker, q perm.Querier, s *sqlscripts.Script) (*sqlScriptDetail, error) {
	content, err := sqlscripts.ContentOf(ctx, q, s, nil)
	if err != nil {
		return nil, err
	}
	var ref *sqlDatasourceRef
	if output := sqlscripts.OutputOf(content); output != nil {
		target, err := sqlscripts.TargetOf(ctx, q, output)
		if err != nil {
			return nil, err
		}
		if target != nil {
			ok, err := c.Has(ctx, target.ProjectID, perm.View)
			if err != nil {
				return nil, err
			}
			if ok {
				ref = &sqlDatasourceRef{ID: target.ID, Name: target.Name, ProjectID: target.ProjectID, Rows: target.Rows,
					UpdatedAt: isoPtr(&target.UpdatedAt)}
			}
		}
	}
	out, err := sqlOutOf(ctx, c, q, s, content)
	if err != nil {
		return nil, err
	}
	return &sqlScriptDetail{sqlScriptOut: out, Content: content, Datasource: ref}, nil
}

func (d *sqlScriptDetail) send(w http.ResponseWriter, status int) { server.JSON(w, status, d) }

// detailAfter answers _detail of the script read again (session.refresh), with the
// permissions read again too.
func sqlSendDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, id int64, status int) {
	ctx := r.Context()
	s, err := sqlscripts.Get(ctx, d.DB, id)
	if err != nil || s == nil {
		scFailed(d, w, "sql_scripts: reading the script again", fmt.Errorf("script %d: %v", id, err))
		return
	}
	det, err := sqlDetailOf(ctx, perm.For(d.DB, &p.User), d.DB, s)
	if err != nil {
		scFailed(d, w, "sql_scripts: the script's detail", err)
		return
	}
	det.send(w, status)
}

// sqlVisibleScript is get_visible with the request's answers: the script, or false once answered.
func sqlVisibleScript(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, id int64) (*sqlscripts.Script, bool) {
	s, refusal, err := sqlscripts.GetVisible(r.Context(), c, d.DB, id)
	if err != nil {
		scFailed(d, w, "sql_scripts: reading the script", err)
		return nil, false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return nil, false
	}
	return s, true
}

// scEnsure is ensure_can, answered.
func scEnsure(d *Deps, w http.ResponseWriter, ctx context.Context, c *perm.Checker, project int64, need perm.Capability) bool {
	refusal, err := c.Ensure(ctx, project, need)
	if err != nil {
		scFailed(d, w, "checking a permission", err)
		return false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return false
	}
	return true
}

// scFolderExists is session.get(Project, id) is not None.
func scFolderExists(ctx context.Context, q perm.Querier, id int64) (bool, error) {
	var one int
	err := q.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, id).Scan(&one)
	if scNoRows(err) {
		return false, nil
	}
	return err == nil, err
}

// ── lists ─────────────────────────────────────────────────────────────────────

func sqlSendList(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, projects []int64) {
	ctx := r.Context()
	scripts, err := sqlscripts.Visible(ctx, c, d.DB, projects)
	if err != nil {
		scFailed(d, w, "sql_scripts: the list", err)
		return
	}
	contents, err := sqlscripts.CurrentContents(ctx, d.DB, scripts)
	if err != nil {
		scFailed(d, w, "sql_scripts: the contents", err)
		return
	}
	out := make([]sqlScriptOut, 0, len(scripts))
	for i := range scripts {
		o, err := sqlOutOf(ctx, c, d.DB, &scripts[i], contents[scripts[i].ID])
		if err != nil {
			scFailed(d, w, "sql_scripts: a script of the list", err)
			return
		}
		out = append(out, o)
	}
	server.JSON(w, http.StatusOK, out)
}

// listSQLScripts is GET /sql-scripts (list_sql_scripts): every SQL script this person sees.
func listSQLScripts(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	sqlSendList(d, w, r, perm.For(d.DB, &p.User), nil)
}

// listProjectSQLScripts is GET /projects/{project_id}/sql-scripts (list_project_sql_scripts).
func listProjectSQLScripts(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, ids[0])
	if err != nil {
		scFailed(d, w, "sql_scripts: the folder", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, ids[0], perm.View) {
		return
	}
	sqlSendList(d, w, r, c, []int64{ids[0]})
}

// sqlCatalog is GET /sql-scripts/catalog (sql_catalog): the datasources this person may
// query, with their columns.
func sqlCatalog(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	folders, err := sqlscripts.Folders(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "sql_scripts: the folders", err)
		return
	}
	paths := sqlscripts.Paths(folders, " / ")
	list, err := sqlscripts.ReadableDatasources(ctx, perm.For(d.DB, &p.User), d.DB)
	if err != nil {
		scFailed(d, w, "sql_scripts: the datasources", err)
		return
	}
	out := make([]sqlCatalogItem, 0, len(list))
	for _, ds := range list {
		cols, err := sqlCatalogColumns(ds.Columns)
		if err != nil {
			scFailed(d, w, "sql_scripts: a datasource's columns", err)
			return
		}
		folder := ""
		if f, ok := folders[ds.ProjectID]; ok {
			folder = f.Name
		}
		out = append(out, sqlCatalogItem{ID: ds.ID, Name: ds.Name, Folder: folder, Path: paths[ds.ProjectID], Rows: ds.Rows,
			Columns: cols, HasData: ds.Key != ""})
	}
	server.JSON(w, http.StatusOK, out)
}

// sqlCatalogColumns: `json.loads(ds.columns or "[]")`, the objects of the list as columns;
// unreadable JSON is no columns. A list is iterated, an object by its keys (none of which
// is an object): only a list gives columns.
func sqlCatalogColumns(raw string) ([]sqlCatalogColumn, error) {
	if raw == "" {
		raw = "[]"
	}
	cols := []sqlCatalogColumn{}
	v, err := pyjson.Loads(raw)
	if err != nil {
		return cols, nil
	}
	switch list := v.(type) {
	case []any:
		for _, c := range list {
			o, ok := c.(pyjson.Object)
			if !ok {
				continue
			}
			name, has := o.Get("name")
			if !has {
				name = ""
			}
			dtype, has := o.Get("dtype")
			if !has {
				dtype = ""
			}
			cols = append(cols, sqlCatalogColumn{Name: pyjson.Str(name), Type: pyjson.Str(dtype)})
		}
	case pyjson.Object, string:
	default:
		return nil, fmt.Errorf("the columns are not iterable: %s", raw)
	}
	return cols, nil
}

// getSQLScript is GET /sql-scripts/{script_id} (get_sql_script).
func getSQLScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id")
	if !ok {
		return
	}
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, ids[0])
	if !ok {
		return
	}
	det, err := sqlDetailOf(r.Context(), c, d.DB, s)
	if err != nil {
		scFailed(d, w, "sql_scripts: the script's detail", err)
		return
	}
	det.send(w, http.StatusOK)
}

// ── copy, delete ──────────────────────────────────────────────────────────────

// duplicateSQLScript is POST /sql-scripts/{script_id}/duplicate (duplicate_sql_script): a
// copy in its folder, as «name (i)», from the current version.
func duplicateSQLScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	src, ok := sqlVisibleScript(d, w, r, c, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, src.ProjectID, perm.Edit) {
		return
	}
	var copyID int64
	var copyName string
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		name, err := copynames.Free(ctx, tx, "sql_scripts", src.ProjectID, src.Name)
		if err != nil {
			return err
		}
		content, err := sqlscripts.ContentOf(ctx, tx, src, nil)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version,
			run_schedule, run_scheduled_by, next_run_at, created_at, updated_at)
			VALUES ($1, $2, $3, $4, 0, NULL, NULL, NULL, $5, $5) RETURNING id`,
			name, src.Description, src.ProjectID, p.User.ID, now).Scan(&copyID); err != nil {
			return err
		}
		cp := &sqlscripts.Script{ID: copyID}
		if _, err := sqlscripts.AddVersion(ctx, tx, cp, content, "created", p.User.ID); err != nil {
			return err
		}
		copyName = name
		_, err = tx.Exec(ctx, `UPDATE sql_scripts SET current_version = $1 WHERE id = $2`, cp.CurrentVersion, copyID)
		return err
	})
	if copynames.Clash(err) {
		scRefuse(w, http.StatusConflict, copynames.ClashDetail)
		return
	}
	if err != nil {
		scFailed(d, w, "sql_scripts: the copy", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.create", TargetType: scPtrStr("sql_script"),
		TargetID: &copyID, TargetLabel: &copyName,
		Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "duplicate_of", Value: src.ID}}})
	sqlSendDetail(d, w, r, p, copyID, http.StatusCreated)
}

// sqlGroupNames is _not_in_groups' list: «a», «b» … of the first ten.
func sqlGroupNames(groups []flowgroups.Group) (string, []string) {
	names := make([]string, len(groups))
	quoted := make([]string, 0, 10)
	for i, g := range groups {
		names[i] = g.Name
		if i < 10 {
			quoted = append(quoted, "«"+g.Name+"»")
		}
	}
	s := strings.Join(quoted, ", ")
	if len(groups) > 10 {
		s += " …"
	}
	return s, names
}

// sqlNotInGroups is _not_in_groups: the 409 when flow groups run the script (`message` has
// «{names}» where their names go), or nil.
func sqlNotInGroups(ctx context.Context, q perm.Querier, scriptID int64, message string) (any, error) {
	groups, err := sqlscripts.GroupsUsing(ctx, q, scriptID)
	if err != nil || len(groups) == 0 {
		return nil, err
	}
	names, list := sqlGroupNames(groups)
	return pyjson.Object{{Key: "message", Value: strings.Replace(message, "{names}", names, 1)}, {Key: "flows", Value: []any{}},
		{Key: "groups", Value: list}}, nil
}

// deleteSQLScript is DELETE /sql-scripts/{script_id} (delete_sql_script): its runs stay in
// the history, no longer attached to it; its datasource stays.
func deleteSQLScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	type refusal struct {
		status int
		detail any
	}
	var no *refusal
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		// in line with «Refresh now» and the scheduler: nothing starts between the check and the delete
		if err := sqlscripts.InLine(ctx, tx, s.ID); err != nil {
			return err
		}
		live, err := sqlscripts.LiveRun(ctx, tx, s.ID)
		if err != nil {
			return err
		}
		if live {
			no = &refusal{http.StatusConflict, "The script is writing its datasource: delete it once it has finished"}
			return scRefused
		}
		inGroups, err := sqlNotInGroups(ctx, tx, s.ID, "The script is used by the flow groups {names}: remove it from there first")
		if err != nil {
			return err
		}
		if inGroups != nil {
			no = &refusal{http.StatusConflict, inGroups}
			return scRefused
		}
		if ref, detail, err := reports.NotReadByReports(ctx, c, tx, "sql_script", s.ID, "The script"); err != nil {
			return err
		} else if ref != nil {
			no = &refusal{ref.Status, detail}
			return scRefused
		}
		if ref, detail, err := dashboards.NotReadBy(ctx, c, tx, "sql_script", s.ID, "The script"); err != nil {
			return err
		} else if ref != nil {
			no = &refusal{ref.Status, detail}
			return scRefused
		}
		if _, err := tx.Exec(ctx, `UPDATE runs SET sql_script_id = NULL WHERE sql_script_id = $1`, s.ID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM sql_script_versions WHERE script_id = $1`, s.ID); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM sql_scripts WHERE id = $1`, s.ID)
		return err
	})
	if no != nil {
		scRefuseWith(w, no.status, no.detail)
		return
	}
	if err != nil {
		scFailed(d, w, "sql_scripts: deleting the script", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.delete", TargetType: scPtrStr("sql_script"),
		TargetID: scPtrInt(ids[0]), TargetLabel: scPtrStr(s.Name)})
	scNoContent(w)
}

var scRefused = fmt.Errorf("refused")

// ── versions ──────────────────────────────────────────────────────────────────

// listSQLScriptVersions is GET /sql-scripts/{script_id}/versions (list_versions).
func listSQLScriptVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id")
	if !ok {
		return
	}
	s, ok := sqlVisibleScript(d, w, r, perm.For(d.DB, &p.User), ids[0])
	if !ok {
		return
	}
	out, err := scVersionsOf(r.Context(), d.DB, "sql_script_versions", "script_id", s.ID, s.CurrentVersion)
	if err != nil {
		scFailed(d, w, "sql_scripts: the versions", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// scVersionsOf is the versions of an object, newest first, with their authors' emails.
func scVersionsOf(ctx context.Context, q perm.Querier, table, fk string, id, current int64) ([]scVersionOut, error) {
	rows, err := q.Query(ctx, fmt.Sprintf(`SELECT v.version, v.note, v.created_at, u.email FROM %s v
		LEFT JOIN users u ON u.id = v.created_by WHERE v.%s = $1 ORDER BY v.version DESC`, table, fk), id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []scVersionOut{}
	for rows.Next() {
		var v scVersionOut
		var at time.Time
		if err := rows.Scan(&v.Version, &v.Note, &at, &v.CreatedBy); err != nil {
			return nil, err
		}
		v.CreatedAt, v.Current = pyjson.Isoformat(at), v.Version == current
		out = append(out, v)
	}
	return out, rows.Err()
}

// getSQLScriptVersion is GET /sql-scripts/{script_id}/versions/{version} (get_version).
func getSQLScriptVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	s, ok := sqlVisibleScript(d, w, r, perm.For(d.DB, &p.User), ids[0])
	if !ok {
		return
	}
	exists, content, err := sqlscripts.VersionRow(ctx, d.DB, s, &ids[1])
	if err != nil {
		scFailed(d, w, "sql_scripts: the version", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Version not found")
		return
	}
	parsed, err := sqlscripts.Parsed(content)
	if err != nil {
		scFailed(d, w, "sql_scripts: the version's content", err)
		return
	}
	server.JSON(w, http.StatusOK, parsed)
}

// sqlChecked is _checked: the content to save; a connection or a datasource it did not
// have already must be one this person may use.
func sqlChecked(ctx context.Context, c *perm.Checker, q perm.Querier, user *auth.User, content any,
	current pyjson.Object) (pyjson.Object, *auth.Refusal, error) {
	checked, refusal := sqlscripts.CheckContent(content)
	if refusal != nil {
		return nil, refusal, nil
	}
	if cid, _ := checked.Get("connection_id"); cid != nil && !pyjson.Equal(cid, scGet(current, "connection_id")) {
		if _, refusal, err := sqlscripts.UsableConnection(ctx, c, q, cid.(int64)); err != nil || refusal != nil {
			return nil, refusal, err
		}
	}
	if out, _ := checked.Get("output"); out != nil && !pyjson.Equal(out, scGet(current, "output")) {
		if refusal, err := sqlscripts.CheckOutput(ctx, c, q, user, out.(pyjson.Object)); err != nil || refusal != nil {
			return nil, refusal, err
		}
	}
	return checked, nil, nil
}

func scGet(o pyjson.Object, key string) any {
	v, _ := o.Get(key)
	return v
}

// sqlGroupsOff is _groups_off: the flow groups that run the script, or send a report or a
// dashboard that reads it, whose schedules a change that widens it switches off.
func sqlGroupsOff(ctx context.Context, tx pgx.Tx, user *auth.User, s *sqlscripts.Script, reasons []string) ([]flowgroups.Off, error) {
	if len(reasons) == 0 {
		return nil, nil
	}
	why := strings.Join(reasons, "; ")
	var affected []flowgroups.Affected
	groups, err := sqlscripts.GroupsUsing(ctx, tx, s.ID)
	if err != nil {
		return nil, err
	}
	for _, g := range groups {
		affected = append(affected, flowgroups.Affected{Group: g,
			Reason: fmt.Sprintf("the SQL script '%s' it runs changed: %s", s.Name, why)})
	}
	byReports, err := reports.GroupsReading(ctx, tx, "sql_script", s.ID)
	if err != nil {
		return nil, err
	}
	for _, x := range byReports {
		affected = append(affected, flowgroups.Affected{Group: x.Group,
			Reason: fmt.Sprintf("the SQL script '%s' its report '%s' reads changed: %s", s.Name, x.Report.Name, why)})
	}
	byDashboards, err := dashboards.GroupsReading(ctx, tx, "sql_script", s.ID)
	if err != nil {
		return nil, err
	}
	for _, x := range byDashboards {
		affected = append(affected, flowgroups.Affected{Group: x.Group,
			Reason: fmt.Sprintf("the SQL script '%s' its dashboard '%s' reads changed: %s", s.Name, x.Dashboard.Name, why)})
	}
	return flowgroups.TurnOff(ctx, tx, user.ID, affected)
}

// scScheduleOff is _schedule_off: a change by someone else that makes the script read or
// write more switches its schedule off. Returns the audit's detail.
func scScheduleOff(schedule *string, by *int64, user *auth.User, reasons []string) pyjson.Object {
	if schedule == nil || *schedule == "" || by == nil || *by == user.ID || len(reasons) == 0 {
		return nil
	}
	return pyjson.Object{{Key: "turned_off", Value: true}, {Key: "reason", Value: strings.Join(reasons, "; ")},
		{Key: "author", Value: *by}}
}

// scRecordOff is _record_off.
func scRecordOff(ctx context.Context, d *Deps, user *auth.User, action, targetType string, id int64, label string,
	mine pyjson.Object, groups []flowgroups.Off) {
	if mine != nil {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: action, TargetType: scPtrStr(targetType),
			TargetID: scPtrInt(id), TargetLabel: scPtrStr(label), Detail: mine})
	}
	for _, g := range groups {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: "flow_group.schedule", TargetType: scPtrStr("flow_group"),
			TargetID: scPtrInt(g.ID), TargetLabel: scPtrStr(g.Name), Detail: g.Detail})
	}
}

// restoreSQLScriptVersion is POST /sql-scripts/{script_id}/versions/{version}/restore
// (restore_version): an older version becomes the newest, and the current one.
func restoreSQLScriptVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "script_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	version := ids[1]
	exists, raw, err := sqlscripts.VersionRow(ctx, d.DB, s, &version)
	if err != nil {
		scFailed(d, w, "sql_scripts: the version", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Version not found")
		return
	}
	if version == s.CurrentVersion {
		sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
		return
	}
	current, err := sqlscripts.ContentOf(ctx, d.DB, s, nil)
	if err != nil {
		scFailed(d, w, "sql_scripts: the current content", err)
		return
	}
	old, err := sqlscripts.Parsed(raw)
	if err != nil {
		scFailed(d, w, "sql_scripts: the version's content", err)
		return
	}
	content, refusal, err := sqlChecked(ctx, c, d.DB, &p.User, old, current)
	if err != nil {
		scFailed(d, w, "sql_scripts: checking the content", err)
		return
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	// _keeps_its_datasource
	if sqlscripts.Truthy(scGet(current, "output")) && !sqlscripts.Truthy(scGet(content, "output")) {
		detail, err := sqlNotInGroups(ctx, d.DB, s.ID, "The flow groups {names} write the script's datasource: "+
			"remove it from there first")
		if err != nil {
			scFailed(d, w, "sql_scripts: the groups", err)
			return
		}
		if detail != nil {
			scRefuseWith(w, http.StatusConflict, detail)
			return
		}
	}
	reasons := sqlscripts.Widens(current, content)
	var off []flowgroups.Off
	mine := scScheduleOff(s.RunSchedule, s.RunScheduledBy, &p.User, reasons)
	var newVersion int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if off, err = sqlGroupsOff(ctx, tx, &p.User, s, reasons); err != nil {
			return err
		}
		if mine != nil {
			if _, err := tx.Exec(ctx, `UPDATE sql_scripts SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL
				WHERE id = $1`, s.ID); err != nil {
				return err
			}
		}
		if newVersion, err = sqlscripts.AddVersion(ctx, tx, s, content, fmt.Sprintf("restored from v%d", version), p.User.ID); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE sql_scripts SET current_version = $1, updated_at = $2 WHERE id = $3`,
			newVersion, time.Now().UTC(), s.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "sql_scripts: restoring the version", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.restore", TargetType: scPtrStr("sql_script"),
		TargetID: scPtrInt(s.ID), TargetLabel: scPtrStr(s.Name),
		Detail: pyjson.Object{{Key: "from", Value: version}, {Key: "version", Value: newVersion}}})
	scRecordOff(ctx, d, &p.User, "sql_script.schedule", "sql_script", s.ID, s.Name, mine, off)
	sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
}
