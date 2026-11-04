package api

// The schedules of SQL scripts and saved scripts (routes/sql_scripts.py
// set_sql_script_schedule, routes/scripts.py set_script_schedule). Clearing one is answered
// here; setting one reads the cron with croniter (services/schedule.py), which the port
// leaves to the Python gateway: that request is handed over as it came.

import (
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("schedules_clear", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPut, Pattern: "/sql-scripts/{script_id}/schedule",
				Handler: scWithBody(d, "/sql-scripts/{script_id}/schedule", clearSQLScriptSchedule)},
			{Method: http.MethodPut, Pattern: "/scripts/{script_id}/schedule",
				Handler: scWithBody(d, "/scripts/{script_id}/schedule", clearScriptSchedule)},
		}
	})
}

// scCron validates the path and the ScheduleUpdate body: the script's id, and whether the
// cron is empty (a clear). False once answered.
func scCron(w http.ResponseWriter, r *http.Request, body pyd.Body, model *pyd.Model) (int64, bool, bool) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	in := params.Body(body, model)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return 0, false, false
	}
	id, ok := sid.(int64)
	if !ok {
		id = -1
	}
	cron, _ := in.Get("cron").(string)
	return id, pyd.Strip(cron) == "", true
}

// clearSQLScriptSchedule is PUT /sql-scripts/{script_id}/schedule with no cron.
func clearSQLScriptSchedule(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	id, clear, ok := scCron(w, r, body, gatewaymodels.SqlScriptsScheduleUpdate)
	if !ok {
		return
	}
	if !clear {
		scForward(d, w, r)
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, id)
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Run) {
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE sql_scripts SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL,
		updated_at = $1 WHERE id = $2`, time.Now().UTC(), s.ID); err != nil {
		fail(d, w, "sql_scripts: the schedule", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.schedule", TargetType: scPtrStr("sql_script"),
		TargetID: &s.ID, TargetLabel: &s.Name, Detail: pyjson.Object{{Key: "cron", Value: nil}}})
	sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
}

// clearScriptSchedule is PUT /scripts/{script_id}/schedule with no cron.
func clearScriptSchedule(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	id, clear, ok := scCron(w, r, body, gatewaymodels.ScriptsScheduleUpdate)
	if !ok {
		return
	}
	if !clear {
		scForward(d, w, r)
		return
	}
	if !scriptNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := scriptVisible(d, w, r, c, p, id)
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Run) {
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE saved_scripts SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL,
		updated_at = $1 WHERE id = $2`, time.Now().UTC(), s.ID); err != nil {
		fail(d, w, "scripts: the schedule", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.schedule", TargetType: scPtrStr("saved_script"),
		TargetID: &s.ID, TargetLabel: &s.Name, Detail: pyjson.Object{{Key: "cron", Value: nil}}})
	scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
}
