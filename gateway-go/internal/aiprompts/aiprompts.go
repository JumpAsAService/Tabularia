// Package aiprompts is services/ai_prompts.py for the panel: the AI agents' default
// instructions and the context Tabularia adds to them, as routes/ai.py shows them with
// example values (prompts.json, written by gen_prompts.py in the gateway's image). The
// agents themselves run in Python (docs/specs/gateway-go.md, decision 4).
package aiprompts

import (
	_ "embed"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// MaxChars is MAX_CHARS.
const MaxChars = 20_000

//go:embed prompts.json
var raw []byte

var data = func() pyjson.Object {
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		panic("aiprompts: prompts.json: " + err.Error())
	}
	return v.(pyjson.Object)
}()

func get(key string) any { v, _ := data.Get(key); return v }

// Agents is AGENTS, in the panel's order.
func Agents() []string {
	var out []string
	for _, a := range get("agents").([]any) {
		out = append(out, a.(string))
	}
	return out
}

// Default is DEFAULTS[agent]; ok=false for an unknown agent.
func Default(agent string) (string, bool) {
	v, ok := get("defaults").(pyjson.Object).Get(agent)
	if !ok {
		return "", false
	}
	return v.(string), true
}

var languages = map[string]string{"it": "Italian", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}

// languageName is language_name(locale).
func languageName(locale string) string {
	r := []rune(locale)
	if len(r) > 2 {
		r = r[:2]
	}
	if l, ok := languages[strings.ToLower(string(r))]; ok {
		return l
	}
	return "English"
}

// ExampleContext is routes/ai.py _example_context(agent, locale): today's date and the
// rows' cap filled in.
func ExampleContext(agent, locale string, withheld bool, maxResultRows int, today time.Time) string {
	w := "0"
	if withheld {
		w = "1"
	}
	v, ok := get("contexts").(pyjson.Object).Get(agent + "|" + languageName(locale) + "|" + w)
	if !ok {
		return ""
	}
	s := strings.ReplaceAll(v.(string), get("today").(string), today.Format("2006-01-02"))
	return strings.ReplaceAll(s, get("rows").(string), strconv.Itoa(maxResultRows))
}
