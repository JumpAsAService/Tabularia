package bayescompile

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

func req(by, model, version int64, again bool) bayesmodels.CompileRequest {
	return bayesmodels.CompileRequest{ModelID: model, Version: version, ProgramSHA256: progA, By: by, Again: again}
}

func ids(rows []Row) []int64 {
	var out []int64
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}

func TestNewRow(t *testing.T) {
	r, err := NewRow(progA, Image{CmdStan: "2.40.0", Digest: imgB.Digest}, 4)
	key, _ := CacheKey(progA, imgB)
	if err != nil || r.CacheKey != key || r.State != StateNone || r.Arch != "amd64" || r.GroupVersion != 4 || r.ID != 0 {
		t.Fatalf("%+v %v", r, err)
	}
	if _, err := NewRow("x", imgB, 1); err == nil {
		t.Error("a row for a foreign program")
	}
}

// «The queue», steps 1–3, for every state of the key's row, with and without «Compile now».
func TestPlanEnqueueByState(t *testing.T) {
	fresh, _ := NewRow(progA, imgB, 1)
	at := t0.Add(time.Minute)
	raised := defaults
	raised.DeadlineSeconds = 600
	moreMemory := defaults
	moreMemory.MemoryBytes = 4 << 30
	timeout := func(reason string) Row {
		return Row{ID: 5, CacheKey: fresh.CacheKey, State: StateTimeout, Reason: reason, DeadlineSeconds: 300,
			MemoryBytes: 3 << 30, Detail: "x", FinishedAt: t0}
	}
	failed := func(reason string) Row {
		return Row{ID: 5, CacheKey: fresh.CacheKey, State: StateFailed, Reason: reason, Detail: "x", FinishedAt: t0, Attempt: 2}
	}
	// a binary larger than the cache: its size recorded (the owner, correction of 2026-11-24 from C1)
	tooLarge := func(size int64) Row {
		r := failed(ReasonTooLarge)
		r.BinaryBytes = size
		return r
	}
	bigger := func(limit int64) Limits {
		l := defaults
		l.CacheMaxBytes = limit
		return l
	}
	cases := []struct {
		name  string
		row   Row
		again bool
		now   Limits
		queue bool
	}{
		{"none", fresh, false, defaults, true},
		{"ready", Row{ID: 5, State: StateReady}, false, defaults, false},
		{"ready, compile now", Row{ID: 5, State: StateReady}, true, defaults, false},
		{"queued", Row{ID: 5, State: StateQueued}, true, defaults, false},
		{"running", Row{ID: 5, State: StateRunning}, true, defaults, false},
		{"timeout by deadline, same limits", timeout(ReasonDeadline), false, defaults, false},
		{"timeout by deadline, compile now", timeout(ReasonDeadline), true, defaults, false},
		{"timeout by deadline, more memory only", timeout(ReasonDeadline), true, moreMemory, false},
		{"timeout by deadline, deadline raised", timeout(ReasonDeadline), false, raised, true},
		{"timeout by memory, same limits, compile now", timeout(ReasonMemory), true, defaults, false},
		{"timeout by memory, deadline raised only", timeout(ReasonMemory), true, raised, false},
		{"timeout by memory, memory raised", timeout(ReasonMemory), false, moreMemory, true},
		{"failed by the compiler, a save", failed(ReasonCompileFailed), false, defaults, false},
		{"failed by the compiler, compile now", failed(ReasonCompileFailed), true, defaults, true},
		{"failed cross-check, compile now", failed(ReasonCrossCheck), true, defaults, true},
		{"failed too large, a run", failed(ReasonTooLarge), false, defaults, false},
		{"failed too large, same cache, compile now", tooLarge(3 << 30), true, defaults, false},
		{"failed too large, cache raised but still smaller", tooLarge(3 << 30), true, bigger(3<<30 - 1), false},
		{"failed too large, cache raised to its size, a save", tooLarge(3 << 30), false, bigger(3 << 30), true},
		{"failed too large, cache raised, a run", tooLarge(3 << 30), false, bigger(4 << 30), true},
		{"failed job, compile now", failed(ReasonJob), true, defaults, true},
		{"failed by the cluster, a save", failed(ReasonCluster), false, defaults, true},
	}
	for _, c := range cases {
		p := PlanEnqueue(c.row, req(9, 3, 2, c.again), nil, c.now, at)
		if p.Queue != c.queue {
			t.Errorf("%s: queue %v", c.name, p.Queue)
			continue
		}
		if !c.queue {
			if !reflect.DeepEqual(p.Row, c.row) {
				t.Errorf("%s: the row changed: %+v", c.name, p.Row)
			}
			continue
		}
		r := p.Row
		if r.State != StateQueued || r.RequestedBy != 9 || r.ModelID != 3 || r.Version != 2 || r.Attempt != 0 ||
			!r.QueuedAt.Equal(at) || r.Reason != "" || r.Detail != "" || !r.FinishedAt.IsZero() || r.ID != c.row.ID ||
			r.CacheKey != c.row.CacheKey {
			t.Errorf("%s: %+v", c.name, r)
		}
	}
}

// Two versions of the same model saved one after the other by the same person: the older one, still queued, leaves
// the queue; a running one does not; another model of the same person stays; another person's stays.
func TestOvertaken(t *testing.T) {
	v1 := queuedRow(1, 7, 3, 1, t0)                  // person 7, model 3, version 1: queued
	v2run := queuedRow(2, 7, 3, 2, t0)               // version 2: running
	v2run.State = StateRunning                       //
	other := queuedRow(3, 7, 4, 1, t0)               // another model of person 7
	theirs := queuedRow(4, 8, 3, 1, t0)              // person 8 queued model 3's version 1
	newer := queuedRow(5, 7, 3, 9, t0)               // a newer version than the request's
	others := []Row{v1, v2run, other, theirs, newer} //
	fresh, _ := NewRow(progB, imgB, 1)               // the version 3 just saved: another program
	p := PlanEnqueue(fresh, bayesmodels.CompileRequest{ModelID: 3, Version: 3, ProgramSHA256: progB, By: 7}, others, defaults, t0)
	if !p.Queue || !reflect.DeepEqual(ids(p.Overtaken), []int64{1}) {
		t.Fatalf("overtaken %v, queue %v", ids(p.Overtaken), p.Queue)
	}
	// saved right after: v1 then v2 of the same model, v1 still queued behind another compile of the person
	first, _ := NewRow(progA, imgB, 1)
	p1 := PlanEnqueue(first, bayesmodels.CompileRequest{ModelID: 3, Version: 1, ProgramSHA256: progA, By: 7}, nil, defaults, t0)
	p1.Row.ID = 10
	second, _ := NewRow(progB, imgB, 1)
	p2 := PlanEnqueue(second, bayesmodels.CompileRequest{ModelID: 3, Version: 2, ProgramSHA256: progB, By: 7},
		[]Row{p1.Row}, defaults, t0.Add(time.Second))
	if !p2.Queue || !reflect.DeepEqual(ids(p2.Overtaken), []int64{10}) {
		t.Fatalf("the older save stayed: %v", ids(p2.Overtaken))
	}
	// the same program again (a layout-only save): its own row is never overtaken, and nothing is queued twice
	again := PlanEnqueue(p1.Row, bayesmodels.CompileRequest{ModelID: 3, Version: 2, ProgramSHA256: progA, By: 7},
		[]Row{p1.Row}, defaults, t0)
	if again.Queue || len(again.Overtaken) != 0 {
		t.Fatalf("%+v", again)
	}
	// even when the new version's program needs nothing (a timeout under the same limit), the older one leaves
	to := Row{ID: 11, CacheKey: second.CacheKey, State: StateTimeout, Reason: ReasonDeadline, DeadlineSeconds: 300, MemoryBytes: 3 << 30}
	p3 := PlanEnqueue(to, bayesmodels.CompileRequest{ModelID: 3, Version: 2, ProgramSHA256: progB, By: 7}, []Row{p1.Row}, defaults, t0)
	if p3.Queue || !reflect.DeepEqual(ids(p3.Overtaken), []int64{10}) {
		t.Fatalf("%+v", p3)
	}
	// whatever the new version's own row is (ready, queued, running, failed, too large), the older queued one leaves:
	// the store asks Enqueue after every save (the owner, correction of 2026-11-24 from C1)
	for _, st := range []Row{
		{ID: 12, CacheKey: second.CacheKey, State: StateReady, BinarySHA256: progB, BinaryBytes: 3 << 20},
		{ID: 12, CacheKey: second.CacheKey, State: StateQueued, RequestedBy: 8, ModelID: 5, Version: 1},
		{ID: 12, CacheKey: second.CacheKey, State: StateRunning, RequestedBy: 8, ModelID: 5, Version: 1},
		{ID: 12, CacheKey: second.CacheKey, State: StateFailed, Reason: ReasonCompileFailed},
		{ID: 12, CacheKey: second.CacheKey, State: StateFailed, Reason: ReasonTooLarge, BinaryBytes: 3 << 30},
	} {
		p := PlanEnqueue(st, bayesmodels.CompileRequest{ModelID: 3, Version: 2, ProgramSHA256: progB, By: 7}, []Row{p1.Row}, defaults, t0)
		if p.Queue || !reflect.DeepEqual(p.Row, st) || !reflect.DeepEqual(ids(p.Overtaken), []int64{10}) {
			t.Errorf("%s/%s: %+v", st.State, st.Reason, p)
		}
	}
	// no person, no model: nothing overtaken
	if o := Overtaken(fresh, bayesmodels.CompileRequest{ModelID: 3, Version: 3}, others); len(o) != 0 {
		t.Errorf("no person: %v", ids(o))
	}
	if o := Overtaken(fresh, bayesmodels.CompileRequest{Version: 3, By: 7}, others); len(o) != 0 {
		t.Errorf("no model: %v", ids(o))
	}
}

// The queue's order with three people and two places: a person never has two running, the order is queued_at.
func TestNextToStart(t *testing.T) {
	at := func(s int) time.Time { return t0.Add(time.Duration(s) * time.Second) }
	a1, a2 := queuedRow(1, 1, 10, 1, at(1)), queuedRow(2, 1, 11, 1, at(2)) // person 1: two models
	b1 := queuedRow(3, 2, 20, 1, at(3))                                    // person 2
	c1 := queuedRow(4, 3, 30, 1, at(4))                                    // person 3
	rows := []Row{c1, b1, a2, a1}
	if got := ids(NextToStart(rows, 2)); !reflect.DeepEqual(got, []int64{1, 3}) {
		t.Fatalf("round 1: %v", got)
	}
	// a1 and b1 run: no free place
	a1.State, b1.State = StateRunning, StateRunning
	rows = []Row{c1, b1, a2, a1}
	if got := NextToStart(rows, 2); len(got) != 0 {
		t.Fatalf("round 2: %v", ids(got))
	}
	// b1 ends: one place, a2 is older than c1 but person 1 has a1 running
	rows = []Row{c1, a2, a1}
	if got := ids(NextToStart(rows, 2)); !reflect.DeepEqual(got, []int64{4}) {
		t.Fatalf("round 3: %v", got)
	}
	// a1 ends: a2 starts
	rows = []Row{a2}
	if got := ids(NextToStart(rows, 2)); !reflect.DeepEqual(got, []int64{2}) {
		t.Fatalf("round 4: %v", got)
	}
	// equal times: by id; a running compile of an older image still holds its place
	x, y := queuedRow(8, 5, 1, 1, t0), queuedRow(7, 6, 1, 1, t0)
	old := queuedRow(9, 4, 1, 1, t0)
	old.State, old.ImageDigest = StateRunning, "sha256:"+strings.Repeat("0", 64)
	if got := ids(NextToStart([]Row{x, y, old}, 2)); !reflect.DeepEqual(got, []int64{7}) {
		t.Fatalf("ties: %v", got)
	}
	// more places than people: still one each
	fresh := []Row{queuedRow(1, 1, 10, 1, at(1)), queuedRow(2, 1, 11, 1, at(2)), queuedRow(3, 2, 20, 1, at(3))}
	if got := ids(NextToStart(fresh, 5)); !reflect.DeepEqual(got, []int64{1, 3}) {
		t.Fatalf("five places: %v", got)
	}
	// slots below the running count (the administrator lowered them): nothing starts
	if got := NextToStart([]Row{old, x}, 0); len(got) != 0 {
		t.Fatalf("no places: %v", ids(got))
	}
	// rows with no person are persons of their own (see TestNextToStartNoPersonIsAPersonOfItsOwn)
	n1, n2 := queuedRow(11, 0, 1, 1, at(1)), queuedRow(12, 0, 2, 1, at(2))
	if got := ids(NextToStart([]Row{n1, n2}, 2)); !reflect.DeepEqual(got, []int64{11, 12}) {
		t.Fatalf("no person: %v", got)
	}
}

// A row with no person (requested_by NULL: the person was deleted) is a person of its own: two of them
// run side by side, and neither holds back a person's compile.
func TestNextToStartNoPersonIsAPersonOfItsOwn(t *testing.T) {
	at := func(s int) time.Time { return t0.Add(time.Duration(s) * time.Second) }
	n1, n2 := queuedRow(1, 0, 10, 1, at(1)), queuedRow(2, 0, 11, 1, at(2))
	p := queuedRow(3, 5, 12, 1, at(3))
	if got := ids(NextToStart([]Row{p, n2, n1}, 3)); !reflect.DeepEqual(got, []int64{1, 2, 3}) {
		t.Fatalf("queued with no person: %v", got)
	}
	// one with no person running holds no one back: not another with no person, not a person
	run := queuedRow(4, 0, 13, 1, at(0))
	run.State = StateRunning
	if got := ids(NextToStart([]Row{run, n1, p}, 3)); !reflect.DeepEqual(got, []int64{1, 3}) {
		t.Fatalf("one with no person running: %v", got)
	}
}
