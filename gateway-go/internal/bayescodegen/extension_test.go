package bayescodegen

import (
	"errors"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// fakeExtension is the test extension of the golden case extension_fake: what predictive-checks
// will add, in miniature. Its data: a number of bins and their edges (after Mode). Its written
// declarations: a statistic of the replicated outcome, and the prior draws of a group's levels
// sized by the mode. Its nested block: the linear predictor's local (with the category's Full_ it
// reads), ancestral draws of the hyperparameters, the levels drawn from them in a loop over the
// groups in the prior predictive, the replicated outcome.
type fakeExtension struct{}

var (
	fakeBins  = Decl{Name: "Ppc_bins", Elem: Int, Shape: Scalar, NonNegative: true, Kind: "bins"}
	fakeEdges = Decl{Name: "Ppc_edges", Elem: Real, Shape: Vector, Size: fakeBins.AsSize(), Kind: "bin_edges"}
	fakeFirst = Decl{Name: "Ppc_first_rep", Elem: Real, Shape: Scalar, Role: bayescontract.RoleCheck, Check: "first"}
	fakeMu    = Decl{Name: "Prior_mu", Elem: Real, Shape: Scalar}
	fakeTau   = Decl{Name: "Prior_tau", Elem: Real, Shape: Scalar}
)

func (fakeExtension) Data(*bayesgraph.Model) []Decl { return []Decl{fakeBins, fakeEdges} }

func (fakeExtension) Generated(m *bayesgraph.Model, s Scope) ([]Decl, []Stmt, error) {
	var errs []error
	must := func(e Expr, err error) Expr {
		errs = append(errs, err)
		return e
	}
	slot := func(node, slot string, ids func(string) (Expr, bool)) Expr {
		return must(s.Slot(bayesgraph.SlotKey{Node: node, Slot: slot}, ids))
	}
	groups, err := s.PlateSize("grp")
	errs = append(errs, err)
	rows, err := s.Rows("lik")
	errs = append(errs, err)
	priorA := Decl{Name: "Prior_a", Elem: Real, Shape: Array, Size: groups.WhenPrior(), Role: bayescontract.RolePriorDraw, Of: "a"}
	yrep := Decl{Name: "Ppc_yrep", Elem: Real, Shape: Array, Size: rows}
	draws := func(id string) (Expr, bool) {
		switch id {
		case "mu":
			return fakeMu.Ref(), true
		case "tau":
			return fakeTau.Ref(), true
		}
		return Expr{}, false
	}
	stmts := []Stmt{
		Declare(fakeMu, must(s.Call("normal_rng", slot("mu", "prior.mu", nil), slot("mu", "prior.sigma", nil)))),
		Declare(fakeTau, must(s.Call("normal_rng", slot("tau", "prior.mu", nil), slot("tau", "prior.sigma", nil)))),
		For(groups.WhenPrior(), Assign(must(Index(priorA.Ref(), LoopIndex())),
			must(s.Call("normal_rng", slot("a", "prior.mu", draws), slot("a", "prior.sigma", draws))))),
		Declare(yrep, must(s.Call("normal_rng", slot("lik", "args.mu", nil), slot("lik", "args.sigma", nil)))),
		Assign(fakeFirst.Ref(), must(Index(yrep.Ref(), must(Integer(1))))),
	}
	return []Decl{fakeFirst, priorA}, stmts, errors.Join(errs...)
}

// extFunc is an extension made of two functions, for the refusals.
type extFunc struct {
	data func() []Decl
	gen  func(s Scope) ([]Decl, []Stmt, error)
}

func (e extFunc) Data(*bayesgraph.Model) []Decl {
	if e.data == nil {
		return nil
	}
	return e.data()
}

func (e extFunc) Generated(_ *bayesgraph.Model, s Scope) ([]Decl, []Stmt, error) {
	if e.gen == nil {
		return nil, nil, nil
	}
	return e.gen(s)
}

func TestExtensionFakePlacement(t *testing.T) {
	_, p := generateWith(t, readGraph(t, "extension_fake"), fakeExtension{})
	text := string(p.Text)
	for _, want := range []string{
		"  int<lower=0, upper=1> Mode;\n  int<lower=0> Ppc_bins;\n  vector[Ppc_bins] Ppc_edges;\n}\n",
		"generated quantities {\n  real Ppc_first_rep;\n  array[Mode == 1 ? J_grp : 0] real Prior_a;\n  {\n",
		"    vector[K_c] Full_bc = append_row(0.0, b_c);\n    vector[N] eta = ",
		"    for (I in 1:(Mode == 1 ? J_grp : 0)) {\n      Prior_a[I] = normal_rng(Prior_mu, Prior_tau);\n    }\n",
		"    array[N] real Ppc_yrep = normal_rng(eta, s);\n    Ppc_first_rep = Ppc_yrep[1];\n  }\n}\n",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("the program has no\n%s\nin\n%s", want, text)
		}
	}
	// Without the extension, the same model has no generated quantities and no extension data.
	_, plain := generateBytes(t, readGraph(t, "extension_fake"))
	if strings.Contains(string(plain.Text), "generated quantities") || strings.Contains(string(plain.Text), "Ppc_") {
		t.Errorf("the program without the extension holds its parts:\n%s", plain.Text)
	}
	var roles []string
	for _, q := range p.Quantities {
		if q.Role == bayescontract.RoleCheck || q.Role == bayescontract.RolePriorDraw {
			roles = append(roles, q.Stan+":"+q.Role+":"+q.Check+q.Of)
		}
	}
	if got := strings.Join(roles, ","); got != "Ppc_first_rep:check:first,Prior_a:prior_draw:a" {
		t.Errorf("the written quantities of the extension: %s", got)
	}
	last := p.Data[len(p.Data)-1]
	if last.Stan != "Ppc_edges" || last.Kind != "bin_edges" {
		t.Errorf("the extension's data are not last in the manifest: %+v", p.Data)
	}
}

// Every way an extension could put text of its own, or touch the program's variables, is refused
// with ErrExtension, and no program is returned.
func TestExtensionRefusals(t *testing.T) {
	_, m := modelOf(t, readGraph(t, "extension_fake"))
	real := func(name string) Decl { return Decl{Name: name, Elem: Real, Shape: Scalar} }
	written := func(d Decl) Decl { d.Role, d.Check = bayescontract.RoleCheck, "stat"; return d }
	cases := map[string]extFunc{
		"a data name outside the prefixes": {data: func() []Decl { return []Decl{{Name: "bins", Elem: Int, Shape: Scalar, Kind: "bins"}} }},
		"a data name with text":            {data: func() []Decl { return []Decl{{Name: "Ppc_x; print(1)", Elem: Int, Shape: Scalar, Kind: "bins"}} }},
		"a data name with a reserved suffix": {data: func() []Decl {
			return []Decl{{Name: "Ppc_x_lpdf", Elem: Real, Shape: Scalar, Kind: "bins"}}
		}},
		"a data without its kind": {data: func() []Decl { return []Decl{real("Ppc_x")} }},
		"a data of the program's kind": {data: func() []Decl {
			return []Decl{{Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: bayescontract.DataSize}}
		}},
		"a data sized by the mode": {data: func() []Decl {
			n := Decl{Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: "bins"}
			return []Decl{n, {Name: "Ppc_v", Elem: Real, Shape: Vector, Size: n.AsSize().WhenPrior(), Kind: "edges"}}
		}},
		"a size of a data declared after": {data: func() []Decl {
			n := Decl{Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: "bins"}
			return []Decl{{Name: "Ppc_v", Elem: Real, Shape: Vector, Size: n.AsSize(), Kind: "edges"}, n}
		}},
		"a name twice": {data: func() []Decl {
			return []Decl{{Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: "bins"}, {Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: "bins"}}
		}},
		"a written declaration without a role": {gen: func(Scope) ([]Decl, []Stmt, error) { return []Decl{real("Ppc_x")}, nil, nil }},
		"a prior draw of nothing": {gen: func(Scope) ([]Decl, []Stmt, error) {
			return []Decl{{Name: "Prior_x", Elem: Real, Shape: Scalar, Role: bayescontract.RolePriorDraw, Of: "nope"}}, nil, nil
		}},
		"a prior draw of data": {gen: func(Scope) ([]Decl, []Stmt, error) {
			return []Decl{{Name: "Prior_x", Elem: Real, Shape: Scalar, Role: bayescontract.RolePriorDraw, Of: "y"}}, nil, nil
		}},
		"a prior draw with a size not by the mode": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			n, _ := s.PlateSize("grp")
			return []Decl{{Name: "Prior_x", Elem: Real, Shape: Array, Size: n, Role: bayescontract.RolePriorDraw, Of: "a"}}, nil, nil
		}},
		"an assignment to a parameter": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			mu, _ := s.Identifier("mu")
			one, _ := Num(1)
			return nil, []Stmt{Assign(mu, one)}, nil
		}},
		"an assignment to the extension's data": {data: func() []Decl { return []Decl{{Name: "Ppc_n", Elem: Int, Shape: Scalar, Kind: "bins"}} },
			gen: func(Scope) ([]Decl, []Stmt, error) {
				one, _ := Integer(1)
				return nil, []Stmt{Assign(Decl{Name: "Ppc_n"}.Ref(), one)}, nil
			}},
		"a variable read before it is declared": {gen: func(Scope) ([]Decl, []Stmt, error) {
			x := written(real("Ppc_x"))
			return []Decl{x}, []Stmt{Assign(x.Ref(), real("Ppc_later").Ref())}, nil
		}},
		"the loop's index outside a loop": {gen: func(Scope) ([]Decl, []Stmt, error) {
			x := Decl{Name: "Ppc_x", Elem: Real, Shape: Array, Role: bayescontract.RoleCheck, Check: "stat"}
			x.Size, _ = Count(3)
			ix, _ := Index(x.Ref(), LoopIndex())
			one, _ := Num(1)
			return []Decl{x}, []Stmt{Assign(ix, one)}, nil
		}},
		"a loop inside a loop": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			n, _ := s.PlateSize("grp")
			x := written(real("Ppc_x"))
			one, _ := Num(1)
			return []Decl{x}, []Stmt{For(n, For(n, Assign(x.Ref(), one)))}, nil
		}},
		"a call outside the catalogue": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			one, _ := Num(1)
			_, err := s.Call("print", one)
			return nil, nil, err
		}},
		"an identifier of a per-row value": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.Identifier("eta")
			return nil, nil, err
		}},
		"ids giving what is not an identifier": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			two, _ := Num(2)
			_, err := s.Slot(bayesgraph.SlotKey{Node: "a", Slot: "prior.mu"}, func(string) (Expr, bool) { return two, true })
			return nil, nil, err
		}},
		"a value of no kind": {gen: func(Scope) ([]Decl, []Stmt, error) {
			x := written(real("Ppc_x"))
			return []Decl{x}, []Stmt{Assign(x.Ref(), Expr{})}, nil
		}},
	}
	for name, ext := range cases {
		t.Run(name, func(t *testing.T) {
			p, err := Generate(m, ext)
			if !errors.Is(err, ErrExtension) || p != nil {
				t.Fatalf("want ErrExtension and no program, got %v", err)
			}
		})
	}
}

// The extension's names never meet the program's: a name of the form is not one the generator
// writes for itself, and Decl.Ref of a name outside the form is no value.
func TestExtensionNames(t *testing.T) {
	for _, s := range []string{"Ppc_mean", "Prior_a", "Ppc_q10_rep", "Prior_x_2"} {
		if !extensionName(s) {
			t.Errorf("%s refused", s)
		}
	}
	for _, s := range []string{"", "Ppc_", "Ppc__x", "Ppc_x_", "Ppc_X", "ppc_x", "Z_x", "Full_x", "N", "Mode", "Ppc_x.y",
		"Ppc_x_lpdf", "Prior_cdf", strings.Repeat("Ppc_x", 20)} {
		if extensionName(s) {
			t.Errorf("%s accepted", s)
		}
		if (Decl{Name: s}).Ref().ok() {
			t.Errorf("%s gives a value", s)
		}
	}
}

// On a series, the replicated outcome of a lagged likelihood has N_lag values, and its arguments
// are taken on Rows_lag as the model block takes them; a lagged local is rebuilt on Rows_lag.
func TestExtensionOnLaggedRows(t *testing.T) {
	ext := extFunc{gen: func(s Scope) ([]Decl, []Stmt, error) {
		rows, err := s.Rows("lik")
		if err != nil {
			return nil, nil, err
		}
		mu, err1 := s.Slot(bayesgraph.SlotKey{Node: "lik", Slot: "args.mu"}, nil)
		sigma, err2 := s.Slot(bayesgraph.SlotKey{Node: "lik", Slot: "args.sigma"}, nil)
		draw, err3 := s.Call("normal_rng", mu, sigma)
		yrep := Decl{Name: "Ppc_yrep", Elem: Real, Shape: Array, Size: rows}
		return nil, []Stmt{Declare(yrep, draw)}, errors.Join(err1, err2, err3)
	}}
	_, p := generateWith(t, seriesJSON(false, paramA, paramS,
		`{"id":"m","kind":"transformed","name":"m","label":"M","plate":"rows","value":{"expr":"a * lag(y, 1)"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"m"},"sigma":{"expr":"s"}}}`), ext)
	want := "generated quantities {\n  {\n    vector[N] m;\n    m[Rows_lag] = (a * (y[Lag1]));\n" +
		"    array[N_lag] real Ppc_yrep = normal_rng(m[Rows_lag], s);\n  }\n}\n"
	if !strings.HasSuffix(string(p.Text), want) {
		t.Fatalf("the generated quantities are not\n%s\nin\n%s", want, p.Text)
	}
}

// rowsExt is an extension with an array of positions among a statement's rows, Ppc_rows (sized by
// Ppc_n), a real array and a written check, for the values on the rows it names (lot P1).
var (
	rowsN     = Decl{Name: "Ppc_n", Elem: Int, Shape: Scalar, NonNegative: true, Kind: "number"}
	rowsArray = Decl{Name: "Ppc_rows", Elem: Int, Shape: Array, Size: rowsN.AsSize(), NonNegative: true, Kind: "rows"}
	rowsReal  = Decl{Name: "Ppc_reals", Elem: Real, Shape: Array, Size: rowsN.AsSize(), Kind: "values"}
)

func rowsExt(gen func(s Scope) ([]Decl, []Stmt, error)) extFunc {
	return extFunc{data: func() []Decl { return []Decl{rowsN, rowsArray, rowsReal} }, gen: gen}
}

// replicated is an extension drawing Ppc_rep from a likelihood's normal arguments on the rows
// named, with ids.
func replicated(lik string, ids func(string) (Expr, bool)) extFunc {
	return rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		mu, err1 := s.SlotOn(bayesgraph.SlotKey{Node: lik, Slot: "args.mu"}, ids, rowsArray.Ref())
		sigma, err2 := s.SlotOn(bayesgraph.SlotKey{Node: lik, Slot: "args.sigma"}, ids, rowsArray.Ref())
		draw, err3 := s.Call("normal_rng", mu, sigma)
		rep := Decl{Name: "Ppc_rep", Elem: Real, Shape: Array, Size: rowsN.AsSize()}
		return nil, []Stmt{Declare(rep, draw)}, errors.Join(err1, err2, err3)
	})
}

func gqOf(t *testing.T, p *Program) string {
	t.Helper()
	i := strings.Index(string(p.Text), "generated quantities {\n")
	if i < 0 {
		t.Fatalf("no generated quantities:\n%s", p.Text)
	}
	return string(p.Text[i:])
}

// SlotOn takes every value over the statement's plate at the positions named: on every row
// x[Ppc_rows], on the lagged rows x[Rows_lag[Ppc_rows]] and lag(x, k) as x[Lag<k>[Ppc_rows]], an
// index's array and its lifts at them too; a single value stays one. ValueOn the same for a node.
func TestSlotOnAndValueOn(t *testing.T) {
	// every row, through a per-row local rebuilt over every row (no ids)
	_, p := generateWith(t, readGraph(t, "extension_fake"), replicated("lik", nil))
	want := "generated quantities {\n  {\n    vector[K_c] Full_bc = append_row(0.0, b_c);\n" +
		"    vector[N] eta = (((a[g]) + (b_x * x)) + (Full_bc[c]));\n" +
		"    array[Ppc_n] real Ppc_rep = normal_rng(eta[Ppc_rows], s);\n  }\n}\n"
	if got := gqOf(t, p); got != want {
		t.Errorf("every row:\n%s\nwant\n%s", got, want)
	}
	// ValueOn of the linear predictor itself: its data, its index and its category at the rows
	ext := rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		v, err := s.ValueOn("eta", nil, rowsArray.Ref())
		x := Decl{Name: "Ppc_eta", Elem: Real, Shape: Vector, Size: rowsN.AsSize()}
		return nil, []Stmt{Declare(x, v)}, err
	})
	_, p = generateWith(t, readGraph(t, "extension_fake"), ext)
	if got := gqOf(t, p); !strings.Contains(got, "    vector[Ppc_n] Ppc_eta = (((a[g[Ppc_rows]]) + (b_x * x[Ppc_rows])) + (Full_bc[c[Ppc_rows]]));\n") {
		t.Errorf("ValueOn:\n%s", got)
	}
	// the lagged rows: a lagged likelihood reading a lagged value that reads one without lag
	series := seriesJSON(false, paramA, paramS, dataX,
		`{"id":"e","kind":"linear_predictor","name":"e","label":"E","plate":"rows","intercept":{"expr":"a"},"terms":[{"data":"x","coef":{"expr":"a"}}]}`,
		`{"id":"m","kind":"transformed","name":"m","label":"M","plate":"rows","value":{"expr":"e + a * lag(y, 2)"}}`,
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"m"},"sigma":{"expr":"s"}}}`)
	_, p = generateWith(t, series, replicated("lik", nil))
	if got := gqOf(t, p); !strings.Contains(got, "    array[Ppc_n] real Ppc_rep = normal_rng(m[Rows_lag[Ppc_rows]], s);\n") {
		t.Errorf("lagged, no ids:\n%s", got)
	}
	ext = rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		v, err := s.ValueOn("m", nil, rowsArray.Ref())
		x := Decl{Name: "Ppc_m", Elem: Real, Shape: Vector, Size: rowsN.AsSize()}
		return nil, []Stmt{Declare(x, v)}, err
	})
	_, p = generateWith(t, series, ext)
	if got := gqOf(t, p); !strings.Contains(got, "    vector[Ppc_n] Ppc_m = (e[Rows_lag[Ppc_rows]] + (a * (y[Lag2[Ppc_rows]])));\n") {
		t.Errorf("ValueOn lagged:\n%s", got)
	}
	// a group plate: positions among its units; an index lifted to it at them too
	group := []byte(`{"format_version":1,"plates":[{"id":"rows","kind":"rows","label":"Rows"},` +
		`{"id":"dst","kind":"group","label":"D","key":"d"},{"id":"sch","kind":"group","label":"S","parent":"dst","key":"k"}],"nodes":[` +
		dataY + `,{"id":"d","kind":"data","name":"d","label":"D","plate":"rows","type":"group_index","target":"dst"},` +
		`{"id":"k","kind":"data","name":"k","label":"K","plate":"rows","type":"group_index","target":"sch"},` +
		paramMu + `,` + paramTau + `,` + paramS + `,` +
		`{"id":"md","kind":"parameter","name":"md","label":"MD","plate":"dst","prior":{"dist":"normal","args":{"mu":{"expr":"mu"},"sigma":{"expr":"tau"}}}},` +
		`{"id":"ms","kind":"parameter","name":"ms","label":"MS","plate":"sch","prior":{"dist":"normal","args":{"mu":{"expr":"md[d]"},"sigma":{"expr":"tau"}}}},` +
		`{"id":"lik","kind":"likelihood","label":"L","observed":"y","dist":"normal","args":{"mu":{"expr":"ms[k]"},"sigma":{"expr":"s"}}}]}`)
	ext = rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		v, err := s.SlotOn(bayesgraph.SlotKey{Node: "ms", Slot: "prior.mu"}, nil, rowsArray.Ref())
		sd, err2 := s.SlotOn(bayesgraph.SlotKey{Node: "ms", Slot: "prior.sigma"}, nil, rowsArray.Ref())
		x := Decl{Name: "Ppc_ms", Elem: Real, Shape: Vector, Size: rowsN.AsSize()}
		y := Decl{Name: "Ppc_sd", Elem: Real, Shape: Scalar}
		return nil, []Stmt{Declare(x, v), Declare(y, sd)}, errors.Join(err, err2)
	})
	_, p = generateWith(t, group, ext)
	got := gqOf(t, p)
	if !strings.Contains(got, "    vector[Ppc_n] Ppc_ms = (md[Up_d_sch[Ppc_rows]]);\n") || !strings.Contains(got, "    real Ppc_sd = tau;\n") {
		t.Errorf("a group plate:\n%s", got)
	}
}

// With ids, a value per row that reads a node ids answers is rebuilt with ids in a local of its
// own on the rows named (the prior's path never reads the posterior's eta), once however often it
// is read; a node ids does not answer that reads one it does is refused.
func TestValueWithIdsRebuildsPerRowValues(t *testing.T) {
	_, m := modelOf(t, readGraph(t, "extension_checks_lagged"))
	draws := map[string]Decl{}
	for _, id := range []string{"a", "bx", "phi", "s"} {
		draws[id] = Decl{Name: "Prior_" + id, Elem: Real, Shape: Scalar}
	}
	ids := func(id string) (Expr, bool) {
		if d, ok := draws[id]; ok {
			return d.Ref(), true
		}
		return Expr{}, false
	}
	ext := rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		var stmts []Stmt
		for _, id := range []string{"a", "bx", "phi", "s"} {
			one, _ := Num(1)
			stmts = append(stmts, Declare(draws[id], one))
		}
		mu, err := s.SlotOn(bayesgraph.SlotKey{Node: "lik", Slot: "args.mu"}, ids, rowsArray.Ref())
		x := Decl{Name: "Prior_mu", Elem: Real, Shape: Vector, Size: rowsN.AsSize()}
		return nil, append(stmts, Declare(x, mu)), err
	})
	p, err := Generate(m, ext)
	if err != nil {
		t.Fatal(err)
	}
	want := "    vector[N] E_eta_1;\n" +
		"    E_eta_1[Rows_lag[Ppc_rows]] = (Prior_a + (Prior_bx * x[Rows_lag[Ppc_rows]]));\n" +
		"    vector[N] E_m_1;\n" +
		"    E_m_1[Rows_lag[Ppc_rows]] = (E_eta_1[Rows_lag[Ppc_rows]] + (Prior_phi * (y[Lag1[Ppc_rows]])));\n" +
		"    vector[Ppc_n] Prior_mu = E_m_1[Rows_lag[Ppc_rows]];\n"
	if got := gqOf(t, p); !strings.Contains(got, want) || strings.Contains(got, "vector[N] eta") {
		t.Errorf("the per-row values rebuilt:\n%s\nwant\n%s", got, want)
	}
	var internal []string
	for _, e := range p.Mapping {
		if strings.HasPrefix(e.Stan, "E_") {
			internal = append(internal, e.Stan+":"+e.Block+":"+e.Kind)
		}
	}
	if got := strings.Join(internal, ","); got != "E_eta_1:generated quantities:internal,E_m_1:generated quantities:internal" {
		t.Errorf("the mapping of the rebuilt locals: %s", got)
	}

	// ids for the hyperparameters only: the group's levels, whose prior reads them, are refused
	_, m = modelOf(t, readGraph(t, "extension_fake"))
	hyper := func(id string) (Expr, bool) {
		if id == "mu" || id == "tau" {
			return fakeMu.Ref(), true
		}
		return Expr{}, false
	}
	for name, ext := range map[string]extFunc{
		"Slot": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.Slot(bayesgraph.SlotKey{Node: "lik", Slot: "args.mu"}, hyper)
			return nil, nil, err
		}},
		"SlotOn": replicated("lik", hyper),
		"ValueWith": {gen: func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.ValueWith("eta", hyper)
			return nil, nil, err
		}},
	} {
		if p, err := Generate(m, ext); !errors.Is(err, ErrExtension) || p != nil || !strings.Contains(err.Error(), `"a" reads a node ids answers`) {
			t.Errorf("%s: want the refusal of a, got %v", name, err)
		}
	}
}

// Add and Mul write a sum and a product fully parenthesized; a difference is a sum with a product
// by -1.0. Neither takes what is no value, and their result is no variable.
func TestAddMul(t *testing.T) {
	two, _ := Num(2)
	minus, _ := Num(-1)
	x := Decl{Name: "Ppc_x", Elem: Real, Shape: Scalar}
	prod, err := Mul(minus, x.Ref())
	if err != nil {
		t.Fatal(err)
	}
	sum, err := Add(two, prod)
	if err != nil || sum.text != "(2.0 + ((-1.0) * Ppc_x))" {
		t.Fatalf("%q, %v", sum.text, err)
	}
	if len(sum.ext) != 1 || sum.ext[0] != "Ppc_x" {
		t.Errorf("the names read: %v", sum.ext)
	}
	for _, c := range [][2]Expr{{{}, two}, {two, {}}} {
		if _, err := Add(c[0], c[1]); !errors.Is(err, ErrExtension) {
			t.Errorf("Add of no value: %v", err)
		}
		if _, err := Mul(c[0], c[1]); !errors.Is(err, ErrExtension) {
			t.Errorf("Mul of no value: %v", err)
		}
	}
	one, _ := Integer(1)
	if _, err := Index(sum, one); !errors.Is(err, ErrExtension) {
		t.Errorf("an indexing of a sum: %v", err)
	}
	// in a program: the loop's index read through a product stays in its loop
	_, m := modelOf(t, readGraph(t, "extension_fake"))
	w := Decl{Name: "Ppc_w", Elem: Real, Shape: Array, Size: rowsN.AsSize(), Role: bayescontract.RoleCheck, Check: "stat"}
	ext := rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		ri, _ := Index(rowsReal.Ref(), LoopIndex())
		wi, _ := Index(w.Ref(), LoopIndex())
		v, err := Add(ri, must(Mul(ri, two)))
		return []Decl{w}, []Stmt{For(rowsN.AsSize(), Assign(wi, v))}, err
	})
	p, err := Generate(m, ext)
	if err != nil || !strings.Contains(string(p.Text), "      Ppc_w[I] = (Ppc_reals[I] + (Ppc_reals[I] * 2.0));\n") {
		t.Fatalf("%v\n%s", err, p.Text)
	}
	ext = rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		ri, _ := Index(rowsReal.Ref(), LoopIndex())
		v, err := Add(ri, two)
		return []Decl{written(Decl{Name: "Ppc_v", Elem: Real, Shape: Scalar})}, []Stmt{Assign(Decl{Name: "Ppc_v"}.Ref(), v)}, err
	})
	if _, err := Generate(m, ext); !errors.Is(err, ErrExtension) {
		t.Errorf("the loop's index in a sum outside a loop: %v", err)
	}
}

func must(e Expr, err error) Expr {
	if err != nil {
		panic(err)
	}
	return e
}

func written(d Decl) Decl {
	d.Role, d.Check = bayescontract.RoleCheck, "stat"
	return d
}

// What SlotOn and ValueOn refuse: rows that are not an array of integers of the extension's data, a
// slot or a node that is a single value, a node that is neither transformed nor a linear predictor;
// and a local declared in a loop is read in that loop only.
func TestRowsRefusals(t *testing.T) {
	_, m := modelOf(t, readGraph(t, "extension_fake"))
	slotOn := func(key bayesgraph.SlotKey, rows func(s Scope) Expr) extFunc {
		return rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.SlotOn(key, nil, rows(s))
			return nil, nil, err
		})
	}
	lik := bayesgraph.SlotKey{Node: "lik", Slot: "args.mu"}
	cases := map[string]extFunc{
		"rows of reals":        slotOn(lik, func(Scope) Expr { return rowsReal.Ref() }),
		"rows that are a size": slotOn(lik, func(Scope) Expr { return rowsN.Ref() }),
		"rows of the program":  slotOn(lik, func(s Scope) Expr { g, _ := s.Identifier("g"); return g }),
		"rows of no value":     slotOn(lik, func(Scope) Expr { return Expr{} }),
		"rows indexed": slotOn(lik, func(Scope) Expr {
			one, _ := Integer(1)
			r, _ := Index(rowsArray.Ref(), one)
			return r
		}),
		"rows not declared": slotOn(lik, func(Scope) Expr {
			return Decl{Name: "Ppc_other", Elem: Int, Shape: Array, Kind: "rows"}.Ref()
		}),
		"a written array as rows": rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			w := Decl{Name: "Ppc_w", Elem: Int, Shape: Array, Size: rowsN.AsSize(), Role: bayescontract.RoleCheck, Check: "stat"}
			_, err := s.SlotOn(lik, nil, w.Ref())
			return []Decl{w}, nil, err
		}),
		"a slot of a single value": slotOn(bayesgraph.SlotKey{Node: "mu", Slot: "prior.mu"}, func(Scope) Expr { return rowsArray.Ref() }),
		"not a slot":               slotOn(bayesgraph.SlotKey{Node: "lik", Slot: "args.nu"}, func(Scope) Expr { return rowsArray.Ref() }),
		"ValueOn of a parameter": rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.ValueOn("a", nil, rowsArray.Ref())
			return nil, nil, err
		}),
		"ValueOn with rows of reals": rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			_, err := s.ValueOn("eta", nil, rowsReal.Ref())
			return nil, nil, err
		}),
		"a loop's local read after it": rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			l := Decl{Name: "Ppc_l", Elem: Real, Shape: Scalar}
			w := written(Decl{Name: "Ppc_w", Elem: Real, Shape: Scalar})
			one, _ := Num(1)
			return []Decl{w}, []Stmt{For(rowsN.AsSize(), Declare(l, one)), Assign(w.Ref(), l.Ref())}, nil
		}),
		"a loop's local read in another loop": rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
			l := Decl{Name: "Ppc_l", Elem: Real, Shape: Scalar}
			w := written(Decl{Name: "Ppc_w", Elem: Real, Shape: Scalar})
			one, _ := Num(1)
			return []Decl{w}, []Stmt{For(rowsN.AsSize(), Declare(l, one)), For(rowsN.AsSize(), Assign(w.Ref(), l.Ref()))}, nil
		}),
	}
	for name, ext := range cases {
		t.Run(name, func(t *testing.T) {
			if p, err := Generate(m, ext); !errors.Is(err, ErrExtension) || p != nil {
				t.Fatalf("want ErrExtension and no program, got %v", err)
			}
		})
	}
	// a single value's slot on the rows of a plate stays a single value
	ext := rowsExt(func(s Scope) ([]Decl, []Stmt, error) {
		v, err := s.SlotOn(bayesgraph.SlotKey{Node: "lik", Slot: "args.sigma"}, nil, rowsArray.Ref())
		return nil, []Stmt{Declare(Decl{Name: "Ppc_s", Elem: Real, Shape: Scalar}, v)}, err
	})
	if _, p := generateWith(t, readGraph(t, "extension_fake"), ext); !strings.Contains(string(p.Text), "    real Ppc_s = s;\n") {
		t.Errorf("a single value on the rows:\n%s", p.Text)
	}
}
