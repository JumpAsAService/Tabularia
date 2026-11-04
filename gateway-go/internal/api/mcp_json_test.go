package api

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// json.loads on bytes, as CPython 3.12 answers each (a lone surrogate, which Python keeps,
// is U+FFFD here as in internal/pyjson).
func TestJSONBytesAreDecodedAsJSONLoadsDoes(t *testing.T) {
	for _, c := range []struct {
		in   string
		want string // the value as compact JSON; "" = a ValueError
	}{
		{`{"a": 1}`, `{"a":1}`},
		{"\xef\xbb\xbf{\"a\": 1}", `{"a":1}`},
		{"\xff\xfe{\x00\"\x00a\x00\"\x00:\x00 \x00\"\x00\xe9\x00\"\x00}\x00", `{"a":"é"}`},
		{"{\x00\"\x00a\x00\"\x00:\x00 \x00\"\x00\xe9\x00\"\x00}\x00", `{"a":"é"}`},
		{"\x00{\x00\"\x00a\x00\"\x00:\x00 \x00\"\x00\xe9\x00\"\x00}", `{"a":"é"}`},
		{"\xff\xfe\x00\x00{\x00\x00\x00\"\x00\x00\x00a\x00\x00\x00\"\x00\x00\x00:\x00\x00\x00 \x00\x00\x00\"\x00\x00\x00\x00\xf6\x01\x00\"\x00\x00\x00}\x00\x00\x00", `{"a":"😀"}`},
		{"{\x00\x00\x00\"\x00\x00\x00a\x00\x00\x00\"\x00\x00\x00:\x00\x00\x00 \x00\x00\x001\x00\x00\x00}\x00\x00\x00", `{"a":1}`},
		{"\x00\x00\x00{\x00\x00\x00\"\x00\x00\x00a\x00\x00\x00\"\x00\x00\x00:\x00\x00\x00 \x00\x00\x001\x00\x00\x00}", `{"a":1}`},
		{"\x00{\x00}", `{}`},
		{"{\x00}\x00", `{}`},
		{"\xff\xfe{\x00}", ""},
		{"\xff\xfe{", ""},
		{"{\"a\": \"\xed\xa0\x80\"}", `{"a":"�"}`},
		{"{\"a\": \"\xff\"}", ""},
		{"\"\xc3\"", ""},
		{"\xfe\xff\x00\"\xd8\x00\x00\"", `"�"`},
		{"1\x00", `1`},
		{"\x00\x00\x00{\x00\x00\x00}", `{}`},
		{"\xff\xfe\x00\x00{\x00\x00\x00}\x00\x00\x00", `{}`},
		{"\xff\xfe\x00\x00{\x00\x00", ""},
		{"\xef\xbb\xbf\xef\xbb\xbf{}", ""},
		{"null", `null`},
		{"\"\xed\xb0\x80x\"", `"�x"`},
	} {
		got := ""
		if text, ok := jsonBytesText([]byte(c.in)); ok {
			if v, err := pyjson.Loads(text); err == nil {
				got, _ = pyjson.Dumps(v, pyjson.Options{Compact: true})
			}
		}
		if got != c.want {
			t.Errorf("%q: got %q, want %q", c.in, got, c.want)
		}
	}
}
