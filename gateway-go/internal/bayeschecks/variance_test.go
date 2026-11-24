package bayeschecks

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// param is a single parameter node `id` with this prior (arguments as expressions) and bounds.
func param(id, dist string, args map[string]string, bounds string) string {
	var a []string
	for k, v := range args {
		a = append(a, fmt.Sprintf(`%q: {"expr": %q}`, k, v))
	}
	slices.Sort(a)
	b := ""
	if bounds != "" {
		b = `, "bounds": ` + bounds
	}
	return fmt.Sprintf(`{"id": %q, "kind": "parameter", "name": %q, "label": %q, "plate": null, "prior": {"dist": %q, "args": {%s}}%s}`,
		id, id, id, dist, strings.Join(a, ", "), b)
}

func transformed(id, expr string) string {
	return fmt.Sprintf(`{"id": %q, "kind": "transformed", "name": %q, "label": %q, "plate": null, "value": {"expr": %q}}`, id, id, id, expr)
}

// varianceModel is a graph of these nodes, observed through y ~ normal(mu, 1).
func varianceModel(t *testing.T, nodes ...string) *bayesgraph.Model {
	t.Helper()
	doc := `{"format_version": 1, "plates": [{"id": "rows", "kind": "rows", "label": "Rows"}], "nodes": [
		{"id": "y", "kind": "data", "name": "y", "label": "y", "plate": "rows", "type": "real"},
		{"id": "lik", "kind": "likelihood", "label": "lik", "observed": "y", "dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "1"}}},
		` + strings.Join(nodes, ",\n") + `]}`
	m, ok := validModel([]byte(doc))
	if !ok {
		if m == nil {
			t.Fatalf("the graph does not decode:\n%s", doc)
		}
		t.Fatalf("not valid: %+v", m.Findings)
	}
	return m
}

// FiniteVariance on the spec's cases (bayes-runner.md, «Testing strategy»): a Cauchy prior, a
// Student-t with ν 3 and 2, an inverse-gamma with α 2.5, a half-Cauchy truncated to [0, 10], a
// normal whose scale has an inverse-gamma prior with α 1.5; and the prudent cases of «As built — R4».
func TestFiniteVariance(t *testing.T) {
	normal01 := map[string]string{"mu": "0", "sigma": "1"}
	cases := []struct {
		name   string
		nodes  []string
		finite []string
	}{
		{"a Cauchy prior", []string{param("mu", "cauchy", normal01, "")}, nil},
		{"a Student-t with nu 3", []string{param("mu", "student_t", map[string]string{"nu": "3", "mu": "0", "sigma": "1"}, "")}, []string{"mu"}},
		{"a Student-t with nu 2", []string{param("mu", "student_t", map[string]string{"nu": "2", "mu": "0", "sigma": "1"}, "")}, nil},
		{"an inverse-gamma with alpha 2.5", []string{param("mu", "inv_gamma", map[string]string{"alpha": "2.5", "beta": "1"}, "")}, []string{"mu"}},
		{"an inverse-gamma with alpha 2", []string{param("mu", "inv_gamma", map[string]string{"alpha": "2", "beta": "1"}, "")}, nil},
		{"a half-Cauchy truncated to [0, 10]", []string{param("mu", "cauchy", normal01, `{"lower": 0, "upper": 10}`)}, []string{"mu"}},
		{"a half-Cauchy", []string{param("mu", "cauchy", normal01, `{"lower": 0}`)}, nil},
		{"a normal whose scale has an inverse-gamma prior with alpha 1.5", []string{
			param("s", "inv_gamma", map[string]string{"alpha": "1.5", "beta": "1"}, ""),
			param("mu", "normal", map[string]string{"mu": "0", "sigma": "s"}, "")}, nil},
		{"a normal whose scale has an exponential prior", []string{
			param("s", "exponential", map[string]string{"beta": "1"}, ""),
			param("mu", "normal", map[string]string{"mu": "0", "sigma": "2 * s"}, "")}, []string{"s", "mu"}},
		{"a normal whose scale is a function of a parameter: not proven", []string{
			param("s", "normal", normal01, ""),
			param("mu", "normal", map[string]string{"mu": "0", "sigma": "exp(s)"}, "")}, []string{"s"}},
		{"a rate that varies: not proven", []string{
			param("r", "gamma", map[string]string{"alpha": "1", "beta": "1"}, ""),
			param("mu", "exponential", map[string]string{"beta": "r"}, "")}, []string{"r"}},
		{"a nu that varies: not proven", []string{
			param("n", "gamma", map[string]string{"alpha": "2", "beta": "0.1"}, ""),
			param("mu", "student_t", map[string]string{"nu": "n", "mu": "0", "sigma": "1"}, "")}, []string{"n"}},
		{"a bounded support whatever its parents", []string{
			param("a", "cauchy", normal01, `{"lower": 0}`),
			param("mu", "beta", map[string]string{"alpha": "a", "beta": "1"}, "")}, []string{"mu"}},
		{"transformed values: affine, bounded, a function", []string{
			param("b", "normal", normal01, ""),
			transformed("mu", "2 * b + 1 - b / 4"),
			transformed("e", "exp(b)"),
			transformed("p", "inv_logit(b)"),
			transformed("q", "b * b")}, []string{"b", "mu", "p"}},
		{"a transformed value of a Cauchy", []string{
			param("c", "cauchy", normal01, ""),
			transformed("mu", "c + 1")}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := varianceModel(t, c.nodes...)
			got := FiniteVariance(m)
			if got == nil {
				t.Fatal("nil for a valid model")
			}
			want := c.finite
			if want == nil {
				want = []string{}
			}
			slices.Sort(got)
			slices.Sort(want)
			if !slices.Equal(got, want) {
				t.Errorf("finite %v, want %v (marginals %v)", got, want, Analyze(m).Marginals)
			}
		})
	}
	if FiniteVariance(nil) != nil {
		t.Error("nil model: not nil")
	}
}

// On every valid graph at hand, FiniteVariance is a subset of the marginals, in their order, and
// each is the `of` of a prior_marginal of the program; the correlated effects' components and
// correlation of the golden hier_slopes are finite.
func TestFiniteVarianceOnTheCorpus(t *testing.T) {
	for name, data := range corpusGraphs(t) {
		m, ok := validModel(data)
		if !ok {
			continue
		}
		fv := FiniteVariance(m)
		marg := Analyze(m).Marginals
		i := 0
		for _, q := range fv {
			for i < len(marg) && marg[i] != q {
				i++
			}
			if i == len(marg) {
				t.Errorf("%s: %v is not an ordered subset of the marginals %v", name, fv, marg)
				break
			}
		}
	}
	data := readFile(t, filepath.Join(codegenDir, "hier_slopes.graph.json"))
	m, _ := validModel(data)
	got := FiniteVariance(m)
	for _, q := range []string{"mua", "mub", "taua", "taub", "eff.ca", "eff.cb", "eff.correlation", "sigma"} {
		if !slices.Contains(got, q) {
			t.Errorf("hier_slopes: %s not finite (%v)", q, got)
		}
	}
	p, err := bayescodegen.Generate(m, Extension{})
	if err != nil {
		t.Fatal(err)
	}
	ofs := map[string]bool{}
	for _, q := range p.Quantities {
		if q.Role == bayescontract.RolePriorMarginal {
			ofs[q.Of] = true
		}
	}
	for _, q := range got {
		if !ofs[q] {
			t.Errorf("hier_slopes: %s is no prior_marginal's of", q)
		}
	}
}

// ScalePairs: a term and the intercept as Stan identifiers; a varying intercept taken at the row's
// unit; a category's term is no pair (a term's coefficient is always a parameter: model-graph's
// coef_not_parameter).
func TestScalePairs(t *testing.T) {
	pairs := func(name string) string {
		data := readFile(t, filepath.Join(codegenDir, name+".graph.json"))
		m, ok := validModel(data)
		if !ok {
			t.Fatalf("%s: not valid", name)
		}
		p, err := bayescodegen.Generate(m, Extension{})
		if err != nil {
			t.Fatal(err)
		}
		b, _ := json.Marshal(ScalePairs(m, p.Data, p.Quantities))
		return string(b)
	}
	for name, want := range map[string]string{
		"glm_linear":      `[{"coef":"b_x","data":"x"},{"intercept":"alpha","lp":["b_x"],"data":["x"]}]`,
		"hier_intercepts": `[{"coef":"b_x","data":"x"},{"intercept":"a_g","lp":["b_x"],"data":["x"]}]`,
		"free_category":   `[{"coef":"b_x","data":"x"},{"intercept":"alpha","lp":["b_x"],"data":["x"]}]`,
		"hier_slopes":     `[]`,
	} {
		if got := pairs(name); got != want {
			t.Errorf("%s: %s, want %s", name, got, want)
		}
	}
}
