package pod

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The most values of a `col__in=` filter, and of a cursor's text.
const (
	MaxInValues   = dataapiwire.MaxInValues
	MaxCursorText = 32 << 10
	MaxSortKeys   = dataapiwire.MaxSortKeys
)

// the operators as the query string writes them: col=, col__in=, col__gte=…
var suffixes = map[string]string{"in": "in", "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "null": "null"}

var sqlOperators = map[string]string{"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

// Problem is an answer other than the data: a status, a sentence and a code.
type Problem struct {
	Status int
	Detail string
	Code   string
	// seconds, for a 429 and a 503
	RetryAfter int
}

func (p *Problem) Error() string { return p.Detail }

func badRequest(code, format string, args ...any) *Problem {
	return &Problem{Status: 400, Detail: fmt.Sprintf(format, args...), Code: code}
}

// Cond is one filter: a column, an operator and its values, bound.
type Cond struct {
	Column Column
	Op     string
	Params []Param
	IsNull bool // for «null»
}

// SortKey is a column of the order, descending or not.
type SortKey struct {
	Column Column
	Desc   bool
}

// Query is a request read and checked against the resource and its prepared table.
type Query struct {
	Conds   []Cond
	Columns []Column
	Sort    []SortKey
	Limit   int
	Count   bool
	Format  string
	// the raw parameters the query was read from (without _cursor and _format): what a cursor
	// carries to ask the same again
	Raw map[string]string
	// where the page starts, from a cursor
	After *Position
}

// Position is where a page ends: the row's place in the file and, for an order of its own,
// the row's values of the sort columns (as a request writes them; nil for null).
type Position struct {
	Row    int64     `json:"r"`
	Values []*string `json:"v,omitempty"`
}

// cursorText is the cursor as it travels: the generation of the data, the query, the position
type cursorText struct {
	Generation string            `json:"g"`
	Raw        map[string]string `json:"q"`
	At         Position          `json:"p"`
}

// EncodeCursor writes the cursor that asks for the page after `at`.
func EncodeCursor(generation string, raw map[string]string, at Position) string {
	b, _ := json.Marshal(cursorText{Generation: generation, Raw: raw, At: at})
	return base64.RawURLEncoding.EncodeToString(b)
}

// ParseQuery reads a request's query string for a resource.
func ParseQuery(rawQuery string, res *dataapiwire.Resource, t *Table) (*Query, *Problem) {
	values, err := url.ParseQuery(rawQuery)
	if err != nil {
		return nil, badRequest("bad_query", "The query string cannot be read: %v", err)
	}
	for name, list := range values {
		for _, v := range list {
			if strings.HasPrefix(v, dataapiwire.KeyPrefix) {
				return nil, &Problem{Status: 400, Code: "key_in_query",
					Detail: "Send the key in the Authorization header (Bearer) or in X-API-Key, never in the address: revoke this one, it was exposed"}
			}
		}
		if len(list) > 1 {
			return nil, badRequest("repeated_parameter", "«%s» is given %d times: give it once", name, len(list))
		}
	}
	raw := map[string]string{}
	for name, list := range values {
		raw[name] = list[0]
	}
	format := "json"
	if f, ok := raw["_format"]; ok {
		if f != "json" && f != "csv" {
			return nil, badRequest("bad_format", "_format is json or csv")
		}
		format = f
		delete(raw, "_format")
	}
	var after *Position
	if c, ok := raw["_cursor"]; ok {
		if len(raw) > 1 {
			return nil, badRequest("cursor_alone", "With _cursor give nothing else but _format: the cursor carries the query")
		}
		cur, problem := decodeCursor(c, t)
		if problem != nil {
			return nil, problem
		}
		raw, after = cur.Raw, &cur.At
		if raw == nil {
			raw = map[string]string{}
		}
		delete(raw, "_cursor")
		delete(raw, "_format")
	}
	q, problem := readQuery(raw, res, t)
	if problem != nil {
		return nil, problem
	}
	q.Format = format
	if after != nil {
		if !q.FileOrdered(t) && len(after.Values) != len(q.Sort) {
			return nil, badRequest("bad_cursor", "The cursor cannot be read: ask the first page again")
		}
		q.After = after
	}
	return q, nil
}

func decodeCursor(text string, t *Table) (*cursorText, *Problem) {
	bad := badRequest("bad_cursor", "The cursor cannot be read: ask the first page again")
	if len(text) > MaxCursorText {
		return nil, bad
	}
	b, err := base64.RawURLEncoding.DecodeString(text)
	if err != nil {
		return nil, bad
	}
	var c cursorText
	if err := json.Unmarshal(b, &c); err != nil || c.At.Row < 0 {
		return nil, bad
	}
	if c.Generation != t.Generation {
		return nil, &Problem{Status: 409, Code: "data_changed",
			Detail: "The data changed since this cursor was given: ask the first page again, so a page never mixes two versions"}
	}
	return &c, nil
}

// filterOf finds the filter a parameter names: the column itself (equal), or the column and
// an operator after «__» (a column whose own name has «__» is found first)
func filterOf(name string, res *dataapiwire.Resource) (dataapiwire.Filter, string, bool) {
	find := func(col string) (dataapiwire.Filter, bool) {
		for _, f := range res.Filters {
			if f.Column == col {
				return f, true
			}
		}
		return dataapiwire.Filter{}, false
	}
	if f, ok := find(name); ok {
		return f, "eq", true
	}
	if i := strings.LastIndex(name, "__"); i > 0 {
		if op, ok := suffixes[name[i+2:]]; ok {
			if f, ok := find(name[:i]); ok {
				return f, op, true
			}
		}
	}
	return dataapiwire.Filter{}, "", false
}

func filterNames(res *dataapiwire.Resource) string {
	if len(res.Filters) == 0 {
		return "this resource has none"
	}
	names := []string{}
	for _, f := range res.Filters {
		names = append(names, f.Column)
	}
	return "the columns one may filter on are " + strings.Join(names, ", ")
}

func readQuery(raw map[string]string, res *dataapiwire.Resource, t *Table) (*Query, *Problem) {
	q := &Query{Raw: raw, Limit: res.MaxPage}
	names := make([]string, 0, len(raw))
	for name := range raw {
		names = append(names, name)
	}
	sort.Strings(names) // the same request reads the same, and the first problem is the same
	sortText, sortGiven := "", false
	for _, name := range names {
		value := raw[name]
		if strings.HasPrefix(name, "_") {
			switch name {
			case "_columns":
				cols, problem := readColumns(value, res, t)
				if problem != nil {
					return nil, problem
				}
				q.Columns = cols
			case "_sort":
				sortText, sortGiven = value, true
			case "_limit":
				n, err := strconv.Atoi(value)
				if err != nil || n < 1 || n > res.MaxPage {
					return nil, badRequest("bad_limit", "_limit is a whole number from 1 to %d", res.MaxPage)
				}
				q.Limit = n
			case "_count":
				switch value {
				case "true":
					q.Count = true
				case "false":
				default:
					return nil, badRequest("bad_count", "_count is true or false")
				}
			default:
				return nil, badRequest("unknown_parameter",
					"«%s» is not a parameter: _columns, _sort, _limit, _cursor, _count and _format are, and the filters", name)
			}
			continue
		}
		f, op, ok := filterOf(name, res)
		if !ok {
			return nil, badRequest("not_filterable", "«%s» is not a filter: %s", name, filterNames(res))
		}
		if !slices.Contains(f.Operators, op) {
			return nil, badRequest("operator_not_allowed", "«%s» cannot be filtered with «%s»: it allows %s", f.Column, op,
				strings.Join(f.Operators, ", "))
		}
		col, ok := t.Column(f.Column)
		if !ok || !col.Comparable() {
			return nil, badRequest("not_filterable", "«%s» cannot be filtered on", f.Column)
		}
		cond, problem := readCond(col, op, value)
		if problem != nil {
			return nil, problem
		}
		q.Conds = append(q.Conds, cond)
	}
	if q.Columns == nil {
		// the file's columns that the definition still publishes: a file prepared before a
		// column was taken out may be served until the new one is ready, never that column
		q.Columns = []Column{}
		for _, c := range t.Columns {
			if slices.Contains(res.Columns, c.Name) {
				q.Columns = append(q.Columns, c)
			}
		}
	}
	if !sortGiven {
		sortText = strings.Join(res.DefaultSort, ",")
	}
	keys, problem := readSort(sortText, res, t)
	if problem != nil {
		return nil, problem
	}
	q.Sort = keys
	return q, nil
}

func readCond(col Column, op, value string) (Cond, *Problem) {
	cond := Cond{Column: col, Op: op}
	switch op {
	case "null":
		switch value {
		case "true":
			cond.IsNull = true
		case "false":
		default:
			return cond, badRequest("bad_value", "%s__null is true or false", col.Name)
		}
		return cond, nil
	case "in":
		parts := strings.Split(value, ",")
		if len(parts) > MaxInValues {
			return cond, badRequest("too_many_values", "%s__in takes at most %d values", col.Name, MaxInValues)
		}
		for _, part := range parts {
			p, err := col.ParseValue(part)
			if err != nil {
				return cond, badRequest("bad_value", "%s", err.Error())
			}
			cond.Params = append(cond.Params, p)
		}
		return cond, nil
	}
	if op != "eq" && !col.Ordered() {
		return cond, badRequest("operator_not_allowed", "«%s» cannot be compared with «%s»: only numbers and dates can", col.Name, op)
	}
	p, err := col.ParseValue(value)
	if err != nil {
		return cond, badRequest("bad_value", "%s", err.Error())
	}
	cond.Params = []Param{p}
	return cond, nil
}

func readColumns(value string, res *dataapiwire.Resource, t *Table) ([]Column, *Problem) {
	out := []Column{}
	seen := map[string]bool{}
	for _, name := range strings.Split(value, ",") {
		name = strings.TrimSpace(name)
		col, ok := t.Column(name)
		if !ok || !slices.Contains(res.Columns, name) {
			return nil, badRequest("unknown_column", "«%s» is not a column of this resource: %s", name, strings.Join(res.Columns, ", "))
		}
		if !seen[name] {
			seen[name] = true
			out = append(out, col)
		}
	}
	return out, nil
}

func readSort(value string, res *dataapiwire.Resource, t *Table) ([]SortKey, *Problem) {
	if strings.TrimSpace(value) == "" {
		return nil, nil
	}
	keys := []SortKey{}
	seen := map[string]bool{}
	for _, part := range strings.Split(value, ",") {
		part = strings.TrimSpace(part)
		desc := strings.HasPrefix(part, "-")
		name := strings.TrimPrefix(part, "-")
		ok := false
		for _, f := range res.Filters {
			ok = ok || f.Column == name
		}
		col, found := t.Column(name)
		if !ok || !found || !col.Comparable() {
			return nil, badRequest("not_sortable", "«%s» cannot order the rows: %s", name, strings.Replace(filterNames(res),
				"filter on", "order by", 1))
		}
		if seen[name] {
			return nil, badRequest("not_sortable", "_sort names «%s» twice", name)
		}
		seen[name] = true
		keys = append(keys, SortKey{Column: col, Desc: desc})
	}
	if len(keys) > MaxSortKeys {
		return nil, badRequest("not_sortable", "_sort takes at most %d columns", MaxSortKeys)
	}
	return keys, nil
}

// FileOrdered says whether the rows go in the file's own order (by the first filterable
// column, then as they were written): no sort asked, or exactly that one ascending.
func (q *Query) FileOrdered(t *Table) bool {
	return len(q.Sort) == 0 || (len(q.Sort) == 1 && !q.Sort[0].Desc && q.Sort[0].Column.Name == t.FileOrder)
}

// where writes the filters, with their placeholders, and the arguments bound to them.
func (q *Query) where() (string, []any) {
	parts := []string{}
	args := []any{}
	for _, c := range q.Conds {
		col := ident(c.Column.Name)
		switch c.Op {
		case "null":
			if c.IsNull {
				parts = append(parts, col+" IS NULL")
			} else {
				parts = append(parts, col+" IS NOT NULL")
			}
		case "in":
			holders := make([]string, len(c.Params))
			for i, p := range c.Params {
				holders[i] = p.Placeholder
				args = append(args, p.Value)
			}
			parts = append(parts, col+" IN ("+strings.Join(holders, ", ")+")")
		default:
			parts = append(parts, col+" "+sqlOperators[c.Op]+" "+c.Params[0].Placeholder)
			args = append(args, c.Params[0].Value)
		}
	}
	return strings.Join(parts, " AND "), args
}

// PageSQL is the page's query: the row's place, the sort columns, then the columns asked;
// one row more than the page, to know whether another follows. Every value is a placeholder.
func (q *Query) PageSQL(t *Table) (string, []any, error) {
	where, args := q.where()
	conds := []string{}
	if where != "" {
		conds = append(conds, where)
	}
	sel := []string{"rowid"}
	for _, k := range q.Sort {
		sel = append(sel, ident(k.Column.Name))
	}
	for _, c := range q.Columns {
		sel = append(sel, ident(c.Name))
	}
	order := ""
	if q.FileOrdered(t) {
		if q.After != nil {
			conds = append(conds, "rowid > ?")
			args = append(args, q.After.Row)
		}
		// the file's order is rowid's, and without an ORDER BY the rows come out as they were
		// written (preserve_insertion_order, set when the file is opened): the scan stops as
		// soon as the page is full, where an ORDER BY rowid would cost a third more on a lookup
	} else {
		if q.After != nil {
			cond, more, err := q.keyset()
			if err != nil {
				return "", nil, err
			}
			conds = append(conds, cond)
			args = append(args, more...)
		}
		keys := []string{}
		for _, k := range q.Sort {
			dir := " ASC"
			if k.Desc {
				dir = " DESC"
			}
			keys = append(keys, ident(k.Column.Name)+dir+" NULLS LAST")
		}
		order = " ORDER BY " + strings.Join(keys, ", ") + ", rowid"
	}
	text := "SELECT " + strings.Join(sel, ", ") + " FROM serve"
	if len(conds) > 0 {
		text += " WHERE " + strings.Join(conds, " AND ")
	}
	text += order + " LIMIT ?"
	args = append(args, int64(q.Limit+1))
	return text, args, nil
}

// keyset is «after the cursor's row» in an order of the request's own: for each sort column
// in turn, the rows equal on the ones before and past it on this one, then the same values
// and a later row. Nulls come last, either way.
func (q *Query) keyset() (string, []any, error) {
	ors := []string{}
	args := []any{}
	equal := []string{}
	eqArgs := []any{}
	for i, k := range q.Sort {
		col := ident(k.Column.Name)
		v := q.After.Values[i]
		var p Param
		if v != nil {
			var err error
			if p, err = k.Column.ParseValue(*v); err != nil {
				return "", nil, badRequest("bad_cursor", "The cursor cannot be read: ask the first page again")
			}
			op := ">"
			if k.Desc {
				op = "<"
			}
			ors = append(ors, "("+strings.Join(append(slices.Clone(equal), "("+col+" "+op+" "+p.Placeholder+" OR "+col+" IS NULL)"), " AND ")+")")
			args = append(args, eqArgs...)
			args = append(args, p.Value)
			equal = append(equal, col+" = "+p.Placeholder)
			eqArgs = append(eqArgs, p.Value)
		} else {
			// past a null there is nothing but more nulls, ordered by what follows
			equal = append(equal, col+" IS NULL")
		}
	}
	ors = append(ors, "("+strings.Join(append(slices.Clone(equal), "rowid > ?"), " AND ")+")")
	args = append(args, eqArgs...)
	args = append(args, q.After.Row)
	return "(" + strings.Join(ors, " OR ") + ")", args, nil
}

// CountSQL counts the rows the filters match.
func (q *Query) CountSQL() (string, []any) {
	where, args := q.where()
	text := "SELECT count(*) FROM serve"
	if where != "" {
		text += " WHERE " + where
	}
	return text, args
}
