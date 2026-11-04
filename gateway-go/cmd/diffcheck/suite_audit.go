package main

import (
	"net/http"
	"strconv"
	"time"
)

// routes/audit.py: the audit log, the sign-ins per hour, the open sessions and closing one.
func init() {
	register(Suite{
		Module: "audit",
		Seed: func(w *World) {
			now := time.Now().UTC()
			ev := func(ts time.Time, actor *int64, label, action, outcome string, ttype *string, tid *int64, tlabel, detail, ip, agent *string) {
				w.Exec(`INSERT INTO audit_logs (ts, actor_id, actor_label, action, outcome, target_type, target_id, target_label,
					detail, ip, user_agent) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
					ts, actor, label, action, outcome, ttype, tid, tlabel, detail, ip, agent)
			}
			admin, editor := ptr(w.People["admin"]), ptr(w.People["editor"])
			old := time.Date(2026, 1, 10, 9, 30, 0, 0, time.UTC)
			ev(old, admin, "admin@x.test", "flow.create", "success", ptr("flow"), ptr(int64(7)), ptr("Sales flow"),
				ptr(`{"name": "Sales flow", "engine": "duckdb", "rows": 12, "ratio": 1.5}`), ptr("10.0.0.1"), ptr("Firefox"))
			ev(old.Add(time.Hour), editor, "editor@x.test", "user.promote", "success", ptr("user"), ptr(w.People["writer"]),
				ptr("writer@x.test"), ptr(`{"host": "db.internal", "url": 5, "path": null, "prefix": "tab_abcd", "to": ["a@b.it"],
					"nested": {"sql": "select 1", "owner": "carla@x.test", "list": ["x@y.z", 3, {"server": "s", "fingerprint": true}]},
					"groups": ["Sales"], "full_name": "Carla Rossi", "sub": {"k": 1}, "count": 3, "who": "@nobody"}`),
				ptr("10.0.0.2"), ptr("Chrome"))
			ev(old.Add(2*time.Hour), nil, "anonymous", "auth.login_failed", "failure", nil, nil, ptr("ghost@x.test"),
				ptr(`{"reason": "bad password", "prefix": ""}`), ptr("203.0.113.9"), nil)
			ev(old.Add(3*time.Hour), admin, "admin@x.test", "connection.update", "success", ptr("connection"), ptr(int64(3)),
				ptr("Warehouse"), ptr(`not json at all`), nil, nil)
			ev(old.Add(4*time.Hour), admin, "service-bot", "token.create", "success", ptr("user"), ptr(int64(9)), ptr("no email here"),
				ptr(`{"name": "script", "a": 1, "a": 2, "prefix": 0, "secret": 0, "big": 123456789012345678901234567890}`), nil, nil)
			ev(old.Add(5*time.Hour), admin, "admin@x.test", "flow.delete", "success", ptr("flow"), ptr(int64(7)), ptr("Sales flow"), ptr(``), nil, nil)
			ev(old.Add(6*time.Hour), editor, "editor@x.test", "flow.run", "failure", ptr("flow"), ptr(int64(7)), ptr("100% done_x"),
				ptr(`null`), ptr("10.0.0.3"), ptr("curl"))
			// sign-ins of the last hours, for access-activity
			for i, m := range []time.Duration{20, 50, 130, 135, 300, 1500, 2000} {
				action, outcome := "auth.login", "success"
				if i%3 == 1 {
					action, outcome = "auth.login_failed", "failure"
				}
				ev(now.Add(-m*time.Minute), editor, "editor@x.test", action, outcome, ptr("user"), editor, ptr("editor@x.test"), nil,
					ptr("10.1.1.1"), ptr("Safari"))
			}
			ev(now.Add(-40*time.Minute), admin, "admin@x.test", "auth.logout", "success", nil, nil, nil, nil, nil, nil)

			// sessions: one to close, one expired, one quiet, an observer's
			at := func(d time.Duration) time.Time { return now.Add(d) }
			w.Exec(`INSERT INTO user_sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method) VALUES
				('audit-close', $1, $2, $3, $4, '10.9.9.9', 'Edge', 'password'),
				('audit-gone', $1, $2, $2, $5, '10.9.9.8', 'Edge', 'password'),
				('audit-quiet', $6, $2, $7, $4, NULL, NULL, 'sso')`,
				w.People["writer"], at(-3*time.Hour), at(-2*time.Minute), at(5*time.Hour), at(-time.Minute),
				w.People["sso"], at(-40*time.Minute))
			w.Token("admin", "tab_audit_admin_write_00000", "write", time.Hour)
			w.Token("admin", "tab_audit_admin_read_000000", "read", time.Hour)
			w.Token("service", "tab_audit_service_00000000", "write", time.Hour)
		},
		Phases: []Phase{
			{Name: "environment's settings"},
			{Name: "panel: Rome's time, idle timeout 30 min", Panel: map[string]string{
				"app.timezone": `"Europe/Rome"`, "auth.session_idle_minutes": "30"}},
			{Name: "panel: a zone that does not exist", Panel: map[string]string{"app.timezone": `"Mars/Olympus"`}},
		},
		Steps: func(w *World) []Step {
			var s []Step
			get := func(name, path string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "GET", Path: path, Auth: auth})
			}
			for _, p := range []string{"admin", "editor", "obs_flag", "obs_group", "admin_group", "inactive", "service", "writer"} {
				get("the audit · "+p, "/audit", w.As(p))
			}
			get("the audit · nobody", "/audit", nil)
			get("the audit · nobody, bad query: 401 first", "/audit?limit=0&since=x", nil)
			get("the audit · a write token", "/audit", Bearer("tab_audit_admin_write_00000"))
			get("the audit · a read token", "/audit", Bearer("tab_audit_admin_read_000000"))
			get("the audit · a service account's token (not an observer)", "/audit", Bearer("tab_audit_service_00000000"))
			for _, who := range []string{"admin", "obs_flag"} {
				for _, q := range []string{
					"?q=sales", "?q=x.test", "?q=10.0", "?q=LOGIN", "?q=writer", "?q=%25", "?q=_", "?q=", "?q=no%20match",
					"?action=flow.create", "?action=", "?target_type=flow", "?target_type=user&outcome=failure",
					"?outcome=success&limit=3", "?outcome=failure&offset=2", "?actor_id=" + itoa(w.People["editor"]),
					"?actor_id=99999999999", "?actor_id=-1", "?actor_id=1.0", "?actor_id=+1",
					"?since=2026-01-10T11:00:00", "?since=2026-01-10T12:00:00%2B02:00&until=2026-01-10T13:00:00Z",
					"?until=2026-01-10", "?since=2026-01-10T10:30:00.000001", "?since=1768040000",
					"?limit=1", "?limit=200", "?limit=2&offset=1", "?offset=1000", "?q=flow&action=flow.run&limit=5",
					"?target_type=connection", "?target_type=flow&q=done",
				} {
					get("the audit "+q+" · "+who, "/audit"+q, w.As(who))
				}
			}
			for _, q := range []string{
				"?limit=0", "?limit=201", "?limit=x", "?offset=-1", "?actor_id=abc", "?since=yesterday",
				"?since=2026-13-01", "?until=2026-01-10T25:00", "?limit=0&offset=-1&actor_id=x&since=y",
				"?limit=5&limit=0", "?limit=0&limit=5",
			} {
				get("the audit, refused "+q, "/audit"+q, w.As("admin"))
			}
			for _, p := range []string{"admin", "obs_group", "editor"} {
				get("the actions · "+p, "/audit/actions", w.As(p))
			}
			get("the actions · nobody", "/audit/actions", nil)
			for _, q := range []string{"", "?hours=1", "?hours=3", "?hours=168", "?hours=0", "?hours=169", "?hours=x", "?hours=2.0"} {
				get("the sign-ins per hour "+q, "/audit/access-activity"+q, w.As("admin"))
			}
			get("the sign-ins per hour · observer", "/audit/access-activity?hours=30", w.As("obs_flag"))
			get("the sign-ins per hour · editor", "/audit/access-activity", w.As("editor"))
			for _, p := range []string{"admin", "obs_flag", "admin_group", "editor"} {
				get("the sessions · "+p, "/audit/sessions", w.As(p))
			}
			get("the sessions · a token: none is current", "/audit/sessions", Bearer("tab_audit_admin_write_00000"))
			for _, q := range []string{"?limit=1", "?limit=500", "?limit=0", "?limit=501", "?limit=z"} {
				get("the sessions "+q, "/audit/sessions"+q, w.As("admin"))
			}
			del := func(name, sid string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "DELETE", Path: "/audit/sessions/" + sid, Auth: auth})
			}
			del("close · nobody", "audit-close", nil)
			del("close · editor (no reading either)", "audit-close", w.As("editor"))
			del("close · an observer", "audit-close", w.As("obs_flag"))
			del("close · the cookie without the CSRF header", "audit-close", Cookie(w.JWT("admin", "s-admin")))
			del("close · a read token", "audit-close", Bearer("tab_audit_admin_read_000000"))
			del("close · a session that is not there", "audit-nope", w.As("admin"))
			del("close · an expired one, still a row", "audit-gone", w.As("admin_group"))
			del("close · admin", "audit-close", w.As("admin"))
			del("close · admin, again", "audit-close", w.As("admin"))
			del("close · a write token", "audit-quiet", Bearer("tab_audit_admin_write_00000"))
			get("the audit after closing", "/audit?limit=3", w.As("admin"))
			return s
		},
	})
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }
