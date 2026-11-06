package pyd

import (
	"errors"
	"strings"
)

// LoadsStr is json.loads of a str (text read from the database): no encoding guessed, a
// leading BOM refused as Python refuses it. The values are as ReadBody's.
func LoadsStr(s string) (any, error) {
	if strings.HasPrefix(s, "\ufeff") {
		return nil, &JSONDecodeError{Msg: "Unexpected UTF-8 BOM (decode using utf-8-sig)", Pos: 0}
	}
	sc := scanner{s: []rune(s)}
	idx := sc.skipWS(0)
	v, end, err := sc.scanOnce(idx, 0)
	if err != nil {
		var stop stopIteration
		if errors.As(err, &stop) {
			return nil, &JSONDecodeError{Msg: "Expecting value", Pos: int(stop)}
		}
		return nil, err
	}
	if end = sc.skipWS(end); end != len(sc.s) {
		return nil, &JSONDecodeError{Msg: "Extra data", Pos: end}
	}
	return v, nil
}

// Dumps is Starlette's JSONResponse rendering of a value of this package's
// (json.dumps(ensure_ascii=False, allow_nan=False, separators=(",", ":"))).
func Dumps(v any) ([]byte, error) { return dumpsJSON(v) }

// MarshalJSON writes a dict as Python does, its keys in order: an *Object can sit in
// a Go struct answered with encoding/json.
func (o *Object) MarshalJSON() ([]byte, error) { return dumpsJSON(o) }

// PyStr is str() of a json.loads value: a str as it is, the rest as repr() writes it
// (lists and dicts with ", " and ": ", their strings quoted).
func PyStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return pyReprDeep(v)
}

func pyReprDeep(v any) string {
	switch x := v.(type) {
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = pyReprDeep(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case *Object:
		parts := make([]string, 0, x.Len())
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			parts = append(parts, pyStrRepr(k)+": "+pyReprDeep(e))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return pyRepr(v)
}

// Truthy is Python's bool() of a json.loads value.
func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case *Object:
		return x.Len() > 0
	}
	return true // *big.Int: never zero (normInt keeps small ones as int64)
}
