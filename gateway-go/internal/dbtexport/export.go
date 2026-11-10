// Package dbtexport is services/dbt_export.py and services/dbt_messages.py: a flow's dbt export
// payload, what the engine compiles into a dbt project (models, sources, seeds, schema.yml,
// tests), and the plan the download dialog shows. The gateway decides here what it knows of
// the catalog; the engine writes the SQL (and translates it with sqlglot: nothing of that
// happens in the gateway). The AI's part (services/dbt_ai.py: descriptions, translations)
// is not here: an export that asks for it goes whole to the AI service.
package dbtexport

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"golang.org/x/text/unicode/norm"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyval"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// federable is _FEDERABLE: what DuckDB federates, its default port and its remote query function.
var federable = map[string]struct {
	port     int64
	function string
}{"postgresql": {5432, "postgres_query"}, "mysql": {3306, "mysql_query"}, "mariadb": {3306, "mysql_query"}}

// ports is _PORT.
var ports = map[string]int64{"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8123}

var (
	native          = map[string]bool{"postgresql": true, "clickhouse": true}
	clickhouseReads = map[string]bool{"postgresql": true, "mysql": true, "mariadb": true, "clickhouse": true}
)

func readable(dbType string) bool {
	_, fed := federable[dbType]
	return fed || native[dbType] || clickhouseReads[dbType]
}

// SeedMaxRows is SEED_MAX_ROWS; MaxIdentifier is MAX_IDENTIFIER.
const (
	SeedMaxRows      = 50_000
	MaxIdentifier    = 63
	maxPrefix        = 20
	cteLen           = len("__dbt__cte__")
	MaxUpstreamDepth = 8
)

// dialects is _DIALECT: the flow's engine → the dialect of its free text.
var dialects = map[string]string{"polars": "polars", "duckdb": "duckdb", "chdb": "clickhouse", "clickhouse": "clickhouse",
	"bigquery": "bigquery"}

// notQuotable is _NOT_QUOTABLE.
var notQuotable = map[string]map[string]string{
	"dbt-postgres":   {"source": "\"'", "relation": "\"'", "schema": "\"'\\"},
	"dbt-duckdb":     {"source": "\"'", "relation": "\"", "schema": "\""},
	"dbt-clickhouse": {"source": "`\\'", "relation": "`\\'", "schema": "`\\'"},
}

var (
	columnTests = map[string]bool{"not_null": true, "unique": true, "accepted_values": true}
	modelTests  = map[string]bool{"range": true, "pattern": true, "row_count": true, "expression": true}
)

// Error is DbtExportError: the export refused. Message is the English text (str(e)), Code and
// Params say it in the reader's language (ErrorMessage); ToDeclare are the sources a target
// needs declared by the team's own project.
type Error struct {
	Message   string
	Code      string
	Params    pyjson.Object
	ToDeclare []string
}

func (e *Error) Error() string { return e.Message }

func refuse(message, code string, params ...any) {
	p := pyjson.Object{}
	for i := 0; i+1 < len(params); i += 2 {
		p = append(p, pyjson.KV{Key: params[i].(string), Value: params[i+1]})
	}
	pyval.Throw(&Error{Message: message, Code: code, Params: p})
}

// ── the rows ────────────────────────────────────────────────────────────────

// Flow is the `flows` row as the export reads it.
type Flow struct {
	ID               int64
	Name             string
	Description      string
	ProjectID        int64
	OwnerID          *int64
	Definition       string
	Engine           string
	ProductionEngine *string
}

// Datasource is a `datasources` row.
type Datasource struct {
	ID                 int64
	Name, Description  string
	ProjectID          int64
	Bucket, Key        string
	Rows               *int64
	Columns            string
	ColumnDescriptions string
	Kind               string
	FlowID             *int64
	ConnectionID       *int64
	SourceType         *string
	SourceRef          *string
}

// Connection is a `connections` row.
type Connection struct {
	ID                                          int64
	Name, DBType, Host                          string
	Port                                        *int64
	Username, Database, DBSchema, Extra, Secret string
}

const flowColumns = `id, name, description, project_id, owner_id, definition, engine, production_engine`

func scanFlow(row pgx.Row) (*Flow, error) {
	var f Flow
	err := row.Scan(&f.ID, &f.Name, &f.Description, &f.ProjectID, &f.OwnerID, &f.Definition, &f.Engine, &f.ProductionEngine)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &f, err
}

// ReadFlow is session.get(Flow, id).
func ReadFlow(ctx context.Context, q Querier, id int64) (*Flow, error) {
	return scanFlow(q.QueryRow(ctx, `SELECT `+flowColumns+` FROM flows WHERE id = $1`, id))
}

const datasourceColumns = `id, name, description, project_id, bucket, key, rows, columns, column_descriptions, kind, flow_id,
	connection_id, source_type, source_ref`

func scanDatasource(row pgx.Row) (*Datasource, error) {
	var d Datasource
	err := row.Scan(&d.ID, &d.Name, &d.Description, &d.ProjectID, &d.Bucket, &d.Key, &d.Rows, &d.Columns, &d.ColumnDescriptions,
		&d.Kind, &d.FlowID, &d.ConnectionID, &d.SourceType, &d.SourceRef)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &d, err
}

func scanConnection(row pgx.Row) (*Connection, error) {
	var c Connection
	err := row.Scan(&c.ID, &c.Name, &c.DBType, &c.Host, &c.Port, &c.Username, &c.Database, &c.DBSchema, &c.Extra)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &c, err
}

// ── names ───────────────────────────────────────────────────────────────────

// isAlnum is str.isalnum() of one character.
func isAlnum(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) }

// isDigit is str.isdigit() of one character: the decimal digits and the other digits
// (superscripts, circled…).
func isDigit(r rune) bool {
	return unicode.IsDigit(r) || (unicode.Is(unicode.No, r) && strings.ContainsRune("¹²³⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉", r)) ||
		(r >= 0x2460 && r <= 0x2468) || (r >= 0x2474 && r <= 0x247C) || (r >= 0x2488 && r <= 0x2490) ||
		(r >= 0x24F5 && r <= 0x24FD) || r == 0x24EA || r == 0x24FF || (r >= 0x2776 && r <= 0x277E) ||
		(r >= 0x2780 && r <= 0x2788) || (r >= 0x278A && r <= 0x2792)
}

// pyLower is str.lower(): Go's ToLower with Python's one full mapping that is not a single
// character (İ is i and a combining dot).
func pyLower(s string) string {
	return strings.ToLower(strings.ReplaceAll(s, "\u0130", "i\u0307"))
}

// Slug is _slug.
func Slug(s any) string {
	var b strings.Builder
	for _, r := range pyLower(pyval.Str(s)) {
		if isAlnum(r) || r == '_' {
			b.WriteRune(r)
		} else {
			b.WriteByte('_')
		}
	}
	out := strings.Trim(b.String(), "_")
	if out == "" {
		out = "x"
	}
	if r, _ := utf8.DecodeRuneInString(out); isDigit(r) {
		return "m_" + out
	}
	return out
}

// validUTF8Prefix is bytes[:n].decode("utf-8", "ignore") of a text cut in the middle of a
// character: the broken tail dropped.
func validUTF8Prefix(s string, n int) string {
	if n >= len(s) {
		return s
	}
	if n < 0 {
		n = 0
	}
	cut := s[:n]
	var b strings.Builder
	for len(cut) > 0 {
		r, size := utf8.DecodeRuneInString(cut)
		if r == utf8.RuneError && size <= 1 {
			cut = cut[1:]
			continue
		}
		b.WriteString(cut[:size])
		cut = cut[size:]
	}
	return b.String()
}

// within is _within: `name` within `cap` bytes, a longer one ending with a short hash of the whole.
func within(name string, cap int) string {
	if len(name) <= cap {
		return name
	}
	sum := sha256.Sum256([]byte(name))
	fingerprint := hex.EncodeToString(sum[:])[:8]
	head := strings.TrimRight(validUTF8Prefix(name, cap-len(fingerprint)-1), "_")
	return head + "_" + fingerprint
}

var nonFolder = regexp.MustCompile(`[^a-z0-9]+`)

// DefaultFolder is _default_folder: the existing-project package's folder when the dialog
// leaves it empty.
func DefaultFolder(name string) string {
	var b strings.Builder
	for _, r := range norm.NFKD.String(name) {
		if r < 0x80 {
			b.WriteRune(r)
		}
	}
	s := strings.Trim(nonFolder.ReplaceAllString(strings.ToLower(b.String()), "_"), "_")
	if len(s) > 40 {
		s = s[:40]
	}
	s = strings.TrimRight(s, "_")
	if s == "" {
		return "tabularia"
	}
	return s
}

func defaultSchema(c *Connection) string {
	if c.DBSchema != "" {
		return c.DBSchema
	}
	if c.DBType == "clickhouse" || c.DBType == "mysql" || c.DBType == "mariadb" {
		return c.Database
	}
	return "public"
}

// asPostgres is _as_postgres: the first 63 bytes, whole characters.
func asPostgres(name string) string { return validUTF8Prefix(name, MaxIdentifier) }

// rebuildable is _rebuildable.
func rebuildable(alias string) bool {
	for _, s := range []string{"__dbt_tmp", "__dbt_backup"} {
		r := []rune(alias)
		if n := MaxIdentifier - len(s); len(r) > n {
			r = r[:n]
		}
		if len(string(r)+s) > MaxIdentifier {
			return false
		}
	}
	return true
}

// quotable is _quotable.
func quotable(name, adapter, where string) {
	for _, char := range notQuotable[adapter][where] {
		if strings.ContainsRune(name, char) {
			c := string(char)
			refuse(fmt.Sprintf("the name «%s» has the character %s, which %s cannot quote in a relation name", name, c, adapter),
				"name_not_quotable", "name", name, "character", c, "adapter", adapter)
		}
	}
}

// parseRef is _parse_ref: `schema.table` → (schema, table); `table` → (default, table).
func parseRef(sourceRef, def string) (string, string) {
	parts := strings.Split(sourceRef, ".")
	if len(parts) >= 2 {
		return parts[len(parts)-2], parts[len(parts)-1]
	}
	return def, parts[len(parts)-1]
}

func loadsOr(text, def string) any {
	if text == "" {
		text = def
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		pyval.Raise("JSONDecodeError", "%s", err.Error())
	}
	return v
}

// columns is _columns.
func columns(ds *Datasource) []any {
	out := []any{}
	if ds == nil {
		return out
	}
	for _, c := range pyval.Iter(loadsOr(ds.Columns, "[]")) {
		if n := pyval.Get(c, "name"); pyval.Truthy(n) {
			out = append(out, n)
		}
	}
	return out
}

// dtypes is _dtypes.
func dtypes(ds *Datasource) pyjson.Object {
	out := pyjson.Object{}
	if ds == nil {
		return out
	}
	for _, c := range pyval.Iter(loadsOr(ds.Columns, "[]")) {
		if n := pyval.Get(c, "name"); pyval.Truthy(n) {
			out = out.Set(pyval.JSONKey(n), pyval.Get(c, "dtype"))
		}
	}
	return out
}

// descriptions is _descriptions.
func descriptions(ds *Datasource) pyjson.Object {
	out := pyjson.Object{}
	if ds == nil {
		return out
	}
	text := ds.ColumnDescriptions
	if text == "" {
		text = "{}"
	}
	d, err := pyjson.Loads(text)
	if err != nil {
		return out
	}
	o, ok := d.(pyjson.Object)
	if !ok {
		pyval.Raise("AttributeError", "'%s' object has no attribute 'items'", pyval.TypeName(d))
	}
	for _, kv := range o {
		if pyval.Truthy(kv.Value) {
			out = out.Set(kv.Key, pyval.Str(kv.Value))
		}
	}
	return out
}

func descOf(descr pyjson.Object, column any) string {
	if s, ok := column.(string); ok {
		if v, ok := descr.Get(s); ok {
			return v.(string)
		}
	}
	return ""
}

// ── the options ─────────────────────────────────────────────────────────────

// SourceOption is SourceOption: a source as the data team's project wants it.
type SourceOption struct {
	Name     *string
	Declared bool
}

// Options is DbtOptions, the download dialog's choices.
type Options struct {
	Target, Package, Folder, Prefix string
	Layers                          bool
	SourceKeys                      []string // the dict's order
	Sources                         map[string]SourceOption
	Schema                          *string
	MaterializationKeys             []string
	Materializations                map[string]string
	Tests, Emails                   bool
	AIDescriptions, AITranslations  bool
}

// DefaultOptions is DbtOptions(target=target).
func DefaultOptions(target string) *Options {
	return &Options{Target: target, Package: "project", Sources: map[string]SourceOption{}, Materializations: map[string]string{},
		Tests: true, Emails: true}
}

// Dump is model_dump(by_alias=True), for the audit.
func (o *Options) Dump() pyjson.Object {
	sources := pyjson.Object{}
	for _, k := range o.SourceKeys {
		s := o.Sources[k]
		var name any
		if s.Name != nil {
			name = *s.Name
		}
		sources = append(sources, pyjson.KV{Key: k, Value: pyjson.Object{{Key: "name", Value: name}, {Key: "declared", Value: s.Declared}}})
	}
	mats := pyjson.Object{}
	for _, k := range o.MaterializationKeys {
		mats = append(mats, pyjson.KV{Key: k, Value: o.Materializations[k]})
	}
	var schema any
	if o.Schema != nil {
		schema = *o.Schema
	}
	return pyjson.Object{{Key: "target", Value: o.Target}, {Key: "package", Value: o.Package}, {Key: "folder", Value: o.Folder},
		{Key: "prefix", Value: o.Prefix}, {Key: "layers", Value: o.Layers}, {Key: "sources", Value: sources},
		{Key: "schema", Value: schema}, {Key: "materializations", Value: mats}, {Key: "tests", Value: o.Tests},
		{Key: "emails", Value: o.Emails}, {Key: "ai_descriptions", Value: o.AIDescriptions},
		{Key: "ai_translations", Value: o.AITranslations}}
}

// byPackage is _by_package: what the package makes meaningless dropped.
func byPackage(o *Options) *Options {
	c := *o
	if c.Package == "folder" && c.Schema != nil {
		c.Schema = nil
	}
	if c.Package == "project" {
		any := false
		for _, s := range c.Sources {
			any = any || s.Declared
		}
		if any {
			src := map[string]SourceOption{}
			for k, s := range c.Sources {
				s.Declared = false
				src[k] = s
			}
			c.Sources = src
		}
	}
	return &c
}

func sourceKey(connID int64, schema string) string { return fmt.Sprintf("%d:%s", connID, schema) }

func outputKey(flowID int64, nodeID any) string {
	return fmt.Sprintf("%d:%s", flowID, pyval.Str(nodeID))
}

// names is _Names: model names unique in the project, the dialog's prefix in front.
type names struct {
	used   map[string]bool
	prefix string
}

func (n *names) new(base any, suffix string, ephemeral bool) string {
	cap := MaxIdentifier - maxPrefix
	if ephemeral {
		cap -= cteLen
	}
	name := within(Slug(base), cap)
	if n.used[name] && suffix != "" {
		name = within(Slug(pyval.Str(base)+"_"+suffix), cap)
	}
	cand := name
	for i := 2; n.used[cand]; i++ {
		cand = within(fmt.Sprintf("%s_%d", name, i), cap)
	}
	n.used[cand] = true
	return n.prefix + cand
}

// ── the project ─────────────────────────────────────────────────────────────

type table struct {
	ds            *Datasource
	conn          *Connection
	schema, table string
}

type withoutSchema struct {
	conn   *Connection
	tables []string
}

type flowContext struct {
	definition any
	byID       *pyval.Dict
	published  *pyval.Dict // (name, folder) → the Output's node id
	duplicates *pyval.Dict
	suffix     string
	origin     pyjson.Object
	dialect    any
}

// output is one Output being written: its run body, node, source reference, chain.
type output struct {
	req  pyjson.Object
	node any
	rif  pyjson.Object
	key  any
	ops  []any
	ids  []any
}

// Project is _Project: the export's state while walking the flow and the upstream flows.
type Project struct {
	ctx        context.Context
	q          Querier
	bucket     string
	target     string
	options    *Options
	Outputs    []any
	exportedBy *string
	names      *names
	models     []any // *pyjson.Object
	seeds      []any
	tables     *pyval.Dict // key → *table
	FlowsSeen  map[int64]bool
	note       []any
	fileNames  *pyval.Dict
	fileRows   *pyval.Dict // parquet key → the rows its file node says it has
	references *pyval.Dict
	root       *int64
	dest       map[string]pyjson.Object
	contexts   map[int64]*flowContext
	running    *pyval.Dict
	waiting    *pyval.Dict
	done       *pyval.Dict
	withoutSch *pyval.Dict // conn id → *withoutSchema
	sourceBase *pyval.Dict // (conn id, schema) → name
}

func newProject(ctx context.Context, q Querier, bucket, target string, exportedBy *string, options *Options) *Project {
	o := options
	if o == nil {
		o = DefaultOptions(target)
	}
	p := &Project{ctx: ctx, q: q, bucket: bucket, target: target, options: byPackage(o), Outputs: []any{},
		tables: pyval.NewDict(), FlowsSeen: map[int64]bool{}, note: []any{}, fileNames: pyval.NewDict(), fileRows: pyval.NewDict(), references: pyval.NewDict(),
		dest: map[string]pyjson.Object{}, contexts: map[int64]*flowContext{}, running: pyval.NewDict(), waiting: pyval.NewDict(),
		done: pyval.NewDict(), withoutSch: pyval.NewDict(), sourceBase: pyval.NewDict()}
	if options == nil || options.Emails {
		p.exportedBy = exportedBy
	}
	p.names = &names{used: map[string]bool{}, prefix: p.options.Prefix}
	return p
}

func (p *Project) row(sql string, args ...any) pgx.Row { return p.q.QueryRow(p.ctx, sql, args...) }

func (p *Project) datasource(sql string, args ...any) *Datasource {
	ds, err := scanDatasource(p.row(`SELECT `+datasourceColumns+` FROM datasources `+sql, args...))
	pyval.Check(err)
	return ds
}

func (p *Project) connection(id int64) *Connection {
	c, err := scanConnection(p.row(`SELECT id, name, db_type, host, port, username, database, db_schema, extra FROM connections WHERE id = $1`, id))
	pyval.Check(err)
	return c
}

func (p *Project) flow(id int64) *Flow {
	f, err := ReadFlow(p.ctx, p.q, id)
	pyval.Check(err)
	return f
}

func (p *Project) layout() pyjson.Object {
	return pyjson.Object{{Key: "package", Value: p.options.Package}, {Key: "folder", Value: p.options.Folder},
		{Key: "layers", Value: p.options.Layers}}
}

// origin is _origin: which flow a model comes from.
func (p *Project) origin(f *Flow) pyjson.Object {
	var version *int64
	var created *time.Time
	err := p.row(`SELECT version, created_at FROM flow_versions WHERE flow_id = $1 ORDER BY version DESC LIMIT 1`, f.ID).Scan(&version, &created)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		pyval.Throw(err)
	}
	var folder, owner any
	var s string
	if err := p.row(`SELECT name FROM projects WHERE id = $1`, f.ProjectID).Scan(&s); err == nil {
		folder = s
	} else if !errors.Is(err, pgx.ErrNoRows) {
		pyval.Throw(err)
	}
	if f.OwnerID != nil && *f.OwnerID != 0 {
		if err := p.row(`SELECT email FROM users WHERE id = $1`, *f.OwnerID).Scan(&s); err == nil {
			owner = s
		} else if !errors.Is(err, pgx.ErrNoRows) {
			pyval.Throw(err)
		}
	}
	var v, saved any
	if version != nil {
		v = *version
		if created != nil {
			saved = created.Format("2006-01-02 15:04") + " UTC"
		}
	}
	return pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "flow_id", Value: f.ID}, {Key: "flow_version", Value: v},
		{Key: "version_saved_at", Value: saved}, {Key: "folder", Value: folder}, {Key: "owner", Value: owner}}
}

// trace is _trace: the model's meta (only facts of the flow) and tags.
func (p *Project) trace(model *pyjson.Object, origin pyjson.Object, extra ...any) *pyjson.Object {
	meta := append(pyjson.Object{}, origin...)
	for i := 0; i+1 < len(extra); i += 2 {
		if pyval.Truthy(extra[i+1]) {
			meta = meta.Set(extra[i].(string), extra[i+1])
		}
	}
	if !p.options.Emails {
		meta = del(meta, "owner")
	}
	set(model, "meta", meta)
	if f := origin.Value("flow"); pyval.Truthy(f) {
		set(model, "tags", []any{"tabularia", Slug(f)})
	} else {
		set(model, "tags", []any{"tabularia"})
	}
	return model
}

func set(o *pyjson.Object, k string, v any) { *o = o.Set(k, v) }

func del(o pyjson.Object, k string) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range o {
		if kv.Key != k {
			out = append(out, kv)
		}
	}
	return out
}

func get(o *pyjson.Object, k string) any { return o.Value(k) }

// ── sources ─────────────────────────────────────────────────────────────────

// source classifies a parquet key: a DB table, a SQL query, another flow's output or a file.
func (p *Project) source(key any, depth int) pyjson.Object {
	if r, ok := p.references.Lookup(key); ok {
		return r.(pyjson.Object)
	}
	k, ok := key.(string)
	if !ok {
		pyval.Throw(fmt.Errorf("a parquet key that is not text: %v", key)) // Python's database refuses the comparison
	}
	ds := p.datasource(`WHERE key = $1 LIMIT 1`, k)
	var ref pyjson.Object
	switch {
	case ds != nil && ds.Kind == "database" && ds.SourceType != nil &&
		pystr.In(*ds.SourceType, "sharepoint", "sftp", "api", "s3", "gsheets"):
		ref = pyjson.Object{{Key: "seed", Value: p.seed(ds, key)}}
	case ds != nil && ds.Kind == "database":
		var conn *Connection
		if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
			conn = p.connection(*ds.ConnectionID)
		}
		if conn == nil {
			refuse("the source «"+ds.Name+"» has no valid connection", "source_no_connection", "source", ds.Name)
		}
		if ds.SourceType != nil && *ds.SourceType == "sql" {
			ref = pyjson.Object{{Key: "model", Value: p.queryModel(ds, conn)}}
		} else {
			sr := ""
			if ds.SourceRef != nil {
				sr = *ds.SourceRef
			}
			schema, tbl := parseRef(sr, defaultSchema(conn))
			if !strings.Contains(sr, ".") {
				p.withoutSchemaOf(conn, tbl)
			}
			p.tables.Set(key, &table{ds, conn, schema, tbl})
			ref = pyjson.Object{{Key: "key", Value: key}}
		}
	case ds != nil && ds.Kind == "flow" && ds.FlowID != nil && *ds.FlowID != 0:
		ref = pyjson.Object{{Key: "model", Value: p.upstreamFlow(ds, depth)}}
	default:
		ref = pyjson.Object{{Key: "seed", Value: p.seed(ds, key)}}
	}
	p.references.Set(key, ref)
	return ref
}

func (p *Project) withoutSchemaOf(conn *Connection, tbl string) {
	if conn.DBType == "postgresql" && conn.DBSchema == "" {
		ws := p.withoutSch.SetDefault(conn.ID, &withoutSchema{conn: conn}).(*withoutSchema)
		for _, t := range ws.tables {
			if t == tbl {
				return
			}
		}
		ws.tables = append(ws.tables, tbl)
	}
}

func (p *Project) notesWithoutSchema() {
	for _, v := range p.withoutSch.Values() {
		ws := v.(*withoutSchema)
		listed := make([]string, len(ws.tables))
		for i, t := range ws.tables {
			listed[i] = "`" + t + "`"
		}
		p.note = append(p.note, "The connection «"+ws.conn.Name+"» has no Schema: Tabularia reads and writes "+strings.Join(listed, ", ")+
			" through the search_path of the database role (by default a schema named like the role, when there is one, then `public`), "+
			"while this project names them in `public`. They are the same tables unless the role has a schema of its own or another "+
			"search_path: then set the Schema of the connection in Tabularia and export again.")
	}
}

func (p *Project) columnItems(ds *Datasource, withType bool) []any {
	descr := descriptions(ds)
	var types pyjson.Object
	if withType {
		types = dtypes(ds)
	}
	out := []any{}
	for _, c := range columns(ds) {
		item := pyjson.Object{{Key: "name", Value: c}, {Key: "description", Value: descOf(descr, c)}}
		if withType {
			item = append(item, pyjson.KV{Key: "dtype", Value: types.Value(pyval.JSONKey(c))})
		}
		out = append(out, item)
	}
	return out
}

// queryModel is _query_model: a datasource from a QUERY becomes an ephemeral model.
func (p *Project) queryModel(ds *Datasource, conn *Connection) string {
	name := p.names.new("src_"+ds.Name, "", true)
	sr := ""
	if ds.SourceRef != nil {
		sr = *ds.SourceRef
	}
	var sql string
	if p.target == "duckdb" {
		fed, ok := federable[conn.DBType]
		if !ok {
			pyval.Throw(notFederable(conn))
		}
		p.tables.SetDefault(fmt.Sprintf("__conn_%d", conn.ID), &table{ds, conn, "", ""})
		query := strings.ReplaceAll(strings.TrimRight(pystr.Strip(sr), ";"), "'", "''")
		sql = fmt.Sprintf("SELECT * FROM %s('db_%d', '%s')", fed.function, conn.ID, query)
	} else {
		p.tables.SetDefault(fmt.Sprintf("__conn_%d", conn.ID), &table{ds, conn, "", ""})
		sql = strings.TrimRight(pystr.Strip(sr), ";")
		if p.target == "native" {
			p.note = append(p.note, "The model `"+name+"` is the SQL query of the datasource «"+ds.Name+"», as written: its table references are not `source()` macros.")
		}
	}
	var translate any
	if p.target == "clickhouse" {
		translate = pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "label", Value: ds.Name}}
	}
	model := &pyjson.Object{{Key: "name", Value: name}, {Key: "kind", Value: "raw"}, {Key: "materialized", Value: "ephemeral"},
		{Key: "raw_sql", Value: sql}, {Key: "translate", Value: translate}, {Key: "columns", Value: p.columnItems(ds, true)},
		{Key: "description", Value: "Datasource «" + ds.Name + "»: a SQL query on " + conn.Name + "."}, {Key: "tests", Value: []any{}}}
	p.models = append(p.models, p.trace(model, pyjson.Object{{Key: "datasource", Value: ds.Name}, {Key: "datasource_id", Value: ds.ID}}))
	return name
}

// upstreamFlow is _upstream_flow: another flow's output brings in only the branch read here.
func (p *Project) upstreamFlow(ds *Datasource, depth int) string {
	f := p.flow(*ds.FlowID)
	if f == nil {
		refuse("the source «"+ds.Name+"» is the output of a flow that no longer exists", "upstream_missing", "source", ds.Name)
	}
	if depth >= MaxUpstreamDepth {
		refuse(fmt.Sprintf("too many upstream flows in a chain (more than %d): the source «%s» cannot be included", MaxUpstreamDepth, ds.Name),
			"upstream_too_deep", "max", int64(MaxUpstreamDepth), "source", ds.Name)
	}
	ctx := p.context(f)
	slot := pyval.Tuple{ds.Name, ds.ProjectID}
	node, ok := ctx.published.Lookup(slot)
	if !ok {
		seen := map[string]bool{}
		var listed []string
		for _, k := range ctx.published.Keys() {
			s := "«" + pyval.Str(k.(pyval.Tuple)[0]) + "»"
			if !seen[s] {
				seen[s] = true
				listed = append(listed, s)
			}
		}
		listing := strings.Join(listed, ", ")
		if listing == "" {
			listing = "—"
		}
		refuse("the flow «"+f.Name+"» has no output that publishes «"+ds.Name+"» (it publishes "+listing+"): the datasource "+
			"was renamed or moved after the flow published it, or the flow's Output changed",
			"upstream_no_output", "flow", f.Name, "source", ds.Name, "published", listing)
	}
	if ctx.duplicates.Has(slot) {
		refuse("in the flow «"+f.Name+"» more than one Output publishes «"+ds.Name+"»", "upstream_output_ambiguous",
			"flow", f.Name, "source", ds.Name)
	}
	slotKey := pyval.Tuple{f.ID, node}
	if name, ok := p.done.Lookup(slotKey); ok {
		return name.(string)
	}
	if p.running.Has(slotKey) {
		refuse("cycle between flows: «"+f.Name+"» depends on itself through «"+ds.Name+"»", "flow_cycle", "flow", f.Name, "source", ds.Name)
	}
	if w, ok := p.waiting.Lookup(slotKey); ok {
		p.writeModels(f, []*output{w.(*output)})
	} else {
		p.include(f, depth+1, []any{node})
	}
	name, ok := p.done.Lookup(slotKey)
	if !ok {
		pyval.Raise("KeyError", "%s", pyjson.Repr([]any{f.ID, node}))
	}
	return name.(string)
}

// seed is _seed: a file, or a source dbt cannot read live, becomes a CSV seed.
func (p *Project) seed(ds *Datasource, key any) string {
	if ds != nil && ds.Rows != nil && *ds.Rows > SeedMaxRows {
		tooLarge(ds.Name, *ds.Rows)
	}
	if ds == nil {
		// a file loaded in the editor: the rows its node says it has (the plan offered the targets and
		// the download failed, when the engine counted them)
		if n, ok := fileRows(p.fileRows.Get(key)); ok && n > SeedMaxRows {
			tooLarge(pyval.Str(pyval.Or(p.fileNames.Get(key), key)), n)
		}
	}
	var base string
	if ds != nil {
		base = ds.Name
	} else {
		var b any = p.fileNames.Get(key)
		if !pyval.Truthy(b) {
			k, ok := key.(string)
			if !ok {
				pyval.Raise("AttributeError", "'%s' object has no attribute 'rsplit'", pyval.TypeName(key))
			}
			b = k[strings.LastIndex(k, "/")+1:]
		}
		s := b.(string)
		if i := strings.LastIndex(s, "."); i >= 0 {
			s = s[:i]
		}
		base = s
	}
	name := p.names.new("seed_"+base, "", false)
	bucket := p.bucket
	var dsID any
	var description string
	if ds != nil {
		if ds.Bucket != "" {
			bucket = ds.Bucket
		}
		dsID = ds.ID
		description = "Datasource «" + ds.Name + "»"
	} else {
		description = "File " + pyval.Str(pyval.Or(p.fileNames.Get(key), key))
	}
	p.seeds = append(p.seeds, &pyjson.Object{{Key: "name", Value: name}, {Key: "bucket", Value: bucket}, {Key: "key", Value: key},
		{Key: "datasource_id", Value: dsID},
		{Key: "description", Value: description + ", exported as a CSV seed: the data is a snapshot, not a live source."},
		{Key: "columns", Value: p.columnItems(ds, false)}})
	p.note = append(p.note, "The seed `"+name+"` is a copy of the data at export time: reload it (`dbt seed`) when it changes.")
	return name
}

// tooLarge refuses a seed over SeedMaxRows; source: the datasource or the file, as the user named it.
func tooLarge(source string, n int64) {
	rows := thousands(n)
	max := thousands(SeedMaxRows)
	refuse(fmt.Sprintf("the source «%s» has %s rows: too many for a dbt seed (at most %s). Import it into a database and use that as the source.",
		source, rows, max), "seed_too_large", "source", source, "rows", rows, "max", max)
}

// fileRows is a file node's `rows`, when it is a number.
func fileRows(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case float64:
		if x > 1e18 {
			return SeedMaxRows + 1, true
		}
		return int64(x), x == x
	case pyjson.Int:
		return SeedMaxRows + 1, true // past int64: over the cap all the same
	}
	return 0, false
}

// thousands is f"{n:,}".
func thousands(n int64) string {
	s := strconv.FormatInt(n, 10)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	var b strings.Builder
	for i, r := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	if neg {
		return "-" + b.String()
	}
	return b.String()
}

// ── flows ───────────────────────────────────────────────────────────────────

func (p *Project) includeFlow(f *Flow) {
	if p.root == nil {
		id := f.ID
		p.root = &id
	}
	p.include(f, 0, nil)
}

// context is _context: what the export needs of a flow, read once.
func (p *Project) context(f *Flow) *flowContext {
	if c, ok := p.contexts[f.ID]; ok {
		return c
	}
	c := &flowContext{}
	if err := pyval.Catch(func() {
		text := f.Definition
		if text == "" {
			text = "{}"
		}
		def, err := pyjson.Loads(text)
		if err != nil {
			pyval.Raise("JSONDecodeError", "%s", jsonDecodeMessage(text, err))
		}
		c.definition = def
		c.byID = pyval.NewDict()
		for _, n := range pyval.Iter(pyval.Or(pyval.Get(def, "nodes"), []any{})) {
			c.byID.Set(pyval.Index(n, "id"), n)
		}
		c.published, c.duplicates = pyval.NewDict(), pyval.NewDict()
		for _, n := range c.byID.Values() {
			data := pyval.Or(pyval.Get(n, "data"), pyjson.Object{})
			if pyval.Eq(pyval.Get(n, "type"), "source") && pyval.Truthy(pyval.Get(data, "parquetKey")) && pyval.Truthy(pyval.Get(data, "filename")) {
				p.fileNames.Set(pyval.Index(data, "parquetKey"), pyval.Str(pyval.Index(data, "filename")))
				p.fileRows.Set(pyval.Index(data, "parquetKey"), pyval.Get(data, "rows"))
			}
			if pyval.Eq(pyval.Get(n, "type"), "output") && pyval.Eq(pyval.Or(pyval.Get(data, "destType"), "datasource"), "datasource") &&
				pyval.Strip(pyval.Or(pyval.Get(data, "name"), "")) != "" {
				slot := pyval.Tuple{pyval.Strip(pyval.Or(pyval.Get(data, "name"), "")), pyval.Get(data, "projectId")}
				if c.published.Has(slot) {
					c.duplicates.Set(slot, true)
				}
				c.published.SetDefault(slot, pyval.Index(n, "id"))
			}
		}
	}); err != nil {
		if pe, ok := err.(*pyval.Error); ok && isCaught(pe) {
			pyval.Throw(invalidDefinition(f, pe))
		}
		pyval.Throw(err)
	}
	c.suffix = Slug(f.Name)
	c.origin = p.origin(f)
	eng := f.Engine
	if f.ProductionEngine != nil && *f.ProductionEngine != "" {
		eng = *f.ProductionEngine
	}
	if d, ok := dialects[eng]; ok {
		c.dialect = d
	}
	p.contexts[f.ID] = c
	return c
}

// isCaught is `except (KeyError, TypeError, ValueError, AttributeError)` (JSONDecodeError is a
// ValueError).
func isCaught(e *pyval.Error) bool {
	switch e.Type {
	case "KeyError", "TypeError", "ValueError", "AttributeError", "JSONDecodeError":
		return true
	}
	return false
}

// jsonDecodeMessage is json.JSONDecodeError's message for a text that does not parse: the
// common case of a text that is not JSON at all at its first character.
func jsonDecodeMessage(text string, err error) string {
	msg := err.Error()
	if msg == "Expecting value" {
		i := 0
		for i < len(text) && strings.ContainsRune(" \t\n\r", rune(text[i])) {
			i++
		}
		line := strings.Count(text[:i], "\n") + 1
		col := i - strings.LastIndex(text[:i], "\n")
		return fmt.Sprintf("Expecting value: line %d column %d (char %d)", line, col, utf8.RuneCountInString(text[:i]))
	}
	return msg
}

// include is _include: the models of the flow's Outputs (every one, or only `only`).
func (p *Project) include(f *Flow, depth int, only []any) {
	p.FlowsSeen[f.ID] = true
	ctx := p.context(f)
	p.withoutScript(f, ctx.definition, only)

	resolveDS := func(dsID any) (*flowresolver.Source, error) {
		var id int64
		if err := pyval.Catch(func() { id = pyval.Int(dsID) }); err != nil {
			if pe, ok := err.(*pyval.Error); ok && (pe.Type == "TypeError" || pe.Type == "ValueError") {
				return nil, nil
			}
			return nil, err
		}
		var ds *Datasource
		if err := pyval.Catch(func() { ds = p.datasource(`WHERE id = $1`, id) }); err != nil {
			return nil, err
		}
		if ds != nil && ds.Key != "" {
			return &flowresolver.Source{Bucket: ds.Bucket, Key: ds.Key}, nil
		}
		return nil, nil
	}

	var outputsDef []any
	for _, n := range pyval.Iter(pyval.Or(pyval.Get(ctx.definition, "nodes"), []any{})) {
		if pyval.IsDict(n) && pyval.Eq(pyval.Get(n, "type"), "output") {
			outputsDef = append(outputsDef, n)
		}
	}
	toResolve := only
	email := false
	for _, n := range outputsDef {
		if pyval.Eq(pyval.Get(pyval.Or(pyval.Get(n, "data"), pyjson.Object{}), "destType"), "email") {
			email = true
			break
		}
	}
	if email {
		toResolve = []any{}
		for _, n := range outputsDef {
			if !pyval.Eq(pyval.Get(pyval.Or(pyval.Get(n, "data"), pyjson.Object{}), "destType"), "email") &&
				(only == nil || inList(pyval.Index(n, "id"), only)) {
				toResolve = append(toResolve, pyval.Index(n, "id"))
			}
		}
	}
	chains, err := flowresolver.ResolveOutputChains(ctx.definition, resolveDS, p.bucket, toResolve)
	if err != nil {
		var fe *flowresolver.Error
		var pe *pyval.Error
		switch {
		case errors.As(err, &fe):
			pyval.Throw(readError(f, fe))
		case errors.As(err, &pe) && isCaught(pe):
			pyval.Throw(invalidDefinition(f, pe))
		}
		pyval.Throw(err)
	}

	var outputs []*output
	exportable := 0
	for _, c := range chains {
		req, node := c.Body, c.Node
		dest := pyval.Or(req.Value("destination"), pyjson.Object{})
		if pyval.Truthy(req.Value("email")) || !inAny(pyval.Get(dest, "type"), nil, "database", "s3", "sftp") {
			continue
		}
		exportable++
		slot := pyval.Tuple{f.ID, pyval.Get(pyval.Or(node, pyjson.Object{}), "id")}
		if p.done.Has(slot) || p.waiting.Has(slot) || p.running.Has(slot) {
			continue
		}
		key := pyval.Index(req, "input_key")
		p.running.Set(slot, true)
		ref := p.source(key, depth)
		p.nestedSources(pyval.Or(req.Value("operations"), []any{}), depth)
		p.running.Delete(slot)
		ops := append([]any{}, pyval.Iter(pyval.Or(req.Value("operations"), []any{}))...)
		ids := append([]any{}, c.OpIDs...)
		var pubKeys []any
		for _, k := range pyval.Iter(pyval.Or(pyval.Get(pyval.Or(req.Value("publish"), pyjson.Object{}), "sort_keys"), []any{})) {
			if pyval.Truthy(k) && pyval.Strip(k) != "" {
				pubKeys = append(pubKeys, pyval.Strip(k))
			}
		}
		if len(pubKeys) > 0 {
			ops = append(ops, pyjson.Object{{Key: "type", Value: "sort"}, {Key: "params", Value: pyjson.Object{
				{Key: "by", Value: pubKeys}, {Key: "ignore_missing", Value: true}}}})
			ids = append(ids, nil)
		}
		u := &output{req: req, node: node, rif: ref, key: key, ops: ops, ids: ids}
		p.waiting.Set(slot, u)
		outputs = append(outputs, u)
	}
	if only == nil && exportable == 0 {
		refuse("the flow «"+f.Name+"» has no exportable outputs (only email?)", "no_outputs", "flow", f.Name)
	}
	var still []*output
	for _, u := range outputs {
		if p.waiting.Has(pyval.Tuple{f.ID, nodeID(u.node)}) {
			still = append(still, u)
		}
	}
	p.writeModels(f, still)
}

func nodeID(node any) any { return pyval.Get(pyval.Or(node, pyjson.Object{}), "id") }

func inList(v any, list []any) bool {
	for _, x := range list {
		if pyval.Eq(v, x) {
			return true
		}
	}
	return false
}

func inAny(v any, options ...any) bool { return inList(v, options) }

// withoutScript is _without_script: an Output that reads what a Script or Container node
// writes refuses the export; one no exported Output reads from is left out, with a note.
func (p *Project) withoutScript(f *Flow, definition any, only []any) {
	nodes := pyval.NewDict()
	for _, n := range pyval.Iter(pyval.Or(pyval.Get(definition, "nodes"), []any{})) {
		if o, ok := n.(pyjson.Object); ok {
			if id, has := o.Get("id"); has {
				nodes.Set(id, n)
			}
		}
	}
	var scripts []any
	for i, k := range nodes.Keys() {
		if inAny(pyval.Get(nodes.Values()[i], "type"), "script", "container") {
			scripts = append(scripts, k)
		}
	}
	if len(scripts) == 0 {
		return
	}
	edges := pyval.Iter(pyval.Or(pyval.Get(definition, "edges"), []any{}))
	var outs []any
	for _, n := range nodes.Values() {
		if pyval.Eq(pyval.Get(n, "type"), "output") && (only == nil || inList(pyval.Index(n, "id"), only)) &&
			!pyval.Eq(pyval.Get(pyval.Or(pyval.Get(n, "data"), pyjson.Object{}), "destType"), "email") {
			outs = append(outs, n)
		}
	}
	feeding := pyval.NewDict()
	for _, out := range outs {
		upstream, err := flowresolver.ScriptsUpstream(pyval.Index(out, "id"), nodes, edges)
		pyval.Check(err)
		if len(upstream) > 0 {
			script := nodes.Get(upstream[0])
			label := pyval.Str(pyval.Or(pyval.Get(pyval.Or(pyval.Get(script, "data"), pyjson.Object{}), "label"), pyval.Index(script, "id")))
			d := pyval.Or(pyval.Get(out, "data"), pyjson.Object{})
			name := pyval.Str(pyval.Or(pyval.Get(d, "table"), pyval.Or(pyval.Get(d, "name"), pyval.Or(pyval.Get(d, "s3Key"),
				pyval.Or(pyval.Get(d, "sftpFileName"), pyval.Index(out, "id"))))))
			if pyval.Eq(pyval.Get(script, "type"), "container") {
				refuse("the output «"+name+"» of the flow «"+f.Name+"» reads what the Container node «"+label+"» writes: an image runs on Tabularia's cluster, not in dbt",
					"container_not_exportable", "flow", f.Name, "output", name, "container", label)
			}
			refuse("the output «"+name+"» of the flow «"+f.Name+"» reads what the Script node «"+label+"» writes: Python or R runs on Tabularia's cluster, not in dbt",
				"script_not_exportable", "flow", f.Name, "output", name, "script", label)
		}
		for _, s := range upstream {
			feeding.Set(s, true)
		}
	}
	if only == nil {
		var rest []any
		for _, s := range scripts {
			if !feeding.Has(s) {
				rest = append(rest, s)
			}
		}
		pyval.Check(flowresolver.SortIDs(rest))
		for _, i := range rest {
			n := nodes.Get(i)
			label := pyval.Str(pyval.Or(pyval.Get(pyval.Or(pyval.Get(n, "data"), pyjson.Object{}), "label"), i))
			what, runs := "Script node", "Python or R"
			if pyval.Eq(pyval.Get(n, "type"), "container") {
				what, runs = "Container node", "an image"
			}
			p.note = append(p.note, "The flow «"+f.Name+"» also runs the "+what+" «"+label+"» ("+runs+"): it is not "+
				"part of this dbt project, and Tabularia keeps running it with the flow.")
		}
	}
}

// writeModels is _write_models: the steps two or more outputs share become one intermediate
// model, then one model per output.
func (p *Project) writeModels(f *Flow, outputs []*output) {
	if len(outputs) == 0 {
		return
	}
	ctx := p.context(f)
	for _, u := range outputs {
		p.waiting.Delete(pyval.Tuple{f.ID, nodeID(u.node)})
	}
	cuts := cutsOf(outputs)
	sortCuts(cuts)
	inter := pyval.NewDict() // cut → name
	var interOrder []cut
	for _, c := range cuts {
		var u *output
		for _, x := range outputs {
			if len(x.ids) >= len(c.ids) && tupleEq(x.ids[:len(c.ids)], c.ids) && pyval.Eq(x.key, c.key) {
				u = x
				break
			}
		}
		lastID, ok := c.ids[len(c.ids)-1].(string)
		if !ok {
			pyval.Raise("AttributeError", "'%s' object has no attribute 'split'", pyval.TypeName(c.ids[len(c.ids)-1]))
		}
		last := ctx.byID.Get(strings.SplitN(lastID, "#", 2)[0])
		if last == nil {
			last = pyjson.Object{}
		}
		label := pyval.Or(pyval.Get(pyval.Or(pyval.Get(last, "data"), pyjson.Object{}), "label"), pyval.Or(pyval.Get(last, "id"), "step"))
		name := p.names.new("int_"+pyval.Str(label), ctx.suffix, true)
		base, rest := baseOf(u, c.ids, inter, interOrder)
		inter.Set(c.tuple(), name)
		interOrder = append(interOrder, c)
		lbl := pyval.Str(label)
		model := &pyjson.Object{{Key: "name", Value: name}, {Key: "kind", Value: "intermediate"}, {Key: "materialized", Value: "ephemeral"},
			{Key: "source", Value: base}, {Key: "operations", Value: rest},
			{Key: "description", Value: "Shared steps of the flow «" + f.Name + "», up to the node «" + lbl + "»."},
			{Key: "columns", Value: []any{}}, {Key: "tests", Value: []any{}}, {Key: "dialect", Value: ctx.dialect}}
		p.models = append(p.models, p.trace(model, ctx.origin, "output", "shared steps up to the node «"+lbl+"»"))
	}
	for _, u := range outputs {
		model := p.outputModel(f, u, inter, interOrder, ctx.suffix)
		set(model, "dialect", ctx.dialect)
		p.materialization(f, u, model)
		out := get(model, "_output")
		*model = del(*model, "_output")
		p.models = append(p.models, p.trace(model, ctx.origin, "output", out, "node", nodeID(u.node)))
		p.done.Set(pyval.Tuple{f.ID, nodeID(u.node)}, get(model, "name"))
	}
}

// materialization is _materialization: the dialog's choice for this output, when allowed.
func (p *Project) materialization(f *Flow, u *output, model *pyjson.Object) {
	slot := outputKey(f.ID, nodeID(u.node))
	allowed := []string{"table", "view"}
	if get(model, "materialized") == "incremental" {
		allowed = []string{"incremental", "table", "view"}
	}
	al := make([]any, len(allowed))
	for i, a := range allowed {
		al[i] = a
	}
	p.Outputs = append(p.Outputs, pyjson.Object{{Key: "key", Value: slot}, {Key: "model", Value: get(model, "name")},
		{Key: "flow", Value: f.Name}, {Key: "label", Value: get(model, "_output")}, {Key: "materialized", Value: get(model, "materialized")},
		{Key: "materializations", Value: al}})
	choice, ok := p.options.Materializations[slot]
	if !ok {
		return
	}
	for _, a := range allowed {
		if a == choice {
			set(model, "materialized", choice)
			return
		}
	}
	name := pyval.Str(get(model, "name"))
	list := strings.Join(allowed, ", ")
	refuse("the output «"+name+"» cannot be «"+choice+"»: choose among "+list, "materialization_not_allowed",
		"model", name, "choice", choice, "allowed", list)
}

// nestedSources is _nested_sources: the sources of the right branches (join/union) and of the
// foreach's driver and body.
func (p *Project) nestedSources(operations any, depth int) {
	if !pyval.Truthy(operations) {
		return
	}
	for _, op := range pyval.Iter(operations) {
		params := pyval.Or(pyval.Get(op, "params"), pyjson.Object{})
		for _, nested := range []string{"right", "driver"} {
			ref, ok := pyval.Get(params, nested).(pyjson.Object)
			if !ok {
				continue
			}
			src, ok := ref.Value("source").(pyjson.Object)
			if !ok || !pyval.Truthy(src.Value("key")) {
				continue
			}
			sref := p.source(pyval.Index(src, "key"), depth)
			if _, has := sref.Get("key"); !has {
				for i := range ref {
					if ref[i].Key == "source" {
						ref[i].Value = append(pyjson.Object{}, sref...)
					}
				}
			}
			p.nestedSources(pyval.Or(ref.Value("operations"), []any{}), depth)
		}
		p.nestedSources(pyval.Or(pyval.Get(params, "body"), []any{}), depth)
	}
}

// cut is a shared prefix: (source key, node ids).
type cut struct {
	key any
	ids []any
}

func (c cut) tuple() pyval.Tuple { return pyval.Tuple{c.key, pyval.Tuple(c.ids)} }

func tupleEq(a, b []any) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !pyval.Eq(a[i], b[i]) {
			return false
		}
	}
	return true
}

// cutsOf is _cuts: the prefixes shared by at least two outputs, where their roads part.
func cutsOf(outputs []*output) []cut {
	count := pyval.NewDict()
	var order []cut
	for _, u := range outputs {
		for k := 1; k <= len(u.ids); k++ {
			if u.ids[k-1] == nil {
				break
			}
			c := cut{u.key, append([]any{}, u.ids[:k]...)}
			if n, ok := count.Lookup(c.tuple()); ok {
				count.Set(c.tuple(), n.(int)+1)
			} else {
				count.Set(c.tuple(), 1)
				order = append(order, c)
			}
		}
	}
	var cuts []cut
	for _, sig := range order {
		n := count.Get(sig.tuple()).(int)
		if n < 2 {
			continue
		}
		var children []int
		for _, s2 := range order {
			if pyval.Eq(s2.key, sig.key) && len(s2.ids) == len(sig.ids)+1 && tupleEq(s2.ids[:len(sig.ids)], sig.ids) {
				children = append(children, count.Get(s2.tuple()).(int))
			}
		}
		endsHere := false
		for _, u := range outputs {
			if tupleEq(u.ids, sig.ids) && pyval.Eq(u.key, sig.key) {
				endsHere = true
			}
		}
		same := false
		for _, m := range children {
			if m == n {
				same = true
			}
		}
		if endsHere || !same {
			cuts = append(cuts, sig)
		}
	}
	return cuts
}

// sortCuts is sorted(cuts, key=lambda t: (len(t[1]), t)).
func sortCuts(cuts []cut) {
	less := func(a, b cut) bool {
		if len(a.ids) != len(b.ids) {
			return len(a.ids) < len(b.ids)
		}
		return pyval.Less(a.tuple(), b.tuple())
	}
	for i := 1; i < len(cuts); i++ { // a stable insertion sort: the comparison may raise
		for j := i; j > 0 && less(cuts[j], cuts[j-1]); j-- {
			cuts[j], cuts[j-1] = cuts[j-1], cuts[j]
		}
	}
}

// baseOf is _base: the longest intermediate model that is a prefix of the chain, else the
// source; and the remaining operations.
func baseOf(u *output, prefix []any, inter *pyval.Dict, order []cut) (pyjson.Object, []any) {
	var best *cut
	var bestName string
	for _, c := range order {
		if pyval.Eq(c.key, u.key) && len(c.ids) < len(prefix) && tupleEq(prefix[:len(c.ids)], c.ids) {
			if best == nil || len(c.ids) > len(best.ids) {
				cc := c
				best, bestName = &cc, inter.Get(c.tuple()).(string)
			}
		}
	}
	if best == nil {
		return append(pyjson.Object{}, u.rif...), append([]any{}, u.ops[:len(prefix)]...)
	}
	return pyjson.Object{{Key: "model", Value: bestName}}, append([]any{}, u.ops[len(best.ids):len(prefix)]...)
}

var placeholder = regexp.MustCompile(`\{[^{}]*\}`)

// outputModel is _output_model.
func (p *Project) outputModel(f *Flow, u *output, inter *pyval.Dict, order []cut, suffix string) *pyjson.Object {
	req := u.req
	pub := pyval.Or(req.Value("publish"), pyjson.Object{})
	dest := pyval.Or(req.Value("destination"), pyjson.Object{})
	whole := cut{u.key, u.ids}
	var base pyjson.Object
	var rest []any
	if name, ok := inter.Lookup(whole.tuple()); ok {
		base, rest = pyjson.Object{{Key: "model", Value: name}}, []any{}
	} else {
		base, rest = baseOf(u, u.ids, inter, order)
	}
	model := &pyjson.Object{{Key: "kind", Value: "output"}, {Key: "source", Value: base}, {Key: "operations", Value: rest},
		{Key: "tests", Value: []any{}}, {Key: "columns", Value: []any{}}}
	desc := ""
	if f.Description != "" {
		desc = " " + f.Description
	}
	switch {
	case pyval.Truthy(pub):
		dsName := pyval.Or(pyval.Get(pub, "name"), "model")
		projectID := pyval.Get(pub, "project_id")
		set(model, "name", p.names.new(dsName, suffix, false))
		set(model, "materialized", "table")
		set(model, "publishes", []any{dsName, projectID})
		set(model, "description", "Output «"+pyval.Str(dsName)+"» of the flow «"+f.Name+"»."+desc)
		set(model, "_output", "output «"+pyval.Str(dsName)+"» (datasource)")
		if pid, ok := projectID.(int64); ok {
			if ds := p.datasource(`WHERE name = $1 AND project_id = $2 LIMIT 1`, pyval.Str(dsName), pid); ds != nil {
				p.schemaAndContract(model, ds)
			}
		}
	case pyval.Eq(pyval.Get(dest, "type"), "database"):
		tbl := pyval.Strip(pyval.Or(pyval.Get(dest, "table"), ""))
		_, tableName := parseRef(tbl, "")
		var nm any = tableName
		if tableName == "" {
			nm = "model"
		}
		set(model, "name", p.names.new(nm, suffix, false))
		mode := pyval.Or(pyval.Get(dest, "mode"), "append")
		if pyval.Eq(mode, "append") {
			set(model, "materialized", "incremental")
		} else {
			set(model, "materialized", "table")
		}
		if p.root != nil && f.ID == *p.root {
			p.dest[pyval.Str(get(model, "name"))] = pyjson.Object{{Key: "connection_id", Value: pyval.Get(dest, "connection_id")},
				{Key: "table", Value: tbl}, {Key: "mode", Value: mode}}
		}
		set(model, "description", "Output table `"+tbl+"` of the flow «"+f.Name+"» ("+pyval.Str(mode)+").")
		set(model, "_output", "output table `"+tbl+"` ("+pyval.Str(mode)+")")
		if pyval.Truthy(pyval.Get(dest, "post_sql")) {
			p.note = append(p.note, "The output `"+tbl+"` runs a post-SQL after each write in Tabularia; dbt does not: add it as a `post-hook` on the model `"+
				pyval.Str(get(model, "name"))+"` if you need it.")
		}
	case pyval.Eq(pyval.Get(dest, "type"), "sftp"):
		fn := pyval.Or(pyval.Get(dest, "file_name"), "model")
		stem := strings.Trim(strings.SplitN(placeholder.ReplaceAllString(textOf(fn, "sub"), ""), ".", 2)[0], "_- ")
		if stem == "" {
			stem = "model"
		}
		set(model, "name", p.names.new(stem, suffix, false))
		set(model, "materialized", "table")
		fname := pyval.Str(pyval.Get(dest, "file_name"))
		set(model, "description", "Output file `"+fname+"` written to an SFTP server by the flow «"+f.Name+"»: in dbt it is a table (export it from the warehouse).")
		set(model, "_output", "output file `"+fname+"` (SFTP)")
	default:
		key := textOf(pyval.Or(pyval.Get(dest, "key"), "model"), "rsplit")
		stem := strings.SplitN(key[strings.LastIndex(key, "/")+1:], ".", 2)[0]
		if k := pyval.Or(pyval.Get(dest, "key"), ""); strings.Contains(textOf(k, "__contains__"), "{") {
			ks := textOf(k, "rsplit")
			stem = strings.Trim(strings.SplitN(placeholder.ReplaceAllString(ks[strings.LastIndex(ks, "/")+1:], ""), ".", 2)[0], "_- ")
			if stem == "" {
				stem = "model"
			}
		}
		set(model, "name", p.names.new(stem, suffix, false))
		set(model, "materialized", "table")
		k := pyval.Str(pyval.Get(dest, "key"))
		set(model, "description", "Output file `"+k+"` of the flow «"+f.Name+"»: in dbt it is a table (export it from the warehouse).")
		set(model, "_output", "output file `"+k+"`")
	}
	return model
}

// textOf is a value a string method is called on: a text, or the TypeError/AttributeError.
func textOf(v any, method string) string {
	s, ok := v.(string)
	if !ok {
		if method == "sub" {
			pyval.Raise("TypeError", "expected string or bytes-like object, got '%s'", pyval.TypeName(v))
		}
		if method == "__contains__" {
			pyval.Raise("TypeError", "argument of type '%s' is not iterable", pyval.TypeName(v))
		}
		pyval.Raise("AttributeError", "'%s' object has no attribute '%s'", pyval.TypeName(v), method)
	}
	return s
}

// activeDocument is contracts.active_document: the contract to evaluate, nil when there is
// none or it is off.
func (p *Project) activeDocument(dsID int64) pyjson.Object {
	var enabled bool
	var doc string
	err := p.row(`SELECT enabled, document FROM data_contracts WHERE datasource_id = $1 ORDER BY id LIMIT 1`, dsID).Scan(&enabled, &doc)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && !enabled) {
		return nil
	}
	pyval.Check(err)
	v, lerr := pyjson.Loads(doc)
	if lerr != nil {
		return nil
	}
	if !pyval.Truthy(pyval.Get(v, "rules")) {
		return nil
	}
	return v.(pyjson.Object)
}

// schemaAndContract is _schema_and_contract: the published datasource's columns and the
// rules of its data contract as dbt tests.
func (p *Project) schemaAndContract(model *pyjson.Object, ds *Datasource) {
	set(model, "datasource_id", ds.ID)
	descr := descriptions(ds)
	cols := pyval.NewDict()
	for _, c := range columns(ds) {
		cols.Set(c, &pyjson.Object{{Key: "name", Value: c}, {Key: "description", Value: descOf(descr, c)}, {Key: "tests", Value: []any{}}})
	}
	doc := p.activeDocument(ds.ID)
	var rules []any
	if doc != nil {
		if r := doc.Value("rules"); pyval.Truthy(r) {
			rules = pyval.Iter(r)
		}
	}
	if !p.options.Tests {
		var kept []any
		for _, r := range rules {
			if pyval.Eq(pyval.Get(r, "kind"), "column") {
				kept = append(kept, r)
			}
		}
		rules = kept
	}
	column := func(col any) *pyjson.Object {
		return cols.SetDefault(col, &pyjson.Object{{Key: "name", Value: col}, {Key: "description", Value: descOf(descr, col)},
			{Key: "tests", Value: []any{}}}).(*pyjson.Object)
	}
	tests := []any{}
	for _, r := range rules {
		kind := pyval.Get(r, "kind")
		sev := "error"
		if pyval.Eq(pyval.Get(r, "severity"), "warning") {
			sev = "warn"
		}
		col := pyval.Get(r, "column")
		if pyval.Eq(kind, "unique") {
			src := pyval.Get(r, "columns")
			if !pyval.Truthy(src) {
				if pyval.Truthy(col) {
					src = []any{col}
				} else {
					src = []any{}
				}
			}
			var cs []any
			for _, c := range pyval.Iter(src) {
				if pyval.Truthy(c) {
					cs = append(cs, c)
				}
			}
			if len(cs) == 1 {
				col = cs[0]
			} else if len(cs) > 0 {
				tests = append(tests, pyjson.Object{{Key: "id", Value: pyval.Get(r, "id")}, {Key: "kind", Value: "unique_combination"},
					{Key: "columns", Value: cs}, {Key: "severity", Value: sev}})
				continue
			} else {
				continue
			}
		}
		pyval.Key(kind) // `kind in {…}`: an unhashable kind raises
		k, _ := kind.(string)
		switch {
		case columnTests[k] && pyval.Truthy(col):
			item := column(col)
			t := pyjson.Object{{Key: "kind", Value: kind}, {Key: "severity", Value: sev}}
			if k == "accepted_values" {
				t = append(t, pyjson.KV{Key: "values", Value: pyval.List(pyval.Or(pyval.Get(r, "values"), []any{}))})
			}
			set(item, "tests", append(get(item, "tests").([]any), t))
		case pyval.Eq(kind, "column") && pyval.Truthy(col):
			item := column(col)
			if dt := pyval.Get(r, "dtype"); pyval.Truthy(dt) {
				d := pyval.Str(get(item, "description"))
				if d != "" {
					d += " "
				}
				set(item, "description", d+"Contract: declared as "+pyval.Str(pyval.Index(r, "dtype"))+".")
			}
		case modelTests[k]:
			t := pyjson.Object{}
			for _, key := range []string{"id", "kind", "column", "min", "max", "regex", "sql"} {
				if v := pyval.Get(r, key); v != nil {
					t = append(t, pyjson.KV{Key: key, Value: v})
				}
			}
			tests = append(tests, t.Set("severity", sev))
		case pyval.Eq(kind, "freshness"):
			p.note = append(p.note, "The contract of «"+ds.Name+"» has a freshness rule ("+pyval.Str(pyval.Get(r, "max_age_hours"))+
				" h): dbt checks freshness on sources, not on models — schedule the run accordingly.")
		}
	}
	set(model, "tests", append(get(model, "tests").([]any), tests...))
	set(model, "columns", cols.Values())
	if doc != nil && pyval.Truthy(doc.Value("description")) {
		set(model, "description", pyval.Str(get(model, "description"))+" Contract: "+pyval.Str(doc.Value("description")))
	}
}
