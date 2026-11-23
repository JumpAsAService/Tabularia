package modelcatalog

import (
	"bytes"
	"encoding/json"
	"strings"
)

// The group `bayes` (docs/specs/bayes-report.md, lot Q0; docs/specs/bayes-contract.md, lot K0b) has its model, `bayes_model`, in
// the models' catalogue, and its card's `bayes` object in card.schema.json. Every other group's model Job receives a VIEW of
// both without what is the group's, filtered in the order of the file, so that their ConfigMaps and RunnerHash keep their
// bytes; the group `bayes` receives them whole. What is the group's:
//
//   - the group of that id, in `groups`, and every model of it, in `models`;
//   - in `limits`, the keys that start with «<group>_»;
//   - in `section_shapes`, `kinds` and `output_types`, the entry that the group's models name (as a key or as a value) and that
//     no other model, no `predict` and no `nodes` names, nor a kind that stays («of»): a shared entry stays;
//   - in the card schema, the property named as the group (and its name in `required`), and the `allOf` rule that names that
//     property or one of the group's models.
//
// A catalogue and a schema that hold nothing of the group are returned as they are, byte for byte.

// ownedSections are the catalogue's sections whose entries a group's models can own by naming them alone.
var ownedSections = []string{"section_shapes", "kinds", "output_types"}

// obj is a JSON object that keeps the order of its keys.
type obj struct {
	keys []string
	vals map[string]any
}

func (o *obj) set(k string, v any) {
	if _, ok := o.vals[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.vals[k] = v
}

func (o *obj) del(k string) bool {
	if _, ok := o.vals[k]; !ok {
		return false
	}
	delete(o.vals, k)
	for i, x := range o.keys {
		if x == k {
			o.keys = append(o.keys[:i:i], o.keys[i+1:]...)
			break
		}
	}
	return true
}

func parseOrdered(b []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	return readOrdered(dec)
}

func readOrdered(dec *json.Decoder) (any, error) {
	t, err := dec.Token()
	if err != nil {
		return nil, err
	}
	d, ok := t.(json.Delim)
	if !ok {
		return t, nil
	}
	if d == '{' {
		o := &obj{vals: map[string]any{}}
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return nil, err
			}
			v, err := readOrdered(dec)
			if err != nil {
				return nil, err
			}
			o.set(kt.(string), v)
		}
		_, err := dec.Token()
		return o, err
	}
	list := []any{}
	for dec.More() {
		v, err := readOrdered(dec)
		if err != nil {
			return nil, err
		}
		list = append(list, v)
	}
	_, err = dec.Token()
	return list, err
}

func writeOrdered(buf *bytes.Buffer, v any) {
	switch x := v.(type) {
	case *obj:
		buf.WriteByte('{')
		for i, k := range x.keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeOrdered(buf, k)
			buf.WriteByte(':')
			writeOrdered(buf, x.vals[k])
		}
		buf.WriteByte('}')
	case []any:
		buf.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeOrdered(buf, e)
		}
		buf.WriteByte(']')
	default:
		enc := json.NewEncoder(buf)
		enc.SetEscapeHTML(false)
		_ = enc.Encode(x) // a string, a number, a boolean or null; Encode ends with a newline
		buf.Truncate(buf.Len() - 1)
	}
}

func asObj(v any) *obj { o, _ := v.(*obj); return o }

// names collects every key and every string value under v.
func names(v any, into map[string]bool) {
	switch x := v.(type) {
	case *obj:
		for _, k := range x.keys {
			into[k] = true
			names(x.vals[k], into)
		}
	case []any:
		for _, e := range x {
			names(e, into)
		}
	case string:
		into[x] = true
	}
}

// ownModels are the models of the group, and the other models.
func splitModels(root *obj, group string) (own, others []any) {
	list, _ := root.vals["models"].([]any)
	for _, m := range list {
		if o := asObj(m); o != nil && o.vals["group"] == group {
			own = append(own, m)
		} else {
			others = append(others, m)
		}
	}
	return own, others
}

// withoutGroup removes from the catalogue what is the group's; false when nothing was.
func withoutGroup(root *obj, group string) bool {
	changed := false
	if gl, ok := root.vals["groups"].([]any); ok {
		kept := make([]any, 0, len(gl))
		for _, g := range gl {
			if o := asObj(g); o != nil && o.vals["id"] == group {
				changed = true
				continue
			}
			kept = append(kept, g)
		}
		root.vals["groups"] = kept
	}
	own, others := splitModels(root, group)
	if len(own) > 0 {
		root.vals["models"] = append([]any{}, others...)
		changed = true
	}
	if lim := asObj(root.vals["limits"]); lim != nil {
		for _, k := range append([]string{}, lim.keys...) {
			if strings.HasPrefix(k, group+"_") {
				lim.del(k)
				changed = true
			}
		}
	}
	if len(own) == 0 {
		return changed
	}
	named := map[string]bool{}
	for _, m := range own {
		names(m, named)
	}
	shared := map[string]bool{}
	for _, m := range others {
		names(m, shared)
	}
	for _, k := range []string{"predict", "nodes"} {
		names(root.vals[k], shared)
	}
	if kinds := asObj(root.vals["kinds"]); kinds != nil {
		for _, k := range kinds.keys {
			if of, ok := asObj(kinds.vals[k]).vals["of"].(string); ok {
				shared[of] = true
			}
		}
	}
	for _, section := range ownedSections {
		o := asObj(root.vals[section])
		if o == nil {
			continue
		}
		for _, k := range append([]string{}, o.keys...) {
			if named[k] && !shared[k] {
				o.del(k)
				changed = true
			}
		}
	}
	return changed
}

// CompactFor is the catalogue a model Job of the group receives as catalog.json: Compact for the group `bayes`, and for every other
// group the catalogue without what is the group `bayes`'s.
func CompactFor(group string) []byte { return compactFor(raw, group) }

func compactFor(catalogue []byte, group string) []byte {
	if group == BayesGroup {
		return compactOf(catalogue)
	}
	root, err := parseOrdered(catalogue)
	if err != nil {
		panic("modelcatalog: catalog.json: " + err.Error())
	}
	o := asObj(root)
	if !withoutGroup(o, BayesGroup) {
		return compactOf(catalogue)
	}
	var buf bytes.Buffer
	writeOrdered(&buf, o)
	return buf.Bytes()
}

// BayesGroup is the id of the group whose entries the other groups' view leaves out.
const BayesGroup = "bayes"

// SchemaFor is the card schema a model Job of the group receives as card.schema.json: the schema as it is for the group `bayes`,
// and for every other group the schema without the card's `bayes` object (its property, its name in `required`, and the `allOf`
// rules that name it or the group's models), the file's order kept and its indentation of two spaces. A schema with no such
// object is returned as it is.
func SchemaFor(schema []byte, group string) []byte { return schemaFor(schema, raw, group) }

func schemaFor(schema, catalogue []byte, group string) []byte {
	if group == BayesGroup {
		return schema
	}
	root, err := parseOrdered(schema)
	if err != nil {
		panic("modelcatalog: card.schema.json: " + err.Error())
	}
	o := asObj(root)
	changed := false
	if props := asObj(o.vals["properties"]); props != nil && props.del(BayesGroup) {
		changed = true
	}
	if req, ok := o.vals["required"].([]any); ok {
		kept := make([]any, 0, len(req))
		for _, r := range req {
			if r != BayesGroup {
				kept = append(kept, r)
			}
		}
		if len(kept) != len(req) {
			o.vals["required"] = kept
			changed = true
		}
	}
	if rules, ok := o.vals["allOf"].([]any); ok {
		own := map[string]bool{}
		for _, m := range ownModelIDs(catalogue) {
			own[m] = true
		}
		kept := make([]any, 0, len(rules))
		for _, r := range rules {
			if namesGroup(r, own) {
				changed = true
				continue
			}
			kept = append(kept, r)
		}
		o.vals["allOf"] = kept
	}
	if !changed {
		return schema
	}
	var buf, out bytes.Buffer
	writeOrdered(&buf, o)
	if err := json.Indent(&out, buf.Bytes(), "", "  "); err != nil {
		panic("modelcatalog: card.schema.json: " + err.Error())
	}
	if bytes.HasSuffix(schema, []byte("\n")) {
		out.WriteByte('\n')
	}
	return out.Bytes()
}

// ownModelIDs are the ids of the models of the group `bayes` in the catalogue.
func ownModelIDs(catalogue []byte) []string {
	root, err := parseOrdered(catalogue)
	if err != nil {
		panic("modelcatalog: catalog.json: " + err.Error())
	}
	own, _ := splitModels(asObj(root), BayesGroup)
	var ids []string
	for _, m := range own {
		if id, ok := asObj(m).vals["id"].(string); ok {
			ids = append(ids, id)
		}
	}
	return ids
}

// namesGroup: a rule of the schema's allOf that says «bayes» as a property, in a `required` list or as the group, or names a model of it.
func namesGroup(rule any, models map[string]bool) bool {
	switch x := rule.(type) {
	case *obj:
		for _, k := range x.keys {
			if k == "required" {
				if list, ok := x.vals[k].([]any); ok {
					for _, e := range list {
						if e == BayesGroup {
							return true
						}
					}
				}
			}
			if k == "properties" {
				if p := asObj(x.vals[k]); p != nil {
					if _, ok := p.vals[BayesGroup]; ok {
						return true
					}
				}
			}
			if k == "group" && hasValue(x.vals[k], map[string]bool{BayesGroup: true}) {
				return true
			}
			if k == "model" && hasValue(x.vals[k], models) {
				return true
			}
			if namesGroup(x.vals[k], models) {
				return true
			}
		}
	case []any:
		for _, e := range x {
			if namesGroup(e, models) {
				return true
			}
		}
	}
	return false
}

// hasValue: a property's schema (a const, or an enum) that names one of the values.
func hasValue(schema any, values map[string]bool) bool {
	o := asObj(schema)
	if o == nil {
		return false
	}
	if s, ok := o.vals["const"].(string); ok && values[s] {
		return true
	}
	if list, ok := o.vals["enum"].([]any); ok {
		for _, e := range list {
			if s, ok := e.(string); ok && values[s] {
				return true
			}
		}
	}
	return false
}

func compactOf(b []byte) []byte {
	var out bytes.Buffer
	if err := json.Compact(&out, b); err != nil {
		panic("modelcatalog: catalog.json: " + err.Error())
	}
	return out.Bytes()
}
