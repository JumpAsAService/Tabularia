package api

// The schedules of SQL scripts and saved scripts (routes/sql_scripts.py
// set_sql_script_schedule, routes/scripts.py set_script_schedule): set (a 5-field cron, read
// as croniter reads it: internal/cron, in the panel's time zone) or cleared.

import (
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("schedules_clear", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPut, Pattern: "/sql-scripts/{script_id}/schedule",
				Handler: scWithBody(d, "/sql-scripts/{script_id}/schedule", setSQLScriptSchedule)},
			{Method: http.MethodPut, Pattern: "/scripts/{script_id}/schedule",
				Handler: scWithBody(d, "/scripts/{script_id}/schedule", setScriptSchedule)},
		}
	})
}

// scCron validates the path and the ScheduleUpdate body: the script's id and the cron
// stripped ("" clears). False once answered.
func scCron(w http.ResponseWriter, r *http.Request, body pyd.Body, model *pyd.Model) (int64, string, bool) {
	var params pyd.Params
	sid := params.Path("script_id", r.PathValue("script_id"), pyd.Int())
	in := params.Body(body, model)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return 0, "", false
	}
	id, ok := sid.(int64)
	if !ok {
		id = -1
	}
	spec, _ := in.Get("cron").(string)
	return id, pyd.Strip(spec), true
}

// scValidCron is validate_schedule: the cron stripped, or its 422. False once answered.
func scValidCron(d *Deps, w http.ResponseWriter, spec string) (string, bool) {
	spec, err := cron.ValidateSchedule(spec)
	var bad *cron.ScheduleError
	if errors.As(err, &bad) {
		detail(w, http.StatusUnprocessableEntity, bad.Msg)
		return "", false
	}
	if err != nil {
		fail(d, w, "the schedule", err)
		return "", false
	}
	return spec, true
}

// scNextRun is next_fire(cron, now): the next instant in the panel's time zone, in UTC (the
// column is naive UTC). False once answered: a cron that never fires is croniter's
// CroniterBadDateError, uncaught (a 500).
func scNextRun(d *Deps, w http.ResponseWriter, spec string) (*time.Time, bool) {
	next, err := cron.NextFire(spec, time.Now().UTC(), d.Panel.Timezone())
	if err != nil {
		fail(d, w, "the schedule's next run", err)
		return nil, false
	}
	next = next.UTC()
	return &next, true
}

// setSQLScriptSchedule is PUT /sql-scripts/{script_id}/schedule: a cron (what the schedule
// would lack to write the datasource is refused first: sql_script_runs.gaps) or none.
func setSQLScriptSchedule(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	id, spec, ok := scCron(w, r, body, gatewaymodels.SqlScriptsScheduleUpdate)
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	s, ok := sqlVisibleScript(d, w, r, c, id)
	if !ok || !scEnsure(d, w, ctx, c, s.ProjectID, perm.Run) {
		return
	}
	var schedule, by any
	var next *time.Time
	if spec != "" {
		valid, ok := scValidCron(d, w, spec)
		if !ok {
			return
		}
		missing, err := d.sqlGaps(ctx, &p.User, s)
		if err != nil {
			fail(d, w, "sql_scripts: what the schedule lacks", err)
			return
		}
		if len(missing) > 0 {
			detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("The schedule would not write: %s", missing[0]))
			return
		}
		if next, ok = scNextRun(d, w, valid); !ok {
			return
		}
		schedule, by = valid, p.User.ID
	}
	if _, err := d.DB.Exec(ctx, `UPDATE sql_scripts SET run_schedule = $1, run_scheduled_by = $2, next_run_at = $3,
		updated_at = $4 WHERE id = $5`, schedule, by, next, time.Now().UTC(), s.ID); err != nil {
		fail(d, w, "sql_scripts: the schedule", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "sql_script.schedule", TargetType: scPtrStr("sql_script"),
		TargetID: &s.ID, TargetLabel: &s.Name, Detail: pyjson.Object{{Key: "cron", Value: schedule}}})
	sqlSendDetail(d, w, r, p, s.ID, http.StatusOK)
}

// setScriptSchedule is PUT /scripts/{script_id}/schedule: a cron (a script without code
// is refused) or none.
func setScriptSchedule(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	id, spec, ok := scCron(w, r, body, gatewaymodels.ScriptsScheduleUpdate)
	if !ok {
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
	var schedule, by any
	var next *time.Time
	if spec != "" {
		content, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
		if err != nil {
			fail(d, w, "scripts: the content", err)
			return
		}
		if !pyjson.Truthy(content.Value("files")) {
			detail(w, http.StatusUnprocessableEntity, "The script has no code yet: write it before scheduling it")
			return
		}
		valid, ok := scValidCron(d, w, spec)
		if !ok {
			return
		}
		if next, ok = scNextRun(d, w, valid); !ok {
			return
		}
		schedule, by = valid, p.User.ID
	}
	if _, err := d.DB.Exec(ctx, `UPDATE saved_scripts SET run_schedule = $1, run_scheduled_by = $2, next_run_at = $3,
		updated_at = $4 WHERE id = $5`, schedule, by, next, time.Now().UTC(), s.ID); err != nil {
		fail(d, w, "scripts: the schedule", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "script.schedule", TargetType: scPtrStr("saved_script"),
		TargetID: &s.ID, TargetLabel: &s.Name, Detail: pyjson.Object{{Key: "cron", Value: schedule}}})
	scriptSendDetail(d, w, r, p, s.ID, http.StatusOK)
}
