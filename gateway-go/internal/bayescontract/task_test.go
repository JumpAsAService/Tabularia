package bayescontract_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayeschecks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// The files of testdata/ are written by these tests and read by the runner's (Python) tests; a change of the contract that is
// meant rewrites them:
//
//	BAYES_CONTRACT_WRITE=1 go test ./internal/bayescontract/
var write = os.Getenv("BAYES_CONTRACT_WRITE") == "1"

const goldenDir = "../bayescodegen/testdata/golden"

// manifestFile is a golden case's <case>.manifest.json of stan-codegen.
type manifestFile struct {
	SemanticHash  string                       `json:"semantic_hash"`
	ProgramSHA256 string                       `json:"program_sha256"`
	Data          []bayescontract.DataEntry    `json:"data"`
	Quantities    []bayescontract.Quantity     `json:"quantities"`
	Mapping       []bayescontract.MappingEntry `json:"-"`
	Plates        []bayescontract.Plate        `json:"-"`
}

func platesOf(m *bayesgraph.Model) []bayescontract.Plate {
	var out []bayescontract.Plate
	for _, p := range m.Plates {
		out = append(out, bayescontract.Plate{ID: p.ID, Kind: p.Kind, Label: p.Label})
	}
	return out
}

func modelOf(t *testing.T, data []byte) *bayesgraph.Model {
	t.Helper()
	g, fs := bayesgraph.Decode(data)
	if g == nil {
		t.Fatalf("the graph does not decode: %v", fs)
	}
	return bayesgraph.BuildModel(g)
}

// golden reads a case of stan-codegen's golden files: its manifests, its mapping, the plates of its graph.
func golden(t *testing.T, name string) manifestFile {
	t.Helper()
	read := func(suffix string, v any) {
		b, err := os.ReadFile(filepath.Join(goldenDir, name+suffix))
		if err != nil {
			t.Fatal(err)
		}
		if v != nil {
			if err := json.Unmarshal(b, v); err != nil {
				t.Fatalf("%s%s: %v", name, suffix, err)
			}
		}
	}
	var mf manifestFile
	read(".manifest.json", &mf)
	read(".mapping.json", &mf.Mapping)
	b, err := os.ReadFile(filepath.Join(goldenDir, name+".graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	mf.Plates = platesOf(modelOf(t, b))
	return mf
}

func goldenCases(t *testing.T) []string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(goldenDir, "*.manifest.json"))
	if err != nil || len(files) < 20 {
		t.Fatalf("stan-codegen's golden cases: %v (%d)", err, len(files))
	}
	var out []string
	for _, f := range files {
		out = append(out, strings.TrimSuffix(filepath.Base(f), ".manifest.json"))
	}
	return out
}

// Every golden case of stan-codegen (the extension's fake included) holds together as the contract says.
func TestTheGoldenManifestsHoldTogether(t *testing.T) {
	for _, name := range goldenCases(t) {
		mf := golden(t, name)
		if err := bayescontract.CheckManifests(mf.Plates, mf.Mapping, mf.Data, mf.Quantities); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

// Every valid graph of model-graph's corpus and of the opened templates gives manifests that hold together; a model the
// generator does not write (ErrUnsupported) is left out.
func TestTheCorpusManifestsHoldTogether(t *testing.T) {
	var graphs [][]byte
	var names []string
	corpus, _ := filepath.Glob("../bayesgraph/testdata/corpus/*.json")
	for _, f := range corpus {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var c struct {
			Input json.RawMessage `json:"input"`
		}
		if err := json.Unmarshal(b, &c); err != nil {
			t.Fatal(err)
		}
		if len(c.Input) > 0 && c.Input[0] == '{' {
			graphs, names = append(graphs, c.Input), append(names, filepath.Base(f))
		}
	}
	opened, _ := filepath.Glob("../bayestemplates/testdata/opened/*.json")
	for _, f := range opened {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		graphs, names = append(graphs, b), append(names, filepath.Base(f))
	}
	checked := 0
	for i, data := range graphs {
		g, _ := bayesgraph.Decode(data)
		if g == nil {
			continue
		}
		m := bayesgraph.BuildModel(g)
		if !m.Valid() {
			continue
		}
		p, err := bayescodegen.Generate(m, nil)
		if errors.Is(err, bayescodegen.ErrUnsupported) {
			continue
		}
		if err != nil {
			t.Errorf("%s: %v", names[i], err)
			continue
		}
		if err := bayescontract.CheckManifests(platesOf(m), p.Mapping, p.Data, p.Quantities); err != nil {
			t.Errorf("%s: %v", names[i], err)
		}
		checked++
	}
	if checked < 50 {
		t.Errorf("only %d valid graphs checked", checked)
	}
}

func TestBrokenManifestsAreRefused(t *testing.T) {
	base := golden(t, "extension_fake")
	cases := map[string]func(m *manifestFile){
		"N not first": func(m *manifestFile) { m.Data[0], m.Data[1] = m.Data[1], m.Data[0] },
		"no Mode": func(m *manifestFile) {
			m.Data = slices.DeleteFunc(m.Data, func(d bayescontract.DataEntry) bool { return d.Kind == "mode" })
		},
		"a size of nothing":           func(m *manifestFile) { m.Data[1].Of = "nowhere" },
		"an unknown kind":             func(m *manifestFile) { m.Data[3].Kind = "Category" },
		"a support on a real":         func(m *manifestFile) { m.Data[5].Support = "any" },
		"a column on a size":          func(m *manifestFile) { m.Data[0].Column = "x" },
		"a lag on a real":             func(m *manifestFile) { m.Data[5].Lag = 1 },
		"a quantity without its node": func(m *manifestFile) { m.Quantities[0].Node = "" },
		"an unknown role":             func(m *manifestFile) { m.Quantities[1].Role = "parameter" },
		"null dims":                   func(m *manifestFile) { m.Quantities[1].Dims = nil },
		"a dimension of two things": func(m *manifestFile) {
			m.Quantities[5].Dims = []bayescontract.Dim{{Plate: "grp", Size: "N"}}
		},
		"an unlisted plate":       func(m *manifestFile) { m.Plates = m.Plates[:1] },
		"a prior draw of nothing": func(m *manifestFile) { m.Quantities[7].Of = "nope" },
		"a name twice":            func(m *manifestFile) { m.Quantities[1].Stan = "y" },
		"a quantity not in mapping": func(m *manifestFile) {
			m.Mapping = slices.DeleteFunc(m.Mapping, func(e bayescontract.MappingEntry) bool { return e.Stan == "mu" })
		},
		"a plate listed twice": func(m *manifestFile) { m.Plates = append(m.Plates, m.Plates[0]) },
		// predictive-checks, lot P2: a check names a data node the data reads, a prior marginal is a single value of a
		// quantity, an extension's datum names a data node the data reads
		"a check of no outcome": func(m *manifestFile) {
			for i := range m.Quantities {
				if m.Quantities[i].Role == bayescontract.RoleCheck {
					m.Quantities[i].Of = "mu"
				}
			}
		},
		"a prior marginal with a dimension": func(m *manifestFile) { m.Quantities[7].Role = bayescontract.RolePriorMarginal },
		"a prior marginal of nothing": func(m *manifestFile) {
			m.Quantities[1].Role, m.Quantities[1].Of, m.Quantities[1].Node = bayescontract.RolePriorMarginal, "nope", ""
		},
		"an extension's datum of no data node": func(m *manifestFile) { m.Data[len(m.Data)-1].Node = "mu" },
		// the correction of 2026-11-24 from P3a: positions are among the rows or Rows_lag, which the data must hold
		"positions among a plate of groups":   func(m *manifestFile) { m.Data[len(m.Data)-1].Of = "grp" },
		"positions among Rows_lag without it": func(m *manifestFile) { m.Data[len(m.Data)-1].Of = bayescontract.RowsLag },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			var m manifestFile
			b, _ := json.Marshal(base)
			_ = json.Unmarshal(b, &m)
			m.Mapping = slices.Clone(base.Mapping)
			m.Plates = slices.Clone(base.Plates)
			mutate(&m)
			if err := bayescontract.CheckManifests(m.Plates, m.Mapping, m.Data, m.Quantities); err == nil {
				t.Error("accepted")
			}
		})
	}
}

// The header CmdStan writes, as measured on the image (lot K0): the sampler's fields, then column-major elements; a levels
// plate has K - 1 elements; a prior draw with a dimension has none in the sample mode.
func TestTheHeader(t *testing.T) {
	slopes := golden(t, "hier_slopes")
	got, err := bayescontract.Header(bayescontract.ModeSample, slopes.Quantities, bayescontract.Sizes{"rows": 5, "grp": 3})
	if err != nil {
		t.Fatal(err)
	}
	want := "lp__,accept_stat__,stepsize__,treedepth__,n_leapfrog__,divergent__,energy__,mu_a,mu_b,sigma,tau_a,tau_b," +
		"Z_eff.1.1,Z_eff.2.1,Z_eff.1.2,Z_eff.2.2,Z_eff.1.3,Z_eff.2.3,L_eff.1.1,L_eff.2.1,L_eff.1.2,L_eff.2.2," +
		"a_g.1,a_g.2,a_g.3,b_g.1,b_g.2,b_g.3,rho_g.1.1,rho_g.2.1,rho_g.1.2,rho_g.2.2"
	if strings.Join(got, ",") != want {
		t.Errorf("hier_slopes:\n got %s\nwant %s", strings.Join(got, ","), want)
	}
	fake := golden(t, "extension_fake")
	sizes := bayescontract.Sizes{"rows": 10, "grp": 2, "levels:c": 3, "Ppc_bins": 4}
	sample, err := bayescontract.Header(bayescontract.ModeSample, fake.Quantities, sizes)
	if err != nil {
		t.Fatal(err)
	}
	prior, err := bayescontract.Header(bayescontract.ModePriorPredictive, fake.Quantities, sizes)
	if err != nil {
		t.Fatal(err)
	}
	if s := strings.Join(sample[7:], ","); s != "b_c.1,b_c.2,b_x,mu,s,tau,a.1,a.2,Ppc_first_rep" {
		t.Errorf("extension_fake, sample: %s", s)
	}
	if s := strings.Join(prior, ","); s != "lp__,accept_stat__,b_c.1,b_c.2,b_x,mu,s,tau,a.1,a.2,Ppc_first_rep,Prior_a.1,Prior_a.2" {
		t.Errorf("extension_fake, prior_predictive: %s", s)
	}
	if _, err := bayescontract.Header(bayescontract.ModeSample, fake.Quantities, bayescontract.Sizes{"rows": 1}); err == nil {
		t.Error("a missing size was accepted")
	}
}

// goldenFile is a file of stan-codegen's golden cases.
func goldenFile(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(goldenDir, name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func hash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// withColumns is the data manifest as flow-node writes it: each entry that reads a column bound to one (here, its node's id).
func withColumns(data []bayescontract.DataEntry) []bayescontract.DataEntry {
	out := slices.Clone(data)
	for i := range out {
		if bayescontract.DataReadsColumn(out[i].Kind) {
			out[i].Column = "col_" + out[i].Node
		}
	}
	return out
}

func sampler(mode string) *bayescontract.Sampler {
	s := &bayescontract.Sampler{Chains: 4, Warmup: 1000, Draws: 1000, Seed: 20261123, AdaptDelta: 0.8, MaxTreedepth: 10,
		Refresh: 100}
	if mode == bayescontract.ModePriorPredictive {
		s.Warmup, s.Draws = 0, 500
	}
	return s
}

// exampleTasks are the task.json of each mode, as the gateway writes them.
func exampleTasks(t *testing.T) map[string]modelgroups.Task {
	t.Helper()
	limits := map[string]int{"time_budget_seconds": 1800, "memory_bytes": 2 << 30, "started_unix": 1795000000,
		"max_result_rows": modelcatalog.Limit("max_result_rows"), "max_chart_points": modelcatalog.Limit("max_chart_points")}
	for _, l := range bayescontract.Limits {
		limits[l.Key] = int(l.Default)
	}
	common := func(mode, variant, output string) modelgroups.Task {
		params := map[string]any{"mode": variant, "output": output}
		if mode == bayescontract.ModeCompile {
			params = map[string]any{}
		}
		return modelgroups.Task{Protocol: bayescontract.Protocol, Mode: mode, Group: bayescontract.Group, GroupVersion: 3,
			Model: bayescontract.Model, Variant: variant, Params: params, Output: output, WriteOutput: output != bayescontract.OutputResults && output != "",
			RowsFrom: "trial", Limits: limits, InputRows: 600, IDs: modelgroups.IDs{FlowID: 42, NodeID: "n7", RunID: 9811, StepRunID: 9812},
			RecipeHash: hash("recipe"), RunnerHash: hash("runner")}
	}
	// The examples' codegen_version stays 1 (the generator before predictive-checks' lot P1, whose
	// programs of these two cases are the same bytes): the runner's tests read these files as they
	// are (test_card.py). The runner's lot P3 of predictive-checks moves them to the current one.
	const exampleCodegenVersion = 1
	ref := func(mf manifestFile, binary bool) bayescontract.ModelRef {
		r := bayescontract.ModelRef{ID: 17, Version: 4, SemanticHash: mf.SemanticHash, ProgramSHA256: mf.ProgramSHA256,
			CodegenVersion: exampleCodegenVersion, CmdStan: "2.40.0"}
		if binary {
			r.BinarySHA256 = hash("the binary")
		}
		return r
	}
	slopes, fake := golden(t, "hier_slopes"), golden(t, "extension_fake")
	out := map[string]modelgroups.Task{}

	c := common(bayescontract.ModeCompile, bayescontract.ModeCompile, "")
	c.Bayes = &bayescontract.Task{Model: ref(slopes, false)}
	c.RowsFrom, c.InputRows = "", 0 // the compile reads no input
	out[bayescontract.ModeCompile] = c

	// bayes-runner's lot R4: the prior marginals with a finite variance and the original-scale pairs from the stored graph,
	// the card's program size and image
	withCard := func(r bayescontract.ModelRef, name string) bayescontract.ModelRef {
		r.ProgramBytes = int64(len(goldenFile(t, name+".stan")))
		r.Image = "registry:5000/tabularia-bayes@sha256:" + hash("the image")
		return r
	}
	s := common(bayescontract.ModeSample, bayescontract.ModeSample, bayescontract.OutputSummary)
	s.MLflow = true
	s.Bayes = &bayescontract.Task{Model: withCard(ref(slopes, true), "hier_slopes"), Sampler: sampler(bayescontract.ModeSample),
		Plates: slopes.Plates, Mapping: slopes.Mapping, Data: withColumns(slopes.Data), Quantities: slopes.Quantities,
		FiniteVariance: bayeschecks.FiniteVariance(modelOf(t, goldenFile(t, "hier_slopes.graph.json")))}
	out[bayescontract.ModeSample] = s

	// a sample with the original scale: a linear predictor's term and intercept
	linear := golden(t, "glm_linear")
	lm := modelOf(t, goldenFile(t, "glm_linear.graph.json"))
	sc := common(bayescontract.ModeSample, bayescontract.ModeSample, bayescontract.OutputDraws)
	sc.MLflow = true
	sc.Bayes = &bayescontract.Task{Model: withCard(ref(linear, true), "glm_linear"), Sampler: sampler(bayescontract.ModeSample),
		Plates: linear.Plates, Mapping: linear.Mapping, Data: withColumns(linear.Data), Quantities: linear.Quantities,
		FiniteVariance: bayeschecks.FiniteVariance(lm), ScalePairs: bayeschecks.ScalePairs(lm, linear.Data, linear.Quantities)}
	out["sample_scale"] = sc

	p := common(bayescontract.ModePriorPredictive, bayescontract.ModePriorPredictive, bayescontract.OutputResults)
	p.Bayes = &bayescontract.Task{Model: ref(fake, true), Sampler: sampler(bayescontract.ModePriorPredictive), Plates: fake.Plates,
		Mapping: fake.Mapping, Data: withColumns(fake.Data), Quantities: fake.Quantities,
		Checks: json.RawMessage(`{"statistics":["first"]}`)}
	out[bayescontract.ModePriorPredictive] = p

	st := common(bayescontract.ModeSelftest, "", "")
	st.Params, st.RowsFrom, st.InputRows, st.IDs = map[string]any{}, "run", 0, modelgroups.IDs{}
	out[bayescontract.ModeSelftest] = st
	return out
}

func indent(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(b, '\n')
}

// compareOrWrite compares a file of testdata/ with what the contract gives, or writes it.
func compareOrWrite(t *testing.T, path string, got []byte) {
	t.Helper()
	if write {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%s: %v (BAYES_CONTRACT_WRITE=1 writes it)", path, err)
	}
	if !bytes.Equal(want, got) {
		t.Errorf("%s is not what the contract gives: BAYES_CONTRACT_WRITE=1 go test ./internal/bayescontract/ if the change is meant", path)
	}
}

// The examples of testdata/tasks/ pass the gateway's check, read back to the same task, and are the files.
func TestTheExampleTasks(t *testing.T) {
	sizes := map[string]bayescontract.Sizes{
		bayescontract.ModeSample:          {"rows": 600, "grp": 20},
		"sample_scale":                    {"rows": 600},
		bayescontract.ModePriorPredictive: {"rows": 600, "grp": 20, "levels:c": 3, "Ppc_bins": 10},
	}
	for mode, task := range exampleTasks(t) {
		if task.Bayes != nil {
			if err := task.Bayes.Check(task.Mode, task.Output); err != nil {
				t.Errorf("%s: %v", mode, err)
			}
		}
		text := indent(t, task)
		var back modelgroups.Task
		if err := json.Unmarshal(text, &back); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(indent(t, back), text) {
			t.Errorf("%s: read back, the task is another", mode)
		}
		compareOrWrite(t, filepath.Join("testdata", "tasks", mode+".json"), text)
		if sz, ok := sizes[mode]; ok {
			header, err := bayescontract.Header(task.Mode, task.Bayes.Quantities, sz)
			if err != nil {
				t.Fatal(err)
			}
			compareOrWrite(t, filepath.Join("testdata", "tasks", mode+".header.json"),
				indent(t, map[string]any{"sizes": sz, "header": header}))
		}
	}
	tasks := exampleTasks(t)
	if task := tasks[bayescontract.ModeSelftest]; bytes.Contains(indent(t, task), []byte(`"bayes": `)) {
		t.Error("the self-test's task has a bayes object")
	}
	// lot R4's fields: absent where nothing is said, the examples' values
	for _, mode := range []string{bayescontract.ModeCompile, bayescontract.ModePriorPredictive} {
		for _, key := range []string{`"finite_variance"`, `"scale_pairs"`, `"program_bytes"`, `"image"`} {
			if bytes.Contains(indent(t, tasks[mode]), []byte(key)) {
				t.Errorf("%s: %s written", mode, key)
			}
		}
	}
	b, _ := json.Marshal(tasks["sample_scale"].Bayes)
	for _, part := range []string{`"finite_variance":["alpha","bx","sigma"]`,
		`"scale_pairs":[{"coef":"b_x","data":"x"},{"intercept":"alpha","lp":["b_x"],"data":["x"]}]`} {
		if !bytes.Contains(b, []byte(part)) {
			t.Errorf("sample_scale: %s not in %s", part, b)
		}
	}
	if fv := tasks[bayescontract.ModeSample].Bayes.FiniteVariance; len(fv) != 8 {
		t.Errorf("hier_slopes' finite variances: %v", fv)
	}
	// an empty list is written (decided: none), nil is not (not decided)
	none, _ := json.Marshal(bayescontract.Task{FiniteVariance: []string{}})
	if !bytes.Contains(none, []byte(`"finite_variance":[]`)) {
		t.Errorf("an empty finite_variance: %s", none)
	}
}

func TestTheChecksOfATask(t *testing.T) {
	tasks := exampleTasks(t)
	radius := 1.5
	minus := -1.0
	one := 1.0
	cases := []struct {
		name   string
		mode   string
		output string
		mutate func(b *bayescontract.Task)
	}{
		{"nine chains", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.Chains = 9 }},
		{"no draw", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.Draws = 0 }},
		{"warm-up in the prior predictive", "prior_predictive", "results", func(b *bayescontract.Task) { b.Sampler.Warmup = 10 }},
		{"adapt_delta of 1", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.AdaptDelta = 1 }},
		{"a seed beyond 32 bits", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.Seed = 1 << 32 }},
		{"a tree depth of 16", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.MaxTreedepth = 16 }},
		{"no binary hash", "sample", "summary", func(b *bayescontract.Task) { b.Model.BinarySHA256 = "" }},
		{"a short program hash", "sample", "summary", func(b *bayescontract.Task) { b.Model.ProgramSHA256 = "abc" }},
		{"an unknown output", "sample", "wide", func(*bayescontract.Task) {}},
		{"replicated without its thinning", "sample", "replicated", func(*bayescontract.Task) {}},
		{"a thinning beyond the draws", "sample", "replicated", func(b *bayescontract.Task) {
			b.Replicated = &bayescontract.Replicated{DrawsPerChain: 1001}
		}},
		{"a thinning without replicated", "sample", "summary", func(b *bayescontract.Task) {
			b.Replicated = &bayescontract.Replicated{DrawsPerChain: 10}
		}},
		{"checks that are a list", "sample", "summary", func(b *bayescontract.Task) { b.Checks = json.RawMessage(`[1]`) }},
		{"draws in the prior predictive", "prior_predictive", "draws", func(*bayescontract.Task) {}},
		{"a negative radius", "sample", "summary", func(b *bayescontract.Task) { b.Sampler.Init = &bayescontract.Init{Radius: &minus} }},
		{"an initial value of data", "sample", "summary", func(b *bayescontract.Task) {
			b.Sampler.Init = &bayescontract.Init{Values: map[string]bayescontract.InitValue{"y": {Number: &one}}}
		}},
		{"a number for a vector", "sample", "summary", func(b *bayescontract.Task) {
			b.Sampler.Init = &bayescontract.Init{Values: map[string]bayescontract.InitValue{"a_g": {Number: &one}}}
		}},
		{"a sampler in the compile mode", "compile", "", func(b *bayescontract.Task) { b.Sampler = &bayescontract.Sampler{} }},
		{"a manifest in the compile mode", "compile", "", func(b *bayescontract.Task) { b.Data = tasks["sample"].Bayes.Data }},
		{"a broken manifest", "sample", "summary", func(b *bayescontract.Task) { b.Quantities[0].Dims = []bayescontract.Dim{{Plate: "nope"}} }},
		// lot R4
		{"a finite variance of no quantity", "sample", "summary", func(b *bayescontract.Task) { b.FiniteVariance = []string{"nope"} }},
		{"a finite variance twice", "sample", "summary", func(b *bayescontract.Task) { b.FiniteVariance = []string{"mua", "mua"} }},
		{"a finite variance in the compile mode", "compile", "", func(b *bayescontract.Task) { b.FiniteVariance = []string{} }},
		{"a negative program size", "sample", "summary", func(b *bayescontract.Task) { b.Model.ProgramBytes = -1 }},
		{"a pair of a vector's coefficient", "sample", "summary", func(b *bayescontract.Task) {
			b.ScalePairs = []bayescontract.ScalePair{{Term: &bayescontract.TermPair{Coef: "a_g", Data: "x"}}}
		}},
		{"a pair of data not standardized", "sample", "summary", func(b *bayescontract.Task) {
			b.ScalePairs = []bayescontract.ScalePair{{Term: &bayescontract.TermPair{Coef: "mu_a", Data: "y"}}}
		}},
		{"a pair both a term and an intercept", "sample", "summary", func(b *bayescontract.Task) {
			b.ScalePairs = []bayescontract.ScalePair{{Term: &bayescontract.TermPair{Coef: "mu_a", Data: "x"},
				Intercept: &bayescontract.InterceptPair{LP: []string{"mu_a"}, Data: []string{"x"}}}}
		}},
		{"an intercept without terms", "sample", "summary", func(b *bayescontract.Task) {
			b.ScalePairs = []bayescontract.ScalePair{{Intercept: &bayescontract.InterceptPair{LP: []string{}, Data: []string{}}}}
		}},
		{"an intercept of nothing", "sample", "summary", func(b *bayescontract.Task) {
			nope := "nope"
			b.ScalePairs = []bayescontract.ScalePair{{Intercept: &bayescontract.InterceptPair{Intercept: &nope, LP: []string{"mu_a"}, Data: []string{"x"}}}}
		}},
		// lot F2 of flow-node: the series
		{"a series by a plate without a group index", "sample", "summary", func(b *bayescontract.Task) {
			nope := "nope"
			b.Series = &bayescontract.Series{By: &nope}
		}},
		{"a series' time without a name", "sample", "summary", func(b *bayescontract.Task) {
			empty := ""
			b.Series = &bayescontract.Series{Time: &empty}
		}},
		{"a series in the compile mode", "compile", "", func(b *bayescontract.Task) { b.Series = &bayescontract.Series{} }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var b bayescontract.Task
			src, _ := json.Marshal(tasks[c.mode].Bayes)
			if err := json.Unmarshal(src, &b); err != nil {
				t.Fatal(err)
			}
			c.mutate(&b)
			if err := b.Check(c.mode, c.output); err == nil {
				t.Error("accepted")
			}
		})
	}
	// and what passes
	var b bayescontract.Task
	src, _ := json.Marshal(tasks["sample"].Bayes)
	_ = json.Unmarshal(src, &b)
	b.Sampler.Init = &bayescontract.Init{Radius: &radius}
	if err := b.Check("sample", "summary"); err != nil {
		t.Error(err)
	}
	b.Sampler.Init = &bayescontract.Init{Values: map[string]bayescontract.InitValue{"mu_a": {Number: &one}, "a_g": {List: []float64{0, 1, 2}}}}
	b.Replicated = &bayescontract.Replicated{DrawsPerChain: 50}
	if err := b.Check("sample", "replicated"); err != nil {
		t.Error(err)
	}
	text, _ := json.Marshal(b.Sampler.Init)
	if string(text) != `{"a_g":[0,1,2],"mu_a":1}` {
		t.Errorf("init: %s", text)
	}
	// a varying intercept, without an intercept slot, read back
	ag := "a_g"
	b.ScalePairs = []bayescontract.ScalePair{{Term: &bayescontract.TermPair{Coef: "mu_a", Data: "x"}},
		{Intercept: &bayescontract.InterceptPair{Intercept: &ag, LP: []string{"mu_a"}, Data: []string{"x"}}},
		{Intercept: &bayescontract.InterceptPair{LP: []string{"mu_a"}, Data: []string{"x"}}}}
	if err := b.Check("sample", "replicated"); err != nil {
		t.Error(err)
	}
	text, _ = json.Marshal(b.ScalePairs)
	if string(text) != `[{"coef":"mu_a","data":"x"},{"intercept":"a_g","lp":["mu_a"],"data":["x"]},{"intercept":null,"lp":["mu_a"],"data":["x"]}]` {
		t.Errorf("scale pairs: %s", text)
	}
	var back []bayescontract.ScalePair
	if err := json.Unmarshal(text, &back); err != nil || len(back) != 3 || back[0].Term == nil || back[2].Intercept == nil || back[2].Intercept.Intercept != nil {
		t.Errorf("scale pairs read back: %v %+v", err, back)
	}
	// a series by the group plate, its time a column; one series with its time unchecked
	grp, week := "grp", "week"
	for _, series := range []*bayescontract.Series{{By: &grp, Time: &week}, {}} {
		b.Series = series
		if err := b.Check("sample", "replicated"); err != nil {
			t.Error(err)
		}
	}
	text, _ = json.Marshal(bayescontract.Series{By: &grp, Time: &week})
	if string(text) != `{"by":"grp","time":"week"}` {
		t.Errorf("series: %s", text)
	}
	b.Series = nil
	if text, _ = json.Marshal(b); strings.Contains(string(text), "series") {
		t.Errorf("a task without a series names none: %s", text)
	}
}

// A task with no list writes empty lists, never null.
func TestListsAreNeverNull(t *testing.T) {
	b, err := json.Marshal(bayescontract.Task{})
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{`"plates":[]`, `"mapping":[]`, `"data":[]`, `"quantities":[]`, `"checks":null`, `"replicated":null`,
		`"sampler":null`} {
		if !bytes.Contains(b, []byte(key)) {
			t.Errorf("%s not in %s", key, b)
		}
	}
}

// The ten-column table is bounded by construction: at the most chains and the largest graph, under the catalogue's
// max_result_rows (the engine reads the results whole and refuses more).
func TestTheCapsKeepTheResultsUnderTheEnginesLimit(t *testing.T) {
	most := bayescontract.MaxResultRows(bayescatalog.GetLimits().MaxNodes)
	if limit := int64(modelcatalog.Limit("max_result_rows")); limit <= 0 || most >= limit {
		t.Errorf("at most %d rows, the engine's limit %d", most, limit)
	}
	// 26,000 (summary, 13 rows × 2,000) + 3,000 (original scale, 5 × 600 entries) + 25,920 (charts) + 1,800 (6 pairs × 300)
	// + 24,000 (units) + 6,000 (quantity) + 1,600 (data) + 90 (sampler) + 80 (model) + 5,000 (checks).
	if most != 93_490 {
		t.Errorf("the arithmetic gives %d rows (93,490 in the spec): say the new number in the spec", most)
	}
}

// testdata/contract.json is the declaration the runner's tests read.
func TestTheDeclarationFile(t *testing.T) {
	decl := map[string]any{
		"protocol": bayescontract.Protocol, "group": bayescontract.Group, "model": bayescontract.Model,
		"modes": bayescontract.Modes, "outputs": bayescontract.Outputs, "output_columns": bayescontract.OutputColumns,
		"summary_kinds": bayescontract.SummaryKinds, "summary_scales": bayescontract.SummaryScales,
		"sampler_columns": map[string][]string{bayescontract.ModeSample: bayescontract.SamplerColumnsNUTS,
			bayescontract.ModePriorPredictive: bayescontract.SamplerColumnsFixedParam},
		"limits": bayescontract.Limits, "shapes": bayescontract.Shapes, "variants": bayescontract.Variants,
		"refusals": bayescontract.Refusals,
		"exit":     map[string]int{"ok": bayescontract.ExitOK, "bug": bayescontract.ExitBug, "refused": bayescontract.ExitRefused, "selftest_failed": bayescontract.ExitSelftestFails},
		"sampler": map[string]any{
			"chains":        []int{bayescontract.MinChains, bayescontract.MaxChains, bayescontract.DefaultChains},
			"warmup":        []int{bayescontract.MinWarmup, bayescontract.MaxWarmup, bayescontract.DefaultWarmup},
			"draws":         []int{bayescontract.MinDraws, bayescontract.MaxDraws, bayescontract.DefaultDraws},
			"max_treedepth": []int{bayescontract.MinMaxTreedepth, bayescontract.MaxMaxTreedepth, bayescontract.DefaultMaxTreedepth},
			"seed_max":      int64(bayescontract.MaxSeed), "adapt_delta_default": bayescontract.DefaultAdaptDelta,
			"refresh_default": bayescontract.DefaultRefresh, "draws_per_chain_default": bayescontract.DefaultDrawsPerChain},
		"max_result_rows_by_construction": bayescontract.MaxResultRows(bayescatalog.GetLimits().MaxNodes),
	}
	compareOrWrite(t, filepath.Join("testdata", "contract.json"), indent(t, decl))
}
