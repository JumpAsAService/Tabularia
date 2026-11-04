package reports

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportdef"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// ErrNeedsPython is where the Python code reads a SQL with sqlglot (sql_scripts.resolve: the
// datasources a SQL on the catalog names, report_params.without_params) and no AI service was
// given to ask (Resolver.SQL, Reviewer.SQL): the request is answered by the Python gateway, as
// it came. Up to that point the Go code only read the database.
var ErrNeedsPython = errors.New("this request reads a SQL with sqlglot: the Python gateway answers it")

var paramRef = regexp.MustCompile(`^\{([A-Za-z_][A-Za-z0-9_]*)\}\n?$`)

func get(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

func obj(v any) pyjson.Object {
	o, _ := v.(pyjson.Object)
	return o
}

func list(v any) []any {
	l, _ := v.([]any)
	return l
}

// or is `v or def`.
func or(v, def any) any {
	if pystr.Truthy(v) {
		return v
	}
	return def
}

// FilterParams is _filter_params: the parameters a datasource's or a view's filters name.
func FilterParams(src pyjson.Object) map[string]bool {
	out := map[string]bool{}
	for _, f := range list(or(get(src, "filters"), []any{})) {
		v, ok := get(obj(f), "value").(string)
		if !ok {
			continue
		}
		if m := paramRef.FindStringSubmatch(pystr.Strip(v)); m != nil && !reportparams.Builtins[m[1]] {
			out[m[1]] = true
		}
	}
	return out
}

// SQLSource is what _sql_of finds: the SQL, its connection (nil: the catalog) and its engine.
type SQLSource struct {
	SQL    string
	Conn   *Connection
	Engine any // a string or nil
}

// Dialect is _dialect.
func (s *SQLSource) Dialect() reportparams.Dialect {
	if s.Conn != nil {
		return reportparams.DialectOf(s.Conn.DBType)
	}
	e, _ := s.Engine.(string)
	return reportparams.DialectOf(e)
}

func asID(v any) int64 {
	n, _ := pystr.Number(v)
	return int64(n)
}

// SQLOf is _sql_of; with `c` the person's right to the script and the connection is checked.
func SQLOf(ctx context.Context, q perm.Querier, c *perm.Checker, src pyjson.Object) (*SQLSource, error) {
	var sql string
	var cid, engineV any
	if get(src, "kind") == "sql_script" {
		var script *Script
		var err error
		if c != nil {
			script, err = ReadableScript(ctx, q, c, asID(get(src, "script_id")))
		} else {
			script, err = GetScript(ctx, q, asID(get(src, "script_id")))
		}
		if err != nil {
			return nil, err
		}
		content, err := ScriptContent(ctx, q, script)
		if err != nil {
			return nil, err
		}
		s, _ := or(get(content, "sql"), "").(string)
		sql, cid, engineV = s, get(content, "connection_id"), get(content, "engine")
	} else {
		sql, _ = get(src, "sql").(string)
		cid, engineV = get(src, "connection_id"), get(src, "engine")
	}
	if cid == nil {
		return &SQLSource{SQL: StripComments(sql), Engine: engineV}, nil
	}
	if !pystr.IsInt(cid) {
		return nil, ErrInternal
	}
	var conn *Connection
	var err error
	if c != nil {
		conn, err = UsableConnection(ctx, q, c, asID(cid))
	} else {
		conn, err = QueryableConnection(ctx, q, asID(cid))
	}
	if err != nil {
		return nil, err
	}
	return &SQLSource{SQL: sql, Conn: conn}, nil
}

// Columns is _columns: the names of a datasource's columns.
func (d *Datasource) columns() (map[string]bool, error) {
	text := d.Columns
	if text == "" {
		text = "[]"
	}
	v, err := pyjson.Loads(text)
	out := map[string]bool{}
	if err != nil {
		return out, nil
	}
	switch x := v.(type) {
	case []any:
		for _, c := range x {
			if o, ok := c.(pyjson.Object); ok {
				out[pystr.Str(get(o, "name"))] = true
			}
		}
	case pyjson.Object, string:
	default:
		return nil, ErrInternal
	}
	return out, nil
}

// problemOf is the Problem a refusal of a dataset becomes in check_references.
func problemOf(name, where string, err error) error {
	var u *Unreadable
	var r *Refusal
	var sqlr *reportparams.Refusal
	switch {
	case errors.As(err, &u), errors.As(err, &r):
		return &reportdef.Problem{Message: fmt.Sprintf("The dataset «%s»: %s", name, pystr.Within(Text(err))), Where: where}
	case errors.As(err, &sqlr):
		return &reportdef.Problem{Message: fmt.Sprintf("The dataset «%s»: %s", name, pystr.Within(sqlr.Message)), Where: where}
	}
	return err
}

func paramsOf(def pyjson.Object) (map[string]bool, []pyjson.Object) {
	names := map[string]bool{}
	var params []pyjson.Object
	for _, p := range list(or(get(def, "parameters"), []any{})) {
		po := obj(p)
		params = append(params, po)
		if n, ok := get(po, "name").(string); ok {
			names[n] = true
		}
	}
	return names, params
}

// CheckReferences is check_references: what a definition points to, at a save. A dataset whose
// source is new or changed must point to something that exists and that this person may read;
// every SQL uses only the report's parameters; two lists of choices do not follow each other.
func CheckReferences(ctx context.Context, q perm.Querier, c *perm.Checker, def pyjson.Object, current any) error {
	return CheckReferencesWith(ctx, q, c, nil, def, current)
}

// CheckReferencesWith is CheckReferences with the AI service that reads a SQL on the catalog
// (_resolve_catalog); without it such a dataset is ErrNeedsPython.
func CheckReferencesWith(ctx context.Context, q perm.Querier, c *perm.Checker, svc *aisvc.Client, def pyjson.Object, current any) error {
	before := map[string]any{}
	if current != nil {
		cur, ok := current.(pyjson.Object)
		if !ok {
			if pystr.Truthy(current) {
				return ErrInternal
			}
		}
		for _, d := range list(or(get(cur, "datasets"), []any{})) {
			if o, ok := d.(pyjson.Object); ok {
				if n, ok := get(o, "name").(string); ok {
					before[n] = get(o, "source")
				}
			}
		}
	}
	params, _ := paramsOf(def)
	uses := map[string]map[string]bool{}
	for i, d := range list(or(get(def, "datasets"), []any{})) {
		ds := obj(d)
		name := get(ds, "name").(string)
		src := obj(get(ds, "source"))
		where := fmt.Sprintf("datasets[%d].source", i)
		prev, had := before[name]
		changed := !had || !pystr.Eq(prev, src)
		used := FilterParams(src)
		kind := get(src, "kind")
		err := func() error {
			if kind == "sql" || (kind == "sql_script" && changed) {
				var checker *perm.Checker
				if changed {
					checker = c
				}
				s, err := SQLOf(ctx, q, checker, src)
				if err != nil {
					return err
				}
				u, _, refusal := reportparams.CheckSQL(s.SQL, s.Dialect(), params)
				if refusal != nil {
					return refusal
				}
				for k := range u {
					used[k] = true
				}
				if changed && s.Conn == nil { // the datasources it names, as a render finds them
					if svc == nil {
						return ErrNeedsPython
					}
					return resolveCatalog(ctx, q, c, svc, s, paramDefs(def))
				}
				return nil
			}
			if changed && (kind == "datasource" || kind == "view") {
				var target *Datasource
				var err error
				if kind == "datasource" {
					target, err = ReadableDatasource(ctx, q, c, asID(get(src, "datasource_id")))
				} else {
					_, target, err = ReadableView(ctx, q, c, asID(get(src, "view_id")))
				}
				if err != nil {
					return err
				}
				known, err := target.columns()
				if err != nil {
					return err
				}
				for _, f := range list(or(get(src, "filters"), []any{})) {
					col := pystr.Str(get(obj(f), "column"))
					if len(known) > 0 && !known[col] {
						return &Refusal{Status: http.StatusUnprocessableEntity,
							Detail: fmt.Sprintf("its filter names the column «%s», which «%s» does not have", col, target.Name)}
					}
				}
			}
			return nil
		}()
		if err != nil {
			return problemOf(name, where, err)
		}
		uses[name] = used
	}
	return checkNoCircle(def, uses)
}

// paramDefs is {p["name"]: p for p in definition["parameters"]}.
func paramDefs(def pyjson.Object) map[string]pyjson.Object {
	out := map[string]pyjson.Object{}
	for _, p := range list(or(get(def, "parameters"), []any{})) {
		po := obj(p)
		out[pystr.Str(get(po, "name"))] = po
	}
	return out
}

// resolveCatalog is _resolve_catalog: a SQL on the catalog read as a render reads it, without
// asking the engine: every parameter a value of its type, every placeholder a date. A refusal
// for a datasource the person cannot read (or that does not exist), or a SQL that cannot be read.
func resolveCatalog(ctx context.Context, q perm.Querier, c *perm.Checker, svc *aisvc.Client, s *SQLSource, params map[string]pyjson.Object) error {
	engineName, _ := s.Engine.(string)
	dialect := reportparams.DialectOf(engineName)
	names := make(map[string]bool, len(params))
	types := make(map[string]string, len(params))
	for n, p := range params {
		names[n] = true
		types[n] = pystr.Str(get(p, "type"))
	}
	_, bodies, r := reportparams.CheckSQL(s.SQL, dialect, names)
	if r != nil {
		return r
	}
	now := time.Now().UTC() // date.today() of the gateway, which runs in UTC
	today := reportparams.Date{T: time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)}
	values := make(map[string]any, len(params))
	for n, p := range params {
		values[n] = neutral(p, today)
	}
	placeholders := make(map[string]string, len(bodies))
	for b := range bodies {
		placeholders[b] = "2000-01-01"
	}
	sql, r := reportparams.Fill(s.SQL, dialect, types, values, placeholders)
	if r != nil {
		return r
	}
	_, err := sqlscripts.Resolve(ctx, q, c, svc, sql, engineName)
	return fromSQLScripts(err)
}

func choicesDataset(p pyjson.Object) any {
	return get(obj(or(get(p, "choices"), pyjson.Object{})), "dataset")
}

// NotFollowed is _not_followed: the parameters the choices of `param` do not follow when read.
func NotFollowed(def pyjson.Object, param pyjson.Object) map[string]bool {
	_, params := paramsOf(def)
	name := get(param, "name")
	at := 0
	for i, p := range params {
		if pystr.Eq(get(p, "name"), name) {
			at = i
			break
		}
	}
	source := choicesDataset(param)
	out := map[string]bool{pystr.Str(name): true}
	for _, p := range params[at+1:] {
		if pystr.Eq(choicesDataset(p), source) {
			out[pystr.Str(get(p, "name"))] = true
		}
	}
	return out
}

func checkNoCircle(def pyjson.Object, uses map[string]map[string]bool) error {
	_, params := paramsOf(def)
	var withList []pyjson.Object
	for _, p := range params {
		if pystr.Truthy(choicesDataset(p)) {
			withList = append(withList, p)
		}
	}
	follows := map[string][]string{}
	for _, p := range withList {
		ds, _ := choicesDataset(p).(string)
		not := NotFollowed(def, p)
		var f []string
		for u := range uses[ds] {
			if !not[u] {
				f = append(f, u)
			}
		}
		sort.Strings(f)
		follows[get(p, "name").(string)] = f
	}
	var path func(start, name string, seen []string) []string
	path = func(start, name string, seen []string) []string {
		for _, other := range follows[name] {
			if other == start {
				return seen
			}
			in := false
			for _, s := range seen {
				if s == other {
					in = true
				}
			}
			if !in {
				if found := path(start, other, append(append([]string{}, seen...), other)); found != nil {
					return found
				}
			}
		}
		return nil
	}
	for _, p := range withList {
		n := get(p, "name").(string)
		if circle := path(n, n, []string{n}); circle != nil {
			quoted := make([]string, len(circle))
			for i, c := range circle {
				quoted[i] = "«" + c + "»"
			}
			index := 0
			for i, q := range params {
				if pystr.Eq(q, p) {
					index = i
					break
				}
			}
			return &reportdef.Problem{Message: fmt.Sprintf("The choices of %s follow each other: each list reads a dataset that the other "+
				"parameter filters, and neither can be chosen first. Take that filter off one of the two datasets", strings.Join(quoted, " and ")),
				Where: fmt.Sprintf("parameters[%d].choices.dataset", index)}
		}
	}
	return nil
}

// FieldType is _field_type.
func FieldType(dtype string) string {
	d := strings.ToLower(dtype)
	for _, p := range [][2]string{{"int", "integer"}, {"uint", "integer"}, {"float", "float"}, {"decimal", "float"},
		{"double", "float"}, {"string", "string"}, {"utf8", "string"}, {"str", "string"}, {"categorical", "string"},
		{"enum", "string"}, {"datetime", "datetime"}, {"timestamp", "datetime"}, {"date", "date"}, {"time", "time"},
		{"bool", "boolean"}} {
		if strings.HasPrefix(d, p[0]) {
			return p[1]
		}
	}
	return "other"
}

// KnownColumns is known_columns: the columns of the datasets that read a datasource as it is,
// which the gateway knows, of those this person may read.
func KnownColumns(ctx context.Context, q perm.Querier, c *perm.Checker, def pyjson.Object) (pyjson.Object, error) {
	out := pyjson.Object{}
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		ds := obj(d)
		src := obj(or(get(ds, "source"), pyjson.Object{}))
		if get(src, "kind") != "datasource" {
			continue
		}
		target, err := GetDatasource(ctx, q, asID(get(src, "datasource_id")))
		if err != nil {
			return nil, err
		}
		if target == nil {
			continue
		}
		if ok, err := c.Has(ctx, target.ProjectID, perm.View); err != nil {
			return nil, err
		} else if !ok {
			continue
		}
		text := target.Columns
		if text == "" {
			text = "[]"
		}
		v, err := pyjson.Loads(text)
		if err != nil {
			continue
		}
		found := pyjson.Object{}
		for _, col := range list(v) {
			co, ok := col.(pyjson.Object)
			if !ok || !pystr.Truthy(get(co, "name")) {
				continue
			}
			found = found.Set(pystr.Str(get(co, "name")), FieldType(pystr.Str(or(get(co, "dtype"), ""))))
		}
		if len(found) > 0 {
			out = out.Set(pystr.Str(get(ds, "name")), found)
		}
	}
	return out, nil
}

// EngineProblems is engine_problems: the expressions, fields and formats checked by the engine
// (POST /reports/check), as it says them; an engine that cannot answer finds none.
func EngineProblems(ctx context.Context, eng *engine.Client, log *slog.Logger, def any, columns pyjson.Object) ([]pyjson.Object, error) {
	body := pyjson.Object{{Key: "definition", Value: reportdef.WithoutImages(def)}}
	if len(columns) > 0 {
		body = append(body, pyjson.KV{Key: "columns", Value: columns})
	}
	cctx, cancel := context.WithTimeout(ctx, engine.Check)
	defer cancel()
	res, raw, err := eng.JSON(cctx, http.MethodPost, "/reports/check", body)
	if err != nil {
		log.Warn("report not checked by the engine", "err", err)
		return nil, nil
	}
	if res.StatusCode != 200 {
		return nil, nil
	}
	answerV, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, nil
	}
	answer, ok := answerV.(pyjson.Object)
	if !ok {
		return nil, ErrInternal // answer.get of a list
	}
	var found []pyjson.Object
	for _, e := range list(or(get(answer, "problems"), or(get(answer, "errors"), []any{}))) {
		eo, ok := e.(pyjson.Object)
		if !ok {
			continue
		}
		if len(found) == 100 {
			break
		}
		pos, has := eo.Get("pos")
		if !has {
			pos = get(eo, "position")
		}
		var item any
		if s, ok := get(eo, "item").(string); ok {
			item = s
		}
		var p any
		if pystr.IsInt(pos) {
			p = pos
		}
		var length any = int64(0)
		if l := get(eo, "length"); pystr.IsInt(l) {
			length = l
		} else if b, ok := l.(bool); ok {
			length = b // isinstance(True, int)
		}
		message := pystr.Cut(pystr.Sentence(pystr.Str(or(get(eo, "message"), "The report has an error"))), 2000)
		found = append(found, pyjson.Object{{Key: "item", Value: item},
			{Key: "field", Value: reportdef.ByName(pystr.Str(or(get(eo, "field"), or(get(eo, "where"), ""))), def)},
			{Key: "pos", Value: p}, {Key: "length", Value: length}, {Key: "message", Value: message}})
	}
	return found, nil
}

// Review is reports.Review: a definition looked over.
type Review struct {
	Definition pyjson.Object // as it will be saved; nil with problems
	Problems   []pyjson.Object
	Where      *string
}

// Refusal is review.refusal(): the 422.
func (r *Review) Refusal() *Refusal {
	return &Refusal{Status: http.StatusUnprocessableEntity, Detail: reportdef.Refusal(r.Problems, r.Where)}
}

// Reviewer is what reviewing reads: the database, the person, the engine.
type Reviewer struct {
	Q      perm.Querier
	C      *perm.Checker
	Engine *engine.Client
	Log    *slog.Logger
	SQL    *aisvc.Client // reads a SQL on the catalog (the AI service)
}

func pair(p pyjson.Object) string {
	b, _ := json.Marshal([]any{get(p, "item"), get(p, "field")})
	return string(b)
}

// Review is review: every problem of a definition, the gateway's first (the first one only) and
// the engine's (all of them), a place said once.
func (rv *Reviewer) Review(ctx context.Context, definition any, current any) (*Review, error) {
	var found []pyjson.Object
	var where *string
	checked, err := reportdef.Check(definition)
	if err == nil {
		err = CheckReferencesWith(ctx, rv.Q, rv.C, rv.SQL, checked, current)
	}
	if err != nil {
		p, ok := reportdef.AsProblem(err)
		if !ok {
			return nil, err
		}
		found = append(found, p.Dict(definition))
		checked, where = nil, &p.Where
	}
	var columns pyjson.Object
	if checked != nil {
		if columns, err = KnownColumns(ctx, rv.Q, rv.C, checked); err != nil {
			return nil, err
		}
	}
	seen := map[string]bool{}
	for _, f := range found {
		seen[pair(f)] = true
	}
	engineFound, err := EngineProblems(ctx, rv.Engine, rv.Log, definition, columns)
	if err != nil {
		return nil, err
	}
	for _, e := range engineFound {
		if !seen[pair(e)] {
			seen[pair(e)] = true
			found = append(found, e)
		}
	}
	if len(found) > 0 {
		checked = nil
	}
	return &Review{Definition: checked, Problems: found, Where: where}, nil
}

// Checked is checked: the definition to save, or the 422 saying every problem and where.
func (rv *Reviewer) Checked(ctx context.Context, definition any, current any) (pyjson.Object, error) {
	r, err := rv.Review(ctx, definition, current)
	if err != nil {
		return nil, err
	}
	if len(r.Problems) > 0 {
		return nil, r.Refusal()
	}
	return r.Definition, nil
}
