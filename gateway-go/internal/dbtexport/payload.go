package dbtexport

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtls"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
)

// Build is _build: the payload for the engine and the project it came from. A refusal is an
// *Error (the route's 422); a *pyval.Error is a Python exception nobody catches (a 500), as is
// any other error (the database's).
func Build(ctx context.Context, q Querier, f *Flow, bucket, target string, exportedBy *string, options *Options) (payload pyjson.Object, p *Project, err error) {
	err = pyval.Catch(func() { payload, p = build(ctx, q, f, bucket, target, exportedBy, options) })
	return
}

func build(ctx context.Context, q Querier, f *Flow, bucket, target string, exportedBy *string, options *Options) (pyjson.Object, *Project) {
	if options != nil {
		target = options.Target
	}
	if target != "duckdb" && target != "native" && target != "clickhouse" {
		refuse("unknown target: '"+target+"'", "unknown_target", "target", target)
	}
	p := newProject(ctx, q, bucket, target, exportedBy, options)
	p.includeFlow(f)
	conns := pyval.NewDict()
	for _, t := range p.tables.Values() {
		c := t.(*table).conn
		conns.Set(c.ID, c)
	}
	var cs []*Connection
	for _, c := range conns.Values() {
		cs = append(cs, c.(*Connection))
	}
	var payload pyjson.Object
	switch target {
	case "native":
		payload = p.nativePayload(f, cs)
	case "clickhouse":
		payload = p.clickhousePayload(f, cs)
	default:
		payload = p.duckdbPayload(f, cs)
	}
	p.notesWithoutSchema()
	payload = payload.Set("notes", p.note)
	if e := knownRefusal(p.models, target); e != nil {
		pyval.Throw(e)
	}
	return payload, p
}

// operations is _operations: the operations, the right branches' too.
func operations(ops any) []any {
	var out []any
	if !pyval.Truthy(ops) {
		return out
	}
	for _, op := range pyval.Iter(ops) {
		out = append(out, op)
		var right any = pyjson.Object{}
		if pyval.IsDict(op) {
			right = pyval.Or(pyval.Get(pyval.Or(pyval.Get(op, "params"), pyjson.Object{}), "right"), pyjson.Object{})
		}
		out = append(out, operations(pyval.Or(pyval.Get(right, "operations"), []any{}))...)
	}
	return out
}

// knownRefusal is _known_refusal: what the engine would refuse anyway, said earlier.
func knownRefusal(models []any, target string) *Error {
	kinds := map[string]bool{}
	for _, m := range models {
		for _, op := range operations(pyval.Or(get(m.(*pyjson.Object), "operations"), []any{})) {
			if pyval.IsDict(op) {
				k := pyval.Get(op, "type")
				pyval.Key(k)
				if s, ok := k.(string); ok {
					kinds[s] = true
				}
			}
		}
	}
	if kinds["foreach"] {
		return &Error{Message: "the «foreach» node cannot be exported: dbt has no loop at runtime", Code: "foreach_not_exportable", Params: pyjson.Object{}}
	}
	if kinds["pivot"] && (target == "native" || target == "clickhouse") {
		return &Error{Message: "the «pivot» node produces columns decided by the data: it exports only with the federated target (dbt-duckdb), " +
			"or use in its place an sql node that lists the columns", Code: "pivot_not_portable", Params: pyjson.Object{}}
	}
	for _, m := range models {
		for _, op := range operations(pyval.Or(get(m.(*pyjson.Object), "operations"), []any{})) {
			if pyval.IsDict(op) {
				if e := incompleteStep(op); e != nil {
					return e
				}
			}
		}
	}
	return nil
}

// required is _REQUIRED.
var required = map[string][]string{
	"select": {"columns"}, "reorder": {"columns"}, "drop": {"columns"}, "rename": {"mapping"}, "cast": {"columns"},
	"filter": {"column"}, "sort": {"by"}, "limit": {"n"}, "fill_null": {"columns"}, "group_by": {"by", "aggregations"},
	"compute": {"columns"}, "sql": {"query"}, "join": {"right"}, "union": {"right"}, "pivot": {"index", "on", "values"},
}

// empty is `v in (None, "", [])`.
func empty(v any) bool {
	return pyval.Eq(v, nil) || pyval.Eq(v, "") || pyval.Eq(v, []any{})
}

func listLen(v any) int {
	if l, ok := v.([]any); ok {
		return len(l)
	}
	return 1
}

// incompleteStep is _incomplete_step: a step saved half done, refused as the engine does.
func incompleteStep(op any) *Error {
	kind := pyval.Str(pyval.Get(op, "type"))
	params := pyval.Or(pyval.Get(op, "params"), pyjson.Object{})
	no := func(detail string) *Error {
		return &Error{Message: kind + ": the step is incomplete or not valid (" + detail + ")", Code: "step_invalid",
			Params: pyjson.Object{{Key: "step", Value: kind}, {Key: "detail", Value: detail}}}
	}
	for _, k := range required[kind] {
		if empty(pyval.Get(params, k)) {
			return no("required parameter missing: '" + k + "'")
		}
	}
	if kind == "join" && !pyval.Eq(pyval.GetOr(params, "how", "inner"), "cross") && empty(pyval.Get(params, "on")) {
		left, right := pyval.Get(params, "left_on"), pyval.Get(params, "right_on")
		if empty(left) || empty(right) {
			return no("required parameter missing: 'on', or 'left_on' and 'right_on'")
		}
		if listLen(left) != listLen(right) {
			return no("join: left_on and right_on must have the same number of columns")
		}
	}
	if kind == "group_by" {
		if aggs, ok := pyval.Index(params, "aggregations").([]any); ok {
			for _, a := range aggs {
				if !pyval.IsDict(a) || empty(pyval.Get(a, "column")) {
					return no("an aggregation has no column")
				}
			}
		}
	}
	if kind == "filter" {
		operator, value := pyval.GetOr(params, "operator", "eq"), pyval.Get(params, "value")
		if !pyval.Eq(operator, "is_null") && !pyval.Eq(operator, "is_not_null") {
			list, isList := value.([]any)
			if value == nil || ((pyval.Eq(operator, "in") || pyval.Eq(operator, "not_in")) && pyval.Eq(value, []any{})) ||
				(pyval.Eq(operator, "between") && (!isList || len(list) != 2)) {
				column := pyval.Str(pyval.Index(params, "column"))
				op := pyval.Str(operator)
				return &Error{Message: "filter on «" + column + "»: no value for the operator '" + op + "'", Code: "filter_value_missing",
					Params: pyjson.Object{{Key: "column", Value: column}, {Key: "operator", Value: op}}}
			}
		}
	}
	return nil
}

// readError is _read_error: a flow the resolver cannot read, said with the flow's name.
func readError(f *Flow, e *flowresolver.Error) *Error {
	switch e.Code {
	case "no_outputs":
		return &Error{Message: "the flow «" + f.Name + "» has no exportable outputs: " + e.Message, Code: "no_outputs",
			Params: pyjson.Object{{Key: "flow", Value: f.Name}}}
	case "output_no_source":
		return &Error{Message: "the flow «" + f.Name + "»: " + e.Message, Code: "output_no_source",
			Params: pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "output", Value: pyval.GetOr(e.Params, "output", "")}}}
	case "flow_loop":
		return &Error{Message: "the flow «" + f.Name + "»: " + e.Message, Code: "flow_loop",
			Params: pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "node", Value: pyval.GetOr(e.Params, "node", "")}}}
	}
	return &Error{Message: "the flow «" + f.Name + "» cannot be read: " + e.Message, Code: "flow_resolve",
		Params: pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "detail", Value: e.Message}}}
}

func notExportable(c *Connection) *Error {
	return &Error{Message: "the connection «" + c.Name + "» is " + c.DBType + ", which no dbt target reads", Code: "source_not_exportable",
		Params: pyjson.Object{{Key: "connection", Value: c.Name}, {Key: "db_type", Value: c.DBType}}}
}

func notFederable(c *Connection) *Error {
	if !readable(c.DBType) {
		return notExportable(c)
	}
	return &Error{Message: "the connection «" + c.Name + "» is " + c.DBType + ": DuckDB cannot federate it (federation: postgresql/mysql/mariadb)",
		Code: "not_federable", Params: pyjson.Object{{Key: "connection", Value: c.Name}, {Key: "db_type", Value: c.DBType}}}
}

// invalidDefinition is _invalid_definition: a definition the editor would never save.
func invalidDefinition(f *Flow, e *pyval.Error) *Error {
	typ := e.Type
	detail := "its definition is not valid (" + typ + ": " + pystr.Cut(e.Msg, 120) + ")"
	return &Error{Message: "the flow «" + f.Name + "» cannot be read: " + detail, Code: "flow_resolve",
		Params: pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "detail", Value: detail}}}
}

// sourceNames is _source_names: the name of each (connection, schema) in sources.yml.
func (p *Project) sourceNames(def func(*Connection, string) string) func(*Connection, string) string {
	type pair struct {
		conn   *Connection
		schema string
		tables []string
	}
	pairs := pyval.NewDict()
	for _, v := range p.tables.Values() {
		t := v.(*table)
		if t.table == "" {
			continue
		}
		pr := pairs.SetDefault(pyval.Tuple{t.conn.ID, t.schema}, &pair{conn: t.conn, schema: t.schema}).(*pair)
		found := false
		for _, x := range pr.tables {
			if x == t.table {
				found = true
			}
		}
		if !found {
			pr.tables = append(pr.tables, t.table)
		}
	}
	names := map[string]string{}
	keyOf := func(id int64, schema string) string { return pyval.Key(pyval.Tuple{id, schema}) }
	for _, v := range pairs.Values() {
		pr := v.(*pair)
		if choice, ok := p.options.Sources[sourceKey(pr.conn.ID, pr.schema)]; ok && choice.Name != nil && *choice.Name != "" {
			names[keyOf(pr.conn.ID, pr.schema)] = *choice.Name
		}
	}
	used := map[string]bool{}
	for _, n := range names {
		used[n] = true
	}
	for _, v := range pairs.Values() {
		pr := v.(*pair)
		p.sourceBase.Set(pyval.Tuple{pr.conn.ID, pr.schema}, def(pr.conn, pr.schema))
		if _, ok := names[keyOf(pr.conn.ID, pr.schema)]; !ok {
			base := def(pr.conn, pr.schema)
			cand := base
			for n := 2; used[cand]; n++ {
				cand = fmt.Sprintf("%s_%d", base, n)
			}
			names[keyOf(pr.conn.ID, pr.schema)] = cand
			used[cand] = true
		}
	}
	views := map[string]*pair{}
	for _, v := range pairs.Values() {
		pr := v.(*pair)
		name := names[keyOf(pr.conn.ID, pr.schema)]
		for _, t := range pr.tables {
			k := name + "\x00" + t
			other, ok := views[k]
			if !ok {
				views[k] = pr
				continue
			}
			if other != pr {
				before := other.conn.Name + "." + other.schema
				sources := before + ", " + pr.conn.Name + "." + pr.schema
				refuse("two sources are both named «"+name+"» and both have a table «"+t+"» ("+sources+")",
					"source_name_duplicate", "name", name, "table", t, "sources", sources)
			}
		}
	}
	return func(c *Connection, schema string) string { return names[keyOf(c.ID, schema)] }
}

// macroSource is _macro_source; jinjaString is _jinja_string.
func macroSource(name, tbl string) string {
	return "{{ source(" + jinjaString(name) + ", " + jinjaString(tbl) + ") }}"
}

var quoteSplit = regexp.MustCompile(`['\\]`)

func jinjaString(s string) string {
	if !strings.Contains(s, "\\") && !strings.Contains(s, "'") {
		return "'" + s + "'"
	}
	if !strings.Contains(s, "\\") && !strings.Contains(s, `"`) {
		return `"` + s + `"`
	}
	// re.split(r"(['\\])", s), the empty pieces dropped
	var parts []string
	last := 0
	for _, m := range quoteSplit.FindAllStringIndex(s, -1) {
		parts = append(parts, s[last:m[0]], s[m[0]:m[1]])
		last = m[1]
	}
	parts = append(parts, s[last:])
	var pieces []string
	for _, x := range parts {
		switch x {
		case "":
		case "'":
			pieces = append(pieces, `"'"`)
		case `\`:
			pieces = append(pieces, `'\\'`)
		default:
			pieces = append(pieces, "'"+x+"'")
		}
	}
	if len(pieces) <= 1 {
		pieces = append(pieces, "''")
	}
	return strings.Join(pieces, " ~ ")
}

// quotableSources is _quotable_sources.
func (p *Project) quotableSources(adapter func(*Connection) string, withSchema func(*Connection) bool) {
	for _, v := range p.tables.Values() {
		t := v.(*table)
		choice, ok := p.options.Sources[sourceKey(t.conn.ID, t.schema)]
		if t.table == "" || (ok && choice.Declared) {
			continue
		}
		var list []string
		if withSchema(t.conn) {
			list = append(list, t.schema)
		}
		for _, name := range append(list, t.table) {
			quotable(name, adapter(t.conn), "source")
		}
	}
}

// sourcesYAML is _sources_yaml: one entry per (connection, schema).
func (p *Project) sourcesYAML(nameFor func(*Connection, string) string, databaseFor func(*Connection) any) []any {
	sources := pyval.NewDict()
	for _, v := range p.tables.Values() {
		t := v.(*table)
		if t.table == "" {
			continue
		}
		choice, ok := p.options.Sources[sourceKey(t.conn.ID, t.schema)]
		s := sources.SetDefault(pyval.Tuple{t.conn.ID, t.schema}, &pyjson.Object{{Key: "name", Value: nameFor(t.conn, t.schema)},
			{Key: "database", Value: databaseFor(t.conn)}, {Key: "schema", Value: t.schema}, {Key: "connection_id", Value: t.conn.ID},
			{Key: "key", Value: sourceKey(t.conn.ID, t.schema)}, {Key: "connection", Value: t.conn.Name},
			{Key: "declared", Value: ok && choice.Declared}, {Key: "tables", Value: []any{}}}).(*pyjson.Object)
		tables := get(s, "tables").([]any)
		found := false
		for _, x := range tables {
			if x.(pyjson.Object).Value("name") == t.table {
				found = true
			}
		}
		if !found {
			desc := "Datasource «" + t.ds.Name + "»"
			if t.ds.Description != "" {
				desc += ": " + t.ds.Description
			}
			set(s, "tables", append(tables, pyjson.Object{{Key: "name", Value: t.table}, {Key: "datasource_id", Value: t.ds.ID},
				{Key: "description", Value: desc}, {Key: "columns", Value: p.columnItems(t.ds, false)}}))
		}
	}
	return sources.Values()
}

var pgSSLMode = map[string]string{"verify": "verify-full", "trust": "require"}

// federatedTLSNote is _federated_tls_note.
func federatedTLSNote(c *Connection, reader string) string {
	mode := dbtls.Mode(c.DBType, c.Extra)
	if mode != "verify" && mode != "trust" {
		return ""
	}
	how := "without verifying its certificate"
	if mode == "verify" {
		how = "verifying its certificate"
	}
	if reader == "duckdb" && c.DBType == "postgresql" {
		env := "`PGSSLMODE=" + pgSSLMode[mode] + "`"
		if _, ok := dbtls.EngineOptions(c.DBType, c.Extra).Get("ca_cert"); ok {
			env += " with `PGSSLROOTCERT` naming the company authority's certificate"
		}
		return "Tabularia reaches «" + c.Name + "» encrypted, " + how + ". DuckDB reads it through libpq: run dbt with " + env +
			" to do the same (the secret above does not say it)."
	}
	if reader == "duckdb" {
		return "Tabularia reaches «" + c.Name + "» encrypted, " + how + ": the DuckDB secret above does not say it, " +
			"add the encryption to it as DuckDB's MySQL extension documents."
	}
	return "Tabularia reaches «" + c.Name + "» encrypted, " + how + ". ClickHouse reads it itself, through its " +
		c.DBType + " table engine: that encryption is the ClickHouse server's setting, not this project's."
}

func optStr(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

func portOr(port *int64, def any) any {
	if port != nil && *port != 0 {
		return *port
	}
	return def
}

func (p *Project) duckdbPayload(f *Flow, conns []*Connection) pyjson.Object {
	attachments := []any{}
	for _, c := range conns {
		fed, ok := federable[c.DBType]
		if !ok {
			pyval.Throw(notFederable(c))
		}
		if note := federatedTLSNote(c, "duckdb"); note != "" {
			p.note = append(p.note, note)
		}
		attachments = append(attachments, pyjson.Object{{Key: "alias", Value: fmt.Sprintf("db_%d", c.ID)}, {Key: "db_type", Value: c.DBType},
			{Key: "pw_env", Value: fmt.Sprintf("TABULARIA_DB_%d_PASSWORD", c.ID)},
			{Key: "conn", Value: pyjson.Object{{Key: "host", Value: c.Host}, {Key: "port", Value: portOr(c.Port, fed.port)},
				{Key: "database", Value: c.Database}, {Key: "username", Value: c.Username}}}})
	}
	p.quotableSources(func(*Connection) string { return "dbt-duckdb" }, func(*Connection) bool { return true })
	name := p.sourceNames(func(c *Connection, schema string) string { return Slug(fmt.Sprintf("db_%d_%s", c.ID, schema)) })
	sources := p.sourcesYAML(name, func(c *Connection) any { return fmt.Sprintf("db_%d", c.ID) })
	sourceMap := pyjson.Object{}
	for i, k := range p.tables.Keys() {
		t := p.tables.Values()[i].(*table)
		if t.table != "" {
			sourceMap = sourceMap.Set(pyval.JSONKey(k), pyjson.Object{{Key: "ref", Value: macroSource(name(t.conn, t.schema), t.table)},
				{Key: "columns", Value: columns(t.ds)}, {Key: "types", Value: dtypes(t.ds)}, {Key: "label", Value: t.table}})
		}
	}
	return pyjson.Object{{Key: "flow_name", Value: f.Name}, {Key: "flow_description", Value: f.Description}, {Key: "mode", Value: "duckdb"},
		{Key: "models", Value: p.models}, {Key: "seeds", Value: p.seeds}, {Key: "source_map", Value: sourceMap},
		{Key: "attachments", Value: attachments}, {Key: "sources", Value: sources}, {Key: "notes", Value: nil},
		{Key: "layout", Value: p.layout()}, {Key: "schema", Value: optStr(p.options.Schema)}, {Key: "exported_by", Value: optStr(p.exportedBy)}}
}

// nativeDestinations is _native_destinations: where the exported flow's database outputs land
// on the native target.
func (p *Project) nativeDestinations(conn *Connection) {
	for _, mv := range p.models {
		m := mv.(*pyjson.Object)
		mname := pyval.Str(get(m, "name"))
		dest, ok := p.dest[mname]
		if !ok {
			continue
		}
		var cid *int64
		if err := pyval.Catch(func() { n := pyval.Int(dest.Value("connection_id")); cid = &n }); err != nil {
			if pe, ok := err.(*pyval.Error); !ok || (pe.Type != "TypeError" && pe.Type != "ValueError") {
				pyval.Throw(err)
			}
		}
		tableText := pyval.Str(pyval.Or(dest.Value("table"), ""))
		var parts []string
		for _, x := range strings.Split(tableText, ".") {
			if s := pystr.Strip(x); s != "" {
				parts = append(parts, s)
			}
		}
		destTable := pyval.Str(dest.Value("table"))
		mode := pyval.Str(dest.Value("mode"))
		switch {
		case cid != nil && *cid == conn.ID && len(parts) > 0:
			own := "table"
			if mode == "append" {
				own = "incremental"
			}
			if get(m, "materialized") != own {
				p.note = append(p.note, "The model `"+mname+"` is the output table `"+destTable+"` ("+mode+") of the flow, materialized "+
					"as `"+pyval.Str(get(m, "materialized"))+"`: dbt builds it in the project's schema, so that it never replaces the table "+
					"Tabularia writes.")
				continue
			}
			schema := defaultSchema(conn)
			if len(parts) >= 2 {
				schema = parts[len(parts)-2]
			}
			alias := parts[len(parts)-1]
			if conn.DBType == "postgresql" {
				schema, alias = asPostgres(schema), asPostgres(alias)
				if own == "table" && !rebuildable(alias) {
					p.note = append(p.note, "The model `"+mname+"` is the output table `"+destTable+"` of the flow, a name dbt-postgres cannot "+
						"rebuild: it cuts the names of its temporary tables (`__dbt_tmp`, `__dbt_backup`) to 63 characters, "+
						"Postgres cuts names to 63 bytes, and with accented letters they collide. dbt builds it in the "+
						"project's schema.")
					continue
				}
			}
			quotable(schema, nativeAdapter(conn), "schema")
			quotable(alias, nativeAdapter(conn), "relation")
			set(m, "schema", schema)
			set(m, "alias", alias)
			if len(parts) == 1 {
				p.withoutSchemaOf(conn, parts[0])
			}
		case cid == nil || *cid != conn.ID:
			where := "another connection"
			if cid != nil {
				if other := p.connection(*cid); other != nil {
					where = "«" + other.Name + "»"
				}
			}
			p.note = append(p.note, "The model `"+mname+"` is the output table `"+destTable+"` that Tabularia writes on "+where+", not on the "+
				"warehouse dbt runs on: dbt builds it in the project's schema, and loading it into "+where+" is the data team's job.")
		}
	}
}

func nativeAdapter(c *Connection) string {
	if c.DBType == "clickhouse" {
		return "dbt-clickhouse"
	}
	return "dbt-postgres"
}

func (p *Project) nativePayload(f *Flow, conns []*Connection) pyjson.Object {
	for _, c := range conns {
		if !readable(c.DBType) {
			pyval.Throw(notExportable(c))
		}
	}
	if len(conns) > 1 {
		refuse("the native export requires all sources to be on the SAME connection (a single warehouse). This flow uses more than one — use the «federated» export.",
			"native_one_connection")
	}
	if len(conns) == 0 {
		refuse("the native export needs at least one database source: this flow reads only files", "native_needs_database")
	}
	conn := conns[0]
	if !native[conn.DBType] {
		refuse("no native dbt adapter for '"+conn.DBType+"'", "no_native_adapter", "db_type", conn.DBType)
	}
	p.quotableSources(nativeAdapter, func(*Connection) bool { return true })
	p.nativeDestinations(conn)
	name := p.sourceNames(func(c *Connection, schema string) string { return Slug("src_" + schema) })
	sources := p.sourcesYAML(name, func(c *Connection) any { return c.Database })
	sourceMap := pyjson.Object{}
	for i, k := range p.tables.Keys() {
		t := p.tables.Values()[i].(*table)
		if t.table != "" {
			sourceMap = sourceMap.Set(pyval.JSONKey(k), pyjson.Object{{Key: "ident", Value: fmt.Sprintf("__s_%d", i)},
				{Key: "macro", Value: macroSource(name(t.conn, t.schema), t.table)}, {Key: "columns", Value: columns(t.ds)},
				{Key: "types", Value: dtypes(t.ds)}, {Key: "label", Value: t.table}})
		}
	}
	plain := ports[conn.DBType]
	var port any
	if conn.Port != nil && *conn.Port != 0 {
		port = *conn.Port
	} else if dp := dbtls.DefaultPort(conn.DBType, conn.Extra, &plain); dp != nil {
		port = *dp
	}
	connection := pyjson.Object{{Key: "host", Value: conn.Host}, {Key: "port", Value: port}, {Key: "database", Value: conn.Database},
		{Key: "username", Value: conn.Username}}
	connection = append(connection, dbtls.EngineOptions(conn.DBType, conn.Extra)...)
	schema := "dbt_tabularia"
	if p.options.Schema != nil && *p.options.Schema != "" {
		schema = *p.options.Schema
	}
	nat := pyjson.Object{{Key: "db_type", Value: conn.DBType}, {Key: "pw_env", Value: fmt.Sprintf("TABULARIA_DB_%d_PASSWORD", conn.ID)},
		{Key: "target_schema", Value: schema}, {Key: "conn", Value: connection}}
	return pyjson.Object{{Key: "flow_name", Value: f.Name}, {Key: "flow_description", Value: f.Description}, {Key: "mode", Value: "native"},
		{Key: "models", Value: p.models}, {Key: "seeds", Value: p.seeds}, {Key: "source_map", Value: sourceMap}, {Key: "native", Value: nat},
		{Key: "sources", Value: sources}, {Key: "notes", Value: nil}, {Key: "layout", Value: p.layout()},
		{Key: "exported_by", Value: optStr(p.exportedBy)}}
}

func (p *Project) clickhousePayload(f *Flow, conns []*Connection) pyjson.Object {
	var unreadable []*table
	for _, v := range p.tables.Values() {
		t := v.(*table)
		if !clickhouseReads[t.conn.DBType] {
			unreadable = append(unreadable, t)
		}
	}
	for _, t := range unreadable {
		if t.table == "" {
			pyval.Throw(notExportable(t.conn))
		}
	}
	var missing []*Connection
	for _, t := range unreadable {
		if s, ok := p.options.Sources[sourceKey(t.conn.ID, t.schema)]; !ok || !s.Declared {
			missing = append(missing, t.conn)
		}
	}
	if len(missing) > 0 {
		c := missing[0]
		e := &Error{Message: "the connection «" + c.Name + "» is " + c.DBType + ": ClickHouse can read it only if it is PostgreSQL, MySQL or MariaDB",
			Code: "clickhouse_cannot_read", Params: pyjson.Object{{Key: "connection", Value: c.Name}, {Key: "db_type", Value: c.DBType}}}
		seen := map[string]bool{}
		for _, t := range unreadable {
			k := sourceKey(t.conn.ID, t.schema)
			if !seen[k] {
				seen[k] = true
				e.ToDeclare = append(e.ToDeclare, k)
			}
		}
		pyval.Throw(e)
	}
	p.quotableSources(func(*Connection) string { return "dbt-clickhouse" }, func(c *Connection) bool { return c.DBType == "clickhouse" })
	name := p.sourceNames(func(c *Connection, schema string) string { return Slug(fmt.Sprintf("db_%d_%s", c.ID, schema)) })
	sources := p.sourcesYAML(name, func(*Connection) any { return nil })
	sourceMap := pyjson.Object{}
	for i, k := range p.tables.Keys() {
		t := p.tables.Values()[i].(*table)
		if t.table != "" {
			sourceMap = sourceMap.Set(pyval.JSONKey(k), pyjson.Object{{Key: "ident", Value: fmt.Sprintf("__s_%d", i)},
				{Key: "macro", Value: macroSource(name(t.conn, t.schema), t.table)}, {Key: "columns", Value: columns(t.ds)},
				{Key: "types", Value: dtypes(t.ds)}, {Key: "label", Value: t.table}})
		}
	}
	connections := []any{}
	for _, c := range conns {
		var def any
		if pt, ok := ports[c.DBType]; ok {
			def = pt
		}
		connections = append(connections, pyjson.Object{{Key: "id", Value: c.ID}, {Key: "name", Value: c.Name}, {Key: "db_type", Value: c.DBType},
			{Key: "host", Value: c.Host}, {Key: "port", Value: portOr(c.Port, def)}, {Key: "database", Value: c.Database},
			{Key: "username", Value: c.Username}, {Key: "pw_env", Value: fmt.Sprintf("TABULARIA_DB_%d_PASSWORD", c.ID)}})
	}
	for _, c := range conns {
		if c.DBType != "clickhouse" {
			if note := federatedTLSNote(c, "clickhouse"); note != "" {
				p.note = append(p.note, note)
			}
		}
	}
	schema := "dbt_tabularia"
	if p.options.Schema != nil && *p.options.Schema != "" {
		schema = *p.options.Schema
	}
	return pyjson.Object{{Key: "flow_name", Value: f.Name}, {Key: "flow_description", Value: f.Description}, {Key: "mode", Value: "clickhouse"},
		{Key: "models", Value: p.models}, {Key: "seeds", Value: p.seeds}, {Key: "source_map", Value: sourceMap}, {Key: "sources", Value: sources},
		{Key: "notes", Value: nil}, {Key: "clickhouse", Value: pyjson.Object{{Key: "connections", Value: connections},
			{Key: "target_schema", Value: schema}}}, {Key: "layout", Value: p.layout()}, {Key: "exported_by", Value: optStr(p.exportedBy)}}
}

// ── the plan ────────────────────────────────────────────────────────────────

// Plan is export_plan: for each target whether it is possible (and why not), its sources with
// the default name; the outputs with the allowed materializations. No engine call.
func Plan(ctx context.Context, q Querier, f *Flow, bucket, language string) (out pyjson.Object, err error) {
	targets := []any{}
	var outputs, outputsIfDeclared []any
	for _, target := range []string{"clickhouse", "duckdb", "native"} {
		payload, p, err := Build(ctx, q, f, bucket, target, nil, nil)
		var e *Error
		if errors.As(err, &e) {
			item := pyjson.Object{{Key: "id", Value: target}, {Key: "available", Value: false}, {Key: "reason", Value: ErrorMessage(e, language)},
				{Key: "sources", Value: []any{}}}
			if len(e.ToDeclare) > 0 {
				requires, dp, dproject, err := ifDeclared(ctx, q, f, bucket, target, e.ToDeclare)
				if err != nil {
					return nil, err
				}
				if requires != nil {
					item = item.Set("requires", requires)
					item = item.Set("sources", planSources(dp, dproject))
					if !pyval.Truthy(outputsIfDeclared) {
						outputsIfDeclared = dproject.Outputs
					}
				}
			}
			targets = append(targets, item)
			continue
		}
		if err != nil {
			return nil, err
		}
		targets = append(targets, pyjson.Object{{Key: "id", Value: target}, {Key: "available", Value: true}, {Key: "reason", Value: nil},
			{Key: "sources", Value: planSources(payload, p)}})
		if outputs == nil {
			outputs = p.Outputs
		}
	}
	chosen := outputs
	if !pyval.Truthy(chosen) {
		chosen = outputsIfDeclared
	}
	if !pyval.Truthy(chosen) {
		chosen = []any{}
	}
	return pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "default_folder", Value: DefaultFolder(f.Name)}, {Key: "targets", Value: targets},
		{Key: "outputs", Value: chosen}}, nil
}

func planSources(payload pyjson.Object, p *Project) []any {
	out := []any{}
	srcs, _ := payload.Value("sources").([]any)
	for _, v := range srcs {
		s := v.(*pyjson.Object)
		var tables []string
		for _, t := range get(s, "tables").([]any) {
			tables = append(tables, t.(pyjson.Object).Value("name").(string))
		}
		sortStrings(tables)
		tl := make([]any, len(tables))
		for i, t := range tables {
			tl[i] = t
		}
		base := get(s, "name")
		if b, ok := p.sourceBase.Lookup(pyval.Tuple{get(s, "connection_id"), get(s, "schema")}); ok {
			base = b
		}
		out = append(out, pyjson.Object{{Key: "key", Value: get(s, "key")}, {Key: "connection", Value: get(s, "connection")},
			{Key: "schema", Value: get(s, "schema")}, {Key: "default_name", Value: get(s, "name")}, {Key: "default_base", Value: base},
			{Key: "tables", Value: tl}})
	}
	return out
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && lessCodePoints(s[j], s[j-1]); j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

func lessCodePoints(a, b string) bool {
	ra, rb := []rune(a), []rune(b)
	for i := 0; i < len(ra) && i < len(rb); i++ {
		if ra[i] != rb[i] {
			return ra[i] < rb[i]
		}
	}
	return len(ra) < len(rb)
}

// ifDeclared is _if_declared: a target that works only in the folder package with these
// sources declared by the team's own project.
func ifDeclared(ctx context.Context, q Querier, f *Flow, bucket, target string, keys []string) (pyjson.Object, pyjson.Object, *Project, error) {
	o := DefaultOptions(target)
	o.Package = "folder"
	for _, k := range keys {
		o.SourceKeys = append(o.SourceKeys, k)
		o.Sources[k] = SourceOption{Declared: true}
	}
	payload, p, err := Build(ctx, q, f, bucket, target, nil, o)
	var e *Error
	if errors.As(err, &e) {
		return nil, nil, nil, nil
	}
	if err != nil {
		return nil, nil, nil, err
	}
	declared := make([]any, len(keys))
	for i, k := range keys {
		declared[i] = k
	}
	return pyjson.Object{{Key: "package", Value: "folder"}, {Key: "declared", Value: declared}}, payload, p, nil
}
