package pyd_test

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
)

// A route such as `POST /projects/{project_id}/banners?limit=` with a BannerCreate
// body: the body is read before the authentication (a broken JSON is a 422 even
// for whoever is not signed in), then path, query and body are validated in
// FastAPI's order and the errors answered together.
func Example() {
	handler := func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		body, early := pyd.ReadBody(raw, r.Header.Get("Content-Type"))
		if early != nil {
			early.Write(w) // 422 json_invalid, or 400
			return
		}
		// ... authentication and permissions here, as FastAPI's dependencies ...
		var p pyd.Params
		projectID := p.Path("project_id", r.PathValue("project_id"), pyd.Int())
		limit := p.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(500), 50)
		banner := p.Body(body, gatewaymodels.BannerCreate)
		if resp := p.Response(); resp != nil {
			resp.Write(w)
			return
		}
		fmt.Fprintf(w, "project %d, limit %d, %q at level %q", projectID.(int64), limit.(int64),
			banner.Get("message"), banner.Get("level"))
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /projects/{project_id}/banners", handler)

	for _, req := range []struct{ url, body string }{
		{"/projects/7/banners", `{"message":"maintenance tonight"}`},
		{"/projects/x/banners?limit=0", `{"message":"","level":"error"}`},
		{"/projects/7/banners", `{"message": }`},
	} {
		rec := httptest.NewRecorder()
		r := httptest.NewRequest("POST", req.url, strings.NewReader(req.body))
		r.Header.Set("Content-Type", "application/json")
		mux.ServeHTTP(rec, r)
		fmt.Println(rec.Code, rec.Body.String())
	}
	// Output:
	// 200 project 7, limit 50, "maintenance tonight" at level "warning"
	// 422 {"detail":[{"type":"int_parsing","loc":["path","project_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"x"},{"type":"greater_than_equal","loc":["query","limit"],"msg":"Input should be greater than or equal to 1","input":"0","ctx":{"ge":1}},{"type":"string_too_short","loc":["body","message"],"msg":"String should have at least 1 character","input":"","ctx":{"min_length":1}},{"type":"literal_error","loc":["body","level"],"msg":"Input should be 'info', 'warning' or 'danger'","input":"error","ctx":{"expected":"'info', 'warning' or 'danger'"}}]}
	// 422 {"detail":[{"type":"json_invalid","loc":["body",12],"msg":"JSON decode error","input":{},"ctx":{"error":"Expecting value"}}]}
}
