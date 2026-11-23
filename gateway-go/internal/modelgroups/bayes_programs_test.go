package modelgroups

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// The programs and the lists of the self-test's checks 6 and 7 (docs/specs/bayes-image.md, «The self-test») are generated from the
// distribution catalogue (internal/bayescatalog/catalog.json) by this test and kept as golden files of the group `bayes`:
//
//   - reference/programs/catalog_names.stan calls every name of the catalogue's «Stan» column (the statement, the density or mass,
//     the generator, the CDFs where the catalogue has them, the binomial's generator through its inverse link) and every function
//     of the expression language by its Stan name; reference/data/catalog_names.json is what `stanc --info` must report of it, no
//     more and no less (a `~` statement is reported as its _lupdf or _lupmf);
//   - reference/programs/catalog_points.stan evaluates, for every distribution of one scalar outcome, its _lpdf (or _lpmf) and its
//     _lcdf (where the catalogue has one) at points given as data; reference/data/catalog_points.json says, distribution by
//     distribution, which arguments are real and which are integers, and whether the outcome is an integer, so that the runner
//     writes the cases of points.json as the program's data.
//
// When the catalogue changes and the files do not, the test fails; it rewrites them with
//
//	TABULARIA_UPDATE_GOLDEN=1 go test ./internal/modelgroups/ -run TestTheBayesProgramsAreTheCataloguesNames
type catalogueForPrograms struct {
	Distributions []struct {
		ID     string `json:"id"`
		Params []struct {
			Name       string `json:"name"`
			Constraint string `json:"constraint"`
		} `json:"params"`
		Support string `json:"support"`
		Stan    struct {
			Statement      string `json:"statement"`
			Density        string `json:"density"`
			RNG            string `json:"rng"`
			RNGInverseLink string `json:"rng_inverse_link"`
			LCDF           string `json:"lcdf"`
			LCCDF          string `json:"lccdf"`
		} `json:"stan"`
	} `json:"distributions"`
	Language struct {
		Functions []struct {
			Name      string `json:"name"`
			Stan      string `json:"stan"`
			Arguments int    `json:"arguments"`
		} `json:"functions"`
	} `json:"language"`
}

// The data each support and each argument's constraint is written with in catalog_names.stan (only stanc reads the program: the
// types matter, not the values), and the type a generator returns.
var (
	outcomeVar     = map[string]string{"real": "x", "positive": "x", "unit": "x", "bounded": "x", "binary": "y", "zero_to_n": "y", "counts": "y", "cholesky_factor_corr": "Lc", "vector": "v"}
	argumentVar    = map[string]string{"real": "x", "positive": "x", "unit": "x", "non_negative_integer": "n", "vector": "v", "cholesky_factor_cov": "Lv"}
	drawType       = map[string]string{"real": "real", "positive": "real", "unit": "real", "bounded": "real", "binary": "int", "zero_to_n": "int", "counts": "int", "cholesky_factor_corr": "matrix[2, 2]", "vector": "vector[2]"}
	integerOutcome = map[string]bool{"binary": true, "zero_to_n": true, "counts": true}
)

const generatedHeader = "// Generated from gateway-go/internal/bayescatalog/catalog.json by TestTheBayesProgramsAreTheCataloguesNames\n" +
	"// (gateway-go/internal/modelgroups/bayes_programs_test.go): do not edit.\n"

// call is `f(outcome | args)`, or `f(outcome)` without arguments.
func call(f, outcome string, args []string) string {
	if len(args) == 0 {
		return f + "(" + outcome + ")"
	}
	return f + "(" + outcome + " | " + strings.Join(args, ", ") + ")"
}

func bayesPrograms(t *testing.T) map[string][]byte {
	t.Helper()
	var c catalogueForPrograms
	if err := json.Unmarshal(bayescatalog.Raw(), &c); err != nil {
		t.Fatal(err)
	}
	// catalog_names.stan and what stanc must report of it
	var model, gq strings.Builder
	functions, distributions := map[string]bool{}, map[string]bool{}
	for _, d := range c.Distributions {
		out, ok := outcomeVar[d.Support]
		if !ok {
			t.Fatalf("%s: the support %q has no outcome in catalog_names.stan", d.ID, d.Support)
		}
		var args []string
		for _, p := range d.Params {
			a, ok := argumentVar[p.Constraint]
			if !ok {
				t.Fatalf("%s: the constraint %q has no argument in catalog_names.stan", d.ID, p.Constraint)
			}
			args = append(args, a)
		}
		fmt.Fprintf(&model, "  // %s\n  %s ~ %s(%s);\n", d.ID, out, d.Stan.Statement, strings.Join(args, ", "))
		suffix := "_lupdf"
		if strings.HasSuffix(d.Stan.Density, "_lpmf") {
			suffix = "_lupmf"
		}
		distributions[d.Stan.Statement+suffix] = true
		for _, f := range []string{d.Stan.Density, d.Stan.LCDF, d.Stan.LCCDF} {
			if f != "" {
				fmt.Fprintf(&model, "  target += %s;\n", call(f, out, args))
				distributions[f] = true
			}
		}
		// a generator of correlation matrices takes their dimension first; the binomial's takes the probability through the
		// catalogue's inverse link (binomial_rng(N, inv_logit(alpha)))
		rngArgs := append([]string(nil), args...)
		if d.Support == "cholesky_factor_corr" {
			rngArgs = append([]string{"2"}, rngArgs...)
		}
		if d.Stan.RNGInverseLink != "" {
			last := len(rngArgs) - 1
			rngArgs[last] = d.Stan.RNGInverseLink + "(" + rngArgs[last] + ")"
			functions[d.Stan.RNGInverseLink] = true
		}
		fmt.Fprintf(&gq, "  %s draw_%s = %s(%s);\n", drawType[d.Support], d.ID, d.Stan.RNG, strings.Join(rngArgs, ", "))
		functions[d.Stan.RNG] = true
	}
	for _, f := range c.Language.Functions {
		if f.Stan == "" {
			continue // lag: the language's own, written by the codegen as indexing
		}
		if f.Arguments != 1 {
			t.Fatalf("%s: %d arguments, catalog_names.stan calls a function of one", f.Name, f.Arguments)
		}
		fmt.Fprintf(&gq, "  real function_%s = %s(x);\n", f.Name, f.Stan)
		functions[f.Stan] = true
	}
	names := generatedHeader +
		"// The self-test's check 6 (docs/specs/bayes-image.md, «The self-test»): every name of the catalogue's «Stan» column and every\n" +
		"// function of the expression language, called once, so that `stanc --info` reports each (only stanc reads this program).\n" +
		"data {\n  real x;\n  int y;\n  int<lower=0> n;\n  vector[2] v;\n  cholesky_factor_corr[2] Lc;\n  cholesky_factor_cov[2] Lv;\n}\n" +
		"parameters {\n  real theta;\n}\nmodel {\n  theta ~ std_normal();\n" + model.String() + "}\n" +
		"generated quantities {\n" + gq.String() + "}\n"
	// theta's own statement is std_normal's, already among the names
	sorted := func(m map[string]bool) []string {
		out := make([]string, 0, len(m))
		for k := range m {
			out = append(out, k)
		}
		sort.Strings(out)
		return out
	}
	namesJSON, _ := json.MarshalIndent(map[string]any{"format_version": 1, "functions": sorted(functions), "distributions": sorted(distributions)}, "", "  ")

	// catalog_points.stan and its layout: the distributions of one scalar outcome
	var data, decl, loops strings.Builder
	type layout struct {
		Real    []int `json:"real"`    // the positions (from 0) of the real arguments, in the order of a_<id>
		Integer []int `json:"integer"` // those of the integer ones, in the order of i_<id>
		IntX    bool  `json:"integer_outcome"`
		LCDF    bool  `json:"lcdf"`
	}
	layouts := map[string]layout{}
	for _, d := range c.Distributions {
		if _, scalar := drawType[d.Support]; !scalar || strings.HasPrefix(drawType[d.Support], "matrix") || strings.HasPrefix(drawType[d.Support], "vector") {
			continue
		}
		var l layout
		var args []string
		for i, p := range d.Params {
			if p.Constraint == "non_negative_integer" {
				l.Integer = append(l.Integer, i)
				args = append(args, fmt.Sprintf("i_%s[i, %d]", d.ID, len(l.Integer)))
			} else {
				l.Real = append(l.Real, i)
				args = append(args, fmt.Sprintf("a_%s[i, %d]", d.ID, len(l.Real)))
			}
		}
		l.IntX, l.LCDF = integerOutcome[d.Support], d.Stan.LCDF != ""
		if l.Real == nil {
			l.Real = []int{}
		}
		if l.Integer == nil {
			l.Integer = []int{}
		}
		layouts[d.ID] = l
		fmt.Fprintf(&data, "  int<lower=0> n_%s;\n", d.ID)
		if len(l.Real) > 0 {
			fmt.Fprintf(&data, "  array[n_%s, %d] real a_%s;\n", d.ID, len(l.Real), d.ID)
		}
		if len(l.Integer) > 0 {
			fmt.Fprintf(&data, "  array[n_%s, %d] int i_%s;\n", d.ID, len(l.Integer), d.ID)
		}
		x := "real"
		if l.IntX {
			x = "int"
		}
		fmt.Fprintf(&data, "  array[n_%s] %s x_%s;\n", d.ID, x, d.ID)
		fmt.Fprintf(&decl, "  array[n_%s] real lp_%s;\n", d.ID, d.ID)
		fmt.Fprintf(&loops, "  for (i in 1:n_%s) {\n    lp_%s[i] = %s;\n", d.ID, d.ID, call(d.Stan.Density, "x_"+d.ID+"[i]", args))
		if l.LCDF {
			fmt.Fprintf(&decl, "  array[n_%s] real lcdf_%s;\n", d.ID, d.ID)
			fmt.Fprintf(&loops, "    lcdf_%s[i] = %s;\n", d.ID, call(d.Stan.LCDF, "x_"+d.ID+"[i]", args))
		}
		loops.WriteString("  }\n")
	}
	points := generatedHeader +
		"// The self-test's check 7 (docs/specs/bayes-image.md, «The self-test»): Stan's own log-density (or log-mass) and log-CDF of\n" +
		"// every distribution of one scalar outcome, at the witness's points (reference/data/points.json, written as this program's\n" +
		"// data by the runner); run once with the fixed_param sampler.\n" +
		"data {\n" + data.String() + "}\n" + "generated quantities {\n" + decl.String() + loops.String() + "}\n"
	pointsJSON, _ := json.MarshalIndent(map[string]any{"format_version": 1, "distributions": layouts}, "", "  ")
	return map[string][]byte{
		"groups/bayes/reference/programs/catalog_names.stan":  []byte(names),
		"groups/bayes/reference/data/catalog_names.json":      append(namesJSON, '\n'),
		"groups/bayes/reference/programs/catalog_points.stan": []byte(points),
		"groups/bayes/reference/data/catalog_points.json":     append(pointsJSON, '\n'),
	}
}

func TestTheBayesProgramsAreTheCataloguesNames(t *testing.T) {
	update := os.Getenv("TABULARIA_UPDATE_GOLDEN") == "1"
	for path, want := range bayesPrograms(t) {
		if update {
			if err := os.WriteFile(path, want, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		got, err := files.ReadFile(path)
		if err != nil || !bytes.Equal(got, want) {
			t.Errorf("%s is not what the catalogue generates (%v): TABULARIA_UPDATE_GOLDEN=1 go test ./internal/modelgroups/ -run %s",
				path, err, "TestTheBayesProgramsAreTheCataloguesNames")
		}
	}
}

// The witness's points travel with the group byte for byte (the self-test's check 7 reads reference/data/points.json).
func TestTheBayesPointsAreTheCataloguesWitness(t *testing.T) {
	want, err := os.ReadFile("../bayescatalog/testdata/points.json")
	if err != nil {
		t.Fatal(err)
	}
	got, err := files.ReadFile("groups/bayes/reference/data/points.json")
	if err != nil || !bytes.Equal(got, want) {
		t.Errorf("groups/bayes/reference/data/points.json is not internal/bayescatalog/testdata/points.json, byte for byte (%v): copy it", err)
	}
}
