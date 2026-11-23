package bayescatalog

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// Every internal function of the catalogue (and every function a quantile's form writes) is a
// function of the pinned Stan: a program that calls each of them once passes stanc --info, which
// reports each, as the self-test's check 6 does for the catalogue's «Stan» column
// (docs/specs/bayes-image.md). Stan 2.40 has no fabs: a name stanc does not know fails here, not in
// a saved program. BAYES_STANC is stanc 2.40.0's linux executable; without it the test is skipped,
// and BAYES_STANC_REQUIRED=1 (the workflow bayes-codegen.yml) makes the skip a failure:
//
//	BAYES_STANC=/path/to/linux-stanc go test ./internal/bayescatalog/ -run Stanc -v

// internalCalls is a call of each internal function with arguments of a type it takes (the
// variables of internalProgram), or a statement for a distribution's name. A name added to the
// catalogue needs its call here.
var internalCalls = map[string]string{
	"to_vector":                         "to_vector(a)",
	"rep_vector":                        "rep_vector(x, n)",
	"append_row":                        "append_row(x, v)",
	"diag_pre_multiply":                 "diag_pre_multiply(v, L)",
	"multiply_lower_tri_self_transpose": "multiply_lower_tri_self_transpose(L)",
	"inv_logit":                         "inv_logit(x)",
	"std_normal":                        "~",
	"mean":                              "mean(v)",
	"sd":                                "sd(v)",
	"min":                               "min(v)",
	"max":                               "max(v)",
	"quantile":                          "quantile(v, 0.5)",
	"floor":                             "floor(x)",
	"fmin":                              "fmin(x, x)",
	"fmax":                              "fmax(x, x)",
	"to_int":                            "to_int(x)",
	"append_array":                      "append_array(a, a)",
	"rep_array":                         "rep_array(x, n)",
	"inv_Phi":                           "inv_Phi(x)",
	"inv_inc_beta":                      "inv_inc_beta(x, x, x)",
	"tan":                               "tan(x)",
	"log1m":                             "log1m(x)",
	"uniform_rng":                       "uniform_rng(x, x)",
	"Phi":                               "Phi(x)",
	"atan":                              "atan(x)",
	"inc_beta":                          "inc_beta(x, x, x)",
	"step":                              "step(x)",
	"inv":                               "inv(x)",
	// functions of the language a quantile's form writes
	"square": "square(x)",
	"sqrt":   "sqrt(x)",
	"exp":    "exp(x)",
	"log":    "log(x)",
	"expm1":  "expm1(x)",
}

// internalProgram calls each name once, each in a nested block of the generated quantities (where
// a generator and to_int of a real may be called) holding a local of the call's type; a
// distribution's name is a statement of the model block.
func internalProgram(t *testing.T, names []string) []byte {
	t.Helper()
	var model, gq strings.Builder
	for _, name := range names {
		call, ok := internalCalls[name]
		switch {
		case !ok:
			t.Fatalf("the internal function %q has no call in internalCalls: add one", name)
		case call == "~":
			model.WriteString("  theta ~ " + name + "();\n")
		default:
			gq.WriteString("  {\n    " + resultType(name) + " r = " + call + ";\n  }\n")
		}
	}
	return []byte("data {\n  real x;\n  int<lower=0> n;\n  vector[2] v;\n  array[2] real a;\n  cholesky_factor_corr[2] L;\n}\n" +
		"parameters {\n  real theta;\n}\n" +
		"model {\n" + model.String() + "}\n" +
		"generated quantities {\n" + gq.String() + "}\n")
}

// resultType is the Stan type of the call of internalCalls of a name.
func resultType(name string) string {
	switch name {
	case "to_vector":
		return "vector[2]"
	case "rep_vector":
		return "vector[n]"
	case "append_row":
		return "vector[3]"
	case "diag_pre_multiply", "multiply_lower_tri_self_transpose":
		return "matrix[2, 2]"
	case "to_int":
		return "int"
	case "append_array":
		return "array[4] real"
	case "rep_array":
		return "array[n] real"
	}
	return "real"
}

func TestInternalFunctionsStanc(t *testing.T) {
	stanc := os.Getenv("BAYES_STANC")
	if stanc == "" {
		if os.Getenv("BAYES_STANC_REQUIRED") == "1" {
			t.Fatal("BAYES_STANC_REQUIRED=1 and BAYES_STANC is not set: the stanc check cannot be skipped here")
		}
		t.Skip("BAYES_STANC is not set: the stanc check needs stanc 2.40.0's linux executable")
	}
	if out, err := exec.Command(stanc, "--version").CombinedOutput(); err != nil {
		t.Fatalf("%s --version: %v\n%s", stanc, err, out)
	} else if want := "v" + StanLists().Version; !strings.Contains(string(out), want) {
		t.Fatalf("stanc says %q, the catalogue pins Stan %s", strings.TrimSpace(string(out)), want)
	}
	names := map[string]bool{}
	for _, f := range Declared().InternalFunctions {
		names[f] = true
	}
	for _, fs := range QuantileFunctions {
		for _, f := range fs {
			names[f] = true
		}
	}
	sorted := make([]string, 0, len(names))
	for n := range names {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)
	text := internalProgram(t, sorted)

	dir := t.TempDir()
	file := filepath.Join(dir, "internal_functions.stan")
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
		t.Fatalf("stanc refuses the program of the internal functions: %v\n%s\n%s", err, stderr.String(), text)
	}
	var info struct {
		Functions     []string `json:"functions"`
		Distributions []string `json:"distributions"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &info); err != nil {
		t.Fatalf("stanc --info: %v\n%s", err, stdout.String())
	}
	reported := map[string]bool{}
	for _, f := range info.Functions {
		reported[f] = true
	}
	for _, d := range info.Distributions {
		reported[strings.TrimSuffix(d, "_lupdf")] = true
	}
	for _, n := range sorted {
		if !reported[n] {
			t.Errorf("stanc --info does not report %q\n%s", n, stdout.String())
		}
	}
	t.Logf("%d functions checked with stanc", len(sorted))
}
