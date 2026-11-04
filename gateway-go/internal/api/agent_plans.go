package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/agent_plans.py: a plan of the agent read and cancelled by its owner. Confirming
// one applies its actions through the agent's tools (services/agent/plans.py apply_plan):
// that stays with the Python AI service (docs/specs/gateway-go.md, decision 4), so
// POST /agent/plans/{plan_id}/confirm is not registered here and goes on to Python.
func init() {
	register("agent_plans", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/agent/plans/{plan_id}", Handler: server.Authed(d.Auth, "/agent/plans/{plan_id}", readPlan(d))},
			{Method: http.MethodPost, Pattern: "/agent/plans/{plan_id}/cancel", Handler: server.Authed(d.Auth, "/agent/plans/{plan_id}/cancel", cancelPlan(d))},
		}
	})
}

// planInternal is _INTERNAL: what an action carries for the applying, not for the reader.
var planInternal = map[string]bool{"definition": true, "base_hash": true, "key": true, "diff": true}

type planRow struct {
	id, user                int64
	origin, status, summary string
	tokenName               *string
	chatID                  *int64
	actions                 string
	result                  *string
	created, expires        time.Time
	applied                 *time.Time
}

// PlanOut is routes/agent_plans.py PlanOut.
type PlanOut struct {
	ID         int64   `json:"id"`
	Origin     string  `json:"origin"`
	TokenName  *string `json:"token_name"`
	ChatID     *int64  `json:"chat_id"`
	Status     string  `json:"status"`
	Summary    string  `json:"summary"`
	Actions    []any   `json:"actions"`
	Result     any     `json:"result"`
	CreatedAt  string  `json:"created_at"`
	ExpiresAt  string  `json:"expires_at"`
	AppliedAt  *string `json:"applied_at"`
	CanConfirm bool    `json:"can_confirm"`
	Revision   string  `json:"revision"`
}

func readPlanRow(ctx context.Context, d *Deps, id int64) (planRow, bool, error) {
	var p planRow
	err := d.DB.QueryRow(ctx, `SELECT id, user_id, origin, status, summary, token_name, chat_id, actions, result, created_at,
		expires_at, applied_at FROM agent_plans WHERE id = $1`, id).Scan(&p.id, &p.user, &p.origin, &p.status, &p.summary,
		&p.tokenName, &p.chatID, &p.actions, &p.result, &p.created, &p.expires, &p.applied)
	if errors.Is(err, pgx.ErrNoRows) {
		return p, false, nil
	}
	return p, err == nil, err
}

// ownPlan is _own: the plan when it is the person's (anyone else's is a 404, an
// administrator's too); a pending one past its expiry is marked expired.
func ownPlan(ctx context.Context, d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) (planRow, bool) {
	var ps pyd.Params
	idV := ps.Path("plan_id", r.PathValue("plan_id"), pyd.Int())
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return planRow{}, false
	}
	plan, found, err := readPlanRow(ctx, d, pathID(idV))
	if err != nil {
		adminFail(d, w, "reading the plan", err)
		return plan, false
	}
	if !found || plan.user != p.User.ID {
		adminDetail(w, http.StatusNotFound, "Plan not found")
		return plan, false
	}
	if plan.status == "pending" && !plan.expires.After(nowNaive()) {
		if _, err := d.DB.Exec(ctx, `UPDATE agent_plans SET status = 'expired' WHERE id = $1`, plan.id); err != nil {
			adminFail(d, w, "expiring the plan", err)
			return plan, false
		}
		plan.status = "expired"
	}
	return plan, true
}

// planOut is plan_out: the actions numbered, without what is for the applying; the
// outcome; the revision the person confirms.
func planOut(plan planRow) (PlanOut, error) {
	actions := []any{}
	if v, err := pyjson.Loads([]byte(orEmptyList(plan.actions))); err == nil { // _parsed
		if list, ok := v.([]any); ok {
			for i, a := range list {
				obj, ok := a.(pyjson.Object)
				if !ok {
					return PlanOut{}, errors.New("an action that is not a dict")
				}
				out := pyjson.Object{{Key: "n", Value: int64(i + 1)}}
				for _, kv := range obj {
					if !planInternal[kv.Key] {
						out = out.Set(kv.Key, kv.Value)
					}
				}
				actions = append(actions, out)
			}
		}
	}
	var result any
	if plan.result != nil && *plan.result != "" { // results_of
		if v, err := pyjson.Loads([]byte(*plan.result)); err == nil {
			list, ok := v.([]any)
			if !ok && v != nil {
				return PlanOut{}, errors.New("a result that is not a list")
			}
			for _, e := range list {
				if _, ok := e.(pyjson.Object); !ok {
					return PlanOut{}, errors.New("a result that is not a dict")
				}
			}
			if ok {
				result = list
			}
		}
	}
	sum := sha256.Sum256([]byte(plan.actions))
	return PlanOut{ID: plan.id, Origin: plan.origin, TokenName: plan.tokenName, ChatID: plan.chatID, Status: plan.status,
		Summary: plan.summary, Actions: actions, Result: result, CreatedAt: pyjson.Isoformat(plan.created),
		ExpiresAt: pyjson.Isoformat(plan.expires), AppliedAt: isoPtr(plan.applied), CanConfirm: plan.status == "pending",
		Revision: hex.EncodeToString(sum[:])[:16]}, nil
}

func orEmptyList(s string) string {
	if s == "" {
		return "[]"
	}
	return s
}

func writePlan(d *Deps, w http.ResponseWriter, plan planRow) {
	out, err := planOut(plan)
	if err != nil {
		adminFail(d, w, "the plan's view", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// readPlan is GET /agent/plans/{plan_id}.
func readPlan(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		plan, ok := ownPlan(r.Context(), d, w, r, p)
		if ok {
			writePlan(d, w, plan)
		}
	}
}

// cancelPlan is POST /agent/plans/{plan_id}/cancel (plans.cancel_plan_of): only a pending
// plan, claimed with a conditional UPDATE.
func cancelPlan(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		plan, ok := ownPlan(ctx, d, w, r, p)
		if !ok {
			return
		}
		if plan.status != "pending" {
			adminDetail(w, http.StatusConflict, fmt.Sprintf("Plan %d is %s: only a pending plan can be cancelled.", plan.id, plan.status))
			return
		}
		tag, err := d.DB.Exec(ctx, `UPDATE agent_plans SET status = 'cancelled' WHERE id = $1 AND status = 'pending'`, plan.id)
		if err != nil {
			adminFail(d, w, "cancelling the plan", err)
			return
		}
		if tag.RowsAffected() != 1 {
			adminDetail(w, http.StatusConflict, fmt.Sprintf("Plan %d is no longer pending.", plan.id))
			return
		}
		tt, label := "agent_plan", fmt.Sprintf("plan %d", plan.id)
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "agent.plan_cancel", TargetType: &tt,
			TargetID: &plan.id, TargetLabel: &label, Detail: pyjson.Object{{Key: "origin", Value: plan.origin}}})
		plan, _, err = readPlanRow(ctx, d, plan.id)
		if err != nil {
			adminFail(d, w, "reading the plan", err)
			return
		}
		if planViewFails(plan) {
			// cancel_plan_of returns plans.plan_view, which the route discards; where it fails
			// the cancel is done and recorded, and the answer is a 500
			server.InternalError(w)
			return
		}
		writePlan(d, w, plan)
	}
}

// planViewFails tells where plans.plan_view raises: an action's `diff_lines` that len()
// refuses (a number, true) or that is sliced when it is not a list or a string.
func planViewFails(plan planRow) bool {
	v, err := pyjson.Loads([]byte(orEmptyList(plan.actions)))
	list, ok := v.([]any)
	if err != nil || !ok {
		return false
	}
	for _, a := range list {
		obj, ok := a.(pyjson.Object)
		if !ok {
			return true
		}
		lines, _ := obj.Get("diff_lines")
		switch x := lines.(type) {
		case int64, float64, pyjson.BigInt:
			if pyTruthy(x) {
				return true
			}
		case bool:
			if x {
				return true
			}
		case pyjson.Object:
			if len(x) > 200 {
				return true
			}
		}
	}
	return false
}
