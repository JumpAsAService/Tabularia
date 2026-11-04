package main

import "fmt"

// The folders, flows and runs the suites of the flows' lane (flows, flow_groups,
// scheduling, proxy, runs) share, seeded once by the first of them that runs. Names
// carry the «fl-» prefix, so the other lanes' seeds do not collide.
//
//	fl-Sales     Sales: EDIT          (editor, admin)
//	  fl-Child   inherits it
//	fl-Other     writer: VIEW; Analysts: RUN
//	fl-Dest      Sales: EDIT          (a move's destination)
//	fl-Hidden    nobody
var FL = struct {
	seeded, schedSeeded               bool
	Sales, Child, Other, Dest, Hidden int64
	// flows
	Alpha, Beta, Gamma, Boxed, Keys, Sched, Secret, Broken, Duck, Spark, Scripted, Doomed int64
	// a datasource of fl-Hidden, one of fl-Sales published by Doomed
	HiddenDS, SalesDS, OtherDS                         int64
	Sched2, Smtp, SmtpOpen, SmtpBroken, SmtpHidden, PG int64
	Env, Registry, Script                              int64
}{}

func (w *World) seedFlows() {
	if FL.seeded {
		return
	}
	FL.seeded = true
	folder := func(name string, parent *int64) int64 {
		return w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
			VALUES ($1, '', $2, $3, '2026-01-01') RETURNING id`, name, parent, w.People["admin"])
	}
	FL.Sales = folder("fl-Sales", nil)
	FL.Child = folder("fl-Child", &FL.Sales)
	FL.Other = folder("fl-Other", nil)
	FL.Dest = folder("fl-Dest", nil)
	FL.Hidden = folder("fl-Hidden", nil)
	grant := func(project int64, user, group *int64, c string) {
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)`, project, user, group, c)
	}
	sales, analysts, writer := Groups["Sales"], Groups["Analysts"], w.People["writer"]
	grant(FL.Sales, nil, &sales, "edit")
	grant(FL.Other, &writer, nil, "view")
	grant(FL.Other, nil, &analysts, "run")
	grant(FL.Dest, nil, &sales, "edit")

	FL.Env = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('fl-env', '', 'python', true, '1', '1', '1Gi', '1Gi', 3600,
		'2026-01-01', '2026-01-01') RETURNING id`)
	w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, FL.Env, analysts)
	FL.Registry = w.Insert(`INSERT INTO container_registries (name, host, protocol, tls, username, repositories, enabled, created_at,
		updated_at) VALUES ('fl-registry', 'ghcr.io', 'https', 'verify', '', 'team/*', true, '2026-01-01', '2026-01-01') RETURNING id`)
	FL.Script = w.Insert(`INSERT INTO saved_scripts (name, description, project_id, owner_id, environment_id, current_version, created_at,
		updated_at) VALUES ('fl-script', '', $1, $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, FL.Hidden, w.People["admin"], FL.Env)

	FL.HiddenDS = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, created_at, updated_at) VALUES ('fl-hidden-ds', '', $1, $2, 'data-prep', 'datasets/hidden.parquet',
		'[]', '[]', '{}', 'upload', '2026-01-01', '2026-01-01') RETURNING id`, FL.Hidden, w.People["admin"])

	FL.OtherDS = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, created_at, updated_at) VALUES ('fl-other-ds', '', $1, $2, 'data-prep', 'datasets/other.parquet',
		'[]', '[]', '{}', 'upload', '2026-01-01', '2026-01-01') RETURNING id`, FL.Other, w.People["admin"])
	w.Exec(`INSERT INTO uploads (dataset_id, bucket, parquet_key, raw_key, filename, owner_id, created_at)
		VALUES ('up1', 'data-prep', 'datasets/up-editor.parquet', 'raw/up-editor.csv', 'a.csv', $1, '2026-01-01')`, w.People["editor"])

	flow := func(name string, project int64, definition, engine string, schedule *string) int64 {
		var by *int64
		if schedule != nil {
			by = ptr(w.People["admin"])
		}
		return w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, run_schedule, run_scheduled_by,
			created_at, updated_at) VALUES ($1, $8, $2, $3, $4, $5, $6, $7, '2026-01-02 10:00:00', '2026-01-02 11:30:00.250000')
			RETURNING id`, name, project, w.People["admin"], definition, engine, schedule, by, "about "+name)
	}
	out := `{"nodes": [{"id": "o1", "type": "output", "data": {"label": "Out"}}], "edges": []}`
	FL.Alpha = flow("fl Alpha", FL.Sales, out, "polars", nil)
	FL.Beta = flow("fl Beta (1)", FL.Child, `{"nodes": [], "edges": []}`, "duckdb", nil)
	FL.Gamma = flow("fl Gamma", FL.Other, out, "polars", nil)
	FL.Boxed = flow("fl Boxed", FL.Sales, fmt.Sprintf(`{"nodes": [{"id": "c1", "type": "container", "data": {"registryId": %d,
		"repository": "team/app", "label": "Box"}}]}`, FL.Registry), "polars", nil)
	FL.Keys = flow("fl Keys", FL.Sales, fmt.Sprintf(`{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": %d}}]}`,
		FL.HiddenDS), "polars", nil)
	FL.Sched = flow("fl Sched", FL.Sales, out, "polars", ptr("0 3 * * *"))
	FL.Secret = flow("fl Secret", FL.Hidden, out, "polars", nil)
	FL.Broken = flow("fl Broken", FL.Sales, "not json", "polars", nil)
	FL.Duck = flow("fl Duck", FL.Sales, out, "duckdb", nil)
	FL.Spark = flow("fl Spark", FL.Sales, out, "spark", nil)
	FL.Scripted = flow("fl Scripted", FL.Other, fmt.Sprintf(`{"nodes": [{"id": "x", "type": "script", "data": {"environmentId": %d}},
		{"id": "y", "type": "script", "data": {"scriptId": %d}}]}`, FL.Env, FL.Script), "polars", nil)
	FL.Doomed = flow("fl Doomed", FL.Sales, out, "polars", nil)
	FL.Sched2 = flow("fl Sched2", FL.Sales, out, "polars", ptr("*/30 * * * *"))
	conn := func(name string, project int64, kind, extra string) int64 {
		return w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, 'smtp.x.test', 587, 'u', '', 'db', '', $5, '2026-01-01', '2026-01-01') RETURNING id`,
			name, project, w.People["admin"], kind, extra)
	}
	FL.Smtp = conn("fl-smtp", FL.Sales, "smtp", `{"allowed_domains": ["X.it", "@y.it", " "]}`)
	FL.SmtpOpen = conn("fl-smtp-open", FL.Sales, "smtp", ``)
	FL.SmtpBroken = conn("fl-smtp-broken", FL.Sales, "smtp", `{"allowed_domains": 5}`)
	FL.SmtpHidden = conn("fl-smtp-hidden", FL.Hidden, "smtp", `{}`)
	FL.PG = conn("fl-pg", FL.Sales, "postgres", `{}`)
	grant(FL.Sales, nil, &sales, "connect")
	obsGroup := w.People["obs_group"]
	grant(FL.Child, &obsGroup, nil, "view")

	version := func(flow, v int64, definition, note string) {
		w.Exec(`INSERT INTO flow_versions (flow_id, version, definition, note, created_at, created_by)
			VALUES ($1, $2, $3, $4, '2026-01-02 10:00:00', $5)`, flow, v, definition, note, w.People["editor"])
	}
	version(FL.Alpha, 1, `{"nodes": [], "edges": []}`, "created")
	version(FL.Alpha, 2, out, "edited")
	version(FL.Sched, 1, fmt.Sprintf(`{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 424242}}]}`), "created")
	version(FL.Sched, 2, out, "edited")
	version(FL.Keys, 1, out, "created")
	version(FL.Doomed, 1, out, "created")

	run := func(flow int64, status, kind string, started, finished *string, parent *int64) int64 {
		return w.Insert(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
			output_key, publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, parent_run_id)
			VALUES ($1, $2, $8, $3, $4, 'manual', 'datasets/in.parquet', 'data-prep', 'out/x.parquet', '', false, '[]',
			$5, $6, $7) RETURNING id`, kind, flow, status, w.People["admin"], started, finished, parent,
			fmt.Sprintf("t-%d-%s-%s", flow, status, *started))
	}
	run(FL.Alpha, "SUCCESS", "flow", ptr("2026-01-03 10:00:00"), ptr("2026-01-03 10:00:12.345678"), nil)
	run(FL.Alpha, "FAILURE", "flow", ptr("2026-01-04 10:00:00"), ptr("2026-01-04 10:01:00.000001"), nil)
	run(FL.Alpha, "SUCCESS", "flow", ptr("2026-01-05 10:00:00"), ptr("2026-01-05 10:00:00.333333"), nil)
	run(FL.Alpha, "SUCCESS", "flow", ptr("2026-01-06 10:00:00"), ptr("2026-01-06 09:00:00"), nil) // finished before it started
	doomedRun := run(FL.Doomed, "SUCCESS", "orchestration", ptr("2026-01-03 09:59:57"), ptr("2026-01-03 10:00:01"), nil)
	run(FL.Alpha, "SUCCESS", "flow", ptr("2026-01-03 09:59:58"), ptr("2026-01-03 10:00:02"), &doomedRun) // a child of Doomed's
	run(FL.Doomed, "SUCCESS", "flow", ptr("2026-01-03 09:59:59"), nil, &doomedRun)                       // no two runs start together: the history's order
	FL.SalesDS = w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys,
		column_descriptions, kind, flow_id, created_at, updated_at) VALUES ('fl-sales-ds', '', $1, $2, 'data-prep', 'datasets/sales.parquet',
		'[]', '[]', '{}', 'flow', $3, '2026-01-01', '2026-01-01') RETURNING id`, FL.Sales, w.People["admin"], FL.Doomed)
	chat := w.Insert(`INSERT INTO ai_chats (user_id, title, model_id, created_at, updated_at, kind, flow_id)
		VALUES ($1, 'about doomed', 'm', '2026-01-01', '2026-01-01', 'flow', $2) RETURNING id`, w.People["editor"], FL.Doomed)
	w.Exec(`INSERT INTO ai_chat_turns (chat_id, seq, question, messages, model_id, input_tokens, output_tokens, requests, created_at)
		VALUES ($1, 1, 'q', '[]', 'm', 1, 1, 1, '2026-01-01')`, chat)
	w.Exec(`INSERT INTO dbt_ai_texts (flow_id, flow_version, kind, key, text, model_id, created_at)
		VALUES ($1, 1, 'model', 'k', 't', 'm', '2026-01-01')`, FL.Doomed)
	w.Exec(`INSERT INTO flow_presence (flow_id, instance, user_id, email, full_name, since, seen) VALUES ($1, 'tab-1', $2, 'admin@x.test',
		'Person admin', 1, 1)`, FL.Alpha, w.People["admin"])
}
