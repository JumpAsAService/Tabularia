// Package jobs is the background work of app/main.py start_background_work: the loops a
// process runs when its APP__ROLE is «all» or «orchestrator». Each loop is a Job; its
// rounds (Ticks) can also be run one at a time, at a fixed moment (cmd/jobsonce), which is
// how the comparison proves a loop does what the Python one does.
//
// Several processes may run the same loops together (Python ones and Go ones too, while
// both exist): every loop lines up through the database exactly as the Python one does,
// with the same advisory-lock keys and the same conditional updates.
package jobs

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// Job is one loop of start_background_work.
type Job struct {
	Name string
	// Order is its place in start_background_work's list
	Order int
	// Loop runs it as the Python task does, until ctx ends (or returns at once, as the
	// Python loops that have nothing to do on this installation)
	Loop func(ctx context.Context)
	// Ticks are its rounds by name, run once by cmd/jobsonce: what the Python one-round
	// functions do (gateway-go/cmd/diffcheck/ticks/ticks.py names the same ones)
	Ticks map[string]Tick
}

// Tick is one round; its result (JSON-able, nil when the Python one returns nothing) is
// compared with the Python one's.
type Tick func(ctx context.Context) (any, error)

// ── the clock ────────────────────────────────────────────────────────────────

var frozen atomic.Pointer[time.Time]

// Now is datetime.now(timezone.utc). cmd/jobsonce freezes it, as the comparison's Python
// side freezes datetime.now: a round then sees the same moment on both sides.
func Now() time.Time {
	if t := frozen.Load(); t != nil {
		return *t
	}
	return time.Now().UTC()
}

// NaiveNow is datetime.now(timezone.utc).replace(tzinfo=None): the TIMESTAMP columns are
// naive UTC (pgx writes a time.Time in UTC to a timestamp without time zone).
func NaiveNow() time.Time { return Now() }

// Freeze fixes the clock (nil thaws it).
func Freeze(t *time.Time) {
	if t == nil {
		frozen.Store(nil)
		return
	}
	u := t.UTC()
	frozen.Store(&u)
}

// ── this process ─────────────────────────────────────────────────────────────

// Instance is orchestrator.INSTANCE: «host:pid:8 hex», this process's signature on the
// orchestrations it runs; it changes at every start. GATEWAY_GO__INSTANCE fixes it (the
// comparison gives both sides the same one).
var Instance = func() string {
	if v := os.Getenv("GATEWAY_GO__INSTANCE"); v != "" {
		return v
	}
	host, _ := os.Hostname()
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%s:%d:%s", host, os.Getpid(), hex.EncodeToString(b))
}()

var draining atomic.Bool

// Drain is orchestrator.drain: the process got its stop signal, it takes no more runs from
// the queue (the ones running carry on until the loops stop).
func Drain() { draining.Store(true) }

// Draining says whether Drain was called.
func Draining() bool { return draining.Load() }

// ResetDrain is queue_loop's `_draining = False` when it starts.
func ResetDrain() { draining.Store(false) }

// ── running them ─────────────────────────────────────────────────────────────

// Sorted is the jobs in start_background_work's order.
func Sorted(js []Job) []Job {
	out := append([]Job(nil), js...)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Order < out[j].Order })
	return out
}

// Start runs every job in a goroutine of its own; the returned function waits for all of
// them to return (after ctx ends), as the lifespan awaits its cancelled tasks.
func Start(ctx context.Context, js []Job) (wait func()) {
	var wg sync.WaitGroup
	for _, j := range Sorted(js) {
		if j.Loop == nil {
			continue
		}
		wg.Add(1)
		go func(j Job) {
			defer wg.Done()
			j.Loop(ctx)
		}(j)
	}
	return wg.Wait
}

// Sleep is `await asyncio.wait_for(stop.wait(), timeout=seconds)`: false when ctx ended
// (the stop event set) before the time was up.
func Sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// Seconds turns a number of seconds (a setting) into a Duration.
func Seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }
