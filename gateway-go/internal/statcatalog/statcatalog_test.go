package statcatalog

import (
	"bytes"
	"os"
	"strings"
	"testing"
)

// The copy is the engine's snapshot (backend/app/engine/stats/catalog.json), byte for byte:
// write it again with `python3 .github/scripts/stat_catalog.py` when the catalogue changes.
func TestTheCopyIsTheEnginesSnapshot(t *testing.T) {
	want, err := os.ReadFile("../../../backend/app/engine/stats/catalog.json")
	if err != nil {
		t.Skip("the engine's sources are not beside the gateway's: ", err)
	}
	if !bytes.Equal(raw, want) {
		t.Fatal("internal/statcatalog/catalog.json differs from backend/app/engine/stats/catalog.json: run python3 .github/scripts/stat_catalog.py")
	}
}

func TestEveryTestIsAnOperationOfItsFamily(t *testing.T) {
	ops := Operations()
	if len(ops) == 0 || len(ops) != len(Tests()) {
		t.Fatalf("operations: %v", ops)
	}
	families := map[string]bool{}
	for _, f := range Families() {
		families[f] = true
	}
	if len(families) != 6 {
		t.Errorf("families: %v", Families())
	}
	seen := map[string]bool{}
	for i, test := range Tests() {
		if test.Operation != ops[i] || test.Operation != "stat_"+test.ID || !strings.HasPrefix(test.Operation, data.Prefix) {
			t.Errorf("%+v is not operation %q", test, ops[i])
		}
		if seen[test.Operation] {
			t.Errorf("%s is listed twice", test.Operation)
		}
		seen[test.Operation] = true
		if !families[test.Family] {
			t.Errorf("%s: unknown family %q", test.ID, test.Family)
		}
		if !Is(test.Operation) {
			t.Errorf("Is(%q) is false", test.Operation)
		}
	}
	if !Is("stat_t_two_sample") {
		t.Error("the two-sample t test is not in the catalogue")
	}
}

func TestOnlyTheCataloguesOperationsAreStatistics(t *testing.T) {
	for _, name := range []string{"", "stat_", "stat_nope", "t_two_sample", "pivot", "foreach", "STAT_T_TWO_SAMPLE", " stat_t_two_sample"} {
		if Is(name) {
			t.Errorf("Is(%q) is true", name)
		}
	}
}

// What a caller gets is its own: changing it does not change the catalogue.
func TestTheListsAreCopies(t *testing.T) {
	ops := Operations()
	ops[0] = "changed"
	tests := Tests()
	tests[0].Operation = "changed"
	families := Families()
	families[0] = "changed"
	if Operations()[0] == "changed" || Tests()[0].Operation == "changed" || Families()[0] == "changed" {
		t.Error("a caller changed the catalogue")
	}
}
