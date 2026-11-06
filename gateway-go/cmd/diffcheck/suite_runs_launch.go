package main

import (
	"fmt"
	"strings"
	"time"
)

// routes/runs.py launch_run and launch_email_test: a flow's run queued on the engine with its
// publish, destination, copy and email resolved by the gateway; the email node's trial.

// RL are the ids of this suite's seed.
var RL = map[string]int64{}

const outKey = `out/[0-9a-f]{32}\.parquet`

func init() {
	register(Suite{
		Module: "runs_launch",
		Seed: func(w *World) {
			seedData(w)
			maskPattern("runs", "output_key", snapshotStamp)
			maskPattern("runs", "output_key", outKey)
			maskPattern("runs", "task_id", `^pending-[0-9a-f]{32}$`)
			w.Token("writer", "tab_rl_writer_write_0000000", "write", time.Hour)
			folder := func(key string) {
				RL[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
					VALUES ($1, '', NULL, $2, '2026-01-02') RETURNING id`, "rl-"+key, w.People["admin"])
			}
			for _, f := range []string{"flows", "pub", "conn", "view", "noconn"} {
				folder(f)
			}
			grant := func(f, person, c string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`, RL[f], w.People[person], c)
			}
			grant("flows", "editor", "run")
			grant("flows", "editor", "view")
			grant("flows", "writer", "run")
			grant("pub", "editor", "edit")
			grant("conn", "editor", "connect")
			grant("conn", "writer", "connect")
			grant("view", "editor", "view")
			grant("noconn", "editor", "view")
			flow := func(key, engine string, production *string) {
				RL["flow:"+key] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, production_engine,
					created_at, updated_at) VALUES ($1, '', $2, $3, '{}', $4, $5, '2026-01-03', '2026-01-03') RETURNING id`,
					"rl-flow-"+key, RL["flows"], w.People["admin"], engine, production)
			}
			flow("a", "duckdb", ptr("polars"))
			flow("b", "polars", nil)
			RL["flow:hidden"] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
				VALUES ('rl-hidden', '', $1, NULL, '{}', 'duckdb', '2026-01-03', '2026-01-03') RETURNING id`, RL["view"])
			ds := func(key, name, f, kind, dsKey string) {
				RL["ds:"+key] = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns,
					sort_keys, column_descriptions, kind, created_at, updated_at) VALUES ($1, '', $2, NULL, 'data-prep', $3, 1, '[]', '[]', '{}',
					$4, '2026-01-04', '2026-01-04') RETURNING id`, name, RL[f], dsKey, kind)
			}
			ds("src", "rl-src", "view", "flow", "datasets/rl-src.parquet")
			ds("out", "rl-out", "pub", "flow", "datasets/rl-out.parquet")
			ds("db", "rl-db", "pub", "database", "datasets/rl-db.parquet")
			ds("secret", "rl-secret", "noconn", "flow", "datasets/rl-secret.parquet")
			w.Exec(`UPDATE projects SET parent_id = NULL WHERE id = $1`, RL["noconn"])
			w.Exec(`DELETE FROM permissions WHERE project_id = $1`, RL["noconn"])
			w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_by, updated_by,
				created_at, updated_at) VALUES ($1, 2, '{"rules": [{"id": "r", "kind": "not_null", "column": "a"}]}', true, 'passed', 0, 0, NULL, NULL,
				'2026-02-01', '2026-02-01')`, RL["ds:out"])
			conn := func(key, f, dbType, host, database, extra string, port *int) {
				RL["conn:"+key] = w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
					password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
					VALUES ($1, '', $2, NULL, $3, $4, $5, 'u', $6, $7, '', $8, '', 'ssh-ed25519', 'SHA256:k', '2026-01-06', '2026-01-06') RETURNING id`,
					"rl-"+key, RL[f], dbType, host, port, seal("pw"), database, extra)
			}
			conn("pg", "conn", "postgresql", "pg.test", "sales", `{"tls": "verify"}`, nil)
			conn("sftp", "conn", "sftp", "sftp.test", "", `{}`, ptr(2222))
			conn("s3", "conn", "s3", "", "", `{}`, nil)
			conn("s3b", "conn", "s3", "http://minio.test", "lake", `{}`, nil)
			conn("smtp", "conn", "smtp", "mail.test", "", `{"allowed_domains": "x.test, @Y.test", "from_address": "a@x.test"}`, ptr(25))
			conn("smtpopen", "conn", "smtp", "mail2.test", "", `{}`, nil)
			conn("smtpbad", "conn", "smtp", "mail3.test", "", `{"allowed_domains": 5}`, nil)
			conn("api", "conn", "api", "https://api.test", "", `{}`, nil)
			conn("hidden", "noconn", "postgresql", "h.test", "x", `{}`, nil)
		},
		Engine: []EngineRule{
			{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-rl"}},
			{Method: "POST", Path: "^/placeholders/preview$", Body: map[string]any{"fields": map[string]any{}}},
		},
		Steps: runsLaunchSteps,
	})
}

func runsLaunchSteps(w *World) []Step {
	var s []Step
	masked := []string{"*.body.output_key"}
	scrub := []string{snapshotStamp, outKey}
	add := func(person, name, path string, body any, rules ...EngineRule) {
		st := Step{Name: name + " · " + person, Method: "POST", Path: path, JSON: body, Auth: w.As(person), EngineMask: masked, Scrub: scrub}
		if len(rules) > 0 {
			st.Engine = rules
		}
		s = append(s, st)
	}
	runs := func(k string) string { return fmt.Sprintf("/flows/%d/runs", RL["flow:"+k]) }
	trial := func(k string) string { return fmt.Sprintf("/flows/%d/email-test", RL["flow:"+k]) }
	c := func(k string) int64 { return RL["conn:"+k] }
	base := func(more map[string]any) map[string]any {
		b := map[string]any{"bucket": "", "input_key": "datasets/rl-src.parquet", "operations": []any{map[string]any{"type": "filter",
			"params": map[string]any{"expr": "a > 1", "n": 1.5}}}}
		for k, v := range more {
			b[k] = v
		}
		return b
	}
	preview := func(body any) EngineRule {
		return EngineRule{Method: "POST", Path: "^/placeholders/preview$", Body: body}
	}
	for _, st := range forEveryone(w, "launch", "POST", runs("a"), base(nil)) {
		st.EngineMask, st.Scrub = masked, scrub
		s = append(s, st)
	}
	add("editor", "launch, a minimal body", runs("b"), map[string]any{"bucket": "data-prep", "input_key": "datasets/rl-src.parquet"})
	add("editor", "launch, no body", runs("b"), nil)
	add("editor", "launch, a bad body", runs("b"), map[string]any{"bucket": 1, "operations": "x", "publish": map[string]any{}})
	add("editor", "launch an unknown flow", "/flows/999999/runs", base(nil))
	add("editor", "launch an odd flow", "/flows/x/runs", base(nil))
	add("editor", "launch without RUN", runs("hidden"), base(nil))
	add("editor", "launch, another bucket", runs("a"), base(map[string]any{"bucket": "other"}))
	add("admin", "launch, another bucket as admin", runs("a"), base(map[string]any{"bucket": "other"}))
	add("editor", "launch, an unmanaged key", runs("a"), base(map[string]any{"input_key": "secret/x.parquet"}))
	add("editor", "launch, an unreadable key", runs("a"), base(map[string]any{"input_key": "datasets/rl-secret.parquet"}))
	add("editor", "launch, a nested unreadable key", runs("a"), base(map[string]any{"operations": []any{map[string]any{"type": "join",
		"params": map[string]any{"right": map[string]any{"bucket": "data-prep", "key": "datasets/rl-secret.parquet"}}}}}))
	add("editor", "launch, dev samples kept in development", runs("a"), base(map[string]any{"operations": []any{map[string]any{"type": "limit",
		"_dev_sample": true, "params": map[string]any{"n": 10}}}}))
	for _, f := range []struct {
		name string
		rule EngineRule
	}{
		{"the engine refuses", EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Status: 429, Body: map[string]any{"detail": "busy"}}},
		{"no task id", EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{}}},
		{"not JSON", EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Text: "x"}},
		{"a numeric task id", EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": 77}}},
	} {
		add("editor", "launch: "+f.name, runs("a"), base(nil), f.rule)
		add("editor", "launch with SFTP: "+f.name, runs("a"), base(map[string]any{"destination": map[string]any{"type": "sftp",
			"connection_id": c("sftp"), "file_name": "x.csv", "format": "csv"}}), f.rule)
	}

	// publish
	pub := func(more map[string]any) map[string]any {
		p := map[string]any{"name": " rl-new ", "project_id": RL["pub"]}
		for k, v := range more {
			p[k] = v
		}
		return base(map[string]any{"publish": p})
	}
	add("editor", "publish", runs("a"), pub(map[string]any{"description": "d", "sort_keys": []any{" b ", "", "a"}}))
	add("editor", "publish over a flow's", runs("a"), pub(map[string]any{"name": "rl-out", "overwrite": true}))
	add("editor", "publish over, without overwrite", runs("a"), pub(map[string]any{"name": "rl-out"}))
	add("editor", "publish over a database's", runs("a"), pub(map[string]any{"name": "rl-db", "overwrite": true}))
	add("editor", "publish, an empty name", runs("a"), pub(map[string]any{"name": "  "}))
	add("editor", "publish, an unknown folder", runs("a"), pub(map[string]any{"project_id": 999999}))
	add("editor", "publish, a huge folder", runs("a"), pub(map[string]any{"project_id": 99999999999}))
	add("editor", "publish where only VIEW", runs("a"), pub(map[string]any{"project_id": RL["view"]}))
	add("editor", "publish over, contract broken", runs("a"), pub(map[string]any{"name": "rl-out", "overwrite": true}),
		EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Status: 400, Text: "bad contract"})

	// destinations
	dest := func(d map[string]any) map[string]any { return base(map[string]any{"destination": d}) }
	for _, d := range []map[string]any{
		{"type": "database", "connection_id": c("pg"), "table": " t ", "mode": "replace", "post_sql": "analyze"},
		{"connection_id": c("pg")}, {"connection_id": c("s3"), "table": "t"}, {"connection_id": c("smtp"), "table": "t"},
		{"connection_id": 999999, "table": "t"}, {"connection_id": c("hidden"), "table": "t"},
		{"type": "s3", "connection_id": c("s3b"), "key": " /a/b.parquet/ ", "partition_by": []any{"x"}},
		{"type": "s3", "connection_id": c("s3"), "key": "a", "bucket": " own ", "format": "csv", "timezone": "Europe/Rome"},
		{"type": "s3", "connection_id": c("s3"), "key": "a"}, {"type": "s3", "connection_id": c("s3b"), "key": " / "},
		{"type": "s3", "connection_id": c("s3b"), "key": "a", "format": "xlsx"}, {"type": "s3", "connection_id": c("pg"), "key": "a"},
		{"type": "s3", "connection_id": c("s3b"), "key": "out/{today}.parquet"},
		{"type": "sftp", "connection_id": c("sftp"), "folder": " out ", "file_name": " r_{run_id}.csv ", "format": "csv"},
		{"type": "sftp", "connection_id": c("sftp"), "file_name": "a.xlsx", "format": "xlsx", "timezone": " UTC "},
		{"type": "sftp", "connection_id": c("sftp"), "file_name": "a/b.csv"}, {"type": "sftp", "connection_id": c("sftp"), "file_name": "{a/b}.csv"},
		{"type": "sftp", "connection_id": c("sftp"), "file_name": " "}, {"type": "sftp", "connection_id": c("sftp"), "folder": "a\\..", "file_name": "x"},
		{"type": "sftp", "connection_id": c("pg"), "file_name": "x"}, {"type": "sftp", "connection_id": c("sftp"), "file_name": "x", "format": "parquet",
			"timezone": "Mars/Base"},
		{"type": "ftp", "connection_id": c("sftp")},
	} {
		add("editor", fmt.Sprintf("destination %v", d), runs("a"), dest(d))
	}
	sftpT := dest(map[string]any{"type": "sftp", "connection_id": c("sftp"), "folder": "in/{x}", "file_name": "{today}.csv", "timezone": "Europe/Rome"})
	for _, p := range []struct {
		name string
		rule EngineRule
	}{
		{"the zone refused", preview(map[string]any{"error": "unknown zone"})},
		{"a field refused", preview(map[string]any{"fields": map[string]any{"folder": map[string]any{"error": "no {x}"}}})},
		{"a field fine", preview(map[string]any{"fields": map[string]any{"folder": map[string]any{"value": "in/1"}, "file name": nil}})},
		{"fields not a dict", preview(map[string]any{"fields": []any{1}})},
		{"a field not a dict", preview(map[string]any{"fields": map[string]any{"file name": "x"}})},
		{"the engine fails", EngineRule{Method: "POST", Path: "^/placeholders/preview$", Status: 500, Text: "x"}},
		{"not JSON", EngineRule{Method: "POST", Path: "^/placeholders/preview$", Text: "x"}},
	} {
		st := Step{Name: "sftp templates: " + p.name + " · editor", Method: "POST", Path: runs("a"), JSON: sftpT, Auth: w.As("editor"),
			EngineMask: masked, Scrub: scrub, Engine: []EngineRule{p.rule, {Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-rl"}}}}
		s = append(s, st)
	}

	// the copy
	for _, m := range []map[string]any{
		{"connection_id": c("s3b"), "key": " /copy.parquet "}, {"connection_id": c("s3"), "key": "c", "bucket": " b "},
		{"connection_id": c("s3"), "key": "c"}, {"connection_id": c("s3b"), "key": "  "}, {"connection_id": c("s3b"), "key": "c/{today}", "timezone": "Europe/Rome"},
		{"connection_id": c("pg"), "key": "c"}, {"connection_id": 999999, "key": "c"}, {"connection_id": c("hidden"), "key": "c"}, {"connection_id": c("s3b")},
	} {
		add("editor", fmt.Sprintf("copy %v", m), runs("a"), base(map[string]any{"mirror": m}))
	}
	add("editor", "copy, its template refused", runs("a"), base(map[string]any{"mirror": map[string]any{"connection_id": c("s3b"), "key": "{x}"}}),
		preview(map[string]any{"fields": map[string]any{"copy's S3 key": map[string]any{"error": strings.Repeat("é", 600)}}}),
		EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Body: map[string]any{"task_id": "t-rl"}})

	// the email
	email := func(e map[string]any) map[string]any { return base(map[string]any{"email": e}) }
	for _, e := range []map[string]any{
		{"connection_id": c("smtp"), "to": []any{" a@x.test ", ""}, "cc": []any{"b@Y.TEST"}, "subject": "Report", "body": "<b>hi</b>", "body_is_html": true},
		{"connection_id": c("smtp"), "to": []any{"a@z.test", "b@x.test", "nodomain"}},
		{"connection_id": c("smtp"), "to": []any{" "}}, {"connection_id": c("smtpbad"), "to": []any{"a@x.test"}},
		{"connection_id": c("smtpopen"), "to": []any{"anyone@any.test"}, "attachment_name": "r_{today}", "attachment_format": "csv",
			"stop_on_failure": false, "timezone": "Europe/Rome"},
		{"connection_id": c("smtpopen"), "to": []any{"a@b.test"}, "subject": "{flow} on {today}"},
		{"connection_id": c("pg"), "to": []any{"a@x.test"}}, {"connection_id": 999999}, {"connection_id": c("hidden"), "to": []any{"a@x.test"}},
		{"connection_id": c("smtp"), "attachment_format": "pdf"},
	} {
		add("editor", fmt.Sprintf("email %v", e), runs("a"), email(e))
	}
	add("editor", "email, its subject refused", runs("a"), email(map[string]any{"connection_id": c("smtpopen"), "to": []any{"a@b.test"},
		"subject": "{x}"}), preview(map[string]any{"fields": map[string]any{"subject": map[string]any{"error": "no {x}"}}}))
	all := base(map[string]any{
		"publish":     map[string]any{"name": "rl-all", "project_id": RL["pub"], "overwrite": true},
		"destination": map[string]any{"type": "s3", "connection_id": c("s3b"), "key": "a_{run_id}.parquet"},
		"mirror":      map[string]any{"connection_id": c("s3b"), "key": "m_{run_id}"},
		"email":       map[string]any{"connection_id": c("smtp"), "to": []any{"z@x.test"}, "subject": "{run_id}"},
	})
	add("editor", "everything at once", runs("a"), all)
	add("editor", "everything at once, the engine refuses", runs("a"), all,
		EngineRule{Method: "POST", Path: "^/tasks/transform-data$", Status: 500, Text: "down"})

	// the email's trial
	for _, st := range forEveryone(w, "email trial", "POST", trial("a"), all) {
		st.EngineMask, st.Scrub = masked, scrub
		s = append(s, st)
	}
	add("editor", "email trial without an email", trial("a"), base(nil))
	add("editor", "email trial, domains malformed (not applied)", trial("a"), email(map[string]any{"connection_id": c("smtpbad")}))
	add("editor", "email trial, no recipients needed", trial("b"), email(map[string]any{"connection_id": c("smtp"), "attachment_name": "{today}"}))
	add("editor", "email trial of an unknown flow", "/flows/999999/email-test", base(nil))
	add("editor", "email trial without RUN", trial("hidden"), email(map[string]any{"connection_id": c("smtp")}))
	add("editor", "email trial, a bad body", trial("a"), map[string]any{"email": map[string]any{"to": "x"}})
	s = append(s, Step{Name: "launch with the writer's token", Method: "POST", Path: runs("a"), JSON: email(map[string]any{"connection_id": c("smtp"),
		"to": []any{"q@x.test"}}), Auth: Bearer("tab_rl_writer_write_0000000"), EngineMask: masked, Scrub: scrub})
	return append(s, settleRandomKeys("runs_launch"))
}
