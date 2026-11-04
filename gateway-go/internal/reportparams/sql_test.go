package reportparams

import (
	"encoding/json"
	"os"
	"sort"
	"testing"
	"time"
)

// TestAsPythonFills compares check_sql and fill with services/report_params.py on the cases
// testdata/golden.py made, every dialect.
func TestAsPythonFills(t *testing.T) {
	raw, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []map[string]any
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	day := func(y, m, d int) Date { return Date{time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC)} }
	values := []map[string]any{
		{"r": "Nord", "n": int64(5), "d": day(2026, 1, 2), "b": true, "m": []any{"a", "b"}},
		{"r": "it's \\ a\nb\t\x00é", "n": -2.5, "d": nil, "b": false, "m": []any{}},
		{"r": nil, "n": 1e-7, "d": day(1999, 12, 31), "b": nil, "m": []any{int64(1), 2.0, int64(-3)}},
		{"r": "", "n": 123456789012345.0, "d": nil, "b": true, "m": []any{true, nil}},
	}
	types := map[string]string{"r": "text", "n": "number", "d": "date", "b": "boolean", "m": "text"}
	params := map[string]bool{}
	for k := range types {
		params[k] = true
	}
	placeholders := map[string]string{"today": "2026-11-03", "today-7": "2026-10-27", "now:yyyy": "2026", "now": "2026-11-03T10:20",
		"start_of_month+1m": "1762160400"}
	failed := 0
	for i, c := range cases {
		d := DialectOf(c["dialect"].(string))
		got := map[string]any{"dialect": c["dialect"], "sql": c["sql"], "values": c["values"]}
		used, ph, refusal := CheckSQL(c["sql"].(string), d, params)
		if refusal == nil {
			got["used"], got["placeholders"] = sortedKeys(used), sortedKeys(ph)
			texts := map[string]string{}
			for b := range ph {
				if v, ok := placeholders[b]; ok {
					texts[b] = v
				} else {
					texts[b] = "x'y"
				}
			}
			filled, r := Fill(c["sql"].(string), d, types, values[int(c["values"].(float64))], texts)
			if r != nil {
				got["refused"] = r.Message
			} else {
				got["filled"] = filled
			}
		} else {
			got["refused"] = refusal.Message
		}
		g, _ := json.Marshal(got)
		w, _ := json.Marshal(c)
		if string(g) != string(w) {
			failed++
			if failed < 20 {
				t.Errorf("case %d\n got  %s\n want %s", i, g, w)
			}
		}
	}
	if failed > 0 {
		t.Errorf("%d of %d differ", failed, len(cases))
	}
}

func sortedKeys(m map[string]bool) []any {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	r := make([]any, len(out))
	for i, s := range out {
		r[i] = s
	}
	return r
}
