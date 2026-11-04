package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// A flow group's Pushmail cell (services/flow_group_runner.py _pushmail_cell, services/pushmail.py):
// one email through an SMTP connection, with one .xlsx per datasource and one file per report or
// dashboard, rendered by the engine in the task that writes the email. Every part is checked
// again with the rights of whoever runs the group. A cell whose reports or dashboards have AI
// comments (reports.comments_for asks the assistant) is not carried by the Go queue.

const pushmailMaxReportParams = 100 // PUSHMAIL_MAX_REPORT_PARAMS

var pushmailEmail = regexp.MustCompile(`^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$`)

// pushmailLanguage is pushmail.email_language.
func pushmailLanguage(u *auth.User) string {
	if u.Locale != nil {
		switch *u.Locale {
		case "en", "it", "fr", "de", "es":
			return *u.Locale
		}
	}
	return "en"
}

func cfgList(cfg pyjson.Object, k string) []pyjson.Object { return objectList(cfg.Value(k)) }

func strippedList(v any) []string {
	var out []string
	list, _ := v.([]any)
	for _, a := range list {
		if s := pystr.Strip(pyjson.Str(a)); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func pushmailName(cfg pyjson.Object) string {
	if n := pystr.Strip(textOrEmpty(cfg.Value("label"))); n != "" {
		return n
	}
	return "Pushmail"
}

// pushmailErrors is pushmail_errors: what the person lacks, or what is wrong, to send the cell.
func (d *Deps) pushmailErrors(ctx context.Context, user *auth.User, cfg pyjson.Object) ([]string, error) {
	if cfg == nil {
		return []string{"pushmail: the cell has no settings"}, nil
	}
	c := perm.For(d.DB, user)
	readable, err := c.Readable(ctx)
	if err != nil {
		return nil, err
	}
	name := pushmailName(cfg)
	var errs []string
	var conn *connpayload.Conn
	if id, ok := cfg.Value("connectionId").(int64); ok && id != 0 {
		if conn, err = connpayload.Load(ctx, d.DB, id); err != nil {
			return nil, err
		}
	}
	if conn == nil || conn.DBType != "smtp" {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: choose an SMTP connection", name))
		conn = nil
	} else if ok, err := c.Has(ctx, conn.ProjectID, perm.Connect); err != nil {
		return nil, err
	} else if !ok {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: sending through «%s» needs CONNECT on its folder", name, conn.Name))
		conn = nil
	}
	to := strippedList(cfg.Value("to"))
	everyone := append(append([]string{}, to...), strippedList(cfg.Value("cc"))...)
	if len(to) == 0 {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: add at least one recipient", name))
	}
	var bad []string
	for _, a := range everyone {
		if !pushmailEmail.MatchString(a) {
			bad = append(bad, a)
		}
	}
	if len(bad) > 0 {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: not an email address: %s", name, strings.Join(bad[:min(5, len(bad))], ", ")))
	}
	if conn != nil && len(everyone) > 0 {
		allowed, ref := allowedEmailDomains(&connection{ID: conn.ID, Name: conn.Name, ProjectID: conn.ProjectID, DBType: conn.DBType, Extra: conn.Extra})
		if ref != nil {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: %s", name, ref.Detail))
			allowed = nil
		}
		in := map[string]bool{}
		for _, a := range allowed {
			in[a] = true
		}
		var outside []string
		for _, a := range everyone {
			if len(allowed) > 0 && !in[strings.ToLower(a[strings.LastIndex(a, "@")+1:])] {
				outside = append(outside, a)
			}
		}
		if len(outside) > 0 {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: recipients outside the domains «%s» allows (%s): %s", name, conn.Name,
				strings.Join(allowed, ", "), strings.Join(outside[:min(5, len(outside))], ", ")))
		}
	}
	attachments, reps, boards := cfgList(cfg, "attachments"), cfgList(cfg, "reports"), cfgList(cfg, "dashboards")
	if len(attachments) == 0 && len(reps) == 0 && len(boards) == 0 {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: choose at least one datasource, report or dashboard", name))
	}
	var ids []int64
	for n, a := range attachments {
		id, ok := a.Value("datasourceId").(int64)
		if !ok {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: choose the datasource of file %d", name, n+1))
		} else {
			ids = append(ids, id)
		}
		if pystr.Strip(textOrEmpty(a.Value("fileName"))) == "" {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: give file %d a name", name, n+1))
		}
	}
	seen := map[int64]bool{}
	dup := false
	var unique []int64
	for _, id := range ids {
		if seen[id] {
			dup = true
			continue
		}
		seen[id] = true
		unique = append(unique, id)
	}
	if dup {
		errs = append(errs, fmt.Sprintf("pushmail «%s»: the same datasource twice", name))
	}
	for _, id := range unique {
		ds, err := loadJobDatasource(ctx, d.DB, id)
		if err != nil {
			return nil, err
		}
		if ds == nil || !readable[ds.ProjectID] {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: datasource %d not found", name, id))
		}
	}
	more, err := d.pushmailDocErrors(ctx, c, cfg, name, false)
	if err != nil {
		return nil, err
	}
	errs = append(errs, more...)
	more, err = d.pushmailDocErrors(ctx, c, cfg, name, true)
	if err != nil {
		return nil, err
	}
	return append(errs, more...), nil
}

// pushmailDocErrors is report_errors (dashboards false) and dashboard_errors.
func (d *Deps) pushmailDocErrors(ctx context.Context, c *perm.Checker, cfg pyjson.Object, name string, boards bool) ([]string, error) {
	noun, list, idKey, kind := "report", cfgList(cfg, "reports"), "reportId", reports.Kind
	words := func(s string) string { return s }
	if boards {
		noun, list, idKey, kind = "dashboard", cfgList(cfg, "dashboards"), "dashboardId", dashboards.Kind
		words = dashboards.InOurWords
	}
	var errs []string
	if !boards {
		inBody := 0
		for _, r := range list {
			if r.Value("inBody") == true {
				inBody++
			}
		}
		if inBody > 1 {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: only one report can go in the email's body", name))
		}
	}
	for n, r := range list {
		id, ok := r.Value(idKey).(int64)
		if !ok {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: choose the %s %d", name, noun, n+1))
			continue
		}
		if pystr.Strip(textOrEmpty(r.Value("fileName"))) == "" {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: give the file of %s %d a name", name, noun, n+1))
		}
		if len(pyjson.Obj(r.Value("params"))) > pushmailMaxReportParams {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: %s %d has too many values", name, noun, n+1))
			continue
		}
		item, def, err := d.visibleDocument(ctx, c, kind, id)
		if err != nil {
			return nil, err
		}
		if item == nil {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: %s %d not found", name, noun, id))
			continue
		}
		if boards {
			def = dashboards.AsReport(def)
		}
		given, gerr := reports.GivenParams(pyjson.Obj(r.Value("params")))
		if gerr == nil {
			gerr = reports.CheckValues(def, given)
		}
		if gerr != nil {
			if !isRefusal(gerr) {
				return nil, gerr
			}
			errs = append(errs, fmt.Sprintf("pushmail «%s»: the %s «%s»: %s", name, noun, item.Name, words(reports.Text(gerr))))
			continue
		}
		problem, err := reports.ReadingProblem(ctx, d.DB, c, d.SQL, def)
		if err != nil {
			return nil, err
		}
		if problem != "" {
			errs = append(errs, fmt.Sprintf("pushmail «%s»: the %s «%s»: %s", name, noun, item.Name, words(problem)))
		}
	}
	return errs, nil
}

func isRefusal(err error) bool {
	var r *reports.Refusal
	var u *reports.Unreadable
	return errors.As(err, &r) || errors.As(err, &u)
}

// visibleDocument is session.get + can_see + content_of of a report or dashboard; nil when it
// does not exist or the person does not see it.
func (d *Deps) visibleDocument(ctx context.Context, c *perm.Checker, kind *versioned.Kind, id int64) (*versioned.Item, pyjson.Object, error) {
	item, err := kind.Get(ctx, d.DB, id)
	if err != nil || item == nil {
		return nil, nil, err
	}
	if see, err := versioned.CanSee(ctx, c, item); err != nil || !see {
		return nil, nil, err
	}
	content, err := kind.ContentOf(ctx, d.DB, item, nil)
	if err != nil {
		return nil, nil, err
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		return nil, nil, errors.New("a definition that is not an object")
	}
	return item, def, nil
}

// pushmailCarried says whether the Go queue runs this cell: not when one of its reports or
// dashboards has an AI comment (the assistant writes it).
func (d *Deps) pushmailCarried(ctx context.Context, cfg pyjson.Object) bool {
	for _, k := range []struct {
		list, id string
		kind     *versioned.Kind
	}{{"reports", "reportId", reports.Kind}, {"dashboards", "dashboardId", dashboards.Kind}} {
		for _, r := range cfgList(cfg, k.list) {
			id, ok := r.Value(k.id).(int64)
			if !ok {
				continue
			}
			item, err := k.kind.Get(ctx, d.DB, id)
			if err != nil || item == nil {
				continue
			}
			content, err := k.kind.ContentOf(ctx, d.DB, item, nil)
			if err != nil {
				return false
			}
			if def, ok := content.(pyjson.Object); ok && reports.HasComment(def) {
				return false
			}
		}
	}
	return true
}

// renderedDoc is one report or dashboard of a cell, prepared.
type renderedDoc struct {
	item    *versioned.Item
	spec    pyjson.Object // what the engine renders
	summary pyjson.Object // in the run's summary
	detail  pyjson.Object
}

// cellDocuments is _cell_reports (boards false) and _cell_dashboards: every document as the
// engine renders it, with the rights of whoever runs the group; why one cannot be sent.
func (q *queue) cellDocuments(ctx context.Context, user *auth.User, cfg pyjson.Object, group *int64, trigger string, read map[int64]bool,
	boards bool) ([]renderedDoc, string, error) {
	d := q.d
	c := perm.For(d.DB, user)
	noun, list, idKey, kind, action := "report", cfgList(cfg, "reports"), "reportId", reports.Kind, "report.render"
	if boards {
		noun, list, idKey, kind, action = "dashboard", cfgList(cfg, "dashboards"), "dashboardId", dashboards.Kind, "dashboard.render"
	}
	var out []renderedDoc
	for _, r := range list {
		id, has := r.Value(idKey).(int64)
		var item *versioned.Item
		var content pyjson.Object
		var err error
		if has {
			if item, content, err = d.visibleDocument(ctx, c, kind, id); err != nil {
				return nil, "", err
			}
		}
		if item == nil {
			return out, fmt.Sprintf("%s %s not found", noun, pyjson.Str(r.Value(idKey))), nil
		}
		def, printed := content, pyjson.Object(nil)
		if boards {
			printed = dashboards.LaidOut(content)
			def = pyjson.Obj(printed.Value("definition"))
		}
		var groupID any
		if group != nil {
			groupID = *group
		}
		detail := pyjson.Object{{Key: "format", Value: r.Value("format")}, {Key: "from", Value: "flow_group"}, {Key: "flow_group", Value: groupID},
			{Key: "trigger", Value: trigger}, {Key: "parameters", Value: pyjson.Object{}}}
		given, err := reports.GivenParams(pyjson.Obj(r.Value("params")))
		var values, datasets pyjson.Object
		if err == nil {
			detail = detail.Set("parameters", reports.GivenDetail(given))
			rs := &reports.Resolver{Q: d.DB, C: c, Engine: d.Engine, Log: d.Log, SQL: d.SQL, Read: read}
			values, datasets, err = rs.Prepare(ctx, def, given, &detail)
		}
		if err != nil {
			if !isRefusal(err) {
				return nil, "", err
			}
			said := reports.Text(err)
			if boards {
				said = dashboards.InOurWords(said)
			}
			detail = detail.Set("error", runes(said, 500))
			q.auditDocument(ctx, user, action, noun, item, "failure", detail)
			return out, fmt.Sprintf("the %s «%s»: %s", noun, item.Name, said), nil
		}
		params := pyjson.Object{}
		for _, kv := range values {
			params = append(params, pyjson.KV{Key: kv.Key, Value: reportparams.ToJSON(kv.Value)})
		}
		locale := pyjson.Or(content.Value("locale"), "en")
		spec := pyjson.Object{{Key: "name", Value: item.Name}, {Key: "definition", Value: def}}
		summary := pyjson.Object{{Key: "id", Value: item.ID}, {Key: "name", Value: item.Name}, {Key: "format", Value: r.Value("format")}}
		if boards {
			spec = append(spec, pyjson.KV{Key: "tiles", Value: printed.Value("tiles")}, pyjson.KV{Key: "page", Value: printed.Value("page")})
		}
		spec = append(spec, pyjson.KV{Key: "datasets", Value: datasets}, pyjson.KV{Key: "params", Value: params},
			pyjson.KV{Key: "format", Value: r.Value("format")}, pyjson.KV{Key: "file_name", Value: r.Value("fileName")})
		if !boards {
			spec = append(spec, pyjson.KV{Key: "in_body", Value: r.Value("inBody")})
			summary = append(summary, pyjson.KV{Key: "in_body", Value: r.Value("inBody")})
		}
		// _cell_comments: a definition without AI comments has none, and no reading
		spec = append(spec, pyjson.KV{Key: "locale", Value: locale}, pyjson.KV{Key: "comments", Value: pyjson.Object{}},
			pyjson.KV{Key: "reading", Value: nil})
		out = append(out, renderedDoc{item: item, spec: spec, summary: summary, detail: detail})
	}
	return out, "", nil
}

// auditDocument is reports._audit_render / dashboards.audit_render.
func (q *queue) auditDocument(ctx context.Context, user *auth.User, action, noun string, item *versioned.Item, outcome string, detail pyjson.Object) {
	now := jobs.Now()
	audit.Record(ctx, q.d.DB, q.d.Log, audit.Event{Actor: user, Action: action, Outcome: outcome, TargetType: strp(noun), TargetID: &item.ID,
		TargetLabel: &item.Name, At: &now, Detail: detail})
}

// auditDocuments is _audit_reports / _audit_dashboards, once the email's outcome is known.
func (q *queue) auditDocuments(ctx context.Context, user *auth.User, docs []renderedDoc, boards bool, outcome string, runID int64, errText *string) {
	action, noun := "report.render", "report"
	if boards {
		action, noun = "dashboard.render", "dashboard"
	}
	for _, doc := range docs {
		detail := append(append(pyjson.Object(nil), doc.detail...), pyjson.KV{Key: "run_id", Value: runID})
		if errText != nil && *errText != "" {
			detail = detail.Set("error", runes(*errText, 500))
		}
		q.auditDocument(ctx, user, action, noun, doc.item, outcome, detail)
	}
}

// pushmailCell is _pushmail_cell.
func (q *queue) pushmailCell(ctx context.Context, id, userID int64, trigger string, cell *groupCell, cells *groupCells, step, col int) (string, error) {
	d := q.d
	user, err := loadUser(ctx, d.DB, userID)
	if err != nil {
		return "", err
	}
	if user == nil || !user.IsActive {
		return userGone, nil
	}
	cfg := cell.pushmail
	problems, err := d.pushmailErrors(ctx, user, cfg)
	if err != nil {
		return "", err
	}
	if len(problems) > 0 || cfg == nil {
		if len(problems) == 0 {
			return "pushmail: the cell has no settings", nil
		}
		return strings.Join(problems, "; "), nil
	}
	name := pushmailName(cfg)
	var group *int64
	var groupName string
	if run, err := d.jobRequest(ctx, user).readRun(id); err != nil {
		return "", err
	} else if run != nil && run.FlowGroupID != nil {
		if err := d.DB.QueryRow(ctx, `SELECT name FROM flow_groups WHERE id = $1`, *run.FlowGroupID).Scan(&groupName); err == nil {
			group = run.FlowGroupID
		}
	}
	connID, _ := cfg.Value("connectionId").(int64)
	conn, err := connpayload.Load(ctx, d.DB, connID)
	if err != nil || conn == nil {
		return "", errors.Join(err, errors.New("the cell's connection vanished"))
	}
	type attachment struct {
		ds        *jobDatasource
		fileName  any
		maxRows   any
		datasetID int64
	}
	var files []attachment
	var empty []string
	for _, a := range cfgList(cfg, "attachments") {
		dsID, _ := a.Value("datasourceId").(int64)
		ds, err := loadJobDatasource(ctx, d.DB, dsID)
		if err != nil || ds == nil {
			return "", errors.Join(err, errors.New("an attachment's datasource vanished"))
		}
		files = append(files, attachment{ds, a.Value("fileName"), a.Value("maxRows"), dsID})
		if ds.Key == "" {
			empty = append(empty, ds.Name)
		}
	}
	if len(empty) > 0 {
		return fmt.Sprintf("«%s»: no data yet in %s (never imported or published)", name, strings.Join(empty, ", ")), nil
	}
	read := map[int64]bool{}
	rendered, failed, err := q.cellDocuments(ctx, user, cfg, group, trigger, read, false)
	if err != nil {
		return "", err
	}
	if failed != "" {
		return fmt.Sprintf("«%s»: %s", name, failed), nil
	}
	boards, failed, err := q.cellDocuments(ctx, user, cfg, group, trigger, read, true)
	if err != nil {
		return "", err
	}
	if failed != "" {
		return fmt.Sprintf("«%s»: %s", name, failed), nil
	}
	to, cc := strippedList(cfg.Value("to")), strippedList(cfg.Value("cc"))
	all := stringsAny(append(append([]string{}, to...), cc...))
	names, dsIDs := []any{}, []any{}
	for _, f := range files {
		names, dsIDs = append(names, f.ds.Name), append(dsIDs, f.datasetID)
	}
	summaries := func(docs []renderedDoc) []any {
		out := []any{}
		for _, doc := range docs {
			out = append(out, doc.summary)
		}
		return out
	}
	readIDs := []int64{}
	for id := range read {
		readIDs = append(readIDs, id)
	}
	sort.Slice(readIDs, func(i, j int) bool { return readIDs[i] < readIDs[j] })
	readAny := []any{}
	for _, id := range readIDs {
		readAny = append(readAny, id)
	}
	email := pyDumps(pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "host", Value: conn.Host}, {Key: "to", Value: all},
		{Key: "subject", Value: cfg.Value("subject")}, {Key: "attachments", Value: names}, {Key: "datasource_ids", Value: dsIDs},
		{Key: "reports", Value: summaries(rendered)}, {Key: "dashboards", Value: summaries(boards)}, {Key: "report_datasource_ids", Value: readAny},
		{Key: "cell", Value: name}, {Key: "cell_id", Value: cell.id}, {Key: "ok", Value: nil}})
	var childID int64
	if err := d.DB.QueryRow(ctx, `INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, parent_run_id, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, email, started_at) VALUES ('pushmail', $1, 'PENDING', $2, $3, $4,
		'', '', '', '', false, '[]', $5, $6) RETURNING id`, placeholderTask+newHex(), user.ID, trigger, id, email, jobs.Now()).Scan(&childID); err != nil {
		return "", err
	}
	cells.set(ctx, step, col, "run_id", childID)
	tz := pystr.Strip(textOrEmpty(cfg.Value("timezone")))
	if tz == "" {
		tz = "UTC"
	}
	attachments := []any{}
	for _, f := range files {
		attachments = append(attachments, pyjson.Object{{Key: "bucket", Value: f.ds.Bucket}, {Key: "key", Value: f.ds.Key},
			{Key: "label", Value: f.ds.Name}, {Key: "file_name", Value: f.fileName}, {Key: "max_rows", Value: f.maxRows}})
	}
	specs := func(docs []renderedDoc) []any {
		out := []any{}
		for _, doc := range docs {
			out = append(out, doc.spec)
		}
		return out
	}
	spec := pyjson.Object{{Key: "to", Value: stringsAny(to)}, {Key: "cc", Value: stringsAny(cc)}, {Key: "subject", Value: cfg.Value("subject")},
		{Key: "body", Value: cfg.Value("body")}, {Key: "body_is_html", Value: cfg.Value("bodyIsHtml")}, {Key: "timezone", Value: tz},
		{Key: "run_id", Value: childID}, {Key: "flow", Value: groupName}, {Key: "lang", Value: pushmailLanguage(user)},
		{Key: "attachments", Value: attachments}, {Key: "reports", Value: specs(rendered)}, {Key: "dashboards", Value: specs(boards)}}
	ids := func(docs []renderedDoc) []any {
		out := []any{}
		for _, doc := range docs {
			out = append(out, doc.item.ID)
		}
		return out
	}
	now := jobs.Now()
	var targetID *int64
	var targetLabel *string
	if group != nil {
		targetID, targetLabel = group, &groupName
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: "email.send", TargetType: strp("flow_group"), TargetID: targetID,
		TargetLabel: targetLabel, At: &now, Detail: pyjson.Object{{Key: "connection_id", Value: conn.ID}, {Key: "host", Value: conn.Host},
			{Key: "to", Value: all}, {Key: "subject", Value: cfg.Value("subject")}, {Key: "cell", Value: name}, {Key: "datasources", Value: dsIDs},
			{Key: "reports", Value: ids(rendered)}, {Key: "dashboards", Value: ids(boards)}, {Key: "trigger", Value: trigger},
			{Key: "run_id", Value: childID}}})
	task, failure := func() (string, string) {
		cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/db/pushmail", pyjson.Object{{Key: "connection", Value: connpayload.Payload(conn)},
			{Key: "spec", Value: spec}})
		if err != nil {
			return "", transportErrorName(err)
		}
		answer := pyjson.Object{}
		if len(raw) > 0 {
			v, err := pyjson.Loads(string(raw))
			if err != nil {
				return "", err.Error()
			}
			answer = pyjson.Obj(v)
		}
		if res.StatusCode >= 400 {
			if dt := answer.Value("detail"); pyjson.Truthy(dt) {
				return "", pyjson.Str(dt)
			}
			return "", fmt.Sprint(res.StatusCode)
		}
		return pyjson.Str(answer.Value("task_id")), ""
	}()
	if failure != "" {
		why := "the engine did not take the email: " + failure
		q.finalize(ctx, childID, "FAILURE", &why, false)
		q.auditDocuments(ctx, user, rendered, false, "failure", childID, &why)
		q.auditDocuments(ctx, user, boards, true, "failure", childID, &why)
		return fmt.Sprintf("«%s»: %s", name, why), nil
	}
	if _, err := d.DB.Exec(ctx, `UPDATE runs SET task_id = $2 WHERE id = $1`, childID, task); err != nil {
		return "", err
	}
	done, err := q.waitRun(ctx, childID)
	if err != nil {
		return "", err
	}
	if done.Status == "SUCCESS" {
		q.auditDocuments(ctx, user, rendered, false, "success", childID, nil)
		q.auditDocuments(ctx, user, boards, true, "success", childID, nil)
		return "", nil
	}
	q.auditDocuments(ctx, user, rendered, false, "failure", childID, done.Error)
	q.auditDocuments(ctx, user, boards, true, "failure", childID, done.Error)
	if done.Error != nil && *done.Error != "" {
		return fmt.Sprintf("«%s»: %s", name, *done.Error), nil
	}
	return fmt.Sprintf("«%s»: the email was not sent", name), nil
}
