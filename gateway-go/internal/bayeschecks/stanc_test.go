package bayeschecks

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
)

// The stanc check of the programs with the extension, as the codegen's (its stanc_test.go): every
// golden checks_* program and the program of every valid graph at hand parse and type-check with
// the pinned stanc, its --info agrees with the manifests (CrossCheck), and stanc writes its C++
// for the golden ones. BAYES_STANC is stanc 2.40.0's linux executable; without it the tests are
// skipped, and BAYES_STANC_REQUIRED=1 makes the skip a failure:
//
//	BAYES_STANC=/path/to/linux-stanc BAYES_STANC_REQUIRED=1 go test ./internal/bayeschecks/ -run Stanc -v
//
// The 300 programs of the codegen's random graphs with this extension are checked by
// bayescodegen's TestChecksExtensionStanc (it needs the codegen's generator of graphs).

func stancPath(t *testing.T) string {
	t.Helper()
	path := os.Getenv("BAYES_STANC")
	if path == "" {
		if os.Getenv("BAYES_STANC_REQUIRED") == "1" {
			t.Fatal("BAYES_STANC_REQUIRED=1 and BAYES_STANC is not set: the stanc check cannot be skipped here")
		}
		t.Skip("BAYES_STANC is not set: the stanc check needs stanc 2.40.0's linux executable")
	}
	out, err := exec.Command(path, "--version").CombinedOutput()
	if err != nil {
		t.Fatalf("%s --version: %v\n%s", path, err, out)
	}
	if want := "v" + bayescatalog.StanLists().Version; !strings.Contains(string(out), want) {
		t.Fatalf("stanc says %q, the catalogue pins Stan %s", strings.TrimSpace(string(out)), want)
	}
	return path
}

func stancRun(t testing.TB, stanc, dir, name string, text []byte, args ...string) ([]byte, error) {
	t.Helper()
	file := filepath.Join(dir, name+".stan")
	if err := os.WriteFile(file, text, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	var stdout, stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, stanc, append(args, file)...)
	cmd.Dir = dir
	cmd.Env = []string{"PATH=/usr/bin:/bin"}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return nil, errors.New(err.Error() + "\n" + stderr.String())
	}
	return stdout.Bytes(), nil
}

func stancCheck(t testing.TB, stanc, dir, name string, p *bayescodegen.Program) {
	t.Helper()
	info, err := stancRun(t, stanc, dir, name, p.Text, "--info")
	if err != nil {
		t.Errorf("%s: stanc refuses the program: %v\n%s", name, err, p.Text)
		return
	}
	for _, m := range bayescodegen.CrossCheck(info, p) {
		t.Errorf("%s: %s", name, m)
	}
}

func TestGoldenStanc(t *testing.T) {
	stanc := stancPath(t)
	dir := t.TempDir()
	for _, c := range goldenCases(t) {
		t.Run(c.name, func(t *testing.T) {
			_, _, p := generate(t, readFile(t, c.graph))
			if !bytes.Equal(readFile(t, filepath.Join(goldenDir, c.name+".stan")), p.Text) {
				t.Fatalf("%s: the golden file is not what Generate writes", c.name)
			}
			stancCheck(t, stanc, dir, c.name, p)
			if _, err := stancRun(t, stanc, dir, c.name, p.Text, "--o="+filepath.Join(dir, c.name+".hpp")); err != nil {
				t.Errorf("%s: stanc cannot compile the program: %v", c.name, err)
			}
		})
	}
}

func TestCorpusStanc(t *testing.T) {
	stanc := stancPath(t)
	dir := t.TempDir()
	graphs := corpusGraphs(t)
	names := make([]string, 0, len(graphs))
	for n := range graphs {
		names = append(names, n)
	}
	sort.Strings(names)
	checked := 0
	for i, name := range names {
		m, ok := validModel(graphs[name])
		if !ok {
			continue
		}
		p, err := bayescodegen.Generate(m, Extension{})
		if errors.Is(err, bayescodegen.ErrUnsupported) {
			continue
		}
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		stancCheck(t, stanc, dir, "c"+itoa(i), p)
		checked++
	}
	t.Logf("%d programs with the extension checked with stanc", checked)
}
