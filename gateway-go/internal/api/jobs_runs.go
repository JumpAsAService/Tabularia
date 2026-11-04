package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// What the runs' background loops (scheduler_loop, queue_loop) share: a «request» with no
// HTTP request behind it, for the helpers the routes use (the reconcile of a run), the
// data contracts' verdict on a run, an orphaned Script/Container run closed, the failure
// notice, the connection the engine opens.

// jobRequest is a request made by the background work as `u` (nil: nobody, where the
// Python code has no user: the reconcile of the scheduler).
func (d *Deps) jobRequest(ctx context.Context, u *auth.User) *request {
	p := &auth.Principal{}
	if u != nil {
		p.User = *u
	}
	c := perm.For(d.DB, &p.User)
	return &request{d: d, ctx: ctx, p: p, c: c, acc: flowaccess.New(d.DB, c, &p.User, d.flowSettings())}
}

// loadUser is session.get(User, id): nil when gone.
func loadUser(ctx context.Context, q perm.Querier, id int64) (*auth.User, error) {
	var u auth.User
	err := q.QueryRow(ctx, `SELECT id, email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, locale,
		last_seen_at FROM users WHERE id = $1`, id).Scan(&u.ID, &u.Email, &u.FullName, &u.HashedPassword, &u.IsActive, &u.IsSuperuser,
		&u.IsObserver, &u.IsService, &u.Locale, &u.LastSeenAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &u, nil
}

// loadUserOpt is `session.get(User, x) if x else None`.
func loadUserOpt(ctx context.Context, q perm.Querier, id *int64) (*auth.User, error) {
	if id == nil || *id == 0 {
		return nil, nil
	}
	return loadUser(ctx, q, *id)
}

// ── the data contract's verdict on a run (routes/runs.py _reconcile) ──────────

// contractPayload is services/contracts.py payload: the contract turned on, with its
// version, or nil.
func (q *request) contractPayload(dsID *int64) (pyjson.Object, error) {
	if dsID == nil {
		return nil, nil
	}
	c, err := contractOf(q.ctx, q.d.DB, *dsID)
	if err != nil || c == nil || !c.Enabled {
		return nil, err
	}
	v, lerr := pyjson.Loads(c.Document)
	if lerr != nil {
		return nil, nil // JSONDecodeError
	}
	doc, ok := v.(pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(v))
	}
	if !pyjson.Truthy(doc.Value("rules")) {
		return nil, nil
	}
	return append(pyjson.Object(nil), doc...).Set("version", c.Version), nil
}

func pyTypeName(v any) string {
	switch v.(type) {
	case []any:
		return "list"
	case string:
		return "str"
	case int64:
		return "int"
	case float64:
		return "float"
	case bool:
		return "bool"
	case nil:
		return "NoneType"
	}
	return "dict"
}

// contractVerdict is _reconcile's `contract_report` and `refused`: the report the worker
// evaluated on the data just written, when the destination still has its contract.
func (q *request) contractVerdict(run *runRow, newStatus string, result pyjson.Object) (pyjson.Object, bool, error) {
	if newStatus != "SUCCESS" {
		return nil, false, nil
	}
	report, ok := result.Value("contract").(pyjson.Object)
	if !ok {
		return nil, false, nil
	}
	target, err := q.contractTargetID(run)
	if err != nil {
		return nil, false, err
	}
	doc, err := q.contractPayload(target)
	if err != nil {
		return nil, false, err
	}
	if doc == nil {
		return nil, false, nil // the contract was removed or turned off meanwhile
	}
	return report, report.Value("outcome") == "failed", nil
}

// brokenRulesMessage is contracts.broken_rules_message.
func brokenRulesMessage(report pyjson.Object) string {
	var parts []string
	rules, _ := report.Value("rules").([]any)
	for _, r := range rules {
		x := pyjson.Obj(r)
		if pyjson.Truthy(x.Value("passed")) || x.Value("severity") == "warning" {
			continue
		}
		where := ruleWhere(x)
		howMany := ""
		if pyjson.Truthy(x.Value("violations")) {
			howMany = fmt.Sprintf(" (%s rows)", pyjson.Str(x.Value("violations")))
		}
		on := ""
		if where != "" {
			on = " on " + where
		}
		parts = append(parts, pyjson.Str(x.Value("kind"))+on+howMany)
	}
	if len(parts) == 0 && pyjson.Truthy(report.Value("error")) {
		parts = append(parts, "contract could not be evaluated: "+pyjson.Str(report.Value("error")))
	}
	shown := parts
	if len(shown) > 6 {
		shown = shown[:6]
	}
	listing := strings.Join(shown, "; ")
	if len(parts) > 6 {
		listing += fmt.Sprintf("; and %d more", len(parts)-6)
	}
	return "Data contract violated: " + listing + ". The new data was NOT published: " +
		"the datasource still serves the previous snapshot."
}

// ruleWhere is `x.get("column") or ", ".join(x.get("columns") or []) or x.get("name") or ""`.
func ruleWhere(x pyjson.Object) string {
	if v := x.Value("column"); pyjson.Truthy(v) {
		return pyjson.Str(v)
	}
	if cols, ok := x.Value("columns").([]any); ok && len(cols) > 0 {
		names := make([]string, len(cols))
		for i, c := range cols {
			names[i] = pyjson.Str(c)
		}
		if j := strings.Join(names, ", "); j != "" {
			return j
		}
	}
	if v := x.Value("name"); pyjson.Truthy(v) {
		return pyjson.Str(v)
	}
	return ""
}

// refuseSnapshot is _refuse_snapshot, in the claim's transaction.
func (q *request) refuseSnapshot(tx pgx.Tx, run *runRow, report pyjson.Object) error {
	if err := q.scheduleBlobDeletion(tx, run.OutputBucket, run.OutputKey, fmt.Sprintf("run %d refused by the data contract", run.ID)); err != nil {
		return err
	}
	target, err := q.contractTargetIDTx(tx, run)
	if err != nil || target == nil {
		return err
	}
	c, err := contractOf(q.ctx, tx, *target)
	if err != nil || c == nil {
		return err
	}
	trigger := "publish"
	if run.Kind == "ingest" {
		trigger = "refresh"
	}
	_, err = recordContractResult(q.ctx, tx, c, report, trigger, &run.ID, run.OutputKey, true)
	return err
}

// contractTargetIDTx is _contract_target_id inside the claim's transaction.
func (q *request) contractTargetIDTx(tx pgx.Tx, run *runRow) (*int64, error) {
	if run.DatasourceID != nil {
		return run.DatasourceID, nil
	}
	if run.PublishName != nil && *run.PublishName != "" && run.PublishProjectID != nil && *run.PublishProjectID != 0 {
		var id int64
		err := tx.QueryRow(q.ctx, `SELECT id FROM datasources WHERE project_id = $1 AND name = $2 ORDER BY id LIMIT 1`,
			*run.PublishProjectID, *run.PublishName).Scan(&id)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return &id, err
	}
	return nil, nil
}

// recordContract is _record_contract: the report becomes the contract's state when the run's
// data was really published.
func (q *request) recordContract(tx pgx.Tx, runID int64, report pyjson.Object, trigger string) error {
	if report == nil {
		return nil
	}
	run, err := scanRunTx(q.ctx, tx, runID)
	if err != nil || run.DatasourceID == nil {
		return err
	}
	var snapshotRun *int64
	err = tx.QueryRow(q.ctx, `SELECT snapshot_run_id FROM datasources WHERE id = $1`, *run.DatasourceID).Scan(&snapshotRun)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if snapshotRun == nil || *snapshotRun != run.ID {
		return nil
	}
	c, err := contractOf(q.ctx, tx, *run.DatasourceID)
	if err != nil || c == nil {
		return err
	}
	_, err = recordContractResult(q.ctx, tx, c, report, trigger, &run.ID, run.OutputKey, false)
	return err
}

// contractNotifyTriggers is NOTIFY_TRIGGERS.
var contractNotifyTriggers = map[string]bool{"refresh": true, "publish": true, "freshness": true}

// contractSignal is contracts.signal.
func contractSignal(blockedAt *time.Time, status string) string {
	if blockedAt != nil {
		return "refused"
	}
	return status
}

// recordContractResult is contracts.record_result: the report kept, the contract's state
// updated, the notice marked due on a change of state. In the caller's transaction.
func recordContractResult(ctx context.Context, tx pgx.Tx, c *contractRow, report pyjson.Object, trigger string, runID *int64,
	snapshotKey string, blocked bool) (int64, error) {
	now := jobs.Now()
	outcome := "failed"
	if o, ok := report.Value("outcome").(string); ok && (o == "passed" || o == "warning" || o == "failed") {
		outcome = o
	}
	text, err := pyjson.Dumps(report, pyjson.Options{})
	if err != nil {
		return 0, err
	}
	count := func(k string) (int64, error) {
		v := report.Value(k)
		if !pyjson.Truthy(v) {
			return 0, nil
		}
		n, ok := pyInt(v)
		if !ok {
			return 0, fmt.Errorf("int() of the report's %s", k)
		}
		return n, nil
	}
	errs, err := count("errors")
	if err != nil {
		return 0, err
	}
	warns, err := count("warnings")
	if err != nil {
		return 0, err
	}
	rows, err := intColumn(report.Value("rows"))
	if err != nil {
		return 0, err
	}
	before := contractSignal(c.BlockedAt, c.Status)
	status, checkedAt, cErrors, cWarnings, lastReport := c.Status, c.CheckedAt, c.Errors, c.Warnings, c.LastReport
	blockedAt, blockedRun := c.BlockedAt, c.BlockedRunID
	if blocked {
		blockedAt, blockedRun = &now, runID
	} else {
		status, checkedAt, cErrors, cWarnings, lastReport = outcome, &now, errs, warns, &text
		if trigger == "refresh" || trigger == "publish" {
			blockedAt, blockedRun = nil, nil
		}
	}
	var notify *string
	if contractNotifyTriggers[trigger] && c.NotifyEmails != nil && *c.NotifyEmails != "" && c.NotifyConnectionID != nil &&
		*c.NotifyConnectionID != 0 {
		after := contractSignal(blockedAt, status)
		if after != before && !(after == "passed" && before == "pending") {
			notify = strp("pending")
		}
	}
	var id int64
	if err := tx.QueryRow(ctx, `INSERT INTO data_contract_results (datasource_id, contract_version, trigger, run_id, snapshot_key,
		outcome, blocked, errors, warnings, rows, report, notify, evaluated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
		RETURNING id`, c.DatasourceID, c.Version, trigger, runID, snapshotKey, outcome, blocked, errs, warns, rows, text, notify, now).
		Scan(&id); err != nil {
		return 0, err
	}
	_, err = tx.Exec(ctx, `UPDATE data_contracts SET status = $2, checked_at = $3, errors = $4, warnings = $5, last_report = $6,
		blocked_at = $7, blocked_run_id = $8 WHERE id = $1`, c.ID, status, checkedAt, cErrors, cWarnings, lastReport, blockedAt, blockedRun)
	return id, err
}

// ── an orphaned Script or Container run (services/script_runs.py close_if_orphaned) ──

const (
	trialStartSeconds  = 300 // TRIAL_START_SECONDS
	trialDeadSeconds   = 120 // TRIAL_DEAD_SECONDS
	labelScriptRun     = "tabularia.io/script-run"
	labelContainerRun  = "tabularia.io/container-run"
	labelOrchestration = "tabularia.io/run"
)

// scriptJSON is json.loads(run.script or "{}"), nil when it is not JSON.
func scriptJSON(script *string) (any, bool) {
	text := deref(script)
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	return v, err == nil
}

// closeIfOrphaned is close_if_orphaned: a Script or Container run still open whose process
// died, its Jobs stopped and it closed saying so.
func (q *request) closeIfOrphaned(run *runRow, sc *scriptcfg.Settings) (*runRow, error) {
	if terminal[run.Status] {
		return run, nil
	}
	var reason string
	if run.ParentRunID != nil && *run.ParentRunID != 0 {
		var status string
		err := q.d.DB.QueryRow(q.ctx, `SELECT status FROM runs WHERE id = $1`, *run.ParentRunID).Scan(&status)
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && !terminal[status]) {
			return run, nil
		}
		if err != nil {
			return nil, err
		}
		reason = fmt.Sprintf("the flow's run ended before the %s did (its orchestrator stopped)", run.Kind)
	} else {
		var budget int64
		if v, ok := scriptJSON(run.Script); ok {
			o, isObj := v.(pyjson.Object)
			if !isObj {
				return nil, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(v))
			}
			if b := o.Value("budget_seconds"); pyjson.Truthy(b) {
				n, ok := pyIntOf(b)
				if ok {
					budget = n
				}
			}
		}
		now := jobs.Now()
		age := now.Sub(run.StartedAt).Seconds()
		switch {
		case run.HeartbeatAt != nil:
			if now.Sub(*run.HeartbeatAt).Seconds() < trialDeadSeconds {
				return run, nil
			}
			reason = "the trial stopped: the process running it is gone (a gateway restarted or removed)"
		case run.Status == "PENDING" && age >= trialStartSeconds:
			reason = "the trial never started: the process that took it stopped"
		case budget != 0 && age >= float64(budget):
			reason = "the trial outlived its time: the process running it stopped"
		default:
			return run, nil
		}
	}
	ns := ""
	if v, ok := scriptJSON(run.Script); ok {
		if o, isObj := v.(pyjson.Object); isObj {
			if s, isText := o.Value("namespace").(string); isText {
				ns = s
			} else if x := o.Value("namespace"); pyjson.Truthy(x) {
				ns = pyjson.Str(x)
			}
		}
	}
	trial := run.ParentRunID == nil || *run.ParentRunID == 0
	var on bool
	var selector string
	if run.Kind == "container" {
		on = sc.Containers.Enabled
		if ns == "" {
			ns = sc.Containers.Namespace
			if trial && sc.Containers.TrialNamespace != "" {
				ns = sc.Containers.TrialNamespace
			}
		}
		selector = fmt.Sprintf("%s=%d", labelContainerRun, run.ID)
	} else {
		on = sc.DisabledReason() == ""
		if ns == "" {
			ns = sc.Scripts.Namespace
			if trial && sc.Scripts.TrialNamespace != "" {
				ns = sc.Scripts.TrialNamespace
			}
		}
		selector = fmt.Sprintf("%s=%d", labelScriptRun, run.ID)
	}
	if on {
		if err := kube.DeleteJobs(q.ctx, sc.Kubernetes, ns, selector); err != nil {
			q.d.Log.Warn("Jobs of the run not deleted", "kind", run.Kind, "run", run.ID, "err", err)
		}
	}
	if _, err := q.d.DB.Exec(q.ctx, `UPDATE runs SET status = 'FAILURE', finished_at = $2, error = $3 WHERE id = $1`,
		run.ID, jobs.Now(), reason); err != nil {
		return nil, err
	}
	return q.readRun(run.ID)
}

// pyIntOf is int(x) of a json value (a float truncated, a numeric text parsed).
func pyIntOf(v any) (int64, bool) { return pyInt(v) }

// reconcileAny is _reconcile for any run, as a background loop calls it: a Script or
// Container run only closed when orphaned.
func (q *request) reconcileAny(run *runRow, sc *scriptcfg.Settings) (*runRow, error) {
	if !terminal[run.Status] && run.Kind != "orchestration" && (run.Kind == "script" || run.Kind == "container") {
		return q.closeIfOrphaned(run, sc)
	}
	return q.reconcile(run, progressOf{})
}

// ── the connection the engine opens (routes/connections.py engine_connection_payload) ──

type engineConn struct {
	ID, ProjectID                                   int64
	Name, DBType, Host, Username, PasswordEncrypted string
	Port                                            *int64
	Database, DBSchema, Extra, PrivateKeyEncrypted  string
	HostKeyType, HostKeyFingerprint                 string
}

// loadEngineConn is session.get(Connection, id): nil when gone.
func loadEngineConn(ctx context.Context, q perm.Querier, id int64) (*engineConn, error) {
	var c engineConn
	err := q.QueryRow(ctx, `SELECT id, project_id, name, db_type, host, username, password_encrypted, port, database, db_schema, extra,
		private_key_encrypted, host_key_type, host_key_fingerprint FROM connections WHERE id = $1`, id).Scan(&c.ID, &c.ProjectID, &c.Name,
		&c.DBType, &c.Host, &c.Username, &c.PasswordEncrypted, &c.Port, &c.Database, &c.DBSchema, &c.Extra, &c.PrivateKeyEncrypted,
		&c.HostKeyType, &c.HostKeyFingerprint)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// extraObject is json.loads(extra or "{}") as a dict, {} when broken or not one.
func extraObject(extra string) (pyjson.Object, bool) {
	if extra == "" {
		extra = "{}"
	}
	v, err := pyjson.Loads(extra)
	if err != nil {
		return pyjson.Object{}, false
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return pyjson.Object{}, false
	}
	return o, true
}

// payload is engine_connection_payload: the secrets still encrypted.
func (c *engineConn) payload() pyjson.Object {
	strip := pystr.Strip
	orText := func(v any) string {
		if !pyjson.Truthy(v) {
			return ""
		}
		return pyjson.Str(v)
	}
	switch c.DBType {
	case "sharepoint":
		opts, isDict := extraObject(c.Extra)
		library := ""
		if isDict {
			library = strip(orText(opts.Value("library")))
		}
		return pyjson.Object{{Key: "tenant_id", Value: strip(c.Database)}, {Key: "client_id", Value: strip(c.Username)},
			{Key: "client_secret_encrypted", Value: c.PasswordEncrypted}, {Key: "site_url", Value: strip(c.Host)},
			{Key: "library", Value: library}}
	case "smtp":
		opts, _ := extraObject(c.Extra)
		var port any = int64(587)
		if c.Port != nil && *c.Port != 0 {
			port = *c.Port
		}
		return pyjson.Object{{Key: "db_type", Value: "smtp"}, {Key: "host", Value: c.Host}, {Key: "port", Value: port},
			{Key: "username", Value: c.Username}, {Key: "password_encrypted", Value: c.PasswordEncrypted},
			{Key: "from_address", Value: pyjson.Or(opts.Value("from_address"), "")}, {Key: "from_name", Value: pyjson.Or(opts.Value("from_name"), "")},
			{Key: "tls", Value: pyjson.Or(opts.Value("tls"), "starttls")}}
	case "sftp":
		opts, isDict := extraObject(c.Extra)
		authBy := "password"
		if a, ok := opts.Value("auth").(string); isDict && ok && (a == "password" || a == "key") {
			authBy = a
		}
		var port any = int64(22)
		if c.Port != nil && *c.Port != 0 {
			port = *c.Port
		}
		password, key := "", ""
		if authBy == "password" {
			password = c.PasswordEncrypted
		} else {
			key = c.PrivateKeyEncrypted
		}
		return pyjson.Object{{Key: "db_type", Value: "sftp"}, {Key: "host", Value: strip(c.Host)}, {Key: "port", Value: port},
			{Key: "username", Value: strip(c.Username)}, {Key: "auth", Value: authBy}, {Key: "password_encrypted", Value: password},
			{Key: "private_key_encrypted", Value: key}, {Key: "host_key_type", Value: c.HostKeyType},
			{Key: "host_key_fingerprint", Value: c.HostKeyFingerprint}}
	case "s3":
		return pyjson.Object{{Key: "db_type", Value: "s3"}, {Key: "endpoint_url", Value: c.Host}, {Key: "access_key", Value: c.Username},
			{Key: "secret_key_encrypted", Value: c.PasswordEncrypted}, {Key: "bucket", Value: c.Database}, {Key: "region", Value: c.DBSchema}}
	case "api":
		opts, _ := extraObject(c.Extra)
		in := func(v any, options ...string) (string, bool) {
			s, ok := v.(string)
			if !ok {
				return "", false
			}
			for _, o := range options {
				if s == o {
					return s, true
				}
			}
			return "", false
		}
		authBy, ok := in(opts.Value("auth"), "none", "api_key", "bearer", "basic", "oauth2")
		if !ok {
			authBy = "none"
		}
		headers := pyjson.Object{}
		if h, isDict := opts.Value("headers").(pyjson.Object); isDict {
			for _, kv := range h {
				headers = headers.Set(strip(kv.Key), pyjson.Str(kv.Value))
			}
		}
		username, secretEnc := "", ""
		if authBy == "basic" || authBy == "oauth2" {
			username = strip(c.Username)
		}
		if authBy != "none" {
			secretEnc = c.PasswordEncrypted
		}
		keyIn, ok := in(opts.Value("key_in"), "header", "query")
		if !ok {
			keyIn = "header"
		}
		clientAuth, ok := in(opts.Value("client_auth"), "body", "basic")
		if !ok {
			clientAuth = "body"
		}
		return pyjson.Object{{Key: "db_type", Value: "api"}, {Key: "base_url", Value: strip(c.Host)}, {Key: "auth", Value: authBy},
			{Key: "username", Value: username}, {Key: "secret_encrypted", Value: secretEnc},
			{Key: "key_name", Value: strip(orText(opts.Value("key_name")))}, {Key: "key_in", Value: keyIn},
			{Key: "token_url", Value: strip(orText(opts.Value("token_url")))}, {Key: "scope", Value: strip(orText(opts.Value("scope")))},
			{Key: "client_auth", Value: clientAuth}, {Key: "headers", Value: headers},
			{Key: "test_path", Value: strip(orText(opts.Value("test_path")))}}
	case "gsheets":
		return pyjson.Object{{Key: "db_type", Value: "gsheets"}, {Key: "key_encrypted", Value: c.PasswordEncrypted},
			{Key: "client_email", Value: c.Host}, {Key: "project_id", Value: c.Database}}
	}
	return reports.ConnectionPayload(&reports.Connection{ID: c.ID, Name: c.Name, ProjectID: c.ProjectID, DBType: c.DBType, Host: c.Host,
		Port: c.Port, Username: c.Username, PasswordEncrypted: c.PasswordEncrypted, Database: c.Database, DBSchema: c.DBSchema, Extra: c.Extra})
}

// ── the emails' texts (services/email_texts.py) ───────────────────────────────

// emailLanguageOf is language_of.
func emailLanguageOf(code *string) string {
	if code != nil {
		if _, ok := emailTexts[*code]; ok {
			return *code
		}
	}
	return "en"
}

// emailText is text(lang, key, **values): str.format of the named fields.
func emailText(lang, key string, values ...any) string {
	t := emailTexts[lang][key]
	for i := 0; i+1 < len(values); i += 2 {
		t = strings.ReplaceAll(t, "{"+values[i].(string)+"}", pyjson.Str(values[i+1]))
	}
	return t
}

// emailLabelled is labelled: «Label: value» lines, the values aligned.
func emailLabelled(lang string, rows [][2]string) []string {
	width := 0
	labels := make([]string, len(rows))
	for i, r := range rows {
		labels[i] = emailText(lang, r[0])
		width = max(width, pystr.Len(labels[i]))
	}
	width++
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = labels[i] + strings.Repeat(" ", width-pystr.Len(labels[i])) + r[1]
	}
	return out
}

type langGroup struct {
	lang string
	to   []string
}

// byLanguage is by_language: the recipients grouped by the language they get, in order.
func byLanguage(ctx context.Context, q perm.Querier, addresses []string) ([]langGroup, error) {
	chosen := map[string]string{}
	if len(addresses) > 0 {
		lower := make([]string, len(addresses))
		for i, a := range addresses {
			lower[i] = strings.ToLower(a)
		}
		rows, err := q.Query(ctx, `SELECT lower(email), locale FROM users WHERE lower(email) = ANY($1) AND locale IS NOT NULL`, lower)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var e, l string
			if err := rows.Scan(&e, &l); err != nil {
				rows.Close()
				return nil, err
			}
			chosen[e] = l
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	var out []langGroup
	at := map[string]int{}
	for _, a := range addresses {
		var code *string
		if l, ok := chosen[strings.ToLower(a)]; ok {
			code = &l
		}
		lang := emailLanguageOf(code)
		i, ok := at[lang]
		if !ok {
			i = len(out)
			at[lang] = i
			out = append(out, langGroup{lang: lang})
		}
		out[i].to = append(out[i].to, a)
	}
	return out, nil
}

// postNotice is the POST /db/notify of the notices (timeout=60): whether the engine took it.
func (d *Deps) postNotice(ctx context.Context, body pyjson.Object) (bool, error) {
	c, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	res, raw, err := d.Engine.JSON(c, http.MethodPost, "/db/notify", body)
	if err != nil {
		return false, err
	}
	if res.StatusCode >= 400 {
		d.Log.Warn("notice refused by the engine", "status", res.StatusCode, "detail", runes(string(raw), 300))
		return false, nil
	}
	return true, nil
}

// ── services/notifier.py notify_failure ───────────────────────────────────────

const notifyMaxErrorChars = 1200

// notifyFailure is notify_failure: a scheduled flow's orchestration that failed, told to the
// flow's addresses when the run before had not failed. It never fails.
func (d *Deps) notifyFailure(ctx context.Context, run *runRow) {
	if run.Kind != "orchestration" || run.Status != "FAILURE" || run.TriggerType != "schedule" {
		return
	}
	if err := d.notifyFailureErr(ctx, run); err != nil {
		d.Log.Error("failure notice not sent", "run", run.ID, "err", err)
	}
}

func (d *Deps) notifyFailureErr(ctx context.Context, run *runRow) error {
	if run.FlowID == nil || *run.FlowID == 0 {
		return nil
	}
	var name string
	var emails *string
	var connID *int64
	err := d.DB.QueryRow(ctx, `SELECT name, notify_emails, notify_connection_id FROM flows WHERE id = $1`, *run.FlowID).
		Scan(&name, &emails, &connID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	var to []string
	for _, a := range strings.Split(strings.ReplaceAll(deref(emails), ";", ","), ",") {
		if a = pystr.Strip(a); a != "" {
			to = append(to, a)
		}
	}
	if len(to) == 0 || connID == nil || *connID == 0 {
		return nil
	}
	conn, err := loadEngineConn(ctx, d.DB, *connID)
	if err != nil {
		return err
	}
	if conn == nil || conn.DBType != "smtp" {
		d.Log.Info("failure notice skipped: SMTP connection missing", "flow", *run.FlowID)
		return nil
	}
	// _was_failing_already
	var previous string
	err = d.DB.QueryRow(ctx, `SELECT status FROM runs WHERE flow_id = $1 AND kind = 'orchestration' AND id != $2
		AND status IN ('SUCCESS', 'FAILURE') AND (error IS NULL OR error NOT IN ('flow already running', 'flusso già in esecuzione'))
		ORDER BY started_at DESC, id DESC LIMIT 1`, *run.FlowID, run.ID).Scan(&previous)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	if previous == "FAILURE" {
		d.Log.Info("failure notice skipped: it was failing already", "flow", *run.FlowID)
		return nil
	}
	groups, err := byLanguage(ctx, d.DB, to)
	if err != nil {
		return err
	}
	ok := true
	for _, g := range groups {
		took, err := d.postNotice(ctx, pyjson.Object{{Key: "connection", Value: conn.payload()}, {Key: "to", Value: stringsAny(g.to)},
			{Key: "subject", Value: emailText(g.lang, "flow_subject", "flow", name)}, {Key: "body", Value: failureBody(name, run, g.lang)}})
		if err != nil {
			return err
		}
		ok = ok && took
	}
	outcome := "success"
	if !ok {
		outcome = "failure"
	}
	now := jobs.Now()
	audit.Record(ctx, d.DB, d.Log, audit.Event{ActorLabel: "scheduler", Action: "email.send", Outcome: outcome, TargetType: strp("flow"),
		TargetID: run.FlowID, TargetLabel: &name, At: &now, Detail: pyjson.Object{{Key: "reason", Value: "run_failed"},
			{Key: "run_id", Value: run.ID}, {Key: "recipients", Value: int64(len(to))}}})
	return nil
}

// failureBody is notifier._body.
func failureBody(flow string, run *runRow, lang string) string {
	when := jobs.Now()
	if run.FinishedAt != nil {
		when = *run.FinishedAt
	}
	fields := [][2]string{{"when", when.Format("2006-01-02 15:04") + " UTC"}, {"run", fmt.Sprintf("#%d", run.ID)}}
	if run.Engine != nil && *run.Engine != "" {
		fields = append(fields, [2]string{"engine", *run.Engine})
	}
	rows := append([]string{emailText(lang, "flow_intro", "flow", flow), ""}, emailLabelled(lang, fields)...)
	if run.Error != nil && *run.Error != "" {
		rows = append(rows, "", emailText(lang, "error"), runes(*run.Error, notifyMaxErrorChars))
	}
	rows = append(rows, "", "—", emailText(lang, "flow_footer"))
	return strings.Join(rows, "\n")
}
