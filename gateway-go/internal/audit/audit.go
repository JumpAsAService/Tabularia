// Package audit is services/audit.py record_audit: one row of audit_logs per significant
// event, never failing the action that caused it. Of an observer neither the address nor the
// user agent is written; done with an API token, the token's name is added to the detail;
// done by the agent, `via` and the plan.
package audit

import (
	"context"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// Execer is a pool, a connection or a transaction.
type Execer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	roles.Querier
}

// Event is record_audit's keyword arguments.
type Event struct {
	Actor       *auth.User // nil: no actor (a failed sign-in)
	ActorLabel  string     // "" = the actor's email, or «anonymous»
	Action      string
	Outcome     string // "" = success
	TargetType  *string
	TargetID    *int64
	TargetLabel *string
	Detail      pyjson.Object // in the order the Python code builds the dict
}

// Record writes the event; an error is logged, never returned (record_audit never raises).
func Record(ctx context.Context, db Execer, log *slog.Logger, e Event) {
	info := reqinfo.From(ctx)
	minimize := false
	if e.Actor != nil {
		if r, err := roles.Of(ctx, db, e.Actor.ID, e.Actor.IsSuperuser, e.Actor.IsObserver); err == nil {
			minimize = r.ObserverOnly()
		}
	}
	detail := e.Detail
	if p := info.Principal; p != nil && p.TokenName != "" {
		detail = append(detail, pyjson.KV{Key: "token", Value: p.TokenName})
	}
	if info.Via != "" {
		detail = append(detail, pyjson.KV{Key: "via", Value: info.Via})
		if info.AgentPlan != 0 {
			detail = append(detail, pyjson.KV{Key: "plan", Value: info.AgentPlan})
		}
	}
	var detailText *string
	if len(detail) > 0 { // `if detail` in Python: an empty dict is no detail
		s := pyjson.Audit(detail)
		detailText = &s
	}
	var actorID *int64
	label := e.ActorLabel
	if e.Actor != nil {
		actorID = &e.Actor.ID
		if label == "" {
			label = e.Actor.Email
		}
	} else if label == "" {
		label = "anonymous"
	}
	outcome := e.Outcome
	if outcome == "" {
		outcome = "success"
	}
	var ip, agent *string
	if !minimize {
		if info.ClientIP != "" {
			ip = &info.ClientIP
		}
		if info.UserAgent != "" {
			agent = &info.UserAgent
		}
	}
	_, err := db.Exec(ctx, `INSERT INTO audit_logs (ts, actor_id, actor_label, action, outcome, target_type, target_id,
			target_label, detail, ip, user_agent) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
		time.Now().UTC(), actorID, label, e.Action, outcome, e.TargetType, e.TargetID, e.TargetLabel, detailText, ip, agent)
	if err != nil {
		log.Error("record_audit: cannot write the event", "action", e.Action, "err", err)
	}
}
