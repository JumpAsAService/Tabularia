package pod

// The data under Discover (docs/specs/xmla.md, piece 2): the members of a level, read from the
// resource's local DuckDB table, in order, a row at a time. The SQL is written from the model's
// names and the table's column names only; a key a client sent reaches it as a bound value.

import (
	"context"
	"database/sql"
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// cubeOf is the cube a resource's prepared table becomes.
func cubeOf(res *dataapiwire.Resource, t *Table) *xmlamodel.Cube {
	cols := make([]xmlamodel.Column, len(t.Columns))
	for i, c := range t.Columns {
		w, s := xmlamodel.DecimalOf(c.Type)
		cols[i] = xmlamodel.Column{Name: c.Name, Kind: xmlamodel.KindOfDuckDB(c.Type), Width: w, Scale: s}
	}
	name := res.Cube
	if name == "" {
		name = res.Name
	}
	cube := xmlamodel.Build(name, res.Name, cols)
	cube.Updated = res.Version
	return cube
}

// duckData reads the members of the tables held for this request, by resource.
type duckData struct{ tables map[string]*Table }

// norm is the column as a calendar reads it: a timestamp with a time zone in UTC.
func norm(d *xmlamodel.Dimension) string {
	if d.Kind == dataapiwire.KindTimestampTZ {
		return "timezone('UTC', " + ident(d.Column) + ")"
	}
	return ident(d.Column)
}

// calendar is how a level of the Year-Quarter-Month-Day hierarchy is read: what its members are
// grouped by, what its children are counted by, and which of the level's parts a member below it
// is bound to.
type calendar struct {
	groups []string // grouping expressions (ints), or one expression for a day
	child  string   // the expression whose distinct values are the children, "" for a day
}

func calendarOf(l *xmlamodel.Level, n string) calendar {
	switch l.Part {
	case xmlamodel.PartYear:
		return calendar{[]string{"year(" + n + ")"}, "quarter(" + n + ")"}
	case xmlamodel.PartQuarter:
		return calendar{[]string{"year(" + n + ")", "quarter(" + n + ")"}, "month(" + n + ")"}
	case xmlamodel.PartMonth:
		return calendar{[]string{"year(" + n + ")", "month(" + n + ")"}, "CAST(" + n + " AS DATE)"}
	}
	return calendar{[]string{"CAST(" + n + " AS DATE)"}, ""}
}

// within is the condition that a row belongs to a calendar member, with its bound values.
func within(m xmlamodel.Member, n string) (string, []any, bool) {
	p, ok := xmlamodel.DateParts(m.Level, m.Key)
	if !ok {
		return "", nil, false
	}
	switch m.Level.Part {
	case xmlamodel.PartYear:
		return "year(" + n + ") = ?", []any{p.Year}, true
	case xmlamodel.PartQuarter:
		return "year(" + n + ") = ? AND quarter(" + n + ") = ?", []any{p.Year, p.Quarter}, true
	case xmlamodel.PartMonth:
		return "year(" + n + ") = ? AND month(" + n + ") = ?", []any{p.Year, p.Month}, true
	}
	return "CAST(" + n + " AS DATE) = CAST(? AS DATE)", []any{m.Key}, true
}

func isCalendar(l *xmlamodel.Level) bool {
	return l.Part >= xmlamodel.PartYear
}

// Members streams the members of a level in order, a null last, with the count of their children.
func (d *duckData) Members(ctx context.Context, cube *xmlamodel.Cube, l *xmlamodel.Level, under *xmlamodel.Member, fn func(xmlamodel.Member, int) error) error {
	t := d.tables[cube.Resource]
	if t == nil {
		return fmt.Errorf("no table for %s", cube.Resource)
	}
	dim := l.Hierarchy.Dimension
	n := norm(dim)
	if !isCalendar(l) {
		if under != nil || l.Part != xmlamodel.PartValue {
			return nil // a value has no members below it
		}
		rows, err := t.Query(ctx, "SELECT CAST(g AS VARCHAR) FROM (SELECT DISTINCT "+n+" AS g FROM serve) ORDER BY g NULLS LAST")
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var k sql.NullString
			if err := rows.Scan(&k); err != nil {
				return err
			}
			m := xmlamodel.Member{Level: l, Kind: xmlamodel.MemberKey, Key: k.String}
			if !k.Valid {
				m.Kind = xmlamodel.MemberNull
			}
			if err := fn(m, 0); err != nil {
				return err
			}
		}
		return rows.Err()
	}
	cal := calendarOf(l, n)
	where, args := "", []any(nil)
	if l.Number > 1 {
		where = n + " IS NOT NULL"
	}
	if under != nil {
		if under.Level.Number >= l.Number || under.Level.Hierarchy != l.Hierarchy || under.Kind != xmlamodel.MemberKey {
			return nil
		}
		cond, a, ok := within(*under, n)
		if !ok {
			return nil
		}
		if where != "" {
			where += " AND "
		}
		where, args = where+cond, a
	}
	sel := make([]string, len(cal.groups))
	copy(sel, cal.groups)
	if l.Part == xmlamodel.PartDay {
		sel[0] = "CAST(" + sel[0] + " AS VARCHAR)"
	}
	count := "0"
	if cal.child != "" {
		count = "count(DISTINCT " + cal.child + ")"
	}
	query := "SELECT " + strings.Join(sel, ", ") + ", " + count + " FROM serve"
	if where != "" {
		query += " WHERE " + where
	}
	query += " GROUP BY " + strings.Join(cal.groups, ", ") + " ORDER BY " + strings.Join(cal.groups, ", ") + " NULLS LAST"
	rows, err := t.Query(ctx, query, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var children int
		m := xmlamodel.Member{Level: l, Kind: xmlamodel.MemberKey}
		switch l.Part {
		case xmlamodel.PartYear:
			var y sql.NullInt64
			if err := rows.Scan(&y, &children); err != nil {
				return err
			}
			if !y.Valid {
				m.Kind = xmlamodel.MemberNull
			} else {
				m.Key = xmlamodel.Parts{Year: int(y.Int64)}.Key(l.Part)
			}
		case xmlamodel.PartQuarter, xmlamodel.PartMonth:
			var y, x sql.NullInt64
			if err := rows.Scan(&y, &x, &children); err != nil {
				return err
			}
			p := xmlamodel.Parts{Year: int(y.Int64), Quarter: int(x.Int64), Month: int(x.Int64)}
			m.Key = p.Key(l.Part)
		default:
			var k sql.NullString
			if err := rows.Scan(&k, &children); err != nil {
				return err
			}
			m.Key = k.String
		}
		if err := fn(m, children); err != nil {
			return err
		}
	}
	return rows.Err()
}

// Member looks a member up: the All member always exists; the others when a row has them.
func (d *duckData) Member(ctx context.Context, cube *xmlamodel.Cube, m xmlamodel.Member) (int, bool, error) {
	t := d.tables[cube.Resource]
	if t == nil {
		return 0, false, fmt.Errorf("no table for %s", cube.Resource)
	}
	h := m.Hierarchy()
	n := norm(h.Dimension)
	one := func(query string, args ...any) (int, int, error) {
		rows, err := t.Query(ctx, query, args...)
		if err != nil {
			return 0, 0, err
		}
		defer rows.Close()
		var a, b int
		if rows.Next() {
			if err := rows.Scan(&a, &b); err != nil {
				return 0, 0, err
			}
		}
		return a, b, rows.Err()
	}
	switch m.Kind {
	case xmlamodel.MemberAll:
		first := n
		if isCalendar(h.Levels[1]) {
			first = "year(" + n + ")"
		}
		c, _, err := one("SELECT count(DISTINCT " + first + ") + CASE WHEN count(*) > count(" + n + ") THEN 1 ELSE 0 END, 0 FROM serve")
		return c, true, err
	case xmlamodel.MemberNull:
		found, _, err := one("SELECT CAST(EXISTS(SELECT 1 FROM serve WHERE " + n + " IS NULL) AS INTEGER), 0")
		return 0, found == 1, err
	}
	if isCalendar(m.Level) {
		cond, args, ok := within(m, n)
		if !ok {
			return 0, false, nil
		}
		child := calendarOf(m.Level, n).child
		if child == "" {
			found, _, err := one("SELECT CAST(EXISTS(SELECT 1 FROM serve WHERE "+cond+") AS INTEGER), 0", args...)
			return 0, found == 1, err
		}
		rows, children, err := one("SELECT count(*), count(DISTINCT "+child+") FROM serve WHERE "+cond, args...)
		return children, rows > 0, err
	}
	// by the column's text, the way Execute compares a key (xmlamodel.KeyComparedAsText); a plain
	// text column needs no cast
	cond := "CAST(" + n + " AS VARCHAR) = ?"
	if c, ok := t.Column(h.Dimension.Column); ok && c.Type == "VARCHAR" && xmlamodel.KeyComparedAsText(h.Dimension.Kind) {
		cond = n + " = ?"
	}
	found, _, err := one("SELECT CAST(EXISTS(SELECT 1 FROM serve WHERE "+cond+") AS INTEGER), 0", m.Key)
	return 0, found == 1, err
}
