package bayescodegen

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
)

// The owner's decision of 2026-11-24 (lot R6): CmdStan's CSV has 8 significant digits and generate_quantities checks a constrained
// type when it reads the draws back, so the manifest of a program with correlated effects says the draws cannot be reread (the
// Cholesky factor's unit-norm check is 1e-8), and no other program says it. Every golden program is held to its own text.
func TestTheManifestSaysWhichProgramsCannotBeReread(t *testing.T) {
	graph, err := os.ReadFile(filepath.Join("testdata", "golden", "hier_slopes.graph.json"))
	if err != nil {
		t.Fatal(err)
	}
	_, p := generateBytes(t, graph)
	typ, no := bayescontract.GQRereadRefusal(p.Quantities)
	if !no || typ != "cholesky_factor_corr" {
		t.Fatalf("hier_slopes: the manifest says %q, %v", typ, no)
	}
	flagged := 0
	for _, q := range p.Quantities {
		if q.GQReread != nil {
			flagged++
			if *q.GQReread || !strings.HasPrefix(q.Stan, "L_") || q.Role != bayescontract.RoleInternal {
				t.Errorf("the quantity %s is marked %v", q.Stan, *q.GQReread)
			}
		}
	}
	if flagged != 1 {
		t.Errorf("%d quantities marked, want the factor alone", flagged)
	}
	if p.CodegenVersion < 4 {
		t.Errorf("codegen version %d: the manifests that say it are version 4's", p.CodegenVersion)
	}
	programs, err := filepath.Glob(filepath.Join("testdata", "golden", "*.graph.json"))
	if err != nil || len(programs) == 0 {
		t.Fatalf("no golden program: %v", err)
	}
	for _, path := range programs {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		_, g := generateBytes(t, data)
		_, no := bayescontract.GQRereadRefusal(g.Quantities)
		// every constrained type the generator writes (the catalogue allows no other: bounds are checked inclusive and written
		// with the bound's own digits)
		if want := strings.Contains(string(g.Text), "cholesky_factor_corr["); no != want {
			t.Errorf("%s: the program has a constrained factor %v, its manifest says it cannot be reread %v", filepath.Base(path), want, no)
		}
	}
}
