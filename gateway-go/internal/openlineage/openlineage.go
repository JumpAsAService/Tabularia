// Package openlineage is services/openlineage.py: Tabularia's lineage in the OpenLineage
// standard, its events built as the official Python client (openlineage-python 1.53.0)
// serializes them (Serde.to_json: None left out, facets with the client's `_producer` and their
// `_schemaURL`), and sent to the collector (send.go).
package openlineage

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Producer is PRODUCER; ClientProducer is the facets' `_producer` the client writes.
const (
	Producer       = "https://github.com/JumpAsAService/Tabularia"
	ClientProducer = "https://github.com/OpenLineage/OpenLineage/tree/1.53.0/client/python"
	specs          = "https://openlineage.io/spec/facets/"
)

// Settings is OpenLineageSettings, and the library's own variables.
type Settings struct {
	URL, Endpoint, APIKey, File, Namespace string
	Timeout                                float64
	LibraryURL, LibraryConfig              string // OPENLINEAGE_URL, OPENLINEAGE_CONFIG
	EngineBucket                           string // ENGINE__BUCKET
}

// FromEnv reads them (the same names and defaults as core/config.py).
func FromEnv() Settings {
	env := config.FromOS()
	str := func(n, def string) string {
		if v, ok := env(n); ok {
			return v
		}
		return def
	}
	s := Settings{URL: str("OPENLINEAGE__URL", ""), Endpoint: str("OPENLINEAGE__ENDPOINT", "api/v1/lineage"),
		APIKey: str("OPENLINEAGE__API_KEY", ""), File: str("OPENLINEAGE__FILE", ""), Namespace: str("OPENLINEAGE__NAMESPACE", "tabularia"),
		Timeout: 10, LibraryURL: os.Getenv("OPENLINEAGE_URL"), LibraryConfig: os.Getenv("OPENLINEAGE_CONFIG"),
		EngineBucket: str("ENGINE__BUCKET", "data-prep")}
	if v, ok := env("OPENLINEAGE__TIMEOUT_SECONDS"); ok {
		var f float64
		if _, err := fmt.Sscan(strings.TrimSpace(v), &f); err == nil && f > 0 {
			s.Timeout = f
		}
	}
	return s
}

// Enabled is enabled(): the events go somewhere.
func (s Settings) Enabled() bool {
	return s.URL != "" || s.File != "" || s.LibraryURL != "" || s.LibraryConfig != ""
}

// facet is a facet of the client: its producer, its schema and its fields.
func facet(schema string, fields ...pyjson.KV) pyjson.Object {
	return append(pyjson.Object{{Key: "_producer", Value: ClientProducer}, {Key: "_schemaURL", Value: schema}}, fields...)
}

func std(version, name string) string {
	return specs + version + "/" + name + ".json#/$defs/" + name
}

// Now is _now: the moment in UTC with milliseconds and a Z.
func Now() string { return time.Now().UTC().Format("2006-01-02T15:04:05.000") + "Z" }

// Dataset is an InputDataset or an OutputDataset.
type Dataset struct {
	Namespace, Name string
	Facets          pyjson.Object
	Output          bool
	OutputFacets    pyjson.Object
}

// JSON is the dataset as Serde writes it.
func (d Dataset) JSON() pyjson.Object {
	facets := d.Facets
	if facets == nil {
		facets = pyjson.Object{}
	}
	if d.Output {
		of := d.OutputFacets
		if of == nil {
			of = pyjson.Object{}
		}
		return pyjson.Object{{Key: "namespace", Value: d.Namespace}, {Key: "name", Value: d.Name}, {Key: "facets", Value: facets},
			{Key: "outputFacets", Value: of}}
	}
	return pyjson.Object{{Key: "namespace", Value: d.Namespace}, {Key: "name", Value: d.Name}, {Key: "facets", Value: facets},
		{Key: "inputFacets", Value: pyjson.Object{}}}
}

func outputStatistics(rows *int64) pyjson.Object {
	if rows == nil {
		return pyjson.Object{}
	}
	return pyjson.Object{{Key: "outputStatistics", Value: facet(std("1-0-2", "OutputStatisticsOutputDatasetFacet"),
		pyjson.KV{Key: "rowCount", Value: *rows})}}
}

// ── the objects read ────────────────────────────────────────────────────────

// Builder holds what the events of one request read: the folders once (_project_path).
type Builder struct {
	S       Settings
	Q       perm.Querier
	names   map[int64]string
	parents map[int64]*int64
}

// ProjectPath is _project_path: the folder's path, «A/B».
func (b *Builder) ProjectPath(ctx context.Context, projectID *int64) (string, error) {
	if projectID == nil {
		return "", nil
	}
	if b.names == nil {
		rows, err := b.Q.Query(ctx, `SELECT id, name, parent_id FROM projects`)
		if err != nil {
			return "", err
		}
		b.names, b.parents = map[int64]string{}, map[int64]*int64{}
		for rows.Next() {
			var id int64
			var name string
			var parent *int64
			if err := rows.Scan(&id, &name, &parent); err != nil {
				rows.Close()
				return "", err
			}
			b.names[id], b.parents[id] = name, parent
		}
		rows.Close()
	}
	chain := perm.Ancestors(b.parents, *projectID)
	var parts []string
	for i := len(chain) - 1; i >= 0; i-- {
		if n, ok := b.names[chain[i]]; ok {
			parts = append(parts, n)
		}
	}
	return strings.Join(parts, "/"), nil
}

// Datasource is the part of a datasource the events read.
type Datasource struct {
	ID, ProjectID                        int64
	Name, Description, Bucket, Key, Kind string
	Columns                              string
	ConnectionID                         *int64
	SourceType, SourceRef                *string
}

const dsColumns = `id, project_id, name, description, bucket, key, kind, columns, connection_id, source_type, source_ref`

func scanDS(row pgx.Row) (*Datasource, error) {
	var d Datasource
	err := row.Scan(&d.ID, &d.ProjectID, &d.Name, &d.Description, &d.Bucket, &d.Key, &d.Kind, &d.Columns, &d.ConnectionID, &d.SourceType, &d.SourceRef)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &d, err
}

// GetDatasource is session.get(Datasource, id).
func (b *Builder) GetDatasource(ctx context.Context, id int64) (*Datasource, error) {
	return scanDS(b.Q.QueryRow(ctx, `SELECT `+dsColumns+` FROM datasources WHERE id = $1`, id))
}

// Connection is the part of a connection the events read.
type Connection struct {
	ID                                     int64
	Name, DBType, Host, Username, DBSchema string
	Database                               *string
	Port                                   *int64
}

// GetConnection is session.get(Connection, id).
func (b *Builder) GetConnection(ctx context.Context, id int64) (*Connection, error) {
	var c Connection
	err := b.Q.QueryRow(ctx, `SELECT id, name, db_type, coalesce(host, ''), coalesce(username, ''), coalesce(db_schema, ''), database, port
		FROM connections WHERE id = $1`, id).Scan(&c.ID, &c.Name, &c.DBType, &c.Host, &c.Username, &c.DBSchema, &c.Database, &c.Port)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &c, err
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// storageNamespace is _storage_namespace.
func storageNamespace(bucket string) string {
	if strings.Contains(strings.ToLower(os.Getenv("STORAGE__ENDPOINT")), "googleapis") {
		return "gs://" + bucket
	}
	return "s3://" + bucket
}

// schemaFacet is _schema_facet: nil without fields.
func schemaFacet(columns string) pyjson.Object {
	if columns == "" {
		columns = "[]"
	}
	v, err := pyjson.Loads(columns)
	if err != nil {
		return nil
	}
	list, _ := v.([]any)
	var fields []any
	for _, c := range list {
		co, ok := c.(pyjson.Object)
		if !ok || !pyjson.Truthy(co.Value("name")) {
			continue
		}
		fields = append(fields, pyjson.Object{{Key: "name", Value: pyjson.Str(co.Value("name"))},
			{Key: "type", Value: pyjson.Str(pyjson.Or(co.Value("dtype"), ""))}, {Key: "fields", Value: []any{}}})
	}
	if fields == nil {
		return nil
	}
	return facet(std("1-2-0", "SchemaDatasetFacet"), pyjson.KV{Key: "fields", Value: fields})
}

// DatasourceName is datasource_name.
func (b *Builder) DatasourceName(ctx context.Context, ds *Datasource) (string, error) {
	path, err := b.ProjectPath(ctx, &ds.ProjectID)
	if path == "" {
		return "/" + ds.Name, err
	}
	return "/" + path + "/" + ds.Name, err
}

// DatasourceDataset is datasource_dataset.
func (b *Builder) DatasourceDataset(ctx context.Context, ds *Datasource, output bool, rows *int64) (Dataset, error) {
	physical := storageNamespace(ds.Bucket)
	folder, err := b.ProjectPath(ctx, &ds.ProjectID)
	if err != nil {
		return Dataset{}, err
	}
	facets := pyjson.Object{
		{Key: "dataSource", Value: facet(std("1-0-1", "DatasourceDatasetFacet"), pyjson.KV{Key: "name", Value: ds.Name},
			pyjson.KV{Key: "uri", Value: physical + "/" + ds.Key})},
		{Key: "storage", Value: facet(std("1-0-1", "StorageDatasetFacet"), pyjson.KV{Key: "storageLayer", Value: "object-storage"},
			pyjson.KV{Key: "fileFormat", Value: "parquet"})},
		{Key: "tabularia", Value: facet(Producer+"/blob/main/docs/openlineage/TabulariaDatasetFacet.json",
			pyjson.KV{Key: "datasourceId", Value: ds.ID}, pyjson.KV{Key: "projectId", Value: ds.ProjectID}, pyjson.KV{Key: "kind", Value: ds.Kind},
			pyjson.KV{Key: "folder", Value: folder})},
	}
	if ds.Key != "" {
		facets = append(facets, pyjson.KV{Key: "datasetVersion", Value: facet(std("1-0-1", "DatasetVersionDatasetFacet"),
			pyjson.KV{Key: "datasetVersion", Value: ds.Key})})
	}
	facets = append(facets, pyjson.KV{Key: "symlinks", Value: facet(std("1-0-1", "SymlinksDatasetFacet"),
		pyjson.KV{Key: "identifiers", Value: []any{pyjson.Object{{Key: "namespace", Value: physical},
			{Key: "name", Value: fmt.Sprintf("/datasets/%d", ds.ID)}, {Key: "type", Value: "LOCATION"}}}})})
	if s := schemaFacet(ds.Columns); s != nil {
		facets = append(facets, pyjson.KV{Key: "schema", Value: s})
	}
	if ds.Description != "" {
		facets = append(facets, pyjson.KV{Key: "documentation", Value: facet(std("1-1-0", "DocumentationDatasetFacet"),
			pyjson.KV{Key: "description", Value: ds.Description})})
	}
	name, err := b.DatasourceName(ctx, ds)
	if err != nil {
		return Dataset{}, err
	}
	d := Dataset{Namespace: "tabularia://" + b.S.Namespace, Name: name, Facets: facets, Output: output}
	if output {
		d.OutputFacets = outputStatistics(rows)
	}
	return d, nil
}

var ports = map[string]int64{"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8443, "trino": 8080, "oracle": 1521, "sqlserver": 1433}
var schemes = map[string]string{"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse", "trino": "trino",
	"oracle": "oracle", "sqlserver": "tsql"}

func hostPort(c *Connection) string {
	parts := strings.Split(c.Host, "://")
	host := strings.TrimRight(parts[len(parts)-1], "/")
	if strings.Contains(host, ":") {
		return host
	}
	port := ports[c.DBType]
	if c.Port != nil && *c.Port != 0 {
		port = *c.Port
	}
	return fmt.Sprintf("%s:%d", host, port)
}

// TableDatasetName is table_dataset_name.
func TableDatasetName(c *Connection, table string) string {
	join := func(parts []string) string {
		var out []string
		for _, p := range parts {
			if p != "" {
				out = append(out, p)
			}
		}
		return strings.Join(out, ".")
	}
	if c.DBType == "oracle" {
		oracle := func(part string) string {
			part = strings.TrimSpace(part)
			if len(part) > 1 && part[0] == '"' && part[len(part)-1] == '"' {
				return part[1 : len(part)-1]
			}
			return strings.ToUpper(part)
		}
		var names []string
		for _, p := range strings.Split(strings.TrimSpace(table), ".") {
			if strings.TrimSpace(p) != "" {
				names = append(names, oracle(p))
			}
		}
		if len(names) == 1 {
			first := strings.ToUpper(c.Username)
			if c.DBSchema != "" {
				first = oracle(c.DBSchema)
			}
			names = append([]string{first}, names...)
		}
		return join(append([]string{deref(c.Database)}, names...))
	}
	var parts []string
	for _, p := range strings.Split(strings.TrimSpace(table), ".") {
		if strings.TrimSpace(p) != "" {
			parts = append(parts, strings.Trim(p, "\"`[]"))
		}
	}
	or := func(a, b string) string {
		if a != "" {
			return a
		}
		return b
	}
	switch c.DBType {
	case "sqlserver":
		if len(parts) == 1 {
			parts = append([]string{or(c.DBSchema, "dbo")}, parts...)
		}
		if len(parts) == 2 {
			parts = append([]string{deref(c.Database)}, parts...)
		}
	case "postgresql", "trino":
		if len(parts) == 1 {
			def := "default"
			if c.DBType == "postgresql" {
				def = "public"
			}
			parts = append([]string{or(c.DBSchema, def)}, parts...)
		}
		if len(parts) == 2 {
			parts = append([]string{deref(c.Database)}, parts...)
		}
	default:
		if len(parts) == 1 {
			parts = append([]string{deref(c.Database)}, parts...)
		}
	}
	return join(parts)
}

// TableNamespace is table_dataset's namespace.
func TableNamespace(c *Connection) string {
	scheme := schemes[c.DBType]
	if c.DBType == "sqlserver" {
		scheme = "mssql"
	} else if scheme == "" {
		scheme = c.DBType
	}
	return scheme + "://" + hostPort(c)
}

// TableDataset is table_dataset.
func TableDataset(c *Connection, table string, output bool, rows *int64, mode string) Dataset {
	ns := TableNamespace(c)
	facets := pyjson.Object{{Key: "dataSource", Value: facet(std("1-0-1", "DatasourceDatasetFacet"), pyjson.KV{Key: "name", Value: c.Name},
		pyjson.KV{Key: "uri", Value: ns})}}
	d := Dataset{Namespace: ns, Name: TableDatasetName(c, table), Output: output}
	if output {
		if mode != "" {
			state := "ALTER"
			if mode == "replace" {
				state = "OVERWRITE"
			}
			facets = append(facets, pyjson.KV{Key: "lifecycleStateChange", Value: facet(std("1-0-1", "LifecycleStateChangeDatasetFacet"),
				pyjson.KV{Key: "lifecycleStateChange", Value: state})})
		}
		d.OutputFacets = outputStatistics(rows)
	}
	d.Facets = facets
	return d
}

// ObjectDataset is object_dataset.
func ObjectDataset(endpoint, bucket, key, format string, output bool, rows *int64, written string) Dataset {
	ns := "s3://" + bucket
	if strings.Contains(strings.ToLower(endpoint), "googleapis") {
		ns = "gs://" + bucket
	}
	facets := pyjson.Object{}
	if format != "" {
		facets = append(facets, pyjson.KV{Key: "storage", Value: facet(std("1-0-1", "StorageDatasetFacet"),
			pyjson.KV{Key: "storageLayer", Value: "object-storage"}, pyjson.KV{Key: "fileFormat", Value: format})})
	}
	if written != "" {
		facets = append(facets, pyjson.KV{Key: "datasetVersion", Value: facet(std("1-0-1", "DatasetVersionDatasetFacet"),
			pyjson.KV{Key: "datasetVersion", Value: written})})
	}
	d := Dataset{Namespace: ns, Name: "/" + strings.Trim(key, "/"), Facets: facets, Output: output}
	if output {
		d.OutputFacets = outputStatistics(rows)
	}
	return d
}

// SFTPDataset is sftp_dataset.
func SFTPDataset(address, path, format string, output bool, rows *int64, written string) Dataset {
	facets := pyjson.Object{}
	if format != "" {
		facets = append(facets, pyjson.KV{Key: "storage", Value: facet(std("1-0-1", "StorageDatasetFacet"),
			pyjson.KV{Key: "storageLayer", Value: "file"}, pyjson.KV{Key: "fileFormat", Value: format})})
	}
	if written != "" {
		facets = append(facets, pyjson.KV{Key: "datasetVersion", Value: facet(std("1-0-1", "DatasetVersionDatasetFacet"),
			pyjson.KV{Key: "datasetVersion", Value: written})})
	}
	d := Dataset{Namespace: "sftp://" + address, Name: "/" + strings.Trim(path, "/"), Facets: facets, Output: output}
	if output {
		d.OutputFacets = outputStatistics(rows)
	}
	return d
}

// SFTPAddress is _sftp_address.
func SFTPAddress(c *Connection) string {
	port := int64(22)
	if c.Port != nil && *c.Port != 0 {
		port = *c.Port
	}
	return fmt.Sprintf("%s:%d", c.Host, port)
}

// SFTPPath is _sftp_path.
func SFTPPath(folder, name string) string {
	var parts []string
	for _, p := range []string{strings.Trim(folder, "/"), strings.Trim(name, "/")} {
		if p != "" {
			parts = append(parts, p)
		}
	}
	return strings.Join(parts, "/")
}

// EmailDataset is email_dataset: the SMTP server, never the recipients.
func EmailDataset(host string, port int64, name, connectionName string) Dataset {
	parts := strings.Split(host, "://")
	ns := fmt.Sprintf("smtp://%s:%d", strings.TrimRight(parts[len(parts)-1], "/"), port)
	if connectionName == "" {
		connectionName = ns
	}
	return Dataset{Namespace: ns, Name: name, Output: true, OutputFacets: pyjson.Object{},
		Facets: pyjson.Object{{Key: "dataSource", Value: facet(std("1-0-1", "DatasourceDatasetFacet"),
			pyjson.KV{Key: "name", Value: connectionName}, pyjson.KV{Key: "uri", Value: ns})}}}
}

// Unique is _unique: one dataset per namespace and name, the first.
func Unique(ds []Dataset) []Dataset {
	seen := map[[2]string]bool{}
	out := []Dataset{}
	for _, d := range ds {
		k := [2]string{d.Namespace, d.Name}
		if !seen[k] {
			seen[k] = true
			out = append(out, d)
		}
	}
	return out
}
