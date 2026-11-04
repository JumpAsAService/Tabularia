// Package scaling is services/kube_scaling.py reapply_loop: on Kubernetes, the workers'
// autoscaling bounds set from Admin → Workers (the table worker_scaling) put back every
// minute where the cluster has others (a helm upgrade, a hand edit). It returns at once
// where KUBERNETES__SCALING is off.
//
// One process at a time: the transaction's advisory lock 7402119, asked once (whoever is
// saving the bounds holds it and applies them), as the Python loop asks it.
package scaling

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	reapplyEvery = 60 * time.Second // REAPPLY_EVERY
	lockKey      = 7_402_119        // LOCK
)

func init() {
	api.RegisterJob("scaling_loop", func(d *api.Deps) jobs.Job {
		s := &scaling{db: d.DB, log: d.Log, panel: d.Panel, cfg: d.Config.Jobs.Kube, enabled: kube.FromEnv().Scaling,
			api: scriptcfg.Load(config.FromOS()).Kubernetes}
		return jobs.Job{Order: 8, Loop: s.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_round: how many bounds it put back
			"scaling": func(ctx context.Context) (any, error) {
				if !s.enabled {
					return nil, nil
				}
				return s.oneRound(ctx)
			},
		}}
	})
}

type scaling struct {
	db      *pgxpool.Pool
	log     *slog.Logger
	panel   *settings.Panel
	cfg     config.KubeJobsConfig
	enabled bool // KUBERNETES__SCALING
	api     scriptcfg.Kubernetes
}

// target is kube_scaling.Target.
type target struct {
	kind, deployment, autoscaler, autoscalerName string
	ceiling                                      int64
}

// target is kube_scaling.target(kind).
func (s *scaling) target(kind string) (target, error) {
	var auto, deployment string
	var ceiling int64
	switch kind {
	case "run":
		auto, deployment, ceiling = s.cfg.RunWorkerAutoscaler, s.cfg.RunWorkerDeployment, s.cfg.RunWorkerMaxReplicas
	case "preview":
		auto, deployment, ceiling = s.cfg.PreviewWorkerAutoscaler, s.cfg.PreviewWorkerDeployment, s.cfg.PreviewWorkerMaxReplicas
	default: // getattr(k, f"{kind}_worker_autoscaler"): an AttributeError
		return target{}, fmt.Errorf("'KubernetesSettings' object has no attribute '%s_worker_autoscaler'", kind)
	}
	aKind, aName, _ := strings.Cut(strings.TrimSpace(auto), "/")
	aKind, aName = strings.ToLower(strings.TrimSpace(aKind)), strings.TrimSpace(aName)
	if (aKind != "hpa" && aKind != "scaledobject") || aName == "" {
		aKind, aName = "", ""
	}
	return target{kind, strings.TrimSpace(deployment), aKind, aName, ceiling}, nil
}

// namespace is kube_scaling._namespace.
func (s *scaling) namespace() (string, error) {
	if s.cfg.Namespace != "" {
		return s.cfg.Namespace, nil
	}
	raw, err := os.ReadFile(s.cfg.NamespaceFile)
	if err != nil {
		return "", &kube.Error{Status: 0, Message: "The pod's namespace is unknown: set KUBERNETES__NAMESPACE"}
	}
	return strings.TrimSpace(string(raw)), nil
}

// autoscalerPath is kube_scaling._paths' second: "" when there is no autoscaler.
func (s *scaling) autoscalerPath(t target) (string, error) {
	ns, err := s.namespace()
	if err != nil {
		return "", err
	}
	switch t.autoscaler {
	case "hpa":
		return "/apis/autoscaling/v2/namespaces/" + ns + "/horizontalpodautoscalers/" + t.autoscalerName, nil
	case "scaledobject":
		return "/apis/keda.sh/v1alpha1/namespaces/" + ns + "/scaledobjects/" + t.autoscalerName, nil
	}
	return "", nil
}

// pyInt is int(v) of a JSON value.
func pyInt(v any) (int64, error) {
	switch x := v.(type) {
	case int64:
		return x, nil
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return 0, fmt.Errorf("cannot convert float %v to integer", x)
		}
		return int64(x), nil
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(strings.ReplaceAll(x, "_", "")), 10, 64)
		if err != nil {
			return 0, fmt.Errorf("invalid literal for int() with base 10: %s", pyjson.Repr(x))
		}
		return n, nil
	}
	return 0, fmt.Errorf("int() argument must be a string, a bytes-like object or a real number, not '%s'", pyType(v))
}

func pyType(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case pyjson.Object:
		return "dict"
	case []any:
		return "list"
	}
	return "object"
}

// bounds is kube_scaling._bounds.
func bounds(t target, obj any) (int64, int64, error) {
	o, isDict := obj.(pyjson.Object)
	if !isDict {
		return 0, 0, fmt.Errorf("'%s' object has no attribute 'get'", pyType(obj))
	}
	spec := kube.Dict(o.Value("spec"))
	get := func(key string, def int64) (int64, error) {
		v, ok := spec.Get(key)
		if !ok {
			return def, nil
		}
		return pyInt(v)
	}
	minKey, maxKey, minDef, maxDef := "minReplicaCount", "maxReplicaCount", int64(0), int64(100) // KEDA's defaults
	if t.autoscaler == "hpa" {
		minKey, maxKey, minDef, maxDef = "minReplicas", "maxReplicas", 1, 1
	}
	lo, err := get(minKey, minDef)
	if err != nil {
		return 0, 0, err
	}
	hi, err := get(maxKey, maxDef)
	return lo, hi, err
}

// patch is kube_scaling._patch.
func patch(t target, lo, hi int64) pyjson.Object {
	if t.autoscaler == "hpa" {
		return pyjson.Object{{Key: "spec", Value: pyjson.Object{{Key: "minReplicas", Value: lo}, {Key: "maxReplicas", Value: hi}}}}
	}
	return pyjson.Object{{Key: "spec", Value: pyjson.Object{{Key: "minReplicaCount", Value: lo}, {Key: "maxReplicaCount", Value: hi}}}}
}

func (s *scaling) kubeAPI() scriptcfg.Kubernetes {
	k := s.api
	k.TimeoutSeconds = s.panel.KubernetesTimeout()
	return k
}

// oneRound is one_round: the bounds put back where the cluster has others; how many.
// Skipped when someone is saving them (they apply them).
func (s *scaling) oneRound(ctx context.Context) (int64, error) {
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(lockKey)).Scan(&got); err != nil {
		return 0, err
	}
	if !got {
		return 0, nil
	}
	type row struct {
		kind   string
		lo, hi int64
	}
	rows, err := tx.Query(ctx, `SELECT kind, min_replicas, max_replicas FROM worker_scaling`)
	if err != nil {
		return 0, err
	}
	var list []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.kind, &r.lo, &r.hi); err != nil {
			rows.Close()
			return 0, err
		}
		list = append(list, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	var fixed int64
	k := s.kubeAPI()
	for _, r := range list {
		t, err := s.target(r.kind)
		if err != nil {
			return 0, err
		}
		if t.autoscaler == "" {
			continue
		}
		// a ceiling lowered in the environment since: it wins
		hi := min(r.hi, t.ceiling)
		lo := min(r.lo, hi)
		path, err := s.autoscalerPath(t)
		if err != nil {
			return 0, err
		}
		obj, err := kube.ScalingCall(ctx, k, http.MethodGet, path, nil)
		if err != nil {
			return 0, err
		}
		curLo, curHi, err := bounds(t, obj)
		if err != nil {
			return 0, err
		}
		if curLo != lo || curHi != hi {
			if _, err := kube.ScalingCall(ctx, k, http.MethodPatch, path, patch(t, lo, hi)); err != nil {
				return 0, err
			}
			fixed++
			s.log.Info(fmt.Sprintf("workers: the %s workers' autoscaler had other bounds, put back to %d–%d", r.kind, lo, hi))
		}
	}
	return fixed, tx.Commit(ctx) // releases the lock
}

// loop is reapply_loop.
func (s *scaling) loop(ctx context.Context) {
	if !s.enabled {
		return
	}
	for ctx.Err() == nil {
		if _, err := s.oneRound(ctx); err != nil && !errors.Is(err, context.Canceled) {
			// API server down or restarting: at the next round
			s.log.Warn(fmt.Sprintf("workers: autoscaling bounds not checked (%v), again in %ss", err, "60.0"))
		}
		if !jobs.Sleep(ctx, reapplyEvery) {
			break
		}
	}
}
