package api

// GET /data-apis/{api_id}/documentation: a data API's documentation for its page, with no key
// and no pod (docs/specs/data-apis.md, «Pieces», 4): each resource with its columns, their kinds,
// the filters and their operators; the errors the pods answer; and the OpenAPI description the
// pods serve, made by the same code (dataapiwire.OpenAPI) from the definition, the columns typed
// by their datasources. VIEW on the folder, as the page.

import (
	"net/http"
	"regexp"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
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
	// the XMLA interface, for pivot tables: only when this data API has it on and the installation
	// offers it (docs/specs/xmla.md)
	Xmla *dataAPIDocXmla `json:"xmla,omitempty"`
}

// dataAPIDocXmla is what the page needs to say how a spreadsheet connects: the endpoint's path
// (after the same origin as `path`), the catalog and each resource's cube as the client will list it.
type dataAPIDocXmla struct {
	Path    string           `json:"path"`    // /data/{slug}/xmla
	Catalog string           `json:"catalog"` // the data API's name
	Cubes   []dataAPIDocCube `json:"cubes"`
}

type dataAPIDocCube struct {
	Name     string                `json:"name"` // the caption, or the resource's name
	Resource string                `json:"resource"`
	Measures []dataAPIDocMeasure   `json:"measures"`
	Dims     []dataAPIDocDimension `json:"dimensions"`
	Skipped  []string              `json:"skipped"` // columns left out of the cube (lists, structs, blobs…)
}

type dataAPIDocMeasure struct {
	Name   string `json:"name"`
	Agg    string `json:"aggregation"` // rows, sum, average, min, max
	Folder string `json:"folder"`      // "" for the row count
	Column string `json:"column"`      // "" for the row count
}

type dataAPIDocDimension struct {
	Name        string                `json:"name"`
	Column      string                `json:"column"`
	Kind        string                `json:"kind"`
	Hierarchies []dataAPIDocHierarchy `json:"hierarchies"`
}

type dataAPIDocHierarchy struct {
	Name   string   `json:"name"`
	Levels []string `json:"levels"` // without the (All) level
}

var polarsScale = regexp.MustCompile(`scale=(\d+)|,\s*(\d+)\)`)
var polarsPrecision = regexp.MustCompile(`precision=(\d+)|\((\d+),`)

// decimalOfPolars reads the precision and scale of a Polars decimal type
// (Decimal(precision=12, scale=2)); 0, 0 when it has none.
func decimalOfPolars(dtype string) (width, scale int) {
	pick := func(re *regexp.Regexp) int {
		m := re.FindStringSubmatch(dtype)
		for _, g := range m[min(1, len(m)):] {
			if g != "" {
				n, _ := strconv.Atoi(g)
				return n
			}
		}
		return 0
	}
	return pick(polarsPrecision), pick(polarsScale)
}

var aggNames = map[xmlamodel.Agg]string{xmlamodel.AggRows: "rows", xmlamodel.AggSum: "sum", xmlamodel.AggAverage: "average",
	xmlamodel.AggMin: "min", xmlamodel.AggMax: "max"}

// xmlaDoc is the XMLA part of a data API's documentation: each resource's cube worked out with
// xmlamodel.Build from its columns and the kinds the pods give them (dataapiwire.KindOfPolars on the
// datasource's type), the same model the pods answer Discover with. A column its datasource no longer
// has is left out, as the pods do before the data is prepared.
func xmlaDoc(a *dataapis.API, def dataapis.Definition, sources map[int64]*dataapis.Datasource) *dataAPIDocXmla {
	out := &dataAPIDocXmla{Path: "/data/" + a.Slug + "/xmla", Catalog: a.Name, Cubes: []dataAPIDocCube{}}
	for _, r := range def.Resources {
		types := map[string]string{}
		if ds := sources[r.Source.ID]; ds != nil {
			for _, c := range ds.Columns {
				types[c.Name] = c.Type
			}
		}
		cols := []xmlamodel.Column{}
		for _, name := range r.Columns {
			typ, ok := types[name]
			if !ok {
				continue
			}
			col := xmlamodel.Column{Name: name, Kind: dataapiwire.KindOfPolars(typ)}
			if col.Kind == dataapiwire.KindDecimal {
				col.Width, col.Scale = decimalOfPolars(typ)
			}
			cols = append(cols, col)
		}
		cube := xmlamodel.Build(r.CubeName(), r.Name, cols)
		co := dataAPIDocCube{Name: cube.Name, Resource: r.Name, Measures: []dataAPIDocMeasure{}, Dims: []dataAPIDocDimension{},
			Skipped: []string{}}
		for _, m := range cube.Measures {
			co.Measures = append(co.Measures, dataAPIDocMeasure{Name: m.Name, Agg: aggNames[m.Agg], Folder: m.Folder, Column: m.Column})
		}
		for _, d := range cube.Dimensions {
			do := dataAPIDocDimension{Name: d.Name, Column: d.Column, Kind: d.Kind, Hierarchies: []dataAPIDocHierarchy{}}
			for _, h := range d.Hierarchies {
				ho := dataAPIDocHierarchy{Name: h.Name, Levels: []string{}}
				for _, l := range h.Levels[1:] {
					ho.Levels = append(ho.Levels, l.Name)
				}
				do.Hierarchies = append(do.Hierarchies, ho)
			}
			co.Dims = append(co.Dims, do)
		}
		for _, c := range cube.Skipped {
			co.Skipped = append(co.Skipped, c.Name)
		}
		out.Cubes = append(out.Cubes, co)
	}
	return out
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
	if def.Xmla.Enabled && xmlaOn(d) {
		out.Xmla = xmlaDoc(a, def, sources)
	}
	w.Header().Set("Cache-Control", "no-store")
	server.JSON(w, http.StatusOK, out)
}
