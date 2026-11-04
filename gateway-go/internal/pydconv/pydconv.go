// Package pydconv carries a value between pyjson (json.loads as the gateway reads its own
// columns) and pyd (the values pydantic validates).
package pydconv

import (
	"math/big"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// ToPyd turns a value of pyjson.Loads into the same value as pyd holds it (its dict, its
// big integers), to validate it against a model.
func ToPyd(v any) any {
	switch x := v.(type) {
	case pyjson.Object:
		o := pyd.NewObject()
		for _, kv := range x {
			o.Set(kv.Key, ToPyd(kv.Value))
		}
		return o
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = ToPyd(e)
		}
		return out
	case pyjson.Int:
		b, _ := new(big.Int).SetString(string(x), 10)
		return b
	}
	return v
}

// FromPyd is the way back, for pyjson.Dumps.
func FromPyd(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		o := pyjson.Object{}
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			o = append(o, pyjson.KV{Key: k, Value: FromPyd(e)})
		}
		return o
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = FromPyd(e)
		}
		return out
	case *big.Int:
		return pyjson.Int(x.String())
	}
	return v
}
