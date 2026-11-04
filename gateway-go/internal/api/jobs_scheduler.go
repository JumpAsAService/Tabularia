package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// services/scheduler.py scheduler_loop: every minute the runs not finished are reconciled
// with the engine, the schedules due fire (datasources' refreshes, flows, flow groups, saved
// scripts, SQL scripts), the data contracts' freshness is checked again and their notices
// sent, the expired shared state cleaned and the blobs past their grace deleted.
//
// Several processes (Python ones too) may run it together: a slot is TAKEN with an UPDATE
// conditional on the value read, before anything is launched, and the queues line up on the
// same advisory locks.

const schedulerTickSeconds = 60 // TICK_SECONDS

func init() {
	RegisterJob("scheduler_loop", func(d *Deps) jobs.Job {
		s := &scheduler{d: d, sc: scriptcfg.Load(config.FromOS())}
		return jobs.Job{Order: 1, Loop: s.loop, Ticks: map[string]jobs.Tick{
			"scheduler": func(ctx context.Context) (any, error) { return nil, s.tick(ctx) },
		}}
	})
}

type scheduler struct {
	d  *Deps
	sc *scriptcfg.Settings // ScriptsSettings, ContainersSettings, KubernetesSettings: read at start
}

func (s *scheduler) loop(ctx context.Context) {
	s.d.Log.Info("scheduler started: datasource refresh + flow runs", "tick_seconds", schedulerTickSeconds)
	for ctx.Err() == nil {
		if err := s.tick(ctx); err != nil {
			s.d.Log.Error("scheduler: error in the tick", "err", err)
		}
		if !jobs.Sleep(ctx, schedulerTickSeconds*time.Second) {
			break
		}
	}
	s.d.Log.Info("scheduler stopped")
}

// nextFire is next_fire(spec, now).replace(tzinfo=None): false when it raises.
func (s *scheduler) nextFire(spec *string, now time.Time) (time.Time, bool) {
	if spec == nil {
		return time.Time{}, false
	}
	n, err := cron.NextFire(*spec, now, s.d.Panel.AppTimezone())
	return n, err == nil
}

// tick is _tick.
func (s *scheduler) tick(ctx context.Context) error {
	now := jobs.Now()
	q := s.d.jobRequest(ctx, nil)

	// the server-side reconcile of the runs not finished
	if ids, err := idsOf(ctx, s.d.DB, `SELECT id FROM runs WHERE status NOT IN ('SUCCESS', 'FAILURE') AND task_id != ''`); err != nil {
		s.d.Log.Error("scheduler: the runs' reconcile pass failed", "err", err)
	} else {
		for _, id := range ids {
			run, err := q.readRun(id)
			if err == nil && run != nil {
				_, err = q.reconcile(run, progressOf{})
			}
			if err != nil {
				s.d.Log.Error("scheduler: reconcile of a run failed", "run", id, "err", err)
			}
		}
	}

	// datasources
	type due struct {
		id   int64
		seen time.Time
	}
	dueOf := func(sql string) ([]due, error) {
		rows, err := s.d.DB.Query(ctx, sql, now)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		var out []due
		for rows.Next() {
			var x due
			if err := rows.Scan(&x.id, &x.seen); err != nil {
				return nil, err
			}
			out = append(out, x)
		}
		return out, rows.Err()
	}
	list, err := dueOf(`SELECT id, next_refresh_at FROM datasources WHERE refresh_schedule IS NOT NULL AND next_refresh_at IS NOT NULL
		AND next_refresh_at <= $1`)
	if err != nil {
		return err
	}
	for _, x := range list {
		if err := s.fireDS(ctx, x.id, now, x.seen); err != nil {
			s.d.Log.Error("scheduler: refresh of datasource failed", "datasource", x.id, "err", err)
			s.advance(ctx, "datasources", "refresh_schedule", "next_refresh_at", x.id, now)
		}
	}

	list, err = dueOf(`SELECT id, next_run_at FROM flows WHERE run_schedule IS NOT NULL AND next_run_at IS NOT NULL AND next_run_at <= $1`)
	if err != nil {
		return err
	}
	for _, x := range list {
		if err := s.fireQueued(ctx, flowSlot, x.id, now, x.seen); err != nil {
			s.d.Log.Error("scheduler: run of flow failed", "flow", x.id, "err", err)
			s.advance(ctx, "flows", "run_schedule", "next_run_at", x.id, now)
		}
	}

	list, err = dueOf(`SELECT id, next_run_at FROM flow_groups WHERE run_schedule IS NOT NULL AND next_run_at IS NOT NULL
		AND next_run_at <= $1`)
	if err != nil {
		return err
	}
	for _, x := range list {
		if err := s.fireQueued(ctx, groupSlot, x.id, now, x.seen); err != nil {
			s.d.Log.Error("scheduler: run of flow group failed", "group", x.id, "err", err)
			s.advance(ctx, "flow_groups", "run_schedule", "next_run_at", x.id, now)
		}
	}

	if s.sc.DisabledReason() == "" {
		list, err = dueOf(`SELECT id, next_run_at FROM saved_scripts WHERE run_schedule IS NOT NULL AND next_run_at IS NOT NULL
			AND next_run_at <= $1`)
		if err != nil {
			return err
		}
		for _, x := range list {
			if err := s.fireQueued(ctx, scriptSlot, x.id, now, x.seen); err != nil {
				s.d.Log.Error("scheduler: run of saved script failed", "script", x.id, "err", err)
				s.advance(ctx, "saved_scripts", "run_schedule", "next_run_at", x.id, now)
			}
		}
	}

	list, err = dueOf(`SELECT id, next_run_at FROM sql_scripts WHERE run_schedule IS NOT NULL AND next_run_at IS NOT NULL
		AND next_run_at <= $1`)
	if err != nil {
		return err
	}
	for _, x := range list {
		if err := s.fireSQLScript(ctx, x.id, now, x.seen); err != nil {
			s.d.Log.Error("scheduler: write of SQL script failed", "script", x.id, "err", err)
			s.advance(ctx, "sql_scripts", "run_schedule", "next_run_at", x.id, now)
		}
	}

	// data contracts: freshness, then the notices due
	if n, err := s.sweepFreshness(ctx, now); err != nil {
		s.d.Log.Error("scheduler: the data contracts' freshness check failed", "err", err)
	} else if n > 0 {
		s.d.Log.Info("scheduler: data contracts changed state for freshness", "count", n)
	}
	if n := s.deliverPending(ctx); n > 0 {
		s.d.Log.Info("scheduler: data contract notices sent", "count", n)
	}

	// the shared state's expired rows
	if err := s.clean(ctx, now); err != nil {
		s.d.Log.Error("scheduler: cleanup of the shared state failed", "err", err)
	}

	// the blobs whose grace expired
	if n, err := s.sweepBlobDeletions(ctx, now); err != nil {
		s.d.Log.Error("scheduler: sweep of the deferred blobs failed", "err", err)
	} else if n > 0 {
		s.d.Log.Info("scheduler: obsolete blobs deleted", "count", n)
	}
	return nil
}

func idsOf(ctx context.Context, q perm.Querier, sql string, args ...any) ([]int64, error) {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// advance is _advance_ds / _advance_flow / …: after a failed fire, the next slot from now
// (the schedule off when it no longer has one).
func (s *scheduler) advance(ctx context.Context, table, schedule, next string, id int64, now time.Time) {
	var spec *string
	err := s.d.DB.QueryRow(ctx, fmt.Sprintf(`SELECT %s FROM %s WHERE id = $1`, schedule, table), id).Scan(&spec)
	if err != nil {
		s.d.Log.Error("scheduler: the next slot not written", "table", table, "id", id, "err", err)
		return
	}
	if n, ok := s.nextFire(spec, now); ok {
		_, err = s.d.DB.Exec(ctx, fmt.Sprintf(`UPDATE %s SET %s = $2 WHERE id = $1`, table, next), id, n)
	} else {
		_, err = s.d.DB.Exec(ctx, fmt.Sprintf(`UPDATE %s SET %s = NULL, %s = NULL WHERE id = $1`, table, schedule, next), id)
	}
	if err != nil {
		s.d.Log.Error("scheduler: the next slot not written", "table", table, "id", id, "err", err)
	}
}

// disable is _disable_ds / _disable_flow / …: the schedule, its author and its slot gone.
func (s *scheduler) disable(ctx context.Context, table, schedule, by, next string, id int64, why string) error {
	s.d.Log.Warn("scheduler: schedule turned off", "table", table, "id", id, "reason", why)
	_, err := s.d.DB.Exec(ctx, fmt.Sprintf(`UPDATE %s SET %s = NULL, %s = NULL, %s = NULL WHERE id = $1`, table, schedule, by, next), id)
	return err
}

// takeSlot is the conditional UPDATE that moves a slot forward only when it is still the one
// read and still due: true for one process only.
func (s *scheduler) takeSlot(ctx context.Context, tx pgx.Tx, table, schedule, next string, id int64, spec *string, seen,
	now time.Time) (bool, error) {
	var sql string
	args := []any{id, seen, now}
	if n, ok := s.nextFire(spec, now); ok {
		sql = fmt.Sprintf(`UPDATE %s SET %s = $4 WHERE id = $1 AND %s = $2 AND %s <= $3`, table, next, next, next)
		args = append(args, n)
	} else {
		sql = fmt.Sprintf(`UPDATE %s SET %s = NULL, %s = NULL WHERE id = $1 AND %s = $2 AND %s <= $3`, table, schedule, next, next, next)
	}
	tag, err := tx.Exec(ctx, sql, args...)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// ── datasource: scheduled refresh ─────────────────────────────────────────────

// fireDS is _fire_ds.
func (s *scheduler) fireDS(ctx context.Context, id int64, now, seen time.Time) error {
	ds, err := loadJobDatasource(ctx, s.d.DB, id)
	if err != nil || ds == nil {
		return err
	}
	off := func(why string) error {
		return s.disable(ctx, "datasources", "refresh_schedule", "refresh_scheduled_by", "next_refresh_at", id, why)
	}
	user, err := loadUserOpt(ctx, s.d.DB, ds.RefreshScheduledBy)
	if err != nil {
		return err
	}
	if user == nil || !user.IsActive {
		return off("the schedule's author is missing or disabled")
	}
	var conn *connpayload.Conn
	if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
		if conn, err = connpayload.Load(ctx, s.d.DB, *ds.ConnectionID); err != nil {
			return err
		}
	}
	if conn == nil {
		return off("connection does not exist")
	}
	c := perm.For(s.d.DB, user)
	if ok, err := c.Has(ctx, ds.ProjectID, perm.Run); err != nil || !ok {
		if err != nil {
			return err
		}
		return off(fmt.Sprintf("RUN missing for %s on the datasource's folder", user.Email))
	}
	if ok, err := c.Has(ctx, conn.ProjectID, perm.Connect); err != nil || !ok {
		if err != nil {
			return err
		}
		return off(fmt.Sprintf("CONNECT missing for %s on the connection's folder", user.Email))
	}
	taken := false
	if err := pgx.BeginFunc(ctx, s.d.DB, func(tx pgx.Tx) error {
		var err error
		taken, err = s.takeSlot(ctx, tx, "datasources", "refresh_schedule", "next_refresh_at", id, ds.RefreshSchedule, seen, now)
		return err
	}); err != nil {
		return err
	}
	if !taken {
		return nil // another process took the slot
	}
	run, err := s.d.startRefreshIfFree(ctx, user, id, conn, "schedule", nil)
	if err != nil {
		return err
	}
	if run == 0 {
		s.d.Log.Info("scheduler: datasource is already refreshing, scheduled slot skipped", "datasource", id, "name", ds.Name)
		return nil
	}
	s.d.Log.Info("scheduler: scheduled refresh launched", "datasource", id, "name", ds.Name)
	return nil
}

// ── flows, flow groups, saved scripts: a run queued for an orchestrator ───────

type queuedSlot struct {
	table, owner   string // the schedulable's table; the runs' column naming it
	lock           int    // the advisory locks' namespace
	authorGone     string
	what           string // for the log
	engineMode     string // the run's engine_mode
	checkAuthority bool   // RUN on the folder checked again (groups, scripts)
}

var (
	flowSlot = queuedSlot{table: "flows", owner: "flow_id", lock: queueLock, authorGone: "the schedule's author is missing or disabled",
		what: "flow", engineMode: "production"}
	groupSlot = queuedSlot{table: "flow_groups", owner: "flow_group_id", lock: groupQueueLock,
		authorGone: "the schedule's author is gone or deactivated", what: "flow group", engineMode: "production", checkAuthority: true}
	scriptSlot = queuedSlot{table: "saved_scripts", owner: "saved_script_id", lock: scriptQueueLock,
		authorGone: "the schedule's author is gone or deactivated", what: "saved script", engineMode: "production", checkAuthority: true}
)

// scriptQueueLock is saved_script_runs._SCRIPT_QUEUE_LOCK.
const scriptQueueLock = 74610003

// fireQueued is _fire_flow, _fire_group and _fire_script: the author checked, the slot taken
// and the run queued in the same transaction (both or neither), unless one is going already.
func (s *scheduler) fireQueued(ctx context.Context, k queuedSlot, id int64, now, seen time.Time) error {
	var name string
	var projectID int64
	var envID *int64
	var spec *string
	var by *int64
	envCol := "NULL::bigint"
	if k.table == "saved_scripts" {
		envCol = "environment_id"
	}
	err := s.d.DB.QueryRow(ctx, fmt.Sprintf(`SELECT name, project_id, %s, run_schedule, run_scheduled_by FROM %s WHERE id = $1`,
		envCol, k.table), id).Scan(&name, &projectID, &envID, &spec, &by)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	off := func(why string) error {
		return s.disable(ctx, k.table, "run_schedule", "run_scheduled_by", "next_run_at", id, why)
	}
	user, err := loadUserOpt(ctx, s.d.DB, by)
	if err != nil {
		return err
	}
	if user == nil || !user.IsActive {
		return off(k.authorGone)
	}
	if k.checkAuthority {
		c := perm.For(s.d.DB, user)
		see := true
		if k.table == "saved_scripts" {
			if see, err = savedscripts.CanSee(ctx, c, s.d.DB, user.ID, projectID, *envID); err != nil {
				return err
			}
		}
		run := false
		if see {
			if run, err = c.Has(ctx, projectID, perm.Run); err != nil {
				return err
			}
		}
		if !run {
			if k.table == "saved_scripts" {
				return off(fmt.Sprintf("%s can no longer run the script", user.Email))
			}
			return off(fmt.Sprintf("RUN missing for %s on the group's folder", user.Email))
		}
	}
	queued, running := false, false
	err = pgx.BeginFunc(ctx, s.d.DB, func(tx pgx.Tx) error {
		taken, err := s.takeSlot(ctx, tx, k.table, "run_schedule", "next_run_at", id, spec, seen, now)
		if err != nil || !taken {
			if err == nil {
				err = errRollback // another process took the slot
			}
			return err
		}
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, $2)`, k.lock, int32(id)); err != nil {
			return err
		}
		var one int
		err = tx.QueryRow(ctx, fmt.Sprintf(`SELECT id FROM runs WHERE kind = 'orchestration' AND %s = $1
			AND status IN ('PENDING', 'STARTED') LIMIT 1`, k.owner), id).Scan(&one)
		if err == nil {
			running = true
			return nil // the slot spent, the run skipped
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if _, err := tx.Exec(ctx, fmt.Sprintf(`INSERT INTO runs (kind, %s, task_id, status, launched_by, trigger_type, engine_mode,
			input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ('orchestration', $1, '', 'PENDING', $2, 'schedule', $3, '', '', '', '', false, '[]', $4)`, k.owner),
			id, user.ID, k.engineMode, jobs.Now()); err != nil {
			return err
		}
		queued = true
		return nil
	})
	if errors.Is(err, errRollback) {
		return nil
	}
	if err != nil {
		return err
	}
	if running {
		s.d.Log.Info("scheduler: still running, slot skipped", "what", k.what, "id", id, "name", name)
	}
	if queued {
		wakeQueue()
		s.d.Log.Info("scheduler: run queued", "what", k.what, "id", id, "name", name)
	}
	return nil
}

// wakeQueue is orchestrator.wake: the queue of this process looks at once (queue_loop).
var wakeQueue = func() {}

// ── SQL script: writing its datasource ────────────────────────────────────────

// fireSQLScript is _fire_sql_script: the slot taken, then the write started with the
// author's authority; a write that cannot start recorded as a failed run.
func (s *scheduler) fireSQLScript(ctx context.Context, id int64, now, seen time.Time) error {
	script, err := sqlscripts.Get(ctx, s.d.DB, id)
	if err != nil || script == nil {
		return err
	}
	var spec *string
	var by *int64
	if err := s.d.DB.QueryRow(ctx, `SELECT run_schedule, run_scheduled_by FROM sql_scripts WHERE id = $1`, id).Scan(&spec, &by); err != nil {
		return err
	}
	off := func(why string) error {
		return s.disable(ctx, "sql_scripts", "run_schedule", "run_scheduled_by", "next_run_at", id, why)
	}
	user, err := loadUserOpt(ctx, s.d.DB, by)
	if err != nil {
		return err
	}
	if user == nil || !user.IsActive {
		return off("the schedule's author is gone or deactivated")
	}
	c := perm.For(s.d.DB, user)
	see, err := sqlscripts.CanSee(ctx, c, script)
	if err != nil {
		return err
	}
	run := false
	if see {
		if run, err = c.Has(ctx, script.ProjectID, perm.Run); err != nil {
			return err
		}
	}
	if !run {
		return off(fmt.Sprintf("%s can no longer run the script", user.Email))
	}
	taken := false
	if err := pgx.BeginFunc(ctx, s.d.DB, func(tx pgx.Tx) error {
		var err error
		taken, err = s.takeSlot(ctx, tx, "sql_scripts", "run_schedule", "next_run_at", id, spec, seen, now)
		return err
	}); err != nil {
		return err
	}
	if !taken {
		return nil
	}
	if script, err = sqlscripts.Get(ctx, s.d.DB, id); err != nil || script == nil {
		return err
	}
	_, refusal, err := s.d.launchSQLScript(ctx, user, script, "schedule", nil)
	if refusal != nil {
		if refusal.Status == http.StatusConflict && refusal.Detail == runlaunch.SQLScriptAlreadyRunning {
			s.d.Log.Info("scheduler: SQL script still writing, slot skipped", "script", id, "name", script.Name)
			return nil
		}
		s.d.Log.Warn("scheduler: SQL script not written", "script", id, "name", script.Name, "detail", refusal.Detail)
		return s.d.failedSQLRun(ctx, user, id, "schedule",
			fmt.Sprintf("Scheduled write not started, with the rights of %s: %s", user.Email, refusal.Detail))
	}
	if err != nil {
		s.d.Log.Error("scheduler: SQL script not written", "script", id, "name", script.Name, "err", err)
		why := "internal error"
		if name := transportErrorName(err); name != "" {
			why = "the engine could not be reached"
			return s.d.failedSQLRun(ctx, user, id, "schedule", fmt.Sprintf("Scheduled write not started: %s (%s)", why, name))
		}
		return s.d.failedSQLRun(ctx, user, id, "schedule", fmt.Sprintf("Scheduled write not started: %s (%s)", why, pyErrorName(err)))
	}
	s.d.Log.Info("scheduler: SQL script writing its datasource", "script", id, "name", script.Name)
	return nil
}

// transportErrorName is the httpx.TransportError's class name of an error reaching the
// engine, "" when it is not one.
func transportErrorName(err error) string {
	var ue *url.Error
	if !errors.As(err, &ue) {
		return ""
	}
	return httpxName(err)
}

// pyErrorName is type(e).__name__ of what is not a transport error.
func pyErrorName(err error) string {
	var je *pyJSONError
	if errors.As(err, &je) {
		return "JSONDecodeError"
	}
	return "Exception"
}

// pyJSONError is an answer of the engine that is not JSON (json.JSONDecodeError).
type pyJSONError struct{ err error }

func (e *pyJSONError) Error() string { return e.err.Error() }

// launchSQLScript is sql_script_runs.launch (runlaunch.LaunchSQLScript): the run's id, or
// the refusal (an HTTPException in Python). A script on the catalog is read by the AI
// service (sqlglot). The engine down comes back as the transport error it is.
func (d *Deps) launchSQLScript(ctx context.Context, user *auth.User, script *sqlscripts.Script, trigger string,
	parent *int64) (int64, *auth.Refusal, error) {
	id, err := runlaunch.LaunchSQLScript(ctx, runlaunch.SQLEnv{FlowEnv: d.flowEnv(), SQL: d.SQL}, user, script, trigger, parent, false)
	var ar *auth.Refusal
	var down *runlaunch.EngineDown
	switch {
	case err == nil:
		return id, nil, nil
	case errors.As(err, &ar):
		return 0, ar, nil
	case errors.As(err, &down):
		return 0, nil, down.Err
	}
	if ref := sqlRefusal(err); ref != nil {
		return 0, ref, nil
	}
	return 0, nil, err
}

func pyStripText(s string) string { return pystr.Strip(s) }

// stripSQLEnd is `.strip().rstrip(";").strip()`.
func stripSQLEnd(s string) string {
	s = pyStripText(s)
	for len(s) > 0 && s[len(s)-1] == ';' {
		s = s[:len(s)-1]
	}
	return pyStripText(s)
}

// failedSQLRun is sql_script_runs.failed_run: a write that could not start, kept as a failed
// run with its reason.
func (d *Deps) failedSQLRun(ctx context.Context, user *auth.User, scriptID int64, trigger, reason string) error {
	var by *int64
	if user != nil {
		by = &user.ID
	}
	// started_at is the model's default (models/run.py _now); finished_at the
	// datetime.now that failed_run imports inside itself: both the real clock in production
	_, err := d.DB.Exec(ctx, `INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, error, input_key,
		output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at)
		VALUES ('sql', $1, '', 'FAILURE', $2, $3, $4, '', '', '', '', false, '[]', $5, $6)`, scriptID, by, trigger, runes(reason, 2000),
		jobs.Now(), time.Now().UTC())
	return err
}

// ── the engine's timeouts ─────────────────────────────────────────────────────

func withCheck(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, engine.Check)
}

// sqlRefusal is the HTTPException a SQL on the catalog raises (pick_engine, resolve,
// Unreadable): its 422 (or the engine policy's status) and its detail.
func sqlRefusal(err error) *auth.Refusal {
	var ref *reports.Refusal
	var sr *sqlscripts.Refusal
	var su *sqlscripts.Unreadable
	var ru *reports.Unreadable
	switch {
	case errors.As(err, &ref):
		return &auth.Refusal{Status: ref.Status, Detail: pyjson.Str(ref.Detail)}
	case errors.As(err, &sr):
		return &auth.Refusal{Status: 422, Detail: sr.Message}
	case errors.As(err, &su):
		return &auth.Refusal{Status: 422, Detail: su.Message}
	case errors.As(err, &ru):
		return &auth.Refusal{Status: 422, Detail: ru.Message}
	}
	return nil
}
