package pyjson

// Truthy is Python's bool() of a value json.loads gave.
func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int64:
		return x != 0
	case int:
		return x != 0
	case Int:
		return true
	case float64:
		return x != 0
	case Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return v != nil
}

// Or is Python's `a or b`.
func Or(a, b any) any {
	if Truthy(a) {
		return a
	}
	return b
}

// IsInt is isinstance(v, int) without bool (the checks that exclude it, or where a
// boolean id is no id).
func IsInt(v any) (int64, bool) {
	n, ok := v.(int64)
	return n, ok
}

// Obj is v as a dict, or nil.
func Obj(v any) Object {
	o, _ := v.(Object)
	return o
}

// Value is d.get(key): the value, nil when missing.
func (o Object) Value(key string) any {
	v, _ := o.Get(key)
	return v
}
