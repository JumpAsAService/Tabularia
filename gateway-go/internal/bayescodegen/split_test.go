package bayescodegen

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// Lot S5: a vector expression of more than maxLocalNodes nodes (its vector nodes and their
// single-value operands) is written as a chain of locals E_<node id>_<k>, each holding at most
// maxLocalNodes of them, in every place the generator writes one; nothing changes below the bound.

// e4Chain is the expression language's E4 form, `a * v / (v + b) - … + 1`, of n terms: 8n + 1 nodes,
// all counted by a part when v is over the context.
func e4Chain(v string, n int) string {
	term := "a * " + v + " / (" + v + " + b)"
	return strings.Repeat(term+" - ", n-1) + term + " + 1"
}

// sumOf is `v + v + … + v`, n times: 2n - 1 nodes, all vector nodes when v is.
func sumOf(v string, n int) string { return strings.TrimSuffix(strings.Repeat(v+" + ", n), " + ") }

func jsonNode(format string, a ...any) string { return fmt.Sprintf(format, a...) }

const paramBB = `{"id":"b","kind":"parameter","name":"b","label":"B","prior":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}}}`

// splitSiteGraphs are the graphs of the places a split is written (each also checked by hand with
// stanc: see the spec's «As built — lot S5»).
var splitSiteGraphs = map[string][]byte{
	"bound_64": graphJSON(dataY, dataX, paramA, paramS,
		jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"exp(%s)"},"sigma":{"expr":"s"}}}`, sumOf("x", 32))),
	"bound_65": graphJSON(dataY, dataX, paramA, paramS,
		jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"s"}}}`, sumOf("x", 33))),
	"lagged": seriesJSON(false, paramA, paramBB, paramS,
		`{"id":"x","kind":"data","name":"x","label":"X","plate":"rows","type":"real"}`,
		jsonNode(`{"id":"d","kind":"transformed","name":"d","label":"D","plate":"rows","value":{"expr":"%s"}}`, e4Chain("lag(y, 1)", 14)),
		jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"d + %s"},"sigma":{"expr":"s"}}}`, e4Chain("x", 12))),
	"lp_chain":      lpChainJSON(24),
	"lp_lagged":     lpLaggedJSON(22),
	"state_init":    stateJSON(`{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"s"}}}`, "non_centred"),
	"state_nu":      stateJSON(`{"dist":"student_t","args":{"nu":{"expr":"%s"},"mu":{"expr":"0"},"sigma":{"expr":"s"}}}`, "non_centred"),
	"state_centred": stateJSON(`{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"s"}}}`, "centred"),
	"truncated": groupGraphJSON("", paramS,
		`{"id":"bg","kind":"parameter","name":"bg","label":"BG","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		paramA, paramBB,
		jsonNode(`{"id":"al","kind":"parameter","name":"al","label":"AL","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"1"}}},"bounds":{"lower":0}}`, e4Chain("bg", 12)),
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"al[g]"},"sigma":{"expr":"s"}}}`),
	"affine_8": groupGraphJSON("", paramS,
		`{"id":"bg","kind":"parameter","name":"bg","label":"BG","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		jsonNode(`{"id":"al","kind":"parameter","name":"al","label":"AL","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"exp(%s)"},"sigma":{"expr":"1"}}}}`, sumOf("bg", 4)),
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"al[g]"},"sigma":{"expr":"s"}}}`),
	"explicit_9": groupGraphJSON("", paramS,
		`{"id":"bg","kind":"parameter","name":"bg","label":"BG","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		jsonNode(`{"id":"al","kind":"parameter","name":"al","label":"AL","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"1"}}}}`, sumOf("bg", 5)),
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"al[g]"},"sigma":{"expr":"s"}}}`),
	"explicit_65": groupGraphJSON("", paramS,
		`{"id":"bg","kind":"parameter","name":"bg","label":"BG","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		jsonNode(`{"id":"al","kind":"parameter","name":"al","label":"AL","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"bg"}}}}`, sumOf("bg", 33)),
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"al[g]"},"sigma":{"expr":"s"}}}`),
	"explicit_wrapped": groupGraphJSON("", paramS,
		`{"id":"bg","kind":"parameter","name":"bg","label":"BG","plate":"grp","prior":{"dist":"exponential","args":{"beta":{"expr":"1"}}}}`,
		jsonNode(`{"id":"tg","kind":"transformed","name":"tg","label":"TG","plate":"grp","value":{"expr":"exp(bg)"}}`),
		jsonNode(`{"id":"al","kind":"parameter","name":"al","label":"AL","plate":"grp","prior":{"dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"%s"}}}}`, sumOf("tg", 16), sumOf("bg", 16)),
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"al[g]"},"sigma":{"expr":"s"}}}`),
}

// stateJSON is a series with two latent states: lv's init reads the other state w, in init (the
// init's distribution, with "%s" for the large expression) and form.
func stateJSON(init, form string) []byte {
	return seriesJSON(false, paramA, paramBB, paramS,
		`{"id":"w","kind":"state","name":"w","label":"W","plate":"rows","process":"random_walk","init":{"dist":"normal","args":{"mu":{"expr":"0"},"sigma":{"expr":"1"}}},"scale":{"expr":"s"}}`,
		`{"id":"lv","kind":"state","name":"lv","label":"Level","plate":"rows","process":"random_walk","init":`+
			strings.ReplaceAll(init, "%s", e4Chain("w", 12))+`,"scale":{"expr":"s"},"form":"`+form+`"}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"lv + w"},"sigma":{"expr":"s"}}}`)
}

// lpLaggedJSON is a series whose linear predictor has n terms and an intercept with lag.
func lpLaggedJSON(n int) []byte {
	var nodes, terms []string
	for i := 1; i <= n; i++ {
		nodes = append(nodes, jsonNode(`{"id":"x%d","kind":"data","name":"x%d","label":"X","plate":"rows","type":"real"}`, i, i))
		terms = append(terms, jsonNode(`{"data":"x%d","coef":{"expr":"a"}}`, i))
	}
	nodes = append(nodes, paramA, paramS,
		`{"id":"eta","kind":"linear_predictor","name":"eta","label":"Eta","plate":"rows","intercept":{"expr":"a * lag(y, 1)"},"terms":[`+strings.Join(terms, ",")+`]}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta"},"sigma":{"expr":"s"}}}`)
	return seriesJSON(false, nodes...)
}

// lpChainJSON is a regression with n predictors x1…xn through a linear predictor on the rows, and
// one on the group plate (with n coefficients times the group's data u1…un).
func lpChainJSON(n int) []byte {
	var nodes, terms, gterms []string
	for i := 1; i <= n; i++ {
		nodes = append(nodes,
			jsonNode(`{"id":"x%d","kind":"data","name":"x%d","label":"X","plate":"rows","type":"real"}`, i, i),
			jsonNode(`{"id":"u%d","kind":"data","name":"u%d","label":"U","plate":"grp","type":"real"}`, i, i))
		terms = append(terms, jsonNode(`{"data":"x%d","coef":{"expr":"a"}}`, i))
		gterms = append(gterms, jsonNode(`{"data":"u%d","coef":{"expr":"a"}}`, i))
	}
	nodes = append(nodes, paramA, paramS,
		`{"id":"eta","kind":"linear_predictor","name":"eta","label":"Eta","plate":"rows","intercept":{"expr":"a"},"terms":[`+strings.Join(terms, ",")+`]}`,
		`{"id":"gam","kind":"linear_predictor","name":"gam","label":"Gam","plate":"grp","intercept":{"expr":"a"},"terms":[`+strings.Join(gterms, ",")+`]}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"eta + gam[g]"},"sigma":{"expr":"s"}}}`)
	return groupGraphJSON("", nodes...)
}

func splitProgram(t *testing.T, name string) *Program {
	t.Helper()
	data, ok := splitSiteGraphs[name]
	if !ok {
		t.Fatalf("no graph %s", name)
	}
	_, p := generateBytes(t, data)
	return p
}

// heldOf is the nodes a part of a split tree holds: its vector nodes, a cut operand one, a
// single-value operand one.
func heldOf(t *bayesexpr.Typed, cut map[*bayesexpr.Typed]bool, top bool) int {
	if t == nil || !vectorNode(t) {
		return 0
	}
	if cut[t] && !top {
		return 1
	}
	n := 1
	for _, a := range t.Args {
		if vectorNode(a) {
			n += heldOf(a, cut, false)
		} else {
			n++
		}
	}
	return n
}

// TestPlanSplit: on every slot of the graphs of this file and of the golden corpus, the parts hold
// at most maxLocalNodes nodes, they cover the tree (each cut is counted once as a part and
// once as a node of its parent's part), each part comes after the parts it reads, and a tree of
// at most maxLocalNodes nodes is not cut.
func TestPlanSplit(t *testing.T) {
	graphs := map[string][]byte{}
	for k, v := range splitSiteGraphs {
		graphs[k] = v
	}
	for _, c := range goldenCases {
		graphs["golden "+c] = readGraph(t, c)
	}
	graphs["one 249"] = graphJSON(dataY, dataX, paramA, paramBB, paramS,
		jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"s"}}}`, e4Chain("x", 31)))
	graphs["balanced"] = graphJSON(dataY, dataX, paramA, paramBB, paramS,
		jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"%s"},"sigma":{"expr":"s"}}}`,
			strings.Repeat("exp(", 4)+sumOf("("+sumOf("exp(x) * (a + b)", 4)+")", 6)+strings.Repeat(")", 4)))
	split := 0
	for name, data := range graphs {
		_, m := modelOf(t, data)
		for key, slot := range m.Slots {
			if slot.Expr == nil {
				continue
			}
			tree := slot.Expr.Tree
			p := planSplit(tree)
			cut := map[*bayesexpr.Typed]bool{}
			for _, c := range p.cuts {
				cut[c] = true
			}
			held := 0
			for i, c := range p.cuts {
				h := heldOf(c, cut, true)
				if h > maxLocalNodes || h < 2 {
					t.Errorf("%s %v: part %d holds %d vector nodes", name, key, i, h)
				}
				held += h
				for _, earlier := range p.cuts[i+1:] {
					if heldOf(c, map[*bayesexpr.Typed]bool{earlier: true}, true) != heldOf(c, nil, true) {
						t.Errorf("%s %v: part %d reads a part written after it", name, key, i)
					}
				}
			}
			if r := heldOf(tree, cut, true); r != p.resid || r > maxLocalNodes {
				t.Errorf("%s %v: the statement holds %d vector nodes (plan %d)", name, key, r, p.resid)
			}
			if held+p.resid != p.total+len(p.cuts) {
				t.Errorf("%s %v: the parts hold %d + %d, the tree %d with %d cuts", name, key, held, p.resid, p.total, len(p.cuts))
			}
			if (len(p.cuts) > 0) != (p.total > maxLocalNodes) {
				t.Errorf("%s %v: %d vector nodes, %d cuts", name, key, p.total, len(p.cuts))
			}
			if len(p.cuts) > 0 {
				split++
			}
		}
	}
	if split < 8 {
		t.Errorf("only %d slots split: the test does not reach the splitting", split)
	}
}

// splitLocal is an identifier of a part of a split expression.
var splitLocal = regexp.MustCompile(`\bE_[a-z0-9]+_[0-9]+\b`)

// wantPatterns checks that the text matches each pattern, where "(+" stands for one or more "("
// (the parentheses of a chain depend on where it is cut) and the rest is literal.
func wantPatterns(t *testing.T, name, text string, patterns ...string) {
	t.Helper()
	for _, p := range patterns {
		parts := strings.Split(p, "(+")
		for i := range parts {
			parts[i] = regexp.QuoteMeta(parts[i])
		}
		if !regexp.MustCompile(strings.Join(parts, `\(+`)).MatchString(text) {
			t.Errorf("%s: the program has no\n%s\nin\n%s", name, p, text)
		}
	}
}

func TestSplitBound(t *testing.T) {
	if p := splitProgram(t, "bound_64"); splitLocal.Match(p.Text) {
		t.Errorf("64 nodes are split:\n%s", p.Text)
	}
	p := splitProgram(t, "bound_65")
	wantLines(t, p, "  vector[N] E_lik_1 = "+strings.Repeat("(", 31)+"x"+strings.Repeat(" + x)", 31)+";",
		"  y ~ normal((E_lik_1 + x), s);")
	var internal []string
	for _, e := range p.Mapping {
		if e.Kind == bayescontract.KindInternal {
			internal = append(internal, e.Stan+"@"+e.Block+"@"+*e.NodeID)
		}
	}
	if got := strings.Join(internal, ","); got != "E_lik_1@model@lik" {
		t.Errorf("the mapping's internal entries: %s", got)
	}
	for _, q := range p.Quantities {
		if strings.HasPrefix(q.Stan, "E_") {
			t.Errorf("a local is written: %+v", q)
		}
	}
}

// On a series, a lagged value's locals are vectors over every row assigned on Rows_lag, read on
// Rows_lag; a lagged likelihood's argument too.
func TestSplitLagged(t *testing.T) {
	text := string(splitProgram(t, "lagged").Text)
	wantPatterns(t, "lagged", text,
		"\n  vector[N] E_d_1;\n  E_d_1[Rows_lag] = (+a * (y[Lag1])) ./ ",
		"\n  vector[N] d;\n  d[Rows_lag] = (+E_d_1[Rows_lag] - ",
		"\n  vector[N] E_lik_1;\n  E_lik_1[Rows_lag] = (+d[Rows_lag] + ",
		"\n  y[Rows_lag] ~ normal((+E_lik_1[Rows_lag] - ")
	if strings.Contains(text, "E_d_1 = ") || strings.Contains(text, "E_lik_1 = ") {
		t.Errorf("a lagged local is assigned on every row:\n%s", text)
	}
}

// A linear predictor's long sum: on the rows a local of the model block, on a group plate a nested
// block of transformed parameters, on lagged rows a local assigned on Rows_lag; rebuilt with the
// same names in the extension's nested block. A term holds 4 nodes with its addition, a single
// intercept 1: 15 terms are 61 nodes, one part; 16 are 65.
func TestSplitLinearPredictor(t *testing.T) {
	text := string(splitProgram(t, "lp_chain").Text)
	wantPatterns(t, "lp_chain", text,
		"transformed parameters {\n  vector[J_grp] gam;\n  {\n    vector[J_grp] E_gam_1 = (+a + (a * u1)) + ",
		" + (a * u15));\n    gam = (+E_gam_1 + (a * u16)) + ",
		" + (a * u24));\n  }\n}\n",
		"model {\n  vector[N] E_eta_1 = (+a + (a * x1)) + ",
		" + (a * x15));\n  vector[N] eta = (+E_eta_1 + (a * x16)) + ")
	text = string(splitProgram(t, "lp_lagged").Text)
	wantPatterns(t, "lp_lagged", text,
		"\n  vector[N] E_eta_1;\n  E_eta_1[Rows_lag] = (+a * (y[Lag1])) + (a * x1[Rows_lag])) + ",
		" + (a * x15[Rows_lag]));\n  vector[N] eta;\n  eta[Rows_lag] = (+E_eta_1[Rows_lag] + (a * x16[Rows_lag])) + ")
	ext := extFunc{gen: func(s Scope) ([]Decl, []Stmt, error) {
		eta, err := s.Local("eta")
		rows, err2 := s.Rows("lik")
		return nil, []Stmt{Declare(Decl{Name: "Ppc_eta", Elem: Real, Shape: Vector, Size: rows}, eta)}, errors.Join(err, err2)
	}}
	_, p := generateWith(t, lpChainJSON(24), ext)
	text = string(p.Text)
	wantPatterns(t, "rebuilt", text[strings.Index(text, "generated quantities {"):],
		"\n    vector[N] E_eta_1 = (+a + (a * x1)) + ",
		"\n    vector[N] eta = (+E_eta_1 + (a * x16)) + ",
		" + (a * x24));\n    vector[N] Ppc_eta = eta;\n")
	if n := strings.Count(string(indentJSON(t, p.Mapping)), `"E_eta_1"`); n != 1 {
		t.Errorf("E_eta_1 is %d times in the mapping", n)
	}
	if _, short := generateBytes(t, lpChainJSON(15)); splitLocal.Match(short.Text) {
		t.Errorf("a sum of 15 terms (61 nodes) is split:\n%s", short.Text)
	}
	if _, long := generateBytes(t, lpChainJSON(16)); !splitLocal.Match(long.Text) {
		t.Errorf("a sum of 16 terms (65 nodes) is not split:\n%s", long.Text)
	}
}

// A truncated prior over a plate: the locals in the loop's nested block, before its Arg.
func TestSplitTruncatedPrior(t *testing.T) {
	wantPatterns(t, "truncated", string(splitProgram(t, "truncated").Text),
		"\n  {\n    vector[J_grp] E_al_1 = (+a * bg) ./ (bg + b)) - ",
		";\n    vector[J_grp] Arg1_al = (+E_al_1 - ",
		";\n    for (I in 1:J_grp) {\n      al[I] ~ normal(Arg1_al[I], 1.0) T[0.0, ];\n    }\n  }\n")
}

// The affine form keeps a location of 8 nodes; one of 9 takes the explicit form, written on one
// line; one of 65 is split in a nested block of transformed parameters; when the location and the
// scale fit apart but not together in (loc + (scale .* Z)), the larger is a local.
func TestSplitNonCentred(t *testing.T) {
	p := splitProgram(t, "affine_8")
	wantLines(t, p, "  vector<offset=exp((((bg + bg) + bg) + bg)), multiplier=1.0>[J_grp] al;",
		"  al ~ normal(exp((((bg + bg) + bg) + bg)), 1.0);")
	noLine(t, p, "Z_al")
	p = splitProgram(t, "explicit_9")
	wantLines(t, p, "  vector[J_grp] Z_al;", "  vector[J_grp] al = (((((bg + bg) + bg) + bg) + bg) + (1.0 * Z_al));", "  Z_al ~ std_normal();")
	noLine(t, p, "offset")
	wantPatterns(t, "explicit_65", string(splitProgram(t, "explicit_65").Text),
		"parameters {\n  vector<lower=0.0>[J_grp] bg;\n  vector[J_grp] Z_al;\n",
		"transformed parameters {\n  vector[J_grp] al;\n  {\n    vector[J_grp] E_al_1 = (+bg + bg) + ",
		"\n    al = ((E_al_1 + bg) + (bg .* Z_al));\n  }\n}\n",
		"\n  Z_al ~ std_normal();\n")
	// 31 + 31 nodes, and the form's 3: the location (first on a tie) becomes a local.
	want := "  vector[J_grp] al;\n  {\n    vector[J_grp] E_al_1 = " + strings.Repeat("(", 15) + "tg" + strings.Repeat(" + tg)", 15) + ";\n" +
		"    al = (E_al_1 + (" + strings.Repeat("(", 15) + "bg" + strings.Repeat(" + bg)", 15) + " .* Z_al));\n  }\n"
	if text := string(splitProgram(t, "explicit_wrapped").Text); !strings.Contains(text, want) {
		t.Errorf("the program has no\n%s\nin\n%s", want, text)
	}
}

// A state's init on the first rows: non-centred, the location a local over every row assigned on
// Rows_first, in a nested block of transformed parameters with the first values; an argument that
// is neither location nor scale, and a centred init, locals of the model block.
func TestSplitStates(t *testing.T) {
	for name, wants := range map[string][]string{
		"state_init": {"\n  vector[N] lv;\n  {\n    vector[N] E_lv_1;\n    E_lv_1[Rows_first] = (+a * w[Rows_first]) ./ ",
			"\n    lv[Rows_first] = (+E_lv_1[Rows_first] - ", " + (s * Z_lv[Rows_first]));\n  }\n  for (I in 1:N_next) {\n"},
		"state_nu": {"\n  Z_w ~ std_normal();\n  vector[N] E_lv_1;\n  E_lv_1[Rows_first] = (+a * w[Rows_first]) ./ ",
			"\n  Z_lv[Rows_first] ~ student_t((+E_lv_1[Rows_first] - ", " + 1.0), 0.0, 1.0);\n  Z_lv[Rows_next] ~ std_normal();\n"},
		"state_centred": {"\n  vector[N] E_lv_1;\n  E_lv_1[Rows_first] = (+a * w[Rows_first]) ./ ",
			"\n  lv[Rows_first] ~ normal((+E_lv_1[Rows_first] - "},
	} {
		wantPatterns(t, name, string(splitProgram(t, name).Text), wants...)
	}
}

// splitExt asks for the likelihood's mean (a split value) once, or twice.
func splitExt(twice bool) Extension {
	return extFunc{gen: func(s Scope) ([]Decl, []Stmt, error) {
		rows, err := s.Rows("lik")
		if err != nil {
			return nil, nil, err
		}
		mu, err1 := s.Slot(bayesgraph.SlotKey{Node: "lik", Slot: "args.mu"}, nil)
		sigma, err2 := s.Slot(bayesgraph.SlotKey{Node: "lik", Slot: "args.sigma"}, nil)
		draw, err3 := s.Call("normal_rng", mu, sigma)
		yrep := Decl{Name: "Ppc_yrep", Elem: Real, Shape: Array, Size: rows}
		stmts := []Stmt{Declare(yrep, draw)}
		if twice {
			stmts = append(stmts, Declare(Decl{Name: "Ppc_again", Elem: Real, Shape: Array, Size: rows}, draw))
		}
		return nil, stmts, errors.Join(err, err1, err2, err3)
	}}
}

// splitExtGraph has a split per-row value m, read by a split likelihood mean.
var splitExtGraph = graphJSON(dataY, dataX, paramA, paramBB, paramS,
	jsonNode(`{"id":"m","kind":"transformed","name":"m","label":"M","plate":"rows","value":{"expr":"%s"}}`, e4Chain("x", 12)),
	jsonNode(`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"m + %s"},"sigma":{"expr":"s"}}}`, e4Chain("x", 12)))

// The extension: a per-row local it reads is rebuilt with the model block's names (not in the
// mapping again); a split value it asks for has locals of its own, numbered after the node's,
// written before its statement; the same split value in two statements is refused.
func TestSplitExtension(t *testing.T) {
	_, p := generateWith(t, splitExtGraph, splitExt(false))
	text := string(p.Text)
	gq := text[strings.Index(text, "generated quantities {"):]
	model := text[strings.Index(text, "model {"):strings.Index(text, "generated quantities {")]
	wantPatterns(t, "model", model, "\n  vector[N] E_m_1 = (+a * x) ./ ", "\n  vector[N] m = (+E_m_1 - ",
		"\n  vector[N] E_lik_1 = (+m + ", "\n  y ~ normal((+E_lik_1 - ")
	wantPatterns(t, "generated quantities", gq, "\n    vector[N] E_m_1 = (+a * x) ./ ", "\n    vector[N] m = (+E_m_1 - ",
		"\n    vector[N] E_lik_2 = (+m + ", "\n    array[N] real Ppc_yrep = normal_rng((+E_lik_2 - ")
	var entries []string
	for _, e := range p.Mapping {
		if e.Kind == bayescontract.KindInternal {
			entries = append(entries, e.Stan+"@"+e.Block)
		}
	}
	if got := strings.Join(entries, ","); got != "E_m_1@model,E_lik_1@model,E_lik_2@generated quantities" {
		t.Errorf("the mapping's locals: %s", got)
	}
	g, _ := bayesgraph.Decode(splitExtGraph)
	if p, err := Generate(bayesgraph.BuildModel(g), splitExt(true)); !errors.Is(err, ErrExtension) || p != nil {
		t.Errorf("a split value used twice: %v", err)
	}
}

// The program the measures of E4 compiled (three transformed nodes of 249 nodes each), as Generate
// writes it: four locals each, as the hand-split program of the measures (8 terms a local).
func TestSplitE4Program(t *testing.T) {
	var nodes []string
	nodes = append(nodes, dataY, dataX, paramA, paramBB, paramS)
	for i := 1; i <= 3; i++ {
		nodes = append(nodes, jsonNode(`{"id":"t%d","kind":"transformed","name":"t%d","label":"T","plate":"rows","value":{"expr":"%s"}}`, i, i, e4Chain("x", 31)))
	}
	nodes = append(nodes, `{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"t1 + t2 + t3"},"sigma":{"expr":"s"}}}`)
	_, p := generateBytes(t, graphJSON(nodes...))
	text := string(p.Text)
	for i := 1; i <= 3; i++ {
		for k := 1; k <= 4; k++ {
			if !strings.Contains(text, fmt.Sprintf("\n  vector[N] E_t%d_%d = ", i, k)) {
				t.Errorf("no local E_t%d_%d:\n%s", i, k, text)
			}
		}
		if strings.Contains(text, fmt.Sprintf("E_t%d_5", i)) {
			t.Errorf("more than four locals for t%d", i)
		}
	}
	// Each term of the form is 8 nodes and one division: at most 64 / 8 terms a line.
	for _, l := range strings.Split(text, "\n") {
		if n := strings.Count(l, "./"); n > maxLocalNodes/8 {
			t.Errorf("a line holds %d terms: %s", n, l)
		}
	}
}
