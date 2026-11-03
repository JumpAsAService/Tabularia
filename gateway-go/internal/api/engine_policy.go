package api

import (
	"context"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/enginepolicy"
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
