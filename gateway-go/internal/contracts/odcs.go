package contracts

import (
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// services/odcs.py: the data contract in the open ODCS format (v3.0.2), export only.

const odcsVersion = "v3.0.2"

var (
	logical = map[string]string{"integer": "integer", "number": "number", "string": "string", "boolean": "boolean",
		"date": "date", "datetime": "date"}
	dimension = map[string]string{
		"column": "conformity", "not_null": "completeness", "unique": "uniqueness", "accepted_values": "conformity",
		"range": "conformity", "pattern": "conformity", "row_count": "completeness", "freshness": "timeliness",
		"expression": "consistency",
	}
	famInt    = regexp.MustCompile(`^U?Int`)
	famNumber = regexp.MustCompile(`^(Float|Decimal)`)
	famString = regexp.MustCompile(`^(String|Utf8|Categorical|Enum)`)
)

// ErrNotExportable is an exception the Python code does not catch (a KeyError, a TypeError on a
// document the form check would not have let through): a 500.
var ErrNotExportable = errors.New("the contract cannot be written as ODCS")

// dict is a Python dict built in place: its keys keep the order they were added in, and the
// values that are dicts or lists are shared, as Python's references are.
type dict struct {
	keys []string
	vals map[string]any
}

func newDict(kv ...any) *dict {
	d := &dict{vals: map[string]any{}}
	for i := 0; i+1 < len(kv); i += 2 {
		d.set(kv[i].(string), kv[i+1])
	}
	return d
}

func (d *dict) set(k string, v any) {
	if _, ok := d.vals[k]; !ok {
		d.keys = append(d.keys, k)
	}
	d.vals[k] = v
}

func (d *dict) get(k string) (any, bool) { v, ok := d.vals[k]; return v, ok }

// list is a Python list appended to in place.
type list struct{ items []any }

func (d *dict) setdefaultList(k string) *list {
	if v, ok := d.vals[k].(*list); ok {
		return v
	}
	l := &list{}
	d.set(k, l)
	return l
}

func (d *dict) setdefaultDict(k string) *dict {
	if v, ok := d.vals[k].(*dict); ok {
		return v
	}
	n := newDict()
	d.set(k, n)
	return n
}

// family is _family: a physical type's family ("" = None). A dtype that is not a text is
// re.match on it: a TypeError.
func family(dtype any) (string, error) {
	d := ""
	if pyd.Truthy(dtype) {
		s, ok := dtype.(string)
		if !ok {
			return "", ErrNotExportable
		}
		d = s
	}
	switch {
	case famInt.MatchString(d):
		return "integer", nil
	case famNumber.MatchString(d):
		return "number", nil
	case d == "Boolean":
		return "boolean", nil
	case d == "Date":
		return "date", nil
	case strings.HasPrefix(d, "Datetime"):
		return "datetime", nil
	case famString.MatchString(d):
		return "string", nil
	}
	return "", nil
}

func ident(name string) string { return `"` + strings.ReplaceAll(name, `"`, `""`) + `"` }

// str is f"{v}" / str(v) of a json.loads value.
func str(v any) string {
	if b, ok := v.(*big.Int); ok {
		return b.String()
	}
	return pystr.Str(toPyjson(v))
}

// literal is _literal.
func literal(v any) string {
	switch x := v.(type) {
	case bool:
		if x {
			return "TRUE"
		}
		return "FALSE"
	case int64, *big.Int, float64:
		return str(x)
	}
	return "'" + strings.ReplaceAll(str(v), "'", "''") + "'"
}

// PyjsonOf turns a json.loads value (pyd's) into pyjson's, its big integers too.
func PyjsonOf(v any) any { return toPyjson(v) }

func toPyjson(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		out := pyjson.Object{}
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			out = append(out, pyjson.KV{Key: k, Value: toPyjson(e)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = toPyjson(e)
		}
		return out
	case *big.Int:
		return pyjson.Int(x.String())
	}
	return v
}

// check is _check: a `quality` item, with its severity and the rule's id and kind.
func check(rule *pyd.Object, kv ...any) *dict {
	d := newDict(kv...)
	kind, _ := rule.Get("kind")
	sev, _ := rule.Get("severity")
	id, has := rule.Get("id")
	if !has {
		id = ""
	}
	d.set("dimension", dimension[kind.(string)])
	d.set("severity", sev)
	d.set("customProperties", []any{newDict("property", "tabulariaRuleId", "value", id),
		newDict("property", "tabulariaRuleKind", "value", kind)})
	return d
}

func zeroRows(rule *pyd.Object, name, where, description string) *dict {
	return check(rule, "type", "sql", "name", name, "description", description,
		"query", "SELECT COUNT(*) FROM ${table} WHERE "+where, "mustBe", int64(0), "unit", "rows")
}

// propKey keeps apart the column names Python's dict keeps apart (5 and "5").
func propKey(v any) string { return fmt.Sprintf("%T:%s", v, str(v)) }

// Input is what to_odcs is given.
type Input struct {
	DatasourceID       int64
	Name, Description  string
	Columns            []any
	ColumnDescriptions *pyd.Object
	Document           *pyd.Object
	Version            int64
	Enabled            bool
	CreatedAt          *time.Time
}

// ToODCS is to_odcs: the ODCS document, in the order it must be written.
func ToODCS(in Input) (any, error) {
	var rules []*pyd.Object
	if rv, ok := in.Document.Get("rules"); ok {
		items, isList := rv.([]any)
		if !isList {
			return nil, ErrNotExportable
		}
		for _, x := range items {
			if r, ok := x.(*pyd.Object); ok {
				rules = append(rules, r)
			}
		}
	}
	// the `column` rules first (a stable sort on kind != "column")
	var first, rest []*pyd.Object
	for _, r := range rules {
		k, ok := r.Get("kind")
		if !ok {
			return nil, ErrNotExportable
		}
		if k == "column" {
			first = append(first, r)
		} else {
			rest = append(rest, r)
		}
	}
	rules = append(first, rest...)

	props := newDict()
	describe := func(name any) (any, error) {
		if in.ColumnDescriptions == nil {
			return nil, nil
		}
		s, ok := name.(string)
		if !ok {
			switch name.(type) {
			case []any, *pyd.Object:
				return nil, ErrNotExportable // unhashable
			}
			return nil, nil
		}
		v, _ := in.ColumnDescriptions.Get(s)
		return v, nil
	}
	for _, x := range in.Columns {
		c, ok := x.(*pyd.Object)
		if !ok {
			continue
		}
		name, _ := c.Get("name")
		if !pyd.Truthy(name) {
			continue
		}
		p := newDict("name", name)
		dtype, _ := c.Get("dtype")
		fam, err := family(dtype)
		if err != nil {
			return nil, err
		}
		if fam != "" {
			p.set("logicalType", logical[fam])
		}
		if pyd.Truthy(dtype) {
			p.set("physicalType", dtype)
		}
		desc, err := describe(name)
		if err != nil {
			return nil, err
		}
		if pyd.Truthy(desc) {
			p.set("description", desc)
		}
		props.set(propKey(name), p)
	}
	prop := func(col any) *dict {
		k := propKey(col)
		if v, ok := props.get(k); ok {
			return v.(*dict)
		}
		p := newDict("name", col)
		props.set(k, p)
		return p
	}

	var tableQuality, sla []any
	for _, r := range rules {
		get := func(k string) any { v, _ := r.Get(k); return v }
		kind, _ := get("kind").(string)
		blocking := get("severity") == "error"
		col := get("column")
		switch kind {
		case "column":
			p := prop(col)
			dtype := get("dtype")
			if pyd.Truthy(dtype) {
				ds, _ := dtype.(string)
				l, ok := logical[ds]
				if !ok {
					return nil, ErrNotExportable
				}
				p.set("logicalType", l)
			}
			promise, tail := "present", "."
			if pyd.Truthy(dtype) {
				promise, tail = "of type "+str(dtype), " and its type is "+str(dtype)+"."
			}
			p.setdefaultList("quality").items = append(p.setdefaultList("quality").items, check(r, "type", "text",
				"name", str(col)+" is "+promise, "description", "The column "+str(col)+" exists"+tail))
		case "not_null":
			p := prop(col)
			if blocking {
				p.set("required", true)
			}
			q := p.setdefaultList("quality")
			q.items = append(q.items, check(r, "type", "library", "rule", "nullValues", "name", str(col)+" is never null",
				"mustBe", int64(0), "unit", "rows"))
		case "unique":
			cols, _ := get("columns").([]any)
			if !pyd.Truthy(get("columns")) {
				cols = nil
			} else if cols == nil {
				return nil, ErrNotExportable
			}
			if len(cols) == 1 {
				p := prop(cols[0])
				if blocking {
					p.set("unique", true)
				}
				q := p.setdefaultList("quality")
				q.items = append(q.items, check(r, "type", "library", "rule", "duplicateCount", "name", str(cols[0])+" is unique",
					"mustBe", int64(0), "unit", "rows"))
			} else {
				names, quoted := make([]string, len(cols)), make([]string, len(cols))
				for i, c := range cols {
					s, ok := c.(string)
					if !ok {
						return nil, ErrNotExportable // ", ".join of a non-text
					}
					names[i], quoted[i] = s, ident(s)
				}
				listing := strings.Join(quoted, ", ")
				tableQuality = append(tableQuality, check(r, "type", "sql", "name", "unique combination of "+strings.Join(names, ", "),
					"description", "No two rows share the same values of "+strings.Join(names, ", ")+" (nulls count as values).",
					"query", "SELECT COUNT(*) FROM (SELECT "+listing+" FROM ${table} GROUP BY "+listing+" HAVING COUNT(*) > 1) AS duplicated",
					"mustBe", int64(0), "unit", "rows"))
			}
		case "accepted_values":
			vals, _ := get("values").([]any)
			lits, words := make([]string, len(vals)), make([]string, len(vals))
			for i, v := range vals {
				lits[i], words[i] = literal(v), str(v)
			}
			c, ok := col.(string)
			if !ok {
				return nil, ErrNotExportable
			}
			p := prop(col)
			q := p.setdefaultList("quality")
			q.items = append(q.items, zeroRows(r, c+" has only accepted values",
				ident(c)+" IS NOT NULL AND "+ident(c)+" NOT IN ("+strings.Join(lits, ", ")+")",
				"Every non-null value is one of: "+strings.Join(words, ", ")+"."))
		case "range":
			c, ok := col.(string)
			if !ok {
				return nil, ErrNotExportable
			}
			p, lo, hi := prop(col), get("min"), get("max")
			lt, _ := p.get("logicalType")
			numeric := lt == "integer" || lt == "number"
			allOf := func(test func(any) bool) bool { return (lo == nil || test(lo)) && (hi == nil || test(hi)) }
			isStr := func(v any) bool { _, ok := v.(string); return ok }
			if (numeric && allOf(isNumber)) || (lt == "date" && allOf(isStr)) {
				options := p.setdefaultDict("logicalTypeOptions")
				if lo != nil {
					options.set("minimum", lo)
				}
				if hi != nil {
					options.set("maximum", hi)
				}
			}
			var outside, limits []string
			if lo != nil {
				outside = append(outside, ident(c)+" < "+literal(lo))
				limits = append(limits, "at least "+str(lo))
			}
			if hi != nil {
				outside = append(outside, ident(c)+" > "+literal(hi))
				limits = append(limits, "at most "+str(hi))
			}
			q := p.setdefaultList("quality")
			q.items = append(q.items, zeroRows(r, c+" is in range", strings.Join(outside, " OR "),
				"Every non-null value is "+strings.Join(limits, " and ")+"."))
		case "pattern":
			p := prop(col)
			re, has := r.Get("regex")
			if !has {
				return nil, ErrNotExportable
			}
			if lt, _ := p.get("logicalType"); lt == "string" {
				p.setdefaultDict("logicalTypeOptions").set("pattern", re)
			}
			q := p.setdefaultList("quality")
			q.items = append(q.items, check(r, "type", "text", "name", str(col)+" matches the pattern",
				"description", "Every non-null value matches the regular expression "+str(re)+" in full."))
		case "row_count":
			lo, hi := get("min"), get("max")
			kv := []any{"type", "library", "rule", "rowCount", "name", "row count"}
			switch {
			case lo != nil && hi != nil:
				kv = append(kv, "mustBeBetween", []any{lo, hi})
			case lo != nil:
				kv = append(kv, "mustBeGreaterOrEqualTo", lo)
			default:
				kv = append(kv, "mustBeLessOrEqualTo", hi)
			}
			tableQuality = append(tableQuality, check(r, append(kv, "unit", "rows")...))
		case "freshness":
			hours, has := r.Get("max_age_hours")
			if !has {
				return nil, ErrNotExportable
			}
			item := newDict("property", "latency", "value", hours, "unit", "h")
			subject := "the last update of the data"
			if pyd.Truthy(col) {
				item.set("element", in.Name+"."+str(col))
				subject = "the newest value of " + str(col)
			}
			sla = append(sla, item)
			tableQuality = append(tableQuality, check(r, "type", "text", "name", "freshness",
				"description", "At most "+str(hours)+" hours have passed since "+subject+"."))
		case "expression":
			sql, has := r.Get("sql")
			if !has {
				return nil, ErrNotExportable
			}
			name := get("name")
			if !pyd.Truthy(name) {
				name = "condition"
			}
			tableQuality = append(tableQuality, check(r, "type", "sql", "name", name,
				"description", "The condition is true on every row; a null result counts as a violation. SQL dialect: Polars SQL.",
				"query", "SELECT COUNT(*) FROM ${table} WHERE NOT COALESCE(("+str(sql)+"), FALSE)", "mustBe", int64(0), "unit", "rows"))
		}
	}

	obj := newDict("name", in.Name, "logicalType", "object", "physicalType", "table")
	if in.Description != "" {
		obj.set("description", in.Description)
	}
	properties := make([]any, 0, len(props.keys))
	for _, k := range props.keys {
		properties = append(properties, props.vals[k])
	}
	obj.set("properties", properties)
	if len(tableQuality) > 0 {
		obj.set("quality", tableQuality)
	}
	status := "draft"
	if in.Enabled {
		status = "active"
	}
	out := newDict("apiVersion", odcsVersion, "kind", "DataContract", "id", fmt.Sprintf("urn:tabularia:datasource:%d", in.DatasourceID),
		"name", in.Name, "version", fmt.Sprintf("%d.0.0", in.Version), "status", status)
	text, _ := in.Document.Get("description")
	if !pyd.Truthy(text) {
		text = in.Description
	}
	if pyd.Truthy(text) {
		out.set("description", newDict("purpose", text))
	}
	out.set("schema", []any{obj})
	if len(sla) > 0 {
		out.set("slaProperties", sla)
	}
	out.set("customProperties", []any{newDict("property", "tabulariaDatasourceId", "value", in.DatasourceID),
		newDict("property", "tabulariaContractVersion", "value", in.Version)})
	if in.CreatedAt != nil {
		out.set("contractCreatedTs", in.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	}
	return out, nil
}

// ── YAML ─────────────────────────────────────────────────────────────────────

// toEscape is _TO_ESCAPE: what YAML would not read as it is inside a double-quoted string.
var toEscape = regexp.MustCompile(`[\x{7f}-\x{9f}\x{2028}\x{2029}\x{feff}]`)

// scalar is _scalar.
func scalar(v any) (string, error) {
	switch x := v.(type) {
	case nil:
		return "null", nil
	case bool:
		if x {
			return "true", nil
		}
		return "false", nil
	case int64:
		return str(x), nil
	case *big.Int:
		return x.String(), nil
	case float64:
		switch {
		case math.IsNaN(x):
			return "nan", nil
		case math.IsInf(x, 1):
			return "inf", nil
		case math.IsInf(x, -1):
			return "-inf", nil
		}
		return pyjson.FloatRepr(x), nil
	}
	s, err := pyjson.Dumps(str(v), pyjson.Options{})
	if err != nil {
		return "", err
	}
	return toEscape.ReplaceAllStringFunc(s, func(m string) string { return fmt.Sprintf("\\u%04x", []rune(m)[0]) }), nil
}

func isScalar(v any) bool {
	switch v.(type) {
	case *dict, *list, []any, *pyd.Object:
		return false
	}
	return true
}

// elements is a list's items (a *list, a []any, a json.loads list).
func elements(v any) []any {
	switch x := v.(type) {
	case *list:
		return x.items
	case []any:
		return x
	}
	return nil
}

// asDict turns a json.loads dict (a value of the document) into a dict.
func asDict(v any) (*dict, bool) {
	switch x := v.(type) {
	case *dict:
		return x, true
	case *pyd.Object:
		d := newDict()
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			d.set(k, e)
		}
		return d, true
	}
	return nil, false
}

func empty(v any) bool {
	if d, ok := asDict(v); ok {
		return len(d.keys) == 0
	}
	return len(elements(v)) == 0
}

func lines(v any, indent int) ([]string, error) {
	pad := strings.Repeat(" ", indent)
	var out []string
	if d, ok := asDict(v); ok {
		for _, k := range d.keys {
			x := d.vals[k]
			switch {
			case isScalar(x):
				s, err := scalar(x)
				if err != nil {
					return nil, err
				}
				out = append(out, pad+k+": "+s)
			case empty(x):
				if _, isDict := asDict(x); isDict {
					out = append(out, pad+k+": {}")
				} else {
					out = append(out, pad+k+": []")
				}
			case allScalars(x):
				parts := []string{}
				for _, e := range elements(x) {
					s, err := scalar(e)
					if err != nil {
						return nil, err
					}
					parts = append(parts, s)
				}
				out = append(out, pad+k+": ["+strings.Join(parts, ", ")+"]")
			default:
				out = append(out, pad+k+":")
				more, err := lines(x, indent+2)
				if err != nil {
					return nil, err
				}
				out = append(out, more...)
			}
		}
		return out, nil
	}
	for _, x := range elements(v) {
		if isScalar(x) {
			s, err := scalar(x)
			if err != nil {
				return nil, err
			}
			out = append(out, pad+"- "+s)
			continue
		}
		body, err := lines(x, indent+2)
		if err != nil {
			return nil, err
		}
		if len(body) == 0 {
			return nil, ErrNotExportable // body[0] of an empty one: an IndexError
		}
		out = append(out, pad+"- "+strings.TrimLeft(body[0], " "))
		out = append(out, body[1:]...)
	}
	return out, nil
}

// allScalars is `isinstance(x, list) and all(_is_scalar(i) for i in x)`.
func allScalars(v any) bool {
	if _, isDict := asDict(v); isDict {
		return false
	}
	for _, e := range elements(v) {
		if !isScalar(e) {
			return false
		}
	}
	return true
}

// DumpYAML is dump_yaml.
func DumpYAML(document any) (string, error) {
	l, err := lines(document, 0)
	if err != nil {
		return "", err
	}
	return strings.Join(l, "\n") + "\n", nil
}
