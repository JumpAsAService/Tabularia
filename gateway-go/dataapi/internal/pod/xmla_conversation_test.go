package pod

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// TestXMLAConversations plays the files of testdata/xmla against a pod: each is a sequence of
// requests, each with the answer it must get. The format is explained at the head of each file;
// the one there is written from the protocol's documentation, not recorded from a client.

type conversationStep struct {
	name       string
	key        string
	body       string
	expect     []string
	line       int
	directives []string
}

func readConversation(t *testing.T, path string) []conversationStep {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var steps []conversationStep
	var cur *conversationStep
	inExpect := false
	var body []string
	flush := func() {
		if cur != nil {
			cur.body = strings.TrimSpace(strings.Join(body, "\n"))
			steps = append(steps, *cur)
		}
		cur, body, inExpect = nil, nil, false
	}
	for i, line := range strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n") {
		switch {
		case strings.HasPrefix(line, "=== "):
			flush()
			cur = &conversationStep{name: strings.TrimSpace(strings.TrimPrefix(line, "=== ")), key: "main", line: i + 1}
		case cur == nil:
			// the comments before the first step
		case inExpect:
			if strings.TrimSpace(line) != "" {
				cur.expect = append(cur.expect, strings.TrimSpace(line))
			}
		case strings.TrimSpace(line) == "--- expect":
			inExpect = true
		case len(body) == 0 && strings.HasPrefix(line, "@"):
			f := strings.Fields(line[1:])
			if len(f) == 2 && f[0] == "key" {
				cur.key = f[1]
			} else {
				t.Fatalf("%s:%d: unknown directive %q", path, i+1, line)
			}
		default:
			body = append(body, line)
		}
	}
	flush()
	if len(steps) == 0 {
		t.Fatalf("%s: no step", path)
	}
	return steps
}

func TestXMLAConversations(t *testing.T) {
	files, _ := filepath.Glob("testdata/xmla/*.txt")
	if len(files) == 0 {
		t.Fatal("no conversation in testdata/xmla")
	}
	for _, file := range files {
		t.Run(filepath.Base(file), func(t *testing.T) {
			f := salesFixture(t)
			for _, st := range readConversation(t, file) {
				a := f.post(st.body, st.key)
				status, rows, columns := 200, 0, ""
				var p *parsed
				if a.status == 200 {
					p = parseRowset(t, string(a.body))
					rows, columns = len(p.rows), strings.Join(p.cols, " ")
				}
				next := 0
				for _, e := range st.expect {
					verb, rest, _ := strings.Cut(e, " ")
					rest = strings.TrimSpace(rest)
					switch verb {
					case "status":
						status, _ = strconv.Atoi(rest)
					case "columns":
						if p == nil || columns != strings.Join(strings.Fields(rest), " ") {
							// the Name and Type inside the restrictions of DISCOVER_SCHEMA_ROWSETS belong to their column
							if p == nil || strings.Replace(columns, " Name Type", "", 1) != strings.Join(strings.Fields(rest), " ") {
								t.Errorf("%s (line %d): columns\n got %s\nwant %s", st.name, st.line, columns, rest)
							}
						}
					case "rows":
						if n, _ := strconv.Atoi(rest); p == nil || rows != n {
							t.Errorf("%s (line %d): %d rows, want %s (status %d)", st.name, st.line, rows, rest, a.status)
						}
					case "row":
						if p == nil || next >= len(p.rows) {
							t.Errorf("%s (line %d): no row %d", st.name, st.line, next+1)
							next++
							continue
						}
						for _, kv := range strings.Split(rest, " | ") {
							k, v, _ := strings.Cut(kv, "=")
							if got := p.rows[next][strings.TrimSpace(k)]; got != v {
								t.Errorf("%s (line %d): row %d, %s = %q, want %q", st.name, st.line, next+1, strings.TrimSpace(k), got, v)
							}
						}
						next++
					case "fault":
						if a.status != 500 || !strings.Contains(string(a.body), "<soap:Fault>") || !strings.Contains(string(a.body), rest) {
							t.Errorf("%s (line %d): want a fault with %q: %d %s", st.name, st.line, rest, a.status, a.body)
						}
					default:
						t.Fatalf("%s (line %d): unknown expectation %q", st.name, st.line, e)
					}
				}
				if a.status != status {
					t.Errorf("%s (line %d): status %d, want %d: %s", st.name, st.line, a.status, status, a.body)
				}
			}
		})
	}
}
