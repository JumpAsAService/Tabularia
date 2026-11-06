package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// services/settings_rollout.py rollout_loop: on Kubernetes, the pods of the components that
// read a changed setting only at start restarted one at a time, when an administrator asks
// (Administration → Settings, «Roll out»). It evicts the oldest pod started before the
// request, waits until the component's pods are all ready again, evicts the next, component
// after component, the orchestrator last. It returns at once off Kubernetes or without the
// chart's settingsRollout. The pods are read with the routes' helpers (settings_rollout.go).
//
// One step at a time across the processes: the transaction's advisory lock 7402121, asked
// once, as the Python loop asks it.

const (
	rolloutStepEvery = 5 * time.Second // STEP_EVERY
	rolloutLock      = 7_402_121       // LOCK
	rolloutMaxOutput = 15 * 60         // MAX_OUTPUT: a pod on its way out longer than this no longer holds the rollout
)

func init() {
	RegisterJob("settings_rollout_loop", func(d *Deps) jobs.Job {
		return jobs.Job{Order: 12, Loop: func(ctx context.Context) { rolloutLoop(ctx, d) }, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_step: what it did ("evicted <component>: <pod>", …)
			"settings_rollout": func(ctx context.Context) (any, error) {
				if rolloutMode() != "kubernetes" {
					return nil, nil
				}
				did, err := rolloutOneStep(ctx, d)
				if did == "" {
					return nil, err
				}
				return did, err
			},
		}}
	})
}

// rolloutLeaving is _leaving: on its way out, and not for so long that its node must be gone.
func rolloutLeaving(pod any) bool {
	raw, _ := podMeta(pod).Get("deletionTimestamp")
	if !pyjson.Truthy(raw) {
		return false
	}
	since, ok := k8sTime(raw)
	if !ok {
		return true
	}
	return jobs.Now().Sub(since).Seconds() < rolloutMaxOutput
}

// rolloutEvict is evict: true if evicted (or already gone), false if a PodDisruptionBudget
// says not now.
func rolloutEvict(ctx context.Context, pod string) (bool, error) {
	ns := scriptcfg.GetRuntime().ReleaseNamespace()
	_, err := kube.Call(ctx, kubeAPI(), http.MethodPost, "/api/v1/namespaces/"+ns+"/pods/"+pod+"/eviction", pyjson.Object{
		{Key: "apiVersion", Value: "policy/v1"}, {Key: "kind", Value: "Eviction"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: pod}, {Key: "namespace", Value: ns}}}}, false)
	switch kube.Status(err) {
	case 404:
		return true, nil
	case 429:
		return false, nil
	}
	return err == nil, err
}

// rolloutOneStep is one_step: evicts one pod, or closes a finished request, or waits; what
// it did ("" when nothing).
func rolloutOneStep(ctx context.Context, d *Deps) (string, error) {
	me := os.Getenv("HOSTNAME")
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(rolloutLock)).Scan(&got); err != nil {
		return "", err
	}
	if !got {
		return "", nil
	}
	requested := map[string]time.Time{}
	rows, err := tx.Query(ctx, `SELECT component, requested_at FROM settings_rollouts WHERE requested_at IS NOT NULL`)
	if err != nil {
		return "", err
	}
	for rows.Next() {
		var c string
		var at time.Time
		if err := rows.Scan(&c, &at); err != nil {
			rows.Close()
			return "", err
		}
		requested[c] = at
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return "", err
	}
	for _, c := range rolloutOrder {
		at, ok := requested[c]
		if !ok {
			continue
		}
		all, err := rolloutPods(ctx, c)
		if err != nil {
			return "", err
		}
		var pl []any
		for _, p := range all {
			if !podLeaving(p) || rolloutLeaving(p) {
				pl = append(pl, p)
			}
		}
		stale := rolloutOld(pl, &at)
		staleNames := map[string]bool{}
		for _, p := range stale {
			staleNames[podName(p)] = true
		}
		// a pod on its way out, or a new one not ready yet: wait. A new pod that never gets
		// ready stops the rollout there, with the old pods still serving
		var busy []string
		for _, p := range pl {
			isReady, err := podReady(p)
			if err != nil {
				return "", err
			}
			if !isReady && (podLeaving(p) || !staleNames[podName(p)]) {
				busy = append(busy, podName(p))
			}
		}
		if len(busy) > 0 {
			return "waiting " + c + ": " + strings.Join(busy, ", "), tx.Commit(ctx)
		}
		if len(stale) == 0 {
			// closed only if nobody asked again meanwhile (a new request, not under the lock)
			if _, err := tx.Exec(ctx, `UPDATE settings_rollouts SET requested_at = NULL WHERE component = $1 AND requested_at = $2`,
				c, at); err != nil {
				return "", err
			}
			if err := tx.Commit(ctx); err != nil {
				return "", err
			}
			d.Log.Info("settings: rollout of " + c + " done")
			return "done " + c, nil
		}
		// an old pod that is not ready first (it serves nothing), itself last: the next
		// orchestrator closes the request
		var target any
		for _, p := range stale {
			isReady, err := podReady(p)
			if err != nil {
				return "", err
			}
			if !isReady {
				target = p
				break
			}
		}
		if target == nil {
			target = stale[0]
			for _, p := range stale {
				if podName(p) != me {
					target = p
					break
				}
			}
		}
		pod := podName(target)
		evicted, err := rolloutEvict(ctx, pod)
		if err != nil {
			return "", err
		}
		if !evicted {
			return "blocked " + c + ": " + pod, tx.Commit(ctx)
		}
		d.Log.Info(fmt.Sprintf("settings: rollout of %s, pod %s evicted (%d left)", c, pod, len(stale)-1))
		return "evicted " + c + ": " + pod, tx.Commit(ctx)
	}
	return "", tx.Commit(ctx)
}

// rolloutLoop is rollout_loop.
func rolloutLoop(ctx context.Context, d *Deps) {
	if rolloutMode() != "kubernetes" {
		return
	}
	for ctx.Err() == nil {
		if _, err := rolloutOneStep(ctx, d); err != nil && !errors.Is(err, context.Canceled) {
			// the API or the database down: at the next step
			d.Log.Warn(fmt.Sprintf("settings: rollout step failed (%v), again in %ss", err, "5.0"))
		}
		if !jobs.Sleep(ctx, rolloutStepEvery) {
			break
		}
	}
}
