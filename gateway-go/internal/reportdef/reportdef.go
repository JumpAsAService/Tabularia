// Package reportdef is services/report_definition.py: what a report's definition (`v: 1`) may
// hold, checked at every save. The structure is closed (an unknown key is refused by name), the
// keys and bounds are the engine's, lengths are millimetres with a tenth at most and every item
// lies on the page. A problem says the item, the path inside the definition and what is wrong,
// in the words of the Python code, which the messages copy.
package reportdef

import (
	"errors"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
)

const (
	MaxDefinitionBytes = 4 * 1024 * 1024
	MaxImageBytes      = 512 * 1024
	MaxParameters      = 50
	MaxDatasets        = 20
	MaxItems           = 400
	MaxColumns         = 60
	MaxGroups          = 6
	MaxSeries          = 12
	MaxBandItems       = 200
	MaxBandHeight      = 500
	MaxMatrixWidth     = 500
	MaxName            = 256
	MaxSQLChars        = 100_000
	MaxExpr            = 4000
	MaxGroupName       = 100
	MaxCommentInstr    = 1000
	MaxCommentReads    = 10
	MinCommentWidth    = 40
)

// PageSizes are PAGE_SIZES, in their order.
var pageSizeNames = []string{"A4", "A3", "A5", "Letter", "Legal"}
var pageSizes = map[string][2]float64{"A4": {210, 297}, "A3": {297, 420}, "A5": {148, 210}, "Letter": {215.9, 279.4},
	"Legal": {215.9, 355.6}}

// KnownEngines are engine_policy.KNOWN_ENGINES.
var KnownEngines = []any{"polars", "duckdb", "chdb", "clickhouse", "bigquery"}

var (
	locales        = []any{"en", "it", "de", "es", "fr"}
	fieldTypes     = []any{"string", "integer", "float", "date", "datetime", "time", "boolean", "other"}
	filterOps      = []any{"=", "<>", "<", "<=", ">", ">=", "in", "contains"}
	chartKinds     = []any{"bar", "column", "line", "area", "pie", "donut", "combo", "waterfall", "funnel", "treemap", "heatmap", "scatter", "gauge", "bullet"}
	itemTypes      = []any{"text", "image", "line", "rect", "table", "chart", "kpi", "comment"}
	commentedTypes = []string{"kpi", "chart", "table"}
	pageItemTypes  = []any{"text", "image", "line", "rect"}
	// re.match with `$`: a final newline passes, as in Python
	DatasetName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}\n?$`)
	ItemID      = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}\n?$`)
	color       = regexp.MustCompile(`^#[0-9a-fA-F]{6}\n?$`)
	itemPath    = regexp.MustCompile(`^sections\.(?:page_header|body|page_footer)\.items(?:\[[0-9]+\])?\.?(.*)$`)
	listPath    = regexp.MustCompile(`^(datasets|parameters)\[([0-9]+)\](.*)$`)
	paramRef    = regexp.MustCompile(`^\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
)

// Problem is what is wrong, and where: the path inside the definition and the item (its id).
type Problem struct {
	Message string
	Where   string
	Item    *string
}

func (p *Problem) Error() string { return p.Message }

// ErrInternal is what makes the Python code fail on its own (a TypeError): a 500.
var ErrInternal = errors.New("the Python code fails on this definition")

func problem(message, where string, item *string) error {
	return &Problem{Message: message, Where: where, Item: item}
}

// AsProblem returns the Problem in err, if it is one.
func AsProblem(err error) (*Problem, bool) {
	var p *Problem
	ok := errors.As(err, &p)
	return p, ok
}

// Dict is a problem as the engine says its own (Problem.as_dict).
func (p *Problem) Dict(definition any) pyjson.Object {
	field := p.Where
	if p.Item != nil {
		if m := itemPath.FindStringSubmatch(field); m != nil {
			field = m[1]
		}
	}
	var item any
	if p.Item != nil {
		item = *p.Item
	}
	return pyjson.Object{{Key: "item", Value: item}, {Key: "field", Value: ByName(field, definition)}, {Key: "pos", Value: nil},
		{Key: "length", Value: int64(0)}, {Key: "message", Value: p.Message}}
}

// ByName is by_name: a dataset's or a parameter's path by its name (`datasets[orders].source`),
// by its place when the name is not one, or two have it.
func ByName(field string, definition any) string {
	m := listPath.FindStringSubmatch(field)
	def, ok := definition.(pyjson.Object)
	if m == nil || !ok {
		return field
	}
	listV, _ := def.Get(m[1])
	list, ok := listV.([]any)
	if !ok {
		return field
	}
	names := make([]any, len(list))
	for i, e := range list {
		if o, ok := e.(pyjson.Object); ok {
			names[i], _ = o.Get("name")
		}
	}
	k, err := strconv.Atoi(m[2])
	if err != nil || k >= len(names) {
		return field
	}
	name, ok := names[k].(string)
	if !ok || !DatasetName.MatchString(name) {
		return field
	}
	count := 0
	for _, n := range names {
		if pystr.Eq(n, name) {
			count++
		}
	}
	if count == 1 {
		return m[1] + "[" + name + "]" + m[3]
	}
	return field
}

// Refusal is refusal: the 422's detail of a definition with problems: the first one's message,
// item and place, and every problem (`problems`, and `errors`, the same list).
func Refusal(problems []pyjson.Object, where *string) pyjson.Object {
	first := problems[0]
	msg, _ := first.Get("message")
	field, _ := first.Get("field")
	item, _ := first.Get("item")
	var w any = field
	if where != nil {
		w = *where
	}
	list := make([]any, len(problems))
	for i, p := range problems {
		list[i] = p
	}
	return pyjson.Object{{Key: "message", Value: msg}, {Key: "where", Value: w}, {Key: "item", Value: item},
		{Key: "errors", Value: list}, {Key: "problems", Value: list}}
}

// ── small checks ──────────────────────────────────────────────────────────────

func whatOr(what, where string) string {
	if what != "" {
		return what
	}
	return where
}

func get(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

func has(o pyjson.Object, k string) bool {
	_, ok := o.Get(k)
	return ok
}

// keys is _keys.
func keys(value any, where string, allowed []string, required []string, what string, item *string) (pyjson.Object, error) {
	o, ok := value.(pyjson.Object)
	if !ok {
		return nil, problem(whatOr(what, where)+" is an object", where, item)
	}
	allow := map[string]bool{}
	for _, a := range allowed {
		allow[a] = true
	}
	var unknown []string
	for _, kv := range o {
		if !allow[kv.Key] {
			unknown = append(unknown, kv.Key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, problem(fmt.Sprintf("%s: unknown key «%s»", whatOr(what, where), pystr.Cut(unknown[0], 40)), where, item)
	}
	for _, k := range required {
		v := get(o, k)
		if v == nil || v == "" {
			return nil, problem(fmt.Sprintf("%s: «%s» is missing", whatOr(what, where), k), where+"."+k, item)
		}
	}
	return o, nil
}

// G is `{x:g}`.
func G(x float64) string { return strconv.FormatFloat(x, 'g', 6, 64) }

// F1 is `{x:.1f}`.
func F1(x float64) string { return strconv.FormatFloat(x, 'f', 1, 64) }

// number is _number.
func number(value any, where, what string, lo, hi float64, item *string) (float64, error) {
	v, ok := pystr.Number(value)
	if !ok || math.IsInf(v, 0) || math.IsNaN(v) {
		return 0, problem(what+" is a number", where, item)
	}
	if !(lo <= v && v <= hi) {
		return 0, problem(fmt.Sprintf("%s is between %s and %s", what, G(lo), G(hi)), where, item)
	}
	return v, nil
}

// mm is _mm: a length in millimetres, with a tenth at most.
func mm(value any, where, what string, lo, hi float64, item *string) (float64, error) {
	v, err := number(value, where, what, lo, hi, item)
	if err != nil {
		return 0, err
	}
	if math.Abs(v*10-math.RoundToEven(v*10)) > 1e-6 {
		return 0, problem(fmt.Sprintf("%s: millimetres with a tenth at most (%s)", what, G(v)), where, item)
	}
	return v, nil
}

// boolean is _bool.
func boolean(value any, where, what string, item *string) error {
	if value != nil {
		if _, ok := value.(bool); !ok {
			return problem(what+" is true or false", where, item)
		}
	}
	return nil
}

// text is _text.
func text(value any, where, what string, limit int, item *string, required bool) error {
	if value == nil && !required {
		return nil
	}
	s, ok := value.(string)
	if !ok || (required && pystr.Strip(s) == "") {
		msg := what + " is a text"
		if required {
			msg += " that cannot be empty"
		}
		return problem(msg, where, item)
	}
	if pystr.Len(s) > limit {
		return problem(fmt.Sprintf("%s is at most %d characters", what, limit), where, item)
	}
	return nil
}

func expr(value any, where, what string, item *string, required bool) error {
	return text(value, where, what, MaxExpr, item, required)
}

func joinOptions(options []any) string {
	parts := make([]string, len(options))
	for i, o := range options {
		parts[i] = pystr.Str(o)
	}
	return strings.Join(parts, ", ")
}

// choice is _choice.
func choice(value any, options []any, where, what string, item *string, required bool) error {
	if value == nil && !required {
		return nil
	}
	if !pystr.In(value, options...) {
		return problem(fmt.Sprintf("%s is one of %s", what, joinOptions(options)), where, item)
	}
	return nil
}

// colour is _color.
func colour(value any, where, what string, item *string) error {
	if value == nil {
		return nil
	}
	if s, ok := value.(string); !ok || !color.MatchString(s) {
		return problem(what+" is a colour as #rrggbb", where, item)
	}
	return nil
}

var (
	weights      = []any{"normal", "semibold", "bold", int64(400), int64(600), int64(700)}
	styleKeys    = []string{"size", "weight", "italic", "color", "background", "align", "valign", "padding", "border", "radius", "format", "can_grow"}
	optionsKeys  = []string{"stacked", "labels", "legend", "sort", "top", "min", "max", "target", "bands", "format"}
	alignments   = []any{"left", "center", "right"}
	valignments  = []any{"top", "middle", "bottom"}
	sortChoices  = []any{"none", "asc", "desc"}
	directions   = []any{"asc", "desc"}
	pageBreaks   = []any{"none", "between", "after"}
	fits         = []any{"contain", "cover", "stretch"}
	seriesAs     = []any{"bar", "line"}
	axes         = []any{"left", "right"}
	orientations = []any{"portrait", "landscape"}
)

// style is _style.
func style(value any, where, what string, item *string) error {
	if value == nil {
		return nil
	}
	st, err := keys(value, where, styleKeys, nil, what, item)
	if err != nil {
		return err
	}
	if v := get(st, "size"); v != nil {
		if _, err := number(v, where+".size", what+"'s size (points)", 4, 96, item); err != nil {
			return err
		}
	}
	if w := get(st, "weight"); w != nil {
		if _, isBool := w.(bool); isBool || !pystr.In(w, weights...) {
			return problem(what+"'s weight is normal, semibold or bold", where+".weight", item)
		}
	}
	checks := []func() error{
		func() error { return boolean(get(st, "italic"), where+".italic", what+"'s italic", item) },
		func() error { return colour(get(st, "color"), where+".color", what+"'s colour", item) },
		func() error { return colour(get(st, "background"), where+".background", what+"'s background", item) },
		func() error {
			return choice(get(st, "align"), alignments, where+".align", what+"'s alignment", item, false)
		},
		func() error {
			return choice(get(st, "valign"), valignments, where+".valign", what+"'s vertical alignment", item, false)
		},
	}
	for _, c := range checks {
		if err := c(); err != nil {
			return err
		}
	}
	for _, key := range []string{"padding", "radius"} {
		if v := get(st, key); v != nil {
			if _, err := number(v, where+"."+key, what+"'s "+key, 0, 50, item); err != nil {
				return err
			}
		}
	}
	if b := get(st, "border"); b != nil {
		border, err := keys(b, where+".border", []string{"width", "color"}, nil, what+"'s border", item)
		if err != nil {
			return err
		}
		if v := get(border, "width"); v != nil {
			if _, err := number(v, where+".border.width", what+"'s border width", 0, 5, item); err != nil {
				return err
			}
		}
		if err := colour(get(border, "color"), where+".border.color", what+"'s border colour", item); err != nil {
			return err
		}
	}
	if err := text(get(st, "format"), where+".format", what+"'s format", 64, item, false); err != nil {
		return err
	}
	if has(st, "can_grow") {
		if _, ok := get(st, "can_grow").(bool); !ok {
			return problem(what+"'s can_grow is true or false", where+".can_grow", item)
		}
	}
	return nil
}

// options is _options: a chart's.
func options(value any, where, what string, item *string) error {
	if value == nil {
		return nil
	}
	o, err := keys(value, where, optionsKeys, nil, what, item)
	if err != nil {
		return err
	}
	for _, flag := range []string{"stacked", "labels"} {
		if has(o, flag) {
			if _, ok := get(o, flag).(bool); !ok {
				return problem(what+"."+flag+" is true or false", where+"."+flag, item)
			}
		}
	}
	if err := boolean(get(o, "legend"), where+".legend", what+".legend", item); err != nil {
		return err
	}
	if has(o, "sort") {
		if err := choice(get(o, "sort"), sortChoices, where+".sort", what+".sort", item, true); err != nil {
			return err
		}
	}
	if top := get(o, "top"); top != nil {
		n, _ := pystr.Number(top)
		if !pystr.IsInt(top) || !(1 <= n && n <= 500) {
			return problem(what+".top is a whole number between 1 and 500", where+".top", item)
		}
	}
	for _, key := range []string{"min", "max"} {
		if v := get(o, key); v != nil {
			if _, err := number(v, where+"."+key, what+"."+key, -1e300, 1e300, item); err != nil {
				return err
			}
		}
	}
	target := get(o, "target")
	if _, ok := target.(string); ok {
		if err := expr(target, where+".target", what+".target", item, false); err != nil {
			return err
		}
	} else if target != nil {
		if _, err := number(target, where+".target", what+".target", -1e300, 1e300, item); err != nil {
			return err
		}
	}
	if has(o, "bands") {
		bands, ok := get(o, "bands").([]any)
		if !ok || len(bands) > 10 {
			return problem(what+".bands are a list of at most 10 numbers", where+".bands", item)
		}
		for k, b := range bands {
			if _, err := number(b, fmt.Sprintf("%s.bands[%d]", where, k), what+".bands", -1e300, 1e300, item); err != nil {
				return err
			}
		}
	}
	return text(get(o, "format"), where+".format", what+".format", 64, item, false)
}

// ── the definition ────────────────────────────────────────────────────────────

// PageBox is page_box: the width and height between the margins, in mm.
func PageBox(page pyjson.Object) (float64, float64) {
	size, ok := page.Get("size")
	if !ok {
		size = "A4"
	}
	var w, h float64
	if s, ok := size.(pyjson.Object); ok {
		w, _ = pystr.Number(get(s, "w"))
		h, _ = pystr.Number(get(s, "h"))
	} else {
		wh := pageSizes[size.(string)]
		w, h = wh[0], wh[1]
	}
	if get(page, "orientation") == "landscape" {
		w, h = math.Max(w, h), math.Min(w, h)
	}
	margins := []float64{15, 12, 15, 12}
	if m, ok := get(page, "margins").([]any); ok && len(m) == 4 {
		for i := range margins {
			margins[i], _ = pystr.Number(m[i])
		}
	}
	return w - margins[3] - margins[1], h - margins[0] - margins[2]
}

// Page is _page: the width and height the margins leave.
func Page(value any) (float64, float64, error) {
	page, err := keys(value, "page", []string{"size", "orientation", "margins"}, nil, "The page", nil)
	if err != nil {
		return 0, 0, err
	}
	size, ok := page.Get("size")
	if !ok {
		size = "A4"
	}
	if s, ok := size.(pyjson.Object); ok {
		if _, err := keys(s, "page.size", []string{"w", "h"}, []string{"w", "h"}, "The page's size", nil); err != nil {
			return 0, 0, err
		}
		if _, err := mm(get(s, "w"), "page.size.w", "The page's width", 50, 2000, nil); err != nil {
			return 0, 0, err
		}
		if _, err := mm(get(s, "h"), "page.size.h", "The page's height", 50, 2000, nil); err != nil {
			return 0, 0, err
		}
	} else {
		if !pystr.Hashable(size) {
			return 0, 0, ErrInternal // `size not in PAGE_SIZES` of a list: unhashable
		}
		name, ok := size.(string)
		if _, known := pageSizes[name]; !ok || !known {
			return 0, 0, problem("The page's size is one of "+strings.Join(pageSizeNames, ", ")+", or {w, h} in mm", "page.size", nil)
		}
	}
	if err := choice(get(page, "orientation"), orientations, "page.orientation", "The orientation", nil, false); err != nil {
		return 0, 0, err
	}
	if m := get(page, "margins"); m != nil {
		list, ok := m.([]any)
		if !ok || len(list) != 4 {
			return 0, 0, problem("The margins are four lengths: top, right, bottom, left", "page.margins", nil)
		}
		for i, x := range list {
			if _, err := mm(x, fmt.Sprintf("page.margins[%d]", i), "A margin", 0, 100, nil); err != nil {
				return 0, 0, err
			}
		}
	}
	w, h := PageBox(page)
	if w < 20 || h < 20 {
		return 0, 0, problem("The margins leave less than 2 cm of page", "page.margins", nil)
	}
	return w, h, nil
}

// Parameter is _parameter.
func Parameter(value any, i int, names map[string]bool) (pyjson.Object, error) {
	where := fmt.Sprintf("parameters[%d]", i)
	p, err := keys(value, where, []string{"name", "label", "type", "multi", "required", "default", "choices"},
		[]string{"name", "type"}, "A parameter", nil)
	if err != nil {
		return nil, err
	}
	name, ok := get(p, "name").(string)
	if !ok || !reportparams.Name.MatchString(name) {
		return nil, problem("A parameter's name has letters, digits and _, and starts with a letter", where+".name", nil)
	}
	if reportparams.Reserved[name] {
		return nil, problem(fmt.Sprintf("«%s» is a name of Tabularia's placeholders: give the parameter another", name), where+".name", nil)
	}
	if names[name] {
		return nil, problem(fmt.Sprintf("Two parameters are called «%s»", name), where+".name", nil)
	}
	what := fmt.Sprintf("The parameter «%s»", name)
	if err := text(get(p, "label"), where+".label", what+"'s label", 200, nil, false); err != nil {
		return nil, err
	}
	if err := choice(get(p, "type"), reportparams.Types, where+".type", what+"'s type", nil, true); err != nil {
		return nil, err
	}
	if err := boolean(get(p, "multi"), where+".multi", what+"'s multi", nil); err != nil {
		return nil, err
	}
	if err := boolean(get(p, "required"), where+".required", what+"'s required", nil); err != nil {
		return nil, err
	}
	multi := pystr.Truthy(get(p, "multi"))
	conv := func(v any, at string) (any, error) {
		if _, ok := reportparams.PlaceholderOf(v); ok {
			return nil, nil // resolved at every render, in the report's zone
		}
		switch v.(type) {
		case pyjson.Object, []any:
			return nil, problem(fmt.Sprintf("%s: a value is a %s", what, pystr.Str(get(p, "type"))), at, nil)
		}
		var t string
		switch x := v.(type) {
		case string:
			t = x
		case bool:
			t = strings.ToLower(pystr.Str(x))
		default:
			t, _ = pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
		}
		c, bad := reportparams.Convert(p, t)
		if bad != nil {
			return nil, problem(bad.Error(), at, nil)
		}
		return c, nil
	}
	var defaults []any
	if d := get(p, "default"); d != nil {
		list, isList := d.([]any)
		if multi != isList {
			kind := "one value"
			if multi {
				kind = "a list of values"
			}
			return nil, problem(fmt.Sprintf("%s's default is %s", what, kind), where+".default", nil)
		}
		if !multi {
			list = []any{d}
		}
		for _, x := range list {
			c, err := conv(x, where+".default")
			if err != nil {
				return nil, err
			}
			defaults = append(defaults, c)
		}
	}
	if ch := get(p, "choices"); ch != nil {
		at := where + ".choices"
		choices, ok := ch.(pyjson.Object)
		if !ok {
			return nil, problem(what+"'s choices are {values} or {dataset, value, label}", at, nil)
		}
		if has(choices, "values") {
			if _, err := keys(choices, at, []string{"values"}, nil, what+"'s choices", nil); err != nil {
				return nil, err
			}
			fixed, ok := get(choices, "values").([]any)
			if !ok || len(fixed) < 1 || len(fixed) > 1000 {
				return nil, problem(what+"'s choices are a list of 1 to 1000 values", at, nil)
			}
			allowed := make([]any, len(fixed))
			for k, v := range fixed {
				c, err := conv(v, fmt.Sprintf("%s.values[%d]", at, k))
				if err != nil {
					return nil, err
				}
				allowed[k] = c
			}
			for _, d := range defaults {
				if d != nil && !pystr.In(d, allowed...) {
					return nil, problem(what+"'s default is not one of its choices", where+".default", nil)
				}
			}
		} else {
			if _, err := keys(choices, at, []string{"dataset", "value", "label"}, []string{"dataset", "value"}, what+"'s choices", nil); err != nil {
				return nil, err
			}
			if err := expr(get(choices, "value"), at+".value", what+"'s choice value", nil, true); err != nil {
				return nil, err
			}
			if err := expr(get(choices, "label"), at+".label", what+"'s choice label", nil, false); err != nil {
				return nil, err
			}
		}
	}
	names[name] = true
	return p, nil
}

func filters(value any, where, what string, parameters map[string]pyjson.Object) error {
	if value == nil {
		return nil
	}
	list, ok := value.([]any)
	if !ok || len(list) > 20 {
		return problem(what+"'s filters are a list of at most 20", where, nil)
	}
	for k, fv := range list {
		at := fmt.Sprintf("%s[%d]", where, k)
		f, err := keys(fv, at, []string{"column", "op", "value"}, []string{"column", "op"}, what+"'s filter", nil)
		if err != nil {
			return err
		}
		if err := text(get(f, "column"), at+".column", what+"'s filter column", 300, nil, true); err != nil {
			return err
		}
		if err := choice(get(f, "op"), filterOps, at+".op", what+"'s filter operator", nil, true); err != nil {
			return err
		}
		v := get(f, "value")
		isIn := pystr.Eq(get(f, "op"), "in")
		var ref []string
		if s, ok := v.(string); ok {
			ref = paramRef.FindStringSubmatch(pystr.Strip(s))
		}
		vl, isList := v.([]any)
		_, isDict := v.(pyjson.Object)
		switch {
		case ref != nil && !reportparams.Builtins[ref[1]]:
			p, ok := parameters[ref[1]]
			if !ok {
				return problem(fmt.Sprintf("%s's filter: the report has no parameter «%s»", what, ref[1]), at+".value", nil)
			}
			multi := pystr.Truthy(get(p, "multi"))
			if multi != isIn {
				why := "is one value: in takes a list"
				if multi {
					why = "holds a list: use the operator in"
				}
				return problem(fmt.Sprintf("%s's filter: «%s» %s", what, ref[1], why), at+".op", nil)
			}
		case isIn && !(isList && len(vl) >= 1 && len(vl) <= 1000):
			return problem(what+"'s filter: in takes a list of values", at+".value", nil)
		case (isDict || isList) && !isIn:
			return problem(what+"'s filter takes one value", at+".value", nil)
		}
		if isList {
			for _, x := range vl {
				switch x.(type) {
				case pyjson.Object, []any:
					return problem(what+"'s filter takes simple values", at+".value", nil)
				}
			}
		}
	}
	return nil
}

func ident(src pyjson.Object, key string) bool { return pystr.IsInt(get(src, key)) }

// Dataset is _dataset.
func Dataset(value any, i int, names map[string]bool, parameters map[string]pyjson.Object) (pyjson.Object, error) {
	where := fmt.Sprintf("datasets[%d]", i)
	ds, err := keys(value, where, []string{"name", "source", "fields", "calculated"}, []string{"name", "source"}, "A dataset", nil)
	if err != nil {
		return nil, err
	}
	name, ok := get(ds, "name").(string)
	if !ok || !DatasetName.MatchString(name) {
		return nil, problem("A dataset's name has letters, digits and _, and starts with a letter", where+".name", nil)
	}
	if names[name] {
		return nil, problem(fmt.Sprintf("Two datasets are called «%s»", name), where+".name", nil)
	}
	what := fmt.Sprintf("The dataset «%s»", name)
	src, ok := get(ds, "source").(pyjson.Object)
	if !ok {
		return nil, problem(what+"'s source is an object", where+".source", nil)
	}
	at := where + ".source"
	switch kind := get(src, "kind"); {
	case pystr.Eq(kind, "sql"):
		if _, err := keys(src, at, []string{"kind", "sql", "connection_id", "engine"}, []string{"sql"}, what+"'s source", nil); err != nil {
			return nil, err
		}
		if err := text(get(src, "sql"), at+".sql", what+"'s SQL", MaxSQLChars, nil, true); err != nil {
			return nil, err
		}
		if get(src, "connection_id") != nil && !ident(src, "connection_id") {
			return nil, problem(what+"'s connection is a number", at+".connection_id", nil)
		}
		if e := get(src, "engine"); e != nil && !pystr.In(e, KnownEngines...) {
			return nil, problem(fmt.Sprintf("%s: unknown engine %s", what, pystr.Cut(pystr.Str(e), 40)), at+".engine", nil)
		}
		if get(src, "connection_id") != nil && get(src, "engine") != nil {
			return nil, problem(what+" runs on its connection: it takes no engine", at+".engine", nil)
		}
	case pystr.Eq(kind, "datasource") || pystr.Eq(kind, "view"):
		key, k := "datasource_id", "datasource"
		if pystr.Eq(kind, "view") {
			key, k = "view_id", "view"
		}
		if _, err := keys(src, at, []string{"kind", key, "filters"}, []string{key}, what+"'s source", nil); err != nil {
			return nil, err
		}
		if !ident(src, key) {
			return nil, problem(fmt.Sprintf("%s's %s is a number", what, k), at+"."+key, nil)
		}
		if err := filters(get(src, "filters"), at+".filters", what, parameters); err != nil {
			return nil, err
		}
	case pystr.Eq(kind, "sql_script"):
		if _, err := keys(src, at, []string{"kind", "script_id"}, []string{"script_id"}, what+"'s source", nil); err != nil {
			return nil, err
		}
		if !ident(src, "script_id") {
			return nil, problem(what+"'s SQL script is a number", at+".script_id", nil)
		}
	default:
		return nil, problem(what+"'s source is sql, datasource, view or sql_script", at+".kind", nil)
	}
	if fv := get(ds, "fields"); fv != nil {
		fields, ok := fv.([]any)
		if !ok || len(fields) > 1000 {
			return nil, problem(what+"'s fields are a list of at most 1000", where+".fields", nil)
		}
		for k, x := range fields {
			fat := fmt.Sprintf("%s.fields[%d]", where, k)
			f, err := keys(x, fat, []string{"name", "type"}, []string{"name"}, what+"'s field", nil)
			if err != nil {
				return nil, err
			}
			if err := text(get(f, "name"), fat+".name", what+"'s field name", MaxName, nil, true); err != nil {
				return nil, err
			}
			if err := choice(get(f, "type"), fieldTypes, fat+".type", what+"'s field type", nil, false); err != nil {
				return nil, err
			}
		}
	}
	if cv := get(ds, "calculated"); cv != nil {
		calculated, ok := cv.([]any)
		if !ok || len(calculated) > 100 {
			return nil, problem(what+"'s calculated fields are a list of at most 100", where+".calculated", nil)
		}
		seen := map[string]bool{}
		for k, x := range calculated {
			cat := fmt.Sprintf("%s.calculated[%d]", where, k)
			c, err := keys(x, cat, []string{"name", "expr"}, []string{"name", "expr"}, what+"'s calculated field", nil)
			if err != nil {
				return nil, err
			}
			if err := text(get(c, "name"), cat+".name", what+"'s calculated field name", MaxName, nil, true); err != nil {
				return nil, err
			}
			cname := get(c, "name").(string)
			if seen[cname] {
				return nil, problem(fmt.Sprintf("%s has two calculated fields called «%s»", what, cname), cat+".name", nil)
			}
			seen[cname] = true
			if err := expr(get(c, "expr"), cat+".expr", fmt.Sprintf("%s's calculated field «%s»", what, cname), nil, true); err != nil {
				return nil, err
			}
		}
	}
	names[name] = true
	return ds, nil
}

func strp(s string) *string { return &s }

// items is _items: the items of a section, each within it; a height of -1 is the body's,
// which grows over pages.
func items(value any, where, section string, width, height float64, datasets map[string]bool, ids map[string]bool) ([]pyjson.Object, error) {
	if value == nil {
		return nil, nil
	}
	most := MaxBandItems
	allowed := pageItemTypes
	if section == "body" {
		most, allowed = MaxItems, itemTypes
	}
	list, ok := value.([]any)
	if !ok || len(list) > most {
		return nil, problem(fmt.Sprintf("The %s's items are a list of at most %d", section, most), where, nil)
	}
	var out []pyjson.Object
	for i, v := range list {
		at := fmt.Sprintf("%s[%d]", where, i)
		it, ok := v.(pyjson.Object)
		if !ok {
			return nil, problem(fmt.Sprintf("An item of the %s is an object", section), at, nil)
		}
		id, ok := get(it, "id").(string)
		if !ok || !ItemID.MatchString(id) {
			return nil, problem(fmt.Sprintf("An item of the %s needs an id (letters, digits, _ and -)", section), at+".id", nil)
		}
		if ids[id] {
			return nil, problem(fmt.Sprintf("Two items have the id «%s»", id), at+".id", strp(id))
		}
		ids[id] = true
		kind := get(it, "type")
		if !pystr.In(kind, allowed...) {
			return nil, problem(fmt.Sprintf("The %s holds only %s: not «%s»", section, joinOptions(allowed), pystr.Cut(pystr.Str(kind), 20)),
				at+".type", strp(id))
		}
		if err := item(it, at, kind.(string), id, width, height, datasets, section); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, nil
}

var baseKeys = []string{"id", "type", "x", "y", "w", "h", "style", "visible"}
var typeKeys = map[string][]string{
	"text":    {"value", "dataset"},
	"image":   {"asset", "fit"},
	"line":    {},
	"rect":    {},
	"table":   {"dataset", "title", "columns", "groups", "detail", "sort", "filter", "repeat_header", "grand_total", "zebra", "matrix", "interactive"},
	"chart":   {"dataset", "title", "kind", "category", "series", "split_by", "options"},
	"kpi":     {"dataset", "label", "value", "compare", "format", "sparkline"},
	"comment": {"title", "instruction", "reads"},
}

func item(it pyjson.Object, at, kind, id string, width, height float64, datasets map[string]bool, section string) error {
	item := strp(id)
	if kind == "rect" {
		for _, k := range []string{"fill", "border", "radius"} {
			if has(it, k) {
				return problem(fmt.Sprintf("The item «%s»: a rectangle's fill, border and radius go in its style "+
					"(style.background, style.border, style.radius)", id), at+"."+k, item)
			}
		}
	}
	what := fmt.Sprintf("The item «%s»", id)
	if _, err := keys(it, at, append(append([]string{}, baseKeys...), typeKeys[kind]...), []string{"x", "y", "w", "h"}, what, item); err != nil {
		return err
	}
	pageH := height
	if height < 0 {
		pageH = 5000
	}
	x, err := mm(get(it, "x"), at+".x", what+"'s x", 0, width, item)
	if err != nil {
		return err
	}
	y, err := mm(get(it, "y"), at+".y", what+"'s y", 0, pageH, item)
	if err != nil {
		return err
	}
	if kind == "line" { // from (x, y) to (x + w, y + h): either way, on the page
		w, err := mm(get(it, "w"), at+".w", what+"'s w", -width, width, item)
		if err != nil {
			return err
		}
		h, err := mm(get(it, "h"), at+".h", what+"'s h", -pageH, pageH, item)
		if err != nil {
			return err
		}
		if !(0 <= x+w && x+w <= width+1e-6 && (height < 0 && y+h >= 0 || 0 <= y+h && y+h <= pageH+1e-6)) {
			return problem(fmt.Sprintf("%s ends outside the %s", what, section), at, item)
		}
	} else {
		w, err := mm(get(it, "w"), at+".w", what+"'s width", 0.1, width, item)
		if err != nil {
			return err
		}
		h, err := mm(get(it, "h"), at+".h", what+"'s height", 0.1, pageH, item)
		if err != nil {
			return err
		}
		if x+w > width+1e-6 {
			return problem(fmt.Sprintf("%s goes past the right margin by %s mm", what, F1(x+w-width)), at, item)
		}
		if height >= 0 && y+h > height+1e-6 {
			return problem(fmt.Sprintf("%s goes past the %s's height by %s mm", what, section, F1(y+h-height)), at, item)
		}
	}
	if err := style(get(it, "style"), at+".style", what+"'s style", item); err != nil {
		return err
	}
	if err := expr(get(it, "visible"), at+".visible", what+"'s visible", item, false); err != nil {
		return err
	}
	dataset := func() error {
		name := get(it, "dataset")
		n, ok := name.(string)
		if !pystr.Hashable(name) {
			return ErrInternal
		}
		if !ok || !datasets[n] {
			return problem(fmt.Sprintf("%s reads the dataset «%s», which the report does not have", what, pystr.Cut(pystr.Str(name), 64)),
				at+".dataset", item)
		}
		return nil
	}
	switch kind {
	case "text": // a text may read a dataset's fields, in the body
		if err := text(get(it, "value"), at+".value", what+"'s text", MaxExpr, item, false); err != nil {
			return err
		}
		if get(it, "dataset") != nil {
			if section != "body" {
				return problem(fmt.Sprintf("%s: the %s reads no dataset", what, section), at+".dataset", item)
			}
			return dataset()
		}
	case "image":
		asset, err := CheckImage(get(it, "asset"), at+".asset", id)
		if err != nil {
			return err
		}
		it.Set("asset", asset)
		return choice(get(it, "fit"), fits, at+".fit", what+"'s fit", item, false)
	case "table":
		if err := dataset(); err != nil {
			return err
		}
		return table(it, at, what, item, width)
	case "chart":
		if err := dataset(); err != nil {
			return err
		}
		if err := text(get(it, "title"), at+".title", what+"'s title", 200, item, false); err != nil {
			return err
		}
		if err := choice(get(it, "kind"), chartKinds, at+".kind", what+"'s kind", item, true); err != nil {
			return err
		}
		if err := expr(get(it, "category"), at+".category", what+"'s category", item, false); err != nil {
			return err
		}
		if err := expr(get(it, "split_by"), at+".split_by", what+"'s split", item, false); err != nil {
			return err
		}
		series, ok := get(it, "series").([]any)
		if !ok || len(series) < 1 || len(series) > MaxSeries {
			return problem(fmt.Sprintf("%s needs 1 to %d series", what, MaxSeries), at+".series", item)
		}
		for k, sv := range series {
			sat := fmt.Sprintf("%s.series[%d]", at, k)
			s, err := keys(sv, sat, []string{"value", "name", "as", "axis", "format"}, []string{"value"}, what+"'s series", item)
			if err != nil {
				return err
			}
			for _, c := range []func() error{
				func() error { return expr(get(s, "value"), sat+".value", what+"'s series value", item, true) },
				func() error { return text(get(s, "name"), sat+".name", what+"'s series name", 200, item, false) },
				func() error { return text(get(s, "format"), sat+".format", what+"'s series format", 64, item, false) },
				func() error { return choice(get(s, "as"), seriesAs, sat+".as", what+"'s series as", item, false) },
				func() error { return choice(get(s, "axis"), axes, sat+".axis", what+"'s series axis", item, false) },
			} {
				if err := c(); err != nil {
					return err
				}
			}
		}
		return options(get(it, "options"), at+".options", what+"'s options", item)
	case "kpi":
		if err := dataset(); err != nil {
			return err
		}
		for _, c := range []func() error{
			func() error { return text(get(it, "label"), at+".label", what+"'s label", 200, item, false) },
			func() error { return expr(get(it, "value"), at+".value", what+"'s value", item, true) },
			func() error { return expr(get(it, "compare"), at+".compare", what+"'s comparison", item, false) },
			func() error { return text(get(it, "format"), at+".format", what+"'s format", 64, item, false) },
		} {
			if err := c(); err != nil {
				return err
			}
		}
		if sv := get(it, "sparkline"); sv != nil {
			spark, err := keys(sv, at+".sparkline", []string{"category", "value"}, []string{"category", "value"}, what+"'s sparkline", item)
			if err != nil {
				return err
			}
			if err := expr(get(spark, "category"), at+".sparkline.category", what+"'s sparkline category", item, true); err != nil {
				return err
			}
			return expr(get(spark, "value"), at+".sparkline.value", what+"'s sparkline value", item, true)
		}
	case "comment":
		if err := text(get(it, "title"), at+".title", what+"'s title", 200, item, false); err != nil {
			return err
		}
		if err := text(get(it, "instruction"), at+".instruction", what+"'s instruction", MaxCommentInstr, item, false); err != nil {
			return err
		}
		reads, ok := get(it, "reads").([]any)
		if !ok || len(reads) < 1 || len(reads) > MaxCommentReads {
			return problem(fmt.Sprintf("%s comments on 1 to %d elements", what, MaxCommentReads), at+".reads", item)
		}
		if w, ok := pystr.Number(get(it, "w")); ok && w < MinCommentWidth {
			return problem(fmt.Sprintf("%s is at least %d mm wide: its text and the line that says who wrote it must fit a page",
				what, MinCommentWidth), at+".w", item)
		}
	}
	return nil
}

func commentsRead(list []pyjson.Object, where string) error {
	kinds := map[string]string{}
	for _, it := range list {
		kinds[get(it, "id").(string)] = get(it, "type").(string)
	}
	for i, it := range list {
		if get(it, "type") != "comment" {
			continue
		}
		id := get(it, "id").(string)
		reads := get(it, "reads").([]any)
		for k, nv := range reads {
			at := fmt.Sprintf("%s[%d].reads[%d]", where, i, k)
			name, ok := nv.(string)
			commented := false
			if ok {
				for _, t := range commentedTypes {
					if kinds[name] == t {
						commented = true
					}
				}
			}
			if !commented {
				return problem(fmt.Sprintf("The item «%s» comments on «%s»: not a KPI, a chart or a table here", id, pystr.Cut(pystr.Str(nv), 64)),
					at, strp(id))
			}
			for _, before := range reads[:k] {
				if pystr.Eq(before, name) {
					return problem(fmt.Sprintf("The item «%s» names «%s» twice", id, name), at, strp(id))
				}
			}
		}
	}
	return nil
}

func table(it pyjson.Object, at, what string, item *string, width float64) error {
	if err := text(get(it, "title"), at+".title", what+"'s title", 100, item, false); err != nil {
		return err
	}
	matrixV := get(it, "matrix")
	columnsV, present := it.Get("columns")
	if !present {
		if matrixV != nil {
			columnsV = []any{}
		} else {
			columnsV = nil
		}
	}
	least := 1
	if matrixV != nil {
		least = 0
	}
	columns, ok := columnsV.([]any)
	if !ok || len(columns) < least || len(columns) > MaxColumns {
		return problem(fmt.Sprintf("%s needs %d to %d columns", what, least, MaxColumns), at+".columns", item)
	}
	total := 0.0
	for k, cv := range columns {
		cat := fmt.Sprintf("%s.columns[%d]", at, k)
		var required []string
		if matrixV == nil {
			required = []string{"value"}
		}
		c, err := keys(cv, cat, []string{"header", "value", "width", "align", "format", "total", "style", "rules"}, required,
			fmt.Sprintf("%s's column %d", what, k+1), item)
		if err != nil {
			return err
		}
		if err := text(get(c, "header"), cat+".header", what+"'s column header", 500, item, false); err != nil {
			return err
		}
		if err := expr(get(c, "value"), cat+".value", what+"'s column value", item, matrixV == nil); err != nil {
			return err
		}
		if v := get(c, "width"); v != nil {
			x, err := mm(v, cat+".width", what+"'s column width", 1, width, item)
			if err != nil {
				return err
			}
			total += x
		}
		for _, chk := range []func() error{
			func() error {
				return choice(get(c, "align"), alignments, cat+".align", what+"'s column alignment", item, false)
			},
			func() error { return text(get(c, "format"), cat+".format", what+"'s column format", 64, item, false) },
			func() error { return expr(get(c, "total"), cat+".total", what+"'s column total", item, false) },
			func() error { return style(get(c, "style"), cat+".style", what+"'s column style", item) },
		} {
			if err := chk(); err != nil {
				return err
			}
		}
		if rv := get(c, "rules"); rv != nil {
			rules, ok := rv.([]any)
			if !ok || len(rules) > 20 {
				return problem(what+"'s column rules are a list of at most 20", cat+".rules", item)
			}
			for r, x := range rules {
				rat := fmt.Sprintf("%s.rules[%d]", cat, r)
				rule, err := keys(x, rat, []string{"when", "style"}, []string{"when"}, what+"'s rule", item)
				if err != nil {
					return err
				}
				if err := expr(get(rule, "when"), rat+".when", what+"'s rule", item, true); err != nil {
					return err
				}
				if err := style(get(rule, "style"), rat+".style", what+"'s rule style", item); err != nil {
					return err
				}
			}
		}
	}
	if total > width+1e-6 {
		return problem(fmt.Sprintf("%s's columns are %s mm wide: the page has %s mm", what, F1(total), F1(width)), at+".columns", item)
	}
	if gv := get(it, "groups"); gv != nil {
		groups, ok := gv.([]any)
		if !ok || len(groups) > MaxGroups {
			return problem(fmt.Sprintf("%s's groups are a list of at most %d", what, MaxGroups), at+".groups", item)
		}
		for k, x := range groups {
			gat := fmt.Sprintf("%s.groups[%d]", at, k)
			g, err := keys(x, gat, []string{"by", "name", "label", "header", "footer", "page_break", "sort", "keep_together"},
				[]string{"by"}, what+"'s group", item)
			if err != nil {
				return err
			}
			if err := expr(get(g, "by"), gat+".by", what+"'s group", item, true); err != nil {
				return err
			}
			if err := text(get(g, "name"), gat+".name", what+"'s group name", MaxGroupName, item, false); err != nil {
				return err
			}
			if err := expr(get(g, "label"), gat+".label", what+"'s group label", item, false); err != nil {
				return err
			}
			for _, flag := range []string{"header", "footer", "keep_together"} {
				if err := boolean(get(g, flag), gat+"."+flag, what+"'s group "+flag, item); err != nil {
					return err
				}
			}
			if err := choice(get(g, "page_break"), pageBreaks, gat+".page_break", what+"'s page break", item, false); err != nil {
				return err
			}
			if err := choice(get(g, "sort"), directions, gat+".sort", what+"'s group sort", item, false); err != nil {
				return err
			}
		}
	}
	if sv := get(it, "sort"); sv != nil {
		sorts, ok := sv.([]any)
		if !ok || len(sorts) > 10 {
			return problem(what+"'s sort is a list of at most 10", at+".sort", item)
		}
		for k, x := range sorts {
			sat := fmt.Sprintf("%s.sort[%d]", at, k)
			s, err := keys(x, sat, []string{"by", "dir"}, []string{"by"}, what+"'s sort", item)
			if err != nil {
				return err
			}
			if err := expr(get(s, "by"), sat+".by", what+"'s sort", item, true); err != nil {
				return err
			}
			if err := choice(get(s, "dir"), directions, sat+".dir", what+"'s sort direction", item, false); err != nil {
				return err
			}
		}
	}
	if err := expr(get(it, "filter"), at+".filter", what+"'s filter", item, false); err != nil {
		return err
	}
	for _, flag := range []string{"detail", "repeat_header", "grand_total", "zebra"} {
		if err := boolean(get(it, flag), at+"."+flag, what+"'s "+flag, item); err != nil {
			return err
		}
	}
	if matrixV != nil {
		matrix, err := keys(matrixV, at+".matrix", []string{"columns_by", "value", "total", "format", "width"},
			[]string{"columns_by", "value"}, what+"'s matrix", item)
		if err != nil {
			return err
		}
		by := get(matrix, "columns_by")
		byList, isList := by.([]any)
		if isList && (len(byList) < 1 || len(byList) > 3) {
			return problem(what+"'s matrix has one to three levels of columns", at+".matrix.columns_by", item)
		}
		if !isList {
			byList = []any{by}
		}
		for k, one := range byList {
			where := at + ".matrix.columns_by"
			if isList {
				where += fmt.Sprintf("[%d]", k)
			}
			if err := expr(one, where, what+"'s matrix columns", item, true); err != nil {
				return err
			}
		}
		if err := expr(get(matrix, "value"), at+".matrix.value", what+"'s matrix value", item, true); err != nil {
			return err
		}
		if err := boolean(get(matrix, "total"), at+".matrix.total", what+"'s matrix total", item); err != nil {
			return err
		}
		if err := text(get(matrix, "format"), at+".matrix.format", what+"'s matrix format", 64, item, false); err != nil {
			return err
		}
		if v := get(matrix, "width"); v != nil {
			if _, err := mm(v, at+".matrix.width", what+"'s matrix column width", 1, math.Min(width, MaxMatrixWidth), item); err != nil {
				return err
			}
		}
	}
	if iv := get(it, "interactive"); iv != nil {
		interactive, err := keys(iv, at+".interactive", []string{"sort", "toggle"}, nil, what+"'s interactivity", item)
		if err != nil {
			return err
		}
		if err := boolean(get(interactive, "sort"), at+".interactive.sort", what+"'s interactive sort", item); err != nil {
			return err
		}
		return boolean(get(interactive, "toggle"), at+".interactive.toggle", what+"'s interactive toggle", item)
	}
	return nil
}

func aloneInItsRow(list []pyjson.Object) error {
	for _, t := range list {
		if get(t, "type") != "table" {
			continue
		}
		ty, _ := pystr.Number(get(t, "y"))
		th, _ := pystr.Number(get(t, "h"))
		top, bottom := ty, ty+th
		for _, other := range list {
			if &other[0] == &t[0] {
				continue
			}
			oy, _ := pystr.Number(get(other, "y"))
			oh, _ := pystr.Number(get(other, "h"))
			oTop, oBottom := math.Min(oy, oy+oh), math.Max(oy, oy+oh)
			if oTop < bottom-1e-6 && oBottom > top+1e-6 {
				id := get(other, "id").(string)
				return problem(fmt.Sprintf("The table «%s» takes its row alone: move «%s» above or below it", get(t, "id"), id),
					"sections.body.items", strp(id))
			}
		}
	}
	return nil
}

func or(v any, def any) any {
	if pystr.Truthy(v) {
		return v
	}
	return def
}

// LoadZone is ZoneInfo(tz): whether the time zone exists.
func LoadZone(tz string) bool {
	if tz == "" || tz == "Local" || strings.HasPrefix(tz, "/") {
		return false
	}
	_, err := time.LoadLocation(tz)
	return err == nil
}

// Check is check: the definition as it will be saved (its images normalized in place), or the
// Problem that says what is wrong and where.
func Check(definition any) (pyjson.Object, error) {
	def, ok := definition.(pyjson.Object)
	if !ok {
		return nil, problem("The report's definition is an object", "", nil)
	}
	if s, err := pyjson.Dumps(def, pyjson.Options{EnsureASCII: true}); err == nil && len(s) > MaxDefinitionBytes {
		return nil, problem(fmt.Sprintf("The report is larger than %d MiB: use smaller images", MaxDefinitionBytes/(1024*1024)), "", nil)
	}
	if _, err := keys(def, "", []string{"v", "page", "locale", "timezone", "theme", "parameters", "datasets", "sections"},
		[]string{"v"}, "The report", nil); err != nil {
		return nil, err
	}
	if !pystr.Eq(get(def, "v"), int64(1)) {
		return nil, problem("The report's definition is version 1 (v: 1)", "v", nil)
	}
	width, height, err := Page(or(get(def, "page"), pyjson.Object{}))
	if err != nil {
		return nil, err
	}
	if err := choice(get(def, "locale"), locales, "locale", "The language", nil, false); err != nil {
		return nil, err
	}
	if tz := get(def, "timezone"); tz != nil {
		s, ok := tz.(string)
		if !ok || !LoadZone(s) {
			return nil, problem(fmt.Sprintf("Unknown time zone «%s»: write one as Europe/Rome", pystr.Cut(pystr.Str(tz), 64)), "timezone", nil)
		}
	}
	if tv := get(def, "theme"); tv != nil {
		theme, err := keys(tv, "theme", []string{"font", "size", "color", "accent"}, nil, "The theme", nil)
		if err != nil {
			return nil, err
		}
		if err := text(get(theme, "font"), "theme.font", "The theme's font", 64, nil, false); err != nil {
			return nil, err
		}
		if v := get(theme, "size"); v != nil {
			if _, err := number(v, "theme.size", "The theme's size", 5, 24, nil); err != nil {
				return nil, err
			}
		}
		if err := colour(get(theme, "color"), "theme.color", "The theme's colour", nil); err != nil {
			return nil, err
		}
		if err := colour(get(theme, "accent"), "theme.accent", "The theme's accent", nil); err != nil {
			return nil, err
		}
	}
	byName, err := Parameters(def)
	if err != nil {
		return nil, err
	}
	parameters, _ := or(get(def, "parameters"), []any{}).([]any)
	dsV := or(get(def, "datasets"), []any{})
	datasets, ok := dsV.([]any)
	if !ok || len(datasets) > MaxDatasets {
		return nil, problem(fmt.Sprintf("The datasets are a list of at most %d", MaxDatasets), "datasets", nil)
	}
	dsNames := map[string]bool{}
	for i, ds := range datasets {
		if _, err := Dataset(ds, i, dsNames, byName); err != nil {
			return nil, err
		}
	}
	for i, pv := range parameters {
		p := pv.(pyjson.Object)
		ch, _ := or(get(p, "choices"), pyjson.Object{}).(pyjson.Object)
		source := get(ch, "dataset")
		if source == nil {
			continue
		}
		if !pystr.Hashable(source) {
			return nil, ErrInternal
		}
		if s, ok := source.(string); !ok || !dsNames[s] {
			return nil, problem(fmt.Sprintf("The parameter «%s» takes its choices from «%s», which the report does not have",
				get(p, "name"), pystr.Cut(pystr.Str(source), 64)), fmt.Sprintf("parameters[%d].choices.dataset", i), nil)
		}
	}
	sections, err := keys(or(get(def, "sections"), pyjson.Object{}), "sections", []string{"page_header", "body", "page_footer"}, nil, "The sections", nil)
	if err != nil {
		return nil, err
	}
	header, err := keys(or(get(sections, "page_header"), pyjson.Object{}), "sections.page_header", []string{"height", "on_first_page", "items"}, nil, "The page header", nil)
	if err != nil {
		return nil, err
	}
	footer, err := keys(or(get(sections, "page_footer"), pyjson.Object{}), "sections.page_footer", []string{"height", "on_last_page", "items"}, nil, "The page footer", nil)
	if err != nil {
		return nil, err
	}
	body, err := keys(or(get(sections, "body"), pyjson.Object{}), "sections.body", []string{"items"}, nil, "The body", nil)
	if err != nil {
		return nil, err
	}
	tallest := math.Min(height, MaxBandHeight)
	heightOf := func(o pyjson.Object) any {
		if v, ok := o.Get("height"); ok {
			return v
		}
		return int64(0)
	}
	hh, err := mm(heightOf(header), "sections.page_header.height", "The page header's height", 0, tallest, nil)
	if err != nil {
		return nil, err
	}
	fh, err := mm(heightOf(footer), "sections.page_footer.height", "The page footer's height", 0, tallest, nil)
	if err != nil {
		return nil, err
	}
	if hh+fh > height-20 {
		return nil, problem("The page header and footer leave less than 2 cm for the body", "sections", nil)
	}
	if err := boolean(get(header, "on_first_page"), "sections.page_header.on_first_page", "The page header's first page", nil); err != nil {
		return nil, err
	}
	if err := boolean(get(footer, "on_last_page"), "sections.page_footer.on_last_page", "The page footer's last page", nil); err != nil {
		return nil, err
	}
	ids := map[string]bool{}
	if _, err := items(get(header, "items"), "sections.page_header.items", "page header", width, hh, dsNames, ids); err != nil {
		return nil, err
	}
	if _, err := items(get(footer, "items"), "sections.page_footer.items", "page footer", width, fh, dsNames, ids); err != nil {
		return nil, err
	}
	bodyItems, err := items(get(body, "items"), "sections.body.items", "body", width, -1, dsNames, ids)
	if err != nil {
		return nil, err
	}
	for i, it := range bodyItems {
		h, _ := pystr.Number(get(it, "h"))
		if get(it, "type") != "line" && h > height-hh-fh+1e-6 {
			return nil, problem(fmt.Sprintf("The item «%s» is taller than the body of a page (%s mm)", get(it, "id"), F1(height-hh-fh)),
				fmt.Sprintf("sections.body.items[%d].h", i), strp(get(it, "id").(string)))
		}
	}
	if err := aloneInItsRow(bodyItems); err != nil {
		return nil, err
	}
	if err := commentsRead(bodyItems, "sections.body.items"); err != nil {
		return nil, err
	}
	return def, nil
}

// Parameters is the parameters' part of check: each one checked, by name.
func Parameters(def pyjson.Object) (map[string]pyjson.Object, error) {
	pv := or(get(def, "parameters"), []any{})
	parameters, ok := pv.([]any)
	if !ok || len(parameters) > MaxParameters {
		return nil, problem(fmt.Sprintf("The parameters are a list of at most %d", MaxParameters), "parameters", nil)
	}
	names := map[string]bool{}
	byName := map[string]pyjson.Object{}
	for i, p := range parameters {
		checked, err := Parameter(p, i, names)
		if err != nil {
			return nil, err
		}
		byName[get(checked, "name").(string)] = checked
	}
	return byName, nil
}

// CheckParameters is check_parameters: the parameters of a definition being edited, which is
// what its form needs: a definition with those only.
func CheckParameters(definition any) (pyjson.Object, error) {
	def, ok := definition.(pyjson.Object)
	if !ok {
		return nil, problem("The report's definition is an object", "", nil)
	}
	if _, err := Parameters(def); err != nil {
		return nil, err
	}
	parameters := or(get(def, "parameters"), []any{})
	tz, ok := get(def, "timezone").(string)
	if !ok {
		tz = "UTC"
	}
	out := pyjson.Object{{Key: "v", Value: int64(1)}, {Key: "timezone", Value: tz}}
	if locale, ok := get(def, "locale").(string); ok {
		out = append(out, pyjson.KV{Key: "locale", Value: locale})
	}
	return append(out, pyjson.KV{Key: "parameters", Value: parameters}, pyjson.KV{Key: "datasets", Value: []any{}}), nil
}

// CheckDataset is check_dataset: the parameters and one dataset of a definition being edited.
func CheckDataset(definition any, name string) (pyjson.Object, error) {
	checked, err := CheckParameters(definition)
	if err != nil {
		return nil, err
	}
	byName := map[string]pyjson.Object{}
	for _, p := range get(checked, "parameters").([]any) {
		po := p.(pyjson.Object)
		byName[get(po, "name").(string)] = po
	}
	def := definition.(pyjson.Object)
	datasets, _ := or(get(def, "datasets"), []any{}).([]any)
	for i, ds := range datasets {
		if o, ok := ds.(pyjson.Object); ok && pystr.Eq(get(o, "name"), name) {
			if _, err := Dataset(o, i, map[string]bool{}, byName); err != nil {
				return nil, err
			}
			return append(checked[:len(checked)-1:len(checked)-1], pyjson.KV{Key: "datasets", Value: []any{o}}), nil
		}
	}
	return nil, problem(fmt.Sprintf("The report has no dataset «%s»", pystr.Cut(name, 64)), "datasets", nil)
}

// WithoutImages is without_images: the definition with its images' data left out.
func WithoutImages(node any) any {
	switch x := node.(type) {
	case pyjson.Object:
		out := make(pyjson.Object, len(x))
		for i, kv := range x {
			if _, isStr := kv.Value.(string); kv.Key == "asset" && isStr {
				out[i] = pyjson.KV{Key: kv.Key, Value: ""}
			} else {
				out[i] = pyjson.KV{Key: kv.Key, Value: WithoutImages(kv.Value)}
			}
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = WithoutImages(e)
		}
		return out
	}
	return node
}
