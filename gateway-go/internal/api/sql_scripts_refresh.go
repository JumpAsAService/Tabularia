package api

import (
	"errors"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// routes/sql_scripts.py refresh_sql_script: the script's whole result written into its
// datasource now (services/sql_script_runs.py launch).

func init() {
	register("sql_scripts_refresh", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodPost, Pattern: "/sql-scripts/{script_id}/refresh",
			Handler: server.Authed(d.Auth, "/sql-scripts/{script_id}/refresh", refreshSQLScript(d))}}
	})
}

// sqlEnv is what a SQL script's write is launched with.
func (d *Deps) sqlEnv() runlaunch.SQLEnv { return runlaunch.SQLEnv{FlowEnv: d.flowEnv(), SQL: d.SQL} }

// launchErr answers what stopped a launch: a refusal, the engine down (main.py's 503), a 500.
func launchErr(d *Deps, w http.ResponseWriter, err error) {
	var ar *auth.Refusal
	var rr *reports.Refusal
	var sr *sqlscripts.Refusal
	var su *sqlscripts.Unreadable
	var ed *runlaunch.EngineDown
	var rd *reports.EngineDown
	switch {
	case errors.As(err, &ar):
		server.Refuse(w, ar)
	case errors.As(err, &rr):
		server.JSON(w, rr.Status, map[string]any{"detail": rr.Detail})
	case errors.As(err, &sr):
		detail(w, http.StatusUnprocessableEntity, sr.Message)
	case errors.As(err, &su):
		detail(w, http.StatusUnprocessableEntity, su.Message)
	case errors.As(err, &ed), errors.As(err, &rd):
		detail(w, http.StatusServiceUnavailable, "The engine is not answering: try again in a moment")
	default:
		fail500(d, w, "launch", err)
	}
}

// refreshSQLScript is POST /sql-scripts/{script_id}/refresh: VIEW on the script, RUN on its
// folder, then the write; the run as the script's runs list it.
func refreshSQLScript(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "script_id")
		if !ok {
			return
		}
		c := perm.For(d.DB, &p.User)
		s, ok := sqlVisibleScript(d, w, r, c, ids[0])
		if !ok {
			return
		}
		if !scEnsure(d, w, r.Context(), c, s.ProjectID, perm.Run) {
			return
		}
		runID, err := runlaunch.LaunchSQLScript(r.Context(), d.sqlEnv(), &p.User, s, "manual", nil, true)
		if err != nil {
			launchErr(d, w, err)
			return
		}
		q := d.req(w, r, p)
		run, err := q.readRun(runID)
		if err != nil || run == nil {
			q.fail(errors.Join(err, errors.New("the run just written is gone")))
			return
		}
		launchedBy := p.User.Email
		server.JSON(w, http.StatusAccepted, sqlRunOut{RunID: run.ID, Status: run.Status, TriggerType: run.TriggerType,
			StartedAt: iso(run.StartedAt), FinishedAt: isoPtr(run.FinishedAt), Error: run.Error, LaunchedBy: &launchedBy,
			Rows: run.RowsWritten, DatasourceID: run.DatasourceID, ParentRunID: run.ParentRunID})
	}
}
