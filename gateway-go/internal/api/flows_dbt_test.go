package api

import (
	"encoding/json"
	"os"
	"testing"
)

func TestDbtContentDispositionAsPython(t *testing.T) {
	raw, err := os.ReadFile("../dbtexport/testdata/python_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var c struct{ CD map[string]string }
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	for name, want := range c.CD {
		if got := dbtContentDisposition(dbtFileName(name), "_dbt_duckdb.zip"); got != want {
			t.Errorf("%q: %q, want %q", name, got, want)
		}
	}
	if pyG(100) != "100" || pyG(12.5) != "12.5" || pyG(3600) != "3600" || pyG(10.123456789) != "10.1235" {
		t.Error("pyG")
	}
}
