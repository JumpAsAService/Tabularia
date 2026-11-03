package api

import "testing"

// pydantic's lax int, as probed in the gateway's image (pydantic 2.13)
func TestPyIntAsPydantic(t *testing.T) {
	good := map[string]int64{"1": 1, "1.0": 1, " 1": 1, "+1": 1, "-1": -1, "01": 1, "1_000": 1000, "1.00": 1, "-0": 0,
		"1_0.0": 10, " 1": 1, "99999999999999999999": NoSuchID, "3000000000": NoSuchID}
	for in, want := range good {
		if got, ok := pyInt(in); !ok || got != want {
			t.Errorf("%q: %d %v, want %d", in, got, ok, want)
		}
	}
	for _, in := range []string{"abc", "1.5", "1e3", "", "0x10", "1.", ".5", "1__0", "_1", "1_", "+-1", "1.0_0", "-", "1.0.0"} {
		if _, ok := pyInt(in); ok {
			t.Errorf("%q should not parse", in)
		}
	}
}
