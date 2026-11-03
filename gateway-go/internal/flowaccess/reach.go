package flowaccess

import (
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// referenceKinds is schedule_authority._REFERENCES: the keys of a node that name another
// object, and what they name.
var referenceKinds = map[string]string{
	"datasourceId": "datasource", "flowId": "flow", "connectionId": "connection",
	"mirrorConnectionId": "connection", "projectId": "folder", "environmentId": "environment",
	"registryId": "registry", "scriptId": "saved_script",
}

// Ref is one thing a definition names: (kind, id or key or fingerprint).
type Ref struct {
	Kind  string
	Value string
}

// Digits is `isinstance(value, (int, str)) and str(value).isdigit()`, and the int: an
// integer that is not negative, or a text of ASCII digits (Python's isdigit takes other
// digits too: a case not worth a branch).
func Digits(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, x >= 0
	case bool:
		return 0, false // str(True) is «True»
	case string:
		if x == "" {
			return 0, false
		}
		for _, c := range x {
			if c < '0' || c > '9' {
				return 0, false
			}
		}
		n, err := strconv.ParseInt(x, 10, 64)
		return n, err == nil
	}
	return 0, false
}

// ReachOf is schedule_authority.reach_of: everything a flow definition names.
func ReachOf(definition string) map[Ref]bool {
	parsed := parse(definition, "{}")
	found := map[Ref]bool{}
	for k := range objects.CollectStorageKeys(parsed) {
		found[Ref{"key", k}] = true
	}
	var walk func(any)
	walk = func(n any) {
		switch x := n.(type) {
		case pyjson.Object:
			for _, kv := range x {
				if kind, ok := referenceKinds[kv.Key]; ok {
					if id, ok := Digits(kv.Value); ok {
						found[Ref{kind, strconv.FormatInt(id, 10)}] = true
						continue
					}
				}
				walk(kv.Value)
			}
		case []any:
			for _, e := range x {
				walk(e)
			}
		}
	}
	walk(parsed)
	obj := pyjson.Obj(parsed)
	for _, n := range ScriptNodes(obj) {
		found[Ref{"script", CodeFingerprint(n)}] = true
	}
	for _, n := range ContainerNodes(obj) {
		found[Ref{"container", ContainerFingerprint(n)}] = true
	}
	return found
}

// Widens is schedule_authority.widens: what `after` names that `before` did not.
func Widens(before, after string) []Ref {
	old := ReachOf(before)
	var out []Ref
	for r := range ReachOf(after) {
		if !old[r] {
			out = append(out, r)
		}
	}
	return out
}
