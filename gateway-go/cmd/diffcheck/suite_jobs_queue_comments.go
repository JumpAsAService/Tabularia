package main

import "fmt"

// QC: what the comments' rounds seed.
var QC = struct {
	Groups map[string]int64
}{}

// services/flow_group_runner.py _cell_comments, one round of the queue at a time: a Pushmail
// cell whose report and dashboard have an AI comment. In stack B the comments are written by
// the AI service (/internal/ai/comments, reports.comments_for); with no model enabled they say
// so (no_provider) after the engine read their numbers, and the email goes all the same.
func init() {
	register(Suite{Module: "jobs_queue_comments", Seed: seedQueueComments, Steps: queueCommentsSteps})
}

func seedQueueComments(w *World) {
	w.Exec(`UPDATE runs SET status = 'FAILURE', error = 'left in the queue by another suite', finished_at = started_at
		WHERE kind = 'orchestration' AND status IN ('PENDING', 'STARTED')`)
	tidyRuns(w)
	admin, writer := w.People["admin"], w.People["writer"]
	main := w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at) VALUES ('qc-Main', '', NULL, $1, '2026-01-01')
		RETURNING id`, admin)
	for _, c := range []string{"edit", "connect"} {
		w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, NULL, $3)`, main, writer, c)
	}
	smtp := w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username, password_encrypted,
		database, db_schema, extra, created_at, updated_at) VALUES ('qc-smtp', '', $1, $2, 'smtp', 'h.x.test', NULL, 'u', 'enc', 'db', '',
		'{"allowed_domains": ["x.test"]}', '2026-01-01', '2026-01-01') RETURNING id`, main, admin)
	src := w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions,
		kind, created_at, updated_at) VALUES ('qc src', '', $1, $2, 'data-prep', 'datasets/qc/src.parquet', '[]', '[]', '{}', 'upload',
		'2026-01-01', '2026-01-01') RETURNING id`, main, admin)
	report := w.Insert(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ('qc report', '', $1, $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, main, admin)
	w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, 'created',
		'2026-01-01', $3)`, report, fmt.Sprintf(`{"v": 1, "locale": "it", "parameters": [{"name": "region", "type": "text", "default": "Nord"}],
		"datasets": [{"name": "o", "source": {"kind": "datasource", "datasource_id": %d}}], "sections": {"body": {"items": [
		{"id": "k1", "type": "kpi", "x": 0, "y": 0, "w": 50, "h": 20, "dataset": "o", "value": "SUM([n])"},
		{"id": "ai", "type": "comment", "x": 0, "y": 30, "w": 100, "h": 30, "reads": ["k1"], "instruction": "Say {region}"}]}}}`, src), admin)
	board := w.Insert(`INSERT INTO dashboards (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ('qc board', '', $1, $2, 1, '2026-01-01', '2026-01-01') RETURNING id`, main, admin)
	w.Exec(`INSERT INTO dashboard_versions (dashboard_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, 'created',
		'2026-01-01', $3)`, board, fmt.Sprintf(`{"v": 1, "page": {"size": "A4"}, "datasets": [{"name": "o", "source": {"kind": "datasource",
		"datasource_id": %d}}], "tiles": [{"id": "k1", "type": "kpi", "grid": {"x": 0, "y": 0, "w": 2, "h": 2}, "dataset": "o", "value": "1"},
		{"id": "ai", "type": "comment", "grid": {"x": 2, "y": 0, "w": 4, "h": 3}, "reads": ["k1"]}]}`, src), admin)
	QC.Groups = map[string]int64{}
	group := func(key, reportsJSON, boardsJSON string) {
		QC.Groups[key] = w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, '2026-01-01', '2026-01-01') RETURNING id`, "qc "+key, main, admin,
			fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 21, "pushmail": {"label": "Commented", "connectionId": %d, "to": ["boss@x.test"],
			"subject": "Sales", "body": "Hi", "attachments": [], "reports": %s, "dashboards": %s}}]]}`, smtp, reportsJSON, boardsJSON))
	}
	group("both", fmt.Sprintf(`[{"reportId": %d, "format": "pdf", "params": {"region": "Sud"}, "fileName": "r.pdf", "inBody": true}]`, report),
		fmt.Sprintf(`[{"dashboardId": %d, "format": "png", "params": {}, "fileName": "b.png"}]`, board))
	group("excel", fmt.Sprintf(`[{"reportId": %d, "format": "xlsx", "params": {}, "fileName": "r.xlsx", "inBody": false}]`, report), `[]`)
	group("excelbody", fmt.Sprintf(`[{"reportId": %d, "format": "xlsx", "params": {}, "fileName": "r.xlsx", "inBody": true}]`, report), `[]`)
}

func queueCommentsSteps(w *World) []Step {
	at := "2026-04-02T09:00:00Z"
	engine := []EngineRule{
		{Method: "POST", Path: "^/reports/tiles$", Body: map[string]any{"tiles": map[string]any{"k1": map[string]any{"type": "kpi", "text": "3"}}}},
		{Method: "POST", Path: "^/db/pushmail$", Body: map[string]any{"task_id": "t-qc-mail"}},
		{Method: "GET", Path: "^/tasks/t-qc-mail$", Body: map[string]any{"status": "SUCCESS", "result": map[string]any{"rows_written": 0,
			"email": map[string]any{"ok": true}}}},
		{Method: "GET", Path: "^/engines$", Body: []any{map[string]any{"id": "duckdb", "available": true}}},
		{Method: "GET", Path: "^/tasks/", Body: map[string]any{"status": "PENDING"}},
	}
	tick := func(name, group, by string, rules []EngineRule) Step {
		return Step{Name: name, Method: "TICK", Path: "queue", Tick: "queue", Now: at, Engine: rules,
			Before: func(w *World) { orchGroup(w, QC.Groups[group], "manual", by) }, Unordered: []string{"", "taken", "refused"},
			// the comments of a cell are written side by side; the rows they read are named by a random word
			EngineUnordered: true, EngineScrub: []string{`[0-9a-f]{32}`}}
	}
	// no model the comments may use: they say so, on both sides, without a provider
	noModel := Step{Name: "no AI model enabled for the comments", Method: "GET", Path: "/health", Before: func(w *World) {
		w.Exec(`DROP TABLE IF EXISTS qc_models_kept`)
		w.Exec(`CREATE TABLE qc_models_kept AS SELECT model_id FROM ai_models WHERE enabled`)
		w.Exec(`UPDATE ai_models SET enabled = false`)
	}}
	restore := Step{Name: "the AI models as they were", Method: "GET", Path: "/health", Before: func(w *World) {
		w.Exec(`UPDATE ai_models SET enabled = true WHERE model_id IN (SELECT model_id FROM qc_models_kept)`)
		w.Exec(`DROP TABLE qc_models_kept`)
	}}
	failing := append([]EngineRule{{Method: "POST", Path: "^/reports/tiles$", Status: 500, Body: map[string]any{"detail": "boom"}}}, engine...)
	return []Step{
		noModel,
		tick("a Pushmail cell whose report and dashboard have an AI comment", "both", "admin", engine),
		tick("its AI comments when the engine cannot read their numbers", "both", "admin", failing),
		tick("a report going as Excel, not in the body: no comment asked", "excel", "admin", engine),
		tick("a report going as Excel in the body: its comment asked", "excelbody", "writer", engine),
		restore,
		{Name: "the parquets' random names put away", Method: "TICK", Path: "queue.beat", Tick: "queue.beat", Now: at, Before: putAwayRandomKeys},
		tidyRunsStep,
	}
}
