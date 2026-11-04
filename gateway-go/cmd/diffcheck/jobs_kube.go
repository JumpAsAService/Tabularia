package main

import (
	"maps"

	"github.com/jackc/pgx/v5/pgxpool"
)

// What the suites of the Kubernetes loops (jobs_scaling, jobs_script_builds, jobs_notebooks,
// jobs_settings_rollout) share: the fake engine standing in for the Kubernetes API, and an
// advisory lock held by «another process» on both databases.

// kubeEnv is an installation on Kubernetes whose API is the stack's fake engine, with
// `more` on top.
func kubeEnv(more map[string]string) map[string]string {
	env := map[string]string{"KUBERNETES__API_URL": "http://{engine}", "KUBERNETES__TOKEN_FILE": "/etc/hostname"}
	maps.Copy(env, more)
	return env
}

// heldLocks are the connections holding an advisory lock (holdLock), on both databases.
var heldLocks []*pgxpool.Conn

// holdLock takes the session advisory lock `key` on both databases, on connections kept
// aside: a round asking for it finds it taken, as when another process holds it.
func holdLock(w *World, key int64) {
	for _, s := range []*side{w.a, w.b} {
		c, err := s.db.Acquire(w.ctx)
		if err != nil {
			fail("%s: %v", s.name, err)
		}
		if _, err := c.Exec(w.ctx, `SELECT pg_advisory_lock($1)`, key); err != nil {
			fail("%s: %v", s.name, err)
		}
		heldLocks = append(heldLocks, c)
	}
}

// releaseLocks lets the locks of holdLock go.
func releaseLocks(w *World) {
	for _, c := range heldLocks {
		_, _ = c.Exec(w.ctx, `SELECT pg_advisory_unlock_all()`)
		c.Release()
	}
	heldLocks = nil
}
