// Package masking is services/masking.py: the personal data an observer reads masked.
package masking

import (
	"strings"
	"unicode"
)

// Email is mask_email: «alice.rossi@esempio.it» → «a***@esempio.it»; the domain stays, the
// first character keeps two people apart without saying who they are.
func Email(email string) string {
	text := strings.TrimFunc(email, unicode.IsSpace)
	at := strings.LastIndex(text, "@")
	if at < 0 {
		if text == "" {
			return ""
		}
		return "***"
	}
	local, domain := text[:at], text[at+1:]
	if local == "" {
		return "***@" + domain
	}
	first := []rune(local)[:1]
	return string(first) + "***@" + domain
}
