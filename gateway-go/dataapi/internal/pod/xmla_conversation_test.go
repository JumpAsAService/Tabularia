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
				var ds *dataset
				if a.status == 200 && strings.Contains(string(a.body), "mddataset") {
					ds = parseDataset(t, string(a.body))
				}
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
					case "axis": // axis Axis1 All | 2025 | 2026: the captions of the tuples, in order
						name, list, _ := strings.Cut(rest, " ")
						want := []string{}
						for _, x := range strings.Split(list, " | ") {
							want = append(want, strings.TrimSpace(x))
						}
						if ds == nil || strings.Join(ds.labels(name), "\x00") != strings.Join(want, "\x00") {
							t.Errorf("%s (line %d): %s is %v, want %q", st.name, st.line, name, ds != nil && ds.axes[name] != nil, want)
						}
					case "cells": // cells N: how many cells are not empty
						if n, _ := strconv.Atoi(rest); ds == nil || len(ds.cells) != n {
							t.Errorf("%s (line %d): cells, want %s", st.name, st.line, rest)
						}
					case "cell": // cell 0=2 | 1=15.75: the value of the cells of these ordinals
						for _, kv := range strings.Split(rest, " | ") {
							k, v, _ := strings.Cut(kv, "=")
							ord, _ := strconv.Atoi(strings.TrimSpace(k))
							if c, ok := ds.cells[ord]; ds == nil || !ok || c.value != v {
								t.Errorf("%s (line %d): cell %d is %+v, want %q", st.name, st.line, ord, c, v)
							}
						}
					case "has", "hasnot": // the body holds this text, or not
						if has := strings.Contains(string(a.body), rest); has != (verb == "has") {
							t.Errorf("%s (line %d): %s %q", st.name, st.line, verb, rest)
						}
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
