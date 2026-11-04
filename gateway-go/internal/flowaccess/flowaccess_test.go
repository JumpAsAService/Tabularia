package flowaccess

import "testing"

func TestWidens(t *testing.T) {
	before := `{"nodes": [{"type": "source", "data": {"datasourceId": 3, "key": "datasets/a.parquet"}}]}`
	if n := len(Widens(before, before)); n != 0 {
		t.Fatalf("the same definition widens by %d", n)
	}
	after := `{"nodes": [{"type": "source", "data": {"datasourceId": "3", "key": "datasets/a.parquet"}},
		{"type": "output", "data": {"connectionId": 7, "flowId": true}}]}`
	got := Widens(before, after)
	if len(got) != 1 || got[0] != (Ref{"connection", "7"}) {
		t.Fatalf("widens: %v", got)
	}
	script := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "x"}]}}]}`
	changed := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "y"}]}}]}`
	if len(Widens(script, changed)) != 1 {
		t.Fatal("new code does not widen")
	}
}

func TestDigits(t *testing.T) {
	for _, c := range []struct {
		v  any
		ok bool
	}{{int64(4), true}, {"12", true}, {"-1", false}, {int64(-1), false}, {true, false}, {"1.0", false}, {"", false}, {1.0, false}} {
		if _, ok := Digits(c.v); ok != c.ok {
			t.Errorf("Digits(%#v) = %v", c.v, ok)
		}
	}
}
