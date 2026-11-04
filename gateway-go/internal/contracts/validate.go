// Package contracts is services/contracts.py's form of a data contract (validate_document) and
// services/odcs.py, its export in the ODCS format. The gateway evaluates nothing: the engine
// does; here the document is checked and written.
package contracts

import (
	"context"
	"math"
	"math/big"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const (
	maxRules  = 200 // MAX_RULES
	maxValues = 500 // MAX_VALUES
)

var (
	severities = []string{"warning", "error"}
	dtypes     = []string{"integer", "number", "string", "boolean", "date", "datetime"}
	// fields is _FIELDS: what each kind of rule carries besides id, kind, severity
	fields = map[string][]string{
		"column":          {"column", "dtype"},
		"not_null":        {"column"},
		"unique":          {"columns"},
		"accepted_values": {"column", "values"},
		"range":           {"column", "min", "max"},
		"pattern":         {"column", "regex"},
		"row_count":       {"min", "max"},
		"freshness":       {"max_age_hours", "column"},
		"expression":      {"sql", "name"},
	}
)

// oneOf is `v in (…)` for a tuple of texts: equal to one of them.
func oneOf(v any, set []string) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	for _, x := range set {
		if s == x {
			return true
		}
	}
	return false
}

func isNumber(v any) bool {
	switch v.(type) {
	case int64, *big.Int, float64:
		return true
	}
	return false
}

func isInt(v any) bool {
	switch v.(type) {
	case int64, *big.Int:
		return true
	}
	return false
}

// greater is `a > b` of two Python numbers (NaN compares false).
func greater(a, b any) bool { return cmp(a, b) > 0 }

// cmp is -1, 0, 1, or 2 when the two cannot be ordered (a NaN).
func cmp(a, b any) int {
	fa, aFloat := a.(float64)
	fb, bFloat := b.(float64)
	if (aFloat && math.IsNaN(fa)) || (bFloat && math.IsNaN(fb)) {
		return 2
	}
	if aFloat && math.IsInf(fa, 0) || bFloat && math.IsInf(fb, 0) {
		x, y := toFloat(a), toFloat(b)
		switch {
		case x < y:
			return -1
		case x > y:
			return 1
		}
		return 0
	}
	return toRat(a).Cmp(toRat(b))
}

func toFloat(v any) float64 {
	switch x := v.(type) {
	case int64:
		return float64(x)
	case *big.Int:
		f, _ := new(big.Float).SetInt(x).Float64()
		return f
	case float64:
		return x
	}
	return 0
}

func toRat(v any) *big.Rat {
	switch x := v.(type) {
	case int64:
		return new(big.Rat).SetInt64(x)
	case *big.Int:
		return new(big.Rat).SetInt(x)
	case float64:
		return new(big.Rat).SetFloat64(x)
	}
	return new(big.Rat)
}

func nonBlank(v any) bool {
	s, ok := v.(string)
	return ok && pyd.Strip(s) != ""
}

// checkRule is _check_rule: the code of the rule's first defect, "" when it is well written.
// `compiles` is re.compile's verdict on a pattern.
func checkRule(r *pyd.Object, compiles func(string) bool) string {
	get := func(k string) any { v, _ := r.Get(k); return v }
	kind := get("kind").(string)
	switch kind {
	case "column", "not_null", "accepted_values", "range", "pattern":
		if !nonBlank(get("column")) {
			return "missing_column"
		}
	}
	if kind == "column" && get("dtype") != nil && !oneOf(get("dtype"), dtypes) {
		return "bad_dtype"
	}
	if kind == "unique" {
		cols, ok := get("columns").([]any)
		if !ok || len(cols) == 0 {
			return "missing_column"
		}
		for _, c := range cols {
			if !nonBlank(c) {
				return "missing_column"
			}
		}
	}
	if kind == "accepted_values" {
		vals, ok := get("values").([]any)
		if !ok || len(vals) == 0 {
			return "empty_values"
		}
		if len(vals) > maxValues {
			return "too_many_values"
		}
		for _, v := range vals {
			switch v.(type) {
			case string, int64, *big.Int, float64, bool:
			default:
				return "bad_values"
			}
		}
	}
	if kind == "range" || kind == "row_count" {
		lo, hi := get("min"), get("max")
		if lo == nil && hi == nil {
			return "no_bounds"
		}
		for _, v := range []any{lo, hi} {
			if v == nil {
				continue
			}
			if kind == "row_count" {
				if !isInt(v) || cmp(v, int64(0)) < 0 {
					return "bad_bounds"
				}
			} else if _, isStr := v.(string); !isNumber(v) && !isStr {
				return "bad_bounds"
			}
		}
		if isNumber(lo) && isNumber(hi) && greater(lo, hi) {
			return "bad_bounds"
		}
	}
	if kind == "pattern" {
		re, ok := get("regex").(string)
		if !ok || re == "" || pyd.Len(re) > 500 {
			return "bad_regex"
		}
		if !compiles(re) {
			return "bad_regex"
		}
	}
	if kind == "freshness" {
		h := get("max_age_hours")
		if !isNumber(h) || cmp(h, int64(0)) != 1 {
			return "bad_max_age"
		}
		if c := get("column"); c != nil && !nonBlank(c) {
			return "missing_column"
		}
	}
	if kind == "expression" {
		sql, ok := get("sql").(string)
		if !ok || pyd.Strip(sql) == "" || pyd.Len(sql) > 2000 {
			return "empty_expression"
		}
	}
	return ""
}

// Failure is one entry of ContractInvalid.errors: {"index", "code"}.
func failure(index any, code string) pyjson.Object {
	return pyjson.Object{{Key: "index", Value: index}, {Key: "code", Value: code}}
}

// Invalid is ContractInvalid: where and why, with codes the interface translates.
type Invalid struct{ Errors []pyjson.Object }

func (e *Invalid) Error() string { return strconv.Itoa(len(e.Errors)) + " errors in the contract" }

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// validate is validate_document with re.compile's verdicts given.
func validate(doc *pyd.Object, compiles func(string) bool) (*pyd.Object, *Invalid) {
	rulesV, has := doc.Get("rules")
	if !has {
		rulesV = []any{}
	}
	rulesIn, ok := rulesV.([]any)
	if !ok {
		return nil, &Invalid{[]pyjson.Object{failure(nil, "not_a_contract")}}
	}
	if len(rulesIn) > maxRules {
		return nil, &Invalid{[]pyjson.Object{failure(nil, "too_many_rules")}}
	}
	var errs []pyjson.Object
	rules := []any{}
	used := map[string]bool{}
	for i, x := range rulesIn {
		raw, ok := x.(*pyd.Object)
		kind, _ := func() (any, bool) {
			if !ok {
				return nil, false
			}
			return raw.Get("kind")
		}()
		if !ok || !oneOf(kind, kindsOrder) {
			errs = append(errs, failure(int64(i), "unknown_kind"))
			continue
		}
		if sev, _ := raw.Get("severity"); !oneOf(sev, severities) {
			errs = append(errs, failure(int64(i), "bad_severity"))
			continue
		}
		sev, _ := raw.Get("severity")
		r := pyd.NewObject()
		r.Set("kind", kind)
		r.Set("severity", sev)
		get := func(k string) any { v, _ := raw.Get(k); return v }
		if kind == "unique" && !pyd.Truthy(get("columns")) && pyd.Truthy(get("column")) {
			short := pyd.NewObject() // {**raw, "columns": [raw["column"]]}
			for _, k := range raw.Keys() {
				short.Set(k, get(k))
			}
			short.Set("columns", []any{get("column")})
			raw = short
		}
		for _, f := range fields[kind.(string)] {
			if v, _ := raw.Get(f); v != nil && v != "" {
				r.Set(f, v)
			}
		}
		if code := checkRule(r, compiles); code != "" {
			errs = append(errs, failure(int64(i), code))
			continue
		}
		rid := ""
		if id, ok := get("id").(string); ok && pyd.Len(id) > 0 && pyd.Len(id) <= 40 {
			rid = id
		}
		if rid == "" || used[rid] {
			n := len(rules) + 1
			for used["r"+strconv.Itoa(n)] {
				n++
			}
			rid = "r" + strconv.Itoa(n)
		}
		used[rid] = true
		rule := pyd.NewObject()
		rule.Set("id", rid)
		for _, k := range r.Keys() {
			v, _ := r.Get(k)
			rule.Set(k, v)
		}
		rules = append(rules, rule)
	}
	if len(errs) > 0 {
		return nil, &Invalid{errs}
	}
	description := ""
	if s, ok := doc.Get("description"); ok {
		if t, isStr := s.(string); isStr {
			description = t
		}
	}
	out := pyd.NewObject()
	out.Set("description", truncate(description, 2000))
	out.Set("rules", rules)
	return out, nil
}

var kindsOrder = []string{"column", "not_null", "unique", "accepted_values", "range", "pattern", "row_count", "freshness", "expression"}

// Compiler says, for each pattern, whether Python's re.compile takes it (aisvc.Client.Compiles).
type Compiler func(ctx context.Context, patterns []string) ([]bool, error)

// ValidateDocument is validate_document: the contract in canonical form (only the known
// fields, unique ids), or *Invalid. The patterns of the «pattern» rules are asked of Python
// (`compile`), all in one call, only when there are any.
func ValidateDocument(ctx context.Context, doc *pyd.Object, compile Compiler) (*pyd.Object, error) {
	var asked []string
	seen := map[string]bool{}
	_, _ = validate(doc, func(p string) bool {
		if !seen[p] {
			seen[p] = true
			asked = append(asked, p)
		}
		return true
	})
	verdict := map[string]bool{}
	if len(asked) > 0 {
		answers, err := compile(ctx, asked)
		if err != nil {
			return nil, err
		}
		for i, p := range asked {
			verdict[p] = answers[i]
		}
	}
	out, invalid := validate(doc, func(p string) bool { return verdict[p] })
	if invalid != nil {
		return nil, invalid
	}
	return out, nil
}
