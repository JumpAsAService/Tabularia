package bayesexpr

import (
	"bytes"
	"encoding/json"
	"strconv"
	"strings"
)

// Kind is the kind of a node of the syntax tree.
type Kind uint8

const (
	KindNum    Kind = iota + 1 // a literal: Num, finite, >= 0, at most max_abs_literal
	KindName                   // a node of the graph: Name
	KindIndex                  // Name[By]: a node indexed by a group index
	KindCall                   // Name(Args...): a function of the catalogue
	KindLag                    // lag(Name, K)
	KindNeg                    // -Args[0]
	KindBinary                 // Args[0] Op Args[1], Op one of + - * / ^
)

// Node is a node of the syntax tree. Pos and Len are the span of its text in the input, in code
// points, parentheses around it included; they are not part of its JSON form and not compared.
type Node struct {
	Kind Kind
	Op   string
	Name string
	By   string
	K    int64
	Num  float64
	Args []*Node
	Pos  int
	Len  int

	// digits: a literal written with digits only (lag's k is one).
	digits bool
	// at is the node's own span, without the parentheses around it (zero when it has none);
	// sub are the spans of an indexing's two names (x, g) and of a lag's name and number, for
	// the type checker's errors. None of them is part of the JSON form.
	at  span
	sub [2]span
}

// span is a stretch of the input, in code points.
type span struct{ pos, len int }

// end is the offset just after the node's span.
func (n *Node) end() int { return n.Pos + n.Len }

// own is the node's span without the parentheses around it.
func (n *Node) own() span {
	if n.at.len == 0 {
		return span{n.Pos, n.Len}
	}
	return n.at
}

// formatNumber is a literal's canonical text: the shortest that reads back to the same double.
func formatNumber(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }

// MarshalJSON writes the node's JSON form, the same in Go and TypeScript:
//
//	{"num":"0.5"} {"name":"a"} {"index":"mu_school","by":"school"} {"call":"exp","args":[...]}
//	{"lag":"y","k":2} {"neg":{...}} {"op":"+","args":[{...},{...}]}
func (n *Node) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	if err := n.writeJSON(&b); err != nil {
		return nil, err
	}
	return b.Bytes(), nil
}

func (n *Node) writeJSON(b *bytes.Buffer) error {
	str := func(key, s string) error {
		b.WriteString(`"` + key + `":`)
		q, err := json.Marshal(s)
		b.Write(q)
		return err
	}
	args := func() error {
		b.WriteString(`"args":[`)
		for i, a := range n.Args {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := a.writeJSON(b); err != nil {
				return err
			}
		}
		b.WriteByte(']')
		return nil
	}
	var err error
	b.WriteByte('{')
	switch n.Kind {
	case KindNum:
		err = str("num", formatNumber(n.Num))
	case KindName:
		err = str("name", n.Name)
	case KindIndex:
		if err = str("index", n.Name); err == nil {
			b.WriteByte(',')
			err = str("by", n.By)
		}
	case KindCall:
		if err = str("call", n.Name); err == nil {
			b.WriteByte(',')
			err = args()
		}
	case KindLag:
		if err = str("lag", n.Name); err == nil {
			b.WriteString(`,"k":` + strconv.FormatInt(n.K, 10))
		}
	case KindNeg:
		b.WriteString(`"neg":`)
		err = n.Args[0].writeJSON(b)
	case KindBinary:
		if err = str("op", n.Op); err == nil {
			b.WriteByte(',')
			err = args()
		}
	}
	b.WriteByte('}')
	return err
}

// The levels of the grammar, from the loosest: a node of a level fits where that level or a
// looser one is expected, and is put in parentheses elsewhere.
const (
	levelSum = iota
	levelProduct
	levelSigned
	levelPower
	levelAtom
)

func level(n *Node) int {
	switch n.Kind {
	case KindBinary:
		switch n.Op {
		case "+", "-":
			return levelSum
		case "*", "/":
			return levelProduct
		}
		return levelPower
	case KindNeg:
		return levelSigned
	}
	return levelAtom
}

// Format is the canonical text of a tree: minimal parentheses by the precedence of the grammar
// (^ right, unary -, * / left, + - left), one space around each binary operator, none after a
// unary minus, numbers in their shortest form. Parse(Format(t)) is t.
//
// The text can be longer than the person's (1e5 is 100000, a+b is a + b): it is for reading, and
// may exceed max_expression_bytes.
func Format(n *Node) string {
	var b strings.Builder
	format(&b, n, levelSum)
	return b.String()
}

func format(b *strings.Builder, n *Node, min int) {
	if level(n) < min {
		b.WriteByte('(')
		format(b, n, levelSum)
		b.WriteByte(')')
		return
	}
	switch n.Kind {
	case KindNum:
		b.WriteString(formatNumber(n.Num))
	case KindName:
		b.WriteString(n.Name)
	case KindIndex:
		b.WriteString(n.Name + "[" + n.By + "]")
	case KindLag:
		b.WriteString(lagName + "(" + n.Name + ", " + strconv.FormatInt(n.K, 10) + ")")
	case KindCall:
		b.WriteString(n.Name + "(")
		for i, a := range n.Args {
			if i > 0 {
				b.WriteString(", ")
			}
			format(b, a, levelSum)
		}
		b.WriteByte(')')
	case KindNeg:
		b.WriteByte('-')
		format(b, n.Args[0], levelSigned)
	case KindBinary:
		// The left operand of a left-associative operator may be of its level, the right one
		// must be tighter; ^ is right-associative and its base is an atom.
		left, right := levelSum, levelProduct
		switch n.Op {
		case "*", "/":
			left, right = levelProduct, levelSigned
		case "^":
			left, right = levelAtom, levelSigned
		}
		format(b, n.Args[0], left)
		b.WriteString(" " + n.Op + " ")
		format(b, n.Args[1], right)
	}
}
