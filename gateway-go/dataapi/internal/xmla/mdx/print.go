package mdx

import (
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

// String prints the statement as canonical single-line MDX. Parsing the output
// gives back an equal tree.
func (s *Statement) String() string {
	var b strings.Builder
	writeStatement(&b, s)
	return b.String()
}

// ExprString prints one expression as canonical MDX.
func ExprString(e Expr) string {
	var b strings.Builder
	writeExpr(&b, e)
	return b.String()
}

func writeStatement(b *strings.Builder, s *Statement) {
	b.WriteString("SELECT")
	for i, a := range s.Axes {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteByte(' ')
		if a.NonEmpty {
			b.WriteString("NON EMPTY ")
		}
		writeExpr(b, a.Set)
		if len(a.DimProps) > 0 {
			b.WriteString(" DIMENSION PROPERTIES ")
			writeProps(b, a.DimProps)
		}
		b.WriteString(" ON ")
		b.WriteString(AxisName(a.Index))
	}
	b.WriteString(" FROM ")
	if s.From.Sub != nil {
		b.WriteByte('(')
		writeStatement(b, s.From.Sub)
		b.WriteByte(')')
	} else {
		writeSegments(b, s.From.Cube)
	}
	if s.Slicer != nil {
		b.WriteString(" WHERE ")
		writeExpr(b, s.Slicer)
	}
	if len(s.Cells) > 0 {
		b.WriteString(" CELL PROPERTIES ")
		writeProps(b, s.Cells)
	}
}

// AxisName is the keyword for an axis number, or AXIS(n).
func AxisName(i int) string {
	switch i {
	case 0:
		return "COLUMNS"
	case 1:
		return "ROWS"
	case 2:
		return "PAGES"
	case 3:
		return "CHAPTERS"
	case 4:
		return "SECTIONS"
	}
	return "AXIS(" + strconv.Itoa(i) + ")"
}

func writeProps(b *strings.Builder, ps []Property) {
	for i, p := range ps {
		if i > 0 {
			b.WriteString(", ")
		}
		writeSegments(b, p)
	}
}

func writeSegments(b *strings.Builder, segs []Segment) {
	for i, s := range segs {
		if i > 0 {
			b.WriteByte('.')
		}
		writeSegment(b, s)
	}
}

func writeSegment(b *strings.Builder, s Segment) {
	if len(s.Keys) > 0 {
		for _, k := range s.Keys {
			b.WriteByte('&')
			writeBracket(b, k)
		}
		return
	}
	if !s.Bracketed && isBareIdent(s.Name) {
		b.WriteString(s.Name)
		return
	}
	writeBracket(b, s.Name)
}

func writeBracket(b *strings.Builder, name string) {
	b.WriteByte('[')
	b.WriteString(strings.ReplaceAll(name, "]", "]]"))
	b.WriteByte(']')
}

func isBareIdent(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		if i == 0 && !isIdentStart(r) {
			return false
		}
		if !isIdentPart(r) || r == utf8.RuneError || unicode.IsSpace(r) {
			return false
		}
	}
	return true
}

func writeList(b *strings.Builder, items []Expr) {
	for i, it := range items {
		if i > 0 {
			b.WriteString(", ")
		}
		writeExpr(b, it)
	}
}

func writeExpr(b *strings.Builder, e Expr) {
	switch n := e.(type) {
	case *SetLit:
		b.WriteByte('{')
		writeList(b, n.Items)
		b.WriteByte('}')
	case *Paren:
		b.WriteByte('(')
		writeList(b, n.Items)
		b.WriteByte(')')
	case *Ref:
		writeSegments(b, n.Segments)
		if n.Suffix != "" {
			b.WriteByte('.')
			b.WriteString(n.Suffix)
		}
	case *Call:
		b.WriteString(n.Name)
		b.WriteByte('(')
		writeList(b, n.Args)
		b.WriteByte(')')
	case *Empty:
	case *Flag:
		b.WriteString(n.Name)
	case *Number:
		b.WriteString(n.Text)
	case *Compare:
		writeExpr(b, n.Left)
		b.WriteByte(' ')
		b.WriteString(n.Op)
		b.WriteByte(' ')
		writeExpr(b, n.Right)
	case *Logic:
		for i, t := range n.Terms {
			if i > 0 {
				b.WriteByte(' ')
				b.WriteString(n.Op)
				b.WriteByte(' ')
			}
			// AND binds tighter than OR: an OR inside an AND needs parentheses.
			if l, ok := t.(*Logic); ok && n.Op == "AND" && l.Op == "OR" {
				b.WriteByte('(')
				writeExpr(b, t)
				b.WriteByte(')')
			} else {
				writeExpr(b, t)
			}
		}
	}
}
