package modelgroups

import (
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// a package of a recipe: pinned, as the scripting environments' R packages are written
var pinned = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]*@[0-9][0-9.-]*$`)

func TestEveryGroupOfTheCatalogueHasARecipeAndARunner(t *testing.T) {
	groups := Groups()
	if len(groups) != len(modelcatalog.Groups()) || len(groups) != 3 {
		t.Fatalf("groups: %d", len(groups))
	}
	for _, g := range groups {
		r := g.Recipe
		if r.Language != "r" || r.Base == "" || r.PinnedOn == "" || r.MaxRows <= 0 || r.MaxSeconds <= 0 ||
			r.Resources.CPU == "" || r.Resources.Memory == "" {
			t.Errorf("%s: recipe %+v", g.ID, r)
		}
		if _, err := BaseImage(r, scriptcfg.DefaultBaseImages); err != nil {
			t.Errorf("%s: %v", g.ID, err)
		}
		seen := map[string]bool{}
		for _, p := range r.Packages {
			name, _, _ := strings.Cut(p, "@")
			if !pinned.MatchString(p) || seen[name] {
				t.Errorf("%s: package %q not pinned, or twice", g.ID, p)
			}
			seen[name] = true
		}
		// arrow reads and writes the Parquet files, jsonlite task.json and the card
		if !seen["arrow"] || !seen["jsonlite"] || len(r.Packages) > 200 {
			t.Errorf("%s: arrow and jsonlite must be pinned, at most 200 packages", g.ID)
		}
		if len(g.RecipeHash) != 64 || len(g.RunnerHash) != 64 || len(g.ReferenceHash) != 64 {
			t.Errorf("%s: hashes %q %q %q", g.ID, g.RecipeHash, g.RunnerHash, g.ReferenceHash)
		}
		code, err := CodeFiles(g.ID, nil, false)
		if err != nil {
			t.Fatal(err)
		}
		paths := map[string]bool{}
		size := 0
		for _, f := range code {
			paths[f.Path] = true
			size += len(f.Content)
		}
		for _, want := range []string{"runner/run.R", "common/R/protocol.R", "common/R/main.R", "common/R/selftest.R",
			"common/selftest/table.csv", "card.schema.json", "catalog.json"} {
			if !paths[want] {
				t.Errorf("%s: no %s among %v", g.ID, want, paths)
			}
		}
		// one file per model: every model of the group is a function of its runner
		run := string(mustFile(t, code, "runner/run.R"))
		for _, m := range g.Models {
			if !strings.Contains(run, m+" = list(") {
				t.Errorf("%s: run.R has no route for %s", g.ID, m)
			}
			if !paths["runner/R/"+m+".R"] {
				t.Errorf("%s: no runner/R/%s.R", g.ID, m)
			}
		}
		withRef, _ := CodeFiles(g.ID, []byte("{}"), true)
		for _, f := range withRef {
			size += len(f.Content)
		}
		// a ConfigMap holds at most 1 MiB: the code and the small reference tables, twice over
		if size > 900<<10 {
			t.Errorf("%s: %d bytes of code", g.ID, size)
		}
		if string(mustFile(t, withRef, "task.json")) != "{}" || len(mustFile(t, withRef, "reference/expected.json")) == 0 {
			t.Errorf("%s: task.json or the reference cases are missing", g.ID)
		}
	}
	if _, err := CodeFiles("nope", nil, false); err == nil {
		t.Error("files of no group")
	}
}

func mustFile(t *testing.T, code []File, path string) []byte {
	t.Helper()
	for _, f := range code {
		if f.Path == path {
			return f.Content
		}
	}
	t.Fatalf("no %s", path)
	return nil
}

func TestTheJobReceivesTheCataloguesBytes(t *testing.T) {
	code, _ := CodeFiles("survival", nil, false)
	if string(mustFile(t, code, "catalog.json")) != string(modelcatalog.Raw()) || len(CardSchema()) == 0 {
		t.Error("catalog.json is not the catalogue")
	}
	var schema map[string]any
	if err := json.Unmarshal(CardSchema(), &schema); err != nil || schema["title"] != "Tabularia model card" {
		t.Errorf("card.schema.json: %v", err)
	}
	data, items := ConfigMap(code)
	if len(data) != len(code) || len(items) != len(code) || items[0]["key"] != "f0" || items[0]["path"] != code[0].Path {
		t.Errorf("ConfigMap: %d %d %v", len(data), len(items), items[0])
	}
}

func TestTheDockerfileIsTheScriptingEnvironmentsOne(t *testing.T) {
	s := scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2}
	d, err := Dockerfile("regression", scriptcfg.DefaultBaseImages, s)
	if err != nil {
		t.Fatal(err)
	}
	g, _ := Get("regression")
	want, _ := scriptimage.Dockerfile("r", "rocker/r-ver:4.6.0", g.Recipe.Packages, s)
	if d != want || !strings.Contains(d, "\nglmnet@5.2\n") || !strings.HasPrefix(d, "FROM rocker/r-ver:4.6.0\n") {
		t.Errorf("Dockerfile:\n%s", d)
	}
	if _, err := Dockerfile("regression", map[string][]scriptcfg.Image{"r": {{Version: "4.5", Image: "x"}}}, s); err == nil ||
		!strings.Contains(err.Error(), "the base r 4.6 is not one of the base images offered") {
		t.Errorf("a base not offered: %v", err)
	}
	if _, err := Dockerfile("nope", scriptcfg.DefaultBaseImages, s); err == nil {
		t.Error("a Dockerfile of no group")
	}
}

func TestAFitTask(t *testing.T) {
	task, err := FitTask("linear", map[string]any{"outcome": "y", "predictors": []any{"x"}, "output": "rows"}, true, 500)
	if err != nil {
		t.Fatal(err)
	}
	g, _ := Get("regression")
	if task.Protocol != Protocol || task.Mode != "fit" || task.Group != "regression" || task.Variant != "unpenalized" ||
		task.Output != "rows" || !task.WriteOutput || task.Limits["max_chart_points"] != 500 || task.Limits["max_terms"] != 200 ||
		task.RecipeHash != g.RecipeHash || task.RunnerHash != g.RunnerHash || task.Params["se"] != "classical" {
		t.Errorf("task: %+v", task)
	}
	var back map[string]any
	if err := json.Unmarshal(task.JSON(), &back); err != nil || back["protocol"] != float64(1) || back["write_output"] != true {
		t.Errorf("task.json: %s", task.JSON())
	}
	if modelcatalog.Limit("max_chart_points") != 2000 {
		t.Error("a task changed the catalogue's limits")
	}
	task, _ = FitTask("arima", map[string]any{"time": "t", "value": "v", "frequency": "month", "orders": "auto"}, false, 0)
	if task.Variant != "auto" || task.WriteOutput || task.Group != "timeseries" || task.Limits["max_chart_points"] != 2000 {
		t.Errorf("arima: %+v", task)
	}
	_, err = FitTask("linear", map[string]any{"outcome": "y"}, false, 0)
	var refusal *modelcatalog.Error
	if !errors.As(err, &refusal) || refusal.Message != "linear: 'predictors' is required" {
		t.Errorf("a refusal: %v", err)
	}
	if _, err := FitTask("nope", nil, false, 0); err == nil {
		t.Error("a task of no model")
	}
}

func TestTheLogsLines(t *testing.T) {
	log := "Loading…\nTABULARIA_ERROR {\"code\":\"x\",\"message\":\"first\"}\nmore\n" +
		"TABULARIA_ERROR {\"code\":\"separation\",\"message\":\"price separates the outcome\",\"params\":{\"column\":\"price\"}}\n"
	e, ok := ErrorFromLog(log)
	if !ok || e.Code != "separation" || e.Message != "price separates the outcome" || e.Params["column"] != "price" {
		t.Errorf("error: %+v", e)
	}
	if _, ok := ErrorFromLog("TABULARIA_ERROR not json\n"); ok {
		t.Error("a line that is not JSON")
	}
	r, ok := SelftestFromLog("TABULARIA_SELFTEST ok a\nTABULARIA_SELFTEST_REPORT {\"group\":\"regression\",\"ok\":11,\"failed\":0,\"skipped\":0,\"checks\":[{\"name\":\"a\",\"status\":\"ok\",\"detail\":\"\"}]}\n")
	if !ok || !r.Passed() || r.Group != "regression" || len(r.Checks) != 1 {
		t.Errorf("report: %+v", r)
	}
	r, _ = SelftestFromLog("TABULARIA_SELFTEST_REPORT {\"ok\":10,\"failed\":0,\"skipped\":1}\n")
	if r.Passed() {
		t.Error("a skipped check passed in an image")
	}
	if _, ok := SelftestFromLog("nothing"); ok {
		t.Error("a report from nothing")
	}
}

// The protocol's constants are the R library's.
func TestTheRLibrarySpeaksTheSameProtocol(t *testing.T) {
	code, _ := CodeFiles("regression", nil, false)
	protocol := string(mustFile(t, code, "common/R/protocol.R"))
	for _, want := range []string{"TAB_PROTOCOL <- 1L", "TAB_EXIT_REFUSED <- 3L", "TAB_EXIT_SELFTEST_FAILED <- 4L",
		`cat("TABULARIA_ERROR "`, EnvInput, EnvOutput, EnvResults, EnvArtifacts, EnvModelIn} {
		if !strings.Contains(protocol, want) {
			t.Errorf("protocol.R does not say %q", want)
		}
	}
	if !strings.Contains(string(mustFile(t, code, "common/R/selftest.R")), `cat("TABULARIA_SELFTEST_REPORT "`) {
		t.Error("selftest.R does not print the report's line")
	}
}
