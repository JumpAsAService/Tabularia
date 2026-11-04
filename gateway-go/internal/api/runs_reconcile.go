package api

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/runs.py: reading one run, or a flow's, reconciled with the engine on the way
// (_reconcile). Two branches stay with the Python gateway, which gets the whole request:
// a Script or Container run (its Kubernetes Jobs, script_runs.close_if_orphaned), a
// scheduled orchestration closed for staying queued too long (notifier.notify_failure's
// email), a run whose datasource has a data contract on (services/contracts.py), and any
// run not finished while OpenLineage is on (openlineage.run_closed).

func init() {
	register("runs_read", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/runs/{run_id}", Handler: server.Authed(d.Auth, "/runs/{run_id}", d.getRun)},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/runs", Handler: server.Authed(d.Auth, "/flows/{flow_id}/runs", d.listRuns)},
		}
	})
}

var terminal = map[string]bool{"SUCCESS": true, "FAILURE": true}

const (
	placeholderTask          = "pending-"
	neverQueuedAfterSeconds  = 300
	sideEffectAttempts       = 3
	queueTimedOut            = "The run stayed in the queue too long without an orchestrator picking it up: it did not run. Launch the flow again."
	neverQueued              = "The run was never queued on the engine: the gateway stopped while launching it. Launch it again."
	revokedByAdministrator   = "Job stopped (revoked by an administrator)"
	oomMessage               = "The run was stopped: its worker process was killed, most often because it ran out of available memory. Reduce the data (filter/limit) or simplify the flow; if it needs more memory, raise the workers' limit (worker.resources.limits.memory in the Helm chart, WORKER_MEM_LIMIT with Compose). A worker stopped meanwhile (an upgrade, a drained node) ends a run the same way: run it again."
	webhookPayloadMaxErrorLn = 1000
)

var (
	oomSigns          = []string{"signal 9", "sigkill", "workerlosterror", "memoryerror", "out of memory"}
	engineTold        = []string{"The worker running it stopped before the end"}
	dbMessagePrefixes = []string{"PostgreSQL:", "ClickHouse:", "MySQL:", "MariaDB:", "Trino:", "Oracle:", "SQL Server:", "S3:",
		"Database:", "SFTP:", "API:", "Google Sheets:"}
)

func (q *request) readRun(id int64) (*runRow, error) {
	var x runRow
	err := q.d.DB.QueryRow(q.ctx, `SELECT `+runColumns+` FROM runs r WHERE r.id = $1`, id).Scan(x.dest()...)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &x, err
}

func ageSeconds(t time.Time) float64 { return time.Since(t).Seconds() }

func runes(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// friendlyError is _friendly_error: an OOM kill said in words.
func friendlyError(e, detail *string) *string {
	if e != nil {
		for _, p := range append(append([]string{}, dbMessagePrefixes...), engineTold...) {
			if strings.HasPrefix(*e, p) {
				return e
			}
		}
	}
	blob := strings.ToLower(deref(e) + "\n" + deref(detail))
	for _, s := range oomSigns {
		if strings.Contains(blob, s) {
			return strp(oomMessage)
		}
	}
	return e
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// pyDumps is json.dumps(v): Python's defaults.
func pyDumps(v any) string {
	s, _ := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
	return s
}

// sftpWritten is _sftp_written.
func sftpWritten(summary *string, outcome any) *string {
	o, ok := outcome.(pyjson.Object)
	if summary == nil || *summary == "" || !ok {
		return nil
	}
	v, err := pyjson.Loads(*summary)
	d, isObj := v.(pyjson.Object)
	if err != nil || !isObj {
		return nil
	}
	switch d.Value("type") {
	case "sftp":
		for _, k := range []string{"path", "bytes"} {
			if x, has := o.Get(k); has {
				d = d.Set(k, x)
			}
		}
	case "s3":
		k := o.Value("key")
		if !pyjson.Truthy(k) || pyjson.Repr(k) == pyjson.Repr(d.Value("key")) {
			return nil
		}
		d = d.Set("written_key", k)
	default:
		return nil
	}
	s := runes(pyDumps(d), 2000)
	return &s
}

// notesOf is _notes_of.
func notesOf(result pyjson.Object) *string {
	missing, _ := result.Value("missing").([]any)
	repeated, _ := result.Value("duplicate_keys").(int64)
	if len(missing) == 0 && repeated == 0 {
		return nil
	}
	shown := missing
	if len(shown) > 50 {
		shown = shown[:50]
	}
	notes := pyjson.Object{{Key: "missing", Value: append([]any{}, shown...)},
		{Key: "missing_count", Value: pyjson.Or(result.Value("missing_count"), int64(len(missing)))},
		{Key: "expansions", Value: result.Value("expansions")}}
	if repeated != 0 {
		notes = notes.Set("duplicate_keys", repeated)
	}
	s := runes(pyDumps(notes), 20000)
	return &s
}

// emailSent is _email_sent: the worker's outcome over the summary written at launch.
func emailSent(summary *string, outcome any) string {
	text := deref(summary)
	if text == "" {
		text = "{}"
	}
	launched, err := pyjson.Loads(text)
	if err != nil {
		launched = pyjson.Object{}
	}
	l, okL := launched.(pyjson.Object)
	o, okO := outcome.(pyjson.Object)
	if okL && okO {
		merged := append(pyjson.Object(nil), l...)
		for _, kv := range o {
			merged = merged.Set(kv.Key, kv.Value)
		}
		return pyDumps(merged)
	}
	return runes(pyDumps(outcome), 2000)
}

// emailNotSent is _email_not_sent.
func emailNotSent(summary *string) *string {
	if summary == nil || *summary == "" {
		return nil
	}
	v, err := pyjson.Loads(*summary)
	d, ok := v.(pyjson.Object)
	if err != nil || !ok || d.Value("ok") != nil {
		return nil
	}
	d = d.Set("ok", false).Set("error", "not sent: the run did not succeed")
	s := pyDumps(d)
	return &s
}

// scheduleBlobDeletion is blobgc.schedule_blob_deletion (in the caller's transaction).
func (q *request) scheduleBlobDeletion(tx pgx.Tx, bucket, key, reason string) error {
	if key == "" {
		return nil
	}
	now := time.Now().UTC()
	grace := time.Duration(q.d.Panel.RunStaleTimeout()+600) * time.Second
	_, err := tx.Exec(q.ctx, `INSERT INTO pending_blob_deletions (bucket, key, reason, delete_after, created_at) VALUES ($1, $2, $3, $4, $5)`,
		bucket, key, reason, now.Add(grace), now)
	return err
}

// contractTargetID is _contract_target_id.
func (q *request) contractTargetID(run *runRow) (*int64, error) {
	if run.DatasourceID != nil {
		return run.DatasourceID, nil
	}
	if run.PublishName != nil && *run.PublishName != "" && run.PublishProjectID != nil && *run.PublishProjectID != 0 {
		var id int64
		err := q.d.DB.QueryRow(q.ctx, `SELECT id FROM datasources WHERE project_id = $1 AND name = $2 ORDER BY id LIMIT 1`,
			*run.PublishProjectID, *run.PublishName).Scan(&id)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return &id, err
	}
	return nil, nil
}

// activeContract is contracts.active_document != None.
func (q *request) activeContract(dsID *int64) (bool, error) {
	if dsID == nil {
		return false, nil
	}
	var enabled bool
	var doc string
	err := q.d.DB.QueryRow(q.ctx, `SELECT enabled, document FROM data_contracts WHERE datasource_id = $1 ORDER BY id LIMIT 1`, *dsID).
		Scan(&enabled, &doc)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && !enabled) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	v, lerr := pyjson.Loads(doc)
	if lerr != nil {
		return false, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return true, nil // doc.get on something else: Python raises, and the Python gateway answers it
	}
	return pyjson.Truthy(o.Value("rules")), nil
}

// needsPython says whether reconciling this run takes a branch the Go gateway leaves to
// the Python one (see the file's comment).
func (q *request) needsPython(run *runRow) (bool, error) {
	if terminal[run.Status] {
		return false, nil
	}
	if q.d.Config.OpenLineage { // a run that closes tells the catalog (openlineage.run_closed)
		return true, nil
	}
	switch run.Kind {
	case "script", "container":
		return true, nil
	case "orchestration":
		return run.Status == "PENDING" && run.TriggerType == "schedule" &&
			ageSeconds(run.StartedAt) > q.d.Panel.QueueTimeout(), nil
	}
	if strings.HasPrefix(run.TaskID, placeholderTask) {
		return false, nil
	}
	target, err := q.contractTargetID(run)
	if err != nil {
		return false, err
	}
	return q.activeContract(target)
}

// progressOf keeps the engine's progress of a running run, for run_out (session.info).
type progressOf map[int64]pyjson.Object

// reconcile is _reconcile: a run not finished aligned with its task on the engine.
func (q *request) reconcile(run *runRow, progress progressOf) (*runRow, error) {
	if terminal[run.Status] {
		return run, nil
	}
	if run.Kind == "orchestration" {
		if run.Status == "PENDING" {
			return q.closeIfQueuedTooLong(run)
		}
		return run, nil
	}
	if strings.HasPrefix(run.TaskID, placeholderTask) {
		return q.closeIfNeverQueued(run)
	}
	ctx, cancel := context.WithTimeout(q.ctx, engine.Check)
	res, raw, err := q.d.Engine.JSON(ctx, http.MethodGet, "/tasks/"+run.TaskID, nil)
	cancel()
	if err != nil {
		q.d.Log.Warn("reconciling a run put off", "run", run.ID, "err", err)
		return run, nil
	}
	_ = res
	parsed, err := pyjson.Loads(string(raw))
	if err != nil {
		q.d.Log.Warn("reconciling a run put off", "run", run.ID, "err", err)
		return run, nil
	}
	data, ok := parsed.(pyjson.Object)
	if !ok {
		return nil, errors.New("the engine's task is not an object")
	}
	newStatus := run.Status
	if v, has := data.Get("status"); has {
		s, isText := v.(string)
		if !isText {
			return nil, errors.New("the engine's status is not a text")
		}
		newStatus = s
	}
	if p, ok := data.Value("progress").(pyjson.Object); ok {
		progress[run.ID] = p
	}
	result := data.Value("result")
	if !pyjson.Truthy(result) {
		result = pyjson.Object{}
	}
	textOf := func(k string) (*string, error) {
		switch v := data.Value(k).(type) {
		case nil:
			return nil, nil
		case string:
			return &v, nil
		}
		return nil, fmt.Errorf("the engine's %s is not a text", k)
	}
	errText, err := textOf("error")
	if err != nil {
		return nil, err
	}
	errDetail, err := textOf("error_detail")
	if err != nil {
		return nil, err
	}
	if newStatus == "REVOKED" {
		newStatus = "FAILURE"
		if errText == nil || *errText == "" {
			errText = strp(revokedByAdministrator)
		}
		errDetail = nil
	}
	started := run.StartedAt
	if run.EngineStartedAt != nil {
		started = *run.EngineStartedAt
	}
	stale := q.d.Panel.RunStaleTimeout()
	if !terminal[newStatus] && ageSeconds(started) > float64(stale) {
		ctx, cancel := context.WithTimeout(q.ctx, engine.Check)
		if _, _, err := q.d.Engine.JSON(ctx, http.MethodDelete, "/tasks/"+run.TaskID, nil); err != nil {
			q.d.Log.Warn("revoking a stale run's task failed", "run", run.ID, "err", err)
		}
		cancel()
		newStatus = "FAILURE"
		errText = strp(fmt.Sprintf("Timeout: the run exceeded the maximum time (%d min) without completing and was stopped. "+
			"If the flow is just slow, raise the limit (ENGINE__RUN_STALE_TIMEOUT_SECONDS) or reduce the data.", pyFloorDiv(stale, 60)))
		errDetail = nil
	}
	if newStatus == run.Status {
		return run, nil
	}
	if !terminal[newStatus] {
		// conditional: another process may have closed the run meanwhile
		sql := `UPDATE runs SET status = $2 WHERE id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`
		if newStatus == "STARTED" && run.EngineStartedAt == nil {
			sql = `UPDATE runs SET status = $2, engine_started_at = $3 WHERE id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`
			_, err = q.d.DB.Exec(q.ctx, sql, run.ID, newStatus, time.Now().UTC())
		} else {
			_, err = q.d.DB.Exec(q.ctx, sql, run.ID, newStatus)
		}
		if err != nil {
			return nil, err
		}
		return q.readRun(run.ID)
	}
	// the terminal claim and its effect, atomic
	type col struct {
		name  string
		value any
	}
	values := []col{{"status", newStatus}, {"finished_at", time.Now().UTC()}}
	res2, isObj := result.(pyjson.Object)
	if newStatus == "SUCCESS" {
		if !isObj {
			return nil, errors.New("the engine's result is not an object")
		}
		rows, err := intColumn(res2.Value("rows_written"))
		if err != nil {
			return nil, err
		}
		values = append(values, col{"rows_written", rows})
		if m, has := res2.Get("mirror"); has && m != nil {
			values = append(values, col{"mirror", runes(pyDumps(m), 2000)})
		}
		if e, has := res2.Get("email"); has && e != nil {
			values = append(values, col{"email", emailSent(run.Email, e)})
		}
		if wr := sftpWritten(run.Destination, res2.Value("destination")); wr != nil {
			values = append(values, col{"destination", *wr})
		}
		if n := notesOf(res2); n != nil {
			values = append(values, col{"notes", *n})
		}
	} else {
		f := friendlyError(errText, errDetail)
		values = append(values, col{"error", runes(deref(f), 2000)})
		var detail any
		if errDetail != nil && *errDetail != "" {
			detail = runes(*errDetail, 20000)
		}
		values = append(values, col{"error_detail", detail})
		if ns := emailNotSent(run.Email); ns != nil {
			values = append(values, col{"email", *ns})
		}
	}
	set := make([]string, len(values))
	args := []any{run.ID}
	for i, c := range values {
		args = append(args, c.value)
		set[i] = fmt.Sprintf("%s = $%d", c.name, i+2)
	}
	claim := `UPDATE runs SET ` + strings.Join(set, ", ") + ` WHERE id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`
	for attempt := 1; attempt <= sideEffectAttempts; attempt++ {
		claimed := true
		err := pgx.BeginFunc(q.ctx, q.d.DB, func(tx pgx.Tx) error {
			tag, err := tx.Exec(q.ctx, claim, args...)
			if err != nil {
				return err
			}
			if tag.RowsAffected() == 0 { // another request closed this run already
				claimed = false
				return errRollback
			}
			after, err := scanRunTx(q.ctx, tx, run.ID)
			if err != nil {
				return err
			}
			if newStatus == "SUCCESS" && after.Kind == "ingest" {
				if err := q.finalizeIngest(tx, after, res2); err != nil {
					return err
				}
			} else if newStatus == "SUCCESS" && after.PublishName != nil && *after.PublishName != "" && after.PublishProjectID != nil &&
				*after.PublishProjectID != 0 && after.DatasourceID == nil {
				if err := q.publishDatasource(tx, after, res2); err != nil {
					return err
				}
			}
			if after, err = scanRunTx(q.ctx, tx, run.ID); err != nil {
				return err
			}
			return q.enqueueWebhooks(tx, after)
		})
		if !claimed {
			return q.readRun(run.ID)
		}
		if err == nil {
			return q.readRun(run.ID)
		}
		q.d.Log.Warn("run: post-claim effect failed", "run", run.ID, "attempt", attempt, "err", err)
	}
	return q.readRun(run.ID)
}

var errRollback = errors.New("rollback")

func pyFloorDiv(a, b int64) int64 {
	return int64(math.Floor(float64(a) / float64(b)))
}

// intColumn is a JSON value written into an integer column as psycopg and PostgreSQL do.
func intColumn(v any) (any, error) {
	switch x := v.(type) {
	case nil:
		return nil, nil
	case int64:
		return x, nil
	case float64:
		return int64(math.RoundToEven(x)), nil
	case bool:
		return nil, errors.New("a boolean is no integer column")
	}
	return nil, fmt.Errorf("%T is no integer column", v)
}

func scanRunTx(ctx context.Context, tx pgx.Tx, id int64) (*runRow, error) {
	var x runRow
	err := tx.QueryRow(ctx, `SELECT `+runColumns+` FROM runs r WHERE r.id = $1`, id).Scan(x.dest()...)
	return &x, err
}

// closeIfQueuedTooLong is close_if_queued_too_long, for a run that is not a scheduled
// one (those go to the Python gateway, whose notice may go out).
func (q *request) closeIfQueuedTooLong(run *runRow) (*runRow, error) {
	if ageSeconds(run.StartedAt) <= q.d.Panel.QueueTimeout() {
		return run, nil
	}
	err := pgx.BeginFunc(q.ctx, q.d.DB, func(tx pgx.Tx) error {
		tag, err := tx.Exec(q.ctx, `UPDATE runs SET status = 'FAILURE', error = $2, finished_at = $3 WHERE id = $1 AND status = 'PENDING'`,
			run.ID, queueTimedOut, time.Now().UTC())
		if err != nil || tag.RowsAffected() != 1 {
			return err
		}
		after, err := scanRunTx(q.ctx, tx, run.ID)
		if err != nil {
			return err
		}
		return q.enqueueWebhooks(tx, after)
	})
	if err != nil {
		return nil, err
	}
	return q.readRun(run.ID)
}

// closeIfNeverQueued is _close_if_never_queued.
func (q *request) closeIfNeverQueued(run *runRow) (*runRow, error) {
	if ageSeconds(run.StartedAt) <= neverQueuedAfterSeconds {
		return run, nil
	}
	if _, err := q.d.DB.Exec(q.ctx, `UPDATE runs SET status = 'FAILURE', finished_at = $2, error = $3
		WHERE id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`, run.ID, time.Now().UTC(), neverQueued); err != nil {
		return nil, err
	}
	return q.readRun(run.ID)
}

type dsSnapshot struct {
	ID            int64
	Bucket, Key   string
	Kind          string
	OwnerID       *int64
	SnapshotRunID *int64
}

// finalizeIngest is _finalize_ingest: the snapshot's swap, its datasource locked.
func (q *request) finalizeIngest(tx pgx.Tx, run *runRow, result pyjson.Object) error {
	var ds *dsSnapshot
	if run.DatasourceID != nil {
		var x dsSnapshot
		err := tx.QueryRow(q.ctx, `SELECT id, bucket, key, kind, owner_id, snapshot_run_id FROM datasources WHERE id = $1 FOR UPDATE`,
			*run.DatasourceID).Scan(&x.ID, &x.Bucket, &x.Key, &x.Kind, &x.OwnerID, &x.SnapshotRunID)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if err == nil {
			ds = &x
		}
	}
	if ds == nil {
		return q.scheduleBlobDeletion(tx, run.OutputBucket, run.OutputKey, "datasource gone during the refresh")
	}
	if ds.SnapshotRunID != nil && run.ID < *ds.SnapshotRunID {
		return q.scheduleBlobDeletion(tx, run.OutputBucket, run.OutputKey,
			fmt.Sprintf("refresh %d overtaken by run %d", run.ID, *ds.SnapshotRunID))
	}
	bucket := pyjson.Or(result.Value("bucket"), run.OutputBucket)
	rows, err := intColumn(result.Value("rows_written"))
	if err != nil {
		return err
	}
	now := time.Now().UTC()
	if _, err := tx.Exec(q.ctx, `UPDATE datasources SET bucket = $2, key = $3, rows = $4, columns = $5, refreshed_at = $6,
		updated_at = $6, snapshot_run_id = $7 WHERE id = $1`, ds.ID, pyjson.Str(bucket), run.OutputKey, rows,
		pyDumps(pyjson.Or(result.Value("columns"), []any{})), now, run.ID); err != nil {
		return err
	}
	if ds.Key != "" && ds.Key != run.OutputKey {
		return q.scheduleBlobDeletion(tx, ds.Bucket, ds.Key, fmt.Sprintf("snapshot of datasource %d replaced", ds.ID))
	}
	return nil
}

// publishDatasource is _publish_datasource: the datasource the run promised, created or
// overwritten.
func (q *request) publishDatasource(tx pgx.Tx, run *runRow, result pyjson.Object) error {
	columns := pyDumps(pyjson.Or(result.Value("columns"), []any{}))
	rows, err := intColumn(result.Value("rows_written"))
	if err != nil {
		return err
	}
	find := func(name string) (*dsSnapshot, error) {
		var x dsSnapshot
		err := tx.QueryRow(q.ctx, `SELECT id, bucket, key, kind, owner_id, snapshot_run_id FROM datasources
			WHERE project_id = $1 AND name = $2 ORDER BY id LIMIT 1`, *run.PublishProjectID, name).
			Scan(&x.ID, &x.Bucket, &x.Key, &x.Kind, &x.OwnerID, &x.SnapshotRunID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return &x, err
	}
	if run.PublishOverwrite {
		existing, err := find(*run.PublishName)
		if err != nil {
			return err
		}
		if existing != nil && existing.Kind == "flow" {
			if existing.SnapshotRunID != nil && run.ID < *existing.SnapshotRunID {
				return q.scheduleBlobDeletion(tx, run.OutputBucket, run.OutputKey,
					fmt.Sprintf("publish %d overtaken by run %d", run.ID, *existing.SnapshotRunID))
			}
			owner := existing.OwnerID
			if owner == nil || *owner == 0 {
				owner = run.LaunchedBy
			}
			if _, err := tx.Exec(q.ctx, `UPDATE datasources SET bucket = $2, key = $3, rows = $4, columns = $5, sort_keys = $6,
				description = $7, flow_id = $8, owner_id = $9, snapshot_run_id = $10 WHERE id = $1`, existing.ID, run.OutputBucket,
				run.OutputKey, rows, columns, run.PublishSortKeys, run.PublishDescription, run.FlowID, owner, run.ID); err != nil {
				return err
			}
			if _, err := tx.Exec(q.ctx, `UPDATE runs SET datasource_id = $2 WHERE id = $1`, run.ID, existing.ID); err != nil {
				return err
			}
			if existing.Key != "" && existing.Key != run.OutputKey {
				return q.scheduleBlobDeletion(tx, existing.Bucket, existing.Key, fmt.Sprintf("datasource %d overwritten", existing.ID))
			}
			return nil
		}
	}
	suffix := fmt.Sprintf("%s (%s)", *run.PublishName, runes(run.TaskID, 8))
	first := *run.PublishName
	if taken, err := find(first); err != nil {
		return err
	} else if taken != nil {
		first = suffix
	}
	candidates := []string{first}
	if suffix != first {
		candidates = append(candidates, suffix)
	}
	for _, name := range candidates {
		var id int64
		err := pgx.BeginFunc(q.ctx, tx, func(sp pgx.Tx) error { // a savepoint: a clash undoes this attempt only
			now := time.Now().UTC()
			return sp.QueryRow(q.ctx, `INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns,
				sort_keys, column_descriptions, kind, flow_id, snapshot_run_id, created_at, updated_at)
				VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '{}', 'flow', $10, $11, $12, $12) RETURNING id`,
				name, run.PublishDescription, *run.PublishProjectID, run.LaunchedBy, run.OutputBucket, run.OutputKey, rows, columns,
				run.PublishSortKeys, run.FlowID, run.ID, now).Scan(&id)
		})
		if err != nil {
			var pe interface{ SQLState() string }
			if errors.As(err, &pe) && strings.HasPrefix(pe.SQLState(), "23") { // IntegrityError
				continue
			}
			return err
		}
		_, err = tx.Exec(q.ctx, `UPDATE runs SET datasource_id = $2 WHERE id = $1`, run.ID, id)
		return err
	}
	q.d.Log.Error("run: datasource not published (repeated conflicts or project deleted)", "run", run.ID)
	return q.scheduleBlobDeletion(tx, run.OutputBucket, run.OutputKey, fmt.Sprintf("publishing of run %d failed", run.ID))
}

// runEvent is webhooks.run_event.
func runEvent(run *runRow) string {
	outcome := map[string]string{"SUCCESS": "succeeded", "FAILURE": "failed"}[run.Status]
	if outcome == "" || run.FlowGroupID != nil || run.SavedScriptID != nil {
		return ""
	}
	if run.Kind == "orchestration" || (run.Kind == "flow" && run.ParentRunID == nil) {
		return "flow.run." + outcome
	}
	if run.Kind == "ingest" {
		return "datasource.refresh." + outcome
	}
	return ""
}

// webhookISO is webhooks._iso.
func webhookISO(t *time.Time) any {
	if t == nil {
		return nil
	}
	return strings.Replace(pyjson.Isoformat(*t), "+00:00", "Z", 1)
}

// enqueueWebhooks is webhooks.enqueue_for_run: the deliveries born in the transaction that
// closes the run. A fault is logged, never returned.
func (q *request) enqueueWebhooks(tx pgx.Tx, run *runRow) error {
	event := runEvent(run)
	if event == "" {
		return nil
	}
	type hook struct {
		id      int64
		project *int64
	}
	rows, err := q.d.DB.Query(q.ctx, `SELECT id, events, project_id FROM webhooks WHERE active`)
	if err != nil {
		q.d.Log.Error("webhook: deliveries not prepared", "run", run.ID, "err", err)
		return nil
	}
	var subscribed []hook
	for rows.Next() {
		var h hook
		var events string
		if err := rows.Scan(&h.id, &events, &h.project); err != nil {
			rows.Close()
			return nil
		}
		if events == "" {
			events = "[]"
		}
		if v, err := pyjson.Loads(events); err == nil {
			list, _ := v.([]any)
			for _, e := range list {
				if e == event {
					subscribed = append(subscribed, h)
					break
				}
			}
		}
	}
	rows.Close()
	if len(subscribed) == 0 {
		return nil
	}
	var duration any
	if run.FinishedAt != nil {
		duration = pyRound(float64(run.FinishedAt.Sub(run.StartedAt).Microseconds())/1e6, 1)
	}
	var errText any
	if run.Error != nil && *run.Error != "" {
		errText = runes(*run.Error, webhookPayloadMaxErrorLn)
	}
	data := pyjson.Object{{Key: "run", Value: pyjson.Object{{Key: "id", Value: run.ID}, {Key: "kind", Value: run.Kind},
		{Key: "status", Value: run.Status}, {Key: "trigger", Value: run.TriggerType}, {Key: "started_at", Value: webhookISO(&run.StartedAt)},
		{Key: "finished_at", Value: webhookISO(run.FinishedAt)}, {Key: "duration_seconds", Value: duration},
		{Key: "rows_written", Value: optInt64(run.RowsWritten)}, {Key: "error", Value: errText}}}}
	var folder *int64
	read := func(sql string, id int64) (string, int64, bool) {
		var name string
		var project int64
		err := q.d.DB.QueryRow(q.ctx, sql, id).Scan(&name, &project)
		return name, project, err == nil
	}
	if run.FlowID != nil && run.Kind != "ingest" {
		if name, project, ok := read(`SELECT name, project_id FROM flows WHERE id = $1`, *run.FlowID); ok {
			data = data.Set("flow", pyjson.Object{{Key: "id", Value: *run.FlowID}, {Key: "name", Value: name}})
			folder = &project
		}
	}
	if run.Kind == "ingest" && run.DatasourceID != nil {
		if name, project, ok := read(`SELECT name, project_id FROM datasources WHERE id = $1`, *run.DatasourceID); ok {
			data = data.Set("datasource", pyjson.Object{{Key: "id", Value: *run.DatasourceID}, {Key: "name", Value: name}})
			folder = &project
		}
	}
	if folder != nil {
		var name any
		var n string
		if err := q.d.DB.QueryRow(q.ctx, `SELECT name FROM projects WHERE id = $1`, *folder).Scan(&n); err == nil {
			name = n
		}
		data = data.Set("folder", pyjson.Object{{Key: "id", Value: *folder}, {Key: "name", Value: name}})
	}
	parents, err := perm.For(q.d.DB, &q.p.User).Parents(q.ctx)
	if err != nil {
		return nil
	}
	at := time.Now().UTC()
	for _, h := range subscribed {
		in := h.project == nil
		if !in && folder != nil {
			for _, a := range perm.Ancestors(parents, *folder) {
				in = in || a == *h.project
			}
		}
		if !in {
			continue
		}
		body, _ := pyjson.Dumps(pyjson.Object{{Key: "id", Value: newUUID()}, {Key: "event", Value: event},
			{Key: "created_at", Value: webhookISO(&at)}, {Key: "data", Value: data}}, pyjson.Options{Compact: true})
		if _, err := tx.Exec(q.ctx, `INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts, next_attempt_at, created_at)
			VALUES ($1, $2, $3, 'pending', 0, $4, $4)`, h.id, event, body, at); err != nil {
			return err
		}
	}
	return nil
}

func optInt64(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

// newUUID is str(uuid.uuid4()).
func newUUID() string {
	h := newHex()
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

// runProgressModel is RunProgress, to validate what the engine says as pydantic does.
var runProgressModel = pyd.NewModel("RunProgress",
	pyd.F("phase", pyd.Str()), pyd.F("seconds", pyd.Int()),
	pyd.F("rows", pyd.Optional(pyd.Int())).Default(nil), pyd.F("file", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("files", pyd.Optional(pyd.Int())).Default(nil), pyd.F("call", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("calls", pyd.Optional(pyd.Int())).Default(nil), pyd.F("pages", pyd.Optional(pyd.Int())).Default(nil),
)

// runOut is run_out: a run not finished carries its progress.
func runOut(run *runRow, progress progressOf) RunOut {
	out := run.out()
	if terminal[run.Status] {
		return out
	}
	phase := pyd.NewObject()
	if run.Status == "PENDING" {
		phase.Set("phase", "queued")
		phase.Set("seconds", int64(ageSeconds(run.StartedAt)))
	} else {
		from := progress[run.ID]
		for _, k := range []string{"phase", "rows", "file", "files", "call", "calls", "pages"} {
			if v, ok := from.Get(k); ok {
				phase.Set(k, toPyd(v))
			}
		}
		if _, ok := phase.Get("phase"); !ok {
			phase.Set("phase", "running")
		}
		started := run.StartedAt
		if run.EngineStartedAt != nil {
			started = *run.EngineStartedAt
		}
		phase.Set("seconds", int64(ageSeconds(started)))
	}
	v, errs := runProgressModel.Type().Validate(phase)
	if len(errs) > 0 {
		return out // an engine that sends something else: shown without progress
	}
	mv := v.(*pyd.ModelValue)
	p := &runProgress{Phase: mv.Get("phase").(string), Seconds: mv.Get("seconds").(int64)}
	for k, dst := range map[string]**int64{"rows": &p.Rows, "file": &p.File, "files": &p.Files, "call": &p.Call, "calls": &p.Calls, "pages": &p.Pages} {
		if n, ok := mv.Get(k).(int64); ok {
			*dst = &n
		}
	}
	out.Progress = p
	return out
}

// getRun is GET /runs/{run_id} (get_run): read with the rights of the object it belongs to,
// then reconciled.
func (d *Deps) getRun(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "run_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	run, err := q.readRun(ids[0])
	if err != nil {
		q.fail(err)
		return
	}
	notFound := func() { q.refuse(http.StatusNotFound, "Run not found") }
	if run == nil {
		notFound()
		return
	}
	adminOrLauncher := func() (bool, error) {
		admin, err := q.c.IsAdmin(q.ctx)
		return admin || (run.LaunchedBy != nil && *run.LaunchedBy == p.User.ID), err
	}
	projectOf := func(sql string, id int64) (*int64, error) {
		var pid int64
		err := d.DB.QueryRow(q.ctx, sql, id).Scan(&pid)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return &pid, err
	}
	switch {
	case run.Kind == "ingest":
		var pid *int64
		if run.DatasourceID != nil {
			if pid, err = projectOf(`SELECT project_id FROM datasources WHERE id = $1`, *run.DatasourceID); err != nil {
				q.fail(err)
				return
			}
		}
		if pid != nil {
			if !q.ensure(*pid, perm.View) {
				return
			}
		} else if ok, err := adminOrLauncher(); err != nil || !ok {
			if err != nil {
				q.fail(err)
			} else {
				notFound()
			}
			return
		}
	case run.FlowGroupID != nil:
		pid, err := projectOf(`SELECT project_id FROM flow_groups WHERE id = $1`, *run.FlowGroupID)
		if err != nil {
			q.fail(err)
			return
		}
		if pid == nil {
			notFound()
			return
		}
		if !q.ensure(*pid, perm.View) {
			return
		}
	case run.SavedScriptID != nil || (run.Kind == "script" && run.FlowID == nil):
		var s *flowaccess.SavedScript
		if run.SavedScriptID != nil {
			if s, err = q.acc.SavedScriptByID(q.ctx, *run.SavedScriptID); err != nil {
				q.fail(err)
				return
			}
		}
		if s == nil {
			if ok, err := adminOrLauncher(); err != nil || !ok {
				if err != nil {
					q.fail(err)
				} else {
					notFound()
				}
				return
			}
		} else if see, err := q.acc.CanSeeScript(q.ctx, s); err != nil || !see {
			if err != nil {
				q.fail(err)
			} else {
				notFound()
			}
			return
		}
	case run.Kind == "sql":
		var pid *int64
		if run.SQLScriptID != nil {
			if pid, err = projectOf(`SELECT project_id FROM sql_scripts WHERE id = $1`, *run.SQLScriptID); err != nil {
				q.fail(err)
				return
			}
		}
		if pid == nil {
			if ok, err := adminOrLauncher(); err != nil || !ok {
				if err != nil {
					q.fail(err)
				} else {
					notFound()
				}
				return
			}
		} else if see, err := q.c.Has(q.ctx, *pid, perm.View); err != nil || !see {
			if err != nil {
				q.fail(err)
			} else {
				notFound()
			}
			return
		}
	case run.FlowID == nil && run.ParentRunID != nil:
		var pid *int64
		if parent, err := q.readRun(*run.ParentRunID); err != nil {
			q.fail(err)
			return
		} else if parent != nil && parent.FlowGroupID != nil {
			if pid, err = projectOf(`SELECT project_id FROM flow_groups WHERE id = $1`, *parent.FlowGroupID); err != nil {
				q.fail(err)
				return
			}
		}
		if pid == nil {
			if ok, err := adminOrLauncher(); err != nil || !ok {
				if err != nil {
					q.fail(err)
				} else {
					notFound()
				}
				return
			}
		} else if !q.ensure(*pid, perm.View) {
			return
		}
	default:
		var fid int64
		if run.FlowID != nil {
			fid = *run.FlowID
		}
		f := q.flow(fid)
		if f == nil || !q.ensure(f.ProjectID, perm.View) {
			return
		}
		if run.Kind == "script" || run.Kind == "container" {
			if why, err := q.acc.Blocked(q.ctx, f.Definition); err != nil || why != "" {
				if err != nil {
					q.fail(err)
				} else {
					notFound()
				}
				return
			}
		}
	}
	if py, err := q.needsPython(run); err != nil {
		q.fail(err)
		return
	} else if py {
		d.Upstream.ServeHTTP(w, r)
		return
	}
	progress := progressOf{}
	if run, err = q.reconcile(run, progress); err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, runOut(run, progress))
}

// listRuns is GET /flows/{flow_id}/runs: the flow's last 50 runs, each reconciled.
func (d *Deps) listRuns(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return
	}
	rows, err := d.DB.Query(q.ctx, `SELECT `+runColumns+` FROM runs r WHERE r.flow_id = $1 ORDER BY r.started_at DESC LIMIT 50`, f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	var runs []*runRow
	for rows.Next() {
		var x runRow
		if err := rows.Scan(x.dest()...); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		runs = append(runs, &x)
	}
	rows.Close()
	for _, run := range runs {
		if py, err := q.needsPython(run); err != nil {
			q.fail(err)
			return
		} else if py {
			d.Upstream.ServeHTTP(w, r)
			return
		}
	}
	progress := progressOf{}
	out := []RunOut{}
	for _, run := range runs {
		x, err := q.reconcile(run, progress)
		if err != nil {
			q.fail(err)
			return
		}
		out = append(out, runOut(x, progress))
	}
	server.JSON(w, http.StatusOK, out)
}
