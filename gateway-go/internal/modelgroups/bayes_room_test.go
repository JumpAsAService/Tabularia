package modelgroups

import (
	"os"
	"path/filepath"
	"testing"
)

// The room left in the group bayes's ConfigMap with the largest task of the corpus (docs/specs/bayes-runner.md, «The
// ConfigMap's room», lot R5): when the margin under the 900 KiB the size test allows falls under 100 KB, the runner's
// tests/ leave the ConfigMap (to groups/bayes/reference/runner_tests/, outside runner/).
func TestTheBayesConfigMapKeepsItsRoom(t *testing.T) {
	paths, err := filepath.Glob("../bayescontract/testdata/tasks/*.json")
	if err != nil || len(paths) == 0 {
		t.Fatalf("no task in the corpus: %v", err)
	}
	var largest []byte
	for _, p := range paths {
		b, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		if len(b) > len(largest) {
			largest = b
		}
	}
	size := configMapSize(mustCode(t, "bayes", largest, false))
	margin := 900<<10 - size
	t.Logf("the ConfigMap with the largest task (%d bytes): %d bytes, %d under the 900 KiB", len(largest), size, margin)
	if margin < 100_000 {
		t.Errorf("only %d bytes of room: move runner/tests out of the ConfigMap", margin)
	}
}
