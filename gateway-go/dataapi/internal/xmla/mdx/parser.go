package mdx

import (
	"strconv"
	"strings"
)

// Parse reads one MDX statement. On refusal the error is an *Error.
func Parse(src string) (stmt *Statement, err error) {
	defer func() {
		if r := recover(); r != nil {
			// Only our own refusals are recovered: any other panic is a bug that
			// tests and the fuzzer must see.
			e, ok := r.(*Error)
			if !ok {
				panic(r)
			}
			stmt, err = nil, e
		}
	}()
	if len(src) > MaxStatementBytes {
		panic(newError(src, CodeTooLong, "", 0, "the statement is longer than %d bytes", MaxStatementBytes))
	}
	p := &parser{src: src, lx: lexer{src: src}}
	return p.parseTop(), nil
}

type parser struct {
	src   string
	lx    lexer
	buf   []token
	depth int
	// axisOff keeps where each axis keyword was, for the axis checks.
	axisOff map[*Axis]int
}

func (p *parser) fail(code, name string, off int, format string, args ...any) {
	panic(newError(p.src, code, name, off, format, args...))
}

func (p *parser) peek(i int) token {
	for len(p.buf) <= i {
		p.buf = append(p.buf, p.lx.next())
	}
	return p.buf[i]
}

func (p *parser) next() token {
	t := p.peek(0)
	if t.kind != tEOF {
		p.buf = p.buf[1:]
	}
	return t
}

func (p *parser) enter() {
	p.depth++
	if p.depth > MaxDepth {
		p.fail(CodeTooDeep, "", p.peek(0).off, "the statement nests deeper than %d levels", MaxDepth)
	}
}

func (p *parser) leave() { p.depth-- }

func (p *parser) unexpected(want string) {
	t := p.peek(0)
	p.fail(CodeSyntax, "", t.off, "expected %s but found %s", want, t.describe())
}

func (p *parser) expectPunct(s string) token {
	if !p.peek(0).is(s) {
		p.unexpected("\"" + s + "\"")
	}
	return p.next()
}

func (p *parser) expectKw(w string) token {
	if !p.peek(0).kw(w) {
		p.unexpected(w)
	}
	return p.next()
}

// parseTop refuses by name the statements that are not a SELECT, then parses it.
func (p *parser) parseTop() *Statement {
	t := p.peek(0)
	switch {
	case t.kind == tEOF:
		p.fail(CodeEmpty, "", t.off, "the statement is empty")
	case t.kw("WITH"):
		n := p.peek(1)
		switch {
		case n.kw("MEMBER"):
			p.fail(CodeWithMember, "WITH MEMBER", t.off, "WITH MEMBER (a calculated member) is not understood")
		case n.kw("SET"):
			p.fail(CodeWithSet, "WITH SET", t.off, "WITH SET (a named set) is not understood")
		}
		name := "WITH"
		if n.kind == tIdent {
			name += " " + strings.ToUpper(n.text)
		}
		p.fail(CodeWithOther, name, t.off, "%s is not understood", name)
	case t.kw("CREATE"):
		name := "CREATE"
		if n := p.peek(1); n.kind == tIdent {
			name += " " + strings.ToUpper(n.text)
		}
		p.fail(CodeCreate, name, t.off, "%s is not understood", name)
	case t.kw("DRILLTHROUGH"):
		p.fail(CodeDrillthrough, "DRILLTHROUGH", t.off, "DRILLTHROUGH is not understood")
	case t.kw("CALL"):
		p.fail(CodeCall, "CALL", t.off, "CALL is not understood")
	case !t.kw("SELECT"):
		if t.kind == tIdent {
			name := strings.ToUpper(t.text)
			p.fail(CodeUnsupportedStatement, name, t.off, "the statement %s is not understood, only SELECT is", name)
		}
		p.unexpected("SELECT")
	}
	st := p.parseSelect(true)
	if t := p.peek(0); t.kind != tEOF {
		p.fail(CodeSyntax, "", t.off, "unexpected %s after the end of the statement", t.describe())
	}
	return st
}

func (p *parser) parseSelect(top bool) *Statement {
	p.expectKw("SELECT")
	st := &Statement{}
	if p.peek(0).kind != tEOF && !p.peek(0).kw("FROM") {
		for {
			st.Axes = append(st.Axes, p.parseAxis())
			if p.peek(0).is(",") {
				p.next()
				if p.peek(0).kw("FROM") {
					p.fail(CodeSyntax, "", p.peek(0).off, "an axis was expected after the comma")
				}
				continue
			}
			break
		}
	}
	if !p.peek(0).kw("FROM") {
		p.fail(CodeNoFrom, "", p.peek(0).off, "SELECT without a FROM clause")
	}
	p.checkAxes(st)
	p.next()

	if p.peek(0).is("(") && p.peek(1).kw("SELECT") {
		p.next()
		p.enter()
		st.From.Sub = p.parseSelect(false)
		p.leave()
		p.expectPunct(")")
	} else {
		st.From.Cube = p.parsePath("a cube name")
	}
	if p.peek(0).kw("WHERE") {
		p.next()
		off := p.peek(0).off
		st.Slicer = p.parseExpr()
		p.requireSet(st.Slicer, off)
	}
	if top && p.peek(0).kw("CELL") {
		p.next()
		p.expectKw("PROPERTIES")
		st.Cells = p.parseProps()
	}
	return st
}

// checkAxes refuses a repeated axis. Holes in the numbering (ROWS without
// COLUMNS) are not refused here: whether they mean anything is for the planner.
func (p *parser) checkAxes(st *Statement) {
	seen := map[int]bool{}
	for _, a := range st.Axes {
		if seen[a.Index] {
			p.fail(CodeAxisDuplicate, AxisName(a.Index), p.axisOff[a], "axis %s is written twice", AxisName(a.Index))
		}
		seen[a.Index] = true
	}
}

func (p *parser) parseAxis() *Axis {
	a := &Axis{}
	if p.peek(0).kw("NON") && p.peek(1).kw("EMPTY") {
		p.next()
		p.next()
		a.NonEmpty = true
	}
	off := p.peek(0).off
	a.Set = p.parseExpr()
	p.requireSet(a.Set, off)
	if p.peek(0).kw("DIMENSION") {
		p.next()
		p.expectKw("PROPERTIES")
		a.DimProps = p.parseProps()
	} else if p.peek(0).kw("PROPERTIES") {
		p.next()
		a.DimProps = p.parseProps()
	}
	p.expectKw("ON")
	at := p.peek(0).off
	a.Index = p.parseAxisSpec()
	if p.axisOff == nil {
		p.axisOff = map[*Axis]int{}
	}
	p.axisOff[a] = at
	return a
}

func (p *parser) parseAxisSpec() int {
	t := p.peek(0)
	switch {
	case t.kind == tIdent:
		switch strings.ToUpper(t.text) {
		case "COLUMNS":
			p.next()
			return 0
		case "ROWS":
			p.next()
			return 1
		case "PAGES":
			p.next()
			return 2
		case "CHAPTERS":
			p.next()
			return 3
		case "SECTIONS":
			p.next()
			return 4
		case "AXIS":
			p.next()
			p.expectPunct("(")
			n := p.parseAxisNumber()
			p.expectPunct(")")
			return n
		}
	case t.kind == tNumber:
		return p.parseAxisNumber()
	}
	p.unexpected("an axis (COLUMNS, ROWS, PAGES or a number)")
	return 0
}

func (p *parser) parseAxisNumber() int {
	t := p.peek(0)
	if t.kind != tNumber {
		p.unexpected("an axis number")
	}
	n, err := strconv.Atoi(t.text)
	if err != nil || n < 0 || n >= MaxAxes {
		p.fail(CodeSyntax, "", t.off, "axis number must be an integer from 0 to %d", MaxAxes-1)
	}
	p.next()
	return n
}

// parsePath reads name{.name}: a cube, or a property.
func (p *parser) parsePath(what string) []Segment {
	var segs []Segment
	for {
		t := p.peek(0)
		switch t.kind {
		case tBracket:
			segs = append(segs, Segment{Name: t.text, Bracketed: true})
		case tIdent:
			segs = append(segs, Segment{Name: t.text})
		default:
			p.unexpected(what)
		}
		p.next()
		if !p.peek(0).is(".") {
			return segs
		}
		p.next()
	}
}

func (p *parser) parseProps() []Property {
	var out []Property
	for {
		out = append(out, Property(p.parsePath("a property name")))
		if !p.peek(0).is(",") {
			return out
		}
		p.next()
	}
}

// requireSet refuses a number or a keyword where a set or member must be.
func (p *parser) requireSet(e Expr, off int) {
	switch n := e.(type) {
	case *SetLit, *Paren, *Ref, *Call:
	case *Number:
		p.fail(CodeUnexpectedLiteral, n.Text, off, "the number %s is not a set or a member", n.Text)
	case *Flag:
		p.fail(CodeUnexpectedLiteral, n.Name, off, "%s is not a set or a member", n.Name)
	default:
		p.fail(CodeSyntax, "", off, "a set or member was expected here")
	}
}

// parseExpr reads term { "*" term }. A chain of * is a CrossJoin; anything
// that makes it arithmetic is refused.
func (p *parser) parseExpr() Expr {
	first := p.parseTerm()
	if !p.peek(0).is("*") {
		p.rejectOperators()
		return first
	}
	items := []Expr{first}
	starOff := p.peek(0).off
	for p.peek(0).is("*") {
		p.next()
		items = append(items, p.parseTerm())
	}
	measures := 0
	for _, it := range items {
		switch n := it.(type) {
		case *Number:
			p.fail(CodeArithmetic, "*", starOff, "arithmetic with * is not understood")
		case *Ref:
			if n.Suffix == "" && isMeasure(n) {
				measures++
			}
		}
	}
	if measures > 1 {
		p.fail(CodeArithmetic, "*", starOff, "arithmetic between measures is not understood")
	}
	p.rejectOperators()
	return &Call{Name: "CrossJoin", Args: items}
}

// rejectOperators is called after a complete operand.
func (p *parser) rejectOperators() {
	t := p.peek(0)
	switch {
	case t.is("+"), t.is("-"), t.is("/"):
		p.fail(CodeArithmetic, t.text, t.off, "arithmetic with %s is not understood", t.text)
	case t.is(":"):
		p.fail(CodeRange, ":", t.off, "a range (a:b) is not understood")
	}
}

func isMeasure(r *Ref) bool {
	return len(r.Segments) > 0 && strings.EqualFold(r.Segments[0].Name, "Measures") && len(r.Segments[0].Keys) == 0
}

func (p *parser) parseTerm() Expr {
	t := p.peek(0)
	switch t.kind {
	case tPunct:
		switch t.text {
		case "{":
			p.next()
			p.enter()
			set := &SetLit{}
			if !p.peek(0).is("}") {
				set.Items = p.parseItems()
			}
			p.expectPunct("}")
			p.leave()
			return set
		case "(":
			p.next()
			p.enter()
			par := &Paren{Items: p.parseItems()}
			p.expectPunct(")")
			p.leave()
			return par
		case "-":
			if n := p.peek(1); n.kind == tNumber && n.off == t.end {
				p.next()
				p.next()
				return p.number("-"+n.text, n.off)
			}
		}
	case tNumber:
		p.next()
		return p.number(t.text, t.off)
	case tString:
		p.fail(CodeUnexpectedLiteral, "", t.off, "a string is not a set or a member")
	case tBracket:
		return p.parseRef()
	case tIdent:
		if p.peek(1).is("(") {
			return p.parseCall()
		}
		return p.parseRef()
	}
	p.unexpected("a set, tuple or member")
	return nil
}

func (p *parser) number(text string, off int) *Number {
	v, err := strconv.ParseFloat(text, 64)
	if err != nil {
		p.fail(CodeSyntax, "", off, "the number %s is out of range", text)
	}
	return &Number{Text: text, Value: v}
}

// parseItems reads expr {, expr} inside { } or ( ).
func (p *parser) parseItems() []Expr {
	var items []Expr
	for {
		off := p.peek(0).off
		e := p.parseExpr()
		p.requireSet(e, off)
		items = append(items, e)
		if !p.peek(0).is(",") {
			return items
		}
		p.next()
	}
}

var memberSuffixes = map[string]string{
	"members": "Members", "allmembers": "AllMembers",
	"children": "Children", "defaultmember": "DefaultMember",
}

// memberFunctions are member properties a pivot may send but the subset does
// not cover; they are named in the refusal instead of being read as a name.
var memberFunctions = map[string]bool{
	"currentmember": true, "parent": true, "prevmember": true, "nextmember": true,
	"firstchild": true, "lastchild": true, "lead": true, "lag": true, "level": true,
	"hierarchy": true, "dimension": true, "properties": true, "name": true,
	"uniquename": true, "caption": true, "value": true, "siblings": true,
	"descendants": true, "ancestors": true, "item": true, "count": true,
	"ordinal": true, "membervalue": true, "member_caption": true,
	"firstsibling": true, "lastsibling": true, "datamember": true,
}

func (p *parser) parseRef() Expr {
	t := p.next()
	ref := &Ref{}
	if t.kind == tBracket {
		ref.Segments = append(ref.Segments, Segment{Name: t.text, Bracketed: true})
	} else {
		ref.Segments = append(ref.Segments, Segment{Name: t.text})
	}
	for p.peek(0).is(".") {
		dot := p.next()
		n := p.peek(0)
		switch {
		case ref.Suffix != "":
			name := n.text
			if n.kind != tIdent {
				name = ""
			}
			p.fail(CodeUnsupportedMember, name, dot.off, "a member function after .%s is not understood", ref.Suffix)
		case n.kind == tBracket:
			p.next()
			ref.Segments = append(ref.Segments, Segment{Name: n.text, Bracketed: true})
		case n.is("&"):
			var keys []string
			for p.peek(0).is("&") {
				p.next()
				k := p.peek(0)
				if k.kind != tBracket {
					p.unexpected("[key] after &")
				}
				p.next()
				keys = append(keys, k.text)
			}
			ref.Segments = append(ref.Segments, Segment{Keys: keys})
		case n.kind == tIdent:
			low := strings.ToLower(n.text)
			if p.peek(1).is("(") {
				p.fail(CodeUnknownFunction, n.text, n.off, "the function .%s() is not understood", n.text)
			}
			p.next()
			if s, ok := memberSuffixes[low]; ok {
				ref.Suffix = s
			} else if memberFunctions[low] {
				p.fail(CodeUnsupportedMember, n.text, n.off, "the member property .%s is not understood", n.text)
			} else {
				ref.Segments = append(ref.Segments, Segment{Name: n.text})
			}
		default:
			p.unexpected("a name after \".\"")
		}
	}
	return ref
}

var flagWords = map[string]bool{
	"POST": true, "PRE": true, "INCLUDE_CALC_MEMBERS": true, "RECURSIVE": true,
	"ALL": true, "ASC": true, "DESC": true, "BASC": true, "BDESC": true,
}

// parseCall reads Name(args). Arguments may be skipped between commas, but not
// first and not last: f(,a) and f(a,) are errors.
func (p *parser) parseCall() Expr {
	nameTok := p.next()
	canon, ok := functionNames[strings.ToLower(nameTok.text)]
	if !ok {
		p.fail(CodeUnknownFunction, nameTok.text, nameTok.off, "the function %s is not understood", nameTok.text)
	}
	p.expectPunct("(")
	p.enter()
	call := &Call{Name: canon}
	var starts []int
	if !p.peek(0).is(")") {
		for {
			t := p.peek(0)
			starts = append(starts, t.off)
			switch {
			case t.is(",") || t.is(")"):
				if len(call.Args) == 0 {
					p.fail(CodeSyntax, "", t.off, "a missing first argument in %s", canon)
				}
				call.Args = append(call.Args, &Empty{})
			case canon == "Filter" && len(call.Args) == 1:
				call.Args = append(call.Args, p.parseCond())
			case t.kind == tIdent && flagWords[strings.ToUpper(t.text)] &&
				(p.peek(1).is(",") || p.peek(1).is(")")):
				p.next()
				call.Args = append(call.Args, &Flag{Name: strings.ToUpper(t.text)})
			default:
				call.Args = append(call.Args, p.parseExpr())
			}
			if p.peek(0).is(",") {
				p.next()
				continue
			}
			break
		}
		if _, empty := call.Args[len(call.Args)-1].(*Empty); empty {
			p.fail(CodeSyntax, "", p.peek(0).off, "an extra comma at the end of the arguments of %s", canon)
		}
	}
	p.expectPunct(")")
	p.leave()
	p.validateCall(call, nameTok.off, starts)
	return call
}

// parseCond reads a Filter condition: measure <op> number joined by AND / OR.
func (p *parser) parseCond() Expr {
	p.enter()
	defer p.leave()
	return p.parseLogic("OR", func() Expr { return p.parseLogic("AND", p.parseCmp) })
}

func (p *parser) parseLogic(op string, operand func() Expr) Expr {
	first := operand()
	if !p.peek(0).kw(op) {
		return first
	}
	l := &Logic{Op: op}
	add := func(e Expr) {
		if sub, ok := e.(*Logic); ok && sub.Op == op {
			l.Terms = append(l.Terms, sub.Terms...)
		} else {
			l.Terms = append(l.Terms, e)
		}
	}
	add(first)
	for p.peek(0).kw(op) {
		p.next()
		add(operand())
	}
	return l
}

func (p *parser) parseCmp() Expr {
	t := p.peek(0)
	if t.is("(") {
		p.next()
		e := p.parseCond()
		p.expectPunct(")")
		return e
	}
	if t.kw("NOT") {
		p.fail(CodeFilterCondition, "NOT", t.off, "NOT is not understood in a Filter condition")
	}
	if t.kind != tBracket && t.kind != tIdent {
		p.fail(CodeFilterCondition, "", t.off, "expected a measure in the Filter condition, found %s", t.describe())
	}
	left, ok := p.parseTerm().(*Ref)
	if !ok || left.Suffix != "" || !isMeasure(left) {
		p.fail(CodeFilterCondition, "", t.off, "the left side of a Filter comparison must be a measure")
	}
	p.rejectOperators()
	opTok := p.peek(0)
	switch {
	case opTok.is("="), opTok.is("<>"), opTok.is("<"), opTok.is("<="), opTok.is(">"), opTok.is(">="):
		p.next()
	default:
		p.fail(CodeFilterCondition, "", opTok.off, "expected a comparison (=, <>, <, <=, >, >=) after the measure, found %s", opTok.describe())
	}
	n := p.peek(0)
	var num *Number
	switch {
	case n.kind == tNumber:
		p.next()
		num = p.number(n.text, n.off)
	case n.is("-") && p.peek(1).kind == tNumber && p.peek(1).off == n.end:
		p.next()
		d := p.next()
		num = p.number("-"+d.text, d.off)
	default:
		p.fail(CodeFilterCondition, "", n.off, "the right side of a Filter comparison must be a number")
	}
	p.rejectOperators()
	if s := p.peek(0); s.is("*") {
		p.fail(CodeArithmetic, "*", s.off, "arithmetic with * is not understood")
	}
	return &Compare{Left: left, Op: opTok.text, Right: num}
}
