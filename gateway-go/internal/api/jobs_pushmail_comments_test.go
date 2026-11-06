package api

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// a test key, not a secret of anything
const commentsKey = "Vd3m7oIM2w3o2r6Y-ZbM6O1t3Qk3c7HnS0WBu6Mxx2E="

func TestTheCellsCommentsAreAskedOfTheAIService(t *testing.T) {
	var mu sync.Mutex
	asked := map[string]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		v, _ := pyjson.Loads(string(raw))
		title := pyjson.Str(pyjson.Obj(v).Value("title"))
		mu.Lock()
		asked[title] = string(raw)
		mu.Unlock()
		if title == "broken" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"comments": {"ai": {"state": "no_provider", "b": 1, "a": 2}}, "reading": "abc"}`))
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	q := &queue{d: &Deps{SQL: aisvc.New(u, commentsKey), Log: slog.New(slog.NewTextHandler(io.Discard, nil))}}

	commented := pyjson.Object{{Key: "sections", Value: pyjson.Object{{Key: "body", Value: pyjson.Object{{Key: "items", Value: []any{
		pyjson.Object{{Key: "id", Value: "ai"}, {Key: "type", Value: "comment"}}}}}}}}}
	plain := pyjson.Object{{Key: "v", Value: int64(1)}}
	doc := func(name string, def pyjson.Object, format, inBody any) renderedDoc {
		return renderedDoc{item: &versioned.Item{ID: 7, Name: name}, definition: def, format: format, inBody: inBody,
			spec: pyjson.Object{{Key: "name", Value: name}, {Key: "comments", Value: pyjson.Object{}}, {Key: "reading", Value: nil}}}
	}
	rendered := []renderedDoc{doc("pdf", commented, "pdf", true), doc("excel", commented, "xlsx", false),
		doc("excel in body", commented, "xlsx", true), doc("no comment", plain, "pdf", false), doc("broken", commented, "pdf", false)}
	boards := []renderedDoc{doc("board", commented, "png", nil)}
	boards[0].content = pyjson.Object{{Key: "tiles", Value: []any{}}}
	q.cellComments(context.Background(), &auth.User{ID: 3}, rendered, boards)

	said := func(d renderedDoc) string { s, _ := pyjson.Dumps(d.spec, pyjson.Options{Compact: true}); return s }
	want := `{"name":"pdf","comments":{"ai":{"state":"no_provider","b":1,"a":2}},"reading":"abc"}`
	if got := said(rendered[0]); got != want {
		t.Errorf("a commented report: %s", got)
	}
	for _, i := range []int{1, 3, 4} {
		if got := said(rendered[i]); !strings.HasSuffix(got, `"comments":{},"reading":null}`) {
			t.Errorf("%s: %s", rendered[i].item.Name, got)
		}
	}
	if _, ok := asked["excel"]; ok {
		t.Error("an Excel file without a summary in the body asks nothing")
	}
	if _, ok := asked["no comment"]; ok {
		t.Error("a definition without a comment asks nothing")
	}
	if _, ok := asked["excel in body"]; !ok {
		t.Error("an Excel file with its summary in the body asks")
	}
	if got := asked["board"]; !strings.Contains(got, `"where":["dashboard",7]`) || !strings.Contains(got, `"of_report":false`) ||
		!strings.Contains(got, `"words_of":{"tiles":[]}`) || !strings.Contains(got, `"user_id":3`) || !strings.Contains(got, `"given":{}`) {
		t.Errorf("the dashboard's request: %s", got)
	}
	if got := asked["pdf"]; !strings.Contains(got, `"where":["report",7]`) || !strings.Contains(got, `"of_report":true`) ||
		!strings.Contains(got, `"words_of":null`) {
		t.Errorf("the report's request: %s", got)
	}
}
