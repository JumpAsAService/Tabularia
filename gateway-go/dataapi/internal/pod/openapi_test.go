package pod

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
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
