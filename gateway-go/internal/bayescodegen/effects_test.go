package bayescodegen

import (
	"errors"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// seriesJSON is a graph whose rows are a series ordered by tm (by the group plate grp, keyed by g,
// when by is true), with y, tm (and g) and the nodes given.
func seriesJSON(by bool, nodes ...string) []byte {
	plates := `{"id":"rows","kind":"rows","label":"Rows","series":{"time":"tm","by":null}}`
	base := dataY + `,{"id":"tm","kind":"data","name":"tm","label":"T","plate":"rows","type":"time"}`
	if by {
		plates = `{"id":"rows","kind":"rows","label":"Rows","series":{"time":"tm","by":"grp"}},{"id":"grp","kind":"group","label":"G","key":"g"}`
		base += `,{"id":"g","kind":"data","name":"g","label":"G","plate":"rows","type":"group_index","target":"grp"}`
	}
	return []byte(`{"format_version":1,"plates":[` + plates + `],"nodes":[` + base + `,` + strings.Join(nodes, ",") + `]}`)
}

func generateSeries(t *testing.T, by bool, nodes ...string) *Program {
	t.Helper()
	_, p := generateBytes(t, seriesJSON(by, nodes...))
	return p
}

func refusedWith(t *testing.T, data []byte, want error) {
	t.Helper()
	_, m := modelOf(t, data)
	if p, err := Generate(m, nil); !errors.Is(err, want) || p != nil {
		t.Fatalf("want %v, got %v", want, err)
	}
}

// lag: a value per row computed with lag is declared over every row and assigned on Rows_lag; what
// reads it runs on Rows_lag too, and so do a linear predictor's data; a value without lag read by a
// lagged statement is taken on Rows_lag.
func TestLagSubsets(t *testing.T) {
	p := generateSeries(t, false, paramA, paramS, dataX,
		`{"id":"z","kind":"data","name":"z","label":"Z","plate":"rows","type":"real"}`,
		`{"id":"d","kind":"transformed","name":"d","label":"D","plate":"rows","value":{"expr":"z - lag(z, 1)"}}`,
		`{"id":"m","kind":"transformed","name":"m","label":"M","plate":"rows","value":{"expr":"a * lag(x, 2) + exp(a) * x"}}`,
		`{"id":"eta","kind":"linear_predictor","name":"eta","label":"E","plate":"rows","intercept":{"expr":"m"},"terms":[{"data":"x","coef":{"expr":"a"}}]}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta + d"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p,
		"  int<lower=0> N_lag;",
		"  array[N_lag] int<lower=1, upper=N> Rows_lag;",
		"  array[N_lag] int<lower=1, upper=N> Lag1;",
		"  array[N_lag] int<lower=1, upper=N> Lag2;",
		"transformed data {",
		"  vector[N] d;",
		"  d[Rows_lag] = (z[Rows_lag] - (z[Lag1]));",
		"  vector[N] m;",
		"  m[Rows_lag] = ((a * (x[Lag2])) + (exp(a) * x[Rows_lag]));",
		"  vector[N] eta;",
		"  eta[Rows_lag] = (m[Rows_lag] + (a * x[Rows_lag]));",
		"  y[Rows_lag] ~ normal((eta[Rows_lag] + d[Rows_lag]), s);",
	)
	var lags []int
	for _, d := range p.Data {
		if d.Kind == DataLag {
			lags = append(lags, d.Lag)
		}
	}
	if len(lags) != 2 || lags[0] != 1 || lags[1] != 2 {
		t.Errorf("the lags of the data manifest: %v", lags)
	}
}

// A likelihood without lag in a series runs on every row; one with lag on Rows_lag.
func TestLagOnlyWhereUsed(t *testing.T) {
	p := generateSeries(t, false, paramA, paramS,
		`{"id":"z","kind":"data","name":"z","label":"Z","plate":"rows","type":"real"}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a"},"sigma":{"expr":"s"}}}`,
		`{"id":"lik2","kind":"likelihood","label":"L2","observed":"z","dist":"normal","args":{"mu":{"expr":"lag(z, 1) + a"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p, "  y ~ normal(a, s);", "  z[Rows_lag] ~ normal(((z[Lag1]) + a), s);")
}

// Refused with ErrUnsupported: the lag of a value itself computed with lag (it is not computed on
// the rows the lag reads), and a state's init that reads a lagged value.
func TestLagRefusals(t *testing.T) {
	refusedWith(t, seriesJSON(false, paramA, paramS,
		`{"id":"d","kind":"transformed","name":"d","label":"D","plate":"rows","value":{"expr":"y - lag(y, 1)"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + lag(d, 1)"},"sigma":{"expr":"s"}}}`),
		ErrUnsupported)
	refusedWith(t, seriesJSON(false, paramA, paramS,
		`{"id":"lv","kind":"state","name":"lv","label":"Level","plate":"rows","process":"random_walk","init":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}},"scale":{"expr":"s"}}`,
		`{"id":"q","kind":"transformed","name":"q","label":"Q","plate":"rows","value":{"expr":"lag(lv, 1) * a"}}`,
		`{"id":"lv2","kind":"state","name":"lv2","label":"Level 2","plate":"rows","process":"random_walk","init":{"dist":"normal","args":{"mu":{"expr":"q"},"sigma":{"expr":"1"}}},"scale":{"expr":"s"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"lv + lv2"},"sigma":{"expr":"s"}}}`),
		ErrUnsupported)
}

// A state whose init is location-scale but not normal: the first rows' standard values from the
// init at location 0 and scale 1, the next ones std_normal(); a series by group; lag of a state.
func TestStates(t *testing.T) {
	p := generateSeries(t, true, paramS,
		`{"id":"lv","kind":"state","name":"lv","label":"Level","plate":"rows","process":"random_walk","init":{"dist":"student_t","args":{"nu":{"expr":"4"},"mu":{"expr":"0"},"sigma":{"expr":"s"}}},"scale":{"expr":"s"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"lv + 0.5 * lag(lv, 1)"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p,
		"  vector[N] Z_lv;",
		"  lv[Rows_first] = (0.0 + (s * Z_lv[Rows_first]));",
		"  for (I in 1:N_next) {",
		"    lv[Rows_next[I]] = (lv[Prev[I]] + (s * Z_lv[Rows_next[I]]));",
		"  Z_lv[Rows_first] ~ student_t(4.0, 0.0, 1.0);",
		"  Z_lv[Rows_next] ~ std_normal();",
		"  y[Rows_lag] ~ normal((lv[Rows_lag] + (0.5 * (lv[Lag1]))), s);",
	)
	if !strings.Contains(string(p.Text), "  array[N] int<lower=1, upper=J_grp> g;\n") {
		t.Errorf("the series' key is not declared:\n%s", p.Text)
	}
	// a non-location-scale init keeps the state centred
	p = generateSeries(t, false, paramS,
		`{"id":"lv","kind":"state","name":"lv","label":"Level","plate":"rows","process":"random_walk","init":{"dist":"gamma","args":{"alpha":{"expr":"2"},"beta":{"expr":"1"}}},"scale":{"expr":"s"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"lv"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p, "  vector[N] lv;", "  lv[Rows_first] ~ gamma(2.0, 1.0);", "  lv[Rows_next] ~ normal(lv[Prev], s);")
	noLine(t, p, "Z_lv")
}

const effectsNode = `{"id":"eff","kind":"correlated_effects","label":"E","plate":"grp","components":[` +
	`{"id":"ca","label":"A","mean":{"expr":"mu"},"scale":{"expr":"tau"}},` +
	`{"id":"cb","name":"lower","label":"B","mean":{"expr":"0"},"scale":{"expr":"1"}},` +
	`{"id":"cc","name":"c_g","label":"C","mean":{"expr":"0"},"scale":{"expr":"tau"}}],` +
	`"correlation":{"label":"R","eta":{"expr":"4"}}}`

// Components without a name or with a reserved one get V_<node>_<component>; a correlation
// without a name Omega_<node>; three components; a parameter whose location reads a component is
// in the explicit form (a component is a transformed parameter in both forms).
func TestEffectsNamesAndExplicit(t *testing.T) {
	for _, form := range []string{"", "centred"} {
		p := generateGroup(t, form, paramMu, paramTau, paramS,
			effectsNode,
			`{"id":"h","kind":"parameter","name":"h","label":"H","plate":"grp","prior":{"dist":"normal","args":{"mu":{"ref":"eff.ca"},"sigma":{"expr":"tau"}}}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"c_g[g] + h[g]"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p, "  matrix[3, 3] Omega_eff = multiply_lower_tri_self_transpose(L_eff);", "  L_eff ~ lkj_corr_cholesky(4.0);")
		if form == "" {
			wantLines(t, p,
				"  vector[J_grp] V_eff_ca;",
				"  vector[J_grp] V_eff_cb;",
				"    matrix[3, J_grp] W_eff = diag_pre_multiply(to_vector([tau, 1.0, tau]), L_eff) * Z_eff;",
				"    V_eff_cb = 0.0 + to_vector(W_eff[2]);",
				"    c_g = 0.0 + to_vector(W_eff[3]);",
				"  vector[J_grp] Z_h;",
				"  vector[J_grp] h = (V_eff_ca + (tau * Z_h));",
				"  Z_h ~ std_normal();",
			)
			// the component is declared before the parameter that reads it
			text := string(p.Text)
			if strings.Index(text, "vector[J_grp] h =") < strings.Index(text, "c_g = ") {
				t.Errorf("h is computed before the components:\n%s", text)
			}
		} else {
			wantLines(t, p,
				"  array[J_grp] vector[3] V_eff;",
				"  vector[J_grp] V_eff_ca = to_vector(V_eff[:, 1]);",
				"  vector[J_grp] c_g = to_vector(V_eff[:, 3]);",
				"  V_eff ~ multi_normal_cholesky(to_vector([mu, 0.0, 0.0]), diag_pre_multiply(to_vector([tau, 1.0, tau]), L_eff));",
				"  h ~ normal(V_eff_ca, tau);",
			)
		}
		renamed := map[string]string{}
		for _, e := range p.Mapping {
			if e.Renamed != nil {
				renamed[e.Stan] = *e.Renamed + ":" + e.Word
			}
		}
		if renamed["V_eff_ca"] != "no_name:" || renamed["V_eff_cb"] != "reserved:lower" || renamed["Omega_eff"] != "no_name:" {
			t.Errorf("form %q: the renamed identifiers %v", form, renamed)
		}
	}
}

// A transformed node that depends on no parameter is transformed data, and a parameter whose
// location reads one stays in the affine form (it is declared before the parameters). The graph
// refuses a prior that reads data, so the location can only be a transformed node of constants.
func TestTransformedDataIsDeclaredBeforeParameters(t *testing.T) {
	p := generateGroup(t, "", paramTau, paramS,
		`{"id":"k","kind":"transformed","name":"k","label":"K","value":{"expr":"log(3)"}}`,
		`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"k"},"sigma":{"expr":"tau"}}}}`,
		likGroup)
	wantLines(t, p, "transformed data {", "  real k = log(3.0);", "  vector<offset=k, multiplier=tau>[J_grp] a;")
	noLine(t, p, "Z_a")
}

// The two cases lot S1 left as ErrUnsupported are refused by the graph's validator: a category on
// a group plate (graph.data_type) and a transformed node or a linear predictor on a category's
// levels (graph.node_plate). The generator treats them as bugs.
func TestValidatorRefusesWhatTheGeneratorDoesNotWrite(t *testing.T) {
	cases := map[string]struct {
		data []byte
		code string
	}{
		"a category on a group plate": {groupGraphJSON("", paramA, paramS,
			`{"id":"c","kind":"data","name":"c","label":"C","plate":"grp","type":"category"}`,
			`{"id":"b","kind":"parameter","name":"b","label":"B","plate":"levels:c","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + b[c[g]]"},"sigma":{"expr":"s"}}}`),
			"graph.data_type"},
		"a transformed node on a category's levels": {graphJSON(dataY, paramA, paramS, dataC, paramB,
			`{"id":"tb","kind":"transformed","name":"tb","label":"TB","plate":"levels:c","value":{"expr":"2 * b"}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + tb[c]"},"sigma":{"expr":"s"}}}`),
			"graph.node_plate"},
		"a linear predictor on a category's levels": {graphJSON(dataY, paramA, paramS, dataC, paramB, dataX,
			`{"id":"lp","kind":"linear_predictor","name":"lp","label":"LP","plate":"levels:c","intercept":{"expr":"b"},"terms":[]}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + lp[c] + x"},"sigma":{"expr":"s"}}}`),
			"graph.node_plate"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			g, findings := bayesgraph.Decode(c.data)
			if g == nil {
				t.Fatalf("does not decode: %v", findings)
			}
			m := bayesgraph.BuildModel(g)
			found := false
			for _, f := range m.Findings {
				if f.Code == c.code && f.Severity == bayesgraph.Error {
					found = true
				}
			}
			if !found {
				t.Fatalf("no %s error: %+v", c.code, m.Findings)
			}
			if _, err := Generate(m, nil); !errors.Is(err, ErrInvalidModel) {
				t.Fatalf("want ErrInvalidModel, got %v", err)
			}
		})
	}
}
