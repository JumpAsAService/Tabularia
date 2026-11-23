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
	for _, name := range []string{"SCRIPTS__BASE_IMAGES", "SCRIPTS__CRAN_URL", "SCRIPTS__PYPI_INDEX_URL"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name) // Tabularia's defaults; t.Setenv puts the value back after
	}
	var out bytes.Buffer
	if err := run([]string{"-jobs", "3", "dockerfile", "survival"}, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(out.String(), "FROM rocker/r-ver:4.6.0\n") || !strings.Contains(out.String(), "\nsurvival@3.8-12 ") ||
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
		"catalog.json", "card.schema.json", "task.json", "reference/expected.json.gz"} {
		if _, err := os.Stat(filepath.Join(code, f)); err != nil {
			t.Errorf("no %s: %v", f, err)
		}
	}

	_ = os.WriteFile(params, []byte(`{"outcome": "y"}`), 0o644)
	if err := run([]string{"task", "linear", params}, &out); err == nil || err.Error() != "linear: 'predictors' is required" {
		t.Errorf("a refused task: %v", err)
	}
	out.Reset()
	if err := run([]string{"groups"}, &out); err != nil || strings.Count(out.String(), "\n") != 8 { // the catalogue's seven and `bayes`, ahead of it
		t.Errorf("groups: %v\n%s", err, out.String())
	}
	// a Python group: its Dockerfile and its code, the library of its language only
	out.Reset()
	if err := run([]string{"dockerfile", "boosting"}, &out); err != nil || !strings.HasPrefix(out.String(), "FROM python:3.14-slim\n") ||
		!strings.Contains(out.String(), "pip install --no-deps --only-binary=:all: --require-hashes") || !strings.Contains(out.String(), "\ncatboost==1.2.10 --hash=sha256:") {
		t.Errorf("dockerfile boosting: %v\n%s", err, out.String())
	}
	py := filepath.Join(dir, "py")
	if err := run([]string{"code", "boosting", py}, &out); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"runner/run.py", "runner/py/boosting_xgboost.py", "common/py/tab_main.py", "common/selftest/table.csv",
		"catalog.json", "card.schema.json", "reference/expected.json.gz"} {
		if _, err := os.Stat(filepath.Join(py, f)); err != nil {
			t.Errorf("no %s: %v", f, err)
		}
	}
	if _, err := os.Stat(filepath.Join(py, "common", "R")); err == nil {
		t.Error("the R library in a Python group's code")
	}
	// the pre-trained forecasting group: its Dockerfile takes the sizes, before or after the group's name, and the mirrors
	for _, args := range [][]string{{"dockerfile", "foundation_forecast", "--sizes", "28m,5m"}, {"dockerfile", "--sizes=28m,5m", "foundation_forecast"}} {
		out.Reset()
		if err := run(args, &out); err != nil || strings.Count(out.String(), "ADD --checksum=") != 6 || strings.Contains(out.String(), "Timestral-123M") ||
			!strings.Contains(out.String(), "ARG TARGETARCH\n") || !strings.Contains(out.String(), "https://huggingface.co/mistralai/Timestral-28M/resolve/") {
			t.Errorf("dockerfile foundation_forecast %v: %v\n%s", args, err, out.String())
		}
	}
	out.Reset()
	if err := run([]string{"dockerfile", "foundation_forecast"}, &out); err != nil || strings.Count(out.String(), "ADD --checksum=") != 12 {
		t.Errorf("every size when none is asked for: %v\n%s", err, out.String())
	}
	out.Reset()
	if err := run([]string{"dockerfile", "foundation_forecast", "--sizes", "5m", "--weights-base-url", "https://hub.example/", "--wheels-base-url", "https://pt.example/cpu"}, &out); err != nil ||
		!strings.Contains(out.String(), "https://hub.example/mistralai/Timestral-5M/resolve/") || !strings.Contains(out.String(), `"https://pt.example/cpu/torch-2.14.1%2Bcpu-`) ||
		strings.Contains(out.String(), "huggingface.co") {
		t.Errorf("mirrors: %v\n%s", err, out.String())
	}
	// the code of the group: the runner, the vendored package with its __init__.py, the library of its language
	fcode := filepath.Join(dir, "ff")
	if err := run([]string{"code", "foundation_forecast", fcode}, &out); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"runner/run.py", "runner/py/ff_timestral.py", "runner/py/timestral/__init__.py", "runner/py/timestral/NOTICE",
		"runner/py/timestral/LICENSE", "runner/py/timestral/vendor.json", "common/py/tab_main.py", "reference/expected.json.gz"} {
		if _, err := os.Stat(filepath.Join(fcode, f)); err != nil {
			t.Errorf("no %s: %v", f, err)
		}
	}
	for _, bad := range [][]string{{"dockerfile", "foundation_forecast", "--sizes", "7m"}, {"dockerfile", "boosting", "--sizes", "28m"}, {"dockerfile", "foundation_forecast", "extra"}} {
		if err := run(bad, &out); err == nil {
			t.Errorf("%v: no error", bad)
		}
	}
	for _, bad := range [][]string{{}, {"nope"}, {"dockerfile"}, {"dockerfile", "nope"}, {"code", "nope", dir}} {
		if err := run(bad, &out); err == nil {
			t.Errorf("%v: no error", bad)
		}
	}
}
