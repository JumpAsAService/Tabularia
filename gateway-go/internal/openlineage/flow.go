package openlineage

import (
	"context"
	"errors"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Flow is the part of a flow the events read.
type Flow struct {
	ID, ProjectID                 int64
	Name, Description, Definition string
}

// JobNameForFlow is job_name_for_flow.
func (b *Builder) JobNameForFlow(ctx context.Context, f *Flow) (string, error) {
	path, err := b.ProjectPath(ctx, &f.ProjectID)
	if path == "" {
		return f.Name, err
	}
	return path + "/" + f.Name, err
}

// definition is _definition: the flow's definition, {} when it is not a JSON object.
func definition(f *Flow) pyjson.Object {
	text := f.Definition
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return pyjson.Object{}
	}
	o, _ := v.(pyjson.Object)
	return o
}

var errNotADict = errors.New("openlineage: a node or its data is not a dict")

// nodesOf is `definition.get("nodes") or []`, each a dict (an AttributeError otherwise).
func nodesOf(def pyjson.Object) ([]pyjson.Object, error) {
	v := def.Value("nodes")
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	list, ok := v.([]any)
	if !ok {
		return nil, errNotADict
	}
	var out []pyjson.Object
	for _, n := range list {
		o, ok := n.(pyjson.Object)
		if !ok {
			return nil, errNotADict
		}
		out = append(out, o)
	}
	return out, nil
}

func dataOf(n pyjson.Object) (pyjson.Object, error) {
	v := n.Value("data")
	if !pyjson.Truthy(v) {
		return pyjson.Object{}, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, errNotADict
	}
	return o, nil
}

// idOf is an id from the definition as session.get takes it: an integer (or its text).
func idOf(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case float64:
		if x == float64(int64(x)) {
			return int64(x), true
		}
	case string:
		if n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64); err == nil {
			return n, true
		}
	}
	return 0, false
}

func text(v any) string {
	s, _ := pyjson.Or(v, "").(string)
	return s
}

// sourceDatasets is _source_datasets.
func (b *Builder) sourceDatasets(ctx context.Context, nodes []pyjson.Object) ([]Dataset, error) {
	var inputs []Dataset
	for _, n := range nodes {
		d, err := dataOf(n)
		if err != nil {
			return nil, err
		}
		var ds *Datasource
		if id := d.Value("datasourceId"); id != nil {
			if i, ok := idOf(id); ok {
				if ds, err = b.GetDatasource(ctx, i); err != nil {
					return nil, err
				}
			}
		}
		if ds != nil {
			x, err := b.DatasourceDataset(ctx, ds, false, nil)
			if err != nil {
				return nil, err
			}
			inputs = append(inputs, x)
		} else if key := d.Value("parquetKey"); pyjson.Truthy(key) {
			bucket := text(d.Value("bucket"))
			if bucket == "" {
				bucket = b.S.EngineBucket
			}
			inputs = append(inputs, ObjectDataset("", bucket, pyjson.Str(key), "parquet", false, nil, ""))
		}
	}
	return Unique(inputs), nil
}

// FlowInputs is flow_inputs.
func (b *Builder) FlowInputs(ctx context.Context, f *Flow) ([]Dataset, error) {
	nodes, err := nodesOf(definition(f))
	if err != nil {
		return nil, err
	}
	var sources []pyjson.Object
	for _, n := range nodes {
		if n.Value("type") == "source" {
			sources = append(sources, n)
		}
	}
	return b.sourceDatasets(ctx, sources)
}

// findDatasource is _find_datasource.
func (b *Builder) findDatasource(ctx context.Context, project any, name string) (*Datasource, error) {
	p, ok := idOf(project)
	if project == nil || name == "" || !ok {
		return nil, nil
	}
	ds, err := scanDS(b.Q.QueryRow(ctx, `SELECT `+dsColumns+` FROM datasources WHERE project_id = $1 AND name = $2 LIMIT 1`, p, name))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return ds, err
}

func (b *Builder) connection(ctx context.Context, id any) (*Connection, error) {
	if !pyjson.Truthy(id) {
		return nil, nil
	}
	i, ok := idOf(id)
	if !ok {
		return nil, nil
	}
	return b.GetConnection(ctx, i)
}

// FlowEmailName is flow_email_name.
func (b *Builder) FlowEmailName(ctx context.Context, f *Flow) (string, error) {
	name, err := b.JobNameForFlow(ctx, f)
	return "/" + strings.Trim(name, "/") + "/email", err
}

// FlowOutputs is flow_outputs: what the flow writes, from its Output nodes.
func (b *Builder) FlowOutputs(ctx context.Context, f *Flow) ([]Dataset, error) {
	nodes, err := nodesOf(definition(f))
	if err != nil {
		return nil, err
	}
	var outputs []Dataset
	for _, n := range nodes {
		if n.Value("type") != "output" {
			continue
		}
		d, err := dataOf(n)
		if err != nil {
			return nil, err
		}
		switch pyjson.Or(d.Value("destType"), "datasource") {
		case "datasource":
			name := strings.TrimSpace(text(d.Value("name")))
			ds, err := b.findDatasource(ctx, d.Value("projectId"), name)
			if err != nil {
				return nil, err
			}
			if ds != nil {
				x, err := b.DatasourceDataset(ctx, ds, true, nil)
				if err != nil {
					return nil, err
				}
				outputs = append(outputs, x)
			} else if pyjson.Truthy(d.Value("name")) {
				var pid *int64
				if p, ok := idOf(d.Value("projectId")); ok {
					pid = &p
				}
				path, err := b.ProjectPath(ctx, pid)
				if err != nil {
					return nil, err
				}
				outputs = append(outputs, Dataset{Namespace: "tabularia://" + b.S.Namespace, Output: true,
					Name: strings.ReplaceAll("/"+path+"/"+strings.TrimSpace(text(d.Value("name"))), "//", "/")})
			}
			if pyjson.Truthy(d.Value("mirrorEnabled")) && pyjson.Truthy(d.Value("mirrorConnectionId")) {
				m, err := b.connection(ctx, d.Value("mirrorConnectionId"))
				if err != nil {
					return nil, err
				}
				if m != nil {
					bucket := text(d.Value("mirrorBucket"))
					if bucket == "" {
						bucket = deref(m.Database)
					}
					outputs = append(outputs, ObjectDataset(m.Host, strings.TrimSpace(bucket), text(d.Value("mirrorKey")), "parquet", true, nil, ""))
				}
			}
		case "database":
			c, err := b.connection(ctx, d.Value("connectionId"))
			if err != nil {
				return nil, err
			}
			if table := strings.TrimSpace(text(d.Value("table"))); c != nil && table != "" {
				mode := text(d.Value("mode"))
				if mode == "" {
					mode = "append"
				}
				outputs = append(outputs, TableDataset(c, table, true, nil, mode))
			}
		case "s3":
			c, err := b.connection(ctx, d.Value("connectionId"))
			if err != nil {
				return nil, err
			}
			if key := strings.TrimSpace(text(d.Value("s3Key"))); c != nil && key != "" {
				bucket := text(d.Value("s3Bucket"))
				if bucket == "" {
					bucket = deref(c.Database)
				}
				format := text(d.Value("s3Format"))
				if format == "" {
					format = "parquet"
				}
				outputs = append(outputs, ObjectDataset(c.Host, strings.TrimSpace(bucket), key, format, true, nil, ""))
			}
		case "sftp":
			c, err := b.connection(ctx, d.Value("connectionId"))
			if err != nil {
				return nil, err
			}
			if name := strings.TrimSpace(text(d.Value("sftpFileName"))); c != nil && name != "" {
				format := text(d.Value("sftpFormat"))
				if format == "" {
					format = "csv"
				}
				outputs = append(outputs, SFTPDataset(SFTPAddress(c), SFTPPath(text(d.Value("sftpFolder")), name), format, true, nil, ""))
			}
		case "email":
			c, err := b.connection(ctx, d.Value("connectionId"))
			if err != nil {
				return nil, err
			}
			if c != nil {
				port := int64(587)
				if c.Port != nil && *c.Port != 0 {
					port = *c.Port
				}
				name, err := b.FlowEmailName(ctx, f)
				if err != nil {
					return nil, err
				}
				outputs = append(outputs, EmailDataset(c.Host, port, name, c.Name))
			}
		}
	}
	return Unique(outputs), nil
}

// Job is the event's job: its namespace, name and facets.
type Job struct {
	Name   string
	Facets pyjson.Object
}

func (j Job) json(namespace string) pyjson.Object {
	return pyjson.Object{{Key: "namespace", Value: namespace}, {Key: "name", Value: j.Name}, {Key: "facets", Value: j.Facets}}
}

// JobType is the jobType facet.
func JobType(jobType string) pyjson.Object {
	return facet(std("2-0-4", "JobTypeJobFacet"), pyjson.KV{Key: "processingType", Value: "BATCH"},
		pyjson.KV{Key: "integration", Value: "TABULARIA"}, pyjson.KV{Key: "jobType", Value: jobType})
}

// Documentation is the job's documentation facet.
func Documentation(description string) pyjson.Object {
	return facet(std("1-1-0", "DocumentationJobFacet"), pyjson.KV{Key: "description", Value: description})
}

// FlowJob is _job.
func (b *Builder) FlowJob(ctx context.Context, f *Flow, extra pyjson.Object) (Job, error) {
	facets := pyjson.Object{{Key: "jobType", Value: JobType("FLOW")}}
	if f.Description != "" {
		facets = append(facets, pyjson.KV{Key: "documentation", Value: Documentation(f.Description)})
	}
	for _, kv := range extra {
		facets = facets.Set(kv.Key, kv.Value)
	}
	name, err := b.JobNameForFlow(ctx, f)
	return Job{Name: name, Facets: facets}, err
}

func datasets(ds []Dataset) []any {
	out := []any{}
	for _, d := range ds {
		out = append(out, d.JSON())
	}
	return out
}

// JobEvent is a JobEvent as Serde writes it.
func (b *Builder) JobEvent(job Job, inputs, outputs []Dataset) pyjson.Object {
	return pyjson.Object{{Key: "eventTime", Value: Now()}, {Key: "producer", Value: Producer},
		{Key: "schemaURL", Value: "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/JobEvent"},
		{Key: "job", Value: job.json(b.S.Namespace)}, {Key: "inputs", Value: datasets(inputs)}, {Key: "outputs", Value: datasets(outputs)}}
}

// JobEventsForFlow is job_events_for_flow: the flow's static lineage, from its definition.
func (b *Builder) JobEventsForFlow(ctx context.Context, f *Flow) ([]pyjson.Object, error) {
	job, err := b.FlowJob(ctx, f, nil)
	if err != nil {
		return nil, err
	}
	inputs, err := b.FlowInputs(ctx, f)
	if err != nil {
		return nil, err
	}
	outputs, err := b.FlowOutputs(ctx, f)
	if err != nil {
		return nil, err
	}
	return []pyjson.Object{b.JobEvent(job, inputs, outputs)}, nil
}
