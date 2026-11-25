package xmla

// The SQL of Execute, written through a Dialect. Every value of the MDX (a member's key, a number)
// reaches the text as a bound value; the names of columns come from the model and are quoted by
// the dialect.

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// the markers a key carries in the tables joined by Execute: a null cannot be compared with
// itself in a join, so every key is a text that is never null
const (
	keyAll  = "a" // the All member
	keyNull = "n" // the null member
	keyHead = "v" // the value, after this letter
	maxSets = 512 // the most grouping sets one aggregation takes
)

// qb collects the bound values of one statement in the order their placeholders are written.
type qb struct {
	d    Dialect
	args []any
}

func (b *qb) arg(v any) string {
	b.args = append(b.args, v)
	return b.d.Placeholder(len(b.args))
}

// gen writes the SQL over the table a cube reads.
type gen struct {
	d     Dialect
	table string
}

// col is the column of a dimension as a pivot groups it: a timestamp with a zone in UTC.
func (g *gen) col(dim *xmlamodel.Dimension) string {
	c := g.d.Quote(dim.Column)
	if dim.Kind == dataapiwire.KindTimestampTZ {
		return g.d.UTC(c)
	}
	return c
}

func isCalendar(l *xmlamodel.Level) bool { return l.Part >= xmlamodel.PartYear }

// groupExprs are the expressions a level is grouped by.
func (g *gen) groupExprs(l *xmlamodel.Level) []string {
	n := g.col(l.Hierarchy.Dimension)
	switch l.Part {
	case xmlamodel.PartYear:
		return []string{g.d.Year(n)}
	case xmlamodel.PartQuarter:
		return []string{g.d.Year(n), g.d.Quarter(n)}
	case xmlamodel.PartMonth:
		return []string{g.d.Year(n), g.d.Month(n)}
	case xmlamodel.PartDay:
		return []string{g.d.Date(n)}
	}
	return []string{n}
}

// keyExpr is the text of a member's key at a level, from the names of its grouping columns.
func (g *gen) keyExpr(l *xmlamodel.Level, c []string) string {
	d := g.d
	switch l.Part {
	case xmlamodel.PartYear:
		return d.ZeroPad(c[0], 4)
	case xmlamodel.PartQuarter:
		return d.Concat(d.ZeroPad(c[0], 4), d.Literal("-Q"), d.Text(c[1]))
	case xmlamodel.PartMonth:
		return d.Concat(d.ZeroPad(c[0], 4), d.Literal("-"), d.ZeroPad(c[1], 2))
	}
	return d.Text(c[0])
}

// childExpr is what the distinct values of count the children of a member of the level.
func (g *gen) childExpr(l *xmlamodel.Level) string {
	n := g.col(l.Hierarchy.Dimension)
	switch l.Part {
	case xmlamodel.PartYear:
		return g.d.Quarter(n)
	case xmlamodel.PartQuarter:
		return g.d.Month(n)
	case xmlamodel.PartMonth:
		return g.d.Date(n)
	}
	return ""
}

// memberCond is the condition that a row belongs to the member; "" when every row does.
func (g *gen) memberCond(b *qb, m xmlamodel.Member) string {
	d := g.d
	dim := m.Level.Hierarchy.Dimension
	n := g.col(dim)
	switch m.Kind {
	case xmlamodel.MemberAll:
		return ""
	case xmlamodel.MemberNull:
		return n + " IS NULL"
	}
	if isCalendar(m.Level) {
		p, ok := xmlamodel.DateParts(m.Level, m.Key)
		if !ok {
			return "1 = 0"
		}
		switch m.Level.Part {
		case xmlamodel.PartYear:
			return d.Year(n) + " = " + b.arg(p.Year)
		case xmlamodel.PartQuarter:
			return d.Year(n) + " = " + b.arg(p.Year) + " AND " + d.Quarter(n) + " = " + b.arg(p.Quarter)
		case xmlamodel.PartMonth:
			return d.Year(n) + " = " + b.arg(p.Year) + " AND " + d.Month(n) + " = " + b.arg(p.Month)
		}
		return d.Date(n) + " = " + d.CastParam(ParamDate, b.arg(m.Key))
	}
	switch dim.Kind {
	case dataapiwire.KindInt:
		if v, err := strconv.ParseInt(m.Key, 10, 64); err == nil {
			return n + " = " + b.arg(v)
		}
	case dataapiwire.KindBool:
		return n + " = " + b.arg(m.Key == "true")
	case dataapiwire.KindDate:
		return n + " = " + d.CastParam(ParamDate, b.arg(m.Key))
	case dataapiwire.KindTimestamp, dataapiwire.KindTimestampTZ:
		return n + " = " + d.CastParam(ParamTimestamp, b.arg(m.Key))
	}
	return d.Text(n) + " = " + b.arg(m.Key)
}

// membersCond is the condition that a row belongs to one of the members of a hierarchy.
func (g *gen) membersCond(b *qb, ms []xmlamodel.Member) string {
	var parts, plain []string
	var plainKeys []xmlamodel.Member
	for _, m := range ms {
		if m.Kind == xmlamodel.MemberAll {
			return ""
		}
		if m.Kind == xmlamodel.MemberKey && !isCalendar(m.Level) {
			plainKeys = append(plainKeys, m)
			continue
		}
		parts = append(parts, "("+g.memberCond(b, m)+")")
	}
	// the plain keys of a column, several at once: one IN, when its values read the same way
	if len(plainKeys) > 1 {
		dim := plainKeys[0].Level.Hierarchy.Dimension
		n := g.col(dim)
		ints := dim.Kind == dataapiwire.KindInt
		var vals []any
		for _, m := range plainKeys {
			if ints {
				v, err := strconv.ParseInt(m.Key, 10, 64)
				if err != nil {
					ints, vals = false, nil
					break
				}
				vals = append(vals, v)
			}
		}
		text := dim.Kind != dataapiwire.KindInt && dim.Kind != dataapiwire.KindBool && dim.Kind != dataapiwire.KindDate &&
			dim.Kind != dataapiwire.KindTimestamp && dim.Kind != dataapiwire.KindTimestampTZ
		switch {
		case ints:
			ph := make([]string, len(vals))
			for i, v := range vals {
				ph[i] = b.arg(v)
			}
			plain = append(plain, n+" IN ("+strings.Join(ph, ", ")+")")
			plainKeys = nil
		case text:
			ph := make([]string, len(plainKeys))
			for i, m := range plainKeys {
				ph[i] = b.arg(m.Key)
			}
			plain = append(plain, g.d.Text(n)+" IN ("+strings.Join(ph, ", ")+")")
			plainKeys = nil
		}
	}
	for _, m := range plainKeys {
		parts = append(parts, "("+g.memberCond(b, m)+")")
	}
	parts = append(parts, plain...)
	if len(parts) == 0 {
		return "1 = 0" // a filter with no member lets no row through
	}
	return "(" + strings.Join(parts, " OR ") + ")"
}

// where writes WHERE with the filters of every list, all of them to be met, then the conditions
// `extra` gives (called after the filters, so that the bound values stay in the order of the text).
func (g *gen) where(b *qb, extra func() []string, filters ...[]hfilter) string {
	var conds []string
	for _, list := range filters {
		for _, f := range list {
			if c := g.membersCond(b, f.members); c != "" {
				conds = append(conds, c)
			}
		}
	}
	if extra != nil {
		conds = append(conds, extra()...)
	}
	if len(conds) == 0 {
		return ""
	}
	return " WHERE " + strings.Join(conds, " AND ")
}

// hUse is a hierarchy an aggregation groups by, with the levels it is grouped at (0 is All).
type hUse struct {
	h      *xmlamodel.Hierarchy
	levels []int // ascending, no repeat
}

// aggSelect writes one aggregation of the measures, grouped by the levels of every hierarchy in
// one pass (grouping sets). Each hierarchy gives the columns lv<i> (its level) and k<i> (the key
// of its member, with a marker for All and for the null); then come m<j>, the measures.
func (g *gen) aggSelect(uses []hUse, ms []*xmlamodel.Measure, where string, asFloat bool) (string, error) {
	d := g.d
	type gcol struct{ name, expr string }
	var gcols []gcol
	byLevel := make([]map[int][]int, len(uses)) // use → level → indexes of its columns
	all := make([][]int, len(uses))             // use → every column index
	for i, u := range uses {
		byLevel[i] = map[int][]int{}
		seen := map[string]int{}
		for _, ln := range u.levels {
			if ln == 0 {
				byLevel[i][0] = nil
				continue
			}
			for _, ex := range g.groupExprs(u.h.Levels[ln]) {
				idx, ok := seen[ex]
				if !ok {
					idx = len(gcols)
					gcols = append(gcols, gcol{fmt.Sprintf("g%d", idx), ex})
					seen[ex] = idx
					all[i] = append(all[i], idx)
				}
				byLevel[i][ln] = append(byLevel[i][ln], idx)
			}
		}
	}
	// the sets: one for each choice of a level in every hierarchy
	sets := [][]int{nil}
	for i, u := range uses {
		var next [][]int
		for _, s := range sets {
			for _, ln := range u.levels {
				c := append(append([]int{}, s...), byLevel[i][ln]...)
				next = append(next, c)
			}
		}
		sets = next
		if len(sets) > maxSets {
			return "", refuse("the levels of this statement make more than %d totals to compute", maxSets)
		}
	}
	// the inner select: the grouping expressions and the columns the measures read
	var inner []string
	for _, c := range gcols {
		inner = append(inner, c.expr+" AS "+c.name)
	}
	srcName := map[string]string{}
	var mexprs []string
	for _, m := range ms {
		if m.Agg == xmlamodel.AggRows {
			mexprs = append(mexprs, d.Aggregate(m.Agg, ""))
			continue
		}
		name, ok := srcName[m.Column]
		if !ok {
			name = fmt.Sprintf("c%d", len(srcName))
			srcName[m.Column] = name
			inner = append(inner, d.Quote(m.Column)+" AS "+name)
		}
		mexprs = append(mexprs, d.Aggregate(m.Agg, name))
	}
	if len(inner) == 0 {
		inner = []string{"1 AS one"}
	}
	var sel []string
	for i, u := range uses {
		if len(all[i]) == 0 {
			sel = append(sel, "0 AS lv"+strconv.Itoa(i), d.Literal(keyAll)+" AS k"+strconv.Itoa(i))
			continue
		}
		names := make([]string, len(all[i]))
		for p, idx := range all[i] {
			names[p] = gcols[idx].name
		}
		grouping := d.Grouping(names)
		var lvCase, kCase strings.Builder
		lvCase.WriteString("CASE " + grouping)
		kCase.WriteString("CASE " + grouping)
		for _, ln := range u.levels {
			in := map[int]bool{}
			for _, idx := range byLevel[i][ln] {
				in[idx] = true
			}
			mask := 0
			for p, idx := range all[i] {
				if !in[idx] {
					mask |= 1 << (len(all[i]) - 1 - p)
				}
			}
			lvCase.WriteString(fmt.Sprintf(" WHEN %d THEN %d", mask, ln))
			if ln == 0 {
				kCase.WriteString(fmt.Sprintf(" WHEN %d THEN %s", mask, d.Literal(keyAll)))
				continue
			}
			cn := make([]string, len(byLevel[i][ln]))
			for p, idx := range byLevel[i][ln] {
				cn[p] = gcols[idx].name
			}
			key := d.Coalesce(d.Concat(d.Literal(keyHead), g.keyExpr(u.h.Levels[ln], cn)), d.Literal(keyNull))
			kCase.WriteString(fmt.Sprintf(" WHEN %d THEN %s", mask, key))
		}
		lvCase.WriteString(" END AS lv" + strconv.Itoa(i))
		kCase.WriteString(" END AS k" + strconv.Itoa(i))
		sel = append(sel, lvCase.String(), kCase.String())
	}
	for j, e := range mexprs {
		if asFloat {
			e = d.Float(e)
		}
		sel = append(sel, e+" AS m"+strconv.Itoa(j))
	}
	q := "SELECT " + strings.Join(sel, ", ") + " FROM (SELECT " + strings.Join(inner, ", ") + " FROM " + d.Quote(g.table) + where + ")"
	if len(gcols) > 0 {
		named := make([][]string, len(sets))
		for i, s := range sets {
			named[i] = make([]string, len(s))
			for p, idx := range s {
				named[i][p] = gcols[idx].name
			}
		}
		q += " " + d.GroupBy(named)
	}
	return q, nil
}

// keyOf is the key a member carries in the joined tables (see the markers above).
func keyOf(m xmlamodel.Member) string {
	switch m.Kind {
	case xmlamodel.MemberAll:
		return keyAll
	case xmlamodel.MemberNull:
		return keyNull
	}
	return keyHead + m.Key
}
