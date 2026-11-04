package reports

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/enginepolicy"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// Given is the parameters' values as they arrive (given_params): one list per name, in order.
type Given = pyjson.Object

// BadValue is report_params.bad_value: a 400 naming the parameter.
func BadValue(name, message string) *Refusal {
	b := &reportparams.BadValue{Name: name, Message: message}
	return &Refusal{Status: http.StatusBadRequest, Detail: b.Detail()}
}

func fromBad(b *reportparams.BadValue) *Refusal { return BadValue(b.Name, b.Message) }

// GivenParams is given_params: values as they arrive in a body, one or a list per name (null:
// none given).
func GivenParams(params pyjson.Object) (Given, error) {
	out := Given{}
	for _, kv := range params {
		if kv.Value == nil {
			continue
		}
		values, isList := kv.Value.([]any)
		if !isList {
			values = []any{kv.Value}
		}
		for _, v := range values {
			switch v.(type) {
			case pyjson.Object, []any:
				return nil, BadValue(kv.Key, "a value is a text, a number or true/false")
			}
		}
		out = out.Set(kv.Key, values)
	}
	return out, nil
}

// ── the values ───────────────────────────────────────────────────────────────

// Resolver is what reading the parameters and the datasets asks: the database, the person,
// the engine.
type Resolver struct {
	Q      perm.Querier
	C      *perm.Checker
	Engine *engine.Client
	Log    *slog.Logger
	Fernet interface{ Decrypt(string) (string, error) }
	// SQL reads a SQL with sqlglot, in the AI service (sql_scripts.resolve,
	// report_params.without_params); without it those datasets are ErrNeedsPython
	SQL *aisvc.Client
	// Read, when not nil, gets the datasources the resolved datasets read (resolve_datasets'
	// `read`: a flow group's Pushmail cell names them in its run)
	Read map[int64]bool
}

func refusal(status int, format string, args ...any) *Refusal {
	return &Refusal{Status: status, Detail: fmt.Sprintf(format, args...)}
}

// ResolvePlaceholders is report_params.resolve_placeholders: {inside: (text, error)}, resolved
// now by the engine in `timezone`, twenty to a call.
func (rs *Resolver) ResolvePlaceholders(ctx context.Context, bodies map[string]bool, timezone, lang string) (map[string][2]any, error) {
	var wanted []string
	for b := range bodies {
		wanted = append(wanted, b)
	}
	sort.Strings(wanted)
	out := map[string][2]any{}
	for start := 0; start < len(wanted); start += 20 {
		chunk := wanted[start:min(start+20, len(wanted))]
		templates := pyjson.Object{}
		for i, b := range chunk {
			templates = append(templates, pyjson.KV{Key: fmt.Sprintf("p%d", i), Value: "{" + b + "}"})
		}
		if timezone == "" {
			timezone = "UTC"
		}
		cctx, cancel := context.WithTimeout(ctx, engine.Check)
		res, raw, err := rs.Engine.JSON(cctx, http.MethodPost, "/placeholders/preview", pyjson.Object{{Key: "kind", Value: "input"},
			{Key: "templates", Value: templates}, {Key: "timezone", Value: timezone}, {Key: "lang", Value: lang}})
		cancel()
		if err != nil {
			return nil, refusal(502, "The engine could not resolve the placeholders: %s", errName(err))
		}
		if res.StatusCode >= 400 {
			return nil, refusal(502, "The engine could not resolve the placeholders (%d)", res.StatusCode)
		}
		answer, err := pyjson.Loads(string(raw))
		if err != nil {
			return nil, ErrInternal
		}
		ao := obj(answer)
		if e := get(ao, "error"); pystr.Truthy(e) {
			return nil, refusal(422, "The report's time zone: %s", pystr.Str(e))
		}
		fields := obj(or(get(ao, "fields"), pyjson.Object{}))
		for i, b := range chunk {
			got := obj(or(get(fields, fmt.Sprintf("p%d", i)), pyjson.Object{}))
			value := get(got, "value")
			var e any = get(got, "error")
			if !pystr.Truthy(e) {
				if value != nil {
					e = nil
				} else {
					e = "no value"
				}
			}
			out[b] = [2]any{value, e}
		}
	}
	return out, nil
}

func errName(err error) string {
	var te interface{ Timeout() bool }
	if errors.As(err, &te) && te.Timeout() {
		return "ReadTimeout"
	}
	return "ConnectError"
}

// texts is _texts: the texts a parameter takes; nil = no value.
func texts(param pyjson.Object, given []any, hasGiven bool) ([]any, error) {
	name := pystr.Str(get(param, "name"))
	multi := pystr.Truthy(get(param, "multi"))
	if hasGiven {
		var kept []any
		for _, g := range given {
			if s, ok := g.(string); ok && pystr.Strip(s) == "" {
				continue
			}
			kept = append(kept, g)
		}
		given = kept
		if len(given) == 1 {
			if s, ok := given[0].(string); ok && pystr.Strip(s) == reportparams.NoValue {
				return nil, nil
			}
		}
		if len(given) > 0 {
			if !multi && len(given) > 1 {
				return nil, BadValue(name, "it takes one value")
			}
			if len(given) > reportparams.MaxValues {
				return nil, BadValue(name, fmt.Sprintf("at most %d values", reportparams.MaxValues))
			}
			return given, nil
		}
	}
	def := get(param, "default")
	if l, ok := def.([]any); def == nil || (ok && len(l) == 0) {
		return nil, nil
	} else if ok {
		return append([]any{}, l...), nil
	}
	return []any{def}, nil
}

// params of a definition, by name and in order.
func parameters(def pyjson.Object) []pyjson.Object {
	var out []pyjson.Object
	for _, p := range list(or(get(def, "parameters"), []any{})) {
		out = append(out, obj(p))
	}
	return out
}

func asText(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "true"
		}
		return "false"
	}
	return pystr.Str(v)
}

// ValuesFor is values_for: every parameter's value (the given texts, the defaults for the
// others), a 400 naming the parameter when a value does not fit.
func (rs *Resolver) ValuesFor(ctx context.Context, def pyjson.Object, given Given) (pyjson.Object, error) {
	params := parameters(def)
	known := map[string]bool{}
	for _, p := range params {
		known[pystr.Str(get(p, "name"))] = true
	}
	for _, kv := range given {
		if !known[kv.Key] {
			return nil, BadValue(kv.Key, "the report has no parameter by this name")
		}
	}
	all := make([][]any, len(params))
	bodies := map[string]bool{}
	for i, p := range params {
		g, has := given.Get(pystr.Str(get(p, "name")))
		gl, _ := g.([]any)
		ts, err := texts(p, gl, has)
		if err != nil {
			return nil, err
		}
		all[i] = ts
		for _, t := range ts {
			if b, ok := reportparams.PlaceholderOf(t); ok {
				bodies[b] = true
			}
		}
	}
	var resolved map[string][2]any
	if len(bodies) > 0 {
		tz, _ := or(get(def, "timezone"), "UTC").(string)
		var err error
		if resolved, err = rs.ResolvePlaceholders(ctx, bodies, tz, reportparams.LangOf(def)); err != nil {
			return nil, err
		}
	}
	return converted(params, all, resolved, true)
}

// converted is _converted; without `resolving` the placeholders are left out (check_values).
func converted(params []pyjson.Object, all [][]any, resolved map[string][2]any, resolving bool) (pyjson.Object, error) {
	values := pyjson.Object{}
	for i, p := range params {
		name := pystr.Str(get(p, "name"))
		multi := pystr.Truthy(get(p, "multi"))
		empty := func() any {
			if multi {
				return []any{}
			}
			return nil
		}
		ts := all[i]
		if ts == nil {
			if pystr.Truthy(get(p, "required")) {
				return nil, BadValue(name, "it needs a value")
			}
			values = values.Set(name, empty())
			continue
		}
		var conv []any
		seen := map[string]bool{}
		for _, t := range ts {
			if b, ok := reportparams.PlaceholderOf(t); ok {
				if !resolving {
					continue
				}
				r := resolved[b]
				if r[1] != nil {
					return nil, BadValue(name, fmt.Sprintf("{%s}: %s", b, pystr.Str(r[1])))
				}
				t = r[0]
			} else if s, ok := t.(string); ok && strings.Contains(s, " ") {
				if _, ok := reportparams.PlaceholderOf(strings.ReplaceAll(s, " ", "+")); ok {
					return nil, BadValue(name, fmt.Sprintf("'%s' is not a placeholder: in an address a + is read as a space, write it %%2B",
						pystr.Cut(pystr.Strip(s), 40)))
				}
			}
			v, bad := reportparams.Convert(p, asText(t))
			if bad != nil {
				return nil, fromBad(bad)
			}
			if k := reportparams.Key(v); !seen[k] {
				seen[k] = true
				conv = append(conv, v)
			}
		}
		if len(conv) == 0 {
			values = values.Set(name, empty())
			continue
		}
		if fixed, ok := get(obj(or(get(p, "choices"), pyjson.Object{})), "values").([]any); ok {
			allowed := map[string]bool{}
			for _, c := range fixed {
				v, bad := reportparams.Convert(p, asText(c))
				if bad != nil {
					return nil, fromBad(bad)
				}
				allowed[reportparams.Key(v)] = true
			}
			for _, v := range conv {
				if !allowed[reportparams.Key(v)] {
					return nil, BadValue(name, fmt.Sprintf("'%s' is not one of its choices", pystr.Str(reportparams.ToJSON(v))))
				}
			}
		}
		if multi {
			values = values.Set(name, conv)
		} else {
			values = values.Set(name, conv[0])
		}
	}
	return values, nil
}

// ── the engines ──────────────────────────────────────────────────────────────

// KnownEngines are engine_policy.KNOWN_ENGINES.
var KnownEngines = []string{"polars", "duckdb", "chdb", "clickhouse", "bigquery"}

// Engines is _Engines: the engine of each dataset, asked once per resolution.
type Engines struct {
	rs     *Resolver
	picked map[string]string
}

// NewEngines starts a resolution's choices.
func (rs *Resolver) NewEngines() *Engines { return &Engines{rs: rs, picked: map[string]string{}} }

// PickEngine is engine_policy.pick_engine: the engine of a query, available and allowed.
func (rs *Resolver) PickEngine(ctx context.Context, requested any) (string, error) {
	return rs.pickEngine(ctx, requested)
}

func (rs *Resolver) pickEngine(ctx context.Context, requested any) (string, error) {
	disabled := map[string]bool{}
	rows, err := rs.Q.Query(ctx, `SELECT engine_id FROM disabled_engines`)
	if err != nil {
		return "", err
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return "", err
		}
		disabled[id] = true
	}
	rows.Close()
	allowed := map[string]bool{}
	for _, e := range KnownEngines {
		if !disabled[e] {
			allowed[e] = true
		}
	}
	usable, ok := rs.usableEngines(ctx, allowed)
	if !ok {
		usable = nil
		for e := range allowed {
			usable = append(usable, e)
		}
		sort.Strings(usable)
	}
	if pystr.Truthy(requested) {
		for _, u := range usable {
			if pystr.Eq(requested, u) {
				return u, nil
			}
		}
		names := strings.Join(usable, ", ")
		if names == "" {
			names = "none"
		}
		return "", refusal(422, "This engine cannot be used: %s. Available: %s", pystr.Str(requested), names)
	}
	if len(usable) == 0 {
		return "", refusal(422, "No engine is available")
	}
	in := map[string]bool{}
	for _, u := range usable {
		in[u] = true
	}
	return enginepolicy.Default(in), nil
}

// usableEngines reads the engine's catalog (GET /engines); false when it cannot be read.
func (rs *Resolver) usableEngines(ctx context.Context, allowed map[string]bool) ([]string, bool) {
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	res, raw, err := rs.Engine.JSON(cctx, http.MethodGet, "/engines", nil)
	if err != nil || res.StatusCode >= 400 {
		return nil, false
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, false
	}
	var items []any
	switch x := v.(type) {
	case []any:
		items = x
	case pyjson.Object:
		if len(x) > 0 {
			return nil, false // its keys are texts: `e.get` fails
		}
	case string:
		if x != "" {
			return nil, false
		}
	default:
		return nil, false
	}
	usable := []string{}
	for _, e := range items {
		o, ok := e.(pyjson.Object)
		if !ok {
			return nil, false
		}
		id, isStr := get(o, "id").(string)
		if pystr.Truthy(get(o, "available")) && isStr && allowed[id] {
			usable = append(usable, id)
		} else if !isStr && pystr.Truthy(get(o, "available")) && !pystr.Hashable(get(o, "id")) {
			return nil, false
		}
	}
	return usable, true
}

// Pick is _Engines.pick: `fallback`, a view saved on an engine no longer allowed reads with
// the default one.
func (e *Engines) Pick(ctx context.Context, requested any, fallback bool) (string, error) {
	if v, ok := e.picked[pystr.Repr(requested)]; ok {
		return v, nil
	}
	picked, err := e.rs.pickEngine(ctx, requested)
	if err != nil {
		var r *Refusal
		if errors.As(err, &r) && requested != nil && fallback {
			return e.Pick(ctx, nil, false)
		}
		return "", err
	}
	e.picked[pystr.Repr(requested)] = picked
	return picked, nil
}

// ── a dataset on its way to the engine ───────────────────────────────────────

// Plan is _Plan.
type Plan struct {
	Name         string
	Kind         string // sql, connection, chain
	SQL          string
	Conn         *Connection
	Engine       string
	Chain        pyjson.Object
	DatasourceID int64
	Filters      []any
	Placeholders map[string]bool
	Left         map[string]bool // the parameters to ignore that its SQL still names
}

var ops = map[string]string{"=": "eq", "<>": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge", "in": "in", "contains": "contains"}

var numericDtype = regexp.MustCompile(`(?i)Int|Float|Decimal`)
var wholeText = regexp.MustCompile(`^\s*[+-]?[0-9]+\s*$`)

// pyFloat is float(text): Python's spelling of a number (underscores between digits, inf, nan).
func pyFloat(s string) (float64, bool) {
	t := pystr.Strip(s)
	low := strings.ToLower(strings.TrimLeft(t, "+-"))
	if strings.HasPrefix(low, "0x") || strings.HasPrefix(low, "0b") || strings.HasPrefix(low, "0o") || strings.Contains(t, "__") ||
		strings.HasPrefix(t, "_") || strings.HasSuffix(t, "_") || strings.Contains(t, "_.") || strings.Contains(t, "._") {
		return 0, false
	}
	switch low {
	case "inf", "infinity", "nan":
	default:
		for _, r := range low {
			if !(r >= '0' && r <= '9' || r == '.' || r == 'e' || r == '+' || r == '-' || r == '_') {
				return 0, false
			}
		}
	}
	f, err := strconv.ParseFloat(strings.ReplaceAll(t, "_", ""), 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		return 0, false
	}
	return f, true
}

// viewValue is _view_value: a filter's value as the Viewer sends it.
func viewValue(value any, numeric bool) any {
	s, ok := value.(string)
	if !ok || s == "" {
		return value
	}
	if strings.Contains(s, ",") {
		var out []any
		for _, p := range strings.Split(s, ",") {
			out = append(out, pystr.Strip(p))
		}
		return out
	}
	if numeric {
		if n, ok := pyFloat(s); ok {
			if !math.IsInf(n, 0) && !math.IsNaN(n) && n == math.Trunc(n) && wholeText.MatchString(s) {
				if math.Abs(n) < 1<<63 {
					return int64(n)
				}
			}
			return n
		}
	}
	return value
}

// ViewOperations is view_operations: a saved view's operations, as the Viewer builds them.
func ViewOperations(spec pyjson.Object, ds *Datasource) []any {
	dtypes := map[string]string{}
	if v, err := pyjson.Loads(or(ds.Columns, "[]").(string)); err == nil {
		for _, c := range list(v) {
			if o, ok := c.(pyjson.Object); ok {
				dtypes[pystr.Str(get(o, "name"))] = pystr.Str(or(get(o, "dtype"), ""))
			}
		}
	}
	var out []any
	for _, fv := range list(or(get(spec, "filters"), []any{})) {
		f, ok := fv.(pyjson.Object)
		if !ok || !pystr.Truthy(get(f, "column")) || !pystr.Truthy(get(f, "operator")) {
			continue
		}
		numeric := numericDtype.MatchString(dtypes[pystr.Str(get(f, "column"))])
		params := pyjson.Object{{Key: "column", Value: get(f, "column")}, {Key: "operator", Value: get(f, "operator")}}
		op := get(f, "operator")
		if op == "between" {
			v1, has1 := f.Get("value")
			v2, has2 := f.Get("value2")
			if !has1 || v1 == "" || !has2 || v2 == "" {
				continue
			}
			params = append(params, pyjson.KV{Key: "value", Value: []any{viewValue(v1, numeric), viewValue(v2, numeric)}})
		} else if op != "is_null" && op != "is_not_null" {
			v, has := f.Get("value")
			if !has {
				v = ""
			}
			params = append(params, pyjson.KV{Key: "value", Value: viewValue(v, numeric)})
		}
		out = append(out, pyjson.Object{{Key: "type", Value: "filter"}, {Key: "params", Value: params}})
	}
	var computed []any
	for _, cv := range list(or(get(spec, "computedFields"), []any{})) {
		c, ok := cv.(pyjson.Object)
		if !ok {
			continue
		}
		name := pystr.Strip(pystr.Str(or(get(c, "name"), "")))
		expr := pystr.Strip(pystr.Str(or(get(c, "expr"), "")))
		if name != "" && expr != "" {
			computed = append(computed, pyjson.Object{{Key: "name", Value: name}, {Key: "expr", Value: expr}})
		}
	}
	if len(computed) > 0 {
		out = append(out, pyjson.Object{{Key: "type", Value: "compute"}, {Key: "params", Value: pyjson.Object{{Key: "columns", Value: computed}}}})
	}
	pivot := obj(or(get(spec, "pivot"), pyjson.Object{}))
	var index, on []any
	for _, c := range list(or(get(pivot, "index"), []any{})) {
		if pystr.Truthy(c) {
			index = append(index, c)
		}
	}
	for _, c := range list(or(get(pivot, "on"), []any{})) {
		if pystr.Truthy(c) {
			on = append(on, c)
		}
	}
	if pystr.Truthy(get(spec, "pivotOn")) && len(on) > 0 && pystr.Truthy(get(pivot, "values")) && len(index) > 0 {
		out = append(out, pyjson.Object{{Key: "type", Value: "pivot"}, {Key: "params", Value: pyjson.Object{
			{Key: "index", Value: index}, {Key: "on", Value: on}, {Key: "values", Value: get(pivot, "values")},
			{Key: "func", Value: or(get(pivot, "func"), "sum")}}}})
	}
	if out == nil {
		out = []any{}
	}
	return out
}

// plan is _plan. `ignoring`: parameters read as having no value, their conditions in a SQL taken
// for true (the choices of a parameter); the SQL is checked before and after.
func (rs *Resolver) plan(ctx context.Context, ds pyjson.Object, params map[string]bool, engines *Engines, ignoring map[string]bool) (*Plan, error) {
	src, name := obj(get(ds, "source")), pystr.Str(get(ds, "name"))
	switch get(src, "kind") {
	case "sql", "sql_script":
		s, err := SQLOf(ctx, rs.Q, rs.C, src)
		if err != nil {
			return nil, err
		}
		if (s.Conn == nil || len(ignoring) > 0) && rs.SQL == nil {
			return nil, ErrNeedsPython // read by sqlglot: without the AI service, the Python gateway's
		}
		p := &Plan{Name: name, Kind: "connection", SQL: s.SQL, Conn: s.Conn, Left: map[string]bool{}}
		key := ""
		if s.Conn != nil {
			key = s.Conn.DBType
		} else {
			if p.Engine, err = engines.Pick(ctx, s.Engine, false); err != nil {
				return nil, err
			}
			p.Kind, key = "sql", p.Engine
		}
		dialect := reportparams.DialectOf(key)
		_, placeholders, r := reportparams.CheckSQL(s.SQL, dialect, params)
		if r != nil {
			return nil, r
		}
		p.Placeholders = placeholders
		if len(ignoring) > 0 {
			names := make([]string, 0, len(ignoring))
			for n := range ignoring {
				names = append(names, n)
			}
			sort.Strings(names)
			if p.SQL, err = rs.SQL.WithoutParams(ctx, s.SQL, key, names); err != nil {
				var refused *aisvc.Refusal
				if errors.As(err, &refused) {
					return nil, &reportparams.Refusal{Message: refused.Message}
				}
				return nil, err
			}
			used, placeholders, r := reportparams.CheckSQL(p.SQL, dialect, params)
			if r != nil {
				return nil, r
			}
			p.Placeholders = placeholders
			for u := range used {
				if ignoring[u] {
					p.Left[u] = true
				}
			}
		}
		return p, nil
	}
	var target *Datasource
	var operations []any
	var engineV any
	var err error
	if get(src, "kind") == "datasource" {
		target, err = ReadableDatasource(ctx, rs.Q, rs.C, asID(get(src, "datasource_id")))
		if err != nil {
			return nil, err
		}
		operations = []any{}
	} else {
		var view *View
		view, target, err = ReadableView(ctx, rs.Q, rs.C, asID(get(src, "view_id")))
		if err != nil {
			return nil, err
		}
		spec := pyjson.Object{}
		text := view.Spec
		if text == "" {
			text = "{}"
		}
		if v, err := pyjson.Loads(text); err == nil {
			if o, ok := v.(pyjson.Object); ok {
				spec = o
			} else if pystr.Truthy(v) {
				return nil, ErrInternal // (spec or {}).get of a list
			}
		}
		operations = ViewOperations(spec, target)
		engineV = get(spec, "engine")
	}
	if target.Key == "" {
		return nil, refusal(422, "the datasource «%s» has no data yet", target.Name)
	}
	engineName, err := engines.Pick(ctx, engineV, true)
	if err != nil {
		return nil, err
	}
	p := &Plan{Name: name, Kind: "chain", Engine: engineName, Filters: list(or(get(src, "filters"), []any{})),
		Chain:        pyjson.Object{{Key: "bucket", Value: target.Bucket}, {Key: "input_key", Value: target.Key}, {Key: "operations", Value: operations}},
		DatasourceID: target.ID, Placeholders: map[string]bool{}}
	for _, f := range p.Filters {
		if b, ok := reportparams.PlaceholderOf(get(obj(f), "value")); ok {
			p.Placeholders[b] = true
		}
	}
	return p, nil
}

// InDataset is _in_dataset: a refusal of a dataset, said with its name.
func InDataset(name string, err error) error {
	var r *Refusal
	var u *Unreadable
	var sqlr *reportparams.Refusal
	switch {
	case errors.As(err, &u):
		return &Unreadable{Message: fmt.Sprintf("The dataset «%s»: %s", name, pystr.Within(u.Message))}
	case errors.As(err, &r):
		if o, ok := r.Detail.(pyjson.Object); ok {
			if _, has := o.Get("parameter"); has {
				return r
			}
		}
		status := r.Status
		if status != 400 && status != 502 && status != 504 {
			status = 422
		}
		return &Refusal{Status: status, Detail: fmt.Sprintf("The dataset «%s»: %s", name, pystr.Within(Text(r)))}
	case errors.As(err, &sqlr):
		return &Refusal{Status: 422, Detail: fmt.Sprintf("The dataset «%s»: %s", name, pystr.Within(sqlr.Message))}
	}
	return err
}

func (rs *Resolver) filterOps(p *Plan, params map[string]bool, values pyjson.Object, placeholders map[string]string) []any {
	var out []any
	for _, fv := range p.Filters {
		f := obj(fv)
		value := get(f, "value")
		if s, ok := value.(string); ok {
			if m := paramRef.FindStringSubmatch(pystr.Strip(s)); m != nil && params[m[1]] {
				v := get(values, m[1])
				if l, isList := v.([]any); v == nil || (isList && len(l) == 0) {
					continue
				}
				value = reportparams.ToJSON(v)
				out = append(out, filterOp(f, value))
				continue
			}
		}
		if b, ok := reportparams.PlaceholderOf(value); ok {
			value = placeholders[b]
		}
		out = append(out, filterOp(f, value))
	}
	return out
}

func filterOp(f pyjson.Object, value any) pyjson.Object {
	return pyjson.Object{{Key: "type", Value: "filter"}, {Key: "params", Value: pyjson.Object{{Key: "column", Value: get(f, "column")},
		{Key: "operator", Value: ops[pystr.Str(get(f, "op"))]}, {Key: "value", Value: value}}}}
}

func (rs *Resolver) finish(ctx context.Context, p *Plan, def pyjson.Object, values pyjson.Object, placeholders map[string]string,
	read map[int64]bool) (pyjson.Object, error) {
	types := map[string]string{}
	paramNames := map[string]bool{}
	for _, q := range parameters(def) {
		n := pystr.Str(get(q, "name"))
		types[n] = pystr.Str(get(q, "type"))
		paramNames[n] = true
	}
	if p.Kind == "connection" || p.Kind == "sql" {
		vals := map[string]any{}
		for _, kv := range values {
			vals[kv.Key] = kv.Value
		}
		for n := range p.Left { // what to ignore that the SQL still names: NULL, several values or one
			vals[n] = nil
		}
		if p.Kind == "sql" {
			sql, r := reportparams.Fill(p.SQL, reportparams.DialectOf(p.Engine), types, vals, placeholders)
			if r != nil {
				return nil, r
			}
			resolved, err := rs.resolveCatalog(ctx, sql, p.Engine)
			if err != nil {
				return nil, err
			}
			if len(resolved.Tables) == 0 {
				return nil, refusal(422, "the query reads no datasource: name one in FROM")
			}
			if read != nil {
				for _, n := range resolved.Tables {
					read[n.Datasource.ID] = true
				}
			}
			return append(sqlscripts.CatalogChain(resolved), pyjson.KV{Key: "engine", Value: p.Engine}), nil
		}
		sql, r := reportparams.Fill(p.SQL, reportparams.DialectOf(p.Conn.DBType), types, vals, placeholders)
		if r != nil {
			return nil, r
		}
		return pyjson.Object{{Key: "connection", Value: ConnectionPayload(p.Conn)}, {Key: "sql", Value: sql}}, nil
	}
	if read != nil {
		read[p.DatasourceID] = true
	}
	chain := append(pyjson.Object{}, p.Chain...)
	operations := append(rs.filterOps(p, paramNames, values, placeholders), list(get(chain, "operations"))...)
	if operations == nil {
		operations = []any{}
	}
	chain = chain.Set("operations", operations)
	return append(chain, pyjson.KV{Key: "engine", Value: p.Engine}), nil
}

// resolveCatalog is sql_scripts.resolve with the person's rights, its refusals said as the
// report's.
func (rs *Resolver) resolveCatalog(ctx context.Context, sql, engineName string) (*sqlscripts.Resolved, error) {
	if rs.SQL == nil {
		return nil, ErrNeedsPython
	}
	resolved, err := sqlscripts.Resolve(ctx, rs.Q, rs.C, rs.SQL, sql, engineName)
	return resolved, fromSQLScripts(err)
}

// fromSQLScripts says a refusal of sqlscripts as this package's.
func fromSQLScripts(err error) error {
	var u *sqlscripts.Unreadable
	var r *sqlscripts.Refusal
	switch {
	case errors.As(err, &u):
		return &Unreadable{Message: u.Message}
	case errors.As(err, &r):
		return &Refusal{Status: http.StatusUnprocessableEntity, Detail: r.Message}
	case errors.Is(err, sqlscripts.ErrInternal):
		return ErrInternal
	}
	return err
}

// ResolveDatasets is resolve_datasets: every dataset (or those in `only`) as the engine reads
// it, with the person's rights and the parameters' values in place.
func (rs *Resolver) ResolveDatasets(ctx context.Context, def pyjson.Object, values pyjson.Object, only map[string]bool,
	engines *Engines) (pyjson.Object, error) {
	return rs.ResolveDatasetsIgnoring(ctx, def, values, only, engines, nil, nil)
}

// ResolveDatasetsIgnoring is resolve_datasets with `ignoring` (see plan); the parameters to
// ignore that a SQL still names go in `left`, when given.
func (rs *Resolver) ResolveDatasetsIgnoring(ctx context.Context, def pyjson.Object, values pyjson.Object, only map[string]bool,
	engines *Engines, ignoring map[string]bool, left map[string]bool) (pyjson.Object, error) {
	params := map[string]bool{}
	for _, q := range parameters(def) {
		params[pystr.Str(get(q, "name"))] = true
	}
	if engines == nil {
		engines = rs.NewEngines()
	}
	var plans []*Plan
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		ds := obj(d)
		name := pystr.Str(get(ds, "name"))
		if only != nil && !only[name] {
			continue
		}
		p, err := rs.plan(ctx, ds, params, engines, ignoring)
		if err != nil {
			if errors.Is(err, ErrNeedsPython) || errors.Is(err, ErrInternal) {
				return nil, err
			}
			return nil, InDataset(name, err)
		}
		plans = append(plans, p)
		if left != nil {
			for n := range p.Left {
				left[n] = true
			}
		}
	}
	bodies := map[string]bool{}
	for _, p := range plans {
		for b := range p.Placeholders {
			bodies[b] = true
		}
	}
	texts := map[string]string{}
	if len(bodies) > 0 {
		tz, _ := or(get(def, "timezone"), "UTC").(string)
		resolved, err := rs.ResolvePlaceholders(ctx, bodies, tz, reportparams.LangOf(def))
		if err != nil {
			return nil, err
		}
		var sortedBodies []string
		for b := range resolved {
			sortedBodies = append(sortedBodies, b)
		}
		sort.Strings(sortedBodies)
		for _, b := range sortedBodies {
			r := resolved[b]
			if r[1] != nil {
				where := ""
				for _, p := range plans {
					if p.Placeholders[b] {
						where = p.Name
						break
					}
				}
				return nil, refusal(422, "The dataset «%s»: {%s}: %s", where, b, pystr.Str(r[1]))
			}
			texts[b] = pystr.Str(r[0])
		}
	}
	out := pyjson.Object{}
	for _, p := range plans {
		resolved, err := rs.finish(ctx, p, def, values, texts, rs.Read)
		if err != nil {
			if errors.Is(err, ErrNeedsPython) || errors.Is(err, ErrInternal) {
				return nil, err
			}
			return nil, InDataset(p.Name, err)
		}
		out = out.Set(p.Name, resolved)
	}
	return out, nil
}

// ── the connection the engine opens ──────────────────────────────────────────

// ConnectionPayload is routes/connections.py engine_connection_payload for a database: the
// password still encrypted, and the TLS options (services/db_tls.py engine_options).
func ConnectionPayload(c *Connection) pyjson.Object {
	var port any
	if c.Port != nil {
		port = *c.Port
	}
	return pyjson.Object{{Key: "db_type", Value: c.DBType}, {Key: "host", Value: c.Host}, {Key: "port", Value: port},
		{Key: "username", Value: c.Username}, {Key: "password_encrypted", Value: c.PasswordEncrypted},
		{Key: "database", Value: c.Database}, {Key: "db_schema", Value: c.DBSchema}, {Key: "extra", Value: tlsOptions(c.DBType, c.Extra)}}
}

func tlsOptions(dbType string, extra string) pyjson.Object {
	if !Queryable[dbType] {
		return pyjson.Object{}
	}
	data := pyjson.Object{}
	if extra == "" {
		extra = "{}"
	}
	if v, err := pyjson.Loads(extra); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			data = o
		}
	}
	tls := ""
	if t, ok := get(data, "tls").(string); ok && (t == "verify" || t == "trust" || t == "none") {
		tls = t
	} else if dbType == "sqlserver" {
		tls = "verify"
	}
	out := pyjson.Object{}
	if tls != "" {
		out = append(out, pyjson.KV{Key: "tls", Value: tls})
	}
	if ca, ok := get(data, "ca_cert").(string); ok && tls == "verify" && dbType != "sqlserver" && pystr.Strip(ca) != "" {
		out = append(out, pyjson.KV{Key: "ca_cert", Value: pystr.Strip(ca)})
	}
	return out
}

// ── what the engine refuses ──────────────────────────────────────────────────

// EngineFailure is sql_scripts._engine_failure: the engine's refusal as the gateway's answer.
func EngineFailure(status int, raw []byte, message func(string) string) error {
	if status == 504 {
		return refusal(504, "The query took too long and was stopped")
	}
	detail := string(raw)
	if v, err := pyjson.Loads(string(raw)); err == nil {
		o, ok := v.(pyjson.Object)
		if !ok {
			return ErrInternal
		}
		detail = pystr.Str(get(o, "detail"))
	}
	if status == 429 {
		return &Refusal{Status: 429, Detail: pystr.Cut(detail, 2000)}
	}
	code := 422
	if status >= 500 {
		code = 502
	}
	return &Refusal{Status: code, Detail: pystr.Cut(message(detail), 2000)}
}
