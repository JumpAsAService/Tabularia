package bayesexpr

import (
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"slices"
	"testing"
)

// corpusCase is one case of testdata/corpus/handwritten.json. A case without Env is read by
// Parse alone (lot E0); the type checker's cases carry an environment.
type corpusCase struct {
	ID     string          `json:"id"`
	Input  string          `json:"input"`
	Env    json.RawMessage `json:"env,omitempty"`
	Note   string          `json:"note,omitempty"`
	Expect struct {
		OK     bool              `json:"ok"`
		AST    json.RawMessage   `json:"ast"`
		Format string            `json:"format"`
		Code   Code              `json:"code"`
		Pos    int               `json:"pos"`
		Len    int               `json:"len"`
		Args   map[string]string `json:"args"`
	} `json:"expect"`
}

func readCorpus(t testing.TB) []corpusCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/corpus/handwritten.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []corpusCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	return file.Cases
}

// sameJSON compares two JSON texts as values (key order and spacing aside).
func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatalf("%s: %v", a, err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatalf("%s: %v", b, err)
	}
	return reflect.DeepEqual(x, y)
}

func TestCorpusSyntax(t *testing.T) {
	cases := readCorpus(t)
	seen := map[string]bool{}
	codes := map[Code]int{}
	for _, c := range cases {
		if seen[c.ID] {
			t.Errorf("%s: id twice", c.ID)
		}
		seen[c.ID] = true
		if c.Env != nil {
			continue
		}
		n, err := Parse(c.Input)
		if c.Expect.OK {
			if err != nil {
				t.Errorf("%s: %q: want ok, got %v", c.ID, c.Input, err)
				continue
			}
			got, _ := json.Marshal(n)
			if !sameJSON(t, got, c.Expect.AST) {
				t.Errorf("%s: %q: ast %s, want %s", c.ID, c.Input, got, c.Expect.AST)
			}
			if f := Format(n); f != c.Expect.Format {
				t.Errorf("%s: %q: format %q, want %q", c.ID, c.Input, f, c.Expect.Format)
			}
			continue
		}
		var e *Error
		if !errors.As(err, &e) {
			t.Errorf("%s: %q: want %s, got %v", c.ID, c.Input, c.Expect.Code, err)
			continue
		}
		codes[c.Expect.Code]++
		want := &Error{Code: c.Expect.Code, Pos: c.Expect.Pos, Len: c.Expect.Len, Args: c.Expect.Args}
		if e.Code != want.Code || e.Pos != want.Pos || e.Len != want.Len || !sameArgs(e.Args, want.Args) {
			t.Errorf("%s: %q: got %v, want %v", c.ID, c.Input, e, want)
		}
	}
	for _, code := range syntaxCodes {
		if codes[code] < 2 {
			t.Errorf("%s: %d cases in the corpus, want at least 2", code, codes[code])
		}
	}
	for code := range codes {
		if !slices.Contains(syntaxCodes, code) {
			t.Errorf("%s: not a code of Parse", code)
		}
	}
}

func sameArgs(a, b map[string]string) bool {
	if len(a) == 0 && len(b) == 0 {
		return true
	}
	return reflect.DeepEqual(a, b)
}
