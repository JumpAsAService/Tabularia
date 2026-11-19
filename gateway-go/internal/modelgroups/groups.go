// Package modelgroups holds the groups of statistical models as Tabularia ships them
// (docs/specs/statistical-models.md, «Model groups and their images»): per group a recipe
// (recipe.json: language, base, packages pinned by version, resources, max_rows), a runner
// (runner/run.R, the dispatcher, and one file per model in runner/R/), reference cases
// (reference/expected.json and the small tables of reference/data/), plus the R library every
// runner shares (common/R/) and the model card's schema (card.schema.json). Everything is
// embedded: the gateway builds a group's image from its recipe with the scripting machinery,
// and hands the runner's files to every model Job in its ConfigMap (CodeFiles).
//
// The protocol between a Job and Tabularia — task.json, the environment, the files written,
// the exit codes, the log's lines — is described in common/R/protocol.R; Task, the Env* and
// Exit* constants and the parsers of the log's lines are its Go side.
package modelgroups

import (
	"bufio"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"sort"
	"strings"

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

// RunCommand and SelftestCommand are the container's command, in CodeDir.
var (
	RunCommand      = []string{"Rscript", CodeDir + "/runner/run.R"}
	SelftestCommand = []string{"Rscript", CodeDir + "/runner/run.R", "--selftest"}
)

// Recipe is a group's recipe.json.
type Recipe struct {
	Language  string   `json:"language"`
	Base      string   `json:"base"` // a version of scripts.baseImages (SCRIPTS__BASE_IMAGES)
	PinnedOn  string   `json:"pinned_on"`
	Packages  []string `json:"packages"` // name@version, dependencies first
	Resources struct {
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

var shipped = mustLoad()

// Groups are the groups shipped, in the catalogue's order.
func Groups() []Group {
	out := make([]Group, len(shipped))
	for i, g := range shipped {
		out[i] = g
		out[i].Models = append([]string(nil), g.Models...)
		out[i].Recipe.Packages = append([]string(nil), g.Recipe.Packages...)
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

// CodeFiles are the files a model Job of the group finds in CodeDir, in a fixed order:
// runner/run.R, runner/R/*.R, common/R/*.R, common/selftest/*, card.schema.json,
// catalog.json, then task.json when given, and the reference cases (reference/expected.json,
// reference/data/*) when withReference (the self-test). A ConfigMap holds them as
// f0, f1… with items mapping each key to its path, as a Script node's files.
func CodeFiles(group string, task []byte, withReference bool) ([]File, error) {
	if _, ok := Get(group); !ok {
		return nil, fmt.Errorf("no model group %q", group)
	}
	return codeFiles(group, task, withReference)
}

func codeFiles(group string, task []byte, withReference bool) ([]File, error) {
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
	if err := add("common", "common/"); err != nil {
		return nil, err
	}
	out = append(out, File{Path: "card.schema.json", Content: CardSchema()}, File{Path: "catalog.json", Content: modelcatalog.Raw()})
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

// Dockerfile is the group's image as the build loop of the scripting environments makes it:
// the base image, arrow, then the recipe's packages in their order.
func Dockerfile(group string, images map[string][]scriptcfg.Image, s scriptimage.Settings) (string, error) {
	g, ok := Get(group)
	if !ok {
		return "", fmt.Errorf("no model group %q", group)
	}
	base, err := BaseImage(g.Recipe, images)
	if err != nil {
		return "", err
	}
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
		sum := sha256.Sum256(raw)
		g := Group{ID: cg.ID, Models: cg.Models, Recipe: r, RecipeHash: hex.EncodeToString(sum[:])}
		out = append(out, g)
	}
	for i := range out {
		code, err := codeFiles(out[i].ID, nil, true)
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
