package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// routes/runs.py _launch_flow_run, as the orchestrator launches an Output node of a flow:
// the whole RBAC with the authority of the run's person, the destination, copy and email
// resolved by the gateway, the task queued on the engine and its run recorded.

// jobFlow is a flows row, the columns the orchestration reads.
type jobFlow struct {
	ID, ProjectID    int64
	Name, Definition string
	Engine           string
	ProductionEngine *string
}

func loadJobFlow(ctx context.Context, q perm.Querier, id int64) (*jobFlow, error) {
	var f jobFlow
	err := q.QueryRow(ctx, `SELECT id, project_id, name, definition, engine, production_engine FROM flows WHERE id = $1`, id).
		Scan(&f.ID, &f.ProjectID, &f.Name, &f.Definition, &f.Engine, &f.ProductionEngine)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &f, nil
}

// resolveRunEngine is resolve_run_engine.
func resolveRunEngine(f *jobFlow, mode string) string {
	if mode == "production" && f.ProductionEngine != nil && *f.ProductionEngine != "" {
		return *f.ProductionEngine
	}
	return f.Engine
}

// refusalError is an HTTPException raised inside the orchestration: str(e) is
// «status: detail» (Starlette's HTTPException.__str__).
type refusalError struct {
	Status int
	Detail string
}

func (e *refusalError) Error() string { return fmt.Sprintf("%d: %s", e.Status, e.Detail) }

func refusalOf(r *auth.Refusal) error { return &refusalError{Status: r.Status, Detail: r.Detail} }

// errorDetail is `getattr(e, "detail", None) or str(e)`.
func errorDetail(err error) string {
	var r *refusalError
	if errors.As(err, &r) && r.Detail != "" {
		return r.Detail
	}
	var h *httpError
	if errors.As(err, &h) && h.Detail != "" {
		return h.Detail
	}
	return err.Error()
}

// pyStrOfError is str(e): an HTTPException as «status: detail».
func pyStrOfError(err error) string {
	var h *httpError
	if errors.As(err, &h) {
		return fmt.Sprintf("%d: %s", h.Status, h.Detail)
	}
	return err.Error()
}

var sftpOutside = regexp.MustCompile(`\{\{|\}\}|\{[^{}]*\}`)

func templated(t string) bool { return strings.ContainsAny(t, "{}") }

// zoneOf is _zone.
func zoneOf(tz string) pyjson.Object {
	tz = pyStripText(tz)
	if tz != "" && tz != "UTC" {
		return pyjson.Object{{Key: "timezone", Value: tz}}
	}
	return pyjson.Object{}
}

// checkOutputTemplates is _check_output_templates: the placeholders checked by the engine's
// checker before anything is queued (an engine that cannot answer checks them at run time).
func (d *Deps) checkOutputTemplates(ctx context.Context, templates [][3]string, tz, flowName, prefix string) error {
	wanted := pyjson.Object{}
	targets := pyjson.Object{}
	for _, t := range templates {
		if templated(t[1]) {
			wanted = append(wanted, pyjson.KV{Key: t[0], Value: t[1]})
			targets = append(targets, pyjson.KV{Key: t[0], Value: t[2]})
		}
	}
	if len(wanted) == 0 && len(zoneOf(tz)) == 0 {
		return nil
	}
	zone := pyStripText(tz)
	if zone == "" {
		zone = "UTC"
	}
	cctx, cancel := withCheck(ctx)
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/placeholders/preview", pyjson.Object{{Key: "kind", Value: "output"},
		{Key: "templates", Value: wanted}, {Key: "timezone", Value: zone}, {Key: "targets", Value: targets}, {Key: "flow", Value: flowName}})
	cancel()
	if err != nil || res.StatusCode >= 400 {
		d.Log.Warn("output templates not checked before the run", "err", err)
		return nil
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return &pyJSONError{err}
	}
	answer := pyjson.Obj(v)
	if e := answer.Value("error"); pyjson.Truthy(e) {
		return &httpError{Status: 422, Detail: prefix + pyjson.Str(e)}
	}
	fields := pyjson.Obj(pyjson.Or(answer.Value("fields"), pyjson.Object{}))
	for _, kv := range wanted {
		e := pyjson.Obj(pyjson.Or(fields.Value(kv.Key), pyjson.Object{})).Value("error")
		if pyjson.Truthy(e) {
			return &httpError{Status: 422, Detail: fmt.Sprintf("%s%s: %s", prefix, kv.Key, pyjson.Str(e))}
		}
	}
	return nil
}

func str(o pyjson.Object, k string) string { s, _ := o.Value(k).(string); return s }

// launchFlowRun is _launch_flow_run for an orchestration's Output: the run's id.
func (d *Deps) launchFlowRun(ctx context.Context, user *auth.User, flow *jobFlow, req pyjson.Object, trigger string, parent *int64,
	engineMode string) (int64, error) {
	v, errs := gatewaymodels.RunCreate.Type().Validate(pydconv.ToPyd(req))
	if len(errs) > 0 {
		// pydantic's ValidationError: its text is not reproduced (see the report)
		return 0, fmt.Errorf("%d validation error for RunCreate", len(errs))
	}
	body := pydconv.FromPyd(v.(*pyd.ModelValue).Dump()).(pyjson.Object)
	c := perm.For(d.DB, user)
	if ref, err := c.Ensure(ctx, flow.ProjectID, perm.Run); err != nil || ref != nil {
		if err != nil {
			return 0, err
		}
		return 0, refusalOf(ref)
	}
	engineName := resolveRunEngine(flow, engineMode)
	ops, _ := body.Value("operations").([]any)
	if trigger != "trial" {
		var removed int
		if ops, removed = stripDevSampleOps(ops); removed > 0 {
			d.Log.Info("run of flow: development samples removed, it reads every record", "flow", flow.ID, "removed", removed)
		}
	}
	engineBucket := d.Config.EngineBucket
	bucket := str(body, "bucket")
	if bucket == "" {
		bucket = engineBucket
	}
	inputKey := str(body, "input_key")
	read := pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "input_key", Value: inputKey}, {Key: "operations", Value: ops}}
	if ref, err := objects.EnsureReadsPinned(ctx, c, read, engineBucket); err != nil || ref != nil {
		if err != nil {
			return 0, err
		}
		return 0, refusalOf(ref)
	}
	if ref, err := objects.EnsureCanReadKeys(ctx, c, d.DB, user, objects.CollectStorageKeys(read)); err != nil || ref != nil {
		if err != nil {
			return 0, err
		}
		return 0, refusalOf(ref)
	}
	publish := pyjson.Obj(body.Value("publish"))
	var publishKeys []any
	if publish != nil {
		keys, _ := publish.Value("sort_keys").([]any)
		for _, k := range keys {
			if s, _ := k.(string); s != "" && pyStripText(s) != "" {
				publishKeys = append(publishKeys, pyStripText(s))
			}
		}
	}
	if len(publishKeys) > 0 {
		ops = append(append([]any{}, ops...), pyjson.Object{{Key: "type", Value: "sort"}, {Key: "params", Value: pyjson.Object{
			{Key: "by", Value: publishKeys}, {Key: "ignore_missing", Value: true}}}})
	}
	refuse := func(status int, detail string) (int64, error) { return 0, &httpError{Status: status, Detail: detail} }
	var targetID *int64
	var pubName string
	if publish != nil {
		project, _ := publish.Value("project_id").(int64)
		var one int
		if err := d.DB.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, project).Scan(&one); errors.Is(err, pgx.ErrNoRows) {
			return refuse(404, "Destination folder not found")
		} else if err != nil {
			return 0, err
		}
		if ref, err := c.Ensure(ctx, project, perm.Edit); err != nil || ref != nil {
			if err != nil {
				return 0, err
			}
			return 0, refusalOf(ref)
		}
		pubName = pyStripText(str(publish, "name"))
		if pubName == "" {
			return refuse(422, "The datasource needs a name")
		}
		var id int64
		var kind string
		err := d.DB.QueryRow(ctx, `SELECT id, kind FROM datasources WHERE project_id = $1 AND name = $2 LIMIT 1`, project, pubName).Scan(&id, &kind)
		if err == nil {
			if publish.Value("overwrite") != true {
				return refuse(409, fmt.Sprintf("There is already a datasource '%s' in this folder", str(publish, "name")))
			}
			if kind != "flow" {
				return refuse(409, fmt.Sprintf("'%s' is a datasource of kind «%s»: a flow cannot overwrite it, choose another name",
					str(publish, "name"), kind))
			}
			targetID = &id
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return 0, err
		}
	}
	connOf := func(id any, missing string, capability perm.Capability) (*engineConn, error) {
		n, _ := id.(int64)
		conn, err := loadEngineConn(ctx, d.DB, n)
		if err != nil {
			return nil, err
		}
		if conn == nil {
			return nil, &httpError{Status: 404, Detail: missing}
		}
		if ref, err := c.Ensure(ctx, conn.ProjectID, capability); err != nil || ref != nil {
			if err != nil {
				return nil, err
			}
			return nil, refusalOf(ref)
		}
		return conn, nil
	}
	templateContext := func(tz string) pyjson.Object {
		return append(pyjson.Object{{Key: "flow", Value: flow.Name}, {Key: "run_id", Value: nil}}, zoneOf(tz)...)
	}
	var destination, mirror, email pyjson.Object
	var destSummary, mirrorSummary, emailSummary *string
	sp := func(o pyjson.Object) *string { s := pyDumps(o); return &s }
	if dest := pyjson.Obj(body.Value("destination")); dest != nil {
		conn, err := connOf(dest.Value("connection_id"), "Connection not found", perm.Connect)
		if err != nil {
			return 0, err
		}
		tz, format := str(dest, "timezone"), str(dest, "format")
		switch str(dest, "type") {
		case "sftp":
			if conn.DBType != "sftp" {
				return refuse(422, "The chosen connection is not SFTP")
			}
			fileName, folder := pyStripText(str(dest, "file_name")), pyStripText(str(dest, "folder"))
			if fileName == "" {
				return refuse(422, "SFTP: the file name is empty")
			}
			if strings.ContainsAny(sftpOutside.ReplaceAllString(fileName, ""), "/\\") {
				return refuse(422, "SFTP: the file name cannot contain a folder: put the folder in its own field")
			}
			for _, part := range strings.Split(strings.ReplaceAll(folder, "\\", "/"), "/") {
				if part == ".." {
					return refuse(422, "SFTP: '..' is not allowed in the folder: write the folder you mean")
				}
			}
			if format != "csv" && format != "parquet" && format != "xlsx" {
				return refuse(422, "SFTP outputs are CSV, parquet or Excel")
			}
			if err := d.checkOutputTemplates(ctx, [][3]string{{"file name", fileName, "file"}, {"folder", folder, "path"}}, tz, flow.Name,
				"SFTP: "); err != nil {
				return 0, err
			}
			destination = pyjson.Object{{Key: "type", Value: "sftp"}, {Key: "connection", Value: conn.payload()}, {Key: "target", Value: append(
				pyjson.Object{{Key: "folder", Value: folder}, {Key: "file_name", Value: fileName}, {Key: "format", Value: format},
					{Key: "run_id", Value: nil}, {Key: "flow", Value: flow.Name}}, zoneOf(tz)...)}}
			port := int64(22)
			if conn.Port != nil && *conn.Port != 0 {
				port = *conn.Port
			}
			destSummary = sp(append(pyjson.Object{{Key: "type", Value: "sftp"}, {Key: "connection_id", Value: conn.ID},
				{Key: "host", Value: fmt.Sprintf("%s:%d", conn.Host, port)}, {Key: "folder", Value: folder}, {Key: "file_name", Value: fileName},
				{Key: "format", Value: format}}, zoneOf(tz)...))
		case "s3":
			if format != "parquet" && format != "csv" {
				return refuse(422, "S3 outputs are parquet or CSV")
			}
			if conn.DBType != "s3" {
				return refuse(422, "The chosen connection is not S3/object storage")
			}
			key := strings.Trim(pyStripText(str(dest, "key")), "/")
			if key == "" {
				return refuse(422, "The S3 key/path is empty")
			}
			override := pyStripText(str(dest, "bucket"))
			if override == "" && pyStripText(conn.Database) == "" {
				return refuse(422, "No bucket: set one on the output or as the connection's default")
			}
			if err := d.checkOutputTemplates(ctx, [][3]string{{"S3 key", key, "path"}}, tz, flow.Name, "S3: "); err != nil {
				return 0, err
			}
			target := pyjson.Object{{Key: "bucket", Value: override}, {Key: "key", Value: key}, {Key: "format", Value: format},
				{Key: "partition_by", Value: dest.Value("partition_by")}}
			if templated(key) {
				target = append(target, templateContext(tz)...)
			}
			destination = pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection", Value: conn.payload()}, {Key: "target", Value: target}}
			endpoint, b := conn.Host, override
			if endpoint == "" {
				endpoint = "aws"
			}
			if b == "" {
				b = conn.Database
			}
			destSummary = sp(append(pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection_id", Value: conn.ID}, {Key: "db_type", Value: "s3"},
				{Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: b}, {Key: "key", Value: key}, {Key: "format", Value: format},
				{Key: "partition_by", Value: dest.Value("partition_by")}}, zoneOf(tz)...))
		default:
			if conn.DBType == "s3" {
				return refuse(422, "The chosen connection is S3: use an S3 destination")
			}
			if notDatabases[conn.DBType] {
				return refuse(422, fmt.Sprintf("A %s connection cannot receive a table: a database connection is needed", conn.DBType))
			}
			table := pyStripText(str(dest, "table"))
			if table == "" {
				return refuse(422, "The destination table name is empty")
			}
			destination = pyjson.Object{{Key: "type", Value: "database"}, {Key: "connection", Value: conn.payload()}, {Key: "target", Value: pyjson.Object{
				{Key: "table", Value: table}, {Key: "mode", Value: dest.Value("mode")}, {Key: "post_sql", Value: dest.Value("post_sql")}}}}
			destSummary = sp(pyjson.Object{{Key: "type", Value: "database"}, {Key: "connection_id", Value: conn.ID}, {Key: "db_type", Value: conn.DBType},
				{Key: "host", Value: conn.Host}, {Key: "database", Value: conn.Database}, {Key: "table", Value: table},
				{Key: "mode", Value: dest.Value("mode")}})
		}
	}
	if m := pyjson.Obj(body.Value("mirror")); m != nil {
		conn, err := connOf(m.Value("connection_id"), "S3 copy connection not found", perm.Connect)
		if err != nil {
			return 0, err
		}
		if conn.DBType != "s3" {
			return refuse(422, "The copy's connection is not S3/object storage")
		}
		key, b, tz := strings.Trim(pyStripText(str(m, "key")), "/"), pyStripText(str(m, "bucket")), str(m, "timezone")
		refused := ""
		switch {
		case key == "":
			refused = "the copy's S3 path is empty: set it on the Output node"
		case b == "" && pyStripText(conn.Database) == "":
			refused = "no bucket for the copy: set one on the output or as the connection's default"
		default:
			if err := d.checkOutputTemplates(ctx, [][3]string{{"copy's S3 key", key, "path"}}, tz, flow.Name, "S3: "); err != nil {
				var h *httpError
				if !errors.As(err, &h) {
					return 0, err
				}
				refused = h.Detail
			}
		}
		endpoint, shown := conn.Host, b
		if endpoint == "" {
			endpoint = "aws"
		}
		if shown == "" {
			shown = conn.Database
		}
		if refused != "" {
			mirrorSummary = sp(pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: shown},
				{Key: "key", Value: key}, {Key: "format", Value: "parquet"}, {Key: "ok", Value: false}, {Key: "error", Value: runes(refused, 500)}})
		} else {
			target := pyjson.Object{}
			if templated(key) {
				target = templateContext(tz)
			}
			target = append(target, pyjson.Object{{Key: "bucket", Value: b}, {Key: "key", Value: key}, {Key: "format", Value: "parquet"},
				{Key: "partition_by", Value: []any{}}}...)
			mirror = pyjson.Object{{Key: "connection", Value: conn.payload()}, {Key: "target", Value: target}}
			mirrorSummary = sp(pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "endpoint", Value: endpoint}, {Key: "bucket", Value: shown},
				{Key: "key", Value: key}, {Key: "format", Value: "parquet"}, {Key: "ok", Value: nil}})
		}
	}
	if e := pyjson.Obj(body.Value("email")); e != nil {
		conn, err := connOf(e.Value("connection_id"), "SMTP connection not found", perm.Connect)
		if err != nil {
			return 0, err
		}
		if conn.DBType != "smtp" {
			return refuse(422, "The chosen connection is not SMTP")
		}
		clean := func(k string) []any {
			out := []any{}
			list, _ := e.Value(k).([]any)
			for _, a := range list {
				if s := pyStripText(pyjson.Str(a)); s != "" {
					out = append(out, s)
				}
			}
			return out
		}
		to, cc := clean("to"), clean("cc")
		recipients := append(append([]any{}, to...), cc...)
		if len(recipients) == 0 {
			return refuse(422, "The email has no recipients")
		}
		allowed, ref := allowedEmailDomains(&connection{ID: conn.ID, Name: conn.Name, ProjectID: conn.ProjectID, DBType: conn.DBType, Extra: conn.Extra})
		if ref != nil {
			return 0, &httpError{Status: ref.Status, Detail: ref.Detail}
		}
		if len(allowed) > 0 {
			ok := map[string]bool{}
			for _, a := range allowed {
				ok[a] = true
			}
			var outside []string
			for _, r := range recipients {
				a := r.(string)
				if !ok[strings.ToLower(a[strings.LastIndex(a, "@")+1:])] {
					outside = append(outside, a)
				}
			}
			if len(outside) > 0 {
				return refuse(422, fmt.Sprintf("Recipients outside the domains this connection allows (%s): %s", strings.Join(allowed, ", "),
					strings.Join(outside, ", ")))
			}
		}
		attachment := str(e, "attachment_name")
		if attachment == "" {
			attachment = "report"
		}
		tz := str(e, "timezone")
		if err := d.checkOutputTemplates(ctx, [][3]string{{"subject", str(e, "subject"), "text"}, {"attachment name", attachment, "file"}},
			tz, flow.Name, ""); err != nil {
			return 0, err
		}
		target := pyjson.Object{{Key: "to", Value: to}, {Key: "cc", Value: cc}, {Key: "subject", Value: e.Value("subject")},
			{Key: "body", Value: e.Value("body")}, {Key: "body_is_html", Value: e.Value("body_is_html")}, {Key: "attachment_name", Value: attachment},
			{Key: "attachment_format", Value: e.Value("attachment_format")}}
		if templated(str(e, "subject")) || templated(str(e, "attachment_name")) {
			target = append(target, templateContext(tz)...)
		}
		email = pyjson.Object{{Key: "connection", Value: conn.payload()}, {Key: "stop_on_failure", Value: e.Value("stop_on_failure")},
			{Key: "target", Value: target}}
		emailSummary = sp(pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "host", Value: conn.Host}, {Key: "to", Value: recipients},
			{Key: "subject", Value: e.Value("subject")}, {Key: "attachment", Value: attachment}, {Key: "format", Value: e.Value("attachment_format")},
			{Key: "ok", Value: nil}})
		now := jobs.Now()
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: "email.send", TargetType: strp("flow"), TargetID: &flow.ID,
			TargetLabel: &flow.Name, At: &now, Detail: pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "host", Value: conn.Host},
				{Key: "to", Value: recipients}, {Key: "subject", Value: e.Value("subject")}, {Key: "format", Value: e.Value("attachment_format")},
				{Key: "trigger", Value: trigger}}})
	}
	outputKey := "out/" + newHex() + ".parquet"
	if publish != nil {
		outputKey = snapshotKey(targetID)
	}
	var pubNameP *string
	var pubProject any
	pubDescription, pubOverwrite := "", false
	if publish != nil {
		pubNameP, pubProject = &pubName, publish.Value("project_id")
		pubDescription, pubOverwrite = str(publish, "description"), publish.Value("overwrite") == true
	}
	if publishKeys == nil {
		publishKeys = []any{}
	}
	insert := func(task string) (int64, error) {
		var id int64
		err := d.DB.QueryRow(ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, parent_run_id, engine,
			input_key, output_bucket, output_key, publish_name, publish_project_id, publish_description, publish_overwrite, publish_sort_keys,
			destination, mirror, email, started_at) VALUES ('flow', $1, $2, 'PENDING', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
			$15, $16, $17, $18) RETURNING id`, flow.ID, task, user.ID, trigger, parent, engineName, inputKey, bucket, outputKey, pubNameP,
			pubProject, pubDescription, pubOverwrite, pyDumps(publishKeys), destSummary, mirrorSummary, emailSummary, jobs.Now()).Scan(&id)
		return id, err
	}
	// the targets that name the run: an SFTP Output always, the others when a template asks
	var numbered []pyjson.Object
	if destination != nil {
		t := pyjson.Obj(destination.Value("target"))
		if _, has := t.Get("run_id"); has || destination.Value("type") == "sftp" {
			numbered = append(numbered, t)
		}
	}
	for _, p := range []pyjson.Object{mirror, email} {
		if p != nil {
			if _, has := pyjson.Obj(p.Value("target")).Get("run_id"); has {
				numbered = append(numbered, pyjson.Obj(p.Value("target")))
			}
		}
	}
	var early int64
	if len(numbered) > 0 {
		var err error
		if early, err = insert(placeholderTask + newHex()); err != nil {
			return 0, err
		}
		set := func(p pyjson.Object) pyjson.Object {
			if p == nil {
				return nil
			}
			t := pyjson.Obj(p.Value("target"))
			if _, has := t.Get("run_id"); has || (p.Value("type") == "sftp") {
				t = t.Set("run_id", early)
			}
			return p.Set("target", t)
		}
		destination, mirror, email = set(destination), set(mirror), set(email)
	}
	q := d.jobRequest(ctx, user)
	task, err := func() (string, error) {
		submit := pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "input_key", Value: inputKey}, {Key: "output_key", Value: outputKey},
			{Key: "operations", Value: ops}, {Key: "destination", Value: nilIfEmpty(destination)}, {Key: "mirror", Value: nilIfEmpty(mirror)},
			{Key: "email", Value: nilIfEmpty(email)}, {Key: "engine", Value: engineName}}
		contract, err := q.contractPayload(targetID)
		if err != nil {
			return "", err
		}
		if contract != nil {
			submit = append(submit, pyjson.KV{Key: "contract", Value: contract})
		}
		cctx, cancel := withCheck(ctx)
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/tasks/transform-data", submit)
		cancel()
		if err != nil {
			return "", err
		}
		if res.StatusCode >= 400 {
			return "", &httpError{Status: res.StatusCode, Detail: runes(string(raw), 500)}
		}
		task, err := engineTaskID(raw)
		if err == nil && task == "" {
			err = &httpError{Status: 502, Detail: "The engine did not return a task_id"}
		}
		return task, err
	}()
	if err != nil {
		if early != 0 {
			_, _ = d.DB.Exec(ctx, `DELETE FROM runs WHERE id = $1`, early)
		}
		return 0, err
	}
	if early != 0 {
		_, err := d.DB.Exec(ctx, `UPDATE runs SET task_id = $2 WHERE id = $1`, early, task)
		return early, err
	}
	return insert(task)
}

func nilIfEmpty(o pyjson.Object) any {
	if o == nil {
		return nil
	}
	return o
}

// notDatabases is routes/connections.py NOT_DATABASES.
var notDatabases = map[string]bool{"smtp": true, "sharepoint": true, "sftp": true, "api": true, "gsheets": true}
