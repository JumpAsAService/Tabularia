package bayesgraph

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestUpgradeChain(t *testing.T) {
	doc := map[string]any{"format_version": json.Number("1"), "legacy": json.Number("5")}
	table := map[int]upgradeFunc{
		1: func(d map[string]any) (map[string]any, error) {
			out := map[string]any{}
			for k, v := range d {
				out[k] = v
			}
			out["step1"] = true
			delete(out, "legacy")
			return out, nil
		},
		2: func(d map[string]any) (map[string]any, error) {
			out := map[string]any{}
			for k, v := range d {
				out[k] = v
			}
			out["step2"] = true
			return out, nil
		},
	}
	got, err := upgrade(doc, 1, 3, table)
	if err != nil {
		t.Fatal(err)
	}
	if got["format_version"] != json.Number("3") || got["step1"] != true || got["step2"] != true || got["legacy"] != nil {
		t.Fatalf("%v", got)
	}
	if doc["legacy"] == nil || doc["step1"] != nil {
		t.Fatal("upgrade modified its argument")
	}
	if _, err := upgrade(doc, 1, 4, table); err == nil || !strings.Contains(err.Error(), "version 3") {
		t.Fatalf("a missing step must be an error: %v", err)
	}
	table[1] = func(map[string]any) (map[string]any, error) { return nil, fmt.Errorf("boom") }
	if _, err := upgrade(doc, 1, 3, table); err == nil {
		t.Fatal("a failing step must fail")
	}
	same, err := upgrade(doc, 3, 3, table)
	if err != nil || same["format_version"] != json.Number("1") {
		t.Fatal("nothing to upgrade must change nothing")
	}
}

func TestVersionOf(t *testing.T) {
	for in, want := range map[any]int{json.Number("1"): 1, json.Number("12"): 12} {
		if got, err := versionOf(map[string]any{"format_version": in}); err != nil || got != want {
			t.Errorf("%v: %d %v", in, got, err)
		}
	}
	for _, in := range []any{json.Number("0"), json.Number("-1"), json.Number("1.0"), json.Number("1e0"), json.Number("99999999999999"), "1", nil, true} {
		if _, err := versionOf(map[string]any{"format_version": in}); err == nil {
			t.Errorf("%v accepted", in)
		}
	}
	if _, err := versionOf(map[string]any{}); err == nil {
		t.Error("missing accepted")
	}
}

func TestCurrentVersionNeedsNoUpgradeAndTablePlaceholderIsEmpty(t *testing.T) {
	if FormatVersion != 1 || len(upgrades) != 0 {
		t.Fatalf("version %d with %d upgrades: add the upgrade test for the new format", FormatVersion, len(upgrades))
	}
}
