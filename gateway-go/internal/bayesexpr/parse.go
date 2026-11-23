// Package bayesexpr is the expression language of the Bayesian model editor
// (docs/specs/bayes-expression-language.md): what a person types for a value of the graph
// (a + b * x, inv_logit(alpha[school] + beta * dose), phi * lag(y, 1)), read by a closed grammar
// into a syntax tree. The gateway is its one authority; the browser's mirror only gives feedback.
//
// This file and lex.go are the reading: the checks before any token (length in bytes, ASCII,
// control bytes), the tokens, and a recursive descent whose every recursive step is guarded by
// max_expression_nesting and whose every node is counted against max_expression_nodes, so that
// no input exhausts the stack, the time or the memory. The parser stops at the first error.
package bayesexpr

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// lagName is the function the language treats apart: lag(x, k), x a bare name, k a whole number
// written in digits. The parser gives it its own node, so that a tree never holds a call to lag
// and Format(lag(y, 2.0)) cannot become lag(y, 2).
const lagName = "lag"

// lagApart reports that the catalogue declares lag apart with two arguments; otherwise lag would
// be read as any call (a test fails first).
var lagApart = func() bool {
	f, ok := bayescatalog.LookupFunction(lagName)
	return ok && f.Apart && f.Arguments == 2
}()

// Parse reads one expression. The error, when there is one, is an *Error: the first from the
// left.
func Parse(src string) (*Node, error) {
	n, _, err := parse(src, &catalogLimits)
	if err != nil {
		return nil, err
	}
	return n, nil
}

// stats are what a parse did, for the tests: the tokens read and the deepest nesting reached.
type stats struct {
	tokens   int
	maxDepth int
	nodes    int
}

func parse(src string, lim *bayescatalog.Limits) (*Node, stats, *Error) {
	if err := precheck(src, lim); err != nil {
		return nil, stats{}, err
	}
	p := &parser{lx: newLexer(src, lim), src: src, lim: lim}
	n, err := p.input()
	return n, stats{tokens: p.tokens, maxDepth: p.maxDepth, nodes: p.nodes}, err
}

type parser struct {
	lx  lexer
	src string
	lim *bayescatalog.Limits
	tok token

	depth    int
	maxDepth int
	nodes    int
	tokens   int
}

func (p *parser) input() (*Node, *Error) {
	if err := p.advance(); err != nil {
		return nil, err
	}
	if p.tok.kind == tEOF {
		return nil, &Error{Code: CodeEmpty, Pos: 0, Len: 0}
	}
	n, err := p.sum()
	if err != nil {
		return nil, err
	}
	if p.tok.kind != tEOF {
		return nil, p.unexpected(expectOperator)
	}
	return n, nil
}

func (p *parser) advance() *Error {
	t, err := p.lx.next()
	if err != nil {
		return err
	}
	p.tok = t
	p.tokens++
	return nil
}

func errAt(t token, code Code, args map[string]string) *Error {
	return &Error{Code: code, Pos: t.pos, Len: t.end - t.pos, Args: args}
}

func (p *parser) unexpected(expected string) *Error {
	return errAt(p.tok, CodeUnexpected, map[string]string{"expected": expected})
}

func unclosed(open token) *Error {
	return errAt(open, CodeUnclosed, map[string]string{"bracket": string(rune(openByte(open)))})
}

func openByte(t token) byte {
	if t.kind == tLBracket {
		return '['
	}
	return '('
}

// count is called at the token of every node the parser creates (a number, a name, a call, an
// indexing, a unary minus, a binary operator), before the node is read further.
func (p *parser) count(t token) *Error {
	if p.nodes >= p.lim.MaxExpressionNodes {
		return errAt(t, CodeTooBig, nil)
	}
	p.nodes++
	return nil
}

// enter is called before each recursive step (a unary minus's operand, a power's exponent,
// parentheses, a call's arguments, brackets), at the token that opens it: the depth is refused
// before the recursion starts.
func (p *parser) enter(t token) *Error {
	if p.depth >= p.lim.MaxExpressionNesting {
		return errAt(t, CodeTooDeep, nil)
	}
	p.depth++
	if p.depth > p.maxDepth {
		p.maxDepth = p.depth
	}
	return nil
}

func (p *parser) leave() { p.depth-- }

func binary(op token, left, right *Node) *Node {
	sym := "^"
	switch op.kind {
	case tPlus:
		sym = "+"
	case tMinus:
		sym = "-"
	case tStar:
		sym = "*"
	case tSlash:
		sym = "/"
	}
	return &Node{Kind: KindBinary, Op: sym, Args: []*Node{left, right}, Pos: left.Pos, Len: right.end() - left.Pos}
}

// sum = product {("+" | "-") product}: a loop, not a recursion.
func (p *parser) sum() (*Node, *Error) {
	left, err := p.product()
	if err != nil {
		return nil, err
	}
	for p.tok.kind == tPlus || p.tok.kind == tMinus {
		op := p.tok
		if err := p.count(op); err != nil {
			return nil, err
		}
		if err := p.advance(); err != nil {
			return nil, err
		}
		right, err := p.product()
		if err != nil {
			return nil, err
		}
		left = binary(op, left, right)
	}
	return left, nil
}

// product = signed {("*" | "/") signed}: a loop.
func (p *parser) product() (*Node, *Error) {
	left, err := p.signed()
	if err != nil {
		return nil, err
	}
	for p.tok.kind == tStar || p.tok.kind == tSlash {
		op := p.tok
		if err := p.count(op); err != nil {
			return nil, err
		}
		if err := p.advance(); err != nil {
			return nil, err
		}
		right, err := p.signed()
		if err != nil {
			return nil, err
		}
		left = binary(op, left, right)
	}
	return left, nil
}

// signed = "-" signed | power.
func (p *parser) signed() (*Node, *Error) {
	if p.tok.kind != tMinus {
		return p.power()
	}
	minus := p.tok
	if err := p.enter(minus); err != nil {
		return nil, err
	}
	defer p.leave()
	if err := p.count(minus); err != nil {
		return nil, err
	}
	if err := p.advance(); err != nil {
		return nil, err
	}
	x, err := p.signed()
	if err != nil {
		return nil, err
	}
	return &Node{Kind: KindNeg, Args: []*Node{x}, Pos: minus.pos, Len: x.end() - minus.pos}, nil
}

// power = atom ["^" signed]: right-associative through signed.
func (p *parser) power() (*Node, *Error) {
	base, err := p.atom()
	if err != nil {
		return nil, err
	}
	if p.tok.kind != tCaret {
		return base, nil
	}
	caret := p.tok
	if err := p.enter(caret); err != nil {
		return nil, err
	}
	defer p.leave()
	if err := p.count(caret); err != nil {
		return nil, err
	}
	if err := p.advance(); err != nil {
		return nil, err
	}
	exp, err := p.signed()
	if err != nil {
		return nil, err
	}
	return binary(caret, base, exp), nil
}

// atom = number | name "(" expression {"," expression} ")" | name "[" name "]" | name
// | "(" expression ")".
func (p *parser) atom() (*Node, *Error) {
	t := p.tok
	switch t.kind {
	case tNumber:
		if err := p.count(t); err != nil {
			return nil, err
		}
		if err := p.advance(); err != nil {
			return nil, err
		}
		return &Node{Kind: KindNum, Num: t.num, digits: t.digits, Pos: t.pos, Len: t.end - t.pos}, nil
	case tName:
		if err := p.count(t); err != nil {
			return nil, err
		}
		if err := p.advance(); err != nil {
			return nil, err
		}
		switch p.tok.kind {
		case tLParen:
			return p.call(t)
		case tLBracket:
			return p.index(t)
		}
		return &Node{Kind: KindName, Name: p.src[t.pos:t.end], Pos: t.pos, Len: t.end - t.pos}, nil
	case tLParen:
		if err := p.enter(t); err != nil {
			return nil, err
		}
		defer p.leave()
		if err := p.advance(); err != nil {
			return nil, err
		}
		inner, err := p.sum()
		if err != nil {
			return nil, err
		}
		switch p.tok.kind {
		case tRParen:
		case tEOF:
			return nil, unclosed(t)
		default:
			return nil, p.unexpected(expectParen)
		}
		closing := p.tok
		if err := p.advance(); err != nil {
			return nil, err
		}
		// The node's span takes in its parentheses.
		inner.Pos, inner.Len = t.pos, closing.end-t.pos
		return inner, nil
	}
	return nil, p.unexpected(expectValue)
}

// call reads "(" expression {"," expression} ")" after a name.
func (p *parser) call(name token) (*Node, *Error) {
	open := p.tok
	fn := p.src[name.pos:name.end]
	if err := p.enter(open); err != nil {
		return nil, err
	}
	defer p.leave()
	if err := p.advance(); err != nil {
		return nil, err
	}
	if p.tok.kind == tRParen {
		return nil, &Error{Code: CodeCallEmpty, Pos: name.pos, Len: p.tok.end - name.pos, Args: map[string]string{"name": fn}}
	}
	var args []*Node
	for {
		arg, err := p.sum()
		if err != nil {
			return nil, err
		}
		args = append(args, arg)
		switch p.tok.kind {
		case tComma:
			if err := p.advance(); err != nil {
				return nil, err
			}
			continue
		case tRParen:
		case tEOF:
			return nil, unclosed(open)
		default:
			return nil, p.unexpected(expectArgument)
		}
		break
	}
	closing := p.tok
	if err := p.advance(); err != nil {
		return nil, err
	}
	n := &Node{Kind: KindCall, Name: fn, Args: args, Pos: name.pos, Len: closing.end - name.pos}
	if fn == lagName && lagApart {
		return lag(n)
	}
	return n, nil
}

// lag turns lag(x, k) into its node: x a bare name, k a literal written in digits only (the one
// integer literal of the language). Whether k is in 1…max_lag and x a quantity of a series is the
// type checker's.
func lag(call *Node) (*Node, *Error) {
	span := func(n *Node) *Error { return &Error{Code: CodeLagArgument, Pos: n.Pos, Len: n.Len} }
	if len(call.Args) != 2 {
		return nil, span(call)
	}
	x, k := call.Args[0], call.Args[1]
	if x.Kind != KindName {
		return nil, span(x)
	}
	if k.Kind != KindNum || !k.digits {
		return nil, span(k)
	}
	// k is at most max_abs_literal (1e15): exact in a double and in an int64.
	return &Node{Kind: KindLag, Name: x.Name, K: int64(k.Num), Pos: call.Pos, Len: call.Len}, nil
}

// index reads "[" name "]" after a name.
func (p *parser) index(name token) (*Node, *Error) {
	open := p.tok
	if err := p.enter(open); err != nil {
		return nil, err
	}
	defer p.leave()
	if err := p.advance(); err != nil {
		return nil, err
	}
	by := p.tok
	switch by.kind {
	case tName:
	case tEOF:
		return nil, unclosed(open)
	default:
		return nil, p.unexpected(expectIndexName)
	}
	if err := p.advance(); err != nil {
		return nil, err
	}
	switch p.tok.kind {
	case tRBracket:
	case tEOF:
		return nil, unclosed(open)
	default:
		return nil, p.unexpected(expectBracket)
	}
	closing := p.tok
	if err := p.advance(); err != nil {
		return nil, err
	}
	return &Node{Kind: KindIndex, Name: p.src[name.pos:name.end], By: p.src[by.pos:by.end],
		Pos: name.pos, Len: closing.end - name.pos}, nil
}
