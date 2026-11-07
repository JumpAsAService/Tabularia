package api

// services/lineage.py: the cross-flow lineage graph among flows, datasources, connections,
// external destinations, saved scripts, SQL scripts, notebooks and flow groups, derived on
// demand and filtered by the folders the person can read; and, in the Go gateway only, the data
// APIs (readers of the datasources they publish).

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

const (
	edgeRead        = "read"
	edgePublish     = "publish"
	edgeIngest      = "ingest"
	edgeWrite       = "write"
	edgeRefresh     = "refresh"
	edgeOrchestrate = "orchestrate"
)

// lgNode is a node's dict: its attributes in the order they were first given.
type lgNode struct{ attrs pyjson.Object }

func (n *lgNode) meta() pyjson.Object { m, _ := n.attrs.Value("meta").(pyjson.Object); return m }
func (n *lgNode) setMeta(k string, v any) {
	n.attrs = n.attrs.Set("meta", n.meta().Set(k, v))
}

type lgEdge struct{ source, target, kind string }

// lgGraph is _Graph.
type lgGraph struct {
	order []string
	nodes map[string]*lgNode
	edges []*lgEdge
	seen  map[lgEdge]bool
}

func newGraph() *lgGraph { return &lgGraph{nodes: map[string]*lgNode{}, seen: map[lgEdge]bool{}} }

// add is add_node: a real node is never overwritten by a restricted placeholder.
func (g *lgGraph) add(id string, attrs pyjson.Object) {
	if n, ok := g.nodes[id]; ok {
		if !pyjson.Truthy(attrs.Value("restricted")) {
			for _, kv := range attrs {
				if kv.Value != nil {
					n.attrs = n.attrs.Set(kv.Key, kv.Value)
				}
			}
		}
		return
	}
	g.nodes[id] = &lgNode{attrs: append(pyjson.Object{{Key: "id", Value: id}}, attrs...)}
	g.order = append(g.order, id)
}

func (g *lgGraph) edge(source, target, kind string) {
	k := lgEdge{source, target, kind}
	if g.seen[k] {
		return
	}
	g.seen[k] = true
	g.edges = append(g.edges, &k)
}

// pyIntKey is a JSON value used as a key of a dict of ints: 5, 5.0 and True find 5, 1.
func pyIntKey(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case float64:
		if x == float64(int64(x)) {
			return int64(x), true
		}
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

// unhashable is a dict or a list used as a key: a TypeError in Python.
func unhashable(v any) bool {
	switch v.(type) {
	case pyjson.Object, []any:
		return true
	}
	return false
}

var errUnhashable = errors.New("lineage: an unhashable reference")

type lgUnknown struct {
	keys   map[string]bool
	values []any
}

func (u *lgUnknown) add(v any) {
	k := pyjson.Str(v)
	if i, ok := pyIntKey(v); ok {
		k = strconv.FormatInt(i, 10)
	}
	if !u.keys[k] {
		u.keys[k] = true
		u.values = append(u.values, v)
	}
}

func lgRestrictedDS(g *lgGraph, id any, unknown *lgUnknown) string {
	unknown.add(id)
	nid := "ds:" + pyjson.Str(id)
	g.add(nid, pyjson.Object{{Key: "type", Value: "datasource"}, {Key: "label", Value: "Datasource #" + pyjson.Str(id)},
		{Key: "restricted", Value: true}, {Key: "kind", Value: nil}, {Key: "meta", Value: pyjson.Object{{Key: "reason", Value: "unknown"}}}})
	return nid
}

func lgRestrictedFlow(g *lgGraph, id any, unknown *lgUnknown) string {
	unknown.add(id)
	nid := "flow:" + pyjson.Str(id)
	g.add(nid, pyjson.Object{{Key: "type", Value: "flow"}, {Key: "label", Value: "Flow #" + pyjson.Str(id)},
		{Key: "restricted", Value: true}, {Key: "meta", Value: pyjson.Object{{Key: "reason", Value: "unknown"}}}})
	return nid
}

// lgAddImage is _add_image.
func lgAddImage(g *lgGraph, data pyjson.Object, runner string) {
	repository := strings.TrimSpace(pyjson.Str(pyjson.Or(data.Value("repository"), "")))
	if repository == "" {
		return
	}
	node := "image:" + pyjson.Str(data.Value("registryId")) + "|" + repository
	tag := strings.TrimSpace(pyjson.Str(pyjson.Or(data.Value("tag"), "")))
	if tag == "" {
		tag = "latest"
	}
	g.add(node, pyjson.Object{{Key: "type", Value: "container"}, {Key: "label", Value: repository},
		{Key: "meta", Value: pyjson.Object{{Key: "registry_id", Value: data.Value("registryId")}, {Key: "tag", Value: tag}}}})
	g.edge(runner, node, edgeOrchestrate)
}

type lgDS struct {
	id, project    int64
	name, kind     string
	rows           *int64
	refreshed      *time.Time
	flowID, connID *int64
}

type lgConn struct {
	id, project    int64
	name, dbType   string
	database, host *string
}

type lgFlow struct {
	id, project  int64
	name, engine string
	schedule     *string
	definition   string
}

// lgDict is `x or {}` read as a dict: nil for a falsy one, an error for a truthy non-dict (an
// AttributeError on .get in Python).
func lgDict(v any) (pyjson.Object, error) {
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, errors.New("lineage: not a dict")
	}
	return o, nil
}

// lgList is `x or []` iterated: a list's items, a dict's keys, a text's characters.
func lgList(v any) ([]any, error) {
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	switch x := v.(type) {
	case []any:
		return x, nil
	case pyjson.Object:
		out := make([]any, len(x))
		for i, kv := range x {
			out[i] = kv.Key
		}
		return out, nil
	case string:
		var out []any
		for _, c := range x {
			out = append(out, string(c))
		}
		return out, nil
	}
	return nil, errors.New("lineage: not iterable")
}

func lgStripText(v any) string {
	s, _ := pyjson.Or(v, "").(string)
	return strings.TrimSpace(s)
}

// buildLineage is build_full_graph: the whole graph over the objects in the folders this
// person can read.
func buildLineage(ctx context.Context, q perm.Querier, c *perm.Checker, userID int64, sql *aisvc.Client) (*lgGraph, error) {
	g := newGraph()
	readableSet, err := c.Readable(ctx)
	if err != nil || len(readableSet) == 0 {
		return g, err
	}
	readable := perm.Sorted(readableSet)

	var flows []lgFlow
	rows, err := q.Query(ctx, `SELECT id, name, project_id, engine, run_schedule, coalesce(definition, '') FROM flows WHERE project_id = ANY($1)`, readable)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var f lgFlow
		if err := rows.Scan(&f.id, &f.name, &f.project, &f.engine, &f.schedule, &f.definition); err != nil {
			rows.Close()
			return nil, err
		}
		flows = append(flows, f)
	}
	rows.Close()
	var datasources []lgDS
	rows, err = q.Query(ctx, `SELECT id, name, project_id, kind, rows, refreshed_at, flow_id, connection_id FROM datasources
		WHERE project_id = ANY($1)`, readable)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var d lgDS
		if err := rows.Scan(&d.id, &d.name, &d.project, &d.kind, &d.rows, &d.refreshed, &d.flowID, &d.connID); err != nil {
			rows.Close()
			return nil, err
		}
		datasources = append(datasources, d)
	}
	rows.Close()
	var connections []lgConn
	rows, err = q.Query(ctx, `SELECT id, name, project_id, db_type, database, host FROM connections WHERE project_id = ANY($1)`, readable)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var x lgConn
		if err := rows.Scan(&x.id, &x.name, &x.project, &x.dbType, &x.database, &x.host); err != nil {
			rows.Close()
			return nil, err
		}
		connections = append(connections, x)
	}
	rows.Close()

	flowIDs := map[int64]bool{}
	var flowList []int64
	for _, f := range flows {
		flowIDs[f.id] = true
		flowList = append(flowList, f.id)
	}
	dsByID := map[int64]*lgDS{}
	for i := range datasources {
		dsByID[datasources[i].id] = &datasources[i]
	}
	connByID := map[int64]*lgConn{}
	for i := range connections {
		connByID[connections[i].id] = &connections[i]
	}

	for _, x := range connections {
		g.add(fmt.Sprintf("conn:%d", x.id), pyjson.Object{{Key: "type", Value: "connection"}, {Key: "label", Value: x.name},
			{Key: "project_id", Value: x.project}, {Key: "meta", Value: pyjson.Object{{Key: "db_type", Value: x.dbType}, {Key: "database", Value: x.database}}}})
	}
	for _, d := range datasources {
		g.add(fmt.Sprintf("ds:%d", d.id), pyjson.Object{{Key: "type", Value: "datasource"}, {Key: "label", Value: d.name},
			{Key: "project_id", Value: d.project}, {Key: "kind", Value: d.kind},
			{Key: "meta", Value: pyjson.Object{{Key: "rows", Value: d.rows}, {Key: "refreshed_at", Value: naiveIso(d.refreshed)}}}})
	}
	type lastRun struct {
		status string
		at     time.Time
	}
	last := map[int64]lastRun{}
	lastSuccess := map[int64]time.Time{}
	if len(flowList) > 0 {
		rows, err = q.Query(ctx, `SELECT r.flow_id, r.status, r.started_at FROM runs r WHERE r.id IN (
			SELECT (SELECT n.id FROM runs n WHERE n.flow_id = f.id AND n.kind = 'flow' ORDER BY n.started_at DESC LIMIT 1)
			FROM flows f WHERE f.id = ANY($1))`, flowList)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			var l lastRun
			if err := rows.Scan(&id, &l.status, &l.at); err != nil {
				rows.Close()
				return nil, err
			}
			last[id] = l
		}
		rows.Close()
		rows, err = q.Query(ctx, `SELECT flow_id, max(coalesce(finished_at, started_at)) FROM runs WHERE flow_id = ANY($1)
			AND kind = 'flow' AND status = 'SUCCESS' GROUP BY flow_id`, flowList)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			var at time.Time
			if err := rows.Scan(&id, &at); err != nil {
				rows.Close()
				return nil, err
			}
			lastSuccess[id] = at
		}
		rows.Close()
	}
	for _, f := range flows {
		lr, has := last[f.id]
		var at, status any
		if has {
			at, status = naiveIso(&lr.at), lr.status
		}
		g.add(fmt.Sprintf("flow:%d", f.id), pyjson.Object{{Key: "type", Value: "flow"}, {Key: "label", Value: f.name},
			{Key: "project_id", Value: f.project}, {Key: "meta", Value: pyjson.Object{{Key: "engine", Value: f.engine},
				{Key: "scheduled", Value: f.schedule != nil && *f.schedule != ""}, {Key: "last_run_at", Value: at},
				{Key: "last_run_status", Value: status}, {Key: "never_run", Value: !has}}}})
	}
	for _, d := range datasources {
		if d.flowID != nil && *d.flowID != 0 && flowIDs[*d.flowID] {
			g.edge(fmt.Sprintf("flow:%d", *d.flowID), fmt.Sprintf("ds:%d", d.id), edgePublish)
		}
		if d.connID != nil && *d.connID != 0 && connByID[*d.connID] != nil {
			g.edge(fmt.Sprintf("conn:%d", *d.connID), fmt.Sprintf("ds:%d", d.id), edgeIngest)
		}
	}

	unknownDS := &lgUnknown{keys: map[string]bool{}}
	unknownFlow := &lgUnknown{keys: map[string]bool{}}
	dsRef := func(v any) (string, error) {
		if unhashable(v) {
			return "", errUnhashable
		}
		if i, ok := pyIntKey(v); ok && dsByID[i] != nil {
			return "ds:" + pyjson.Str(v), nil
		}
		return lgRestrictedDS(g, v, unknownDS), nil
	}
	flowRef := func(v any) (string, error) {
		if unhashable(v) {
			return "", errUnhashable
		}
		if i, ok := pyIntKey(v); ok && flowIDs[i] {
			return "flow:" + pyjson.Str(v), nil
		}
		return lgRestrictedFlow(g, v, unknownFlow), nil
	}
	connOf := func(v any) (*lgConn, error) {
		if unhashable(v) {
			return nil, errUnhashable
		}
		if i, ok := pyIntKey(v); ok {
			return connByID[i], nil
		}
		return nil, nil
	}
	for _, f := range flows {
		text := f.definition
		if text == "" {
			text = "{}"
		}
		parsed, err := pyjson.Loads(text)
		if err != nil {
			continue // json.JSONDecodeError: logged and left out
		}
		def, ok := parsed.(pyjson.Object)
		if !ok {
			return nil, errors.New("lineage: a definition that is not a dict")
		}
		nodes, err := lgList(def.Value("nodes"))
		if err != nil {
			return nil, err
		}
		me := fmt.Sprintf("flow:%d", f.id)
		for _, nv := range nodes {
			node, ok := nv.(pyjson.Object)
			if !ok {
				return nil, errors.New("lineage: a node that is not a dict")
			}
			d, err := lgDict(node.Value("data"))
			if err != nil {
				return nil, err
			}
			switch node.Value("type") {
			case "source", "refresh":
				id := d.Value("datasourceId")
				if id == nil {
					continue
				}
				target, err := dsRef(id)
				if err != nil {
					return nil, err
				}
				if node.Value("type") == "source" {
					g.edge(target, me, edgeRead)
				} else {
					g.edge(me, target, edgeRefresh)
				}
			case "runflow":
				id := d.Value("flowId")
				if id == nil {
					continue
				}
				target, err := flowRef(id)
				if err != nil {
					return nil, err
				}
				g.edge(me, target, edgeOrchestrate)
			case "output":
				dest := pyjson.Or(d.Value("destType"), "datasource")
				switch dest {
				case "database":
					connID := d.Value("connectionId")
					table := lgStripText(d.Value("table"))
					if table == "" {
						table = "(table?)"
					}
					sink := "dbsink:" + pyjson.Str(connID) + "|" + table
					conn, err := connOf(connID)
					if err != nil {
						return nil, err
					}
					var name, dbType any
					if conn != nil {
						name, dbType = conn.name, conn.dbType
					}
					g.add(sink, pyjson.Object{{Key: "type", Value: "db_sink"}, {Key: "label", Value: table}, {Key: "restricted", Value: conn == nil},
						{Key: "meta", Value: pyjson.Object{{Key: "connection_id", Value: connID}, {Key: "connection_name", Value: name},
							{Key: "db_type", Value: dbType}, {Key: "mode", Value: pyjson.Or(d.Value("mode"), "append")}}}})
					g.edge(me, sink, edgeWrite)
				case "s3":
					bucket := lgStripText(d.Value("s3Bucket"))
					key := lgStripText(d.Value("s3Key"))
					if key == "" {
						key = "(key?)"
					}
					sink := "s3sink:" + bucket + "|" + key
					parts, err := lgList(d.Value("partitionBy"))
					if err != nil {
						return nil, err
					}
					g.add(sink, pyjson.Object{{Key: "type", Value: "s3_sink"}, {Key: "label", Value: key},
						{Key: "meta", Value: pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "key", Value: key},
							{Key: "format", Value: pyjson.Or(d.Value("s3Format"), "parquet")}, {Key: "partitions", Value: int64(len(parts))}}}})
					g.edge(me, sink, edgeWrite)
				case "sftp":
					connID := d.Value("connectionId")
					folder := lgStripText(d.Value("sftpFolder"))
					name := lgStripText(d.Value("sftpFileName"))
					if name == "" {
						name = "(file?)"
					}
					sink := "sftpsink:" + pyjson.Str(connID) + "|" + folder + "|" + name
					conn, err := connOf(connID)
					if err != nil {
						return nil, err
					}
					var cname, host any
					if conn != nil {
						cname = conn.name
						if conn.host != nil {
							host = *conn.host
						}
					}
					g.add(sink, pyjson.Object{{Key: "type", Value: "sftp_sink"}, {Key: "label", Value: name}, {Key: "restricted", Value: conn == nil},
						{Key: "meta", Value: pyjson.Object{{Key: "connection_id", Value: connID}, {Key: "connection_name", Value: cname},
							{Key: "host", Value: host}, {Key: "folder", Value: folder}, {Key: "format", Value: pyjson.Or(d.Value("sftpFormat"), "csv")}}}})
					g.edge(me, sink, edgeWrite)
				}
			}
		}
	}

	// the datasource with a name in a folder: the first one, in the query's order
	type at struct {
		project int64
		name    string
	}
	dsAt := map[at]*lgDS{}
	for i := range datasources {
		k := at{datasources[i].project, datasources[i].name}
		if dsAt[k] == nil {
			dsAt[k] = &datasources[i]
		}
	}
	dsAtOf := func(out pyjson.Object) *lgDS {
		p, ok := pyIntKey(out.Value("project_id"))
		name, isStr := out.Value("name").(string)
		if !ok || !isStr {
			return nil
		}
		if _, isBool := out.Value("project_id").(bool); isBool {
			return nil
		}
		return dsAt[at{p, name}]
	}

	// saved scripts
	scriptIDs := map[int64]bool{}
	if scriptcfg.Get().DisabledReason() == "" {
		scripts, err := savedscripts.Visible(ctx, c, q, userID, nil)
		if err != nil {
			return nil, err
		}
		contents, err := lgSavedContents(ctx, q, scripts)
		if err != nil {
			return nil, err
		}
		for _, x := range scripts {
			scriptIDs[x.ID] = true
			sid := fmt.Sprintf("script:%d", x.ID)
			g.add(sid, pyjson.Object{{Key: "type", Value: "script"}, {Key: "label", Value: x.Name}, {Key: "project_id", Value: x.ProjectID},
				{Key: "meta", Value: pyjson.Object{{Key: "scheduled", Value: x.RunSchedule != nil && *x.RunSchedule != ""},
					{Key: "version", Value: x.CurrentVersion}}}})
			content := contents[x.ID]
			inputs, err := lgList(content.Value("inputs"))
			if err != nil {
				return nil, err
			}
			for _, item := range inputs {
				io, ok := item.(pyjson.Object)
				if !ok {
					return nil, errors.New("lineage: an input that is not a dict")
				}
				id := io.Value("datasource_id")
				if _, isInt := id.(int64); !isInt {
					if _, isBool := id.(bool); !isBool {
						continue
					}
				}
				source, err := dsRef(id)
				if err != nil {
					return nil, err
				}
				g.edge(source, sid, edgeRead)
			}
			out, err := lgDict(content.Value("output"))
			if err != nil {
				return nil, err
			}
			if pyjson.Truthy(out.Value("publish")) {
				if published := dsAtOf(out); published != nil {
					g.edge(sid, fmt.Sprintf("ds:%d", published.id), edgePublish)
				}
			}
		}
	}
	if len(scriptIDs) > 0 {
		for _, f := range flows {
			if !strings.Contains(f.definition, `"scriptId"`) {
				continue
			}
			for _, node := range scriptnodes.ScriptNodes(f.definition) {
				if sid, ok := scriptnodes.SavedScriptID(node); ok && scriptIDs[sid] {
					g.edge(fmt.Sprintf("flow:%d", f.id), fmt.Sprintf("script:%d", sid), edgeOrchestrate)
				}
			}
		}
	}

	// SQL scripts
	queries, err := sqlscripts.Visible(ctx, c, q, nil)
	if err != nil {
		return nil, err
	}
	sqlIDs := map[int64]bool{}
	contents, err := sqlscripts.CurrentContents(ctx, q, queries)
	if err != nil {
		return nil, err
	}
	byName := map[string][]*lgDS{}
	var folders map[int64]string
	if len(queries) > 0 {
		for i := range datasources {
			k := strings.ToLower(datasources[i].name)
			byName[k] = append(byName[k], &datasources[i])
		}
		if folders, err = lgFolderNames(ctx, q); err != nil {
			return nil, err
		}
	}
	for _, x := range queries {
		sqlIDs[x.ID] = true
		content := contents[x.ID]
		qid := fmt.Sprintf("sql:%d", x.ID)
		g.add(qid, pyjson.Object{{Key: "type", Value: "sql_script"}, {Key: "label", Value: x.Name}, {Key: "project_id", Value: x.ProjectID},
			{Key: "meta", Value: pyjson.Object{{Key: "scheduled", Value: x.RunSchedule != nil && *x.RunSchedule != ""},
				{Key: "version", Value: x.CurrentVersion}, {Key: "engine", Value: content.Value("engine")}}}})
		if cid := content.Value("connection_id"); cid != nil {
			conn, err := connOf(cid)
			if err != nil {
				return nil, err
			}
			if conn != nil {
				g.edge(fmt.Sprintf("conn:%d", conn.id), qid, edgeRead)
			}
		} else {
			text, _ := pyjson.Or(content.Value("sql"), "").(string)
			engine, _ := content.Value("engine").(string)
			found, err := lgNamedDatasources(ctx, sql, text, engine, byName, folders)
			if err != nil {
				return nil, err
			}
			for _, ds := range found {
				g.edge(fmt.Sprintf("ds:%d", ds.id), qid, edgeRead)
			}
		}
		if out, ok := content.Value("output").(pyjson.Object); ok {
			if written := dsAtOf(out); written != nil {
				g.edge(qid, fmt.Sprintf("ds:%d", written.id), edgePublish)
			}
		}
	}

	// notebooks: the datasources they have read
	where, args, ok, err := savedscripts.VisibleFilter(ctx, c, q, userID, nil)
	if err != nil {
		return nil, err
	}
	if ok {
		type nb struct {
			id, project, version int64
			name                 string
		}
		var nbs []nb
		rows, err := q.Query(ctx, `SELECT id, name, project_id, current_version FROM notebooks`+where+` ORDER BY name`, args...)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var n nb
			if err := rows.Scan(&n.id, &n.name, &n.project, &n.version); err != nil {
				rows.Close()
				return nil, err
			}
			nbs = append(nbs, n)
		}
		rows.Close()
		reads := map[int64][]int64{}
		if len(nbs) > 0 {
			ids := make([]int64, len(nbs))
			for i, n := range nbs {
				ids[i] = n.id
			}
			rows, err := q.Query(ctx, `SELECT notebook_id, datasource_id FROM notebook_reads WHERE notebook_id = ANY($1) ORDER BY id`, ids)
			if err != nil {
				return nil, err
			}
			seen := map[[2]int64]bool{}
			for rows.Next() {
				var n, d int64
				if err := rows.Scan(&n, &d); err != nil {
					rows.Close()
					return nil, err
				}
				if !seen[[2]int64{n, d}] {
					seen[[2]int64{n, d}] = true
					reads[n] = append(reads[n], d)
				}
			}
			rows.Close()
		}
		for _, n := range nbs {
			nid := fmt.Sprintf("notebook:%d", n.id)
			g.add(nid, pyjson.Object{{Key: "type", Value: "notebook"}, {Key: "label", Value: n.name}, {Key: "project_id", Value: n.project},
				{Key: "meta", Value: pyjson.Object{{Key: "version", Value: n.version}}}})
			for _, d := range reads[n.id] {
				source, _ := dsRef(d)
				g.edge(source, nid, edgeRead)
			}
		}
	}

	// data APIs: readers of the datasources they publish (docs/specs/data-apis.md)
	rows, err = q.Query(ctx, `SELECT a.id, a.name, a.project_id, a.slug, a.state, a.current_version, r.datasource_id
		FROM data_apis a LEFT JOIN data_api_resources r ON r.data_api_id = a.id AND r.datasource_id IS NOT NULL
		WHERE a.project_id = ANY($1) ORDER BY a.name, a.id, r.position`, readable)
	if err != nil {
		return nil, err
	}
	type dapi struct {
		id, project, version int64
		name, slug, state    string
		reads                []int64
	}
	var dapis []*dapi
	for rows.Next() {
		var x dapi
		var ds *int64
		if err := rows.Scan(&x.id, &x.name, &x.project, &x.slug, &x.state, &x.version, &ds); err != nil {
			rows.Close()
			return nil, err
		}
		if n := len(dapis); n == 0 || dapis[n-1].id != x.id {
			dapis = append(dapis, &x)
		}
		if ds != nil {
			last := dapis[len(dapis)-1]
			last.reads = append(last.reads, *ds)
		}
	}
	rows.Close()
	for _, x := range dapis {
		aid := fmt.Sprintf("dataapi:%d", x.id)
		g.add(aid, pyjson.Object{{Key: "type", Value: "data_api"}, {Key: "label", Value: x.name}, {Key: "project_id", Value: x.project},
			{Key: "meta", Value: pyjson.Object{{Key: "path", Value: "/data/" + x.slug + "/"}, {Key: "state", Value: x.state},
				{Key: "version", Value: x.version}, {Key: "resources", Value: int64(len(x.reads))}}}})
		for _, d := range x.reads {
			source, _ := dsRef(d)
			g.edge(source, aid, edgeRead)
		}
	}

	// flow groups
	rows, err = q.Query(ctx, `SELECT id, name, project_id, coalesce(definition, ''), run_schedule FROM flow_groups WHERE project_id = ANY($1)`, readable)
	if err != nil {
		return nil, err
	}
	type grp struct {
		id, project      int64
		name, definition string
		schedule         *string
	}
	var groups []grp
	for rows.Next() {
		var x grp
		if err := rows.Scan(&x.id, &x.name, &x.project, &x.definition, &x.schedule); err != nil {
			rows.Close()
			return nil, err
		}
		groups = append(groups, x)
	}
	rows.Close()
	for _, x := range groups {
		var cells []*pyd.ModelValue
		steps, _ := flowgroups.Parse(x.definition).Get("steps").([]any)
		for _, row := range steps {
			list, _ := row.([]any)
			for _, cv := range list {
				if cm, ok := cv.(*pyd.ModelValue); ok {
					cells = append(cells, cm)
				}
			}
		}
		gid := fmt.Sprintf("group:%d", x.id)
		g.add(gid, pyjson.Object{{Key: "type", Value: "flow_group"}, {Key: "label", Value: x.name}, {Key: "project_id", Value: x.project},
			{Key: "meta", Value: pyjson.Object{{Key: "scheduled", Value: x.schedule != nil && *x.schedule != ""}, {Key: "cells", Value: int64(len(cells))}}}})
		for _, cm := range cells {
			id, _ := cm.Get("id").(int64)
			switch cm.Get("kind") {
			case "flow":
				target, _ := flowRef(id)
				g.edge(gid, target, edgeOrchestrate)
			case "refresh":
				target, _ := dsRef(id)
				g.edge(gid, target, edgeRefresh)
			case "script":
				if scriptIDs[id] {
					g.edge(gid, fmt.Sprintf("script:%d", id), edgeOrchestrate)
				}
			case "sql":
				if sqlIDs[id] {
					g.edge(gid, fmt.Sprintf("sql:%d", id), edgeOrchestrate)
				}
			case "container":
				data, _ := scFromPyd(cm.Get("container")).(pyjson.Object)
				lgAddImage(g, data, gid)
			case "pushmail":
				pm, ok := cm.Get("pushmail").(*pyd.ModelValue)
				if !ok {
					continue
				}
				mail := fmt.Sprintf("mail:%d|%d", x.id, id)
				connID := pm.Get("connectionId")
				var cname any
				if i, ok := connID.(int64); ok && connByID[i] != nil {
					cname = connByID[i].name
				}
				label := strings.TrimSpace(pm.Get("label").(string))
				if label == "" {
					label = "Pushmail"
				}
				recipients := 0
				for _, k := range []string{"to", "cc"} {
					for _, a := range pm.Get(k).([]any) {
						if strings.TrimSpace(a.(string)) != "" {
							recipients++
						}
					}
				}
				attachments, _ := pm.Get("attachments").([]any)
				reports, _ := pm.Get("reports").([]any)
				dashboards, _ := pm.Get("dashboards").([]any)
				g.add(mail, pyjson.Object{{Key: "type", Value: "email_sink"}, {Key: "label", Value: label},
					{Key: "meta", Value: pyjson.Object{{Key: "connection_id", Value: connID}, {Key: "connection_name", Value: cname},
						{Key: "recipients", Value: int64(recipients)}, {Key: "attachments", Value: int64(len(attachments))},
						{Key: "reports", Value: int64(len(reports))}, {Key: "dashboards", Value: int64(len(dashboards))}}}})
				g.edge(gid, mail, edgeWrite)
				for _, av := range attachments {
					a := av.(*pyd.ModelValue)
					dsID, ok := a.Get("datasourceId").(int64)
					if !ok {
						continue
					}
					source, _ := dsRef(dsID)
					g.edge(source, mail, edgeRead)
				}
			}
		}
	}

	// the images the flows' Container nodes run
	for _, f := range flows {
		if !strings.Contains(f.definition, `"container"`) {
			continue
		}
		for _, node := range flowaccess.ContainerNodes(f.definition) {
			data, err := lgDict(node.Value("data"))
			if err != nil {
				return nil, err
			}
			lgAddImage(g, data, fmt.Sprintf("flow:%d", f.id))
		}
	}

	// removed vs not accessible
	for _, u := range []struct {
		set    *lgUnknown
		table  string
		prefix string
	}{{unknownDS, "datasources", "ds:"}, {unknownFlow, "flows", "flow:"}} {
		if len(u.set.values) == 0 {
			continue
		}
		var ids []int64
		for _, v := range u.set.values {
			switch x := v.(type) {
			case int64:
				ids = append(ids, x)
			case float64:
				ids = append(ids, int64(x)) // compared as numbers
			case string:
				n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64)
				if err != nil {
					return nil, fmt.Errorf("lineage: %q is not an integer for PostgreSQL", x)
				}
				ids = append(ids, n)
			default:
				return nil, errors.New("lineage: a reference PostgreSQL cannot compare")
			}
		}
		existing := map[int64]bool{}
		rows, err := q.Query(ctx, `SELECT id FROM `+u.table+` WHERE id = ANY($1)`, ids)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return nil, err
			}
			existing[id] = true
		}
		rows.Close()
		for i, v := range u.set.values {
			reason := "removed"
			if existing[ids[i]] {
				reason = "inaccessible"
			}
			g.nodes[u.prefix+pyjson.Str(v)].setMeta("reason", reason)
		}
	}

	// direct staleness: a flow reads a datasource refreshed after its last success
	for _, e := range g.edges {
		if e.kind != edgeRead || !strings.HasPrefix(e.source, "ds:") || !strings.HasPrefix(e.target, "flow:") {
			continue
		}
		dsID, err := pyIntText(strings.SplitN(e.source, ":", 3)[1])
		if err != nil {
			return nil, err
		}
		ds := dsByID[dsID]
		if ds == nil || ds.refreshed == nil {
			continue
		}
		fID, err := pyIntText(strings.SplitN(e.target, ":", 3)[1])
		if err != nil {
			return nil, err
		}
		succ, ok := lastSuccess[fID]
		if ok && ds.refreshed.After(succ) {
			if node := g.nodes[e.target]; node != nil {
				node.setMeta("stale", true)
				node.setMeta("stale_reason", "source")
			}
		}
	}
	// cascading staleness, along the data
	down := map[string][]string{}
	for _, e := range g.edges {
		if e.kind == edgeRead || e.kind == edgePublish || e.kind == edgeWrite {
			down[e.source] = append(down[e.source], e.target)
		}
	}
	var queue []string
	visited := map[string]bool{}
	for _, id := range g.order {
		if pyjson.Truthy(g.nodes[id].meta().Value("stale")) {
			queue = append(queue, id)
			visited[id] = true
		}
	}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		for _, nxt := range down[cur] {
			node := g.nodes[nxt]
			if node == nil {
				continue
			}
			if !pyjson.Truthy(node.meta().Value("stale")) {
				node.setMeta("stale", true)
				node.setMeta("stale_reason", "upstream")
			}
			if !visited[nxt] {
				visited[nxt] = true
				queue = append(queue, nxt)
			}
		}
	}
	return g, nil
}

// pyIntText is int(text): Python's ValueError (a 500) where it is not an integer.
func pyIntText(s string) (int64, error) {
	return strconv.ParseInt(strings.ReplaceAll(strings.TrimSpace(s), "_", ""), 10, 64)
}

// lgSavedContents is current_contents of saved scripts: each one's current version over
// the empty content, read at once.
func lgSavedContents(ctx context.Context, q perm.Querier, scripts []savedscripts.Script) (map[int64]pyjson.Object, error) {
	out := map[int64]pyjson.Object{}
	if len(scripts) == 0 {
		return out, nil
	}
	ids := make([]int64, len(scripts))
	for i, s := range scripts {
		ids[i] = s.ID
	}
	rows, err := q.Query(ctx, `SELECT v.script_id, v.content FROM saved_script_versions v JOIN saved_scripts s
		ON s.id = v.script_id AND s.current_version = v.version WHERE s.id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	raw := map[int64]*string{}
	for rows.Next() {
		var id int64
		var content string
		if err := rows.Scan(&id, &content); err != nil {
			rows.Close()
			return nil, err
		}
		raw[id] = &content
	}
	rows.Close()
	for _, s := range scripts {
		c, err := savedscripts.Over(savedscripts.EmptyContent(), raw[s.ID])
		if err != nil {
			return nil, err
		}
		out[s.ID] = c
	}
	return out, nil
}

// lgFolderNames is sql_scripts.folder_names: every folder's own name.
func lgFolderNames(ctx context.Context, q perm.Querier) (map[int64]string, error) {
	rows, err := q.Query(ctx, `SELECT id, name FROM projects`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]string{}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			return nil, err
		}
		out[id] = name
	}
	return out, rows.Err()
}

// lgNamedDatasources is sql_scripts.named_datasources: the datasources a SQL names, as
// resolve finds them but without refusing anything (sqlglot's reading asked of the AI service).
func lgNamedDatasources(ctx context.Context, svc *aisvc.Client, text, engine string, byName map[string][]*lgDS,
	folders map[int64]string) ([]*lgDS, error) {
	sql := strings.TrimSpace(strings.TrimRight(strings.TrimSpace(sqlscripts.StripComments(text)), ";"))
	if sql == "" {
		return nil, nil
	}
	parsed, err := svc.Tables(ctx, sql, sqlscripts.DialectOf(engine))
	if err != nil {
		return nil, err
	}
	if parsed.Error != nil || parsed.Statements != 1 {
		return nil, nil
	}
	ctes := map[string]bool{}
	for _, c := range parsed.CTEs {
		ctes[c] = true
	}
	type tbl struct{ name, folder string }
	var tables []tbl
	for _, t := range parsed.Tables {
		if t.Catalog {
			continue
		}
		if t.Folder == "" && ctes[strings.ToLower(t.Name)] {
			continue
		}
		k := tbl{t.Name, t.Folder}
		dup := false
		for _, x := range tables {
			if x == k {
				dup = true
			}
		}
		if !dup {
			tables = append(tables, k)
		}
	}
	var out []*lgDS
	seen := map[int64]bool{}
	for _, t := range tables {
		matches := byName[strings.ToLower(t.name)]
		if t.folder != "" {
			var in []*lgDS
			for _, d := range matches {
				if strings.ToLower(folders[d.project]) == strings.ToLower(t.folder) {
					in = append(in, d)
				}
			}
			matches = in
		}
		if len(matches) > 1 {
			var exact []*lgDS
			for _, d := range matches {
				if d.name == t.name {
					exact = append(exact, d)
				}
			}
			matches = exact
		}
		if len(matches) == 1 && !seen[matches[0].id] {
			seen[matches[0].id] = true
			out = append(out, matches[0])
		}
	}
	return out, nil
}
