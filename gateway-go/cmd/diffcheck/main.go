// diffcheck compares the Go gateway with the Python one (docs/specs/gateway-go.md,
// «Compatibility contract»). scripts/diffcheck.sh starts two twin stacks, each with its own
// PostgreSQL and its own fake engine: in stack A the Python gateway answers, in stack B the Go
// gateway (with a Python gateway behind it for the routes not ported yet). Both databases get
// the same seed, then every step of every suite is sent to both, and after each step it
// compares:
//
//   - the answer: status, the headers the browser and the proxies read, the JSON;
//   - the whole content of the two databases, every table, recent timestamps normalized and
//     the columns that hold random values masked;
//   - the requests each gateway sent to its engine.
//
// A suite is a file suite_<module>.go that registers itself in an init(): one per module of
// the Python gateway, so that ports done side by side never edit a shared file.
// DIFF_ONLY=<module>[,<module>] runs only those suites.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Step is one request of a suite, sent to both gateways.
type Step struct {
	Name        string
	Method      string
	Path        string // with its query
	JSON        any    // the body, encoded as JSON (with its Content-Type)
	Raw         []byte // or a raw body
	ContentType string // for Raw
	Auth        func(*http.Request)
	Header      map[string]string
	Engine      []EngineRule // scripted on both engines before this step (nil: the suite's)
	Mask        []string     // dotted paths of the answer's JSON (* for every item) holding random values
	// Scrub: regular expressions whose matches, inside any string of the answer's JSON, are
	// random (an id or a moment written inside a longer text, as a webhook's payload)
	Scrub []string
}

// EngineRule is one scripted answer of the fake engine (cmd/fakeengine).
type EngineRule struct {
	Method string `json:"method,omitempty"`
	Path   string `json:"path"` // a regular expression on the path
	Status int    `json:"status,omitempty"`
	Body   any    `json:"body,omitempty"`
	Text   string `json:"text,omitempty"` // a body that is not JSON (Body then unused)
	Type   string `json:"type,omitempty"` // its content type
}

// Suite is the comparison of one module.
type Suite struct {
	Module string
	// Seed adds what the suite needs to both databases (after the common people)
	Seed func(w *World)
	// Phases run the steps once per phase; nil = once with the environment's settings
	Phases []Phase
	Engine []EngineRule
	Steps  func(w *World) []Step
}

// Phase is a set of panel settings (gateway_settings) both gateways read again within ten
// seconds.
type Phase struct {
	Name  string
	Panel map[string]string
}

var suites []Suite

func register(s Suite) { suites = append(suites, s) }

// maskedColumns: table → columns whose values are random on each side (a token, a session id)
var maskedColumns = map[string][]string{}

func maskColumns(table string, columns ...string) {
	maskedColumns[table] = append(maskedColumns[table], columns...)
}

// maskedPatterns: table → column → what inside a text is random (a token's prefix, a moment
// far from now in an audit event's detail), replaced by «<masked>»
var maskedPatterns = map[string]map[string][]*regexp.Regexp{}

func maskPattern(table, column, pattern string) {
	if maskedPatterns[table] == nil {
		maskedPatterns[table] = map[string][]*regexp.Regexp{}
	}
	maskedPatterns[table][column] = append(maskedPatterns[table][column], regexp.MustCompile(pattern))
}

// scrubbedColumns: table → column → patterns whose matches inside the text are random
var scrubbedColumns = map[string]map[string][]*regexp.Regexp{}

func scrubColumn(table, column string, patterns ...string) {
	if scrubbedColumns[table] == nil {
		scrubbedColumns[table] = map[string][]*regexp.Regexp{}
	}
	for _, p := range patterns {
		scrubbedColumns[table][column] = append(scrubbedColumns[table][column], regexp.MustCompile(p))
	}
}

// scrub replaces the patterns' matches in every string of v by «<scrubbed>».
func scrub(v any, res []*regexp.Regexp) any {
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			x[k] = scrub(e, res)
		}
	case []any:
		for i, e := range x {
			x[i] = scrub(e, res)
		}
	case string:
		for _, re := range res {
			x = re.ReplaceAllString(x, "<scrubbed>")
		}
		return x
	}
	return v
}

// side is one stack.
type side struct {
	name    string
	db      *pgxpool.Pool
	gateway string
	engine  string
}

// World is both stacks and the people seeded in them.
type World struct {
	ctx    context.Context
	a, b   *side
	secret []byte
	People map[string]int64
	// the panel's values both gateways have read
	inForce map[string]string
}

func main() {
	ctx := context.Background()
	w := &World{ctx: ctx, secret: []byte(must("DIFF_SECRET")), People: map[string]int64{}}
	w.a = &side{"python", pool(ctx, must("DIFF_A_DSN")), must("DIFF_A_URL"), must("DIFF_A_ENGINE")}
	w.b = &side{"go", pool(ctx, must("DIFF_B_DSN")), must("DIFF_B_URL"), must("DIFF_B_ENGINE")}
	w.seedPeople()

	only := map[string]bool{}
	for _, m := range strings.Split(os.Getenv("DIFF_ONLY"), ",") {
		if m = strings.TrimSpace(m); m != "" {
			only[m] = true
		}
	}
	sort.SliceStable(suites, func(i, j int) bool { return suites[i].Module < suites[j].Module })
	failed, total := 0, 0
	for _, s := range suites {
		if len(only) > 0 && !only[s.Module] {
			continue
		}
		if s.Seed != nil {
			s.Seed(w)
		}
		phases := s.Phases
		if phases == nil {
			phases = []Phase{{Name: "environment's settings"}}
		}
		for _, ph := range phases {
			w.panel(ph.Panel)
			fmt.Printf("── %s · %s\n", s.Module, ph.Name)
			for _, st := range s.Steps(w) {
				rules := st.Engine
				if rules == nil {
					rules = s.Engine
				}
				w.script(rules)
				total++
				stepAt = time.Now()
				if diff := w.compare(st); diff != "" {
					failed++
					fmt.Printf("  DIFF  %-6s %-40s %s\n%s\n", st.Method, short(st.Path), st.Name, diff)
					continue
				}
				fmt.Printf("  same  %-6s %-40s %s\n", st.Method, short(st.Path), st.Name)
			}
		}
	}
	if failed > 0 {
		fmt.Printf("%d of %d steps differ\n", failed, total)
		os.Exit(1)
	}
	fmt.Printf("%d steps, each on both gateways: no difference\n", total)
}

// Exec runs a statement on both databases (the seed).
func (w *World) Exec(q string, args ...any) {
	for _, s := range []*side{w.a, w.b} {
		if _, err := s.db.Exec(w.ctx, q, args...); err != nil {
			fail("%s: %v\n%s", s.name, err, q)
		}
	}
}

// Insert runs an INSERT … RETURNING id on both and checks they gave the same id.
func (w *World) Insert(q string, args ...any) int64 {
	var ids [2]int64
	for i, s := range []*side{w.a, w.b} {
		if err := s.db.QueryRow(w.ctx, q, args...).Scan(&ids[i]); err != nil {
			fail("%s: %v\n%s", s.name, err, q)
		}
	}
	if ids[0] != ids[1] {
		fail("the seed diverged: id %d in A, %d in B\n%s", ids[0], ids[1], q)
	}
	return ids[0]
}

func (w *World) panel(values map[string]string) {
	if reflect.DeepEqual(values, w.inForce) || (len(values) == 0 && len(w.inForce) == 0) {
		return
	}
	w.Exec(`DELETE FROM gateway_settings`)
	for k, v := range values {
		w.Exec(`INSERT INTO gateway_settings (key, value, updated_at) VALUES ($1, $2, now())`, k, v)
	}
	w.inForce = values
	time.Sleep(11 * time.Second) // overrides.REREAD_EVERY, on both gateways
}

func (w *World) script(rules []EngineRule) {
	if rules == nil {
		rules = []EngineRule{}
	}
	raw, _ := json.Marshal(rules)
	for _, s := range []*side{w.a, w.b} {
		res, err := http.Post(s.engine+"/__script", "application/json", bytes.NewReader(raw))
		if err != nil {
			fail("%s engine script: %v", s.name, err)
		}
		res.Body.Close()
		recorded(s) // forget what came before this step
	}
}

func recorded(s *side) any {
	res, err := http.Get(s.engine + "/__recorded")
	if err != nil {
		fail("%s engine: %v", s.name, err)
	}
	defer res.Body.Close()
	var out any
	json.NewDecoder(res.Body).Decode(&out)
	return normalize(out)
}

// ── one step on both sides ───────────────────────────────────────────────────

type answer struct {
	Status  int
	Headers map[string]string
	Body    any
	Engine  any
	Tables  map[string][]any
}

var compared = []string{"Content-Type", "Www-Authenticate", "Location", "Cache-Control", "Content-Disposition", "Allow", "Retry-After"}

var (
	expires  = regexp.MustCompile(`expires=[^;]+`)
	tokenVal = regexp.MustCompile(`tab_token=[^;"]+`)
)

// noRedirects compares a redirect itself (its Location), never the page it points to.
var noRedirects = &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}

func (w *World) ask(s *side, st Step) answer {
	var body io.Reader
	contentType := st.ContentType
	switch {
	case st.JSON != nil:
		raw, _ := json.Marshal(st.JSON)
		body, contentType = bytes.NewReader(raw), "application/json"
	case st.Raw != nil:
		body = bytes.NewReader(st.Raw)
	}
	req, err := http.NewRequest(st.Method, s.gateway+st.Path, body)
	if err != nil {
		fail("%v", err)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("X-Forwarded-For", "5.6.7.8")
	req.Header.Set("User-Agent", "diffcheck")
	for k, v := range st.Header {
		req.Header.Set(k, v)
		if k == "Host" { // net/http sends req.Host, not the header
			req.Host = v
		}
	}
	if st.Auth != nil {
		st.Auth(req)
	}
	res, err := noRedirects.Do(req)
	if err != nil {
		fail("%s: %v", s.name, err)
	}
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	a := answer{Status: res.StatusCode, Headers: map[string]string{}}
	for _, h := range compared {
		if v := res.Header.Get(h); v != "" {
			// a redirect to the gateway itself names its address, which differs between the sides
			a.Headers[h] = strings.Replace(v, s.gateway, "<gateway>", 1)
		}
	}
	if v := res.Header.Values("Set-Cookie"); len(v) > 0 {
		c := expires.ReplaceAllString(strings.Join(v, " | "), "expires=…")
		a.Headers["Set-Cookie"] = tokenVal.ReplaceAllStringFunc(c, func(m string) string {
			if m == `tab_token=""` {
				return m
			}
			return "tab_token=<token>"
		})
	}
	var parsed any
	if len(raw) > 0 && json.Unmarshal(raw, &parsed) == nil {
		for _, path := range st.Mask {
			parsed = maskPath(parsed, strings.Split(path, "."))
		}
		if len(st.Scrub) > 0 {
			var res []*regexp.Regexp
			for _, p := range st.Scrub {
				res = append(res, regexp.MustCompile(p))
			}
			parsed = scrub(parsed, res)
		}
		a.Body = normalize(parsed)
	} else {
		a.Body = string(bytes.TrimSpace(raw))
	}
	a.Engine = recorded(s)
	a.Tables = dump(w.ctx, s)
	return a
}

func (w *World) compare(st Step) string {
	pa, gb := w.ask(w.a, st), w.ask(w.b, st)
	if os.Getenv("DIFF_VERBOSE") != "" { // what Python answered, to see which branch a step reached
		p, _ := json.Marshal(pa.Body)
		e, _ := json.Marshal(pa.Engine)
		fmt.Printf("        python %d %s\n        engine %s\n", pa.Status, clip(p), clip(e))
	}
	var out []string
	if pa.Status != gb.Status {
		out = append(out, fmt.Sprintf("    status: python %d, go %d", pa.Status, gb.Status))
	}
	if !reflect.DeepEqual(pa.Headers, gb.Headers) {
		out = append(out, fmt.Sprintf("    headers:\n      python %v\n      go     %v", pa.Headers, gb.Headers))
	}
	if !reflect.DeepEqual(pa.Body, gb.Body) {
		p, _ := json.Marshal(pa.Body)
		g, _ := json.Marshal(gb.Body)
		p, g = fromFirstDifference(p, g)
		out = append(out, fmt.Sprintf("    body:\n      python %s\n      go     %s", clip(p), clip(g)))
	}
	if !reflect.DeepEqual(pa.Engine, gb.Engine) {
		p, _ := json.Marshal(pa.Engine)
		g, _ := json.Marshal(gb.Engine)
		out = append(out, fmt.Sprintf("    sent to the engine:\n      python %s\n      go     %s", clip(p), clip(g)))
	}
	for _, t := range tableNames(pa.Tables, gb.Tables) {
		if !reflect.DeepEqual(pa.Tables[t], gb.Tables[t]) {
			out = append(out, fmt.Sprintf("    table %s:\n%s", t, rowDiff(pa.Tables[t], gb.Tables[t])))
		}
	}
	return strings.Join(out, "\n")
}

// ── the databases' content ───────────────────────────────────────────────────

func dump(ctx context.Context, s *side) map[string][]any {
	rows, err := s.db.Query(ctx, `SELECT table_name FROM information_schema.tables
		WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version' ORDER BY 1`)
	if err != nil {
		fail("%s: %v", s.name, err)
	}
	var tables []string
	for rows.Next() {
		var t string
		rows.Scan(&t)
		tables = append(tables, t)
	}
	rows.Close()
	out := map[string][]any{}
	for _, t := range tables {
		var raw []byte
		// every row as JSON, in a stable order whatever the primary key
		q := fmt.Sprintf(`SELECT coalesce(json_agg(j ORDER BY j::text), '[]') FROM (SELECT to_jsonb(x) AS j FROM %q x) r`, t)
		if err := s.db.QueryRow(ctx, q).Scan(&raw); err != nil {
			fail("%s %s: %v", s.name, t, err)
		}
		var list []any
		json.Unmarshal(raw, &list)
		for _, row := range list {
			if m, ok := row.(map[string]any); ok {
				for _, c := range maskedColumns[t] {
					if v, has := m[c]; has && v != nil {
						m[c] = "<masked>"
					}
				}
				for c, patterns := range maskedPatterns[t] {
					if v, ok := m[c].(string); ok {
						for _, re := range patterns {
							v = re.ReplaceAllString(v, "<masked>")
						}
						m[c] = v
					}
				}
				for c, res := range scrubbedColumns[t] {
					if v, has := m[c]; has {
						m[c] = scrub(v, res)
					}
				}
			}
		}
		if len(list) > 0 {
			rows := normalize(list).([]any)
			// in order again once masked: a random value (a session's id) must not decide where
			// its row falls
			sort.SliceStable(rows, func(i, j int) bool {
				a, _ := json.Marshal(rows[i])
				b, _ := json.Marshal(rows[j])
				return string(a) < string(b)
			})
			out[t] = rows
		}
	}
	return out
}

var stamp = regexp.MustCompile(`^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?$`)

// runAt is when the run began and stepAt when the current step did. normalize hides every
// timestamp from ten minutes before the run to ten minutes after the step: the same window
// for both sides of a step (each side measuring from its own read put a timestamp near the
// edge inside on one side and outside on the other), and one that does not leave behind,
// in a run of more than an hour, what the first suites wrote a few milliseconds apart.
var (
	runAt  = time.Now()
	stepAt = runAt
)

// normalize replaces the timestamps written during the run (or seeded just before it) by
// «<now>»: the two sides write them a few milliseconds apart.
func normalize(v any) any {
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			x[k] = normalize(e)
		}
	case []any:
		for i, e := range x {
			x[i] = normalize(e)
		}
	case string:
		if stamp.MatchString(x) {
			s := strings.Replace(x, " ", "T", 1)
			for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999"} {
				if t, err := time.Parse(layout, s); err == nil {
					if t.After(runAt.Add(-10*time.Minute)) && t.Before(stepAt.Add(10*time.Minute)) {
						return "<now>"
					}
					break
				}
			}
		}
	}
	return v
}

func maskPath(v any, path []string) any {
	if len(path) == 0 {
		if v == nil {
			return nil
		}
		return "<masked>"
	}
	switch x := v.(type) {
	case map[string]any:
		if e, ok := x[path[0]]; ok {
			x[path[0]] = maskPath(e, path[1:])
		}
	case []any:
		if path[0] == "*" {
			for i := range x {
				x[i] = maskPath(x[i], path[1:])
			}
		}
	}
	return v
}

func tableNames(a, b map[string][]any) []string {
	set := map[string]bool{}
	for t := range a {
		set[t] = true
	}
	for t := range b {
		set[t] = true
	}
	var out []string
	for t := range set {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

func rowDiff(a, b []any) string {
	var out []string
	n := max(len(a), len(b))
	for i := 0; i < n && len(out) < 6; i++ {
		var ra, rb any
		if i < len(a) {
			ra = a[i]
		}
		if i < len(b) {
			rb = b[i]
		}
		if !reflect.DeepEqual(ra, rb) {
			pa, _ := json.Marshal(ra)
			pb, _ := json.Marshal(rb)
			out = append(out, fmt.Sprintf("      python %s\n      go     %s", clip(pa), clip(pb)))
		}
	}
	return strings.Join(out, "\n")
}

// fromFirstDifference cuts two long texts a little before where they start to differ, so
// that the clipped lines show the difference.
func fromFirstDifference(a, b []byte) ([]byte, []byte) {
	i := 0
	for i < len(a) && i < len(b) && a[i] == b[i] {
		i++
	}
	if i < 300 {
		return a, b
	}
	return append([]byte("…"), a[i-200:]...), append([]byte("…"), b[i-200:]...)
}

func clip(b []byte) string {
	if len(b) > 900 {
		return string(b[:900]) + "…"
	}
	return string(b)
}

func short(p string) string {
	if len(p) > 40 {
		return p[:39] + "…"
	}
	return p
}

func pool(ctx context.Context, dsn string) *pgxpool.Pool {
	p, err := pgxpool.New(ctx, dsn)
	if err != nil {
		fail("%v", err)
	}
	return p
}

func must(name string) string {
	v := os.Getenv(name)
	if v == "" {
		fail("%s is not set: run scripts/diffcheck.sh", name)
	}
	return v
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(2)
}
