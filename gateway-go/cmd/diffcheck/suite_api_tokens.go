package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// tokenIDs by the secret, for the steps that revoke one.
func (w *World) tokenID(raw string) string {
	var id int64
	if err := w.a.db.QueryRow(w.ctx, `SELECT id FROM api_tokens WHERE prefix = $1 AND user_id IS NOT NULL ORDER BY id LIMIT 1`, raw[:8]).Scan(&id); err != nil {
		return "0"
	}
	return fmt.Sprint(id)
}

// routes/api_tokens.py: one's own tokens and, for the administrator, everyone's.
func init() {
	random := func() {
		maskColumns("api_tokens", "prefix", "token_hash", "expires_at")
		maskPattern("audit_logs", "detail", `tab_[A-Za-z0-9_-]{8}`)
		maskPattern("audit_logs", "detail", `"expires_at": "[^"]*"`)
	}
	created := []string{"token", "prefix", "expires_at"}
	listed := []string{"*.prefix", "*.expires_at"}
	register(Suite{
		Module: "api_tokens",
		Seed: func(w *World) {
			random()
			w.Person("at-own", "at-own@x.test", "", false, false, false)
			w.Token("at-own", "tab_aow1_write_000000000000", "write", time.Hour)
			w.Token("at-own", "tab_aor1_read_0000000000000", "read", time.Hour)
			w.Token("at-own", "tab_aog1_gone_0000000000000", "write", 2*time.Hour)
			w.Token("at-own", "tab_aoo1_old_00000000000000", "write", -time.Hour)
			w.Person("at-svc", "at-svc@service.invalid", "", false, false, true)
			w.Token("at-svc", "tab_asv1_svc_00000000000000", "write", time.Hour)
			w.Exec(`UPDATE api_tokens SET last_used_at = '2026-01-02', last_used_ip = '1.1.1.1' WHERE prefix = 'tab_aow1'`)
			w.Token("admin", "tab_aad1_admin_000000000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any, mask []string) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Mask: mask})
			}
			add("one's tokens", "GET", "/auth/tokens", w.As("at-own"), nil, nil)
			add("one's tokens, with a token", "GET", "/auth/tokens", Bearer("tab_aor1_read_0000000000000"), nil, nil)
			add("nobody's", "GET", "/auth/tokens", nil, nil, nil)
			add("an observer's (none)", "GET", "/auth/tokens", w.As("obs_flag"), nil, nil)

			add("create one", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "  nightly  ", "scope": "write", "expires_in_days": 30}, created)
			add("create one, the defaults", "POST", "/auth/tokens", w.As("writer"), map[string]any{"name": "default"}, created)
			add("an administrator's, a year", "POST", "/auth/tokens", w.As("admin_group"), map[string]any{"name": "year", "expires_in_days": 365}, created)
			add("with a token", "POST", "/auth/tokens", Bearer("tab_aow1_write_000000000000"), map[string]any{"name": "x"}, created)
			add("a service account, with its token", "POST", "/auth/tokens", Bearer("tab_asv1_svc_00000000000000"), map[string]any{"name": "x"}, created)
			add("an observer", "POST", "/auth/tokens", w.As("obs_group"), map[string]any{"name": "x"}, created)
			add("a blank name", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "   "}, created)
			add("a long name", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": strings.Repeat("n", 81)}, created)
			add("no days", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "x", "expires_in_days": 0}, created)
			add("too many days", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "x", "expires_in_days": 366}, created)
			add("days beyond any integer", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "x", "expires_in_days": 1e30}, created)
			add("an unknown scope", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"name": "x", "scope": "admin"}, created)
			add("no name", "POST", "/auth/tokens", w.As("at-own"), map[string]any{"scope": "read"}, created)
			add("nobody", "POST", "/auth/tokens", nil, map[string]any{"name": "x"}, created)
			s = append(s, bodyEdges("POST", "/auth/tokens", w.As("at-own"))...)
			add("one's tokens now", "GET", "/auth/tokens", w.As("at-own"), nil, listed)

			add("everyone's", "GET", "/admin/api-tokens", w.As("admin"), nil, listed)
			add("everyone's, to an observer", "GET", "/admin/api-tokens", w.As("obs_flag"), nil, listed)
			add("everyone's, not an administrator", "GET", "/admin/api-tokens", w.As("writer"), nil, listed)
			add("everyone's, nobody", "GET", "/admin/api-tokens", nil, nil, listed)

			add("revoke one's own", "DELETE", "/auth/tokens/"+w.tokenID("tab_aog1_gone_0000000000000"), w.As("at-own"), nil, nil)
			add("again: gone", "DELETE", "/auth/tokens/"+w.tokenID("tab_aog1_gone_0000000000000"), w.As("at-own"), nil, nil)
			add("someone else's", "DELETE", "/auth/tokens/"+w.tokenID("tab_asv1_svc_00000000000000"), w.As("at-own"), nil, nil)
			add("a read token revokes itself", "DELETE", "/auth/tokens/"+w.tokenID("tab_aor1_read_0000000000000"), Bearer("tab_aor1_read_0000000000000"), nil, nil)
			add("a write token revokes itself", "DELETE", "/auth/tokens/"+w.tokenID("tab_aow1_write_000000000000"), Bearer("tab_aow1_write_000000000000"), nil, nil)
			add("a bad path", "DELETE", "/auth/tokens/x", w.As("at-own"), nil, nil)
			add("nobody", "DELETE", "/auth/tokens/1", nil, nil, nil)
			add("an administrator revokes any", "DELETE", "/admin/api-tokens/"+w.tokenID("tab_asv1_svc_00000000000000"), w.As("admin"), nil, nil)
			add("again", "DELETE", "/admin/api-tokens/"+w.tokenID("tab_asv1_svc_00000000000000"), w.As("admin"), nil, nil)
			add("an expired one", "DELETE", "/admin/api-tokens/"+w.tokenID("tab_aoo1_old_00000000000000"), Bearer("tab_aad1_admin_000000000000"), nil, nil)
			add("an observer revokes", "DELETE", "/admin/api-tokens/"+w.tokenID("tab_aor1_read_0000000000000"), w.As("obs_flag"), nil, nil)
			add("a bad path", "DELETE", "/admin/api-tokens/x", w.As("admin"), nil, nil)
			return s
		},
	})
	register(Suite{
		Module: "api_tokens",
		Phases: []Phase{{Name: "panel: no personal tokens, 30 days at most", Panel: map[string]string{
			"auth.personal_tokens": "false", "auth.api_token_max_days": "30"}}},
		Steps: func(w *World) []Step {
			return []Step{
				{Name: "personal tokens off", Method: "POST", Path: "/auth/tokens", Auth: w.As("writer"), JSON: map[string]any{"name": "x"}, Mask: created},
				{Name: "a service account, with its token", Method: "POST", Path: "/auth/tokens", Auth: Bearer("tab_asv1_svc_00000000000000"), JSON: map[string]any{"name": "x"}, Mask: created},
				{Name: "one's tokens", Method: "GET", Path: "/auth/tokens", Auth: w.As("writer"), Mask: listed},
			}
		},
	})
}
