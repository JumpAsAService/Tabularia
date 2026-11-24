// Command bayesfixtures writes the programs of the golden test of the priors' marginals
// (docs/specs/bayes-prior-preview.md, lot P3): for each case, the program a save stores for its graph
// (the codegen with predictive-checks' extension) and the quantities of its manifest, read by
// scripts/bayes-prior-fixtures.sh, which compiles and runs them in the group's image. The programs
// are generated from the graphs as they are now, never read from a golden file.
//
//	go run ./cmd/bayesfixtures prepare <dir>
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayeschecks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

const (
	codegenDir = "internal/bayescodegen/testdata/golden"
	checksDir  = "internal/bayeschecks/testdata/golden"
)

// cases are the ten templates, the free cases with hyperpriors, truncations and correlations, and the
// checks module's own graphs of truncations and of three correlated effects (name, graph file).
var cases = [][2]string{
	{"glm_linear", codegenDir + "/glm_linear.graph.json"},
	{"glm_logistic", codegenDir + "/glm_logistic.graph.json"},
	{"glm_poisson", codegenDir + "/glm_poisson.graph.json"},
	{"glm_negbin", codegenDir + "/glm_negbin.graph.json"},
	{"hier_intercepts", codegenDir + "/hier_intercepts.graph.json"},
	{"hier_slopes", codegenDir + "/hier_slopes.graph.json"},
	{"hier_nested", codegenDir + "/hier_nested.graph.json"},
	{"ts_ar2", codegenDir + "/ts_ar2.graph.json"},
	{"ts_random_walk", codegenDir + "/ts_random_walk.graph.json"},
	{"ts_local_level", codegenDir + "/ts_local_level.graph.json"},
	{"ts_local_level_centred", codegenDir + "/ts_local_level_centred.graph.json"},
	{"hier_slopes_centred", codegenDir + "/hier_slopes_centred.graph.json"},
	{"free_truncated_hyper", codegenDir + "/free_truncated_hyper.graph.json"},
	{"free_explicit_noncentred", codegenDir + "/free_explicit_noncentred.graph.json"},
	{"free_category", codegenDir + "/free_category.graph.json"},
	{"truncated", checksDir + "/checks_truncated.graph.json"},
	{"effects3", checksDir + "/checks_effects3.graph.json"},
}

func main() {
	if len(os.Args) != 3 || os.Args[1] != "prepare" {
		fmt.Fprintln(os.Stderr, "usage: bayesfixtures prepare <dir>   (run from gateway-go)")
		os.Exit(2)
	}
	if err := prepare(os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, "bayesfixtures:", err)
		os.Exit(1)
	}
}

func prepare(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for _, c := range cases {
		data, err := os.ReadFile(c[1])
		if err != nil {
			return err
		}
		g, findings := bayesgraph.Decode(data)
		if g == nil {
			return fmt.Errorf("%s: the graph does not decode: %v", c[0], findings)
		}
		m := bayesgraph.BuildModel(g)
		if !m.Valid() {
			return fmt.Errorf("%s: the graph is not a valid model: %+v", c[0], m.Findings)
		}
		p, err := bayescodegen.Generate(m, bayeschecks.Extension{})
		if err != nil {
			return fmt.Errorf("%s: %w", c[0], err)
		}
		manifest, err := json.MarshalIndent(map[string]any{"program_sha256": p.SHA256, "quantities": p.Quantities}, "", "  ")
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(dir, c[0]+".stan"), p.Text, 0o644); err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(dir, c[0]+".manifest.json"), manifest, 0o644); err != nil {
			return err
		}
	}
	return nil
}
