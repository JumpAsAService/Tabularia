package pyjson

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"testing"
	"time"
)

// decode reads a case's tagged value: {"obj": [[k, v], …]} an ordered dict, {"f": "1e16"} a
// float, {"dt": "…"} a naive datetime; JSON numbers without a point stay integers.
func decode(t *testing.T, raw json.RawMessage) any {
	var probe any
	d := json.NewDecoder(bytesReader(raw))
	d.UseNumber()
	if err := d.Decode(&probe); err != nil {
		t.Fatal(err)
	}
	return tagged(t, probe)
}

func tagged(t *testing.T, v any) any {
	switch x := v.(type) {
	case map[string]any:
		if pairs, ok := x["obj"].([]any); ok {
			o := Object{}
			for _, p := range pairs {
				kv := p.([]any)
				o = append(o, KV{kv[0].(string), tagged(t, kv[1])})
			}
			return o
		}
		if f, ok := x["f"].(string); ok {
			switch f {
			case "inf":
				return math.Inf(1)
			case "-inf":
				return math.Inf(-1)
			case "nan":
				return math.NaN()
			}
			n, err := strconv.ParseFloat(f, 64)
			if err != nil {
				t.Fatal(err)
			}
			return n
		}
		if s, ok := x["dt"].(string); ok {
			at, err := time.Parse("2006-01-02T15:04:05.999999", s)
			if err != nil {
				t.Fatal(err)
			}
			return at
		}
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = tagged(t, e)
		}
		return out
	case json.Number:
		n, err := x.Int64()
		if err != nil {
			t.Fatal(err)
		}
		return n
	}
	return v
}

// Every case against what Python's json.dumps wrote (testdata/golden.py).
func TestAsPythonsJSONDumps(t *testing.T) {
	var cases []struct {
		V       json.RawMessage `json:"v"`
		ASCII   bool            `json:"ascii"`
		Compact bool            `json:"compact"`
		Sort    bool            `json:"sort"`
	}
	var golden []string
	read(t, "testdata/cases.json", &cases)
	read(t, "testdata/golden.json", &golden)
	if len(cases) != len(golden) {
		t.Fatalf("%d cases, %d golden", len(cases), len(golden))
	}
	for i, c := range cases {
		got, err := Dumps(decode(t, c.V), Options{EnsureASCII: c.ASCII, Compact: c.Compact, SortKeys: c.Sort})
		if err != nil {
			t.Fatal(err)
		}
		if got != golden[i] {
			t.Errorf("case %d:\n got  %s\n want %s", i, got, golden[i])
		}
	}
}

func TestIsoformatAsUtcIso(t *testing.T) {
	if s := Isoformat(time.Date(2026, 11, 3, 18, 0, 0, 0, time.UTC)); s != "2026-11-03T18:00:00+00:00" {
		t.Error(s)
	}
	if s := Isoformat(time.Date(2026, 11, 3, 18, 0, 0, 120000000, time.UTC)); s != "2026-11-03T18:00:00.120000+00:00" {
		t.Error(s)
	}
}

func read(t *testing.T, path string, v any) {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, v); err != nil {
		t.Fatal(err)
	}
}
