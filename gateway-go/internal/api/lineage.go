package api

// routes/lineage.py: the cross-flow lineage graph (lineage_graph.go) and a flow's lineage in
// the OpenLineage standard (openlineage.go).

import (
	"net/http"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/openlineage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("lineage", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/lineage", Handler: server.Authed(d.Auth, "/lineage", getLineage(d))},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/openlineage",
				Handler: server.Authed(d.Auth, "/flows/{flow_id}/openlineage", flowOpenLineage(d, false))},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/openlineage",
				Handler: server.Authed(d.Auth, "/flows/{flow_id}/openlineage", flowOpenLineage(d, true))},
		}
	})
}

var lineageCenter = map[string]string{"flow": "flow", "datasource": "ds", "connection": "conn", "script": "script", "sql": "sql",
	"notebook": "notebook", "group": "group"}

// subgraph is lineage.subgraph: what is reachable from `center` within `depth` hops, following
// the edges upstream, downstream or both. Its nodes come from a set in Python: no order.
func subgraph(g *lgGraph, center, direction string, depth int64) *lgGraph {
	out := newGraph()
	if g.nodes[center] == nil {
		return out
	}
	downstream := map[string][]*lgEdge{}
	upstream := map[string][]*lgEdge{}
	for _, e := range g.edges {
		downstream[e.source] = append(downstream[e.source], e)
		upstream[e.target] = append(upstream[e.target], e)
	}
	keep := map[string]bool{center: true}
	var keepEdges []*lgEdge
	type item struct {
		id     string
		budget int64
	}
	queue := []item{{center, depth}}
	visited := map[item]bool{}
	goDown := direction == "both" || direction == "downstream"
	goUp := direction == "both" || direction == "upstream"
	for len(queue) > 0 {
		it := queue[0]
		queue = queue[1:]
		if it.budget <= 0 || visited[it] {
			continue
		}
		visited[it] = true
		if goDown {
			for _, e := range downstream[it.id] {
				keep[e.target] = true
				keepEdges = append(keepEdges, e)
				queue = append(queue, item{e.target, it.budget - 1})
			}
		}
		if goUp {
			for _, e := range upstream[it.id] {
				keep[e.source] = true
				keepEdges = append(keepEdges, e)
				queue = append(queue, item{e.source, it.budget - 1})
			}
		}
	}
	for _, id := range g.order {
		if keep[id] {
			out.nodes[id] = g.nodes[id]
			out.order = append(out.order, id)
		}
	}
	for _, e := range keepEdges {
		if out.seen[*e] || out.nodes[e.source] == nil || out.nodes[e.target] == nil {
			continue
		}
		out.seen[*e] = true
		out.edges = append(out.edges, e)
	}
	return out
}

// lineageJSON is _serialize: LineageGraph.
func lineageJSON(g *lgGraph, center any) pyjson.Object {
	nodes := []any{}
	for _, id := range g.order {
		a := g.nodes[id].attrs
		meta := a.Value("meta")
		if meta == nil {
			meta = pyjson.Object{}
		}
		nodes = append(nodes, pyjson.Object{{Key: "id", Value: id}, {Key: "type", Value: a.Value("type")},
			{Key: "label", Value: a.Value("label")}, {Key: "project_id", Value: a.Value("project_id")}, {Key: "kind", Value: a.Value("kind")},
			{Key: "restricted", Value: pyjson.Truthy(a.Value("restricted"))}, {Key: "meta", Value: meta}})
	}
	edges := []any{}
	for _, e := range g.edges {
		edges = append(edges, pyjson.Object{{Key: "source", Value: e.source}, {Key: "target", Value: e.target}, {Key: "kind", Value: e.kind}})
	}
	return pyjson.Object{{Key: "nodes", Value: nodes}, {Key: "edges", Value: edges}, {Key: "center", Value: center}}
}

// getLineage is GET /lineage (get_lineage): the whole graph over the readable objects, or the
// subgraph around one within `depth` hops.
func getLineage(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		q := pyd.ParseQuery(r.URL.RawQuery)
		kind := ps.Query(q, "type", pyd.Optional(pyd.Literal("flow", "datasource", "connection", "script", "sql", "notebook", "group")), nil)
		id := ps.Query(q, "id", pyd.Optional(pyd.Int()), nil)
		direction, _ := ps.Query(q, "direction", pyd.Literal("both", "upstream", "downstream"), "both").(string)
		depth, _ := ps.Query(q, "depth", pyd.Int().Ge(1).Le(8), int64(3)).(int64)
		if !validated(w, &ps) {
			return
		}
		full, err := buildLineage(r.Context(), d.DB, perm.For(d.DB, &p.User), p.User.ID, d.SQL)
		if err != nil {
			fail(d, w, "lineage: the graph", err)
			return
		}
		if kind == nil || id == nil {
			writePy(w, lineageJSON(full, nil))
			return
		}
		center := lineageCenter[kind.(string)] + ":" + pyIntString(id)
		if full.nodes[center] == nil {
			detail(w, http.StatusNotFound, "Object not found or not accessible")
			return
		}
		writePy(w, lineageJSON(subgraph(full, center, direction, depth), center))
	}
}

// pyIntString is str() of a validated int (any size).
func pyIntString(v any) string {
	if i, ok := v.(int64); ok {
		return strconv.FormatInt(i, 10)
	}
	return pyjson.Str(v)
}

// flowOpenLineage is GET /flows/{flow_id}/openlineage (flow_openlineage): the flow's lineage as
// an OpenLineage JobEvent, from its definition; with `emit`, POST (emit_flow_openlineage): sent
// to the configured collector.
func flowOpenLineage(d *Deps, emit bool) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "flow_id")
		if !ok {
			return
		}
		q := d.req(w, r, p)
		f := q.flow(ids[0])
		if f == nil || !q.ensure(f.ProjectID, perm.View) {
			return
		}
		s := openlineage.FromEnv()
		if d.Lineage != nil {
			s = d.Lineage.Settings() // the panel's timeout (openlineage._get_client)
		}
		if emit && !s.Enabled() {
			detail(w, http.StatusConflict, "OpenLineage is not configured (OPENLINEAGE__URL or OPENLINEAGE__FILE)")
			return
		}
		b := &openlineage.Builder{S: s, Q: d.DB}
		events, err := b.JobEventsForFlow(q.ctx, &openlineage.Flow{ID: f.ID, ProjectID: f.ProjectID, Name: f.Name,
			Description: f.Description, Definition: f.Definition})
		if err != nil && !emit {
			q.fail(err)
			return
		}
		if !emit {
			list := []any{}
			for _, e := range events {
				list = append(list, e)
			}
			writePy(w, list)
			return
		}
		n := 0
		if err == nil {
			if d.Lineage != nil {
				n, err = d.Lineage.EmitNow(q.ctx, events) // the process's client (_get_client)
			} else {
				n, err = s.Emit(q.ctx, events)
			}
		}
		if err != nil {
			detail(w, http.StatusBadGateway, "The OpenLineage collector rejected the events: "+runeClip(err.Error(), 300))
			return
		}
		writePyStatus(w, http.StatusAccepted, pyjson.Object{{Key: "events", Value: int64(n)}})
	}
}
