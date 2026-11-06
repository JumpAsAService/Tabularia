package reports

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportdef"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
)

// Formats are FORMATS.
var Formats = []string{"pdf", "xlsx", "html", "svg"}

const (
	MaxChoices    = 1000
	MaxStateBytes = 64 * 1024
)

// HasComment is ai_comment.has_comment.
func HasComment(definition any) bool {
	for _, kv := range ElementsOf(definition) {
		if get(kv.Value.(pyjson.Object), "type") == "comment" {
			return true
		}
	}
	return false
}

// CheckDatasetChoices is check_dataset_choices: a value of a parameter whose choices come from a
// dataset must be one of them, when the engine can say and the list is whole.
func (rs *Resolver) CheckDatasetChoices(ctx context.Context, def pyjson.Object, values pyjson.Object, cache pyjson.Object, ofDashboard bool) error {
	for _, p := range parameters(def) {
		source := choicesDataset(p)
		name := pystr.Str(get(p, "name"))
		value := get(values, name)
		if l, isList := value.([]any); !pystr.Truthy(source) || value == nil || (isList && len(l) == 0) {
			continue
		}
		got, err := rs.DatasetChoices(ctx, def, values, p, cache, ofDashboard)
		if err != nil {
			var r *Refusal
			var u *Unreadable
			status := 0
			if errors.As(err, &r) {
				status = r.Status
			} else if errors.As(err, &u) {
				status = 422
			} else {
				return err
			}
			if status >= 500 && cache == nil {
				return err
			}
			rs.Log.Info("report: the choices were not read to check its value", "parameter", name, "status", status)
			continue
		}
		if got == nil || got.Truncated || !got.Whole {
			continue
		}
		allowed := map[string]bool{}
		for _, c := range got.Choices {
			v, bad := reportparams.Convert(p, asText(get(obj(c), "value")))
			if bad == nil {
				allowed[reportparams.Key(v)] = true
			}
		}
		vs, isList := value.([]any)
		if !isList {
			vs = []any{value}
		}
		for _, v := range vs {
			if !allowed[reportparams.Key(v)] {
				return BadValue(name, fmt.Sprintf("'%s' is not one of its choices", pystr.Str(reportparams.ToJSON(v))))
			}
		}
	}
	return nil
}

// Choices is what dataset_choices answers.
type Choices struct {
	Choices   []any
	Truncated bool
	Whole     bool
	Problem   string // why they cannot be read (_unread)
}

// DatasetChoices is dataset_choices for a dataset that reads a datasource or a view: the choices of a parameter, read by the engine with the
// person's rights; nil when the engine has no such route.
func (rs *Resolver) DatasetChoices(ctx context.Context, def pyjson.Object, values pyjson.Object, param pyjson.Object, cache pyjson.Object,
	ofDashboard bool) (*Choices, error) {
	choices := obj(get(param, "choices"))
	dataset := pystr.Str(get(choices, "dataset"))
	ignoring := NotFollowed(def, param)
	reading := append(pyjson.Object{}, values...)
	for _, p := range parameters(def) {
		n := pystr.Str(get(p, "name"))
		if ignoring[n] {
			if pystr.Truthy(get(p, "multi")) {
				reading = reading.Set(n, []any{})
			} else {
				reading = reading.Set(n, nil)
			}
		}
	}
	left := map[string]bool{}
	datasets, err := rs.ResolveDatasetsIgnoring(ctx, def, reading, map[string]bool{dataset: true}, nil, ignoring, left)
	if err != nil {
		return nil, err
	}
	params := pyjson.Object{}
	for _, kv := range reading {
		params = append(params, pyjson.KV{Key: kv.Key, Value: reportparams.ToJSON(kv.Value)})
	}
	locale := or(get(def, "locale"), "en")
	body := pyjson.Object{{Key: "definition", Value: reportdef.WithoutImages(def)}, {Key: "dataset", Value: dataset},
		{Key: "datasets", Value: pyjson.Object{{Key: dataset, Value: get(datasets, dataset)}}}, {Key: "params", Value: params},
		{Key: "value", Value: get(choices, "value")}, {Key: "label", Value: get(choices, "label")}, {Key: "limit", Value: int64(MaxChoices)},
		{Key: "locale", Value: locale}}
	if cache != nil {
		body = append(body, pyjson.KV{Key: "cache", Value: cache})
	}
	if ofDashboard {
		body = append(body, pyjson.KV{Key: "of_dashboard", Value: true})
	}
	cctx, cancel := context.WithTimeout(ctx, 300*time.Second)
	defer cancel()
	res, raw, err := rs.Engine.JSON(cctx, http.MethodPost, "/reports/choices", body)
	if err != nil {
		return nil, err
	}
	if res.StatusCode == 404 || res.StatusCode == 405 {
		return nil, nil
	}
	name := pystr.Str(get(param, "name"))
	if res.StatusCode >= 400 && left[name] { // a NULL the database does not take there
		return unread(name), nil
	}
	if res.StatusCode >= 400 {
		return nil, EngineFailure(res.StatusCode, raw, func(d string) string { return fmt.Sprintf("The choices of «%s»: %s", name, pystr.Within(d)) })
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, ErrInternal
	}
	answer, ok := v.(pyjson.Object)
	if !ok {
		return nil, ErrInternal
	}
	var found []any
	for _, c := range list(or(get(answer, "choices"), []any{})) {
		if _, ok := c.(pyjson.Object); ok && len(found) < MaxChoices {
			found = append(found, c)
		}
	}
	if len(found) == 0 && left[name] {
		return unread(name), nil
	}
	return &Choices{Choices: found, Truncated: pystr.Truthy(get(answer, "truncated")), Whole: len(left) == 0}, nil
}

// unread is _unread: the choices of a parameter whose dataset writes it outside a condition,
// where it has no value yet.
func unread(param string) *Choices {
	name := "{" + param + "}"
	return &Choices{Choices: []any{}, Problem: fmt.Sprintf("The choices of «%s» cannot be read: their dataset writes %s outside a WHERE, "+
		"HAVING or ON condition, where it has no value yet. Move it into a condition, such as %s IS "+
		"NULL OR a_column = %s, or read the choices from a dataset of their own", param, name, name, name)}
}

// Prepare is prepare: the parameters' values (400 naming the one that does not fit) and every
// dataset resolved with the person's rights; the converted values go in the audit's detail.
func (rs *Resolver) Prepare(ctx context.Context, def pyjson.Object, given Given, detail *pyjson.Object) (pyjson.Object, pyjson.Object, error) {
	values, err := rs.ValuesFor(ctx, def, given)
	if err != nil {
		return nil, nil, err
	}
	if detail != nil {
		shown := pyjson.Object{}
		for _, kv := range values {
			shown = append(shown, pyjson.KV{Key: kv.Key, Value: Shown(reportparams.ToJSON(kv.Value))})
		}
		*detail = detail.Set("parameters", shown)
	}
	datasets, err := rs.ResolveDatasets(ctx, def, values, nil, nil)
	if err != nil {
		return nil, nil, err
	}
	if err := rs.CheckDatasetChoices(ctx, def, values, nil, false); err != nil {
		return nil, nil, err
	}
	return values, datasets, nil
}

// Shown is _shown: a value as the audit keeps it, a long text cut, a long list its first values.
func Shown(v any) any {
	switch x := v.(type) {
	case []any:
		out := []any{}
		for i, e := range x {
			if i == 50 {
				out = append(out, "…")
				break
			}
			out = append(out, Shown(e))
		}
		return out
	case string:
		return pystr.Cut(x, 200)
	}
	return v
}

// GivenDetail is given_detail: the values as given, a few and cut short.
func GivenDetail(given Given) pyjson.Object {
	out := pyjson.Object{}
	for i, kv := range given {
		if i == 40 {
			break
		}
		vs := list(kv.Value)
		var texts []any
		for j, v := range vs {
			if j == 20 {
				break
			}
			texts = append(texts, pystr.Cut(pystr.Str(v), 200))
		}
		if texts == nil {
			texts = []any{}
		}
		out = out.Set(pystr.Cut(kv.Key, 64), texts)
	}
	return out
}

// CheckState is check_state: the on-screen view a browser sends.
func CheckState(state pyjson.Object, present bool) error {
	if !present {
		return nil
	}
	if s, _ := pyjson.Dumps(state, pyjson.Options{EnsureASCII: true}); len(s) > MaxStateBytes {
		return refusal(422, "The view's state is too large")
	}
	items, has := state.Get("items")
	if !has {
		return nil
	}
	o, ok := items.(pyjson.Object)
	if !ok {
		return refusal(422, "The view's state has its items as an object by item's id")
	}
	for _, kv := range o {
		if _, ok := kv.Value.(pyjson.Object); !ok {
			return refusal(422, "The view's state has its items as an object by item's id")
		}
	}
	return nil
}

var (
	badFileChars = regexp.MustCompile(`[\\/:*?"<>|\x00-\x1f]+`)
	pySpaces     = regexp.MustCompile(`[\s\x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+`)
)

// Filename is filename: the file's name, the object's without what a file name cannot hold.
func Filename(name string, fmtName string) string { return FilenameOr(name, fmtName, "report") }

// FilenameOr is Filename with another name for an object that has none (a SQL script's
// export_filename: «sql-script»).
func FilenameOr(name, fmtName, fallback string) string {
	n := badFileChars.ReplaceAllString(name, " ")
	n = strings.Trim(pySpaces.ReplaceAllString(n, " "), " .")
	if n == "" {
		n = fallback
	}
	return n + "." + fmtName
}

// Started is what a render answers: the export to follow, its file's name, the audit's detail.
type Started struct {
	TaskID   any
	Filename any
	Detail   pyjson.Object
}

// RenderRequest is what reports.render takes.
type RenderRequest struct {
	Name     *string // the report's, nil for a new one in the editor
	Format   string
	Given    Given
	State    pyjson.Object
	HasState bool
	Origin   string
	Owner    string
	Wait     int
	// Comments and Reading are the AI comments the gateway got for this document (a share
	// link's, docs/specs/share-links.md): sent to the engine only when not nil
	Comments pyjson.Object
	Reading  any
}

// Render is render (without the audit, which the caller writes with the outcome and the detail):
// the render of `def` with the values given, read with the person's rights, started on the
// engine as an export of theirs. The detail is filled whatever happens.
func (rs *Resolver) Render(ctx context.Context, def pyjson.Object, req RenderRequest) (*Started, pyjson.Object, error) {
	detail := pyjson.Object{{Key: "format", Value: req.Format}, {Key: "from", Value: req.Origin}, {Key: "parameters", Value: GivenDetail(req.Given)}}
	started, err := func() (*Started, error) {
		values, datasets, err := rs.Prepare(ctx, def, req.Given, &detail)
		if err != nil {
			return nil, err
		}
		title := ""
		if req.Name != nil {
			title = *req.Name
		}
		name := Filename(title, req.Format)
		params := pyjson.Object{}
		for _, kv := range values {
			params = append(params, pyjson.KV{Key: kv.Key, Value: reportparams.ToJSON(kv.Value)})
		}
		state := req.State
		if !req.HasState || state == nil {
			state = pyjson.Object{}
		}
		body := pyjson.Object{{Key: "definition", Value: def}, {Key: "datasets", Value: datasets}, {Key: "params", Value: params},
			{Key: "format", Value: req.Format}, {Key: "filename", Value: name}, {Key: "owner", Value: req.Owner},
			{Key: "wait_seconds", Value: int64(req.Wait)}, {Key: "locale", Value: or(get(def, "locale"), "en")}, {Key: "state", Value: state}}
		if req.Name != nil {
			body = append(body, pyjson.KV{Key: "title", Value: *req.Name})
		}
		if req.Comments != nil {
			body = append(body, pyjson.KV{Key: "comments", Value: req.Comments}, pyjson.KV{Key: "reading", Value: req.Reading})
		}
		cctx, cancel := context.WithTimeout(ctx, time.Duration(60+req.Wait)*time.Second)
		defer cancel()
		res, raw, err := rs.Engine.JSON(cctx, http.MethodPost, "/reports/render", body)
		if err != nil {
			return nil, &EngineDown{err}
		}
		if res.StatusCode >= 400 {
			return nil, EngineFailure(res.StatusCode, raw, pystr.Sentence)
		}
		v, err := pyjson.Loads(string(raw))
		if err != nil {
			return nil, ErrInternal
		}
		answer, ok := v.(pyjson.Object)
		if !ok {
			return nil, ErrInternal
		}
		task, has := answer.Get("task_id")
		if !has {
			return nil, ErrInternal
		}
		file := or(get(answer, "filename"), name)
		return &Started{TaskID: task, Filename: file}, nil
	}()
	if err != nil {
		var r *Refusal
		var u *Unreadable
		var down *EngineDown
		switch {
		case errors.As(err, &r), errors.As(err, &u):
			detail = detail.Set("error", pystr.Cut(Text(err), 500))
		case errors.As(err, &down):
			detail = detail.Set("error", "The engine is not answering")
		}
		return nil, detail, err
	}
	started.Detail = detail
	return started, detail, nil
}

// EngineDown is httpx.TransportError: the gateway's handler answers it with a 503.
type EngineDown struct{ Err error }

func (e *EngineDown) Error() string { return "the engine is not answering: " + e.Err.Error() }

// ── the form's parameters ───────────────────────────────────────────────────

// ParameterOut is routes/reports.py ParameterOut.
type ParameterOut struct {
	Name             string   `json:"name"`
	Label            any      `json:"label"`
	Type             any      `json:"type"`
	Multi            bool     `json:"multi"`
	Required         bool     `json:"required"`
	Default          any      `json:"default"`
	Choices          []Choice `json:"choices"`
	ChoicesTruncated bool     `json:"choices_truncated"`
	ChoicesError     *string  `json:"choices_error"`
}

// Choice is routes/reports.py Choice.
type Choice struct {
	Value any     `json:"value"`
	Label *string `json:"label"`
}

func (rs *Resolver) valueOf(ctx context.Context, def pyjson.Object, p pyjson.Object, one Given) (any, error) {
	single := append(pyjson.Object{}, def...).Set("parameters", []any{p})
	values, err := rs.ValuesFor(ctx, single, one)
	if err != nil {
		var r *Refusal
		if errors.As(err, &r) {
			if pystr.Truthy(get(p, "multi")) {
				return []any{}, nil
			}
			return nil, nil
		}
		return nil, err
	}
	return get(values, pystr.Str(get(p, "name"))), nil
}

// ParametersOut is routes/reports.py _parameters_out: the parameters as the form shows them,
// defaults resolved, choices read (`choicesFrom`: the definition a dataset's choices are read
// with, by its name; it may refuse with a Problem).
func (rs *Resolver) ParametersOut(ctx context.Context, def pyjson.Object, given Given, choicesFrom func(string) (pyjson.Object, error),
	unreadableSaid string) ([]ParameterOut, error) {
	return rs.ParametersOutCached(ctx, def, given, choicesFrom, nil, false)
}

// ParametersOutCached is _parameters_out with a saved dashboard's `cache` (its lists kept and
// shared) and `ofDashboard`.
func (rs *Resolver) ParametersOutCached(ctx context.Context, def pyjson.Object, given Given, choicesFrom func(string) (pyjson.Object, error),
	cache pyjson.Object, ofDashboard bool) ([]ParameterOut, error) {
	unreadableSaid := ""
	if cache != nil {
		unreadableSaid = "You cannot read the list of this parameter: ask whoever manages its data"
	}
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
	values := pyjson.Object{}
	for _, p := range params {
		name := pystr.Str(get(p, "name"))
		one := Given{}
		if g, ok := given.Get(name); ok {
			one = Given{{Key: name, Value: g}}
		}
		v, err := rs.valueOf(ctx, def, p, one)
		if err != nil {
			return nil, err
		}
		values = values.Set(name, v)
	}
	defaults := map[string]any{}
	for _, p := range params {
		name := pystr.Str(get(p, "name"))
		if _, ok := given.Get(name); !ok {
			defaults[name] = get(values, name)
			continue
		}
		v, err := rs.valueOf(ctx, def, p, Given{})
		if err != nil {
			return nil, err
		}
		defaults[name] = v
	}
	out := []ParameterOut{}
	for _, p := range params {
		name := pystr.Str(get(p, "name"))
		item := ParameterOut{Name: name, Label: or(get(p, "label"), name), Type: get(p, "type"), Multi: pystr.Truthy(get(p, "multi")),
			Required: pystr.Truthy(get(p, "required")), Default: reportparams.ToJSON(defaults[name])}
		choices := obj(or(get(p, "choices"), pyjson.Object{}))
		if fixed, has := choices.Get("values"); has {
			item.Choices = []Choice{}
			for _, v := range list(fixed) {
				label := pystr.Str(v)
				item.Choices = append(item.Choices, Choice{Value: v, Label: &label})
			}
		} else if ds := get(choices, "dataset"); pystr.Truthy(ds) {
			setErr := func(m string) { item.ChoicesError = &m }
			err := func() error {
				from, err := choicesFrom(pystr.Str(ds))
				if err != nil {
					return err
				}
				got, err := rs.DatasetChoices(ctx, from, values, p, cache, ofDashboard)
				if err != nil {
					return err
				}
				if got == nil {
					setErr("The engine cannot read choices from a dataset yet")
					return nil
				}
				if got.Problem != "" {
					setErr(got.Problem)
					return nil
				}
				item.Choices = []Choice{}
				for _, c := range got.Choices {
					co := obj(c)
					var label *string
					if l := get(co, "label"); l != nil {
						s := pystr.Str(l)
						label = &s
					}
					item.Choices = append(item.Choices, Choice{Value: get(co, "value"), Label: label})
				}
				item.ChoicesTruncated = got.Truncated
				return nil
			}()
			if err != nil {
				var u *Unreadable
				var r *Refusal
				p, isProblem := reportdef.AsProblem(err)
				switch {
				case errors.As(err, &u):
					if unreadableSaid != "" {
						setErr(unreadableSaid)
					} else {
						setErr(pystr.Cut(u.Message, 500))
					}
				case errors.As(err, &r):
					setErr(pystr.Cut(Text(r), 500))
				case isProblem:
					setErr(pystr.Cut(p.Message, 500))
				default:
					return nil, err
				}
			}
		}
		out = append(out, item)
	}
	return out, nil
}

// ── a dataset's fields and rows, for the editor ─────────────────────────────

// neutral is _neutral: a value of the parameter's type.
func neutral(p pyjson.Object, today reportparams.Date) any {
	var v any
	switch get(p, "type") {
	case "text":
		v = ""
	case "number":
		v = int64(0)
	case "date":
		v = today
	case "boolean":
		v = false
	}
	if pystr.Truthy(get(p, "multi")) {
		return []any{v}
	}
	return v
}

// Field is a column of a dataset: its name and type.
type Field struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// Fields is fields: the columns of a dataset, read with the person's rights; the parameters
// take their defaults, or a neutral value of their type.
func (rs *Resolver) Fields(ctx context.Context, def pyjson.Object, dataset, slot string) ([]Field, error) {
	var ds pyjson.Object
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		if pystr.Eq(get(obj(d), "name"), dataset) {
			ds = obj(d)
			break
		}
	}
	if ds == nil {
		return nil, refusal(404, "The report has no dataset «%s»", dataset)
	}
	values := pyjson.Object{}
	now := time.Now().UTC()
	today := reportparams.Date{T: time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)}
	for _, p := range parameters(def) {
		v, err := rs.valueOf(ctx, def, p, Given{})
		if err != nil {
			return nil, err
		}
		if l, isList := v.([]any); v == nil || (isList && len(l) == 0) {
			v = neutral(p, today)
		}
		values = values.Set(pystr.Str(get(p, "name")), v)
	}
	resolvedAll, err := rs.ResolveDatasets(ctx, def, values, map[string]bool{dataset: true}, nil)
	if err != nil {
		return nil, err
	}
	resolved := obj(get(resolvedAll, dataset))
	src := obj(get(ds, "source"))
	if get(src, "kind") == "datasource" {
		target, err := GetDatasource(ctx, rs.Q, asID(get(src, "datasource_id")))
		if err != nil {
			return nil, err
		}
		if target != nil {
			text := target.Columns
			if text == "" {
				text = "[]"
			}
			if v, err := pyjson.Loads(text); err == nil && pystr.Truthy(v) {
				var out []Field
				for _, c := range list(v) {
					if co, ok := c.(pyjson.Object); ok {
						out = append(out, Field{pystr.Str(get(co, "name")), FieldType(pystr.Str(or(get(co, "dtype"), "")))})
					}
				}
				if out == nil {
					out = []Field{}
				}
				return out, nil
			}
		}
	}
	path := "/tasks/preview"
	body := append(pyjson.Object{}, resolved...)
	if _, has := resolved.Get("connection"); has {
		path = "/db/query"
		body = body.Set("limit", int64(1)).Set("slot", slot)
	} else {
		body = body.Set("limit", int64(1)).Set("no_cache", true).Set("slot", slot)
	}
	cctx, cancel := context.WithTimeout(ctx, 300*time.Second)
	defer cancel()
	res, raw, err := rs.Engine.JSON(cctx, http.MethodPost, path, body)
	if err != nil {
		return nil, refusal(502, "The engine is not answering: %s", errName(err))
	}
	if res.StatusCode >= 400 {
		return nil, InDataset(dataset, EngineFailure(res.StatusCode, raw, func(d string) string { return d }))
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, ErrInternal
	}
	out := []Field{}
	for _, c := range list(or(get(obj(v), "columns"), []any{})) {
		if co, ok := c.(pyjson.Object); ok {
			out = append(out, Field{pystr.Str(get(co, "name")), FieldType(pystr.Str(or(get(co, "dtype"), "")))})
		}
	}
	return out, nil
}

// CountSeconds is COUNT_SECONDS.
const CountSeconds = 20

// ToCount is to_count: the dataset as a render with the defaults reads it, to be counted; nil
// when the count could not mean anything or the dataset cannot be read.
func (rs *Resolver) ToCount(ctx context.Context, def pyjson.Object, dataset string) (pyjson.Object, error) {
	var ds pyjson.Object
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		if pystr.Eq(get(obj(d), "name"), dataset) {
			ds = obj(d)
			break
		}
	}
	if ds == nil {
		return nil, nil
	}
	params := map[string]bool{}
	values := pyjson.Object{}
	missing := map[string]bool{}
	for _, p := range parameters(def) {
		name := pystr.Str(get(p, "name"))
		params[name] = true
		single := append(pyjson.Object{}, def...).Set("parameters", []any{p})
		v, err := rs.ValuesFor(ctx, single, Given{})
		if err != nil {
			var r *Refusal
			if !errors.As(err, &r) {
				return nil, err
			}
			if pystr.Truthy(get(p, "multi")) {
				values = values.Set(name, []any{})
			} else {
				values = values.Set(name, nil)
			}
			missing[name] = true
			continue
		}
		values = values.Set(name, get(v, name))
	}
	refused := func(err error) bool {
		var r *Refusal
		var u *Unreadable
		var s *reportparams.Refusal
		return errors.As(err, &r) || errors.As(err, &u) || errors.As(err, &s)
	}
	if len(missing) > 0 {
		src := obj(get(ds, "source"))
		var used map[string]bool
		if k := get(src, "kind"); k == "sql" || k == "sql_script" {
			s, err := SQLOf(ctx, rs.Q, rs.C, src)
			if err != nil {
				if refused(err) {
					return nil, nil
				}
				return nil, err
			}
			u, _, r := reportparams.CheckSQL(s.SQL, s.Dialect(), params)
			if r != nil {
				return nil, nil
			}
			used = u
		} else {
			used = FilterParams(src)
		}
		for u := range used {
			if missing[u] {
				return nil, nil
			}
		}
	}
	resolved, err := rs.ResolveDatasets(ctx, def, values, map[string]bool{dataset: true}, nil)
	if err != nil {
		if refused(err) {
			return nil, nil
		}
		return nil, err
	}
	return obj(get(resolved, dataset)), nil
}

// CountRows is count_rows: the rows of a dataset ToCount resolved, counted by the engine up to
// the cap a render reads; nil when they could not be counted.
func (rs *Resolver) CountRows(ctx context.Context, resolved pyjson.Object, slot string) pyjson.Object {
	if resolved == nil {
		return nil
	}
	cctx, cancel := context.WithTimeout(ctx, (CountSeconds+10)*time.Second)
	defer cancel()
	res, raw, err := rs.Engine.JSON(cctx, http.MethodPost, "/reports/count", pyjson.Object{{Key: "dataset", Value: resolved},
		{Key: "slot", Value: slot}, {Key: "seconds", Value: int64(CountSeconds)}})
	if err != nil || res.StatusCode >= 400 {
		return nil
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil
	}
	got, ok := v.(pyjson.Object)
	if !ok {
		return nil
	}
	rows, okR := pystr.Number(get(got, "rows"))
	maxRows, okM := pystr.Number(get(got, "max_rows"))
	_, hasOver := got.Get("over")
	if !okR || !okM || !hasOver {
		return nil
	}
	return pyjson.Object{{Key: "rows", Value: int64(rows)}, {Key: "over", Value: pystr.Truthy(get(got, "over"))},
		{Key: "max_rows", Value: int64(maxRows)}}
}
