package pod

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The pods' program on real DuckDB files: a datasource's parquet written here, served by a
// file server as the storage would, prepared, and asked over HTTP as a CRM would.

// the rows of the test datasource, i from 1 to 1000, written in a random order
const sourceSQL = `COPY (SELECT i AS id, 'name ' || i AS name,
	CASE WHEN i %% 10 = 0 THEN NULL ELSE ['North', 'South', 'Centre'][1 + i %% 3] END AS region,
	(i * 1.25)::DECIMAL(12,2) AS score, DATE '2026-01-01' + (i %% 100)::INTEGER AS signup,
	TIMESTAMP '2026-01-01 08:00:00' + INTERVAL (i) MINUTE AS seen, i %% 2 = 0 AS active, i / 7.0 AS ratio,
	[i, i + 1] AS tags, CASE WHEN i %% 50 = 0 THEN NULL ELSE i %% 13 END AS bucket, 'odd ' || i AS "odd ""col"""
	FROM range(1, %d) t(i) ORDER BY hash(i * %d)) TO '%s' (FORMAT parquet)`

type row struct {
	id     int64
	region *string
	score  float64
	signup time.Time
	seen   time.Time
	active bool
	ratio  float64
	bucket *int64
	name   string
}

func rowOf(i int64) row {
	r := row{id: i, score: float64(i) * 1.25, signup: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, int(i%100)),
		seen: time.Date(2026, 1, 1, 8, 0, 0, 0, time.UTC).Add(time.Duration(i) * time.Minute), active: i%2 == 0,
		ratio: float64(i) / 7, name: fmt.Sprintf("name %d", i)}
	if i%10 != 0 {
		reg := []string{"North", "South", "Centre"}[i%3]
		r.region = &reg
	}
	if i%50 != 0 {
		b := i % 13
		r.bucket = &b
	}
	return r
}

func writeSource(t *testing.T, path string, rows, seed int) {
	t.Helper()
	db, err := sql.Open("duckdb", "")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(fmt.Sprintf(sourceSQL, rows+1, seed, path)); err != nil {
		t.Fatal(err)
	}
}

type fixture struct {
	t     *testing.T
	dir   string
	files *httptest.Server
	s     *Server
	h     http.Handler
	m     *dataapiwire.Manifest
	now   time.Time
	keys  map[string]string
	logs  *bytes.Buffer
}

var allOps = []string{"eq", "in", "gt", "gte", "lt", "lte", "null"}

func customers(url string) dataapiwire.Resource {
	return dataapiwire.Resource{Name: "customers", Available: true, Version: "2026-11-07T08:00:00Z", Fingerprint: "one", URL: url,
		Columns: []string{"id", "name", "region", "score", "signup", "seen", "active", "ratio", "tags", "bucket", `odd "col"`},
		Filters: []dataapiwire.Filter{{Column: "id", Operators: allOps}, {Column: "region", Operators: []string{"eq", "in", "null"}},
			{Column: "score", Operators: []string{"gt", "gte", "lt", "lte"}}, {Column: "signup", Operators: []string{"eq", "gte", "lt"}},
			{Column: "seen", Operators: []string{"gt", "lte"}}, {Column: "active", Operators: []string{"eq"}},
			{Column: "ratio", Operators: []string{"lt"}}, {Column: "bucket", Operators: []string{"eq", "null", "gte"}},
			{Column: "name", Operators: []string{"eq"}}},
		MaxPage: 100}
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{t: t, dir: t.TempDir(), now: time.Date(2026, 11, 7, 9, 0, 0, 0, time.UTC), keys: map[string]string{},
		logs: &bytes.Buffer{}}
	src := filepath.Join(f.dir, "storage")
	if err := os.MkdirAll(src, 0o700); err != nil {
		t.Fatal(err)
	}
	writeSource(t, filepath.Join(src, "v1.parquet"), 1000, 7)
	db, _ := sql.Open("duckdb", "")
	if _, err := db.Exec(fmt.Sprintf(`COPY (SELECT i AS order_id, i %% 7 AS customer FROM range(1, 51) t(i)) TO '%s' (FORMAT parquet)`,
		filepath.Join(src, "orders.parquet"))); err != nil {
		t.Fatal(err)
	}
	db.Close()
	f.files = httptest.NewServer(http.FileServer(http.Dir(src)))
	t.Cleanup(f.files.Close)
	cfg := Config{APIID: 1, MemoryMB: 256, Threads: 2, QueryTimeout: 10 * time.Second, ManifestMaxAge: 15 * time.Minute, PodName: "test"}
	prep := &Preparer{Dir: filepath.Join(f.dir, "data"), HTTP: http.DefaultClient}
	f.s = NewServer(cfg, slog.New(slog.NewJSONHandler(f.logs, nil)), nil, prep)
	f.s.now = func() time.Time { return f.now }
	f.s.limiter.now = f.s.now
	f.h = f.s.Handler()
	t.Cleanup(f.s.Close)
	key := func(name string, k dataapiwire.Key) dataapiwire.Key {
		secret, _ := dataapiwire.Random(dataapiwire.KeyPrefix)
		f.keys[name] = secret
		k.Hash, k.Hint = dataapiwire.Hash(secret), secret[4:12]
		if k.ExpiresAt.IsZero() {
			k.ExpiresAt = f.now.Add(30 * 24 * time.Hour)
		}
		if k.RatePerMinute == 0 {
			k.RatePerMinute = 100_000
		}
		return k
	}
	past, future := f.now.Add(-time.Hour), f.now.Add(time.Hour)
	f.m = &dataapiwire.Manifest{ID: 1, Slug: "crm", Name: "CRM", State: "active", Replicas: 1, PollSeconds: 20, At: f.now,
		Resources: []dataapiwire.Resource{customers(f.files.URL + "/v1.parquet"),
			{Name: "orders", Available: true, Version: "2026-11-07T07:00:00Z", Fingerprint: "o1", URL: f.files.URL + "/orders.parquet",
				Columns: []string{"order_id", "customer"}, Filters: []dataapiwire.Filter{}, MaxPage: 10}},
		Keys: []dataapiwire.Key{
			key("main", dataapiwire.Key{ID: 1}),
			key("limited", dataapiwire.Key{ID: 2, RatePerMinute: 2}),
			key("cidr", dataapiwire.Key{ID: 3, CIDRs: []string{"203.0.113.0/24", "2001:db8::/32"}}),
			key("orders", dataapiwire.Key{ID: 4, Resources: []string{"orders"}}),
			key("revoked", dataapiwire.Key{ID: 5, RevokedAt: &past}),
			key("expired", dataapiwire.Key{ID: 6, ExpiresAt: past}),
			key("grace", dataapiwire.Key{ID: 7, GraceUntil: &future}),
			key("replaced", dataapiwire.Key{ID: 8, GraceUntil: &past}),
		}}
	return f
}

// load applies the manifest and prepares the data
func (f *fixture) load() {
	f.t.Helper()
	f.s.Apply(f.m)
	f.s.PrepareAll(context.Background())
	if !f.s.Ready() {
		f.t.Fatalf("not ready: %s", f.logs.String())
	}
}

type answer struct {
	status int
	header http.Header
	body   []byte
	json   map[string]any
}

func (a answer) code() string { s, _ := a.json["code"].(string); return s }

func (a answer) ids() []int64 {
	out := []int64{}
	data, _ := a.json["data"].([]any)
	for _, r := range data {
		out = append(out, int64(r.(map[string]any)["id"].(float64)))
	}
	return out
}

// get asks a path with a key (by its name in the fixture, or as it is), from 198.51.100.7
func (f *fixture) get(path, key string, edit ...func(*http.Request)) answer {
	f.t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	r.RemoteAddr = "198.51.100.7:4321"
	if secret, ok := f.keys[key]; ok {
		key = secret
	}
	if key != "" {
		r.Header.Set("Authorization", "Bearer "+key)
	}
	for _, e := range edit {
		e(r)
	}
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	a := answer{status: w.Code, header: w.Header(), body: w.Body.Bytes()}
	if strings.HasPrefix(w.Header().Get("Content-Type"), "application/json") {
		var v map[string]any
		if err := json.Unmarshal(a.body, &v); err != nil {
			f.t.Fatalf("%s: not JSON: %s", path, a.body)
		}
		a.json = v
	}
	return a
}

func (f *fixture) want(a answer, status int, code string) {
	f.t.Helper()
	if a.status != status || (code != "" && a.code() != code) {
		f.t.Fatalf("want %d %s, got %d %s", status, code, a.status, a.body)
	}
	if status >= 400 {
		if d, _ := a.json["detail"].(string); d == "" {
			f.t.Fatalf("no detail: %s", a.body)
		}
	}
}

func TestNotReadyBeforeTheData(t *testing.T) {
	f := newFixture(t)
	f.want(f.get("/data/crm/customers?id=1", "main"), 503, "loading")
	if a := f.get("/readyz", ""); a.status != 503 {
		t.Fatal(a.status)
	}
	if a := f.get("/data/crm/health", ""); a.status != 503 || a.json["status"] != "loading" {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// the manifest is in, the data not yet: the keys are checked, the data waits
	f.s.Apply(f.m)
	f.want(f.get("/data/crm/customers?id=1", ""), 401, "key_missing")
	a := f.get("/data/crm/customers?id=1", "main")
	f.want(a, 503, "loading")
	if a.header.Get("Retry-After") != "5" {
		t.Error(a.header)
	}
	if f.s.Ready() {
		t.Fatal("ready without data")
	}
	f.s.PrepareAll(context.Background())
	f.want(f.get("/data/crm/customers?id=1", "main"), 200, "")
	if a := f.get("/readyz", ""); a.status != 200 {
		t.Fatal(a.status)
	}
	if a := f.get("/data/crm/health", ""); a.status != 200 || a.json["status"] != "ready" {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// a resource the creator may no longer read does not hold the pod back, nor does one with
	// no data yet
	f.m.Resources[1].Available, f.m.Resources[1].Unavailable, f.m.Resources[1].URL = false, "no_data", ""
	f.s.Apply(f.m)
	if !f.s.Ready() {
		t.Error("not ready")
	}
}

func TestKeys(t *testing.T) {
	f := newFixture(t)
	f.load()
	ok := "/data/crm/customers?id=5"
	f.want(f.get(ok, "main"), 200, "")
	f.want(f.get(ok, "grace"), 200, "")
	f.want(f.get(ok, ""), 401, "key_missing")
	a := f.get(ok, "tdk_short")
	f.want(a, 401, "key_invalid")
	if a.header.Get("WWW-Authenticate") == "" {
		t.Error(a.header)
	}
	other, _ := dataapiwire.Random(dataapiwire.KeyPrefix)
	f.want(f.get(ok, other), 401, "key_invalid")
	f.want(f.get(ok, "revoked"), 401, "key_revoked")
	f.want(f.get(ok, "expired"), 401, "key_expired")
	f.want(f.get(ok, "replaced"), 401, "key_replaced")
	// X-API-Key does as well; another scheme does not
	f.want(f.get(ok, "", func(r *http.Request) { r.Header.Set("X-API-Key", f.keys["main"]) }), 200, "")
	f.want(f.get(ok, "", func(r *http.Request) { r.Header.Set("Authorization", "Basic "+f.keys["main"]) }), 401, "key_missing")
	// never in the address: said so even when it is the only key sent, and on the description
	for _, q := range []string{"?api_key=", "?id=1&key=", "?_cursor="} {
		f.want(f.get("/data/crm/customers"+q+f.keys["main"], ""), 400, "key_in_query")
		f.want(f.get("/data/crm/customers"+q+f.keys["main"], "grace"), 400, "key_in_query")
		f.want(f.get("/data/crm/openapi.json"+q+f.keys["main"], ""), 400, "key_in_query")
	}
	// a key of another resource
	f.want(f.get(ok, "orders"), 403, "key_not_allowed")
	f.want(f.get("/data/crm/orders", "orders"), 200, "")
	// another API, no such resource
	f.want(f.get("/data/erp/customers?id=5", "main"), 404, "not_found")
	f.want(f.get("/data/crm/invoices", "main"), 404, "unknown_resource")
	f.want(f.get("/data/crm/", "main"), 404, "not_found")
	f.want(f.get("/elsewhere", "main"), 404, "not_found")
	// read-only
	r := httptest.NewRequest(http.MethodPost, ok, nil)
	r.Header.Set("Authorization", "Bearer "+f.keys["main"])
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	if w.Code != 405 || w.Header().Get("Allow") != "GET" {
		t.Error(w.Code)
	}
	// a revoked key reaches the pod with the next manifest
	f.m.Keys[0].RevokedAt = &f.now
	f.s.Apply(f.m)
	f.want(f.get(ok, "main"), 401, "key_revoked")
}

func TestAddresses(t *testing.T) {
	f := newFixture(t)
	f.load()
	from := func(addr string) func(*http.Request) { return func(r *http.Request) { r.RemoteAddr = addr } }
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("203.0.113.9:1")), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("[2001:db8::7]:1")), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "cidr"), 403, "address_not_allowed")
	// X-Forwarded-For only from a trusted proxy
	xff := func(r *http.Request) { r.Header.Set("X-Forwarded-For", "203.0.113.9") }
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("10.0.0.5:1"), xff), 403, "address_not_allowed")
	f.s.clientIP = clientIPFor([]string{"10.0.0.0/8"})
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("10.0.0.5:1"), xff), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("198.51.100.7:1"), xff), 403, "address_not_allowed")
	// a range that does not read lets nothing in
	f.m.Keys[2].CIDRs = []string{"not a range"}
	f.s.Apply(f.m)
	f.want(f.get("/data/crm/customers?id=1", "cidr", from("203.0.113.9:1")), 403, "address_not_allowed")
}

func TestRateLimit(t *testing.T) {
	f := newFixture(t)
	f.load()
	f.want(f.get("/data/crm/customers?id=1", "limited"), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "limited"), 200, "")
	a := f.get("/data/crm/customers?id=1", "limited")
	f.want(a, 429, "rate_limited")
	if a.header.Get("Retry-After") != "30" {
		t.Errorf("%v", a.header)
	}
	f.now = f.now.Add(30 * time.Second)
	f.want(f.get("/data/crm/customers?id=1", "limited"), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "limited"), 429, "rate_limited")
	// other keys are not touched
	f.want(f.get("/data/crm/customers?id=1", "main"), 200, "")
	// the rate is divided by the replicas
	if PerPod(600, 3) != 200 || PerPod(1, 3) != 1 || PerPod(10, 0) != 10 || PerPod(7, 2) != 4 {
		t.Error(PerPod(600, 3), PerPod(1, 3), PerPod(7, 2))
	}
	f.m.Keys[1].RatePerMinute, f.m.Replicas = 4, 2
	f.s.Apply(f.m)
	f.now = f.now.Add(time.Minute)
	f.want(f.get("/data/crm/customers?id=1", "limited"), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "limited"), 200, "")
	f.want(f.get("/data/crm/customers?id=1", "limited"), 429, "rate_limited")
}

// expected is the ids of the rows matching `keep`, by id
func expected(n int64, keep func(row) bool) []int64 {
	out := []int64{}
	for i := int64(1); i <= n; i++ {
		if keep(rowOf(i)) {
			out = append(out, i)
		}
	}
	return out
}

func TestFilters(t *testing.T) {
	f := newFixture(t)
	f.load()
	day := func(s string) time.Time { d, _ := time.Parse("2006-01-02", s); return d }
	at := func(s string) time.Time { d, _ := time.Parse("2006-01-02T15:04:05", s); return d }
	str := func(p *string) string {
		if p == nil {
			return "<nil>"
		}
		return *p
	}
	for _, c := range []struct {
		query string
		keep  func(row) bool
	}{
		{"id=17", func(r row) bool { return r.id == 17 }},
		{"id=%2B17", func(r row) bool { return r.id == 17 }},
		{"id=5000", func(r row) bool { return false }},
		{"id__in=3,9,27,81,243,729,2187", func(r row) bool { return slices.Contains([]int64{3, 9, 27, 81, 243, 729}, r.id) }},
		{"id__gt=990", func(r row) bool { return r.id > 990 }},
		{"id__gte=990&id__lt=995", func(r row) bool { return r.id >= 990 && r.id < 995 }},
		{"id__lte=3", func(r row) bool { return r.id <= 3 }},
		{"id__null=true", func(r row) bool { return false }},
		{"id__null=false&id__lt=4", func(r row) bool { return r.id < 4 }},
		{"region=North&id__lt=50", func(r row) bool { return str(r.region) == "North" && r.id < 50 }},
		{"region__in=North,Centre&id__lt=40", func(r row) bool { return (str(r.region) == "North" || str(r.region) == "Centre") && r.id < 40 }},
		{"region__null=true&id__lt=100", func(r row) bool { return r.region == nil && r.id < 100 }},
		{"region=north", func(r row) bool { return false }},
		{"score__gte=1240&score__lte=1243.75", func(r row) bool { return r.score >= 1240 && r.score <= 1243.75 }},
		{"score__gt=1248.751", func(r row) bool { return r.score > 1248.751 }},
		{"score__lt=3.75", func(r row) bool { return r.score < 3.75 }},
		{"signup=2026-01-05&id__lt=300", func(r row) bool { return r.signup.Equal(day("2026-01-05")) && r.id < 300 }},
		{"signup__gte=2026-04-09&id__lt=300", func(r row) bool { return !r.signup.Before(day("2026-04-09")) && r.id < 300 }},
		{"seen__gt=2026-01-01T23:58:00&seen__lte=2026-01-02T00:03:00", func(r row) bool {
			return r.seen.After(at("2026-01-01T23:58:00")) && !r.seen.After(at("2026-01-02T00:03:00"))
		}},
		{"seen__lte=2026-01-01", func(r row) bool { return false }},
		{"seen__lte=2026-01-01T08:02:00Z", func(r row) bool { return r.id <= 2 }},
		// a zone other than UTC is the same instant: 10:02 in Rome's summer is 08:02 UTC
		{"seen__lte=2026-01-01T10:02:00%2B02:00", func(r row) bool { return r.id <= 2 }},
		{"active=true&id__lt=10", func(r row) bool { return r.active && r.id < 10 }},
		{"active=FALSE&id__lt=10", func(r row) bool { return !r.active && r.id < 10 }},
		{"ratio__lt=0.5", func(r row) bool { return r.ratio < 0.5 }},
		{"bucket=0", func(r row) bool { return r.bucket != nil && *r.bucket == 0 }},
		{"bucket__null=true", func(r row) bool { return r.bucket == nil }},
		{"bucket__gte=12&id__lt=100", func(r row) bool { return r.bucket != nil && *r.bucket >= 12 && r.id < 100 }},
		{"name=name%20314", func(r row) bool { return r.id == 314 }},
		{"name=name%201%27%20OR%201%3D1%20--", func(r row) bool { return false }},
	} {
		want := expected(1000, c.keep)
		a := f.get("/data/crm/customers?"+c.query+"&_limit=100&_count=true", "main")
		if a.status != 200 {
			t.Errorf("%s: %d %s", c.query, a.status, a.body)
			continue
		}
		got := a.ids()
		if len(want) > 100 {
			want = want[:100]
		}
		if !slices.Equal(got, want) {
			t.Errorf("%s: got %v, want %v", c.query, got, want)
		}
		if n := a.json["count"].(float64); int(n) != len(expected(1000, c.keep)) {
			t.Errorf("%s: count %v", c.query, n)
		}
	}
	// the values as they go out
	a := f.get("/data/crm/customers?id=7", "main")
	rec := a.json["data"].([]any)[0].(map[string]any)
	if rec["name"] != "name 7" || rec["region"] != "South" || rec["score"] != 8.75 || rec["signup"] != "2026-01-08" ||
		rec["seen"] != "2026-01-01T08:07:00" || rec["active"] != false || rec["bucket"] != 7.0 || rec[`odd "col"`] != "odd 7" ||
		fmt.Sprint(rec["tags"]) != "[7 8]" || a.json["version"] != "2026-11-07T08:00:00Z" || a.json["next"] != nil {
		t.Errorf("%s", a.body)
	}
	if _, ok := a.json["count"]; ok {
		t.Error("a count not asked for")
	}
	if a := f.get("/data/crm/customers?id=10", "main"); !strings.Contains(string(a.body), `"region":null`) || !strings.Contains(string(a.body), `"score":12.5`) {
		t.Errorf("%s", a.body)
	}
}

func TestWrongRequests(t *testing.T) {
	f := newFixture(t)
	f.load()
	for _, c := range []struct{ query, code, says string }{
		{"id=abc", "bad_value", "«id» is a whole number"},
		{"id=1.5", "bad_value", "«id»"},
		{"id=99999999999999999999", "bad_value", "between"},
		{"score__gte=a", "bad_value", "«score» is a decimal"},
		{"signup__gte=2026-13-01", "bad_value", "«signup» is a date"},
		{"seen__gt=yesterday", "bad_value", "«seen»"},
		{"active=yes", "bad_value", "«active» is true or false"},
		{"ratio__lt=NaN", "bad_value", "«ratio» is a number"},
		{"bucket__null=maybe", "bad_value", "bucket__null"},
		{"id__in=1,,3", "bad_value", "«id»"},
		{"id__in=" + strings.TrimSuffix(strings.Repeat("1,", 501), ","), "too_many_values", "at most 500"},
		{"name__in=a", "operator_not_allowed", "«name» cannot be filtered with «in»"},
		{"region__gte=North", "operator_not_allowed", "«region»"},
		{"tags=1", "not_filterable", "«tags» is not a filter"},
		{"nothing=1", "not_filterable", "the columns one may filter on are id, region"},
		{"id__between=1", "not_filterable", "«id__between»"},
		{"id=1&id=2", "repeated_parameter", "«id» is given 2 times"},
		{"_columns=id,phone", "unknown_column", "«phone»"},
		{"_columns=", "unknown_column", "«»"},
		{"_sort=tags", "not_sortable", "«tags»"},
		{"_sort=", "not_sortable", "names no column"},
		{"_sort=%20", "not_sortable", "names no column"},
		{"_sort=id,-id", "not_sortable", "twice"},
		{"_sort=id,region,score,signup,seen,active", "not_sortable", "at most 5"},
		{"_limit=0", "bad_limit", "from 1 to 100"},
		{"_limit=101", "bad_limit", "from 1 to 100"},
		{"_limit=ten", "bad_limit", ""},
		{"_count=yes", "bad_count", ""},
		{"_format=xml", "bad_format", ""},
		{"_page=2", "unknown_parameter", "«_page»"},
		{"_cursor=%%%", "bad_query", ""},
		{"_cursor=bm9wZQ", "bad_cursor", ""},
		{"_cursor=bm9wZQ&id=1", "cursor_alone", ""},
	} {
		a := f.get("/data/crm/customers?"+c.query, "main")
		if a.status != 400 || a.code() != c.code || !strings.Contains(fmt.Sprint(a.json["detail"]), c.says) {
			t.Errorf("%s: %d %s", c.query, a.status, a.body)
		}
	}
}

func TestColumnsSortAndCSV(t *testing.T) {
	f := newFixture(t)
	f.load()
	a := f.get("/data/crm/customers?id__lt=4&_columns=region,id,region", "main")
	if string(a.body) != `{"data":[{"region":"South","id":1},{"region":"Centre","id":2},{"region":"North","id":3}],"next":null,"version":"2026-11-07T08:00:00Z"}` {
		t.Errorf("%s", a.body)
	}
	// descending, nulls last, then by id
	a = f.get("/data/crm/customers?id__lt=60&_sort=-bucket,id&_columns=id,bucket&_limit=60", "main")
	got := a.ids()
	want := expected(59, func(row) bool { return true })
	sort.SliceStable(want, func(i, j int) bool {
		bi, bj := rowOf(want[i]).bucket, rowOf(want[j]).bucket
		switch {
		case bi == nil || bj == nil:
			return bj == nil && bi != nil
		case *bi != *bj:
			return *bi > *bj
		}
		return want[i] < want[j]
	})
	if !slices.Equal(got, want) {
		t.Errorf("got %v\nwant %v", got, want)
	}
	// the default order
	f.m.Resources[0].DefaultSort = []string{"-id"}
	f.s.Apply(f.m)
	if ids := f.get("/data/crm/customers?_limit=3", "main").ids(); !slices.Equal(ids, []int64{1000, 999, 998}) {
		t.Error(ids)
	}
	if ids := f.get("/data/crm/customers?_limit=3&_sort=id", "main").ids(); !slices.Equal(ids, []int64{1, 2, 3}) {
		t.Error(ids)
	}
	// CSV
	a = f.get("/data/crm/customers?id__lte=3&_columns=id,name,region,score,tags&_sort=id&_format=csv&_limit=2&_count=true", "main")
	if a.status != 200 || a.header.Get("Content-Type") != "text/csv; charset=utf-8" || a.header.Get("X-Total-Count") != "3" ||
		a.header.Get("X-Data-Version") != "2026-11-07T08:00:00Z" || a.header.Get("X-Next-Cursor") == "" {
		t.Fatalf("%d %v %s", a.status, a.header, a.body)
	}
	records, err := csv.NewReader(bytes.NewReader(a.body)).ReadAll()
	if err != nil || len(records) != 3 || strings.Join(records[0], "|") != "id|name|region|score|tags" ||
		strings.Join(records[1], "|") != "1|name 1|South|1.25|[1,2]" {
		t.Errorf("%v %v", records, err)
	}
	next := f.get("/data/crm/customers?_format=csv&_cursor="+a.header.Get("X-Next-Cursor"), "main")
	records, _ = csv.NewReader(bytes.NewReader(next.body)).ReadAll()
	if len(records) != 2 || records[1][0] != "3" || next.header.Get("X-Next-Cursor") != "" {
		t.Errorf("%v %v %s", records, next.header, next.body)
	}
}

// pages walks every page of a query and returns the ids in order
func (f *fixture) pages(query string) ([]int64, []string) {
	f.t.Helper()
	ids := []int64{}
	cursors := []string{}
	a := f.get("/data/crm/customers?"+query, "main")
	for {
		if a.status != 200 {
			f.t.Fatalf("%s: %d %s", query, a.status, a.body)
		}
		ids = append(ids, a.ids()...)
		next, _ := a.json["next"].(string)
		if next == "" {
			return ids, cursors
		}
		cursors = append(cursors, next)
		a = f.get("/data/crm/customers?_cursor="+url.QueryEscape(next), "main")
	}
}

func TestPagesAndCursors(t *testing.T) {
	f := newFixture(t)
	f.load()
	// the file's order: every row once, by id
	ids, cursors := f.pages("_limit=70&_columns=id")
	if !slices.Equal(ids, expected(1000, func(row) bool { return true })) || len(cursors) != 14 {
		t.Fatalf("%d rows, %d pages", len(ids), len(cursors))
	}
	// filtered, and in an order of the request's own with ties and nulls
	ids, _ = f.pages("region__in=North,South&_sort=-bucket,-signup&_limit=33&_count=true")
	want := expected(1000, func(r row) bool { return r.region != nil && (*r.region == "North" || *r.region == "South") })
	sort.SliceStable(want, func(i, j int) bool {
		a, b := rowOf(want[i]), rowOf(want[j])
		switch {
		case (a.bucket == nil) != (b.bucket == nil):
			return b.bucket == nil
		case a.bucket != nil && *a.bucket != *b.bucket:
			return *a.bucket > *b.bucket
		case !a.signup.Equal(b.signup):
			return a.signup.After(b.signup)
		}
		return a.id < b.id
	})
	if !slices.Equal(ids, want) {
		t.Fatalf("got %d rows, want %d; first %v / %v", len(ids), len(want), ids[:min(10, len(ids))], want[:10])
	}
	// every kind of sort column through a cursor
	for _, s := range []string{"score", "-seen", "active,-ratio", "name", "-region,id", "signup,-id"} {
		got, _ := f.pages("_sort=" + s + "&_limit=97&_columns=id")
		sorted := slices.Clone(got)
		slices.Sort(sorted)
		if !slices.Equal(sorted, expected(1000, func(row) bool { return true })) {
			t.Errorf("%s: %d rows, not each once", s, len(got))
		}
	}
	// a cursor carries the query, the count too
	a := f.get("/data/crm/customers?region=North&_limit=5&_count=true&_columns=id", "main")
	b := f.get("/data/crm/customers?_cursor="+a.json["next"].(string), "main")
	if b.json["count"] != a.json["count"] || len(b.ids()) != 5 || b.ids()[0] <= a.ids()[4] {
		t.Errorf("%s\n%s", a.body, b.body)
	}
	// a refresh: the next version is prepared and swapped in; an old cursor is a 409
	writeSource(t, filepath.Join(f.dir, "storage", "v2.parquet"), 1200, 11)
	f.m.Resources[0].URL, f.m.Resources[0].Fingerprint, f.m.Resources[0].Version = f.files.URL+"/v2.parquet", "two", "2026-11-07T10:00:00Z"
	f.s.Apply(f.m)
	// until it is prepared, the old version answers and its cursors hold
	if c := f.get("/data/crm/customers?_cursor="+cursors[0], "main"); c.status != 200 || c.json["version"] != "2026-11-07T08:00:00Z" {
		t.Fatalf("%d %s", c.status, c.body)
	}
	f.s.PrepareAll(context.Background())
	c := f.get("/data/crm/customers?_cursor="+cursors[0], "main")
	f.want(c, 409, "data_changed")
	if d := f.get("/data/crm/customers?_count=true&_limit=1", "main"); d.json["version"] != "2026-11-07T10:00:00Z" || d.json["count"] != 1200.0 {
		t.Errorf("%s", d.body)
	}
	// the old file is gone once nothing reads it
	files, _ := filepath.Glob(filepath.Join(f.dir, "data", "*.duckdb"))
	if len(files) != 2 { // customers v2 and orders
		t.Errorf("%v", files)
	}
	// a change of the columns prepares the same data again: the cursors of the other layout too
	cur := f.get("/data/crm/customers?_limit=1", "main").json["next"].(string)
	f.m.Resources[0].Columns = f.m.Resources[0].Columns[:5]
	f.s.Apply(f.m)
	f.s.PrepareAll(context.Background())
	f.want(f.get("/data/crm/customers?_cursor="+cur, "main"), 409, "data_changed")
	if a := f.get("/data/crm/customers?id=1", "main"); strings.Contains(string(a.body), "tags") || a.status != 200 {
		t.Errorf("%s", a.body)
	}
}

func TestNoSQLFromTheRequest(t *testing.T) {
	f := newFixture(t)
	f.load()
	evil := []string{`1; DROP TABLE serve; --`, `' OR '1'='1`, `1) OR (1=1`, `"; SELECT * FROM duckdb_settings(); --`,
		`x' UNION SELECT 1,2,3 --`, `read_csv('/etc/passwd')`, `\x00`, `0x10`, `1e3`}
	for _, v := range evil {
		e := url.QueryEscape(v)
		for _, q := range []string{"name=" + e, "region__in=Nowhere," + e, "id=" + e, "signup__gte=" + e, "active=" + e,
			"score__lt=" + e, "_columns=" + e, "_sort=" + e, "_limit=" + e, "_count=" + e, "_format=" + e, e + "=1",
			"id__" + e + "=1", "_cursor=" + e} {
			a := f.get("/data/crm/customers?"+q, "main")
			if a.status != 400 && !(a.status == 200 && len(a.ids()) == 0) {
				t.Errorf("%s: %d %s", q, a.status, a.body)
			}
		}
	}
	// a cursor made by hand, with the query and the position tampered with
	for _, raw := range []map[string]string{{"name": "' OR 1=1 --"}, {"_sort": "id; DROP TABLE serve"}, {"_columns": "id\"--"},
		{"_limit": "1000000"}} {
		gen := f.s.tables["customers"].Generation
		a := f.get("/data/crm/customers?_cursor="+EncodeCursor(gen, raw, Position{Row: 1}), "main")
		if a.status != 400 && !(a.status == 200 && len(a.ids()) == 0) {
			t.Errorf("%v: %d %s", raw, a.status, a.body)
		}
	}
	gen := f.s.tables["customers"].Generation
	tampered := EncodeCursor(gen, map[string]string{"_sort": "name"}, Position{Row: 3, Values: []*string{ptr("' OR 1=1 --")}})
	if a := f.get("/data/crm/customers?_cursor="+tampered, "main"); a.status != 200 {
		t.Errorf("%d %s", a.status, a.body)
	}
	bad := EncodeCursor(gen, map[string]string{"_sort": "-id"}, Position{Row: 3, Values: []*string{ptr("1 OR 1=1")}})
	f.want(f.get("/data/crm/customers?_cursor="+bad, "main"), 400, "bad_cursor")
	// the table is whole after all of it
	if a := f.get("/data/crm/customers?_count=true&_limit=1", "main"); a.json["count"] != 1000.0 {
		t.Fatalf("%s", a.body)
	}
	// the serving connection reads nothing but its file
	tbl := f.s.table("customers")
	defer tbl.release()
	if _, err := tbl.db.Exec("CREATE TABLE x (a INTEGER)"); err == nil {
		t.Error("the file is writable")
	}
	if _, err := tbl.db.Query("SELECT * FROM read_csv('/etc/passwd')"); err == nil {
		t.Error("the serving DuckDB reads other files")
	}
}

func ptr(s string) *string { return &s }

func TestStates(t *testing.T) {
	f := newFixture(t)
	f.load()
	f.m.State = "paused"
	f.s.Apply(f.m)
	f.want(f.get("/data/crm/customers?id=1", "main"), 503, "api_paused")
	if f.s.Ready() || len(f.s.tables) != 0 {
		t.Error("still serving")
	}
	f.m.State, f.m.Suspended = "suspended", "creator_rights"
	f.s.Apply(f.m)
	f.want(f.get("/data/crm/customers?id=1", "main"), 503, "api_suspended")
	f.m.State, f.m.Suspended = "active", ""
	f.load()
	// the creator may no longer read the datasource: 403 on it, the others answer
	f.m.Resources[0].Available, f.m.Resources[0].Unavailable, f.m.Resources[0].URL = false, "datasource_rights", ""
	f.s.Apply(f.m)
	f.want(f.get("/data/crm/customers?id=1", "main"), 403, "resource_unavailable")
	f.want(f.get("/data/crm/orders", "main"), 200, "")
	// no data file (never written, or gone from the storage) and none served: said so, not
	// «still loading» for ever
	f.m.Resources[0].Unavailable = "no_data"
	f.s.Apply(f.m)
	a := f.get("/data/crm/customers?id=1", "main")
	f.want(a, 503, "no_data")
	if !strings.Contains(fmt.Sprint(a.json["detail"]), "no data file") {
		t.Errorf("%s", a.body)
	}
	// a gateway out of reach for too long: no more answers
	f.now = f.now.Add(16 * time.Minute)
	f.want(f.get("/data/crm/orders", "main"), 503, "manifest_stale")
	if a := f.get("/data/crm/health", ""); a.json["status"] != "stale" {
		t.Errorf("%s", a.body)
	}
}

func TestUsageAndLogs(t *testing.T) {
	f := newFixture(t)
	f.load()
	f.get("/data/crm/customers?id__lt=11", "main")
	f.get("/data/crm/customers?id=x", "main")
	f.get("/data/crm/customers?id=1", "limited")
	f.get("/data/crm/customers?id=1", "limited")
	f.get("/data/crm/customers?id=1", "limited")
	f.get("/data/crm/customers?id=1", "revoked")
	f.get("/data/crm/customers?id=1", "")
	list := f.s.usage.Take()
	byKey := map[int64]dataapiwire.UsageMinute{}
	for _, m := range list {
		byKey[m.KeyID] = m
	}
	if m := byKey[1]; m.Requests != 2 || m.Rows != 10 || m.Errors != 1 || !m.Minute.Equal(f.now.Truncate(time.Minute)) {
		t.Errorf("%+v", m)
	}
	if m := byKey[2]; m.Requests != 3 || m.Throttled != 1 || m.Rows != 2 {
		t.Errorf("%+v", m)
	}
	if m := byKey[5]; m.Requests != 1 || m.Errors != 1 {
		t.Errorf("%+v", m)
	}
	if len(list) != 3 || len(f.s.usage.Take()) != 0 {
		t.Errorf("%+v", list)
	}
	f.s.usage.Return(list)
	if len(f.s.usage.Take()) != 3 {
		t.Error("not returned")
	}
	// one JSON line per request, never the key nor the query
	lines := 0
	for _, l := range strings.Split(strings.TrimSpace(f.logs.String()), "\n") {
		var v map[string]any
		if err := json.Unmarshal([]byte(l), &v); err != nil {
			t.Fatalf("%q", l)
		}
		if v["msg"] != "request" {
			continue
		}
		lines++
		if lines == 1 && (v["key"] != f.keys["main"][4:12] || v["resource"] != "customers" || v["status"] != 200.0 || v["rows"] != 10.0 ||
			v["path"] != "/data/crm/customers" || v["ms"] == nil || v["client"] != "198.51.100.7") {
			t.Errorf("%s", l)
		}
		if lines == 2 && v["code"] != "bad_value" {
			t.Errorf("%s", l)
		}
	}
	for _, secret := range f.keys {
		if strings.Contains(f.logs.String(), secret) {
			t.Fatal("a key in the logs")
		}
	}
	if lines != 7 || strings.Contains(f.logs.String(), "id__lt") {
		t.Errorf("%d lines: %s", lines, f.logs.String())
	}
}

func TestValueTypes(t *testing.T) {
	for _, c := range []struct {
		typ, in string
		ok      bool
	}{
		{"TINYINT", "127", true}, {"TINYINT", "128", false}, {"UTINYINT", "-1", false}, {"UBIGINT", "18446744073709551615", true},
		{"HUGEINT", "-170141183460469231731687303715884105728", true}, {"INTEGER", "1e3", false}, {"DOUBLE", "1e3", true},
		{"DOUBLE", "Infinity", false}, {"DECIMAL(4,1)", "123.45", true}, {"DECIMAL(4,1)", strings.Repeat("9", 39), false},
		{"BOOLEAN", "True", true}, {"DATE", "2026-02-30", false}, {"TIMESTAMP", "2026-02-03T04:05:06.123456789", true},
		{"TIMESTAMP WITH TIME ZONE", "2026-02-03T04:05:06+02:00", true}, {"TIME", "25:00:00", false}, {"TIME", "08:30", true},
		{"UUID", "0190a4f2-7c1e-7d3a-9b2c-1f2e3d4c5b6a", true}, {"UUID", "nope", false}, {"VARCHAR", "", true},
		{"INTEGER[]", "1", false},
	} {
		_, err := NewColumn("c", c.typ).ParseValue(c.in)
		if (err == nil) != c.ok {
			t.Errorf("%s %q: %v", c.typ, c.in, err)
		}
	}
	// a decimal goes out as a number when a float holds it, as a string when not
	d := NewColumn("d", "DECIMAL(38,10)")
	big1, _ := new(big.Int).SetString("123456789012345678901234567890", 10)
	if v, _ := marshal(d.Plain(mkDecimal(1250, 2))); string(v) != "12.5" {
		t.Error(string(v))
	}
	if v, _ := marshal(d.Plain(mkDecimalBig(big1, 10))); string(v) != `"12345678901234567890.123456789"` {
		t.Error(string(v))
	}
	if v, _ := marshal(NewColumn("f", "DOUBLE").Plain(nan())); string(v) != `"NaN"` {
		t.Error(string(v))
	}
	tz := NewColumn("t", "TIMESTAMP WITH TIME ZONE")
	if v := tz.Plain(time.Date(2026, 1, 2, 3, 4, 5, 0, time.FixedZone("x", 3600))); v != "2026-01-02T02:04:05Z" {
		t.Error(v)
	}
}

// Without an ORDER BY the rows come in the file's order also across DuckDB's row groups and
// with several threads: the pages of the file's order rely on it.
func TestFileOrderAcrossRowGroups(t *testing.T) {
	f := newFixture(t)
	writeSource(t, filepath.Join(f.dir, "storage", "big.parquet"), 400_000, 3)
	f.m.Resources[0].URL, f.m.Resources[0].Fingerprint = f.files.URL+"/big.parquet", "big"
	f.s.cfg.Threads, f.s.cfg.MemoryMB = 4, 1200
	f.load()
	for _, q := range []string{"id__gte=121500&_limit=100&_columns=id", "region=North&id__gte=240000&_limit=100&_columns=id",
		"_limit=100&_columns=id&active=true&id__gte=360000"} {
		a := f.get("/data/crm/customers?"+q, "main")
		var all []int64
		for page := 0; page < 30 && a.status == 200; page++ {
			all = append(all, a.ids()...)
			next, _ := a.json["next"].(string)
			if next == "" {
				break
			}
			a = f.get("/data/crm/customers?_cursor="+next, "main")
		}
		if len(all) < 1000 || !slices.IsSorted(all) || slices.Compact(slices.Clone(all))[len(all)-1] != all[len(all)-1] {
			t.Fatalf("%s: %d rows, sorted %v", q, len(all), slices.IsSorted(all))
		}
		for i := 1; i < len(all); i++ {
			if all[i] == all[i-1] {
				t.Fatalf("%s: %d twice", q, all[i])
			}
		}
	}
}

// A column taken out of a resource is not served a moment longer, while the file with the
// new columns is prepared, or when it cannot be (the data missing, a preparation failing).
func TestColumnTakenOutStopsAtOnce(t *testing.T) {
	f := newFixture(t)
	f.load()
	res := &f.m.Resources[0]
	res.Columns = slices.DeleteFunc(slices.Clone(res.Columns), func(c string) bool { return c == "name" })
	res.Filters = slices.DeleteFunc(slices.Clone(res.Filters), func(x dataapiwire.Filter) bool { return x.Column == "name" })
	res.Available, res.Unavailable, res.URL = false, "no_data", "" // the old file stays the one served
	f.s.Apply(f.m)
	for _, path := range []string{"/data/crm/customers?id=1", "/data/crm/customers?id=1&_format=csv"} {
		a := f.get(path, "main")
		f.want(a, 200, "")
		if bytes.Contains(a.body, []byte(`name`)) {
			t.Errorf("%s: the column taken out is still served: %s", path, a.body)
		}
	}
	f.want(f.get("/data/crm/customers?_columns=id,name", "main"), 400, "unknown_column")
	f.want(f.get("/data/crm/customers?name=x", "main"), 400, "not_filterable")
}

// A page that ends on a NaN (or an infinity) in the sort column: its cursor reads, and the
// pages give every row once (DuckDB orders NaN after every number, before the nulls).
func TestCursorOverNaN(t *testing.T) {
	f := newFixture(t)
	nan := strings.Replace(sourceSQL, "i / 7.0 AS ratio",
		"CASE WHEN i %% 5 = 0 THEN 'NaN'::DOUBLE WHEN i %% 97 = 0 THEN 'Infinity'::DOUBLE ELSE i / 7.0 END AS ratio", 1)
	db, err := sql.Open("duckdb", "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(fmt.Sprintf(nan, 1001, 7, filepath.Join(f.dir, "storage", "v1.parquet"))); err != nil {
		t.Fatal(err)
	}
	db.Close()
	f.load()
	for _, s := range []string{"ratio", "-ratio", "ratio,-id"} {
		got, cursors := f.pages("_sort=" + s + "&_limit=50&_columns=id")
		sorted := slices.Clone(got)
		slices.Sort(sorted)
		if !slices.Equal(sorted, expected(1000, func(row) bool { return true })) || len(cursors) != 19 {
			t.Errorf("%s: %d rows over %d pages, not each once", s, len(got), len(cursors)+1)
		}
	}
}
