package bayesexpr

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// The corpus's environments: a simple Env built from plates and named nodes, not model-graph's.

type envPlate struct {
	ID     Plate  `json:"id"`
	Kind   string `json:"kind"` // rows, group, levels
	Parent Plate  `json:"parent"`
	Series bool   `json:"series"`
}

type envNode struct {
	Name     string                 `json:"name"`
	ID       string                 `json:"id"`
	Role     Role                   `json:"role"`
	Type     string                 `json:"type"` // real, int, index, time
	Plate    Plate                  `json:"plate"`
	Target   Plate                  `json:"target"`
	Interval *bayescatalog.Interval `json:"interval"`
}

type envSpec struct {
	Plates []envPlate `json:"plates"`
	Nodes  []envNode  `json:"nodes"`
}

// testEnv is an Env over an envSpec, with the slot's context. It fails the test when the checker
// calls Encloses or IsSeries with the scalar plate.
type testEnv struct {
	t      testing.TB
	plates map[Plate]envPlate
	nodes  []envNode
	ctx    Plate
}

func newTestEnv(t testing.TB, spec envSpec, ctx Plate) *testEnv {
	e := &testEnv{t: t, plates: map[Plate]envPlate{}, nodes: spec.Nodes, ctx: ctx}
	for _, p := range spec.Plates {
		e.plates[p.ID] = p
	}
	return e
}

var elems = map[string]Elem{"real": ElemReal, "int": ElemInt, "index": ElemIndex, "time": ElemTime, "not_number": ElemNotNumber}

func (e *testEnv) Lookup(name string) (Symbol, bool) {
	for _, n := range e.nodes {
		if n.Name != name {
			continue
		}
		s := Symbol{ID: n.ID, Role: n.Role, Type: Type{Elem: elems[n.Type], Plate: n.Plate, Target: n.Target}}
		if n.Interval != nil {
			i := FromCatalog(*n.Interval)
			s.Interval = &i
		}
		return s, true
	}
	return Symbol{}, false
}

func (e *testEnv) Context() Plate { return e.ctx }

func (e *testEnv) Encloses(outer, inner Plate) bool {
	if outer == "" || inner == "" {
		e.t.Errorf("Encloses(%q, %q): the scalar plate", outer, inner)
	}
	if outer == inner {
		return true
	}
	o, i := e.plates[outer], e.plates[inner]
	switch {
	case o.Kind == "rows":
		return false
	case i.Kind == "rows":
		return true
	case o.Kind == "levels" || i.Kind == "levels":
		return false
	}
	for p, seen := i.Parent, 0; p != "" && seen < len(e.plates); p, seen = e.plates[p].Parent, seen+1 {
		if p == outer {
			return true
		}
	}
	return false
}

func (e *testEnv) IsSeries(p Plate) bool {
	if p == "" {
		e.t.Errorf("IsSeries(\"\")")
	}
	return e.plates[p].Series
}

func (e *testEnv) IndexOf(target Plate) (string, bool) {
	for _, n := range e.nodes {
		if n.Type == "index" && n.Target == target {
			return n.Name, true
		}
	}
	return "", false
}

func readEnvs(t testing.TB) map[string]envSpec {
	t.Helper()
	raw, err := os.ReadFile("testdata/corpus/handwritten.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Envs map[string]envSpec `json:"envs"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	return file.Envs
}

// caseEnv is a case's environment: {use, context}.
func caseEnv(t testing.TB, envs map[string]envSpec, raw json.RawMessage) *testEnv {
	t.Helper()
	var ref struct {
		Use     string `json:"use"`
		Context *Plate `json:"context"`
	}
	if err := json.Unmarshal(raw, &ref); err != nil {
		t.Fatalf("env %s: %v", raw, err)
	}
	spec, ok := envs[ref.Use]
	if !ok || ref.Context == nil {
		t.Fatalf("env %s: no such environment, or no context", raw)
	}
	if *ref.Context != "" && !slices.ContainsFunc(spec.Plates, func(p envPlate) bool { return p.ID == *ref.Context }) {
		t.Fatalf("env %s: no plate %q", raw, *ref.Context)
	}
	return newTestEnv(t, spec, *ref.Context)
}

func intervalJSON(t *testing.T, i Interval) []byte {
	t.Helper()
	b, err := json.Marshal(i.Catalog())
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// TestCorpusTypes: every typed case of the handwritten corpus gives its expected result, every
// code of the checker is met at least twice, and no typed case gives a code of neither list.
func TestCorpusTypes(t *testing.T) {
	envs := readEnvs(t)
	codes := map[Code]int{}
	typed := 0
	for _, c := range readCorpus(t) {
		if c.Env == nil {
			continue
		}
		typed++
		env := caseEnv(t, envs, c.Env)
		got, err := CheckText(c.Input, env)
		if c.Expect.OK {
			if err != nil {
				t.Errorf("%s: %q: want ok, got %v", c.ID, c.Input, err)
				continue
			}
			ast, _ := json.Marshal(got.Tree.Node)
			if !sameJSON(t, ast, c.Expect.AST) {
				t.Errorf("%s: %q: ast %s, want %s", c.ID, c.Input, ast, c.Expect.AST)
			}
			if f := Format(got.Tree.Node); f != c.Expect.Format {
				t.Errorf("%s: %q: format %q, want %q", c.ID, c.Input, f, c.Expect.Format)
			}
			if s := got.Tree.Type.String(); s != c.Expect.Type {
				t.Errorf("%s: %q: type %s, want %s", c.ID, c.Input, s, c.Expect.Type)
			}
			if c.Expect.Interval == nil {
				t.Errorf("%s: no expected interval", c.ID)
			} else if iv := intervalJSON(t, got.Interval()); !sameJSON(t, iv, c.Expect.Interval) {
				t.Errorf("%s: %q: interval %s, want %s", c.ID, c.Input, iv, c.Expect.Interval)
			}
			continue
		}
		var e *Error
		if !errors.As(err, &e) {
			t.Errorf("%s: %q: want %s, got %v", c.ID, c.Input, c.Expect.Code, err)
			continue
		}
		if !slices.Contains(checkCodes, e.Code) && !slices.Contains(syntaxCodes, e.Code) {
			t.Errorf("%s: %s is not a code", c.ID, e.Code)
		}
		codes[e.Code]++
		want := &Error{Code: c.Expect.Code, Pos: c.Expect.Pos, Len: c.Expect.Len, Args: c.Expect.Args}
		if e.Code != want.Code || e.Pos != want.Pos || e.Len != want.Len || !sameArgs(e.Args, want.Args) {
			t.Errorf("%s: %q: got %v, want %v", c.ID, c.Input, e, want)
		}
	}
	if typed < 100 {
		t.Errorf("%d typed cases, want at least 100", typed)
	}
	for _, code := range checkCodes {
		if codes[code] < 2 {
			t.Errorf("%s: %d typed cases, want at least 2", code, codes[code])
		}
	}
}

// The threats' names and calls never type: a variable of the program, Stan's statements.
func TestCheckThreats(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "rows")
	for _, src := range []string{"target", "lp__", "N", "V_n1", "print(mu)", "reject(mu)", "normal_lpdf(mu)",
		"increment_log_prob(mu)", "lower", "model", "lag(mu, 1)", "lag"} {
		if _, err := CheckText(src, env); err == nil {
			t.Errorf("%q: typed", src)
		}
	}
}

// TestCheckRefusesMalformedTrees: a tree Parse cannot make is ErrInvalidTree, never a panic
// (E4, finding 7).
func TestCheckRefusesMalformedTrees(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "rows")
	name := func() *Node { return &Node{Kind: KindName, Name: "mu"} }
	cycle := &Node{Kind: KindNeg}
	cycle.Args = []*Node{cycle}
	deep := name()
	for i := 0; i < 100000; i++ {
		deep = &Node{Kind: KindNeg, Args: []*Node{deep}}
	}
	cases := map[string]*Node{
		"nil":                   nil,
		"neg without operand":   {Kind: KindNeg},
		"neg with two":          {Kind: KindNeg, Args: []*Node{name(), name()}},
		"binary with one":       {Kind: KindBinary, Op: "+", Args: []*Node{name()}},
		"binary without op":     {Kind: KindBinary, Args: []*Node{name(), name()}},
		"binary of another op":  {Kind: KindBinary, Op: ".*", Args: []*Node{name(), name()}},
		"name with operands":    {Kind: KindName, Name: "mu", Args: []*Node{name()}},
		"number with operands":  {Kind: KindNum, Num: 1, Args: []*Node{name()}},
		"lag with operands":     {Kind: KindLag, Name: "mu", K: 1, Args: []*Node{name()}},
		"index with operands":   {Kind: KindIndex, Name: "mu", By: "school", Args: []*Node{name()}},
		"no kind":               {},
		"unknown kind":          {Kind: 200},
		"call with a nil":       {Kind: KindCall, Name: "exp", Args: []*Node{nil}},
		"nested nil":            {Kind: KindBinary, Op: "+", Args: []*Node{name(), {Kind: KindNeg, Args: []*Node{nil}}}},
		"cycle":                 cycle,
		"deeper than any parse": deep,
	}
	for what, n := range cases {
		c, err := Check(n, env)
		if !errors.Is(err, ErrInvalidTree) || c != nil {
			t.Errorf("%s: Check = %v, %v; want ErrInvalidTree", what, c, err)
		}
		var e *Error
		if errors.As(err, &e) {
			t.Errorf("%s: an *Error (a person's) for an internal one", what)
		}
	}
	if _, err := Check(name(), nil); !errors.Is(err, ErrInvalidTree) {
		t.Errorf("no environment: %v", err)
	}
	// A call of the wrong arity is a tree Parse makes: the checker's error, with its span.
	_, err := Check(&Node{Kind: KindCall, Name: "exp", Args: []*Node{name(), name()}}, env)
	var e *Error
	if !errors.As(err, &e) || e.Code != CodeArity {
		t.Errorf("exp(mu, mu): %v, want %s", err, CodeArity)
	}
}

func check(t *testing.T, env Env, src string) *Checked {
	t.Helper()
	c, err := CheckText(src, env)
	if err != nil {
		t.Fatalf("%q: %v", src, err)
	}
	return c
}

func TestCheckRecordsLagsAndLifts(t *testing.T) {
	envs := readEnvs(t)
	series := newTestEnv(t, envs["series"], "rows")
	c := check(t, series, "phi * lag(y, 2) + phi * lag(y, 1) + lag(level, 2) + mu_region[region]")
	if !slices.Equal(c.Lags, []int{1, 2}) || len(c.Lifts) != 0 {
		t.Errorf("lags %v, lifts %v", c.Lags, c.Lifts)
	}
	if c.Context != "rows" {
		t.Errorf("context %q", c.Context)
	}
	schools := newTestEnv(t, envs["schools"], "p1")
	c = check(t, schools, "mu_district[district] + alpha_school[school] - mu_district[district]")
	// alpha_school[school] per school is alpha_school itself: not a lift.
	want := []Lift{{Index: "district", ID: "n5", Rows: "rows", Context: "p1"}}
	if !slices.Equal(c.Lifts, want) || len(c.Lags) != 0 {
		t.Errorf("lifts %+v, lags %v", c.Lifts, c.Lags)
	}
	for ctx, src := range map[Plate]string{"p1": "alpha_school[school]", "levels:n6": "beta_color[color]"} {
		c := check(t, newTestEnv(t, envs["schools"], ctx), src)
		if len(c.Lifts) != 0 || c.Tree.Lifted || c.Tree.By == nil || c.Tree.Type.String() != "real["+string(ctx)+"]" {
			t.Errorf("%s in %s: %+v %+v", src, ctx, c.Lifts, c.Tree)
		}
	}
	// An indexing on the context's own rows is not a lift.
	c = check(t, newTestEnv(t, envs["schools"], "rows"), "mu_district[district]")
	if len(c.Lifts) != 0 || c.Tree.Lifted || c.Tree.By == nil || c.Tree.By.ID != "n5" || c.Tree.Sym.ID != "n13" {
		t.Errorf("rows: %+v", c.Tree)
	}
}

// The typed tree gives the emitter what it needs: the symbols of the names, and which integer
// leaves it converts. Every operation is real; only a leaf is int.
func TestCheckTypedTree(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "rows")
	c := check(t, env, "(n_total + n_total) / count + alpha_school[school]")
	div := c.Tree.Args[0]
	if div.Node.Op != "/" || div.Type.String() != "real[rows]" || div.Convert {
		t.Fatalf("div: %v %s", div.Node.Op, div.Type)
	}
	sum, r := div.Args[0], div.Args[1]
	if sum.Type.String() != "real" || sum.Convert {
		t.Errorf("sum: %s %v", sum.Type, sum.Convert)
	}
	for _, leaf := range sum.Args {
		if leaf.Type.String() != "int" || !leaf.Convert || leaf.Sym.ID != "n19" {
			t.Errorf("sum's leaf: %s %v", leaf.Type, leaf.Convert)
		}
	}
	if r.Type.String() != "int[rows]" || !r.Convert || r.Sym.ID != "n3" {
		t.Errorf("divisor: %s %v", r.Type, r.Convert)
	}
	if ix := c.Tree.Args[1]; ix.Sym.ID != "n12" || ix.By.ID != "n4" || ix.Lifted || ix.Convert {
		t.Errorf("index: %+v", ix)
	}
	// A whole expression that is one integer leaf stays int, not converted.
	for _, src := range []string{"count", "n_school[school]", "((count))"} {
		if c := check(t, env, src); c.Tree.Type.Elem != ElemInt || c.Tree.Convert {
			t.Errorf("%s: %s %v", src, c.Tree.Type, c.Tree.Convert)
		}
	}
	for _, src := range []string{"-n_total", "n_total * n_total", "exp(n_total)", "n_school[school] ^ 2"} {
		c := check(t, env, src)
		if c.Tree.Type.Elem != ElemReal || !c.Tree.Args[0].Convert {
			t.Errorf("%s: %s, leaf converted %v", src, c.Tree.Type, c.Tree.Args[0].Convert)
		}
	}
	// A lag is real; its integer node is converted, even alone.
	series := newTestEnv(t, readEnvs(t)["series"], "rows")
	for src, conv := range map[string]bool{"lag(u, 1)": true, "lag(y, 1)": false} {
		if c := check(t, series, src); c.Tree.Type.String() != "real[rows]" || c.Tree.Convert != conv {
			t.Errorf("%s: %s %v", src, c.Tree.Type, c.Tree.Convert)
		}
	}
}

// The limit of lag is the catalogue's.
func TestLagLimitIsTheCatalogues(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["series"], "rows")
	max := bayescatalog.GetLimits().MaxLag
	check(t, env, "lag(y, "+strconv.Itoa(max)+")")
	_, err := CheckText("lag(y, "+strconv.Itoa(max+1)+")", env)
	var e *Error
	if !errors.As(err, &e) || e.Code != CodeLagK || e.Args["max"] != strconv.Itoa(max) {
		t.Errorf("%v", err)
	}
}

// The deepest and largest trees the parser allows are typed without trouble.
func TestCheckAtTheLimits(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "rows")
	lim := bayescatalog.GetLimits()
	for _, src := range []string{
		strings.Repeat("(", lim.MaxExpressionNesting) + "score" + strings.Repeat(")", lim.MaxExpressionNesting),
		strings.Repeat("-", lim.MaxExpressionNesting) + "score",
		"score" + strings.Repeat("^score", lim.MaxExpressionNesting),
		"score" + strings.Repeat("+score", (lim.MaxExpressionNodes-1)/2),
		"theta" + strings.Repeat("*theta", (lim.MaxExpressionNodes-1)/2),
	} {
		c := check(t, env, src)
		if c.Tree.Type.String() != "real[rows]" {
			t.Errorf("%.20q: %s", src, c.Tree.Type)
		}
	}
	// A product of 128 values in (0, 1) stays in (0, 1).
	c := check(t, env, "theta"+strings.Repeat("*theta", 127))
	if c.Interval() != (Interval{Lo: 0, Hi: 1, LoOpen: true, HiOpen: true}) {
		t.Errorf("%+v", c.Interval())
	}
}

func iv(lo, hi float64, loOpen, hiOpen bool) Interval {
	return Interval{Lo: lo, Hi: hi, LoOpen: loOpen, HiOpen: hiOpen}
}

var inf = math.Inf(1)

func TestIntervalArithmetic(t *testing.T) {
	pos := iv(0, inf, true, true)
	nonneg := iv(0, inf, false, true)
	unit := iv(0, 1, true, true)
	all := Unbounded()
	for _, c := range []struct {
		name string
		got  Interval
		want Interval
	}{
		{"neg", negInterval(iv(1, 2, true, false)), iv(-2, -1, false, true)},
		{"neg zero", negInterval(Point(0)), Point(0)},
		{"add", addInterval(nonneg, pos), pos},
		{"add unbounded", addInterval(all, Point(1)), all},
		{"sub", subInterval(Point(1), unit), unit},
		{"sub pos", subInterval(pos, pos), all},
		{"mul unit", mulInterval(unit, unit), unit},
		{"mul closed zero", mulInterval(iv(0, 1, false, false), unit), iv(0, 1, false, true)},
		{"mul zero by unbounded", mulInterval(Point(0), all), Point(0)},
		{"mul nonneg nonneg", mulInterval(nonneg, nonneg), nonneg},
		{"mul signs", mulInterval(iv(-1, 1, false, false), iv(-1, 1, true, false)), iv(-1, 1, false, false)},
		{"mul negative", mulInterval(Point(-3), pos), iv(math.Inf(-1), 0, true, true)},
		{"mul unbounded", mulInterval(iv(-1, 1, true, true), nonneg), all},
		{"div pos", divInterval(Point(1), pos), pos},
		{"div by zero closed", divInterval(Point(1), nonneg), all},
		{"div by zero inside", divInterval(Point(1), iv(-1, 1, true, true)), all},
		{"div negative open zero", divInterval(Point(2), iv(-1, 0, true, true)), iv(math.Inf(-1), -2, true, true)},
		{"div by unit", divInterval(Point(1), unit), iv(1, inf, true, true)},
		{"div by unbounded positive", divInterval(iv(math.Inf(-1), 5, true, false), iv(1, inf, false, true)), iv(math.Inf(-1), 5, true, false)},
		{"pow positive", powInterval(unit), pos},
		{"pow nonneg", powInterval(nonneg), all},
		{"overflow", mulInterval(Point(1e300), Point(1e300)), all},
		{"ieee", addInterval(Point(0.1), Point(0.2)), Point(0.30000000000000004)},
	} {
		if c.got != c.want {
			t.Errorf("%s: %+v, want %+v", c.name, c.got, c.want)
		}
		if math.Signbit(c.got.Lo) && c.got.Lo == 0 || math.Signbit(c.got.Hi) && c.got.Hi == 0 {
			t.Errorf("%s: -0", c.name)
		}
	}
}

func TestIntervalWithin(t *testing.T) {
	pos := NamedInterval("positive")
	if pos != iv(0, inf, true, true) {
		t.Fatalf("positive: %+v", pos)
	}
	for _, c := range []struct {
		i    Interval
		want Relation
	}{
		{pos, Inside},
		{iv(0, 1, true, true), Inside},
		{Point(2), Inside},
		{iv(0, inf, false, true), Overlap},
		{Unbounded(), Overlap},
		{Point(0), Disjoint},
		{iv(math.Inf(-1), 0, true, false), Disjoint},
		{iv(math.Inf(-1), 0, true, true), Disjoint},
		{iv(-1, 0.5, false, false), Overlap},
	} {
		if got := c.i.Within(pos); got != c.want {
			t.Errorf("%+v within (0, inf): %d, want %d", c.i, got, c.want)
		}
	}
	unit := NamedInterval("unit")
	if Point(1).Within(unit) != Disjoint || iv(0, 1, false, true).Within(unit) != Overlap || Point(0.5).Within(unit) != Inside {
		t.Errorf("unit")
	}
	if NamedInterval("no such") != Unbounded() || NamedInterval("real") != Unbounded() {
		t.Errorf("real")
	}
}

// Every function of the catalogue but lag has a known range, which a call gives.
func TestFunctionRanges(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "")
	for _, f := range bayescatalog.Declared().Language.Functions {
		if f.Apart {
			continue
		}
		if !bayescatalog.Declared().Intervals.Has(f.Range) {
			t.Errorf("%s: range %q", f.Name, f.Range)
		}
		c := check(t, env, f.Name+"(mu)")
		if c.Interval() != NamedInterval(f.Range) {
			t.Errorf("%s: %+v", f.Name, c.Interval())
		}
	}
}

// An Env that answers IndexOf false: needs_index has no index argument.
func TestNeedsIndexWithoutAName(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "rows")
	env.nodes = slices.DeleteFunc(slices.Clone(env.nodes), func(n envNode) bool { return n.Name == "school" })
	_, err := CheckText("alpha_school", env)
	var e *Error
	if !errors.As(err, &e) || e.Code != CodeNeedsIndex || len(e.Args) != 2 || e.Args["plate"] != "p1" {
		t.Errorf("%v", err)
	}
}
