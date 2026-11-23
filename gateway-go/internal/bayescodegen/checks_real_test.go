package bayescodegen_test

import (
	"bytes"
	"errors"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayeschecks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// predictive-checks' extension (lot P2, internal/bayeschecks) on the codegen's random graphs: every
// valid model gives a program with it whose manifests hold together, stanc accepts 300 of them,
// and the fuzzer finds no graph it fails on. Here because the generator of graphs is this
// package's (export_test.go); bayeschecks' own tests cover its golden cases and the corpora.

func model(data []byte) (*bayesgraph.Model, bool) {
	g, _ := bayesgraph.Decode(data)
	if g == nil {
		return nil, false
	}
	m := bayesgraph.BuildModel(g)
	return m, m.Valid()
}

func plates(m *bayesgraph.Model) []bayescontract.Plate {
	var out []bayescontract.Plate
	for _, p := range m.Plates {
		out = append(out, bayescontract.Plate{ID: p.ID, Kind: p.Kind, Label: p.Label})
	}
	return out
}

// checksProgram is the program with the extension, its manifests checked.
func checksProgram(t testing.TB, data []byte) (*bayescodegen.Program, bool) {
	t.Helper()
	m, ok := model(data)
	if !ok {
		return nil, false
	}
	p, err := bayescodegen.Generate(m, bayeschecks.Extension{})
	if err != nil {
		t.Fatalf("a valid model gives %v\n%s", err, data)
	}
	if err := bayescontract.CheckManifests(plates(m), p.Mapping, p.Data, p.Quantities); err != nil {
		t.Fatalf("%v\n%s", err, data)
	}
	return p, true
}

func TestChecksExtensionOnGeneratedGraphs(t *testing.T) {
	n, prior := 0, 0
	for seed := 0; seed < 500; seed++ {
		data := bayescodegen.GenGraph(seed)
		if _, ok := checksProgram(t, data); ok {
			n++
			if m, _ := model(data); bayeschecks.Analyze(m).PriorPredictive {
				prior++
			}
		}
	}
	t.Logf("%d programs with the extension, %d with a prior predictive", n, prior)
	if n < 400 || prior < n/2 {
		t.Errorf("%d programs, %d with a prior predictive", n, prior)
	}
}

func TestChecksExtensionStanc(t *testing.T) {
	stanc := os.Getenv("BAYES_STANC")
	if stanc == "" {
		if os.Getenv("BAYES_STANC_REQUIRED") == "1" {
			t.Fatal("BAYES_STANC_REQUIRED=1 and BAYES_STANC is not set")
		}
		t.Skip("BAYES_STANC is not set")
	}
	dir := t.TempDir()
	n := 300
	if testing.Short() {
		n = 40
	}
	checked := 0
	for seed := 0; seed < n; seed++ {
		p, ok := checksProgram(t, bayescodegen.GenGraph(seed))
		if !ok {
			continue
		}
		file := filepath.Join(dir, "g"+strconv.Itoa(seed)+".stan")
		if err := os.WriteFile(file, p.Text, 0o600); err != nil {
			t.Fatal(err)
		}
		var stdout, stderr bytes.Buffer
		cmd := exec.Command(stanc, "--info", file)
		cmd.Env = []string{"PATH=/usr/bin:/bin"}
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		if err := cmd.Run(); err != nil {
			t.Errorf("seed %d: stanc refuses the program: %v\n%s\n%s", seed, err, stderr.String(), p.Text)
			continue
		}
		for _, m := range bayescodegen.CrossCheck(stdout.Bytes(), p) {
			t.Errorf("seed %d: %s", seed, m)
		}
		checked++
	}
	t.Logf("%d programs with predictive-checks' extension checked with stanc", checked)
}

// FuzzChecksExtension: on the fuzzer's graphs, a valid model always gives a program with the
// extension (never ErrExtension: a save would fail), its manifests hold together, it passes the
// output's checks and is the same on a second generation; the analysis never panics.
//
//	flock <heavy.lock> go test -run='^$' -fuzz=FuzzChecksExtension -fuzztime=2m -parallel 2 ./internal/bayescodegen/
func FuzzChecksExtension(f *testing.F) {
	f.Add([]byte{})
	for seed := uint64(0); seed < 32; seed++ {
		r := rand.New(rand.NewPCG(seed, 0xc4ec))
		b := make([]byte, 700)
		for i := range b {
			b[i] = byte(r.IntN(256))
		}
		f.Add(b)
	}
	f.Fuzz(func(t *testing.T, seed []byte) {
		data := bayescodegen.ChosenGraph(seed)
		m, ok := model(data)
		if m != nil {
			bayeschecks.Analyze(m)
		}
		if !ok {
			return
		}
		p, err := bayescodegen.Generate(m, bayeschecks.Extension{})
		if errors.Is(err, bayescodegen.ErrUnsupported) {
			return
		}
		if err != nil {
			t.Fatalf("a valid model gives %v\n%s", err, data)
		}
		if err := bayescontract.CheckManifests(plates(m), p.Mapping, p.Data, p.Quantities); err != nil {
			t.Fatalf("%v\n%s", err, data)
		}
		m2, _ := model(data)
		q, err := bayescodegen.Generate(m2, bayeschecks.Extension{})
		if err != nil || !bytes.Equal(p.Text, q.Text) {
			t.Fatalf("two generations differ (%v)\n%s", err, data)
		}
	})
}
