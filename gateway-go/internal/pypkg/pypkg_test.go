package pypkg

import (
	"encoding/json"
	"os"
	"testing"
)

// testdata/python.json is what packaging 26.3 and script_packages answer (gateway image).
func TestAsPackaging(t *testing.T) {
	raw, err := os.ReadFile("testdata/python.json")
	if err != nil {
		t.Fatal(err)
	}
	var want struct {
		Versions [][]any    `json:"versions"`
		Order    []string   `json:"order"`
		Wheels   [][]any    `json:"wheels"`
		Names    [][]string `json:"names"`
	}
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	var ok []*Version
	for _, c := range want.Versions {
		v, err := Parse(c[0].(string))
		if c[1] == nil {
			if err == nil {
				t.Errorf("%q: parsed as %s, Python refuses it", c[0], v)
			}
			continue
		}
		if err != nil {
			t.Errorf("%q: %v", c[0], err)
			continue
		}
		if v.String() != c[1] || v.IsPrerelease() != c[2] {
			t.Errorf("%q: %s %v, Python %v %v", c[0], v, v.IsPrerelease(), c[1], c[2])
		}
		ok = append(ok, v)
	}
	// sorted(reverse=True) is stable: the equal ones keep their order
	for i := 1; i < len(ok); i++ {
		for j := i; j > 0 && Compare(ok[j], ok[j-1]) > 0; j-- {
			ok[j], ok[j-1] = ok[j-1], ok[j]
		}
	}
	for i, v := range ok {
		if v.String() != want.Order[i] {
			t.Errorf("order %d: %s, Python %s", i, v, want.Order[i])
		}
	}
	for _, c := range want.Wheels {
		name := c[0].(string)
		if c[1] != nil {
			fits, err := WheelFits(name, [2]int{3, 12})
			if err != nil || fits != c[1].(bool) {
				t.Errorf("%s fits: %v %v, Python %v", name, fits, err, c[1])
			}
		}
		got := fileVersion("pkg", name)
		w, _ := c[2].(string)
		if got != w {
			t.Errorf("%s version: %q, Python %q", name, got, w)
		}
	}
	for _, n := range want.Names {
		if got := CanonicalizeName(n[0]); got != n[1] {
			t.Errorf("%s: %s, Python %s", n[0], got, n[1])
		}
	}
}
