package main

import (
	"net/http"
	"time"
)

// routes/siem.py: the SIEM destination read, changed and tried. «siem.test» is each stack's
// own fake engine (scripts/diffcheck.sh), standing in for Splunk's HEC.
func init() {
	maskColumns("siem_destinations", "token_enc") // Fernet: random on each side
	hec := "^/services/collector/event$"
	register(Suite{
		Module: "siem",
		Seed: func(w *World) {
			w.Exec(`DELETE FROM siem_destinations`)
			w.Token("admin", "tab_siem_admin_write_000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			get := func(name string, auth func(*http.Request)) {
				s = append(s, Step{Name: name, Method: "GET", Path: "/admin/siem", Auth: auth})
			}
			put := func(name string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: "PUT", Path: "/admin/siem", Auth: auth, JSON: body})
			}
			try := func(name string, auth func(*http.Request), rules []EngineRule) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/admin/siem/test", Auth: auth, Engine: rules})
			}
			admin := w.As("admin")
			for _, p := range []string{"admin", "obs_flag", "admin_group", "editor", "inactive"} {
				get("never configured · "+p, w.As(p))
			}
			get("never configured · nobody", nil)
			get("never configured · a token", Bearer("tab_siem_admin_write_000000"))
			s = append(s,
				Step{Name: "a broken JSON from nobody", Method: "PUT", Path: "/admin/siem", Raw: []byte(`{"url": `), ContentType: "application/json"},
				Step{Name: "no body", Method: "PUT", Path: "/admin/siem", Auth: admin},
				Step{Name: "a form", Method: "PUT", Path: "/admin/siem", Auth: admin, Raw: []byte("url=x"), ContentType: "application/x-www-form-urlencoded"},
			)
			put("nobody", nil, map[string]any{})
			put("editor", w.As("editor"), map[string]any{})
			put("an observer", w.As("obs_flag"), map[string]any{"enabled": false})
			put("a token", Bearer("tab_siem_admin_write_000000"), map[string]any{})
			put("a token, in Italian", func(r *http.Request) {
				Bearer("tab_siem_admin_write_000000")(r)
				r.Header.Set("Accept-Language", "it")
			}, map[string]any{"url": 5})
			put("bad types", admin, map[string]any{"url": 5, "enabled": "maybe", "verify_tls": []int{}, "token": nil})
			put("too long", admin, map[string]any{"url": "https://x.test/" + string(make([]byte, 0)) + repeat("a", 2000),
				"index": repeat("i", 121), "host": repeat("h", 256), "token": repeat("t", 201)})
			put("nothing", admin, map[string]any{})
			put("verify_tls as it is: nothing saved", admin, map[string]any{"verify_tls": true})
			put("on without address nor token", admin, map[string]any{"enabled": true})
			for _, u := range []string{"ftp://x.test", "https://[::1", "http://host:99999/", "http://a..b/", "https://",
				"http://host/x\x00", "https://x.test:443/a b", "  "} {
				put("the address "+u, admin, map[string]any{"url": u})
			}
			put("an address only Python judges (IDNA)", admin, map[string]any{"url": "http://hé.test/hec"})
			get("an address only Python reads · observer", w.As("obs_flag"))
			put("address, token, index", admin, map[string]any{"url": " http://siem.test:8000 ", "token": " s3cr3t ",
				"index": " main ", "source": "", "sourcetype": " tab ", "host": "  "})
			get("configured · admin", admin)
			get("configured · observer", w.As("obs_flag"))
			put("the same again: only the token changes", admin, map[string]any{"url": "http://siem.test:8000", "token": "s3cr3t", "index": "main"})
			try("try · observer", w.As("obs_flag"), nil)
			try("try · nobody", nil, nil)
			try("try · a token", Bearer("tab_siem_admin_write_000000"), nil)
			try("try · Splunk answers {}", admin, nil)
			try("try · Splunk says Success", admin, []EngineRule{{Path: hec, Body: map[string]any{"text": "Success", "code": 0}}})
			try("try · a wrong token", admin, []EngineRule{{Path: hec, Status: 403, Body: map[string]any{"text": "Invalid token", "code": 4}}})
			try("try · 200 with a code", admin, []EngineRule{{Path: hec, Body: map[string]any{"text": "Data channel is missing", "code": 10}}})
			try("try · 200, a list", admin, []EngineRule{{Path: hec, Body: []int{1, 2}}})
			try("try · 503, not JSON", admin, []EngineRule{{Path: hec, Status: 503, Body: "busy"}})
			try("try · 400 with a text that is a number", admin, []EngineRule{{Path: hec, Status: 400, Body: map[string]any{"text": 7}}})
			try("try · 500 with an empty text", admin, []EngineRule{{Path: hec, Status: 500, Body: map[string]any{"text": "", "code": 8}}})
			put("on", admin, map[string]any{"enabled": true})
			get("on · admin: the backlog", admin)
			get("on · observer", w.As("obs_flag"))
			put("index and host changed, the TLS check off", admin, map[string]any{"index": "", "host": "gw-1", "verify_tls": false, "source": "src"})
			try("try · with host and source", admin, nil)
			put("the collector's path", admin, map[string]any{"url": "http://siem.test:8000/services/collector"})
			try("try · the collector's path", admin, nil)
			put("a proxy's path with a query", admin, map[string]any{"url": "http://siem.test:8000/proxy/hec?x=1&y=a%20b"})
			try("try · a proxy's path", admin, nil)
			put("a closed port", admin, map[string]any{"url": "http://siem.test:9"})
			try("try · a closed port", admin, nil)
			put("loopback", admin, map[string]any{"url": "http://127.0.0.1:8000"})
			try("try · loopback", admin, nil)
			put("IPv6 loopback", admin, map[string]any{"url": "http://[::1]:8000/"})
			try("try · IPv6 loopback", admin, nil)
			put("the metadata address", admin, map[string]any{"url": "http://169.254.169.254/"})
			try("try · the metadata address", admin, nil)
			put("a name that does not resolve", admin, map[string]any{"url": "http://nothing-here.siem.test:8000/"})
			try("try · a name that does not resolve", admin, nil)
			put("removing the token while on", admin, map[string]any{"token": ""})
			put("off and no token", admin, map[string]any{"enabled": false, "token": "  "})
			try("try · no token", admin, nil)
			put("off and on again", admin, map[string]any{"enabled": true, "token": "again", "url": "http://siem.test:8000"})
			get("again · admin", admin)
			return s
		},
	})
}

func repeat(c string, n int) string {
	out := make([]byte, 0, n*len(c))
	for i := 0; i < n; i++ {
		out = append(out, c...)
	}
	return string(out)
}
