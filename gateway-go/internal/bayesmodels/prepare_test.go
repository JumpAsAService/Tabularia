package bayesmodels

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayestemplates"
)

// The pure parts: what a save stores, the family, the pins of a flow, the compile's request.

const (
	goldenDir = "../bayescodegen/testdata/golden"
	corpusDir = "../bayesgraph/testdata/corpus"
)

func mustPrepare(t *testing.T, graph []byte) (*Content, []bayesgraph.Finding) {
	t.Helper()
	c, fs, err := Prepare(graph)
	if err != nil {
		t.Fatalf("Prepare: %v (findings %v)", err, fs)
	}
	return c, fs
}

// Every golden graph of the generator (the templates opened, and the free models) saves with the
// golden program: its bytes, its sha256, its semantic hash, its manifests and mapping table.
func TestPrepareGivesTheGoldenProgram(t *testing.T) {
	graphs, err := filepath.Glob(filepath.Join(goldenDir, "*.graph.json"))
	if err != nil || len(graphs) == 0 {
		t.Fatalf("no golden graphs: %v", err)
	}
	for _, path := range graphs {
		name := strings.TrimSuffix(filepath.Base(path), ".graph.json")
		if strings.HasPrefix(name, "extension_") {
			continue // written with an extension, which a save never has
		}
		t.Run(name, func(t *testing.T) {
			graph, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			c, _ := mustPrepare(t, graph)
			var man struct {
				CodegenVersion int             `json:"codegen_version"`
				SemanticHash   string          `json:"semantic_hash"`
				ProgramSHA256  string          `json:"program_sha256"`
				Data           json.RawMessage `json:"data"`
				Quantities     json.RawMessage `json:"quantities"`
			}
			readJSON(t, filepath.Join(goldenDir, name+".manifest.json"), &man)
			if c.ProgramSHA256 != man.ProgramSHA256 || c.SemanticHash != man.SemanticHash || c.CodegenVersion != man.CodegenVersion {
				t.Fatalf("sha256 %s, hash %s, codegen %d; golden %s, %s, %d", c.ProgramSHA256, c.SemanticHash, c.CodegenVersion,
					man.ProgramSHA256, man.SemanticHash, man.CodegenVersion)
			}
			stan, err := os.ReadFile(filepath.Join(goldenDir, name+".stan"))
			if err != nil {
				t.Fatal(err)
			}
			if c.Program != string(stan) {
				t.Fatal("the program is not the golden one")
			}
			sameJSON(t, "data manifest", c.DataManifest, man.Data)
			sameJSON(t, "quantity manifest", c.QuantityManifest, man.Quantities)
			mapping, err := os.ReadFile(filepath.Join(goldenDir, name+".mapping.json"))
			if err != nil {
				t.Fatal(err)
			}
			sameJSON(t, "mapping", c.Mapping, mapping)
			if c.Findings == "" || c.Findings == "null" {
				t.Fatalf("findings %q: a JSON list is stored", c.Findings)
			}
			if len(c.CatalogSHA256) != 64 || c.FormatVersion != bayesgraph.FormatVersion {
				t.Fatalf("catalog %q, format %d", c.CatalogSHA256, c.FormatVersion)
			}
		})
	}
}

// Every template, as the gallery offers it, saves; its family is the one the template declares,
// derived from the graph alone.
func TestTemplatesSaveWithTheirFamily(t *testing.T) {
	list := bayestemplates.List()
	if len(list) == 0 {
		t.Fatal("no templates")
	}
	for _, tp := range list {
		c, fs := mustPrepare(t, tp.Graph)
		if c.Family != tp.Family {
			t.Errorf("%s: family %q, the template says %q", tp.ID, c.Family, tp.Family)
		}
		for _, f := range fs {
			if f.Severity == bayesgraph.Error {
				t.Errorf("%s: an error came back with a program: %+v", tp.ID, f)
			}
		}
	}
}

// The validator's corpus: a graph with an error is refused with the gateway's findings and no
// content; one with warnings only is prepared, its warnings stored. Both kinds are in the corpus.
func TestCorpusRefusesErrorsAndKeepsWarnings(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(corpusDir, "*.json"))
	if err != nil || len(files) == 0 {
		t.Fatalf("no corpus: %v", err)
	}
	refused, warned := 0, 0
	for _, path := range files {
		var cs struct {
			Input json.RawMessage `json:"input"`
			Raw   *string         `json:"raw"`
		}
		readJSON(t, path, &cs)
		doc := []byte(cs.Input)
		if cs.Raw != nil {
			doc = []byte(*cs.Raw)
		}
		if len(doc) == 0 {
			continue
		}
		c, fs, err := Prepare(doc)
		hasError := false
		for _, f := range fs {
			if f.Severity == bayesgraph.Error {
				hasError = true
			}
		}
		var r *Refused
		switch {
		case hasError:
			if !errors.As(err, &r) || c != nil || len(r.Findings) != len(fs) {
				t.Errorf("%s: an error must refuse the save (content %v, err %v)", filepath.Base(path), c != nil, err)
			}
			refused++
		case err != nil:
			t.Errorf("%s: no error among the findings, and yet %v", filepath.Base(path), err)
		default:
			var stored []bayesgraph.Finding
			if err := json.Unmarshal([]byte(c.Findings), &stored); err != nil || len(stored) != len(fs) {
				t.Errorf("%s: the stored findings %s are not the %d warnings", filepath.Base(path), c.Findings, len(fs))
			}
			if len(fs) > 0 {
				warned++
			}
		}
	}
	if refused == 0 || warned == 0 {
		t.Fatalf("the corpus gave %d refused and %d saved with warnings: both are needed", refused, warned)
	}
}

func TestADocumentThatDoesNotDecodeIsRefused(t *testing.T) {
	for _, doc := range []string{`{`, `[]`, `{"format_version": 99, "plates": [], "nodes": []}`,
		`{"format_version": 1, "plates": [], "nodes": [], "extra": 1}`} {
		c, fs, err := Prepare([]byte(doc))
		var r *Refused
		if c != nil || !errors.As(err, &r) || len(fs) == 0 || fs[0].Code != "graph.format" {
			t.Errorf("%s: content %v, findings %v, err %v", doc, c != nil, fs, err)
		}
	}
	big := bytes.Repeat([]byte(" "), 300*1024)
	if _, fs, err := Prepare(big); err == nil || len(fs) == 0 || fs[0].Code != "graph.too_big" {
		t.Errorf("a graph over max_graph_bytes: %v %v", fs, err)
	}
}

func template(t *testing.T, id string) []byte {
	t.Helper()
	tp, ok := bayestemplates.Get(id)
	if !ok {
		t.Fatalf("no template %s", id)
	}
	return tp.Graph
}

// moved is the graph with one box of its layout moved.
func moved(t *testing.T, graph []byte, dx float64) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(graph, &doc); err != nil {
		t.Fatal(err)
	}
	layout, _ := doc["layout"].(map[string]any)
	if len(layout) == 0 {
		t.Fatal("the graph has no layout")
	}
	for _, box := range layout {
		b := box.(map[string]any)
		b["x"] = b["x"].(float64) + dx
		break
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// relabelled is the graph with its first node's label changed: the semantic hash changes.
func relabelled(t *testing.T, graph []byte, label string) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(graph, &doc); err != nil {
		t.Fatal(err)
	}
	doc["nodes"].([]any)[0].(map[string]any)["label"] = label
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// A box moved: another canonical text, the same semantic hash and the same program, noted layout.
// The same graph written otherwise (spaces, key order) is unchanged; a label is an edit.
func TestLayoutOnlyAndUnchanged(t *testing.T) {
	g := template(t, "hier_slopes")
	c0, _ := mustPrepare(t, g)
	cur := &Version{Version: 3, Content: *c0}

	c1, _ := mustPrepare(t, moved(t, g, 40))
	if c1.Graph == c0.Graph || c1.SemanticHash != c0.SemanticHash || c1.Program != c0.Program || c1.ProgramSHA256 != c0.ProgramSHA256 {
		t.Fatal("a layout-only change must keep the hash and the program and change the stored graph")
	}
	if Unchanged(cur, c1) || noteOf(cur, c1) != NoteLayout {
		t.Fatalf("a layout-only change: unchanged %v, note %q", Unchanged(cur, c1), noteOf(cur, c1))
	}

	var doc any
	if err := json.Unmarshal(g, &doc); err != nil {
		t.Fatal(err)
	}
	pretty, err := json.MarshalIndent(doc, "", "    ")
	if err != nil {
		t.Fatal(err)
	}
	c2, _ := mustPrepare(t, pretty)
	if !Unchanged(cur, c2) {
		t.Fatal("the same graph written with other spaces and key order must be unchanged")
	}

	c3, _ := mustPrepare(t, relabelled(t, g, "Another label"))
	if Unchanged(cur, c3) || c3.SemanticHash == c0.SemanticHash || noteOf(cur, c3) != NoteEdited {
		t.Fatal("a label changed is an edit with another semantic hash")
	}

	older := *cur
	older.CodegenVersion = bayescodegen.CodegenVersion - 1
	if Unchanged(&older, c2) {
		t.Fatal("a version written by an older generator is written again with the current one")
	}
	if Unchanged(nil, c2) {
		t.Fatal("no current version: nothing is unchanged")
	}
}

func TestFamily(t *testing.T) {
	build := func(doc string) *bayesgraph.Model {
		t.Helper()
		g, fs := bayesgraph.Decode([]byte(doc))
		if g == nil {
			t.Fatalf("decode: %v", fs)
		}
		m := bayesgraph.BuildModel(g)
		if !m.Valid() {
			t.Fatalf("not valid: %v", m.Findings)
		}
		return m
	}
	const head = `{"format_version": 1, "plates": [{"id": "rows", "kind": "rows", "label": "Rows"}], "nodes": [
		{"id": "y", "kind": "data", "name": "y", "label": "Outcome", "plate": "rows", "type": "real"},
		{"id": "x", "kind": "data", "name": "x", "label": "Predictor", "plate": "rows", "type": "real"},
		{"id": "a", "kind": "parameter", "name": "a", "label": "Intercept", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
		{"id": "b", "kind": "parameter", "name": "b", "label": "Slope", "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
		{"id": "s", "kind": "parameter", "name": "s", "label": "Noise", "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},`
	glm := build(head + `
		{"id": "eta", "kind": "linear_predictor", "name": "eta", "label": "Predictor", "plate": "rows",
		 "intercept": {"expr": "a"}, "terms": [{"data": "x", "coef": {"expr": "b"}}]},
		{"id": "lik", "kind": "likelihood", "label": "Outcome", "plate": "rows", "observed": "y", "dist": "normal",
		 "args": {"mu": {"ref": "eta"}, "sigma": {"expr": "s"}}}]}`)
	if f := Family(glm); f != FamilyGLM {
		t.Errorf("a normal likelihood on a linear predictor's ref: %q", f)
	}
	free := build(head + `
		{"id": "lik", "kind": "likelihood", "label": "Outcome", "plate": "rows", "observed": "y", "dist": "normal",
		 "args": {"mu": {"expr": "a + b * x"}, "sigma": {"expr": "s"}}}]}`)
	if f := Family(free); f != FamilyFree {
		t.Errorf("a location written as an expression: %q", f)
	}
	notLocation := build(head + `
		{"id": "eta", "kind": "linear_predictor", "name": "eta", "label": "Predictor", "plate": "rows",
		 "intercept": {"expr": "a"}, "terms": [{"data": "x", "coef": {"expr": "b"}}]},
		{"id": "lik", "kind": "likelihood", "label": "Outcome", "plate": "rows", "observed": "y", "dist": "normal",
		 "args": {"mu": {"expr": "eta + 1"}, "sigma": {"expr": "s"}}}]}`)
	if f := Family(notLocation); f != FamilyFree {
		t.Errorf("a linear predictor inside an expression: %q", f)
	}
	for id, want := range map[string]string{"glm_linear": FamilyGLM, "glm_logistic": FamilyGLM, "glm_poisson": FamilyGLM,
		"glm_negbin": FamilyGLM, "hier_intercepts": FamilyHierarchical, "hier_nested": FamilyHierarchical,
		"ts_ar": FamilySeries, "ts_local_level": FamilySeries} {
		c, _ := mustPrepare(t, template(t, id))
		if c.Family != want {
			t.Errorf("%s: %q, want %q", id, c.Family, want)
		}
	}
}

func TestPinned(t *testing.T) {
	def := `{"nodes": [
		{"id": "n1", "type": "bayes_model", "data": {"bayesModelId": 7, "bayesModelVersion": 3}},
		{"id": "n2", "type": "bayes_model", "data": {"bayesModelId": 8}},
		{"id": "n3", "type": "script", "data": {"scriptId": 7}},
		{"id": "n4", "type": "bayes_model", "data": {"bayesModelId": "7"}},
		{"id": "n5", "type": "bayes_model", "data": {"bayesModelId": 7.5}},
		{"id": "n6", "type": "bayes_model"}, 3, null], "edges": []}`
	got := Pinned(def)
	want := []Pin{{NodeID: "n1", ModelID: 7, Version: 3}, {NodeID: "n2", ModelID: 8}}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("%+v", got)
	}
	for _, bad := range []string{"", "{", "[]", `{"nodes": 3}`} {
		if p := Pinned(bad); len(p) != 0 {
			t.Errorf("%q: %+v", bad, p)
		}
	}
}

type fakeCompiler struct {
	state      CompileState
	stateErr   error
	enqueueErr error
	enqueued   []CompileRequest
	// onEnqueue runs inside Enqueue (the database tests look at what is committed)
	onEnqueue func(CompileRequest)
}

func (f *fakeCompiler) Enqueue(_ context.Context, r CompileRequest) error {
	if f.onEnqueue != nil {
		f.onEnqueue(r)
	}
	f.enqueued = append(f.enqueued, r)
	return f.enqueueErr
}

func (f *fakeCompiler) State(context.Context, string) (CompileState, error) {
	if f.state.State == "" {
		return CompileState{State: CompileNone}, f.stateErr
	}
	return f.state, f.stateErr
}

func TestAfterCommit(t *testing.T) {
	ctx := context.Background()
	var logged bytes.Buffer
	log := slog.New(slog.NewTextHandler(&logged, nil))
	r := CompileRequest{ModelID: 1, Version: 2, ProgramSHA256: "abc", By: 5}
	for _, s := range []string{CompileReady, CompileQueued, CompileRunning} {
		f := &fakeCompiler{state: CompileState{State: s}}
		if afterCommit(ctx, f, log, r) || len(f.enqueued) != 0 {
			t.Errorf("%s: asked again", s)
		}
	}
	for _, s := range []string{CompileNone, CompileFailed} {
		f := &fakeCompiler{state: CompileState{State: s}}
		if !afterCommit(ctx, f, log, r) || len(f.enqueued) != 1 || f.enqueued[0] != r {
			t.Errorf("%s: not asked: %+v", s, f.enqueued)
		}
	}
	f := &fakeCompiler{enqueueErr: errors.New("the queue is down")}
	if afterCommit(ctx, f, log, r) || !strings.Contains(logged.String(), "the queue is down") {
		t.Errorf("an error of Enqueue is logged, never returned: %q", logged.String())
	}
	f = &fakeCompiler{stateErr: errors.New("no state")}
	if !afterCommit(ctx, f, log, r) {
		t.Error("a state that cannot be read: the compile is asked for anyway")
	}
	if afterCommit(ctx, nil, log, r) {
		t.Error("no compiler: nothing asked")
	}
	var none Compiler = NoCompiler{}
	if s, err := none.State(ctx, "x"); err != nil || s.State != CompileNone || none.Enqueue(ctx, r) != nil {
		t.Errorf("NoCompiler: %+v %v", s, err)
	}
}

func readJSON(t *testing.T, path string, v any) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, v); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
}

// sameJSON compares two JSON texts as values.
func sameJSON(t *testing.T, what, got string, want []byte) {
	t.Helper()
	var a, b any
	if err := json.Unmarshal([]byte(got), &a); err != nil {
		t.Fatalf("%s: %v", what, err)
	}
	if err := json.Unmarshal(want, &b); err != nil {
		t.Fatalf("%s: %v", what, err)
	}
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	if !bytes.Equal(x, y) {
		t.Fatalf("%s differs from the golden one", what)
	}
}
