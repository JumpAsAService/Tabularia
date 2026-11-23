package bayescodegen

import (
	"strings"
	"testing"
)

// groupGraphJSON is a graph on the rows plate and one group plate grp (key g, form form: "" for
// the default) with the nodes given; y and g are always there.
func groupGraphJSON(form string, nodes ...string) []byte {
	f := ""
	if form != "" {
		f = `,"form":"` + form + `"`
	}
	return []byte(`{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"Rows"},` +
		`{"id":"grp","kind":"group","label":"Group","key":"g"` + f + `}],"nodes":[` + dataY + `,` +
		`{"id":"g","kind":"data","name":"g","label":"G","plate":"rows","type":"group_index","target":"grp"},` +
		strings.Join(nodes, ",") + `]}`)
}

const (
	paramMu  = `{"id":"mu","kind":"parameter","name":"mu","label":"Mu","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`
	paramTau = `{"id":"tau","kind":"parameter","name":"tau","label":"Tau","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}},"bounds":{"lower":0}}`
	likGroup = `{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a[g]"},"sigma":{"expr":"s"}}}`
)

func generateGroup(t *testing.T, form string, nodes ...string) *Program {
	t.Helper()
	_, p := generateBytes(t, groupGraphJSON(form, nodes...))
	return p
}

func noLine(t *testing.T, p *Program, sub string) {
	t.Helper()
	if strings.Contains(string(p.Text), sub) {
		t.Errorf("the program holds %q:\n%s", sub, p.Text)
	}
}

func TestGroupPlateForms(t *testing.T) {
	t.Run("the plate's switch: centred", func(t *testing.T) {
		p := generateGroup(t, "centred", paramMu, paramTau, paramS,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"mu"},"sigma":{"expr":"tau"}}}}`,
			likGroup)
		wantLines(t, p, "  vector[J_grp] a;", "  a ~ normal(mu, tau);", "  y ~ normal((a[g]), s);")
		noLine(t, p, "offset")
	})
	t.Run("a bounded parameter on a non-centred plate stays centred", func(t *testing.T) {
		p := generateGroup(t, "", paramMu, paramS,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"2"}}},"bounds":{"lower":0}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"mu + a[g]"},"sigma":{"expr":"s"}}}`)
		// constant arguments: the declared bound alone, vectorized (the catalogue's rule 4)
		wantLines(t, p, "  vector<lower=0.0>[J_grp] a;", "  a ~ normal(0.0, 2.0);")
		noLine(t, p, "for (")
	})
	t.Run("a prior that is not location-scale stays centred", func(t *testing.T) {
		p := generateGroup(t, "", paramMu,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"mu"},"sigma":{"expr":"a[g]"}}}`)
		wantLines(t, p, "  vector<lower=0.0>[J_grp] a;", "  a ~ exponential(1.0);")
	})
	t.Run("cauchy: affine; student_t through a transformed scale: explicit", func(t *testing.T) {
		p := generateGroup(t, "", paramMu, paramTau, paramS,
			`{"id":"tw","kind":"transformed","name":"tw","label":"Tau twice","value":{"expr":"2 * tau"}}`,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"cauchy","args":{"mu":{"expr":"mu"},"sigma":{"expr":"tau"}}}}`,
			`{"id":"c","kind":"parameter","name":"c","label":"C","plate":"grp","prior":{"dist":"student_t","args":{"nu":{"expr":"3"},"mu":{"expr":"0"},"sigma":{"expr":"tw"}}}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a[g] + c[g]"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p,
			"  vector<offset=mu, multiplier=tau>[J_grp] a;",
			"  vector[J_grp] Z_c;",
			"  real tw = (2.0 * tau);",
			"  vector[J_grp] c = (0.0 + (tw * Z_c));",
			"  a ~ cauchy(mu, tau);",
			"  Z_c ~ student_t(3.0, 0.0, 1.0);",
		)
		noLine(t, p, "  c ~ ")
	})
	t.Run("a location that reads an explicit parameter is explicit; a vector scale multiplies elementwise", func(t *testing.T) {
		p := generateGroup(t, "", paramMu, paramTau, paramS,
			`{"id":"m","kind":"transformed","name":"m","label":"M","value":{"expr":"exp(mu)"}}`,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"m"},"sigma":{"expr":"tau"}}}}`,
			`{"id":"sc","kind":"transformed","name":"sc","label":"Scale per group","plate":"grp","value":{"expr":"exp(a)"}}`,
			`{"id":"b","kind":"parameter","name":"b","label":"B","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"a"},"sigma":{"expr":"sc"}}}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"b[g]"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p,
			"  vector[J_grp] Z_a;",
			"  vector[J_grp] Z_b;",
			"  vector[J_grp] a = (m + (tau * Z_a));",
			"  vector[J_grp] sc = exp(a);",
			"  vector[J_grp] b = (a + (sc .* Z_b));",
			"  Z_a ~ std_normal();",
			"  Z_b ~ std_normal();",
		)
		var internal []string
		for _, q := range p.Quantities {
			if q.Role == RoleInternal {
				internal = append(internal, q.Stan)
			}
		}
		if strings.Join(internal, ",") != "Z_a,Z_b" {
			t.Errorf("internal quantities %v", internal)
		}
	})
}

func TestGroupPlateValues(t *testing.T) {
	p := generateGroup(t, "", paramMu, paramTau, paramS,
		`{"id":"z","kind":"data","name":"z","label":"Z","plate":"grp","type":"real","standardize":true}`,
		`{"id":"k","kind":"data","name":"k","label":"K","plate":"grp","type":"int","support":"nonnegative"}`,
		`{"id":"bz","kind":"parameter","name":"b_z","label":"BZ","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
		`{"id":"bk","kind":"parameter","name":"b_k","label":"BK","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
		`{"id":"eg","kind":"linear_predictor","name":"eta_g","label":"EG","plate":"grp","intercept":{"expr":"mu"},"terms":[{"data":"z","coef":{"expr":"b_z"}},{"data":"k","coef":{"expr":"b_k"}}]}`,
		`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"tau"}}}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a[g] + eta_g[g]"},"sigma":{"expr":"s"}}}`)
	wantLines(t, p,
		"  vector[J_grp] z;",
		"  array[J_grp] int<lower=0> k;",
		"transformed parameters {",
		"  vector[J_grp] eta_g = ((mu + (b_z * z)) + (b_k * to_vector(k)));",
		"  y ~ normal(((a[g]) + (eta_g[g])), s);",
	)
	for _, q := range p.Quantities {
		if q.Stan == "eta_g" && (len(q.Dims) != 1 || q.Dims[0].Plate != "grp") {
			t.Errorf("eta_g is written over %+v", q.Dims)
		}
	}
	t.Run("a likelihood observing a group plate", func(t *testing.T) {
		p := generateGroup(t, "", paramMu, paramS,
			`{"id":"yg","kind":"data","name":"yg","label":"YG","plate":"grp","type":"real"}`,
			`{"id":"a","kind":"parameter","name":"a","label":"A","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`,
			likGroup,
			`{"id":"lg","kind":"likelihood","label":"LG","observed":"yg","dist":"normal","args":{"mu":{"expr":"mu + a"},"sigma":{"expr":"s"}}}`)
		wantLines(t, p, "  yg ~ normal((mu + a), s);")
	})
}

const (
	dataC  = `{"id":"c","kind":"data","name":"c","label":"C","plate":"rows","type":"category"}`
	paramB = `{"id":"b","kind":"parameter","name":"b","label":"B","plate":"levels:c","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`
)

func TestCategories(t *testing.T) {
	t.Run("a coefficient read per row by its level, in an expression", func(t *testing.T) {
		p := generateText(t, dataY, paramA, paramS, dataC, paramB,
			`{"id":"mr","kind":"transformed","name":"mr","label":"Mean","plate":"rows","value":{"expr":"a + b[c]"}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"mr"},"sigma":{"expr":"s + exp(b[c])"}}}`)
		wantLines(t, p,
			"  int<lower=1> K_c;",
			"  array[N] int<lower=1, upper=K_c> c;",
			"  vector[K_c - 1] b;",
			"  vector[K_c] Full_b = append_row(0.0, b);",
			"  vector[N] mr = (a + (Full_b[c]));",
			"  b ~ normal(0.0, 1.0);",
			"  y ~ normal(mr, (s + exp((Full_b[c]))));",
		)
	})
	t.Run("truncated coefficients: one per level after the reference, in a loop", func(t *testing.T) {
		p := generateText(t, dataY, paramA, paramS, dataC, paramB,
			`{"id":"h","kind":"parameter","name":"h","label":"H","plate":"levels:c","prior":{"dist":"normal","args":{"mu":{"expr":"b"},"sigma":{"expr":"s"}}},"bounds":{"lower":-3}}`,
			`{"id":"q","kind":"parameter","name":"q","label":"Q","plate":"levels:c","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"s"}}},"bounds":{"upper":2}}`,
			`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"a + b[c] + h[c] + q[c]"},"sigma":{"expr":"s"}}}`)
		want := "  {\n" +
			"    vector[K_c - 1] Arg1_h = b;\n" +
			"    for (I in 1:(K_c - 1)) {\n" +
			"      h[I] ~ normal(Arg1_h[I], s) T[-3.0, ];\n" +
			"    }\n" +
			"  }\n" +
			"  for (I in 1:(K_c - 1)) {\n" +
			"    q[I] ~ normal(0.0, s) T[, 2.0];\n" +
			"  }\n"
		if !strings.Contains(string(p.Text), want) {
			t.Errorf("the loops are not\n%s\nin\n%s", want, p.Text)
		}
		wantLines(t, p, "  vector<lower=-3.0>[K_c - 1] h;", "  vector<upper=2.0>[K_c - 1] q;")
	})
}

func TestLiftsAndSizesOrder(t *testing.T) {
	_, p := generateBytes(t, readGraph(t, "hier_nested"))
	var data []string
	for _, d := range p.Data {
		data = append(data, d.Stan+":"+d.Kind)
	}
	want := "N:size,J_dst:size,J_sch:size,d:group_index,s:group_index,x:real,y:real,Up_d_sch:lift,Mode:mode"
	if got := strings.Join(data, ","); got != want {
		t.Errorf("data manifest %s, want %s", got, want)
	}
	for _, d := range p.Data {
		if d.Stan == "Up_d_sch" && (d.Plate != "sch" || d.Node != "d" || d.Target != "dst") {
			t.Errorf("the lift's entry %+v", d)
		}
	}
}
