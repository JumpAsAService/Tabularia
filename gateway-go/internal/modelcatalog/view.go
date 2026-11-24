package modelcatalog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// The group `bayes` (docs/specs/bayes-report.md, lot Q0; docs/specs/bayes-contract.md, lot K0b) has its model, `bayes_model`, in
// the models' catalogue, and its card's `bayes` object in card.schema.json. Every other group's model Job receives a VIEW of
// both without what is the group's, filtered in the order of the file, so that their ConfigMaps and RunnerHash keep their
// bytes; the group `bayes` receives them whole. What is the group's:
//
//   - the group of that id, in `groups`, and every model of it, in `models`;
//   - in `limits`, the keys that start with «<group>_»;
//   - in `section_shapes`, `kinds` and `output_types`, the entry that the group's models name (as a key or as a value) and that no
//     other model uses the way that section is used (the sections a variant writes, the outputs it hands on, the kinds a key takes),
//     nor `predict`, nor a kind that stays («of»): a shared entry stays, and a word another model has for something else (a reading
//     block called «summary») does not make a section «summary» shared;
//   - in the card schema, the property named as the group (and its name in `required`), the group's and its models' names in the enums
//     of `group` and `model`, and the `allOf` rule that names that property or one of the group's models.
//
// The catalogue is filtered as tokens and written compact, the schema is cut where it stands (its own bytes, its inline arrays and
// its indentation as the file writes them): what is left is the file as it was before the group's entries were added. A catalogue and
// a schema that hold nothing of the group are returned as they are, byte for byte.

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
	// What the other models use of each owned section, the way that section is used: the sections a variant writes (the keys of
	// `sections`) for `section_shapes`, the outputs a variant hands on (the keys of `outputs`) for `output_types`, the kinds a key
	// takes (the values of `needs` and `takes`) for `kinds`. A name that another model uses only as a word (a reading block called
	// «summary» is no section called «summary») does not make an entry shared.
	shared := map[string]map[string]bool{"section_shapes": {}, "output_types": {}, "kinds": {}}
	for _, m := range others {
		mo := asObj(m)
		if mo == nil {
			continue
		}
		for _, field := range []string{"needs", "takes"} {
			if kinds := asObj(mo.vals[field]); kinds != nil {
				for _, k := range kinds.keys {
					if kind, ok := kinds.vals[k].(string); ok {
						shared["kinds"][kind] = true
					}
				}
			}
		}
		variants, _ := mo.vals["variants"].([]any)
		for _, v := range variants {
			vo := asObj(v)
			if vo == nil {
				continue
			}
			for field, into := range map[string]string{"sections": "section_shapes", "outputs": "output_types"} {
				if keys := asObj(vo.vals[field]); keys != nil {
					for _, k := range keys.keys {
						shared[into][k] = true
					}
				}
			}
		}
	}
	// Predict takes kinds too, and a kind may point at another key's kind with `of`: those stay as well
	names(root.vals["predict"], shared["kinds"])
	if kinds := asObj(root.vals["kinds"]); kinds != nil {
		for _, k := range kinds.keys {
			if of, ok := asObj(kinds.vals[k]).vals["of"].(string); ok {
				shared["kinds"][of] = true
			}
		}
	}
	for _, section := range ownedSections {
		o := asObj(root.vals[section])
		if o == nil {
			continue
		}
		for _, k := range append([]string{}, o.keys...) {
			if named[k] && !shared[section][k] {
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
	root, err := scanJSON(schema)
	if err != nil || root.kind != '{' {
		panic("modelcatalog: card.schema.json: not a JSON object")
	}
	own := map[string]bool{}
	for _, m := range ownModelIDs(catalogue) {
		own[m] = true
	}
	var cuts []span
	// the card's `bayes` object, and the names of the group and of its models in the enums of `group` and `model`
	if props := root.member("properties"); props != nil && props.kind == '{' {
		cuts = append(cuts, props.drop(func(key string, _ *jsonNode) bool { return key == BayesGroup })...)
		for field, names := range map[string]map[string]bool{"group": {BayesGroup: true}, "model": own} {
			if enum := props.member(field).member("enum"); enum != nil && enum.kind == '[' {
				cuts = append(cuts, enum.drop(func(_ string, e *jsonNode) bool { return e.kind == '"' && names[e.str(schema)] })...)
			}
		}
	}
	// its name in `required`
	if req := root.member("required"); req != nil && req.kind == '[' {
		cuts = append(cuts, req.drop(func(_ string, e *jsonNode) bool { return e.kind == '"' && e.str(schema) == BayesGroup })...)
	}
	// the rules that name that property, the group or one of its models
	if rules := root.member("allOf"); rules != nil && rules.kind == '[' {
		cuts = append(cuts, rules.drop(func(_ string, e *jsonNode) bool {
			rule, err := parseOrdered(schema[e.start:e.end])
			return err == nil && namesGroup(rule, own)
		})...)
	}
	if len(cuts) == 0 {
		return schema
	}
	sort.Slice(cuts, func(i, j int) bool { return cuts[i].from > cuts[j].from })
	out := append([]byte{}, schema...)
	for _, c := range cuts {
		out = append(out[:c.from], out[c.to:]...)
	}
	return out
}

// A span of the file to cut. The file is edited in place (a token filter, not a decode and an encode): what is left is the file's
// own bytes, its indentation and its inline arrays as they are written.
type span struct{ from, to int }

// jsonNode is a value of a JSON text with the bytes it takes, and its children: the members of an object (with the key's start),
// the elements of an array.
type jsonNode struct {
	kind       byte // { [ " or 0 for a number, a literal
	start, end int
	keys       []string
	keyStart   []int
	kids       []*jsonNode
}

func (n *jsonNode) str(src []byte) string {
	var s string
	_ = json.Unmarshal(src[n.start:n.end], &s)
	return s
}

// member is the value of the key of an object, nil for none (and for a nil node: a chain of members is safe).
func (n *jsonNode) member(key string) *jsonNode {
	if n == nil || n.kind != '{' {
		return nil
	}
	for i, k := range n.keys {
		if k == key {
			return n.kids[i]
		}
	}
	return nil
}

// drop cuts the children the test names (for an array its elements, with an empty key; for an object its members, by key): each
// goes with the separator before it, the first kept one takes the place of the first of all, so that what stays reads as it did
// without them.
func (n *jsonNode) drop(test func(key string, child *jsonNode) bool) []span {
	var cuts []span
	first := -1 // the first child that stays
	for i, c := range n.kids {
		key := ""
		if n.kind == '{' {
			key = n.keys[i]
		}
		if !test(key, c) {
			first = i
			break
		}
	}
	startOf := func(i int) int {
		if n.kind == '{' {
			return n.keyStart[i]
		}
		return n.kids[i].start
	}
	for i, c := range n.kids {
		key := ""
		if n.kind == '{' {
			key = n.keys[i]
		}
		if !test(key, c) {
			continue
		}
		switch {
		case first < 0 && i == 0:
			cuts = append(cuts, span{startOf(0), n.kids[len(n.kids)-1].end})
		case first < 0:
		case i < first && i == 0:
			cuts = append(cuts, span{startOf(0), startOf(first)})
		case i < first:
		default:
			cuts = append(cuts, span{n.kids[i-1].end, c.end})
		}
	}
	return cuts
}

// scanJSON reads the structure of a JSON text: where each value begins and ends.
func scanJSON(b []byte) (*jsonNode, error) {
	p := &scanner{b: b}
	p.space()
	n, err := p.value()
	if err != nil {
		return nil, err
	}
	p.space()
	if p.i != len(b) {
		return nil, fmt.Errorf("text after the value at byte %d", p.i)
	}
	return n, nil
}

type scanner struct {
	b []byte
	i int
}

func (p *scanner) space() {
	for p.i < len(p.b) && (p.b[p.i] == ' ' || p.b[p.i] == '\n' || p.b[p.i] == '\t' || p.b[p.i] == '\r') {
		p.i++
	}
}

func (p *scanner) str() error {
	p.i++ // the opening quote
	for p.i < len(p.b) {
		switch p.b[p.i] {
		case '\\':
			p.i += 2
		case '"':
			p.i++
			return nil
		default:
			p.i++
		}
	}
	return fmt.Errorf("a string is not closed")
}

func (p *scanner) value() (*jsonNode, error) {
	if p.i >= len(p.b) {
		return nil, fmt.Errorf("the text ends where a value is due")
	}
	n := &jsonNode{start: p.i, kind: p.b[p.i]}
	switch p.b[p.i] {
	case '"':
		if err := p.str(); err != nil {
			return nil, err
		}
	case '{', '[':
		closing := byte('}')
		if n.kind == '[' {
			closing = ']'
		}
		p.i++
		p.space()
		if p.i < len(p.b) && p.b[p.i] == closing {
			p.i++
			break
		}
		for {
			p.space()
			if n.kind == '{' {
				if p.i >= len(p.b) || p.b[p.i] != '"' {
					return nil, fmt.Errorf("a key is due at byte %d", p.i)
				}
				at := p.i
				if err := p.str(); err != nil {
					return nil, err
				}
				var key string
				if err := json.Unmarshal(p.b[at:p.i], &key); err != nil {
					return nil, err
				}
				p.space()
				if p.i >= len(p.b) || p.b[p.i] != ':' {
					return nil, fmt.Errorf("a colon is due at byte %d", p.i)
				}
				p.i++
				p.space()
				n.keys = append(n.keys, key)
				n.keyStart = append(n.keyStart, at)
			}
			v, err := p.value()
			if err != nil {
				return nil, err
			}
			n.kids = append(n.kids, v)
			p.space()
			if p.i < len(p.b) && p.b[p.i] == ',' {
				p.i++
				continue
			}
			if p.i < len(p.b) && p.b[p.i] == closing {
				p.i++
				break
			}
			return nil, fmt.Errorf("a comma or the end is due at byte %d", p.i)
		}
	default:
		n.kind = 0
		for p.i < len(p.b) && !strings.ContainsRune(",]} \n\t\r", rune(p.b[p.i])) {
			p.i++
		}
		if p.i == n.start {
			return nil, fmt.Errorf("a value is due at byte %d", p.i)
		}
	}
	n.end = p.i
	return n, nil
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
