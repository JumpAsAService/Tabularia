package bayescodegen

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The fixtures of testdata/ are stanc 2.40.0's --info of two golden programs (what the stanc check of
// CI reads live): CrossCheck is empty on them, and says what changed when one is altered.

func crossCheckCase(t *testing.T, name string) ([]byte, *Program) {
	t.Helper()
	info, err := os.ReadFile(filepath.Join("testdata", name+".info.json"))
	if err != nil {
		t.Fatal(err)
	}
	_, p := generateWith(t, readGraph(t, name), goldenExtension(name))
	return info, p
}

func TestCrossCheckAgrees(t *testing.T) {
	for _, name := range []string{"hier_slopes", "free_binomial"} {
		info, p := crossCheckCase(t, name)
		if ms := CrossCheck(info, p); len(ms) != 0 {
			t.Errorf("%s: %v", name, ms)
		}
	}
}

func TestCrossCheckDisagrees(t *testing.T) {
	cases := []struct {
		name, fixture, from, to, want string
	}{
		{"a parameter stanc does not report", "hier_slopes", `    "tau_b": { "type": "real", "dimensions": 0 },` + "\n", "", "parameters"},
		{"a parameter the manifest does not list", "hier_slopes", `"mu_a": { "type": "real", "dimensions": 0 },`, `"mu_a": { "type": "real", "dimensions": 0 }, "extra": { "type": "real", "dimensions": 0 },`, "parameters"},
		{"a variable in another order", "hier_slopes", `"mu_a": { "type": "real", "dimensions": 0 },
    "mu_b": { "type": "real", "dimensions": 0 },`, `"mu_b": { "type": "real", "dimensions": 0 },
    "mu_a": { "type": "real", "dimensions": 0 },`, "parameters"},
		{"another number of dimensions of a written variable", "hier_slopes", `"Z_eff": { "type": "real", "dimensions": 2 }`, `"Z_eff": { "type": "real", "dimensions": 1 }`, "parameters Z_eff"},
		{"another number of dimensions of a datum", "hier_slopes", `"g": { "type": "int", "dimensions": 1 }`, `"g": { "type": "int", "dimensions": 0 }`, "inputs g"},
		{"a datum that is not in the manifest", "hier_slopes", `"Mode": { "type": "int", "dimensions": 0 }`, `"Mode": { "type": "int", "dimensions": 0 }, "Q": { "type": "int", "dimensions": 0 }`, "inputs"},
		{"a function outside the list", "hier_slopes", `"functions": [`, `"functions": [ "reject",`, "functions reject"},
		{"a distribution outside the list", "hier_slopes", `"normal_lupdf"`, `"weibull_lupdf"`, "distributions weibull_lupdf"},
		{"an included file", "hier_slopes", `"included_files": []`, `"included_files": [ "x.stan" ]`, "included_files"},
		{"a mass reported for a density", "free_binomial", `"binomial_logit_lupmf"`, `"binomial_logit_lupdf"`, "distributions binomial_logit_lupdf"},
	}
	for _, c := range cases {
		info, p := crossCheckCase(t, c.fixture)
		if !strings.Contains(string(info), c.from) {
			t.Fatalf("%s: the fixture has no %q", c.name, c.from)
		}
		bad := []byte(strings.Replace(string(info), c.from, c.to, 1))
		ms := CrossCheck(bad, p)
		found := false
		for _, m := range ms {
			if strings.Contains(m.String(), c.want) {
				found = true
			}
		}
		if !found {
			t.Errorf("%s: want a mismatch about %q, got %v", c.name, c.want, ms)
		}
	}
}

// The unnormalised names stanc reports for the statements are the catalogue's densities: normal_lupdf
// is normal_lpdf, binomial_logit_lupmf binomial_logit_lpmf.
func TestCrossCheckReadsUnnormalisedNames(t *testing.T) {
	for in, want := range map[string]string{"normal_lupdf": "normal_lpdf", "binomial_logit_lupmf": "binomial_logit_lpmf", "to_vector": "to_vector", "y_lupdf_x": "y_lupdf_x"} {
		if got := densityName(in); got != want {
			t.Errorf("densityName(%s) = %s, want %s", in, got, want)
		}
	}
}

// Malformed reports are mismatches, not panics.
func TestCrossCheckRefusesMalformedInfo(t *testing.T) {
	_, p := crossCheckCase(t, "free_binomial")
	for _, info := range []string{``, `[]`, `{"inputs": 3}`, `{"inputs": {"N": {"type": "int"}}}`, `{"inputs": {}, "parameters": {}}`, `{"functions": 1}`} {
		if ms := CrossCheck([]byte(info), p); len(ms) == 0 {
			t.Errorf("%q: no mismatch", info)
		}
	}
	if ms := CrossCheck([]byte(`{}`), nil); len(ms) == 0 {
		t.Error("no program: no mismatch")
	}
}
