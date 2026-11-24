package bayeschecks

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// The golden programs with the extension (the spec's «Testing strategy»): checks_<case> for every
// graph of the codegen's golden corpus that a save may store (its extension_* cases are the
// codegen's test extensions' own), whose bytes the store's tests compare with what a save writes,
// and this module's own cases. BAYES_GOLDEN_WRITE=1 rewrites them, to be reviewed by hand.

const (
	goldenDir   = "testdata/golden"
	codegenDir  = "../bayescodegen/testdata/golden"
	writeGolden = "BAYES_GOLDEN_WRITE"
)

// ownCases are this module's graphs (testdata/golden/<case>.graph.json): every truncation form on
// single values and on a plate, three outcomes of the three kinds, a truncated gamma (no prior
// predictive), correlated effects of three components (the onion's loop), a lagged likelihood and
// one on every row (each outcome's rows named in the data manifest: Correction of 2026-11-24 from P3a).
var ownCases = []string{"checks_truncated", "checks_three_outcomes", "checks_gamma", "checks_effects3", "checks_mixed_lag"}

// fromCodegen are this module's cases written from the codegen's test graphs.
var fromCodegen = map[string]string{
	"checks_counts": "extension_checks_counts", // four likelihoods, the fourth left out
	"checks_lagged": "extension_checks_lagged", // a lagged likelihood reading a value without lag
}

// goldenCase is a case's name and its graph's file.
type goldenCase struct{ name, graph string }

func goldenCases(t testing.TB) []goldenCase {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(codegenDir, "*.graph.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no graphs in the codegen's corpus: %v", err)
	}
	var out []goldenCase
	for _, f := range files {
		name := strings.TrimSuffix(filepath.Base(f), ".graph.json")
		if !strings.HasPrefix(name, "extension_") {
			out = append(out, goldenCase{"checks_" + name, f})
		}
	}
	for name, g := range fromCodegen {
		out = append(out, goldenCase{name, filepath.Join(codegenDir, g+".graph.json")})
	}
	for _, name := range ownCases {
		out = append(out, goldenCase{name, filepath.Join(goldenDir, name+".graph.json")})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out
}

// goldenManifest is the codegen's <case>.manifest.json, with what the analysis says of the model.
type goldenManifest struct {
	CodegenVersion int                       `json:"codegen_version"`
	SemanticHash   string                    `json:"semantic_hash"`
	ProgramSHA256  string                    `json:"program_sha256"`
	Data           []bayescontract.DataEntry `json:"data"`
	Quantities     []bayescontract.Quantity  `json:"quantities"`
	Checks         goldenAnalysis            `json:"checks"`
}

type goldenAnalysis struct {
	Outcomes        []Outcome `json:"outcomes"`
	Likelihoods     int       `json:"likelihoods"`
	PriorPredictive bool      `json:"prior_predictive"`
	NoPrior         string    `json:"no_prior,omitempty"`
	Marginals       []string  `json:"marginals"`
}

func indentJSON(t testing.TB, v any) []byte {
	t.Helper()
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func modelOf(t testing.TB, data []byte) (*bayesgraph.Graph, *bayesgraph.Model) {
	t.Helper()
	g, findings := bayesgraph.Decode(data)
	if g == nil {
		t.Fatalf("the graph does not decode: %v", findings)
	}
	m := bayesgraph.BuildModel(g)
	if !m.Valid() {
		t.Fatalf("the graph is not a valid model: %+v", m.Findings)
	}
	return g, m
}

func generate(t testing.TB, data []byte) (*bayesgraph.Graph, *bayesgraph.Model, *bayescodegen.Program) {
	t.Helper()
	g, m := modelOf(t, data)
	p, err := bayescodegen.Generate(m, Extension{})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	return g, m, p
}

func readFile(t testing.TB, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestGolden(t *testing.T) {
	write := os.Getenv(writeGolden) == "1"
	for _, c := range goldenCases(t) {
		t.Run(c.name, func(t *testing.T) {
			g, m, p := generate(t, readFile(t, c.graph))
			hash, err := bayesgraph.SemanticHash(g)
			if err != nil {
				t.Fatal(err)
			}
			a := Analyze(m)
			ga := goldenAnalysis{a.Outcomes, a.Likelihoods, a.PriorPredictive, a.NoPrior, a.Marginals}
			files := map[string][]byte{
				".stan":          p.Text,
				".mapping.json":  indentJSON(t, p.Mapping),
				".manifest.json": indentJSON(t, goldenManifest{p.CodegenVersion, hash, p.SHA256, p.Data, p.Quantities, ga}),
			}
			base := filepath.Join(goldenDir, c.name)
			if write {
				for ext, b := range files {
					if err := os.WriteFile(base+ext, b, 0o644); err != nil {
						t.Fatal(err)
					}
				}
				return
			}
			stan := readFile(t, base+".stan")
			var old goldenManifest
			if err := json.Unmarshal(readFile(t, base+".manifest.json"), &old); err != nil {
				t.Fatal(err)
			}
			if sum := sha256.Sum256(stan); hex.EncodeToString(sum[:]) != old.ProgramSHA256 {
				t.Errorf("%s.stan is not the program its manifest records (edited by hand?)", c.name)
			}
			for ext, b := range files {
				if want := readFile(t, base+ext); !bytes.Equal(want, b) {
					t.Errorf("%s%s is not the generated one (%s=1, then review)\n--- golden\n%s--- generated\n%s", c.name, ext, writeGolden, want, b)
				}
			}
		})
	}
}

// Every golden file belongs to a case, and every case has its three files.
func TestGoldenFilesListed(t *testing.T) {
	want := map[string]bool{}
	for _, c := range goldenCases(t) {
		for _, ext := range []string{".stan", ".mapping.json", ".manifest.json"} {
			want[c.name+ext] = true
		}
	}
	for _, name := range ownCases {
		want[name+".graph.json"] = true
	}
	files, err := os.ReadDir(goldenDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		if !want[f.Name()] {
			t.Errorf("%s belongs to no case", f.Name())
		}
		delete(want, f.Name())
	}
	for f := range want {
		t.Errorf("%s is missing", f)
	}
}
