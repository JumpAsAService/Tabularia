package pyjson

import (
	"math"
	"testing"
)

func TestStrAndRepr(t *testing.T) {
	for _, c := range []struct {
		v    any
		want string
	}{
		{nil, "None"}, {true, "True"}, {int64(5), "5"}, {1.0, "1.0"}, {"it's", `"it's"`}, {"a", "'a'"},
		{Object{{"a", []any{int64(1), "b"}}}, `{'a': [1, 'b']}`}, {math.Inf(1), "inf"}, {Int("123456789012345678901"), "123456789012345678901"},
	} {
		if got := Repr(c.v); got != c.want {
			t.Errorf("repr %#v = %s, want %s", c.v, got, c.want)
		}
	}
	if Str("x") != "x" || Str(int64(3)) != "3" || !Truthy(Int("1")) || Truthy(Object{}) {
		t.Error("str, truthiness")
	}
}
