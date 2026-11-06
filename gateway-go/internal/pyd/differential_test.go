package pyd_test

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"encoding/json"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
)

// The differential test: every request of testdata/cases.json.gz answered by this
// package and compared, byte for byte (status, content type, body), with what the
// real FastAPI + pydantic of the gateway's image answered (testdata/golden.json.gz,
// written by testdata/run.sh). A body that validates is compared through its
// model_dump(mode="json") and the fields it set, so conversions count too.

type testCase struct {
	ID      string  `json:"id"`
	Kind    string  `json:"kind"`
	Model   string  `json:"model"`
	Body    *string `json:"body"`
	BodyB64 string  `json:"body_b64"`
	CT      *string `json:"ct"`
	Route   string  `json:"route"`
	Query   string  `json:"query"`
	Path    string  `json:"path"`
}

type golden struct {
	ID      string `json:"id"`
	Status  int    `json:"status"`
	CT      string `json:"ct"`
	BodyB64 string `json:"body_b64"`
}

// knownDiffs are the cases this package does not reproduce, by id prefix, with
// the reason: they are reported, not hidden, and every other difference fails.
var knownDiffs = map[string]string{
	"body:echo-depth:": "an input nested ~1000 deep, echoed in an error, makes jsonable_encoder exceed " +
		"Python's recursion limit (a 500); the limit depends on the server's stack depth, so it is not emulated",
}

func readGz(t *testing.T, path string, v any) {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewDecoder(zr).Decode(v); err != nil {
		t.Fatal(err)
	}
}

type paramSpec struct {
	name string
	t    *pyd.Type
	def  []any
}

// typeFromSpec builds a parameter's type from routes.json ("opt:int", "lit:a,b", ...).
func typeFromSpec(spec string, cons map[string]any) *pyd.Type {
	var t *pyd.Type
	switch {
	case strings.HasPrefix(spec, "opt:"):
		t = pyd.Optional(typeFromSpec(spec[4:], nil))
	case strings.HasPrefix(spec, "lit:"):
		var vals []any
		for _, v := range strings.Split(spec[4:], ",") {
			vals = append(vals, v)
		}
		t = pyd.Literal(vals...)
	default:
		t = map[string]func() *pyd.Type{"int": pyd.Int, "str": pyd.Str, "bool": pyd.Bool, "float": pyd.Float,
			"datetime": pyd.Datetime}[spec]()
	}
	for k, v := range cons {
		n := jsonNum(v)
		switch k {
		case "ge":
			t.Ge(n)
		case "le":
			t.Le(n)
		case "gt":
			t.Gt(n)
		case "lt":
			t.Lt(n)
		case "min_length":
			t.MinLen(int(v.(float64)))
		case "max_length":
			t.MaxLen(int(v.(float64)))
		case "pattern":
			t.Pattern(v.(string))
		}
	}
	return t
}

// jsonNum turns a routes.json number into the int or float Python read.
func jsonNum(v any) any {
	if f, ok := v.(float64); ok && f == float64(int64(f)) {
		return int64(f)
	}
	return v
}

func loadRoutes(t *testing.T) (map[string][]paramSpec, map[string]*pyd.Type) {
	var raw struct {
		Query map[string][][]any `json:"query"`
		Path  map[string]string  `json:"path"`
	}
	b, err := os.ReadFile("testdata/routes.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	query := map[string][]paramSpec{}
	for route, params := range raw.Query {
		for _, p := range params {
			cons, _ := p[2].(map[string]any)
			ps := paramSpec{name: p[0].(string), t: typeFromSpec(p[1].(string), cons)}
			if p[3] != "REQUIRED" {
				ps.def = []any{jsonNum(p[3])}
			}
			query[route] = append(query[route], ps)
		}
	}
	path := map[string]*pyd.Type{}
	for route, spec := range raw.Path {
		path[route] = typeFromSpec(spec, nil)
	}
	return query, path
}

func answer(status int, ct string, body []byte) (int, string, []byte) { return status, ct, body }

func lenient(t *testing.T, v any) []byte {
	b, err := pyd.DumpsLenient(v)
	if err != nil {
		t.Fatalf("dump: %v", err)
	}
	return b
}

func success(t *testing.T, mv *pyd.ModelValue) []byte {
	o := pyd.NewObject()
	if mv == nil {
		o.Set("dump", nil)
		o.Set("set", []any{})
	} else {
		o.Set("dump", mv.Dump())
		set := []any{}
		for _, n := range mv.FieldsSet() {
			set = append(set, n)
		}
		o.Set("set", set)
	}
	return lenient(t, o)
}

func values(t *testing.T, names []string, vals []any) []byte {
	v := pyd.NewObject()
	for i, n := range names {
		v.Set(n, pyd.DumpValue(vals[i]))
	}
	o := pyd.NewObject()
	o.Set("values", v)
	return lenient(t, o)
}

// maxShown is how many failures per group are printed (PYD_SHOW_ALL=1: all of them).
var maxShown = func() int {
	if os.Getenv("PYD_SHOW_ALL") != "" {
		return 1 << 30
	}
	return 3
}()

func TestDifferential(t *testing.T) {
	var cases []testCase
	var goldens []golden
	readGz(t, "testdata/cases.json.gz", &cases)
	readGz(t, "testdata/golden.json.gz", &goldens)
	if len(cases) != len(goldens) {
		t.Fatalf("%d cases, %d golden answers: run testdata/run.sh", len(cases), len(goldens))
	}
	models := map[string]*pyd.Model{}
	for k, m := range gatewaymodels.Registry {
		models[k] = m
	}
	for k, m := range synRegistry {
		models[k] = m
	}
	queryRoutes, pathRoutes := loadRoutes(t)

	run := func(c testCase) (int, string, []byte) {
		var raw []byte
		if c.Body != nil {
			raw = []byte(*c.Body)
		} else if c.BodyB64 != "" {
			raw, _ = base64.StdEncoding.DecodeString(c.BodyB64)
		}
		ct := ""
		if c.CT != nil {
			ct = *c.CT
		}
		var p pyd.Params
		switch c.Kind {
		case "body", "obody":
			m := models[c.Model]
			if m == nil {
				t.Fatalf("%s: no Go model %s", c.ID, c.Model)
			}
			b, resp := pyd.ReadBody(raw, ct)
			if resp != nil {
				return answer(resp.Status, resp.ContentType, resp.Body)
			}
			var mv *pyd.ModelValue
			if c.Kind == "body" {
				mv = p.Body(b, m)
			} else {
				mv = p.OptionalBody(b, m)
			}
			if r := p.Response(); r != nil {
				return answer(r.Status, r.ContentType, r.Body)
			}
			return answer(200, "application/json", success(t, mv))
		case "query":
			q := pyd.ParseQuery(c.Query)
			var names []string
			var vals []any
			for _, ps := range queryRoutes[c.Route] {
				names = append(names, ps.name)
				vals = append(vals, p.Query(q, ps.name, ps.t, ps.def...))
			}
			if r := p.Response(); r != nil {
				return answer(r.Status, r.ContentType, r.Body)
			}
			return answer(200, "application/json", values(t, names, vals))
		case "path":
			v := p.Path("v", c.Path, pathRoutes[c.Route])
			if r := p.Response(); r != nil {
				return answer(r.Status, r.ContentType, r.Body)
			}
			return answer(200, "application/json", values(t, []string{"v"}, []any{v}))
		case "combo":
			b, resp := pyd.ReadBody(raw, "application/json")
			if resp != nil {
				return answer(resp.Status, resp.ContentType, resp.Body)
			}
			p.Path("pid", c.Path, pyd.Int())
			p.Query(pyd.ParseQuery(c.Query), "limit", pyd.Int().Ge(1).Le(500), 50)
			p.Body(b, gatewaymodels.LoginRequest)
			if r := p.Response(); r != nil {
				return answer(r.Status, r.ContentType, r.Body)
			}
			return answer(200, "application/json", []byte(`{"ok":true}`))
		}
		t.Fatalf("%s: unknown kind", c.ID)
		return 0, "", nil
	}

	passed, known := 0, 0
	failedBy := map[string]int{}
	for i, c := range cases {
		g := goldens[i]
		if g.ID != c.ID {
			t.Fatalf("golden %d is %s, not %s: run testdata/run.sh", i, g.ID, c.ID)
		}
		want, _ := base64.StdEncoding.DecodeString(g.BodyB64)
		status, ct, got := run(c)
		if status == g.Status && ct == g.CT && bytes.Equal(got, want) {
			passed++
			continue
		}
		reason := ""
		for prefix, r := range knownDiffs {
			if strings.HasPrefix(c.ID, prefix) {
				reason = r
			}
		}
		if reason != "" {
			known++
			t.Logf("KNOWN DIFFERENCE %s: %s", c.ID, reason)
			continue
		}
		group := c.ID[:strings.LastIndex(c.ID, ":")]
		failedBy[group]++
		if failedBy[group] <= maxShown {
			in := c.Query + c.Path
			if c.Body != nil {
				in = *c.Body
			} else if c.BodyB64 != "" {
				in = "b64:" + c.BodyB64
			}
			t.Errorf("%s model=%s\n  input: %.300s\n  want %d %s %.900s\n  got  %d %s %.900s",
				c.ID, c.Model, in, g.Status, g.CT, want, status, ct, got)
		}
	}
	var groups []string
	for g, n := range failedBy {
		groups = append(groups, g+"="+strconv.Itoa(n))
	}
	sort.Strings(groups)
	t.Logf("%d/%d cases identical to FastAPI+pydantic, %d known differences, failures by group: %v",
		passed, len(cases), known, groups)
}
