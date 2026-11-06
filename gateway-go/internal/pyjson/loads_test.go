package pyjson

import "testing"

// json.loads then json.dumps gives back what json.dumps wrote
func TestLoadsDumpsRoundTrip(t *testing.T) {
	for _, text := range []string{
		`{"v": 1, "a": [1, 2.5, -0.0, 1e+16, 1e-05, 123456789012345678901234567890], "b": "caff\u00e8 \ud83d\ude00", "c": null, "d": true}`,
		`[]`, `{}`, `"x"`, `-12`,
	} {
		v, err := Loads(text)
		if err != nil {
			t.Fatalf("%s: %v", text, err)
		}
		if got, _ := Dumps(v, Options{EnsureASCII: true}); got != text {
			t.Errorf("got %s\nwant %s", got, text)
		}
	}
	v, _ := Loads(`{"a": 1, "b": 2, "a": 3}`)
	if got, _ := Dumps(v, Options{}); got != `{"a": 3, "b": 2}` {
		t.Errorf("a repeated key: %s", got)
	}
	for _, bad := range []string{`{"a" 1}`, `[1,]`, `01`, `{"a": 1} x`, "\"a\x01\""} {
		if _, err := Loads(bad); err == nil {
			t.Errorf("%q should not load", bad)
		}
	}
}
