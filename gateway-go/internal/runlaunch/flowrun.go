package runlaunch

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// Flow is the flow a run is launched for: the columns _launch_flow_run reads.
type Flow struct {
	ID, ProjectID    int64
	Name, Engine     string
	ProductionEngine *string
}

// FlowEnv is what a flow's run is launched with.
type FlowEnv struct {
	DB     *pgxpool.Pool
	Engine *engine.Client
	Bucket string // ENGINE__BUCKET
	Log    *slog.Logger
}

// FlowRunOptions are _launch_flow_run's keyword arguments.
type FlowRunOptions struct {
	TriggerType string // "" = manual
	ParentRunID *int64
	EngineMode  string // "" = development
	DryRunTo    string // the email's trial: the only recipient, set by the server
	// ScriptDestination is a Script node's input (payload, summary), built by the caller
	ScriptDestination *ScriptDestination
}

// ScriptDestination is script_destination.
type ScriptDestination struct {
	Payload pyjson.Object
	Summary string
}

// ResolveRunEngine is resolve_run_engine.
func ResolveRunEngine(f *Flow, engineMode string) string {
	if engineMode == "production" && f.ProductionEngine != nil && *f.ProductionEngine != "" {
		return *f.ProductionEngine
	}
	return f.Engine
}

func get(mv *pyd.ModelValue, name string) any { return mv.Get(name) }

func str(mv *pyd.ModelValue, name string) string {
	s, _ := mv.Get(name).(string)
	return s
}

func model(mv *pyd.ModelValue, name string) *pyd.ModelValue {
	m, _ := mv.Get(name).(*pyd.ModelValue)
	return m
}

// id is an Int field as a row id: ok false when no row can have it.
func id(v any) (int64, bool) {
	switch n := v.(type) {
	case int64:
		return n, n >= -1<<31 && n <= 1<<31-1
	case *big.Int:
		return 0, false
	}
	return 0, false
}

// dumps is json.dumps(v): the summaries the runs keep.
func dumps(v any) string {
	s, _ := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
	return s
}

func unprocessable(detail string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: detail}
}

// templated is _templated.
func templated(t string) bool { return strings.ContainsAny(t, "{}") }

// zone is _zone: the Output's time zone, only when it is not UTC.
func zone(tz string) pyjson.Object {
	tz = pystr.Strip(tz)
	if tz != "" && tz != "UTC" {
		return pyjson.Object{{Key: "timezone", Value: tz}}
	}
	return pyjson.Object{}
}

// templateContext is _template_context.
func templateContext(f *Flow, tz string) pyjson.Object {
	return append(pyjson.Object{{Key: "flow", Value: f.Name}, {Key: "run_id", Value: nil}}, zone(tz)...)
}

type template struct{ name, text, target string }

// checkOutputTemplates is _check_output_templates: the Output's placeholders checked by the
// engine's checker before anything is queued. An engine that cannot answer checks them at
// run time instead.
func checkOutputTemplates(ctx context.Context, env FlowEnv, templates []template, tz, flowName, prefix string) (*auth.Refusal, error) {
	wanted, targets := pyjson.Object{}, pyjson.Object{}
	var names []string
	for _, t := range templates {
		if templated(t.text) {
			wanted = wanted.Set(t.name, t.text)
			targets = targets.Set(t.name, t.target)
			names = append(names, t.name)
		}
	}
	zoned := len(zone(tz)) > 0
	if len(wanted) == 0 && !zoned {
		return nil, nil
	}
	timezone := pystr.Strip(tz)
	if timezone == "" {
		timezone = "UTC"
	}
	payload := pyjson.Object{{Key: "kind", Value: "output"}, {Key: "templates", Value: wanted}, {Key: "timezone", Value: timezone},
		{Key: "targets", Value: targets}, {Key: "flow", Value: flowName}}
	cctx, cancel := context.WithTimeout(ctx, engine.Check)
	defer cancel()
	res, raw, err := env.Engine.JSON(cctx, http.MethodPost, "/placeholders/preview", payload)
	if err != nil {
		env.Log.Warn("output templates not checked before the run", "err", err)
		return nil, nil
	}
	if res.StatusCode >= 400 {
		env.Log.Warn("output templates not checked before the run", "status", res.StatusCode)
		return nil, nil
	}
	v, err := pyjson.Loads(string(raw))
	answer, ok := v.(pyjson.Object)
	if err != nil || !ok {
		return nil, ErrPython
	}
	if e := answer.Value("error"); pyjson.Truthy(e) {
		return unprocessable(prefix + pyjson.Str(e)), nil
	}
	fieldsV := pyjson.Or(answer.Value("fields"), pyjson.Object{})
	fields, ok := fieldsV.(pyjson.Object)
	if !ok && len(names) > 0 {
		return nil, ErrPython
	}
	for _, name := range names {
		fv := pyjson.Or(fields.Value(name), pyjson.Object{})
		f, ok := fv.(pyjson.Object)
		if !ok {
			return nil, ErrPython
		}
		if e := f.Value("error"); pyjson.Truthy(e) {
			return unprocessable(fmt.Sprintf("%s%s: %s", prefix, name, pyjson.Str(e))), nil
		}
	}
	return nil, nil
}

var placeholderText = regexp.MustCompile(`\{\{|\}\}|\{[^{}]*\}`)

// checkSFTPFileName is _check_sftp_file_name.
func checkSFTPFileName(fileName string) *auth.Refusal {
	if fileName == "" {
		return unprocessable("SFTP: the file name is empty")
	}
	if outside := placeholderText.ReplaceAllString(fileName, ""); strings.ContainsAny(outside, "/\\") {
		return unprocessable("SFTP: the file name cannot contain a folder: put the folder in its own field")
	}
	return nil
}

func loadConn(ctx context.Context, db DB, v any) (*connpayload.Conn, error) {
	cid, ok := id(v)
	if !ok {
		return nil, nil
	}
	return connpayload.Load(ctx, db, cid)
}

func strList(v any) []string {
	items, _ := v.([]any)
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, it.(string))
	}
	return out
}

func anyList(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

// LaunchFlowRun is _launch_flow_run: a flow's run queued on the engine with the authority of
// `user` (RUN on the flow, EDIT to publish, CONNECT for every destination), its row written.
// The body is a validated RunCreate. Returns the run's id, or the refusal.
func LaunchFlowRun(ctx context.Context, env FlowEnv, user *auth.User, f *Flow, body *pyd.ModelValue, o FlowRunOptions) (int64, *auth.Refusal, error) {
	c := perm.For(env.DB, user)
	if ref, err := c.Ensure(ctx, f.ProjectID, perm.Run); ref != nil || err != nil {
		return 0, ref, err
	}
	trigger := o.TriggerType
	if trigger == "" {
		trigger = "manual"
	}
	engineMode := o.EngineMode
	if engineMode == "" {
		engineMode = "development"
	}
	engineName := ResolveRunEngine(f, engineMode)
	operations, _ := pydconv.FromPyd(get(body, "operations")).([]any)
	if operations == nil {
		operations = []any{}
	}
	if engineMode != "development" {
		var removed int
		operations, removed = flowresolver.StripDevSampleOps(operations)
		if removed > 0 {
			env.Log.Warn("production run: development samples removed", "flow", f.ID, "removed", removed)
		}
	}
	bucket, inputKey := str(body, "bucket"), str(body, "input_key")
	if bucket == "" {
		bucket = env.Bucket
	}
	read := pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "input_key", Value: inputKey}, {Key: "operations", Value: operations}}
	if ref, err := objects.EnsureReadsPinned(ctx, c, read, env.Bucket); ref != nil || err != nil {
		return 0, ref, err
	}
	if ref, err := objects.EnsureCanReadKeys(ctx, c, env.DB, user, objects.CollectStorageKeys(read)); ref != nil || err != nil {
		return 0, ref, err
	}
	publish := model(body, "publish")
	if o.DryRunTo != "" {
		publish = nil
	}
	publishKeys := []string{}
	if publish != nil {
		for _, k := range strList(get(publish, "sort_keys")) {
			if pystr.Strip(k) != "" {
				publishKeys = append(publishKeys, pystr.Strip(k))
			}
		}
	}
	if len(publishKeys) > 0 {
		operations = append(append([]any{}, operations...), pyjson.Object{{Key: "type", Value: "sort"},
			{Key: "params", Value: pyjson.Object{{Key: "by", Value: anyList(publishKeys)}, {Key: "ignore_missing", Value: true}}}})
	}

	var publishTarget *int64
	if publish != nil {
		pid, ok := id(get(publish, "project_id"))
		found := false
		if ok {
			var one int
			err := env.DB.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, pid).Scan(&one)
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return 0, nil, err
			}
			found = err == nil
		}
		if !found {
			return 0, &auth.Refusal{Status: http.StatusNotFound, Detail: "Destination folder not found"}, nil
		}
		if ref, err := c.Ensure(ctx, pid, perm.Edit); ref != nil || err != nil {
			return 0, ref, err
		}
		name := pystr.Strip(str(publish, "name"))
		if name == "" {
			return 0, unprocessable("The datasource needs a name"), nil
		}
		var existing int64
		var kind string
		err := env.DB.QueryRow(ctx, `SELECT id, kind FROM datasources WHERE project_id = $1 AND name = $2 ORDER BY id LIMIT 1`, pid, name).
			Scan(&existing, &kind)
		switch {
		case err == nil:
			if get(publish, "overwrite") != true {
				return 0, &auth.Refusal{Status: http.StatusConflict,
					Detail: fmt.Sprintf("There is already a datasource '%s' in this folder", str(publish, "name"))}, nil
			}
			if kind != "flow" {
				return 0, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("'%s' is a datasource of kind «%s»: "+
					"a flow cannot overwrite it, choose another name", str(publish, "name"), kind)}, nil
			}
			publishTarget = &existing
		case !errors.Is(err, pgx.ErrNoRows):
			return 0, nil, err
		}
	}

	// the Output's destination: the connection by id, its payload built here
	var destPayload pyjson.Object
	var destSummary *string
	dest := model(body, "destination")
	if o.DryRunTo != "" {
		dest = nil
	}
	if o.ScriptDestination != nil {
		destPayload = o.ScriptDestination.Payload
		destSummary = &o.ScriptDestination.Summary
	} else if dest != nil {
		conn, err := loadConn(ctx, env.DB, get(dest, "connection_id"))
		if err != nil {
			return 0, nil, err
		}
		if conn == nil {
			return 0, &auth.Refusal{Status: http.StatusNotFound, Detail: "Connection not found"}, nil
		}
		if ref, err := c.Ensure(ctx, conn.ProjectID, perm.Connect); ref != nil || err != nil {
			return 0, ref, err
		}
		tz, format := str(dest, "timezone"), str(dest, "format")
		switch str(dest, "type") {
		case "sftp":
			if conn.DBType != "sftp" {
				return 0, unprocessable("The chosen connection is not SFTP"), nil
			}
			fileName, folder := pystr.Strip(str(dest, "file_name")), pystr.Strip(str(dest, "folder"))
			if ref := checkSFTPFileName(fileName); ref != nil {
				return 0, ref, nil
			}
			for _, part := range strings.Split(strings.ReplaceAll(folder, "\\", "/"), "/") {
				if part == ".." {
					return 0, unprocessable("SFTP: '..' is not allowed in the folder: write the folder you mean"), nil
				}
			}
			if format != "csv" && format != "parquet" && format != "xlsx" {
				return 0, unprocessable("SFTP outputs are CSV, parquet or Excel"), nil
			}
			if ref, err := checkOutputTemplates(ctx, env, []template{{"file name", fileName, "file"}, {"folder", folder, "path"}}, tz, f.Name,
				"SFTP: "); ref != nil || err != nil {
				return 0, ref, err
			}
			destPayload = pyjson.Object{{Key: "type", Value: "sftp"}, {Key: "connection", Value: connpayload.Payload(conn)},
				{Key: "target", Value: append(pyjson.Object{{Key: "folder", Value: folder}, {Key: "file_name", Value: fileName},
					{Key: "format", Value: format}, {Key: "run_id", Value: nil}, {Key: "flow", Value: f.Name}}, zone(tz)...)}}
			port := "22"
			if conn.Port != nil && *conn.Port != 0 {
				port = fmt.Sprint(*conn.Port)
			}
			s := dumps(append(pyjson.Object{{Key: "type", Value: "sftp"}, {Key: "connection_id", Value: conn.ID},
				{Key: "host", Value: conn.Host + ":" + port}, {Key: "folder", Value: folder}, {Key: "file_name", Value: fileName},
				{Key: "format", Value: format}}, zone(tz)...))
			destSummary = &s
		case "s3":
			if format != "parquet" && format != "csv" {
				return 0, unprocessable("S3 outputs are parquet or CSV"), nil
			}
			if conn.DBType != "s3" {
				return 0, unprocessable("The chosen connection is not S3/object storage"), nil
			}
			key := strings.Trim(pystr.Strip(str(dest, "key")), "/")
			if key == "" {
				return 0, unprocessable("The S3 key/path is empty"), nil
			}
			bucketOverride := pystr.Strip(str(dest, "bucket"))
			if bucketOverride == "" && pystr.Strip(conn.Database) == "" {
				return 0, unprocessable("No bucket: set one on the output or as the connection's default"), nil
			}
			if ref, err := checkOutputTemplates(ctx, env, []template{{"S3 key", key, "path"}}, tz, f.Name, "S3: "); ref != nil || err != nil {
				return 0, ref, err
			}
			partition := pydconv.FromPyd(get(dest, "partition_by"))
			target := pyjson.Object{{Key: "bucket", Value: bucketOverride}, {Key: "key", Value: key}, {Key: "format", Value: format},
				{Key: "partition_by", Value: partition}}
			if templated(key) {
				target = append(target, templateContext(f, tz)...)
			}
			destPayload = pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection", Value: connpayload.Payload(conn)}, {Key: "target", Value: target}}
			endpoint, shownBucket := conn.Host, bucketOverride
			if endpoint == "" {
				endpoint = "aws"
			}
			if shownBucket == "" {
				shownBucket = conn.Database
			}
			s := dumps(append(pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection_id", Value: conn.ID}, {Key: "db_type", Value: "s3"},
				{Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: shownBucket}, {Key: "key", Value: key}, {Key: "format", Value: format},
				{Key: "partition_by", Value: partition}}, zone(tz)...))
			destSummary = &s
		default:
			if conn.DBType == "s3" {
				return 0, unprocessable("The chosen connection is S3: use an S3 destination"), nil
			}
			if connpayload.NotDatabases[conn.DBType] {
				return 0, unprocessable(fmt.Sprintf("A %s connection cannot receive a table: a database connection is needed", conn.DBType)), nil
			}
			table := pystr.Strip(str(dest, "table"))
			if table == "" {
				return 0, unprocessable("The destination table name is empty"), nil
			}
			destPayload = pyjson.Object{{Key: "type", Value: "database"}, {Key: "connection", Value: connpayload.Payload(conn)},
				{Key: "target", Value: pyjson.Object{{Key: "table", Value: table}, {Key: "mode", Value: str(dest, "mode")},
					{Key: "post_sql", Value: str(dest, "post_sql")}}}}
			s := dumps(pyjson.Object{{Key: "type", Value: "database"}, {Key: "connection_id", Value: conn.ID}, {Key: "db_type", Value: conn.DBType},
				{Key: "host", Value: conn.Host}, {Key: "database", Value: conn.Database}, {Key: "table", Value: table},
				{Key: "mode", Value: str(dest, "mode")}})
			destSummary = &s
		}
	}

	// a copy to an external S3 as well as the datasource: best effort, never failing the run
	var mirrorPayload pyjson.Object
	var mirrorSummary *string
	mirror := model(body, "mirror")
	if o.DryRunTo != "" {
		mirror = nil
	}
	if mirror != nil {
		mconn, err := loadConn(ctx, env.DB, get(mirror, "connection_id"))
		if err != nil {
			return 0, nil, err
		}
		if mconn == nil {
			return 0, &auth.Refusal{Status: http.StatusNotFound, Detail: "S3 copy connection not found"}, nil
		}
		if ref, err := c.Ensure(ctx, mconn.ProjectID, perm.Connect); ref != nil || err != nil {
			return 0, ref, err
		}
		if mconn.DBType != "s3" {
			return 0, unprocessable("The copy's connection is not S3/object storage"), nil
		}
		mkey, mbucket, tz := strings.Trim(pystr.Strip(str(mirror, "key")), "/"), pystr.Strip(str(mirror, "bucket")), str(mirror, "timezone")
		refused := ""
		switch {
		case mkey == "":
			refused = "the copy's S3 path is empty: set it on the Output node"
		case mbucket == "" && pystr.Strip(mconn.Database) == "":
			refused = "no bucket for the copy: set one on the output or as the connection's default"
		default:
			ref, err := checkOutputTemplates(ctx, env, []template{{"copy's S3 key", mkey, "path"}}, tz, f.Name, "S3: ")
			if err != nil {
				return 0, nil, err
			}
			if ref != nil {
				refused = ref.Detail
			}
		}
		endpoint, shownBucket := mconn.Host, mbucket
		if endpoint == "" {
			endpoint = "aws"
		}
		if shownBucket == "" {
			shownBucket = mconn.Database
		}
		if refused != "" {
			s := dumps(pyjson.Object{{Key: "connection_id", Value: mconn.ID}, {Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: shownBucket},
				{Key: "key", Value: mkey}, {Key: "format", Value: "parquet"}, {Key: "ok", Value: false}, {Key: "error", Value: cut(refused, 500)}})
			mirrorSummary = &s
		} else {
			target := pyjson.Object{}
			if templated(mkey) {
				target = append(target, templateContext(f, tz)...)
			}
			target = append(target, pyjson.KV{Key: "bucket", Value: mbucket}, pyjson.KV{Key: "key", Value: mkey},
				pyjson.KV{Key: "format", Value: "parquet"}, pyjson.KV{Key: "partition_by", Value: []any{}})
			mirrorPayload = pyjson.Object{{Key: "connection", Value: connpayload.Payload(mconn)}, {Key: "target", Value: target}}
			s := dumps(pyjson.Object{{Key: "connection_id", Value: mconn.ID}, {Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: shownBucket},
				{Key: "key", Value: mkey}, {Key: "format", Value: "parquet"}, {Key: "ok", Value: nil}})
			mirrorSummary = &s
		}
	}

	// the output as an email attachment: the allowed domains are the barrier, applied here
	var emailPayload pyjson.Object
	var emailSummary *string
	if email := model(body, "email"); email != nil {
		econn, err := loadConn(ctx, env.DB, get(email, "connection_id"))
		if err != nil {
			return 0, nil, err
		}
		if econn == nil {
			return 0, &auth.Refusal{Status: http.StatusNotFound, Detail: "SMTP connection not found"}, nil
		}
		if ref, err := c.Ensure(ctx, econn.ProjectID, perm.Connect); ref != nil || err != nil {
			return 0, ref, err
		}
		if econn.DBType != "smtp" {
			return 0, unprocessable("The chosen connection is not SMTP"), nil
		}
		var to, cc, recipients []string
		if o.DryRunTo != "" {
			to, cc, recipients = []string{o.DryRunTo}, []string{}, []string{o.DryRunTo}
		} else {
			to, cc = []string{}, []string{}
			for _, a := range strList(get(email, "to")) {
				if pystr.Strip(a) != "" {
					to = append(to, pystr.Strip(a))
				}
			}
			for _, a := range strList(get(email, "cc")) {
				if pystr.Strip(a) != "" {
					cc = append(cc, pystr.Strip(a))
				}
			}
			recipients = append(append([]string{}, to...), cc...)
			if len(recipients) == 0 {
				return 0, unprocessable("The email has no recipients"), nil
			}
			allowed, ok := connpayload.AllowedEmailDomains(econn)
			if !ok {
				return 0, unprocessable(connpayload.MalformedDomains(econn)), nil
			}
			if len(allowed) > 0 {
				in := map[string]bool{}
				for _, a := range allowed {
					in[a] = true
				}
				var outside []string
				for _, a := range recipients {
					domain := a
					if i := strings.LastIndex(a, "@"); i >= 0 {
						domain = a[i+1:]
					}
					if !in[pystr.Lower(domain)] {
						outside = append(outside, a)
					}
				}
				if len(outside) > 0 {
					return 0, unprocessable(fmt.Sprintf("Recipients outside the domains this connection allows (%s): %s",
						strings.Join(allowed, ", "), strings.Join(outside, ", "))), nil
				}
			}
		}
		subject, attachment, tz := str(email, "subject"), str(email, "attachment_name"), str(email, "timezone")
		if attachment == "" {
			attachment = "report"
		}
		if ref, err := checkOutputTemplates(ctx, env, []template{{"subject", subject, "text"}, {"attachment name", attachment, "file"}}, tz,
			f.Name, ""); ref != nil || err != nil {
			return 0, ref, err
		}
		stop := get(email, "stop_on_failure")
		if o.DryRunTo != "" {
			stop = false
		}
		target := pyjson.Object{{Key: "to", Value: anyList(to)}, {Key: "cc", Value: anyList(cc)}, {Key: "subject", Value: subject},
			{Key: "body", Value: str(email, "body")}, {Key: "body_is_html", Value: get(email, "body_is_html")},
			{Key: "attachment_name", Value: attachment}, {Key: "attachment_format", Value: str(email, "attachment_format")}}
		if templated(subject) || templated(str(email, "attachment_name")) {
			target = append(target, templateContext(f, tz)...)
		}
		emailPayload = pyjson.Object{{Key: "connection", Value: connpayload.Payload(econn)}, {Key: "stop_on_failure", Value: stop},
			{Key: "target", Value: target}}
		s := dumps(pyjson.Object{{Key: "connection_id", Value: econn.ID}, {Key: "host", Value: econn.Host}, {Key: "to", Value: anyList(recipients)},
			{Key: "subject", Value: subject}, {Key: "attachment", Value: attachment}, {Key: "format", Value: str(email, "attachment_format")},
			{Key: "ok", Value: nil}})
		emailSummary = &s
		trig := trigger
		if o.DryRunTo != "" {
			trig = "dry_run"
		}
		kind := "flow"
		// at launch, not at reconciliation: who asked to send what to whom (no request: record_audit
		// without one writes neither the address nor the agent)
		audit.Record(context.Background(), env.DB, env.Log, audit.Event{Actor: user, Action: "email.send", TargetType: &kind,
			TargetID: &f.ID, TargetLabel: &f.Name, Detail: pyjson.Object{{Key: "connection_id", Value: econn.ID},
				{Key: "host", Value: econn.Host}, {Key: "to", Value: anyList(recipients)}, {Key: "subject", Value: subject},
				{Key: "format", Value: str(email, "attachment_format")}, {Key: "trigger", Value: trig}}})
	}

	outputKey := "out/" + Hex32() + ".parquet"
	if publish != nil {
		outputKey = SnapshotKey(publishTarget)
	}
	var publishName *string
	var publishProject *int64
	publishDescription, publishOverwrite := "", false
	if publish != nil {
		n := pystr.Strip(str(publish, "name"))
		publishName = &n
		pid, _ := id(get(publish, "project_id"))
		publishProject = &pid
		publishDescription, publishOverwrite = str(publish, "description"), get(publish, "overwrite") == true
	}
	insert := func(q DB, taskID string) (int64, error) {
		var rid int64
		err := q.QueryRow(ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, parent_run_id, engine, input_key,
			output_bucket, output_key, publish_name, publish_project_id, publish_description, publish_overwrite, publish_sort_keys,
			destination, mirror, email, started_at) VALUES ('flow', $1, $2, 'PENDING', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
			$15, $16, $17, $18) RETURNING id`, f.ID, taskID, user.ID, trigger, o.ParentRunID, engineName, inputKey, bucket, outputKey,
			publishName, publishProject, publishDescription, publishOverwrite, dumps(anyList(publishKeys)), destSummary, mirrorSummary,
			emailSummary, time.Now().UTC()).Scan(&rid)
		return rid, err
	}

	// the targets that name the run: the run row is written before the task is queued
	var numbered []pyjson.Object
	setRunID := func(payload pyjson.Object, always bool) {
		if payload == nil {
			return
		}
		t, _ := payload.Value("target").(pyjson.Object)
		if _, has := t.Get("run_id"); always || has {
			numbered = append(numbered, t)
		}
	}
	if destPayload != nil {
		setRunID(destPayload, destPayload.Value("type") == "sftp")
	}
	setRunID(mirrorPayload, false)
	setRunID(emailPayload, false)
	var early int64
	if len(numbered) > 0 {
		var err error
		if early, err = insert(env.DB, PlaceholderTask+Hex32()); err != nil {
			return 0, nil, err
		}
		for _, t := range numbered {
			t.Set("run_id", early) // the key exists: Set writes in place
		}
	}
	submit := func() (string, *auth.Refusal, error) {
		task := pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "input_key", Value: inputKey}, {Key: "output_key", Value: outputKey},
			{Key: "operations", Value: operations}, {Key: "destination", Value: nilIfEmpty(destPayload)},
			{Key: "mirror", Value: nilIfEmpty(mirrorPayload)}, {Key: "email", Value: nilIfEmpty(emailPayload)}, {Key: "engine", Value: engineName}}
		contract, err := ContractPayload(ctx, env.DB, publishTarget)
		if err != nil {
			return "", nil, err
		}
		if contract != nil {
			task = append(task, pyjson.KV{Key: "contract", Value: contract})
		}
		return SubmitTask(ctx, env.Engine, "/tasks/transform-data", task)
	}
	taskID, ref, err := submit()
	if err != nil || ref != nil {
		if early != 0 {
			if _, derr := env.DB.Exec(ctx, `DELETE FROM runs WHERE id = $1`, early); derr != nil {
				return 0, nil, derr
			}
		}
		return 0, ref, err
	}
	if early != 0 {
		_, err := env.DB.Exec(ctx, `UPDATE runs SET task_id = $1 WHERE id = $2`, taskID, early)
		return early, nil, err
	}
	rid, err := insert(env.DB, taskID)
	return rid, nil, err
}

func nilIfEmpty(o pyjson.Object) any {
	if o == nil {
		return nil
	}
	return o
}

func cut(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
