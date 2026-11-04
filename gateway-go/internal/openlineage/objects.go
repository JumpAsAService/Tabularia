package openlineage

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// The kinds of OBJECT_KINDS, and the flows' and datasources' own.
const (
	KindFlow        = "flow"
	KindDatasource  = "datasource"
	KindSQLScript   = "sql_script"
	KindSavedScript = "saved_script"
	KindNotebook    = "notebook"
	KindFlowGroup   = "flow_group"
)

// objectKinds is OBJECT_KINDS: the table and the word in the job's name, in their order.
var objectKinds = []struct{ kind, table, word string }{
	{KindSQLScript, "sql_scripts", "SQL script"}, {KindSavedScript, "saved_scripts", "script"},
	{KindNotebook, "notebooks", "notebook"}, {KindFlowGroup, "flow_groups", "flow group"},
}

func kindWord(kind string) string {
	for _, k := range objectKinds {
		if k.kind == kind {
			return k.word
		}
	}
	return ""
}

func kindTable(kind string) string {
	switch kind {
	case KindFlow:
		return "flows"
	case KindDatasource:
		return "datasources"
	}
	for _, k := range objectKinds {
		if k.kind == kind {
			return k.table
		}
	}
	return ""
}

// Object is a SQL script, a saved script, a notebook or a flow group: what its events read.
type Object struct {
	Kind              string
	ID, ProjectID     int64
	Name, Description string
	CurrentVersion    int64
	Definition        string // a flow group's
}

// Group is a flow group.
type Group struct{ *Object }

// GetObject is session.get(<the kind's model>, id).
func (b *Builder) GetObject(ctx context.Context, kind string, id int64) (*Object, error) {
	o := Object{Kind: kind}
	var err error
	switch kind {
	case KindFlowGroup:
		err = b.Q.QueryRow(ctx, `SELECT id, project_id, name, description, definition FROM flow_groups WHERE id = $1`, id).
			Scan(&o.ID, &o.ProjectID, &o.Name, &o.Description, &o.Definition)
	case KindNotebook:
		err = b.Q.QueryRow(ctx, `SELECT id, project_id, name, description, current_version FROM notebooks WHERE id = $1`, id).
			Scan(&o.ID, &o.ProjectID, &o.Name, &o.Description, &o.CurrentVersion)
	default:
		err = b.Q.QueryRow(ctx, fmt.Sprintf(`SELECT id, project_id, name, description, current_version FROM %s WHERE id = $1`, kindTable(kind)), id).
			Scan(&o.ID, &o.ProjectID, &o.Name, &o.Description, &o.CurrentVersion)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &o, nil
}

// GetGroup is session.get(FlowGroup, id).
func (b *Builder) GetGroup(ctx context.Context, id int64) (*Group, error) {
	o, err := b.GetObject(ctx, KindFlowGroup, id)
	if o == nil {
		return nil, err
	}
	return &Group{o}, err
}

// JobNameFor is job_name_for of a SQL script, a saved script, a notebook or a flow group.
func (b *Builder) JobNameFor(ctx context.Context, kind string, o *Object) (string, error) {
	path, err := b.ProjectPath(ctx, &o.ProjectID)
	name := o.Name
	if path != "" {
		name = path + "/" + o.Name
	}
	return name + " (" + kindWord(kind) + ")", err
}

// JobNameForRefresh is job_name_for_refresh.
func (b *Builder) JobNameForRefresh(ctx context.Context, ds *Datasource) (string, error) {
	path, err := b.ProjectPath(ctx, &ds.ProjectID)
	if path == "" {
		return ds.Name + " (refresh)", err
	}
	return path + "/" + ds.Name + " (refresh)", err
}

// objectJob is _object_job.
func (b *Builder) objectJob(ctx context.Context, kind string, o *Object, jobType string, extra pyjson.Object) (Job, error) {
	facets := pyjson.Object{{Key: "jobType", Value: JobType(jobType)}}
	if o.Description != "" {
		facets = append(facets, pyjson.KV{Key: "documentation", Value: Documentation(o.Description)})
	}
	for _, kv := range extra {
		facets = facets.Set(kv.Key, kv.Value)
	}
	name, err := b.JobNameFor(ctx, kind, o)
	return Job{Name: name, Facets: facets}, err
}

func pushmailDatasetName(groupPath, groupName, cell string) string {
	var parts []string
	for _, p := range []string{groupPath, groupName, cell} {
		if p != "" {
			parts = append(parts, p)
		}
	}
	return "/" + strings.Join(parts, "/")
}

// datasourceIDs is `[i.get("datasource_id") for i in declared if isinstance(i, dict)]`.
func datasourceIDs(v any) ([]any, error) {
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	list, ok := v.([]any)
	if !ok {
		return nil, errNotADict
	}
	var out []any
	for _, i := range list {
		if o, ok := i.(pyjson.Object); ok {
			out = append(out, o.Value("datasource_id"))
		}
	}
	return out, nil
}

// datasourceInputs is _datasource_inputs: the datasources with these ids, a removed one left out.
func (b *Builder) datasourceInputs(ctx context.Context, ids []any) ([]Dataset, error) {
	var out []Dataset
	for _, i := range ids {
		id, ok := isIntNotBool(i)
		if !ok {
			continue
		}
		ds, err := b.GetDatasource(ctx, id)
		if err != nil {
			return nil, err
		}
		if ds != nil {
			x, err := b.DatasourceDataset(ctx, ds, false, nil)
			if err != nil {
				return nil, err
			}
			out = append(out, x)
		}
	}
	return Unique(out), nil
}

// intendedOutput is _intended_output: the datasource an object writes under `name` in its folder.
func (b *Builder) intendedOutput(ctx context.Context, projectID int64, name string) (Dataset, error) {
	ds, err := b.findDatasource(ctx, projectID, pyd.Strip(name))
	if err != nil {
		return Dataset{}, err
	}
	if ds != nil {
		return b.DatasourceDataset(ctx, ds, true, nil)
	}
	path, err := b.ProjectPath(ctx, &projectID)
	return Dataset{Namespace: "tabularia://" + b.S.Namespace, Output: true,
		Name: strings.ReplaceAll("/"+path+"/"+pyd.Strip(name), "//", "/")}, err
}

// SQLScriptLineage is sql_script_lineage: (inputs, outputs, job facets) of a SQL script; for a
// write, from what the run recorded when it started.
func (b *Builder) SQLScriptLineage(ctx context.Context, o *Object, r *Run) ([]Dataset, []Dataset, pyjson.Object, error) {
	info := pyjson.Object{}
	if r != nil {
		info = jsonOf(r.Script)
	}
	var version *int64
	switch v := info.Value("version").(type) {
	case nil:
	case int64:
		version = &v
	default:
		return nil, nil, nil, errNotADict
	}
	content, err := sqlscripts.ContentOf(ctx, b.Q, &sqlscripts.Script{ID: o.ID, CurrentVersion: o.CurrentVersion}, version)
	if err != nil {
		return nil, nil, nil, err
	}
	sql := text(content.Value("sql"))
	connectionID, has := info.Get("connection_id")
	if !has {
		connectionID = content.Value("connection_id")
	}
	var inputs []Dataset
	var dialect any
	if connectionID != nil {
		var conn *Connection
		if id, ok := idOf(connectionID); ok {
			if conn, err = b.GetConnection(ctx, id); err != nil {
				return nil, nil, nil, err
			}
		}
		if conn != nil {
			if inputs, err = b.tablesInSQL(ctx, conn, sql); err != nil {
				return nil, nil, nil, err
			}
			if d, ok := schemes[conn.DBType]; ok {
				dialect = d
			}
		}
	} else {
		if ids, ok := info.Get("datasources"); ok {
			list, isList := ids.([]any)
			if !isList && ids != nil {
				return nil, nil, nil, errNotADict
			}
			if inputs, err = b.datasourceInputs(ctx, list); err != nil {
				return nil, nil, nil, err
			}
		} else {
			named, err := b.namedDatasources(ctx, sql, text(content.Value("engine")))
			if err != nil {
				return nil, nil, nil, err
			}
			for _, ds := range named {
				x, err := b.DatasourceDataset(ctx, ds, false, nil)
				if err != nil {
					return nil, nil, nil, err
				}
				inputs = append(inputs, x)
			}
			inputs = Unique(inputs)
		}
		engine := ""
		if r != nil {
			engine = str(r.Engine)
		}
		if engine == "" {
			engine = text(content.Value("engine"))
		}
		if engine == "" {
			engine = "polars"
		}
		dialect = "postgres"
		if d, ok := sqlscripts.Dialects[engine]; ok {
			dialect = d
		}
	}
	facets := pyjson.Object{}
	if strings.TrimSpace(sql) != "" {
		facets = append(facets, pyjson.KV{Key: "sql", Value: sqlFacet(sql, dialect)})
	}
	outputs := []Dataset{}
	switch {
	case r != nil && r.DatasourceID != nil:
		ds, err := b.GetDatasource(ctx, *r.DatasourceID)
		if err != nil {
			return nil, nil, nil, err
		}
		if ds != nil {
			x, err := b.DatasourceDataset(ctx, ds, true, r.RowsWritten)
			if err != nil {
				return nil, nil, nil, err
			}
			outputs = append(outputs, x)
		}
	case r != nil:
		if str(r.PublishName) != "" && r.PublishProjectID != nil {
			x, err := b.intendedOutput(ctx, *r.PublishProjectID, *r.PublishName)
			if err != nil {
				return nil, nil, nil, err
			}
			outputs = append(outputs, x)
		}
	default:
		if out, ok := content.Value("output").(pyjson.Object); ok && pyjson.Truthy(out.Value("name")) {
			if pid, ok := isInt(out.Value("project_id")); ok {
				x, err := b.intendedOutput(ctx, pid, pyjson.Str(out.Value("name")))
				if err != nil {
					return nil, nil, nil, err
				}
				outputs = append(outputs, x)
			}
		}
	}
	if inputs == nil {
		inputs = []Dataset{}
	}
	return inputs, outputs, facets, nil
}

func sqlFacet(query string, dialect any) pyjson.Object {
	fields := []pyjson.KV{{Key: "query", Value: query}}
	if dialect != nil {
		fields = append(fields, pyjson.KV{Key: "dialect", Value: dialect})
	}
	return facet(std("1-1-0", "SQLJobFacet"), fields...)
}

// tablesInSQL is _tables_in_sql: the tables a query reads, with the connection's standard names.
func (b *Builder) tablesInSQL(ctx context.Context, c *Connection, sql string) ([]Dataset, error) {
	q, err := b.SQL.QualifiedTables(ctx, sql, c.DBType)
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var names []string
	for _, t := range q {
		n := TableDatasetName(c, t)
		if !seen[n] {
			seen[n] = true
			names = append(names, n)
		}
	}
	sort.Strings(names)
	out := []Dataset{}
	for _, n := range names {
		out = append(out, TableDataset(c, n, false, nil, "")) // named again, as table_dataset does
	}
	return out, nil
}

// namedDatasources is sql_scripts.named_datasources over every datasource: the datasources a SQL
// names, as resolve finds them but without refusing anything.
func (b *Builder) namedDatasources(ctx context.Context, sqlText, engine string) ([]*Datasource, error) {
	sql := strings.TrimSpace(strings.TrimRight(strings.TrimSpace(sqlscripts.StripComments(sqlText)), ";"))
	sql = strings.TrimSpace(sql)
	if sql == "" {
		return nil, nil
	}
	if engine == "" {
		engine = "polars"
	}
	dialect := "postgres"
	if d, ok := sqlscripts.Dialects[engine]; ok {
		dialect = d
	}
	parsed, err := b.SQL.Tables(ctx, sql, dialect)
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
	withFolder := false
	for _, t := range parsed.Tables {
		if t.Catalog || (t.Folder == "" && ctes[strings.ToLower(t.Name)]) {
			continue
		}
		k := tbl{t.Name, t.Folder}
		dup := false
		for _, x := range tables {
			dup = dup || x == k
		}
		if !dup {
			tables = append(tables, k)
			withFolder = withFolder || t.Folder != ""
		}
	}
	if len(tables) == 0 {
		return nil, nil
	}
	rows, err := b.Q.Query(ctx, `SELECT `+dsColumns+` FROM datasources`)
	if err != nil {
		return nil, err
	}
	byName := map[string][]*Datasource{}
	for rows.Next() {
		d, err := scanDS(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		k := strings.ToLower(d.Name)
		byName[k] = append(byName[k], d)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	folders := map[int64]string{}
	if withFolder {
		rows, err := b.Q.Query(ctx, `SELECT id, name FROM projects`)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			var name string
			if err := rows.Scan(&id, &name); err != nil {
				rows.Close()
				return nil, err
			}
			folders[id] = name
		}
		rows.Close()
	}
	var out []*Datasource
	seen := map[int64]bool{}
	for _, t := range tables {
		matches := byName[strings.ToLower(t.name)]
		if t.folder != "" {
			var in []*Datasource
			for _, d := range matches {
				if strings.ToLower(folders[d.ProjectID]) == strings.ToLower(t.folder) {
					in = append(in, d)
				}
			}
			matches = in
		}
		if len(matches) > 1 {
			var exact []*Datasource
			for _, d := range matches {
				if d.Name == t.name {
					exact = append(exact, d)
				}
			}
			matches = exact
		}
		if len(matches) == 1 && !seen[matches[0].ID] {
			seen[matches[0].ID] = true
			out = append(out, matches[0])
		}
	}
	return out, nil
}

// SavedScriptLineage is saved_script_lineage: (inputs, outputs) of a saved script.
func (b *Builder) SavedScriptLineage(ctx context.Context, o *Object, child *Run) ([]Dataset, []Dataset, error) {
	script := &savedscripts.Script{ID: o.ID, CurrentVersion: o.CurrentVersion}
	info := pyjson.Object{}
	if child != nil {
		info = jsonOf(child.Script)
	}
	var content pyjson.Object
	declared, isList := info.Value("inputs").([]any)
	if !isList {
		var err error
		if content, err = savedscripts.ContentOf(ctx, b.Q, script, 0); err != nil {
			return nil, nil, err
		}
		v := content.Value("inputs")
		if pyjson.Truthy(v) {
			if declared, isList = v.([]any); !isList {
				return nil, nil, errNotADict
			}
		}
	}
	ids, err := datasourceIDs(declared)
	if err != nil {
		return nil, nil, err
	}
	inputs, err := b.datasourceInputs(ctx, ids)
	if err != nil {
		return nil, nil, err
	}
	outputs := []Dataset{}
	if child != nil && child.DatasourceID != nil {
		ds, err := b.GetDatasource(ctx, *child.DatasourceID)
		if err != nil {
			return nil, nil, err
		}
		if ds != nil {
			x, err := b.DatasourceDataset(ctx, ds, true, child.RowsWritten)
			if err != nil {
				return nil, nil, err
			}
			outputs = append(outputs, x)
		}
	} else if child == nil || child.Status != "SUCCESS" {
		if content == nil {
			if content, err = savedscripts.ContentOf(ctx, b.Q, script, 0); err != nil {
				return nil, nil, err
			}
		}
		var publish pyjson.Object
		if v := content.Value("output"); pyjson.Truthy(v) {
			var ok bool
			if publish, ok = v.(pyjson.Object); !ok {
				return nil, nil, errNotADict
			}
		}
		if pyjson.Truthy(publish.Value("publish")) && pyjson.Truthy(publish.Value("name")) {
			if pid, ok := isInt(publish.Value("project_id")); ok {
				x, err := b.intendedOutput(ctx, pid, pyjson.Str(publish.Value("name")))
				if err != nil {
					return nil, nil, err
				}
				outputs = append(outputs, x)
			}
		}
	}
	if inputs == nil {
		inputs = []Dataset{}
	}
	return inputs, outputs, nil
}

// NotebookInputs is notebook_inputs: the datasources a notebook has read, in one session (its
// Job's name) or ever.
func (b *Builder) NotebookInputs(ctx context.Context, notebookID int64, sessionKey string) ([]Dataset, error) {
	var rows pgx.Rows
	var err error
	if sessionKey != "" {
		rows, err = b.Q.Query(ctx, `SELECT datasource_id FROM notebook_reads WHERE notebook_id = $1 AND last_session = $2
			ORDER BY read_at, id`, notebookID, sessionKey)
	} else {
		rows, err = b.Q.Query(ctx, `SELECT datasource_id FROM notebook_reads WHERE notebook_id = $1 ORDER BY id`, notebookID)
	}
	if err != nil {
		return nil, err
	}
	var ids []any
	seen := map[int64]bool{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		if sessionKey == "" && seen[id] {
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return b.datasourceInputs(ctx, ids)
}

// JobEventsFor is job_events_for: the STATIC lineage of a SQL script, a saved script, a
// notebook or a flow group.
func (b *Builder) JobEventsFor(ctx context.Context, o *Object) ([]pyjson.Object, error) {
	var inputs, outputs []Dataset
	var job Job
	var err error
	switch o.Kind {
	case KindSQLScript:
		var facets pyjson.Object
		if inputs, outputs, facets, err = b.SQLScriptLineage(ctx, o, nil); err != nil {
			return nil, err
		}
		job, err = b.objectJob(ctx, o.Kind, o, "SQL_SCRIPT", facets)
	case KindSavedScript:
		if inputs, outputs, err = b.SavedScriptLineage(ctx, o, nil); err != nil {
			return nil, err
		}
		job, err = b.objectJob(ctx, o.Kind, o, "SCRIPT", nil)
	case KindNotebook:
		if inputs, err = b.NotebookInputs(ctx, o.ID, ""); err != nil {
			return nil, err
		}
		job, err = b.objectJob(ctx, o.Kind, o, "NOTEBOOK", nil)
	default:
		job, err = b.objectJob(ctx, o.Kind, o, "FLOW_GROUP", nil)
	}
	if err != nil {
		return nil, err
	}
	return []pyjson.Object{b.JobEvent(job, inputs, outputs)}, nil
}

// groupCellJobs is _group_cell_jobs: the child jobs of a group's Container and Pushmail cells.
func groupCellJobs(definition string) []string {
	var labels []string
	seen := map[string]bool{}
	steps, _ := flowgroups.Parse(definition).Get("steps").([]any)
	for _, row := range steps {
		cells, _ := row.([]any)
		for _, c := range cells {
			cell, ok := c.(*pyd.ModelValue)
			if !ok {
				continue
			}
			var label string
			switch cell.Get("kind") {
			case "container":
				data := pyjson.Object{}
				if v, ok := pydconv.FromPyd(cell.Get("container")).(pyjson.Object); ok {
					data = v
				}
				id, _ := cell.Get("id").(int64)
				label = "container " + flowaccess.ContainerLabel(pyjson.Object{{Key: "id", Value: fmt.Sprintf("container-cell-%d", id)},
					{Key: "data", Value: data}})
			case "pushmail":
				pm, ok := cell.Get("pushmail").(*pyd.ModelValue)
				if !ok {
					continue
				}
				l := pyd.Strip(fmt.Sprint(pm.Get("label")))
				if l == "" {
					l = "Pushmail"
				}
				label = "pushmail " + l
			default:
				continue
			}
			if !seen[label] {
				seen[label] = true
				labels = append(labels, label)
			}
		}
	}
	return labels
}

// pushmailCells are the Pushmail cells of a group: their label and connection.
func pushmailCells(definition string) []struct {
	label string
	conn  *int64
} {
	var out []struct {
		label string
		conn  *int64
	}
	steps, _ := flowgroups.Parse(definition).Get("steps").([]any)
	for _, row := range steps {
		cells, _ := row.([]any)
		for _, c := range cells {
			cell, ok := c.(*pyd.ModelValue)
			if !ok || cell.Get("kind") != "pushmail" {
				continue
			}
			pm, ok := cell.Get("pushmail").(*pyd.ModelValue)
			if !ok {
				continue
			}
			x := struct {
				label string
				conn  *int64
			}{label: fmt.Sprint(pm.Get("label"))}
			if id, ok := pm.Get("connectionId").(int64); ok {
				x.conn = &id
			}
			out = append(out, x)
		}
	}
	return out
}

// stepLabels is _step_labels: the jobs of a flow's Script and Container nodes.
func stepLabels(f *Flow) []string {
	var labels []string
	seen := map[string]bool{}
	add := func(l string) {
		if !seen[l] {
			seen[l] = true
			labels = append(labels, l)
		}
	}
	def := definition(f)
	for _, n := range scriptnodes.ScriptNodes(f.Definition) {
		add("script " + scriptnodes.Label(n))
	}
	if nodes, ok := def.Value("nodes").([]any); ok {
		for _, n := range nodes {
			if o, ok := n.(pyjson.Object); ok && o.Value("type") == "container" {
				add("container " + flowaccess.ContainerLabel(o))
			}
		}
	}
	return labels
}

// containerFacet is _container_facet: the image a Container node or cell ran.
func (b *Builder) containerFacet(ctx context.Context, info pyjson.Object) (pyjson.Object, error) {
	strOr := func(v any) string {
		if !pyjson.Truthy(v) {
			return ""
		}
		return pyjson.Str(v)
	}
	fields := []pyjson.KV{{Key: "registry", Value: strOr(info.Value("registry"))}, {Key: "repository", Value: strOr(info.Value("repository"))}}
	if id, ok := isInt(info.Value("registry_id")); ok {
		var host string
		err := b.Q.QueryRow(ctx, `SELECT host FROM container_registries WHERE id = $1`, id).Scan(&host)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		if err == nil {
			fields = append(fields, pyjson.KV{Key: "registryHost", Value: host})
		}
	}
	for _, k := range [][2]string{{"tag", "reference"}, {"digest", "digest"}, {"image", "image"}} {
		if v := info.Value(k[1]); v != nil {
			fields = append(fields, pyjson.KV{Key: k[0], Value: v})
		}
	}
	return facet(Producer+"/blob/main/docs/openlineage/TabulariaContainerJobFacet.json", fields...), nil
}

// UpstreamInputs is upstream_inputs: the source nodes upstream of a node along the data edges.
func (b *Builder) UpstreamInputs(ctx context.Context, f *Flow, nodeID any) ([]Dataset, error) {
	def := definition(f)
	nodes := map[string]pyjson.Object{}
	order := map[string]int{}
	list, _ := def.Value("nodes").([]any)
	for i, n := range list {
		if o, ok := n.(pyjson.Object); ok {
			nodes[hashKey(o.Value("id"))] = o
			order[hashKey(o.Value("id"))] = i
		}
	}
	into := map[string][]any{}
	edges, _ := def.Value("edges").([]any)
	for _, e := range edges {
		o, ok := e.(pyjson.Object)
		if !ok || o.Value("targetHandle") == "seq-in" || o.Value("sourceHandle") == "seq-out" {
			continue
		}
		k := hashKey(o.Value("target"))
		into[k] = append(into[k], o.Value("source"))
	}
	var sources []pyjson.Object
	seen := map[string]bool{hashKey(nodeID): true}
	todo := append([]any{}, into[hashKey(nodeID)]...)
	for len(todo) > 0 {
		nid := todo[len(todo)-1]
		todo = todo[:len(todo)-1]
		k := hashKey(nid)
		n, ok := nodes[k]
		if seen[k] || !ok {
			continue
		}
		seen[k] = true
		if n.Value("type") == "source" {
			sources = append(sources, n)
		}
		todo = append(todo, into[k]...)
	}
	sort.SliceStable(sources, func(i, j int) bool {
		return order[hashKey(sources[i].Value("id"))] < order[hashKey(sources[j].Value("id"))]
	})
	return b.sourceDatasets(ctx, sources)
}

// ── a datasource's refresh: origin_inputs ───────────────────────────────────

func sourceRef(ds *Datasource) (pyjson.Object, bool) {
	t := str(ds.SourceRef)
	if t == "" {
		t = "{}"
	}
	v, err := pyjson.Loads(t)
	if err != nil {
		return pyjson.Object{}, true
	}
	o, ok := v.(pyjson.Object)
	return o, ok
}

// OriginInputs is origin_inputs: where a DATABASE datasource takes its data from.
func (b *Builder) OriginInputs(ctx context.Context, ds *Datasource) ([]Dataset, pyjson.Object, error) {
	if ds.ConnectionID == nil || *ds.ConnectionID == 0 {
		return []Dataset{}, pyjson.Object{}, nil
	}
	c, err := b.GetConnection(ctx, *ds.ConnectionID)
	if err != nil || c == nil {
		return []Dataset{}, pyjson.Object{}, err
	}
	ref, isDict := sourceRef(ds)
	dataSource := func(name, uri string) pyjson.Object {
		return pyjson.Object{{Key: "dataSource", Value: facet(std("1-0-1", "DatasourceDatasetFacet"), pyjson.KV{Key: "name", Value: name},
			pyjson.KV{Key: "uri", Value: uri})}}
	}
	strOr := func(v any) string {
		if !pyjson.Truthy(v) {
			return ""
		}
		return pyjson.Str(v)
	}
	switch c.DBType {
	case "sharepoint":
		if !isDict {
			return nil, nil, errNotADict
		}
		parts := strings.Split(c.Host, "://")
		site := strings.TrimRight(parts[len(parts)-1], "/")
		path := "/" + strings.Trim(strOr(ref.Value("path")), "/")
		if v := ref.Value("sheet"); pyjson.Truthy(v) {
			path += "#" + pyjson.Str(v)
		}
		return []Dataset{{Namespace: "sharepoint://" + site, Name: path, Facets: dataSource(c.Name, "https://"+site)}}, pyjson.Object{}, nil
	case "gsheets":
		if !isDict {
			ref = pyjson.Object{}
		}
		name := "/spreadsheets/d/" + strOr(ref.Value("spreadsheet_id"))
		if v := ref.Value("sheet"); pyjson.Truthy(v) {
			name += "#" + pyjson.Str(v)
		}
		return []Dataset{{Namespace: "https://docs.google.com", Name: name,
			Facets: dataSource(c.Name, "https://docs.google.com/spreadsheets")}}, pyjson.Object{}, nil
	case "api":
		if !isDict {
			ref = pyjson.Object{}
		}
		d, err := apiDataset(c, ref)
		return []Dataset{d}, pyjson.Object{}, err
	case "sftp":
		if !isDict {
			return nil, nil, errNotADict
		}
		return []Dataset{SFTPDataset(SFTPAddress(c), SFTPPath(text(ref.Value("folder")), text(ref.Value("pattern"))), "", false, nil, "")},
			pyjson.Object{}, nil
	case "s3":
		if !isDict {
			return nil, nil, errNotADict
		}
		bucket := pyd.Strip(strOr(ref.Value("bucket")))
		if bucket == "" {
			bucket = pyd.Strip(deref(c.Database))
		}
		return []Dataset{ObjectDataset(c.Host, bucket, SFTPPath(text(ref.Value("folder")), text(ref.Value("pattern"))), "", false, nil, "")},
			pyjson.Object{}, nil
	}
	if str(ds.SourceType) == "sql" {
		sql := str(ds.SourceRef)
		inputs, err := b.tablesInSQL(ctx, c, sql)
		if err != nil {
			return nil, nil, err
		}
		var dialect any
		if d, ok := schemes[c.DBType]; ok {
			dialect = d
		}
		return inputs, pyjson.Object{{Key: "sql", Value: sqlFacet(sql, dialect)}}, nil
	}
	return []Dataset{TableDataset(c, str(ds.SourceRef), false, nil, "")}, pyjson.Object{}, nil
}

// apiDataset is api_dataset: an HTTP API, never the query string.
func apiDataset(c *Connection, ref pyjson.Object) (Dataset, error) {
	u, err := urlsplit(strings.TrimSpace(c.Host))
	if err != nil {
		return Dataset{}, err
	}
	def := 80
	if u.scheme == "https" {
		def = 443
	}
	scheme := u.scheme
	if scheme == "" {
		scheme = "https"
	}
	ns := scheme + "://" + u.hostname
	if u.port != nil && *u.port != 0 && *u.port != def {
		ns += ":" + strconv.Itoa(*u.port)
	}
	refPath := ""
	if v := ref.Value("path"); pyjson.Truthy(v) {
		refPath = pyjson.Str(v)
	}
	refPath, _, _ = strings.Cut(refPath, "?")
	var parts []string
	for _, p := range []string{u.path, refPath} {
		if s := strings.Trim(p, "/"); s != "" {
			parts = append(parts, s)
		}
	}
	return Dataset{Namespace: ns, Name: "/" + strings.Join(parts, "/"), Facets: pyjson.Object{}}, nil
}

type splitURL struct {
	scheme, hostname, path string
	port                   *int
}

// urlsplit is urllib.parse.urlsplit's scheme, hostname, port and path.
func urlsplit(raw string) (splitURL, error) {
	var u splitURL
	rest := raw
	if i := strings.IndexByte(rest, ':'); i > 0 {
		ok := true
		for j, r := range rest[:i] {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || j > 0 && (r >= '0' && r <= '9' || r == '+' || r == '-' || r == '.')) {
				ok = false
				break
			}
		}
		if ok {
			u.scheme, rest = strings.ToLower(rest[:i]), rest[i+1:]
		}
	}
	netloc := ""
	if strings.HasPrefix(rest, "//") {
		rest = rest[2:]
		end := strings.IndexAny(rest, "/?#")
		if end < 0 {
			end = len(rest)
		}
		netloc, rest = rest[:end], rest[end:]
	}
	if end := strings.IndexAny(rest, "?#"); end >= 0 {
		rest = rest[:end]
	}
	u.path = rest
	if i := strings.LastIndexByte(netloc, '@'); i >= 0 {
		netloc = netloc[i+1:]
	}
	host, port := netloc, ""
	if strings.HasPrefix(netloc, "[") {
		if end := strings.IndexByte(netloc, ']'); end >= 0 {
			host = netloc[1:end]
			if p, ok := strings.CutPrefix(netloc[end+1:], ":"); ok {
				port = p
			}
		}
	} else if h, p, ok := strings.Cut(netloc, ":"); ok {
		host, port = h, p
	}
	u.hostname = strings.ToLower(host)
	if port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || strings.TrimLeft(port, "0123456789") != "" {
			return u, fmt.Errorf("Port could not be cast to integer value as %q", port)
		}
		if n < 0 || n > 65535 {
			return u, errors.New("Port out of range 0-65535")
		}
		u.port = &n
	}
	return u, nil
}

// refreshJob is a refresh's job and its inputs (as _describe and _refresh_job_event build them).
func (b *Builder) refreshJob(ctx context.Context, ds *Datasource) (Job, []Dataset, error) {
	inputs, facets, err := b.OriginInputs(ctx, ds)
	if err != nil {
		return Job{}, nil, err
	}
	facets = facets.Set("jobType", JobType("REFRESH"))
	name, err := b.JobNameForRefresh(ctx, ds)
	return Job{Name: name, Facets: facets}, inputs, err
}

// ── a notebook's session ────────────────────────────────────────────────────

// SessionRow is what a notebook session's events read: a NotebookSession, or what is left of it.
type SessionRow struct {
	NotebookID, Version int64
	Mode, JobName       string
	StartedAt           time.Time
}

// NotebookSessionEvents is notebook_session_events: one event of a notebook's session.
func (b *Builder) NotebookSessionEvents(ctx context.Context, row SessionRow, state, errText string, datasourceID *int64) ([]pyjson.Object, error) {
	switch state {
	case "START", "RUNNING", "COMPLETE", "FAIL", "ABORT":
	default:
		return nil, nil
	}
	nb, err := b.GetObject(ctx, KindNotebook, row.NotebookID)
	if err != nil || nb == nil {
		return nil, err
	}
	facets := pyjson.Object{{Key: "tabularia", Value: facet(Producer+"/blob/main/docs/openlineage/TabulariaRunFacet.json",
		pyjson.KV{Key: "kind", Value: "notebook"}, pyjson.KV{Key: "trigger", Value: "manual"}, pyjson.KV{Key: "version", Value: row.Version},
		pyjson.KV{Key: "mode", Value: row.Mode})}}
	if state == "FAIL" && errText != "" {
		facets = append(facets, pyjson.KV{Key: "errorMessage", Value: errorMessage(errText)})
	}
	var inputs []Dataset
	var when string
	switch state {
	case "START":
		inputs, when = []Dataset{}, iso(&row.StartedAt)
	case "RUNNING":
		if datasourceID == nil {
			return nil, nil
		}
		ds, err := b.GetDatasource(ctx, *datasourceID)
		if err != nil || ds == nil {
			return nil, err
		}
		x, err := b.DatasourceDataset(ctx, ds, false, nil)
		if err != nil {
			return nil, err
		}
		inputs, when = []Dataset{x}, Now()
	default:
		if inputs, err = b.NotebookInputs(ctx, row.NotebookID, row.JobName); err != nil {
			return nil, err
		}
		when = Now()
	}
	job, err := b.objectJob(ctx, KindNotebook, nb, "NOTEBOOK", nil)
	if err != nil {
		return nil, err
	}
	return []pyjson.Object{b.runEvent(when, state, b.S.NotebookRunUUID(row.JobName), facets, job, inputs, []Dataset{})}, nil
}
