package bayescodegen

import (
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the check of the output (docs/specs/bayes-stan-codegen.md, «Safety checks on the
// output»), independent of how the text was written: the alphabet and the layout, the refused
// words, and a lexical scan of every call and every sampling statement against the catalogue's
// lists. Generate runs it on every program before it returns one; a failure is a bug of the codegen
// and no program is returned. stanc's --info (CrossCheck) is lot S3's.

// programByte reports the bytes of the program's alphabet: ^[A-Za-z0-9_ .,;:()\[\]{}<>=+\-*/^~\n?]*$.
func programByte(c byte) bool {
	switch {
	case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		return true
	}
	return strings.IndexByte("_ .,;:()[]{}<>=+-*/^~\n?", c) >= 0
}

func wordByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_'
}

var refusedToken = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range refusedTokens {
		m[w] = true
	}
	return m
}()

// CheckProgram checks a program's text: the alphabet and none of //, /* and */; the layout (a line
// feed after every line, no blank line, no trailing space, an indentation of two spaces a level);
// brackets balanced; no refused word as a token; every name followed by "(" a name the program may
// call (except the keyword for) and every name after "~" a statement of the catalogue.
func CheckProgram(text string) error {
	if text == "" || text[len(text)-1] != '\n' {
		return bug("the program does not end with a line feed")
	}
	for i := 0; i < len(text); i++ {
		c := text[i]
		if !programByte(c) {
			return bug("a byte outside the program's alphabet: %s", strconv.QuoteToASCII(string(c)))
		}
		if i+1 < len(text) {
			switch text[i : i+2] {
			case "//", "/*", "*/":
				return bug("a comment in the program")
			}
		}
	}
	if err := checkLayout(text); err != nil {
		return err
	}
	return scanTokens(text)
}

func checkLayout(text string) error {
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	for n, l := range lines {
		where := "line " + strconv.Itoa(n+1)
		indent := len(l) - len(strings.TrimLeft(l, " "))
		switch {
		case strings.TrimSpace(l) == "":
			return bug("%s is blank", where)
		case l[len(l)-1] == ' ':
			return bug("%s ends with a space", where)
		case indent%2 != 0:
			return bug("%s is not indented by two spaces a level", where)
		}
	}
	return nil
}

// scanTokens reads the text as Stan's lexer would, enough to find every name and what follows it.
func scanTokens(s string) error {
	var open []byte
	prev := byte(0) // the last byte that is not a space
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch c {
		case '(', '[', '{':
			open = append(open, c)
		case ')', ']', '}':
			want := map[byte]byte{')': '(', ']': '[', '}': '{'}[c]
			if len(open) == 0 || open[len(open)-1] != want {
				return bug("unbalanced brackets in the program")
			}
			open = open[:len(open)-1]
		case ':':
			if !(wordByte(prev) || prev == '[') {
				return bug("a colon outside a range, a slice or a conditional")
			}
		case '?':
			if i == 0 || s[i-1] != ' ' || i+1 == len(s) || s[i+1] != ' ' {
				return bug("a question mark outside a conditional")
			}
		}
		if c >= '0' && c <= '9' && (i == 0 || !wordByte(s[i-1])) {
			j, err := scanNumber(s, i)
			if err != nil {
				return err
			}
			i, prev = j-1, '0'
			continue
		}
		if c == '.' && (i+1 == len(s) || strings.IndexByte("*/^", s[i+1]) < 0) {
			return bug("a point outside a number or an elementwise operator")
		}
		if wordByte(c) && (i == 0 || !wordByte(s[i-1])) {
			j := i
			for j < len(s) && wordByte(s[j]) {
				j++
			}
			word := s[i:j]
			if c == '_' {
				return bug("a name starting with _ in the program")
			}
			if refusedToken[word] {
				return bug("the word %q in the program", word)
			}
			k := j
			for k < len(s) && s[k] == ' ' {
				k++
			}
			switch {
			case prev == '~':
				if !statements[word] || k == len(s) || s[k] != '(' {
					return bug("%q after ~ is not a statement the catalogue lists", word)
				}
			case k < len(s) && s[k] == '(' && word != "for" && !callable[word]:
				return bug("a call outside the catalogue: %q", word)
			}
			i, prev = j-1, 'a'
			continue
		}
		if c != ' ' && c != '\n' {
			prev = c
		}
		if c == '~' {
			// the next token must be a name (checked above when it comes)
			k := i + 1
			for k < len(s) && s[k] == ' ' {
				k++
			}
			if k == len(s) || !(s[k] >= 'a' && s[k] <= 'z') {
				return bug("~ not followed by a statement")
			}
		}
	}
	if len(open) != 0 {
		return bug("unbalanced brackets in the program")
	}
	return nil
}

// scanNumber reads the number at s[i]: digits, [. digits], [e [+-] digits], and nothing of a name
// or another point after it. It returns the offset after the number.
func scanNumber(s string, i int) (int, error) {
	digits := func(j int) int {
		for j < len(s) && s[j] >= '0' && s[j] <= '9' {
			j++
		}
		return j
	}
	j := digits(i)
	if j < len(s) && s[j] == '.' {
		if k := digits(j + 1); k > j+1 {
			j = k
		} else {
			return 0, bug("a number without its decimals")
		}
	}
	if j < len(s) && (s[j] == 'e' || s[j] == 'E') {
		k := j + 1
		if k < len(s) && (s[k] == '+' || s[k] == '-') {
			k++
		}
		if m := digits(k); m > k {
			j = m
		} else {
			return 0, bug("a number without its exponent")
		}
	}
	if j < len(s) && (wordByte(s[j]) || s[j] == '.') {
		return 0, bug("a number followed by a name or a point")
	}
	return j, nil
}

// CheckIdentifiers checks the mapping table against the identifier rule: every identifier is
// lexically Stan's, unique, not a reserved name nor ending with a reserved suffix nor with "__";
// one that starts with a lower-case letter is the person's name of its node, never renamed; one
// that starts with an upper-case letter is generated.
func CheckIdentifiers(mapping []MappingEntry) error {
	seen := map[string]bool{}
	for _, e := range mapping {
		s := e.Stan
		if s == "" || !(s[0] >= 'a' && s[0] <= 'z' || s[0] >= 'A' && s[0] <= 'Z') {
			return bug("the identifier %s is not one", strconv.Quote(s))
		}
		for i := 1; i < len(s); i++ {
			if !wordByte(s[i]) {
				return bug("the identifier %s is not one", strconv.Quote(s))
			}
		}
		if seen[s] {
			return bug("the identifier %s is declared twice", s)
		}
		seen[s] = true
		if strings.HasSuffix(s, "__") || reservedNames[s] || bayescatalog.StanLists().BackendMember(s) != "" {
			return bug("the identifier %s is a word Stan reserves", s)
		}
		for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
			if strings.HasSuffix(s, suffix) {
				return bug("the identifier %s ends with %s", s, suffix)
			}
		}
		if s[0] >= 'a' && s[0] <= 'z' {
			if e.Name == nil || *e.Name != s || e.Renamed != nil {
				return bug("the identifier %s is not its node's name", s)
			}
		} else if e.Name != nil && e.Renamed == nil {
			return bug("the identifier %s is generated for a name that passes the rule", s)
		}
	}
	return nil
}
