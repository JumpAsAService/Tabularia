package mdx

import (
	"os"
	"strings"
	"testing"
)

// TestRecordedCases runs testdata/cases.txt; the format is explained there.
func TestRecordedCases(t *testing.T) {
	raw, err := os.ReadFile("testdata/cases.txt")
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for i, block := range strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n---\n") {
		lines := strings.Split(block, "\n")
		var expect string
		var body []string
		for j, l := range lines {
			if expect == "" {
				if strings.HasPrefix(l, "=>") {
					expect = strings.TrimSpace(strings.TrimPrefix(l, "=>"))
				} else if !strings.HasPrefix(l, "#") && strings.TrimSpace(l) != "" {
					t.Fatalf("case %d line %d: expected a comment or a => line", i+1, j+1)
				}
				continue
			}
			body = append(body, l)
		}
		if expect == "" {
			continue // the leading comment block
		}
		n++
		src := strings.Join(body, "\n")
		t.Run(strings.ReplaceAll(strings.TrimSpace(strings.SplitN(src, "\n", 2)[0]), " ", "_")[:min(40, len(strings.TrimSpace(strings.SplitN(src, "\n", 2)[0])))], func(t *testing.T) {
			f := strings.Fields(expect)
			switch f[0] {
			case "ok":
				text := roundTrip(t, src)
				if rest := strings.TrimPrefix(strings.TrimSpace(strings.TrimPrefix(expect, "ok")), "print="); rest != strings.TrimSpace(strings.TrimPrefix(expect, "ok")) && text != rest {
					t.Errorf("print\n got: %s\nwant: %s", text, rest)
				}
			case "err":
				_, err := Parse(src)
				e, ok := err.(*Error)
				if !ok {
					t.Fatalf("parsed, want error %s", f[1])
				}
				if e.Code != f[1] {
					t.Errorf("code %q, want %q (%s)", e.Code, f[1], e.Msg)
				}
				for _, kv := range f[2:] {
					switch {
					case strings.HasPrefix(kv, "name="):
						// names may hold spaces: take the rest of the line up to " at="
						name := strings.TrimPrefix(expect[strings.Index(expect, "name="):], "name=")
						if k := strings.Index(name, " at="); k >= 0 {
							name = name[:k]
						}
						if e.Name != name {
							t.Errorf("name %q, want %q", e.Name, name)
						}
					case strings.HasPrefix(kv, "at="):
						at := strings.TrimPrefix(expect[strings.Index(expect, "at="):], "at=")
						if want := strings.Index(src, at); e.Offset != want {
							t.Errorf("offset %d, want %d (%q)", e.Offset, want, at)
						}
					}
				}
			default:
				t.Fatalf("unknown expectation %q", expect)
			}
		})
	}
	if n < 5 {
		t.Errorf("only %d recorded cases ran", n)
	}
}
