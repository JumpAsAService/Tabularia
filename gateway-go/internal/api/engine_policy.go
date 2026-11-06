package api

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/enginepolicy"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// EnginePolicyOut is schemas/models.py EnginePolicyOut.
type EnginePolicyOut struct {
	EngineID   string `json:"engine_id"`
	Allowed    bool   `json:"allowed"`
	FlowsUsing int64  `json:"flows_using"`
}

func init() {
	register("engine_policy", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/engine-policy", Handler: deps.Observer(d.Auth, d.DB, d.Log, "/engine-policy", listEnginePolicy(d))},
			{Method: http.MethodPut, Pattern: "/engine-policy/{engine_id}",
				Handler: withBody(deps.Superuser(d.Auth, d.DB, d.Log, "/engine-policy/{engine_id}", setEnginePolicy(d)))},
		}
	})
}

// flowsUsing is routes/engine_policy.py _flows_using: the development OR the production engine.
func flowsUsing(ctx context.Context, d *Deps, engineID string) (int64, error) {
	var n int64
	err := d.DB.QueryRow(ctx, `SELECT count(*) FROM flows WHERE engine = $1 OR production_engine = $1`, engineID).Scan(&n)
	return n, err
}

// listEnginePolicy is GET /engine-policy (list_engine_policy → _state).
func listEnginePolicy(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		off, err := enginepolicy.Disabled(r.Context(), d.DB)
		if err != nil {
			d.Log.Error("engine policy", "err", err)
			server.InternalError(w)
			return
		}
		out := make([]EnginePolicyOut, 0, len(enginepolicy.Known))
		for _, e := range enginepolicy.Known {
			n, err := flowsUsing(r.Context(), d, e)
			if err != nil {
				d.Log.Error("engine policy", "err", err)
				server.InternalError(w)
				return
			}
			out = append(out, EnginePolicyOut{EngineID: e, Allowed: !off[e], FlowsUsing: n})
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// setEnginePolicy is PUT /engine-policy/{engine_id} (set_engine_policy): disabling stops
// the choice, not the flows using it; the last allowed engine cannot go.
func setEnginePolicy(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		engineID := ps.Path("engine_id", r.PathValue("engine_id"), pyd.Str()).(string)
		body := ps.Body(bodyOf(r), gatewaymodels.EnginePolicyUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		eid := pyd.Lower(pyd.Strip(engineID))
		if !enginepolicy.IsKnown(eid) {
			server.Refuse(w, notFound(fmt.Sprintf("Unknown engine: '%s'", engineID)))
			return
		}
		off, err := enginepolicy.Disabled(ctx, d.DB)
		if err != nil {
			fail500(d, w, "engine policy", err)
			return
		}
		allowed := body.Get("allowed").(bool)
		if allowed {
			if off[eid] {
				if _, err := d.DB.Exec(ctx, `DELETE FROM disabled_engines WHERE engine_id = $1`, eid); err != nil {
					fail500(d, w, "engine policy", err)
					return
				}
			}
		} else {
			n := len(off)
			if !off[eid] {
				n++
			}
			if n >= len(enginepolicy.Known) {
				server.Refuse(w, &auth.Refusal{Status: http.StatusUnprocessableEntity,
					Detail: "At least one engine must stay allowed: without one, no flow could be created."})
				return
			}
			if !off[eid] {
				if _, err := d.DB.Exec(ctx, `INSERT INTO disabled_engines (engine_id, disabled_at, disabled_by) VALUES ($1, $2, $3)`,
					eid, time.Now().UTC(), p.User.ID); err != nil {
					fail500(d, w, "engine policy", err)
					return
				}
			}
		}
		used, err := flowsUsing(ctx, d, eid)
		if err != nil {
			fail500(d, w, "engine policy", err)
			return
		}
		action := "engine.disallow"
		if allowed {
			action = "engine.allow"
		}
		kind := "engine"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: action, TargetType: &kind, TargetLabel: &eid,
			Detail: pyjson.Object{{Key: "flows_using", Value: used}}})
		server.JSON(w, http.StatusOK, EnginePolicyOut{EngineID: eid, Allowed: allowed, FlowsUsing: used})
	}
}
