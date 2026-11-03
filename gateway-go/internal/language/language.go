// Package language is gateway/app/core/language.py: the language of whoever makes the
// request, for the messages the gateway writes for a person. The frontend sends the
// interface's language in Accept-Language; without it, the first language the browser asks
// for among the app's holds, and finally English.
package language

import "strings"

// Languages the app speaks; Default is the interface's.
var Languages = []string{"en", "it", "de", "es", "fr"}

const Default = "en"

// Of maps `it`, `it-IT,it;q=0.9,en;q=0.8`, `fr-CH` … to one of the Languages.
func Of(acceptLanguage string) string {
	for _, part := range strings.Split(acceptLanguage, ",") {
		tag, _, _ := strings.Cut(part, ";")
		base, _, _ := strings.Cut(strings.ToLower(strings.TrimSpace(tag)), "-")
		for _, l := range Languages {
			if base == l {
				return l
			}
		}
	}
	return Default
}

// Pick is api_tokens.message: the table's text in the request's language, English when it
// has none.
func Pick(table map[string]string, acceptLanguage string) string {
	if s, ok := table[Of(acceptLanguage)]; ok {
		return s
	}
	return table[Default]
}
