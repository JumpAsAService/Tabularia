package pypkg

import (
	"errors"
	"regexp"
	"strings"
	"unicode"
)

// ErrInvalidVersion is packaging's InvalidVersion.
var ErrInvalidVersion = errors.New("invalid version")

// num is a non-negative integer as Python holds it (any size): its digits, no leading zero.
type num string

func newNum(digits string) num {
	d := strings.TrimLeft(digits, "0")
	if d == "" {
		d = "0"
	}
	return num(d)
}

func (a num) cmp(b num) int {
	if len(a) != len(b) {
		if len(a) < len(b) {
			return -1
		}
		return 1
	}
	return strings.Compare(string(a), string(b))
}

type letter struct {
	l string
	n num
}

type localPart struct {
	isInt bool
	n     num
	s     string
}

// Version is packaging.version.Version (packaging 26.3): PEP 440, parsed and normalized.
type Version struct {
	epoch   num
	release []num
	pre     *letter
	post    *letter
	dev     *letter
	local   []localPart
}

// the regex of packaging.version (VERBOSE | IGNORECASE), its possessive quantifiers made
// greedy: after the release nothing starts with a digit but «-N», so they match the same
var versionRe = regexp.MustCompile(`(?i)^\s*v?` +
	`(?:(?P<epoch>[0-9]+)!)?` +
	`(?P<release>[0-9]+(?:\.[0-9]+)*)` +
	`(?P<pre>[._-]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[._-]?(?P<pre_n>[0-9]+)?)?` +
	`(?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[._-]?(?P<post_l>post|rev|r)[._-]?(?P<post_n2>[0-9]+)?))?` +
	`(?P<dev>[._-]?(?P<dev_l>dev)[._-]?(?P<dev_n>[0-9]+)?)?` +
	`(?:\+(?P<local>[a-z0-9]+(?:[._-][a-z0-9]+)*))?` +
	`\s*$`)

var letterNormalization = map[string]string{"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc", "rev": "post", "r": "post"}

var localSeparators = regexp.MustCompile(`[._-]`)

func parseLetter(l, n string) *letter {
	if l != "" {
		l = strings.ToLower(l)
		if x, ok := letterNormalization[l]; ok {
			l = x
		}
		if n == "" {
			n = "0"
		}
		return &letter{l, newNum(n)}
	}
	if n != "" {
		return &letter{"post", newNum(n)}
	}
	return nil
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// Parse is Version(text).
func Parse(text string) (*Version, error) {
	simple := true
	for _, c := range text {
		if !(c == '.' || c >= '0' && c <= '9') {
			simple = false
			break
		}
	}
	if simple {
		var rel []num
		for _, p := range strings.Split(text, ".") {
			if p == "" {
				return nil, ErrInvalidVersion
			}
			rel = append(rel, newNum(p))
		}
		return &Version{epoch: "0", release: rel}, nil
	}
	m := versionRe.FindStringSubmatch(text)
	if m == nil {
		return nil, ErrInvalidVersion
	}
	g := func(name string) string { return m[versionRe.SubexpIndex(name)] }
	v := &Version{epoch: "0"}
	if e := g("epoch"); e != "" {
		v.epoch = newNum(e)
	}
	for _, p := range strings.Split(g("release"), ".") {
		v.release = append(v.release, newNum(p))
	}
	v.pre = parseLetter(g("pre_l"), g("pre_n"))
	postN := g("post_n1")
	if postN == "" {
		postN = g("post_n2")
	}
	v.post = parseLetter(g("post_l"), postN)
	v.dev = parseLetter(g("dev_l"), g("dev_n"))
	if l := g("local"); l != "" {
		for _, part := range localSeparators.Split(l, -1) {
			if isDigits(part) {
				v.local = append(v.local, localPart{isInt: true, n: newNum(part)})
			} else {
				v.local = append(v.local, localPart{s: strings.ToLower(part)})
			}
		}
	}
	return v, nil
}

// String is str(Version).
func (v *Version) String() string {
	parts := make([]string, len(v.release))
	for i, r := range v.release {
		parts[i] = string(r)
	}
	s := strings.Join(parts, ".")
	if v.epoch != "0" {
		s = string(v.epoch) + "!" + s
	}
	if v.pre != nil {
		s += v.pre.l + string(v.pre.n)
	}
	if v.post != nil {
		s += ".post" + string(v.post.n)
	}
	if v.dev != nil {
		s += ".dev" + string(v.dev.n)
	}
	if v.local != nil {
		var ls []string
		for _, p := range v.local {
			if p.isInt {
				ls = append(ls, string(p.n))
			} else {
				ls = append(ls, p.s)
			}
		}
		s += "+" + strings.Join(ls, ".")
	}
	return s
}

// IsPrerelease is Version.is_prerelease.
func (v *Version) IsPrerelease() bool { return v.dev != nil || v.pre != nil }

var preRank = map[string]int{"a": 0, "b": 1, "rc": 2}

// suffix is _cmpkey's six numbers: pre's rank and number, post's, dev's.
func (v *Version) suffix() (int, num, int, num, int, num) {
	preR, preN := 3, num("0")
	switch {
	case v.pre == nil && v.post == nil && v.dev != nil:
		preR = -1
	case v.pre != nil:
		preR, preN = preRank[v.pre.l], v.pre.n
	}
	postR, postN := 0, num("0")
	if v.post != nil {
		postR, postN = 1, v.post.n
	}
	devR, devN := 1, num("0")
	if v.dev != nil {
		devR, devN = 0, v.dev.n
	}
	return preR, preN, postR, postN, devR, devN
}

func cmpInt(a, b int) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

// Compare is the order of packaging's _cmpkey: -1, 0 or 1.
func Compare(a, b *Version) int {
	if c := a.epoch.cmp(b.epoch); c != 0 {
		return c
	}
	trim := func(r []num) []num {
		i := len(r)
		for i > 0 && r[i-1] == "0" {
			i--
		}
		return r[:i]
	}
	ra, rb := trim(a.release), trim(b.release)
	for i := 0; i < len(ra) && i < len(rb); i++ {
		if c := ra[i].cmp(rb[i]); c != 0 {
			return c
		}
	}
	if c := cmpInt(len(ra), len(rb)); c != 0 {
		return c
	}
	a1, a2, a3, a4, a5, a6 := a.suffix()
	b1, b2, b3, b4, b5, b6 := b.suffix()
	for _, c := range []int{cmpInt(a1, b1), a2.cmp(b2), cmpInt(a3, b3), a4.cmp(b4), cmpInt(a5, b5), a6.cmp(b6)} {
		if c != 0 {
			return c
		}
	}
	if (a.local == nil) != (b.local == nil) {
		if a.local == nil {
			return -1
		}
		return 1
	}
	for i := 0; i < len(a.local) && i < len(b.local); i++ {
		x, y := a.local[i], b.local[i]
		switch {
		case x.isInt && y.isInt:
			if c := x.n.cmp(y.n); c != 0 {
				return c
			}
		case x.isInt != y.isInt: // (n, "") after (-1, s)
			if x.isInt {
				return 1
			}
			return -1
		default:
			if c := strings.Compare(x.s, y.s); c != 0 {
				return c
			}
		}
	}
	return cmpInt(len(a.local), len(b.local))
}

// isIdentifier is str.isidentifier.
func isIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for i, c := range s {
		if c == '_' || unicode.IsLetter(c) || (i > 0 && (unicode.IsDigit(c) || unicode.Is(unicode.Mn, c) || unicode.Is(unicode.Mc, c) ||
			unicode.Is(unicode.Pc, c) || unicode.Is(unicode.Nl, c))) {
			continue
		}
		return false
	}
	return true
}
