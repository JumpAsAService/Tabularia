// Package modelcatalog is the catalogue of the statistical models as the gateway reads it
// (docs/specs/statistical-models.md, «The catalogue»): the groups and their models, the
// parameters of a model node checked as the engine checks them, the variant they make, the
// Predict node's keys.
//
// The catalogue is declared once, in the engine (backend/app/engine/models/catalog.py);
// catalog.json is a copy of its snapshot, embedded so that the gateway is built from its own
// folder alone. .github/scripts/model_catalog.py writes the copy again and checks it. The
// same bytes go to every model Job (its ConfigMap's catalog.json: Raw).
//
// Check answers what the engine's check_params answers without the columns, word for word:
// both run backend/tests/fixtures/models/params_cases.json. The columns themselves (that they
// exist, their types) are the engine's to check, when it writes the step's input.
package modelcatalog

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

//go:embed catalog.json
var raw []byte

// NodeModel and NodePredict are the flow's node types of the models.
const (
	NodeModel   = "model"
	NodePredict = "model_predict"
)

// Raw is the snapshot, byte for byte: what a model Job receives as catalog.json.
func Raw() []byte { return append([]byte(nil), raw...) }

// Group is a group of models: one image, enabled by an administrator.
type Group struct {
	ID       string   `json:"id"`
	Language string   `json:"language"`
	Label    string   `json:"label"`
	Models   []string `json:"models"`
}

// Kind is how a parameter's value is read and drawn.
type Kind struct {
	Control   string   `json:"control"`
	Numeric   bool     `json:"numeric"`
	Predictor bool     `json:"predictor"`
	Types     []string `json:"types"`
	Of        string   `json:"of"`
	Choices   []string `json:"choices"`
	Above     *float64 `json:"above"`
	Below     *float64 `json:"below"`
	Min       *float64 `json:"min"`
	Max       *float64 `json:"max"`
	MaxCount  *int     `json:"max_count"`
}

// cond is one condition of a rule: a key and what its value must be (a value, a list of
// values, true: given, false: not given).
type cond struct {
	Key  string
	Rule any
}

// Rule is a model's `requires` or `forbids` entry.
type Rule struct {
	Key     string
	Value   any
	When    []cond
	Reason  string
	Message string
}

// Variant is one of a model's variants: when it holds, and what the runner writes.
type Variant struct {
	ID       string
	When     []cond
	Sections map[string]Section
	Outputs  map[string][]string
}

// Section is what a variant writes in a section of the ten columns.
type Section struct {
	Levels  []string `json:"levels"`
	Numbers []string `json:"numbers"`
	Texts   []string `json:"texts"`
}

// Model is one model of the catalogue.
type Model struct {
	ID, Group, Family, Label string
	Keys                     []string // needs then takes, in the form's order
	Needs                    map[string]bool
	KindOf                   map[string]string
	Defaults                 map[string]any
	Applies                  map[string][]cond
	Requires, Forbids        []Rule
	Disjoint                 []string
	Variants                 []Variant
}

type catalogue struct {
	groups  []Group
	models  []*Model
	byID    map[string]*Model
	kinds   map[string]Kind
	limits  map[string]int
	predict json.RawMessage
}

var data = mustParse(raw)

// Groups are the groups, in the catalogue's order.
func Groups() []Group {
	out := make([]Group, len(data.groups))
	for i, g := range data.groups {
		g.Models = append([]string(nil), g.Models...)
		out[i] = g
	}
	return out
}

// Models are the ids of the models, in the catalogue's order.
func Models() []string {
	out := make([]string, len(data.models))
	for i, m := range data.models {
		out[i] = m.ID
	}
	return out
}

// GroupOf is the group of a model, "" for no model.
func GroupOf(model string) string {
	if m, ok := data.byID[model]; ok {
		return m.Group
	}
	return ""
}

// Limit is one of the catalogue's limits (max_terms, max_chart_points…), 0 for none.
func Limit(name string) int { return data.limits[name] }

// Limits are all the catalogue's limits, a copy (what a model Job's task.json carries).
func Limits() map[string]int {
	out := make(map[string]int, len(data.limits))
	for k, v := range data.limits {
		out[k] = v
	}
	return out
}

// KindOf is the kind of a model's parameter, "" when the model does not take it.
func KindOf(model, key string) string {
	if m, ok := data.byID[model]; ok {
		return m.KindOf[key]
	}
	return ""
}

// Error is a refusal of the catalogue's check: the sentence the step fails with.
type Error struct{ Message string }

func (e *Error) Error() string { return e.Message }

func refuse(model, format string, args ...any) error {
	return &Error{Message: model + ": " + fmt.Sprintf(format, args...)}
}

// Check reads a model node's parameters as the engine does (without the columns): a key the
// model does not take is refused, as one it needs and lacks, one given while it does not
// apply, what `requires` and `forbids` say. The answer holds every key that applies, with
// its default when not given (a null is a key not given). Its numbers are float64, as JSON's.
func Check(model string, params map[string]any) (map[string]any, error) {
	m, ok := data.byID[model]
	if !ok {
		return nil, &Error{Message: fmt.Sprintf("there is no model '%s' (the models: %s)", model, strings.Join(Models(), ", "))}
	}
	given := map[string]any{}
	for k, v := range params {
		if v != nil {
			given[k] = v
		}
	}
	unknown := make([]string, 0)
	for k := range given {
		if _, ok := m.KindOf[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, refuse(model, "there is no parameter '%s' (it takes: %s)", unknown[0], strings.Join(m.Keys, ", "))
	}
	for _, k := range m.Keys {
		if _, ok := given[k]; m.Needs[k] && !ok {
			return nil, refuse(model, "'%s' is required", k)
		}
	}
	read := map[string]any{}
	for _, k := range m.Keys {
		if v, ok := given[k]; ok {
			r, err := readValue(model, k, data.kinds[m.KindOf[k]], v)
			if err != nil {
				return nil, err
			}
			read[k] = r
		}
	}
	full := map[string]any{}
	for _, k := range m.Keys {
		if v, ok := read[k]; ok {
			full[k] = v
		} else {
			full[k] = m.Defaults[k]
		}
	}
	for _, k := range m.Keys {
		if _, ok := read[k]; ok && !m.applies(k, full) {
			return nil, refuse(model, "'%s' only applies when %s", k, whenText(m.Applies[k]))
		}
	}
	for _, r := range m.Requires {
		if holdsAll(r.When, full) && !given_(full[r.Key]) {
			return nil, refuse(model, "%s", r.Message)
		}
	}
	for _, r := range m.Forbids {
		if same(full[r.Key], r.Value) && holdsAll(r.When, full) {
			return nil, refuse(model, "%s", r.Message)
		}
	}
	for _, k := range m.Keys {
		kind := data.kinds[m.KindOf[k]]
		v, ok := read[k]
		if !ok || (kind.Control != "reference" && kind.Control != "pairs") {
			continue
		}
		allowed := map[string]bool{}
		for _, c := range columnNames(data.kinds[m.KindOf[kind.Of]], full[kind.Of]) {
			allowed[c] = true
		}
		var named []string
		if kind.Control == "reference" {
			for c := range v.(map[string]any) {
				named = append(named, c)
			}
			sort.Strings(named)
		} else {
			for _, pair := range v.([]any) {
				for _, c := range pair.([]any) {
					named = append(named, c.(string))
				}
			}
		}
		for _, c := range named {
			if !allowed[c] {
				return nil, refuse(model, "'%s' names «%s», which is not one of the '%s'", k, c, kind.Of)
			}
		}
	}
	owner := map[string]string{}
	for _, k := range m.Disjoint {
		for _, c := range columnNames(data.kinds[m.KindOf[k]], full[k]) {
			if o, ok := owner[c]; ok {
				return nil, refuse(model, "the column «%s» is both '%s' and '%s': choose another", c, o, k)
			}
			owner[c] = k
		}
	}
	out := map[string]any{}
	for _, k := range m.Keys {
		if m.applies(k, full) {
			out[k] = full[k]
		}
	}
	return out, nil
}

// VariantOf is the variant a model's checked parameters make (unpenalized, penalized,
// manual, auto, default); nil for no model.
func VariantOf(model string, checked map[string]any) *Variant {
	m, ok := data.byID[model]
	if !ok {
		return nil
	}
	for i := range m.Variants {
		v := &m.Variants[i]
		match := true
		for _, c := range v.When {
			value, ok := checked[c.Key]
			if !ok {
				value = m.Defaults[c.Key]
			}
			if !holds(c.Rule, value) {
				match = false
				break
			}
		}
		if match {
			return v
		}
	}
	return nil
}

// Columns are the columns a model reads with these checked parameters, each once, in the
// form's order, a series' time column first: what its input keeps («Only the columns the
// model reads», docs/specs/statistical-models.md). Nil for no model.
func Columns(model string, checked map[string]any) []string {
	m, ok := data.byID[model]
	if !ok {
		return nil
	}
	var out []string
	seen := map[string]bool{}
	for _, timeFirst := range []bool{true, false} {
		for _, k := range m.Keys {
			kindName := m.KindOf[k]
			if (kindName == "time_column") != timeFirst {
				continue
			}
			for _, c := range columnNames(data.kinds[kindName], checked[k]) {
				if !seen[c] {
					seen[c] = true
					out = append(out, c)
				}
			}
		}
	}
	return out
}

// ── reading values ──────────────────────────────────────────────────────────

func (m *Model) applies(key string, full map[string]any) bool {
	rule, ok := m.Applies[key]
	return !ok || holdsAll(rule, full)
}

func holdsAll(when []cond, full map[string]any) bool {
	for _, c := range when {
		if !holds(c.Rule, full[c.Key]) {
			return false
		}
	}
	return true
}

func given_(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case []any:
		return len(x) > 0
	case []string:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// holds is the engine's _holds: a rule (true, false, a list, a value) against a value.
func holds(rule, value any) bool {
	switch r := rule.(type) {
	case bool:
		if r {
			return given_(value)
		}
		return !given_(value)
	case []any:
		for _, x := range r {
			if same(x, value) {
				return true
			}
		}
		return false
	}
	return same(rule, value)
}

// same compares two JSON values: texts, numbers, booleans and null.
func same(a, b any) bool {
	switch x := a.(type) {
	case string:
		y, ok := b.(string)
		return ok && x == y
	case float64:
		y, ok := b.(float64)
		return ok && x == y
	case bool:
		y, ok := b.(bool)
		return ok && x == y
	case nil:
		return b == nil
	}
	return false
}

func whenText(when []cond) string {
	parts := make([]string, 0, len(when))
	for _, c := range when {
		switch r := c.Rule.(type) {
		case bool:
			if r {
				parts = append(parts, fmt.Sprintf("'%s' is given", c.Key))
			} else {
				parts = append(parts, fmt.Sprintf("'%s' is not given", c.Key))
			}
		case []any:
			vals := make([]string, len(r))
			for i, x := range r {
				vals[i] = fmt.Sprint(x)
			}
			parts = append(parts, c.Key+" = "+strings.Join(vals, " or "))
		default:
			parts = append(parts, fmt.Sprintf("%s = %v", c.Key, r))
		}
	}
	return strings.Join(parts, " and ")
}

func columnNames(kind Kind, v any) []string {
	switch kind.Control {
	case "column":
		if s, ok := v.(string); ok {
			return []string{s}
		}
	case "columns":
		if list, ok := v.([]any); ok {
			out := make([]string, 0, len(list))
			for _, c := range list {
				if s, ok := c.(string); ok {
					out = append(out, s)
				}
			}
			return out
		}
	}
	return nil
}

func num(v float64) string {
	if v == math.Trunc(v) && math.Abs(v) < 1e15 {
		return strconv.FormatInt(int64(v), 10)
	}
	return strconv.FormatFloat(v, 'g', -1, 64)
}

func boundsText(k Kind) string {
	var parts []string
	if k.Above != nil {
		parts = append(parts, "above "+num(*k.Above))
	}
	if k.Min != nil {
		parts = append(parts, "at least "+num(*k.Min))
	}
	if k.Below != nil {
		parts = append(parts, "below "+num(*k.Below))
	}
	if k.Max != nil {
		parts = append(parts, "at most "+num(*k.Max))
	}
	return strings.Join(parts, " and ")
}

func within(k Kind, v float64) bool {
	return (k.Above == nil || v > *k.Above) && (k.Below == nil || v < *k.Below) &&
		(k.Min == nil || v >= *k.Min) && (k.Max == nil || v <= *k.Max)
}

func isNumber(v any) (float64, bool) {
	f, ok := v.(float64)
	return f, ok && !math.IsInf(f, 0) && !math.IsNaN(f)
}

func readLevel(model, key string, v any) (any, error) {
	switch x := v.(type) {
	case string:
		if strings.TrimSpace(x) == "" {
			return nil, refuse(model, "'%s' must be a value of the column", key)
		}
		return x, nil
	case bool:
		return x, nil
	}
	if f, ok := isNumber(v); ok {
		return f, nil
	}
	return nil, refuse(model, "'%s' must be a value of the column: a text, a number, true or false", key)
}

func withBounds(prefix string, k Kind) string {
	if b := boundsText(k); b != "" {
		return prefix + " " + b
	}
	return prefix
}

// readValue is the engine's read_value: a parameter's value as its kind's control reads it.
func readValue(model, key string, k Kind, v any) (any, error) {
	switch k.Control {
	case "column":
		if s, ok := v.(string); ok && s != "" {
			return s, nil
		}
		return nil, refuse(model, "'%s' must be the name of a column", key)
	case "columns":
		list, ok := v.([]any)
		if ok {
			for _, c := range list {
				if s, isStr := c.(string); !isStr || s == "" {
					ok = false
				}
			}
		}
		if !ok {
			return nil, refuse(model, "'%s' must be a list of column names", key)
		}
		seen := map[string]bool{}
		for _, c := range list {
			if seen[c.(string)] {
				return nil, refuse(model, "'%s' names a column twice", key)
			}
			seen[c.(string)] = true
		}
		if k.Min != nil && float64(len(list)) < *k.Min {
			plural := "s"
			if *k.Min == 1 {
				plural = ""
			}
			return nil, refuse(model, "'%s' must name at least %s column%s", key, num(*k.Min), plural)
		}
		if k.Max != nil && float64(len(list)) > *k.Max {
			return nil, refuse(model, "'%s' may name at most %s columns", key, num(*k.Max))
		}
		return append(make([]any, 0, len(list)), list...), nil
	case "level":
		return readLevel(model, key, v)
	case "reference":
		obj, ok := v.(map[string]any)
		if !ok {
			return nil, refuse(model, "'%s' must give a column its reference level, as {column: level}", key)
		}
		names := make([]string, 0, len(obj))
		for c := range obj {
			names = append(names, c)
		}
		sort.Strings(names)
		out := map[string]any{}
		for _, c := range names {
			level, err := readLevel(model, key+"."+c, obj[c])
			if err != nil {
				return nil, err
			}
			out[c] = level
		}
		return out, nil
	case "pairs":
		list, ok := v.([]any)
		if !ok {
			return nil, refuse(model, "'%s' must be a list of pairs of columns", key)
		}
		seen := map[[2]string]bool{}
		out := make([]any, 0, len(list))
		for _, p := range list {
			pair, ok := p.([]any)
			var a, b string
			if ok && len(pair) == 2 {
				a, _ = pair[0].(string)
				b, _ = pair[1].(string)
			}
			if !ok || len(pair) != 2 || a == "" || b == "" || a == b {
				return nil, refuse(model, "'%s' must be a list of pairs of two different columns", key)
			}
			id := [2]string{min(a, b), max(a, b)}
			if seen[id] {
				return nil, refuse(model, "'%s' names the pair «%s», «%s» twice", key, a, b)
			}
			seen[id] = true
			out = append(out, []any{a, b})
		}
		if k.Max != nil && float64(len(out)) > *k.Max {
			return nil, refuse(model, "'%s' may hold at most %s pairs", key, num(*k.Max))
		}
		return out, nil
	case "choice":
		if s, ok := v.(string); ok {
			for _, c := range k.Choices {
				if s == c {
					return s, nil
				}
			}
		}
		return nil, refuse(model, "'%s' must be one of %s", key, strings.Join(k.Choices, ", "))
	case "number":
		if f, ok := isNumber(v); ok && within(k, f) {
			return f, nil
		}
		return nil, refuse(model, "%s", withBounds(fmt.Sprintf("'%s' must be a number", key), k))
	case "integer":
		if f, ok := isNumber(v); ok && f == math.Floor(f) && within(k, f) {
			return f, nil
		}
		return nil, refuse(model, "'%s' must be a whole number from %s to %s", key, num(*k.Min), num(*k.Max))
	case "boolean":
		if b, ok := v.(bool); ok {
			return b, nil
		}
		return nil, refuse(model, "'%s' must be true or false", key)
	case "numbers":
		list, ok := v.([]any)
		values := make([]float64, 0, len(list))
		if ok {
			for _, x := range list {
				f, isNum := isNumber(x)
				if !isNum || !within(k, f) {
					ok = false
					break
				}
				values = append(values, f)
			}
		}
		if !ok {
			return nil, refuse(model, "'%s' must be a list of numbers, each %s", key, boundsText(k))
		}
		seen := map[float64]bool{}
		for _, f := range values {
			if seen[f] {
				return nil, refuse(model, "'%s' holds a number twice", key)
			}
			seen[f] = true
		}
		if k.MaxCount != nil && len(values) > *k.MaxCount {
			return nil, refuse(model, "'%s' may hold at most %d numbers", key, *k.MaxCount)
		}
		sort.Float64s(values)
		out := make([]any, len(values))
		for i, f := range values {
			out[i] = f
		}
		return out, nil
	case "number_or_choice":
		if s, ok := v.(string); ok {
			for _, c := range k.Choices {
				if s == c {
					return s, nil
				}
			}
		}
		if f, ok := isNumber(v); ok && within(k, f) {
			return f, nil
		}
		return nil, refuse(model, "%s", withBounds(fmt.Sprintf("'%s' must be one of %s, or a number", key, strings.Join(k.Choices, ", ")), k))
	}
	panic("modelcatalog: unknown control " + k.Control)
}

// ── parsing the snapshot ────────────────────────────────────────────────────

// ordered reads a JSON object's keys in their order, with their raw values.
func ordered(rawObject json.RawMessage) ([]string, map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(rawObject))
	t, err := dec.Token()
	if err != nil || t != json.Delim('{') {
		return nil, nil, fmt.Errorf("not an object")
	}
	var keys []string
	values := map[string]json.RawMessage{}
	for dec.More() {
		k, err := dec.Token()
		if err != nil {
			return nil, nil, err
		}
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, nil, err
		}
		keys = append(keys, k.(string))
		values[k.(string)] = v
	}
	return keys, values, nil
}

func conds(rawObject json.RawMessage) ([]cond, error) {
	keys, values, err := ordered(rawObject)
	if err != nil {
		return nil, err
	}
	out := make([]cond, 0, len(keys))
	for _, k := range keys {
		var rule any
		if err := json.Unmarshal(values[k], &rule); err != nil {
			return nil, err
		}
		out = append(out, cond{Key: k, Rule: rule})
	}
	return out, nil
}

func rules(raws []json.RawMessage) ([]Rule, error) {
	out := make([]Rule, 0, len(raws))
	for _, r := range raws {
		var x struct {
			Key     string          `json:"key"`
			Value   any             `json:"value"`
			When    json.RawMessage `json:"when"`
			Reason  string          `json:"reason"`
			Message string          `json:"message"`
		}
		if err := json.Unmarshal(r, &x); err != nil {
			return nil, err
		}
		when, err := conds(x.When)
		if err != nil {
			return nil, err
		}
		out = append(out, Rule{Key: x.Key, Value: x.Value, When: when, Reason: x.Reason, Message: x.Message})
	}
	return out, nil
}

func mustParse(b []byte) *catalogue {
	c, err := parse(b)
	if err != nil {
		panic("modelcatalog: catalog.json: " + err.Error())
	}
	return c
}

func parse(b []byte) (*catalogue, error) {
	var top struct {
		Groups  []Group                      `json:"groups"`
		Kinds   map[string]Kind              `json:"kinds"`
		Limits  map[string]int               `json:"limits"`
		Models  []map[string]json.RawMessage `json:"models"`
		Predict json.RawMessage              `json:"predict"`
	}
	if err := json.Unmarshal(b, &top); err != nil {
		return nil, err
	}
	c := &catalogue{groups: top.Groups, kinds: top.Kinds, limits: top.Limits, predict: top.Predict, byID: map[string]*Model{}}
	for _, rm := range top.Models {
		m := &Model{Needs: map[string]bool{}, KindOf: map[string]string{}, Applies: map[string][]cond{}}
		for field, dst := range map[string]*string{"id": &m.ID, "group": &m.Group, "family": &m.Family, "label": &m.Label} {
			if err := json.Unmarshal(rm[field], dst); err != nil {
				return nil, fmt.Errorf("a model's %s: %v", field, err)
			}
		}
		for _, part := range []string{"needs", "takes"} {
			keys, values, err := ordered(rm[part])
			if err != nil {
				return nil, fmt.Errorf("%s.%s: %v", m.ID, part, err)
			}
			for _, k := range keys {
				var kind string
				if err := json.Unmarshal(values[k], &kind); err != nil {
					return nil, err
				}
				if _, ok := c.kinds[kind]; !ok {
					return nil, fmt.Errorf("%s.%s: unknown kind %q", m.ID, k, kind)
				}
				m.Keys = append(m.Keys, k)
				m.KindOf[k] = kind
				m.Needs[k] = part == "needs"
			}
		}
		if err := json.Unmarshal(rm["defaults"], &m.Defaults); err != nil {
			return nil, fmt.Errorf("%s.defaults: %v", m.ID, err)
		}
		keys, values, err := ordered(rm["applies"])
		if err != nil {
			return nil, fmt.Errorf("%s.applies: %v", m.ID, err)
		}
		for _, k := range keys {
			if m.Applies[k], err = conds(values[k]); err != nil {
				return nil, err
			}
		}
		for field, dst := range map[string]*[]Rule{"requires": &m.Requires, "forbids": &m.Forbids} {
			var raws []json.RawMessage
			if err := json.Unmarshal(rm[field], &raws); err != nil {
				return nil, fmt.Errorf("%s.%s: %v", m.ID, field, err)
			}
			if *dst, err = rules(raws); err != nil {
				return nil, fmt.Errorf("%s.%s: %v", m.ID, field, err)
			}
		}
		if err := json.Unmarshal(rm["disjoint"], &m.Disjoint); err != nil {
			return nil, err
		}
		var variants []struct {
			ID       string              `json:"id"`
			When     json.RawMessage     `json:"when"`
			Sections map[string]Section  `json:"sections"`
			Outputs  map[string][]string `json:"outputs"`
		}
		if err := json.Unmarshal(rm["variants"], &variants); err != nil {
			return nil, fmt.Errorf("%s.variants: %v", m.ID, err)
		}
		for _, v := range variants {
			when, err := conds(v.When)
			if err != nil {
				return nil, err
			}
			m.Variants = append(m.Variants, Variant{ID: v.ID, When: when, Sections: v.Sections, Outputs: v.Outputs})
		}
		if m.ID == "" || len(m.Variants) == 0 {
			return nil, fmt.Errorf("a model without id or variants")
		}
		c.models = append(c.models, m)
		c.byID[m.ID] = m
	}
	if len(c.groups) == 0 || len(c.models) == 0 {
		return nil, fmt.Errorf("no group or no model")
	}
	for _, m := range c.models {
		found := false
		for _, g := range c.groups {
			for _, id := range g.Models {
				found = found || (g.ID == m.Group && id == m.ID)
			}
		}
		if !found {
			return nil, fmt.Errorf("the model %s is not listed by its group %s", m.ID, m.Group)
		}
	}
	return c, nil
}
