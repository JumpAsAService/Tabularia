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
