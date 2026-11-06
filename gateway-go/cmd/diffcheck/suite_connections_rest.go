package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

// routes/connections.py: creating, editing and testing connections, and what the engine
// says of them (files, tables, columns, Google Sheets, the API call tried, the placeholders).

// testCA is a self-signed certificate authority made for this comparison (public, no secret).
const testCA = `-----BEGIN CERTIFICATE-----
MIIBjTCCATOgAwIBAgIUJ4bHekTAr7OaWUOLAbekNwaTu7swCgYIKoZIzj0EAwIw
HDEaMBgGA1UEAwwRZGlmZmNoZWNrIHRlc3QgQ0EwHhcNMjYxMTA0MDY1MzU4WhcN
MzYxMTAxMDY1MzU4WjAcMRowGAYDVQQDDBFkaWZmY2hlY2sgdGVzdCBDQTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABMkatwNz/fe6P/XLlohz0OUkuBi7wVjHeZ8x
W9WJjcSq7Rlz+m+0voMjDAY/cXZRr9IRzpPCSy4YmTDimw4ZZuqjUzBRMB0GA1Ud
DgQWBBSdvLoERmVWZfYchfDTRDv+DcPNgDAfBgNVHSMEGDAWgBSdvLoERmVWZfYc
hfDTRDv+DcPNgDAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0gAMEUCIQCT
DlqN6zDokEBhJRgqx/ll0gl39x4YCUOvBV89lUIlRgIgE8QvfFDi3K0RxDTvkbNP
cQw+GvnTI1ZBvkxOUokwB7s=
-----END CERTIFICATE-----
`

// fakeKey looks like a private key to the gateway's check, and is none.
const fakeKey = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-key\n-----END OPENSSH " + "PRIVATE KEY-----"

// gsheetsKey is a service account's JSON key as the gateway checks it (the key is no key).
func gsheetsKey(email, project string) string {
	return fmt.Sprintf(`{"type": "service_account", "client_email": %q, "project_id": %q, "private_key": %q}`,
		email, project, fakeKey)
}

// seal is encrypt_secret with the stacks' key, the same text in both databases.
func seal(plain string) string {
	if fernet == nil {
		return plain
	}
	t, err := fernet.Encrypt(plain)
	if err != nil {
		fail("seal: %v", err)
	}
	return t
}

func init() {
	register(Suite{
		Module: "connections_rest",
		Seed: func(w *World) {
			seedData(w)
			conn := func(key, name, project, dbType, host string, port *int, username, password, database, schema, extra, pk string) {
				Data["cr:"+key] = w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
					password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
					VALUES ($1, 'd', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, '', '', '2026-01-06', '2026-01-06') RETURNING id`,
					name, Data[project], w.People["admin"], dbType, host, port, username, password, database, schema, extra, pk)
			}
			blob := seal(`{"private_key": "` + strings.ReplaceAll(fakeKey, "\n", `\n`) + `\n", "passphrase": "old"}`)
			conn("pg", "cr-pg", "connect", "postgresql", "pg.example.test", ptr(5432), "u", seal("pw"), "sales", "public", `{"tls": "verify"}`, "")
			conn("ch", "cr-ch", "connect", "clickhouse", "ch.example.test", nil, "u", "", "w", "", `{}`, "")
			conn("sql", "cr-sqlserver", "connect", "sqlserver", "ms.example.test", nil, "u", seal("pw"), "w", "", `{}`, "")
			conn("sftp", "cr-sftp", "connect", "sftp", "sftp.example.test", nil, "bob", seal("pw"), "", "", `{"auth": "password"}`, "")
			conn("sftpkey", "cr-sftp-key", "connect", "sftp", "sftp2.example.test", ptr(2222), "bob", seal("pw"), "", "", `{"auth": "key"}`, blob)
			w.Exec(`UPDATE connections SET host_key_type = 'ssh-ed25519', host_key_fingerprint = 'SHA256:old' WHERE id = $1`, Data["cr:sftpkey"])
			conn("api", "cr-api", "connect", "api", "https://api.example.test/v1", nil, "", seal("k"), "", "",
				`{"auth": "api_key", "key_name": "X-Key", "key_in": "header", "headers": {" A ": 1, "B": "b"}, "test_path": 5}`, "")
			conn("oauth", "cr-oauth", "connect", "api", "https://api.example.test", nil, "cid", seal("s"), "", "",
				`{"auth": "oauth2", "token_url": "https://auth.example.test/token", "client_auth": "basic"}`, "")
			conn("apinone", "cr-api-none", "connect", "api", "http://open.example.test:8080", nil, "", "", "", "", `not json`, "")
			conn("gs", "cr-gsheets", "connect", "gsheets", "sa@p.iam.test", nil, "", seal(gsheetsKey("sa@p.iam.test", "p")), "p", "", "{}", "")
			conn("sp", "cr-sharepoint", "connect", "sharepoint", " https://c.sharepoint.test/sites/x ", nil, " cid ", seal("s"), " tenant ", "",
				`{"library": 7}`, "")
			conn("s3", "cr-s3", "connect", "s3", "", nil, "AK", seal("SK"), "", "eu-west-1", "{}", "")
			conn("s3b", "cr-s3-bucket", "connect", "s3", "http://minio.test:9000", nil, "AK", seal("SK"), "lake", "", "{}", "")
			conn("smtp", "cr-smtp", "connect", "smtp", "mail.example.test", ptr(0), "u", seal("pw"), "", "",
				`{"from_address": "noreply@x.test", "tls": "ssl", "allowed_domains": "X.test, @y.test"}`, "")
			conn("taken", "cr-taken", "connect", "postgresql", "h", nil, "u", "", "", "", "{}", "")
			conn("rootpg", "cr-root-pg", "root", "postgresql", "h", nil, "u", "", "", "", "{}", "")
		},
		Engine: []EngineRule{
			{Method: "POST", Path: "^/sftp/inspect$", Body: map[string]any{"ok": true, "host_key": map[string]any{"type": "ssh-ed25519",
				"fingerprint": "SHA256:new"}, "files": []any{"a.csv"}}},
			{Method: "POST", Path: "/inspect$", Body: map[string]any{"ok": true, "tables": []any{"a", "b"}, "n": 1.5}},
			{Method: "POST", Path: "^/placeholders/preview$", Body: map[string]any{"fields": map[string]any{"a": map[string]any{"value": "x"}}}},
		},
		Steps: connectionsRestSteps,
	})
}

func connectionsRestSteps(w *World) []Step {
	var s []Step
	id := func(k string) string { return fmt.Sprint(Data["cr:"+k]) }
	conn := func(k string) string { return "/connections/" + id(k) }
	folder := func(k string) string { return "/projects/" + fmt.Sprint(Data[k]) }
	add := func(person, name, method, path string, body any, rules ...EngineRule) {
		st := Step{Name: name + " · " + person, Method: method, Path: path, JSON: body, Auth: w.As(person)}
		if len(rules) > 0 {
			st.Engine = rules
		}
		s = append(s, st)
	}
	raw := func(person, name, method, path, text string) {
		s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, Raw: []byte(text), ContentType: "application/json",
			Auth: w.As(person)})
	}
	engine := func(path string, status int, body any) EngineRule {
		return EngineRule{Method: "POST", Path: path, Status: status, Body: body}
	}
	text := func(path string, status int, t string) EngineRule {
		return EngineRule{Method: "POST", Path: path, Status: status, Text: t}
	}

	// ── who may: every person on each route ──
	s = append(s, forEveryone(w, "tables", "GET", conn("pg")+"/tables", nil)...)
	s = append(s, forEveryone(w, "test", "POST", conn("pg")+"/test", nil)...)
	s = append(s, forEveryone(w, "sftp files", "POST", conn("sftp")+"/sftp/files", map[string]any{"pattern": "*.csv"})...)
	s = append(s, forEveryone(w, "placeholders", "POST", "/placeholders/preview", map[string]any{"templates": map[string]any{"a": "{today}"}})...)
	s = append(s, forEveryone(w, "draft test", "POST", folder("connect")+"/connections/test",
		map[string]any{"name": "x", "db_type": "postgresql", "host": "h"})...)
	s = append(s, forEveryone(w, "create", "POST", folder("connect")+"/connections",
		map[string]any{"name": "cr-everyone", "db_type": "postgresql", "host": "h"})...)
	s = append(s, forEveryone(w, "edit", "PATCH", conn("ch"), map[string]any{"description": "everyone"})...)
	s = append(s, forEveryone(w, "gsheets spreadsheets", "POST", conn("gs")+"/gsheets/spreadsheets", nil)...)

	// ── the inspections ──
	for _, k := range []string{"pg", "ch", "sql", "sftp", "api", "gs", "sp", "s3", "smtp", "apinone"} {
		add("editor", "tables of "+k, "GET", conn(k)+"/tables", nil)
		add("editor", "columns of "+k, "GET", conn(k)+"/columns?table=public.a", nil)
		add("editor", "test "+k, "POST", conn(k)+"/test", nil)
	}
	add("editor", "tables where EDIT only", "GET", conn("rootpg")+"/tables", nil)
	add("admin", "tables of an unknown one", "GET", "/connections/999999/tables", nil)
	add("admin", "tables of an odd id", "GET", "/connections/x/tables", nil)
	add("admin", "columns without a table", "GET", conn("pg")+"/columns", nil)
	add("admin", "columns, an empty table", "GET", conn("pg")+"/columns?table=", nil)
	add("admin", "columns, a long table", "GET", conn("pg")+"/columns?table="+strings.Repeat("t", 501), nil)
	add("admin", "columns, odd id and no table", "GET", "/connections/x/columns", nil)
	add("admin", "columns twice", "GET", conn("pg")+"/columns?table=a&table=b", nil)
	langs := []string{"it", "de-CH,de;q=0.9", "fr", "es", "pt", "", "xx,es"}
	failures := []struct {
		name string
		rule EngineRule
	}{
		{"a 400 with a text detail", engine(".", 400, map[string]any{"detail": "bad host"})},
		{"a 500 not JSON", text(".", 500, "<html>boom</html>")},
		{"a 503 list", engine(".", 503, []any{1, 2})},
		{"a 422 detail list", engine(".", 422, map[string]any{"detail": []any{map[string]any{"a": 1}}})},
		{"a detail null", engine(".", 409, map[string]any{"detail": nil})},
		{"no detail", engine(".", 404, map[string]any{"error": "x"})},
		{"a long detail", engine(".", 400, map[string]any{"detail": strings.Repeat("é", 700)})},
		{"a dict detail", engine(".", 400, map[string]any{"detail": map[string]any{"code": "other", "x": 1.0, "y": true, "z": nil}})},
		{"the driver missing", engine(".", 400, map[string]any{"detail": map[string]any{"code": "driver_not_installed", "db_type": "sqlserver"}})},
		{"another driver missing", engine(".", 400, map[string]any{"detail": map[string]any{"code": "driver_not_installed", "db_type": "oracle"}})},
		{"a driver missing, no type", engine(".", 400, map[string]any{"detail": map[string]any{"code": "driver_not_installed"}})},
		{"host key changed", engine(".", 409, map[string]any{"detail": map[string]any{"code": "host_key_changed", "host": "h:22",
			"expected": "SHA256:a", "offered": "SHA256:b"}})},
		{"host key unknown, no host", engine(".", 409, map[string]any{"detail": map[string]any{"code": "host_key_unknown"}})},
		{"200 not JSON", text(".", 200, "nope")},
		{"200 a list", engine(".", 200, []any{1, "a"})},
		{"201 NaN", text(".", 201, `{"a": NaN}`)},
	}
	for _, f := range failures {
		add("editor", "tables: "+f.name, "GET", conn("pg")+"/tables", nil, f.rule)
		add("editor", "sftp test: "+f.name, "POST", conn("sftp")+"/test", nil, f.rule)
		add("editor", "sharepoint files: "+f.name, "POST", conn("sp")+"/sharepoint/files", map[string]any{"path": "/a"}, f.rule)
	}
	for _, l := range langs {
		for _, f := range failures[8:13] {
			s = append(s, Step{Name: "columns in «" + l + "»: " + f.name, Method: "GET", Path: conn("sql") + "/columns?table=t",
				Auth: w.As("admin"), Header: map[string]string{"Accept-Language": l}, Engine: []EngineRule{f.rule}})
		}
	}

	// the SFTP test: trust on first use
	sftpAnswer := func(body any) EngineRule { return engine("^/sftp/inspect$", 200, body) }
	shown := map[string]any{"ok": false, "needs_acceptance": true, "host_key": map[string]any{"type": "ssh-rsa", "fingerprint": "SHA256:shown"},
		"previous": map[string]any{"fingerprint": "SHA256:old"}}
	add("editor", "sftp test shows a key", "POST", conn("sftp")+"/test", nil, sftpAnswer(shown))
	add("editor", "sftp test, accepted key not offered", "POST", conn("sftp")+"/test", map[string]any{"expected_fingerprint": " SHA256:x "},
		sftpAnswer(shown))
	s = append(s, Step{Name: "sftp test, not the tested key, in Italian", Method: "POST", Path: conn("sftpkey") + "/test",
		JSON: map[string]any{"expected_fingerprint": "SHA256:x"}, Auth: w.As("editor"), Header: map[string]string{"Accept-Language": "it"},
		Engine: []EngineRule{sftpAnswer(map[string]any{"needs_acceptance": 1, "host_key": map[string]any{}})}})
	add("editor", "sftp test, not the tested key, no host key", "POST", conn("sftp")+"/test", map[string]any{"expected_fingerprint": "f"},
		sftpAnswer(map[string]any{"needs_acceptance": true}))
	add("editor", "sftp test, a host key not a dict", "POST", conn("sftp")+"/test", map[string]any{"expected_fingerprint": "f"},
		sftpAnswer(map[string]any{"needs_acceptance": true, "host_key": "k"}))
	add("editor", "sftp test fails", "POST", conn("sftp")+"/test", nil, sftpAnswer(map[string]any{"ok": false, "error": "auth"}))
	add("editor", "sftp test, ok without a key", "POST", conn("sftp")+"/test", nil, sftpAnswer(map[string]any{"ok": true}))
	add("editor", "sftp test, ok with a key not a dict", "POST", conn("sftp")+"/test", nil, sftpAnswer(map[string]any{"ok": true, "host_key": []any{1}}))
	add("editor", "sftp test accepts", "POST", conn("sftp")+"/test", map[string]any{"expected_fingerprint": "SHA256:new", "expected_type": "ssh-ed25519"})
	add("editor", "sftp test, the same key again", "POST", conn("sftp")+"/test", nil)
	add("editor", "sftp test, a changed key", "POST", conn("sftpkey")+"/test", nil, sftpAnswer(map[string]any{"ok": 1,
		"host_key": map[string]any{"type": 5, "fingerprint": 7.0}, "changed": "x", "accepted": 0}))
	add("editor", "sftp test, a list", "POST", conn("sftp")+"/test", nil, sftpAnswer([]any{1}))
	add("editor", "sftp test, null body", "POST", conn("sftp")+"/test", nil)
	raw("editor", "sftp test, JSON null", "POST", conn("sftp")+"/test", "null")
	raw("editor", "sftp test, bad JSON", "POST", conn("sftp")+"/test", "{")
	raw("nobody", "sftp test, bad JSON", "POST", conn("sftp")+"/test", "{")
	add("editor", "sftp test, a wrong type", "POST", conn("sftp")+"/test", map[string]any{"expected_fingerprint": 5})
	add("editor", "sftp test, a list body", "POST", conn("sftp")+"/test", []any{1})
	// a database tested with the form's encryption
	for _, extra := range []any{`{"tls": "trust"}`, `{"tls": "verify", "ca_cert": "` + strings.ReplaceAll(testCA, "\n", `\n`) + `"}`,
		`{"tls": "none", "ca_cert": " "}`, `{"tls": "bogus"}`, `{"ca_cert": 5}`, `{"tls": "trust", "ca_cert": "x"}`, `not json`, `[1]`, ``, `  `,
		`{"tls": "verify", "ca_cert": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"}`,
		`{"tls": "verify", "ca_cert": "-----BEGIN RSA PRIVATE KEY-----"}`, `{"tls": "verify", "ca_cert": "hello"}`, nil} {
		add("editor", fmt.Sprintf("test pg with extra %.30q", extra), "POST", conn("pg")+"/test", map[string]any{"extra": extra})
		add("editor", fmt.Sprintf("test sqlserver with extra %.30q", extra), "POST", conn("sql")+"/test", map[string]any{"extra": extra})
	}
	add("editor", "test smtp with extra (not a database)", "POST", conn("smtp")+"/test", map[string]any{"extra": "not json"})

	// files, tabs, previews, the API call
	for _, b := range []map[string]any{
		{"pattern": "*.csv"}, {"folder": "in/{today}", "pattern": "a.csv"}, {"folder": "in", "pattern": "x_}.csv"},
		{"pattern": "*.csv", "timezone": " Europe/Rome ", "variables": []any{map[string]any{"name": "v", "values": []any{"a"}}},
			"last_refresh_initial": " 2026 ", "mode": "append", "keys": []any{" id ", " "}},
		{"pattern": "*.csv", "timezone": "UTC", "keys": []any{" "}},
		{"pattern": ""}, {"folder": "../x", "pattern": "a"}, {"pattern": "a", "variables": []any{map[string]any{"name": "r", "type": "range"}}},
		{"pattern": "a", "mode": "other"}, {},
	} {
		add("editor", fmt.Sprintf("sftp files %v", b), "POST", conn("sftp")+"/sftp/files", b)
		add("editor", fmt.Sprintf("s3 files %v", b), "POST", conn("s3b")+"/s3/files", b)
	}
	add("editor", "sftp files on a database", "POST", conn("pg")+"/sftp/files", map[string]any{"pattern": "a"})
	add("editor", "sftp files on a database, bad body", "POST", conn("pg")+"/sftp/files", map[string]any{})
	add("editor", "s3 files without a bucket", "POST", conn("s3")+"/s3/files", map[string]any{"pattern": "a"})
	add("editor", "s3 files with a bucket", "POST", conn("s3")+"/s3/files", map[string]any{"bucket": " b ", "folder": " /a/b/ ", "pattern": " *.csv "})
	add("editor", "s3 files on sftp", "POST", conn("sftp")+"/s3/files", map[string]any{"pattern": "a"})
	for _, b := range []map[string]any{{"path": "/a/*.xlsx"}, {"path": "/a/{today}.csv", "format": "csv", "timezone": "Europe/Rome"},
		{"path": ""}, {"path": "/a", "format": "pdf"}, {"path": "/a", "format": "auto", "variables": []any{map[string]any{"name": "x", "values": []any{}}}}} {
		add("editor", fmt.Sprintf("sharepoint files %v", b), "POST", conn("sp")+"/sharepoint/files", b)
	}
	add("editor", "sharepoint files on s3", "POST", conn("s3")+"/sharepoint/files", map[string]any{"path": "/a"})
	add("editor", "spreadsheets", "POST", conn("gs")+"/gsheets/spreadsheets", nil)
	add("editor", "spreadsheets of a database", "POST", conn("pg")+"/gsheets/spreadsheets", nil)
	add("editor", "tabs", "POST", conn("gs")+"/gsheets/tabs", map[string]any{"spreadsheet": "  https://docs.google.test/x  "})
	add("editor", "tabs, empty", "POST", conn("gs")+"/gsheets/tabs", map[string]any{"spreadsheet": ""})
	add("editor", "tabs of sftp", "POST", conn("sftp")+"/gsheets/tabs", map[string]any{"spreadsheet": "x"})
	for _, b := range []map[string]any{{"spreadsheet_id": "abcdefghijklmnopqrstuvwxyz"}, {"spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet_id": 0,
		"sheet": "S", "range": "a1:c9"}, {"spreadsheet_id": "short"}, {"spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "range": "A1"},
		{"spreadsheet_id": "abcdefghijklmnopqrstuvwxyz", "sheet_id": -1}} {
		add("editor", fmt.Sprintf("gsheets preview %v", b), "POST", conn("gs")+"/gsheets/preview", b)
	}
	for _, b := range []map[string]any{{}, {"method": "POST", "path": "/x", "query": []any{map[string]any{"name": "a", "value": "{today}"}},
		"limits": map[string]any{"max_pages": 2}, "response": map[string]any{"format": "csv"}},
		{"limits": map[string]any{"min_interval_seconds": 1}}, {"limits": map[string]any{"min_interval_seconds": 0}},
		{"method": "PUT"}, {"pagination": map[string]any{"limit": 0}}, {"variables": []any{map[string]any{"name": "v", "values": []any{"1"}}}}} {
		add("editor", fmt.Sprintf("api try %v", b), "POST", conn("api")+"/api/try", b)
	}
	add("editor", "api try on sftp", "POST", conn("sftp")+"/api/try", map[string]any{})
	add("obs_flag", "api try as an observer", "POST", conn("api")+"/api/try", map[string]any{})

	// the placeholders' preview
	for _, b := range []map[string]any{{"templates": map[string]any{"a": "{today}"}, "pushmail": true},
		{"templates": map[string]any{}, "kind": "output", "targets": map[string]any{"a": "file"}, "flow": "f"},
		{"templates": map[string]any{"a": strings.Repeat("x", 4001)}}, {"templates": map[string]any{strings.Repeat("k", 65): "x"}},
		{"templates": map[string]any{"a": 1}}, {"kind": "x"}, {"templates": map[string]any{"a": "b"}, "targets": map[string]any{"a": "dir"}},
		{"templates": map[string]any{"a": "b"}, "variables": []any{map[string]any{"name": "d", "type": "range", "start": "a", "end": "b", "step": "1d"}}}} {
		add("editor", fmt.Sprintf("placeholders %v", b), "POST", "/placeholders/preview", b)
		add("admin", fmt.Sprintf("placeholders %v", b), "POST", "/placeholders/preview", b)
	}
	for _, f := range []EngineRule{engine(".", 400, map[string]any{"detail": "x"}), text(".", 502, "gateway"), text(".", 200, "x"), engine(".", 200, []any{})} {
		add("editor", fmt.Sprintf("placeholders, the engine answers %d", f.Status), "POST", "/placeholders/preview",
			map[string]any{"templates": map[string]any{"a": "{x}"}}, f)
	}

	// ── a draft tested before saving ──
	draft := folder("connect") + "/connections/test"
	base := func(dbType string, more map[string]any) map[string]any {
		b := map[string]any{"name": "n", "db_type": dbType, "host": "h.example.test"}
		for k, v := range more {
			b[k] = v
		}
		return b
	}
	for _, b := range []map[string]any{
		base("postgresql", map[string]any{"port": 5432, "password": "p", "extra": `{"tls": "verify", "ca_cert": " x "}`}),
		base("postgresql", map[string]any{"extra": `{"tls": "trust"}`, "port": 99999999999}),
		base("postgresql", map[string]any{"port": json.Number("123456789012345678901234567890")}),
		base("mysql", map[string]any{"extra": `not json`}), base("oracle", map[string]any{"extra": `{"tls": "verify"}`}),
		base("sqlserver", map[string]any{}), base("trino", map[string]any{"extra": `[]`}),
		base("mongodb", nil), base("s3", map[string]any{"username": "AK", "password": "SK", "database": "b", "db_schema": "r"}),
		base("smtp", map[string]any{"port": 25, "extra": `{"tls": "x"}`}), base("smtp", map[string]any{"port": 0, "extra": `{"from_name": "N"}`}),
		base("smtp", map[string]any{"extra": `{"allowed_domains": [1]}`}), base("smtp", map[string]any{"extra": `{"from_address": 5, "tls": "ssl"}`}),
		base("sharepoint", map[string]any{"host": "http://x"}), base("sharepoint", map[string]any{"host": "HTTPS://x", "username": "c"}),
		base("sharepoint", map[string]any{"host": " https://x ", "username": "c", "database": "t", "password": "s", "extra": `{"library": " L "}`}),
		base("sftp", map[string]any{"username": "u", "password": "p"}),
		base("sftp", map[string]any{"username": "u", "password": "p", "port": 2222, "expected_fingerprint": " SHA256:new ", "expected_type": " t "}),
		base("sftp", map[string]any{"username": "u", "extra": `{"auth": "key"}`, "private_key": fakeKey, "private_key_passphrase": "pp"}),
		base("sftp", map[string]any{"username": "u", "extra": `{"auth": "key"}`, "private_key": "not a key"}),
		base("sftp", map[string]any{"host": "sftp://h", "username": "u", "password": "p"}),
		base("sftp", map[string]any{"username": "u", "password": "p", "port": 0}),
		base("sftp", map[string]any{"username": "u", "password": "p", "port": 70000}),
		base("sftp", map[string]any{"username": " ", "password": "p"}), base("sftp", map[string]any{"username": "u"}),
		base("sftp", map[string]any{"username": "u", "extra": `{"auth": "key"}`}),
		base("sftp", map[string]any{"username": "u", "password": "p", "extra": `{"auth": "x"}`}),
		base("api", map[string]any{"host": "https://api.test/v1"}),
		base("api", map[string]any{"host": "ftp://api.test"}), base("api", map[string]any{"host": "https://u:p@api.test"}),
		base("api", map[string]any{"host": "https://api.test/?a=1"}), base("api", map[string]any{"host": "https://api.test/#f"}),
		base("api", map[string]any{"host": "https://[::1"}), base("api", map[string]any{"host": "https://bücher.test"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "bearer"}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "bearer"}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "basic"}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "oauth2"}`, "password": "t", "username": "c"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "oauth2", "token_url": "https://t.test/x?y"}`, "password": "t", "username": "c"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "oauth2", "token_url": "https://t.test", "client_auth": "x"}`,
			"password": "t", "username": "c"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "oauth2", "token_url": "https://t.test", "client_auth": "basic", "scope": " s "}`,
			"password": "t", "username": " c "}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "api_key"}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "api_key", "key_name": "bad name"}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "api_key", "key_name": "bad name", "key_in": "query"}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "api_key", "key_name": "k", "key_in": null}`, "password": "t"}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"auth": "x"}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"headers": {"a b": "x"}}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"headers": {"": "x", "  ": "y"}}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"headers": {"a": "x\ny"}}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"headers": {"a": 1}}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"headers": [1]}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"test_path": "//x"}`}),
		base("api", map[string]any{"host": "https://api.test", "extra": `{"test_path": "/s"}`}),
		base("gsheets", map[string]any{}), base("gsheets", map[string]any{"password": "not json"}),
		base("gsheets", map[string]any{"password": `{"installed": {}}`}), base("gsheets", map[string]any{"password": `[1]`}),
		base("gsheets", map[string]any{"password": `{"type": "service_account"}`}),
		base("gsheets", map[string]any{"password": strings.Repeat(" ", 3) + gsheetsKey(" sa@x.test ", " proj ")}),
		base("gsheets", map[string]any{"password": strings.Repeat("x", 20001)}),
		{"name": "n", "db_type": "postgresql"}, {"name": "n", "db_type": "postgresql", "host": "h", "port": "x"},
		{"name": "n", "db_type": "postgresql", "host": "h", "private_key": strings.Repeat("k", 32001)},
	} {
		add("editor", fmt.Sprintf("draft %.160v", b), "POST", draft, b)
	}
	add("editor", "draft, sftp key not offered", "POST", draft, base("sftp", map[string]any{"username": "u", "password": "p",
		"expected_fingerprint": "SHA256:x"}), sftpAnswer(shown))
	add("editor", "draft in a folder without CONNECT", "POST", folder("root")+"/connections/test", base("postgresql", nil))
	add("editor", "draft in an unknown folder", "POST", "/projects/999999/connections/test", base("postgresql", nil))
	add("editor", "draft, an odd folder and no body", "POST", "/projects/x/connections/test", nil)
	add("editor", "draft, the driver missing", "POST", draft, base("sqlserver", nil),
		engine(".", 400, map[string]any{"detail": map[string]any{"code": "driver_not_installed", "db_type": "sqlserver"}}))

	// ── create ──
	create := folder("connect") + "/connections"
	named := func(name, dbType string, more map[string]any) map[string]any {
		b := base(dbType, more)
		b["name"] = name
		return b
	}
	for _, b := range []map[string]any{
		named(" cr-new-pg ", "postgresql", map[string]any{"port": 5432, "password": "p", "description": "d", "database": "db", "db_schema": "s",
			"extra": `  {"tls": "verify", "ca_cert": "` + strings.ReplaceAll(testCA, "\n", `\n`) + `"}  `}),
		named("cr-new-pg", "postgresql", nil), named("cr-taken", "mysql", nil), named("  ", "mysql", nil), named("cr-x", "mongodb", nil),
		named("cr-new-ch", "clickhouse", map[string]any{"extra": ""}), named("cr-big-port", "mysql", map[string]any{"port": 3000000000}),
		named("cr-new-s3", "s3", map[string]any{"host": "", "username": "AK", "password": "SK"}),
		named("cr-new-smtp", "smtp", map[string]any{"extra": `{"allowed_domains": ["A.test", " "], "tls": "starttls"}`}),
		named("cr-bad-smtp", "smtp", map[string]any{"extra": `{"allowed_domains": 5, "tls": 1, "from_name": []}`}),
		named("cr-new-sp", "sharepoint", map[string]any{"host": "https://c.sharepoint.test/sites/a", "username": "c", "database": "t"}),
		named("cr-bad-sp", "sharepoint", map[string]any{"host": "https://c.sharepoint.test", "username": "c"}),
		named("cr-new-sftp", "sftp", map[string]any{"username": "u", "password": "p", "port": 22}),
		named("cr-new-sftp-key", "sftp", map[string]any{"username": "u", "extra": `{"auth": "key"}`, "private_key": "\n" + fakeKey + "\n",
			"private_key_passphrase": "é"}),
		named("cr-pk-not-sftp", "postgresql", map[string]any{"private_key": fakeKey}),
		named("cr-new-api", "api", map[string]any{"host": "https://api.test", "extra": `{"auth": "bearer"}`, "password": "t"}),
		named("cr-bad-api", "api", map[string]any{"host": "https://api.test", "extra": `{"auth": "bearer"}`}),
		named("cr-api-badurl", "api", map[string]any{"host": "https://[::1"}),
		named("cr-new-gs", "gsheets", map[string]any{"password": gsheetsKey("sa@x.test", "proj"), "port": 1, "username": "x", "extra": `{"a": 1}`}),
		named("cr-bad-gs-extra", "gsheets", map[string]any{"password": gsheetsKey("sa@x.test", "proj"), "extra": `nope`}),
		named("cr-unicode-é", "trino", map[string]any{"host": "ü.test"}),
	} {
		add("editor", fmt.Sprintf("create %.160v", b), "POST", create, b)
	}
	add("editor", "create where EDIT only", "POST", folder("root")+"/connections", named("cr-root", "postgresql", nil))
	add("admin", "create where EDIT only, as admin", "POST", folder("root")+"/connections", named("cr-root-admin", "postgresql", nil))
	add("editor", "create in an unknown folder", "POST", "/projects/999999/connections", named("cr-y", "postgresql", nil))
	add("editor", "create without a body", "POST", create, nil)
	add("editor", "create without a name", "POST", create, map[string]any{"db_type": "x", "host": "h"})
	s = append(s, Step{Name: "create with the write token", Method: "POST", Path: "/projects/" + fmt.Sprint(Data["root"]) + "/connections",
		JSON: named("cr-token", "postgresql", nil), Auth: Bearer("tab_data_admin_write_000000")})

	// ── edit ──
	edit := func(k, name string, body map[string]any) { add("editor", "edit "+k+": "+name, "PATCH", conn(k), body) }
	edit("ch", "rename", map[string]any{"name": " cr-ch-renamed ", "description": "new"})
	edit("ch", "a taken name", map[string]any{"name": "cr-taken"})
	edit("ch", "an empty name", map[string]any{"name": " "})
	edit("ch", "the same name", map[string]any{"name": "cr-ch-renamed"})
	edit("ch", "move where EDIT only", map[string]any{"project_id": Data["root"]})
	edit("ch", "move to an unknown folder", map[string]any{"project_id": 999999})
	edit("ch", "move to a huge id", map[string]any{"project_id": 99999999999})
	edit("ch", "move to its own folder", map[string]any{"project_id": Data["connect"]})
	edit("ch", "a private key", map[string]any{"private_key": fakeKey})
	edit("ch", "a passphrase", map[string]any{"private_key_passphrase": "x"})
	edit("ch", "an empty passphrase", map[string]any{"private_key_passphrase": ""})
	edit("ch", "the encryption", map[string]any{"extra": `{"tls": "trust"}`, "port": 8443})
	edit("ch", "bad encryption", map[string]any{"extra": `{"tls": "x"}`, "host": "other"})
	edit("ch", "a big port", map[string]any{"port": 3000000000})
	edit("ch", "a password, then none", map[string]any{"password": "p"})
	edit("ch", "the password removed", map[string]any{"password": ""})
	edit("pg", "nothing", map[string]any{})
	edit("pg", "a bad body", map[string]any{"port": "x", "name": 5})
	edit("sftp", "a new host without the password", map[string]any{"host": "other.test"})
	edit("sftp", "the same host", map[string]any{"host": "sftp.example.test", "port": nil})
	edit("sftp", "a new port with the password", map[string]any{"port": 2200, "password": "np"})
	edit("sftp", "a bad port", map[string]any{"port": 0, "password": "np"})
	edit("sftp", "a bad private key", map[string]any{"private_key": "x"})
	edit("sftpkey", "the passphrase alone", map[string]any{"private_key_passphrase": "new"})
	edit("sftpkey", "the key removed", map[string]any{"private_key": "", "private_key_passphrase": "z"})
	edit("sftpkey", "the key removed, then key auth checked", map[string]any{"username": "carl", "password": "p"})
	edit("sftpkey", "a new key with a new user", map[string]any{"username": "dan", "private_key": fakeKey, "extra": `{"auth": "key"}`})
	edit("sftpkey", "the passphrase of a broken blob", map[string]any{"private_key_passphrase": "x"})
	edit("api", "a new host without the secret", map[string]any{"host": "https://other.test"})
	edit("api", "the same origin, another path", map[string]any{"host": "HTTPS://API.example.test:443/v2"})
	edit("api", "a new host with the secret", map[string]any{"host": "https://other.test", "password": "k2"})
	edit("api", "no authentication: the secret dropped", map[string]any{"host": "https://third.test", "extra": `{"auth": "none"}`})
	edit("api", "a bad path", map[string]any{"extra": `{"test_path": "http://x"}`})
	edit("oauth", "a new token URL", map[string]any{"extra": `{"auth": "oauth2", "token_url": "https://auth2.example.test/token"}`})
	edit("oauth", "the same token URL", map[string]any{"extra": `{"auth": "oauth2", "token_url": " https://auth.example.test/token "}`})
	edit("oauth", "a bad host", map[string]any{"host": "https://[::1"})
	edit("apinone", "the port of a broken one", map[string]any{"host": "http://open.example.test:x"})
	edit("apinone", "fixed", map[string]any{"extra": `{"auth": "none"}`, "host": "http://open.example.test:80"})
	edit("gs", "a new key", map[string]any{"password": gsheetsKey("new@x.test", "np"), "host": "ignored", "port": 5, "extra": "bad"})
	edit("gs", "a bad key", map[string]any{"password": "x"})
	edit("gs", "only the name", map[string]any{"name": "cr-gs2", "host": "ignored", "database": "ignored"})
	edit("smtp", "bad options", map[string]any{"extra": `{"tls": "x", "from_address": 1}`})
	edit("smtp", "good options", map[string]any{"extra": `{"tls": "none", "allowed_domains": ""}`})
	add("editor", "edit where EDIT only", "PATCH", conn("rootpg"), map[string]any{"name": "x"})
	add("admin", "edit, an unknown one", "PATCH", "/connections/999999", map[string]any{"name": "x"})
	add("admin", "edit, an odd id and a bad body", "PATCH", "/connections/x", []any{})
	add("admin", "move to the root folder", "PATCH", conn("taken"), map[string]any{"project_id": Data["root"], "name": "cr-root-pg"})
	add("admin", "move to the root folder, another name", "PATCH", conn("taken"), map[string]any{"project_id": Data["root"]})
	return s
}
