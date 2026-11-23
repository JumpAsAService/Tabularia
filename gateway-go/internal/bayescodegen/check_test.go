package bayescodegen

import (
	"errors"
	"math"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

const okProgram = "data {\n  int<lower=0> N;\n  vector[N] y;\n  int<lower=0, upper=1> Mode;\n}\n" +
	"parameters {\n  real a;\n  real<lower=0.0, upper=1e+15> s;\n}\n" +
	"model {\n  vector[N] eta = rep_vector((a .* 2.5e-05), N);\n  s ~ normal(0.0, 1.0) T[0.0, ];\n  y ~ normal(eta, s);\n}\n"

func TestCheckProgramAccepts(t *testing.T) {
	for _, p := range []string{
		okProgram,
		// what later lots write: a loop, a slice, a conditional size
		"model {\n  for (I in 1:J_p1) {\n    x[I] ~ normal(0.0, 1.0);\n  }\n  y ~ std_normal();\n}\n",
		"generated quantities {\n  array[Mode == 1 ? J_p1 : 0] real V_n1;\n  vector[J_p1] c = to_vector(V_n9[:, 1]);\n}\n",
	} {
		if err := CheckProgram(p); err != nil {
			t.Errorf("%v\n%s", err, p)
		}
	}
}

func TestCheckProgramRefuses(t *testing.T) {
	bad := map[string]string{
		"empty":                "",
		"no final line feed":   "model {\n}",
		"a line comment":       "model {\n  y ~ normal(0.0, 1.0); // x\n}\n",
		"a block comment":      "model {\n  /* x */\n}\n",
		"a hash":               "#include x\nmodel {\n}\n",
		"a quote":              "model {\n  y ~ normal(0.0, 1.0);\n  \"x\";\n}\n",
		"an apostrophe":        "model {\n  y = x';\n}\n",
		"a backslash":          "model {\n  y = x \\ z;\n}\n",
		"a pipe":               "model {\n  y = x | z;\n}\n",
		"an ampersand":         "model {\n  y = x & z;\n}\n",
		"an exclamation":       "model {\n  y = !x;\n}\n",
		"a percent":            "model {\n  y = x % z;\n}\n",
		"an at":                "model {\n  y = x@z;\n}\n",
		"a dollar":             "model {\n  y = x$;\n}\n",
		"a backtick":           "model {\n  y = `x`;\n}\n",
		"a tab":                "model {\n\ty ~ normal(0.0, 1.0);\n}\n",
		"a carriage return":    "model {\r\n}\n",
		"non-ASCII":            "model {\n  é ~ normal(0.0, 1.0);\n}\n",
		"print":                "model {\n  print(y);\n}\n",
		"reject":               "model {\n  reject(y);\n}\n",
		"fatal_error":          "model {\n  fatal_error(y);\n}\n",
		"target":               "model {\n  target += normal_lpdf(y, 0.0, 1.0);\n}\n",
		"include":              "model {\n  include;\n}\n",
		"functions":            "functions {\n}\n",
		"profile":              "model {\n  profile (x) {\n  }\n}\n",
		"a call outside":       "model {\n  y ~ normal(foo(x), 1.0);\n}\n",
		"a call with a space":  "model {\n  y ~ normal(foo (x), 1.0);\n}\n",
		"a statement outside":  "model {\n  y ~ my_dist(x);\n}\n",
		"a density after ~":    "model {\n  y ~ normal_lpdf(x);\n}\n",
		"~ without a name":     "model {\n  y ~ (x);\n}\n",
		"a blank line":         "model {\n\n}\n",
		"a trailing space":     "model {\n  y ~ normal(0.0, 1.0); \n}\n",
		"an odd indentation":   "model {\n   y ~ normal(0.0, 1.0);\n}\n",
		"unbalanced":           "model {\n  y ~ normal(0.0, 1.0;\n}\n",
		"crossed":              "model {\n  y ~ normal(x[1), 1.0];\n}\n",
		"a leading underscore": "model {\n  _y ~ normal(0.0, 1.0);\n}\n",
		"a tuple's point":      "model {\n  y ~ normal(x.1, 1.0);\n}\n",
		"a number and a name":  "model {\n  y ~ normal(1x, 1.0);\n}\n",
		"a number's decimals":  "model {\n  y ~ normal(1., 1.0);\n}\n",
		"a lone question mark": "model {\n  y = a?b:c;\n}\n",
		"a lone colon":         "model {\n  y = (:);\n}\n",
	}
	for what, p := range bad {
		if err := CheckProgram(p); !errors.Is(err, ErrBug) {
			t.Errorf("%s: %v", what, err)
		}
	}
}

// The allow-lists are the catalogue's: every name the program may call or sample with is one.
func TestAllowListsAreTheCatalogues(t *testing.T) {
	c := bayescatalog.Declared()
	for _, d := range c.Distributions {
		if !statements[d.Stan.Statement] || !callable[d.Stan.Density] {
			t.Errorf("%s: its statement or density is not allowed", d.ID)
		}
	}
	for _, f := range c.InternalFunctions {
		if !callable[f] {
			t.Errorf("%s: not callable", f)
		}
	}
	if callable["print"] || callable["reject"] || callable["target"] || statements["normal_lpdf"] {
		t.Error("the allow-lists hold a refused word")
	}
	if _, err := call("system", "x"); !errors.Is(err, ErrBug) {
		t.Error("call writes a name outside the catalogue")
	}
	if _, err := sampling("y", bayescatalog.Distribution{Stan: bayescatalog.StanNames{Statement: "evil"}}, nil, ""); !errors.Is(err, ErrBug) {
		t.Error("sampling writes a statement outside the catalogue")
	}
}

func TestRealLiteral(t *testing.T) {
	cases := map[float64]string{0: "0.0", math.Copysign(0, -1): "0.0", 2.5: "2.5", 10: "10.0", -5: "-5.0",
		1e15: "1e+15", 1e-5: "1e-05", 0.1: "0.1", -0.25: "-0.25", 123456789: "1.23456789e+08", 100000: "100000.0"}
	for v, want := range cases {
		if got, err := realLiteral(v); err != nil || got != want {
			t.Errorf("%v: %q %v, want %q", v, got, err, want)
		}
	}
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if _, err := realLiteral(v); !errors.Is(err, ErrBug) {
			t.Errorf("%v: %v", v, err)
		}
	}
}

func TestWriterRefusesWhatIsNotOneLine(t *testing.T) {
	for _, l := range []string{"", " x;", "x; ", "x;\ny;", "x;\r", "\tx;"} {
		var w writer
		w.line(l)
		if _, err := w.text(); !errors.Is(err, ErrBug) {
			t.Errorf("%q: %v", l, err)
		}
	}
	var w writer
	w.open("model")
	if _, err := w.text(); !errors.Is(err, ErrBug) {
		t.Error("a block left open")
	}
	var v writer
	v.close()
	if _, err := v.text(); !errors.Is(err, ErrBug) {
		t.Error("a block closed that was not open")
	}
}
