package pyd

import (
	"strconv"
	"strings"
)

// Loc is an error's location: strings (keys, union tags) and ints (list indexes).
type Loc []any

// KV is a ctx entry: pydantic's ctx is an ordered dict.
type KV struct {
	K string
	V any
}

// Error is one entry of `detail`, as pydantic's errors(include_url=False) has it.
type Error struct {
	Type  string
	Loc   Loc
	Msg   string
	Input any
	Ctx   []KV // nil: no "ctx" key
}

func newErr(typ, msg string, input any, ctx ...KV) *Error {
	return &Error{Type: typ, Msg: msg, Input: input, Ctx: ctx}
}

// prefix puts p in front of every error's loc (the field, the index, the tag).
func prefix(errs []*Error, p ...any) []*Error {
	for _, e := range errs {
		loc := make(Loc, 0, len(p)+len(e.Loc))
		loc = append(loc, p...)
		e.Loc = append(loc, e.Loc...)
	}
	return errs
}

// plural is pydantic's {expected_plural}: "" for exactly one, else "s".
func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// expected joins the allowed values as pydantic's Literal/Enum message does:
// "'a'", "'a' or 'b'", "'a', 'b' or 'c'".
func expected(vals []any) string {
	parts := make([]string, len(vals))
	for i, v := range vals {
		parts[i] = pyRepr(v)
	}
	if len(parts) == 1 {
		return parts[0]
	}
	return strings.Join(parts[:len(parts)-1], ", ") + " or " + parts[len(parts)-1]
}

// numStr is str() of a bound in the messages.
func numStr(v any) string { return pyRepr(v) }

func errStringType(v any) *Error { return newErr("string_type", "Input should be a valid string", v) }
func errIntType(v any) *Error    { return newErr("int_type", "Input should be a valid integer", v) }
func errIntParsing(v any) *Error {
	return newErr("int_parsing", "Input should be a valid integer, unable to parse string as an integer", v)
}
func errIntParsingSize(v any) *Error {
	return newErr("int_parsing_size", "Unable to parse input string as an integer, exceeded maximum size", v)
}
func errIntFromFloat(v any) *Error {
	return newErr("int_from_float", "Input should be a valid integer, got a number with a fractional part", v)
}
func errFinite(v any) *Error    { return newErr("finite_number", "Input should be a finite number", v) }
func errFloatType(v any) *Error { return newErr("float_type", "Input should be a valid number", v) }
func errFloatParsing(v any) *Error {
	return newErr("float_parsing", "Input should be a valid number, unable to parse string as a number", v)
}
func errBoolType(v any) *Error { return newErr("bool_type", "Input should be a valid boolean", v) }
func errBoolParsing(v any) *Error {
	return newErr("bool_parsing", "Input should be a valid boolean, unable to interpret input", v)
}
func errListType(v any) *Error { return newErr("list_type", "Input should be a valid list", v) }
func errDictType(v any) *Error { return newErr("dict_type", "Input should be a valid dictionary", v) }
func errModelAttributes(v any) *Error {
	return newErr("model_attributes_type", "Input should be a valid dictionary or object to extract fields from", v)
}

// errMissing is pydantic's missing field: its input is the whole model input.
func errMissing(v any) *Error { return newErr("missing", "Field required", v) }

func errValue(v any, text string) *Error {
	return newErr("value_error", "Value error, "+text, v, KV{"error", NewObject()})
}

func errTooLong(v any, fieldType string, max, actual int) *Error {
	return newErr("too_long",
		fieldType+" should have at most "+strconv.Itoa(max)+" item"+plural(max)+" after validation, not "+strconv.Itoa(actual),
		v, KV{"field_type", fieldType}, KV{"max_length", max}, KV{"actual_length", actual})
}

func errTooShort(v any, fieldType string, min, actual int) *Error {
	return newErr("too_short",
		fieldType+" should have at least "+strconv.Itoa(min)+" item"+plural(min)+" after validation, not "+strconv.Itoa(actual),
		v, KV{"field_type", fieldType}, KV{"min_length", min}, KV{"actual_length", actual})
}

// locKey is a dict key as pydantic-core writes it in a loc: as UTF-8, a lone
// surrogate's three bytes each replaced by U+FFFD (Rust's from_utf8_lossy).
func locKey(k string) string {
	if !hasSurrogate(k) {
		return k
	}
	return decodeUTF8Replace([]byte(k))
}
