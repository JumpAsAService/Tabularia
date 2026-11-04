package api

import (
	"math/big"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// toPyjson turns a json.loads value (pyd's) into pyjson's, to be written with json.dumps'
// options into a text column.
func toPyjson(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		out := make(pyjson.Object, 0, x.Len())
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			out = append(out, pyjson.KV{Key: k, Value: toPyjson(e)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = toPyjson(e)
		}
		return out
	}
	return v
}

// pyEqual is Python's == on json.loads values: dicts regardless of order, numbers across
// int, float and bool (True == 1 == 1.0).
func pyEqual(a, b any) bool {
	if na, ok := pyNumber(a); ok {
		nb, ok := pyNumber(b)
		if !ok {
			return false
		}
		if na == nil || nb == nil { // infinities equal themselves, NaN nothing
			fa, aok := a.(float64)
			fb, bok := b.(float64)
			return aok && bok && fa == fb
		}
		return na.Cmp(nb) == 0
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !pyEqual(x[i], y[i]) {
				return false
			}
		}
		return true
	case *pyd.Object:
		y, ok := b.(*pyd.Object)
		if !ok || x.Len() != y.Len() {
			return false
		}
		for _, k := range x.Keys() {
			ev, _ := x.Get(k)
			fv, in := y.Get(k)
			if !in || !pyEqual(ev, fv) {
				return false
			}
		}
		return true
	}
	return false
}

// pyNumber is a bool, an int or a finite float as an exact rational (NaN equals nothing).
func pyNumber(v any) (*big.Rat, bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return big.NewRat(1, 1), true
		}
		return new(big.Rat), true
	case int64:
		return new(big.Rat).SetInt64(x), true
	case *big.Int:
		return new(big.Rat).SetInt(x), true
	case float64:
		r := new(big.Rat)
		if r.SetFloat64(x) == nil {
			return nil, true // NaN or infinity: compared below as never equal
		}
		return r, true
	}
	return nil, false
}
