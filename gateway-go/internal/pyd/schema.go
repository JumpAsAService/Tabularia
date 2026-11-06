package pyd

import (
	"fmt"
	"math/big"
	"regexp"
	"strings"
)

type kind int

const (
	kStr kind = iota
	kInt
	kFloat
	kBool
	kAny
	kList
	kDict
	kLiteral
	kEnum
	kUnion
	kNullable
	kModel
	kDatetime
)

// Type is the declaration of what a value must be: the annotation of a field with
// its Field(...) constraints and its AfterValidators. Build it with the
// constructors (Str, Int, List, Optional, ...) and the chainable constraints.
type Type struct {
	kind     kind
	elem     *Type   // list, dict, nullable
	choices  []*Type // union
	literals []any   // literal, enum
	model    *Model
	strict   bool
	minLen   *int
	maxLen   *int
	ge       any // int64, *big.Int or float64, as declared
	gt       any
	le       any
	lt       any
	pattern  *regexp.Regexp
	patSrc   string
	after    []Validator
	tagName  string
}

// Validator is an AfterValidator or a field_validator(mode="after"): it gets the
// validated value and returns it (possibly changed) or an error, whose text
// becomes pydantic's «Value error, <text>».
type Validator func(v any) (any, error)

// Str is `str`.
func Str() *Type { return &Type{kind: kStr} }

// Int is `int`.
func Int() *Type { return &Type{kind: kInt} }

// Float is `float`.
func Float() *Type { return &Type{kind: kFloat} }

// Bool is `bool`.
func Bool() *Type { return &Type{kind: kBool} }

// AnyType is `Any` (and the bare `dict`/`list` items).
func AnyType() *Type { return &Type{kind: kAny} }

// Datetime is `datetime.datetime`.
func Datetime() *Type { return &Type{kind: kDatetime} }

// List is `list[elem]`.
func List(elem *Type) *Type { return &Type{kind: kList, elem: elem} }

// Dict is `dict[str, elem]` (the only key type the gateway uses).
func Dict(elem *Type) *Type { return &Type{kind: kDict, elem: elem} }

// Optional is `Optional[t]` / `t | None`: None passes, anything else is t's.
func Optional(t *Type) *Type { return &Type{kind: kNullable, elem: t} }

// Union is `Union[a, b, ...]` in pydantic's smart mode. A None among the choices
// must be written as Optional(Union(...)), as pydantic itself rewrites it.
func Union(choices ...*Type) *Type { return &Type{kind: kUnion, choices: choices} }

// Literal is `Literal[...]` (strings, ints or bools).
func Literal(values ...any) *Type { return &Type{kind: kLiteral, literals: normLiterals(values)} }

// Enum is an `enum.Enum` subclass, by its members' values.
func Enum(values ...any) *Type { return &Type{kind: kEnum, literals: normLiterals(values)} }

// ModelOf is a field whose annotation is a BaseModel.
func ModelOf(m *Model) *Type { return &Type{kind: kModel, model: m} }

func normLiterals(vs []any) []any {
	out := make([]any, len(vs))
	for i, v := range vs {
		out[i] = normNumber(v)
	}
	return out
}

// normNumber turns Go's integer kinds into the int64 the values use.
func normNumber(v any) any {
	switch x := v.(type) {
	case int:
		return int64(x)
	case int32:
		return int64(x)
	case uint:
		return normInt(new(big.Int).SetUint64(uint64(x)))
	case float32:
		return float64(x)
	}
	return v
}

// target is where a constraint lands: pydantic applies Field(max_length=...) on an
// Optional[...] to the type inside.
func (t *Type) target() *Type {
	if t.kind == kNullable {
		return t.elem.target()
	}
	return t
}

// MinLen is Field(min_length=n): characters for a str, items for a list or dict.
func (t *Type) MinLen(n int) *Type { t.target().minLen = &n; return t }

// MaxLen is Field(max_length=n).
func (t *Type) MaxLen(n int) *Type { t.target().maxLen = &n; return t }

// Ge is Field(ge=v); v keeps the declared kind (an int prints as 1, a float as 1.0).
func (t *Type) Ge(v any) *Type { t.target().ge = normNumber(v); return t }

// Gt is Field(gt=v).
func (t *Type) Gt(v any) *Type { t.target().gt = normNumber(v); return t }

// Le is Field(le=v).
func (t *Type) Le(v any) *Type { t.target().le = normNumber(v); return t }

// Lt is Field(lt=v).
func (t *Type) Lt(v any) *Type { t.target().lt = normNumber(v); return t }

// Pattern is Field(pattern=p), matched with search semantics as pydantic's Rust
// regex does (anchors are the pattern's own).
func (t *Type) Pattern(p string) *Type {
	tt := t.target()
	tt.pattern = regexp.MustCompile(p)
	tt.patSrc = p
	return t
}

// Strict is Annotated[..., Strict()]: no conversions for this type.
func (t *Type) Strict() *Type { t.target().strict = true; return t }

// After adds an AfterValidator on this very type (on an Optional it also sees
// None, as a field_validator on an Optional field does).
func (t *Type) After(v Validator) *Type { t.after = append(t.after, v); return t }

// Tag overrides the name a union choice gets in the loc.
func (t *Type) Tag(name string) *Type { t.tagName = name; return t }

// tag is the name pydantic gives a union choice in the error's loc.
func (t *Type) tag() string {
	if t.tagName != "" {
		return t.tagName
	}
	switch t.kind {
	case kStr:
		return "str"
	case kInt:
		return "int"
	case kFloat:
		return "float"
	case kBool:
		return "bool"
	case kAny:
		return "any"
	case kDatetime:
		return "datetime"
	case kList:
		return "list[" + t.elem.tag() + "]"
	case kDict:
		return "dict[str," + t.elem.tag() + "]"
	case kNullable:
		return "nullable[" + t.elem.tag() + "]"
	case kUnion:
		names := make([]string, len(t.choices))
		for i, c := range t.choices {
			names[i] = c.tag()
		}
		return "union[" + strings.Join(names, ",") + "]"
	case kLiteral:
		names := make([]string, len(t.literals))
		for i, l := range t.literals {
			names[i] = pyRepr(l)
		}
		return "literal[" + strings.Join(names, ",") + "]"
	case kEnum:
		return "enum"
	case kModel:
		return t.model.Name
	}
	return "?"
}

// Field is a model's field. Without Default it is required.
type Field struct {
	Name       string
	alias      string
	T          *Type
	required   bool
	def        any
	defFactory func() any
}

// F declares a required field.
func F(name string, t *Type) *Field { return &Field{Name: name, T: t, required: true} }

// Default is the field's default: a Go value as validation produces it (nil,
// bool, int, float64, string, []any, *Object), copied at every use.
func (f *Field) Default(v any) *Field {
	f.required = false
	f.def = normNumber(v)
	return f
}

// DefaultJSON is a default written as JSON (a model's default instance, a list).
func (f *Field) DefaultJSON(js string) *Field {
	v, err := loadsJSON([]byte(js))
	if err != nil {
		panic(fmt.Sprintf("pyd: default of %s: %v", f.Name, err))
	}
	return f.Default(v)
}

// DefaultFunc is Field(default_factory=fn).
func (f *Field) DefaultFunc(fn func() any) *Field {
	f.required = false
	f.defFactory = fn
	return f
}

// Alias is Field(alias=...): the key read from the input and written in the loc.
func (f *Field) Alias(a string) *Field { f.alias = a; return f }

func (f *Field) key() string {
	if f.alias != "" {
		return f.alias
	}
	return f.Name
}

func (f *Field) defaultValue() any {
	if f.defFactory != nil {
		return f.defFactory()
	}
	return deepCopy(f.def)
}

// Model is a BaseModel: its fields in pydantic's order (a subclass's after the
// parent's, an overridden one where the parent had it).
type Model struct {
	Name           string
	fields         []*Field
	extraForbid    bool
	populateByName bool
	after          []func(*ModelValue) error
}

// NewModel declares a model.
func NewModel(name string, fields ...*Field) *Model {
	return &Model{Name: name, fields: fields}
}

// Extend declares a subclass: the parent's fields and configuration, then these
// fields (one with a parent's name replaces it in place).
func (m *Model) Extend(name string, fields ...*Field) *Model {
	c := &Model{Name: name, extraForbid: m.extraForbid, populateByName: m.populateByName}
	c.fields = append(c.fields, m.fields...)
	c.after = append(c.after, m.after...)
	for _, f := range fields {
		replaced := false
		for i, pf := range c.fields {
			if pf.Name == f.Name {
				c.fields[i] = f
				replaced = true
				break
			}
		}
		if !replaced {
			c.fields = append(c.fields, f)
		}
	}
	return c
}

// ExtraForbid is model_config extra="forbid".
func (m *Model) ExtraForbid() *Model { m.extraForbid = true; return m }

// PopulateByName is model_config populate_by_name=True (validate_by_name): an
// aliased field is also read by its own name, the alias first.
func (m *Model) PopulateByName() *Model { m.populateByName = true; return m }

// After is a model_validator(mode="after"): it runs only when every field is valid.
func (m *Model) After(fn func(*ModelValue) error) *Model { m.after = append(m.after, fn); return m }

// FieldValidator is a field_validator(..., mode="after") on these fields.
func (m *Model) FieldValidator(v Validator, names ...string) *Model {
	for _, n := range names {
		f := m.field(n)
		if f == nil {
			panic("pyd: no field " + n + " in " + m.Name)
		}
		// a copy: the parent's declaration must not get the subclass's validator
		nf := *f
		t := *f.T
		t.after = append(append([]Validator(nil), f.T.after...), v)
		nf.T = &t
		for i, pf := range m.fields {
			if pf == f {
				m.fields[i] = &nf
			}
		}
	}
	return m
}

func (m *Model) field(name string) *Field {
	for _, f := range m.fields {
		if f.Name == name {
			return f
		}
	}
	return nil
}

// Fields lists the model's fields in order.
func (m *Model) Fields() []*Field { return m.fields }

// Type is the model as a field's type.
func (m *Model) Type() *Type { return ModelOf(m) }

func deepCopy(v any) any {
	switch x := v.(type) {
	case []any:
		c := make([]any, len(x))
		for i, e := range x {
			c[i] = deepCopy(e)
		}
		return c
	case *Object:
		c := NewObject()
		for _, k := range x.keys {
			c.Set(k, deepCopy(x.vals[k]))
		}
		return c
	}
	return v
}
