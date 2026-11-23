package bayescodegen

import (
	"errors"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// graphJSON is a graph on the rows plate with the nodes given (each a JSON object).
func graphJSON(nodes ...string) []byte {
	return []byte(`{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"Rows"}],"nodes":[` +
		strings.Join(nodes, ",") + `]}`)
}

const (
	dataY     = `{"id":"y","kind":"data","name":"y","label":"Y","plate":"rows","type":"real"}`
	dataX     = `{"id":"x","kind":"data","name":"x","label":"X","plate":"rows","type":"real","standardize":true}`
	paramA    = `{"id":"a","kind":"parameter","name":"a","label":"A","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`
	paramS    = `{"id":"s","kind":"parameter","name":"s","label":"S","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`
	likNormal = `{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a"},"sigma":{"expr":"s"}}}`
)

func generateText(t *testing.T, nodes ...string) *Program {
	t.Helper()
	_, p := generateBytes(t, graphJSON(nodes...))
	return p
}

func wantLines(t *testing.T, p *Program, lines ...string) {
	t.Helper()
	text := string(p.Text)
	for _, l := range lines {
		if !strings.Contains(text, "\n"+l+"\n") {
			t.Errorf("the program has no line %q:\n%s", l, text)
		}
	}
}

func TestGenerateRefusesInvalidModels(t *testing.T) {
	if _, err := Generate(nil, nil); !errors.Is(err, ErrInvalidModel) {
		t.Errorf("nil model: %v", err)
	}
	g, _ := bayesgraph.Decode(graphJSON(dataY, paramA)) // no likelihood: graph.likelihood
	if _, err := Generate(bayesgraph.BuildModel(g), nil); !errors.Is(err, ErrInvalidModel) {
		t.Errorf("a model with errors: %v", err)
	}
}

type fakeExtension struct{}

func (fakeExtension) extension() {}

func TestGenerateRefusesAnExtensionUntilS2(t *testing.T) {
	_, m := modelOf(t, graphJSON(dataY, paramA, paramS, likNormal))
	if _, err := Generate(m, fakeExtension{}); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("an extension: %v", err)
	}
}

func TestGenerateRefusesLaterLots(t *testing.T) {
	cases := map[string][]byte{
		"group plate": []byte(`{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"R"},
			{"id":"p1","kind":"group","label":"G","key":"g"}],"nodes":[` + dataY + `,
			{"id":"g","kind":"data","name":"g","label":"G","plate":"rows","type":"group_index","target":"p1"},
			{"id":"a","kind":"parameter","name":"a","label":"A","plate":"p1","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}},` +
			paramS + `,{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a[g]"},"sigma":{"expr":"s"}}}]}`),
		"category": graphJSON(dataY, paramA, paramS,
			`{"id":"c","kind":"data","name":"c","label":"C","plate":"rows","type":"category"}`,
			`{"id":"b","kind":"parameter","name":"b","label":"B","plate":"levels:c","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			`{"id":"eta","kind":"linear_predictor","name":"eta","label":"E","plate":"rows","intercept":{"expr":"a"},"terms":[{"data":"c","coef":{"expr":"b"}}]}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta"},"sigma":{"expr":"s"}}}`),
		"transformed data": graphJSON(dataY, paramA, paramS, dataX,
			`{"id":"lx","kind":"transformed","name":"lx","label":"LX","plate":"rows","value":{"expr":"exp(x)"}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + lx"},"sigma":{"expr":"s"}}}`),
	}
	for name, data := range cases {
		t.Run(name, func(t *testing.T) {
			_, m := modelOf(t, data)
			if _, err := Generate(m, nil); !errors.Is(err, ErrUnsupported) {
				t.Fatalf("want ErrUnsupported, got %v", err)
			}
		})
	}
}

// The truncation rule: bounds narrower than the support with an argument that is not a constant
// give T[…] on the narrowed sides; with constant arguments, the declared bounds alone.
func TestTruncation(t *testing.T) {
	p := generateText(t, dataY, paramA, paramS,
		`{"id":"h","kind":"parameter","name":"h","label":"H","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		// a half-normal whose scale is a parameter: T[0.0, ]
		`{"id":"t1","kind":"parameter","name":"t1","label":"T1","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"h"}}},"bounds":{"lower":0}}`,
		// a gamma cut above, its rate a parameter: T[, 5.0]
		`{"id":"t2","kind":"parameter","name":"t2","label":"T2","prior":{"dist":"gamma","args":{"alpha":{"expr":"2"},"beta":{"expr":"h"}}},"bounds":{"upper":5}}`,
		// both sides, the location a parameter: T[-1.0, 1.0]
		`{"id":"t3","kind":"parameter","name":"t3","label":"T3","prior":{"dist":"normal","args":{"mu":{"expr":"a"},"sigma":{"expr":"1"}}},"bounds":{"lower":-1,"upper":1}}`,
		// a half-normal of constants: the bound, no T
		`{"id":"t4","kind":"parameter","name":"t4","label":"T4","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"2"}}},"bounds":{"lower":0}}`,
		// a uniform: its two constants are the range
		`{"id":"t5","kind":"parameter","name":"t5","label":"T5","prior":{"dist":"uniform","args":{"alpha":{"expr":"-1"},"beta":{"expr":"1e15"}}}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + t1 + t2 + t3 + t4 + t5"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p,
		"  real<lower=0.0> t1;",
		"  real<lower=0.0, upper=5.0> t2;",
		"  real<lower=-1.0, upper=1.0> t3;",
		"  real<lower=0.0> t4;",
		"  real<lower=-1.0, upper=1e+15> t5;",
		"  t1 ~ normal(0.0, h) T[0.0, ];",
		"  t2 ~ gamma(2.0, h) T[, 5.0];",
		"  t3 ~ normal(a, 1.0) T[-1.0, 1.0];",
		"  t4 ~ normal(0.0, 2.0);",
		"  t5 ~ uniform((-1.0), 1e+15);",
	)
}

func TestLikelihoodsAndTerms(t *testing.T) {
	t.Run("binomial with trials and an integer predictor", func(t *testing.T) {
		p := generateText(t, paramA,
			`{"id":"y","kind":"data","name":"y","label":"Y","plate":"rows","type":"int","support":"nonnegative"}`,
			`{"id":"n","kind":"data","name":"n","label":"N","plate":"rows","type":"int","support":"nonnegative"}`,
			`{"id":"k","kind":"data","name":"k","label":"K","plate":"rows","type":"int","support":"any"}`,
			`{"id":"b","kind":"parameter","name":"b","label":"B","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			`{"id":"eta","kind":"linear_predictor","name":"eta","label":"E","plate":"rows","intercept":{"expr":"a"},"terms":[{"data":"k","coef":{"expr":"b"}}]}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"binomial_logit","args":{"N":{"expr":"n"},"alpha":{"expr":"eta"}}}`)
		wantLines(t, p,
			"  array[N] int k;",
			"  array[N] int<lower=0> n;",
			"  vector[N] eta = (a + (b * to_vector(k)));",
			"  y ~ binomial_logit(n, eta);",
		)
	})
	t.Run("a linear predictor without intercept, two terms", func(t *testing.T) {
		p := generateText(t, dataY, paramS, dataX,
			`{"id":"z","kind":"data","name":"z","label":"Z","plate":"rows","type":"real"}`,
			`{"id":"b","kind":"parameter","name":"b","label":"B","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			`{"id":"c","kind":"parameter","name":"c","label":"C","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			`{"id":"eta","kind":"linear_predictor","name":"eta","label":"E","plate":"rows","terms":[{"data":"z","coef":{"expr":"c"}},{"data":"x","coef":{"expr":"b"}}]}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p, "  vector[N] eta = ((c * z) + (b * x));")
	})
	t.Run("a linear predictor that is its intercept alone", func(t *testing.T) {
		p := generateText(t, dataY, paramA, paramS,
			`{"id":"eta","kind":"linear_predictor","name":"eta","label":"E","plate":"rows","intercept":{"expr":"a"}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p, "  vector[N] eta = rep_vector(a, N);")
	})
	t.Run("negative binomial with an expression", func(t *testing.T) {
		p := generateText(t, paramA, paramS, dataX,
			`{"id":"y","kind":"data","name":"y","label":"Y","plate":"rows","type":"int","support":"nonnegative"}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"neg_binomial_2_log","args":{"eta":{"expr":"a + 0.5 * x"},"phi":{"expr":"s"}}}`)
		wantLines(t, p, "  y ~ neg_binomial_2_log((a + (0.5 * x)), s);")
	})
}

func TestTransformedNodes(t *testing.T) {
	p := generateText(t, dataY, paramA, paramS, dataX,
		// a single value of parameters: transformed parameters, written
		`{"id":"sd2","kind":"transformed","name":"sd2","label":"Variance","value":{"expr":"s ^ 2"}}`,
		// a value per row of a parameter: a local of the model block, not written
		`{"id":"mu","kind":"transformed","name":"mu","label":"Mean","plate":"rows","value":{"expr":"a + exp(a) * x"}}`,
		// a single value per row (broadcast)
		`{"id":"m0","kind":"transformed","name":"m0","label":"Constant mean","plate":"rows","value":{"expr":"2 * a"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"mu + m0"},"sigma":{"expr":"sqrt(sd2)"}}}`)
	wantLines(t, p,
		"transformed parameters {",
		"  real sd2 = (s ^ 2.0);",
		"  vector[N] m0 = rep_vector((2.0 * a), N);",
		"  vector[N] mu = (a + (exp(a) * x));",
		"  y ~ normal((mu + m0), sqrt(sd2));",
	)
	var written []string
	for _, q := range p.Quantities {
		written = append(written, q.Stan)
	}
	if got := strings.Join(written, ","); got != "a,s,sd2" {
		t.Errorf("written quantities %s, want a,s,sd2 (no per-row local)", got)
	}
	if !strings.Contains(string(p.Text), "}\ntransformed parameters {\n") || !strings.Contains(string(p.Text), "}\nmodel {\n") {
		t.Errorf("the blocks are not in their order:\n%s", p.Text)
	}
}

// The layout of every block: data, parameters and model always there, others left out when empty.
func TestBlocksAlwaysThere(t *testing.T) {
	p := generateText(t, dataY, paramA, paramS, likNormal)
	want := "data {\n  int<lower=0> N;\n  vector[N] y;\n  int<lower=0, upper=1> Mode;\n}\n" +
		"parameters {\n  real a;\n  real<lower=0.0> s;\n}\n" +
		"model {\n  a ~ normal(0.0, 1.0);\n  s ~ exponential(1.0);\n  y ~ normal(a, s);\n}\n"
	if string(p.Text) != want {
		t.Fatalf("got\n%s\nwant\n%s", p.Text, want)
	}
}

// A model's Slots are the only source of an expression's text: tampering the graph's expression
// text after validation changes nothing (the text is never read).
func TestExpressionTextNeverRead(t *testing.T) {
	g, m := modelOf(t, graphJSON(dataY, paramA, paramS, likNormal))
	before, err := Generate(m, nil)
	if err != nil {
		t.Fatal(err)
	}
	evil := `a); print("x"); //`
	for i := range g.Nodes {
		if g.Nodes[i].Kind == "likelihood" {
			s := g.Nodes[i].Args["mu"]
			s.Expr = &evil
			g.Nodes[i].Args["mu"] = s
		}
		g.Nodes[i].Label = `*/ reject("x"); /*`
	}
	after, err := Generate(m, nil)
	if err != nil {
		t.Fatal(err)
	}
	if string(before.Text) != string(after.Text) {
		t.Fatalf("the program read the graph's text:\n%s", after.Text)
	}
}

// emitEnvOf answers only the identifiers the program has; S0 has no index, lag or subset.
func TestEmitEnv(t *testing.T) {
	g := &gen{ident: map[string]string{"a": "alpha"}}
	e := emitEnv{g}
	if e.Identifier("a") != "alpha" || e.Identifier("nope") != "" {
		t.Error("Identifier")
	}
	if e.IndexArray("g", bayesexpr.Plate("rows")) != "" || e.LagArray(1) != "" || e.RowsSubset() != "" {
		t.Error("S0 answers no index, lag or subset")
	}
}
