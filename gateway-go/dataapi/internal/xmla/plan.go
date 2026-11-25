package xmla

// The plan of an MDX statement (docs/specs/xmla.md, «Execution», step 1): the tree the parser gave
// is resolved against the cube into the hierarchies of each axis with their members in order, the
// measures, and the filters of the subselects and the slicer. The members of a level come from the
// data (the rows the subselects let through), so planning reads the table; what it cannot render
// exactly is refused with the name of the construct, never answered by guess.

import (
	"context"
	"database/sql"
	"fmt"
	"math/big"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla/mdx"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// Runner is a connection of the engine: the temporary tables of an Execute live in it.
type Runner interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

// refuse is the fault of a construct this interface does not render.
func refuse(format string, args ...any) *Fault {
	f := NewFault(CodeNotServed, fmt.Sprintf(format, args...))
	f.Refused = true
	return f
}

// badName is the fault of a name the cube does not know.
func badName(format string, args ...any) *Fault {
	f := NewFault(CodeBadRequest, fmt.Sprintf(format, args...))
	f.Refused = true
	return f
}

// DefaultMaxKeyBytes is the most bytes of member keys one statement's planning holds: a column of
// long texts must not fill the pod's memory with the members of its levels, whatever the number of
// tuples the cap allows.
const DefaultMaxKeyBytes = 16 << 20

// defaultMemberBatch is how many members one query of the planning names at most.
const defaultMemberBatch = 500

// maxRestrict is the most members of a part that are written in the WHERE of a query: a part with
// more is nearly the whole of its level, and a condition that long does not narrow what is read.
const maxRestrict = 1000

func keyBytesFault(max int) *Fault {
	return NewFault(CodeLimit, fmt.Sprintf("The members of this statement hold more than %d bytes of keys, the cap of bytes for the members of a statement. "+
		"Put fewer fields on the axes, or filter", max))
}

func tuplesFault(max int) *Fault {
	return NewFault(CodeLimit, fmt.Sprintf("This answer holds more than %d tuples on one axis, the cap of tuples per axis (DATA_API__XMLA_MAX_TUPLES). "+
		"Put fewer fields on the axis, or filter", max))
}

// hfilter lets through the rows of any of the members of a hierarchy.
type hfilter struct {
	h       *xmlamodel.Hierarchy
	members []xmlamodel.Member
}

// item is a member of a part of an axis, or a measure.
type item struct {
	m        xmlamodel.Member
	measure  *xmlamodel.Measure
	children int  // -1 until known
	verify   bool // written by name: it must exist
	// DisplayInfo, once the part is final
	drilled, sameParent bool
}

func (it *item) unique() string {
	if it.measure != nil {
		return it.measure.UniqueName
	}
	return it.m.UniqueName()
}

// part is the members of one hierarchy (or of the measures) on an axis, in order. An axis is the
// product of its parts.
type part struct {
	h     *xmlamodel.Hierarchy // nil: the measures
	items []item
}

// setVal is a set: the product of its parts. A set of several hierarchies that is not such a
// product is not rendered.
type setVal struct{ parts []*part }

func (s *setVal) empty() bool { return len(s.parts) == 0 }

func (s *setVal) size() int {
	n := 1
	for _, p := range s.parts {
		n *= len(p.items)
	}
	return n
}

type axisPlan struct {
	index    int
	nonEmpty bool
	parts    []*part
	props    []string // DIMENSION PROPERTIES, canonical
}

// plan is a statement ready to become SQL.
type plan struct {
	cube     *xmlamodel.Cube
	axes     []*axisPlan
	universe []hfilter
	slicer   []hfilter
	// the measures of the cells: the items of the measures' part when one is on an axis,
	// otherwise the one of the slicer or the cube's default
	measures    []*xmlamodel.Measure
	measureAxis int // the axis that has the measures' part, -1 when none
	measurePart int // and its place among the axis' parts
	slicerAxis  []*part
	cellProps   []string
}

// planner resolves a statement; it reads the data through run.
type planner struct {
	ctx  context.Context
	run  Runner
	g    *gen
	cube *xmlamodel.Cube
	max  int // the most members a part may hold
	// the most members one query names, and the most bytes of keys the planning reads
	batch, maxBytes int
	keyBytes        int

	universe []hfilter
	slicer   []hfilter
}

// ── reading the data ──────────────────────────────────────────────────────────

type entry struct {
	m        xmlamodel.Member
	children int
}

// scan reads the members of a level in order, with the number of their children, among the rows
// the filters let through. With `only`, just those members.
func (p *planner) scan(l *xmlamodel.Level, only []xmlamodel.Member, limit int) ([]entry, error) {
	g, d := p.g, p.g.d
	b := &qb{d: d}
	n := g.col(l.Hierarchy.Dimension)
	where := g.where(b, func() []string {
		var extra []string
		if len(only) > 0 {
			extra = append(extra, g.membersCond(b, only))
		}
		if isCalendar(l) && l.Number > 1 {
			extra = append(extra, n+" IS NOT NULL")
		}
		return extra
	}, p.universe)
	exprs := g.groupExprs(l)
	names := make([]string, len(exprs))
	var inner []string
	for i, e := range exprs {
		names[i] = fmt.Sprintf("g%d", i)
		inner = append(inner, e+" AS "+names[i])
	}
	count := "0"
	if ch := g.childExpr(l); ch != "" {
		inner = append(inner, ch+" AS ch")
		count = d.CountDistinct("ch")
	}
	order := make([]string, len(names))
	for i, nm := range names {
		order[i] = d.Ascending(nm)
	}
	q := "SELECT " + g.keyExpr(l, names) + ", " + count + " FROM (SELECT " + strings.Join(inner, ", ") + " FROM " + d.Quote(g.table) + where +
		") GROUP BY " + strings.Join(names, ", ") + " ORDER BY " + strings.Join(order, ", ") + " " + d.Limit(limit+1)
	rows, err := p.run.QueryContext(p.ctx, q, b.args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []entry
	for rows.Next() {
		var k sql.NullString
		var c int64
		if err := rows.Scan(&k, &c); err != nil {
			return nil, err
		}
		if len(out) >= limit {
			return nil, errTooMany
		}
		if p.keyBytes += len(k.String); p.keyBytes > p.maxBytes {
			return nil, errKeyBytes
		}
		m := xmlamodel.Member{Level: l, Kind: xmlamodel.MemberKey, Key: k.String}
		if !k.Valid {
			m.Kind = xmlamodel.MemberNull
		}
		out = append(out, entry{m, int(c)})
	}
	return out, rows.Err()
}

// errTooMany is a read of members that went past what a set may hold, errKeyBytes one that went
// past the bytes of keys.
var (
	errTooMany  = fmt.Errorf("too many members")
	errKeyBytes = fmt.Errorf("too many bytes of keys")
)

func (p *planner) tooMany(err error) error {
	switch err {
	case errTooMany:
		return tuplesFault(p.max)
	case errKeyBytes:
		return keyBytesFault(p.maxBytes)
	}
	return err
}

// scanOf is scan for the rows of `ms`, with their conditions in batches: the members named are
// never more than a batch in one query, and the level is never read whole. At most `limit`
// members come back in all.
func (p *planner) scanOf(l *xmlamodel.Level, ms []xmlamodel.Member, limit int) ([]entry, error) {
	var out []entry
	for from := 0; from < len(ms); from += p.batch {
		es, err := p.scan(l, ms[from:min(from+p.batch, len(ms))], limit-len(out))
		if err != nil {
			return nil, err
		}
		out = append(out, es...)
	}
	return out, nil
}

// countFirst is how many members the first level has: the children of the All member.
func (p *planner) countFirst(h *xmlamodel.Hierarchy) (int, error) {
	g, d := p.g, p.g.d
	b := &qb{d: d}
	where := g.where(b, nil, p.universe)
	first := g.groupExprs(h.Levels[1])[0]
	q := "SELECT count(*) FROM (SELECT " + first + " AS g0 FROM " + d.Quote(g.table) + where + " GROUP BY 1)"
	rows, err := p.run.QueryContext(p.ctx, q, b.args...)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	var n int64
	if rows.Next() {
		if err := rows.Scan(&n); err != nil {
			return 0, err
		}
	}
	return int(n), rows.Err()
}

// childrenOf reads the children of the members, in order, by parent.
func (p *planner) childrenOf(parents []xmlamodel.Member) (map[string][]entry, error) {
	out := map[string][]entry{}
	byLevel := map[*xmlamodel.Level][]xmlamodel.Member{}
	var levels []*xmlamodel.Level
	for _, m := range parents {
		if m.Kind == xmlamodel.MemberNull {
			continue
		}
		h := m.Hierarchy()
		if m.Level.Number+1 >= len(h.Levels) {
			continue
		}
		child := h.Levels[m.Level.Number+1]
		if _, ok := byLevel[child]; !ok {
			levels = append(levels, child)
		}
		byLevel[child] = append(byLevel[child], m)
	}
	for _, child := range levels {
		ps := byLevel[child]
		wanted := map[string]bool{}
		hasAll := false
		for _, m := range ps {
			wanted[m.UniqueName()] = true
			hasAll = hasAll || m.Kind == xmlamodel.MemberAll
		}
		// the limit is the cap of tuples: past it the part is refused whatever else is read
		var es []entry
		var err error
		if hasAll {
			es, err = p.scan(child, nil, p.max)
		} else {
			es, err = p.scanOf(child, ps, p.max)
		}
		if err != nil {
			return nil, p.tooMany(err)
		}
		for _, e := range es {
			par, _ := e.m.Parent()
			if un := par.UniqueName(); wanted[un] {
				out[un] = append(out[un], e)
			}
		}
	}
	return out, nil
}

// values aggregates measures for each item of a part, in the context of the filters and the
// slicer: what a function that orders or filters a set reads.
func (p *planner) values(pt *part, ms []*xmlamodel.Measure) ([][]*float64, error) {
	g := p.g
	seen := map[int]bool{}
	var levels []int
	for _, it := range pt.items {
		if ln := it.m.Level.Number; !seen[ln] {
			seen[ln] = true
			levels = append(levels, ln)
		}
	}
	sort.Ints(levels)
	b := &qb{d: g.d}
	// only the rows of the members read: an aggregation of the whole level reads what it will not use
	where := g.where(b, func() []string { return g.restrictions(b, pt) }, p.universe, p.slicer)
	q, err := g.aggSelect([]hUse{{pt.h, levels}}, ms, where, true)
	if err != nil {
		return nil, err
	}
	rows, err := p.run.QueryContext(p.ctx, q, b.args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	got := map[string][]*float64{}
	for rows.Next() {
		var lv int
		var k string
		vals := make([]sql.NullFloat64, len(ms))
		dest := []any{&lv, &k}
		for i := range vals {
			dest = append(dest, &vals[i])
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, err
		}
		row := make([]*float64, len(ms))
		for i, v := range vals {
			if v.Valid {
				f := v.Float64
				row[i] = &f
			}
		}
		got[fmt.Sprint(lv, "|", k)] = row
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([][]*float64, len(pt.items))
	for i, it := range pt.items {
		out[i] = got[fmt.Sprint(it.m.Level.Number, "|", keyOf(it.m))]
		if out[i] == nil {
			out[i] = make([]*float64, len(ms))
		}
	}
	return out, nil
}

// complete gives every item its number of children and checks that the members written by name
// exist.
func (p *planner) complete(pt *part) error {
	if pt.h == nil {
		for i := range pt.items {
			pt.items[i].children = 0
		}
		return nil
	}
	byLevel := map[*xmlamodel.Level][]int{}
	var levels []*xmlamodel.Level
	for i, it := range pt.items {
		if it.children >= 0 && !it.verify {
			continue
		}
		if it.m.Kind == xmlamodel.MemberAll {
			n, err := p.countFirst(pt.h)
			if err != nil {
				return err
			}
			pt.items[i].children = n
			continue
		}
		if _, ok := byLevel[it.m.Level]; !ok {
			levels = append(levels, it.m.Level)
		}
		byLevel[it.m.Level] = append(byLevel[it.m.Level], i)
	}
	for _, l := range levels {
		idx := byLevel[l]
		only := make([]xmlamodel.Member, len(idx))
		for k, i := range idx {
			only[k] = pt.items[i].m
		}
		es, err := p.scanOf(l, only, len(only))
		if err != nil {
			return p.tooMany(err)
		}
		found := map[string]int{}
		for _, e := range es {
			found[e.m.UniqueName()] = e.children
		}
		for _, i := range idx {
			c, ok := found[pt.items[i].m.UniqueName()]
			if !ok && pt.items[i].verify {
				return badName("The member %s does not exist in this cube", pt.items[i].m.UniqueName())
			}
			pt.items[i].children, pt.items[i].verify = c, false
		}
	}
	for i := range pt.items {
		if pt.items[i].children < 0 {
			pt.items[i].children = 0
		}
	}
	return nil
}

// ── ordering of members ───────────────────────────────────────────────────────

// keyCmp compares two members of one level: the order of the values, a null last.
func keyCmp(a, b xmlamodel.Member) int {
	if a.Kind == xmlamodel.MemberNull || b.Kind == xmlamodel.MemberNull {
		switch {
		case a.Kind == b.Kind:
			return 0
		case a.Kind == xmlamodel.MemberNull:
			return 1
		}
		return -1
	}
	l := a.Level
	if isCalendar(l) {
		pa, _ := xmlamodel.DateParts(l, a.Key)
		pb, _ := xmlamodel.DateParts(l, b.Key)
		for _, c := range [][2]int{{pa.Year, pb.Year}, {pa.Quarter, pb.Quarter}, {pa.Month, pb.Month}, {pa.Day, pb.Day}} {
			if c[0] != c[1] {
				if c[0] < c[1] {
					return -1
				}
				return 1
			}
		}
		return 0
	}
	if l.Hierarchy.Dimension.Kind == dataapiwire.KindInt {
		x, ok1 := new(big.Int).SetString(a.Key, 10)
		y, ok2 := new(big.Int).SetString(b.Key, 10)
		if ok1 && ok2 {
			return x.Cmp(y)
		}
	}
	return strings.Compare(a.Key, b.Key)
}

// hierCmp orders two members of a hierarchy as Hierarchize does: a parent before its children.
func hierCmp(a, b xmlamodel.Member, post bool) int {
	pa := append(a.Ancestors(), a)
	pb := append(b.Ancestors(), b)
	for i := 0; i < len(pa) && i < len(pb); i++ {
		if pa[i].UniqueName() == pb[i].UniqueName() {
			continue
		}
		return keyCmp(pa[i], pb[i])
	}
	if post {
		return len(pb) - len(pa)
	}
	return len(pa) - len(pb)
}

func hierarchize(pt *part, post bool) {
	if pt.h == nil {
		return
	}
	sort.SliceStable(pt.items, func(i, j int) bool { return hierCmp(pt.items[i].m, pt.items[j].m, post) < 0 })
}

// ── evaluation of sets ────────────────────────────────────────────────────────

func segmentsOf(in []mdx.Segment) []xmlamodel.Segment {
	out := make([]xmlamodel.Segment, len(in))
	for i, s := range in {
		out[i] = xmlamodel.Segment{Name: s.Name, Keys: s.Keys}
	}
	return out
}

func single(it item, h *xmlamodel.Hierarchy) *setVal {
	return &setVal{parts: []*part{{h: h, items: []item{it}}}}
}

func (p *planner) checkSize(v *setVal) error {
	for _, pt := range v.parts {
		if len(pt.items) > p.max {
			return tuplesFault(p.max)
		}
	}
	return nil
}

func (p *planner) eval(e mdx.Expr) (*setVal, error) {
	v, err := p.eval1(e)
	if err != nil {
		return nil, err
	}
	return v, p.checkSize(v)
}

func (p *planner) eval1(e mdx.Expr) (*setVal, error) {
	switch n := e.(type) {
	case *mdx.SetLit:
		var vals []*setVal
		held := 0 // the members of every element so far: the cap is on their sum
		for _, it := range n.Items {
			v, err := p.eval(it)
			if err != nil {
				return nil, err
			}
			if !v.empty() {
				if held += v.size(); held > p.max {
					return nil, tuplesFault(p.max)
				}
			}
			vals = append(vals, v)
		}
		return union(vals)
	case *mdx.Paren:
		if len(n.Items) == 1 {
			return p.eval(n.Items[0])
		}
		return p.tuple(n)
	case *mdx.Ref:
		return p.evalRef(n)
	case *mdx.Call:
		return p.evalCall(n)
	}
	return nil, refuse("an expression that is not a set or a member where one is needed")
}

// tuple is (m1, m2, …): one member of each of different hierarchies.
func (p *planner) tuple(n *mdx.Paren) (*setVal, error) {
	out := &setVal{}
	seen := map[*xmlamodel.Hierarchy]bool{}
	measures := false
	for _, it := range n.Items {
		v, err := p.eval(it)
		if err != nil {
			return nil, err
		}
		if len(v.parts) != 1 || len(v.parts[0].items) != 1 {
			return nil, refuse("a tuple whose members are not single members")
		}
		pt := v.parts[0]
		if pt.h == nil && measures || pt.h != nil && seen[pt.h] {
			return nil, refuse("a tuple with two members of the same hierarchy")
		}
		measures = measures || pt.h == nil
		seen[pt.h] = true
		out.parts = append(out.parts, pt)
	}
	return out, nil
}

// union is {a, b, …}: the members of the same hierarchy one after the other, or tuples that make
// a product.
func union(vals []*setVal) (*setVal, error) {
	var live []*setVal
	for _, v := range vals {
		if !v.empty() {
			live = append(live, v)
		}
	}
	switch len(live) {
	case 0:
		return &setVal{}, nil
	case 1:
		return live[0], nil
	}
	first := live[0]
	for _, v := range live[1:] {
		if len(v.parts) != len(first.parts) {
			return nil, refuse("a set that mixes members of different hierarchies")
		}
		for i := range v.parts {
			if v.parts[i].h != first.parts[i].h {
				return nil, refuse("a set that mixes members of different hierarchies")
			}
		}
	}
	if len(first.parts) == 1 {
		out := &part{h: first.parts[0].h}
		for _, v := range live {
			out.items = append(out.items, v.parts[0].items...)
		}
		return &setVal{parts: []*part{out}}, nil
	}
	// tuples: each operand is one tuple, and together they must be the product of their members
	type tup []item
	var tuples []tup
	for _, v := range live {
		t := make(tup, len(v.parts))
		for i, pt := range v.parts {
			if len(pt.items) != 1 {
				return nil, refuse("a set of tuples that is not the product of its members")
			}
			t[i] = pt.items[0]
		}
		tuples = append(tuples, t)
	}
	out := &setVal{}
	idx := make([]map[string]int, len(first.parts))
	for i, pt := range first.parts {
		out.parts = append(out.parts, &part{h: pt.h})
		idx[i] = map[string]int{}
	}
	for _, t := range tuples {
		for i, it := range t {
			if _, ok := idx[i][it.unique()]; !ok {
				idx[i][it.unique()] = len(out.parts[i].items)
				out.parts[i].items = append(out.parts[i].items, it)
			}
		}
	}
	if out.size() != len(tuples) {
		return nil, refuse("a set of tuples that is not the product of its members")
	}
	for n, t := range tuples {
		rest := n
		for i := len(t) - 1; i >= 0; i-- {
			sz := len(out.parts[i].items)
			if idx[i][t[i].unique()] != rest%sz {
				return nil, refuse("a set of tuples that is not the product of its members, in the order of the product")
			}
			rest /= sz
		}
	}
	return out, nil
}

func cross(vals []*setVal) (*setVal, error) {
	out := &setVal{}
	seen := map[*xmlamodel.Hierarchy]bool{}
	for _, v := range vals {
		if v.empty() {
			return &setVal{}, nil
		}
		for _, pt := range v.parts {
			if seen[pt.h] {
				return nil, refuse("CrossJoin of two sets of the same hierarchy")
			}
			seen[pt.h] = true
			out.parts = append(out.parts, pt)
		}
	}
	return out, nil
}

func (p *planner) evalRef(r *mdx.Ref) (*setVal, error) {
	ref, err := p.cube.Resolve(segmentsOf(r.Segments))
	if err != nil {
		return nil, badName("%v", err)
	}
	all := func(h *xmlamodel.Hierarchy) item { return item{m: h.All(), children: -1} }
	switch r.Suffix {
	case "":
		switch ref.Kind {
		case xmlamodel.RefMeasure:
			return single(item{measure: ref.Measure}, nil), nil
		case xmlamodel.RefMember:
			it := item{m: ref.Member, children: -1}
			it.verify = ref.Member.Kind != xmlamodel.MemberAll
			return single(it, ref.Hierarchy), nil
		}
		return nil, refuse("a hierarchy, a dimension or a level used as a set: write .Members after it")
	case "Members", "AllMembers":
		switch ref.Kind {
		case xmlamodel.RefMeasures, xmlamodel.RefMeasuresLevel:
			return p.allMeasures(), nil
		case xmlamodel.RefHierarchy, xmlamodel.RefDimension:
			h := ref.Hierarchy
			if h == nil {
				h = ref.Dimension.Default()
			}
			return p.hierarchyMembers(h)
		case xmlamodel.RefLevel:
			return p.levelMembers(ref.Level)
		}
		return nil, refuse("%s on a member or a measure", r.Suffix)
	case "Children":
		if ref.Kind != xmlamodel.RefMember {
			return nil, refuse("Children of something that is not a member")
		}
		kids, err := p.childrenOf([]xmlamodel.Member{ref.Member})
		if err != nil {
			return nil, err
		}
		pt := &part{h: ref.Hierarchy}
		for _, e := range kids[ref.Member.UniqueName()] {
			pt.items = append(pt.items, item{m: e.m, children: e.children})
		}
		if len(pt.items) == 0 {
			return &setVal{}, nil
		}
		return &setVal{parts: []*part{pt}}, nil
	case "DefaultMember":
		switch ref.Kind {
		case xmlamodel.RefMeasures, xmlamodel.RefMeasuresLevel:
			return single(item{measure: p.cube.Measures[0]}, nil), nil
		case xmlamodel.RefHierarchy:
			return single(all(ref.Hierarchy), ref.Hierarchy), nil
		case xmlamodel.RefDimension:
			return single(all(ref.Dimension.Default()), ref.Dimension.Default()), nil
		}
		return nil, refuse("DefaultMember of something that is not a hierarchy")
	}
	return nil, refuse("the member function .%s", r.Suffix)
}

func (p *planner) allMeasures() *setVal {
	pt := &part{}
	for _, m := range p.cube.Measures {
		pt.items = append(pt.items, item{measure: m})
	}
	return &setVal{parts: []*part{pt}}
}

func (p *planner) levelMembers(l *xmlamodel.Level) (*setVal, error) {
	h := l.Hierarchy
	if l.Part == xmlamodel.PartAll {
		return single(item{m: h.All(), children: -1}, h), nil
	}
	es, err := p.scan(l, nil, p.max)
	if err != nil {
		return nil, p.tooMany(err)
	}
	pt := &part{h: h}
	for _, e := range es {
		pt.items = append(pt.items, item{m: e.m, children: e.children})
	}
	if len(pt.items) == 0 {
		return &setVal{}, nil
	}
	return &setVal{parts: []*part{pt}}, nil
}

// hierarchyMembers is every member of a hierarchy, parents before children.
func (p *planner) hierarchyMembers(h *xmlamodel.Hierarchy) (*setVal, error) {
	pt := &part{h: h, items: []item{{m: h.All(), children: -1}}}
	for _, l := range h.Levels[1:] {
		es, err := p.scan(l, nil, p.max)
		if err != nil {
			return nil, p.tooMany(err)
		}
		for _, e := range es {
			pt.items = append(pt.items, item{m: e.m, children: e.children})
		}
		if len(pt.items) > p.max {
			return nil, tuplesFault(p.max)
		}
	}
	hierarchize(pt, false)
	return &setVal{parts: []*part{pt}}, nil
}

// onePart is the part of a set of one hierarchy; a set of several is refused naming the function.
func onePart(v *setVal, fn string) (*part, error) {
	if len(v.parts) != 1 {
		return nil, refuse("%s over a set of several hierarchies", fn)
	}
	return v.parts[0], nil
}

func clone(pt *part) *part {
	return &part{h: pt.h, items: append([]item(nil), pt.items...)}
}

func wrap(pt *part) *setVal {
	if len(pt.items) == 0 {
		return &setVal{}
	}
	return &setVal{parts: []*part{pt}}
}

func (p *planner) evalCall(c *mdx.Call) (*setVal, error) {
	arg := func(i int) (*setVal, error) { return p.eval(c.Args[i]) }
	flagAt := func(i int, name string) bool {
		if i >= len(c.Args) {
			return false
		}
		f, ok := c.Args[i].(*mdx.Flag)
		return ok && f.Name == name
	}
	switch c.Name {
	case "CrossJoin":
		var vals []*setVal
		held := 0 // the members of every argument so far: the cap is on their sum
		for i := range c.Args {
			v, err := arg(i)
			if err != nil {
				return nil, err
			}
			for _, pt := range v.parts {
				if held += len(pt.items); held > p.max {
					return nil, tuplesFault(p.max)
				}
			}
			vals = append(vals, v)
		}
		return cross(vals)
	case "AddCalculatedMembers":
		return arg(0) // there are no calculated members: the set itself
	case "Hierarchize":
		v, err := arg(0)
		if err != nil {
			return nil, err
		}
		out := &setVal{}
		for _, pt := range v.parts {
			np := clone(pt)
			hierarchize(np, flagAt(1, "POST"))
			out.parts = append(out.parts, np)
		}
		return out, nil
	case "Distinct":
		v, err := arg(0)
		if err != nil {
			return nil, err
		}
		out := &setVal{}
		for _, pt := range v.parts {
			out.parts = append(out.parts, distinct(pt))
		}
		return out, nil
	case "Head":
		v, err := arg(0)
		if err != nil {
			return nil, err
		}
		n := 1
		if len(c.Args) == 2 {
			if n, err = wholeNumber(c.Args[1], "Head"); err != nil {
				return nil, err
			}
		}
		if v.empty() || v.size() <= n {
			return v, nil
		}
		pt, err := onePart(v, "Head")
		if err != nil {
			return nil, err
		}
		np := clone(pt)
		np.items = np.items[:n]
		return wrap(np), nil
	case "Except":
		a, err := arg(0)
		if err != nil {
			return nil, err
		}
		b, err := arg(1)
		if err != nil {
			return nil, err
		}
		if a.empty() {
			return a, nil
		}
		pa, err := onePart(a, "Except")
		if err != nil {
			return nil, err
		}
		if b.empty() {
			if flagAt(2, "ALL") {
				return a, nil
			}
			return wrap(distinct(pa)), nil
		}
		pb, err := onePart(b, "Except")
		if err != nil {
			return nil, err
		}
		if pa.h != pb.h {
			return nil, refuse("Except of sets of different hierarchies")
		}
		drop := map[string]bool{}
		for _, it := range pb.items {
			drop[it.unique()] = true
		}
		np := &part{h: pa.h}
		for _, it := range pa.items {
			if !drop[it.unique()] {
				np.items = append(np.items, it)
			}
		}
		if !flagAt(2, "ALL") {
			np = distinct(np)
		}
		return wrap(np), nil
	case "DrilldownLevel":
		return p.drilldownLevel(c)
	case "DrilldownMember":
		return p.drilldownMember(c)
	case "TopCount", "BottomCount", "Order", "Filter":
		return p.byValue(c)
	}
	return nil, refuse("the function %s", c.Name)
}

func wholeNumber(e mdx.Expr, fn string) (int, error) {
	n, ok := e.(*mdx.Number)
	if !ok || n.Value < 0 || n.Value != float64(int(n.Value)) || n.Value > 1e9 {
		return 0, refuse("%s with a count that is not a whole number", fn)
	}
	return int(n.Value), nil
}

func distinct(pt *part) *part {
	np := &part{h: pt.h}
	seen := map[string]bool{}
	for _, it := range pt.items {
		if !seen[it.unique()] {
			seen[it.unique()] = true
			np.items = append(np.items, it)
		}
	}
	return np
}

// drilldownLevel: the members of the lowest level of the set (or of the level named) get their
// children after them.
func (p *planner) drilldownLevel(c *mdx.Call) (*setVal, error) {
	v, err := p.eval(c.Args[0])
	if err != nil || v.empty() {
		return v, err
	}
	pt, err := onePart(v, "DrilldownLevel")
	if err != nil {
		return nil, err
	}
	if pt.h == nil {
		return nil, refuse("DrilldownLevel of the measures")
	}
	if len(c.Args) > 2 {
		if _, empty := c.Args[2].(*mdx.Empty); !empty {
			if _, flag := c.Args[2].(*mdx.Flag); !flag {
				return nil, refuse("DrilldownLevel with an index")
			}
		}
	}
	level := -1
	if len(c.Args) > 1 {
		if r, ok := c.Args[1].(*mdx.Ref); ok {
			ref, err := p.cube.Resolve(segmentsOf(r.Segments))
			if err != nil {
				return nil, badName("%v", err)
			}
			if ref.Kind != xmlamodel.RefLevel || ref.Hierarchy != pt.h {
				return nil, refuse("DrilldownLevel with something that is not a level of the hierarchy")
			}
			level = ref.Level.Number
		}
	}
	if level < 0 {
		for _, it := range pt.items {
			level = max(level, it.m.Level.Number)
		}
	}
	var parents []xmlamodel.Member
	for _, it := range pt.items {
		if it.m.Level.Number == level {
			parents = append(parents, it.m)
		}
	}
	kids, err := p.childrenOf(parents)
	if err != nil {
		return nil, err
	}
	np := &part{h: pt.h}
	for _, it := range pt.items {
		np.items = append(np.items, it)
		if it.m.Level.Number == level {
			for _, e := range kids[it.m.UniqueName()] {
				np.items = append(np.items, item{m: e.m, children: e.children})
			}
		}
	}
	return wrap(np), p.checkSize(wrap(np))
}

// drilldownMember: each member of the first set that is in the second gets its children after it.
func (p *planner) drilldownMember(c *mdx.Call) (*setVal, error) {
	a, err := p.eval(c.Args[0])
	if err != nil {
		return nil, err
	}
	b, err := p.eval(c.Args[1])
	if err != nil {
		return nil, err
	}
	if a.empty() || b.empty() {
		return a, nil
	}
	pa, err := onePart(a, "DrilldownMember")
	if err != nil {
		return nil, err
	}
	pb, err := onePart(b, "DrilldownMember")
	if err != nil {
		return nil, err
	}
	if pa.h == nil || pa.h != pb.h {
		return nil, refuse("DrilldownMember with members of another hierarchy")
	}
	recursive := false
	for _, x := range c.Args[2:] {
		if f, ok := x.(*mdx.Flag); ok && f.Name == "RECURSIVE" {
			recursive = true
		}
	}
	in := map[string]bool{}
	for _, it := range pb.items {
		in[it.unique()] = true
	}
	kids := map[string][]entry{}
	read := map[string]bool{}
	// the children of the members to drill, read level by level so that a recursive drill is
	// a few reads and not one for each member
	want := func(ms []xmlamodel.Member) error {
		var todo []xmlamodel.Member
		for _, m := range ms {
			if in[m.UniqueName()] && !read[m.UniqueName()] {
				read[m.UniqueName()] = true
				todo = append(todo, m)
			}
		}
		if len(todo) == 0 {
			return nil
		}
		got, err := p.childrenOf(todo)
		if err != nil {
			return err
		}
		for k, v := range got {
			kids[k] = v
		}
		return nil
	}
	var members []xmlamodel.Member
	for _, it := range pa.items {
		members = append(members, it.m)
	}
	for len(members) > 0 {
		if err := want(members); err != nil {
			return nil, err
		}
		members = nil
		if !recursive {
			break
		}
		for k, es := range kids {
			if read[k] {
				for _, e := range es {
					if !read[e.m.UniqueName()] {
						members = append(members, e.m)
					}
				}
			}
		}
	}
	var expand func(it item, out *[]item)
	expand = func(it item, out *[]item) {
		*out = append(*out, it)
		if !in[it.unique()] {
			return
		}
		for _, e := range kids[it.unique()] {
			child := item{m: e.m, children: e.children}
			if recursive {
				expand(child, out)
			} else {
				*out = append(*out, child)
			}
		}
	}
	np := &part{h: pa.h}
	for _, it := range pa.items {
		expand(it, &np.items)
		if len(np.items) > p.max {
			return nil, tuplesFault(p.max)
		}
	}
	return wrap(np), nil
}

// byValue is TopCount, BottomCount, Order and Filter: the functions that read a measure for each
// member of a set.
func (p *planner) byValue(c *mdx.Call) (*setVal, error) {
	v, err := p.eval(c.Args[0])
	if err != nil || v.empty() {
		return v, err
	}
	pt, err := onePart(v, c.Name)
	if err != nil {
		return nil, err
	}
	if pt.h == nil {
		return nil, refuse("%s over the measures", c.Name)
	}
	measure := func(e mdx.Expr) (*xmlamodel.Measure, error) {
		r, ok := e.(*mdx.Ref)
		if !ok {
			return nil, refuse("%s by something that is not a measure", c.Name)
		}
		ref, err := p.cube.Resolve(segmentsOf(r.Segments))
		if err != nil {
			return nil, badName("%v", err)
		}
		if ref.Kind != xmlamodel.RefMeasure {
			return nil, refuse("%s by something that is not a measure", c.Name)
		}
		return ref.Measure, nil
	}
	switch c.Name {
	case "TopCount", "BottomCount":
		n, err := wholeNumber(c.Args[1], c.Name)
		if err != nil {
			return nil, err
		}
		np := clone(pt)
		if len(c.Args) == 3 {
			m, err := measure(c.Args[2])
			if err != nil {
				return nil, err
			}
			vals, err := p.values(pt, []*xmlamodel.Measure{m})
			if err != nil {
				return nil, err
			}
			orderBy(np, vals, 0, c.Name == "TopCount")
		}
		if n < len(np.items) {
			np.items = np.items[:n]
		}
		return wrap(np), nil
	case "Order":
		m, err := measure(c.Args[1])
		if err != nil {
			return nil, err
		}
		flag := "ASC"
		if len(c.Args) == 3 {
			flag = c.Args[2].(*mdx.Flag).Name
		}
		if flag == "ASC" || flag == "DESC" {
			// these keep the hierarchy: sorting inside each parent, which is only the same thing as
			// sorting the lot when the members are of one level
			for _, it := range pt.items[1:] {
				if it.m.Level != pt.items[0].m.Level {
					return nil, refuse("Order with %s over members of several levels (BASC and BDESC sort them all)", flag)
				}
			}
		}
		vals, err := p.values(pt, []*xmlamodel.Measure{m})
		if err != nil {
			return nil, err
		}
		np := clone(pt)
		orderBy(np, vals, 0, flag == "DESC" || flag == "BDESC")
		return wrap(np), nil
	}
	// Filter
	var ms []*xmlamodel.Measure
	index := map[*mdx.Ref]int{}
	var collect func(e mdx.Expr) error
	collect = func(e mdx.Expr) error {
		switch n := e.(type) {
		case *mdx.Compare:
			m, err := measure(n.Left)
			if err != nil {
				return err
			}
			index[n.Left] = len(ms)
			ms = append(ms, m)
		case *mdx.Logic:
			for _, t := range n.Terms {
				if err := collect(t); err != nil {
					return err
				}
			}
		default:
			return refuse("a Filter condition that is not a comparison of a measure with a number")
		}
		return nil
	}
	if err := collect(c.Args[1]); err != nil {
		return nil, err
	}
	vals, err := p.values(pt, ms)
	if err != nil {
		return nil, err
	}
	np := &part{h: pt.h}
	for i, it := range pt.items {
		if holds(c.Args[1], vals[i], index) {
			np.items = append(np.items, it)
		}
	}
	return wrap(np), nil
}

// holds evaluates a Filter condition on the values of one member; an empty cell counts as zero.
func holds(e mdx.Expr, vals []*float64, index map[*mdx.Ref]int) bool {
	switch n := e.(type) {
	case *mdx.Compare:
		v := 0.0
		if f := vals[index[n.Left]]; f != nil {
			v = *f
		}
		switch n.Op {
		case "=":
			return v == n.Right.Value
		case "<>":
			return v != n.Right.Value
		case "<":
			return v < n.Right.Value
		case "<=":
			return v <= n.Right.Value
		case ">":
			return v > n.Right.Value
		case ">=":
			return v >= n.Right.Value
		}
	case *mdx.Logic:
		for _, t := range n.Terms {
			h := holds(t, vals, index)
			if n.Op == "AND" && !h {
				return false
			}
			if n.Op == "OR" && h {
				return true
			}
		}
		return n.Op == "AND"
	}
	return false
}

// orderBy sorts the items by the value of a measure, a stable sort; the empty ones go last.
func orderBy(pt *part, vals [][]*float64, col int, descending bool) {
	type pair struct {
		it item
		v  *float64
	}
	ps := make([]pair, len(pt.items))
	for i, it := range pt.items {
		ps[i] = pair{it, vals[i][col]}
	}
	sort.SliceStable(ps, func(i, j int) bool {
		a, b := ps[i].v, ps[j].v
		switch {
		case a == nil || b == nil:
			return a != nil && b == nil
		case descending:
			return *a > *b
		}
		return *a < *b
	})
	for i := range ps {
		pt.items[i] = ps[i].it
	}
}

// ── the statement ─────────────────────────────────────────────────────────────

// the member properties and the cell properties a statement may ask for
var (
	memberProps = map[string]bool{"MEMBER_UNIQUE_NAME": true, "MEMBER_CAPTION": true, "MEMBER_NAME": true, "MEMBER_KEY": true,
		"MEMBER_VALUE": true, "LEVEL_NUMBER": true, "LEVEL_UNIQUE_NAME": true, "HIERARCHY_UNIQUE_NAME": true,
		"DIMENSION_UNIQUE_NAME": true, "PARENT_UNIQUE_NAME": true, "PARENT_LEVEL": true, "CHILDREN_CARDINALITY": true, "DISPLAY_INFO": true}
	// the cell properties of the standard: VALUE, FORMATTED_VALUE and FORMAT_STRING are answered,
	// the others are accepted and left out (the client takes its defaults)
	cellProps = map[string]bool{"VALUE": true, "FORMATTED_VALUE": true, "FORMAT_STRING": true, "CELL_ORDINAL": true, "ACTION_TYPE": true,
		"BACK_COLOR": true, "FORE_COLOR": true, "FONT_FLAGS": true, "FONT_NAME": true, "FONT_SIZE": true, "LANGUAGE": true,
		"UPDATEABLE": true, "SOLVE_ORDER": true, "CELL_EVALUATION_LIST": true}
)

func propNames(ps []mdx.Property, allowed map[string]bool, what string) ([]string, error) {
	var out []string
	for _, pr := range ps {
		name := strings.ToUpper(pr.Name())
		if len(pr) != 1 || !allowed[name] {
			return nil, refuse("the %s property %s", what, pr.Name())
		}
		out = append(out, name)
	}
	return out, nil
}

// filtersOf reads a set of the slicer or of a subselect as the filters it stands for: a set of
// members of each hierarchy (its tuples being their product), and the measure it may name.
func (p *planner) filtersOf(e mdx.Expr) ([]hfilter, *xmlamodel.Measure, []*part, error) {
	v, err := p.eval(e)
	if err != nil {
		return nil, nil, nil, err
	}
	if v.empty() {
		return []hfilter{{}}, nil, nil, nil // an empty set lets no row through
	}
	var fs []hfilter
	var parts []*part
	var measure *xmlamodel.Measure
	for _, pt := range v.parts {
		if pt.h == nil {
			if len(pt.items) != 1 {
				return nil, nil, nil, refuse("several measures in a slicer or a subselect")
			}
			measure = pt.items[0].measure
			continue
		}
		if err := p.complete(pt); err != nil {
			return nil, nil, nil, err
		}
		f := hfilter{h: pt.h}
		for _, it := range pt.items {
			f.members = append(f.members, it.m)
		}
		fs = append(fs, f)
		parts = append(parts, pt)
	}
	return fs, measure, parts, nil
}

// build plans a statement.
func (p *planner) build(st *mdx.Statement) (*plan, error) {
	var chain []*mdx.Statement
	for s := st.From.Sub; s != nil; s = s.From.Sub {
		chain = append(chain, s)
	}
	for i := len(chain) - 1; i >= 0; i-- {
		s := chain[i]
		sets := make([]mdx.Expr, 0, len(s.Axes)+1)
		for _, ax := range s.Axes {
			if ax.NonEmpty {
				return nil, refuse("NON EMPTY in a subselect")
			}
			sets = append(sets, ax.Set)
		}
		if s.Slicer != nil {
			sets = append(sets, s.Slicer)
		}
		for _, e := range sets {
			fs, m, _, err := p.filtersOf(e)
			if err != nil {
				return nil, err
			}
			if m != nil {
				return nil, refuse("a measure in a subselect")
			}
			p.universe = append(p.universe, fs...)
		}
	}
	pl := &plan{cube: p.cube, measureAxis: -1, universe: p.universe}
	var slicerMeasure *xmlamodel.Measure
	if st.Slicer != nil {
		fs, m, parts, err := p.filtersOf(st.Slicer)
		if err != nil {
			return nil, err
		}
		p.slicer, slicerMeasure, pl.slicerAxis = fs, m, parts
	}
	pl.slicer = p.slicer
	cp, err := propNames(st.Cells, cellProps, "cell")
	if err != nil {
		return nil, err
	}
	pl.cellProps = cp
	// the axes: numbered from 0 with no hole
	byIndex := map[int]*mdx.Axis{}
	for _, ax := range st.Axes {
		byIndex[ax.Index] = ax
	}
	hierarchies := map[*xmlamodel.Hierarchy]bool{}
	for i := 0; i < len(st.Axes); i++ {
		ax := byIndex[i]
		if ax == nil {
			return nil, refuse("axes that are not numbered from 0 with no gap (%s is missing)", mdx.AxisName(i))
		}
		v, err := p.eval(ax.Set)
		if err != nil {
			return nil, err
		}
		props, err := propNames(ax.DimProps, memberProps, "member")
		if err != nil {
			return nil, err
		}
		a := &axisPlan{index: i, nonEmpty: ax.NonEmpty, parts: v.parts, props: props}
		for n, pt := range v.parts {
			if pt.h == nil {
				if pl.measureAxis >= 0 {
					return nil, refuse("measures on two axes")
				}
				pl.measureAxis, pl.measurePart = i, n
				for _, it := range pt.items {
					pl.measures = append(pl.measures, it.measure)
				}
				for k := range pt.items {
					pt.items[k].children = 0
				}
				continue
			}
			if hierarchies[pt.h] {
				return nil, refuse("a hierarchy on two axes")
			}
			hierarchies[pt.h] = true
			for _, f := range p.slicer {
				if f.h == pt.h {
					return nil, refuse("a hierarchy in the slicer and on an axis")
				}
			}
			if err := p.complete(pt); err != nil {
				return nil, err
			}
			finishPart(pt)
		}
		pl.axes = append(pl.axes, a)
	}
	if pl.measureAxis >= 0 && slicerMeasure != nil {
		return nil, refuse("a measure in the slicer and measures on an axis")
	}
	if pl.measureAxis < 0 {
		m := slicerMeasure
		if m == nil {
			m = p.cube.Measures[0] // the default measure, the one Discover declares
		}
		pl.measures = []*xmlamodel.Measure{m}
	}
	return pl, nil
}

// finishPart sets the DisplayInfo flags of the items of a part: a member is drilled down when its
// children follow it, and has the same parent as the previous one when they share it.
func finishPart(pt *part) {
	for i := range pt.items {
		it := &pt.items[i]
		par, hasParent := it.m.Parent()
		if i+1 < len(pt.items) {
			if np, ok := pt.items[i+1].m.Parent(); ok && np.UniqueName() == it.m.UniqueName() {
				it.drilled = true
			}
		}
		if i == 0 {
			it.sameParent = !hasParent
			continue
		}
		prev, prevHas := pt.items[i-1].m.Parent()
		it.sameParent = hasParent == prevHas && (!hasParent || par.UniqueName() == prev.UniqueName())
	}
}
