package modelgroups

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// a package of a recipe: pinned, as the scripting environments' packages are written (R
// name@version, Python name==version)
var pinned = map[string]*regexp.Regexp{
	"r":      regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]*@[0-9][0-9.-]*$`),
	"python": regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][0-9A-Za-z.+-]*$`),
}

func TestEveryGroupOfTheCatalogueHasARecipeAndARunner(t *testing.T) {
	groups := Groups()
	if len(groups) != len(modelcatalog.Groups()) || len(groups) != 5 {
		t.Fatalf("groups: %d", len(groups))
	}
	for _, g := range groups {
		r := g.Recipe
		if pinned[r.Language] == nil || r.Base == "" || r.PinnedOn == "" || r.MaxRows <= 0 || r.MaxSeconds <= 0 ||
			r.Resources.CPU == "" || r.Resources.Memory == "" {
			t.Errorf("%s: recipe %+v", g.ID, r)
		}
		if _, err := BaseImage(r, scriptcfg.DefaultBaseImages); err != nil {
			t.Errorf("%s: %v", g.ID, err)
		}
		seen := map[string]bool{}
		for _, p := range r.Packages {
			name, _, _ := strings.Cut(p, "@")
			name, _, _ = strings.Cut(name, "==")
			if !pinned[r.Language].MatchString(p) || seen[name] {
				t.Errorf("%s: package %q not pinned, or twice", g.ID, p)
			}
			seen[name] = true
		}
		// R: arrow reads and writes the Parquet files, jsonlite task.json and the card; Python:
		// pyarrow, numpy and pandas, the library's own (json is Python's)
		need := map[string][]string{"r": {"arrow", "jsonlite"}, "python": {"pyarrow", "numpy", "pandas"}}[r.Language]
		for _, n := range need {
			if !seen[n] {
				t.Errorf("%s: %s must be pinned", g.ID, n)
			}
		}
		if len(r.Packages) > 200 {
			t.Errorf("%s: at most 200 packages", g.ID)
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
		wanted := map[string][]string{
			"r": {"runner/run.R", "common/R/protocol.R", "common/R/main.R", "common/R/selftest.R"},
			"python": {"runner/run.py", "common/py/tab_protocol.py", "common/py/tab_main.py", "common/py/tab_selftest.py",
				"common/py/tab_columns.py", "common/py/tab_results.py", "common/py/tab_card.py", "common/py/tab_charts.py"},
		}[r.Language]
		for _, want := range append(wanted, "common/selftest/table.csv", "card.schema.json", "catalog.json") {
			if !paths[want] {
				t.Errorf("%s: no %s among %v", g.ID, want, paths)
			}
		}
		// a group receives its language's library only
		other := map[string]string{"r": "common/py/", "python": "common/R/"}[r.Language]
		for p := range paths {
			if strings.HasPrefix(p, other) {
				t.Errorf("%s: %s is another language's", g.ID, p)
			}
		}
		// one file per model: every model of the group is a function of its runner
		if r.Language == "r" {
			run := string(mustFile(t, code, "runner/run.R"))
			for _, m := range g.Models {
				if !strings.Contains(run, m+" = list(") {
					t.Errorf("%s: run.R has no route for %s", g.ID, m)
				}
				if !paths["runner/R/"+m+".R"] {
					t.Errorf("%s: no runner/R/%s.R", g.ID, m)
				}
			}
		} else {
			// a module is never named as a library: `import xgboost` inside it must reach the library
			run := string(mustFile(t, code, "runner/run.py"))
			for _, m := range g.Models {
				if !strings.Contains(run, `"`+m+`": {"fit": `) {
					t.Errorf("%s: run.py has no route for %s", g.ID, m)
				}
				if !paths["runner/py/"+g.ID+"_"+m+".py"] || paths["runner/py/"+m+".py"] {
					t.Errorf("%s: runner/py/%s_%s.py, never runner/py/%s.py", g.ID, g.ID, m, m)
				}
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

func TestTheJobReceivesTheCatalogue(t *testing.T) {
	code, _ := CodeFiles("survival", nil, false)
	sent := mustFile(t, code, "catalog.json")
	var got, want any
	if json.Unmarshal(sent, &got) != nil || json.Unmarshal(modelcatalog.Raw(), &want) != nil || !reflect.DeepEqual(got, want) ||
		len(sent) >= len(modelcatalog.Raw()) || len(CardSchema()) == 0 {
		t.Error("catalog.json is not the catalogue, without its indentation")
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

// The GNU Scientific Library is the customer lifetime group's alone: its Dockerfile adds libgsl-dev
// to the fixed libraries of every R image, and no other group's image changes (docs/specs/
// customer-lifetime-models.md, «The recipe»).
func TestOnlyTheCustomerLifetimeImageInstallsGSL(t *testing.T) {
	s := scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2}
	for _, g := range Groups() {
		d, err := Dockerfile(g.ID, scriptcfg.DefaultBaseImages, s)
		if err != nil {
			t.Fatal(err)
		}
		if g.ID != "customer_lifetime" {
			if len(g.Recipe.SystemLibraries) != 0 || strings.Contains(d, "libgsl") {
				t.Errorf("%s: a system library the other groups do not have", g.ID)
			}
			continue
		}
		fixed := strings.Join(scriptimage.RSystemLibraries, " ")
		if !reflect.DeepEqual(g.Recipe.SystemLibraries, []string{"libgsl-dev"}) ||
			!strings.Contains(d, "install -y --no-install-recommends "+fixed+" libgsl-dev && rm -rf /var/lib/apt/lists/*\n") ||
			!strings.Contains(d, "\nCLVTools@0.12.1\n") {
			t.Errorf("customer_lifetime:\n%s", d)
		}
		// the same recipe without its system library is what every other R group's Dockerfile is made of
		plain, _ := scriptimage.Dockerfile("r", "rocker/r-ver:4.6.0", g.Recipe.Packages, scriptimage.Settings{CRANURL: s.CRANURL, Jobs: 2, Managed: true})
		if strings.Replace(d, " libgsl-dev", "", 1) != plain {
			t.Error("libgsl-dev is not the only difference")
		}
	}
	// a Job's group copy is not the shipped one
	g, _ := Get("customer_lifetime")
	g.Recipe.SystemLibraries[0] = "x"
	if again, _ := Get("customer_lifetime"); again.Recipe.SystemLibraries[0] != "libgsl-dev" {
		t.Error("Groups hands out the recipe's own slice")
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

// //go:embed leaves out the files and folders whose name starts with «_» or «.»: none may be in
// a runner or a library, and every file of them in the source tree is among the Job's files.
func TestEveryFileOfTheSourceTreeIsInTheJobsCode(t *testing.T) {
	for _, g := range Groups() {
		code, err := CodeFiles(g.ID, nil, false)
		if err != nil {
			t.Fatal(err)
		}
		have := map[string]bool{}
		for _, f := range code {
			have[f.Path] = true
		}
		library := map[string]string{"r": "common/R", "python": "common/py"}[g.Recipe.Language]
		for root, as := range map[string]string{filepath.Join("groups", g.ID, "runner"): "runner", library: library, "common/selftest": "common/selftest"} {
			n := 0
			err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
				if err != nil {
					return err
				}
				if d.Name() == "__pycache__" && d.IsDir() {
					return filepath.SkipDir // bytecode of a local run, never shipped
				}
				if strings.HasPrefix(d.Name(), "_") || strings.HasPrefix(d.Name(), ".") {
					t.Errorf("%s: %s starts with «_» or «.»: //go:embed leaves it out", g.ID, p)
				}
				if d.IsDir() {
					return nil
				}
				rel, _ := filepath.Rel(root, p)
				if want := as + "/" + filepath.ToSlash(rel); !have[want] {
					t.Errorf("%s: %s is not among the Job's files", g.ID, want)
				}
				n++
				return nil
			})
			if err != nil || n == 0 {
				t.Errorf("%s: %s: %d files, %v", g.ID, root, n, err)
			}
		}
		if _, err := os.Stat(filepath.Join("groups", g.ID, "reference", "expected.json")); err != nil {
			t.Errorf("%s: %v", g.ID, err)
		}
	}
}

func TestTheCommandsByLanguage(t *testing.T) {
	run, selftest := Commands("r")
	if strings.Join(run, " ") != "Rscript /tabularia/code/runner/run.R" || strings.Join(selftest, " ") != "Rscript /tabularia/code/runner/run.R --selftest" {
		t.Errorf("R: %v %v", run, selftest)
	}
	run, selftest = Commands("python")
	if strings.Join(run, " ") != "python -I -B /tabularia/code/runner/run.py" ||
		strings.Join(selftest, " ") != "python -I -B /tabularia/code/runner/run.py --selftest" || len(run) != 4 {
		t.Errorf("Python: %v %v", run, selftest)
	}
}

// The Python library speaks the protocol the gateway reads.
func TestThePythonLibrarySpeaksTheSameProtocol(t *testing.T) {
	code, _ := CodeFiles("boosting", nil, false)
	protocol := string(mustFile(t, code, "common/py/tab_protocol.py"))
	for _, want := range []string{"PROTOCOL = 1", "EXIT_REFUSED = 3", "EXIT_SELFTEST_FAILED = 4", `print("TABULARIA_ERROR " + text`,
		EnvInput, EnvOutput, EnvResults, EnvArtifacts, EnvModelIn, EnvModelFileIn} {
		if !strings.Contains(protocol, want) {
			t.Errorf("tab_protocol.py does not say %q", want)
		}
	}
	if !strings.Contains(string(mustFile(t, code, "common/py/tab_selftest.py")), `print("TABULARIA_SELFTEST_REPORT " + to_json(report)`) {
		t.Error("tab_selftest.py does not print the report's line")
	}
}

// A managed Python image: the recipe's packages only, wheels without dependencies, pip check.
func TestThePythonGroupsDockerfile(t *testing.T) {
	d, err := Dockerfile("boosting", scriptcfg.DefaultBaseImages, scriptimage.Settings{Jobs: 2})
	if err != nil {
		t.Fatal(err)
	}
	g, _ := Get("boosting")
	want, _ := scriptimage.Dockerfile("python", "python:3.14-slim", g.Recipe.Packages, scriptimage.Settings{Jobs: 2, Managed: true})
	if d != want || !strings.HasPrefix(d, "FROM python:3.14-slim\n") || !strings.Contains(d, "\nxgboost-cpu==3.4.1\n") ||
		!strings.Contains(d, "--no-deps --only-binary=:all:") || strings.Contains(d, "polars") || strings.Contains(d, "marimo") {
		t.Errorf("Dockerfile:\n%s", d)
	}
}
