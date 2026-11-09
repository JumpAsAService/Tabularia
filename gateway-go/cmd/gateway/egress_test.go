package main

import (
	"reflect"
	"testing"
)

func TestTransparentPorts(t *testing.T) {
	got, err := transparentPorts(" 20000=443, 20001=80,,20002=9000 ")
	if err != nil || !reflect.DeepEqual(got, map[int]int{20000: 443, 20001: 80, 20002: 9000}) {
		t.Errorf("%v %v", got, err)
	}
	if got, err := transparentPorts(""); err != nil || len(got) != 0 {
		t.Errorf("empty: %v %v", got, err)
	}
	for _, bad := range []string{"443", "a=443", "20000=0", "70000=443", "20000=443=1"} {
		if _, err := transparentPorts(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}
