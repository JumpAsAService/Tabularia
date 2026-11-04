package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/service_accounts.py: the integrations' accounts and their tokens.
func init() {
	created := []string{"token", "prefix", "expires_at"}
	register(Suite{
		Module: "service_accounts",
		Seed: func(w *World) {
			maskColumns("api_tokens", "prefix", "token_hash", "expires_at")
			maskPattern("audit_logs", "detail", `tab_[A-Za-z0-9_-]{8}`)
			maskPattern("audit_logs", "detail", `"expires_at": "[^"]*"`)
			// a taken address gets a random suffix
			maskPattern("users", "email", `^report-bot-[0-9a-f]{6}@`)
			maskPattern("audit_logs", "target_label", `^report-bot-[0-9a-f]{6}@`)
			w.Person("sa-etl", "sa-etl@service.invalid", "", false, false, true)
			w.Exec(`UPDATE users SET full_name = 'Zeta ETL' WHERE email = 'sa-etl@service.invalid'`)
			w.Token("sa-etl", "tab_sae1_00000000000000000000", "write", time.Hour)
			w.Token("sa-etl", "tab_sae2_00000000000000000000", "read", time.Hour)
			w.Token("sa-etl", "tab_sae3_old_0000000000000000", "read", -time.Hour)
			w.Exec(`UPDATE api_tokens SET last_used_at = '2026-01-02', last_used_ip = '1.1.1.1' WHERE prefix = 'tab_sae1'`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2), ($1, $3)`, w.People["sa-etl"], Groups["Sales"], Groups["Analysts"])
			w.Person("sa-bi", "report-bot@service.invalid", "", false, false, true)
			w.Exec(`UPDATE users SET full_name = 'Alpha BI' WHERE email = 'report-bot@service.invalid'`)
			w.Token("admin", "tab_saad_000000000000000000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			id := func(k string) string { return fmt.Sprint(w.People[k]) }
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any, mask []string) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body, Mask: mask})
			}
			for _, p := range []string{"admin", "obs_flag", "writer"} {
				add(p+" lists them", "GET", "/service-accounts", w.As(p), nil, nil)
			}
			add("nobody", "GET", "/service-accounts", nil, nil, nil)

			add("create", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": "  Nightly ETL!! ", "description": "  loads  "}, nil)
			add("a name whose address is taken", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": "Report Bot"}, []string{"email"})
			add("a name of symbols only", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": "è!!"}, nil)
			add("a long root", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": strings.Repeat("ab ", 26), "description": strings.Repeat("d", 250)}, nil)
			add("a blank name", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": "  "}, nil)
			add("a name too long", "POST", "/service-accounts", w.As("admin"), map[string]any{"name": strings.Repeat("n", 81)}, nil)
			add("with a token", "POST", "/service-accounts", Bearer("tab_saad_000000000000000000000"), map[string]any{"name": "x"}, nil)
			add("an observer", "POST", "/service-accounts", w.As("obs_flag"), map[string]any{"name": "x"}, nil)
			s = append(s, bodyEdges("POST", "/service-accounts", w.As("admin"))...)

			add("its tokens", "GET", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("admin"), nil, nil)
			add("its tokens, to an observer", "GET", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("obs_group"), nil, nil)
			add("a person's tokens", "GET", "/service-accounts/"+id("writer")+"/tokens", w.As("admin"), nil, nil)
			add("an unknown account", "GET", "/service-accounts/999999/tokens", w.As("admin"), nil, nil)
			add("a bad path", "GET", "/service-accounts/x/tokens", w.As("admin"), nil, nil)
			add("not an observer", "GET", "/service-accounts/x/tokens", w.As("writer"), nil, nil)

			add("a token for it", "POST", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("admin"), map[string]any{"name": "loader", "scope": "write"}, created)
			add("a token for a person", "POST", "/service-accounts/"+id("writer")+"/tokens", w.As("admin"), map[string]any{"name": "x"}, created)
			add("a token, with a token", "POST", "/service-accounts/"+id("sa-etl")+"/tokens", Bearer("tab_saad_000000000000000000000"), map[string]any{"name": "x"}, created)
			add("a token, a bad name", "POST", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("admin"), map[string]any{"name": ""}, created)
			add("a token, bad path and body", "POST", "/service-accounts/x/tokens", w.As("admin"), map[string]any{"scope": "all"}, created)
			add("a token, an observer", "POST", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("obs_flag"), map[string]any{"name": "x"}, created)
			s = append(s, bodyEdges("POST", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("admin"))...)
			add("its tokens now", "GET", "/service-accounts/"+id("sa-etl")+"/tokens", w.As("admin"), nil, []string{"*.prefix", "*.expires_at"})

			tok := func(prefix string) string {
				var n int64
				w.a.db.QueryRow(w.ctx, `SELECT id FROM api_tokens WHERE prefix = $1`, prefix).Scan(&n)
				return fmt.Sprint(n)
			}
			add("revoke its token", "DELETE", "/service-accounts/"+id("sa-etl")+"/tokens/"+tok("tab_sae2"), w.As("admin"), nil, nil)
			add("again", "DELETE", "/service-accounts/"+id("sa-etl")+"/tokens/"+tok("tab_sae2"), w.As("admin"), nil, nil)
			add("another account's token", "DELETE", "/service-accounts/"+id("sa-bi")+"/tokens/"+tok("tab_sae1"), w.As("admin"), nil, nil)
			add("an unknown account", "DELETE", "/service-accounts/999999/tokens/1", w.As("admin"), nil, nil)
			add("ids past the integers", "DELETE", "/service-accounts/3000000000/tokens/3000000000", w.As("admin"), nil, nil)
			add("an account past the integers", "GET", "/service-accounts/3000000000/tokens", w.As("admin"), nil, nil)
			add("bad paths", "DELETE", "/service-accounts/x/tokens/y", w.As("admin"), nil, nil)
			add("an observer revokes", "DELETE", "/service-accounts/"+id("sa-etl")+"/tokens/"+tok("tab_sae1"), w.As("obs_flag"), nil, nil)

			add("delete, with a token", "DELETE", "/service-accounts/"+id("sa-bi"), Bearer("tab_saad_000000000000000000000"), nil, nil)
			add("delete a person", "DELETE", "/service-accounts/"+id("writer"), w.As("admin"), nil, nil)
			add("delete an unknown one", "DELETE", "/service-accounts/999999", w.As("admin"), nil, nil)
			add("delete, a bad path", "DELETE", "/service-accounts/x", w.As("admin"), nil, nil)
			add("delete, an observer", "DELETE", "/service-accounts/"+id("sa-bi"), w.As("obs_flag"), nil, nil)
			add("delete", "DELETE", "/service-accounts/"+id("sa-etl"), w.As("admin"), nil, nil)
			add("the list now", "GET", "/service-accounts", w.As("admin"), nil, []string{"*.email"})
			// the account with the random address goes, so that the next suites see none: it is
			// the second account this suite creates
			var last int64
			w.a.db.QueryRow(w.ctx, `SELECT last_value FROM users_id_seq`).Scan(&last)
			add("delete the account with a random address", "DELETE", fmt.Sprint("/service-accounts/", last+2), w.As("admin"), nil, nil)
			return s
		},
	})
}
