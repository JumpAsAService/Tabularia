package api

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// services/flow_group_runner.py run_group: a flow group's steps in order, the cells of a step
// at the same time (flows, saved scripts, SQL scripts on a connection, containers, refreshes).
// A Pushmail cell (reports and dashboards rendered, their AI comments) is not carried by the Go
// queue: a group with one is left queued for a Python orchestrator (queue.carries).

const (
	cellWaiting, cellRunning, cellSucceeded, cellFailed, cellSkipped = "waiting", "running", "succeeded", "failed", "skipped"
	slotAttempts                                                     = 3   // SLOT_ATTEMPTS
	cellSlotWait                                                     = 0.5 // CELL_SLOT_WAIT_S
	userGone                                                         = "the user running the group is no longer active"
	groupEngineMode                                                  = "production"
)

// groupCell is a FlowGroupCell, what the runner reads.
type groupCell struct {
	kind      string
	id        int64
	container pyjson.Object
}

// groupSteps is definition_of(group).steps.
func groupSteps(definition string) [][]*groupCell {
	out := [][]*groupCell{}
	steps, _ := flowgroups.Parse(definition).Get("steps").([]any)
	for _, row := range steps {
		cells, _ := row.([]any)
		r := make([]*groupCell, 0, len(cells))
		for _, c := range cells {
			cm, ok := c.(*pyd.ModelValue)
			if !ok {
				r = append(r, nil)
				continue
			}
			cell := &groupCell{kind: cm.Get("kind").(string)}
			cell.id, _ = cm.Get("id").(int64)
			if o, ok := cm.Get("container").(*pyd.Object); ok {
				cell.container = pydconv.FromPyd(o).(pyjson.Object)
			}
			r = append(r, cell)
		}
		out = append(out, r)
	}
	return out
}

// groupCells is _Cells: every cell's state, written whole to the group run under a lock.
type groupCells struct {
	d     *Deps
	runID int64
	mu    sync.Mutex
	grid  [][]pyjson.Object
}

func newGroupCells(d *Deps, runID int64, steps [][]*groupCell) *groupCells {
	g := &groupCells{d: d, runID: runID}
	for _, row := range steps {
		r := make([]pyjson.Object, len(row))
		for i, c := range row {
			if c != nil {
				r[i] = pyjson.Object{{Key: "kind", Value: c.kind}, {Key: "id", Value: c.id}, {Key: "state", Value: cellWaiting},
					{Key: "run_id", Value: nil}, {Key: "error", Value: nil}, {Key: "waited", Value: false}}
			}
		}
		g.grid = append(g.grid, r)
	}
	return g
}

func (g *groupCells) write(ctx context.Context) error {
	grid := make([]any, len(g.grid))
	for i, row := range g.grid {
		r := make([]any, len(row))
		for j, c := range row {
			if c != nil {
				r[j] = c
			}
		}
		grid[i] = r
	}
	_, err := g.d.DB.Exec(context.Background(), `UPDATE runs SET group_cells = $2 WHERE id = $1`, g.runID, pyDumps(grid))
	return err
}

func (g *groupCells) set(ctx context.Context, step, col int, fields ...any) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for i := 0; i+1 < len(fields); i += 2 {
		g.grid[step][col] = g.grid[step][col].Set(fields[i].(string), fields[i+1])
	}
	if err := g.write(ctx); err != nil {
		g.d.Log.Error("flow group: the cells not written", "run", g.runID, "err", err)
	}
}

func (g *groupCells) skipAfter(ctx context.Context, step int) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, row := range g.grid[step+1:] {
		for j, c := range row {
			if c != nil {
				row[j] = c.Set("state", cellSkipped)
			}
		}
	}
	if err := g.write(ctx); err != nil {
		g.d.Log.Error("flow group: the cells not written", "run", g.runID, "err", err)
	}
}

// runGroup is run_group.
func (q *queue) runGroup(ctx context.Context, id int64, run *runRow) error {
	d := q.d
	var name, definition string
	var project int64
	err := d.DB.QueryRow(ctx, `SELECT name, project_id, definition FROM flow_groups WHERE id = $1`, *run.FlowGroupID).Scan(&name, &project, &definition)
	gone := errors.Is(err, pgx.ErrNoRows)
	if err != nil && !gone {
		return err
	}
	user, err := loadUserOpt(ctx, d.DB, run.LaunchedBy)
	if err != nil {
		return err
	}
	if gone || user == nil || !user.IsActive {
		q.finalize(ctx, id, "FAILURE", strp("flow group or user no longer valid"), false)
		return nil
	}
	if ok, err := perm.For(d.DB, user).Has(ctx, project, perm.Run); err != nil {
		return err
	} else if !ok {
		q.finalize(ctx, id, "FAILURE", strp(fmt.Sprintf("RUN missing on the folder of flow group '%s'", name)), false)
		return nil
	}
	steps := groupSteps(definition)
	if run.TriggerType == "schedule" {
		gaps, err := q.gapsOfGroup(ctx, user, name, project, steps)
		if err != nil {
			return err
		}
		if len(gaps) > 0 {
			q.finalize(ctx, id, "FAILURE", strp(notStarted(user, gaps)), false)
			return nil
		}
	}
	cells := newGroupCells(d, id, steps)
	if err := cells.write(ctx); err != nil {
		return err
	}
	stopped, err := q.runSteps(ctx, id, user.ID, run.TriggerType, cells, steps)
	if errors.Is(err, errNoLongerOurs) {
		d.Log.Warn("flow group, stopping", "group", name, "err", err)
		return nil
	}
	if err != nil {
		return err
	}
	if stopped != "" {
		q.finalize(ctx, id, "FAILURE", &stopped, false)
		return nil
	}
	q.finalize(ctx, id, "SUCCESS", nil, false)
	return nil
}

// runSteps is _run_steps: why the group stopped, "" when every step succeeded.
func (q *queue) runSteps(ctx context.Context, id, userID int64, trigger string, cells *groupCells, steps [][]*groupCell) (string, error) {
	for n, row := range steps {
		type todo struct {
			col  int
			cell *groupCell
		}
		var list []todo
		for col, c := range row {
			if c != nil {
				list = append(list, todo{col, c})
			}
		}
		if len(list) == 0 {
			continue
		}
		if err := q.stillMine(ctx, id); err != nil {
			return "", err
		}
		var since *time.Time
		if n > 0 {
			t := jobs.Now()
			since = &t
		}
		// a task group: a cell that raises cancels its siblings
		cctx, cancel := context.WithCancel(ctx)
		results := make([]string, len(list))
		errs := make([]error, len(list))
		var wg sync.WaitGroup
		for i, t := range list {
			wg.Add(1)
			go func(i int, t todo) {
				defer wg.Done()
				results[i], errs[i] = q.runCell(cctx, id, userID, trigger, cells, n, t.col, t.cell, since)
				if errs[i] != nil {
					cancel()
				}
			}(i, t)
		}
		wg.Wait()
		cancel()
		for _, err := range errs {
			if err != nil {
				return "", err
			}
		}
		failed := 0
		for _, r := range results {
			if r != "" {
				failed++
			}
		}
		if failed > 0 {
			cells.skipAfter(ctx, n)
			return fmt.Sprintf("step %d: %d of %d cells failed", n+1, failed, len(list)), nil
		}
	}
	return "", nil
}

// runCell is _run_cell: one cell run and its state recorded; why it failed, or "".
func (q *queue) runCell(ctx context.Context, id, userID int64, trigger string, cells *groupCells, step, col int, cell *groupCell,
	since *time.Time) (string, error) {
	for {
		q.mu.Lock()
		full := int64(q.runningCells) >= q.concurrency()
		if !full {
			q.runningCells++
		}
		q.mu.Unlock()
		if !full {
			break
		}
		if !jobs.Sleep(ctx, jobs.Seconds(cellSlotWait)) {
			return "", ctx.Err()
		}
	}
	defer func() {
		q.mu.Lock()
		q.runningCells--
		q.mu.Unlock()
	}()
	cells.set(ctx, step, col, "state", cellRunning)
	var why string
	var err error
	switch cell.kind {
	case "flow":
		why, err = q.flowCell(ctx, id, userID, trigger, cell.id, cells, step, col, since)
	case "script":
		why, err = q.scriptCell(ctx, id, userID, trigger, cell.id, cells, step, col, since)
	case "container":
		why, err = q.containerCell(ctx, id, userID, trigger, cell, cells, step, col)
	case "sql":
		why, err = q.sqlCell(ctx, id, userID, trigger, cell.id, cells, step, col, since)
	case "refresh":
		why, err = q.refreshCell(ctx, id, userID, trigger, cell.id, cells, step, col)
	default:
		err = fmt.Errorf("a %s cell is not carried by the Go orchestrator", cell.kind)
	}
	if errors.Is(err, errNoLongerOurs) || ctx.Err() != nil {
		return "", err
	}
	if err != nil {
		q.d.Log.Error("flow group run: cell failed", "run", id, "step", step+1, "col", col+1, "err", err)
		why = fmt.Sprintf("internal error (%s)", pyErrorName(err))
	}
	state := cellSucceeded
	var errText any
	if why != "" {
		state, errText = cellFailed, why
	}
	cells.set(ctx, step, col, "state", state, "error", errText)
	return why, nil
}

// liveCounts is _counts.
func liveCounts(mode *string, started time.Time, since *time.Time) bool {
	if mode == nil || *mode != groupEngineMode {
		return false
	}
	return since == nil || !started.Before(*since)
}

// takeChildSlot inserts a group cell's child orchestration (its flow's or script's live slot):
// false when the database refuses it (the unique index of the live runs).
func (q *queue) takeChildSlot(ctx context.Context, owner string, ownerID, userID int64, trigger string, parent int64) (int64, bool, error) {
	var id int64
	err := q.d.DB.QueryRow(ctx, fmt.Sprintf(`INSERT INTO runs (kind, %s, task_id, status, launched_by, trigger_type, engine_mode, parent_run_id,
		claimed_by, heartbeat_at, input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
		VALUES ('orchestration', $1, '', 'STARTED', $2, $3, 'production', $4, $5, $6, '', '', '', '', false, '[]', $6) RETURNING id`, owner),
		ownerID, userID, trigger, parent, jobs.Instance, jobs.Now()).Scan(&id)
	var pe *pgconn.PgError
	if errors.As(err, &pe) && strings.HasPrefix(pe.Code, "23") {
		return 0, false, nil
	}
	return id, err == nil, err
}

// flowCell is _flow_cell.
func (q *queue) flowCell(ctx context.Context, id, userID int64, trigger string, flowID int64, cells *groupCells, step, col int,
	since *time.Time) (string, error) {
	d := q.d
	flow, err := loadJobFlow(ctx, d.DB, flowID)
	if err != nil {
		return "", err
	}
	user, err := loadUser(ctx, d.DB, userID)
	if err != nil {
		return "", err
	}
	if flow == nil {
		return fmt.Sprintf("flow %d no longer exists", flowID), nil
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	name := flow.Name
	if ok, err := perm.For(d.DB, user).Has(ctx, flow.ProjectID, perm.Run); err != nil {
		return "", err
	} else if !ok {
		return fmt.Sprintf("'%s': RUN missing on its folder", name), nil
	}
	var child int64
	for attempt := 0; ; attempt++ {
		if attempt == slotAttempts {
			return fmt.Sprintf("'%s': its slot could not be taken", name), nil
		}
		cid, ok, err := q.takeChildSlot(ctx, "flow_id", flow.ID, user.ID, trigger, id)
		if err != nil {
			return "", err
		}
		if ok {
			child = cid
			break
		}
		var liveID int64
		var mode *string
		var started time.Time
		err = d.DB.QueryRow(ctx, `SELECT id, engine_mode, started_at FROM runs WHERE kind = 'orchestration' AND flow_id = $1
			AND status = 'STARTED' LIMIT 1`, flowID).Scan(&liveID, &mode, &started)
		if errors.Is(err, pgx.ErrNoRows) {
			continue
		}
		if err != nil {
			return "", err
		}
		if liveCounts(mode, started, since) {
			return q.waitFor(ctx, id, liveID, name, cells, step, col, true, false)
		}
		if _, err := q.waitFor(ctx, id, liveID, name, cells, step, col, false, false); err != nil {
			return "", err
		}
	}
	cells.set(ctx, step, col, "run_id", child, "waited", false)
	q.mu.Lock()
	q.alive[child] = true
	q.mu.Unlock()
	defer func() {
		q.mu.Lock()
		delete(q.alive, child)
		q.mu.Unlock()
	}()
	errs, err := q.orchestrate(ctx, user, flow, 0, map[int64]bool{}, trigger, child, groupEngineMode, true)
	var oe *orchestrationError
	var fe *flowResolveError
	switch {
	case errors.Is(err, errNoLongerOurs):
		return fmt.Sprintf("'%s': interrupted", name), nil
	case errors.As(err, &oe) || errors.As(err, &fe):
		errs = []string{err.Error()}
	case ctx.Err() != nil:
		bg := context.Background()
		q.finalize(bg, child, "FAILURE", strp(orchInterrupted), false)
		q.stopSteps(bg, child)
		return "", ctx.Err()
	case err != nil:
		d.Log.Error("flow group run: flow failed", "run", id, "flow", flowID, "err", err)
		errs = []string{describeError(err)}
	}
	if len(errs) > 0 {
		text := strings.Join(errs, "; ")
		q.finalize(ctx, child, "FAILURE", &text, false)
		return fmt.Sprintf("'%s': %s", name, text), nil
	}
	q.finalize(ctx, child, "SUCCESS", nil, false)
	return "", nil
}

// describeError is _describe.
func describeError(err error) string {
	if d := errorDetail(err); d != "" && d != err.Error() {
		return d
	}
	var h *httpError
	if errors.As(err, &h) {
		return h.Detail
	}
	return pyErrorName(err) + ": " + err.Error()
}

// scriptCell is _script_cell.
func (q *queue) scriptCell(ctx context.Context, id, userID int64, trigger string, scriptID int64, cells *groupCells, step, col int,
	since *time.Time) (string, error) {
	d := q.d
	s, err := savedscripts.Get(ctx, d.DB, scriptID)
	if err != nil {
		return "", err
	}
	user, err := loadUser(ctx, d.DB, userID)
	if err != nil {
		return "", err
	}
	if s == nil {
		return fmt.Sprintf("saved script %d no longer exists", scriptID), nil
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	name := s.Name
	var child int64
	for attempt := 0; ; attempt++ {
		if attempt == slotAttempts {
			return fmt.Sprintf("'%s': its slot could not be taken", name), nil
		}
		cid, ok, err := q.takeChildSlot(ctx, "saved_script_id", s.ID, user.ID, trigger, id)
		if err != nil {
			return "", err
		}
		if ok {
			child = cid
			break
		}
		var liveID int64
		var started time.Time
		err = d.DB.QueryRow(ctx, `SELECT id, started_at FROM runs WHERE kind = 'orchestration' AND saved_script_id = $1
			AND status = 'STARTED' LIMIT 1`, scriptID).Scan(&liveID, &started)
		if errors.Is(err, pgx.ErrNoRows) {
			continue
		}
		if err != nil {
			return "", err
		}
		if since == nil || !started.Before(*since) {
			return q.waitFor(ctx, id, liveID, name, cells, step, col, true, false)
		}
		if _, err := q.waitFor(ctx, id, liveID, name, cells, step, col, false, false); err != nil {
			return "", err
		}
	}
	cells.set(ctx, step, col, "run_id", child, "waited", false)
	q.mu.Lock()
	q.alive[child] = true
	q.mu.Unlock()
	defer func() {
		q.mu.Lock()
		delete(q.alive, child)
		q.mu.Unlock()
	}()
	why := ""
	err = q.executeSaved(ctx, user, s, child, trigger)
	var sf *scriptFailed
	switch {
	case errors.As(err, &sf):
		why = sf.msg
	case ctx.Err() != nil:
		bg := context.Background()
		q.finalize(bg, child, "FAILURE", strp(orchInterrupted), false)
		q.stopSteps(bg, child)
		return "", ctx.Err()
	case err != nil:
		d.Log.Error("flow group run: saved script failed", "run", id, "script", scriptID, "err", err)
		why = describeError(err)
	}
	if why != "" {
		q.finalize(ctx, child, "FAILURE", &why, false)
		return fmt.Sprintf("'%s': %s", name, why), nil
	}
	q.finalize(ctx, child, "SUCCESS", nil, false)
	return "", nil
}

// sqlCell is _sql_cell.
func (q *queue) sqlCell(ctx context.Context, id, userID int64, trigger string, scriptID int64, cells *groupCells, step, col int,
	since *time.Time) (string, error) {
	d := q.d
	script, err := sqlscripts.Get(ctx, d.DB, scriptID)
	if err != nil {
		return "", err
	}
	user, err := loadUser(ctx, d.DB, userID)
	if err != nil {
		return "", err
	}
	if script == nil {
		return fmt.Sprintf("SQL script %d no longer exists", scriptID), nil
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	name := script.Name
	var runID int64
	parent := id
	for attempt := 0; ; attempt++ {
		if attempt == slotAttempts {
			return fmt.Sprintf("'%s': its write could not be started", name), nil
		}
		var liveID int64
		var started time.Time
		err := d.DB.QueryRow(ctx, `SELECT id, started_at FROM runs WHERE sql_script_id = $1 AND kind = 'sql'
			AND status NOT IN ('SUCCESS', 'FAILURE') ORDER BY id DESC LIMIT 1`, scriptID).Scan(&liveID, &started)
		if err == nil {
			if since == nil || !started.Before(*since) {
				return q.waitFor(ctx, id, liveID, name, cells, step, col, true, true)
			}
			if _, err := q.waitFor(ctx, id, liveID, name, cells, step, col, false, true); err != nil {
				return "", err
			}
			continue
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return "", err
		}
		rid, refusal, err := d.launchSQLScript(ctx, user, script, trigger, &parent)
		if refusal != nil {
			if refusal.Status == 409 && refusal.Detail == sqlAlreadyRunning {
				continue
			}
			return fmt.Sprintf("'%s': %s", name, refusal.Detail), nil
		}
		if err != nil {
			return "", err
		}
		runID = rid
		break
	}
	cells.set(ctx, step, col, "run_id", runID, "waited", false)
	run, err := q.waitRun(ctx, runID)
	if err != nil {
		return "", err
	}
	if run.Status != "SUCCESS" {
		why := run.Status
		if run.Error != nil && *run.Error != "" {
			why = *run.Error
		}
		return fmt.Sprintf("'%s': %s", name, why), nil
	}
	return "", nil
}

// containerCell is _container_cell.
func (q *queue) containerCell(ctx context.Context, id, userID int64, trigger string, cell *groupCell, cells *groupCells, step, col int) (string, error) {
	user, err := loadUser(ctx, q.d.DB, userID)
	if err != nil {
		return "", err
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	nodeID := fmt.Sprintf("container-cell-%d", cell.id)
	data := pyjson.Object{}
	if cell.container != nil {
		data = append(data, cell.container...)
	}
	node := pyjson.Object{{Key: "id", Value: nodeID}, {Key: "type", Value: "container"}, {Key: "data", Value: data}}
	r := &scriptNodeRun{q: q, user: user, definition: pyjson.Object{{Key: "nodes", Value: []any{node}}, {Key: "edges", Value: []any{}}},
		node: node, parent: id, trigger: trigger, mode: groupEngineMode, outputs: map[string]*[2]any{}}
	why := ""
	_, err = q.runContainer(ctx, r)
	var sf *scriptFailed
	switch {
	case errors.As(err, &sf):
		why = sf.msg
	case ctx.Err() != nil:
		return "", ctx.Err()
	case err != nil:
		q.d.Log.Error("flow group run: container cell failed", "run", id, "cell", cell.id, "err", err)
		why = describeError(err)
	}
	// this cell's own run (its node id is the cell's)
	var mine any
	rows, err := q.d.DB.Query(ctx, `SELECT id, script FROM runs WHERE parent_run_id = $1 AND kind = 'container' ORDER BY id DESC`, id)
	if err != nil {
		return "", err
	}
	for rows.Next() {
		var rid int64
		var script *string
		if rows.Scan(&rid, &script) != nil {
			continue
		}
		if v, ok := scriptJSON(script); ok && pyjson.Obj(v).Value("node_id") == nodeID && mine == nil {
			mine = rid
		}
	}
	rows.Close()
	cells.set(ctx, step, col, "run_id", mine)
	return why, nil
}

// refreshCell is _refresh_cell.
func (q *queue) refreshCell(ctx context.Context, id, userID int64, trigger string, dsID int64, cells *groupCells, step, col int) (string, error) {
	ds, err := loadJobDatasource(ctx, q.d.DB, dsID)
	if err != nil {
		return "", err
	}
	user, err := loadUser(ctx, q.d.DB, userID)
	if err != nil {
		return "", err
	}
	if ds == nil {
		return fmt.Sprintf("datasource %d no longer exists", dsID), nil
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	var kind string
	if err := q.d.DB.QueryRow(ctx, `SELECT kind FROM datasources WHERE id = $1`, dsID).Scan(&kind); err != nil {
		return "", err
	}
	if kind != "database" {
		return fmt.Sprintf("'%s' cannot be refreshed", ds.Name), nil
	}
	parent := id
	err = q.refreshAndWait(ctx, user, dsID, trigger, &parent)
	var oe *orchestrationError
	var h *httpError
	switch {
	case errors.As(err, &oe):
		return fmt.Sprintf("'%s': %s", ds.Name, oe.msg), nil
	case errors.As(err, &h):
		return fmt.Sprintf("'%s': %s", ds.Name, h.Detail), nil
	case err != nil:
		return "", err
	}
	var last any
	var lastID int64
	if err := q.d.DB.QueryRow(ctx, `SELECT id FROM runs WHERE datasource_id = $1 AND kind = 'ingest' ORDER BY started_at DESC LIMIT 1`, dsID).
		Scan(&lastID); err == nil {
		last = lastID
	}
	cells.set(ctx, step, col, "run_id", last)
	return "", nil
}

// waitFor is _wait_for: a run already in progress awaited; with takeOutcome, its outcome is
// the cell's.
func (q *queue) waitFor(ctx context.Context, id, liveID int64, name string, cells *groupCells, step, col int, takeOutcome, engineTask bool) (string, error) {
	cells.set(ctx, step, col, "run_id", liveID, "waited", takeOutcome)
	req := q.d.jobRequest(ctx, nil)
	next := waitsOf(q.d.Panel.PollInterval())
	for {
		if engineTask {
			live, err := req.readRun(liveID)
			if err != nil {
				return "", err
			}
			if live != nil && !terminal[live.Status] {
				if _, err := req.reconcileAny(live, q.sc); err != nil {
					return "", err
				}
			}
		}
		var status string
		var errText *string
		err := q.d.DB.QueryRow(ctx, `SELECT status, error FROM runs WHERE id = $1`, liveID).Scan(&status, &errText)
		if errors.Is(err, pgx.ErrNoRows) {
			if takeOutcome {
				return fmt.Sprintf("'%s': the run in progress it waited for is gone", name), nil
			}
			return "", nil
		}
		if err != nil {
			return "", err
		}
		if terminal[status] {
			if !takeOutcome || status == "SUCCESS" {
				return "", nil
			}
			why := fmt.Sprintf("'%s': the run already in progress failed", name)
			if errText != nil && *errText != "" {
				why += " (" + *errText + ")"
			}
			return why, nil
		}
		if err := q.stillMine(ctx, id); err != nil {
			return "", err
		}
		if !jobs.Sleep(ctx, jobs.Seconds(next())) {
			return "", ctx.Err()
		}
	}
}

// gapsOfGroup is schedule_authority.gaps_of_group (its Pushmail cells left to Python).
func (q *queue) gapsOfGroup(ctx context.Context, user *auth.User, name string, project int64, steps [][]*groupCell) ([]string, error) {
	d := q.d
	c := perm.For(d.DB, user)
	var gaps []string
	if ok, err := c.Has(ctx, project, perm.Run); err != nil {
		return nil, err
	} else if !ok {
		gaps = append(gaps, fmt.Sprintf("RUN on the folder of flow group '%s'", name))
	}
	acc := flowaccess.New(d.DB, c, user, d.flowSettings())
	for _, row := range steps {
		for _, cell := range row {
			if cell == nil {
				continue
			}
			var more []string
			var err error
			switch cell.kind {
			case "flow":
				var f *jobFlow
				if f, err = loadJobFlow(ctx, d.DB, cell.id); err == nil && f != nil {
					more, err = q.gapsOfFlow(ctx, user, f, map[int64]bool{})
				}
			case "script":
				var s *savedscripts.Script
				if s, err = savedscripts.Get(ctx, d.DB, cell.id); err == nil && s != nil {
					more, err = d.gapsOfScript(ctx, user, s, false)
				}
			case "container":
				regID, isInt := cell.container.Value("registryId").(int64)
				if !isInt {
					break
				}
				reg, err2 := acc.RegistryByID(ctx, regID)
				if err2 != nil || reg == nil {
					err = err2
					break
				}
				use, err2 := acc.CanUseRegistry(ctx, reg)
				if err2 != nil {
					err = err2
					break
				}
				allowed := false
				if use {
					if allowed, err = acc.RepositoryAllowed(ctx, reg, textOrEmpty(cell.container.Value("repository"))); err != nil {
						break
					}
				}
				if !(use && allowed) {
					more = []string{fmt.Sprintf("use of the image %s of container registry '%s'", pyjson.Str(cell.container.Value("repository")), reg.Name)}
				}
			case "refresh":
				ds, err2 := loadJobDatasource(ctx, d.DB, cell.id)
				if err2 != nil || ds == nil {
					err = err2
					break
				}
				if ok, err2 := c.Has(ctx, ds.ProjectID, perm.Run); err2 != nil {
					err = err2
					break
				} else if !ok {
					more = append(more, fmt.Sprintf("RUN on the folder of datasource '%s' (to refresh it)", ds.Name))
				}
				if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
					conn, err2 := loadEngineConn(ctx, d.DB, *ds.ConnectionID)
					if err2 != nil {
						err = err2
						break
					}
					if conn != nil {
						if ok, err2 := c.Has(ctx, conn.ProjectID, perm.Connect); err2 != nil {
							err = err2
						} else if !ok {
							more = append(more, fmt.Sprintf("CONNECT on connection '%s' (to refresh '%s')", conn.Name, ds.Name))
						}
					}
				}
			case "sql":
				var s *sqlscripts.Script
				if s, err = sqlscripts.Get(ctx, d.DB, cell.id); err == nil && s != nil {
					more, err = d.sqlGaps(ctx, user, s)
				}
			default:
				err = fmt.Errorf("a %s cell is not carried by the Go orchestrator", cell.kind)
			}
			if err != nil {
				return nil, err
			}
			gaps = append(gaps, more...)
		}
	}
	var out []string
	seen := map[string]bool{}
	for _, g := range gaps {
		if !seen[g] {
			seen[g] = true
			out = append(out, g)
		}
	}
	return out, nil
}

// sqlGaps is sql_script_runs.gaps for a script on a connection: what `user` lacks to write it.
func (d *Deps) sqlGaps(ctx context.Context, user *auth.User, script *sqlscripts.Script) ([]string, error) {
	c := perm.For(d.DB, user)
	if see, err := sqlscripts.CanSee(ctx, c, script); err != nil {
		return nil, err
	} else if !see {
		return []string{fmt.Sprintf("%s can no longer see the SQL script «%s»", user.Email, script.Name)}, nil
	}
	if ok, err := c.Has(ctx, script.ProjectID, perm.Run); err != nil {
		return nil, err
	} else if !ok {
		return []string{fmt.Sprintf("RUN missing on the folder of the SQL script «%s»", script.Name)}, nil
	}
	content, err := sqlscripts.ContentOf(ctx, d.DB, script, nil)
	if err != nil {
		return nil, err
	}
	output := sqlscripts.OutputOf(content)
	if output == nil {
		return []string{fmt.Sprintf("The SQL script «%s» does not save its result as a datasource: give it a name and a folder first", script.Name)}, nil
	}
	sql := ""
	if pyjson.Truthy(content.Value("sql")) {
		sql = pyjson.Str(content.Value("sql"))
	}
	if stripSQLEnd(reports.StripComments(sql)) == "" {
		return []string{fmt.Sprintf("The SQL script «%s» has no SQL yet", script.Name)}, nil
	}
	if ref, err := sqlscripts.CheckOutput(ctx, c, d.DB, user, output); err != nil || ref != nil {
		if err != nil {
			return nil, err
		}
		return []string{ref.Detail}, nil
	}
	connID, ok := pyjson.IsInt(content.Value("connection_id"))
	if !ok {
		return nil, errors.New("a SQL script on the catalog needs sqlglot (services/sql_scripts.resolve)")
	}
	if _, ref, err := sqlscripts.UsableConnection(ctx, c, d.DB, connID); err != nil || ref != nil {
		if err != nil {
			return nil, err
		}
		return []string{ref.Detail}, nil
	}
	return nil, nil
}
