package dbtexport

import (
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
)

// services/dbt_messages.py: the dbt export's errors in the reader's language. The tables are
// generated from the Python module (messages_gen.go).

const defaultLanguage = "en"

// format is str.format(**params) for templates of plain `{name}` fields: false when a field
// is missing or the template does not parse (Python's KeyError, IndexError, ValueError).
func format(template string, params pyjson.Object) (string, bool) {
	var b strings.Builder
	for i := 0; i < len(template); i++ {
		c := template[i]
		switch c {
		case '{':
			if i+1 < len(template) && template[i+1] == '{' {
				b.WriteByte('{')
				i++
				continue
			}
			end := strings.IndexByte(template[i:], '}')
			if end < 0 {
				return "", false
			}
			name := template[i+1 : i+end]
			if name == "" || strings.ContainsAny(name, "{.[!:") {
				return "", false // positional fields, attributes, conversions: none in the tables
			}
			v, ok := params.Get(name)
			if !ok {
				return "", false
			}
			b.WriteString(pystr.Str(v))
			i += end
		case '}':
			if i+1 < len(template) && template[i+1] == '}' {
				b.WriteByte('}')
				i++
				continue
			}
			return "", false
		default:
			b.WriteByte(c)
		}
	}
	return b.String(), true
}

func inLanguage(table map[string]string, language string) string {
	if t := table[language]; t != "" {
		return t
	}
	return table[defaultLanguage]
}

// Translate is translate(code, params, language, original_text): the code's sentence in the
// language (or in English); without a known code, or with a parameter missing, the original
// text. The engine's codes and parameters are any JSON: an unknown code that is not text comes
// back as it is (Python returns it), a code or a reason that cannot be a dict key raises
// (a *pyval.Error: the route's 500).
func Translate(code, params any, language, original string) (out any, err error) {
	err = pyval.Catch(func() { out = translate(code, params, language, original) })
	return
}

func translate(code, params any, language, original string) any {
	var models map[string]string
	if pyval.Truthy(code) {
		pyval.Key(code)
		if s, ok := code.(string); ok {
			models = messages[s]
		}
	}
	if models == nil {
		if original != "" {
			return original
		}
		if pyval.Truthy(code) {
			return code
		}
		return ""
	}
	p := pyjson.Object{}
	if pyval.Truthy(params) {
		p = pyval.DictOf(params)
	}
	if code == "internal" {
		if _, has := p.Get("detail"); !has {
			p = p.Set("detail", original)
		}
	}
	if code == "ai_translation_invalid" {
		r := p.Value("reason")
		pyval.Key(r)
		if rs, ok := r.(string); ok && aiReasons[rs] != nil {
			reason := aiReasons[rs]
			if s, ok := format(inLanguage(reason, language), p); ok {
				p = p.Set("reason", s)
			} else {
				p = p.Set("reason", reason[defaultLanguage])
			}
		}
	}
	sentence, ok := format(inLanguage(models, language), p)
	if !ok {
		if original != "" {
			return original
		}
		return models[defaultLanguage]
	}
	if code == "text_not_translatable" {
		advice := dialectAdvice[strings.ToLower(pystr.Str(pyval.Or(p.Value("dialect"), "")))]
		if advice == nil {
			advice = dialectAdvice[""]
		}
		sentence += " " + inLanguage(advice, language)
	}
	return sentence
}

// ErrorMessage is error_message(e, language): a DbtExportError in the language.
func ErrorMessage(e *Error, language string) string {
	out, err := Translate(e.Code, e.Params, language, e.Message)
	if s, ok := out.(string); ok && err == nil {
		return s
	}
	return e.Message
}

// EngineMessage is engine_message(detail, language): the `detail` of an engine refusal,
// {code, params, message} or the text of an old engine.
func EngineMessage(detail any, language string) (any, error) {
	if d, ok := detail.(pyjson.Object); ok {
		return Translate(d.Value("code"), d.Value("params"), language, pystr.Str(pyval.Or(d.Value("message"), "")))
	}
	return pystr.Str(detail), nil
}
