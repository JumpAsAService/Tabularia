// Package retention is services/audit_retention.py retention_loop: the action log's rows
// past AUDIT__RETENTION_MINUTES deleted, and the client addresses past
// AUDIT__IP_RETENTION_DAYS emptied (services/ip_retention.py delete_addresses).
//
// No lock: several processes deleting the same old rows together is harmless, as in Python.
package retention

import (
	"context"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
)

const (
	tickSeconds        = 60   // TICK_SECONDS
	batch              = 5000 // BATCH (audit_retention and ip_retention)
	maxBatchesPerRound = 20   // audit_retention.MAX_BATCHES_PER_ROUND
	maxAddressBatches  = 200  // ip_retention.MAX_BATCHES
	ipEverySeconds     = 3600 // IP_EVERY_SECONDS
)

func init() {
	api.RegisterJob("retention_loop", func(d *api.Deps) jobs.Job {
		r := &retention{db: d.DB, log: d.Log, minutes: int64(d.Config.AuditRetentionMinutes), days: d.Config.Jobs.IPRetentionDays}
		return jobs.Job{Order: 3, Loop: r.loop, Ticks: map[string]jobs.Tick{
			// to_do, then one_round with the addresses (the loop's first round)
			"retention": func(ctx context.Context) (any, error) {
				if r.toDo() {
					return nil, r.oneRound(ctx, true)
				}
				return nil, nil
			},
			// a round of the next 59 minutes: the rows only
			"retention.rows": func(ctx context.Context) (any, error) {
				if r.toDo() {
					return nil, r.oneRound(ctx, false)
				}
				return nil, nil
			},
		}}
	})
}

type retention struct {
	db            *pgxpool.Pool
	log           *slog.Logger
	minutes, days int64 // AUDIT__RETENTION_MINUTES, AUDIT__IP_RETENTION_DAYS: not on the panel
}

// toDo is to_do(settings).
func (r *retention) toDo() bool { return r.minutes > 0 || r.days > 0 }

// loop is retention_loop.
func (r *retention) loop(ctx context.Context) {
	if !r.toDo() {
		r.log.Info("audit: no expiry configured, the log is kept whole")
		return
	}
	var lastAddresses time.Time
	for ctx.Err() == nil {
		now := time.Now()
		addresses := lastAddresses.IsZero() || now.Sub(lastAddresses) >= ipEverySeconds*time.Second
		if err := r.oneRound(ctx, addresses); err != nil {
			r.log.Error("audit: cleanup failed", "err", err)
		} else if addresses {
			lastAddresses = now
		}
		if !jobs.Sleep(ctx, tickSeconds*time.Second) {
			break
		}
	}
	r.log.Info("audit: cleanup stopped")
}

// oneRound is one_round: the rows past the window, then (when `addresses`) the addresses.
func (r *retention) oneRound(ctx context.Context, addresses bool) error {
	removed, err := r.purge(ctx)
	if err != nil {
		return err
	}
	if removed > 0 {
		r.log.Info("audit: deleted items past the window", "removed", removed, "minutes", r.minutes)
	}
	if addresses && r.days > 0 {
		emptied, err := r.deleteAddresses(ctx)
		if err != nil {
			return err
		}
		if emptied > 0 {
			r.log.Info("audit: client address and browser emptied", "rows", emptied, "days", r.days)
		}
	}
	return nil
}

// purge is audit_retention.purge: batch after batch, each its own transaction.
func (r *retention) purge(ctx context.Context) (int64, error) {
	if r.minutes <= 0 {
		return 0, nil
	}
	cutoff := jobs.Now().Add(-time.Duration(r.minutes) * time.Minute)
	var removed int64
	for range maxBatchesPerRound {
		tag, err := r.db.Exec(ctx, `DELETE FROM audit_logs WHERE id IN
			(SELECT id FROM audit_logs WHERE ts < $1 LIMIT $2)`, cutoff, batch)
		if err != nil {
			return removed, err
		}
		n := tag.RowsAffected()
		removed += n
		if n < batch {
			break
		}
	}
	return removed, nil
}

// deleteAddresses is ip_retention.delete_addresses.
func (r *retention) deleteAddresses(ctx context.Context) (int64, error) {
	if r.days <= 0 {
		return 0, nil
	}
	cutoff := jobs.Now().Add(-time.Duration(r.days) * 24 * time.Hour)
	var removed int64
	for range maxAddressBatches {
		tag, err := r.db.Exec(ctx, `UPDATE audit_logs SET ip = NULL, user_agent = NULL WHERE id IN
			(SELECT id FROM audit_logs WHERE ts < $1 AND (ip IS NOT NULL OR user_agent IS NOT NULL) LIMIT $2)`, cutoff, batch)
		if err != nil {
			return removed, err
		}
		n := tag.RowsAffected()
		removed += n
		if n < batch {
			break
		}
	}
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return removed, err
	}
	defer tx.Rollback(ctx)
	for _, q := range []string{
		`UPDATE api_tokens SET last_used_ip = NULL WHERE last_used_at < $1 AND last_used_ip IS NOT NULL`,
		`UPDATE user_sessions SET ip = NULL, user_agent = NULL
			WHERE last_seen_at < $1 AND (ip IS NOT NULL OR user_agent IS NOT NULL)`,
	} {
		if _, err := tx.Exec(ctx, q, cutoff); err != nil {
			return removed, err
		}
	}
	// no release writes it any more: whatever is there is old
	if _, err := tx.Exec(ctx, `UPDATE users SET last_seen_ip = NULL WHERE last_seen_ip IS NOT NULL`); err != nil {
		return removed, err
	}
	return removed, tx.Commit(ctx)
}
