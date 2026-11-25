package xmla

// Execute (docs/specs/xmla.md, piece 4). The statement is parsed, planned against the cube, and
// computed by one aggregation of the engine with grouping sets, whose result goes into a
// temporary table of the engine (on its disk once it outgrows its memory). The axes and the cells
// are then two ordered reads of tables built from it, written as XML while they are read: the
// answer is never held whole. Everything that can fail is done before the first byte is written.

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla/mdx"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// ExecLimits are the administrator's caps an Execute answer meets.
type ExecLimits struct {
	// the most cells in an answer (DATA_API__XMLA_MAX_CELLS) and tuples on one axis (…_MAX_TUPLES)
	MaxCells, MaxTuples int
}

// Source is what Execute computes on: the dialect of the engine and the table of the cube's data.
type Source struct {
	// Open gives the connection the data of a cube is read on (its temporary tables live there);
	// the caller closes it once the Result is closed.
	Open    func(cube *xmlamodel.Cube) (Runner, error)
	Dialect Dialect
	Table   string
}

var seq atomic.Uint64

// Result is a computed answer, waiting to be written. Close it: it holds temporary tables.
type Result struct {
	src     Source
	run     Runner
	catalog string
	plan    *plan
	// the number of tuples of each axis, and of cells in all
	counts []int64
	cells  int64
	// the tables that were made, to drop; the table of each axis that is NON EMPTY; the cells
	tables    []string
	axisTable []string
	cellTable string
	name      string
	props     execProps
	// Refused is the statement, set when it was parsed and planned (for the caller to keep)
}

type execProps struct {
	content string // SchemaData, Data, Schema or None
}

// Plan errors name the construct; the parser's become faults here.
func parseFault(err error) *Fault {
	var e *mdx.Error
	if errors.As(err, &e) {
		code := uint32(CodeNotServed)
		if e.Code == mdx.CodeSyntax || e.Code == mdx.CodeEmpty {
			code = CodeBadRequest
		}
		f := NewFault(code, "The statement was not understood: "+e.Error())
		f.Refused = true
		return f
	}
	return NewFault(CodeBadRequest, "The statement was not understood: "+err.Error())
}

// checkExecProps reads the properties of an Execute: what it serves is Multidimensional with
// TupleFormat axes, for the catalog of the data API.
func checkExecProps(req *Request, cat *Catalog) (execProps, error) {
	var out execProps
	switch f := req.Property("Format"); {
	case f == "", strings.EqualFold(f, "Multidimensional"), strings.EqualFold(f, "Native"):
	default:
		return out, NewFault(CodeNotServed, "The format «"+Shorten(f)+"» is not served for a statement: ask for Multidimensional")
	}
	if a := req.Property("AxisFormat"); a != "" && !strings.EqualFold(a, "TupleFormat") {
		return out, NewFault(CodeNotServed, "The axis format «"+Shorten(a)+"» is not served: ask for TupleFormat")
	}
	switch c := req.Property("Content"); {
	case c == "", strings.EqualFold(c, "SchemaData"):
		out.content = "SchemaData"
	case strings.EqualFold(c, "Data"):
		out.content = "Data"
	case strings.EqualFold(c, "Schema"):
		out.content = "Schema"
	case strings.EqualFold(c, "None"):
		out.content = "None"
	default:
		return out, NewFault(CodeNotServed, "The content «"+Shorten(c)+"» is not served: SchemaData, Data, Schema or None")
	}
	if c := req.Property("Catalog"); c != "" && !strings.EqualFold(c, cat.Name) {
		return out, NewFault(CodeBadRequest, "The catalog «"+Shorten(c)+"» does not exist: this data API's is «"+cat.Name+"»")
	}
	return out, nil
}

// cubeOf finds the cube a statement reads, looking through its subselects.
func cubeOf(st *mdx.Statement, cat *Catalog) (*xmlamodel.Cube, error) {
	for st.From.Sub != nil {
		st = st.From.Sub
	}
	segs := st.From.Cube
	if len(segs) == 0 || len(segs) > 2 {
		return nil, badName("The cube is named [cube] or [catalog].[cube]")
	}
	if len(segs) == 2 && !strings.EqualFold(segs[0].Name, cat.Name) {
		return nil, badName("The catalog «%s» does not exist: this data API's is «%s»", Shorten(segs[0].Name), cat.Name)
	}
	name := segs[len(segs)-1].Name
	for _, c := range cat.Cubes {
		if strings.EqualFold(c.Name, name) {
			return c, nil
		}
	}
	return nil, badName("The cube «%s» does not exist (or this key cannot read it)", Shorten(name))
}

// Execute plans and computes a statement. A refusal is a *Fault with Refused set; the caller may
// keep the statement. On success the Result must be written and closed; on a failure nothing is
// left behind.
func Execute(ctx context.Context, src Source, req *Request, cat *Catalog, lim ExecLimits) (*Result, error) {
	props, err := checkExecProps(req, cat)
	if err != nil {
		return nil, err
	}
	st, err := mdx.Parse(req.Statement)
	if err != nil {
		return nil, parseFault(err)
	}
	cube, err := cubeOf(st, cat)
	if err != nil {
		return nil, err
	}
	if lim.MaxCells <= 0 {
		lim.MaxCells = 1_000_000
	}
	if lim.MaxTuples <= 0 {
		lim.MaxTuples = 10_000
	}
	run, err := src.Open(cube)
	if err != nil {
		return nil, dataFault(ctx, err)
	}
	p := &planner{ctx: ctx, run: run, g: &gen{d: src.Dialect, table: src.Table}, cube: cube, max: lim.MaxTuples}
	pl, err := p.build(st)
	if err != nil {
		return nil, dataFault(ctx, err)
	}
	res := &Result{src: src, run: run, catalog: cat.Name, plan: pl, props: props, name: fmt.Sprintf("xmla_%d", seq.Add(1))}
	if err := res.compute(ctx, p.g, lim); err != nil {
		res.Close()
		return nil, dataFault(ctx, err)
	}
	return res, nil
}

// dataFault is the fault of a failed read of the data: ours pass; a timeout is the cap of seconds.
func dataFault(ctx context.Context, err error) error {
	var f *Fault
	if errors.As(err, &f) {
		return f
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return NewFault(CodeLimit, "The statement took longer than the limit of seconds for a statement")
	}
	if ctx.Err() != nil {
		return NewFault(CodeInternal, "The request was cancelled")
	}
	return NewFault(CodeInternal, "The statement could not be computed: try again")
}

// product multiplies without going past what an ordinal holds.
func product(ns ...int64) (int64, bool) {
	out := int64(1)
	for _, n := range ns {
		if n == 0 {
			return 0, true
		}
		if out > (1<<62)/n {
			return 0, false
		}
		out *= n
	}
	return out, true
}

type hpart struct {
	axis    int
	pt      *part
	use     int // index in the hierarchies the aggregation groups by
	trivial bool
	table   string
	alias   string
	stride  int64 // in the ordinal of its axis
}

func (r *Result) exec(ctx context.Context, query string, args ...any) error {
	_, err := r.run.ExecContext(ctx, query, args...)
	return err
}

// make creates a table from a query, noting it for the drop first: a failed creation is dropped too.
func (r *Result) make(ctx context.Context, name, query string, args ...any) error {
	r.tables = append(r.tables, name)
	return r.exec(ctx, r.src.Dialect.CreateTable(name, query), args...)
}

func (r *Result) compute(ctx context.Context, g *gen, lim ExecLimits) error {
	pl, d := r.plan, r.src.Dialect
	nAxes := len(pl.axes)
	r.counts = make([]int64, nAxes)
	r.axisTable = make([]string, nAxes)
	// the parts, their sizes and strides
	var hps []*hpart
	var uses []hUse
	pre := make([]int64, nAxes)
	empty := false
	for a, ax := range pl.axes {
		sizes := make([]int64, len(ax.parts))
		for i, pt := range ax.parts {
			sizes[i] = int64(len(pt.items))
		}
		total, ok := product(sizes...)
		if !ok {
			return tuplesFault(lim.MaxTuples)
		}
		pre[a] = total
		if len(ax.parts) == 0 {
			pre[a] = 0
		}
		if pre[a] == 0 {
			empty = true
		}
		for i, pt := range ax.parts {
			stride := int64(1)
			for _, s := range sizes[i+1:] {
				stride *= s
			}
			if pt.h == nil {
				continue
			}
			hp := &hpart{axis: a, pt: pt, use: len(uses), stride: stride}
			seen := map[int]bool{}
			var levels []int
			for _, it := range pt.items {
				if ln := it.m.Level.Number; !seen[ln] {
					seen[ln] = true
					levels = append(levels, ln)
				}
			}
			sort.Ints(levels)
			uses = append(uses, hUse{pt.h, levels})
			hp.trivial = len(pt.items) == 1 && pt.items[0].m.Kind == xmlamodel.MemberAll
			hp.alias = fmt.Sprintf("p%d", len(hps))
			hps = append(hps, hp)
		}
		if !ax.nonEmpty && pre[a] > int64(lim.MaxTuples) {
			return tuplesFault(lim.MaxTuples)
		}
		r.counts[a] = pre[a]
	}
	// the slicer axis is the product of its members
	slicerTuples := int64(1)
	for _, pt := range pl.slicerAxis {
		slicerTuples *= int64(len(pt.items))
		if slicerTuples > int64(lim.MaxTuples) {
			return tuplesFault(lim.MaxTuples)
		}
	}
	if empty {
		// an axis with nothing on it: no cell, and the tuples that are asked for whatever the data
		for a, ax := range pl.axes {
			if ax.nonEmpty {
				r.counts[a] = 0
			}
		}
		r.cells = 0
		return nil
	}
	// the aggregation, in one pass
	b := &qb{d: d}
	where := g.where(b, nil, pl.universe, pl.slicer)
	sel, err := g.aggSelect(uses, pl.measures, where, false)
	if err != nil {
		return err
	}
	agg := r.name + "_agg"
	if err := r.make(ctx, agg, sel, b.args...); err != nil {
		return err
	}
	// the order of the members of each part, for the join
	var joins []string
	for i, hp := range hps {
		if hp.trivial {
			continue
		}
		hp.table = fmt.Sprintf("%s_p%d", r.name, i)
		if err := r.makePart(ctx, hp); err != nil {
			return err
		}
		joins = append(joins, fmt.Sprintf("JOIN %s %s ON a.lv%d = %s.lv AND a.k%d = %s.k", hp.table, hp.alias, hp.use, hp.alias, hp.use, hp.alias))
	}
	// the cells with the ordinal of their tuple on each axis (before the empty ones are dropped)
	cols := make([]string, 0, nAxes+len(pl.measures))
	for a := range pl.axes {
		var terms []string
		for _, hp := range hps {
			if hp.axis == a && !hp.trivial {
				terms = append(terms, fmt.Sprintf("%s.o * %d", hp.alias, hp.stride))
			}
		}
		e := "CAST(0 AS BIGINT)"
		if len(terms) > 0 {
			e = strings.Join(terms, " + ")
		}
		cols = append(cols, fmt.Sprintf("(%s) AS a%d", e, a))
	}
	var notNull []string
	for j := range pl.measures {
		cols = append(cols, fmt.Sprintf("%s AS v%d", d.Text("a.m"+strconv.Itoa(j)), j))
		notNull = append(notNull, fmt.Sprintf("a.m%d IS NOT NULL", j))
	}
	joined := r.name + "_j"
	if err := r.make(ctx, joined, "SELECT "+strings.Join(cols, ", ")+" FROM "+agg+" a "+strings.Join(joins, " ")+" WHERE "+strings.Join(notNull, " OR ")); err != nil {
		return err
	}
	// one row for each non-empty cell: its ordinals, its measure and its value
	var branches []string
	for j := range pl.measures {
		var sc []string
		for a := range pl.axes {
			e := fmt.Sprintf("a%d", a)
			if a == pl.measureAxis {
				stride := int64(1)
				for _, pt := range pl.axes[a].parts[pl.measurePart+1:] {
					stride *= int64(len(pt.items))
				}
				e += fmt.Sprintf(" + %d", int64(j)*stride)
			}
			sc = append(sc, fmt.Sprintf("(%s) AS a%d", e, a))
		}
		sc = append(sc, fmt.Sprintf("%d AS mi", j), fmt.Sprintf("v%d AS v", j))
		branches = append(branches, "SELECT "+strings.Join(sc, ", ")+" FROM "+joined+fmt.Sprintf(" WHERE v%d IS NOT NULL", j))
	}
	nonEmptyCells := r.name + "_c"
	if err := r.make(ctx, nonEmptyCells, strings.Join(branches, " UNION ALL ")); err != nil {
		return err
	}
	// the axes that are NON EMPTY keep the tuples with a cell, numbered in order
	for a, ax := range pl.axes {
		if !ax.nonEmpty {
			continue
		}
		t := fmt.Sprintf("%s_x%d", r.name, a)
		col := fmt.Sprintf("a%d", a)
		if err := r.make(ctx, t, fmt.Sprintf("SELECT %s AS pre, row_number() OVER (ORDER BY %s) - 1 AS o FROM (SELECT DISTINCT %s FROM %s)", col, col, col, nonEmptyCells)); err != nil {
			return err
		}
		n, err := r.count(ctx, t)
		if err != nil {
			return err
		}
		if n > int64(lim.MaxTuples) {
			return tuplesFault(lim.MaxTuples)
		}
		r.counts[a], r.axisTable[a] = n, t
	}
	cells, ok := product(r.counts...)
	if !ok || cells > int64(lim.MaxCells) {
		return NewFault(CodeLimit, fmt.Sprintf("This answer holds more than %d cells, the cap of cells in an answer (DATA_API__XMLA_MAX_CELLS). "+
			"Put fewer fields on the axes, or filter", lim.MaxCells))
	}
	r.cells = cells
	// the cells in the order of their ordinal: columns vary fastest
	var ord, from []string
	stride := int64(1)
	for a := range pl.axes {
		if r.axisTable[a] != "" {
			ord = append(ord, fmt.Sprintf("x%d.o * %d", a, stride))
			from = append(from, fmt.Sprintf("JOIN %s x%d ON c.a%d = x%d.pre", r.axisTable[a], a, a, a))
		} else {
			ord = append(ord, fmt.Sprintf("c.a%d * %d", a, stride))
		}
		stride *= r.counts[a]
	}
	oe := "CAST(0 AS BIGINT)"
	if len(ord) > 0 {
		oe = strings.Join(ord, " + ")
	}
	r.cellTable = r.name + "_f"
	return r.make(ctx, r.cellTable, "SELECT ("+oe+") AS ord, c.mi AS mi, c.v AS v FROM "+nonEmptyCells+" c "+strings.Join(from, " ")+" ORDER BY ord")
}

func (r *Result) count(ctx context.Context, table string) (int64, error) {
	rows, err := r.run.QueryContext(ctx, "SELECT count(*) FROM "+table)
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
	return n, rows.Err()
}

// makePart writes the members of a part with their place, to join the aggregation with.
func (r *Result) makePart(ctx context.Context, hp *hpart) error {
	d := r.src.Dialect
	r.tables = append(r.tables, hp.table)
	if err := r.exec(ctx, "CREATE TEMP TABLE "+hp.table+" (o BIGINT, lv INTEGER, k VARCHAR)"); err != nil {
		return err
	}
	const batch = 400
	for from := 0; from < len(hp.pt.items); from += batch {
		to := min(from+batch, len(hp.pt.items))
		b := &qb{d: d}
		var vals []string
		for i := from; i < to; i++ {
			m := hp.pt.items[i].m
			vals = append(vals, fmt.Sprintf("(%s, %s, %s)", b.arg(int64(i)), b.arg(int64(m.Level.Number)), b.arg(keyOf(m))))
		}
		if err := r.exec(ctx, "INSERT INTO "+hp.table+" VALUES "+strings.Join(vals, ", "), b.args...); err != nil {
			return err
		}
	}
	return nil
}

// Close drops the temporary tables, whatever became of the request.
func (r *Result) Close() {
	if r == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(context.Background()), 10*time.Second)
	defer cancel()
	for i := len(r.tables) - 1; i >= 0; i-- {
		_ = r.exec(ctx, r.src.Dialect.DropTable(r.tables[i]))
	}
	r.tables = nil
}

// Cells is how many cells the answer has (empty ones too): the product of its axes.
func (r *Result) Cells() int64 { return r.cells }
