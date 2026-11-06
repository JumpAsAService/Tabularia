package api

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func loads(t *testing.T, s string) any {
	t.Helper()
	v, err := pyjson.Loads(s)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestCPUMillis(t *testing.T) {
	for _, c := range []struct {
		in   any
		want float64
		ok   bool
	}{
		{"250m", 250, true}, {"1", 1000, true}, {"123456n", 0.123456, true}, {" 10u ", 0.01, true}, {"0.5", 500, true},
		{int64(2), 2000, true}, {0.25, 250, true}, {nil, 0, false}, {"", 0, false}, {true, 0, false}, {"1e3", 0, false},
		{"x", 0, false}, {"1.", 0, false},
	} {
		got, ok := cpuMillis(c.in)
		if ok != c.ok || (ok && got != c.want) {
			t.Errorf("cpu_millis(%#v) = %v %v, want %v %v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestMemoryBytes(t *testing.T) {
	for _, c := range []struct {
		in   any
		want any
	}{
		{"512Mi", int64(512 << 20)}, {"1.5Gi", int64(3 << 29)}, {"1000000", int64(1000000)}, {"1e3", int64(1000)}, {"2e2", int64(200)},
		{"3k", int64(3000)}, {"12Ti", int64(12 << 40)}, {int64(1024), int64(1024)}, {"x", nil}, {nil, nil}, {"1Pi", nil},
	} {
		got, err := memoryBytes(c.in)
		if err != nil || got != c.want {
			t.Errorf("memory_bytes(%#v) = %#v %v, want %#v", c.in, got, err, c.want)
		}
	}
	if _, err := memoryBytes("1e400"); err == nil {
		t.Error("1e400: an OverflowError expected")
	}
}

func TestCVInt(t *testing.T) {
	for _, c := range []struct {
		in   any
		want any
	}{
		{"12", int64(12)}, {" 12 ", int64(12)}, {"1_0", int64(10)}, {"-3", int64(-3)}, {1.9, int64(1)}, {true, int64(1)},
		{"abc", nil}, {"1.5", nil}, {nil, nil}, {pyjson.Object{}, nil}, {"_1", nil},
	} {
		got, err := cvInt(c.in)
		if err != nil || got != c.want {
			t.Errorf("_int(%#v) = %#v %v, want %#v", c.in, got, err, c.want)
		}
	}
}

func TestCVState(t *testing.T) {
	for _, c := range []struct {
		pod, state, ready string
		restarts          int64
	}{
		{`{"status": {"phase": "Running", "containerStatuses": [{"ready": true, "restartCount": 2, "state": {}}]}}`, `"Running"`, "1/1", 2},
		{`{"status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}`, `"CrashLoopBackOff"`, "0/1", 0},
		{`{"status": {"containerStatuses": [{"state": {"waiting": {"reason": "ContainerCreating"}}}]}}`, `"Unknown"`, "0/1", 0},
		{`{"status": {"containerStatuses": [{"restartCount": "3", "lastState": {"terminated": {"reason": "OOMKilled"}}}]}}`, `"OOMKilled"`, "0/1", 3},
		{`{"status": {"containerStatuses": [{"lastState": {"terminated": {"reason": "OOMKilled"}}}]}, "spec": {}}`, `"Unknown"`, "0/1", 0},
		{`{"status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False"}]}, "spec": {"containers": [{}, {}]}}`,
			`"Unschedulable"`, "0/2", 0},
		{`{"status": {"containerStatuses": [{"state": {"terminated": {"reason": "Error"}}}]}}`, `"Error"`, "0/1", 0},
		{`{"spec": {"containers": "abc"}}`, `"Unknown"`, "0/3", 0},
	} {
		state, restarts, ready, err := cvState(loads(t, c.pod))
		if err != nil || !pyjson.Equal(state, loads(t, c.state)) || ready != c.ready || restarts != c.restarts {
			t.Errorf("_state(%s) = %v %v %q %v", c.pod, state, restarts, ready, err)
		}
	}
	for _, bad := range []string{`{"status": [1]}`, `{"status": {"containerStatuses": [1]}}`, `{"status": {"containerStatuses": 5}}`,
		`{"status": {"containerStatuses": [{"restartCount": "x"}]}}`, `{"status": {"containerStatuses": [{"state": "x"}]}}`} {
		if _, _, _, err := cvState(loads(t, bad)); err == nil {
			t.Errorf("_state(%s): an error expected", bad)
		}
	}
}

func TestCVLimits(t *testing.T) {
	cpu, memory, err := cvLimits(loads(t, `{"spec": {"containers": [{"resources": {"limits": {"cpu": "500m", "memory": "1Ki"}}},
		{"resources": {}}, {"resources": {"limits": {"cpu": "1", "memory": "x"}}}]}}`))
	if err != nil || cpu != 1500.0 || memory != int64(1024) {
		t.Errorf("_limits = %v %v %v", cpu, memory, err)
	}
	cpu, memory, err = cvLimits(loads(t, `{}`))
	if err != nil || cpu != nil || memory != nil {
		t.Errorf("_limits of nothing = %v %v %v", cpu, memory, err)
	}
	if _, _, err := cvLimits(loads(t, `{"spec": {"containers": [{"resources": [1]}]}}`)); err == nil {
		t.Error("resources a list: an error expected")
	}
}

func TestCVSort(t *testing.T) {
	items := []any{"a", "b", "c", "d"}
	if err := cvSort(items, []any{"x", "", "x", "y"}, true); err != nil || !pyjson.Equal(items, []any{"d", "a", "c", "b"}) {
		t.Errorf("reverse, stable: %v %v", items, err)
	}
	items = []any{"a", "b"}
	if err := cvSort(items, []any{"x", int64(1)}, false); err == nil {
		t.Error("a text and a number: an error expected")
	}
	items = []any{"a"}
	if err := cvSort(items, []any{pyjson.Object{}}, false); err != nil {
		t.Error("one item: nothing compared")
	}
	items = []any{"a", "b", "c"}
	if err := cvSort(items, []any{int64(3), 1.5, false}, false); err != nil || !pyjson.Equal(items, []any{"c", "b", "a"}) {
		t.Errorf("numbers: %v %v", items, err)
	}
}
