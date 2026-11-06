package pyd

import (
	"math"
	"math/big"
	"strconv"
)

// vstate is what changes along one validation: the strict pass of a smart union.
type vstate struct {
	strict bool
}

// Validate checks a value against a type the way pydantic's validate_python does
// under FastAPI (lax mode, from_attributes=True), returning the validated value or
// the errors with their loc relative to the value.
func (t *Type) Validate(v any) (any, []*Error) {
	return t.validate(v, vstate{})
}

func (t *Type) validate(v any, st vstate) (any, []*Error) {
	out, errs := t.validateCore(v, st)
	if errs != nil {
		return nil, errs
	}
	for _, fn := range t.after {
		nv, err := fn(out)
		if err != nil {
			return nil, []*Error{errValue(v, err.Error())}
		}
		out = nv
	}
	return out, nil
}

func one(e *Error) []*Error { return []*Error{e} }

func (t *Type) validateCore(v any, st vstate) (any, []*Error) {
	strict := st.strict || t.strict
	switch t.kind {
	case kAny:
		return v, nil
	case kStr:
		s, ok := v.(string)
		if !ok {
			return nil, one(errStringType(v))
		}
		if hasSurrogate(s) {
			// a str is taken as it is, unless a constraint needs it as UTF-8
			if t.minLen == nil && t.maxLen == nil && t.pattern == nil {
				return s, nil
			}
			return nil, one(errStringUnicode(v))
		}
		return t.checkStr(s, v)
	case kInt:
		i, err := toIntValue(v, strict)
		if err != nil {
			return nil, one(err)
		}
		if e := t.checkBounds(i, v); e != nil {
			return nil, one(e)
		}
		return i, nil
	case kFloat:
		f, err := toFloatValue(v, strict)
		if err != nil {
			return nil, one(err)
		}
		if e := t.checkBounds(f, v); e != nil {
			return nil, one(e)
		}
		return f, nil
	case kBool:
		b, err := toBoolValue(v, strict)
		if err != nil {
			return nil, one(err)
		}
		return b, nil
	case kDatetime:
		d, err := toDatetime(v, strict)
		if err != nil {
			return nil, one(err)
		}
		return d, nil
	case kNullable:
		if v == nil {
			return nil, nil
		}
		return t.elem.validate(v, st)
	case kLiteral, kEnum:
		if str, ok := v.(string); ok && hasSurrogate(str) {
			return nil, one(errStringUnicode(v))
		}
		for _, l := range t.literals {
			if literalMatches(l, v, strict) {
				return l, nil
			}
		}
		exp := expected(t.literals)
		typ := "literal_error"
		if t.kind == kEnum {
			typ = "enum"
		}
		return nil, one(newErr(typ, "Input should be "+exp, v, KV{"expected", exp}))
	case kList:
		return t.validateList(v, st)
	case kDict:
		return t.validateDict(v, st)
	case kUnion:
		return t.validateUnion(v, st)
	case kModel:
		return t.model.validate(v, st)
	}
	panic("pyd: unknown type kind")
}

// checkStr applies pydantic's str constraints in its order: min_length,
// max_length, pattern (lengths in code points).
func (t *Type) checkStr(s string, v any) (any, []*Error) {
	if t.minLen != nil || t.maxLen != nil {
		n := pyLen(s)
		if t.minLen != nil && n < *t.minLen {
			return nil, one(newErr("string_too_short",
				"String should have at least "+strconv.Itoa(*t.minLen)+" character"+plural(*t.minLen),
				v, KV{"min_length", *t.minLen}))
		}
		if t.maxLen != nil && n > *t.maxLen {
			return nil, one(newErr("string_too_long",
				"String should have at most "+strconv.Itoa(*t.maxLen)+" character"+plural(*t.maxLen),
				v, KV{"max_length", *t.maxLen}))
		}
	}
	if t.pattern != nil && !t.pattern.MatchString(s) {
		return nil, one(newErr("string_pattern_mismatch", "String should match pattern '"+t.patSrc+"'",
			v, KV{"pattern", t.patSrc}))
	}
	return s, nil
}

// toIntValue is pydantic's int validator: in lax mode a bool, a whole float or a
// numeric string becomes an int; strict accepts ints only.
func toIntValue(v any, strict bool) (any, *Error) {
	switch x := v.(type) {
	case int64, *big.Int:
		return x, nil
	case bool:
		if strict {
			return nil, errIntType(v)
		}
		if x {
			return int64(1), nil
		}
		return int64(0), nil
	case float64:
		if strict {
			return nil, errIntType(v)
		}
		return floatToInt(v, x)
	case string:
		if strict {
			return nil, errIntType(v)
		}
		if hasSurrogate(x) {
			return nil, errStringUnicode(v)
		}
		return strToInt(v, x)
	}
	return nil, errIntType(v)
}

// toFloatValue is pydantic's float validator (inf and nan allowed, as by default).
func toFloatValue(v any, strict bool) (any, *Error) {
	switch x := v.(type) {
	case float64:
		return x, nil
	case int64, *big.Int:
		f, err := intToFloat(v, x)
		if err != nil {
			return nil, err
		}
		return f, nil
	case bool:
		if strict {
			return nil, errFloatType(v)
		}
		if x {
			return 1.0, nil
		}
		return 0.0, nil
	case string:
		if strict {
			return nil, errFloatType(v)
		}
		if hasSurrogate(x) {
			return nil, errStringUnicode(v)
		}
		f, err := strToFloat(v, x)
		if err != nil {
			return nil, err
		}
		return f, nil
	}
	return nil, errFloatType(v)
}

// toBoolValue is pydantic's bool validator: lax mode reads 0/1 (int or float) and
// the usual words; another i64 or word is bool_parsing, a bigger int or a float
// with a fractional part bool_type.
func toBoolValue(v any, strict bool) (bool, *Error) {
	switch x := v.(type) {
	case bool:
		return x, nil
	case int64:
		if strict {
			return false, errBoolType(v)
		}
		if x == 0 || x == 1 {
			return x == 1, nil
		}
		return false, errBoolParsing(v)
	case float64:
		// a whole float is read as the int it is (2.0 is bool_parsing like 2), any
		// other float, infinities and NaN included, is not a boolean at all
		if strict || x != math.Trunc(x) || !floatInI64(x) {
			return false, errBoolType(v)
		}
		if x == 0 || x == 1 {
			return x == 1, nil
		}
		return false, errBoolParsing(v)
	case string:
		if strict {
			return false, errBoolType(v)
		}
		if hasSurrogate(x) {
			return false, errStringUnicode(v)
		}
		return strToBool(v, x)
	}
	return false, errBoolType(v)
}

// literalMatches is pydantic's literal check: a str only by an equal str; an int
// also by an equal bool or whole float in lax mode.
func literalMatches(l, v any, strict bool) bool {
	switch x := l.(type) {
	case string:
		s, ok := v.(string)
		return ok && s == x
	case bool:
		b, ok := v.(bool)
		return ok && b == x
	case int64:
		switch y := v.(type) {
		case int64:
			return y == x
		case bool:
			return !strict && ((y && x == 1) || (!y && x == 0))
		case float64:
			return !strict && y == float64(x)
		}
	}
	return false
}

// checkBounds applies le, lt, ge, gt in pydantic-core's order, the first failure
// only; ints compare exactly with float bounds, as Python does.
func (t *Type) checkBounds(val, input any) *Error {
	// NaN fails every comparison: the first bound declared refuses it
	isNaN := false
	if f, ok := val.(float64); ok && math.IsNaN(f) {
		isNaN = true
	}
	if t.le != nil && (isNaN || cmpNum(val, t.le) > 0) {
		b, txt := t.bound(t.le)
		return newErr("less_than_equal", "Input should be less than or equal to "+txt, input, KV{"le", b})
	}
	if t.lt != nil && (isNaN || cmpNum(val, t.lt) >= 0) {
		b, txt := t.bound(t.lt)
		return newErr("less_than", "Input should be less than "+txt, input, KV{"lt", b})
	}
	if t.ge != nil && (isNaN || cmpNum(val, t.ge) < 0) {
		b, txt := t.bound(t.ge)
		return newErr("greater_than_equal", "Input should be greater than or equal to "+txt, input, KV{"ge", b})
	}
	if t.gt != nil && (isNaN || cmpNum(val, t.gt) <= 0) {
		b, txt := t.bound(t.gt)
		return newErr("greater_than", "Input should be greater than "+txt, input, KV{"gt", b})
	}
	return nil
}

// bound is a bound as the error shows it: a float type holds its bounds as floats
// (ctx 3600.0 for le=3600) and writes them with Rust's Display (3600, 0.00000025,
// never an exponent); an int type keeps the int.
func (t *Type) bound(b any) (any, string) {
	if t.kind != kFloat {
		return b, numStr(b)
	}
	f, ok := b.(float64)
	if !ok {
		f, _ = intToFloat(b, b)
	}
	switch {
	case math.IsInf(f, 1):
		return f, "inf"
	case math.IsInf(f, -1):
		return f, "-inf"
	case math.IsNaN(f):
		return f, "NaN"
	}
	return f, strconv.FormatFloat(f, 'f', -1, 64)
}

// cmpNum compares two numbers (int64, *big.Int, float64), none of them NaN.
func cmpNum(a, b any) int {
	af, aIsF := a.(float64)
	bf, bIsF := b.(float64)
	switch {
	case aIsF && bIsF:
		switch {
		case af < bf:
			return -1
		case af > bf:
			return 1
		}
		return 0
	case aIsF:
		return -cmpIntFloat(b, af)
	case bIsF:
		return cmpIntFloat(a, bf)
	}
	return toBig(a).Cmp(toBig(b))
}

func (t *Type) validateList(v any, st vstate) (any, []*Error) {
	items, ok := v.([]any)
	if !ok {
		return nil, one(errListType(v))
	}
	// pydantic counts the items as it validates them and stops at the first one
	// beyond max_length: only too_long is left, whatever came before it
	if t.maxLen != nil && len(items) > *t.maxLen {
		return nil, one(errTooLong(v, "List", *t.maxLen, len(items)))
	}
	out := make([]any, len(items))
	var errs []*Error
	for i, it := range items {
		r, e := t.elem.validate(it, st)
		if e != nil {
			errs = append(errs, prefix(e, i)...)
			continue
		}
		out[i] = r
	}
	if errs != nil {
		return nil, errs
	}
	if t.minLen != nil && len(items) < *t.minLen {
		return nil, one(errTooShort(v, "List", *t.minLen, len(items)))
	}
	return out, nil
}

func (t *Type) validateDict(v any, st vstate) (any, []*Error) {
	obj, ok := v.(*Object)
	if !ok {
		return nil, one(errDictType(v))
	}
	out := NewObject()
	var errs []*Error
	for _, k := range obj.keys {
		r, e := t.elem.validate(obj.vals[k], st)
		if e != nil {
			errs = append(errs, prefix(e, locKey(k))...)
			continue
		}
		out.Set(k, r)
	}
	if errs != nil {
		return nil, errs
	}
	if t.maxLen != nil && obj.Len() > *t.maxLen {
		return nil, one(errTooLong(v, "Dictionary", *t.maxLen, obj.Len()))
	}
	if t.minLen != nil && obj.Len() < *t.minLen {
		return nil, one(errTooShort(v, "Dictionary", *t.minLen, obj.Len()))
	}
	return out, nil
}

// validateUnion is the smart union: the first choice valid in strict mode, else
// the first valid in lax mode, else every choice's lax errors, tagged.
func (t *Type) validateUnion(v any, st vstate) (any, []*Error) {
	for _, c := range t.choices {
		if r, e := c.validate(v, vstate{strict: true}); e == nil {
			return r, nil
		}
	}
	if st.strict {
		var errs []*Error
		for _, c := range t.choices {
			_, e := c.validate(v, st)
			errs = append(errs, prefix(e, c.tag())...)
		}
		return nil, errs
	}
	var errs []*Error
	for _, c := range t.choices {
		r, e := c.validate(v, st)
		if e == nil {
			return r, nil
		}
		errs = append(errs, prefix(e, c.tag())...)
	}
	return nil, errs
}

// ModelValue is a validated model: every field's value (the input's, converted,
// or the default) and which fields the input set (model_fields_set).
type ModelValue struct {
	Model  *Model
	values []any
	set    []bool
}

// Get returns a field's value by its Python name.
func (mv *ModelValue) Get(name string) any {
	for i, f := range mv.Model.fields {
		if f.Name == name {
			return mv.values[i]
		}
	}
	panic("pyd: no field " + name + " in " + mv.Model.Name)
}

// IsSet reports whether the input gave the field (exclude_unset keeps it).
func (mv *ModelValue) IsSet(name string) bool {
	for i, f := range mv.Model.fields {
		if f.Name == name {
			return mv.set[i]
		}
	}
	panic("pyd: no field " + name + " in " + mv.Model.Name)
}

func (m *Model) validate(v any, st vstate) (any, []*Error) {
	obj, ok := v.(*Object)
	if !ok {
		return nil, one(errModelAttributes(v))
	}
	mv := &ModelValue{Model: m, values: make([]any, len(m.fields)), set: make([]bool, len(m.fields))}
	used := map[string]bool{}
	var errs []*Error
	for i, f := range m.fields {
		key := f.key()
		raw, found := obj.Get(key)
		if !found && f.alias != "" && m.populateByName {
			key = f.Name
			raw, found = obj.Get(key)
		}
		if !found {
			if f.required {
				errs = append(errs, prefix(one(errMissing(v)), f.key())...)
				continue
			}
			mv.values[i] = f.defaultValue()
			continue
		}
		used[key] = true
		r, e := f.T.validate(raw, st)
		if e != nil {
			errs = append(errs, prefix(e, key)...)
			continue
		}
		mv.values[i] = r
		mv.set[i] = true
	}
	if m.extraForbid {
		for _, k := range obj.keys {
			if used[k] {
				continue
			}
			if hasSurrogate(k) {
				// the key is read as UTF-8 to be named: the whole model fails
				return nil, one(errStringUnicode(v))
			}
			errs = append(errs, prefix(one(newErr("extra_forbidden", "Extra inputs are not permitted", obj.vals[k])), k)...)
		}
	}
	if errs != nil {
		return nil, errs
	}
	for _, fn := range m.after {
		if err := fn(mv); err != nil {
			return nil, one(errValue(v, err.Error()))
		}
	}
	return mv, nil
}

// FieldsSet lists, in field order, the fields the input gave (model_fields_set).
func (mv *ModelValue) FieldsSet() []string {
	var out []string
	for i, f := range mv.Model.fields {
		if mv.set[i] {
			out = append(out, f.Name)
		}
	}
	return out
}
