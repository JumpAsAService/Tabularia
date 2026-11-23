package bayescodegen_test

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayeschecks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
)

// The stanc check of the validator's corpora (bayesgraph's testdata/corpus, the M3 corpus, and
// testdata/mirror/cases.json, the TypeScript mirror's): every graph of them that is a valid model
// gives a program, without an extension and with predictive-checks' (what a save writes), and stanc
// parses and type-checks both, their --info agreeing with the manifests (CrossCheck). The corpora
// hold the rules' edge cases the properties' generator does not write (lot P2 found one: a
// non-centred state's init reading a value per row). BAYES_STANC and BAYES_STANC_REQUIRED as in
// stanc_test.go; the workflow bayes-codegen.yml runs it with stanc required.

// corpusGraphs are the documents of the two corpora by a name of the case.
func corpusGraphs(t *testing.T) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	files, err := filepath.Glob("../bayesgraph/testdata/corpus/*.json")
	if err != nil || len(files) == 0 {
		t.Fatalf("no corpus: %v", err)
	}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var c struct {
			Input json.RawMessage `json:"input"`
			Raw   *string         `json:"raw"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		doc := []byte(c.Input)
		if c.Raw != nil {
			doc = []byte(*c.Raw)
		}
		out["corpus/"+filepath.Base(f)] = doc
	}
	raw, err := os.ReadFile("../bayesgraph/testdata/mirror/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var mirror struct {
		Cases []struct {
			ID    string `json:"id"`
			Input string `json:"input"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &mirror); err != nil || len(mirror.Cases) == 0 {
		t.Fatalf("the mirror corpus: %v", err)
	}
	for i, c := range mirror.Cases {
		out["mirror/"+strconv.Itoa(i)+"-"+c.ID] = []byte(c.Input)
	}
	return out
}

// stancInfo runs stanc --info on a program's text and returns its report.
func stancInfo(t *testing.T, stanc, dir, name string, text []byte) ([]byte, error) {
	t.Helper()
	file := filepath.Join(dir, name+".stan")
	if err := os.WriteFile(file, text, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, stanc, "--info", file)
	cmd.Dir = dir
	cmd.Env = []string{"PATH=/usr/bin:/bin"}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return nil, &stancError{err: err, stderr: stderr.String()}
	}
	return stdout.Bytes(), nil
}

type stancError struct {
	err    error
	stderr string
}

func (e *stancError) Error() string { return e.err.Error() + "\n" + e.stderr }

func TestCorpusStanc(t *testing.T) {
	stanc := os.Getenv("BAYES_STANC")
	if stanc == "" {
		if os.Getenv("BAYES_STANC_REQUIRED") == "1" {
			t.Fatal("BAYES_STANC_REQUIRED=1 and BAYES_STANC is not set: the stanc check cannot be skipped here")
		}
		t.Skip("BAYES_STANC is not set: the stanc check needs stanc 2.40.0's linux executable")
	}
	graphs := corpusGraphs(t)
	names := make([]string, 0, len(graphs))
	for n := range graphs {
		names = append(names, n)
	}
	sort.Strings(names)
	dir := t.TempDir()
	valid, programs := 0, 0
	for i, name := range names {
		m, ok := model(graphs[name])
		if !ok {
			continue
		}
		valid++
		for _, ext := range []bayescodegen.Extension{nil, bayeschecks.Extension{}} {
			p, err := bayescodegen.Generate(m, ext)
			if err != nil {
				t.Errorf("%s (extension %T): a valid model gives %v", name, ext, err)
				continue
			}
			info, err := stancInfo(t, stanc, dir, "c"+strconv.Itoa(i), p.Text)
			if err != nil {
				t.Errorf("%s (extension %T): stanc refuses the program: %v\n%s", name, ext, err, p.Text)
				continue
			}
			for _, mm := range bayescodegen.CrossCheck(info, p) {
				t.Errorf("%s (extension %T): %s", name, ext, mm)
			}
			programs++
		}
	}
	t.Logf("%d valid graphs of %d in the corpora, %d programs checked with stanc", valid, len(names), programs)
	if valid < 100 {
		t.Errorf("only %d valid graphs in the corpora", valid)
	}
}
