package api

import "testing"

// The answers of pydantic's TypeAdapter(int) on these strings (pydantic 2.13).
func TestPydanticInt(t *testing.T) {
	good := map[string]int64{"5": 5, "+5": 5, "-5": -5, "05": 5, " 5": 5, "5 ": 5, "5.0": 5, "5.00": 5,
		"1_000": 1000, "\t5\n": 5, "-0": 0, "0.0": 0, "00": 0, "5 ": 5, "1_0.0": 10, " +5 ": 5}
	for s, want := range good {
		if got, ok := pydanticInt(s); !ok || got != want {
			t.Errorf("%q: got %d %v, want %d", s, got, ok, want)
		}
	}
	for _, s := range []string{"5.5", "abc", "", "0x10", "٣", "5e2", "5.", "_5", "5_", "1__0", "+-5", "5.000001", ".0", "--5", "-+5"} {
		if got, ok := pydanticInt(s); ok {
			t.Errorf("%q: got %d, want a refusal", s, got)
		}
	}
}
