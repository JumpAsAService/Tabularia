package main

import "fmt"

// routes/contracts.py: a datasource's data contract.
func init() {
	report := map[string]any{"outcome": "warning", "errors": 0, "warnings": "2", "rows": 10, "rules": []any{map[string]any{"id": "r1", "ok": 1.0}}}
	register(Suite{
		Module: "contracts",
		Seed:   seedContracts,
		Engine: []EngineRule{
			{Method: "POST", Path: "^/contracts/evaluate$", Body: report},
			{Method: "POST", Path: "^/contracts/profile$", Body: map[string]any{"proposal": map[string]any{"rules": []any{}, "z": 1, "a": 2.0}, "profile": []any{1}}},
		},
		Steps: func(w *World) []Step {
			ds := func(k string) string { return "/datasources/" + fmt.Sprint(Data["ds:"+k]) + "/contract" }
			s := forEveryone(w, "the contract", "GET", ds("root"), nil)
			s = append(s, forEveryone(w, "the history", "GET", ds("root")+"/history", nil)...)
			s = append(s, forEveryone(w, "check", "POST", ds("root")+"/check", nil)...)
			s = append(s, forEveryone(w, "propose", "POST", ds("root")+"/proposal", nil)...)
			as := func(person, name, method, path string, rules []EngineRule) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, Auth: w.As(person), Engine: rules})
			}
			for _, k := range []string{"ready", "half", "hidden", "child", "run", "broken", "nulls"} {
				as("writer", "the contract of "+k, "GET", ds(k), nil)
				as("admin", "the contract of "+k, "GET", ds(k), nil)
			}
			for _, id := range []string{"999999", "x", "99999999999"} {
				as("editor", "the contract of "+id, "GET", "/datasources/"+id+"/contract", nil)
			}
			for _, q := range []string{"?limit=0", "?limit=-5", "?limit=1", "?limit=500", "?limit=x", "?limit=99999999999999999999",
				"?limit=-99999999999999999999", "?with_report=true", "?with_report=yes", "?with_report=maybe", "?with_report=1&limit=2"} {
				as("editor", "the history "+q, "GET", ds("root")+"/history"+q, nil)
			}
			as("editor", "a bad id and a bad query", "GET", "/datasources/x/contract/history?limit=x&with_report=maybe", nil)
			s = append(s, Step{Name: "a bad query, nobody", Method: "GET", Path: ds("root") + "/history?limit=x"})
			as("writer", "a bad query, without VIEW? no: writer reads", "GET", ds("hidden")+"/history?limit=x", nil)
			as("editor", "the history of a datasource without one", "GET", ds("child")+"/history", nil)
			// checking
			check := func(name, k string, rule EngineRule) {
				as("admin", "check: "+name, "POST", ds(k)+"/check", []EngineRule{rule})
			}
			check("the engine refuses with a detail", "root", EngineRule{Path: ".", Status: 422, Body: map[string]any{"detail": "too large"}})
			check("a dict detail", "root", EngineRule{Path: ".", Status: 500, Body: map[string]any{"detail": map[string]any{"a": 1, "b": []any{true, nil}}}})
			check("an empty detail", "root", EngineRule{Path: ".", Status: 500, Body: map[string]any{"detail": ""}})
			check("a list error", "root", EngineRule{Path: ".", Status: 500, Body: []any{1}})
			check("a text error", "root", EngineRule{Path: ".", Status: 503, Text: "down"})
			check("not JSON", "root", EngineRule{Path: ".", Status: 200, Text: "ok"})
			check("errors not a number", "root", EngineRule{Path: ".", Body: map[string]any{"outcome": "x", "errors": "many"}})
			check("errors a float", "root", EngineRule{Path: ".", Body: map[string]any{"outcome": "failed", "errors": 2.7, "warnings": true}})
			check("an empty document is not the one asked", "run", EngineRule{Path: ".", Body: report})
			check("no contract", "child", EngineRule{Path: ".", Body: report})
			check("a document that is a list", "broken", EngineRule{Path: ".", Body: report})
			check("a blocked one", "ready", EngineRule{Path: ".", Body: report})
			as("writer", "check without EDIT", "POST", ds("root")+"/check", nil)
			as("editor", "check with EDIT", "POST", ds("root")+"/check", nil)
			// proposing
			propose := func(name string, rule EngineRule) {
				as("editor", "propose: "+name, "POST", ds("child")+"/proposal", []EngineRule{rule})
			}
			propose("no proposal", EngineRule{Path: ".", Body: map[string]any{"proposal": nil}})
			propose("refused", EngineRule{Path: ".", Status: 404, Body: map[string]any{"detail": "no data"}})
			propose("refused with nothing", EngineRule{Path: ".", Status: 500, Text: ""})
			propose("a list", EngineRule{Path: ".", Body: []any{}})
			as("editor", "propose on a hidden datasource", "POST", ds("hidden")+"/proposal", nil)
			// deleting
			as("writer", "delete without EDIT", "DELETE", ds("half"), nil)
			as("editor", "delete", "DELETE", ds("half"), nil)
			as("editor", "delete again", "DELETE", ds("half"), nil)
			as("editor", "delete where there is none", "DELETE", ds("child"), nil)
			as("editor", "delete an odd id", "DELETE", "/datasources/x/contract", nil)
			return append(s, contractsWrite(w, ds)...)
		},
	})
}

// contractsWrite: writing a contract (put_contract) and its ODCS export.
func contractsWrite(w *World, ds func(string) string) []Step {
	var s []Step
	notNull := map[string]any{"rules": []any{map[string]any{"kind": "not_null", "severity": "error", "column": "a"}}}
	s = append(s, forEveryone(w, "write", "PUT", ds("child"), map[string]any{"document": notNull})...)
	s = append(s, forEveryone(w, "the ODCS", "GET", ds("root")+"/odcs", nil)...)
	put := func(person, name, k string, body any, rules []EngineRule) {
		s = append(s, Step{Name: "write: " + name + " · " + person, Method: "PUT", Path: ds(k), Auth: w.As(person), JSON: body, Engine: rules})
	}
	odcs := func(person, name, k string) {
		s = append(s, Step{Name: "ODCS: " + name + " · " + person, Method: "GET", Path: ds(k) + "/odcs", Auth: w.As(person)})
	}
	rich := map[string]any{"description": "Sales «net»\u2028", "rules": []any{
		map[string]any{"kind": "column", "severity": "error", "column": "a", "dtype": "integer", "id": "keep"},
		map[string]any{"kind": "not_null", "severity": "error", "column": "a", "id": "keep"},
		map[string]any{"kind": "unique", "severity": "error", "column": "a"},
		map[string]any{"kind": "unique", "severity": "warning", "columns": []any{"a", `b"c`}},
		map[string]any{"kind": "accepted_values", "severity": "error", "column": "b", "values": []any{"it's", 1, 2.5, true}},
		map[string]any{"kind": "range", "severity": "error", "column": "a", "min": 1, "max": 1e20},
		map[string]any{"kind": "range", "severity": "warning", "column": "new", "min": "2020-01-01"},
		map[string]any{"kind": "pattern", "severity": "warning", "column": "b", "regex": `^(?<=x)[A-Z]{2}\d+$`},
		map[string]any{"kind": "row_count", "severity": "error", "min": 1, "max": 9},
		map[string]any{"kind": "row_count", "severity": "error", "max": 9},
		map[string]any{"kind": "freshness", "severity": "warning", "max_age_hours": 1.5, "column": "a"},
		map[string]any{"kind": "freshness", "severity": "warning", "max_age_hours": 24},
		map[string]any{"kind": "expression", "severity": "error", "sql": "a > 0", "name": "positive", "extra": 1},
		map[string]any{"kind": "expression", "severity": "error", "sql": "b <> ''", "id": "r1"},
	}}
	put("editor", "a rich one", "child", map[string]any{"document": rich, "enabled": true}, nil)
	odcs("writer", "a rich one", "child")
	put("editor", "the same again", "child", map[string]any{"document": rich}, nil)
	put("editor", "the same, turned off", "child", map[string]any{"document": rich, "enabled": false}, nil)
	odcs("editor", "turned off", "child")
	put("editor", "turned on, nothing changed", "child", map[string]any{"document": rich}, nil)
	// what the form refuses (before the datasource is even looked for)
	bad := func(name string, doc any) {
		put("editor", "refused: "+name, "child", map[string]any{"document": doc}, nil)
	}
	bad("rules not a list", map[string]any{"rules": "x"})
	bad("rules null", map[string]any{"rules": nil})
	bad("too many rules", map[string]any{"rules": make([]any, 201)})
	bad("a pattern Python does not take", map[string]any{"rules": []any{
		map[string]any{"kind": "pattern", "severity": "error", "column": "a", "regex": "("},
		map[string]any{"kind": "pattern", "severity": "error", "column": "a", "regex": "(?P<x>a)(?P=x)"},
		map[string]any{"kind": "pattern", "severity": "error", "column": "a", "regex": "a{2,1}"},
		map[string]any{"kind": "pattern", "severity": "error", "column": " ", "regex": "("},
		map[string]any{"kind": "nope", "severity": "error"}, 5,
		map[string]any{"kind": "range", "severity": "error", "column": "a", "min": 5, "max": 1},
		map[string]any{"kind": "accepted_values", "severity": "error", "column": "a", "values": []any{nil}},
		map[string]any{"kind": "freshness", "severity": "error", "max_age_hours": 0},
		map[string]any{"kind": "expression", "severity": "fatal", "sql": "x"},
	}})
	s = append(s, Step{Name: "write: refused on a datasource that is not there", Method: "PUT", Path: "/datasources/999999/contract",
		Auth: w.As("editor"), JSON: map[string]any{"document": map[string]any{"rules": 1}}})
	s = append(s, Step{Name: "write: a good one on a datasource that is not there", Method: "PUT", Path: "/datasources/999999/contract",
		Auth: w.As("editor"), JSON: map[string]any{"document": notNull}})
	s = append(s, Step{Name: "write: a bad id and a bad body", Method: "PUT", Path: "/datasources/x/contract", Auth: w.As("editor"),
		JSON: map[string]any{"document": []any{}, "enabled": "maybe", "notify_connection_id": "x"}})
	s = append(s, Step{Name: "write: no body", Method: "PUT", Path: ds("child"), Auth: w.As("editor")})
	s = append(s, Step{Name: "write: a broken JSON, nobody", Method: "PUT", Path: ds("child"), Raw: []byte("{"), ContentType: "application/json"})
	put("writer", "without EDIT", "root", map[string]any{"document": notNull}, nil)
	put("editor", "on a hidden datasource", "hidden", map[string]any{"document": notNull}, nil)
	// whom to notify
	notify := func(person, name string, emails any, connID any, before func(*World)) {
		body := map[string]any{"document": rich}
		if emails != "-" {
			body["notify_emails"] = emails
		}
		if connID != "-" {
			body["notify_connection_id"] = connID
		}
		s = append(s, Step{Name: "notify: " + name + " · " + person, Method: "PUT", Path: ds("child"), Auth: w.As(person), JSON: body, Before: before})
	}
	smtpID, pgID := Data["conn:smtp"], Data["conn:pg"]
	notify("editor", "a bad address", "a@x.test; bad", "-", nil)
	notify("editor", "addresses without a connection", " a@x.test; b@y.test ,", "-", nil)
	notify("editor", "a connection that is not SMTP", "-", pgID, nil)
	notify("editor", "a connection that is not there", "-", 999999, nil)
	notify("writer", "an SMTP connection without CONNECT", "-", smtpID, nil)
	notify("editor", "the SMTP connection", "-", smtpID, nil)
	notify("editor", "the same again", "a@x.test, b@y.test", smtpID, nil)
	notify("editor", "only the rules", "-", "-", nil)
	notify("editor", "the limited connection, outside its domains", "a@example.test, c@z.test", Data["conn:smtp-dom"], nil)
	notify("editor", "the limited connection, within them", "a@EXAMPLE.test; b@b.test", Data["conn:smtp-dom"], nil)
	notify("editor", "new addresses, the limited connection kept", "x@nowhere.test", "-", nil)
	notify("editor", "a connection whose domains are malformed", "-", Data["conn:smtp-bad"], nil)
	notify("editor", "the connection removed", "-", 0, nil)
	notify("editor", "the addresses removed", "", "-", nil)
	notify("editor", "the connection removed again", "-", -1, nil)
	// the check after the save: what the engine says
	evaluate := func(name string, rule EngineRule, doc any) {
		put("admin", "the check: "+name, "child", map[string]any{"document": doc}, []EngineRule{rule})
	}
	changed := func(n int) map[string]any {
		return map[string]any{"rules": []any{map[string]any{"kind": "row_count", "severity": "error", "min": n}}}
	}
	evaluate("refused", EngineRule{Path: ".", Status: 422, Body: map[string]any{"detail": "too large"}}, changed(1))
	evaluate("down", EngineRule{Path: ".", Status: 503, Text: "down"}, changed(2))
	evaluate("not JSON", EngineRule{Path: ".", Status: 200, Text: "ok"}, changed(3))
	evaluate("a list", EngineRule{Path: ".", Body: []any{1}}, changed(4))
	evaluate("errors a float", EngineRule{Path: ".", Body: map[string]any{"outcome": "failed", "errors": 2.7, "warnings": true}}, changed(5))
	evaluate("no rules: no check", EngineRule{Path: ".", Status: 500}, map[string]any{"rules": []any{}, "description": 5})
	// a new contract after one deleted: the next version
	s = append(s, Step{Name: "write: delete it", Method: "DELETE", Path: ds("child"), Auth: w.As("editor")})
	put("editor", "after the delete", "child", map[string]any{"document": notNull}, nil)
	put("admin", "over a document that is a list", "broken", map[string]any{"document": notNull}, nil)
	put("admin", "over the seeded one", "ready", map[string]any{"document": map[string]any{"description": "",
		"rules": []any{map[string]any{"id": "r1", "kind": "not_null", "severity": "error", "column": "a"}}}}, nil)
	// the ODCS of what is there
	for _, k := range []string{"child", "half", "nulls", "ready", "broken", "run", "hidden"} {
		odcs("admin", k, k)
	}
	odcs("editor", "the hidden one", "hidden")
	s = append(s, Step{Name: "ODCS: an odd id · editor", Method: "GET", Path: "/datasources/x/contract/odcs", Auth: w.As("editor")})
	s = append(s, Step{Name: "ODCS: not there · editor", Method: "GET", Path: "/datasources/999999/contract/odcs", Auth: w.As("editor")})
	return s
}

var contractsSeeded bool

// seedContracts is the data lane's seed with two more SMTP connections: one limited to some
// domains, one whose limit is malformed.
func seedContracts(w *World) {
	seedData(w)
	if contractsSeeded {
		return
	}
	contractsSeeded = true
	for _, c := range []struct{ key, name, extra string }{{"smtp-dom", "data-conn-smtp-dom", `{"allowed_domains": "Example.TEST, @b.test"}`},
		{"smtp-bad", "data-conn-smtp-bad", `{"allowed_domains": [1]}`}} {
		Data["conn:"+c.key] = w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
			VALUES ($1, '', $2, $3, 'smtp', 'mail2.example.test', NULL, 'u', '', '', '', $4, '', '', '', '2026-01-06', '2026-01-06')
			RETURNING id`, c.name, Data["connect"], w.People["admin"], c.extra)
	}
}
