// Package reportparams is services/report_params.py: a report's parameters, their values (texts
// converted to the parameter's type, placeholders resolved by the engine) and their place in a
// dataset's SQL (a literal of the dialect, never the text as typed).
package reportparams

import (
	"fmt"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// Builtins are BUILTINS: the placeholders' names (backend/app/core/placeholders.py).
var Builtins = map[string]bool{"today": true, "yesterday": true, "start_of_month": true, "start_of_week": true,
	"start_of_year": true, "now": true, "last_refresh": true}

// Reserved is RESERVED: what a parameter cannot be called.
var Reserved = map[string]bool{"run_id": true, "flow": true, "date": true, "datetime": true, "format": true}

func init() {
	for k := range Builtins {
		Reserved[k] = true
	}
}

// Name is NAME (re.match with `$`: a final newline passes, as in Python).
var Name = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,63}\n?$`)

// body is _BODY: a name, an optional offset, an optional format.
var body = regexp.MustCompile(`(?s)^([A-Za-z_][A-Za-z0-9_]*)([+-][0-9]{1,5}(?:min|d|w|m|y|h|s)?)?(?::(.+))?\n?$`)

// Types are TYPES.
var Types = []any{"text", "number", "date", "boolean"}

// Langs are LANGS.
var Langs = []string{"en", "it", "de", "es", "fr"}

const (
	MaxText   = 1000
	MaxValues = 1000
	NoValue   = "{none}"
)

var (
	numberRe = regexp.MustCompile(`^[+-]?(\p{Nd}+(\.\p{Nd}*)?|\.\p{Nd}+)([eE][+-]?\p{Nd}{1,3})?\n?$`)
	isoDate  = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}\n?$`)
)

// BadValue is bad_value: a value that does not fit, a 400 naming the parameter.
type BadValue struct {
	Name    string
	Message string
}

func (b *BadValue) Error() string { return "Parameter «" + b.Name + "»: " + b.Message }

// Detail is the 400's detail: {"message", "parameter"}.
func (b *BadValue) Detail() pyjson.Object {
	return pyjson.Object{{Key: "message", Value: b.Error()}, {Key: "parameter", Value: b.Name}}
}

func bad(name, format string, args ...any) *BadValue {
	return &BadValue{Name: name, Message: fmt.Sprintf(format, args...)}
}

// Date is a datetime.date.
type Date struct{ T time.Time }

// Equal is ==.
func (d Date) Equal(o any) bool {
	x, ok := o.(Date)
	return ok && x.T.Equal(d.T)
}

// ISO is isoformat().
func (d Date) ISO() string { return d.T.Format("2006-01-02") }

// PlaceholderOf is placeholder_of: the inside of `{today-30}` when the text is one placeholder
// of the language, else "" and false.
func PlaceholderOf(v any) (string, bool) {
	text, ok := v.(string)
	if !ok {
		return "", false
	}
	t := pystr.Strip(text)
	r := []rune(t)
	if len(r) < 3 || r[0] != '{' || r[len(r)-1] != '}' || strings.HasPrefix(t, "{{") || strings.Contains(string(r[1:len(r)-1]), "{") {
		return "", false
	}
	inner := pystr.Strip(string(r[1 : len(r)-1]))
	m := body.FindStringSubmatch(inner)
	if m == nil || !Builtins[m[1]] {
		return "", false
	}
	return inner, true
}

// asciiDigits writes Unicode decimal digits as ASCII ones, as Decimal() reads them.
func asciiDigits(s string) string {
	return strings.Map(func(r rune) rune {
		if r > 0x7f && unicode.IsDigit(r) {
			return '0' + digitValue(r)
		}
		return r
	}, s)
}

func digitValue(r rune) rune {
	// the Nd blocks run in tens from their zero
	for z := r; z >= r-9; z-- {
		if !unicode.IsDigit(z) {
			return r - z - 1
		}
	}
	return (r - 0x30) % 10
}

// Convert is convert: one value of the parameter, from its text: a string, an int64 or a
// float64, a Date, a bool.
func Convert(param pyjson.Object, text string) (any, *BadValue) {
	nameV, _ := param.Get("name")
	kindV, _ := param.Get("type")
	name := pystr.Str(nameV)
	if strings.Contains(text, "\x00") {
		return nil, bad(name, "a value cannot hold a NUL character")
	}
	kind, _ := kindV.(string)
	if kind == "text" {
		if pystr.Len(text) > MaxText {
			return nil, bad(name, "a value is at most %d characters", MaxText)
		}
		return text, nil
	}
	t := pystr.Strip(text)
	switch kind {
	case "number":
		if !numberRe.MatchString(t) {
			return nil, bad(name, "'%s' is not a number (decimals after a dot: 1234.5)", pystr.Cut(t, 40))
		}
		a := asciiDigits(t)
		d, ok := new(big.Rat).SetString(strings.TrimPrefix(a, "+"))
		if !ok {
			return nil, bad(name, "'%s' is not a number (decimals after a dot: 1234.5)", pystr.Cut(t, 40))
		}
		limit := new(big.Rat).SetInt(new(big.Int).Exp(big.NewInt(10), big.NewInt(15), nil))
		if new(big.Rat).Abs(d).Cmp(limit) >= 0 {
			return nil, bad(name, "'%s' is too large a number", pystr.Cut(t, 40))
		}
		if d.IsInt() && !strings.ContainsAny(t, ".eE") {
			return d.Num().Int64(), nil
		}
		f, _ := strconv.ParseFloat(strings.TrimPrefix(a, "+"), 64)
		return f, nil
	case "date":
		if isoDate.MatchString(t) {
			if d, err := time.Parse("2006-01-02", strings.TrimSuffix(t, "\n")); err == nil && d.Year() >= 1 {
				return Date{d}, nil
			}
		}
		return nil, bad(name, "'%s' is not a date: write it as yyyy-mm-dd", pystr.Cut(t, 40))
	case "boolean":
		switch strings.ToLower(t) {
		case "true", "1", "yes":
			return true, nil
		case "false", "0", "no":
			return false, nil
		}
		return nil, bad(name, "'%s' is not true or false", pystr.Cut(t, 40))
	}
	return nil, bad(name, "unknown type %s", pystr.Str(kindV))
}

// ToJSON is to_json: a converted value as the engine and the audit receive it.
func ToJSON(v any) any {
	switch x := v.(type) {
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = ToJSON(e)
		}
		return out
	case Date:
		return x.ISO()
	}
	return v
}

// Key is _key: 1 and 1.0 are the same choice, True and 1 are not.
func Key(v any) string {
	switch x := v.(type) {
	case bool:
		return fmt.Sprintf("bool:%v", x)
	case string:
		return "str:" + x
	case Date:
		return "date:" + x.ISO()
	}
	if f, ok := pystr.Number(v); ok {
		return "num:" + strconv.FormatFloat(f, 'g', -1, 64)
	}
	return fmt.Sprintf("%T:%v", v, v)
}

// LangOf is lang_of.
func LangOf(definition pyjson.Object) string {
	l, _ := definition.Get("locale")
	if s, ok := l.(string); ok {
		for _, x := range Langs {
			if x == s {
				return s
			}
		}
	}
	return "en"
}
