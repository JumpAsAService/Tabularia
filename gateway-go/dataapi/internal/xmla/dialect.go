package xmla

// The SQL of Execute is written through a Dialect (docs/specs/xmla.md, «Execution»): what changes
// from one engine to another is here and nowhere else, so that a second engine is a second
// implementation of this interface and not a second interpreter. DuckDB is the only one.

import (
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// ParamType is the type a bound value is read as where it is compared with a column.
type ParamType int

const (
	ParamDate ParamType = iota
	ParamTimestamp
)

// Dialect writes the pieces of SQL that differ between engines. Expressions are given as text and
// returned as text: a name of a column comes from the model and is quoted by Quote; a value of
// the MDX is never written in the text, it is bound (Placeholder).
type Dialect interface {
	// Quote writes an identifier.
	Quote(name string) string
	// Placeholder is the marker of the n-th (1-based) bound value of a statement.
	Placeholder(n int) string
	// date parts of a date or timestamp, as integers
	Year(x string) string
	Quarter(x string) string
	Month(x string) string
	// Date reads x as a date.
	Date(x string) string
	// UTC reads a timestamp with a time zone as the plain timestamp it is in UTC.
	UTC(x string) string
	// Text writes x as text, the way the key of a member is written.
	Text(x string) string
	// ZeroPad writes an integer as text of at least `width` characters, padded with zeros.
	ZeroPad(x string, width int) string
	Concat(parts ...string) string
	Coalesce(x, otherwise string) string
	// Literal writes a constant text of ours (a key marker): never a value of the data.
	Literal(s string) string
	// CastParam reads a bound value (already a placeholder) as a date or a timestamp.
	CastParam(t ParamType, placeholder string) string
	// Aggregate writes the aggregation of a measure over a column ("" for the row count). A count
	// of nothing is empty (null), not zero: a cell with no row is empty in a pivot.
	Aggregate(a xmlamodel.Agg, column string) string
	CountDistinct(x string) string
	// Float reads x as a floating point number, for comparing and ordering.
	Float(x string) string
	// Grouping is the bitmask that says which of the columns a grouping set leaves out: the first
	// column is the highest bit, a bit is 1 for a column that is not grouped.
	Grouping(cols []string) string
	// GroupBy writes the clause that groups by each of the sets of columns in one pass.
	GroupBy(sets [][]string) string
	Ascending(x string) string // an ordering term, a null last
	Limit(n int) string
	CreateTable(name, query string) string
	DropTable(name string) string
}

// DuckDB is the dialect of the pods' engine.
type DuckDB struct{}

func (DuckDB) Quote(name string) string      { return `"` + strings.ReplaceAll(name, `"`, `""`) + `"` }
func (DuckDB) Placeholder(n int) string      { return "?" }
func (DuckDB) Year(x string) string          { return "year(" + x + ")" }
func (DuckDB) Quarter(x string) string       { return "quarter(" + x + ")" }
func (DuckDB) Month(x string) string         { return "month(" + x + ")" }
func (DuckDB) Date(x string) string          { return "CAST(" + x + " AS DATE)" }
func (DuckDB) UTC(x string) string           { return "timezone('UTC', " + x + ")" }
func (DuckDB) Text(x string) string          { return "CAST(" + x + " AS VARCHAR)" }
func (DuckDB) Concat(parts ...string) string { return "(" + strings.Join(parts, " || ") + ")" }
func (DuckDB) Literal(s string) string       { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
func (d DuckDB) ZeroPad(x string, width int) string {
	return "lpad(" + d.Text(x) + ", " + strconv.Itoa(width) + ", '0')"
}
func (DuckDB) Coalesce(x, otherwise string) string { return "COALESCE(" + x + ", " + otherwise + ")" }
func (DuckDB) CastParam(t ParamType, ph string) string {
	if t == ParamDate {
		return "CAST(" + ph + " AS DATE)"
	}
	return "CAST(" + ph + " AS TIMESTAMP)"
}
func (DuckDB) Aggregate(a xmlamodel.Agg, column string) string {
	switch a {
	case xmlamodel.AggSum:
		return "sum(" + column + ")"
	case xmlamodel.AggAverage:
		return "avg(" + column + ")"
	case xmlamodel.AggMin:
		return "min(" + column + ")"
	case xmlamodel.AggMax:
		return "max(" + column + ")"
	}
	return "NULLIF(count(*), 0)"
}
func (DuckDB) CountDistinct(x string) string { return "count(DISTINCT " + x + ")" }
func (DuckDB) Float(x string) string         { return "CAST(" + x + " AS DOUBLE)" }
func (DuckDB) Grouping(cols []string) string { return "GROUPING(" + strings.Join(cols, ", ") + ")" }
func (DuckDB) GroupBy(sets [][]string) string {
	parts := make([]string, len(sets))
	for i, s := range sets {
		parts[i] = "(" + strings.Join(s, ", ") + ")"
	}
	return "GROUP BY GROUPING SETS (" + strings.Join(parts, ", ") + ")"
}
func (DuckDB) Ascending(x string) string { return x + " ASC NULLS LAST" }
func (DuckDB) Limit(n int) string        { return "LIMIT " + strconv.Itoa(n) }
func (DuckDB) CreateTable(name, query string) string {
	return "CREATE TEMP TABLE " + name + " AS " + query
}
func (DuckDB) DropTable(name string) string { return "DROP TABLE IF EXISTS " + name }
