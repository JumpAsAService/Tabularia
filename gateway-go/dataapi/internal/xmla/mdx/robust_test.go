package mdx

import (
	"math/rand"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

// seeds are the valid statements of the tables above, as fuzz and mutation seeds.
func seeds() []string {
	var out []string
	for _, c := range validCases {
		out = append(out, c.src)
	}
	for _, c := range refusalCases {
		out = append(out, c.src)
	}
	return out
}

// checkParse is the contract on any input: it returns, a refusal is an *Error
// with a known code and an offset inside the text, and a success prints back
// to an equal tree.
func checkParse(t testing.TB, src string) {
	st, err := Parse(src)
	if err != nil {
		e, ok := err.(*Error)
		if !ok {
			t.Fatalf("error is %T for %q", err, src)
		}
		if e.Offset < 0 || e.Offset > len(src) || e.Code == "" || e.Msg == "" || e.Line < 1 || e.Column < 1 {
			t.Fatalf("bad error %+v for %q", e, src)
		}
		return
	}
	text := st.String()
	again, err := Parse(text)
	if err != nil {
		t.Fatalf("printed form does not parse: %v\nsource:  %q\nprinted: %q", err, src, text)
	}
	if again.String() != text {
		t.Fatalf("print not stable for %q", src)
	}
}

func TestRobustTruncations(t *testing.T) {
	deadline := time.Now().Add(60 * time.Second)
	for _, s := range seeds() {
		for i := 0; i <= len(s); i++ {
			if !utf8.ValidString(s[:i]) {
				continue
			}
			checkParse(t, s[:i])
		}
		if time.Now().After(deadline) {
			t.Fatal("truncations took too long")
		}
	}
}

func TestRobustMutations(t *testing.T) {
	rng := rand.New(rand.NewSource(20261125))
	alphabet := []string{"[", "]", "]]", "(", ")", "{", "}", ",", ".", "*", "+", "-", "/", ":", "&", "'", `"`, "--", "/*", "*/", "//",
		" ", "\n", "SELECT", "FROM", "ON", "WHERE", "NON EMPTY", "WITH", "AND", "OR", "1", "1e", "é", "\x00", "Members", "COLUMNS", "<", ">=", "<>", "="}
	all := seeds()
	deadline := time.Now().Add(60 * time.Second)
	for n := 0; n < 60000; n++ {
		s := all[rng.Intn(len(all))]
		b := []byte(s)
		for k := 1 + rng.Intn(4); k > 0 && len(b) > 0; k-- {
			pos := rng.Intn(len(b) + 1)
			switch rng.Intn(4) {
			case 0: // delete a span
				end := pos + rng.Intn(6)
				if end > len(b) {
					end = len(b)
				}
				b = append(b[:pos:pos], b[end:]...)
			case 1: // insert a token
				ins := alphabet[rng.Intn(len(alphabet))]
				b = append(b[:pos:pos], append([]byte(ins), b[pos:]...)...)
			case 2: // replace a byte
				if pos < len(b) {
					b[pos] = alphabet[rng.Intn(len(alphabet))][0]
				}
			case 3: // splice in another statement's piece
				o := all[rng.Intn(len(all))]
				if len(o) > 0 {
					from := rng.Intn(len(o))
					to := from + rng.Intn(len(o)-from+1)
					b = append(b[:pos:pos], append([]byte(o[from:to]), b[pos:]...)...)
				}
			}
		}
		checkParse(t, string(b))
		if n%1000 == 0 && time.Now().After(deadline) {
			t.Fatal("mutations took too long")
		}
	}
}

func TestRobustHostile(t *testing.T) {
	cases := map[string]string{
		"open braces":     strings.Repeat("{", 100000),
		"open parens":     "SELECT " + strings.Repeat("(", 100000),
		"open brackets":   "SELECT {" + strings.Repeat("[", 100000),
		"many commas":     "SELECT {" + strings.Repeat(",", 100000) + "}",
		"many axes":       "SELECT " + strings.Repeat("{[a]} ON 0,", 20000),
		"many selects":    strings.Repeat("SELECT ", 20000),
		"many from":       "SELECT {[a]} ON 0 FROM " + strings.Repeat("(SELECT {[a]} ON 0 FROM ", 100000),
		"many stars":      "SELECT " + strings.Repeat("* ", 100000),
		"many dots":       "SELECT {[a]" + strings.Repeat(".", 100000) + "}",
		"many keys":       "SELECT {[a]." + strings.Repeat("&[k]", 50000) + "} ON 0 FROM [x]",
		"many comments":   strings.Repeat("/*", 100000),
		"many quotes":     strings.Repeat(`"`, 100001),
		"many ands":       "SELECT Filter({[a]}, " + strings.Repeat("[Measures].[m] > 1 AND ", 20000) + "[Measures].[m] > 1) ON 0 FROM [x]",
		"many empty args": "SELECT DrilldownLevel({[a]}" + strings.Repeat(",", 100000) + ") ON 0 FROM [x]",
		"nul bytes":       strings.Repeat("\x00", 1000),
		"invalid utf8":    strings.Repeat("\xff\xfe", 1000),
		"long name":       "SELECT {[" + strings.Repeat("a", 500000) + "]} ON 0 FROM [x]",
	}
	for name, src := range cases {
		t.Run(name, func(t *testing.T) {
			done := make(chan struct{})
			go func() {
				defer close(done)
				checkParse(t, src)
			}()
			select {
			case <-done:
			case <-time.After(20 * time.Second):
				t.Fatal("did not terminate")
			}
		})
	}
}

func FuzzParse(f *testing.F) {
	for _, s := range seeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, src string) {
		checkParse(t, src)
	})
}
