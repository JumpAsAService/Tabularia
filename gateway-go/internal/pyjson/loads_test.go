package pyjson

import "testing"

func TestLoadsKeepsOrderTypesAndWritesBackTheSame(t *testing.T) {
	for _, text := range []string{
		`{"b": 1, "a": [1.0, 2.5, "è", null, true], "c": {"z": 1e+20}}`,
		`{"x": 1, "x": 2}`,
		`[]`,
	} {
		v, err := Loads(text)
		if err != nil {
			t.Fatal(err)
		}
		got, _ := Dumps(v, Options{})
		want := map[string]string{`{"x": 1, "x": 2}`: `{"x": 2}`}[text]
		if want == "" {
			want = text
		}
		if got != want {
			t.Errorf("%s: wrote %s", text, got)
		}
	}
	if _, err := Loads(`{"a": 1} x`); err == nil {
		t.Error("extra data must not read")
	}
}

func TestEqualAndRepr(t *testing.T) {
	a, _ := Loads(`{"a": 1, "b": [true, 2.0]}`)
	b, _ := Loads(`{"b": [1, 2], "a": 1.0}`)
	if !Equal(a, b) {
		t.Error("1 == 1.0 and True == 1, keys in any order")
	}
	if Equal(a, Object{}) || Equal("1", int64(1)) {
		t.Error("different values compared equal")
	}
	if got := Repr([]any{"it's", int64(1), nil, true, 1.5}); got != `["it's", 1, None, True, 1.5]` {
		t.Errorf("repr: %s", got)
	}
}
