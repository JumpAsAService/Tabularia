package dbtexport

import (
	"os"
	"reflect"
	"regexp"
	"strings"
	"testing"
)

// The required parameters of each step are written twice: `required` here and `_REQUIRED`
// in gateway/app/services/dbt_export.py (the frozen Python gateway). This reads the Python
// table as text and compares it with the Go one, so that a step added to one and not to the
// other fails here instead of exporting differently.
func TestRequiredAsPython(t *testing.T) {
	raw, err := os.ReadFile("../../../gateway/app/services/dbt_export.py")
	if err != nil {
		t.Skip("the Python gateway source is not in the build context: ", err)
	}
	block := regexp.MustCompile(`(?s)_REQUIRED\s*=\s*\{(.*?)\n\}`).FindSubmatch(raw)
	if block == nil {
		t.Fatal("_REQUIRED not found in dbt_export.py: did it move? update this test")
	}
	want := map[string][]string{}
	entry := regexp.MustCompile(`"(\w+)":\s*\(([^)]*)\)`)
	for _, m := range entry.FindAllStringSubmatch(string(block[1]), -1) {
		var params []string
		for _, p := range regexp.MustCompile(`"(\w+)"`).FindAllStringSubmatch(m[2], -1) {
			params = append(params, p[1])
		}
		want[m[1]] = params
	}
	if len(want) == 0 {
		t.Fatal("no entry parsed from _REQUIRED")
	}
	if !reflect.DeepEqual(required, want) {
		var diff []string
		for k, v := range want {
			if !reflect.DeepEqual(required[k], v) {
				diff = append(diff, k+": Python "+strings.Join(v, ",")+" / Go "+strings.Join(required[k], ","))
			}
		}
		for k, v := range required {
			if _, ok := want[k]; !ok {
				diff = append(diff, k+": only in Go ("+strings.Join(v, ",")+")")
			}
		}
		t.Fatalf("required differs from _REQUIRED: %s", strings.Join(diff, "; "))
	}
}
