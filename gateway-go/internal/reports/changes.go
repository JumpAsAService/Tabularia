package reports

import (
	"fmt"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// ElementsOf is ai_comment.elements_of: a dashboard's tiles or a report's body, by id.
func ElementsOf(definition any) pyjson.Object {
	def := obj(definition)
	items, ok := get(def, "tiles").([]any)
	if !ok {
		body := obj(or(get(obj(or(get(def, "sections"), pyjson.Object{})), "body"), pyjson.Object{}))
		items = list(or(get(body, "items"), []any{}))
	}
	out := pyjson.Object{}
	for _, it := range items {
		o, ok := it.(pyjson.Object)
		if !ok {
			continue
		}
		if id, ok := get(o, "id").(string); ok {
			out = out.Set(id, o)
		}
	}
	return out
}

// sources is widens' sources(): {name: source} of the datasets, in order.
func sources(content any) pyjson.Object {
	out := pyjson.Object{}
	for _, d := range list(or(get(obj(content), "datasets"), []any{})) {
		if o, ok := d.(pyjson.Object); ok {
			out = out.Set(pystr.Repr(get(o, "name")), get(o, "source"))
		}
	}
	return out
}

func displayKey(k string) string {
	// keys are repr()s of the names: a name as str() writes it
	if v, err := pyjson.Loads(k); err == nil {
		return pystr.Str(v)
	}
	if strings.HasPrefix(k, "'") && strings.HasSuffix(k, "'") && len(k) >= 2 {
		return strings.ReplaceAll(k[1:len(k)-1], `\'`, `'`)
	}
	return k
}

// commentsOf is _comments_of: what an AI comment sends to a model.
func commentsOf(content any) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range ElementsOf(content) {
		it := kv.Value.(pyjson.Object)
		if get(it, "type") != "comment" {
			continue
		}
		var reads []string
		for _, r := range list(or(get(it, "reads"), []any{})) {
			reads = append(reads, pystr.Str(r))
		}
		sort.Strings(reads)
		rs := make([]any, len(reads))
		for i, r := range reads {
			rs[i] = r
		}
		out = out.Set(kv.Key, []any{get(it, "instruction"), rs})
	}
	return out
}

// Widens is widens: what a change makes the object read that it did not.
func Widens(before, after any) []string {
	b, a := sources(before), sources(after)
	var added, changed []string
	for _, kv := range a {
		prev, ok := b.Get(kv.Key)
		if !ok {
			added = append(added, displayKey(kv.Key))
		} else if !pystr.Eq(kv.Value, prev) {
			changed = append(changed, displayKey(kv.Key))
		}
	}
	var reasons []string
	if len(added) > 0 {
		reasons = append(reasons, fmt.Sprintf("it reads the new datasets %s", strings.Join(added, ", ")))
	}
	if len(changed) > 0 {
		reasons = append(reasons, fmt.Sprintf("its datasets %s read something else", strings.Join(changed, ", ")))
	}
	if !pystr.Eq(commentsOf(before), commentsOf(after)) {
		reasons = append(reasons, "its AI comments changed, and a schedule pays for them with its author's numbers and budget")
	}
	return reasons
}

// NewDefaults is new_defaults: the parameters whose default a change replaced, sorted.
func NewDefaults(before, after any) []string {
	defaults := func(content any) pyjson.Object {
		out := pyjson.Object{}
		for _, p := range list(or(get(obj(content), "parameters"), []any{})) {
			if o, ok := p.(pyjson.Object); ok {
				out = out.Set(pystr.Repr(get(o, "name")), get(o, "default"))
			}
		}
		return out
	}
	b, a := defaults(before), defaults(after)
	var out []string
	for _, kv := range a {
		if prev, ok := b.Get(kv.Key); ok && !pystr.Eq(kv.Value, prev) {
			out = append(out, displayKey(kv.Key))
		}
	}
	sort.Strings(out)
	return out
}
