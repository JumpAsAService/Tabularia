package pyd

import "fmt"

// Dump is model_dump(mode="json"): the fields by their Python names, nested models
// as dicts, datetimes as pydantic writes them. Floats stay floats (NaN and the
// infinities included, as pydantic leaves them in mode="json"), and a float field
// whose default is an int writes it as a float, as pydantic's serializer does.
func (mv *ModelValue) Dump() *Object {
	o := NewObject()
	for i, f := range mv.Model.fields {
		o.Set(f.Name, dumpTyped(f.T, mv.values[i]))
	}
	return o
}

// dumpTyped serializes a value by its declared type: what matters is the float
// field holding an int (a default is never validated) and a default model
// instance, kept as a dict.
func dumpTyped(t *Type, v any) any {
	if v == nil {
		return nil
	}
	switch t.kind {
	case kFloat:
		if isInt(v) {
			if f, err := intToFloat(v, v); err == nil {
				return f
			}
		}
	case kNullable:
		return dumpTyped(t.elem, v)
	case kList:
		if items, ok := v.([]any); ok {
			out := make([]any, len(items))
			for i, e := range items {
				out[i] = dumpTyped(t.elem, e)
			}
			return out
		}
	case kDict:
		if obj, ok := v.(*Object); ok {
			out := NewObject()
			for _, k := range obj.keys {
				// pydantic's serializer writes a key through UTF-8, lossily
				out.Set(locKey(k), dumpTyped(t.elem, obj.vals[k]))
			}
			return out
		}
	case kModel:
		if obj, ok := v.(*Object); ok {
			out := NewObject()
			for _, k := range obj.keys {
				if f := t.model.field(k); f != nil {
					out.Set(k, dumpTyped(f.T, obj.vals[k]))
				} else {
					out.Set(k, dumpValue(obj.vals[k]))
				}
			}
			return out
		}
	}
	return dumpValue(v)
}

// DumpValue is Dump for any validated value.
func DumpValue(v any) any { return dumpValue(v) }

func dumpValue(v any) any {
	switch x := v.(type) {
	case *ModelValue:
		return x.Dump()
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = dumpValue(e)
		}
		return out
	case *Object:
		out := NewObject()
		for _, k := range x.keys {
			out.Set(locKey(k), dumpValue(x.vals[k]))
		}
		return out
	case DateTime:
		return x.ISO()
	}
	return v
}

// ISO is pydantic's JSON form of a datetime: microseconds (six digits) only when
// not zero, "Z" for an aware UTC, else the ±HH:MM offset; nothing when naive.
func (d DateTime) ISO() string {
	s := d.Format("2006-01-02T15:04:05")
	if us := d.Nanosecond() / 1000; us != 0 {
		s += fmt.Sprintf(".%06d", us)
	}
	if !d.Aware {
		return s
	}
	_, off := d.Zone()
	if off == 0 {
		return s + "Z"
	}
	sign := '+'
	if off < 0 {
		sign = '-'
		off = -off
	}
	return s + fmt.Sprintf("%c%02d:%02d", sign, off/3600, off%3600/60)
}
