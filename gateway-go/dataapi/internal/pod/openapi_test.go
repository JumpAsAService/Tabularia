package pod

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The description validates against OpenAPI 3.1's own schema (testdata: the OpenAPI
// Initiative's published schemas, 2022-10-07, with the dialect its Schema Objects use), and
// the answers validate against the schemas it gives.

func oasCompiler(t *testing.T) *jsonschema.Compiler {
	t.Helper()
	c := jsonschema.NewCompiler()
	for _, name := range []string{"oas-3.1-schema.json", "oas-3.1-schema-base.json", "oas-3.1-dialect-base.json", "oas-3.1-meta-base.json"} {
		raw, err := os.ReadFile("testdata/" + name)
		if err != nil {
			t.Fatal(err)
		}
		doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		if err := c.AddResource(doc.(map[string]any)["$id"].(string), doc); err != nil {
			t.Fatal(err)
		}
	}
	return c
}

func TestOpenAPI(t *testing.T) {
	f := newFixture(t)
	f.want(f.get("/data/crm/openapi.json", "main"), 503, "loading")
	f.load()
	f.want(f.get("/data/crm/openapi.json", ""), 401, "key_missing")
	a := f.get("/data/crm/openapi.json", "main")
	if a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	c := oasCompiler(t)
	sch, err := c.Compile("https://spec.openapis.org/oas/3.1/schema-base/2022-10-07")
	if err != nil {
		t.Fatal(err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(a.body))
	if err != nil {
		t.Fatal(err)
	}
	if err := sch.Validate(doc); err != nil {
		t.Fatalf("not OpenAPI 3.1: %v", err)
	}
	// the validator does refuse what is not OpenAPI
	var broken map[string]any
	_ = json.Unmarshal(a.body, &broken)
	delete(broken, "info")
	broken["paths"].(map[string]any)["/data/crm/orders"].(map[string]any)["get"].(map[string]any)["parameters"] = "none"
	raw, _ := json.Marshal(broken)
	brokenDoc, _ := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if sch.Validate(brokenDoc) == nil {
		t.Fatal("the validator lets a broken description through")
	}
	paths := a.json["paths"].(map[string]any)
	if len(paths) != 2 || paths["/data/crm/customers"] == nil || paths["/data/crm/orders"] == nil {
		t.Fatalf("%v", paths)
	}
	text := string(a.body)
	for _, want := range []string{`"name":"id__in"`, `"name":"region__null"`, `"name":"score__gte"`, `"format":"date"`,
		`"bearer":{"description"`, `"name":"X-API-Key"`, `"enum":["id","-id","region","-region"`} {
		if !strings.Contains(text, want) {
			t.Errorf("no %s", want)
		}
	}
	if strings.Contains(text, f.keys["main"]) {
		t.Fatal("the key in the description")
	}
	// a key of one resource sees that one only
	b := f.get("/data/crm/openapi.json", "orders")
	if p := b.json["paths"].(map[string]any); len(p) != 1 || p["/data/crm/orders"] == nil {
		t.Errorf("%v", p)
	}
	// the answers match the description's schemas
	var root map[string]any
	_ = json.Unmarshal(a.body, &root)
	rc := jsonschema.NewCompiler()
	rd, _ := jsonschema.UnmarshalJSON(bytes.NewReader(a.body))
	if err := rc.AddResource("https://example.test/openapi.json", rd); err != nil {
		t.Fatal(err)
	}
	page, err := rc.Compile("https://example.test/openapi.json#/components/schemas/Page_customers")
	if err != nil {
		t.Fatal(err)
	}
	problem, err := rc.Compile("https://example.test/openapi.json#/components/schemas/Problem")
	if err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{"id__lt=30&_count=true", "_columns=id,tags&_limit=3", "region__null=true&_limit=5"} {
		ans := f.get("/data/crm/customers?"+q, "main")
		v, _ := jsonschema.UnmarshalJSON(bytes.NewReader(ans.body))
		if err := page.Validate(v); err != nil {
			t.Errorf("%s: %v", q, err)
		}
	}
	bad := f.get("/data/crm/customers?id=x", "main")
	v, _ := jsonschema.UnmarshalJSON(bytes.NewReader(bad.body))
	if err := problem.Validate(v); err != nil {
		t.Error(err)
	}
	// a wrong row does not pass: the schema says something
	wrong, _ := jsonschema.UnmarshalJSON(strings.NewReader(`{"data": [{"id": "seven"}], "next": null, "version": "x"}`))
	if page.Validate(wrong) == nil {
		t.Error("a string id passes")
	}
}

// The gateway's Documentation describes a data API with no pod and no key: the same generator
// (dataapiwire.OpenAPI), the columns typed by the datasources' Polars types. For the same
// definition it is the document the pods serve once they have prepared the data.
func TestOpenAPIIsTheGatewaysToo(t *testing.T) {
	f := newFixture(t)
	f.m.DefinitionVersion = 3
	f.load()
	served := f.get("/data/crm/openapi.json", "main")
	if served.status != 200 {
		t.Fatalf("%d %s", served.status, served.body)
	}
	// the types the engine lists for the same parquet columns, written by Polars
	polars := map[string]map[string]string{
		"customers": {"id": "Int64", "name": "String", "region": "String", "score": "Decimal(precision=12, scale=2)",
			"signup": "Date", "seen": "Datetime(time_unit='us', time_zone=None)", "active": "Boolean", "ratio": "Float64",
			"tags": "List(Int64)", "bucket": "Int64", `odd "col"`: "String"},
		"orders": {"order_id": "Int64", "customer": "Int64"},
	}
	kindOf := func(res, col string) (string, bool) {
		typ, ok := polars[res][col]
		return dataapiwire.KindOfPolars(typ), ok
	}
	gateway, err := json.Marshal(dataapiwire.OpenAPI(DocOf(f.m), nil, kindOf, "/"))
	if err != nil {
		t.Fatal(err)
	}
	var a, b any
	_ = json.Unmarshal(served.body, &a)
	_ = json.Unmarshal(gateway, &b)
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("the pod's description is not the gateway's:\npod     %s\ngateway %s", served.body, gateway)
	}
	if v := served.json["info"].(map[string]any)["version"]; v != "3" {
		t.Errorf("the version: %v", v)
	}
}

// A datasource's Polars type and the type DuckDB reads from the parquet Polars wrote (measured
// with Polars 1 and DuckDB 1, 2026-11-07) are the same kind: the gateway types a column as the
// pods will.
func TestPolarsKindsAreThePods(t *testing.T) {
	for _, c := range [][2]string{
		{"Int8", "TINYINT"}, {"Int64", "BIGINT"}, {"UInt8", "UTINYINT"}, {"UInt64", "UBIGINT"}, {"Int128", "BLOB"},
		{"Float32", "FLOAT"}, {"Float64", "DOUBLE"}, {"Decimal(precision=12, scale=2)", "DECIMAL(12,2)"},
		{"Boolean", "BOOLEAN"}, {"String", "VARCHAR"}, {"Categorical", "VARCHAR"}, {"Enum(categories=['x', 'y'])", "VARCHAR"},
		{"Date", "DATE"}, {"Datetime(time_unit='us', time_zone=None)", "TIMESTAMP"},
		{"Datetime(time_unit='ns', time_zone=None)", "TIMESTAMP_NS"}, {"Datetime(time_unit='ms', time_zone=None)", "TIMESTAMP"},
		{"Datetime(time_unit='us', time_zone='Europe/Rome')", "TIMESTAMP WITH TIME ZONE"},
		{"Time", "TIME"}, // TIME_NS in the parquet, made TIME when prepared
		{"Duration(time_unit='us')", "BIGINT"}, {"Binary", "BLOB"}, {"List(Int64)", "BIGINT[]"}, {"Struct({'a': Int64})", "STRUCT(a BIGINT)"},
	} {
		if got, want := dataapiwire.KindOfPolars(c[0]), NewColumn("c", c[1]).Kind; got != want {
			t.Errorf("%s is %s for the gateway, %s (%s) for the pods", c[0], got, c[1], want)
		}
	}
}

// Every error code the pods write is in dataapiwire.ErrorCodes, with its status: what the
// documentation and the description list.
func TestErrorCodesAreCatalogued(t *testing.T) {
	known := map[string]int{}
	for _, e := range dataapiwire.ErrorCodes {
		if _, twice := known[e.Code]; twice {
			t.Errorf("%s twice", e.Code)
		}
		known[e.Code] = e.Status
	}
	files, _ := filepath.Glob("*.go")
	written := regexp.MustCompile(`Status: (\d+), Code: "([a-z_]+)"|badRequest\("([a-z_]+)"`)
	seen := 0
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, _ := os.ReadFile(name)
		for _, m := range written.FindAllStringSubmatch(string(src), -1) {
			code, status := m[2], m[1]
			if code == "" {
				code, status = m[3], "400"
			}
			seen++
			if got, ok := known[code]; !ok || strconv.Itoa(got) != status {
				t.Errorf("%s: %s %s is not catalogued (%d)", name, status, code, got)
			}
		}
	}
	if seen < 30 {
		t.Fatalf("only %d codes found in the sources", seen)
	}
}

// A time Polars wrote (TIME_NS in the parquet) is served as a time, filtered and sorted on.
func TestTimeNanosecondsPrepared(t *testing.T) {
	dir := t.TempDir()
	parquet := filepath.Join(dir, "times.parquet")
	db, err := sql.Open("duckdb", "")
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(fmt.Sprintf(`COPY (SELECT i AS id, CAST(TIME '08:00:00' + INTERVAL (i) MINUTE AS TIME_NS) AS at FROM range(1, 11) t(i))
		TO '%s' (FORMAT parquet)`, parquet))
	db.Close()
	if err != nil {
		t.Skipf("this DuckDB writes no TIME_NS: %v", err)
	}
	res := dataapiwire.Resource{Name: "times", Columns: []string{"id", "at"},
		Filters: []dataapiwire.Filter{{Column: "at", Operators: []string{"gte"}}}, MaxPage: 10}
	p := &Preparer{Dir: dir}
	file := filepath.Join(dir, "times.duckdb")
	if err := p.build(context.Background(), res, parquet, file, Budgeted{PrepareThreads: 1, PrepareMB: 128}); err != nil {
		t.Fatal(err)
	}
	tab, err := Open(file, res, 64, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer tab.close()
	if c, _ := tab.Column("at"); c.Kind != KindTime || c.Type != "TIME" {
		t.Fatalf("%+v", c)
	}
}
