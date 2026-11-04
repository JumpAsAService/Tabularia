package api

// services/notebook_runtime.py reaper_loop: on Kubernetes, the notebooks' pods looked
// after every 30 seconds — the idle ones and those gone from the cluster stopped (an
// editor's file kept as the notebook's draft first, and at every round), the ones that do
// not start failed saying why, the visitors nobody refreshed cleared, the Jobs nobody holds
// deleted. It returns at once where the scripting environments are off.
//
// One reaper at a time: the session advisory lock 7402140, held for the whole round on a
// connection of its own (the round commits as it goes), as the Python reaper holds it.
//
// The session's steps are notebook_runtime.go's, the ones the routes use (nbRefresh, nbAlive,
// nbSyncDraft, nbStop).
//
// Not ported: the OpenLineage events of the sessions that end (openlineage.notebook_session).
// With OpenLineage on, the loop is left to the Python gateway's orchestrator, as the routes
// that tell the catalog are left to it.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	nrLock      = 7_402_140        // LOCK: the reaper's round
	nrReapEvery = 30 * time.Second // REAP_SECONDS
	nrComponent = "app.kubernetes.io/component=notebook"
)

func init() {
	RegisterJob("notebooks_loop", func(d *Deps) jobs.Job {
		n := &notebookReaper{d: d, db: d.DB, log: d.Log, panel: d.Panel, cfg: d.Config.Jobs.Kube, s: scriptcfg.Load(config.FromOS()),
			lineage: d.Config.OpenLineage}
		return jobs.Job{Order: 11, Loop: n.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_round
			"notebooks": func(ctx context.Context) (any, error) {
				if n.s.DisabledReason() != "" {
					return nil, nil
				}
				return nil, n.oneRound(ctx)
			},
		}}
	})
}

type notebookReaper struct {
	d       *Deps
	db      *pgxpool.Pool
	log     *slog.Logger
	panel   *settings.Panel
	cfg     config.KubeJobsConfig
	s       *scriptcfg.Settings
	lineage bool
}

// oneRound is notebook_runtime.one_round.
func (n *notebookReaper) oneRound(ctx context.Context) error {
	conn, err := n.db.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	var mine bool
	if err := conn.QueryRow(ctx, `SELECT pg_try_advisory_lock($1)`, int64(nrLock)).Scan(&mine); err != nil {
		return err
	}
	if !mine {
		return nil
	}
	defer func() {
		// a context of its own: the lock goes even when the round's was cancelled
		_, _ = conn.Exec(context.WithoutCancel(ctx), `SELECT pg_advisory_unlock($1)`, int64(nrLock))
	}()
	k := kubeAPI()
	// the Jobs first: one created after this listing has its row already
	listed, err := kube.ListJobs(ctx, k, n.s.Scripts.Namespace, nrComponent)
	var ke *kube.Error
	if errors.As(err, &ke) {
		n.log.Warn(fmt.Sprintf("notebooks: the Jobs not listed (%s)", ke.Message))
		listed = nil
	} else if err != nil {
		return err
	}
	ok := err == nil
	byName := map[any]any{}
	var names []any // in the listing's order, as a dict keeps them
	for _, j := range listed {
		name := kube.Dict(kube.Dict(j).Value("metadata")).Value("name")
		if _, seen := byName[name]; !seen {
			names = append(names, name)
		}
		byName[name] = j
	}
	idle := float64(n.cfg.NotebooksIdleMinutes * 60)
	rows, err := n.db.Query(ctx, `SELECT `+nbSessionColumns+` FROM notebook_sessions ORDER BY id`)
	if err != nil {
		return err
	}
	var sessions []*nbSession
	for rows.Next() {
		s, err := nbScanSession(rows)
		if err != nil {
			rows.Close()
			return err
		}
		sessions = append(sessions, s)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, row := range sessions {
		var jobsByName map[any]any
		if ok {
			jobsByName = byName
		}
		if err := n.reap(ctx, row, jobsByName, idle); err != nil {
			if errors.As(err, &ke) {
				n.log.Warn(fmt.Sprintf("notebooks: session %d not checked (%s)", row.ID, ke.Message))
				continue
			}
			return err
		}
	}
	// the visitors nobody refreshed (their replica died): they count no more already
	if _, err := n.db.Exec(ctx, `DELETE FROM notebook_visitors WHERE last_seen_at < $1`, nbNaiveNow().Add(-nbVisitorStale)); err != nil {
		return err
	}
	if !ok {
		return nil
	}
	held := map[string]bool{}
	hr, err := n.db.Query(ctx, `SELECT job_name FROM notebook_sessions`)
	if err != nil {
		return err
	}
	for hr.Next() {
		var name string
		if err := hr.Scan(&name); err != nil {
			hr.Close()
			return err
		}
		held[name] = true
	}
	hr.Close()
	if err := hr.Err(); err != nil {
		return err
	}
	for _, key := range names {
		name, _ := key.(string)
		if !pyjson.Truthy(key) || held[name] || kube.JobOutcome(byName[key]) != "" {
			continue
		}
		n.log.Info(fmt.Sprintf("notebooks: the Job %s has no session: deleted", name))
		if err := kube.DeleteJob(ctx, k, n.s.Scripts.Namespace, name); err != nil && !errors.As(err, &ke) {
			return err
		}
	}
	return nil
}

// reap is notebook_runtime._reap. `jobsByName` nil: the Jobs could not be listed.
func (n *notebookReaper) reap(ctx context.Context, row *nbSession, jobsByName map[any]any, idle float64) error {
	if row.Status == "failed" {
		if nbAge(row.StartedAt) > idle {
			return nbStop(ctx, n.d, row, nil, "failed", true)
		}
		return nil
	}
	if row.Status == "starting" {
		return nbRefresh(ctx, n.d, row, true)
	}
	var job any = pyjson.Object{} // not listed: nothing known of it
	gone := false                 // `job is None`: listed, and not there
	if jobsByName != nil {
		var there bool
		job, there = jobsByName[row.JobName]
		gone = !there
	}
	if gone || (pyjson.Truthy(job) && kube.JobOutcome(job) != "") {
		return nbStop(ctx, n.d, row, nil, "ended", false)
	}
	if nbAge(row.LastSeenAt) > idle {
		return nbStop(ctx, n.d, row, nil, "idle", false)
	}
	alive, err := nbAlive(ctx, n.d, row)
	if err != nil || !alive {
		return err
	}
	if row.Mode == "edit" {
		return nbSyncDraft(ctx, n.d, row)
	}
	return nil
}

// loop is reaper_loop.
func (n *notebookReaper) loop(ctx context.Context) {
	if n.s.DisabledReason() != "" {
		return
	}
	if n.lineage {
		n.log.Info("notebooks: OpenLineage is on, the notebooks' pods are left to the Python gateway's orchestrator")
		return
	}
	for ctx.Err() == nil {
		if err := n.oneRound(ctx); err != nil && !errors.Is(err, context.Canceled) {
			// database or cluster down: at the next round
			n.log.Warn(fmt.Sprintf("notebooks: the pods not checked (%v), again in %ds", err, int(nrReapEvery/time.Second)))
		}
		if !jobs.Sleep(ctx, nrReapEvery) {
			break
		}
	}
}
