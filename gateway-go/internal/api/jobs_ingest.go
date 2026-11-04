package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// routes/runs.py start_refresh_if_free and launch_ingest_run, as the background work starts
// a datasource's refresh (the scheduler, a flow's Refresh node, a group's cell).

// refreshLock is routes/runs.py _REFRESH_LOCK: «one refresh per datasource».
const refreshLock = 74610002

// httpError is an HTTPException the Python code raises where a background loop catches it
// (its detail ends up in a run's error).
type httpError struct {
	Status int
	Detail string
}

func (e *httpError) Error() string { return e.Detail }

type jobDatasource struct {
	ID, ProjectID                    int64
	Name, Bucket, Key, SortKeys      string
	ConnectionID, RefreshScheduledBy *int64
	SourceType, SourceRef, Schedule  *string
	RefreshedAt                      *time.Time
	UpdatedAt, CreatedAt             time.Time
	NextRefreshAt                    *time.Time
	RefreshSchedule                  *string
}

// loadJobDatasource is session.get(Datasource, id): nil when gone.
func loadJobDatasource(ctx context.Context, q perm.Querier, id int64) (*jobDatasource, error) {
	var x jobDatasource
	err := q.QueryRow(ctx, `SELECT id, project_id, name, bucket, key, sort_keys, connection_id, refresh_scheduled_by, source_type,
		source_ref, refreshed_at, updated_at, created_at, next_refresh_at, refresh_schedule FROM datasources WHERE id = $1`, id).
		Scan(&x.ID, &x.ProjectID, &x.Name, &x.Bucket, &x.Key, &x.SortKeys, &x.ConnectionID, &x.RefreshScheduledBy, &x.SourceType,
			&x.SourceRef, &x.RefreshedAt, &x.UpdatedAt, &x.CreatedAt, &x.NextRefreshAt, &x.RefreshSchedule)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	x.Schedule = x.RefreshSchedule
	return &x, nil
}

// startRefreshIfFree is start_refresh_if_free: the new run's id, 0 when one is running. The
// last import is reconciled first; then the check and the launch happen in line with every
// other starter (the advisory lock held until the new run is committed).
func (d *Deps) startRefreshIfFree(ctx context.Context, sc *scriptcfg.Settings, user *auth.User, dsID int64, conn *engineConn,
	trigger string, parent *int64) (int64, error) {
	q := d.jobRequest(ctx, user)
	var lastID int64
	err := d.DB.QueryRow(ctx, `SELECT id FROM runs WHERE datasource_id = $1 AND kind = 'ingest' ORDER BY started_at DESC LIMIT 1`, dsID).
		Scan(&lastID)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return 0, err
	}
	if err == nil {
		last, err := q.readRun(lastID)
		if err != nil {
			return 0, err
		}
		if last != nil && !terminal[last.Status] {
			if _, err := q.reconcileAny(last, sc); err != nil {
				return 0, err
			}
		}
	}
	var runID int64
	err = pgx.BeginFunc(ctx, d.DB, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, $2)`, refreshLock, int32(dsID)); err != nil {
			return err
		}
		var live int64
		err := tx.QueryRow(ctx, `SELECT id FROM runs WHERE datasource_id = $1 AND (kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE'))
			ORDER BY id DESC LIMIT 1`, dsID).Scan(&live)
		if err == nil {
			return errRollback // releases the lock
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		ds, err := loadJobDatasource(ctx, tx, dsID)
		if err != nil {
			return err
		}
		if ds == nil {
			return errors.New("the datasource vanished")
		}
		runID, err = d.launchIngestRun(ctx, tx, user, ds, conn, trigger, parent)
		return err
	})
	if errors.Is(err, errRollback) {
		return 0, nil
	}
	return runID, err
}

// templateKeys is _TEMPLATE_KEYS.
var templateKeys = []string{"timezone", "variables", "last_refresh_initial", "mode", "keys"}

// pick is {k: ref[k] for k in keys if k in ref}.
func pick(ref pyjson.Object, keys ...string) pyjson.Object {
	out := pyjson.Object{}
	for _, k := range keys {
		if v, ok := ref.Get(k); ok {
			out = append(out, pyjson.KV{Key: k, Value: v})
		}
	}
	return out
}

// merge is {**a, **b}.
func merge(objs ...pyjson.Object) pyjson.Object {
	out := pyjson.Object{}
	for _, o := range objs {
		for _, kv := range o {
			out = out.Set(kv.Key, kv.Value)
		}
	}
	return out
}

// lastSuccessfulRefresh is _last_successful_refresh: when the last successful refresh started.
func lastSuccessfulRefresh(ctx context.Context, q perm.Querier, dsID int64) (*string, error) {
	var started time.Time
	err := q.QueryRow(ctx, `SELECT started_at FROM runs WHERE datasource_id = $1 AND kind = 'ingest' AND status = 'SUCCESS'
		ORDER BY started_at DESC LIMIT 1`, dsID).Scan(&started)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	s := pyjson.Isoformat(started)
	return &s, nil
}

// refreshContext is _refresh_context.
func refreshContext(ctx context.Context, q perm.Querier, ds *jobDatasource, ref pyjson.Object, fields ...string) (pyjson.Object, error) {
	out := pyjson.Object{}
	templated := pyjson.Truthy(ref.Value("variables"))
	for _, f := range fields {
		v := ref.Value(f)
		text := ""
		if pyjson.Truthy(v) {
			text = pyjson.Str(v)
		}
		templated = templated || strings.Contains(text, "{")
	}
	if templated {
		last, err := lastSuccessfulRefresh(ctx, q, ds.ID)
		if err != nil {
			return nil, err
		}
		if last != nil && *last != "" {
			out = append(out, pyjson.KV{Key: "last_refresh", Value: *last})
		}
	}
	if ref.Value("mode") == "append" && ds.Key != "" {
		out = append(out, pyjson.KV{Key: "previous", Value: pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}}})
	}
	return out, nil
}

// launchIngestRun is launch_ingest_run: the import queued on the engine and its run
// recorded, in the caller's transaction (committed by it).
func (d *Deps) launchIngestRun(ctx context.Context, tx pgx.Tx, user *auth.User, ds *jobDatasource, conn *engineConn, trigger string,
	parent *int64) (int64, error) {
	bucket := d.Config.EngineBucket
	outputKey := snapshotKey(&ds.ID)
	q := d.jobRequest(ctx, user)
	document, err := q.contractPayload(&ds.ID)
	if err != nil {
		return 0, err
	}
	contract := pyjson.Object{}
	if document != nil {
		contract = pyjson.Object{{Key: "contract", Value: document}}
	}
	refOf := func() (pyjson.Object, error) {
		text := deref(ds.SourceRef)
		if text == "" {
			text = "{}"
		}
		v, err := pyjson.Loads(text)
		if err != nil {
			return nil, err
		}
		o, ok := v.(pyjson.Object)
		if !ok {
			return nil, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(v))
		}
		return o, nil
	}
	head := func(source any) pyjson.Object {
		return pyjson.Object{{Key: "connection", Value: conn.payload()}, {Key: "source", Value: source}, {Key: "bucket", Value: bucket},
			{Key: "output_key", Value: outputKey}}
	}
	var path string
	var body pyjson.Object
	switch conn.DBType {
	case "sharepoint":
		ref, err := refOf()
		if err != nil {
			return 0, err
		}
		source := merge(pyjson.Object{{Key: "path", Value: valueOr(ref, "path", "")}, {Key: "sheet", Value: valueOr(ref, "sheet", "")}},
			pick(ref, "format", "csv"), pick(ref, templateKeys...))
		extra, err := refreshContext(ctx, tx, ds, ref, "path")
		if err != nil {
			return 0, err
		}
		path, body = "/sharepoint/ingest", merge(head(source), extra, contract)
	case "api":
		ref, err := refOf()
		if err != nil {
			return 0, err
		}
		last, err := lastSuccessfulRefresh(ctx, tx, ds.ID)
		if err != nil {
			return 0, err
		}
		body = head(ref)
		if last != nil && *last != "" {
			body = append(body, pyjson.KV{Key: "last_refresh", Value: *last})
		}
		if ref.Value("mode") == "append" && ds.Key != "" {
			body = append(body, pyjson.KV{Key: "previous", Value: pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}}})
		}
		path, body = "/http-api/ingest", merge(body, contract)
	case "gsheets":
		ref, err := refOf()
		if err != nil {
			return 0, err
		}
		path, body = "/gsheets/ingest", merge(head(pick(ref, "spreadsheet_id", "sheet_id", "sheet", "range")), contract)
	case "sftp", "s3":
		ref, err := refOf()
		if err != nil {
			return 0, err
		}
		keys := []string{"folder", "pattern", "format", "sheet", "csv"}
		if conn.DBType == "s3" {
			keys = append([]string{"bucket"}, keys...)
		}
		extra, err := refreshContext(ctx, tx, ds, ref, "folder", "pattern")
		if err != nil {
			return 0, err
		}
		path, body = "/"+conn.DBType+"/ingest", merge(head(merge(pick(ref, keys...), pick(ref, templateKeys...))), extra, contract)
	default:
		sortKeys := ds.SortKeys
		if sortKeys == "" {
			sortKeys = "[]"
		}
		keys, err := pyjson.Loads(sortKeys)
		if err != nil {
			return 0, err
		}
		var mode, ref any
		if ds.SourceType != nil {
			mode = *ds.SourceType
		}
		if ds.SourceRef != nil {
			ref = *ds.SourceRef
		}
		path, body = "/db/ingest", merge(head(pyjson.Object{{Key: "mode", Value: mode}, {Key: "ref", Value: ref}, {Key: "sort_keys", Value: keys}}),
			contract)
	}
	cctx, cancel := withCheck(ctx)
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, path, body)
	cancel()
	if err != nil {
		return 0, err
	}
	if res.StatusCode >= 400 {
		return 0, &httpError{Status: res.StatusCode, Detail: runes(string(raw), 500)}
	}
	task, err := engineTaskID(raw)
	if err != nil {
		return 0, err
	}
	if task == "" {
		return 0, &httpError{Status: 502, Detail: "The engine did not return a task_id"}
	}
	var runID int64
	err = tx.QueryRow(ctx, `INSERT INTO runs (kind, datasource_id, task_id, status, launched_by, trigger_type, parent_run_id, input_key,
		output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
		VALUES ('ingest', $1, $2, 'PENDING', $3, $4, $5, $6, $7, $8, '', false, '[]', $9) RETURNING id`, ds.ID, task, user.ID, trigger, parent,
		fmt.Sprintf("%s://%s/%s", conn.DBType, conn.Host, conn.Database), bucket, outputKey, jobs.Now()).Scan(&runID)
	return runID, err
}

// valueOr is ref.get(k, default).
func valueOr(o pyjson.Object, k string, def any) any {
	if v, ok := o.Get(k); ok {
		return v
	}
	return def
}
