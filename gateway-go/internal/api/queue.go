package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("queue", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/queue", Handler: deps.Observer(d.Auth, d.DB, d.Log, "/queue", queueOverview(d))},
			{Method: http.MethodPost, Pattern: "/queue/jobs/{task_id}/stop",
				Handler: deps.Superuser(d.Auth, d.DB, d.Log, "/queue/jobs/{task_id}/stop", stopJob(d))},
		}
	})
}

// queueOverview is GET /queue (routes/queue.py queue_overview): the engine's overview.
func queueOverview(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		res, raw, err := d.Engine.JSON(r.Context(), http.MethodGet, "/tasks/queue", nil)
		if err != nil {
			d.Log.Error("queue: the engine", "err", err)
			server.InternalError(w)
			return
		}
		if res.StatusCode >= 400 {
			server.Refuse(w, engineRefusal(res.StatusCode, raw))
			return
		}
		engineJSON(w, raw)
	}
}

// stopJob is POST /queue/jobs/{task_id}/stop (stop_job): revoke and terminate, audited.
func stopJob(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		id := r.PathValue("task_id")
		path, err := enginePath("/tasks/", id)
		if err != nil {
			server.InternalError(w)
			return
		}
		res, raw, err := d.Engine.JSON(r.Context(), http.MethodDelete, path, nil)
		if err != nil {
			d.Log.Error("queue: the engine", "err", err)
			server.InternalError(w)
			return
		}
		if res.StatusCode >= 400 {
			server.Refuse(w, engineRefusal(res.StatusCode, raw))
			return
		}
		task := "task"
		audit.Record(r.Context(), d.DB, d.Log, audit.Event{Actor: &p.User, Action: "queue.job_stop", TargetType: &task, TargetLabel: &id})
		engineJSON(w, raw)
	}
}
