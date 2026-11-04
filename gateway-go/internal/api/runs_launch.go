package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/runs.py: launching a flow's run, and the email node's trial.

func init() {
	register("runs_launch", func(d *Deps) []server.Route {
		b := func(route string, h authedFunc) http.Handler { return withBody(server.Authed(d.Auth, route, h)) }
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/runs", Handler: b("/flows/{flow_id}/runs", launchRun(d, false))},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/email-test", Handler: b("/flows/{flow_id}/email-test", launchRun(d, true))},
		}
	})
}

// flowEnv is what a flow's run is launched with.
func (d *Deps) flowEnv() runlaunch.FlowEnv {
	return runlaunch.FlowEnv{DB: d.DB, Engine: d.Engine, Bucket: d.Config.EngineBucket, Log: d.Log}
}

// launchRun is POST /flows/{flow_id}/runs (launch_run) and, with emailTest, POST
// /flows/{flow_id}/email-test (launch_email_test): the email node's real attachment sent to
// whoever asks, nothing published or written anywhere.
func launchRun(d *Deps, emailTest bool) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		fv := ps.Path("flow_id", r.PathValue("flow_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.RunCreate)
		if !validated(w, &ps) {
			return
		}
		q := d.req(w, r, p)
		fid, ok := rowID(fv)
		if !ok {
			fid = NoSuchID
		}
		f := q.flow(fid)
		if f == nil {
			return
		}
		opts := runlaunch.FlowRunOptions{}
		if emailTest {
			if body.Get("email") == nil {
				server.Refuse(w, unprocessable("This test needs an Output node of type email"))
				return
			}
			to := pystr.Strip(p.User.Email)
			if to == "" {
				server.Refuse(w, unprocessable("Your account has no email address"))
				return
			}
			opts.DryRunTo = to
		}
		flow := &runlaunch.Flow{ID: f.ID, ProjectID: f.ProjectID, Name: f.Name, Engine: f.Engine, ProductionEngine: f.ProductionEngine}
		runID, ref, err := runlaunch.LaunchFlowRun(q.ctx, d.flowEnv(), &p.User, flow, body, opts)
		if err != nil {
			q.fail(err)
			return
		}
		if ref != nil {
			server.Refuse(w, ref)
			return
		}
		run, err := q.readRun(runID)
		if err != nil {
			q.fail(err)
			return
		}
		server.JSON(w, http.StatusCreated, run.out())
	}
}
