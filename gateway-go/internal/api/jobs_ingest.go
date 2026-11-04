package api

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
)

// routes/runs.py start_refresh_if_free, as the background work starts
// a datasource's refresh (the scheduler, a flow's Refresh node, a group's cell).

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
// last import is reconciled first; then the check and the launch (runlaunch.Ingest, the
// routes' launch too) happen in line with every other starter (the advisory lock held until
// the new run is committed).
func (d *Deps) startRefreshIfFree(ctx context.Context, user *auth.User, dsID int64, conn *connpayload.Conn,
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
			if _, err := q.reconcile(last, progressOf{}); err != nil {
				return 0, err
			}
		}
	}
	var runID int64
	err = pgx.BeginFunc(ctx, d.DB, func(tx pgx.Tx) error {
		if err := runlaunch.InLine(ctx, tx, dsID); err != nil {
			return err
		}
		if live, err := runlaunch.LiveRefresh(ctx, tx, dsID); err != nil || live {
			if err == nil {
				err = errRollback // releases the lock
			}
			return err
		}
		ds, err := loadJobDatasource(ctx, tx, dsID)
		if err != nil {
			return err
		}
		if ds == nil {
			return errors.New("the datasource vanished")
		}
		src := &runlaunch.Source{ID: ds.ID, Bucket: ds.Bucket, Key: ds.Key, SourceType: ds.SourceType, SourceRef: ds.SourceRef, SortKeys: ds.SortKeys}
		id, ref, err := runlaunch.Ingest(ctx, d.ingestEnv(tx), user.ID, src, conn, trigger, parent)
		runID = id
		return launchError(ref, err)
	})
	if errors.Is(err, errRollback) {
		return 0, nil
	}
	return runID, err
}

// launchError is what a launch of internal/runlaunch raised, as the background loops read
// it: the refusal as the HTTPException it is, the engine down as its transport error.
func launchError(ref *auth.Refusal, err error) error {
	if ref != nil {
		return &httpError{Status: ref.Status, Detail: ref.Detail}
	}
	var down *runlaunch.EngineDown
	if errors.As(err, &down) {
		return down.Err
	}
	return err
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

// valueOr is ref.get(k, default).
func valueOr(o pyjson.Object, k string, def any) any {
	if v, ok := o.Get(k); ok {
		return v
	}
	return def
}
