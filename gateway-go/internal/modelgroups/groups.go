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
	"compress/gzip"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync/atomic"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// The vendored inference package of the group `foundation_forecast` is a Python package: its `__init__.py` starts with «_»,
// which a plain pattern leaves out (the `all:` prefix keeps it, for that folder only).
//
//go:embed card.schema.json common groups all:groups/foundation_forecast/runner/py/timestral
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
	// Hashes: for each package, the sha256 of the files the image may install for it, sorted. R: the one source tarball of the
	// version CRAN serves (current or archived); Python: every wheel of the version a Linux build of the base's Python may pick
	// (x86_64 and aarch64). The build downloads each file, refuses it unless its sha256 is one of these, and installs nothing
	// else (pip --require-hashes; R CMD INSTALL of the checked tarball). Written by `go run ./cmd/modelgroup pin <group>`.
	Hashes map[string][]string `json:"hashes"`
	// SystemLibraries: Debian packages an R image installs besides the fixed list every R image has
	// (scriptimage.RSystemLibraries); optional, and used by the customer lifetime group alone (GSL)
	SystemLibraries []string `json:"system_libraries,omitempty"`
	Resources       struct {
		CPU    string `json:"cpu"`    // the limit; the request is half
		Memory string `json:"memory"` // the limit; the request is half
		// Tmp: the default size of a model Job's /tmp, when the group's runs need more than the input's files (the group `bayes`:
		// CmdStan's CSVs and the draws, docs/specs/bayes-contract.md); empty for every other group
		Tmp string `json:"tmp,omitempty"`
	} `json:"resources"`
	// Wheels: files the image installs by their address, each checked by its sha256, besides Packages (the CPU-only build of
	// PyTorch, which is on no index a plain `pip install` would reach: docs/specs/foundation-forecast.md, «The recipe»); the
	// build installs the one of its machine. A Python group's only.
	Wheels []Wheel `json:"wheels,omitempty"`
	// Weights: the files of a pre-trained model the image downloads when the group is built, from pinned revisions and checked by
	// sha256, and the licence they come under. A Python group's only; nil for every other group.
	Weights *Weights `json:"weights,omitempty"`
	// Toolchain: Debian packages a Python image installs first, among a fixed list (scriptimage.Toolchain: g++, make); the C++
	// toolchain of the group `bayes`, which compiles Stan programs (docs/specs/bayes-image.md). A Python group's only.
	Toolchain []string `json:"toolchain,omitempty"`
	// Sources: source tarballs the image builds, one a machine (the build takes the one of its machine), each checked by its
	// sha256; only with CmdStan, which builds them. A Python group's only.
	Sources []Source `json:"sources,omitempty"`
	// CmdStan: how the image builds the source «cmdstan» (the fixed step of scriptimage)
	CmdStan *CmdStan `json:"cmdstan,omitempty"`
	// Compile and Cache: the defaults of the compile Job's limits and of the cache of compiled binaries (the administration card
	// shows where a default comes from; changing them needs no build). The group `bayes`'s only.
	Compile    *CompileLimits `json:"compile,omitempty"`
	Cache      *CacheLimits   `json:"cache,omitempty"`
	MaxRows    int64          `json:"max_rows"`
	MaxSeconds int64          `json:"max_seconds"` // the longest run
}

// Source is a source tarball of a recipe: its name and version, the machine (uname -m) it is for, its address and its sha256.
type Source struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Machine string `json:"machine"` // x86_64 or aarch64
	URL     string `json:"url"`
	SHA256  string `json:"sha256"`
}

// CmdStan is how an image builds CmdStan: with STAN_THREADS, with its precompiled header, with make's -j.
type CmdStan struct {
	Threads           bool `json:"threads"`
	PrecompiledHeader bool `json:"precompiled_header"`
	Jobs              int  `json:"jobs"` // 1 or 2: -j2 was killed by the kernel in the default 4 GiB build Pod (docs/specs/bayes-image.md)
}

// CompileLimits are the defaults of a compile Job: its CPU and memory limits, its longest run and its /tmp.
type CompileLimits struct {
	CPU     string `json:"cpu"`
	Memory  string `json:"memory"`
	Seconds int64  `json:"seconds"`
	Tmp     string `json:"tmp"`
}

// CacheLimits is the default limit of the cache of compiled binaries, in bytes.
type CacheLimits struct {
	MaxBytes int64 `json:"max_bytes"`
}

// Wheel is a wheel installed by its address: `pip install "<url>#sha256=<sha256>"`, for the machine (uname -m) it is for.
type Wheel struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Machine string `json:"machine"` // x86_64 or aarch64
	URL     string `json:"url"`
	SHA256  string `json:"sha256"`
}

// Weights are the pre-trained weights of a group: their licence and, per size, the repository, the revision and the files.
type Weights struct {
	Licence WeightsLicence        `json:"licence"`
	Sizes   map[string]WeightSize `json:"sizes"`
}

// WeightsLicence is the licence the weights are under: its name, the sha256 of its text, and where the text is shipped
// (a path under the group's folder: the bytes the administrator reads, the bytes that travel with every copy of the weights).
type WeightsLicence struct {
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
	Text   string `json:"text"`
}

// WeightSize is one size of the weights: the Hugging Face repository, the revision pinned and its files.
type WeightSize struct {
	Repo     string                `json:"repo"`
	Revision string                `json:"revision"`
	Files    map[string]WeightFile `json:"files"`
}

// WeightFile is a file of a size: its length and its sha256.
type WeightFile struct {
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

// WeightSizes are the sizes a recipe's weights may have, from the smallest; the files every size holds.
var (
	WeightSizes = []string{"5m", "28m", "123m", "209m"}
	WeightFiles = []string{"config.json", "model.safetensors", "LICENSE"}
)

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

// LicenceText is the text of the licence the group's weights come under, the bytes shipped with this Tabularia (their sha256 is
// the recipe's `licence.sha256`: load checks it); "" for a group without weights.
func (g Group) LicenceText() string {
	if g.Recipe.Weights == nil {
		return ""
	}
	b, _ := files.ReadFile(path.Join("groups", g.ID, g.Recipe.Weights.Licence.Text))
	return string(b)
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
		out[i].Recipe.Hashes = make(map[string][]string, len(g.Recipe.Hashes))
		for k, v := range g.Recipe.Hashes {
			out[i].Recipe.Hashes[k] = append([]string(nil), v...)
		}
		out[i].Recipe.SystemLibraries = append([]string(nil), g.Recipe.SystemLibraries...)
		out[i].Recipe.Wheels = append([]Wheel(nil), g.Recipe.Wheels...)
		out[i].Recipe.Toolchain = append([]string(nil), g.Recipe.Toolchain...)
		out[i].Recipe.Sources = append([]Source(nil), g.Recipe.Sources...)
		if g.Recipe.CmdStan != nil {
			c := *g.Recipe.CmdStan
			out[i].Recipe.CmdStan = &c
		}
		if g.Recipe.Compile != nil {
			c := *g.Recipe.Compile
			out[i].Recipe.Compile = &c
		}
		if g.Recipe.Cache != nil {
			c := *g.Recipe.Cache
			out[i].Recipe.Cache = &c
		}
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

// File is one file of a model Job's code, its path under CodeDir. Binary: Content is gzip (a reference file, Path ending in
// .gz), held in the ConfigMap's binaryData; every other file is text, in its data.
type File struct {
	Path    string
	Content []byte
	Binary  bool
}

// gzipped is b compressed with gzip, the same bytes every time for the same input: no name and no time in the header (MTIME 0),
// this Go's compress/flate at its best compression.
func gzipped(b []byte) []byte {
	var buf bytes.Buffer
	w, _ := gzip.NewWriterLevel(&buf, gzip.BestCompression)
	_, _ = w.Write(b)
	_ = w.Close()
	return buf.Bytes()
}

// CodeFiles are the files a model Job of the group finds in CodeDir, in a fixed order: the
// runner (runner/run.R and runner/R/*.R, or runner/run.py and runner/py/*.py), the library of
// the group's language (common/R/*.R or common/py/*.py), common/selftest/*, card.schema.json,
// catalog.json, then task.json when given, and the reference cases (reference/expected.json,
// reference/data/*, and the programs of reference/programs/* of a group that compiles some) when withReference (the self-test). A ConfigMap holds them as
// f0, f1… with items mapping each key to its path, as a Script node's files. The reference files are
// gzipped, as <path>.gz (Binary): a ConfigMap holds at most 1 MiB, and the reference tables are most of a
// self-test's; the runners read them through the common library (tab_ref_path, ref_path), which takes the
// .gz twin of a path. The runner's code stays text.
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
		// the programs a self-test compiles (the group `bayes`'s Stan programs); no other group has the folder
		if _, err := fs.Stat(files, ref+"/programs"); err == nil {
			if err := add(ref+"/programs", "reference/programs/"); err != nil {
				return nil, err
			}
		}
		for i := range out {
			if strings.HasPrefix(out[i].Path, "reference/") {
				out[i] = File{Path: out[i].Path + ".gz", Content: gzipped(out[i].Content), Binary: true}
			}
		}
	}
	return out, nil
}

// ConfigMap is the data, the binaryData (base64, as the API takes it) and the volume's items of
// CodeFiles: keys f0, f1… (a ConfigMap's key may hold no slash), each item mapping a key to its path.
// A text file is in data, a gzipped reference file in binaryData: the volume gives its bytes back as
// they were.
func ConfigMap(code []File) (data, binary map[string]string, items []map[string]string) {
	data, binary = map[string]string{}, map[string]string{}
	for i, f := range code {
		k := fmt.Sprintf("f%d", i)
		if f.Binary {
			binary[k] = base64.StdEncoding.EncodeToString(f.Content)
		} else {
			data[k] = string(f.Content)
		}
		items = append(items, map[string]string{"key": k, "path": f.Path})
	}
	return data, binary, items
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
// recipe is the whole closure), then pip check. A group with wheels or weights gets them too, every size
// of the weights (DockerfileFor says which sizes).
func Dockerfile(group string, images map[string][]scriptcfg.Image, s scriptimage.Settings) (string, error) {
	return DockerfileFor(group, images, s, Build{})
}

// Build is what a version's build adds to the recipe: the sizes of the weights it holds and the mirrors it downloads from.
type Build struct {
	// Sizes of the weights to download, among the recipe's: none (nil) is every size the recipe has, as a version recorded
	// without a `# tabularia:weights` line is built (docs/specs/foundation-forecast.md, «Changes to the machinery»).
	Sizes []string
	// WheelsBaseURL replaces the host of the recipe's wheels (MODELS__WHEELS_BASE_URL), WeightsBaseURL the Hugging Face host of
	// its weights (MODELS__WEIGHTS_BASE_URL), for a cluster that may not reach them; empty keeps the official ones. The paths,
	// the file names and the checksums stay: a mirror holds the same files.
	WheelsBaseURL, WeightsBaseURL string
	// SourcesBaseURL replaces the host of the recipe's sources (MODELS__SOURCES_BASE_URL: a mirror of GitHub's release
	// downloads); empty keeps GitHub. The path and the sha256 stay.
	SourcesBaseURL string
}

// The official hosts a mirror replaces.
const (
	OfficialWheelsBaseURL  = "https://download.pytorch.org/whl/cpu/"
	OfficialWeightsBaseURL = "https://huggingface.co/"
	OfficialSourcesBaseURL = "https://github.com/"
	// WeightsDir is where an image keeps the weights it holds: <WeightsDir>/<size>/{config.json,model.safetensors,LICENSE}
	WeightsDir = "/opt/tabularia/weights"
)

func withBase(address, official, mirror string) string {
	mirror = strings.TrimSpace(mirror)
	if mirror == "" || !strings.HasPrefix(address, official) {
		return address
	}
	return strings.TrimRight(mirror, "/") + "/" + strings.TrimPrefix(address, official)
}

// BuildInputs are the wheels and the files of the group's image: every wheel of the recipe (the Dockerfile picks the one of its
// machine) and the files of the sizes asked for, each with the checksum the recipe holds. An error names a size the recipe does
// not have, or asked for twice.
func (g Group) BuildInputs(b Build) ([]scriptimage.Wheel, []scriptimage.File, error) {
	var wheels []scriptimage.Wheel
	for _, w := range g.Recipe.Wheels {
		wheels = append(wheels, scriptimage.Wheel{Machine: w.Machine, URL: withBase(w.URL, OfficialWheelsBaseURL, b.WheelsBaseURL), SHA256: w.SHA256})
	}
	if g.Recipe.Weights == nil {
		if len(b.Sizes) > 0 {
			return nil, nil, fmt.Errorf("the group %s has no weights: no size to download", g.ID)
		}
		return wheels, nil, nil
	}
	sizes := b.Sizes
	if len(sizes) == 0 {
		for _, size := range WeightSizes {
			if _, ok := g.Recipe.Weights.Sizes[size]; ok {
				sizes = append(sizes, size)
			}
		}
	}
	seen := map[string]bool{}
	var files []scriptimage.File
	for _, size := range sizes {
		w, ok := g.Recipe.Weights.Sizes[size]
		if !ok || seen[size] {
			return nil, nil, fmt.Errorf("the group %s has no size «%s» of weights (or it is asked for twice)", g.ID, size)
		}
		seen[size] = true
		for _, name := range WeightFiles {
			files = append(files, scriptimage.File{
				URL:    withBase(OfficialWeightsBaseURL+w.Repo+"/resolve/"+w.Revision+"/"+name, OfficialWeightsBaseURL, b.WeightsBaseURL),
				SHA256: w.Files[name].SHA256, Path: WeightsDir + "/" + size + "/" + name})
		}
	}
	return wheels, files, nil
}

// DockerfileFor is Dockerfile for a version built with the sizes and the mirrors of b.
func DockerfileFor(group string, images map[string][]scriptcfg.Image, s scriptimage.Settings, b Build) (string, error) {
	g, ok := Get(group)
	if !ok {
		return "", fmt.Errorf("no model group %q", group)
	}
	base, err := BaseImage(g.Recipe, images)
	if err != nil {
		return "", err
	}
	s.Managed = true
	if s, err = g.Settings(s, b); err != nil {
		return "", err
	}
	return scriptimage.Dockerfile(g.Recipe.Language, base, g.Recipe.Packages, s)
}

// Settings are s with what the group's recipe adds to its image's build: the system libraries, the hashes, the wheels and the
// files of the weights (BuildInputs), the toolchain, the sources (their mirror applied) and the CmdStan step. The build loop of
// the gateway and cmd/modelgroup both make a group's Dockerfile with them.
func (g Group) Settings(s scriptimage.Settings, b Build) (scriptimage.Settings, error) {
	s.SystemLibraries = g.Recipe.SystemLibraries
	s.Hashes = g.Recipe.Hashes
	var err error
	if s.Wheels, s.Files, err = g.BuildInputs(b); err != nil {
		return s, err
	}
	s.Toolchain = g.Recipe.Toolchain
	s.Sources = nil
	for _, src := range g.Recipe.Sources {
		s.Sources = append(s.Sources, scriptimage.Source{Name: src.Name, Version: src.Version, Machine: src.Machine,
			URL: withBase(src.URL, OfficialSourcesBaseURL, b.SourcesBaseURL), SHA256: src.SHA256})
	}
	s.CmdStan = nil
	if c := g.Recipe.CmdStan; c != nil {
		s.CmdStan = &scriptimage.CmdStan{Threads: c.Threads, PrecompiledHeader: c.PrecompiledHeader, Jobs: c.Jobs}
	}
	return s, nil
}

// ── task.json ────────────────────────────────────────────────────────────────

// IDs say which step of which run a task is, for the run page and MLflow's tags.
type IDs struct {
	FlowID    int64  `json:"flow_id,omitempty"`
	NodeID    string `json:"node_id,omitempty"`
	RunID     int64  `json:"run_id,omitempty"`
	StepRunID int64  `json:"step_run_id,omitempty"`
}

// TaskLicence is who accepted the weights' licence and when (ISO, UTC): the audit row's actor and time.
type TaskLicence struct {
	By string `json:"by"`
	At string `json:"at"`
}

// Task is task.json, what a model Job is asked to do (protocol.R).
type Task struct {
	Protocol     int            `json:"protocol"`
	Mode         string         `json:"mode"` // fit | predict | selftest; the group bayes: compile | prior_predictive | sample | selftest
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
	// Licence is the acceptance of the weights' licence of the version in use (a group with weights): who and when
	Licence    *TaskLicence `json:"licence,omitempty"`
	RecipeHash string       `json:"recipe_hash"`
	RunnerHash string       `json:"runner_hash"`
	// Bayes is the group `bayes`'s object (docs/specs/bayes-contract.md, «task.json»); nil, and left out, for every other group,
	// whose task.json keeps its bytes
	Bayes *bayescontract.Task `json:"bayes,omitempty"`
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

var (
	sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// a revision of a Hugging Face repository is a commit
	revisionHex = regexp.MustCompile(`^[0-9a-f]{40}$`)
	repoName    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$`)
)

// checkDownloads checks what a recipe says it downloads: the wheels (an https address, 64 hexadecimal digits of sha256, a machine,
// the file named by the address), the weights' sizes (among WeightSizes, a repository, a commit, the three files with their
// sha256) and the licence (its text shipped under the group's folder and of the sha256 the recipe says, which the LICENSE of each
// size repeats: the text an administrator reads is, byte for byte, the file that travels with every copy of the weights).
func (r Recipe) checkDownloads(group string) error {
	fail := func(format string, a ...any) error {
		return fmt.Errorf("%s/recipe.json: %s", group, fmt.Sprintf(format, a...))
	}
	if (len(r.Wheels) > 0 || r.Weights != nil) && r.Language != "python" {
		return fail("wheels and weights are for a Python image")
	}
	machines := map[string]bool{}
	for _, w := range r.Wheels {
		if !strings.HasPrefix(w.URL, "https://") || strings.ContainsAny(w.URL, " \t\n\"'\\#`$") || w.Name == "" || w.Version == "" {
			return fail("the wheel «%s» %s has no https address, or a name or a version is missing", w.Name, w.URL)
		}
		if w.Machine != "x86_64" && w.Machine != "aarch64" {
			return fail("the wheel «%s»: machine «%s» (x86_64 or aarch64)", w.Name, w.Machine)
		}
		if machines[w.Name+" "+w.Machine] {
			return fail("the wheel «%s» is given twice for %s", w.Name, w.Machine)
		}
		machines[w.Name+" "+w.Machine] = true
		if !sha256Hex.MatchString(w.SHA256) {
			return fail("the sha256 of the wheel «%s» (%s) is not 64 hexadecimal digits", w.Name, w.Machine)
		}
		// the file the address names is the wheel of that name and version, for a machine of that name
		file := w.URL[strings.LastIndex(w.URL, "/")+1:]
		if !strings.HasSuffix(file, "_"+w.Machine+".whl") || !strings.HasPrefix(strings.ReplaceAll(file, "%2B", "+"), w.Name+"-"+w.Version+"-") {
			return fail("the wheel «%s» %s (%s) is not the file its address names", w.Name, w.Version, w.Machine)
		}
	}
	if r.Weights == nil {
		return nil
	}
	l := r.Weights.Licence
	if l.Name == "" || !sha256Hex.MatchString(l.SHA256) || l.Text == "" {
		return fail("the weights' licence needs a name, a sha256 and the path of its text")
	}
	text, err := files.ReadFile(path.Join("groups", group, l.Text))
	if err != nil {
		return fail("the text of the weights' licence (%s) is not shipped: %v", l.Text, err)
	}
	if sum := sha256.Sum256(text); hex.EncodeToString(sum[:]) != l.SHA256 {
		return fail("the text of the weights' licence (%s) is not the one its sha256 names", l.Text)
	}
	if len(r.Weights.Sizes) == 0 {
		return fail("the weights have no size")
	}
	for size, w := range r.Weights.Sizes {
		known := false
		for _, s := range WeightSizes {
			known = known || s == size
		}
		if !known {
			return fail("«%s» is not a size of weights (%s)", size, strings.Join(WeightSizes, ", "))
		}
		if !repoName.MatchString(w.Repo) || !revisionHex.MatchString(w.Revision) {
			return fail("the size %s needs a repository (org/name) and a revision (a commit of 40 hexadecimal digits)", size)
		}
		for _, name := range WeightFiles {
			f, ok := w.Files[name]
			if !ok || f.Bytes <= 0 || !sha256Hex.MatchString(f.SHA256) {
				return fail("the size %s needs the file %s with its length and its 64-digit sha256", size, name)
			}
		}
		if len(w.Files) != len(WeightFiles) {
			return fail("the size %s lists a file besides %s", size, strings.Join(WeightFiles, ", "))
		}
		if w.Files["LICENSE"].SHA256 != l.SHA256 || w.Files["LICENSE"].Bytes != int64(len(text)) {
			return fail("the LICENSE of the size %s is not the text the weights' licence ships", size)
		}
	}
	return nil
}

var (
	rPin  = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]*@[0-9][0-9.-]*$`)
	pyPin = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+$`)
)

// CheckHashes checks the recipe's hashes (docs/specs/statistical-models.md, «The supply chain»): every package is pinned to one
// version (R `name@version`, Python `name==version`) and has its sha256 in `hashes` (R one, the tarball's; Python one or more,
// its wheels'), as 64 lower-case hexadecimal digits, sorted and without repeats; `hashes` names no package the recipe does not
// install. Loading does not check them, so that `cmd/modelgroup pin` still starts on a recipe whose versions changed; a test
// checks every recipe shipped, and the Dockerfile of a managed image refuses a package without its hash (no image is built).
func (r Recipe) CheckHashes(group string) error {
	fail := func(format string, a ...any) error {
		return fmt.Errorf("%s/recipe.json: %s (go run ./cmd/modelgroup pin %s writes the hashes)", group, fmt.Sprintf(format, a...), group)
	}
	pin := pyPin
	if r.Language == "r" {
		pin = rPin
	}
	listed := map[string]bool{}
	for _, p := range r.Packages {
		if !pin.MatchString(p) {
			return fail("«%s» is not a package pinned to one version", p)
		}
		if listed[p] {
			return fail("the package «%s» is listed twice", p)
		}
		listed[p] = true
		sums := r.Hashes[p]
		if len(sums) == 0 {
			return fail("the package «%s» has no sha256 in hashes", p)
		}
		if r.Language == "r" && len(sums) != 1 {
			return fail("the package «%s» has %d sha256, an R package has one (its source tarball)", p, len(sums))
		}
		for i, sum := range sums {
			if !sha256Hex.MatchString(sum) {
				return fail("a sha256 of «%s» is not 64 lower-case hexadecimal digits", p)
			}
			if i > 0 && sums[i-1] >= sum {
				return fail("the sha256 of «%s» are not sorted, or one is repeated", p)
			}
		}
	}
	for p := range r.Hashes {
		if !listed[p] {
			return fail("hashes names «%s», which is not one of the packages", p)
		}
	}
	return nil
}

func mustLoad() []Group {
	groups, err := load()
	if err != nil {
		panic("modelgroups: " + err.Error())
	}
	return groups
}

// aheadOfCatalogue are the groups shipped before the models' catalogue names them: their image is built and self-tested as every
// group's, and no model of theirs runs yet. The group `bayes` (docs/specs/bayes-image.md, lot B0): its entry in the catalogue,
// the model `bayes_model`, comes with lot K0 of docs/specs/bayes-contract.md, which takes it out of here (a group the catalogue
// names is the catalogue's).
var aheadOfCatalogue = []modelcatalog.Group{{ID: "bayes", Language: "python", Label: "Bayesian models"}}

// catalogueGroups are the groups of the catalogue, then those shipped ahead of it that it does not name yet.
func catalogueGroups() []modelcatalog.Group {
	groups := modelcatalog.Groups()
	named := map[string]bool{}
	for _, g := range groups {
		named[g.ID] = true
	}
	for _, g := range aheadOfCatalogue {
		if !named[g.ID] {
			groups = append(groups, g)
		}
	}
	return groups
}

var (
	// a quantity of Kubernetes as a recipe writes a memory or a /tmp: whole mebibytes or gibibytes
	quantity = regexp.MustCompile(`^[1-9][0-9]*(Mi|Gi)$`)
	// a CPU as a recipe writes one: whole cores
	cores = regexp.MustCompile(`^[1-9][0-9]*$`)
)

// checkBuild checks what a recipe adds to the build of a Python image (docs/specs/bayes-image.md, «Changes to the machinery»): the
// toolchain's names among scriptimage.Toolchain, without repeats; the sources (a name and a version, x86_64 or aarch64, one a
// machine and one version a name, an https address of a .tar.gz, 64 hexadecimal digits of sha256, and one for x86_64), only with
// `cmdstan`, which needs a source named «cmdstan» and the toolchain's g++ and make, and runs make -j1 or -j2; the defaults of the
// compile and of the cache, and the /tmp of a run.
func (r Recipe) checkBuild(group string) error {
	fail := func(format string, a ...any) error {
		return fmt.Errorf("%s/recipe.json: %s", group, fmt.Sprintf(format, a...))
	}
	if (len(r.Toolchain) > 0 || len(r.Sources) > 0 || r.CmdStan != nil || r.Compile != nil || r.Cache != nil) && r.Language != "python" {
		return fail("toolchain, sources, cmdstan, compile and cache are for a Python image")
	}
	tools := map[string]bool{}
	for _, n := range r.Toolchain {
		if _, ok := scriptimage.Toolchain[n]; !ok || tools[n] {
			return fail("«%s» is not a package of the toolchain (g++, make), or it is named twice", n)
		}
		tools[n] = true
	}
	if (len(r.Sources) > 0) != (r.CmdStan != nil) {
		return fail("sources are built by the cmdstan step: the one needs the other")
	}
	versions, machines := map[string]string{}, map[string]bool{}
	cmdstan := false
	for _, s := range r.Sources {
		if s.Name == "" || s.Version == "" || strings.ContainsAny(s.Name+s.Version, " \t\n\"'\\/#`$") {
			return fail("a source needs a name and a version")
		}
		if s.Machine != "x86_64" && s.Machine != "aarch64" {
			return fail("the source «%s»: machine «%s» (x86_64 or aarch64)", s.Name, s.Machine)
		}
		if v, ok := versions[s.Name]; (ok && v != s.Version) || machines[s.Name+" "+s.Machine] {
			return fail("the source «%s» is given twice for %s, or at two versions", s.Name, s.Machine)
		}
		versions[s.Name], machines[s.Name+" "+s.Machine] = s.Version, true
		if !strings.HasPrefix(s.URL, "https://") || strings.ContainsAny(s.URL, " \t\n\"'\\#`$") || !strings.HasSuffix(s.URL, ".tar.gz") {
			return fail("the source «%s» (%s) has no https address of a .tar.gz", s.Name, s.Machine)
		}
		if !sha256Hex.MatchString(s.SHA256) {
			return fail("the sha256 of the source «%s» (%s) is not 64 hexadecimal digits", s.Name, s.Machine)
		}
		cmdstan = cmdstan || s.Name == "cmdstan"
	}
	for name := range versions {
		if !machines[name+" x86_64"] {
			return fail("the source «%s» has no tarball for x86_64", name)
		}
	}
	if c := r.CmdStan; c != nil {
		if !cmdstan || !tools["g++"] || !tools["make"] {
			return fail("cmdstan needs a source named «cmdstan» and the toolchain's g++ and make")
		}
		if c.Jobs < 1 || c.Jobs > 2 {
			return fail("cmdstan's jobs is %d: make runs -j1 or -j2 (-j2 needs more than the default 4 GiB build Pod)", c.Jobs)
		}
	}
	if c := r.Compile; c != nil && (!cores.MatchString(c.CPU) || !quantity.MatchString(c.Memory) || c.Seconds <= 0 || !quantity.MatchString(c.Tmp)) {
		return fail("compile needs a cpu (whole cores), a memory and a tmp (Mi or Gi) and its seconds")
	}
	if r.Cache != nil && r.Cache.MaxBytes <= 0 {
		return fail("the cache's max_bytes is not positive")
	}
	if r.Resources.Tmp != "" && !quantity.MatchString(r.Resources.Tmp) {
		return fail("resources' tmp «%s» is not a quantity of Mi or Gi", r.Resources.Tmp)
	}
	return nil
}

func load() ([]Group, error) {
	var out []Group
	for _, cg := range catalogueGroups() {
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
		if err := r.checkDownloads(cg.ID); err != nil {
			return nil, err
		}
		if err := r.checkBuild(cg.ID); err != nil {
			return nil, err
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
