package api

// routes/workers.py on Kubernetes with KUBERNETES__SCALING on: the autoscaling bounds of the
// workers' pods, read from the cluster (services/kube_scaling.py read) and set from the panel
// (scala, give_back). The cluster is asked with the gateway's service account
// (kube.ScalingCall); the bounds kept from the panel are the table worker_scaling, under the
// advisory lock 7402119 the loop that puts them back asks too (internal/jobs/scaling).

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// scalingLock is kube_scaling.LOCK.
const scalingLock = 7_402_119

// scalingRow is a row of worker_scaling.
type scalingRow struct {
	min, max  int64
	updatedAt time.Time
	updatedBy *string
}

func readScalingRow(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, kind string) (*scalingRow, error) {
	var r scalingRow
	err := q.QueryRow(ctx, `SELECT min_replicas, max_replicas, updated_at, updated_by FROM worker_scaling WHERE kind = $1`, kind).
		Scan(&r.min, &r.max, &r.updatedAt, &r.updatedBy)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// orNone is `v or None` of a text.
func orNone(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// scalingRead is kube_scaling.read: the Deployment's pods and its autoscaler's bounds as the
// cluster has them, in the order read builds them. A *kube.Error when the cluster says no;
// any other error is a 500 (an answer that is not what read expects).
func scalingRead(ctx context.Context, d *Deps, kind string) (pyjson.Object, error) {
	cfg := d.Config.Jobs.Kube
	t, err := kube.TargetOf(cfg, kind)
	if err != nil {
		return nil, err
	}
	out := pyjson.Object{{Key: "deployment", Value: orNone(t.Deployment)}, {Key: "autoscaler", Value: orNone(t.Autoscaler)},
		{Key: "autoscaler_name", Value: orNone(t.AutoscalerName)}, {Key: "ceiling", Value: t.Ceiling}, {Key: "floor", Value: t.Floor()}}
	if t.Deployment == "" {
		return nil, &kube.Error{Status: 0, Message: fmt.Sprintf("No Deployment configured for the %s workers (KUBERNETES__%s_WORKER_DEPLOYMENT)",
			kind, strings.ToUpper(kind))}
	}
	depPath, autoPath, err := kube.ScalingPaths(cfg, t)
	if err != nil {
		return nil, err
	}
	k := kubeAPI()
	dep, err := kube.ScalingCall(ctx, k, http.MethodGet, depPath, nil)
	if err != nil {
		return nil, err
	}
	statusV, err := cvGet(dep, "status")
	if err != nil {
		return nil, err
	}
	status := orDict(statusV)
	spec, err := cvGetOr(dep, "spec")
	if err != nil {
		return nil, err
	}
	replicas, err := cvGet(spec, "replicas")
	if err != nil {
		return nil, err
	}
	current, err := cvGet(status, "replicas")
	if err != nil {
		return nil, err
	}
	ready, err := cvGet(status, "readyReplicas")
	if err != nil {
		return nil, err
	}
	out = append(out, pyjson.KV{Key: "replicas", Value: replicas}, pyjson.KV{Key: "current", Value: pyjson.Or(current, int64(0))},
		pyjson.KV{Key: "ready", Value: pyjson.Or(ready, int64(0))})
	if autoPath != "" {
		obj, err := kube.ScalingCall(ctx, k, http.MethodGet, autoPath, nil)
		if err != nil {
			return nil, err
		}
		lo, hi, err := kube.Bounds(t, obj)
		if err != nil {
			return nil, err
		}
		out = append(out, pyjson.KV{Key: "min", Value: lo}, pyjson.KV{Key: "max", Value: hi})
		var desired any
		if t.Autoscaler == "hpa" {
			st, err := cvGetOr(obj, "status")
			if err != nil {
				return nil, err
			}
			if desired, err = cvGet(st, "desiredReplicas"); err != nil {
				return nil, err
			}
		}
		out = append(out, pyjson.KV{Key: "desired", Value: desired})
	}
	return out, nil
}

// scalingFields are ScalingOut's fields after on_kubernetes, in its order.
var scalingFields = []string{"deployment", "autoscaler", "autoscaler_name", "replicas", "current", "ready", "desired", "min", "max",
	"floor", "ceiling", "saved", "saved_min", "saved_max", "updated_at", "updated_by", "error"}

// workerScaling is _scaling(kind, row, full): ScalingOut, with what the cluster says set as
// setattr sets it: unchecked, and written as it came (the response's model serializes the
// instance without validating it again: a «replicas» of "3" stays a text).
func workerScaling(ctx context.Context, d *Deps, kind string, full bool) (pyjson.Object, error) {
	if !kube.FromEnv().Scaling {
		return noScaling(d.Config.Admin.OnKubernetes), nil
	}
	row, err := readScalingRow(ctx, d.DB, kind)
	if err != nil {
		return nil, err
	}
	values := map[string]any{"saved": row != nil}
	if row != nil {
		values["saved_min"], values["saved_max"], values["updated_at"] = row.min, row.max, pyjson.Isoformat(row.updatedAt)
		values["updated_by"] = updatedBy(row.updatedBy, full)
	}
	read, err := scalingRead(ctx, d, kind)
	var ke *kube.Error
	switch {
	case errors.As(err, &ke):
		t, _ := kube.TargetOf(d.Config.Jobs.Kube, kind)
		values["deployment"], values["autoscaler"], values["ceiling"], values["floor"] = orNone(t.Deployment), orNone(t.Autoscaler), t.Ceiling, t.Floor()
		values["error"] = ke.Message
	case err != nil:
		return nil, err
	default:
		for _, kv := range read {
			values[kv.Key] = kv.Value
		}
	}
	out := pyjson.Object{{Key: "mode", Value: "kubernetes"}, {Key: "on_kubernetes", Value: false}}
	for _, k := range scalingFields {
		v := values[k]
		if b, isBool := v.(bool); isBool && k != "saved" {
			// an int field's serializer writes a bool as the int it is
			v = int64(0)
			if b {
				v = int64(1)
			}
		}
		out = append(out, pyjson.KV{Key: k, Value: v})
	}
	return out, nil
}

// scalingTaken is the 409 when the lock does not come.
const scalingTaken = "Another change to the scaling is in progress: try again in a moment"

// changeScaling is scala: the bounds first on the cluster, then saved; what the panel reads
// after is what the cluster has.
func changeScaling(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, kind string, lo, hi int64) {
	ctx := r.Context()
	cfg := d.Config.Jobs.Kube
	t, err := kube.TargetOf(cfg, kind)
	if err != nil {
		fail500(d, w, "workers: the target", err)
		return
	}
	if problem := kube.Check(t, lo, hi); problem != nil {
		writePyStatus(w, http.StatusUnprocessableEntity, pyjson.Object{{Key: "detail", Value: pyjson.Object{{Key: "errors", Value: []any{problem}}}}})
		return
	}
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		fail500(d, w, "workers: the lock", err)
		return
	}
	defer tx.Rollback(ctx)
	got, err := lockSettings(ctx, tx, scalingLock)
	if err != nil {
		fail500(d, w, "workers: the lock", err)
		return
	}
	if !got {
		server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: scalingTaken})
		return
	}
	before, err := scalingRead(ctx, d, kind)
	if err == nil {
		err = scalingApply(ctx, d, t, lo, hi)
	}
	var ke *kube.Error
	if errors.As(err, &ke) {
		server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: "Nothing was changed. " + ke.Message})
		return
	}
	if err != nil {
		fail500(d, w, "workers: the cluster", err)
		return
	}
	now := nowNaive()
	if _, err := tx.Exec(ctx, `INSERT INTO worker_scaling (kind, min_replicas, max_replicas, updated_at, updated_by) VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (kind) DO UPDATE SET min_replicas = EXCLUDED.min_replicas, max_replicas = EXCLUDED.max_replicas,
		updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by`, kind, lo, hi, now, p.User.Email); err != nil {
		fail500(d, w, "workers: the bounds", err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		fail500(d, w, "workers: the bounds", err)
		return
	}
	target := "workers"
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "workers.scaling_update", TargetType: &target, TargetLabel: &kind,
		Detail: pyjson.Object{{Key: "from", Value: pyjson.Object{{Key: "min", Value: before.Value("min")}, {Key: "max", Value: before.Value("max")}}},
			{Key: "to", Value: pyjson.Object{{Key: "min", Value: lo}, {Key: "max", Value: hi}}}}})
	workersState(d, w, r, p)
}

// scalingApply is kube_scaling.apply.
func scalingApply(ctx context.Context, d *Deps, t kube.ScalingTarget, lo, hi int64) error {
	_, autoPath, err := kube.ScalingPaths(d.Config.Jobs.Kube, t)
	if err != nil {
		return err
	}
	if autoPath == "" {
		return &kube.Error{Status: 0, Message: fmt.Sprintf("No autoscaler configured for the %s workers", t.Kind)}
	}
	_, err = kube.ScalingCall(ctx, kubeAPI(), http.MethodPatch, autoPath, kube.Patch(t, lo, hi))
	return err
}

// giveBackScaling is give_back: the panel's bounds no longer kept; the cluster keeps the
// current ones until the chart puts its own back.
func giveBackScaling(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, kind string) {
	ctx := r.Context()
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		fail500(d, w, "workers: the lock", err)
		return
	}
	defer tx.Rollback(ctx)
	got, err := lockSettings(ctx, tx, scalingLock)
	if err != nil {
		fail500(d, w, "workers: the lock", err)
		return
	}
	if !got {
		server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: scalingTaken})
		return
	}
	row, err := readScalingRow(ctx, tx, kind)
	if err == nil && row != nil {
		_, err = tx.Exec(ctx, `DELETE FROM worker_scaling WHERE kind = $1`, kind)
	}
	if err == nil {
		err = tx.Commit(ctx) // releases the lock
	}
	if err != nil {
		fail500(d, w, "workers: the bounds", err)
		return
	}
	if row != nil {
		target := "workers"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "workers.scaling_reset", TargetType: &target, TargetLabel: &kind,
			Detail: pyjson.Object{{Key: "from", Value: pyjson.Object{{Key: "min", Value: row.min}, {Key: "max", Value: row.max}}}}})
	}
	workersState(d, w, r, p)
}
