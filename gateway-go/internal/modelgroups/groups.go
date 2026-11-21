// Package modelgroups holds the groups of statistical models as Tabularia ships them
// (docs/specs/statistical-models.md, «Model groups and their images»): per group a recipe
// (recipe.json: language, base, packages pinned by version, resources, max_rows), a runner
// (an R group: runner/run.R, the dispatcher, and one file per model in runner/R/; a Python
// group: runner/run.py and its modules in runner/py/), reference cases (reference/expected.json
// and the small tables of reference/data/), plus the library every runner of a language shares
// (common/R/, common/py/), the self-test's table (common/selftest/) and the model card's schema
// (card.schema.json). Everything is embedded: the gateway builds a group's image from its recipe
// with the scripting machinery, and hands the runner's files to every model Job in its ConfigMap
// (CodeFiles).
//
// The protocol between a Job and Tabularia — task.json, the environment, the files written,
// the exit codes, the log's lines — is described in common/R/protocol.R and, the same for
// Python, common/py/tab_protocol.py; Task, the Env* and Exit* constants and the parsers of the
// log's lines are its Go side.
//
// //go:embed leaves out every file and folder whose name starts with «_» or «.»: no file of a
// runner or of a library may (a Python library is flat modules, without __init__.py; a test
// lists the source tree and checks that CodeFiles holds every file of it).
package modelgroups

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strings"
	"sync/atomic"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

//go:embed card.schema.json common groups
var files embed.FS

// Protocol is the version of task.json the runners speak (TAB_PROTOCOL in protocol.R).
const Protocol = 1

// CodeDir is where a model Job finds its files (the ConfigMap's mount, the working directory).
const CodeDir = "/tabularia/code"

// The environment of a model Job, besides the scripts' AWS_* (the scripts bucket's key).
const (
	EnvInput     = "TABULARIA_INPUT"     // the step's input, Parquet (s3://…)
	EnvOutput    = "TABULARIA_OUTPUT"    // rows or forecast, Parquet, when the node has nodes after it
	EnvResults   = "TABULARIA_RESULTS"   // the ten columns, Parquet: …/<step_id>/results.parquet
	EnvArtifacts = "TABULARIA_ARTIFACTS" // a prefix: …/<step_id>/artifacts/ (model.json, MLmodel, native.rds, run.json, error.json)
	EnvModelIn   = "TABULARIA_MODEL_IN"  // Predict: the model card to apply
	// EnvModelFileIn is Predict's model file (a boosting model's xgboost.json or catboost.cbm),
	// whose sha256 the card holds
	EnvModelFileIn = "TABULARIA_MODEL_FILE_IN"
)

// The exit codes of a runner: anything else is a crash (a bug), the log's tail says what.
const (
	ExitOK             = 0
	ExitRefused        = 3 // artifacts/error.json (and the log's TABULARIA_ERROR line) says why
	ExitSelftestFailed = 4
)

// The lines a runner prints for the gateway to read in its log.
const (
	LogError          = "TABULARIA_ERROR "
	LogSelftestReport = "TABULARIA_SELFTEST_REPORT "
)

// The languages of the groups, and the file a group's runner starts from.
var entries = map[string]string{"r": "runner/run.R", "python": "runner/run.py"}

// libraries are the folders of common/ a group of the language receives, besides common/selftest.
var libraries = map[string]string{"r": "common/R", "python": "common/py"}

// Commands are the container's commands of a group of the language, in CodeDir: a run, and the
// self-test. Python: -I ignores PYTHONPATH, the user's site and the script's folder (run.py puts
// exactly the library's and the runner's folders on sys.path), -B writes no bytecode (the code
// is a read-only ConfigMap).
func Commands(language string) (run, selftest []string) {
	if language == "python" {
		run = []string{"python", "-I", "-B", CodeDir + "/runner/run.py"}
	} else {
		run = []string{"Rscript", CodeDir + "/runner/run.R"}
	}
	return run, append(append([]string(nil), run...), "--selftest")
}

// Recipe is a group's recipe.json.
type Recipe struct {
	Language string   `json:"language"`
	Base     string   `json:"base"` // a version of scripts.baseImages (SCRIPTS__BASE_IMAGES)
	PinnedOn string   `json:"pinned_on"`
	Packages []string `json:"packages"` // R: name@version, dependencies first; Python: name==version, the whole closure
	// SystemLibraries: Debian packages an R image installs besides the fixed list every R image has
	// (scriptimage.RSystemLibraries); optional, and used by the customer lifetime group alone (GSL)
	SystemLibraries []string `json:"system_libraries,omitempty"`
	Resources       struct {
		CPU    string `json:"cpu"`    // the limit; the request is half
		Memory string `json:"memory"` // the limit; the request is half
	} `json:"resources"`
	MaxRows    int64 `json:"max_rows"`
	MaxSeconds int64 `json:"max_seconds"` // the longest run
}

// Group is a group as shipped: its recipe, its models and the hashes that say when its
// image must be built again (RecipeHash) or its self-test run again (RunnerHash,
// ReferenceHash).
type Group struct {
	ID            string
	Models        []string
	Recipe        Recipe
	RecipeHash    string // sha256 of recipe.json: a new one queues a new version (a build)
	RunnerHash    string // sha256 of the runner's files, the common library, the schema and the catalogue
	ReferenceHash string // sha256 of the reference cases
}

// Ships says whether a version of the group's image recorded with these hashes
// (script_environment_versions.recipe_hash and runner_hash, written when it was queued) is the group
// as this Tabularia ships it: the same recipe and the same runner. Only then may this Tabularia make
// the version's Jobs. The row holds the base image and the packages, but the build's Dockerfile takes
// the system libraries from the recipe shipped, and the check's self-test runs the runner shipped:
// a gateway of another release (the other replica of a rolling upgrade, the release a `helm rollback`
// went back to) would build and check another image than the one queued. A version recorded without
// them (before they existed), or of a group this Tabularia does not ship, is never its.
func (g Group) Ships(recipeHash, runnerHash *string) bool {
	return g.ID != "" && recipeHash != nil && runnerHash != nil && *recipeHash == g.RecipeHash && *runnerHash == g.RunnerHash
}

var shipped atomic.Pointer[[]Group]

func init() {
	groups := mustLoad()
	shipped.Store(&groups)
}

// Swap makes gs the groups shipped until restore is called: how a test is, on one database, a
// gateway of another release of Tabularia (another recipe or runner of a group) beside this one, as
// the two replicas of a rolling upgrade are. The gateway itself never calls it.
func Swap(gs []Group) (restore func()) {
	before := shipped.Load()
	gs = append([]Group(nil), gs...)
	shipped.Store(&gs)
	return func() { shipped.Store(before) }
}

// Groups are the groups shipped, in the catalogue's order.
func Groups() []Group {
	groups := *shipped.Load()
	out := make([]Group, len(groups))
	for i, g := range groups {
		out[i] = g
		out[i].Models = append([]string(nil), g.Models...)
		out[i].Recipe.Packages = append([]string(nil), g.Recipe.Packages...)
		out[i].Recipe.SystemLibraries = append([]string(nil), g.Recipe.SystemLibraries...)
	}
	return out
}

// Get is one group, false for none.
func Get(id string) (Group, bool) {
	for _, g := range Groups() {
		if g.ID == id {
			return g, true
		}
	}
	return Group{}, false
}

// CardSchema is card.schema.json, the model card's JSON Schema.
func CardSchema() []byte {
	b, _ := files.ReadFile("card.schema.json")
	return b
}

// File is one file of a model Job's code, its path under CodeDir.
type File struct {
	Path    string
	Content []byte
}

// CodeFiles are the files a model Job of the group finds in CodeDir, in a fixed order: the
// runner (runner/run.R and runner/R/*.R, or runner/run.py and runner/py/*.py), the library of
// the group's language (common/R/*.R or common/py/*.py), common/selftest/*, card.schema.json,
// catalog.json, then task.json when given, and the reference cases (reference/expected.json,
// reference/data/*) when withReference (the self-test). A ConfigMap holds them as
// f0, f1… with items mapping each key to its path, as a Script node's files.
func CodeFiles(group string, task []byte, withReference bool) ([]File, error) {
	g, ok := Get(group)
	if !ok {
		return nil, fmt.Errorf("no model group %q", group)
	}
	return codeFiles(group, g.Recipe.Language, task, withReference)
}

func codeFiles(group, language string, task []byte, withReference bool) ([]File, error) {
	var out []File
	add := func(root, prefix string) error {
		return fs.WalkDir(files, root, func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			b, err := files.ReadFile(p)
			if err != nil {
				return err
			}
			out = append(out, File{Path: prefix + strings.TrimPrefix(p, root+"/"), Content: b})
			return nil
		})
	}
	if err := add("groups/"+group+"/runner", "runner/"); err != nil {
		return nil, err
	}
	library, ok := libraries[language]
	if !ok {
		return nil, fmt.Errorf("the group %s is written in %q, which no library serves", group, language)
	}
	if err := add(library, library+"/"); err != nil {
		return nil, err
	}
	if err := add("common/selftest", "common/selftest/"); err != nil {
		return nil, err
	}
	out = append(out, File{Path: "card.schema.json", Content: CardSchema()}, File{Path: "catalog.json", Content: modelcatalog.Compact()})
	if task != nil {
		out = append(out, File{Path: "task.json", Content: task})
	}
	if withReference {
		ref := "groups/" + group + "/reference"
		b, err := files.ReadFile(ref + "/expected.json")
		if err != nil {
			return nil, err
		}
		out = append(out, File{Path: "reference/expected.json", Content: b})
		if _, err := fs.Stat(files, ref+"/data"); err == nil {
			if err := add(ref+"/data", "reference/data/"); err != nil {
				return nil, err
			}
		}
	}
	return out, nil
}

// ConfigMap is the data and the volume's items of CodeFiles: keys f0, f1… (a ConfigMap's
// key may hold no slash), each item mapping a key to its path.
func ConfigMap(code []File) (data map[string]string, items []map[string]string) {
	data = map[string]string{}
	for i, f := range code {
		k := fmt.Sprintf("f%d", i)
		data[k] = string(f.Content)
		items = append(items, map[string]string{"key": k, "path": f.Path})
	}
	return data, items
}

// BaseImage is the image the recipe's base names, among the scripting environments' base
// images (SCRIPTS__BASE_IMAGES, or Tabularia's defaults).
func BaseImage(r Recipe, images map[string][]scriptcfg.Image) (string, error) {
	for _, im := range images[r.Language] {
		if im.Version == r.Base {
			return im.Image, nil
		}
	}
	return "", fmt.Errorf("the base %s %s is not one of the base images offered (SCRIPTS__BASE_IMAGES)", r.Language, r.Base)
}

// Dockerfile is the group's image as the build loop of the scripting environments makes it for
// a managed environment: R, the base image, arrow, then the recipe's packages in their order;
// Python, the base image and the recipe's packages only, wheels without their dependencies (the
// recipe is the whole closure), then pip check.
func Dockerfile(group string, images map[string][]scriptcfg.Image, s scriptimage.Settings) (string, error) {
	g, ok := Get(group)
	if !ok {
		return "", fmt.Errorf("no model group %q", group)
	}
	base, err := BaseImage(g.Recipe, images)
	if err != nil {
		return "", err
	}
	s.Managed = true
	s.SystemLibraries = g.Recipe.SystemLibraries
	return scriptimage.Dockerfile(g.Recipe.Language, base, g.Recipe.Packages, s)
}

// ── task.json ────────────────────────────────────────────────────────────────

// IDs say which step of which run a task is, for the run page and MLflow's tags.
type IDs struct {
	FlowID    int64  `json:"flow_id,omitempty"`
	NodeID    string `json:"node_id,omitempty"`
	RunID     int64  `json:"run_id,omitempty"`
	StepRunID int64  `json:"step_run_id,omitempty"`
}

// Task is task.json, what a model Job is asked to do (protocol.R).
type Task struct {
	Protocol     int            `json:"protocol"`
	Mode         string         `json:"mode"` // fit | predict | selftest
	Group        string         `json:"group"`
	GroupVersion int64          `json:"group_version"`
	Model        string         `json:"model,omitempty"`
	Variant      string         `json:"variant,omitempty"`
	Params       map[string]any `json:"params"`
	Output       string         `json:"output,omitempty"` // results | rows | table | forecast
	WriteOutput  bool           `json:"write_output"`     // the node has nodes after it and output is not results
	RowsFrom     string         `json:"rows_from,omitempty"`
	MLflow       bool           `json:"mlflow"` // write MLmodel and native.rds
	Limits       map[string]int `json:"limits"`
	InputRows    int64          `json:"input_rows,omitempty"`
	IDs          IDs            `json:"ids"`
	RecipeHash   string         `json:"recipe_hash"`
	RunnerHash   string         `json:"runner_hash"`
}

// FitTask is the task of a model node: its parameters checked by the catalogue (a refusal
// is a *modelcatalog.Error with the sentence), its variant, its output, the catalogue's
// limits with maxChartPoints (MODELS__MAX_CHART_POINTS; 0 keeps the catalogue's).
// hasNext: the node has nodes after it.
func FitTask(model string, params map[string]any, hasNext bool, maxChartPoints int) (Task, error) {
	group := modelcatalog.GroupOf(model)
	g, ok := Get(group)
	if !ok {
		_, err := modelcatalog.Check(model, params)
		return Task{}, err
	}
	checked, err := modelcatalog.Check(model, params)
	if err != nil {
		return Task{}, err
	}
	variant := modelcatalog.VariantOf(model, checked)
	output, _ := checked["output"].(string)
	limits := modelcatalog.Limits()
	if maxChartPoints > 0 {
		limits["max_chart_points"] = maxChartPoints
	}
	return Task{Protocol: Protocol, Mode: "fit", Group: group, Model: model, Variant: variant.ID, Params: checked,
		Output: output, WriteOutput: hasNext && output != "" && output != "results", RowsFrom: "run", Limits: limits,
		RecipeHash: g.RecipeHash, RunnerHash: g.RunnerHash}, nil
}

// PredictTask is the task of a Predict step: the card's model and variant applied (mode
// predict) with the node's options checked by modelcatalog.CheckPredict. The rows always go to
// TABULARIA_OUTPUT: the input's rows with the columns added (`rows`), or an ARIMA's forecast
// (`forecast`). The card itself is TABULARIA_MODEL_IN, never in task.json.
func PredictTask(model, variant string, checked map[string]any) (Task, error) {
	group := modelcatalog.GroupOf(model)
	g, ok := Get(group)
	if !ok {
		return Task{}, &modelcatalog.Error{Message: fmt.Sprintf("there is no model '%s' in this version of Tabularia", model)}
	}
	if !modelcatalog.HasVariant(model, variant) {
		return Task{}, &modelcatalog.Error{Message: fmt.Sprintf("%s: this version of Tabularia has no variant '%s'", model, variant)}
	}
	output := "rows"
	if model == "arima" {
		output = "forecast"
	}
	params := map[string]any{}
	for k, v := range checked {
		params[k] = v
	}
	return Task{Protocol: Protocol, Mode: "predict", Group: group, Model: model, Variant: variant, Params: params, Output: output,
		WriteOutput: true, RowsFrom: "run", Limits: modelcatalog.Limits(), RecipeHash: g.RecipeHash, RunnerHash: g.RunnerHash}, nil
}

// JSON is the task as task.json holds it.
func (t Task) JSON() []byte {
	b, _ := json.Marshal(t)
	return b
}

// ── what a runner prints ─────────────────────────────────────────────────────

// RunnerError is artifacts/error.json, or the log's TABULARIA_ERROR line: a refusal.
type RunnerError struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Params  map[string]any `json:"params"`
}

// UnmarshalJSON reads a refusal whose params are an empty list: R's jsonlite writes an empty
// list as [], not {}, and a refusal with nothing to name (no_repeaters) must still say why.
func (e *RunnerError) UnmarshalJSON(b []byte) error {
	type plain RunnerError
	var raw struct {
		plain
		Params json.RawMessage `json:"params"`
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	*e = RunnerError(raw.plain)
	e.Params = nil
	if p := bytes.TrimSpace(raw.Params); len(p) > 0 && p[0] == '{' {
		if err := json.Unmarshal(p, &e.Params); err != nil {
			return err
		}
	}
	return nil
}

// SelftestReport is the self-test's last line.
type SelftestReport struct {
	Group    string            `json:"group"`
	OK       int               `json:"ok"`
	Failed   int               `json:"failed"`
	Skipped  int               `json:"skipped"`
	Software map[string]string `json:"software"`
	Checks   []struct {
		Name   string `json:"name"`
		Status string `json:"status"` // ok | failed | skipped
		Detail string `json:"detail"`
	} `json:"checks"`
}

// lastLine is the JSON after the last line of the log that starts with mark.
func lastLine(log, mark string, into any) bool {
	var found string
	sc := bufio.NewScanner(strings.NewReader(log))
	sc.Buffer(make([]byte, 0, 64*1024), 8<<20)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, mark) {
			found = strings.TrimPrefix(line, mark)
		}
	}
	return found != "" && json.Unmarshal([]byte(found), into) == nil
}

// ErrorFromLog is the refusal a runner printed, when it printed one.
func ErrorFromLog(log string) (*RunnerError, bool) {
	var e RunnerError
	if !lastLine(log, LogError, &e) || e.Message == "" {
		return nil, false
	}
	return &e, true
}

// SelftestFromLog is the self-test's report, when the log holds one. Passed: it ran and
// nothing failed.
func SelftestFromLog(log string) (*SelftestReport, bool) {
	var r SelftestReport
	if !lastLine(log, LogSelftestReport, &r) {
		return nil, false
	}
	return &r, true
}

// Passed says whether a self-test passed: no check failed, and none was skipped (an image
// skips nothing: arrow is there).
func (r *SelftestReport) Passed() bool {
	return r != nil && r.Failed == 0 && r.Skipped == 0 && r.OK > 0
}

// ── loading ──────────────────────────────────────────────────────────────────

func hashOf(parts []File) string {
	sort.Slice(parts, func(i, j int) bool { return parts[i].Path < parts[j].Path })
	h := sha256.New()
	for _, f := range parts {
		h.Write([]byte(f.Path))
		h.Write([]byte{0})
		h.Write(f.Content)
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func mustLoad() []Group {
	groups, err := load()
	if err != nil {
		panic("modelgroups: " + err.Error())
	}
	return groups
}

func load() ([]Group, error) {
	var out []Group
	for _, cg := range modelcatalog.Groups() {
		raw, err := files.ReadFile(path.Join("groups", cg.ID, "recipe.json"))
		if err != nil {
			return nil, fmt.Errorf("the group %s has no recipe: %v", cg.ID, err)
		}
		var r Recipe
		dec := json.NewDecoder(strings.NewReader(string(raw)))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&r); err != nil {
			return nil, fmt.Errorf("%s/recipe.json: %v", cg.ID, err)
		}
		if r.Language != cg.Language {
			return nil, fmt.Errorf("%s/recipe.json: language %q, the catalogue says %q", cg.ID, r.Language, cg.Language)
		}
		entry, known := entries[r.Language]
		if !known {
			return nil, fmt.Errorf("%s/recipe.json: language %q (r or python)", cg.ID, r.Language)
		}
		if len(r.SystemLibraries) > 0 && r.Language != "r" {
			return nil, fmt.Errorf("%s/recipe.json: system_libraries is for an R image", cg.ID)
		}
		if _, err := fs.Stat(files, path.Join("groups", cg.ID, entry)); err != nil {
			return nil, fmt.Errorf("the group %s has no %s", cg.ID, entry)
		}
		sum := sha256.Sum256(raw)
		g := Group{ID: cg.ID, Models: cg.Models, Recipe: r, RecipeHash: hex.EncodeToString(sum[:])}
		out = append(out, g)
	}
	for i := range out {
		code, err := codeFiles(out[i].ID, out[i].Recipe.Language, nil, true)
		if err != nil {
			return nil, err
		}
		var runner, reference []File
		for _, f := range code {
			if strings.HasPrefix(f.Path, "reference/") {
				reference = append(reference, f)
			} else {
				runner = append(runner, f)
			}
		}
		out[i].RunnerHash = hashOf(runner)
		out[i].ReferenceHash = hashOf(reference)
	}
	return out, nil
}
