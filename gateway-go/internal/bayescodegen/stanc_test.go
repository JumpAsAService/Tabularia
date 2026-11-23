package bayescodegen

import (
	"bytes"
	"context"
	"errors"
	"math/rand/v2"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// The stanc check (docs/specs/bayes-stan-codegen.md, «Safety checks on the output» 3 and «Testing
// strategy»): every golden program, and every program the properties' generator writes, parses and
// type-checks with the pinned stanc, and its --info matches the manifests (CrossCheck is empty).
// This is where what the spec «assumed» of Stan's syntax is run.
//
// BAYES_STANC is the path of stanc 2.40.0's linux executable. Without it the tests are skipped
// with a message, on a developer's machine; BAYES_STANC_REQUIRED=1 (CI sets it) turns the skip
// into a failure, so that a workflow that lost its stanc does not pass:
//
//	BAYES_STANC=/path/to/linux-stanc go test ./internal/bayescodegen/ -run Stanc -v

// stancPath returns the stanc to run, or skips (fails when BAYES_STANC_REQUIRED=1).
func stancPath(t *testing.T) string {
	t.Helper()
	path := os.Getenv("BAYES_STANC")
	if path == "" {
		if os.Getenv("BAYES_STANC_REQUIRED") == "1" {
			t.Fatal("BAYES_STANC_REQUIRED=1 and BAYES_STANC is not set: the stanc check cannot be skipped here")
		}
		t.Skip("BAYES_STANC is not set: the stanc check needs stanc 2.40.0's linux executable " +
			"(the asset linux-stanc of the stanc3 v2.40.0 release, or bin/linux-stanc of the CmdStan tarball), " +
			"e.g. BAYES_STANC=/path/to/linux-stanc go test ./internal/bayescodegen/ -run Stanc -v")
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

// stancRun runs stanc on a program's text and returns its stdout.
func stancRun(t testing.TB, stanc, dir string, name string, text []byte, args ...string) ([]byte, error) {
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

// stancCheck parses and type-checks a program with stanc (--info) and compares the report with the
// program's manifests.
func stancCheck(t testing.TB, stanc, dir, name string, p *Program) {
	t.Helper()
	info, err := stancRun(t, stanc, dir, name, p.Text, "--info")
	if err != nil {
		t.Errorf("%s: stanc refuses the program: %v\n%s", name, err, p.Text)
		return
	}
	for _, m := range CrossCheck(info, p) {
		t.Errorf("%s: %s\n--- program\n%s--- --info\n%s", name, m, p.Text, info)
	}
}

func TestGoldenStanc(t *testing.T) {
	stanc := stancPath(t)
	dir := t.TempDir()
	for _, name := range goldenCases {
		t.Run(name, func(t *testing.T) {
			_, p := generateWith(t, readGraph(t, name), goldenExtension(name))
			want, err := os.ReadFile(filepath.Join(goldenDir, name+".stan"))
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(want, p.Text) {
				t.Fatalf("%s: the golden file is not what Generate writes", name)
			}
			stancCheck(t, stanc, dir, name, p)
			// the compiler's own pass, as `make` runs it (a program stanc reads but cannot write C++ for)
			if _, err := stancRun(t, stanc, dir, name, p.Text, "--o="+filepath.Join(dir, name+".hpp")); err != nil {
				t.Errorf("%s: stanc cannot compile the program: %v", name, err)
			}
		})
	}
}

// TestGeneratedStanc runs stanc on the programs of the generator's graphs (the same ones the
// properties use), with the replicated-outcome extension where the outcome is normal, and with the
// test extension of the checks (lot P1).
func TestGeneratedStanc(t *testing.T) {
	stanc := stancPath(t)
	dir := t.TempDir()
	n := 300
	if testing.Short() {
		n = 40
	}
	checked, withExt := 0, 0
	for seed := 0; seed < n; seed++ {
		data := genGraph(randChooser{rand.New(rand.NewPCG(uint64(seed), 0x57a9c))})
		m, ok := validModel(data)
		if !ok {
			continue
		}
		for _, ext := range []Extension{nil, ppcExtension{}, checksExtension{}} {
			p, err := Generate(m, ext)
			if err != nil {
				t.Fatalf("seed %d: %v\n%s", seed, err, data)
			}
			if ext != nil {
				withExt++
			}
			checked++
			stancCheck(t, stanc, dir, "g"+strconv.Itoa(seed), p)
		}
	}
	if checked < n {
		t.Errorf("only %d programs checked out of %d seeds", checked, n)
	}
	t.Logf("%d programs checked with stanc, %d of them with the extension", checked, withExt)
}
