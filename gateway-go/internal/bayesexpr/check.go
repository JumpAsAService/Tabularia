package bayesexpr

import (
	"slices"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the type checker (docs/specs/bayes-expression-language.md, «Types and shapes»):
// every name of a tree is a node of the graph, with its type and its plate; the rules bring every
// value that is not a single one to the slot's context plate C, so two vectors that meet in an
// operator are always over the same plate. The checker stops at the first error.

// Plate is the id of a plate of the graph; "" is the scalar plate (a single value).
type Plate string

// Elem is the kind of the values a node holds.
type Elem uint8

const (
	ElemReal  Elem = iota + 1 // real numbers
	ElemInt                   // integers
	ElemIndex                 // a group index: for each unit of its plate, a unit of its target
	ElemTime                  // the order of a series: not a number
	// ElemNotNumber is a named value that is not a number (the correlation matrix of correlated
	// effects; any other such value later): refused wherever it is written.
	ElemNotNumber
)

// Type is the type of a node or of a value: real or int, scalar (Plate "") or one per unit of
// Plate; an index is index[Plate→Target].
type Type struct {
	Elem   Elem
	Plate  Plate
	Target Plate
}

// String is the type as the corpus writes it: real, int, real[rows], int[p1], index[rows->p1],
// time[rows].
func (t Type) String() string {
	var s string
	switch t.Elem {
	case ElemReal:
		s = "real"
	case ElemInt:
		s = "int"
	case ElemIndex:
		return "index[" + string(t.Plate) + "->" + string(t.Target) + "]"
	case ElemTime:
		s = "time"
	case ElemNotNumber:
		s = "not_number"
	default:
		return "?"
	}
	if t.Plate == "" {
		return s
	}
	return s + "[" + string(t.Plate) + "]"
}

// Role is the kind of graph node a name stands for (the graph's node kinds).
type Role string

const (
	RoleData            Role = "data"
	RoleParameter       Role = "parameter"
	RoleTransformed     Role = "transformed"
	RoleLinearPredictor Role = "linear_predictor"
	RoleState           Role = "state"
	RoleComponent       Role = "component"   // a component of correlated effects
	RoleCorrelation     Role = "correlation" // the correlation matrix of correlated effects (ElemNotNumber)
)

// lagRoles are the nodes lag may take (data, parameter, transformed, latent state).
var lagRoles = []Role{RoleData, RoleParameter, RoleTransformed, RoleState}

// Symbol is a named node of the graph, as the environment gives it.
type Symbol struct {
	ID   string
	Role Role
	Type Type
	// Interval is the node's declared interval (a parameter's range, a data node's support);
	// nil is unbounded.
	Interval *Interval
}

// Env is what model-graph gives for one slot.
//
// Encloses is reflexive; a group plate encloses the plates nested in it and the rows; it is never
// called with the scalar plate "". IsSeries is never called with "".
type Env interface {
	Lookup(name string) (Symbol, bool)   // a named node: its id, type, plate, role, declared interval
	Context() Plate                      // the plate of the slot's node ("" for a scalar node)
	Encloses(outer, inner Plate) bool    // reflexive; a group plate encloses its nested plates and the rows
	IsSeries(p Plate) bool               // the rows of a series
	IndexOf(target Plate) (string, bool) // the name of the group index whose target is `target`, for messages
}

// Typed is a node of a checked tree with its type: Elem is ElemReal or ElemInt, Plate is "" or
// the context plate. Only a leaf is ever int (a name, an indexing, a lag's node): every
// operation and every call gives a real, and every lag a real over the context.
type Typed struct {
	Node *Node
	Type Type
	Args []*Typed // the operands of a call, a unary minus or a binary operator, as Node.Args
	// Sym is the node a name, an indexing (x of x[g]) or a lag stands for; By is an indexing's
	// group index.
	Sym *Symbol
	By  *Symbol
	// Lifted: an indexing whose index is not on the context plate but lifted to it (each unit of
	// the context belongs to one unit of the index's target).
	Lifted bool
	// Convert: an integer leaf whose value enters an operation or a call, or the integer node of
	// a lag. The emitter makes it real on the leaf itself ((1.0 * n) for a single integer,
	// to_vector(n) over a plate), so an expression never divides integers nor computes with
	// Stan's 32-bit integers. A whole expression that is one integer leaf stays int (and is not
	// converted): model-graph needs it for integer slots.
	Convert bool
}

// Lift is a group index brought to a plate other than its own: the data array of the unit of
// Index's target for each unit of Context, which the runner builds and checks.
type Lift struct {
	Index   string // the index's name
	ID      string // the index's node id
	Rows    Plate  // the index's own plate
	Context Plate
}

// Checked is a typed expression.
type Checked struct {
	Tree    *Typed
	Context Plate
	Lags    []int  // the distinct k of its lags, ascending
	Lifts   []Lift // the distinct lifted indices, by name
}

// Interval is the static interval of the expression's value.
func (c *Checked) Interval() Interval { return c.Tree.Interval() }

// CheckText reads src and types it against env: the typed tree or the first error, syntactic
// or of types.
func CheckText(src string, env Env) (*Checked, error) {
	n, err := Parse(src)
	if err != nil {
		return nil, err
	}
	return Check(n, env)
}

// Check types a tree against env. The error, when there is one, is an *Error: the first met
// walking the tree left to right, a node's own errors before its operands'.
func Check(n *Node, env Env) (*Checked, error) {
	c := &checker{env: env, ctx: env.Context(), maxLag: int64(catalogLimits.MaxLag)}
	t, err := c.check(n)
	if err != nil {
		return nil, err
	}
	slices.Sort(c.lags)
	slices.SortFunc(c.lifts, func(a, b Lift) int {
		if a.Index < b.Index {
			return -1
		}
		if a.Index > b.Index {
			return 1
		}
		return 0
	})
	return &Checked{Tree: t, Context: c.ctx, Lags: slices.Compact(c.lags), Lifts: slices.Compact(c.lifts)}, nil
}

type checker struct {
	env    Env
	ctx    Plate
	maxLag int64
	lags   []int
	lifts  []Lift
}

func errSpan(s span, code Code, args map[string]string) *Error {
	return &Error{Code: code, Pos: s.pos, Len: s.len, Args: args}
}

// argsOf builds an error's args, leaving out the empty values (the scalar plate).
func argsOf(kv ...string) map[string]string {
	m := map[string]string{}
	for i := 0; i+1 < len(kv); i += 2 {
		if kv[i+1] != "" {
			m[kv[i]] = kv[i+1]
		}
	}
	return m
}

// over is the type of a value over the plates of its operands: the context when one of them is
// not a single value, a scalar otherwise.
func (c *checker) over(elem Elem, args ...*Typed) Type {
	for _, a := range args {
		if a.Type.Plate != "" {
			return Type{Elem: elem, Plate: c.ctx}
		}
	}
	return Type{Elem: elem}
}

func (c *checker) check(n *Node) (*Typed, *Error) {
	switch n.Kind {
	case KindNum:
		return &Typed{Node: n, Type: Type{Elem: ElemReal}}, nil
	case KindName:
		return c.name(n)
	case KindIndex:
		return c.index(n)
	case KindCall:
		return c.call(n)
	case KindLag:
		return c.lag(n)
	case KindNeg:
		x, err := c.check(n.Args[0])
		if err != nil {
			return nil, err
		}
		return &Typed{Node: n, Type: c.over(ElemReal, x), Args: operands(x)}, nil
	case KindBinary:
		l, err := c.check(n.Args[0])
		if err != nil {
			return nil, err
		}
		r, err := c.check(n.Args[1])
		if err != nil {
			return nil, err
		}
		return &Typed{Node: n, Type: c.over(ElemReal, l, r), Args: operands(l, r)}, nil
	}
	// The parser makes no other node.
	return nil, errSpan(n.own(), CodeUnknownName, nil)
}

// operands marks the integer leaves among the operands of an operation or a call: Integers are
// numbers, they enter arithmetic and become real, converted by the emitter on the leaf.
func operands(args ...*Typed) []*Typed {
	for _, a := range args {
		if a.Type.Elem == ElemInt {
			a.Convert = true
		}
	}
	return args
}

// lookup finds a name that must be a number: unknown nodes, time nodes and values that are not
// numbers are refused at s (a name, either name of an indexing, a lag's name; a call's argument
// is a name).
func (c *checker) lookup(name string, s span) (Symbol, *Error) {
	sym, ok := c.env.Lookup(name)
	if !ok {
		return Symbol{}, errSpan(s, CodeUnknownName, argsOf("name", name))
	}
	if sym.Type.Elem == ElemTime {
		return Symbol{}, errSpan(s, CodeTimeValue, argsOf("name", name))
	}
	if sym.Type.Elem == ElemNotNumber {
		return Symbol{}, errSpan(s, CodeNotNumber, argsOf("name", name))
	}
	return sym, nil
}

// name: x alone. A single value is a single value anywhere; a value over the context is itself;
// a value over a plate that encloses the context needs its index; any other plate is wrong.
func (c *checker) name(n *Node) (*Typed, *Error) {
	s := n.own()
	sym, err := c.lookup(n.Name, s)
	if err != nil {
		return nil, err
	}
	if sym.Type.Elem == ElemIndex {
		return nil, errSpan(s, CodeIndexAlone, argsOf("name", n.Name, "target", string(sym.Type.Target)))
	}
	p := sym.Type.Plate
	switch {
	case p == "":
		return &Typed{Node: n, Type: Type{Elem: sym.Type.Elem}, Sym: &sym}, nil
	case p == c.ctx:
		return &Typed{Node: n, Type: Type{Elem: sym.Type.Elem, Plate: c.ctx}, Sym: &sym}, nil
	case c.ctx != "" && c.env.Encloses(p, c.ctx):
		index, _ := c.env.IndexOf(p)
		return nil, errSpan(s, CodeNeedsIndex, argsOf("name", n.Name, "plate", string(p), "index", index))
	}
	return nil, errSpan(s, CodeWrongPlate, argsOf("name", n.Name, "plate", string(p), "context", string(c.ctx)))
}

// index: x[g]. x's own errors, then g's, then the two together: x over g's target, and g on the
// context (C = P) or lifted to it (G encloses C, C encloses P).
func (c *checker) index(n *Node) (*Typed, *Error) {
	xs, gs := n.sub[0], n.sub[1]
	x, err := c.lookup(n.Name, xs)
	if err != nil {
		return nil, err
	}
	if x.Type.Elem == ElemIndex {
		return nil, errSpan(xs, CodeIndexAlone, argsOf("name", n.Name, "target", string(x.Type.Target)))
	}
	if x.Type.Plate == "" {
		return nil, errSpan(xs, CodeIndexScalar, argsOf("name", n.Name, "by", n.By))
	}
	g, err := c.lookup(n.By, gs)
	if err != nil {
		return nil, err
	}
	if g.Type.Elem != ElemIndex {
		return nil, errSpan(gs, CodeNotIndex, argsOf("name", n.By))
	}
	rows, target := g.Type.Plate, g.Type.Target
	if x.Type.Plate != target {
		return nil, errSpan(n.own(), CodeIndexTarget,
			argsOf("name", n.Name, "plate", string(x.Type.Plate), "by", n.By, "target", string(target)))
	}
	lifted := false
	switch {
	case c.ctx == rows:
	case c.ctx != "" && c.env.Encloses(target, c.ctx) && c.env.Encloses(c.ctx, rows):
		lifted = true
		c.lifts = append(c.lifts, Lift{Index: n.By, ID: g.ID, Rows: rows, Context: c.ctx})
	default:
		return nil, errSpan(n.own(), CodeIndexContext,
			argsOf("name", n.Name, "by", n.By, "rows", string(rows), "target", string(target), "context", string(c.ctx)))
	}
	return &Typed{Node: n, Type: Type{Elem: x.Type.Elem, Plate: c.ctx}, Sym: &x, By: &g, Lifted: lifted}, nil
}

// functions are the language's functions by name, lag aside.
var functions = func() map[string]bayescatalog.Function {
	m := map[string]bayescatalog.Function{}
	for _, f := range bayescatalog.Declared().Language.Functions {
		if !f.Apart {
			m[f.Name] = f
		}
	}
	return m
}()

// call: f(e1, …). The name and the arity before the arguments; the arguments are values of the
// context (an index alone among them is index_alone).
func (c *checker) call(n *Node) (*Typed, *Error) {
	s := n.own()
	nameSpan := span{s.pos, len(n.Name)}
	f, ok := functions[n.Name]
	if !ok {
		if _, isNode := c.env.Lookup(n.Name); isNode {
			return nil, errSpan(nameSpan, CodeNotFunction, argsOf("name", n.Name))
		}
		return nil, errSpan(nameSpan, CodeUnknownFunction, argsOf("name", n.Name))
	}
	if len(n.Args) != f.Arguments {
		return nil, errSpan(s, CodeArity, argsOf("name", n.Name,
			"want", strconv.Itoa(f.Arguments), "got", strconv.Itoa(len(n.Args))))
	}
	args := make([]*Typed, 0, len(n.Args))
	for _, a := range n.Args {
		t, err := c.check(a)
		if err != nil {
			return nil, err
		}
		args = append(args, t)
	}
	return &Typed{Node: n, Type: c.over(ElemReal, args...), Args: operands(args...)}, nil
}

// lag: lag(x, k), where the context is the rows of a series, x a node over them that lag may
// take, k in 1…max_lag.
func (c *checker) lag(n *Node) (*Typed, *Error) {
	xs, ks := n.sub[0], n.sub[1]
	if c.ctx == "" || !c.env.IsSeries(c.ctx) {
		return nil, errSpan(n.own(), CodeLagNotSeries, argsOf("name", n.Name, "context", string(c.ctx)))
	}
	x, err := c.lookup(n.Name, xs)
	if err != nil {
		return nil, err
	}
	reason := ""
	switch {
	case x.Type.Elem == ElemIndex:
		reason = "index"
	case !slices.Contains(lagRoles, x.Role):
		reason = "kind"
	case x.Type.Plate != c.ctx:
		reason = "plate"
	}
	if reason != "" {
		return nil, errSpan(xs, CodeLagArgument, argsOf("name", n.Name, "reason", reason))
	}
	if n.K < 1 || n.K > c.maxLag {
		return nil, errSpan(ks, CodeLagK, argsOf("max", strconv.FormatInt(c.maxLag, 10)))
	}
	c.lags = append(c.lags, int(n.K))
	// A lag is real over the context; an integer node (Sym's type says so) is converted.
	return &Typed{Node: n, Type: Type{Elem: ElemReal, Plate: c.ctx}, Sym: &x, Convert: x.Type.Elem == ElemInt}, nil
}
