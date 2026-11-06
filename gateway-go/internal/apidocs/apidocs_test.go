package apidocs

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestTheSchemaFilledIn(t *testing.T) {
	d := New(`Tab "x"`, "1.3.0", "/api/")
	rec := httptest.NewRecorder()
	d.OpenAPI(rec, nil)
	var schema map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &schema); err != nil {
		t.Fatal(err)
	}
	info := schema["info"].(map[string]any)
	if info["title"] != `Tab "x" — Gateway` || info["version"] != "1.3.0" {
		t.Fatal(info)
	}
	if servers, _ := schema["servers"].([]any); len(servers) != 1 || servers[0].(map[string]any)["url"] != "/api" {
		t.Fatal(schema["servers"])
	}
	if _, ok := New("T", "1", "").schemaFor(); ok {
		t.Fatal("servers without a root path")
	}
}

func (d *Docs) schemaFor() (any, bool) {
	var schema map[string]any
	_ = json.Unmarshal(d.schema, &schema)
	v, ok := schema["servers"]
	return v, ok
}

func TestThePages(t *testing.T) {
	d := New("Tabularia", "1", "")
	rec := httptest.NewRecorder()
	d.Swagger(rec, nil)
	body := rec.Body.String()
	if !strings.Contains(body, `url: "/openapi.json"`) || !strings.Contains(rec.Header().Get("Content-Security-Policy"), "'sha256-") {
		t.Fatal(body)
	}
	rec = httptest.NewRecorder()
	d.Redoc(rec, nil)
	if !strings.Contains(rec.Body.String(), `<redoc spec-url="/openapi.json">`) ||
		!strings.Contains(rec.Header().Get("Content-Security-Policy"), "script-src https://cdn.jsdelivr.net/npm/;") {
		t.Fatal(rec.Body.String(), rec.Header())
	}
}
