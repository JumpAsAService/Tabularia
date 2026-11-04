package openlineage

import (
	"context"
	"crypto/sha1"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// ── runs: the events of a closed run (services/openlineage.py run_events) ───────────────────

var namespaceURL = [16]byte{0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8}

// uuid5 is uuid.uuid5(uuid.NAMESPACE_URL, name).
func uuid5(name string) string {
	h := sha1.New()
	h.Write(namespaceURL[:])
	h.Write([]byte(name))
	b := h.Sum(nil)[:16]
	b[6] = (b[6] & 0x0f) | 0x50
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// RunUUID is run_uuid: a stable UUID per run, the same from any process.
func (s Settings) RunUUID(runID int64) string {
	return uuid5(fmt.Sprintf("tabularia://%s/runs/%d", s.Namespace, runID))
}

// NotebookRunUUID is notebook_run_uuid: a session's run id, from its Job's name.
func (s Settings) NotebookRunUUID(sessionKey string) string {
	return uuid5(fmt.Sprintf("tabularia://%s/notebook-sessions/%s", s.Namespace, sessionKey))
}

// iso is _iso: a naive moment taken as UTC, with milliseconds and a Z; none is now.
func iso(t *time.Time) string {
	if t == nil {
		return Now()
	}
	return t.UTC().Format("2006-01-02T15:04:05.000") + "Z"
}

// address is _ADDRESS: an e-mail address in a run's error (Python's \w is Unicode's).
var address = regexp.MustCompile(`[\p{L}\p{N}_.%+-]{1,64}@[\p{L}\p{N}_-]{1,63}(?:\.[\p{L}\p{N}_-]{1,63})+`)

// errorText is _error_text: a run's error as it leaves the installation, without addresses.
func errorText(message string) string {
	return address.ReplaceAllString(clip(message, 2000), "<address>")
}

// Run is the part of a run the events read.
type Run struct {
	ID                                                 int64
	Kind, Status, TriggerType                          string
	FlowID, FlowGroupID, SavedScriptID, SQLScriptID    *int64
	DatasourceID, ParentRunID, PublishProjectID        *int64
	RowsWritten                                        *int64
	PublishName, Destination, Email, Mirror, Script    *string
	Engine, Error                                      *string
	StartedAt                                          time.Time
	FinishedAt                                         *time.Time
}

const runColumns = `id, kind, status, trigger_type, flow_id, flow_group_id, saved_script_id, sql_script_id, datasource_id,
	parent_run_id, publish_project_id, rows_written, publish_name, destination, email, mirror, script, engine, error, started_at, finished_at`

func scanRun(row pgx.Row) (*Run, error) {
	var r Run
	err := row.Scan(&r.ID, &r.Kind, &r.Status, &r.TriggerType, &r.FlowID, &r.FlowGroupID, &r.SavedScriptID, &r.SQLScriptID,
		&r.DatasourceID, &r.ParentRunID, &r.PublishProjectID, &r.RowsWritten, &r.PublishName, &r.Destination, &r.Email, &r.Mirror,
		&r.Script, &r.Engine, &r.Error, &r.StartedAt, &r.FinishedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

// GetRun is session.get(Run, id).
func (b *Builder) GetRun(ctx context.Context, id int64) (*Run, error) {
	return scanRun(b.Q.QueryRow(ctx, `SELECT `+runColumns+` FROM runs WHERE id = $1`, id))
}

func (b *Builder) runs(ctx context.Context, where string, args ...any) ([]*Run, error) {
	rows, err := b.Q.Query(ctx, `SELECT `+runColumns+` FROM runs WHERE `+where, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Run
	for rows.Next() {
		r, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func str(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// jsonOf is _json: a JSON object, {} for anything else.
func jsonOf(text *string) pyjson.Object {
	t := str(text)
	if t == "" {
		t = "{}"
	}
	v, err := pyjson.Loads(t)
	if err != nil {
		return pyjson.Object{}
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return pyjson.Object{}
	}
	return o
}

// loadsDict is json.loads(text) caught on JSONDecodeError only: {} then; not an object, an
// error (the AttributeError of `.get`).
func loadsDict(text string) (pyjson.Object, error) {
	v, err := pyjson.Loads(text)
	if err != nil {
		return pyjson.Object{}, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, errNotADict
	}
	return o, nil
}

// isInt is isinstance(v, int) of a JSON value (a bool is an int to Python).
func isInt(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

// isIntNotBool is isinstance(i, int) and not isinstance(i, bool).
func isIntNotBool(v any) (int64, bool) {
	x, ok := v.(int64)
	return x, ok
}

func (b *Builder) runFacet(r *Run, engine string, version any) pyjson.Object {
	fields := []pyjson.KV{{Key: "kind", Value: r.Kind}, {Key: "trigger", Value: orStr(r.TriggerType, "manual")},
		{Key: "runId", Value: r.ID}}
	if engine == "" {
		engine = str(r.Engine)
	}
	if engine != "" {
		fields = append(fields, pyjson.KV{Key: "engine", Value: engine})
	}
	if r.ParentRunID != nil {
		fields = append(fields, pyjson.KV{Key: "parentRunId", Value: b.S.RunUUID(*r.ParentRunID)})
	}
	if version != nil {
		fields = append(fields, pyjson.KV{Key: "version", Value: version})
	}
	return facet(Producer+"/blob/main/docs/openlineage/TabulariaRunFacet.json", fields...)
}

func orStr(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

type described struct {
	job             Job
	inputs, outputs []Dataset
	runFacets       pyjson.Object
}

// describe is _describe: (job, inputs, outputs, run facets) of a closed run, or nil when the
// run is not told.
func (b *Builder) describe(ctx context.Context, r *Run, top bool) (*described, error) {
	rf := pyjson.Object{{Key: "tabularia", Value: b.runFacet(r, "", nil)}}
	switch {
	case r.Kind == "orchestration" && r.FlowGroupID != nil:
		g, err := b.GetGroup(ctx, *r.FlowGroupID)
		if err != nil || g == nil {
			return nil, err
		}
		var inputs, outputs []Dataset
		if top {
			children, err := b.runs(ctx, `parent_run_id = $1 ORDER BY id`, r.ID)
			if err != nil {
				return nil, err
			}
			for _, c := range children {
				d, err := b.describe(ctx, c, false)
				if err != nil {
					return nil, err
				}
				if d != nil {
					inputs = append(inputs, d.inputs...)
					outputs = append(outputs, d.outputs...)
				}
			}
		}
		job, err := b.objectJob(ctx, KindFlowGroup, g.Object, "FLOW_GROUP", nil)
		return &described{job, Unique(inputs), Unique(outputs), rf}, err
	case r.Kind == "orchestration" && r.SavedScriptID != nil:
		sc, err := b.GetObject(ctx, KindSavedScript, *r.SavedScriptID)
		if err != nil || sc == nil {
			return nil, err
		}
		children, err := b.runs(ctx, `parent_run_id = $1 AND kind = 'script' ORDER BY id DESC LIMIT 1`, r.ID)
		if err != nil {
			return nil, err
		}
		var child *Run
		if len(children) > 0 {
			child = children[0]
		}
		inputs, outputs, err := b.SavedScriptLineage(ctx, sc, child)
		if err != nil {
			return nil, err
		}
		if child != nil {
			version, _ := jsonOf(child.Script).Get("script_version")
			rf = rf.Set("tabularia", b.runFacet(r, str(child.Engine), version))
		}
		job, err := b.objectJob(ctx, KindSavedScript, sc, "SCRIPT", nil)
		return &described{job, inputs, outputs, rf}, err
	case r.Kind == "orchestration":
		f, err := b.flowOf(ctx, r.FlowID)
		if err != nil || f == nil {
			return nil, err
		}
		children, err := b.runs(ctx, `parent_run_id = $1 AND kind = 'flow'`, r.ID)
		if err != nil {
			return nil, err
		}
		outputs := []Dataset{}
		for _, c := range children {
			o, err := b.runOutputs(ctx, c)
			if err != nil {
				return nil, err
			}
			outputs = append(outputs, o...)
		}
		job, err := b.FlowJob(ctx, f, nil)
		if err != nil {
			return nil, err
		}
		inputs, err := b.FlowInputs(ctx, f)
		return &described{job, inputs, outputs, rf}, err
	case r.Kind == "flow":
		f, err := b.flowOf(ctx, r.FlowID)
		if err != nil || f == nil {
			return nil, err
		}
		if t, _ := jsonOf(r.Destination).Get("type"); t == "script_input" || t == "container_input" {
			return nil, nil // the input given to a Script or Container node: a step inside the flow
		}
		name, err := b.JobNameForFlow(ctx, f)
		if err != nil {
			return nil, err
		}
		label, err := outputLabel(r.ID, r.PublishName, r.Destination, r.Email)
		if err != nil {
			return nil, err
		}
		inputs, err := b.FlowInputs(ctx, f)
		if err != nil {
			return nil, err
		}
		outputs, err := b.runOutputs(ctx, r)
		return &described{Job{Name: name + "." + label, Facets: pyjson.Object{{Key: "jobType", Value: JobType("OUTPUT")}}}, inputs, outputs, rf}, err
	case r.Kind == "ingest":
		if r.DatasourceID == nil || *r.DatasourceID == 0 {
			return nil, nil
		}
		ds, err := b.GetDatasource(ctx, *r.DatasourceID)
		if err != nil || ds == nil {
			return nil, err
		}
		job, inputs, err := b.refreshJob(ctx, ds)
		if err != nil {
			return nil, err
		}
		var rows *int64
		if r.Status == "SUCCESS" {
			rows = r.RowsWritten
		}
		out, err := b.DatasourceDataset(ctx, ds, true, rows)
		return &described{job, inputs, []Dataset{out}, rf}, err
	case r.Kind == "sql":
		if r.SQLScriptID == nil || *r.SQLScriptID == 0 {
			return nil, nil
		}
		sc, err := b.GetObject(ctx, KindSQLScript, *r.SQLScriptID)
		if err != nil || sc == nil {
			return nil, err
		}
		inputs, outputs, facets, err := b.SQLScriptLineage(ctx, sc, r)
		if err != nil {
			return nil, err
		}
		version, _ := jsonOf(r.Script).Get("version")
		rf = rf.Set("tabularia", b.runFacet(r, "", version))
		job, err := b.objectJob(ctx, KindSQLScript, sc, "SQL_SCRIPT", facets)
		return &described{job, inputs, outputs, rf}, err
	case r.Kind == "script" || r.Kind == "container":
		info := jsonOf(r.Script)
		if pyjson.Truthy(info.Value("trial")) {
			return nil, nil // the editor's trial: not a run of the flow
		}
		jobType := "SCRIPT"
		if r.Kind == "container" {
			jobType = "CONTAINER"
		}
		facets := pyjson.Object{{Key: "jobType", Value: JobType(jobType)}}
		if r.Kind == "container" {
			cf, err := b.containerFacet(ctx, info)
			if err != nil {
				return nil, err
			}
			facets = append(facets, pyjson.KV{Key: "tabularia_container", Value: cf})
		}
		var parentJob string
		inputs := []Dataset{}
		if r.FlowID != nil {
			f, err := b.GetFlow(ctx, *r.FlowID)
			if err != nil || f == nil {
				return nil, err
			}
			if parentJob, err = b.JobNameForFlow(ctx, f); err != nil {
				return nil, err
			}
			if pyjson.Truthy(info.Value("input")) {
				if inputs, err = b.UpstreamInputs(ctx, f, info.Value("node_id")); err != nil {
					return nil, err
				}
			}
			ids, err := datasourceIDs(info.Value("inputs"))
			if err != nil {
				return nil, err
			}
			more, err := b.datasourceInputs(ctx, ids)
			if err != nil {
				return nil, err
			}
			inputs = Unique(append(inputs, more...))
		} else if r.Kind == "container" {
			g, err := b.groupOf(ctx, r)
			if err != nil || g == nil {
				return nil, err
			}
			if parentJob, err = b.JobNameFor(ctx, KindFlowGroup, g.Object); err != nil {
				return nil, err
			}
		} else {
			return nil, nil // a saved script's own run: told by its orchestration
		}
		label := r.Kind
		if v := info.Value("label"); pyjson.Truthy(v) {
			label = pyjson.Str(v)
		} else if v := info.Value("node_id"); pyjson.Truthy(v) {
			label = pyjson.Str(v)
		}
		return &described{Job{Name: parentJob + "." + r.Kind + " " + label, Facets: facets}, inputs, []Dataset{}, rf}, nil
	case r.Kind == "pushmail":
		g, err := b.groupOf(ctx, r)
		if err != nil || g == nil {
			return nil, err
		}
		email := jsonOf(r.Email)
		cell := "Pushmail"
		if v := email.Value("cell"); pyjson.Truthy(v) {
			cell = pyjson.Str(v)
		}
		var conn *Connection
		if id, ok := isInt(email.Value("connection_id")); ok {
			if conn, err = b.GetConnection(ctx, id); err != nil {
				return nil, err
			}
		}
		var host any = email.Value("host")
		port, connName := int64(587), ""
		if conn != nil {
			host, connName = conn.Host, conn.Name
			if conn.Port != nil && *conn.Port != 0 {
				port = *conn.Port
			}
		}
		outputs := []Dataset{}
		if pyjson.Truthy(host) {
			path, err := b.ProjectPath(ctx, &g.ProjectID)
			if err != nil {
				return nil, err
			}
			outputs = append(outputs, EmailDataset(pyjson.Str(host), port, pushmailDatasetName(path, g.Name, cell), connName))
		}
		groupJob, err := b.JobNameFor(ctx, KindFlowGroup, g.Object)
		if err != nil {
			return nil, err
		}
		var read []any
		seen := map[string]bool{}
		for _, k := range []string{"datasource_ids", "report_datasource_ids"} {
			v := email.Value(k)
			if !pyjson.Truthy(v) {
				continue
			}
			list, ok := v.([]any)
			if !ok {
				return nil, errNotADict
			}
			for _, x := range list {
				if key := hashKey(x); !seen[key] {
					seen[key] = true
					read = append(read, x)
				}
			}
		}
		inputs, err := b.datasourceInputs(ctx, read)
		return &described{Job{Name: groupJob + ".pushmail " + cell, Facets: pyjson.Object{{Key: "jobType", Value: JobType("PUSHMAIL")}}},
			inputs, outputs, rf}, err
	}
	return nil, nil
}

// hashKey is a dict key of a JSON value as Python compares them (1 == 1.0 == True).
func hashKey(v any) string {
	switch x := v.(type) {
	case bool:
		if x {
			return "n1"
		}
		return "n0"
	case int64:
		return fmt.Sprintf("n%d", x)
	case float64:
		if x == float64(int64(x)) {
			return fmt.Sprintf("n%d", int64(x))
		}
		return fmt.Sprintf("f%v", x)
	case string:
		return "s" + x
	case nil:
		return "N"
	}
	return fmt.Sprintf("o%p", v)
}

func (b *Builder) flowOf(ctx context.Context, id *int64) (*Flow, error) {
	if id == nil || *id == 0 {
		return nil, nil
	}
	return b.GetFlow(ctx, *id)
}

// GetFlow is session.get(Flow, id).
func (b *Builder) GetFlow(ctx context.Context, id int64) (*Flow, error) {
	var f Flow
	err := b.Q.QueryRow(ctx, `SELECT id, project_id, name, description, definition FROM flows WHERE id = $1`, id).
		Scan(&f.ID, &f.ProjectID, &f.Name, &f.Description, &f.Definition)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &f, err
}

// groupOf is _group_of: the flow group whose run `r` is a cell of.
func (b *Builder) groupOf(ctx context.Context, r *Run) (*Group, error) {
	if r.ParentRunID == nil {
		return nil, nil
	}
	parent, err := b.GetRun(ctx, *r.ParentRunID)
	if err != nil || parent == nil || parent.FlowGroupID == nil || *parent.FlowGroupID == 0 {
		return nil, err
	}
	return b.GetGroup(ctx, *parent.FlowGroupID)
}

// outputLabel is _output_label.
func outputLabel(id int64, publishName, destination, email *string) (string, error) {
	if str(publishName) != "" {
		return "datasource " + *publishName, nil
	}
	if str(destination) != "" {
		v, err := pyjson.Loads(*destination)
		if err == nil {
			d, ok := v.(pyjson.Object)
			if !ok {
				return "", errNotADict
			}
			switch d.Value("type") {
			case "sftp":
				return "file /" + SFTPPath(text(d.Value("folder")), text(d.Value("file_name"))), nil
			case "database":
				return "table " + pyjson.Str(d.Value("table")), nil
			}
			return "file " + pyjson.Str(d.Value("key")), nil
		}
	}
	if str(email) != "" {
		return "email", nil
	}
	if id == 0 {
		return "output None", nil
	}
	return fmt.Sprintf("output %d", id), nil
}

// runOutputs is _run_outputs: the outputs of ONE Output node run, from the run itself.
func (b *Builder) runOutputs(ctx context.Context, r *Run) ([]Dataset, error) {
	outputs := []Dataset{}
	if r.DatasourceID != nil && str(r.PublishName) != "" {
		ds, err := b.GetDatasource(ctx, *r.DatasourceID)
		if err != nil {
			return nil, err
		}
		if ds != nil {
			x, err := b.DatasourceDataset(ctx, ds, true, r.RowsWritten)
			if err != nil {
				return nil, err
			}
			outputs = append(outputs, x)
		}
	}
	if str(r.Destination) != "" {
		dest, err := loadsDict(*r.Destination)
		if err != nil {
			return nil, err
		}
		conn, err := b.connection(ctx, dest.Value("connection_id"))
		if err != nil {
			return nil, err
		}
		switch t := dest.Value("type"); {
		case t == "database" && conn != nil && pyjson.Truthy(dest.Value("table")):
			outputs = append(outputs, TableDataset(conn, text(dest.Value("table")), true, r.RowsWritten, text(dest.Value("mode"))))
		case t == "s3" && pyjson.Truthy(dest.Value("bucket")) && pyjson.Truthy(dest.Value("key")):
			outputs = append(outputs, ObjectDataset(text(dest.Value("endpoint")), text(dest.Value("bucket")), text(dest.Value("key")),
				text(dest.Value("format")), true, r.RowsWritten, text(dest.Value("written_key"))))
		case t == "sftp" && pyjson.Truthy(dest.Value("host")) && (pyjson.Truthy(dest.Value("path")) || pyjson.Truthy(dest.Value("file_name"))):
			outputs = append(outputs, SFTPDataset(text(dest.Value("host")), SFTPPath(text(dest.Value("folder")), text(dest.Value("file_name"))),
				text(dest.Value("format")), true, r.RowsWritten, text(dest.Value("path"))))
		}
	}
	if str(r.Email) != "" && r.Kind == "flow" && r.FlowID != nil {
		e := jsonOf(r.Email)
		var conn *Connection
		if id, ok := isInt(e.Value("connection_id")); ok {
			var err error
			if conn, err = b.GetConnection(ctx, id); err != nil {
				return nil, err
			}
		}
		f, err := b.GetFlow(ctx, *r.FlowID)
		if err != nil {
			return nil, err
		}
		if f != nil && (conn != nil || pyjson.Truthy(e.Value("host"))) {
			host, port, connName := pyjson.Str(e.Value("host")), int64(587), ""
			if conn != nil {
				host, connName = conn.Host, conn.Name
				if conn.Port != nil && *conn.Port != 0 {
					port = *conn.Port
				}
			}
			name, err := b.FlowEmailName(ctx, f)
			if err != nil {
				return nil, err
			}
			outputs = append(outputs, EmailDataset(host, port, name, connName))
		}
	}
	if str(r.Mirror) != "" {
		m, err := loadsDict(*r.Mirror)
		if err != nil {
			return nil, err
		}
		if pyjson.Truthy(m.Value("bucket")) && pyjson.Truthy(m.Value("key")) && m.Value("ok") != false {
			outputs = append(outputs, ObjectDataset(text(m.Value("endpoint")), text(m.Value("bucket")), text(m.Value("key")), "parquet",
				true, r.RowsWritten, ""))
		}
	}
	return outputs, nil
}

func withoutStatistics(outputs []Dataset) []Dataset {
	out := make([]Dataset, len(outputs))
	for i, o := range outputs {
		o.OutputFacets = pyjson.Object{}
		o.Output = true
		out[i] = o
	}
	return out
}

// parentFacet is _parent_facet: only for the steps that exist only inside their parent.
func (b *Builder) parentFacet(ctx context.Context, r *Run) (pyjson.Object, error) {
	if r.ParentRunID == nil {
		return nil, nil
	}
	var parentJob string
	switch {
	case (r.Kind == "flow" || r.Kind == "script" || r.Kind == "container") && r.FlowID != nil:
		parent, err := b.GetRun(ctx, *r.ParentRunID)
		if err != nil || parent == nil || parent.FlowID == nil || *parent.FlowID != *r.FlowID {
			return nil, err
		}
		f, err := b.GetFlow(ctx, *parent.FlowID)
		if err != nil || f == nil {
			return nil, err
		}
		if parentJob, err = b.JobNameForFlow(ctx, f); err != nil {
			return nil, err
		}
	case (r.Kind == "container" || r.Kind == "pushmail") && r.FlowID == nil:
		g, err := b.groupOf(ctx, r)
		if err != nil || g == nil {
			return nil, err
		}
		if parentJob, err = b.JobNameFor(ctx, KindFlowGroup, g.Object); err != nil {
			return nil, err
		}
	default:
		return nil, nil
	}
	return facet(std("1-2-0", "ParentRunFacet"),
		pyjson.KV{Key: "job", Value: pyjson.Object{{Key: "facets", Value: pyjson.Object{}}, {Key: "name", Value: parentJob},
			{Key: "namespace", Value: b.S.Namespace}}},
		pyjson.KV{Key: "run", Value: pyjson.Object{{Key: "facets", Value: pyjson.Object{}}, {Key: "runId", Value: b.S.RunUUID(*r.ParentRunID)}}}), nil
}

// runEvent is a RunEvent as Serde writes it.
func (b *Builder) runEvent(when, eventType, runID string, runFacets pyjson.Object, job Job, inputs, outputs []Dataset) pyjson.Object {
	return pyjson.Object{{Key: "eventTime", Value: when}, {Key: "producer", Value: Producer},
		{Key: "schemaURL", Value: "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"},
		{Key: "eventType", Value: eventType},
		{Key: "run", Value: pyjson.Object{{Key: "runId", Value: runID}, {Key: "facets", Value: runFacets}}},
		{Key: "job", Value: job.json(b.S.Namespace)}, {Key: "inputs", Value: datasets(inputs)}, {Key: "outputs", Value: datasets(outputs)}}
}

func errorMessage(message string) pyjson.Object {
	return facet(std("1-0-1", "ErrorMessageRunFacet"), pyjson.KV{Key: "message", Value: errorText(message)},
		pyjson.KV{Key: "programmingLanguage", Value: "python"})
}

// RunEvents is run_events: the run's START and COMPLETE/FAIL, built when the run has CLOSED.
func (b *Builder) RunEvents(ctx context.Context, runID int64) ([]pyjson.Object, error) {
	r, err := b.GetRun(ctx, runID)
	if err != nil || r == nil {
		return nil, err
	}
	d, err := b.describe(ctx, r, true)
	if err != nil || d == nil {
		return nil, err
	}
	parent, err := b.parentFacet(ctx, r)
	if err != nil {
		return nil, err
	}
	if parent != nil {
		d.runFacets = append(d.runFacets, pyjson.KV{Key: "parent", Value: parent})
	}
	id := b.S.RunUUID(r.ID)
	start := b.runEvent(iso(&r.StartedAt), "START", id, d.runFacets, d.job, d.inputs, withoutStatistics(d.outputs))
	end := append(pyjson.Object{}, d.runFacets...)
	if r.Status != "SUCCESS" && str(r.Error) != "" {
		end = append(end, pyjson.KV{Key: "errorMessage", Value: errorMessage(*r.Error)})
	}
	state := "FAIL"
	if r.Status == "SUCCESS" {
		state = "COMPLETE"
	}
	return []pyjson.Object{start, b.runEvent(iso(r.FinishedAt), state, id, end, d.job, d.inputs, d.outputs)}, nil
}
