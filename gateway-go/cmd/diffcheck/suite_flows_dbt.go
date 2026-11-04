package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// routes/flows.py: the dbt export of a flow (the project built by the gateway, compiled by the
// engine) and the download dialog's plan. Names carry the «dbt-» prefix.
var DBT = struct {
	seeded                                          bool
	Main, Hidden                                    int64
	PG, PGBare, MY, CH, ORA, OtherPG                int64
	Orders, Customers, NoSchema, MyT, ChT, OraT     int64
	Files, Big, Margin, Quoted                      int64
	Simple, Mixed, Upstream, Oracle, Scripted       int64
	NoKey, Text, List, NotJSON, NoOutputs, OnlyMail int64
	Foreach, Pivot, Incomplete, Loop, NoSource      int64
	Seed, Seeded, Unicode, CycleA, CycleB, Quote    int64
	Native, Tests, Nested, BadNode, Accented        int64
	ABOut, BAOut                                    int64
	ProviderAt                                      int64
	enabled                                         []string
}{}

func (w *World) seedDbt() {
	if DBT.seeded {
		return
	}
	DBT.seeded = true
	admin := w.People["admin"]
	folder := func(name string) int64 {
		return w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
			VALUES ($1, '', NULL, $2, '2026-01-01') RETURNING id`, name, admin)
	}
	DBT.Main = folder("dbt-Main")
	DBT.Hidden = folder("dbt-Hidden")
	sales := Groups["Sales"]
	w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, NULL, $2, 'view')`, DBT.Main, sales)
	w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, NULL, $2, 'view')`, DBT.Main, Groups["Watchers"])

	conn := func(name, kind, host string, port *int64, database, schema, extra string) int64 {
		return w.Insert(`INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, $5, $6, 'reader', '', $7, $8, $9, '2026-01-01', '2026-01-01') RETURNING id`,
			name, DBT.Main, admin, kind, host, port, database, schema, extra)
	}
	DBT.PG = conn("dbt-erp", "postgresql", "db.x.test", nil, "erp", "sales",
		`{"tls": "verify", "ca_cert": " -----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE----- "}`)
	DBT.PGBare = conn("dbt-bare", "postgresql", "bare.x.test", ptr(int64(6543)), "bare", "", `{}`)
	DBT.MY = conn("dbt-shop", "mysql", "my.x.test", nil, "shop", "", `{"tls": "trust"}`)
	DBT.CH = conn("dbt-crm", "clickhouse", "ch.x.test", nil, "analytics", "", `{"tls": "trust"}`)
	DBT.ORA = conn("dbt-ledger", "oracle", "ora.x.test", nil, "LEDGER", "FIN", `{}`)
	DBT.OtherPG = conn("dbt-other", "postgresql", "other.x.test", nil, "other", "", `not json`)

	cols := `[{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}, {"name": "importo", "dtype": "Float64"}]`
	ds := func(name, key string, project int64, rows *int64, kind string, connID *int64, sourceType, sourceRef *string, flowID *int64,
		columns, descriptions string) int64 {
		return w.Insert(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys,
			column_descriptions, kind, connection_id, source_type, source_ref, flow_id, created_at, updated_at)
			VALUES ($1, $2, $3, $4, 'data-prep', $5, $6, $7, '[]', $8, $9, $10, $11, $12, $13, '2026-01-01', '2026-01-01') RETURNING id`,
			name, "about "+name, project, admin, key, rows, columns, descriptions, kind, connID, sourceType, sourceRef, flowID)
	}
	table, sql := ptr("table"), ptr("sql")
	DBT.Orders = ds("dbt-orders", "datasets/dbt/orders.parquet", DBT.Main, ptr(int64(1200)), "database", &DBT.PG, table,
		ptr("vendite.ordini"), nil, cols, `{"canale": "Sales channel", "id": ""}`)
	DBT.Customers = ds("dbt-customers", "datasets/dbt/customers.parquet", DBT.Main, nil, "database", &DBT.PG, sql,
		ptr("  SELECT id, nome FROM crm.clienti WHERE nome <> 'x';  "), nil, `[{"name": "id", "dtype": "Int64"}, {"name": "nome"}]`, `{}`)
	DBT.NoSchema = ds("dbt-bare-t", "datasets/dbt/bare.parquet", DBT.Main, nil, "database", &DBT.PGBare, table, ptr("clienti"), nil, cols, `not json`)
	DBT.MyT = ds("dbt-my", "datasets/dbt/my.parquet", DBT.Main, nil, "database", &DBT.MY, table, ptr("listini"), nil, cols, `{}`)
	DBT.ChT = ds("dbt-ch", "datasets/dbt/ch.parquet", DBT.Main, nil, "database", &DBT.CH, table, ptr("analytics.events"), nil, cols, `{}`)
	DBT.OraT = ds("dbt-ora", "datasets/dbt/ora.parquet", DBT.Main, nil, "database", &DBT.ORA, table, ptr("FIN.CONTI"), nil, cols, `{}`)
	DBT.Files = ds("dbt-files", "datasets/dbt/files.parquet", DBT.Main, ptr(int64(10)), "database", &DBT.PG, ptr("sftp"),
		ptr(`{"folder": "/in"}`), nil, cols, `{}`)
	DBT.Big = ds("dbt-big", "datasets/dbt/big.parquet", DBT.Main, ptr(int64(1234567)), "upload", nil, nil, nil, nil, cols, `{}`)
	DBT.Quoted = ds("dbt-quoted", "datasets/dbt/quoted.parquet", DBT.Main, nil, "database", &DBT.MY, table, ptr(`it's`), nil, cols, `{}`)

	flow := func(name string, project int64, definition string, engine string, production *string) int64 {
		return w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, production_engine,
			created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, '2026-01-02', '2026-01-02') RETURNING id`,
			name, "the "+name, project, admin, definition, engine, production)
	}
	src := func(id string, dsID int64) string {
		return fmt.Sprintf(`{"id": %q, "type": "source", "data": {"datasourceId": %d, "label": "src %s"}}`, id, dsID, id)
	}
	op := func(id, kind, params string) string {
		return fmt.Sprintf(`{"id": %q, "type": "operation", "data": {"opType": %q, "label": "step %s", "params": %s}}`, id, kind, id, params)
	}
	edge := func(a, b string) string { return fmt.Sprintf(`{"source": %q, "target": %q}`, a, b) }
	right := func(a, b string) string {
		return fmt.Sprintf(`{"source": %q, "target": %q, "targetHandle": "right"}`, a, b)
	}
	def := func(nodes, edges []string) string {
		b, _ := json.Marshal(nil)
		_ = b
		out := `{"nodes": [`
		for i, n := range nodes {
			if i > 0 {
				out += ", "
			}
			out += n
		}
		out += `], "edges": [`
		for i, e := range edges {
			if i > 0 {
				out += ", "
			}
			out += e
		}
		return out + "]}"
	}
	main := DBT.Main
	pub := func(id, name string, extra string) string {
		return fmt.Sprintf(`{"id": %q, "type": "output", "data": {"destType": "datasource", "name": %q, "projectId": %d%s}}`, id, name, main, extra)
	}
	// orders → filter → compute → [group_by → «dbt-margin»] and [→ table report.righe, append] and an email
	DBT.Simple = flow("dbt Margin", main, def([]string{
		src("src", DBT.Orders),
		op("f", "filter", `{"column": "canale", "operator": "eq", "value": "web"}`),
		op("c", "compute", `{"columns": [{"name": "netto", "expr": "importo * 0.8"}]}`),
		op("g", "group_by", `{"by": ["canale"], "aggregations": [{"column": "netto", "func": "sum", "alias": "tot"}]}`),
		pub("o1", "dbt-margin", `, "overwrite": true, "sortKeys": [" canale ", "", "tot"], "timezone": "Europe/Rome"`),
		fmt.Sprintf(`{"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": " report.righe ", "mode": "append", "postSql": "ANALYZE report.righe"}}`, DBT.PG),
		fmt.Sprintf(`{"id": "o4", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "public.full", "mode": "replace"}}`, DBT.PG),
		fmt.Sprintf(`{"id": "o5", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "elsewhere", "mode": "replace"}}`, DBT.MY),
		`{"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": 99, "emailTo": ["a@x.test"]}}`,
	}, []string{edge("src", "f"), edge("f", "c"), edge("c", "g"), edge("g", "o1"), edge("c", "o2"), edge("c", "o4"), edge("f", "o5"), edge("c", "o3")}),
		"duckdb", ptr("chdb"))
	DBT.Margin = ds("dbt-margin", "datasets/dbt/margin.parquet", main, ptr(int64(5)), "flow", nil, nil, nil, &DBT.Simple,
		`[{"name": "canale", "dtype": "String"}, {"name": "tot", "dtype": "Float64"}]`, `{"tot": "The total"}`)
	w.Exec(`INSERT INTO data_contracts (datasource_id, version, document, enabled, status, errors, warnings, created_by, updated_by,
		created_at, updated_at) VALUES ($1, 2, $2, true, 'pending', 0, 0, $3, $3, '2026-02-01', '2026-02-01')`, DBT.Margin,
		`{"description": "Margins by channel", "rules": [
			{"id": "r1", "kind": "not_null", "severity": "error", "column": "canale"},
			{"id": "r2", "kind": "unique", "severity": "warning", "columns": ["canale", "tot"]},
			{"id": "r3", "kind": "unique", "column": "tot"},
			{"id": "r4", "kind": "accepted_values", "column": "canale", "values": ["web", "shop"]},
			{"id": "r5", "kind": "range", "column": "tot", "min": 0, "max": 1e9, "severity": "warning"},
			{"id": "r6", "kind": "column", "column": "tot", "dtype": "Float64"},
			{"id": "r7", "kind": "column", "column": "nuova", "dtype": "String"},
			{"id": "r8", "kind": "freshness", "max_age_hours": 24.0},
			{"id": "r9", "kind": "pattern", "column": "canale", "regex": "^[a-z]+$"},
			{"id": "r10", "kind": "unique", "columns": []},
			{"id": "r11", "kind": "row_count", "min": 1}]}`, admin)
	w.Exec(`INSERT INTO flow_versions (flow_id, version, definition, note, created_at, created_by)
		VALUES ($1, 1, '{}', 'created', '2026-01-02 10:00:00', $2), ($1, 2, '{}', 'edited', '2026-01-03 11:22:33', $2)`, DBT.Simple, admin)
	w.Exec(`INSERT INTO dbt_ai_texts (flow_id, flow_version, kind, key, text, model_id, created_at) VALUES
		($1, 2, 'describe', 'flow:1@v2', 'What it does', 'gpt-x (Zeta)', '2026-01-01'),
		($1, 2, 'describe', 'col:x', '', 'zzz', '2026-01-01'),
		($1, 2, 'translate', 'sql:abc', 'SELECT 1', '', '2026-01-01'),
		($1, 2, 'claim', 'describe', '1.0', 'claimer', '2026-01-01'),
		($1, 1, 'translate', 'sql:old', 'SELECT 2', 'old (P)', '2026-01-01')`, DBT.Simple)

	// a join of two databases: native refuses, the federated and ClickHouse targets read both
	DBT.Mixed = flow("dbt Mixed", main, def([]string{
		src("a", DBT.Orders), src("b", DBT.MyT), src("q", DBT.Customers),
		op("j", "join", `{"how": "left", "left_on": ["id"], "right_on": ["id"]}`),
		op("u", "union", `{"by_name": true}`),
		pub("o", "dbt-mixed", ""),
	}, []string{edge("a", "j"), right("b", "j"), edge("j", "u"), right("q", "u"), edge("u", "o")}), "polars", nil)

	// another flow's output, read downstream, and an S3 output with placeholders
	DBT.Upstream = flow("dbt Downstream", main, def([]string{
		src("m", DBT.Margin),
		op("s", "sort", `{"by": ["tot"]}`),
		fmt.Sprintf(`{"id": "o", "type": "output", "data": {"destType": "s3", "connectionId": 1, "s3Key": "exports/{flow}/{date:%%Y}-margins.csv", "s3Format": "csv"}}`),
	}, []string{edge("m", "s"), edge("s", "o")}), "clickhouse", nil)

	DBT.Oracle = flow("dbt Ledger", main, def([]string{src("a", DBT.OraT), src("b", DBT.Orders), op("j", "join", `{"on": ["id"]}`),
		pub("o", "dbt-ledger", "")}, []string{edge("a", "j"), right("b", "j"), edge("j", "o")}), "duckdb", nil)

	DBT.Scripted = flow("dbt Scripted", main, def([]string{src("a", DBT.Orders),
		`{"id": "py", "type": "script", "data": {"label": "Clean"}}`, `{"id": "box", "type": "container", "data": {}}`,
		pub("o", "dbt-scripted", "")}, []string{edge("a", "py"), edge("py", "o"), edge("a", "box")}), "duckdb", nil)
	DBT.Native = flow("dbt Native", main, def([]string{src("a", DBT.Orders), src("b", DBT.Customers),
		op("u", "union", `{}`), op("l", "limit", `{"n": 5}`),
		pub("o", "dbt-native", ""),
		fmt.Sprintf(`{"id": "t", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "mart.facts", "mode": "replace"}}`, DBT.PG),
		fmt.Sprintf(`{"id": "t2", "type": "output", "data": {"destType": "database", "connectionId": "%d", "table": "facts_two", "mode": "append"}}`, DBT.PG),
		fmt.Sprintf(`{"id": "t3", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "x.%s", "mode": "replace"}}`, DBT.PG,
			"è_ùàòìéèùàòìéèùàòìéèùàòìéèùàòìéèùàòìé"),
		fmt.Sprintf(`{"id": "t4", "type": "output", "data": {"destType": "database", "connectionId": %d, "table": "mart.facts4", "mode": "append"}}`, DBT.OtherPG),
		`{"id": "t5", "type": "output", "data": {"destType": "database", "connectionId": "abc", "table": "mart.facts5"}}`,
	}, []string{edge("a", "u"), right("b", "u"), edge("u", "l"), edge("l", "o"), edge("l", "t"), edge("u", "t2"), edge("a", "t3"),
		edge("a", "t4"), edge("a", "t5")}), "duckdb", nil)

	DBT.NoKey = flow("dbt NoKey", main, `{"nodes": [{"type": "output"}], "edges": []}`, "duckdb", nil)
	DBT.Text = flow("dbt Text", main, `{"nodes": "abc", "edges": []}`, "duckdb", nil)
	DBT.List = flow("dbt List", main, `[]`, "duckdb", nil)
	DBT.NotJSON = flow("dbt NotJSON", main, `  nope`, "duckdb", nil)
	DBT.BadNode = flow("dbt BadNode", main, `{"nodes": [{"id": "o", "type": "output", "data": "text"}], "edges": []}`, "duckdb", nil)
	DBT.NoOutputs = flow("dbt NoOutputs", main, def([]string{src("a", DBT.Orders)}, nil), "duckdb", nil)
	DBT.OnlyMail = flow("dbt OnlyMail", main, def([]string{src("a", DBT.Orders),
		`{"id": "m", "type": "output", "data": {"destType": "email", "connectionId": 1}}`}, []string{edge("a", "m")}), "duckdb", nil)
	DBT.Foreach = flow("dbt Foreach", main, def([]string{src("a", DBT.Orders), src("d", DBT.MyT),
		`{"id": "fe", "type": "foreach", "data": {"opType": "foreach", "params": {"var": "x"}}}`,
		`{"id": "in", "type": "operation", "parentNode": "fe", "data": {"opType": "limit", "params": {"n": 1}}}`,
		pub("o", "dbt-fe", "")}, []string{edge("a", "fe"), right("d", "fe"), edge("fe", "o")}), "duckdb", nil)
	DBT.Pivot = flow("dbt Pivot", main, def([]string{src("a", DBT.Orders),
		op("p", "pivot", `{"index": ["id"], "on": "canale", "values": "importo"}`), pub("o", "dbt-pivot", "")},
		[]string{edge("a", "p"), edge("p", "o")}), "duckdb", nil)
	DBT.Incomplete = flow("dbt Incomplete", main, def([]string{src("a", DBT.Orders),
		op("f", "filter", `{"column": "canale", "operator": "between", "value": [1]}`), op("g", "group_by", `{"by": ["a"], "aggregations": [{"func": "sum"}]}`),
		op("j", "join", `{"right": {"x": 1}, "left_on": ["a", "b"], "right_on": "c"}`),
		pub("o", "dbt-incomplete", ""), pub("o2", "dbt-incomplete-2", ""), pub("o3", "dbt-incomplete-3", "")},
		[]string{edge("a", "f"), edge("f", "o"), edge("a", "g"), edge("g", "o2"), edge("a", "j"), edge("j", "o3")}), "duckdb", nil)
	DBT.Loop = flow("dbt Loop", main, def([]string{src("a", DBT.Orders), op("j", "join", `{"on": ["id"]}`), pub("o", "dbt-loop", "")},
		[]string{edge("a", "j"), right("j", "j"), edge("j", "o")}), "duckdb", nil)
	DBT.NoSource = flow("dbt NoSource", main, def([]string{op("f", "limit", `{"n": 1}`),
		`{"id": "o", "type": "output", "data": {"destType": "sftp", "sftpFileName": "x.csv"}}`}, []string{edge("f", "o")}), "duckdb", nil)
	DBT.Seed = flow("dbt Seeds", main, def([]string{
		`{"id": "u", "type": "source", "data": {"parquetKey": "uploads/dbt/sales_2026.v2.parquet", "filename": "Sales 2026.xlsx", "bucket": "other-bucket"}}`,
		`{"id": "u2", "type": "source", "data": {"parquetKey": "uploads/dbt/raw.parquet", "filters": [{"column": "x", "operator": "is_null"}, {"column": "", "operator": "eq", "value": 1}, {"column": "y", "operator": "in", "value": []}, {"column": "z", "operator": "between", "value": [1, 2]}], "sample": {"mode": "first", "rows": 10}}}`,
		src("f", DBT.Files),
		op("un", "union", `{}`), op("un2", "union", `{}`),
		`{"id": "o", "type": "output", "data": {"destType": "sftp", "connectionId": 3, "sftpFolder": " /out ", "sftpFileName": "{date:%Y}_report-{run_id}.csv", "timezone": " UTC "}}`,
	}, []string{edge("u", "un"), right("u2", "un"), edge("un", "un2"), right("f", "un2"), edge("un2", "o")}), "duckdb", nil)
	DBT.Seeded = flow("dbt Big", main, def([]string{src("a", DBT.Big), pub("o", "dbt-big-out", "")}, []string{edge("a", "o")}), "duckdb", nil)
	DBT.Unicode = flow("Vendite città Łódź 销售 — x/y", main, def([]string{src("a", DBT.Orders), pub("o", "dbt-ü", "")},
		[]string{edge("a", "o")}), "bigquery", nil)
	// two flows each reading what the other publishes
	DBT.CycleA = flow("dbt CycleA", main, "{}", "duckdb", nil)
	DBT.CycleB = flow("dbt CycleB", main, "{}", "duckdb", nil)
	DBT.ABOut = ds("dbt-a-out", "datasets/dbt/a.parquet", main, nil, "flow", nil, nil, nil, &DBT.CycleA, cols, `{}`)
	DBT.BAOut = ds("dbt-b-out", "datasets/dbt/b.parquet", main, nil, "flow", nil, nil, nil, &DBT.CycleB, cols, `{}`)
	w.Exec(`UPDATE flows SET definition = $1 WHERE id = $2`, def([]string{src("s", DBT.BAOut), pub("o", "dbt-a-out", "")},
		[]string{edge("s", "o")}), DBT.CycleA)
	w.Exec(`UPDATE flows SET definition = $1 WHERE id = $2`, def([]string{src("s", DBT.ABOut), pub("o", "dbt-b-out", "")},
		[]string{edge("s", "o")}), DBT.CycleB)
	DBT.Quote = flow("dbt Quote", main, def([]string{src("a", DBT.Quoted), pub("o", "dbt-quote", "")}, []string{edge("a", "o")}), "duckdb", nil)
	DBT.Tests = flow("dbt Accents", main, def([]string{src("a", DBT.NoSchema), src("b", DBT.Orders), op("j", "join", `{"on": "id"}`),
		pub("o", "dbt-acc", ""), pub("o2", "dbt-acc", "")}, []string{edge("a", "j"), right("b", "j"), edge("j", "o"), edge("a", "o2")}), "duckdb", nil)
	DBT.Nested = flow("dbt Nested", main, def([]string{src("a", DBT.Orders), src("b", DBT.Margin), src("c", DBT.Customers),
		op("j", "join", `{"on": ["id"]}`), op("j2", "join", `{"on": ["id"]}`), pub("o", "dbt-nested", `, "sortKeys": ["id"]`)},
		[]string{edge("a", "j"), right("b", "j"), edge("j", "j2"), right("c", "j2"), edge("j2", "o")}), "duckdb", nil)
	DBT.Accented = flow("dbt ClickHouse", main, def([]string{src("a", DBT.ChT), src("b", DBT.Customers), op("u", "union", `{}`),
		pub("o", "dbt-ch", "")}, []string{edge("a", "u"), right("b", "u"), edge("u", "o")}), "duckdb", nil)
	w.Exec(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('dbt Hidden', '', $1, $2, '{}', 'duckdb', '2026-01-01', '2026-01-01')`, DBT.Hidden, admin)

	w.Token("admin", "tab_dbtw_admin_write_000000", "write", time.Hour)
	w.Token("admin", "tab_dbtr_admin_read_0000000", "read", time.Hour)
	w.Token("writer", "tab_dbtx_writer_write_00000", "write", time.Hour)
	w.Token("service", "tab_dbts_service_write_0000", "write", time.Hour)
}

func init() {
	zip := EngineRule{Method: "POST", Path: "^/dbt/export$", Text: "PK\x03\x04 a dbt project", Type: "application/zip"}
	register(Suite{
		Module: "flows_dbt",
		Seed:   func(w *World) { w.seedDbt() },
		Engine: []EngineRule{zip},
		Steps: func(w *World) []Step {
			var s []Step
			f := func(format string, args ...any) string { return fmt.Sprintf(format, args...) }
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			export := func(id int64) string { return f("/flows/%d/export/dbt", id) }
			plan := func(id int64) string { return f("/flows/%d/export/dbt/plan", id) }

			// ── who may export ──
			for _, p := range []string{"admin", "admin_group", "editor", "obs_flag", "obs_group", "writer", "service", "inactive"} {
				add("export · "+p, "GET", export(DBT.Simple), w.As(p), nil)
				add("plan · "+p, "GET", plan(DBT.Simple), w.As(p), nil)
			}
			add("export · nobody", "GET", export(DBT.Simple), nil, nil)
			add("plan · nobody", "GET", plan(DBT.Simple), nil, nil)
			add("post · nobody", "POST", export(DBT.Simple), nil, map[string]any{})
			add("post · nobody, a bad body", "POST", export(DBT.Simple), nil, map[string]any{"target": "x"})
			for _, tok := range []string{"tab_dbtw_admin_write_000000", "tab_dbtr_admin_read_0000000", "tab_dbtx_writer_write_00000",
				"tab_dbts_service_write_0000"} {
				add("export · token "+tok[:8], "GET", export(DBT.Simple), Bearer(tok), nil)
				add("plan · token "+tok[:8], "GET", plan(DBT.Simple), Bearer(tok), nil)
				add("post · token "+tok[:8], "POST", export(DBT.Simple), Bearer(tok), map[string]any{"target": "native"})
			}
			add("export · hidden folder, admin", "GET", export(DBT.Seeded+100000), w.As("admin"), nil)
			add("export · no such flow", "GET", export(999999), w.As("admin"), nil)
			add("export · no such flow, editor", "GET", export(999999), w.As("editor"), nil)
			add("export · not a number", "GET", "/flows/abc/export/dbt", w.As("admin"), nil)
			add("export · not a number and a bad target", "GET", "/flows/abc/export/dbt?target=x", w.As("admin"), nil)
			add("export · past an int64", "GET", "/flows/99999999999999999999/export/dbt", w.As("admin"), nil)
			add("plan · not a number", "GET", "/flows/abc/export/dbt/plan", w.As("admin"), nil)
			add("plan · no such flow", "GET", plan(999999), w.As("admin"), nil)

			// ── the targets, every flow ──
			flows := []struct {
				name string
				id   int64
			}{{"Simple", DBT.Simple}, {"Mixed", DBT.Mixed}, {"Upstream", DBT.Upstream}, {"Oracle", DBT.Oracle}, {"Scripted", DBT.Scripted},
				{"Native", DBT.Native}, {"NoKey", DBT.NoKey}, {"Text", DBT.Text}, {"List", DBT.List}, {"NotJSON", DBT.NotJSON},
				{"BadNode", DBT.BadNode}, {"NoOutputs", DBT.NoOutputs}, {"OnlyMail", DBT.OnlyMail}, {"Foreach", DBT.Foreach},
				{"Pivot", DBT.Pivot}, {"Incomplete", DBT.Incomplete}, {"Loop", DBT.Loop}, {"NoSource", DBT.NoSource}, {"Seeds", DBT.Seed},
				{"Big", DBT.Seeded}, {"Unicode", DBT.Unicode}, {"CycleA", DBT.CycleA}, {"Quote", DBT.Quote}, {"Accents", DBT.Tests},
				{"Nested", DBT.Nested}, {"ClickHouse", DBT.Accented}}
			for _, fl := range flows {
				add("plan · "+fl.name, "GET", plan(fl.id), w.As("admin"), nil)
				for _, t := range []string{"", "?target=duckdb", "?target=native", "?target=clickhouse"} {
					add("export · "+fl.name+" "+t, "GET", export(fl.id)+t, w.As("admin"), nil)
				}
			}
			for _, q := range []string{"?target=DuckDB", "?target=", "?target=duckdb&target=nope", "?target=nope&target=native", "?other=1"} {
				add("export · target "+q, "GET", export(DBT.Simple)+q, w.As("admin"), nil)
			}
			for _, lang := range []string{"it", "de-CH,de;q=0.9", "fr", "es", "xx", ""} {
				s = append(s, Step{Name: "plan · language " + lang, Method: "GET", Path: plan(DBT.Oracle), Auth: w.As("admin"),
					Header: map[string]string{"Accept-Language": lang}})
				s = append(s, Step{Name: "export · language " + lang, Method: "GET", Path: export(DBT.Foreach), Auth: w.As("admin"),
					Header: map[string]string{"Accept-Language": lang}})
			}

			// ── the dialog's options ──
			srcKey := f("%d:vendite", DBT.PG)
			oraKey := f("%d:FIN", DBT.ORA)
			bodies := []struct {
				name string
				body any
				path int64
			}{
				{"empty", map[string]any{}, DBT.Simple},
				{"native, prefix, schema", map[string]any{"target": "native", "prefix": "tab_", "schema": "dbt_mart"}, DBT.Native},
				{"folder package drops the schema", map[string]any{"target": "native", "package": "folder", "folder": "mart", "schema": "x"}, DBT.Native},
				{"materializations", map[string]any{"materializations": map[string]any{f("%d:o1", DBT.Simple): "view",
					f("%d:o2", DBT.Simple): "incremental", f("%d:nope", DBT.Simple): "table"}}, DBT.Simple},
				{"a materialization not allowed", map[string]any{"materializations": map[string]any{f("%d:o1", DBT.Simple): "incremental"}}, DBT.Simple},
				{"no tests, no emails", map[string]any{"tests": false, "emails": false, "layers": true}, DBT.Simple},
				{"source names", map[string]any{"sources": map[string]any{srcKey: map[string]any{"name": "erp_sales"}}}, DBT.Simple},
				{"two sources one name", map[string]any{"sources": map[string]any{srcKey: map[string]any{"name": "same"},
					f("%d:", DBT.MY): map[string]any{"name": "same"}}}, DBT.Mixed},
				{"declared in a project: dropped", map[string]any{"target": "clickhouse", "sources": map[string]any{oraKey: map[string]any{"declared": true}}}, DBT.Oracle},
				{"declared in a folder", map[string]any{"target": "clickhouse", "package": "folder", "sources": map[string]any{oraKey: map[string]any{"declared": true}}}, DBT.Oracle},
				{"clickhouse, folder, another key declared", map[string]any{"target": "clickhouse", "package": "folder",
					"sources": map[string]any{"9:x": map[string]any{"declared": true}}}, DBT.Oracle},
				{"populate by name", map[string]any{"schema_": "abc"}, DBT.Native},
				{"extra key", map[string]any{"nope": 1}, DBT.Simple},
				{"bad prefix", map[string]any{"prefix": "1tab"}, DBT.Simple},
				{"bad folder", map[string]any{"folder": "A B"}, DBT.Simple},
				{"bad target", map[string]any{"target": "spark"}, DBT.Simple},
				{"bad source name", map[string]any{"sources": map[string]any{"1:x": map[string]any{"name": "Bad Name"}}}, DBT.Simple},
				{"bad materialization", map[string]any{"materializations": map[string]any{"1:x": "snapshot"}}, DBT.Simple},
				{"not an object", []any{1}, DBT.Simple},
				{"null", nil, DBT.Simple},
				{"strings for booleans", map[string]any{"tests": "no", "emails": "yes", "layers": 1}, DBT.Simple},
				{"clickhouse on the unicode flow", map[string]any{"target": "clickhouse"}, DBT.Unicode},
				{"native on Mixed", map[string]any{"target": "native"}, DBT.Mixed},
			}
			for _, b := range bodies {
				add("post · "+b.name, "POST", export(b.path), w.As("admin"), b.body)
			}
			add("post · editor", "POST", export(DBT.Simple), w.As("editor"), map[string]any{})
			add("post · bad path and bad body", "POST", "/flows/abc/export/dbt", w.As("admin"), map[string]any{"target": "x"})
			s = append(s, Step{Name: "post · not JSON", Method: "POST", Path: export(DBT.Simple), Auth: w.As("admin"),
				Raw: []byte("{nope"), ContentType: "application/json"})
			s = append(s, Step{Name: "post · not JSON, nobody", Method: "POST", Path: export(DBT.Simple), Raw: []byte("{nope"),
				ContentType: "application/json"})
			s = append(s, Step{Name: "post · no body", Method: "POST", Path: export(DBT.Simple), Auth: w.As("admin")})

			// ── what the engine answers ──
			engine := []struct {
				name string
				rule EngineRule
			}{
				{"a coded refusal", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "translation_failed", "params": map[string]any{"dialect": "postgres", "detail": "no ARRAY_AGG"}, "message": "m"}}}},
				{"text not translatable", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "text_not_translatable", "params": map[string]any{"step": "c", "dialect": "ClickHouse", "detail": "x"}}}}},
				{"an AI reason", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "ai_translation_invalid", "params": map[string]any{"model": "m", "reason": "columns", "got": "a", "expected": "b"}}}}},
				{"an AI reason, missing a parameter", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "ai_translation_invalid", "params": map[string]any{"model": "m", "reason": "columns"}}}}},
				{"an AI reason, the model a list", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "ai_translation_invalid", "params": map[string]any{"model": []any{1}, "reason": "x"}}}}},
				{"a parameter missing", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "translation_failed", "params": map[string]any{"dialect": "postgres"}, "message": "the engine's words"}}}},
				{"a parameter missing, no message", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "translation_failed", "params": map[string]any{}}}}},
				{"internal without detail", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 500, Body: map[string]any{"detail": map[string]any{
					"code": "internal", "message": "boom"}}}},
				{"an unknown code", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 409, Body: map[string]any{"detail": map[string]any{
					"code": "brand_new", "message": "said in English"}}}},
				{"an unknown code that is a number", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": 7}}}},
				{"params that are not a dict", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 422, Body: map[string]any{"detail": map[string]any{
					"code": "internal", "params": []any{1}}}}},
				{"an old engine's text", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 400, Body: map[string]any{"detail": "old words"}}},
				{"a list detail", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 400, Body: map[string]any{"detail": []any{"a", 1, nil}}}},
				{"no detail", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 503, Body: map[string]any{"error": "x"}}},
				{"not JSON", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 500, Text: "Internal Server Error", Type: "text/plain"}},
				{"a JSON list", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 500, Body: []any{1}}},
				{"a 202", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 202, Body: map[string]any{"detail": "later"}}},
				{"a 302", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 302, Text: "", Headers: map[string]string{"Location": "/x"}}},
				{"a 200 that is JSON", EngineRule{Method: "POST", Path: "^/dbt/export$", Status: 200, Body: map[string]any{"ok": true}}},
			}
			for _, e := range engine {
				for _, lang := range []string{"en", "it"} {
					s = append(s, Step{Name: "engine · " + e.name + " · " + lang, Method: "GET", Path: export(DBT.Simple), Auth: w.As("admin"),
						Engine: []EngineRule{e.rule}, Header: map[string]string{"Accept-Language": lang}})
				}
			}

			// ── the AI: the plan says what is there; an export asking for it goes to the AI service ──
			s = append(s, Step{Name: "plan · a model enabled", Method: "GET", Path: plan(DBT.Simple), Auth: w.As("admin"),
				Before: func(w *World) {
					w.a.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) + 1 FROM ai_providers`).Scan(&DBT.ProviderAt)
					w.Exec(`INSERT INTO ai_providers (name, kind, base_url, secret_encrypted, active, created_at, updated_at)
						VALUES ('dbt-prov', 'openai_compatible', 'http://dbt.test/v1', 'x', true, '2026-01-01', '2026-01-01')`)
					w.Exec(`INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ($1, true, '2026-01-01')`,
						"provider:"+strconv.FormatInt(DBT.ProviderAt, 10)+"/dbt-model")
				}})
			s = append(s, Step{Name: "plan · no model enabled", Method: "GET", Path: plan(DBT.Simple), Auth: w.As("admin"),
				Before: func(w *World) {
					DBT.enabled = nil
					rows, _ := w.a.db.Query(w.ctx, `SELECT model_id FROM ai_models WHERE enabled ORDER BY model_id`)
					for rows.Next() {
						var m string
						rows.Scan(&m)
						DBT.enabled = append(DBT.enabled, m)
					}
					rows.Close()
					w.Exec(`UPDATE ai_models SET enabled = false`)
				}})
			for _, b := range []map[string]any{{"ai_descriptions": true}, {"ai_translations": true}, {"ai_descriptions": true, "target": "nope"},
				{"ai_translations": "yes", "target": "native"}} {
				add(f("post · the AI %v", b), "POST", export(DBT.Simple), w.As("admin"), b)
			}
			s = append(s, Step{Name: "post · AI translations, the engine refusing a translation", Method: "POST", Path: export(DBT.Simple),
				Auth: w.As("admin"), JSON: map[string]any{"ai_translations": true}, Engine: []EngineRule{{Method: "POST", Path: "^/dbt/export$",
					Status: 422, Body: map[string]any{"detail": map[string]any{"code": "translation_failed", "params": map[string]any{
						"model": "margin", "sql": "SELECT 1", "source_dialect": "duckdb", "target_dialect": "postgres", "detail": "x"}}}}}})
			add("post · the AI, editor", "POST", export(DBT.Simple), w.As("editor"), map[string]any{"ai_descriptions": true})
			s = append(s, Step{Name: "plan · models back", Method: "GET", Path: plan(DBT.Simple), Auth: w.As("admin"),
				Before: func(w *World) {
					w.Exec(`UPDATE ai_models SET enabled = true WHERE model_id = ANY($1)`, DBT.enabled)
					w.Exec(`DELETE FROM ai_models WHERE model_id = $1`, "provider:"+strconv.FormatInt(DBT.ProviderAt, 10)+"/dbt-model")
					w.Exec(`DELETE FROM ai_providers WHERE name = 'dbt-prov'`)
				}})
			return s
		},
	})
}
