package bayeschecks

import (
	"bytes"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayestemplates"
)

// The extension's properties on every valid graph at hand: the golden cases, the templates as the
// gallery offers them and opened with their example numbers, and the validator's corpus.

func platesOf(m *bayesgraph.Model) []bayescontract.Plate {
	var out []bayescontract.Plate
	for _, p := range m.Plates {
		out = append(out, bayescontract.Plate{ID: p.ID, Kind: p.Kind, Label: p.Label})
	}
	return out
}

// corpusGraphs are the valid graphs at hand, by a name.
func corpusGraphs(t testing.TB) map[string][]byte {
	t.Helper()
	out := map[string][]byte{}
	for _, c := range goldenCases(t) {
		out[c.name] = readFile(t, c.graph)
	}
	for _, tp := range bayestemplates.List() {
		out["template_"+tp.ID] = tp.Graph
	}
	opened, _ := filepath.Glob("../bayestemplates/testdata/opened/*.json")
	for _, f := range opened {
		out["opened_"+filepath.Base(f)] = readFile(t, f)
	}
	corpus, _ := filepath.Glob("../bayesgraph/testdata/corpus/*.json")
	for _, f := range corpus {
		var cs struct {
			Input json.RawMessage `json:"input"`
			Raw   *string         `json:"raw"`
		}
		if json.Unmarshal(readFile(t, f), &cs) != nil {
			continue
		}
		doc := []byte(cs.Input)
		if cs.Raw != nil {
			doc = []byte(*cs.Raw)
		}
		out["corpus_"+filepath.Base(f)] = doc
	}
	if len(out) < 100 {
		t.Fatalf("only %d graphs", len(out))
	}
	return out
}

func validModel(data []byte) (*bayesgraph.Model, bool) {
	g, _ := bayesgraph.Decode(data)
	if g == nil {
		return nil, false
	}
	m := bayesgraph.BuildModel(g)
	return m, m.Valid()
}

// checkProgram checks what every program with the extension must hold beyond what Generate checks
// (CheckProgram, CheckIdentifiers): manifests that hold together with the contract's check, every
// check naming its outcome and every outcome datum too, every ancestral draw a quantity, sizes by
// the mode or the extension's integers, the caps.
func checkProgram(t testing.TB, name string, m *bayesgraph.Model, p *bayescodegen.Program) {
	t.Helper()
	a := Analyze(m)
	if err := bayescontract.CheckManifests(platesOf(m), p.Mapping, p.Data, p.Quantities); err != nil {
		t.Errorf("%s: %v", name, err)
	}
	observed := map[string]bool{}
	for _, o := range a.Outcomes {
		observed[o.Observed] = true
	}
	if len(a.Outcomes) > MaxOutcomes || len(a.Outcomes) == 0 && a.Likelihoods > 0 {
		t.Errorf("%s: %d outcomes of %d likelihoods", name, len(a.Outcomes), a.Likelihoods)
	}
	nodes := map[string]bool{}
	quantities := map[string]string{}
	for _, q := range p.Quantities {
		if q.Role == bayescontract.RoleQuantity {
			nodes[q.Node] = true
			quantities[q.Node] = q.Stan
		}
	}
	// the check columns at the most the runner sizes them: the flag 1, 40 bins and the two outer
	// slots, 8 units and the others' slot, no replicated row
	sizes := bayescontract.Sizes{}
	ext := map[string]bool{}
	for _, d := range p.Data {
		switch {
		case strings.HasPrefix(d.Stan, "Ppc_"):
			ext[d.Stan] = true
			if d.Stan != "Ppc_on" && (d.Node == "" || !observed[d.Node] && d.Kind != "unit_slot") {
				t.Errorf("%s: the datum %s names no outcome (%q)", name, d.Stan, d.Node)
			}
		case strings.HasPrefix(d.Stan, "Prior_"):
			ext[d.Stan] = true
		}
		switch {
		case d.Stan == "Ppc_on":
			sizes[d.Stan] = 1
		case strings.HasSuffix(d.Stan, "_slots"):
			sizes[d.Stan] = ValueBins + 2
		case strings.HasSuffix(d.Stan, "_units"):
			sizes[d.Stan] = MaxUnits + 1
		default:
			sizes[d.Stan] = 0
		}
	}
	// each outcome's positions name the rows they are among: Rows_lag exactly for a lagged likelihood,
	// whose statement runs on y[Rows_lag] (Correction of 2026-11-24 from P3a)
	lagged := map[string]bool{}
	for _, o := range a.Outcomes {
		for _, d := range p.Data {
			if d.Node == o.Observed && bayescontract.DataReadsColumn(d.Kind) {
				lagged[o.Observed] = strings.Contains(string(p.Text), "\n  "+d.Stan+"[Rows_lag] ~ ")
			}
		}
	}
	for _, d := range p.Data {
		if d.Kind != "post_rows" && d.Kind != "prior_rows" {
			continue
		}
		want := "rows"
		if lagged[d.Node] {
			want = "Rows_lag"
		}
		if d.Of != want {
			t.Errorf("%s: the positions %s are among %q, the statement's rows are %q", name, d.Stan, d.Of, want)
		}
	}
	if !a.PriorPredictive {
		for d := range ext {
			if strings.HasPrefix(d, "Prior_") {
				t.Errorf("%s: no prior predictive, and yet the datum %s", name, d)
			}
		}
	}
	checks, marginals, draws := 0, map[string]bool{}, map[string]bool{}
	for _, q := range p.Quantities {
		switch q.Role {
		case bayescontract.RoleCheck:
			if !observed[q.Of] {
				t.Errorf("%s: the check %s names no outcome (%q)", name, q.Stan, q.Of)
			}
			for _, w := range []string{"p_value", "pvalue", "prob", "share", "ppp"} {
				if strings.Contains(q.Check, w) {
					t.Errorf("%s: the check %s is a %s (answer 12)", name, q.Stan, w)
				}
			}
			n := 1
			for _, d := range q.Dims {
				if d.Size == "" {
					t.Errorf("%s: the check %s has a dimension that is not an extension's integer: %+v", name, q.Stan, d)
				}
				n *= sizes[d.Size]
			}
			checks += n
		case bayescontract.RolePriorMarginal:
			if !nodes[q.Of] || len(q.Dims) != 0 || marginals[q.Of] {
				t.Errorf("%s: the marginal %s of %q", name, q.Stan, q.Of)
			}
			marginals[q.Of] = true
		case bayescontract.RolePriorDraw:
			if !a.PriorPredictive || !nodes[q.Of] || draws[q.Of] {
				t.Errorf("%s: the prior draw %s of %q", name, q.Stan, q.Of)
			}
			draws[q.Of] = true
		}
	}
	if checks > 200 {
		t.Errorf("%s: %d check columns, over max_check_columns (200)", name, checks)
	}
	for _, id := range a.Marginals {
		if !marginals[id] {
			t.Errorf("%s: the analysis says %s has a marginal, the program writes none", name, id)
		}
	}
	if len(marginals) != len(a.Marginals) {
		t.Errorf("%s: %d marginals written, %d in the analysis", name, len(marginals), len(a.Marginals))
	}
	// in the prior predictive, every parameter of the model has its draw
	if a.PriorPredictive {
		for _, n := range m.Graph.Nodes {
			if n.Kind == "parameter" && !draws[n.ID] {
				t.Errorf("%s: the prior predictive and no draw of the parameter %s", name, n.ID)
			}
		}
	}
}

func TestEveryGraphHasTheExtension(t *testing.T) {
	valid, prior, withMarginals := 0, 0, 0
	var without []string
	for name, data := range corpusGraphs(t) {
		m, ok := validModel(data)
		if !ok {
			continue
		}
		valid++
		p, err := bayescodegen.Generate(m, Extension{})
		if errors.Is(err, bayescodegen.ErrUnsupported) {
			continue
		}
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		checkProgram(t, name, m, p)
		if a := Analyze(m); a.PriorPredictive {
			prior++
		} else {
			without = append(without, name+" ("+a.NoPrior+")")
		}
		if len(Analyze(m).Marginals) > 0 {
			withMarginals++
		}
	}
	t.Logf("%d valid graphs, %d with a prior predictive, %d with marginals; without: %v", valid, prior, withMarginals, without)
	if valid < 50 || prior < valid/2 {
		t.Errorf("%d valid graphs, %d with a prior predictive", valid, prior)
	}
}

// shuffled is a graph's JSON with its nodes and plates in another order.
func shuffled(t testing.TB, data []byte, seed uint64) []byte {
	t.Helper()
	var g map[string]any
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatal(err)
	}
	r := rand.New(rand.NewPCG(seed, 7))
	for _, k := range []string{"nodes", "plates"} {
		if xs, ok := g[k].([]any); ok {
			r.Shuffle(len(xs), func(i, j int) { xs[i], xs[j] = xs[j], xs[i] })
		}
	}
	out, err := json.Marshal(g)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// The same bytes twice, and after the graph's JSON is shuffled.
func TestDeterminism(t *testing.T) {
	for _, c := range goldenCases(t) {
		data := readFile(t, c.graph)
		_, _, p := generate(t, data)
		_, _, again := generate(t, data)
		if !bytes.Equal(p.Text, again.Text) {
			t.Errorf("%s: two programs", c.name)
		}
		for seed := uint64(1); seed <= 5; seed++ {
			_, _, s := generate(t, shuffled(t, data, seed))
			if !bytes.Equal(p.Text, s.Text) {
				t.Errorf("%s: another program once the graph is shuffled (seed %d)", c.name, seed)
			}
		}
	}
}

// The analysis of the cases that decide something.
func TestAnalysis(t *testing.T) {
	analysis := func(name string) Analysis {
		for _, c := range goldenCases(t) {
			if c.name == name {
				_, m := modelOf(t, readFile(t, c.graph))
				return Analyze(m)
			}
		}
		t.Fatalf("no case %s", name)
		return Analysis{}
	}
	// a truncated gamma: no marginal for it, no prior predictive
	g := analysis("checks_gamma")
	if g.PriorPredictive || g.NoPrior != NoQuantile || strings.Join(g.Marginals, ",") != "a" {
		t.Errorf("checks_gamma: %+v", g)
	}
	// four likelihoods: the first three in Order are checked
	c := analysis("checks_counts")
	if len(c.Outcomes) != MaxOutcomes || c.Likelihoods != 4 {
		t.Errorf("checks_counts: %d outcomes of %d", len(c.Outcomes), c.Likelihoods)
	}
	// the three kinds, their statistics, the units of the group plate read through an index
	three := analysis("checks_three_outcomes")
	kinds := map[string]string{}
	for _, o := range three.Outcomes {
		kinds[o.Observed] = o.Kind + "/" + o.UnitPlate
	}
	if kinds["y"] != KindContinuous+"/grp" || kinds["k"] != KindCounts+"/grp" || kinds["z"] != KindBinary+"/" {
		t.Errorf("checks_three_outcomes: %v", kinds)
	}
	// nested plates: the units are the first group plate the likelihood reads through an index
	nested := analysis("checks_hier_nested")
	if o := nested.Outcomes[0]; o.UnitPlate != "sch" || o.UnitIndex != "s" {
		t.Errorf("checks_hier_nested: %+v", o)
	}
	// correlated effects: each component and the correlation
	slopes := analysis("checks_hier_slopes")
	if !slopes.PriorPredictive || !strings.Contains(strings.Join(slopes.Marginals, ","), "eff.ca,eff.cb,eff.correlation") {
		t.Errorf("checks_hier_slopes: %+v", slopes)
	}
	// every template has the prior predictive
	for _, tp := range bayestemplates.List() {
		_, m := modelOf(t, tp.Graph)
		if a := Analyze(m); !a.PriorPredictive {
			t.Errorf("template %s: no prior predictive (%s)", tp.ID, a.NoPrior)
		}
	}
}

// No statistic is a p-value, a probability or a share (answer 12), and the quantiles are the spec's.
func TestStatisticsAreNoVerdicts(t *testing.T) {
	for kind, stats := range Statistics {
		for _, s := range stats {
			if _, q := quantileOf[s]; !q && s != "mean" && s != "sd" && s != "min" && s != "max" && s != "zeros" {
				t.Errorf("%s: the statistic %s", kind, s)
			}
		}
	}
	if strings.Join(Statistics[KindContinuous], ",") != "mean,sd,min,max,q10,q50,q90" ||
		strings.Join(Statistics[KindCounts], ",") != "mean,sd,max,q50,q90,zeros" ||
		strings.Join(Statistics[KindBinary], ",") != "mean" {
		t.Errorf("the statistics are not the spec's: %v", Statistics)
	}
	b, err := json.Marshal(TaskChecks(true))
	if err != nil || string(b) != `{"format":1,"on":true,"outcomes":3,"value_bins":40,"stat_bins":30,"density_draws":20,"max_units":8,"ppc_draws":1000}` {
		t.Errorf("bayes.checks: %s %v", b, err)
	}
}
