package settings

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// pyValue turns a json.loads value (pyd's) into pyjson's, its big integers too.
func pyValue(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		out := pyjson.Object{}
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			out = append(out, pyjson.KV{Key: k, Value: pyValue(e)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = pyValue(e)
		}
		return out
	}
	if b, ok := v.(interface{ String() string }); ok {
		if _, isStr := v.(string); !isStr {
			return pyjson.Int(b.String())
		}
	}
	return v
}

// TestCheckIsOverrides compares Check with overrides.check, case by case
// (testdata/check_golden.json, written by internal/contracts/testdata/gen_golden.py).
func TestCheckIsOverrides(t *testing.T) {
	raw, err := os.ReadFile("testdata/check_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		Check []struct {
			Values, Good, Errors string
			Changed              []string
		}
	}
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}
	env, err := config.PanelEnvOf(func(string) (string, bool) { return "", false })
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range golden.Check {
		v, err := pyd.LoadsStr(c.Values)
		if err != nil {
			t.Fatal(err)
		}
		values := pyValue(v).(pyjson.Object)
		// Check takes pyd's values: the big integers back
		for i, kv := range values {
			if n, ok := kv.Value.(pyjson.Int); ok {
				big, _ := pyd.LoadsStr(string(n))
				values[i].Value = big
			}
		}
		changed := map[string]bool{}
		for _, k := range c.Changed {
			changed[k] = true
		}
		good, errs := Check(values, env, changed)
		g, _ := pyjson.Dumps(good, pyjson.Options{EnsureASCII: true})
		e, _ := pyjson.Dumps(errs, pyjson.Options{EnsureASCII: true})
		if g != c.Good || e != c.Errors {
			t.Errorf("%s\n  good   %s\n  python %s\n  errors %s\n  python %s", c.Values, g, c.Good, e, c.Errors)
		}
	}
}

func TestEveryItemHasItsEnvironment(t *testing.T) {
	env, err := config.PanelEnvOf(func(string) (string, bool) { return "", false })
	if err != nil {
		t.Fatal(err)
	}
	for _, it := range Items {
		if _, ok := env[it.Key]; !ok {
			t.Errorf("%s: no environment value", it.Key)
		}
	}
	if len(Items) != 77 {
		t.Errorf("%d items, overrides.ITEMS has 77", len(Items))
	}
	if env["engine.run_stale_timeout_seconds"] != int64(3900) || env["ai.dbt_export_timeout_seconds"] != int64(180) ||
		env["oidc.timeout_seconds"] != 10.0 || env["db.pool_size"] != nil {
		t.Errorf("defaults: %v %v %v %v", env["engine.run_stale_timeout_seconds"], env["ai.dbt_export_timeout_seconds"],
			env["oidc.timeout_seconds"], env["db.pool_size"])
	}
	set, _ := config.PanelEnvOf(func(k string) (string, bool) {
		return map[string]string{"AI__DBT_EXPORT_TIMEOUT_SECONDS": "90", "SCRIPTS__BUILD_CPU": " 3 "}[k],
			k == "AI__DBT_EXPORT_TIMEOUT_SECONDS" || k == "SCRIPTS__BUILD_CPU"
	})
	if set["ai.dbt_export_timeout_seconds"] != 90.0 || set["scripts.build_cpu"] != "3" {
		t.Errorf("from the environment: %v %q", set["ai.dbt_export_timeout_seconds"], set["scripts.build_cpu"])
	}
}
