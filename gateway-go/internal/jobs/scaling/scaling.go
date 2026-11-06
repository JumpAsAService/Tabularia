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
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
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
		t, err := kube.TargetOf(s.cfg, r.kind)
		if err != nil {
			return 0, err
		}
		if t.Autoscaler == "" {
			continue
		}
		// a ceiling lowered in the environment since: it wins
		hi := min(r.hi, t.Ceiling)
		lo := min(r.lo, hi)
		_, path, err := kube.ScalingPaths(s.cfg, t)
		if err != nil {
			return 0, err
		}
		obj, err := kube.ScalingCall(ctx, k, http.MethodGet, path, nil)
		if err != nil {
			return 0, err
		}
		curLo, curHi, err := kube.Bounds(t, obj)
		if err != nil {
			return 0, err
		}
		if curLo != lo || curHi != hi {
			if _, err := kube.ScalingCall(ctx, k, http.MethodPatch, path, kube.Patch(t, lo, hi)); err != nil {
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
