package pyjson

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// Loads is json.loads: objects come back as Object, in the order of their keys (a key
// written twice keeps its first place and its last value, as a dict built from the pairs);
// numbers as int64 when Python reads an int (no '.', 'e' or 'E'), float64 otherwise;
// arrays as []any. What Python would read and write back unchanged, Dumps writes back the
// same. An int beyond int64 is read as a float64 (Python keeps it exact: never stored by
// the gateway).
func Loads(text string) (any, error) {
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	v, err := loadValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("pyjson: extra data after the value")
	}
	return v, nil
}

func loadValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			obj := Object{}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := kt.(string)
				if !ok {
					return nil, fmt.Errorf("pyjson: a key is not a string")
				}
				v, err := loadValue(dec)
				if err != nil {
					return nil, err
				}
				obj = obj.Set(key, v)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return obj, nil
		case '[':
			list := []any{}
			for dec.More() {
				v, err := loadValue(dec)
				if err != nil {
					return nil, err
				}
				list = append(list, v)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return list, nil
		}
		return nil, fmt.Errorf("pyjson: unexpected %v", t)
	case json.Number:
		s := t.String()
		if !strings.ContainsAny(s, ".eE") {
			if i, err := strconv.ParseInt(s, 10, 64); err == nil {
				return i, nil
			}
		}
		return t.Float64()
	default:
		return tok, nil // string, bool, nil
	}
}

// MarshalJSON writes the object in its own order, for the answers (Starlette's
// JSONResponse: compact). Its values are written by encoding/json, so that an answer may
// hold Go structs too; a float keeps Python's repr (1.0, not 1).
func (o Object) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, kv := range o {
		if i > 0 {
			b.WriteByte(',')
		}
		k, err := json.Marshal(kv.Key)
		if err != nil {
			return nil, err
		}
		b.Write(k)
		b.WriteByte(':')
		v, err := marshalValue(kv.Value)
		if err != nil {
			return nil, err
		}
		b.Write(v)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

func marshalValue(v any) ([]byte, error) {
	switch x := v.(type) {
	case float64:
		return []byte(FloatRepr(x)), nil
	case []any:
		var b bytes.Buffer
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			raw, err := marshalValue(e)
			if err != nil {
				return nil, err
			}
			b.Write(raw)
		}
		b.WriteByte(']')
		return b.Bytes(), nil
	}
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), nil
}
