package modelgroups

import (
	"crypto/sha256"
	"encoding/hex"
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
	if len(groups) != len(modelcatalog.Groups()) || len(groups) != 7 {
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
		for _, f := range code {
			paths[f.Path] = true
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
				// the foundation forecast's own module is runner/py/ff_<model>.py (its package, `timestral`, is vendored beside it:
				// a module never has the package's name)
				module := "runner/py/" + g.ID + "_" + m + ".py"
				if g.ID == "foundation_forecast" {
					module = "runner/py/ff_" + m + ".py"
				}
				if !paths[module] || paths["runner/py/"+m+".py"] {
					t.Errorf("%s: %s, never runner/py/%s.py", g.ID, module, m)
				}
			}
		}
		withRef, err := CodeFiles(g.ID, []byte("{}"), true)
		if err != nil {
			t.Fatal(err)
		}
		// a ConfigMap holds at most 1 MiB, and a Job's receives either the code with its task or the
		// self-test's (the code with the reference cases, no task), never both: measure each one as
		// ConfigMap() makes it (the keys and the data, and the items that map them), the biggest of
		// the two against the limit, with room for the metadata
		for what, code := range map[string][]File{"job": mustCode(t, g.ID, []byte("{}"), false), "self-test": mustCode(t, g.ID, nil, true)} {
			if size := configMapSize(code); size > 900<<10 {
				t.Errorf("%s: the %s ConfigMap is %d bytes", g.ID, what, size)
			}
		}
		if string(mustFile(t, withRef, "task.json")) != "{}" || len(mustFile(t, withRef, "reference/expected.json")) == 0 {
			t.Errorf("%s: task.json or the reference cases are missing", g.ID)
		}
	}
	if _, err := CodeFiles("nope", nil, false); err == nil {
		t.Error("files of no group")
	}
}

func mustCode(t *testing.T, group string, task []byte, withReference bool) []File {
	t.Helper()
	code, err := CodeFiles(group, task, withReference)
	if err != nil {
		t.Fatal(err)
	}
	return code
}

// configMapSize is the size of the ConfigMap ConfigMap(code) makes: its keys and values, and the
// items (key and path) of the volume that mounts it.
func configMapSize(code []File) int {
	data, items := ConfigMap(code)
	size := 0
	for k, v := range data {
		size += len(k) + len(v)
	}
	for _, it := range items {
		size += len(it["key"]) + len(it["path"])
	}
	return size
}

func TestTheBiggestConfigMapOfEachGroup(t *testing.T) {
	for _, g := range Groups() {
		t.Logf("%s: job %d bytes, self-test %d bytes", g.ID, configMapSize(mustCode(t, g.ID, []byte("{}"), false)), configMapSize(mustCode(t, g.ID, nil, true)))
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

// The GNU Scientific Library is the customer lifetime group's and, for Rlibeemd (the EMD of the decomposition node), the time
// series group's: their Dockerfiles add libgsl-dev to the fixed libraries of every R image, and no other group's image changes
// (docs/specs/customer-lifetime-models.md, «The recipe»; docs/specs/ts-decomposition.md, «The EMD package»).
func TestOnlyTheCustomerLifetimeAndTheTimeSeriesImagesInstallGSL(t *testing.T) {
	// the package of each that needs the library
	needs := map[string]string{"customer_lifetime": "CLVTools@0.12.1", "timeseries": "Rlibeemd@1.4.4"}
	s := scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2}
	for _, g := range Groups() {
		d, err := Dockerfile(g.ID, scriptcfg.DefaultBaseImages, s)
		if err != nil {
			t.Fatal(err)
		}
		if needs[g.ID] == "" {
			if len(g.Recipe.SystemLibraries) != 0 || strings.Contains(d, "libgsl") {
				t.Errorf("%s: a system library the other groups do not have", g.ID)
			}
			continue
		}
		fixed := strings.Join(scriptimage.RSystemLibraries, " ")
		if !reflect.DeepEqual(g.Recipe.SystemLibraries, []string{"libgsl-dev"}) ||
			!strings.Contains(d, "install -y --no-install-recommends "+fixed+" libgsl-dev && rm -rf /var/lib/apt/lists/*\n") ||
			!strings.Contains(d, "\n"+needs[g.ID]+"\n") {
			t.Errorf("%s:\n%s", g.ID, d)
		}
		// the same recipe without its system library is what every other R group's Dockerfile is made of
		plain, _ := scriptimage.Dockerfile("r", "rocker/r-ver:4.6.0", g.Recipe.Packages, scriptimage.Settings{CRANURL: s.CRANURL, Jobs: 2, Managed: true})
		// and the step that records the versions installed, which the card reads from the lock
		record := "RUN mkdir -p /opt/tabularia && dpkg-query -W -f '${Package}@${Version}\\n' libgsl-dev > " + scriptimage.SystemLibrariesFile + "\n"
		if strings.Replace(strings.Replace(d, record, "", 1), " libgsl-dev", "", 1) != plain || !strings.Contains(d, record) {
			t.Error("libgsl-dev and the record of its version are not the only difference")
		}
	}
	// a Job's group copy is not the shipped one
	g, _ := Get("customer_lifetime")
	g.Recipe.SystemLibraries[0] = "x"
	if again, _ := Get("customer_lifetime"); again.Recipe.SystemLibraries[0] != "libgsl-dev" {
		t.Error("Groups hands out the recipe's own slice")
	}
}

// The experiments group's image is the other R groups' own for as long as it goes: the same base, the same
// system libraries (none of its own), and as packages the first 16 of every R recipe, the `arrow` closure,
// and nothing else (docs/specs/ab-testing.md, «The recipe»). Its two nodes have a route and no `predict`.
func TestTheExperimentsImageIsTheArrowClosureAndNothingElse(t *testing.T) {
	g, ok := Get("experiments")
	if !ok || !reflect.DeepEqual(g.Models, []string{"ab_test", "ab_plan"}) {
		t.Fatalf("group: %+v", g)
	}
	r := g.Recipe
	if r.Language != "r" || r.Base != "4.6" || len(r.SystemLibraries) != 0 || len(r.Packages) != 16 || r.MaxRows != 5_000_000 ||
		r.MaxSeconds != 1800 || r.Resources.CPU != "1" || r.Resources.Memory != "2Gi" {
		t.Errorf("recipe: %+v", r)
	}
	for _, other := range []string{"regression", "survival", "timeseries", "customer_lifetime"} {
		o, _ := Get(other)
		if !reflect.DeepEqual(o.Recipe.Packages[:16], r.Packages) {
			t.Errorf("%s: its first 16 packages are not the experiments' %v", other, r.Packages)
		}
	}
	s := scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2}
	d, err := Dockerfile("experiments", scriptcfg.DefaultBaseImages, s)
	if err != nil || !strings.HasPrefix(d, "FROM rocker/r-ver:4.6.0\n") || strings.Contains(d, "libgsl") ||
		!strings.Contains(d, "\narrow@26.0.1\n") || strings.Contains(d, "data.table") {
		t.Errorf("Dockerfile: %v\n%s", err, d)
	}
	// the base and the system libraries' layer are the other R groups' own, byte for byte
	other, _ := Dockerfile("regression", scriptcfg.DefaultBaseImages, s)
	layers := func(d string) string { before, _, _ := strings.Cut(d, "COPY <<'TABULARIA_PACKAGES'"); return before }
	if layers(d) == "" || layers(d) != layers(other) {
		t.Errorf("the base and system layers differ:\n%s\n----\n%s", layers(d), layers(other))
	}
	code, err := CodeFiles("experiments", nil, true)
	if err != nil {
		t.Fatal(err)
	}
	run := string(mustFile(t, code, "runner/run.R"))
	if strings.Contains(run, "predict = ") || !strings.Contains(run, `tab_main("experiments"`) {
		t.Errorf("run.R:\n%s", run)
	}
	// both nodes are built (the A/B test in lot E2, the planning in lot E5) and write no card
	for _, node := range []string{"ab_test", "ab_plan"} {
		if s := string(mustFile(t, code, "runner/R/"+node+".R")); strings.Contains(s, "tab_not_built(") || !strings.Contains(s, "card = NULL") {
			t.Errorf("%s.R: not built, or it writes a card", node)
		}
	}
	if !strings.Contains(string(mustFile(t, code, "runner/R/ab_data.R")), "options(arrow.int64_downcast = FALSE)") {
		t.Error("the runner does not read an Int64 as it is")
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

// R writes an empty list as [] : a refusal with nothing to name still says why.
func TestARefusalWithEmptyParamsIsRead(t *testing.T) {
	for _, params := range []string{`[]`, `{}`, `null`} {
		log := "TABULARIA_ERROR {\"code\":\"no_repeaters\",\"message\":\"no customer bought again\",\"params\":" + params + "}\n"
		e, ok := ErrorFromLog(log)
		if !ok || e.Code != "no_repeaters" || e.Message != "no customer bought again" || len(e.Params) != 0 {
			t.Errorf("params %s: %+v %v", params, e, ok)
		}
	}
	if e, ok := ErrorFromLog("TABULARIA_ERROR {\"code\":\"x\",\"message\":\"m\",\"params\":5}\n"); !ok || e.Message != "m" {
		t.Error("odd params must not hide the sentence")
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
	vendoredPackage := "groups/foundation_forecast/runner/py/timestral"
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
				// the vendored package alone is embedded with the `all:` prefix: its `__init__.py` is in the Job's files
				if (strings.HasPrefix(d.Name(), "_") || strings.HasPrefix(d.Name(), ".")) && !strings.HasPrefix(filepath.ToSlash(p), vendoredPackage+"/") {
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

// A version's Jobs are made only by a gateway that ships the recipe and the runner it was queued with.
func TestAVersionIsTheGroupsOnlyWithItsRecipeAndItsRunner(t *testing.T) {
	g, _ := Get("timeseries")
	recipe, runner, other := g.RecipeHash, g.RunnerHash, "of-another-release"
	for _, c := range []struct {
		name           string
		recipe, runner *string
		want           bool
	}{
		{"both shipped", &recipe, &runner, true},
		{"another recipe", &other, &runner, false},
		{"another runner", &recipe, &other, false},
		{"recorded without them", nil, nil, false},
		{"without the runner", &recipe, nil, false},
	} {
		if got := g.Ships(c.recipe, c.runner); got != c.want {
			t.Errorf("%s: %v", c.name, got)
		}
	}
	if (Group{}).Ships(&recipe, &runner) {
		t.Error("a group not shipped ships a version")
	}
}

// Swap is another release's groups until restored, and gives back the shipped ones after.
func TestSwapIsAnotherReleaseUntilRestored(t *testing.T) {
	before, _ := Get("timeseries")
	other := Groups()
	for i := range other {
		if other[i].ID == "timeseries" {
			other[i].RecipeHash, other[i].Recipe.SystemLibraries = "of-another-release", nil
		}
	}
	restore := Swap(other)
	other[0].ID = "changed after the swap" // the swap holds its own copy
	if g, ok := Get("timeseries"); !ok || g.RecipeHash != "of-another-release" || len(g.Recipe.SystemLibraries) != 0 {
		t.Errorf("swapped: %+v", g.Recipe)
	}
	if Groups()[0].ID == "changed after the swap" {
		t.Error("the swap shares the caller's slice")
	}
	restore()
	if g, _ := Get("timeseries"); !reflect.DeepEqual(g, before) {
		t.Errorf("not restored: %s", g.RecipeHash)
	}
}

// ── the foundation forecast group: wheels, weights, the vendored package (docs/specs/foundation-forecast.md) ──────────────────────

func TestTheFoundationForecastRecipe(t *testing.T) {
	g, ok := Get("foundation_forecast")
	if !ok || g.Recipe.Language != "python" || g.Recipe.Base != "3.14" || strings.Join(g.Models, ",") != "timestral" {
		t.Fatalf("the group: %+v", g)
	}
	r := g.Recipe
	// torch's CPU build by its address and its sha256, for the two machines; no CUDA package among the packages
	if len(r.Wheels) != 2 || r.Wheels[0].Machine != "x86_64" || r.Wheels[1].Machine != "aarch64" {
		t.Fatalf("the wheels: %+v", r.Wheels)
	}
	for _, w := range r.Wheels {
		if w.Name != "torch" || w.Version != "2.14.1+cpu" || !strings.HasPrefix(w.URL, OfficialWheelsBaseURL+"torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_"+w.Machine+".whl") {
			t.Errorf("the wheel: %+v", w)
		}
	}
	for _, p := range r.Packages {
		if strings.HasPrefix(p, "torch") || strings.HasPrefix(p, "nvidia") || strings.HasPrefix(p, "triton") || strings.HasPrefix(p, "huggingface") ||
			strings.HasPrefix(p, "scipy") || strings.HasPrefix(p, "statsmodels") {
			t.Errorf("%s is no package of this recipe", p)
		}
	}
	// the weights: four sizes at the revisions of the spec, each with its three files, the LICENSE of each the licence's text
	w := r.Weights
	if w == nil || w.Licence.SHA256 != "648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c" || w.Licence.Text != "licence/LICENSE.txt" {
		t.Fatalf("the weights: %+v", w)
	}
	want := map[string][3]string{
		"5m":   {"mistralai/Timestral-5M", "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f", "7ace108c4987e7f2a99a75a445240019d1d31c2117139d1bd05ffdfd25aca738"},
		"28m":  {"mistralai/Timestral-28M", "49b26409ac87b5935539a5d8ffe7f397b720db47", "dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d"},
		"123m": {"mistralai/Timestral-123M", "5a147287addda85f6acd8da3fb8858fe9af7e8f7", "599fafa73cadbbc800b5a346ca900fa741ee3e5fd22e3b4a4430e151e74f4bc2"},
		"209m": {"mistralai/Timestral-209M", "9874d252ab0aae16f7f7c999f98f98d6ad7d8365", "1456effb9927e3daa8c7234f10497e3424233869245a9c72b8ecebaf7ad2b7fa"},
	}
	bytes := map[string]int64{"5m": 20843960, "28m": 111296464, "123m": 490445904, "209m": 837271648}
	if len(w.Sizes) != 4 {
		t.Fatalf("sizes: %v", w.Sizes)
	}
	for size, x := range want {
		got := w.Sizes[size]
		if got.Repo != x[0] || got.Revision != x[1] || got.Files["model.safetensors"].SHA256 != x[2] || got.Files["model.safetensors"].Bytes != bytes[size] ||
			got.Files["LICENSE"].SHA256 != w.Licence.SHA256 || got.Files["LICENSE"].Bytes != 1695 {
			t.Errorf("%s: %+v", size, got)
		}
	}
}

// checkDownloads refuses a recipe that names a download it cannot check: each failure is one sentence.
func TestARecipeThatDownloadsIsChecked(t *testing.T) {
	g, _ := Get("foundation_forecast")
	clone := func() Recipe {
		raw, err := files.ReadFile("groups/foundation_forecast/recipe.json")
		if err != nil {
			t.Fatal(err)
		}
		var r Recipe
		if err := json.Unmarshal(raw, &r); err != nil {
			t.Fatal(err)
		}
		return r
	}
	if err := clone().checkDownloads(g.ID); err != nil {
		t.Fatal(err)
	}
	for what, mutate := range map[string]func(r *Recipe){
		"an R image":            func(r *Recipe) { r.Language = "r" },
		"a wheel over http":     func(r *Recipe) { r.Wheels[0].URL = strings.Replace(r.Wheels[0].URL, "https://", "http://", 1) },
		"a wheel with a hash":   func(r *Recipe) { r.Wheels[0].URL += "#sha256=" + r.Wheels[0].SHA256 },
		"a wheel's sha256":      func(r *Recipe) { r.Wheels[0].SHA256 = r.Wheels[0].SHA256[:63] },
		"a wheel's machine":     func(r *Recipe) { r.Wheels[0].Machine = "riscv64" },
		"a wheel of the other":  func(r *Recipe) { r.Wheels[1].Machine = "x86_64" },
		"a wheel given twice":   func(r *Recipe) { r.Wheels = append(r.Wheels, r.Wheels[0]) },
		"a size":                func(r *Recipe) { r.Weights.Sizes["7m"] = r.Weights.Sizes["5m"] },
		"a revision that moves": func(r *Recipe) { x := r.Weights.Sizes["5m"]; x.Revision = "main"; r.Weights.Sizes["5m"] = x },
		"a repository":          func(r *Recipe) { x := r.Weights.Sizes["5m"]; x.Repo = "Timestral-5M"; r.Weights.Sizes["5m"] = x },
		"a file's sha256": func(r *Recipe) {
			x := r.Weights.Sizes["5m"]
			x.Files = map[string]WeightFile{"model.safetensors": {Bytes: 1, SHA256: "nope"}, "config.json": x.Files["config.json"], "LICENSE": x.Files["LICENSE"]}
			r.Weights.Sizes["5m"] = x
		},
		"a file the group does not know": func(r *Recipe) {
			x := r.Weights.Sizes["5m"]
			x.Files = map[string]WeightFile{"model.safetensors": x.Files["model.safetensors"], "config.json": x.Files["config.json"],
				"LICENSE": x.Files["LICENSE"], "pytorch_model.bin": {Bytes: 1, SHA256: x.Files["LICENSE"].SHA256}}
			r.Weights.Sizes["5m"] = x
		},
		"a licence sha256 that is not the text's": func(r *Recipe) { r.Weights.Licence.SHA256 = r.Weights.Sizes["5m"].Files["config.json"].SHA256 },
		"a LICENSE that is not the licence's": func(r *Recipe) {
			x := r.Weights.Sizes["5m"]
			x.Files = map[string]WeightFile{"model.safetensors": x.Files["model.safetensors"], "config.json": x.Files["config.json"],
				"LICENSE": x.Files["config.json"]}
			r.Weights.Sizes["5m"] = x
		},
		"a licence text that is not shipped": func(r *Recipe) { r.Weights.Licence.Text = "licence/nope.txt" },
		"no size":                            func(r *Recipe) { r.Weights.Sizes = map[string]WeightSize{} },
	} {
		r := clone()
		mutate(&r)
		if err := r.checkDownloads(g.ID); err == nil {
			t.Errorf("%s: no error", what)
		}
	}
}

// The Dockerfile of the foundation forecast group: torch's wheels by address and checksum, the packages, the weights of the sizes
// asked for downloaded and checked (every size when none is asked for), the mirrors; and every other group's Dockerfile is what the
// scripting machinery makes of its recipe, with nothing more.
func TestTheFoundationForecastDockerfile(t *testing.T) {
	s := scriptimage.Settings{Jobs: 2}
	d, err := Dockerfile("foundation_forecast", scriptcfg.DefaultBaseImages, s)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM python:3.14-slim\n", "ARG TARGETARCH\n",
		`RUN if [ "$TARGETARCH" = "amd64" ]; then pip install --no-deps --only-binary=:all: "https://download.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_x86_64.whl#sha256=35103e180214793207f95d7e12e100ecba3ace6f5fb37e1fcefbb07e096ed180"; fi` + "\n",
		`RUN if [ "$TARGETARCH" = "arm64" ]; then pip install --no-deps --only-binary=:all: "https://download.pytorch.org/whl/cpu/torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_aarch64.whl#sha256=f56cd23a3bf003bfbed3da0645ba09b3ccc66c0a2f2ac726a9b98f34e0e4926f"; fi` + "\n",
		"RUN pip install --no-deps --only-binary=:all: -r /opt/tabularia/requirements.txt && pip check\n",
		"ADD --checksum=sha256:dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d --chmod=0644 https://huggingface.co/mistralai/Timestral-28M/resolve/49b26409ac87b5935539a5d8ffe7f397b720db47/model.safetensors /opt/tabularia/weights/28m/model.safetensors\n",
		"ADD --checksum=sha256:648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c --chmod=0644 https://huggingface.co/mistralai/Timestral-5M/resolve/bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f/LICENSE /opt/tabularia/weights/5m/LICENSE\n",
		"\nnumpy==2.5.5\n", "USER 10001\n"} {
		if !strings.Contains(d, want) {
			t.Errorf("no %q in\n%s", want, d)
		}
	}
	// the folders exist, with the mode of a folder, before the files are added (an ADD with --chmod would give them the file's)
	if !strings.Contains(d, "RUN mkdir -p /opt/tabularia/weights/5m /opt/tabularia/weights/28m /opt/tabularia/weights/123m /opt/tabularia/weights/209m\nADD --checksum=") {
		t.Errorf("the folders of the weights are not made before the files:\n%s", d)
	}
	if n := strings.Count(d, "ADD --checksum="); n != 12 {
		t.Errorf("%d downloads: every size has three files", n)
	}
	// the order: the wheels, the requirements, the weights, the user
	order := []string{"ARG TARGETARCH", "pip install --no-deps --only-binary=:all: -r", "ADD --checksum", "USER 10001"}
	at := -1
	for _, mark := range order {
		i := strings.Index(d, mark)
		if i <= at {
			t.Errorf("%q is not after the step before it", mark)
		}
		at = i
	}
	// the sizes of a version: its line says them, the recipe's order is kept whatever the line's
	two, err := DockerfileFor("foundation_forecast", scriptcfg.DefaultBaseImages, s, Build{Sizes: []string{"28m", "5m"}})
	if err != nil || strings.Count(two, "ADD --checksum=") != 6 || strings.Contains(two, "Timestral-123M") || strings.Contains(two, "Timestral-209M") {
		t.Errorf("two sizes: %v\n%s", err, two)
	}
	for _, bad := range [][]string{{"7m"}, {"28m", "28m"}, {"28M"}} {
		if _, err := DockerfileFor("foundation_forecast", scriptcfg.DefaultBaseImages, s, Build{Sizes: bad}); err == nil {
			t.Errorf("sizes %v: no error", bad)
		}
	}
	// the mirrors replace the hosts and keep the paths, the file names and the checksums
	m, err := DockerfileFor("foundation_forecast", scriptcfg.DefaultBaseImages, s,
		Build{Sizes: []string{"5m"}, WheelsBaseURL: "https://mirror.example/torch", WeightsBaseURL: "https://hub.mirror.example/"})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"https://mirror.example/torch/torch-2.14.1%2Bcpu-cp314-cp314-manylinux_2_28_x86_64.whl#sha256=35103e18`,
		"--chmod=0644 https://hub.mirror.example/mistralai/Timestral-5M/resolve/bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f/model.safetensors /opt/tabularia/weights/5m/model.safetensors\n"} {
		if !strings.Contains(m, want) {
			t.Errorf("no %q in\n%s", want, m)
		}
	}
	if strings.Contains(m, "download.pytorch.org") || strings.Contains(m, "huggingface.co") {
		t.Errorf("an official host with a mirror:\n%s", m)
	}
	// every other group: what the scripting machinery makes of its recipe, nothing more (no wheel, no file, no ARG)
	for _, other := range Groups() {
		if other.ID == "foundation_forecast" {
			continue
		}
		got, err := Dockerfile(other.ID, scriptcfg.DefaultBaseImages, scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2})
		if err != nil {
			t.Fatal(err)
		}
		base, _ := BaseImage(other.Recipe, scriptcfg.DefaultBaseImages)
		want, _ := scriptimage.Dockerfile(other.Recipe.Language, base, other.Recipe.Packages,
			scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2, Managed: true, SystemLibraries: other.Recipe.SystemLibraries})
		if got != want || strings.Contains(got, "TARGETARCH") || strings.Contains(got, "ADD --checksum") || len(other.Recipe.Wheels) != 0 || other.Recipe.Weights != nil {
			t.Errorf("%s: its Dockerfile changed", other.ID)
		}
		if w, f, err := other.BuildInputs(Build{}); err != nil || len(f) != 0 || len(w) != 0 {
			t.Errorf("%s: build inputs %v %v %v", other.ID, w, f, err)
		}
		if _, _, err := other.BuildInputs(Build{Sizes: []string{"28m"}}); err == nil {
			t.Errorf("%s: sizes of no weights", other.ID)
		}
	}
}

// The vendored package: the nine files of the commit named in vendor.json, byte for byte but for the one moved import of loading.py, whose
// hash is the patched one's; with Mistral AI's LICENSE and the NOTICE that says what was changed (Apache 2.0, §4).
func TestTheVendoredTimestralPackageIsTheCommitsButForOneImport(t *testing.T) {
	const dir = "groups/foundation_forecast/runner/py/timestral/"
	raw, err := files.ReadFile(dir + "vendor.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Source, Commit, Package, Version, Licence string
		Files                                     map[string]struct{ Upstream_sha256, Sha256 string }
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if v.Commit != "a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff" || v.Version != "1.0.0" || v.Licence != "Apache-2.0" || len(v.Files) != 9 {
		t.Fatalf("vendor.json: %+v", v)
	}
	for name, h := range v.Files {
		b, err := files.ReadFile(dir + name)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if sum := sha256.Sum256(b); hex.EncodeToString(sum[:]) != h.Sha256 {
			t.Errorf("%s is not the file vendor.json hashes", name)
		}
		// the files are the commit's, but loading.py
		if (h.Upstream_sha256 == h.Sha256) == (name == "loading.py") {
			t.Errorf("%s: upstream %s, vendored %s", name, h.Upstream_sha256, h.Sha256)
		}
	}
	loading, _ := files.ReadFile(dir + "loading.py")
	head, _, _ := strings.Cut(string(loading), "def resolve_checkpoint")
	if strings.Contains(head, "huggingface_hub") || !strings.Contains(string(loading), "    from huggingface_hub import snapshot_download\n") {
		t.Errorf("loading.py imports huggingface_hub at the top of the module:\n%s", head)
	}
	notice, _ := files.ReadFile(dir + "NOTICE")
	if !strings.Contains(string(notice), "Modified by Tabularia: the import of huggingface_hub moved into resolve_checkpoint") {
		t.Error("NOTICE does not say the change")
	}
	licence, _ := files.ReadFile(dir + "LICENSE")
	if sum := sha256.Sum256(licence); hex.EncodeToString(sum[:]) != "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30" {
		t.Error("LICENSE is not Mistral AI's Apache 2.0 text")
	}
	// the other files of the package carry no import of the Hub: only loading.py ever did
	for name := range v.Files {
		if name == "loading.py" {
			continue
		}
		b, _ := files.ReadFile(dir + name)
		if strings.Contains(string(b), "huggingface_hub") {
			t.Errorf("%s imports huggingface_hub", name)
		}
	}
}

// The node's task: the forecast is the output the next step receives (`quantile_forecast`), written only when a node follows; the
// results table is the node's own. Nothing is registered: the card is the catalogue's `card: false`.
func TestTheFoundationForecastTask(t *testing.T) {
	g, _ := Get("foundation_forecast")
	params := map[string]any{"time": "day", "value": "orders", "frequency": "day", "series": "channel"}
	task, err := FitTask("timestral", params, true, 0)
	if err != nil || task.Group != "foundation_forecast" || task.Variant != "default" || task.Output != "quantile_forecast" || !task.WriteOutput ||
		task.RecipeHash != g.RecipeHash || task.Limits["max_series"] != 10_000 || task.Limits["max_foundation_horizon"] != 1024 {
		t.Errorf("the task: %+v %v", task, err)
	}
	if task, err := FitTask("timestral", params, false, 0); err != nil || task.WriteOutput {
		t.Errorf("no node after it: %+v %v", task, err)
	}
	if task, err := FitTask("timestral", map[string]any{"time": "day", "value": "orders", "frequency": "day", "output": "results"}, true, 0); err != nil || task.WriteOutput || task.Output != "results" {
		t.Errorf("the results table: %+v %v", task, err)
	}
	if _, err := FitTask("timestral", map[string]any{"time": "day", "value": "orders"}, true, 0); err == nil || err.Error() != "timestral: 'frequency' is required" {
		t.Errorf("a refusal of the catalogue: %v", err)
	}
}
