// Package masking is services/masking.py: the personal data an observer reads masked.
package masking

import "strings"

// Email is mask_email: `alice.rossi@esempio.it` → `a***@esempio.it`; the domain stays, the
// first letter keeps two people apart without saying who they are.
func Email(email string) string {
	text := strings.TrimFunc(email, isPySpace)
	if !strings.Contains(text, "@") {
		if text != "" {
			return "***"
		}
		return ""
	}
	i := strings.LastIndex(text, "@")
	local, domain := text[:i], text[i+1:]
	if local == "" {
		return "***@" + domain
	}
	r := []rune(local)
	return string(r[:1]) + "***@" + domain
}

// isPySpace is str.isspace, which str.strip() removes.
func isPySpace(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}
