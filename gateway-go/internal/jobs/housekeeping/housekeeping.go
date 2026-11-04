// Package housekeeping is services/housekeeping.py housekeeping_loop: every hour the history
// that piles up in the database trimmed (runs' logs, contract reports, versions, report and
// dashboard views, the assistant's conversations, the agent's drafts and plans, the AI
// comments), then the tables touched vacuumed. No lock: as in Python, several processes
// trimming the same rows is harmless (a deadlock is one kind skipped until the next pass).
package housekeeping

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	everySeconds = 3600 // EVERY_SECONDS
	batch        = 1000 // BATCH
	maxBatches   = 200  // MAX_BATCHES
	// run_log_retention.BATCH and MAX_BATCHES
	logBatch      = 500
	logMaxBatches = 200
	// flow_tools.DRAFT_TTL, plans.APPLYING_STUCK_AFTER, report_views.WINDOW_DAYS
	draftTTL           = 24 * time.Hour
	applyingStuckAfter = time.Hour
	windowDays         = 30
)

func init() {
	api.RegisterJob("housekeeping_loop", func(d *api.Deps) jobs.Job {
		h := &housekeeping{db: d.DB, panel: d.Panel, log: d.Log}
		return jobs.Job{Order: 4, Loop: h.loop, Ticks: map[string]jobs.Tick{
			"housekeeping": func(ctx context.Context) (any, error) { return h.onePass(ctx) },
		}}
	})
}

type housekeeping struct {
	db    *pgxpool.Pool
	panel *settings.Panel
	log   *slog.Logger
}

// kinds is one_pass's steps, in its order.
var kinds = []string{"runs", "data_contract_results", "flow_versions", "saved_script_versions", "sql_script_versions",
	"notebook_versions", "report_versions", "report_views", "dashboard_versions", "dashboard_views", "ai_chats",
	"agent_drafts", "agent_plans", "ai_comments", "ai_comment_threads"}

func (h *housekeeping) loop(ctx context.Context) {
	for ctx.Err() == nil {
		done, err := h.onePass(ctx)
		if err != nil {
			h.log.Error("housekeeping: a pass failed", "err", err)
		}
		var parts []string
		for _, k := range kinds {
			if n := done[k]; n > 0 {
				parts = append(parts, fmt.Sprintf("%s %d", k, n))
			}
		}
		if len(parts) > 0 {
			h.log.Info("housekeeping", "trimmed", parts)
		}
		if !jobs.Sleep(ctx, everySeconds*time.Second) {
			return
		}
	}
}

// onePass is one_pass(session): every kind trimmed once, how many rows each touched; a kind
// that fails is logged and counted 0; then the tables touched vacuumed (a vacuum that fails
// fails the pass, as in Python).
func (h *housekeeping) onePass(ctx context.Context) (map[string]int64, error) {
	versions := func(table, parent, owner string) func() (int64, error) {
		return func() (int64, error) {
			kept := h.panel.VersionsKept()
			if kept <= 0 {
				return 0, nil
			}
			return h.inBatches(ctx, fmt.Sprintf(`DELETE FROM %[1]s WHERE id IN (
				SELECT t.id FROM (
					SELECT id, %[2]s, version,
						   ROW_NUMBER() OVER (PARTITION BY %[2]s ORDER BY version DESC) AS n
					FROM %[1]s) t
				WHERE t.n > $2 AND NOT EXISTS (
					SELECT 1 FROM %[3]s s WHERE s.id = t.%[2]s AND s.current_version = t.version)
				LIMIT $1)`, table, parent, owner), kept)
		}
	}
	steps := map[string]func() (int64, error){
		"runs": func() (int64, error) { return h.emptyOut(ctx) },
		"data_contract_results": func() (int64, error) {
			kept := h.panel.ContractResultsKept()
			if kept <= 0 {
				return 0, nil
			}
			return h.inBatches(ctx, `DELETE FROM data_contract_results WHERE id IN (
				SELECT id FROM (
					SELECT id, notify, ROW_NUMBER() OVER (PARTITION BY datasource_id ORDER BY id DESC) AS n
					FROM data_contract_results) t
				WHERE t.n > $2 AND (t.notify IS NULL OR t.notify NOT IN ('pending', 'sending'))
				LIMIT $1)`, kept)
		},
		"flow_versions": func() (int64, error) {
			kept := h.panel.VersionsKept()
			if kept <= 0 {
				return 0, nil
			}
			return h.inBatches(ctx, `DELETE FROM flow_versions WHERE id IN (
				SELECT id FROM (
					SELECT id, ROW_NUMBER() OVER (PARTITION BY flow_id ORDER BY version DESC) AS n
					FROM flow_versions) t
				WHERE t.n > $2 LIMIT $1)`, kept)
		},
		"saved_script_versions": versions("saved_script_versions", "script_id", "saved_scripts"),
		"sql_script_versions":   versions("sql_script_versions", "script_id", "sql_scripts"),
		"notebook_versions":     versions("notebook_versions", "notebook_id", "notebooks"),
		"report_versions":       versions("report_versions", "report_id", "reports"),
		"report_views": func() (int64, error) {
			return h.inBatches(ctx, `DELETE FROM report_views WHERE id IN (
				SELECT id FROM report_views WHERE day < $2 LIMIT $1)`, h.windowStart())
		},
		"dashboard_versions": versions("dashboard_versions", "dashboard_id", "dashboards"),
		"dashboard_views": func() (int64, error) {
			return h.inBatches(ctx, `DELETE FROM dashboard_views WHERE id IN (
				SELECT id FROM dashboard_views WHERE day < $2 LIMIT $1)`, h.windowStart())
		},
		"ai_chats": func() (int64, error) { return h.aiChats(ctx) },
		"agent_drafts": func() (int64, error) {
			return h.exec(ctx, `DELETE FROM agent_drafts WHERE updated_at <= $1`, jobs.Now().Add(-draftTTL))
		},
		"agent_plans": func() (int64, error) { return h.agentPlans(ctx) },
		"ai_comments": func() (int64, error) {
			return h.inBatches(ctx, `DELETE FROM ai_comments WHERE key IN (
				SELECT key FROM ai_comments WHERE expires_at <= $2 LIMIT $1)`, jobs.Now())
		},
		"ai_comment_threads": func() (int64, error) {
			return h.inBatches(ctx, `DELETE FROM ai_comment_threads WHERE thread IN (
				SELECT thread FROM ai_comment_threads WHERE expires_at <= $2 LIMIT $1)`, jobs.Now())
		},
	}
	done := map[string]int64{}
	var touched []string
	for _, k := range kinds {
		n, err := steps[k]()
		if err != nil {
			h.log.Error("housekeeping: not trimmed this time", "kind", k, "err", err)
			n = 0
		}
		done[k] = n
		if n != 0 {
			touched = append(touched, k)
		}
	}
	if done["ai_chats"] != 0 {
		touched = append(touched, "ai_chat_turns")
	}
	return done, h.vacuum(ctx, touched)
}

// inBatches is _in_batches: a DELETE … LIMIT $1 batch after batch, each committed.
func (h *housekeeping) inBatches(ctx context.Context, sql string, arg any) (int64, error) {
	var removed int64
	for range maxBatches {
		tag, err := h.db.Exec(ctx, sql, batch, arg)
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

func (h *housekeeping) exec(ctx context.Context, sql string, args ...any) (int64, error) {
	tag, err := h.db.Exec(ctx, sql, args...)
	return tag.RowsAffected(), err
}

// windowStart is report_views.window_start: the installation's day, less 29.
func (h *housekeeping) windowStart() time.Time {
	loc, err := config.LoadZone(h.panel.Timezone())
	if err != nil {
		loc = time.UTC
	}
	t := jobs.Now().In(loc)
	day := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	return day.AddDate(0, 0, -(windowDays - 1))
}

// aiChats is ai_chats: the conversations untouched for the days, their turns first.
func (h *housekeeping) aiChats(ctx context.Context) (int64, error) {
	days := h.panel.AIChatRetentionDays()
	if days <= 0 {
		return 0, nil
	}
	cutoff := jobs.Now().Add(-time.Duration(days) * 24 * time.Hour)
	var removed int64
	for range maxBatches {
		rows, err := h.db.Query(ctx, `SELECT id FROM ai_chats WHERE updated_at < $1 LIMIT $2`, cutoff, batch)
		if err != nil {
			return removed, err
		}
		ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
		if err != nil || len(ids) == 0 {
			return removed, err
		}
		err = pgx.BeginFunc(ctx, h.db, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, `DELETE FROM ai_chat_turns WHERE chat_id = ANY($1)`, ids); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, `DELETE FROM ai_chats WHERE id = ANY($1)`, ids)
			return err
		})
		if err != nil {
			return removed, err
		}
		removed += int64(len(ids))
		if len(ids) < batch {
			break
		}
	}
	return removed, nil
}

// agentPlans is plans.expire_and_trim, in one transaction.
func (h *housekeeping) agentPlans(ctx context.Context) (int64, error) {
	at := jobs.Now()
	var n int64
	err := pgx.BeginFunc(ctx, h.db, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE agent_plans SET status = 'expired' WHERE status = 'pending' AND expires_at <= $1`, at)
		if err != nil {
			return err
		}
		n += tag.RowsAffected()
		tag, err = tx.Exec(ctx, `UPDATE agent_plans SET status = 'failed', applied_at = $1, result = $2
			WHERE status = 'applying' AND expires_at <= $3`,
			at, `[{"ok": false, "error": "Interrupted while being applied."}]`, at.Add(-applyingStuckAfter))
		if err != nil {
			return err
		}
		n += tag.RowsAffected()
		if days := h.panel.AIChatRetentionDays(); days > 0 {
			tag, err = tx.Exec(ctx, `DELETE FROM agent_plans WHERE status NOT IN ('pending', 'applying') AND created_at < $1`,
				at.Add(-time.Duration(days)*24*time.Hour))
			if err != nil {
				return err
			}
			n += tag.RowsAffected()
		}
		return nil
	})
	return n, err
}

// emptyOut is run_log_retention.empty_out: the engine's tracebacks and the Script and
// Container runs' logs of the runs ended more than the days ago.
func (h *housekeeping) emptyOut(ctx context.Context) (int64, error) {
	days := h.panel.LogRetentionDays()
	if days <= 0 {
		return 0, nil
	}
	cutoff := jobs.Now().Add(-time.Duration(days) * 24 * time.Hour)
	var removed int64
	for range logMaxBatches {
		tag, err := h.db.Exec(ctx, `UPDATE runs SET error_detail = NULL WHERE id IN (SELECT id FROM runs
			WHERE finished_at IS NOT NULL AND finished_at < $1 AND error_detail IS NOT NULL LIMIT $2)`, cutoff, logBatch)
		if err != nil {
			return removed, err
		}
		n := tag.RowsAffected()
		removed += n
		if n < logBatch {
			break
		}
	}
	for range logMaxBatches {
		n, err := h.emptyLogs(ctx, cutoff)
		removed += n
		if err != nil {
			return removed, err
		}
		if n < logBatch {
			break
		}
	}
	return removed, nil
}

var errNotADict = errors.New("a run's script is not a JSON object")

// emptyLogs is one batch of empty_out's second loop: `log` emptied, `log_expired` said.
func (h *housekeeping) emptyLogs(ctx context.Context, cutoff time.Time) (int64, error) {
	var n int64
	err := pgx.BeginFunc(ctx, h.db, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT id, script FROM runs WHERE finished_at IS NOT NULL AND finished_at < $1
			AND kind IN ('script', 'container') AND script LIKE '%"log": "_%' AND script NOT LIKE '%"log_expired": %'
			LIMIT $2`, cutoff, logBatch)
		if err != nil {
			return err
		}
		type item struct {
			id  int64
			raw *string
		}
		list, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (item, error) {
			var it item
			return it, r.Scan(&it.id, &it.raw)
		})
		if err != nil {
			return err
		}
		for _, it := range list {
			raw := "{}"
			if it.raw != nil && *it.raw != "" {
				raw = *it.raw
			}
			v, err := pyjson.Loads(raw)
			if err != nil {
				continue
			}
			info, ok := v.(pyjson.Object)
			if !ok {
				return errNotADict // item assignment on a list raises: the kind fails
			}
			had := pyjson.Truthy(info.Value("log"))
			info = info.Set("log", "").Set("log_expired", had)
			out, err := pyjson.Dumps(info, pyjson.Options{EnsureASCII: true})
			if err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE runs SET script = $1 WHERE id = $2`, out, it.id); err != nil {
				return err
			}
		}
		n = int64(len(list))
		return nil
	})
	return n, err
}

// vacuum is vacuum(session, tables): outside a transaction.
func (h *housekeeping) vacuum(ctx context.Context, tables []string) error {
	for _, t := range tables {
		if _, err := h.db.Exec(ctx, "VACUUM (ANALYZE) "+t); err != nil { // names of this file, never input
			return err
		}
	}
	return nil
}
