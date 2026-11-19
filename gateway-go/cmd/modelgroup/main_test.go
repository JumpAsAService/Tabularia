package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTheSubcommands(t *testing.T) {
	for _, name := range []string{"SCRIPTS__BASE_IMAGES", "SCRIPTS__CRAN_URL"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name) // Tabularia's defaults; t.Setenv puts the value back after
	}
	var out bytes.Buffer
	if err := run([]string{"-jobs", "3", "dockerfile", "survival"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(out.String(), "FROM rocker/r-ver:4.6.0\n") || !strings.Contains(out.String(), "\nsurvival@3.8-12\n") ||
		!strings.Contains(out.String(), "TABULARIA_JOBS=3") {
		t.Errorf("dockerfile:\n%s", out.String())
	}

	dir := t.TempDir()
	task := filepath.Join(dir, "task.json")
	params := filepath.Join(dir, "params.json")
	_ = os.WriteFile(params, []byte(`{"outcome": "y", "predictors": ["x"], "penalty": "ridge"}`), 0o644)
	out.Reset()
	if err := run([]string{"task", "linear", params}, &out); err != nil {
		t.Fatal(err)
	}
	var tk map[string]any
	if err := json.Unmarshal(out.Bytes(), &tk); err != nil || tk["variant"] != "penalized" || tk["rows_from"] != "trial" {
		t.Errorf("task: %s (%v)", out.String(), err)
	}
	_ = os.WriteFile(task, out.Bytes(), 0o644)

	code := filepath.Join(dir, "code")
	out.Reset()
	if err := run([]string{"code", "regression", code, task}, &out); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"runner/run.R", "runner/R/linear.R", "common/R/main.R", "common/selftest/table.csv",
		"catalog.json", "card.schema.json", "task.json", "reference/expected.json"} {
		if _, err := os.Stat(filepath.Join(code, f)); err != nil {
			t.Errorf("no %s: %v", f, err)
		}
	}

	_ = os.WriteFile(params, []byte(`{"outcome": "y"}`), 0o644)
	if err := run([]string{"task", "linear", params}, &out); err == nil || err.Error() != "linear: 'predictors' is required" {
		t.Errorf("a refused task: %v", err)
	}
	out.Reset()
	if err := run([]string{"groups"}, &out); err != nil || strings.Count(out.String(), "\n") != 3 {
		t.Errorf("groups: %v\n%s", err, out.String())
	}
	for _, bad := range [][]string{{}, {"nope"}, {"dockerfile"}, {"dockerfile", "nope"}, {"code", "nope", dir}} {
		if err := run(bad, &out); err == nil {
			t.Errorf("%v: no error", bad)
		}
	}
}
