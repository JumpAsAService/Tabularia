package api

// GET /data-apis/{api_id}/documentation: a data API's documentation for its page, with no key
// and no pod (docs/specs/data-apis.md, «Pieces», 4): each resource with its columns, their kinds,
// the filters and their operators; the errors the pods answer; and the OpenAPI description the
// pods serve, made by the same code (dataapiwire.OpenAPI) from the definition, the columns typed
// by their datasources. VIEW on the folder, as the page.

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("data_apis_docs", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodGet, Pattern: "/data-apis/{api_id}/documentation", Handler: server.Authed(d.Auth,
			"/data-apis/{api_id}/documentation", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { dataAPIDocumentation(d, w, r, p) })}}
	})
}

type dataAPIDocColumn struct {
	Name string `json:"name"`
	// the kind the pods give it (integer, float, decimal, boolean, string, date, timestamp,
	// timestamptz, time, uuid, other); "" when its datasource no longer has it
	Kind string `json:"kind"`
}

type dataAPIDocResource struct {
	Name        string               `json:"name"`
	Path        string               `json:"path"`
	Columns     []dataAPIDocColumn   `json:"columns"`
	Filters     []dataapiwire.Filter `json:"filters"`
	DefaultSort []string             `json:"default_sort"`
	MaxPage     int                  `json:"max_page"`
}

type dataAPIDocOut struct {
	State string `json:"state"`
	// the address the other systems call (DATA_API__PUBLIC_BASE_URL), "" for the interface's own
	BaseURL string `json:"base_url"`
	// the host the Ingress publishes it on, "" for the app's own
	Host        string                  `json:"host"`
	Path        string                  `json:"path"`
	Version     int64                   `json:"version"`
	Resources   []dataAPIDocResource    `json:"resources"`
	MaxInValues int                     `json:"max_in_values"`
	MaxSortKeys int                     `json:"max_sort_keys"`
	Errors      []dataapiwire.ErrorCode `json:"errors"`
	OpenAPI     map[string]any          `json:"openapi"`
}

func dataAPIDocumentation(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIVisible(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	def, err := dataapis.CurrentDefinition(ctx, d.DB, a)
	if err != nil {
		fail(d, w, "data apis: the definition", err)
		return
	}
	ids := []int64{}
	for _, res := range def.Resources {
		ids = append(ids, res.Source.ID)
	}
	sources, err := dataapis.Datasources(ctx, d.DB, ids)
	if err != nil {
		fail(d, w, "data apis: the datasources", err)
		return
	}
	doc, kindOf := dataapis.Doc(a, def, sources)
	pods := d.Config.DataAPI.Pods
	base := pods.PublicBaseURL
	if base == "" {
		base = "/"
	}
	out := dataAPIDocOut{State: a.State, BaseURL: pods.PublicBaseURL, Host: dataapis.IngressHost(pods), Path: "/data/" + a.Slug + "/",
		Version: a.CurrentVersion, Resources: []dataAPIDocResource{}, MaxInValues: dataapiwire.MaxInValues,
		MaxSortKeys: dataapiwire.MaxSortKeys, Errors: dataapiwire.ErrorCodes, OpenAPI: dataapiwire.OpenAPI(doc, nil, kindOf, base)}
	for _, res := range doc.Resources {
		ro := dataAPIDocResource{Name: res.Name, Path: out.Path + res.Name, Columns: []dataAPIDocColumn{}, Filters: res.Filters,
			DefaultSort: res.DefaultSort, MaxPage: res.MaxPage}
		for _, c := range res.Columns {
			kind, _ := kindOf(res.Name, c)
			ro.Columns = append(ro.Columns, dataAPIDocColumn{Name: c, Kind: kind})
		}
		out.Resources = append(out.Resources, ro)
	}
	w.Header().Set("Cache-Control", "no-store")
	server.JSON(w, http.StatusOK, out)
}
