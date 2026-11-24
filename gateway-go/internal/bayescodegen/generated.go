package bayescodegen

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the generated quantities (docs/specs/bayes-stan-codegen.md, «Blocks and their
// order», «The extension point»): the correlation matrices of the correlated effects, then what an
// Extension adds: its written declarations, then one nested block holding the per-row locals it
// asked for (rebuilt as the model block writes them, in Order, after the categories' Full_ they
// read) and its statements. Its data come after Mode.
//
// An extension never writes text. Its declarations (Decl) and statements (Stmt) are typed values,
// and every value in them (Expr) is made by this package: a node's identifier, a slot's or a
// node's value emitted through bayesexpr.Emit (with the extension's own identifiers in place of
// some nodes', for the prior's ancestral draws; on the rows an array of its data names, lot P1 of
// predictive-checks), a call of a name the catalogue lists, a number re-printed from its double,
// an indexing, the loop's index, a sum and a product of two values. Its names have the extension's
// prefixes (Ppc_, Prior_), which no identifier of the program has. A refused declaration or
// statement is ErrExtension; the program it would have made is never returned. Everything it adds
// passes the same checks as the rest of the program.

// Extension adds to a program what predictive-checks needs: data (bins fixed from the observed
// data), written generated quantities (statistics of y_rep, prior draws), and the statements that
// fill them. Everything it writes passes the same checks as the rest of the program.
type Extension interface {
	Data(m *bayesgraph.Model) []Decl
	Generated(m *bayesgraph.Model, s Scope) (written []Decl, statements []Stmt, err error)
}

// Scope lets an extension write a node's value without writing text of its own: the identifiers of
// the program, the per-row locals rebuilt in the extension's block, a node's or a slot's value
// emitted against another source of identifiers (the prior's ancestral draws instead of the
// parameters), the sizes of the plates and of the rows a statement runs on, and calls of the
// catalogue's names.
type Scope interface {
	// Identifier is a node's identifier as the generated quantities see it: data, transformed
	// data, a parameter, a transformed parameter, a state, a component ("<node>.<component>"), a
	// correlation ("<node>.correlation"). A value per row is a local: ask Local.
	Identifier(node string) (Expr, error)
	// Local asks for a node's per-row local (a transformed node or a linear predictor on the
	// rows) in the nested block, with the locals it reads. A lagged local is computed on Rows_lag
	// only (its other rows are not a number).
	Local(node string) (Expr, error)
	// ValueWith is a transformed node's value or a linear predictor's sum emitted with ids in
	// place of the program's identifiers where ids answers (an identifier: a Decl's Ref); on
	// Rows_lag when the node is lagged. The prior's path never reads the posterior's values: a
	// value per row it reads that reads a node ids answers (directly or through what it reads)
	// is rebuilt with ids too, in a local of its own (E_<id>_<k>, written before the statement);
	// any other node that does (a parameter whose prior reads one, a transformed parameter, a
	// component) and that ids does not answer is ErrExtension.
	ValueWith(node string, ids func(node string) (Expr, bool)) (Expr, error)
	// ValueOn is ValueWith on some of the node's rows: the positions among the rows its value is
	// computed on (every row, or Rows_lag when it is lagged) that rows, an array of integers of the
	// extension's data, names. Every value over the node's plate in it is taken at those rows
	// (x[Ppc_o1_rows], x[Rows_lag[Ppc_o1_rows]], lag(x, 1) as x[Lag1[Ppc_o1_rows]]), and so is a
	// value per row it rebuilds. The node is over a plate.
	ValueOn(node string, ids func(node string) (Expr, bool), rows Expr) (Expr, error)
	// Slot is any slot's value (a likelihood's argument, a prior's) emitted as ValueWith does, on
	// the rows its statement runs on.
	Slot(key bayesgraph.SlotKey, ids func(node string) (Expr, bool)) (Expr, error)
	// SlotOn is Slot on some of its statement's rows: the positions among the rows the statement
	// runs on (every row, Rows_lag for a lagged likelihood, Rows_first for a state's first values)
	// that rows, an array of integers of the extension's data, names, as ValueOn takes them. The
	// slot's context is a plate; a value of it that is a single value stays one.
	SlotOn(key bayesgraph.SlotKey, ids func(node string) (Expr, bool), rows Expr) (Expr, error)
	// Call is a call of a name the catalogue lists (a density, a generator, a function, an
	// internal function); anything else is an error.
	Call(name string, args ...Expr) (Expr, error)
	// PlateSize is the number of units of a plate as the manifests count them: N, J_<plate>, and
	// K_<category> - 1 for a category's levels plate (the levels after the reference, as its
	// coefficients are declared).
	PlateSize(plate string) (Size, error)
	// Levels is the number of levels of a category's levels plate, the reference included
	// (K_<category>): the size of the coefficients with the reference's 0 in front (Full_).
	Levels(plate string) (Size, error)
	// Rows is the number of rows a likelihood's statement or a per-row value runs on: N, or N_lag
	// when it is lagged.
	Rows(node string) (Size, error)
}

// Elem is the element type of a declaration.
type Elem int

const (
	Real Elem = iota + 1
	Int
)

// Shape is the shape of a declaration.
type Shape int

const (
	Scalar Shape = iota + 1
	Vector       // vector[Size] (reals)
	Array        // array[Size] real or int
)

// Size is the size of a declaration or of a loop: a plate's units, the rows of a statement, an
// integer of the extension's data, a count; WhenPrior makes it Mode == 1 ? size : 0, so that the
// posterior run writes nothing of what only the prior predictive fills.
type Size struct {
	text  string
	dim   bayescontract.Dim
	prior bool
	ext   string // the extension's data integer it is, when it is one
}

// AsSize is the size an integer of the extension's data holds (declared before, in Data).
func (d Decl) AsSize() Size {
	if !extensionName(d.Name) {
		return Size{}
	}
	return Size{text: d.Name, dim: bayescontract.Dim{Size: d.Name}, ext: d.Name}
}

// sizeOK checks a size: set, and an integer of the extension's data when it names one.
func (g *gen) sizeOK(s Size) error {
	if s.text == "" {
		return extErr("a size that is not one")
	}
	if s.ext != "" {
		d, ok := g.extNames[s.ext]
		if !ok || d.Kind == "" || d.Elem != Int || d.Shape != Scalar {
			return extErr("%s is not an integer of the extension's data declared before", s.ext)
		}
	}
	return nil
}

// maxCount bounds a fixed count (a number of bins, of draws kept).
const maxCount = 1_000_000

// Count is a fixed number of elements.
func Count(n int) (Size, error) {
	if n < 0 || n > maxCount {
		return Size{}, fmt.Errorf("%w: the count %d", ErrExtension, n)
	}
	s := strconv.Itoa(n)
	return Size{text: s, dim: bayescontract.Dim{Size: s}}, nil
}

// WhenPrior is the size in the prior predictive (Mode 1), 0 in the posterior.
func (s Size) WhenPrior() Size {
	s.prior = true
	return s
}

func (s Size) String() string {
	if s.prior {
		return "Mode == 1 ? " + s.text + " : 0"
	}
	return s.text
}

// bound is the size as a loop's upper bound or an array's size (parenthesized in a range).
func (s Size) bound() string {
	if strings.ContainsRune(s.String(), ' ') {
		return "(" + s.String() + ")"
	}
	return s.String()
}

// Decl is a declaration of the extension: in data (Kind its kind in the data manifest; Of a data
// node of the model it is about, the manifest's node: an outcome, a group index), written in the
// generated quantities (Role check, prior_draw or prior_marginal, with Check and Of), or local to
// the nested block (Declare).
type Decl struct {
	Name        string
	Elem        Elem
	Shape       Shape
	Size        Size
	NonNegative bool   // an integer at least 0
	Kind        string // data: the data manifest's kind
	Role        string // written: RoleCheck, RolePriorDraw or RolePriorMarginal
	Check       string // RoleCheck: the statistic
	// Of is, for RolePriorDraw and RolePriorMarginal, the node or component drawn; for RoleCheck, the
	// observed data node of the outcome it checks (optional); for data, a data node of the model
	// (optional).
	Of string
	// RowsOf is, for data that are positions among the rows a likelihood's statement runs on (an
	// array of integers), that likelihood's id: the data manifest's `of` names those rows, the rows
	// plate when it runs on every row, Rows_lag when it is lagged (optional).
	RowsOf string
}

// Ref is the declaration's identifier as a value. A name outside the extension's form gives a
// value that every statement refuses.
func (d Decl) Ref() Expr {
	if !extensionName(d.Name) {
		return Expr{}
	}
	return Expr{text: d.Name, ident: d.Name, root: d.Name, ext: []string{d.Name}}
}

// Expr is a value an extension writes, made only by this package.
type Expr struct {
	text  string
	ident string   // the identifier, when the value is one
	root  string   // the identifier an indexing starts from
	ext   []string // the extension's names it reads
	loop  bool     // it reads the loop's index
	// pre are the locals of a split value (lot S5), written before the statement that uses it,
	// with their mapping entries.
	pre     []string
	entries []bayescontract.MappingEntry
}

func (e Expr) ok() bool { return e.text != "" }

// Num is a real number, re-printed from its double.
func Num(v float64) (Expr, error) {
	s, err := realLiteral(v)
	if err != nil {
		return Expr{}, fmt.Errorf("%w: a number that is not finite", ErrExtension)
	}
	if v < 0 {
		s = "(" + s + ")"
	}
	return Expr{text: s}, nil
}

// Integer is a non-negative integer literal (an index).
func Integer(n int) (Expr, error) {
	if n < 0 || n > maxCount {
		return Expr{}, fmt.Errorf("%w: the integer %d", ErrExtension, n)
	}
	return Expr{text: strconv.Itoa(n)}, nil
}

// LoopIndex is the index of the loop a statement is in (I).
func LoopIndex() Expr { return Expr{text: "I", loop: true} }

// Index is e[i]: an identifier (or an indexing of one) indexed.
func Index(e, i Expr) (Expr, error) {
	if !e.ok() || !i.ok() || e.root == "" {
		return Expr{}, fmt.Errorf("%w: an indexing of what is not a variable", ErrExtension)
	}
	return Expr{text: e.text + "[" + i.text + "]", root: e.root, ext: join(e.ext, i.ext), loop: e.loop || i.loop,
		pre: join(e.pre, i.pre), entries: append(append([]bayescontract.MappingEntry(nil), e.entries...), i.entries...)}, nil
}

func join(a, b []string) []string { return append(append([]string(nil), a...), b...) }

// Add is (a + b). With Mul, the only arithmetic an extension writes (a difference is a sum with
// a product by -1.0, a quotient a product with inv): fully parenthesized, never the person's text.
func Add(a, b Expr) (Expr, error) { return arithmetic(a, "+", b) }

// Mul is (a * b): Stan's product, of two single values or of a single value and a vector (two
// vectors are refused by stanc, never written elementwise).
func Mul(a, b Expr) (Expr, error) { return arithmetic(a, "*", b) }

func arithmetic(a Expr, op string, b Expr) (Expr, error) {
	if !a.ok() || !b.ok() {
		return Expr{}, extErr("an operand of %s that is no value", op)
	}
	return Expr{text: "(" + a.text + " " + op + " " + b.text + ")", ext: join(a.ext, b.ext), loop: a.loop || b.loop,
		pre: join(a.pre, b.pre), entries: append(append([]bayescontract.MappingEntry(nil), a.entries...), b.entries...)}, nil
}

// Stmt is a statement of the extension: an assignment, a for over a size, a local declaration.
type Stmt struct {
	kind          stmtKind
	decl          Decl
	target, value Expr
	size          Size
	body          []Stmt
}

type stmtKind int

const (
	stmtAssign stmtKind = iota + 1
	stmtFor
	stmtDeclare
)

// Assign is target = value; target is the extension's written or local variable, or an indexing
// of one.
func Assign(target, value Expr) Stmt { return Stmt{kind: stmtAssign, target: target, value: value} }

// For is for (I in 1:size) { body }; loops do not nest.
func For(size Size, body ...Stmt) Stmt { return Stmt{kind: stmtFor, size: size, body: body} }

// Declare declares a local of the nested block, with its value when value is not the zero Expr.
func Declare(d Decl, value Expr) Stmt { return Stmt{kind: stmtDeclare, decl: d, value: value} }

// extensionPrefixes are the prefixes of an extension's names: no identifier of the program has
// them (a person's name starts with a lower-case letter; the generator's own are N, J_, K_, Up_,
// N_lag, Rows_*, Lag<k>, N_first, N_next, Prev, Mode, V_, Z_, L_, W_, Full_, Arg<k>_, Omega_, I).
var extensionPrefixes = []string{"Ppc_", "Prior_"}

// extensionName reports a name of the extension: one of its prefixes then [a-z0-9]+ words joined
// by "_", at most 64 bytes, not ending with a suffix Stan reserves.
func extensionName(s string) bool {
	if len(s) > 64 {
		return false
	}
	rest := ""
	for _, p := range extensionPrefixes {
		if r, ok := strings.CutPrefix(s, p); ok {
			rest = r
		}
	}
	if rest == "" || rest[0] == '_' || rest[len(rest)-1] == '_' || strings.Contains(rest, "__") {
		return false
	}
	for i := 0; i < len(rest); i++ {
		if c := rest[i]; !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
		if strings.HasSuffix(s, suffix) {
			return false
		}
	}
	return true
}

// lowerWord reports [a-z][a-z_]*[a-z] (a kind, a statistic), at most 40 bytes.
func lowerWord(s string) bool {
	if len(s) < 2 || len(s) > 40 || s[0] < 'a' || s[0] > 'z' || s[len(s)-1] == '_' {
		return false
	}
	for i := 0; i < len(s); i++ {
		if c := s[i]; !(c >= 'a' && c <= 'z' || c == '_') {
			return false
		}
	}
	return true
}

// checkName reports a statistic's name: [a-z][a-z0-9_]*[a-z0-9] (q10, low_quantile), at most 40
// bytes (lot P2 of predictive-checks: the spec names its quantiles q10, q50, q90).
func checkName(s string) bool {
	if len(s) < 2 || len(s) > 40 || s[0] < 'a' || s[0] > 'z' || s[len(s)-1] == '_' {
		return false
	}
	for i := 0; i < len(s); i++ {
		if c := s[i]; !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

// dataNode reports a data node of the model (what an extension's data or a check may name).
func (g *gen) dataNode(id string) bool {
	n := g.node(id)
	return n != nil && n.Kind == "data"
}

func extErr(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrExtension, fmt.Sprintf(format, a...))
}

// declText is a declaration's type and name, "vector[N] Ppc_x", after its checks.
func (g *gen) declText(d Decl) (string, error) {
	if !extensionName(d.Name) {
		return "", extErr("%s is not a name of the extension (%s then lower-case words)", strconv.Quote(d.Name), strings.Join(extensionPrefixes, ", "))
	}
	if _, dup := g.extNames[d.Name]; dup {
		return "", extErr("%s declared twice", d.Name)
	}
	elem := ""
	switch d.Elem {
	case Real:
		if d.NonNegative {
			return "", extErr("%s: a real declared non-negative", d.Name)
		}
		elem = "real"
	case Int:
		elem = "int"
		if d.NonNegative {
			elem = "int<lower=0>"
		}
	default:
		return "", extErr("%s has no element type", d.Name)
	}
	if d.Shape != Scalar {
		if err := g.sizeOK(d.Size); err != nil {
			return "", fmt.Errorf("%s: %w", d.Name, err)
		}
	} else if d.Size != (Size{}) {
		return "", extErr("%s: a single value with a size", d.Name)
	}
	switch d.Shape {
	case Scalar:
		return elem + " " + d.Name, nil
	case Vector:
		if d.Elem != Real {
			return "", extErr("%s: a vector of integers", d.Name)
		}
		return "vector[" + d.Size.String() + "] " + d.Name, nil
	case Array:
		return "array[" + d.Size.String() + "] " + elem + " " + d.Name, nil
	}
	return "", extErr("%s has no shape", d.Name)
}

func (d Decl) dims() []bayescontract.Dim {
	if d.Shape == Scalar {
		return []bayescontract.Dim{}
	}
	return []bayescontract.Dim{d.Size.dim}
}

// coreDataKinds are the data kinds of the program itself (bayes-contract): an extension's data has a
// kind of its own, which the runner never mistakes for one of the program's.
var coreDataKinds = map[string]bool{
	bayescontract.DataSize: true, bayescontract.DataReal: true, bayescontract.DataInt: true, bayescontract.DataGroupIndex: true,
	bayescontract.DataCategory: true, bayescontract.DataLift: true, bayescontract.DataMode: true, bayescontract.DataRowsSubset: true,
	bayescontract.DataLag: true, bayescontract.DataFirst: true, bayescontract.DataNext: true, bayescontract.DataPrevious: true,
}

// writeExtensionData declares the extension's data, after Mode.
func (g *gen) writeExtensionData() error {
	if g.ext == nil {
		return nil
	}
	for _, d := range g.ext.Data(g.m) {
		if !lowerWord(d.Kind) || coreDataKinds[d.Kind] || d.Role != "" || d.Check != "" || d.Size.prior ||
			(d.Of != "" && !g.dataNode(d.Of)) {
			return extErr("the data %s: a kind of its own in the data manifest, no role, a size not by the mode, a data node or none", strconv.Quote(d.Name))
		}
		among := ""
		if d.RowsOf != "" {
			var err error
			if among, err = g.statementRows(d); err != nil {
				return err
			}
		}
		s, err := g.declText(d)
		if err != nil {
			return err
		}
		g.w.line(s + ";")
		g.extNames[d.Name] = d
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: d.Name, Block: blockData, Kind: bayescontract.KindExtension})
		g.data = append(g.data, bayescontract.DataEntry{Stan: d.Name, Kind: d.Kind, Node: d.Of, Of: among})
	}
	return nil
}

// statementRows is what the positions of an extension's datum are among: the rows plate when its
// likelihood's statement runs on every row, Rows_lag when the likelihood is lagged.
func (g *gen) statementRows(d Decl) (string, error) {
	n := g.node(d.RowsOf)
	if d.Elem != Int || d.Shape != Array || n == nil || n.Kind != "likelihood" {
		return "", extErr("the data %s: positions (an array of integers) among the rows of a likelihood's statement", strconv.Quote(d.Name))
	}
	if obs := g.node(n.Observed); obs == nil || obs.Plate == nil || *obs.Plate != g.rows.ID {
		return "", extErr("the data %s: the likelihood %s is not on the rows", strconv.Quote(d.Name), strconv.Quote(d.RowsOf))
	}
	if g.lagged[d.RowsOf] {
		return identRowsLag, nil
	}
	return g.rows.ID, nil
}

// writeGenerated writes the generated quantities: the correlations, then the extension's written
// declarations and its nested block.
func (g *gen) writeGenerated() error {
	var written []Decl
	var stmts []Stmt
	sc := &scope{g: g, locals: map[string]bool{}, fulls: map[string]bool{}}
	if g.ext != nil {
		var err error
		if written, stmts, err = g.ext.Generated(g.m, sc); err != nil {
			return fmt.Errorf("%w: %w", ErrExtension, err)
		}
		if sc.err != nil {
			return sc.err
		}
	}
	for _, id := range g.m.Order {
		if n := g.node(id); n.Kind == "correlated_effects" {
			if err := g.writeCorrelation(n); err != nil {
				return err
			}
		}
	}
	for _, d := range written {
		if d.Kind != "" {
			return extErr("the written %s has a data kind", strconv.Quote(d.Name))
		}
		switch d.Role {
		case bayescontract.RoleCheck:
			if !checkName(d.Check) || (d.Of != "" && !g.observed(d.Of)) {
				return extErr("the check %s: a statistic, of an observed data node or none", strconv.Quote(d.Name))
			}
		case bayescontract.RolePriorDraw:
			if d.Check != "" || !g.drawable(d.Of) {
				return extErr("the prior draw %s: of a node or component of the model", strconv.Quote(d.Name))
			}
			// the posterior run writes no element of a prior draw with a dimension (bayes-contract, «The quantity
			// manifest»): the runner expects none of its columns in the sample mode
			if d.Shape != Scalar && !d.Size.prior {
				return extErr("the prior draw %s: a size by the mode (WhenPrior)", strconv.Quote(d.Name))
			}
		case bayescontract.RolePriorMarginal:
			// one draw a draw of a quantity, in every run: a single value
			if d.Check != "" || !g.drawable(d.Of) || d.Shape != Scalar {
				return extErr("the prior marginal %s: a single value of a node or component of the model", strconv.Quote(d.Name))
			}
		default:
			return extErr("the written %s has the role %s", strconv.Quote(d.Name), strconv.Quote(d.Role))
		}
		s, err := g.declText(d)
		if err != nil {
			return err
		}
		g.w.line(s + ";")
		g.extNames[d.Name] = d
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: d.Name, Block: blockGenerated, Kind: bayescontract.KindExtension, Role: strp(d.Role)})
		g.quantities = append(g.quantities, bayescontract.Quantity{Stan: d.Name, Role: d.Role, Dims: d.dims(), Check: d.Check, Of: d.Of})
	}
	if len(sc.locals) == 0 && len(stmts) == 0 {
		return nil
	}
	g.w.openNested()
	if err := sc.writeLocals(); err != nil {
		return err
	}
	for _, s := range stmts {
		if err := g.writeStmt(s, 0); err != nil {
			return err
		}
	}
	g.w.close()
	return nil
}

// observed reports a data node a likelihood observes (the outcome a check names).
func (g *gen) observed(id string) bool {
	for _, n := range g.m.Graph.Nodes {
		if n.Kind == "likelihood" && n.Observed == id {
			return g.dataNode(id)
		}
	}
	return false
}

// drawable reports a node or component a prior draw may be of: a parameter, a transformed node,
// a linear predictor, a state, a component, a correlation.
func (g *gen) drawable(id string) bool {
	if _, ok := g.comps[id]; ok {
		return true
	}
	if owner, ok := strings.CutSuffix(id, ".correlation"); ok {
		_, ok := g.effects[owner]
		return ok
	}
	n := g.node(id)
	return n != nil && n.Kind != "data" && n.Kind != "likelihood" && n.Kind != "correlated_effects"
}

// writeStmt writes one statement of the extension, in the loop numbered loop (0: in none). A local
// declared in a loop is read only in that loop.
func (g *gen) writeStmt(s Stmt, loop int) error {
	switch s.kind {
	case stmtAssign:
		if !s.target.ok() || s.target.root == "" || !s.value.ok() {
			return extErr("an assignment without its variable or its value")
		}
		d, ok := g.extNames[s.target.root]
		if !ok || d.Kind != "" {
			return extErr("an assignment to %s, not a variable the extension writes", strconv.Quote(s.target.root))
		}
		if err := g.usable(loop, s.target, s.value); err != nil {
			return err
		}
		if err := g.writePre(s.target, s.value); err != nil {
			return err
		}
		g.w.line(s.target.text + " = " + s.value.text + ";")
	case stmtFor:
		if loop != 0 {
			return extErr("a loop inside a loop")
		}
		if err := g.sizeOK(s.size); err != nil {
			return err
		}
		if len(s.body) == 0 {
			return extErr("a loop without its body")
		}
		g.w.open("for (I in 1:" + s.size.bound() + ")")
		g.extLoops++
		for _, b := range s.body {
			if err := g.writeStmt(b, g.extLoops); err != nil {
				return err
			}
		}
		g.w.close()
	case stmtDeclare:
		d := s.decl
		if d.Kind != "" || d.Role != "" || d.Check != "" || d.Of != "" {
			return extErr("the local %s: no kind, no role", strconv.Quote(d.Name))
		}
		text, err := g.declText(d)
		if err != nil {
			return err
		}
		if s.value.ok() {
			if err := g.usable(loop, s.value); err != nil {
				return err
			}
			if err := g.writePre(s.value); err != nil {
				return err
			}
			text += " = " + s.value.text
		}
		g.w.line(text + ";")
		g.extNames[d.Name] = d
		if loop != 0 {
			g.extLoop[d.Name] = loop
		}
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: d.Name, Block: blockGenerated, Kind: bayescontract.KindExtension})
	default:
		return extErr("a statement of no kind")
	}
	return nil
}

// writePre writes the locals of split values before the statement that reads them. A split value is
// written once: a second statement with it would declare its locals twice (ask for it again).
func (g *gen) writePre(es ...Expr) error {
	for _, e := range es {
		for _, en := range e.entries {
			if g.extPre[en.Stan] {
				return extErr("a split value used twice (%s): ask for it again", en.Stan)
			}
			g.extPre[en.Stan] = true
		}
		for _, l := range e.pre {
			g.w.line(l)
		}
		g.mapping = append(g.mapping, e.entries...)
	}
	return nil
}

// usable checks that values read only what is declared, a loop's local only in its loop, and the
// loop's index inside a loop.
func (g *gen) usable(loop int, es ...Expr) error {
	for _, e := range es {
		if e.loop && loop == 0 {
			return extErr("the loop's index outside a loop")
		}
		for _, n := range e.ext {
			if _, ok := g.extNames[n]; !ok {
				return extErr("%s is read before it is declared", n)
			}
			if l, ok := g.extLoop[n]; ok && l != loop {
				return extErr("%s is read outside the loop it is declared in", n)
			}
		}
	}
	return nil
}

// scope is the Scope of one generation.
type scope struct {
	g      *gen
	locals map[string]bool // the per-row locals the nested block rebuilds
	fulls  map[string]bool // the categories' Full_ it rebuilds
	err    error           // an identifier ids gave that is not one
}

func (s *scope) Identifier(node string) (Expr, error) {
	g := s.g
	if _, ok := g.comps[node]; ok {
		return identExpr(g.ident[node]), nil
	}
	if owner, ok := strings.CutSuffix(node, ".correlation"); ok {
		if e, ok := g.effects[owner]; ok {
			return identExpr(e.corr), nil
		}
	}
	n := g.node(node)
	if n == nil || n.Kind == "likelihood" || n.Kind == "correlated_effects" {
		return Expr{}, extErr("%s has no identifier", strconv.Quote(node))
	}
	if p, err := g.place(n); err != nil {
		return Expr{}, err
	} else if p == placeLocal {
		return Expr{}, extErr("%s is a value per row: ask Local", strconv.Quote(node))
	}
	if n.Kind == "data" && n.Type == "time" {
		return Expr{}, extErr("%s is the time of a series, not declared", strconv.Quote(node))
	}
	return identExpr(g.ident[node]), nil
}

func identExpr(s string) Expr { return Expr{text: s, ident: s, root: s} }

func (s *scope) Local(node string) (Expr, error) {
	n := s.g.node(node)
	if n == nil {
		return Expr{}, extErr("%s is not a node", strconv.Quote(node))
	}
	if p, err := s.g.place(n); err != nil || p != placeLocal {
		return Expr{}, extErr("%s is not a value per row", strconv.Quote(node))
	}
	s.use(node, false)
	return identExpr(s.g.ident[node]), nil
}

// use asks for what an emitted value reads: a per-row local (and what it reads), a Full_.
func (s *scope) use(node string, full bool) {
	if full {
		s.fulls[node] = true
		return
	}
	n := s.g.node(node)
	if n == nil || s.locals[node] {
		return
	}
	if p, err := s.g.place(n); err != nil || p != placeLocal {
		return
	}
	s.locals[node] = true
	for _, r := range s.g.reads[node] {
		if _, ok := s.g.full[r]; ok {
			s.fulls[r] = true
		}
		s.use(r, false)
	}
}

// env is the EmitEnv of a value the extension asks for.
func (s *scope) env(ids func(node string) (Expr, bool)) emitEnv {
	env := emitEnv{g: s.g, use: s.use}
	if ids != nil {
		env.ids = func(node string) (string, bool) {
			e, ok := ids(node)
			if !ok {
				return "", false
			}
			if e.ident == "" && s.err == nil {
				s.err = extErr("ids gave %s for %s, not an identifier", strconv.Quote(e.text), strconv.Quote(node))
			}
			return e.ident, true
		}
	}
	return env
}

// valueExpr wraps an emitted value, with the locals it was split into (sink) and the extension's
// names it reads (from ids, the rows), in its text or in its locals.
func (s *scope) valueExpr(text string, sink *splitSink, err error) (Expr, error) {
	if err != nil {
		return Expr{}, err
	}
	if s.err != nil {
		return Expr{}, s.err
	}
	e := Expr{text: text, pre: sink.lines, entries: sink.entries}
	for _, t := range append([]string{text}, sink.lines...) {
		for _, w := range strings.FieldsFunc(t, func(r rune) bool { return r > 127 || !wordByte(byte(r)) }) {
			if extensionName(w) {
				e.ext = append(e.ext, w)
			}
		}
	}
	return e, nil
}

// splitEnv is the EmitEnv of a value the extension asks for, on frame's rows (nil: its
// statement's), with the sink its locals go to: new numbers after the node's others, declared in
// the extension's nested block. With ids, a value per row that reads a node ids replaces is
// rebuilt with ids in a local (inliner).
func (s *scope) splitEnv(ids func(node string) (Expr, bool), frame *rowsFrame) (emitEnv, *splitSink) {
	sink := &splitSink{block: blockGenerated, fresh: true}
	env := s.env(ids)
	env.sink = sink
	env.frame = frame
	if ids != nil {
		in := &inliner{s: s, ids: ids, done: map[string]string{}, reads: map[string]bool{}}
		env.inline = in.local
		in.env = env
	}
	return env, sink
}

// inliner rebuilds, for one value the extension asks for with ids, each value per row it reads
// that reads a node ids answers: in a local E_<id>_<k> on the value's rows, with ids, once. Any
// other node read that reads a node ids answers, and that ids does not answer, is refused: its
// identifier would be the posterior's value in a value of the prior's draws.
type inliner struct {
	s     *scope
	ids   func(node string) (Expr, bool)
	env   emitEnv
	done  map[string]string // the locals written, by node
	reads map[string]bool   // whether a node reads one ids answers, by node
}

// replaced reports whether node reads, directly or through what it reads, a node ids answers.
func (in *inliner) replaced(node string) bool {
	if r, ok := in.reads[node]; ok {
		return r
	}
	in.reads[node] = false // a cycle is not a model; never followed twice
	r := false
	for _, x := range in.s.g.reads[ownerOf(node)] {
		if _, ok := in.ids(x); ok || in.replaced(x) {
			r = true
			break
		}
	}
	in.reads[node] = r
	return r
}

// local answers Identifier for a value per row that reads a node ids replaces: the local holding it
// rebuilt with ids on the frame's rows (vector[N] E; E[rows] = …; read E[rows] by Emit).
func (in *inliner) local(id string) (string, bool) {
	g := in.s.g
	fail := func(err error) (string, bool) {
		if in.s.err == nil {
			in.s.err = err
		}
		return "", false
	}
	if !in.replaced(id) {
		return "", false
	}
	n := g.node(id)
	if n == nil || n.Plate == nil {
		return fail(extErr("%s reads a node ids answers: ids must answer it too", strconv.Quote(id)))
	}
	if p, err := g.place(n); err != nil || p != placeLocal {
		return fail(extErr("%s reads a node ids answers: ids must answer it too", strconv.Quote(id)))
	}
	if ident, ok := in.done[id]; ok {
		return ident, true
	}
	text, t, err := g.vectorValue(n, in.env)
	if err != nil {
		return fail(err)
	}
	size, err := g.vectorSize(bayesexpr.Plate(*n.Plate))
	if err != nil {
		return fail(err)
	}
	// a single value repeated, an array of integers made a vector, on as many rows as it is
	// written on
	if text, err = g.overPlate(text, t, *n.Plate, g.frameCount(in.env.frame, size)); err != nil {
		return fail(err)
	}
	ident, err := g.localName(in.env.sink, id+"\x00#rows", ownerOf(id), 0)
	if err != nil {
		return fail(err)
	}
	rows := ""
	if in.env.frame != nil {
		rows = in.env.frame.rows()
	}
	in.env.sink.declareLocal(ident, size, rows, text)
	in.done[id] = ident
	return ident, true
}

// frameCount is the number of rows a frame holds: the size of the extension's array of positions,
// N_lag or N_first for the statement's subset, all the plate's units (all) otherwise.
func (g *gen) frameCount(f *rowsFrame, all string) string {
	switch {
	case f == nil:
		return all
	case f.on != "":
		return g.extNames[f.on].Size.String()
	case f.base == identRowsLag:
		return identNLag
	case f.base == identRowsFirst:
		return identNFirst
	}
	return all
}

// rowsArray is the identifier of rows, an array of integers of the extension's data.
func (s *scope) rowsArray(rows Expr) (string, error) {
	d, ok := s.g.extNames[rows.ident]
	if rows.ident == "" || !ok || d.Kind == "" || d.Elem != Int || d.Shape != Array {
		return "", extErr("the rows %s are not an array of integers of the extension's data", strconv.Quote(rows.text))
	}
	return rows.ident, nil
}

func (s *scope) ValueWith(node string, ids func(node string) (Expr, bool)) (Expr, error) {
	return s.value(node, ids, Expr{}, false)
}

func (s *scope) ValueOn(node string, ids func(node string) (Expr, bool), rows Expr) (Expr, error) {
	return s.value(node, ids, rows, true)
}

func (s *scope) value(node string, ids func(node string) (Expr, bool), rows Expr, on bool) (Expr, error) {
	n := s.g.node(node)
	if n == nil || (n.Kind != "transformed" && n.Kind != "linear_predictor") {
		return Expr{}, extErr("%s is not a transformed node nor a linear predictor", strconv.Quote(node))
	}
	base := ""
	if s.g.lagged[node] {
		base = identRowsLag
	}
	var frame *rowsFrame
	switch {
	case on:
		if n.Plate == nil {
			return Expr{}, extErr("%s is a single value: it has no rows", strconv.Quote(node))
		}
		r, err := s.rowsArray(rows)
		if err != nil {
			return Expr{}, err
		}
		frame = &rowsFrame{base: base, on: r}
	case ids != nil && n.Plate != nil:
		frame = &rowsFrame{base: base}
	}
	env, sink := s.splitEnv(ids, frame)
	text, _, err := s.g.vectorValue(n, env)
	return s.valueExpr(text, sink, err)
}

func (s *scope) Slot(key bayesgraph.SlotKey, ids func(node string) (Expr, bool)) (Expr, error) {
	return s.slot(key, ids, Expr{}, false)
}

func (s *scope) SlotOn(key bayesgraph.SlotKey, ids func(node string) (Expr, bool), rows Expr) (Expr, error) {
	return s.slot(key, ids, rows, true)
}

func (s *scope) slot(key bayesgraph.SlotKey, ids func(node string) (Expr, bool), rows Expr, on bool) (Expr, error) {
	t, ok := s.g.m.Slots[key]
	if !ok {
		return Expr{}, extErr("%s.%s is not a slot of the model", key.Node, key.Slot)
	}
	var frame *rowsFrame
	switch {
	case on:
		if t.Context == "" {
			return Expr{}, extErr("%s.%s is a single value: it has no rows", key.Node, key.Slot)
		}
		r, err := s.rowsArray(rows)
		if err != nil {
			return Expr{}, err
		}
		frame = &rowsFrame{base: s.g.subsetOf(key), on: r}
	case ids != nil && t.Context != "":
		frame = &rowsFrame{base: s.g.subsetOf(key)}
	}
	env, sink := s.splitEnv(ids, frame)
	text, _, err := s.g.emitSlot(key, env)
	return s.valueExpr(text, sink, err)
}

func (s *scope) Call(name string, args ...Expr) (Expr, error) {
	texts := make([]string, len(args))
	out := Expr{}
	for i, a := range args {
		if !a.ok() {
			return Expr{}, extErr("the argument %d of %s is no value", i+1, strconv.Quote(name))
		}
		texts[i] = a.text
		out.ext = join(out.ext, a.ext)
		out.loop = out.loop || a.loop
		out.pre = join(out.pre, a.pre)
		out.entries = append(out.entries, a.entries...)
	}
	text, err := call(name, texts...)
	if err != nil {
		return Expr{}, extErr("%s is not a function the catalogue lists", strconv.Quote(name))
	}
	out.text = text
	return out, nil
}

func (s *scope) PlateSize(plate string) (Size, error) {
	sz, ok := s.g.size[plate]
	if !ok {
		return Size{}, extErr("%s is not a plate of the model", strconv.Quote(plate))
	}
	if p, ok := s.g.plates[plate]; ok && p.Kind == bayesgraph.PlateLevels {
		// a levels plate has K - 1 elements (bayes-contract, «Correction of 2026-11-24 from K0» 1)
		sz += " - 1"
	}
	return Size{text: sz, dim: bayescontract.Dim{Plate: plate}}, nil
}

func (s *scope) Levels(plate string) (Size, error) {
	sz, ok := s.g.size[plate]
	if p, isPlate := s.g.plates[plate]; !ok || !isPlate || p.Kind != bayesgraph.PlateLevels {
		return Size{}, extErr("%s is not the levels plate of a category", strconv.Quote(plate))
	}
	return Size{text: sz, dim: bayescontract.Dim{Size: sz}}, nil
}

func (s *scope) Rows(node string) (Size, error) {
	n := s.g.node(node)
	if n == nil {
		return Size{}, extErr("%s is not a node", strconv.Quote(node))
	}
	plate := ""
	switch n.Kind {
	case "likelihood":
		if obs := s.g.node(n.Observed); obs != nil && obs.Plate != nil {
			plate = *obs.Plate
		}
	case "transformed", "linear_predictor":
		if n.Plate != nil {
			plate = *n.Plate
		}
	}
	if plate != s.g.rows.ID {
		return Size{}, extErr("%s is not on the rows", strconv.Quote(node))
	}
	if s.g.lagged[node] {
		return Size{text: identNLag, dim: bayescontract.Dim{Size: identNLag}}, nil
	}
	return Size{text: identRows, dim: bayescontract.Dim{Plate: plate}}, nil
}

// writeLocals rebuilds in the nested block the Full_ and the per-row locals asked for, in Order,
// as the model block writes them.
func (s *scope) writeLocals() error {
	g := s.g
	for _, id := range g.m.Order {
		if s.fulls[id] {
			if err := g.writeFull(id); err != nil {
				return err
			}
		}
	}
	for _, id := range g.m.Order {
		if s.locals[id] {
			if err := g.writeVectorValue(g.node(id), emitEnv{g: g}, "", nil); err != nil {
				return err
			}
		}
	}
	return nil
}
