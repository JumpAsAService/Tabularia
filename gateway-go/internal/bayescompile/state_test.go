package bayescompile

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

var (
	t0       = time.Date(2026, 11, 23, 14, 2, 0, 0, time.UTC)
	defaults = Limits{CPU: "1", Memory: "3Gi", MemoryBytes: 3 << 30, DeadlineSeconds: 300, Tmp: "512Mi",
		TmpBytes: 512 << 20, Slots: 2, CacheMaxBytes: 2 << 30}
)

func queuedRow(id, person, model, version int64, at time.Time) Row {
	return Row{ID: id, CacheKey: strings.Repeat("k", 63) + string(rune('0'+id%10)), ProgramSHA256: progA,
		ImageDigest: imgB.Digest, State: StateQueued, RequestedBy: person, ModelID: model, Version: version, QueuedAt: at}
}

func running(t *testing.T, r Row, l Limits) Row {
	t.Helper()
	r, err := Start(r, l, t0)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

// Every transition of the states' table, and none other.
func TestTransitionsAreTheTable(t *testing.T) {
	states := []string{StateNone, StateQueued, StateRunning, StateReady, StateFailed, StateTimeout}
	want := map[string]bool{
		"none→queued": true, "queued→running": true, "running→ready": true, "running→failed": true,
		"running→timeout": true, "running→queued": true, "ready→none": true, "failed→queued": true,
		"timeout→queued": true, "queued→none": true,
	}
	for _, from := range states {
		for _, to := range states {
			if got := Allowed(from, to); got != want[from+"→"+to] {
				t.Errorf("%s → %s: %v", from, to, got)
			}
		}
	}
	// a state the table does not know
	if Allowed("ready", "compiled") || Allowed("", StateQueued) {
		t.Error("an unknown state moved")
	}
}

// The functions that move a row refuse a row in the wrong state and leave it as it was.
func TestMovesRefuseTheWrongState(t *testing.T) {
	ready := Row{ID: 1, State: StateReady, ProgramSHA256: progA}
	if _, err := Start(ready, defaults, t0); !errors.Is(err, ErrTransition) {
		t.Errorf("start a ready row: %v", err)
	}
	q := queuedRow(2, 1, 1, 1, t0)
	if _, err := Failed(q, ReasonCompileFailed, t0); !errors.Is(err, ErrTransition) {
		t.Errorf("fail a queued row: %v", err)
	}
	if _, err := ClusterFailed(q, "Evicted", t0); !errors.Is(err, ErrTransition) {
		t.Errorf("cluster-fail a queued row: %v", err)
	}
	q.DeadlineSeconds, q.MemoryBytes = 300, 3<<30
	if _, err := TimedOut(q, ReasonDeadline, t0); !errors.Is(err, ErrTransition) {
		t.Errorf("time out a queued row: %v", err)
	}
	if _, err := Kept(q, Message{ProgramSHA256: progA}, 3<<20, 12, "[]", t0); !errors.Is(err, ErrTransition) {
		t.Errorf("keep a queued row: %v", err)
	}
	if _, err := Used(q, t0); !errors.Is(err, ErrTransition) {
		t.Errorf("use a queued row: %v", err)
	}
}

// The owner's rule: a deadline passed is not tried again under the same or a lower deadline; a compile killed for
// memory is not tried again under the same or a lower memory; each limit is its own.
func TestMayRetry(t *testing.T) {
	dl := Row{State: StateTimeout, Reason: ReasonDeadline, DeadlineSeconds: 300, MemoryBytes: 3 << 30}
	mem := Row{State: StateTimeout, Reason: ReasonMemory, DeadlineSeconds: 300, MemoryBytes: 3 << 30}
	with := func(f func(*Limits)) Limits { l := defaults; f(&l); return l }
	cases := []struct {
		name string
		row  Row
		now  Limits
		want bool
	}{
		{"deadline equal", dl, defaults, false},
		{"deadline lowered", dl, with(func(l *Limits) { l.DeadlineSeconds = 200 }), false},
		{"deadline raised", dl, with(func(l *Limits) { l.DeadlineSeconds = 301 }), true},
		{"deadline: memory raised is not enough", dl, with(func(l *Limits) { l.MemoryBytes = 4 << 30 }), false},
		{"memory equal", mem, defaults, false},
		{"memory lowered", mem, with(func(l *Limits) { l.MemoryBytes = 2 << 30 }), false},
		{"memory raised", mem, with(func(l *Limits) { l.MemoryBytes = 4 << 30 }), true},
		{"memory: deadline raised is not enough", mem, with(func(l *Limits) { l.DeadlineSeconds = 600 }), false},
		{"a timeout without a reason", Row{State: StateTimeout}, with(func(l *Limits) { l.DeadlineSeconds = 9999 }), false},
		{"not a timeout", Row{State: StateFailed, Reason: ReasonCompileFailed}, defaults, true},
	}
	for _, c := range cases {
		if got := MayRetry(c.row, c.now); got != c.want {
			t.Errorf("%s: %v", c.name, got)
		}
	}
}

// Start records the limits the compile runs under; a timeout compares with them even when the limits changed since.
func TestStartRecordsTheLimitsATimeoutKeeps(t *testing.T) {
	r := running(t, queuedRow(7, 1, 1, 1, t0), defaults)
	if r.State != StateRunning || r.JobName != "bayes-compile-7-0" || r.DeadlineSeconds != 300 || r.MemoryBytes != 3<<30 ||
		!r.StartedAt.Equal(t0) {
		t.Fatalf("%+v", r)
	}
	at := t0.Add(5 * time.Minute)
	r, err := TimedOut(r, ReasonDeadline, at)
	if err != nil || r.State != StateTimeout || r.Reason != ReasonDeadline || !r.FinishedAt.Equal(at) {
		t.Fatalf("%+v %v", r, err)
	}
	if !strings.Contains(r.Detail, "limit of 300 s (23 Nov)") || !strings.Contains(r.Detail, "not compiled again") {
		t.Errorf("detail %q", r.Detail)
	}
	// the administrator lowered the deadline meanwhile, then set it back: still not retried
	if MayRetry(r, defaults) {
		t.Error("retried at the same deadline")
	}
	m := running(t, queuedRow(8, 1, 1, 1, t0), defaults)
	m, err = TimedOut(m, ReasonMemory, at)
	if err != nil || m.Reason != ReasonMemory || !strings.Contains(m.Detail, "memory limit of 3 GiB") {
		t.Fatalf("%+v %v", m, err)
	}
	if _, err := TimedOut(running(t, queuedRow(9, 1, 1, 1, t0), defaults), ReasonCompileFailed, at); err == nil {
		t.Error("a timeout for a reason that is not a limit")
	}
	if _, err := TimedOut(Row{ID: 10, State: StateRunning}, ReasonDeadline, at); err == nil {
		t.Error("a timeout without the limits recorded")
	}
}

// A failure of the cluster goes back to the queue with its next attempt and its place, at most three runs in all,
// then failed with the cluster's reason.
func TestClusterFailuresThreeAttempts(t *testing.T) {
	r := queuedRow(4, 1, 1, 1, t0)
	for attempt := 0; attempt < MaxAttempts-1; attempt++ {
		r = running(t, r, defaults)
		if r.JobName != JobName(4, attempt) {
			t.Fatalf("job %s at attempt %d", r.JobName, attempt)
		}
		var err error
		if r, err = ClusterFailed(r, "Evicted", t0); err != nil {
			t.Fatal(err)
		}
		if r.State != StateQueued || r.Attempt != attempt+1 || r.JobName != "" || !r.QueuedAt.Equal(t0) {
			t.Fatalf("after attempt %d: %+v", attempt, r)
		}
	}
	r = running(t, r, defaults)
	if r.JobName != "bayes-compile-4-2" {
		t.Fatalf("third job %s", r.JobName)
	}
	r, err := ClusterFailed(r, "DisruptionTarget", t0)
	if err != nil || r.State != StateFailed || r.Reason != ReasonCluster || !strings.Contains(r.Detail, "DisruptionTarget") {
		t.Fatalf("%+v %v", r, err)
	}
	// failed by the cluster: a save or a run queues it again, from its first attempt
	p := PlanEnqueue(r, bayesmodels.CompileRequest{ModelID: 1, Version: 2, ProgramSHA256: progA, By: 3}, nil, defaults, t0.Add(time.Hour))
	if !p.Queue || p.Row.Attempt != 0 || p.Row.Reason != "" || p.Row.RequestedBy != 3 {
		t.Fatalf("%+v", p)
	}
}

func TestFailedAndKept(t *testing.T) {
	for _, reason := range []string{ReasonCompileFailed, ReasonProgramHash, ReasonCrossCheck, ReasonJob} {
		r, err := Failed(running(t, queuedRow(1, 1, 1, 1, t0), defaults), reason, t0)
		if err != nil || r.State != StateFailed || r.Reason != reason || !strings.Contains(r.Detail, "fault of Tabularia") {
			t.Errorf("%s: %+v %v", reason, r, err)
		}
	}
	for _, reason := range []string{ReasonCluster, ReasonDeadline, ReasonMemory, ReasonTooLarge, "other"} {
		if _, err := Failed(running(t, queuedRow(1, 1, 1, 1, t0), defaults), reason, t0); err == nil {
			t.Errorf("%s accepted as a compiler's reason", reason)
		}
	}
	r, err := TooLarge(running(t, queuedRow(1, 1, 1, 1, t0), defaults), 70<<20, 64<<20, t0)
	if err != nil || r.Reason != ReasonTooLarge || r.Detail != "The cache's limit (64 MiB) is smaller than this model's binary (70 MiB): an administrator can raise it." {
		t.Errorf("%+v %v", r, err)
	}
	m := Message{ProgramSHA256: progA, BinarySHA256: strings.Repeat("e", 64), InfoSHA256: strings.Repeat("f", 64)}
	at := t0.Add(12 * time.Second)
	k, err := Kept(running(t, queuedRow(1, 1, 1, 1, t0), defaults), m, 2_900_000, 12.4, `["w"]`, at)
	if err != nil || k.State != StateReady || k.BinarySHA256 != m.BinarySHA256 || k.InfoSHA256 != m.InfoSHA256 ||
		k.BinaryBytes != 2_900_000 || !k.LastUsedAt.Equal(at) || !k.FinishedAt.Equal(at) {
		t.Fatalf("%+v %v", k, err)
	}
	other := m
	other.ProgramSHA256 = progB
	if _, err := Kept(running(t, queuedRow(1, 1, 1, 1, t0), defaults), other, 1, 1, "", at); !errors.Is(err, ErrMessage) {
		t.Errorf("kept another program's binary: %v", err)
	}
	later := at.Add(time.Hour)
	if u, err := Used(k, later); err != nil || !u.LastUsedAt.Equal(later) {
		t.Errorf("%+v %v", u, err)
	}
}

// What a run does with the state: compile first, wait, or fail at once.
func TestRefused(t *testing.T) {
	raised := defaults
	raised.DeadlineSeconds = 600
	dl := &Row{State: StateTimeout, Reason: ReasonDeadline, DeadlineSeconds: 300, MemoryBytes: 3 << 30}
	cases := []struct {
		name string
		row  *Row
		now  Limits
		want bool
	}{
		{"none", nil, defaults, false},
		{"queued", &Row{State: StateQueued}, defaults, false},
		{"running", &Row{State: StateRunning}, defaults, false},
		{"ready", &Row{State: StateReady}, defaults, false},
		{"timeout, same limit", dl, defaults, true},
		{"timeout, raised limit", dl, raised, false},
		{"failed by the cluster", &Row{State: StateFailed, Reason: ReasonCluster}, defaults, false},
		{"failed by the compiler", &Row{State: StateFailed, Reason: ReasonCompileFailed}, defaults, true},
		{"failed: cross-check", &Row{State: StateFailed, Reason: ReasonCrossCheck}, defaults, true},
		{"failed: too large", &Row{State: StateFailed, Reason: ReasonTooLarge}, defaults, true},
	}
	for _, c := range cases {
		if got := Refused(c.row, c.now); got != c.want {
			t.Errorf("%s: %v", c.name, got)
		}
	}
}

func TestCleanableAndStale(t *testing.T) {
	cur, old := imgB.Digest, "sha256:"+strings.Repeat("0", 64)
	for _, c := range []struct {
		row     Row
		held    bool
		clean   bool
		stale   bool
		comment string
	}{
		{Row{State: StateTimeout, ImageDigest: old}, false, true, false, "old image, program gone"},
		{Row{State: StateFailed, ImageDigest: old}, false, true, false, "old image, program gone"},
		{Row{State: StateTimeout, ImageDigest: old}, true, false, false, "a version still holds it"},
		{Row{State: StateTimeout, ImageDigest: cur}, false, false, false, "the current image"},
		{Row{State: StateReady, ImageDigest: old}, false, false, false, "ready: the cache's"},
		{Row{State: StateQueued, ImageDigest: old}, false, false, true, "queued under a rebuilt image"},
		{Row{State: StateQueued, ImageDigest: cur}, false, false, false, "queued"},
	} {
		if Cleanable(c.row, cur, c.held) != c.clean || Stale(c.row, cur) != c.stale {
			t.Errorf("%s: %+v", c.comment, c.row)
		}
	}
}

func TestStateOf(t *testing.T) {
	if s := StateOf(nil, false, 2); s.State != bayesmodels.CompileNone || s.Detail != "Compiled the first time it runs." {
		t.Errorf("%+v", s)
	}
	q := queuedRow(1, 1, 1, 1, t0)
	if s := StateOf(&q, false, 2); s.Detail != "Waiting for a free place to compile (2 at once)." || !s.At.Equal(t0) {
		t.Errorf("%+v", s)
	}
	if s := StateOf(&q, true, 2); s.Detail != "Waiting to compile: another compile of yours is running." {
		t.Errorf("%+v", s)
	}
	r := running(t, q, defaults)
	if s := StateOf(&r, false, 2); s.State != StateRunning || s.Detail != "Compiling since 14:02 (usually 10–30 s)." {
		t.Errorf("%+v", s)
	}
	k, _ := Kept(r, Message{ProgramSHA256: progA, BinarySHA256: progB, InfoSHA256: progB}, 1, 12, "", t0)
	if s := StateOf(&k, false, 2); s.Detail != "Compiled on 23 Nov at 14:02 in 12 s." {
		t.Errorf("%+v", s)
	}
	to, _ := TimedOut(running(t, q, defaults), ReasonDeadline, t0)
	if s := StateOf(&to, false, 2); s.State != bayesmodels.CompileTimeout || s.Detail != to.Detail || !s.At.Equal(t0) {
		t.Errorf("%+v", s)
	}
}

func TestResolve(t *testing.T) {
	recipe := &modelgroups.CompileLimits{CPU: "1", Memory: "3Gi", Seconds: 300, Tmp: "512Mi"}
	cache := &modelgroups.CacheLimits{MaxBytes: 2 << 30}
	l, err := Resolve(recipe, cache, Settings{})
	if err != nil || l != defaults {
		t.Fatalf("%+v %v", l, err)
	}
	mem, secs, slots, max := "4Gi", int64(600), 3, int64(1<<30)
	l, err = Resolve(recipe, cache, Settings{CompileMemory: &mem, CompileSeconds: &secs, CompileSlots: &slots, CacheMaxBytes: &max})
	if err != nil || l.MemoryBytes != 4<<30 || l.DeadlineSeconds != 600 || l.Slots != 3 || l.CacheMaxBytes != 1<<30 || l.CPU != "1" {
		t.Fatalf("%+v %v", l, err)
	}
	bad, zero := "3G", 0
	if _, err := Resolve(recipe, cache, Settings{CompileMemory: &bad}); err == nil {
		t.Error("3G accepted")
	}
	if _, err := Resolve(recipe, cache, Settings{CompileSlots: &zero}); err == nil {
		t.Error("0 compiles at once accepted")
	}
	if _, err := Resolve(nil, cache, Settings{}); err == nil {
		t.Error("no recipe defaults accepted")
	}
}

func TestBytesAndNames(t *testing.T) {
	for n, want := range map[int64]string{2 << 30: "2 GiB", 512 << 20: "512 MiB", 3_040_000: "2.9 MiB", 900: "900 bytes"} {
		if got := Bytes(n); got != want {
			t.Errorf("%d: %s", n, got)
		}
	}
	if StepPrefix(12, 1) != "bayes-compile/12-1/" || JobName(12, 1) != "bayes-compile-12-1" {
		t.Error(StepPrefix(12, 1), JobName(12, 1))
	}
	if n := JobName(1<<62, MaxAttempts); len(n) > 63 {
		t.Errorf("%s is %d characters", n, len(n))
	}
}
