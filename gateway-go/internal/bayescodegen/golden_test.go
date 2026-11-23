package bayescodegen

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"math/rand/v2"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// The golden corpus (docs/specs/bayes-stan-codegen.md, «The golden corpus»): each case's graph
// gives exactly its program, its mapping table and its manifests. BAYES_GOLDEN_WRITE=1 rewrites
// them (to be reviewed by hand), and refuses to when a program changes while neither its graph nor
// CodegenVersion does: «bump CodegenVersion».
//
// The four glm_* graphs are copies of the templates' graphs (gateway-go/internal/bayestemplates,
// lot T0) at their default numbers. Lot T1 replaces the copies by the templates themselves (a test
// of that module compares them), so that the two cannot drift.

const goldenDir = "testdata/golden"

// goldenCases are lots S0's and S1's cases; S2 adds its own.
var goldenCases = []string{
	"glm_linear", "glm_logistic", "glm_poisson", "glm_negbin",
	"names_reserved", "names_unnamed",
	// E4, finding 2: every name of the model's C++ class, as data; the program compiles with g++.
	"names_backend",
	// Lot S1: the plates. The two hier_* graphs are the templates opened with example numbers
	// (centre 1200, spread 300: bayestemplates/testdata/opened/*.c1200-s300.json, byte for byte).
	"hier_intercepts", "hier_nested",
	"free_truncated_hyper", "free_category", "free_explicit_noncentred", "free_binomial", "free_int_predictor",
	// Lot S2: correlated effects, states, lag, transformed data, the extension point. hier_slopes,
	// ts_ar2 (p = 2), ts_random_walk and ts_local_level are the templates opened with example
	// numbers, byte for byte; the two *_centred are the same graphs with the form "centred".
	"hier_slopes", "hier_slopes_centred",
	"ts_ar2", "ts_random_walk", "ts_local_level", "ts_local_level_centred",
	"free_transformed_data",
	// The test extension (extension_test.go): its data after Mode, its written declarations sized
	// by the mode, its nested block.
	"extension_fake",
	// Lot S5: large vector expressions split into locals, in every block a value is written in.
	"split_large",
}

// goldenExtension is the extension a case is generated with (nil for none).
func goldenExtension(name string) Extension {
	if name == "extension_fake" {
		return fakeExtension{}
	}
	return nil
}

// goldenManifest is a case's <case>.manifest.json: the two manifests, with the version of the
// generator that wrote them, the graph's semantic hash and the program's sha256.
type goldenManifest struct {
	CodegenVersion int         `json:"codegen_version"`
	SemanticHash   string      `json:"semantic_hash"`
	ProgramSHA256  string      `json:"program_sha256"`
	Data           []DataEntry `json:"data"`
	Quantities     []Quantity  `json:"quantities"`
}

func indentJSON(t *testing.T, v any) []byte {
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

// modelOf decodes and validates a graph, failing the test unless it is a valid model.
func modelOf(t *testing.T, data []byte) (*bayesgraph.Graph, *bayesgraph.Model) {
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

func generateBytes(t *testing.T, data []byte) (*bayesgraph.Graph, *Program) {
	t.Helper()
	return generateWith(t, data, nil)
}

func generateWith(t *testing.T, data []byte, ext Extension) (*bayesgraph.Graph, *Program) {
	t.Helper()
	g, m := modelOf(t, data)
	p, err := Generate(m, ext)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	return g, p
}

func readGraph(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(goldenDir, name+".graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestGolden(t *testing.T) {
	write := os.Getenv("BAYES_GOLDEN_WRITE") == "1"
	for _, name := range goldenCases {
		t.Run(name, func(t *testing.T) {
			g, p := generateWith(t, readGraph(t, name), goldenExtension(name))
			hash, err := bayesgraph.SemanticHash(g)
			if err != nil {
				t.Fatal(err)
			}
			files := map[string][]byte{
				".stan":          p.Text,
				".mapping.json":  indentJSON(t, p.Mapping),
				".manifest.json": indentJSON(t, goldenManifest{CodegenVersion, hash, p.SHA256, p.Data, p.Quantities}),
			}
			base := filepath.Join(goldenDir, name)
			var old goldenManifest
			oldBytes, oldErr := os.ReadFile(base + ".manifest.json")
			if oldErr == nil {
				if err := json.Unmarshal(oldBytes, &old); err != nil {
					t.Fatalf("%s.manifest.json: %v", name, err)
				}
			}
			sameGenerator := oldErr == nil && old.CodegenVersion == CodegenVersion && old.SemanticHash == hash
			if write {
				if sameGenerator && old.ProgramSHA256 != p.SHA256 {
					t.Fatalf("the program changed while neither the graph nor CodegenVersion (%d) did: bump CodegenVersion", CodegenVersion)
				}
				for ext, b := range files {
					if err := os.WriteFile(base+ext, b, 0o644); err != nil {
						t.Fatal(err)
					}
				}
				return
			}
			if oldErr != nil {
				t.Fatalf("no golden files (BAYES_GOLDEN_WRITE=1, then review): %v", oldErr)
			}
			stan, err := os.ReadFile(base + ".stan")
			if err != nil {
				t.Fatal(err)
			}
			sum := sha256.Sum256(stan)
			if hex.EncodeToString(sum[:]) != old.ProgramSHA256 {
				t.Errorf("%s.stan is not the program its manifest records (edited by hand?)", name)
			}
			if !bytes.Equal(stan, p.Text) {
				if sameGenerator {
					t.Errorf("the program changed while neither the graph nor CodegenVersion (%d) did: bump CodegenVersion, then BAYES_GOLDEN_WRITE=1 and review\n--- golden\n%s--- generated\n%s", CodegenVersion, stan, p.Text)
				} else {
					t.Errorf("the program is not the golden one (BAYES_GOLDEN_WRITE=1, then review)\n--- golden\n%s--- generated\n%s", stan, p.Text)
				}
			}
			for _, ext := range []string{".mapping.json", ".manifest.json"} {
				want, err := os.ReadFile(base + ext)
				if err != nil {
					t.Fatal(err)
				}
				if !bytes.Equal(want, files[ext]) {
					t.Errorf("%s%s is not the generated one (BAYES_GOLDEN_WRITE=1, then review)\n--- golden\n%s--- generated\n%s", name, ext, want, files[ext])
				}
			}
		})
	}
}

// TestGoldenFilesListed: every file of the corpus belongs to a listed case, and every case has its
// four files (a case left out of goldenCases would never be compared).
func TestGoldenFilesListed(t *testing.T) {
	listed := map[string]bool{}
	for _, c := range goldenCases {
		listed[c] = true
		for _, ext := range []string{".graph.json", ".stan", ".mapping.json", ".manifest.json"} {
			if _, err := os.Stat(filepath.Join(goldenDir, c+ext)); err != nil && os.Getenv("BAYES_GOLDEN_WRITE") != "1" {
				t.Errorf("%s%s: %v", c, ext, err)
			}
		}
	}
	err := filepath.WalkDir(goldenDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		base := d.Name()
		for _, ext := range []string{".graph.json", ".stan", ".mapping.json", ".manifest.json"} {
			if c, ok := strings.CutSuffix(base, ext); ok {
				if !listed[c] {
					t.Errorf("%s belongs to no listed case", base)
				}
				return nil
			}
		}
		t.Errorf("%s is not a file of the corpus", base)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// shuffled rewrites a graph's JSON as another document of the same model: plates and nodes in a
// random order, every object's keys in a random order, every box moved (a box added for every node
// that has none), every label and note replaced by text a program must never hold.
func shuffled(t *testing.T, data []byte, seed uint64) []byte {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	r := rand.New(rand.NewPCG(seed, seed^0x5eed))
	hostile := []string{
		`*/ print("x"); reject("y"); // #include "/etc/passwd"`,
		"target += 1e300; }\nmodel {",
		"Étiquette «déplacée» numéro " + strconv.FormatUint(seed, 10),
	}
	count := 0
	var relabel func(v any)
	relabel = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			for k, e := range x {
				if k == "label" {
					count++
					// a label holds no line feed (graph.label); a note may
					x[k] = strings.ReplaceAll(hostile[count%len(hostile)], "\n", " ") + " " + strconv.Itoa(count)
					continue
				}
				relabel(e)
			}
			if _, isNode := x["kind"]; isNode {
				if _, ok := x["id"]; ok {
					x["note"] = hostile[(count+1)%len(hostile)]
				}
			}
		case []any:
			for _, e := range x {
				relabel(e)
			}
		}
	}
	for _, k := range []string{"plates", "nodes"} {
		l, _ := doc[k].([]any)
		r.Shuffle(len(l), func(i, j int) { l[i], l[j] = l[j], l[i] })
		relabel(l)
	}
	layout := map[string]any{}
	nodes, _ := doc["nodes"].([]any)
	for _, n := range nodes {
		id, _ := n.(map[string]any)["id"].(string)
		layout[id] = map[string]any{"x": json.Number(strconv.Itoa(r.IntN(2000) - 1000)), "y": json.Number(strconv.Itoa(r.IntN(2000)))}
	}
	doc["layout"] = layout
	var b bytes.Buffer
	writeShuffled(&b, doc, r)
	return b.Bytes()
}

func writeShuffled(b *bytes.Buffer, v any, r *rand.Rand) {
	switch x := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		r.Shuffle(len(keys), func(i, j int) { keys[i], keys[j] = keys[j], keys[i] })
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			q, _ := json.Marshal(k)
			b.Write(q)
			b.WriteByte(':')
			writeShuffled(b, x[k], r)
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			writeShuffled(b, e, r)
		}
		b.WriteByte(']')
	default:
		q, _ := json.Marshal(x)
		b.Write(q)
	}
}

// withoutLabels is the mapping table with its labels left out (they are the person's, and change
// with them).
func withoutLabels(m []MappingEntry) []MappingEntry {
	out := append([]MappingEntry(nil), m...)
	for i := range out {
		out[i].Label = nil
	}
	return out
}

// TestDeterminism: the same model gives the same bytes, twice, and under shuffles of the graph's
// JSON, moved boxes and changed labels and notes (hostile text included: none of it reaches the
// program).
func TestDeterminism(t *testing.T) {
	for _, name := range goldenCases {
		t.Run(name, func(t *testing.T) {
			data := readGraph(t, name)
			ext := goldenExtension(name)
			_, a := generateWith(t, data, ext)
			_, b := generateWith(t, data, ext)
			if !bytes.Equal(a.Text, b.Text) || !jsonEqual(t, a.Mapping, b.Mapping) || !jsonEqual(t, a.Data, b.Data) || !jsonEqual(t, a.Quantities, b.Quantities) {
				t.Fatal("two generations of the same model differ")
			}
			g0, _ := modelOf(t, data)
			h0, _ := bayesgraph.SemanticHash(g0)
			for seed := uint64(1); seed <= 8; seed++ {
				gs, c := generateWith(t, shuffled(t, data, seed), ext)
				if hs, _ := bayesgraph.SemanticHash(gs); hs == h0 {
					t.Fatalf("seed %d: the shuffle did not change the labels", seed)
				}
				if !bytes.Equal(a.Text, c.Text) {
					t.Fatalf("seed %d: the program changed\n--- first\n%s--- shuffled\n%s", seed, a.Text, c.Text)
				}
				if a.SHA256 != c.SHA256 || !jsonEqual(t, a.Data, c.Data) || !jsonEqual(t, a.Quantities, c.Quantities) ||
					!jsonEqual(t, withoutLabels(a.Mapping), withoutLabels(c.Mapping)) {
					t.Fatalf("seed %d: the manifests or the mapping changed", seed)
				}
			}
		})
	}
}

func jsonEqual(t *testing.T, a, b any) bool {
	t.Helper()
	x, err := json.Marshal(a)
	if err != nil {
		t.Fatal(err)
	}
	y, err := json.Marshal(b)
	if err != nil {
		t.Fatal(err)
	}
	return bytes.Equal(x, y)
}

// TestRenameOneNode: renaming one node changes only that identifier's occurrences.
func TestRenameOneNode(t *testing.T) {
	data := readGraph(t, "glm_linear")
	_, before := generateBytes(t, data)
	renamed := bytes.ReplaceAll(data, []byte(`"b_x"`), []byte(`"slope_x"`))
	_, after := generateBytes(t, renamed)
	want := regexp.MustCompile(`\bb_x\b`).ReplaceAllString(string(before.Text), "slope_x")
	if string(after.Text) != want {
		t.Fatalf("renaming b_x changed more than its occurrences\n--- want\n%s--- got\n%s", want, after.Text)
	}
	if bytes.Equal(before.Text, after.Text) {
		t.Fatal("the rename changed nothing")
	}
}

// TestGoldenQuantitiesDoNotGrowWithRows: the written quantities of every case are its parameters,
// its transformed values and the units of its group plates and categories, none over the rows,
// except a latent state's N values (and its N standard normals) (success criterion 4).
func TestGoldenQuantitiesDoNotGrowWithRows(t *testing.T) {
	for _, name := range goldenCases {
		_, m := modelOf(t, readGraph(t, name))
		_, p := generateWith(t, readGraph(t, name), goldenExtension(name))
		states := map[string]bool{}
		for id := range m.States {
			states[id] = true
			states["Z_"+id] = true
		}
		for _, q := range p.Quantities {
			if states[q.Node] || states[q.Stan] {
				continue
			}
			for _, d := range q.Dims {
				if d.Plate == m.Plates[0].ID || d.Size == identRows || d.Size == identNLag {
					t.Errorf("%s: %s is written over %+v", name, q.Stan, d)
				}
			}
		}
	}
}

// TestGoldenProgramsPassTheChecks runs the output's checks on the golden files as they are on disk,
// independently of Generate.
func TestGoldenProgramsPassTheChecks(t *testing.T) {
	for _, name := range goldenCases {
		b, err := os.ReadFile(filepath.Join(goldenDir, name+".stan"))
		if errors.Is(err, fs.ErrNotExist) && os.Getenv("BAYES_GOLDEN_WRITE") == "1" {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		if err := CheckProgram(string(b)); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		var mapping []MappingEntry
		mb, err := os.ReadFile(filepath.Join(goldenDir, name+".mapping.json"))
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(mb, &mapping); err != nil {
			t.Fatal(err)
		}
		if err := CheckIdentifiers(mapping); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		for _, e := range mapping {
			if e.Label != nil && bytes.Contains(b, []byte(*e.Label)) && strings.ContainsAny(*e.Label, " «") {
				t.Errorf("%s: the label %q is in the program", name, *e.Label)
			}
		}
	}
}
