package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// services/orchestrator.py queue_loop: the queue of the orchestrations (a flow's run, queued
// by «Run now» or by the scheduler) taken by this process and carried to the end, its
// heartbeat, and the runs of processes that are gone closed.
//
// What the Go queue does not carry yet (see the report of the port): a flow group's run
// (flow_group_runner.run_group), a saved script's (saved_script_runs.run_queued), and a flow
// whose definition (or a sub-flow's) has a Script or Container node (script_runs,
// container_runs: Kubernetes Jobs). It leaves those queued, untouched, for a Python
// orchestrator: claim skips them.

const (
	maxFlowDepth       = 5    // MAX_FLOW_DEPTH
	orphanSweep        = 15.0 // ORPHAN_SWEEP_S
	flowAlreadyRunning = "flow already running"
	orchInterrupted    = "Orchestration interrupted: the process running it stopped and the remaining steps " +
		"were not run. Check what has already been produced and run the flow again."
	stepInterrupted = "Stopped: the orchestration this step belonged to was interrupted (the process running " +
		"it stopped). Check what has already been produced and run the flow again."
)

func init() {
	RegisterJob("queue_loop", func(d *Deps) jobs.Job {
		q := newQueue(d)
		return jobs.Job{Order: 2, Loop: q.loop, Ticks: map[string]jobs.Tick{
			// a round of the loop: what is free taken from the queue, the taken ones carried
			// to their end (one after the other, so the round is deterministic), the
			// refused closed
			"queue": q.roundTick,
			// close_orphans
			"queue.orphans": func(ctx context.Context) (any, error) { return q.closeOrphans(ctx) },
			// close_mine at start, then stop_steps of each
			"queue.mine_start": func(ctx context.Context) (any, error) {
				ids, err := q.closeMine(ctx, true)
				for _, id := range ids {
					q.stopSteps(ctx, id)
				}
				return ids, err
			},
			// close_mine at stop
			"queue.mine_stop": func(ctx context.Context) (any, error) { return q.closeMine(ctx, false) },
			// a heartbeat
			"queue.beat": func(ctx context.Context) (any, error) { return q.beat(ctx) },
		}}
	})
}

type queue struct {
	d    *Deps
	sc   *scriptcfg.Settings
	js   jobSettings
	pool int64 // the pool's size + overflow at start, -1 without a ceiling

	mu      sync.Mutex
	alive   map[int64]bool // _alive
	running int            // len(_running)
	wake    chan struct{}

	okSince       time.Time // _heartbeat_ok_since (zero: not healthy)
	lastThreshold *float64
	firstThresh   *[2]float64 // (threshold, since: unix seconds)
}

func newQueue(d *Deps) *queue {
	q := &queue{d: d, sc: scriptcfg.Load(config.FromOS()), js: loadJobSettings(), alive: map[int64]bool{}, wake: make(chan struct{}, 1)}
	q.pool = poolCeiling(d)
	return q
}

// poolCeiling is engine.pool.size() + max_overflow: db.pool_size (or app.gateway_threads) and
// db.max_overflow, from the environment and, read once at start, the panel (overrides.load_at_start).
func poolCeiling(d *Deps) int64 {
	env := config.FromOS()
	read := func(name string, def int64) int64 {
		if v, ok := env(name); ok {
			var n int64
			if _, err := fmt.Sscan(strings.TrimSpace(v), &n); err == nil {
				return n
			}
		}
		return def
	}
	size, threads, overflow := read("DB__POOL_SIZE", 0), read("APP__GATEWAY_THREADS", 40), read("DB__MAX_OVERFLOW", 10)
	if d.DB != nil {
		rows, err := d.DB.Query(context.Background(), `SELECT key, value FROM gateway_settings WHERE key IN ('db.pool_size',
			'app.gateway_threads', 'db.max_overflow')`)
		if err == nil {
			for rows.Next() {
				var k, v string
				if rows.Scan(&k, &v) != nil {
					continue
				}
				n, ok := pyjson.IsInt(func() any { x, _ := pyjson.Loads(v); return x }())
				if !ok {
					continue
				}
				switch k {
				case "db.pool_size":
					size = n
				case "app.gateway_threads":
					threads = n
				case "db.max_overflow":
					overflow = n
				}
			}
			rows.Close()
		}
	}
	if size == 0 {
		size = threads
	}
	if overflow < 0 {
		return -1
	}
	return size + overflow
}

// concurrency is concurrency().
func (q *queue) concurrency() int64 {
	wanted := q.d.Panel.MaxConcurrent()
	if q.pool < 0 {
		return wanted
	}
	return max(1, min(wanted, q.pool))
}

// wakeUp is wake: the queue looks at once.
func (q *queue) wakeUp() {
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

// ── the loop ──────────────────────────────────────────────────────────────────

func (q *queue) loop(ctx context.Context) {
	jobs.ResetDrain()
	wakeQueue = q.wakeUp
	defer func() { wakeQueue = func() {} }()
	hbCtx, stopHeartbeat := context.WithCancel(context.Background())
	go q.heartbeat(hbCtx)
	q.d.Log.Info("orchestrator started", "instance", jobs.Instance, "queue_every", q.d.Panel.QueuePoll(), "together", q.concurrency())
	// the runs of an earlier start of this process
	if previous, err := q.closeMine(context.Background(), true); err != nil {
		q.d.Log.Error("orchestrator: closing the earlier runs failed", "err", err)
	} else {
		for _, id := range previous {
			q.stopSteps(context.Background(), id)
		}
	}
	// the runs carry on until the loop stops; then their context ends, as the tasks are cancelled
	runCtx, cancelRuns := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	var lastSearch time.Time
	for ctx.Err() == nil {
		select {
		case <-q.wake:
		default:
		}
		q.mu.Lock()
		free := q.concurrency() - int64(q.running) - runningCells
		q.mu.Unlock()
		if free > 0 && !jobs.Draining() {
			taken, refused := q.claim(ctx, free)
			for _, id := range taken {
				q.start(runCtx, &wg, id)
			}
			for _, id := range refused {
				q.finalize(ctx, id, "FAILURE", strp(flowAlreadyRunning), true)
			}
		}
		if time.Since(lastSearch).Seconds() >= orphanSweep && q.heartbeatHealthy() {
			lastSearch = time.Now()
			if _, err := q.closeOrphans(ctx); err != nil {
				q.d.Log.Error("orchestrator: error in the queue's round", "err", err)
			}
		}
		select {
		case <-ctx.Done():
		case <-q.wake:
		case <-time.After(jobs.Seconds(q.d.Panel.QueuePoll())):
		}
	}
	stopHeartbeat()
	cancelRuns()
	wg.Wait()
	if _, err := q.closeMine(context.Background(), false); err != nil {
		q.d.Log.Error("orchestrator: final closing failed", "err", err)
	}
	q.d.Log.Info("orchestrator stopped", "instance", jobs.Instance)
}

// runningCells is orchestrator.running_cells: the flow groups' cells running here (the Go
// queue runs no groups yet).
const runningCells = 0

// start is _start: the run carried on in a goroutine of its own, alive for the heartbeat.
func (q *queue) start(ctx context.Context, wg *sync.WaitGroup, id int64) {
	q.mu.Lock()
	q.alive[id] = true
	q.running++
	q.mu.Unlock()
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer func() {
			q.mu.Lock()
			delete(q.alive, id)
			q.running--
			q.mu.Unlock()
		}()
		q.runIt(ctx, id)
	}()
}

// roundTick is one round of the loop, its runs carried to their end before it returns.
func (q *queue) roundTick(ctx context.Context) (any, error) {
	free := q.concurrency() - runningCells
	if free <= 0 || jobs.Draining() {
		return pyjson.Object{{Key: "taken", Value: []any{}}, {Key: "refused", Value: []any{}}}, nil
	}
	taken, refused := q.claim(ctx, free)
	for _, id := range taken {
		q.mu.Lock()
		q.alive[id] = true
		q.mu.Unlock()
		q.runIt(ctx, id)
		q.mu.Lock()
		delete(q.alive, id)
		q.mu.Unlock()
	}
	for _, id := range refused {
		q.finalize(ctx, id, "FAILURE", strp(flowAlreadyRunning), true)
	}
	t, r := make([]any, len(taken)), make([]any, len(refused))
	for i, x := range taken {
		t[i] = x
	}
	for i, x := range refused {
		r[i] = x
	}
	return pyjson.Object{{Key: "taken", Value: t}, {Key: "refused", Value: r}}, nil
}

// ── taking from the queue ─────────────────────────────────────────────────────

// claim is claim: up to `limit` orchestrations signed as this process's, and the manual
// launches of a flow running already (to close as failed).
func (q *queue) claim(ctx context.Context, limit int64) ([]int64, []int64) {
	type cand struct {
		id                  int64
		trigger             string
		flow, group, script *int64
	}
	rows, err := q.d.DB.Query(ctx, `SELECT id, trigger_type, flow_id, flow_group_id, saved_script_id FROM runs
		WHERE kind = 'orchestration' AND status = 'PENDING' ORDER BY id LIMIT $1`, limit+20)
	if err != nil {
		q.d.Log.Error("orchestrate: the queue cannot be read", "err", err)
		return nil, nil
	}
	var cands []cand
	for rows.Next() {
		var c cand
		if rows.Scan(&c.id, &c.trigger, &c.flow, &c.group, &c.script) == nil {
			cands = append(cands, c)
		}
	}
	rows.Close()
	var taken, refused []int64
	for _, c := range cands {
		if int64(len(taken)) >= limit {
			break
		}
		if !q.carries(ctx, c.flow, c.group, c.script) {
			continue // left to a Python orchestrator
		}
		tag, err := q.d.DB.Exec(ctx, `UPDATE runs SET status = 'STARTED', claimed_by = $2, heartbeat_at = $3 WHERE id = $1 AND status = 'PENDING'`,
			c.id, jobs.Instance, jobs.Now())
		var pe *pgconn.PgError
		if errors.As(err, &pe) && strings.HasPrefix(pe.Code, "23") {
			if c.trigger == "schedule" {
				// a slot that fired while the previous round had not finished: skipped without a trace
				if _, err := q.d.DB.Exec(ctx, `DELETE FROM runs WHERE id = $1 AND status = 'PENDING'`, c.id); err != nil {
					q.d.Log.Error("orchestrate: the skipped slot not removed", "run", c.id, "err", err)
				}
				q.d.Log.Info("orchestrate: flow already running: slot skipped", "run", c.id)
			} else {
				refused = append(refused, c.id)
			}
			continue
		}
		if err != nil {
			q.d.Log.Error("orchestrate: taking from the queue interrupted", "run", c.id, "err", err)
			break
		}
		if tag.RowsAffected() == 1 {
			taken = append(taken, c.id)
		}
	}
	return taken, refused
}

// carries says whether the Go queue runs this orchestration (see the file's comment).
func (q *queue) carries(ctx context.Context, flow, group, script *int64) bool {
	if group != nil {
		return false
	}
	if script != nil {
		return true
	}
	if flow == nil {
		return true // run_it closes it as invalid
	}
	seen := map[int64]bool{}
	var walk func(id int64, depth int) bool
	walk = func(id int64, depth int) bool {
		if seen[id] || depth > maxFlowDepth {
			return true
		}
		seen[id] = true
		f, err := loadJobFlow(ctx, q.d.DB, id)
		if err != nil || f == nil {
			return err == nil
		}
		v, err := pyjson.Loads(pyjson.Str(pyjson.Or(f.Definition, "{}")))
		if err != nil {
			return true
		}
		for _, n := range objectList(pyjson.Obj(v).Value("nodes")) {
			switch n.Value("type") {
			case "runflow":
				if sub, ok := pyjson.IsInt(nodeData(n).Value("flowId")); ok && !walk(sub, depth+1) {
					return false
				}
			}
		}
		return true
	}
	return walk(*flow, 0)
}

// ── carrying a run ────────────────────────────────────────────────────────────

// orchestrationError is OrchestrationError (StopSequence too): the run fails with its text.
type orchestrationError struct{ msg string }

func (e *orchestrationError) Error() string { return e.msg }

// errNoLongerOurs is NoLongerOurs: another closed the run; nothing to record.
var errNoLongerOurs = errors.New("the orchestration no longer belongs to this process")

// runIt is run_it for a flow's orchestration.
func (q *queue) runIt(ctx context.Context, id int64) {
	err := q.runFlow(ctx, id)
	if err == nil {
		return
	}
	if ctx.Err() != nil {
		// the process is stopping: written at once, and the steps started are stopped (interrupt)
		bg := context.Background()
		q.finalize(bg, id, "FAILURE", strp(orchInterrupted), false)
		q.stopSteps(bg, id)
		return
	}
	q.d.Log.Error("orchestrate: run failed", "run", id, "err", err)
	q.finalize(ctx, id, "FAILURE", strp("internal error during the orchestration"), false)
}

func (q *queue) runFlow(ctx context.Context, id int64) error {
	req := q.d.jobRequest(ctx, nil)
	run, err := req.readRun(id)
	if err != nil || run == nil {
		return err
	}
	if run.SavedScriptID != nil {
		// a saved script's run: same queue, claim and heartbeat, its own runner
		return q.runSavedQueued(ctx, id, run)
	}
	var flow *jobFlow
	if run.FlowID != nil && *run.FlowID != 0 {
		if flow, err = loadJobFlow(ctx, q.d.DB, *run.FlowID); err != nil {
			return err
		}
	}
	user, err := loadUserOpt(ctx, q.d.DB, run.LaunchedBy)
	if err != nil {
		return err
	}
	if flow == nil || user == nil || !user.IsActive {
		q.d.Log.Warn("orchestrate: flow or user not valid", "run", id)
		q.finalize(ctx, id, "FAILURE", strp("invalid flow or user"), false)
		return nil
	}
	if run.TriggerType == "schedule" {
		gaps, err := q.gapsOfFlow(ctx, user, flow, map[int64]bool{})
		if err != nil {
			return err
		}
		if len(gaps) > 0 {
			q.d.Log.Warn("orchestrate: scheduled run not started", "run", id, "gaps", gaps)
			q.finalize(ctx, id, "FAILURE", strp(notStarted(user, gaps)), false)
			return nil
		}
	}
	mode := "development"
	if run.EngineMode != nil && *run.EngineMode != "" {
		mode = *run.EngineMode
	}
	if _, err := q.d.DB.Exec(ctx, `UPDATE runs SET engine = $2 WHERE id = $1`, id, resolveRunEngine(flow, mode)); err != nil {
		return err
	}
	errs, err := q.orchestrate(ctx, user, flow, 0, map[int64]bool{}, run.TriggerType, id, mode, true)
	var oe *orchestrationError
	var fe *flowResolveError
	switch {
	case errors.Is(err, errNoLongerOurs):
		q.d.Log.Warn("orchestrate: stopping", "flow", flow.ID, "err", err)
		return nil
	case errors.As(err, &oe) || errors.As(err, &fe):
		q.d.Log.Warn("orchestrate: flow interrupted", "flow", flow.ID, "err", err)
		q.finalize(ctx, id, "FAILURE", strp(err.Error()), false)
		return nil
	case err != nil:
		return err
	}
	if len(errs) > 0 {
		q.finalize(ctx, id, "FAILURE", strp(strings.Join(errs, "; ")), false)
	} else {
		q.finalize(ctx, id, "SUCCESS", nil, false)
	}
	return nil
}

// stillMine is _still_mine.
func (q *queue) stillMine(ctx context.Context, id int64) error {
	var status string
	var by *string
	err := q.d.DB.QueryRow(ctx, `SELECT status, claimed_by FROM runs WHERE id = $1`, id).Scan(&status, &by)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && (status != "STARTED" || by == nil || *by != jobs.Instance)) {
		return fmt.Errorf("%w: orchestration %d", errNoLongerOurs, id)
	}
	return err
}

// orchestrate is orchestrate: the action nodes in their sequence; the errors that do not
// stop it, collected.
func (q *queue) orchestrate(ctx context.Context, user *auth.User, flow *jobFlow, depth int, seen map[int64]bool, trigger string,
	parent int64, mode string, guarded bool) ([]string, error) {
	if seen[flow.ID] || depth > maxFlowDepth {
		q.d.Log.Warn("orchestrate: cycle or excessive depth", "flow", flow.ID, "depth", depth)
		return []string{fmt.Sprintf("flow %d: cycle or excessive depth", flow.ID)}, nil
	}
	next := map[int64]bool{flow.ID: true}
	for k := range seen {
		next[k] = true
	}
	text := flow.Definition
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return nil, err
	}
	definition, ok := v.(pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(v))
	}
	bucket := q.d.Config.EngineBucket
	resolveDS := func(i any) *[2]any {
		n, ok := pyjson.IsInt(i)
		if !ok {
			return nil
		}
		var b, k string
		if q.d.DB.QueryRow(ctx, `SELECT bucket, key FROM datasources WHERE id = $1`, n).Scan(&b, &k) != nil || k == "" {
			return nil
		}
		return &[2]any{b, k}
	}
	var errs []string
	parentID := &parent
	outputs := map[string]*[2]any{} // script_outputs
	for _, node := range sequenceOrder(definition) {
		d := nodeData(node)
		if guarded {
			if err := q.stillMine(ctx, parent); err != nil {
				return nil, err
			}
		}
		switch node.Value("type") {
		case "refresh":
			if ds := d.Value("datasourceId"); pyjson.Truthy(ds) {
				if err := q.refreshAndWait(ctx, user, ds, trigger, parentID); err != nil {
					return nil, err
				}
			}
		case "output":
			stops := d.Value("destType") == "email" && d.Value("stopOnFailure") != false
			err := func() error {
				req, err := resolveOutputRequest(definition, node, resolveDS, bucket, mode, int64(q.d.Panel.PreviewSampleRows()), outputs)
				if err != nil {
					return err
				}
				runID, err := q.d.launchFlowRun(ctx, user, flow, req, trigger, parentID, mode)
				if err != nil {
					return err
				}
				run, err := q.waitRun(ctx, runID)
				if err != nil {
					return err
				}
				if run.Status != "SUCCESS" {
					why := run.Status
					if run.Error != nil && *run.Error != "" {
						why = *run.Error
					}
					if stops {
						return &stopSequence{fmt.Sprintf("email sending failed (run %d): %s. The next steps of the flow were not run.", run.ID, why)}
					}
					errs = append(errs, strings.TrimSpace(fmt.Sprintf("output: run %d %s — %s", run.ID, run.Status, deref(run.Error))))
				}
				return nil
			}()
			var stop *stopSequence
			if errors.As(err, &stop) || errors.Is(err, errNoLongerOurs) {
				return nil, err
			}
			if err != nil {
				if ctx.Err() != nil {
					return nil, err
				}
				q.d.Log.Warn("orchestrate: output not run", "flow", flow.ID, "err", err)
				if stops {
					return nil, &stopSequence{fmt.Sprintf("email not sent, refused at launch: %s. The next steps of the flow were not run.",
						errorDetail(err))}
				}
				errs = append(errs, "output: "+pyStrOfError(err))
			}
		case "script", "container":
			run := &scriptNodeRun{q: q, user: user, flow: flow, definition: definition, node: node, parent: parent, trigger: trigger,
				mode: mode, outputs: outputs, resolveDS: resolveDS}
			var out *[2]any
			var err error
			if node.Value("type") == "script" {
				out, err = q.runScript(ctx, run)
			} else {
				out, err = q.runContainer(ctx, run)
			}
			var sf *scriptFailed
			var fe *flowResolveError
			if errors.As(err, &sf) || errors.As(err, &fe) {
				return nil, &orchestrationError{err.Error()}
			}
			if err != nil {
				return nil, err
			}
			if out != nil {
				outputs[nodeKey(node.Value("id"))] = out
			}
		case "runflow":
			sub := d.Value("flowId")
			if !pyjson.Truthy(sub) {
				continue
			}
			var subFlow *jobFlow
			if n, ok := pyjson.IsInt(sub); ok {
				if subFlow, err = loadJobFlow(ctx, q.d.DB, n); err != nil {
					return nil, err
				}
			}
			if subFlow == nil {
				q.d.Log.Warn("orchestrate: missing runflow", "flow", flow.ID, "sub", sub)
				errs = append(errs, fmt.Sprintf("runflow: flow %s does not exist", pyjson.Str(sub)))
				continue
			}
			more, err := q.orchestrate(ctx, user, subFlow, depth+1, next, trigger, parent, mode, guarded)
			if err != nil {
				return nil, err
			}
			errs = append(errs, more...)
		}
	}
	return errs, nil
}

// stopSequence is StopSequence (an OrchestrationError).
type stopSequence struct{ msg string }

func (e *stopSequence) Error() string { return e.msg }

func (e *stopSequence) As(target any) bool {
	if t, ok := target.(**orchestrationError); ok {
		*t = &orchestrationError{e.msg}
		return true
	}
	return false
}

// waitsOf is services/waits.py waits: 0.25, 0.5, then every second for thirty seconds, then
// `cap`; never more than `cap`.
func waitsOf(limit float64) func() float64 {
	delay, waited := 0.25, 0.0
	return func() float64 {
		pause := delay
		if waited < 30 {
			pause = min(delay, 1.0)
		}
		pause = min(pause, limit)
		waited += pause
		delay *= 2
		return pause
	}
}

// waitRun is _wait_run: an output's run followed to its end.
func (q *queue) waitRun(ctx context.Context, id int64) (*runRow, error) {
	req := q.d.jobRequest(ctx, nil)
	next := waitsOf(q.d.Panel.PollInterval())
	for {
		run, err := req.readRun(id)
		if err != nil {
			return nil, err
		}
		if run, err = req.reconcileAny(run, q.sc); err != nil {
			return nil, err
		}
		if terminal[run.Status] {
			return run, nil
		}
		if !jobs.Sleep(ctx, jobs.Seconds(next())) {
			return nil, ctx.Err()
		}
	}
}

// refreshAndWait is _refresh_and_wait: a database datasource refreshed, its end awaited
// (bounded).
func (q *queue) refreshAndWait(ctx context.Context, user *auth.User, dsRef any, trigger string, parent *int64) error {
	id, ok := pyjson.IsInt(dsRef)
	if !ok {
		return nil
	}
	ds, err := loadJobDatasource(ctx, q.d.DB, id)
	if err != nil {
		return err
	}
	var kind string
	if ds != nil {
		if err := q.d.DB.QueryRow(ctx, `SELECT kind FROM datasources WHERE id = $1`, id).Scan(&kind); err != nil {
			return err
		}
	}
	if ds == nil || kind != "database" {
		return nil
	}
	c := perm.For(q.d.DB, user)
	if ok, err := c.Has(ctx, ds.ProjectID, perm.Run); err != nil || !ok {
		if err != nil {
			return err
		}
		return &orchestrationError{fmt.Sprintf("RUN missing for the refresh of datasource %d", id)}
	}
	var conn *engineConn
	if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
		if conn, err = loadEngineConn(ctx, q.d.DB, *ds.ConnectionID); err != nil {
			return err
		}
	}
	if conn == nil {
		return &orchestrationError{fmt.Sprintf("datasource %d: the connection does not exist", id)}
	}
	if ok, err := c.Has(ctx, conn.ProjectID, perm.Connect); err != nil || !ok {
		if err != nil {
			return err
		}
		return &orchestrationError{fmt.Sprintf("CONNECT missing for the refresh of datasource %d", id)}
	}
	var runID int64
	for range 3 {
		if runID, err = q.d.startRefreshIfFree(ctx, q.sc, user, id, conn, trigger, parent); err != nil {
			return err
		}
		if runID == 0 {
			err := q.d.DB.QueryRow(ctx, `SELECT id FROM runs WHERE datasource_id = $1 AND (kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE'))
				ORDER BY id DESC LIMIT 1`, id).Scan(&runID)
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}
		if runID != 0 {
			break
		}
	}
	if runID == 0 {
		return &orchestrationError{fmt.Sprintf("refresh of datasource %d could not be started", id)}
	}
	req := q.d.jobRequest(ctx, user)
	deadline := time.Now().Add(jobs.Seconds(max(float64(q.d.Panel.RefreshWaitSeconds()), q.d.Panel.PollInterval())))
	next := waitsOf(q.d.Panel.PollInterval())
	var run *runRow
	for {
		if run, err = req.readRun(runID); err != nil {
			return err
		}
		if run, err = req.reconcileAny(run, q.sc); err != nil {
			return err
		}
		if terminal[run.Status] || !time.Now().Before(deadline) {
			break
		}
		if !jobs.Sleep(ctx, jobs.Seconds(next())) {
			return ctx.Err()
		}
	}
	if run.Status != "SUCCESS" {
		return &orchestrationError{fmt.Sprintf("refresh of datasource %d failed (%s)", id, run.Status)}
	}
	return nil
}

// ── closing ───────────────────────────────────────────────────────────────────

// finalize is _finalize_orch_run: closed by whoever finds it still open; its webhooks in the
// same transaction, the failure notice after.
func (q *queue) finalize(ctx context.Context, id int64, status string, msg *string, onlyIfQueued bool) {
	cond := `status NOT IN ('SUCCESS', 'FAILURE')`
	if onlyIfQueued {
		cond = `status = 'PENDING'`
	}
	var text *string
	if msg != nil && *msg != "" {
		t := runes(*msg, 2000)
		text = &t
	}
	req := q.d.jobRequest(ctx, nil)
	closed := false
	err := pgx.BeginFunc(ctx, q.d.DB, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE runs SET status = $2, error = $3, finished_at = $4 WHERE id = $1 AND `+cond, id, status, text, jobs.Now())
		if err != nil || tag.RowsAffected() != 1 {
			return err
		}
		closed = true
		after, err := scanRunTx(ctx, tx, id)
		if err != nil {
			return err
		}
		return req.enqueueWebhooks(tx, after)
	})
	if err != nil {
		q.d.Log.Error("orchestrate: the run's outcome not written", "run", id, "err", err)
		return
	}
	if !closed {
		return
	}
	if run, err := req.readRun(id); err == nil && run != nil {
		q.d.notifyFailure(ctx, run)
	}
}

// stopSteps is stop_steps: the steps still open of an interrupted orchestration stopped.
func (q *queue) stopSteps(ctx context.Context, id int64) int64 {
	req := q.d.jobRequest(ctx, nil)
	ids, err := idsOf(ctx, q.d.DB, `SELECT id FROM runs WHERE parent_run_id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`, id)
	if err != nil {
		q.d.Log.Error("orchestrate: the interrupted orchestration's steps not read", "run", id, "err", err)
		return 0
	}
	var stopped int64
	for _, sid := range ids {
		err := func() error {
			run, err := req.readRun(sid)
			if err != nil || run == nil {
				return err
			}
			switch {
			case run.Kind == "script" || run.Kind == "container":
				_, err := req.closeIfOrphaned(run, q.sc)
				return err
			case run.Kind == "orchestration":
				tag, err := q.d.DB.Exec(ctx, `UPDATE runs SET status = 'FAILURE', error = $2, finished_at = $3 WHERE id = $1
					AND status NOT IN ('SUCCESS', 'FAILURE')`, sid, orchInterrupted, jobs.Now())
				if err != nil {
					return err
				}
				if tag.RowsAffected() == 1 {
					stopped += 1 + q.stopSteps(ctx, sid)
				}
				return nil
			}
			if run, err = req.reconcile(run, progressOf{}); err != nil {
				return err
			}
			if terminal[run.Status] {
				return nil
			}
			if run.TaskID != "" {
				cctx, cancel := withCheck(ctx)
				if _, _, err := q.d.Engine.JSON(cctx, http.MethodDelete, "/tasks/"+run.TaskID, nil); err != nil {
					q.d.Log.Warn("revoking a step's task failed", "run", sid, "err", err)
				}
				cancel()
			}
			tag, err := q.d.DB.Exec(ctx, `UPDATE runs SET status = 'FAILURE', error = $2, finished_at = $3 WHERE id = $1
				AND status NOT IN ('SUCCESS', 'FAILURE')`, sid, stepInterrupted, jobs.Now())
			if err != nil {
				return err
			}
			stopped += tag.RowsAffected()
			return nil
		}()
		if err != nil {
			q.d.Log.Error("orchestrate: step of the interrupted orchestration not stopped", "step", sid, "run", id, "err", err)
		}
	}
	if stopped > 0 {
		q.d.Log.Warn("orchestrate: steps of the interrupted orchestration stopped", "count", stopped, "run", id)
	}
	return stopped
}

// deathThreshold is death_threshold: dead_after_seconds, the old one for a while after it
// was lowered.
func (q *queue) deathThreshold() float64 {
	current, now := q.d.Panel.DeadAfter(), float64(time.Now().UnixNano())/1e9
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.lastThreshold != nil && current != *q.lastThreshold {
		q.firstThresh = &[2]float64{*q.lastThreshold, now}
	}
	q.lastThreshold = &current
	const rereadEvery = 10 // overrides.REREAD_EVERY
	if q.firstThresh != nil && now-q.firstThresh[1] < q.firstThresh[0]+rereadEvery {
		return max(current, q.firstThresh[0])
	}
	return current
}

// closeOrphans is close_orphans: the orchestrations whose heartbeat stopped closed, their
// steps stopped; those queued past the maximum time closed too.
func (q *queue) closeOrphans(ctx context.Context) (int64, error) {
	cutoff := jobs.Now().Add(-jobs.Seconds(q.deathThreshold()))
	q.mu.Lock()
	mine := []int64{-1}
	for id := range q.alive {
		mine = append(mine, id)
	}
	q.mu.Unlock()
	sort.Slice(mine, func(i, j int) bool { return mine[i] < mine[j] })
	orphan := `kind = 'orchestration' AND status = 'STARTED' AND (heartbeat_at IS NULL OR heartbeat_at < $1)
		AND NOT (claimed_by = $2 AND id = ANY($3))`
	ids, err := idsOf(ctx, q.d.DB, `SELECT id FROM runs WHERE `+orphan, cutoff, jobs.Instance, mine)
	if err != nil {
		return 0, err
	}
	req := q.d.jobRequest(ctx, nil)
	var closedOnes int64
	for _, id := range ids {
		closed := false
		err := pgx.BeginFunc(ctx, q.d.DB, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, `UPDATE runs SET status = 'FAILURE', error = $5, finished_at = $4 WHERE id = $6 AND `+orphan,
				cutoff, jobs.Instance, mine, jobs.Now(), orchInterrupted, id)
			if err != nil || tag.RowsAffected() != 1 {
				return err
			}
			closed = true
			after, err := scanRunTx(ctx, tx, id)
			if err != nil {
				return err
			}
			return req.enqueueWebhooks(tx, after)
		})
		if err != nil {
			return closedOnes, err
		}
		if !closed {
			continue
		}
		closedOnes++
		q.stopSteps(ctx, id)
		if run, err := req.readRun(id); err == nil && run != nil {
			q.d.notifyFailure(ctx, run)
		}
	}
	old := jobs.Now().Add(-jobs.Seconds(q.d.Panel.QueueTimeout()))
	queued, err := idsOf(ctx, q.d.DB, `SELECT id FROM runs WHERE kind = 'orchestration' AND status = 'PENDING' AND started_at < $1`, old)
	if err != nil {
		return closedOnes, err
	}
	for _, id := range queued {
		run, err := req.readRun(id)
		if err != nil {
			return closedOnes, err
		}
		if run != nil {
			if _, err := req.closeIfQueuedTooLong(run); err != nil {
				return closedOnes, err
			}
		}
	}
	if closedOnes > 0 {
		q.d.Log.Warn("orchestrate: orchestrations of a process that is gone, closed", "count", closedOnes)
	}
	return closedOnes, nil
}

// closeMine is close_mine: at start the runs of an earlier incarnation of this process (same
// host and pid, another signature), at stop all of its own.
func (q *queue) closeMine(ctx context.Context, onlyEarlier bool) ([]int64, error) {
	cond, args := `claimed_by = $1`, []any{jobs.Instance}
	if onlyEarlier {
		prefix := jobs.Instance[:strings.LastIndex(jobs.Instance, ":")+1]
		cond, args = `left(claimed_by, $2) = $3 AND claimed_by != $1`, []any{jobs.Instance, len([]rune(prefix)), prefix}
	}
	req := q.d.jobRequest(ctx, nil)
	var ids []int64
	err := pgx.BeginFunc(ctx, q.d.DB, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, fmt.Sprintf(`UPDATE runs SET status = 'FAILURE', error = $%d, finished_at = $%d
			WHERE kind = 'orchestration' AND status = 'STARTED' AND %s RETURNING id`, len(args)+1, len(args)+2, cond),
			append(args, orchInterrupted, jobs.Now())...)
		if err != nil {
			return err
		}
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		for _, id := range ids {
			after, err := scanRunTx(ctx, tx, id)
			if err != nil {
				return err
			}
			if err := req.enqueueWebhooks(tx, after); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if ids == nil {
		ids = []int64{}
	}
	return ids, nil
}

// beat is beat: «here» on the orchestrations this process is really running.
func (q *queue) beat(ctx context.Context) (int64, error) {
	q.mu.Lock()
	var ids []int64
	for id := range q.alive {
		ids = append(ids, id)
	}
	q.mu.Unlock()
	if len(ids) == 0 {
		var one int
		return 0, q.d.DB.QueryRow(ctx, `SELECT 1`).Scan(&one)
	}
	tag, err := q.d.DB.Exec(ctx, `UPDATE runs SET heartbeat_at = $1 WHERE id = ANY($2) AND kind = 'orchestration' AND status = 'STARTED'
		AND claimed_by = $3`, jobs.Now(), ids, jobs.Instance)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// heartbeat is start_heartbeat's thread: a beat every heartbeat_seconds (read each second).
func (q *queue) heartbeat(ctx context.Context) {
	last := time.Now()
	for jobs.Sleep(ctx, time.Second) {
		if time.Since(last).Seconds() < q.d.Panel.Heartbeat() {
			continue
		}
		last = time.Now()
		_, err := q.beat(ctx)
		q.mu.Lock()
		if err != nil {
			q.okSince = time.Time{}
			q.d.Log.Error("orchestrate: heartbeat failed", "err", err)
		} else if q.okSince.IsZero() {
			q.okSince = time.Now()
		}
		q.mu.Unlock()
	}
}

// heartbeatHealthy is heartbeat_healthy.
func (q *queue) heartbeatHealthy() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return !q.okSince.IsZero() && time.Since(q.okSince).Seconds() >= q.d.Panel.DeadAfter()
}

// ── services/schedule_authority.py ────────────────────────────────────────────

// notStarted is not_started.
func notStarted(user *auth.User, gaps []string) string {
	shown := gaps
	if len(shown) > 5 {
		shown = shown[:5]
	}
	text := strings.Join(shown, "; ")
	if len(gaps) > 5 {
		text += fmt.Sprintf("; and %d more", len(gaps)-5)
	}
	return fmt.Sprintf("Scheduled run not started: the schedule runs with the rights of %s, who no longer has %s. "+
		"Restore those rights, or set the schedule again as someone who has them.", user.Email, text)
}

// gapsOfFlow is gaps_of_flow: what `user` lacks to run the flow as its schedule would.
func (q *queue) gapsOfFlow(ctx context.Context, user *auth.User, flow *jobFlow, seen map[int64]bool) ([]string, error) {
	seen = copyIDs(seen)
	seen[flow.ID] = true
	c := perm.For(q.d.DB, user)
	acc := flowaccess.New(q.d.DB, c, user, q.d.flowSettings())
	var gaps []string
	has := func(project int64, need perm.Capability) (bool, error) { return c.Has(ctx, project, need) }
	if ok, err := has(flow.ProjectID, perm.Run); err != nil {
		return nil, err
	} else if !ok {
		gaps = append(gaps, fmt.Sprintf("RUN on the folder of flow '%s'", flow.Name))
	}
	parsed := pyjson.Object{}
	if v, err := pyjson.Loads(pyjson.Str(pyjson.Or(flow.Definition, "{}"))); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			parsed = o
		}
	}
	readable, err := c.Readable(ctx)
	if err != nil {
		return nil, err
	}
	nodes := objectList(parsed.Value("nodes"))
	refreshed := map[int64]bool{}
	for _, n := range nodes {
		if n.Value("type") == "refresh" {
			if id, ok := flowaccess.Digits(nodeData(n).Value("datasourceId")); ok {
				refreshed[id] = true
			}
		}
	}
	reach := flowaccess.ReachOf(pyDumps(parsed))
	refs := make([]flowaccess.Ref, 0, len(reach))
	for r := range reach {
		refs = append(refs, r)
	}
	sort.Slice(refs, func(i, j int) bool {
		return refs[i].Kind < refs[j].Kind || (refs[i].Kind == refs[j].Kind && refs[i].Value < refs[j].Value)
	})
	keys := map[string]bool{}
	for _, r := range refs {
		var id int64
		fmt.Sscan(r.Value, &id)
		switch r.Kind {
		case "key":
			keys[r.Value] = true
		case "datasource":
			ds, err := loadJobDatasource(ctx, q.d.DB, id)
			if err != nil {
				return nil, err
			}
			if ds == nil {
				continue
			}
			if !readable[ds.ProjectID] {
				gaps = append(gaps, fmt.Sprintf("read access to datasource #%d", id))
			} else if refreshed[id] {
				if ok, err := has(ds.ProjectID, perm.Run); err != nil {
					return nil, err
				} else if !ok {
					gaps = append(gaps, fmt.Sprintf("RUN on the folder of datasource '%s' (to refresh it)", ds.Name))
				}
				if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
					conn, err := loadEngineConn(ctx, q.d.DB, *ds.ConnectionID)
					if err != nil {
						return nil, err
					}
					if conn != nil {
						if ok, err := has(conn.ProjectID, perm.Connect); err != nil {
							return nil, err
						} else if !ok {
							gaps = append(gaps, fmt.Sprintf("CONNECT on connection '%s' (to refresh '%s')", conn.Name, ds.Name))
						}
					}
				}
			}
		case "connection":
			conn, err := loadEngineConn(ctx, q.d.DB, id)
			if err != nil {
				return nil, err
			}
			if conn != nil {
				if ok, err := has(conn.ProjectID, perm.Connect); err != nil {
					return nil, err
				} else if !ok {
					gaps = append(gaps, fmt.Sprintf("CONNECT on connection #%d", id))
				}
			}
		case "environment":
			env, err := loadScriptEnv(ctx, q.d.DB, id)
			if err != nil {
				return nil, err
			}
			if env != nil {
				if ok, err := scriptnodes.CanUse(ctx, c, q.d.DB, user.ID, env.ID); err != nil {
					return nil, err
				} else if !ok {
					gaps = append(gaps, fmt.Sprintf("use of scripting environment '%s'", env.Name))
				}
			}
		case "saved_script":
			s, err := savedscripts.Get(ctx, q.d.DB, id)
			if err != nil {
				return nil, err
			}
			if s != nil {
				more, err := q.d.gapsOfScript(ctx, user, s, true)
				if err != nil {
					return nil, err
				}
				gaps = append(gaps, more...)
			}
		case "registry":
			reg, err := acc.RegistryByID(ctx, id)
			if err != nil {
				return nil, err
			}
			if reg != nil {
				if ok, err := acc.CanUseRegistry(ctx, reg); err != nil {
					return nil, err
				} else if !ok {
					gaps = append(gaps, fmt.Sprintf("use of container registry '%s'", reg.Name))
				}
			}
		case "flow":
			if seen[id] {
				continue
			}
			sub, err := loadJobFlow(ctx, q.d.DB, id)
			if err != nil {
				return nil, err
			}
			if sub != nil {
				more, err := q.gapsOfFlow(ctx, user, sub, seen)
				if err != nil {
					return nil, err
				}
				gaps = append(gaps, more...)
			}
		}
	}
	for _, n := range nodes {
		d := nodeData(n)
		if n.Value("type") == "output" && pyjson.Or(d.Value("destType"), "datasource") == "datasource" {
			if target, ok := flowaccess.Digits(d.Value("projectId")); ok && pyjson.Truthy(d.Value("projectId")) {
				if ok, err := has(target, perm.Edit); err != nil {
					return nil, err
				} else if !ok {
					gaps = append(gaps, fmt.Sprintf("EDIT on folder #%s (flow '%s' publishes there)", pyjson.Str(d.Value("projectId")), flow.Name))
				}
			}
		}
		if regID, isInt := d.Value("registryId").(int64); n.Value("type") == "container" && isInt && pyjson.Truthy(d.Value("repository")) {
			reg, err := acc.RegistryByID(ctx, regID)
			if err != nil {
				return nil, err
			}
			if reg != nil {
				use, err := acc.CanUseRegistry(ctx, reg)
				if err != nil {
					return nil, err
				}
				allowed := false
				if use {
					if allowed, err = acc.RepositoryAllowed(ctx, reg, pyjson.Str(d.Value("repository"))); err != nil {
						return nil, err
					}
				}
				if use && !allowed {
					gaps = append(gaps, fmt.Sprintf("use of the image %s of container registry '%s'", pyjson.Str(d.Value("repository")), reg.Name))
				}
			}
		}
	}
	if ref, err := objects.EnsureCanReadKeys(ctx, c, q.d.DB, user, keys); err != nil {
		return nil, err
	} else if ref != nil {
		gaps = append(gaps, fmt.Sprintf("read access to a file flow '%s' reads", flow.Name))
	}
	// list(dict.fromkeys(gaps))
	var out []string
	dup := map[string]bool{}
	for _, g := range gaps {
		if !dup[g] {
			dup[g] = true
			out = append(out, g)
		}
	}
	return out, nil
}

func copyIDs(m map[int64]bool) map[int64]bool {
	out := map[int64]bool{}
	for k, v := range m {
		out[k] = v
	}
	return out
}
