package pyjson

import (
	"math"
	"testing"
)

func TestLoadsKeepsPythonsDict(t *testing.T) {
	v, err := Loads([]byte(`{"b": 1, "a": [1.0, 2, "x"], "b": 3, "big": 123456789012345678901234567890, "n": NaN}`))
	if err != nil {
		t.Fatal(err)
	}
	o := v.(Object)
	if len(o) != 4 || o[0].Key != "b" || o[0].Value != int64(3) || o[1].Key != "a" {
		t.Fatalf("order or duplicate key: %#v", o)
	}
	if o[2].Value != BigInt("123456789012345678901234567890") || !math.IsNaN(o[3].Value.(float64)) {
		t.Fatalf("numbers: %#v", o)
	}
	s, _ := Dumps(o, Options{EnsureASCII: true})
	if want := `{"b": 3, "a": [1.0, 2, "x"], "big": 123456789012345678901234567890, "n": NaN}`; s != want {
		t.Fatalf("dumps %s", s)
	}
}

func TestLoadsRefuses(t *testing.T) {
	for _, in := range []string{``, `{`, `[1,]`, `{"a" 1}`, "\"a\x01\"", `01`, `1.`, `{"a": 1} x`, "\xff"} {
		if _, err := Loads([]byte(in)); err == nil {
			t.Errorf("%q accepted", in)
		}
	}
	if _, err := LoadsNoConstants([]byte(`{"a": Infinity}`)); err == nil {
		t.Error("Infinity accepted without constants")
	}
	if v, err := Loads([]byte(`"😀 è"`)); err != nil || v != "😀 è" {
		t.Errorf("escapes: %v %v", v, err)
	}
}

func TestStrAndRepr(t *testing.T) {
	for _, c := range []struct {
		v    any
		want string
	}{
		{nil, "None"}, {true, "True"}, {int64(5), "5"}, {1.0, "1.0"}, {"it's", `"it's"`},
		{Object{{"a", []any{int64(1), "b"}}}, `{'a': [1, 'b']}`}, {math.Inf(1), "inf"},
	} {
		got := Repr(c.v)
		if c.v == "it's" {
			got = Repr("it's")
		}
		if got != c.want {
			t.Errorf("repr %#v = %s, want %s", c.v, got, c.want)
		}
	}
	if Str("x") != "x" || Str(int64(3)) != "3" {
		t.Error("str")
	}
}
