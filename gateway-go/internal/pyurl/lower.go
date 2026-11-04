package pyurl

import (
	"strings"
	"unicode"
)

// Lower is Python's str.lower: Unicode's full lower-case mapping (İ becomes i and a combining
// dot), and a capital sigma at the end of a word becomes the final ς (CPython's
// handle_capital_sigma: a cased letter before it, none after, case-ignorable characters
// skipped). Every other character as unicode.ToLower maps it.
func Lower(s string) string {
	if isASCII(s) {
		return strings.ToLower(s)
	}
	rs := []rune(s)
	var b strings.Builder
	b.Grow(len(s))
	for i, r := range rs {
		switch r {
		case 'İ':
			b.WriteString("i̇")
		case 'Σ':
			if finalSigma(rs, i) {
				b.WriteRune('ς')
			} else {
				b.WriteRune('σ')
			}
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// finalSigma is CPython's Final_Sigma test for the sigma at i.
func finalSigma(rs []rune, i int) bool {
	j := i - 1
	for j >= 0 && caseIgnorable(rs[j]) {
		j--
	}
	if j < 0 || !cased(rs[j]) {
		return false
	}
	j = i + 1
	for j < len(rs) && caseIgnorable(rs[j]) {
		j++
	}
	return j == len(rs) || !cased(rs[j])
}

// cased is Unicode's Cased: Lowercase, Uppercase or a title-case letter.
func cased(r rune) bool {
	return unicode.IsUpper(r) || unicode.IsLower(r) || unicode.IsTitle(r) ||
		unicode.Is(unicode.Other_Lowercase, r) || unicode.Is(unicode.Other_Uppercase, r)
}

// caseIgnorable is Unicode's Case_Ignorable: Mn, Me, Cf, Lm, Sk, and the word-break
// MidLetter, MidNumLet and Single_Quote characters.
func caseIgnorable(r rune) bool {
	switch r {
	case '\'', '.', ':', '·', '·', '՟', '״', '‘', '’', '․', '‧', '︓', '﹒',
		'﹕', '＇', '．', '：':
		return true
	}
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk)
}
