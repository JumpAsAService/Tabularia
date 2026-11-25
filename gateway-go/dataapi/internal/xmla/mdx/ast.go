// Package mdx parses the subset of MDX that a spreadsheet pivot table writes
// into a tree, and refuses everything else naming the construct it did not
// understand. It does not resolve names against a cube and knows no SQL: the
// tree keeps names exactly as written (case included) for the planner to
// resolve.
package mdx

// Limits that keep hostile text cheap to refuse.
const (
	// MaxStatementBytes is far above anything a pivot writes (a few KB).
	MaxStatementBytes = 1 << 20
	// MaxDepth bounds the nesting of sets, tuples, calls and subselects, so
	// the recursive descent (and every walk of the tree) has a fixed stack.
	MaxDepth = 64
	// MaxAxes bounds axis numbers: ON 127 is the last one.
	MaxAxes = 128
)

// Statement is one SELECT. The tree carries no source positions, so two
// parses of equivalent text compare equal with reflect.DeepEqual.
type Statement struct {
	Axes   []*Axis // in the order written; Index says which axis each is
	From   Source
	Slicer Expr       // the WHERE clause, nil when absent
	Cells  []Property // CELL PROPERTIES, top-level statement only
}

// Axis returns the axis with the given number (0 columns, 1 rows, 2 pages).
func (s *Statement) Axis(index int) *Axis {
	for _, a := range s.Axes {
		if a.Index == index {
			return a
		}
	}
	return nil
}

// Axis is one "set ON n" part.
type Axis struct {
	Index    int  // 0 COLUMNS, 1 ROWS, 2 PAGES, 3 CHAPTERS, 4 SECTIONS, or the number
	NonEmpty bool // NON EMPTY
	Set      Expr
	DimProps []Property // DIMENSION PROPERTIES
}

// Source is the FROM clause: a cube by name, or a nested SELECT.
type Source struct {
	Cube []Segment  // [cube] or [catalog].[cube]
	Sub  *Statement // non-nil for FROM (SELECT ...)
}

// Property is a (possibly dotted) property name: PARENT_UNIQUE_NAME, VALUE,
// [Level].[Name]. Compare case-insensitively.
type Property []Segment

// Name joins the parts with dots, without brackets.
func (p Property) Name() string {
	s := ""
	for i, seg := range p {
		if i > 0 {
			s += "."
		}
		s += seg.Name
	}
	return s
}

// Segment is one dotted part of a name. A key segment (&[a]&[b]) has Keys and
// no Name. Bracketed records how it was written, to print it back the same.
type Segment struct {
	Name      string
	Bracketed bool
	Keys      []string
}

// Expr is a node of a set, tuple, member or condition expression.
type Expr interface{ isExpr() }

// SetLit is {a, b, ...}.
type SetLit struct{ Items []Expr }

// Paren is (a, b, ...): a tuple, or a parenthesised set or member.
type Paren struct{ Items []Expr }

// Ref is a member, level, hierarchy or measure by name, with an optional
// trailing member function.
type Ref struct {
	Segments []Segment
	// Suffix is "", "Members", "AllMembers", "Children" or "DefaultMember"
	// (canonical case). ".[All]" is an ordinary segment named All.
	Suffix string
}

// Call is a function. Name is canonical (Hierarchize, DrilldownLevel, ...).
// "a * b" arrives as CrossJoin with the operands as Args. Args may hold *Empty
// (skipped optional argument) and *Flag.
type Call struct {
	Name string
	Args []Expr
}

// Empty is an argument left out between commas: DrilldownLevel(s,,,FLAG).
type Empty struct{}

// Flag is a bare keyword argument, upper-cased: POST, INCLUDE_CALC_MEMBERS,
// RECURSIVE, ALL, ASC, DESC, BASC, BDESC.
type Flag struct{ Name string }

// Number is a numeric literal; Text is as written (with a leading "-" for a
// negative one).
type Number struct {
	Text  string
	Value float64
}

// Compare is a measure against a number in a Filter. Op is one of
// = <> < <= > >=.
type Compare struct {
	Left  *Ref
	Op    string
	Right *Number
}

// Logic joins conditions with AND or OR (Op, upper case). Nested groups of the
// same operator are flattened.
type Logic struct {
	Op    string
	Terms []Expr
}

func (*SetLit) isExpr()  {}
func (*Paren) isExpr()   {}
func (*Ref) isExpr()     {}
func (*Call) isExpr()    {}
func (*Empty) isExpr()   {}
func (*Flag) isExpr()    {}
func (*Number) isExpr()  {}
func (*Compare) isExpr() {}
func (*Logic) isExpr()   {}
