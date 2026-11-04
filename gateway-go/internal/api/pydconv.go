package api

import (
	"math/big"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// toPyd turns a value of pyjson.Loads into the same value as pyd holds it (its dict, its
// big integers), to validate it against a model.
func toPyd(v any) any {
	switch x := v.(type) {
	case pyjson.Object:
		o := pyd.NewObject()
		for _, kv := range x {
			o.Set(kv.Key, toPyd(kv.Value))
		}
		return o
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = toPyd(e)
		}
		return out
	case pyjson.Int:
		b, _ := new(big.Int).SetString(string(x), 10)
		return b
	}
	return v
}

// fromPyd is the way back, for pyjson.Dumps.
func fromPyd(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		o := pyjson.Object{}
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			o = append(o, pyjson.KV{Key: k, Value: fromPyd(e)})
		}
		return o
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = fromPyd(e)
		}
		return out
	case *big.Int:
		return pyjson.Int(x.String())
	}
	return v
}
