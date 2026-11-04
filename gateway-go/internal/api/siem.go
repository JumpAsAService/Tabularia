package api

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/siem"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/webhooks"
)

// routes/siem.py: where the audit log goes (Splunk's HTTP Event Collector), and how it is
// going. Read by administrators and observers; written and tested by administrators, never
// with an API token.
func init() {
	register("siem", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/siem", Handler: adminRole(d, "/admin/siem", false, siemRead(d))},
			{Method: http.MethodPut, Pattern: "/admin/siem", Handler: siemWrite(d)},
			{Method: http.MethodPost, Pattern: "/admin/siem/test", Handler: adminRole(d, "/admin/siem/test", true, siemTrial(d))},
		}
	})
}

// noSIEMFromToken is api_tokens.NO_SIEM_FROM_TOKEN.
var noSIEMFromToken = map[string]string{
	"en": "An API token cannot change where the audit log is sent: do it from the interface",
	"it": "Un token API non cambia dove va il registro di audit: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier la destination du journal d'audit : faites-le depuis l'interface",
	"de": "Ein API-Token kann nicht ändern, wohin das Audit-Protokoll geht: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar adónde va el registro de auditoría: hazlo desde la interfaz",
}

// siemRow is the siem_destinations row (models/siem.py), or the one SiemDestination(id=1)
// would be.
type siemRow struct {
	exists                                    bool
	Kind                                      string
	Enabled                                   bool
	URL                                       string
	TokenEnc                                  *string
	Index, Source, Sourcetype, Host           *string
	VerifyTLS                                 bool
	CursorID, SentTotal                       int64
	LastOKAt, LastErrorAt, NextAttemptAt, Upd *time.Time
	LastError, UpdatedBy                      *string
	Failures                                  int64
}

func readSIEM(ctx context.Context, d *Deps) (siemRow, error) {
	r := siemRow{Kind: "splunk_hec", VerifyTLS: true}
	err := d.DB.QueryRow(ctx, `SELECT kind, enabled, url, token_enc, index, source, sourcetype, host, verify_tls, cursor_id,
		sent_total, last_ok_at, last_error, last_error_at, failures, next_attempt_at, updated_at, updated_by
		FROM siem_destinations WHERE id = 1`).Scan(&r.Kind, &r.Enabled, &r.URL, &r.TokenEnc, &r.Index, &r.Source,
		&r.Sourcetype, &r.Host, &r.VerifyTLS, &r.CursorID, &r.SentTotal, &r.LastOKAt, &r.LastError, &r.LastErrorAt,
		&r.Failures, &r.NextAttemptAt, &r.Upd, &r.UpdatedBy)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, nil
	}
	r.exists = err == nil
	return r, err
}

// SiemOut is routes/siem.py SiemOut.
type SiemOut struct {
	Kind            string  `json:"kind"`
	Enabled         bool    `json:"enabled"`
	URL             string  `json:"url"`
	TokenSet        bool    `json:"token_set"`
	Index           *string `json:"index"`
	Source          *string `json:"source"`
	Sourcetype      *string `json:"sourcetype"`
	Host            *string `json:"host"`
	VerifyTLS       bool    `json:"verify_tls"`
	Editable        bool    `json:"editable"`
	SentTotal       int64   `json:"sent_total"`
	Backlog         int64   `json:"backlog"`
	OldestPendingAt *string `json:"oldest_pending_at"`
	LastOKAt        *string `json:"last_ok_at"`
	LastError       *string `json:"last_error"`
	LastErrorAt     *string `json:"last_error_at"`
	Failures        int64   `json:"failures"`
	NextAttemptAt   *string `json:"next_attempt_at"`
}

// siemOut is _out: to the observer only the scheme and the host, no Splunk error. urlsplit's
// ValueError (an address no save lets in) is an error, a 500 as in Python.
func siemOut(ctx context.Context, d *Deps, s siemRow, full bool, p *auth.Principal) (SiemOut, error) {
	var n int64
	var oldest *time.Time
	if s.Enabled {
		// siem.backlog
		if err := d.DB.QueryRow(ctx, `SELECT count(*), min(ts) FROM audit_logs WHERE id > $1`, s.CursorID).Scan(&n, &oldest); err != nil {
			return SiemOut{}, err
		}
	}
	url := s.URL
	if url != "" && !full {
		parts, err := pyurl.URLSplit(url)
		if err != nil {
			return SiemOut{}, err
		}
		url = parts.Scheme + "://" + parts.HostnameOrNone() + "/…"
	}
	out := SiemOut{
		Kind: s.Kind, Enabled: s.Enabled, URL: url, TokenSet: s.TokenEnc != nil && *s.TokenEnc != "", Index: s.Index,
		Source: s.Source, Sourcetype: s.Sourcetype, Host: s.Host, VerifyTLS: s.VerifyTLS,
		Editable: full && p.TokenName == "", SentTotal: s.SentTotal, Backlog: n, OldestPendingAt: isoPtr(oldest),
		LastOKAt: isoPtr(s.LastOKAt), LastErrorAt: isoPtr(s.LastErrorAt), Failures: s.Failures,
		NextAttemptAt: isoPtr(s.NextAttemptAt),
	}
	if full {
		out.LastError = s.LastError
	}
	return out, nil
}

// forwardToPython hands the request to the Python gateway, the body as it came.
func forwardToPython(d *Deps, w http.ResponseWriter, r *http.Request, body []byte) {
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	d.Upstream.ServeHTTP(w, r)
}

// siemRead is GET /admin/siem.
func siemRead(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		s, err := readSIEM(ctx, d)
		if err != nil {
			adminFail(d, w, "reading the SIEM", err)
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		out, err := siemOut(ctx, d, s, full, p)
		siemAnswer(d, w, out, err)
	}
}

// siemWrite is PUT /admin/siem: the body read first (a broken JSON is a 422 for anyone),
// then require_superuser and _no_token, then the body's validation.
func siemWrite(d *Deps) http.Handler {
	return adminBody(d, "/admin/siem", func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		if denyFromToken(w, r, p, noSIEMFromToken) {
			return
		}
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.SiemUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		siemUpdate(d, w, r, p, b, raw)
	})
}

func strOf(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

func eqStr(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func strOrNil(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

func siemUpdate(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, b *pyd.ModelValue, raw []byte) {
	ctx := r.Context()
	s, err := readSIEM(ctx, d)
	if err != nil {
		adminFail(d, w, "reading the SIEM", err)
		return
	}
	before := s
	changes := pyjson.Object{}
	if u := strOf(b.Get("url")); u != nil {
		url := pyd.Strip(*u)
		if url != "" {
			_, err := webhooks.ValidateURL(url)
			if err == nil {
				var ep string
				if ep, err = siem.Endpoint(url); err == nil {
					_, err = webhooks.ValidateURL(ep)
				}
			}
			if errors.Is(err, webhooks.ErrNotASCII) || errors.Is(err, webhooks.ErrIDNAUnavailable) {
				adminFail(d, w, "the address's IDNA encoding", err)
				return
			}
			if err != nil {
				adminDetail(w, http.StatusUnprocessableEntity, err.Error())
				return
			}
		}
		s.URL = url
	}
	text := func(v any) *string { // _text: stripped, empty is none
		t := pyd.Strip(*strOf(v))
		if t == "" {
			return nil
		}
		return &t
	}
	for _, f := range []struct {
		name string
		dst  **string
	}{{"index", &s.Index}, {"source", &s.Source}, {"sourcetype", &s.Sourcetype}, {"host", &s.Host}} {
		if v := b.Get(f.name); v != nil {
			*f.dst = text(v)
		}
	}
	if v, ok := b.Get("verify_tls").(bool); ok {
		s.VerifyTLS = v
	}
	if t := strOf(b.Get("token")); t != nil {
		if tok := pyd.Strip(*t); tok != "" {
			enc, err := d.Fernet.Encrypt(tok)
			if err != nil {
				adminFail(d, w, "encrypting the token", err)
				return
			}
			s.TokenEnc = &enc
			changes = changes.Set("token", "changed")
		} else {
			s.TokenEnc = nil
			changes = changes.Set("token", "removed")
		}
	}
	if v, ok := b.Get("enabled").(bool); ok {
		s.Enabled = v
	}
	if s.Enabled && !(s.URL != "" && s.TokenEnc != nil && *s.TokenEnc != "") {
		adminDetail(w, http.StatusUnprocessableEntity, "Turning forwarding on needs the Splunk address and token")
		return
	}
	if s.Enabled && !before.Enabled {
		if s.CursorID == 0 { // from now: the history stays in the Audit panel
			if err := d.DB.QueryRow(ctx, `SELECT coalesce(max(id), 0) FROM audit_logs`).Scan(&s.CursorID); err != nil {
				adminFail(d, w, "reading the audit's last id", err)
				return
			}
		}
		s.Failures, s.NextAttemptAt = 0, nil
	}
	for _, f := range []string{"url", "token", "verify_tls", "index"} {
		if b.IsSet(f) { // a new configuration: retry at once
			s.Failures, s.NextAttemptAt = 0, nil
		}
	}
	change := func(k string, from, to any) {
		changes = changes.Set(k, pyjson.Object{{Key: "from", Value: from}, {Key: "to", Value: to}})
	}
	if s.Enabled != before.Enabled {
		change("enabled", before.Enabled, s.Enabled)
	}
	if s.URL != before.URL {
		change("url", before.URL, s.URL)
	}
	for _, f := range []struct {
		k      string
		at, bt *string
	}{{"index", before.Index, s.Index}, {"source", before.Source, s.Source}, {"sourcetype", before.Sourcetype, s.Sourcetype},
		{"host", before.Host, s.Host}} {
		if !eqStr(f.at, f.bt) {
			change(f.k, strOrNil(f.at), strOrNil(f.bt))
		}
	}
	if s.VerifyTLS != before.VerifyTLS {
		change("verify_tls", before.VerifyTLS, s.VerifyTLS)
	}
	if len(changes) == 0 { // nothing saved; the answer shows the object as changed in memory
		out, err := siemOut(ctx, d, s, true, p)
		siemAnswer(d, w, out, err)
		return
	}
	now := nowNaive()
	s.Upd, s.UpdatedBy = &now, &p.User.Email
	if s.exists {
		_, err = d.DB.Exec(ctx, `UPDATE siem_destinations SET enabled = $1, url = $2, token_enc = $3, index = $4, source = $5,
			sourcetype = $6, host = $7, verify_tls = $8, cursor_id = $9, failures = $10, next_attempt_at = $11,
			updated_at = $12, updated_by = $13 WHERE id = 1`, s.Enabled, s.URL, s.TokenEnc, s.Index, s.Source, s.Sourcetype,
			s.Host, s.VerifyTLS, s.CursorID, s.Failures, s.NextAttemptAt, s.Upd, s.UpdatedBy)
	} else {
		_, err = d.DB.Exec(ctx, `INSERT INTO siem_destinations (id, kind, enabled, url, token_enc, index, source, sourcetype,
			host, verify_tls, cursor_id, sent_total, last_ok_at, last_error, last_error_at, failures, next_attempt_at,
			updated_at, updated_by) VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, NULL, NULL, NULL, $11, $12, $13, $14)`,
			s.Kind, s.Enabled, s.URL, s.TokenEnc, s.Index, s.Source, s.Sourcetype, s.Host, s.VerifyTLS, s.CursorID,
			s.Failures, s.NextAttemptAt, s.Upd, s.UpdatedBy)
	}
	if err != nil {
		adminFail(d, w, "saving the SIEM", err)
		return
	}
	var label *string
	if s.URL != "" {
		if parts, err := pyurl.URLSplit(s.URL); err == nil {
			if h, ok := parts.Hostname(); ok {
				label = &h
			}
		}
	}
	tt := "siem"
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "siem.update", TargetType: &tt, TargetLabel: label,
		Detail: pyjson.Object{{Key: "changes", Value: changes}}})
	saved, err := readSIEM(ctx, d)
	if err != nil {
		adminFail(d, w, "reading the SIEM", err)
		return
	}
	out, err := siemOut(ctx, d, saved, true, p)
	siemAnswer(d, w, out, err)
}

func siemAnswer(d *Deps, w http.ResponseWriter, out SiemOut, err error) {
	if err != nil {
		adminFail(d, w, "the SIEM's backlog", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// SiemTestOut is routes/siem.py SiemTestOut.
type SiemTestOut struct {
	OK         bool   `json:"ok"`
	StatusCode *int   `json:"status_code"`
	Message    string `json:"message"`
}

// siemTrial is POST /admin/siem/test: a test event sent to Splunk now (not through the
// queue), and what it answered.
func siemTrial(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if denyFromToken(w, r, p, noSIEMFromToken) {
			return
		}
		ctx := r.Context()
		s, err := readSIEM(ctx, d)
		if err != nil {
			adminFail(d, w, "reading the SIEM", err)
			return
		}
		if s.URL == "" || s.TokenEnc == nil || *s.TokenEnc == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "The Splunk address and token are needed")
			return
		}
		token, err := d.Fernet.Decrypt(*s.TokenEnc)
		if err != nil {
			adminFail(d, w, "decrypting the Splunk token", err)
			return
		}
		at := time.Now().UTC()
		ts, _ := strconv.ParseFloat(strconv.FormatFloat(float64(at.UnixMicro())/1e6, 'f', 3, 64), 64) // round(…, 3)
		ev := pyjson.Object{
			{Key: "time", Value: ts}, {Key: "host", Value: siemOr(s.Host, siem.Host)},
			{Key: "source", Value: siemOr(s.Source, siem.Source)}, {Key: "sourcetype", Value: siemOr(s.Sourcetype, siem.Sourcetype)},
		}
		if s.Index != nil && *s.Index != "" {
			ev = append(ev, pyjson.KV{Key: "index", Value: *s.Index})
		}
		ev = append(ev, pyjson.KV{Key: "event", Value: pyjson.Object{
			{Key: "action", Value: "siem.test"}, {Key: "outcome", Value: "success"}, {Key: "user", Value: p.User.Email},
			{Key: "app", Value: "tabularia"}, {Key: "test", Value: true}, {Key: "message", Value: "Tabularia: Splunk delivery test"},
		}})
		timeout := time.Duration(d.Panel.WebhooksTimeout() * float64(time.Second))
		res := siem.Send(ctx, siem.Destination{URL: s.URL, Token: token, VerifyTLS: s.VerifyTLS}, []pyjson.Object{ev}, timeout)
		outcome := "success"
		if !res.OK {
			outcome = "failure"
		}
		// urlsplit(d.url).hostname, after the send: its ValueError (an address no save lets
		// in) is a 500 with the event sent and nothing audited
		parts, err := pyurl.URLSplit(s.URL)
		if err != nil {
			adminFail(d, w, "the SIEM's address", err)
			return
		}
		var label *string
		if h, ok := parts.Hostname(); ok {
			label = &h
		}
		var code any
		if res.Code != nil {
			code = *res.Code
		}
		tt := "siem"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "siem.test", Outcome: outcome, TargetType: &tt,
			TargetLabel: label, Detail: pyjson.Object{{Key: "status_code", Value: code}}})
		server.JSON(w, http.StatusOK, SiemTestOut{OK: res.OK, StatusCode: res.Code, Message: res.Message})
	}
}

// siemOr is `x or DEFAULT`.
func siemOr(s *string, def string) string {
	if s == nil || *s == "" {
		return def
	}
	return *s
}
