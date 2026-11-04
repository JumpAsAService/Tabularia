package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// The scheduler's chores after the schedules: the data contracts' freshness and notices,
// the shared state's expired rows, the blobs past their grace.

// ── services/contracts.py sweep_freshness ─────────────────────────────────────

// sweepFreshness is sweep_freshness: the contracts with a freshness rule whose outcome
// changed as time went by get a new report. One contract per transaction.
func (s *scheduler) sweepFreshness(ctx context.Context, now time.Time) (int, error) {
	rows, err := s.d.DB.Query(ctx, `SELECT data_contracts.id, data_contracts.datasource_id FROM data_contracts, datasources
		WHERE data_contracts.datasource_id = datasources.id AND data_contracts.enabled = true
		AND (data_contracts.document LIKE '%' || $1 || '%')`, `"freshness"`)
	if err != nil {
		return 0, err
	}
	type pair struct{ contract, ds int64 }
	var list []pair
	for rows.Next() {
		var p pair
		if err := rows.Scan(&p.contract, &p.ds); err != nil {
			rows.Close()
			return 0, err
		}
		list = append(list, p)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	changed := 0
	for _, p := range list {
		err := pgx.BeginFunc(ctx, s.d.DB, func(tx pgx.Tx) error {
			c, err := contractOf(ctx, tx, p.ds)
			if err != nil || c == nil || c.ID != p.contract {
				return err
			}
			ds, err := loadJobDatasource(ctx, tx, p.ds)
			if err != nil || ds == nil {
				return err
			}
			did, err := refreshFreshness(ctx, tx, c, ds, now)
			if did {
				changed++
			}
			return err
		})
		if err != nil {
			s.d.Log.Error("freshness of the contract not updated", "datasource", p.ds, "err", err)
		}
	}
	return changed, nil
}

// refreshFreshness is refresh_freshness: the snapshot's freshness rules (those not on a
// column) recomputed on the time passed; true when the outcome changed.
func refreshFreshness(ctx context.Context, tx pgx.Tx, c *contractRow, ds *jobDatasource, now time.Time) (bool, error) {
	if !c.Enabled || c.LastReport == nil || *c.LastReport == "" {
		return false, nil
	}
	dv, err1 := pyjson.Loads(c.Document)
	rv, err2 := pyjson.Loads(*c.LastReport)
	if err1 != nil || err2 != nil {
		return false, nil
	}
	document, ok := dv.(pyjson.Object)
	if !ok {
		return false, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(dv))
	}
	report, ok := rv.(pyjson.Object)
	if !ok {
		return false, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(rv))
	}
	rules := map[string]pyjson.Object{}
	docRules, _ := valueOr(document, "rules", []any{}).([]any)
	for _, r := range docRules {
		o := pyjson.Obj(r)
		if o.Value("kind") == "freshness" && !pyjson.Truthy(o.Value("column")) {
			rules[pyjson.Repr(o.Value("id"))] = o
		}
	}
	if len(rules) == 0 {
		return false, nil
	}
	produced := ds.CreatedAt
	switch {
	case ds.RefreshedAt != nil:
		produced = *ds.RefreshedAt
	case !ds.UpdatedAt.IsZero():
		produced = ds.UpdatedAt
	}
	age := pyRound(now.Sub(produced).Seconds()/3600, 2)
	reportRules, _ := valueOr(report, "rules", []any{}).([]any)
	for i, x := range reportRules {
		xo := pyjson.Obj(x)
		r, ok := rules[pyjson.Repr(xo.Value("id"))]
		if !ok {
			continue
		}
		maxAge := r.Value("max_age_hours")
		limit, _ := pystr.Number(maxAge)
		xo = xo.Set("passed", age <= limit).Set("observed", age).Set("expected", maxAge)
		drop := pyjson.Object{}
		for _, kv := range xo {
			if kv.Key != "error" {
				drop = append(drop, kv)
			}
		}
		reportRules[i] = drop
	}
	if _, has := report.Get("rules"); has {
		report = report.Set("rules", reportRules)
	}
	errs, broken := int64(0), int64(0)
	for _, x := range reportRules {
		xo := pyjson.Obj(x)
		if pyjson.Truthy(xo.Value("passed")) {
			continue
		}
		broken++
		if xo.Value("severity") != "warning" {
			errs++
		}
	}
	outcome := "passed"
	if errs > 0 {
		outcome = "failed"
	} else if broken > 0 {
		outcome = "warning"
	}
	if outcome == c.Status {
		return false, nil
	}
	report = report.Set("outcome", outcome).Set("errors", errs).Set("warnings", broken-errs)
	_, err := recordContractResult(ctx, tx, c, report, "freshness", nil, ds.Key, false)
	return true, err
}

// ── services/contract_notifier.py deliver_pending ─────────────────────────────

const (
	contractNoticesPerRound = 20               // deliver_pending's limit
	contractNoticesBudget   = 30 * time.Second // BUDGET_SECONDS
	maxRulesListed          = 10               // MAX_RULES_LISTED
)

// deliverPending is deliver_pending: the notices due sent, each taken by one process.
func (s *scheduler) deliverPending(ctx context.Context) int {
	start := time.Now()
	ids, err := idsOf(ctx, s.d.DB, `SELECT id FROM data_contract_results WHERE notify = 'pending' ORDER BY id LIMIT $1`,
		contractNoticesPerRound)
	if err != nil {
		s.d.Log.Error("data contract notices: the queue cannot be read", "err", err)
		return 0
	}
	sent := 0
	mark := func(id int64, from *string, to string) (bool, error) {
		var tag interface{ RowsAffected() int64 }
		var err error
		if from != nil {
			tag, err = s.d.DB.Exec(ctx, `UPDATE data_contract_results SET notify = $2 WHERE id = $1 AND notify = $3`, id, to, *from)
		} else {
			tag, err = s.d.DB.Exec(ctx, `UPDATE data_contract_results SET notify = $2 WHERE id = $1`, id, to)
		}
		if err != nil {
			return false, err
		}
		return tag.RowsAffected() == 1, nil
	}
	for _, id := range ids {
		if time.Since(start) > contractNoticesBudget {
			s.d.Log.Warn("data contract notices: the round's time is up, the others at the next")
			break
		}
		took, err := mark(id, strp("pending"), "sending")
		outcome := "failed"
		if err == nil {
			if !took {
				continue // another process took it
			}
			if outcome, err = s.sendContractNotice(ctx, id); err != nil {
				outcome = "failed"
			}
		}
		if err != nil {
			s.d.Log.Error("data contract notice not sent", "result", id, "err", err)
		}
		if _, err := mark(id, nil, outcome); err != nil {
			s.d.Log.Error("data contract notice: outcome not recorded", "result", id, "err", err)
		}
		if outcome == "sent" {
			sent++
		}
	}
	return sent
}

type contractResultRow struct {
	ID, DatasourceID, Version int64
	Trigger, Outcome, Report  string
	RunID, Rows               *int64
	Blocked                   bool
	EvaluatedAt               time.Time
}

// sendContractNotice is _send: 'sent' | 'failed' | 'skipped'.
func (s *scheduler) sendContractNotice(ctx context.Context, id int64) (string, error) {
	var r contractResultRow
	err := s.d.DB.QueryRow(ctx, `SELECT id, datasource_id, contract_version, trigger, outcome, report, run_id, rows, blocked, evaluated_at
		FROM data_contract_results WHERE id = $1`, id).Scan(&r.ID, &r.DatasourceID, &r.Version, &r.Trigger, &r.Outcome, &r.Report, &r.RunID,
		&r.Rows, &r.Blocked, &r.EvaluatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return "skipped", nil
	}
	if err != nil {
		return "", err
	}
	var dsName string
	err = s.d.DB.QueryRow(ctx, `SELECT name FROM datasources WHERE id = $1`, r.DatasourceID).Scan(&dsName)
	if errors.Is(err, pgx.ErrNoRows) {
		return "skipped", nil
	}
	if err != nil {
		return "", err
	}
	c, err := contractOf(ctx, s.d.DB, r.DatasourceID)
	if err != nil {
		return "", err
	}
	if c == nil {
		return "skipped", nil
	}
	var to []string
	for _, x := range strings.Split(deref(c.NotifyEmails), ",") {
		if x = pystr.Strip(x); x != "" {
			to = append(to, x)
		}
	}
	var conn *engineConn
	if c.NotifyConnectionID != nil && *c.NotifyConnectionID != 0 {
		if conn, err = loadEngineConn(ctx, s.d.DB, *c.NotifyConnectionID); err != nil {
			return "", err
		}
	}
	if len(to) == 0 || conn == nil || conn.DBType != "smtp" {
		return "skipped", nil
	}
	groups, err := byLanguage(ctx, s.d.DB, to)
	if err != nil {
		return "", err
	}
	ok := true
	for _, g := range groups {
		subject, body := contractMessage(dsName, &r, g.lang)
		took, err := s.d.postNotice(ctx, pyjson.Object{{Key: "connection", Value: conn.payload()}, {Key: "to", Value: stringsAny(g.to)},
			{Key: "subject", Value: subject}, {Key: "body", Value: body}})
		if err != nil {
			return "", err
		}
		ok = ok && took
	}
	outcome := "success"
	if !ok {
		outcome = "failure"
	}
	state := r.Outcome
	if r.Blocked {
		state = "refused"
	}
	now := jobs.Now()
	audit.Record(ctx, s.d.DB, s.d.Log, audit.Event{ActorLabel: "scheduler", Action: "email.send", Outcome: outcome,
		TargetType: strp("datasource"), TargetID: &r.DatasourceID, TargetLabel: &dsName, At: &now,
		Detail: pyjson.Object{{Key: "reason", Value: "contract_state"}, {Key: "state", Value: state}, {Key: "result_id", Value: r.ID},
			{Key: "recipients", Value: int64(len(to))}}})
	if ok {
		return "sent", nil
	}
	return "failed", nil
}

// contractRuleRow is contract_notifier._row.
func contractRuleRow(x pyjson.Object, lang string) string {
	where := ruleWhere(x)
	severity := emailText(lang, "severity_error")
	if x.Value("severity") == "warning" {
		severity = emailText(lang, "severity_warning")
	}
	head := fmt.Sprintf("[%s] %s", severity, pyjson.Str(x.Value("kind")))
	if where != "" {
		head += " " + emailText(lang, "rule_on", "where", where)
	}
	if e := x.Value("error"); pyjson.Truthy(e) {
		return head + ": " + emailText(lang, "rule_unevaluable", "error", runes(pyjson.Str(e), 200))
	}
	if v := x.Value("violations"); pyjson.Truthy(v) {
		return head + ": " + emailText(lang, "rule_rows", "n", v)
	}
	if v := x.Value("observed"); v != nil {
		return head + ": " + emailText(lang, "rule_observed", "value", v)
	}
	return head
}

// contractMessage is contract_notifier.message: (subject, body).
func contractMessage(dsName string, r *contractResultRow, lang string) (string, string) {
	state := r.Outcome
	if r.Blocked {
		state = "refused"
	}
	report := pyjson.Object{}
	if v, err := pyjson.Loads(r.Report); err == nil {
		report = pyjson.Obj(v)
	}
	var broken []pyjson.Object
	rules, _ := valueOr(report, "rules", []any{}).([]any)
	for _, x := range rules {
		if xo := pyjson.Obj(x); !pyjson.Truthy(xo.Value("passed")) {
			broken = append(broken, xo)
		}
	}
	known := func(k string) bool { _, ok := emailTexts["en"][k]; return ok }
	title, sentence, origin := state, "", r.Trigger
	if known("title_" + state) {
		title = emailText(lang, "title_"+state)
	}
	if known("phrase_" + state) {
		sentence = emailText(lang, "phrase_"+state)
	}
	if known("origin_" + r.Trigger) {
		origin = emailText(lang, "origin_"+r.Trigger)
	}
	if r.RunID != nil && *r.RunID != 0 {
		origin += " " + emailText(lang, "contract_run", "run", *r.RunID)
	}
	fields := [][2]string{{"when", r.EvaluatedAt.Format("2006-01-02 15:04") + " UTC"}, {"origin", origin},
		{"contract", emailText(lang, "contract_version", "version", r.Version)}}
	if r.Rows != nil {
		fields = append(fields, [2]string{"rows", fmt.Sprint(*r.Rows)})
	}
	rows := append([]string{emailText(lang, "contract_intro", "ds", dsName, "title", title), "", sentence, ""}, emailLabelled(lang, fields)...)
	if len(broken) > 0 {
		rows = append(rows, "", emailText(lang, "contract_broken"))
		for i, x := range broken {
			if i == maxRulesListed {
				break
			}
			rows = append(rows, "  - "+contractRuleRow(x, lang))
		}
		if len(broken) > maxRulesListed {
			rows = append(rows, "  "+emailText(lang, "contract_more", "n", int64(len(broken)-maxRulesListed)))
		}
	} else if e := report.Value("error"); pyjson.Truthy(e) {
		rows = append(rows, "", emailText(lang, "contract_unevaluable", "error", runes(pyjson.Str(e), 300)))
	}
	rows = append(rows, "", "—", emailText(lang, "contract_footer"))
	return emailText(lang, "contract_subject", "ds", dsName, "title", title), strings.Join(rows, "\n")
}

// ── the shared state's expired rows ───────────────────────────────────────────

const (
	loginThrottleWindow = 300 * time.Second // login_throttle.WINDOW_SECONDS
	flowPresenceTTL     = 40                // flow_presence.TTL_SECONDS
)

// clean is login_throttle.clean, flow_presence.clean, sessions.clean, api_tokens.clean and
// webhooks.clean, in that order: one failing stops the ones after it, as in Python.
func (s *scheduler) clean(ctx context.Context, now time.Time) error {
	steps := []struct {
		sql  string
		args []any
	}{
		{`DELETE FROM login_attempts WHERE last_at < $1`, []any{now.Add(-loginThrottleWindow)}},
		{`DELETE FROM flow_presence WHERE seen < $1`, []any{float64(now.UnixMicro())/1e6 - flowPresenceTTL}},
	}
	minutes := s.d.Panel.SessionIdleMinutes()
	if minutes > 0 {
		steps = append(steps, struct {
			sql  string
			args []any
		}{`DELETE FROM user_sessions WHERE NOT (expires_at > $1 AND last_seen_at >= $2)`,
			[]any{now, now.Add(-time.Duration(minutes)*time.Minute - auth.ActivityEvery)}})
	} else {
		steps = append(steps, struct {
			sql  string
			args []any
		}{`DELETE FROM user_sessions WHERE NOT (expires_at > $1)`, []any{now}})
	}
	steps = append(steps, []struct {
		sql  string
		args []any
	}{
		{`DELETE FROM api_tokens WHERE expires_at <= $1`, []any{now}},
		{`DELETE FROM webhook_deliveries WHERE created_at < $1 AND status IN ('delivered', 'failed')`,
			[]any{now.Add(-time.Duration(s.d.Panel.WebhooksRetentionDays()) * 24 * time.Hour)}},
	}...)
	for _, st := range steps {
		if _, err := s.d.DB.Exec(ctx, st.sql, st.args...); err != nil {
			return err
		}
	}
	return nil
}

// ── services/blobgc.py sweep_blob_deletions ───────────────────────────────────

// sweepBlobDeletions is sweep_blob_deletions: the blobs past their grace deleted on the
// engine (a 404 counts as done), each row removed as soon as its blob is gone.
func (s *scheduler) sweepBlobDeletions(ctx context.Context, now time.Time) (int64, error) {
	type due struct {
		id          int64
		bucket, key string
	}
	rows, err := s.d.DB.Query(ctx, `SELECT id, bucket, key FROM pending_blob_deletions WHERE delete_after <= $1`, now)
	if err != nil {
		return 0, err
	}
	var list []due
	for rows.Next() {
		var x due
		if err := rows.Scan(&x.id, &x.bucket, &x.key); err != nil {
			rows.Close()
			return 0, err
		}
		list = append(list, x)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	var removed int64
	for _, x := range list {
		cctx, cancel := withCheck(ctx)
		res, _, err := s.d.Engine.JSON(cctx, http.MethodDelete, "/files/object?"+url.Values{"bucket": {x.bucket}, "key": {x.key}}.Encode(), nil)
		cancel()
		if err != nil {
			s.d.Log.Warn("sweep blob postponed", "bucket", x.bucket, "key", x.key, "err", err)
			continue
		}
		if res.StatusCode < 400 || res.StatusCode == 404 {
			tag, err := s.d.DB.Exec(ctx, `DELETE FROM pending_blob_deletions WHERE id = $1`, x.id)
			if err != nil {
				return removed, err
			}
			removed += tag.RowsAffected()
		} else {
			s.d.Log.Warn("blob sweep not deleted: retrying", "bucket", x.bucket, "key", x.key, "status", res.StatusCode)
		}
	}
	return removed, nil
}
