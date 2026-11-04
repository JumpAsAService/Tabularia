package api

// routes/sql_scripts.py: creating a SQL script, changing it, and the list of its writes.

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/openlineage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

func init() {
	register("sql_scripts_write", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/sql-scripts",
				Handler: scWithBody(d, "/projects/{project_id}/sql-scripts", createSQLScript)},
			{Method: http.MethodPatch, Pattern: "/sql-scripts/{script_id}",
				Handler: scWithBody(d, "/sql-scripts/{script_id}", updateSQLScript)},
			{Method: http.MethodGet, Pattern: "/sql-scripts/{script_id}/runs",
				Handler: server.Authed(d.Auth, "/sql-scripts/{script_id}/runs", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
					listSQLScriptRuns(d, w, r, p)
				})},
		}
	})
}

// scOpenLineageOn is openlineage.enabled: events go somewhere (Deps.Lineage sends them).
func scOpenLineageOn() bool {
	env := config.FromOS()
	for _, n := range []string{"OPENLINEAGE__URL", "OPENLINEAGE__FILE"} {
		if v, _ := env(n); v != "" {
			return true
		}
	}
	for _, n := range []string{"OPENLINEAGE_URL", "OPENLINEAGE_CONFIG"} {
		if os.Getenv(n) != "" {
			return true
		}
	}
	return false
}

// sqlName is _name: the name stripped, not empty, free in the folder.
func sqlName(ctx context.Context, q perm.Querier, project int64, name string, exclude int64) (string, *auth.Refusal, error) {
	name = pyd.Strip(name)
	if name == "" {
		return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The SQL script needs a name"}, nil
	}
	var clash int64
	err := q.QueryRow(ctx, `SELECT id FROM sql_scripts WHERE project_id = $1 AND name = $2`, project, name).Scan(&clash)
	if err != nil && !scNoRows(err) {
		return "", nil, err
	}
	if err == nil && clash != exclude {
		return "", &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a SQL script '%s' in this folder", name)}, nil
	}
	return name, nil, nil
}

// createSQLScript is POST /projects/{project_id}/sql-scripts (create_sql_script).
func createSQLScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	pid := params.Path("project_id", r.PathValue("project_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.SqlScriptCreate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	project, ok := pid.(int64)
	if !ok {
		project = -1 // past int64: no such folder
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, project)
	if err != nil {
		fail(d, w, "sql_scripts: the folder", err)
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
	name, refusal, err := sqlName(ctx, d.DB, project, in.Get("name").(string), 0)
	if err != nil {
		fail(d, w, "sql_scripts: the name", err)
		return
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var content any = sqlscripts.EmptyContent()
	if raw := in.Get("content"); raw != nil {
		content = scFromPyd(raw)
	}
	checked, refusal, err := sqlChecked(ctx, c, d.DB, &p.User, content, nil)
	if err != nil {
		fail(d, w, "sql_scripts: checking the content", err)
		return
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var id int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO sql_scripts (name, description, project_id, owner_id, current_version,
			run_schedule, run_scheduled_by, next_run_at, created_at, updated_at)
			VALUES ($1, $2, $3, $4, 0, NULL, NULL, NULL, $5, $5) RETURNING id`,
			name, pyd.Strip(in.Get("description").(string)), project, p.User.ID, now).Scan(&id); err != nil {
			return err
		}
		s := &sqlscripts.Script{ID: id}
		v, err := sqlscripts.AddVersion(ctx, tx, s, checked, "created", p.User.ID)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE sql_scripts SET current_version = $1 WHERE id = $2`, v, id)
		return err
	})
	if err != nil {
		// two creations at once with the same name: the unique name refuses the second (a 500 in Python too)
		fail(d, w, "sql_scripts: creating the script", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.create", TargetType: scPtrStr("sql_script"),
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: project}}})
	sqlSendDetail(d, w, r, p, id, http.StatusCreated)
}

// updateSQLScript is PATCH /sql-scripts/{script_id} (update_sql_script): name, description,
// folder (a move), content; a change of the content is a new version; a rename tells the
// lineage catalog.
func updateSQLScript(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.SqlScriptUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	scriptID, ok := sid.(int64)
	if !ok {
		scriptID = -1
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, scriptID)
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Edit) {
		return
	}
	var lineageNames openlineage.Names
	if in.Get("name") != nil || in.Get("project_id") != nil {
		var err error
		if lineageNames, err = d.Lineage.CatalogNames(ctx, d.DB, openlineage.Ref{Kind: openlineage.KindSQLScript, ID: s.ID,
			ProjectID: s.ProjectID, Name: s.Name}); err != nil {
			fail(d, w, "sql_scripts: the lineage names", err)
			return
		}
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
				fail(d, w, "sql_scripts: the folder", err)
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
	name, refusal, err := sqlName(ctx, d.DB, target, wanted, s.ID)
	if err != nil {
		fail(d, w, "sql_scripts: the name", err)
		return
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	if name != s.Name {
		changes = append(changes, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: s.Name}, {Key: "to", Value: name}}})
	}
	if target != s.ProjectID {
		changes = append(changes, pyjson.KV{Key: "project_id", Value: pyjson.Object{{Key: "from", Value: s.ProjectID}, {Key: "to", Value: target}}})
	}
	var reasons []string
	var newContent pyjson.Object
	if raw := in.Get("content"); raw != nil {
		current, err := sqlscripts.ContentOf(ctx, d.DB, s, nil)
		if err != nil {
			fail(d, w, "sql_scripts: the current content", err)
			return
		}
		// what the request does not name stays as it is (null names it: «no datasource»)
		merged := append(pyjson.Object{}, current...)
		for _, kv := range scFromPyd(raw).(pyjson.Object) {
			merged = merged.Set(kv.Key, kv.Value)
		}
		checked, refusal, err := sqlChecked(ctx, c, d.DB, &p.User, merged, current)
		if err != nil {
			fail(d, w, "sql_scripts: checking the content", err)
			return
		}
		if refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		if sqlscripts.Truthy(scGet(current, "output")) && !sqlscripts.Truthy(scGet(checked, "output")) {
			why, err := sqlNotInGroups(ctx, d.DB, s.ID, "The flow groups {names} write the script's datasource: "+
				"remove it from there first")
			if err != nil {
				fail(d, w, "sql_scripts: the groups", err)
				return
			}
			if why != nil {
				scRefuseWith(w, http.StatusConflict, why)
				return
			}
		}
		if !pyjson.Equal(checked, current) {
			reasons = sqlscripts.Widens(current, checked)
			newContent = checked
		}
	}
	description := s.Description
	descriptionChanged := false
	if v := in.Get("description"); v != nil && pyd.Strip(v.(string)) != s.Description {
		description, descriptionChanged = pyd.Strip(v.(string)), true
	}
	// the version comes before the description among the changes, as the Python dict fills
	var off []flowgroups.Off
	var mine pyjson.Object
	var version int64
	nothing := len(changes) == 0 && newContent == nil && !descriptionChanged
	if nothing {
		sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
		return
	}
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if newContent != nil {
			if version, err = sqlscripts.AddVersion(ctx, tx, s, newContent, "edited", p.User.ID); err != nil {
				return err
			}
		}
		if off, err = sqlGroupsOff(ctx, tx, &p.User, s, reasons); err != nil {
			return err
		}
		if target != s.ProjectID {
			reasons = append(reasons, "it was moved to another folder")
		}
		schedule, by, next := s.RunSchedule, s.RunScheduledBy, s.NextRunAt
		if mine = scScheduleOff(s.RunSchedule, s.RunScheduledBy, &p.User, reasons); mine != nil {
			schedule, by, next = nil, nil, nil
		}
		_, err = tx.Exec(ctx, `UPDATE sql_scripts SET name = $1, project_id = $2, description = $3, current_version = $4,
			run_schedule = $5, run_scheduled_by = $6, next_run_at = $7, updated_at = $8 WHERE id = $9`,
			name, target, description, s.CurrentVersion, schedule, by, next, time.Now().UTC(), s.ID)
		return err
	})
	if err != nil {
		fail(d, w, "sql_scripts: changing the script", err)
		return
	}
	d.Lineage.Renamed(ctx, d.DB, lineageNames)
	if newContent != nil {
		changes = append(changes, pyjson.KV{Key: "version", Value: version})
	}
	if descriptionChanged {
		changes = append(changes, pyjson.KV{Key: "description", Value: true})
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.update", TargetType: scPtrStr("sql_script"),
		TargetID: scPtrInt(s.ID), TargetLabel: &name, Detail: changes})
	scRecordOff(ctx, d, &p.User, "sql_script.schedule", "sql_script", s.ID, name, mine, off)
	sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
}

type sqlRunOut struct {
	RunID        int64   `json:"run_id"`
	Status       string  `json:"status"`
	TriggerType  string  `json:"trigger_type"`
	StartedAt    *string `json:"started_at"`
	FinishedAt   *string `json:"finished_at"`
	Error        *string `json:"error"`
	LaunchedBy   *string `json:"launched_by"`
	Rows         *int64  `json:"rows"`
	DatasourceID *int64  `json:"datasource_id"`
	ParentRunID  *int64  `json:"parent_run_id"`
}

// listSQLScriptRuns is GET /sql-scripts/{script_id}/runs (list_sql_script_runs): the
// latest writes of the script's datasource, newest first. A write still going is brought
// up to date with the engine (routes/runs._reconcile, which the port leaves to the Python
// gateway): then the request is answered there.
func listSQLScriptRuns(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	lim := params.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(100), 10)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	scriptID, ok := sid.(int64)
	if !ok {
		scriptID = -1
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, scriptID)
	if !ok {
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT r.id, r.status, r.trigger_type, r.started_at, r.finished_at, r.error, u.email,
		r.rows_written, r.datasource_id, r.parent_run_id, ds.project_id
		FROM runs r LEFT JOIN users u ON u.id = r.launched_by LEFT JOIN datasources ds ON ds.id = r.datasource_id
		WHERE r.sql_script_id = $1 AND r.kind = 'sql' ORDER BY r.id DESC LIMIT $2`, s.ID, lim.(int64))
	if err != nil {
		fail(d, w, "sql_scripts: the runs", err)
		return
	}
	type item struct {
		out    sqlRunOut
		folder *int64
	}
	var items []item
	for rows.Next() {
		var it item
		var started time.Time
		var finished *time.Time
		if err := rows.Scan(&it.out.RunID, &it.out.Status, &it.out.TriggerType, &started, &finished, &it.out.Error,
			&it.out.LaunchedBy, &it.out.Rows, &it.out.DatasourceID, &it.out.ParentRunID, &it.folder); err != nil {
			rows.Close()
			fail(d, w, "sql_scripts: a run", err)
			return
		}
		it.out.StartedAt, it.out.FinishedAt = isoPtr(&started), isoPtr(finished)
		items = append(items, it)
	}
	rows.Close()
	out := make([]sqlRunOut, 0, len(items))
	for _, it := range items {
		if it.out.Status != "SUCCESS" && it.out.Status != "FAILURE" {
			scForward(d, w, r) // _reconcile: the engine's status, the datasource published
			return
		}
		sees := false
		if it.folder != nil {
			if sees, err = c.Has(ctx, *it.folder, perm.View); err != nil {
				fail(d, w, "sql_scripts: a permission", err)
				return
			}
		}
		if !sees {
			it.out.Rows, it.out.DatasourceID = nil, nil
		}
		out = append(out, it.out)
	}
	server.JSON(w, http.StatusOK, out)
}
