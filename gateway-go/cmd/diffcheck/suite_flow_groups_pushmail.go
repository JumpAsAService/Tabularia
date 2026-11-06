package main

import (
	"fmt"
	"net/http"
)

// PM: the reports and dashboards a Pushmail cell of a flow group may send (the flow groups'
// seed, fl-Sales and fl-Hidden).
var PM = struct {
	Report, HiddenReport, BlindReport, Board, HiddenBoard, BlindBoard, MailGroup int64
}{}

func (w *World) seedPushmailDocs() {
	w.seedFlowGroups()
	admin := w.People["admin"]
	report := func(name string, project, ds int64) int64 {
		id := w.Insert(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at)
			VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, name, project, admin)
		w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, 'created',
			'2026-01-01', $3)`, id, fmt.Sprintf(`{"v": 1, "locale": "it", "parameters": [{"name": "region", "type": "text", "default": "Nord"},
			{"name": "n", "type": "number"}], "datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": %d,
			"filters": [{"column": "region", "op": "=", "value": "{region}"}]}}], "sections": {"body": {"items": []}}}`, ds), admin)
		return id
	}
	board := func(name string, project, ds int64) int64 {
		id := w.Insert(`INSERT INTO dashboards (name, description, project_id, owner_id, current_version, created_at, updated_at)
			VALUES ($1, '', $2, $3, 1, '2026-01-01', '2026-01-01') RETURNING id`, name, project, admin)
		w.Exec(`INSERT INTO dashboard_versions (dashboard_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, 'created',
			'2026-01-01', $3)`, id, fmt.Sprintf(`{"v": 1, "page": {"size": "A4"}, "parameters": [{"name": "year", "type": "number"}],
			"tiles": [{"id": "t1", "title": "Orders", "grid": {"x": 0, "y": 0}, "type": "number", "dataset": "orders"}],
			"datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": %d}}]}`, ds), admin)
		return id
	}
	PM.Report = report("pm report", FL.Sales, FL.SalesDS)
	PM.HiddenReport = report("pm hidden report", FL.Hidden, FL.SalesDS)
	PM.BlindReport = report("pm blind report", FL.Sales, FL.HiddenDS) // seen, but its data is not readable
	PM.Board = board("pm board", FL.Sales, FL.SalesDS)
	PM.HiddenBoard = board("pm hidden board", FL.Hidden, FL.SalesDS)
	PM.BlindBoard = board("pm blind board", FL.Sales, FL.HiddenDS)
	PM.MailGroup = w.Insert(`INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
		VALUES ('pm full grid', 'g', $1, $2, $3, '2026-01-02', '2026-01-02 08:00:00.5') RETURNING id`, FL.Sales, admin,
		fmt.Sprintf(`{"steps": [[{"kind": "pushmail", "id": 3, "pushmail": {"label": "Full", "connectionId": %d, "to": ["a@x.it"],
		"attachments": [{"datasourceId": %d, "fileName": "s.xlsx"}], "reports": [{"reportId": %d, "format": "pdf", "params": {"region": "Sud"},
		"fileName": "r.pdf", "inBody": true}], "dashboards": [{"dashboardId": %d, "format": "png", "params": {}, "fileName": "b.png"}]}}]]}`,
			FL.Smtp, FL.SalesDS, PM.Report, PM.Board))
}

// routes/flow_groups.py _checked with Pushmail cells (services/pushmail.py pushmail_errors): what a
// save of a new or changed cell checks, with the rights of whoever saves.
func init() {
	register(Suite{
		Module: "flow_groups_pushmail",
		Seed:   func(w *World) { w.seedPushmailDocs() },
		Steps: func(w *World) []Step {
			var s []Step
			f := fmt.Sprintf
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			grid := func(cells ...any) map[string]any { return map[string]any{"steps": []any{cells}} }
			cell := func(id int, pm any) map[string]any {
				return map[string]any{"kind": "pushmail", "id": id, "pushmail": pm}
			}
			good := func(changes map[string]any) map[string]any {
				pm := map[string]any{"label": " Weekly ", "connectionId": FL.Smtp, "to": []any{"a@x.it"}, "cc": []any{" b@y.it ", ""},
					"subject": "s", "attachments": []any{map[string]any{"datasourceId": FL.SalesDS, "fileName": "s.xlsx", "maxRows": 10}},
					"reports": []any{map[string]any{"reportId": PM.Report, "format": "pdf", "params": map[string]any{"region": "Sud"},
						"fileName": "r.pdf", "inBody": true}},
					"dashboards": []any{map[string]any{"dashboardId": PM.Board, "format": "png", "params": map[string]any{"year": 2026},
						"fileName": "b.png"}}}
				for k, v := range changes {
					pm[k] = v
				}
				return pm
			}
			n := 0
			create := func(name string, auth func(*http.Request), def any) {
				n++
				add("create · "+name, "POST", f("/projects/%d/flow-groups", FL.Sales), auth,
					map[string]any{"name": f("pm grid %d", n), "definition": def})
			}
			rep := func(m map[string]any) []any {
				r := map[string]any{"reportId": PM.Report, "format": "pdf", "params": map[string]any{}, "fileName": "r.pdf"}
				for k, v := range m {
					r[k] = v
				}
				return []any{r}
			}
			brd := func(m map[string]any) []any {
				b := map[string]any{"dashboardId": PM.Board, "format": "png", "params": map[string]any{}, "fileName": "b.png"}
				for k, v := range m {
					b[k] = v
				}
				return []any{b}
			}
			many := map[string]any{}
			for i := 0; i < 101; i++ {
				many[f("p%d", i)] = i
			}

			create("a good cell · editor", w.As("editor"), grid(cell(1, good(nil))))
			create("a good cell · admin", w.As("admin"), grid(cell(1, good(nil))))
			create("a good cell · writer", w.As("writer"), grid(cell(1, good(nil))))
			create("a good cell · nobody", nil, grid(cell(1, good(nil))))
			create("no settings", w.As("editor"), grid(map[string]any{"kind": "pushmail", "id": 1}))
			create("empty settings", w.As("editor"), grid(cell(1, map[string]any{})))
			create("no connection", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": nil}))))
			create("a connection that is not SMTP", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": FL.PG}))))
			create("no such connection", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": 999999}))))
			create("an SMTP of a folder without CONNECT", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": FL.SmtpHidden}))))
			create("no recipients", w.As("editor"), grid(cell(1, good(map[string]any{"to": []any{" "}, "cc": []any{"c@x.it"}}))))
			create("not addresses", w.As("editor"), grid(cell(1, good(map[string]any{"to": []any{"a", "b@", "c d@x.it", "e@x", "f@x.it", "g", "h"}}))))
			create("outside the domains", w.As("editor"), grid(cell(1, good(map[string]any{"to": []any{"a@z.it", "b@X.IT", "c@q.it"},
				"cc": []any{"d@y.it", "e@w.it"}}))))
			create("a broken domain list", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": FL.SmtpBroken}))))
			create("an open SMTP", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": FL.SmtpOpen, "to": []any{"a@anywhere.test"}}))))
			create("nothing to send", w.As("editor"), grid(cell(1, good(map[string]any{"attachments": []any{}, "reports": []any{}, "dashboards": []any{}}))))
			create("attachments wrong", w.As("editor"), grid(cell(1, good(map[string]any{"attachments": []any{
				map[string]any{"fileName": " "}, map[string]any{"datasourceId": FL.SalesDS, "fileName": "a"},
				map[string]any{"datasourceId": FL.SalesDS, "fileName": "b"}, map[string]any{"datasourceId": FL.HiddenDS, "fileName": "c"},
				map[string]any{"datasourceId": 999999, "fileName": "d"}}}))))
			create("reports wrong", w.As("editor"), grid(cell(1, good(map[string]any{"reports": []any{
				map[string]any{"format": "pdf", "fileName": "a", "inBody": true},
				map[string]any{"reportId": PM.Report, "format": "pdf", "fileName": " ", "inBody": true},
				map[string]any{"reportId": PM.Report, "format": "pdf", "fileName": "c", "params": many},
				map[string]any{"reportId": PM.HiddenReport, "format": "pdf", "fileName": "d"},
				map[string]any{"reportId": 999999, "format": "pdf", "fileName": "e"}}}))))
			create("reports' values wrong", w.As("editor"), grid(cell(1, good(map[string]any{"reports": []any{
				map[string]any{"reportId": PM.Report, "format": "pdf", "fileName": "f", "params": map[string]any{"nope": "x"}},
				map[string]any{"reportId": PM.Report, "format": "pdf", "fileName": "g", "params": map[string]any{"n": "not a number"}},
				map[string]any{"reportId": PM.Report, "format": "pdf", "fileName": "h", "params": map[string]any{"region": []any{"a", "b"}}},
				map[string]any{"reportId": PM.BlindReport, "format": "pdf", "fileName": "i"}}}))))
			create("dashboards wrong", w.As("editor"), grid(cell(1, good(map[string]any{"dashboards": []any{
				map[string]any{"format": "png", "fileName": "a"},
				map[string]any{"dashboardId": PM.Board, "format": "png", "fileName": " "},
				map[string]any{"dashboardId": PM.Board, "format": "png", "fileName": "c", "params": many},
				map[string]any{"dashboardId": PM.HiddenBoard, "format": "png", "fileName": "d"}}}))))
			create("dashboards' values wrong", w.As("editor"), grid(cell(1, good(map[string]any{"dashboards": []any{
				map[string]any{"dashboardId": PM.Board, "format": "png", "fileName": "f", "params": map[string]any{"nope": "x"}},
				map[string]any{"dashboardId": PM.Board, "format": "png", "fileName": "g", "params": map[string]any{"year": "x"}},
				map[string]any{"dashboardId": PM.BlindBoard, "format": "png", "fileName": "i"}}}))))
			create("a blind report · admin reads it", w.As("admin"), grid(cell(1, good(map[string]any{"reports": rep(map[string]any{"reportId": PM.BlindReport})}))))
			create("a hidden dashboard · admin", w.As("admin"), grid(cell(1, good(map[string]any{"dashboards": brd(map[string]any{"dashboardId": PM.HiddenBoard})}))))
			create("two cells, one twice in a step, errors of both", w.As("editor"), map[string]any{"steps": []any{
				[]any{cell(1, good(map[string]any{"to": []any{}})), cell(1, good(nil)), cell(2, good(map[string]any{"connectionId": nil}))},
				[]any{map[string]any{"kind": "flow", "id": 999999}, cell(3, map[string]any{"label": "  "})}}})
			create("more than ten problems", w.As("editor"), grid(cell(1, good(map[string]any{"connectionId": FL.PG, "to": []any{"x"},
				"attachments": []any{map[string]any{"fileName": ""}, map[string]any{"fileName": ""}, map[string]any{"fileName": ""}},
				"reports":     []any{map[string]any{"format": "pdf", "fileName": ""}, map[string]any{"format": "pdf", "fileName": ""}},
				"dashboards":  []any{map[string]any{"format": "png", "fileName": ""}, map[string]any{"format": "png", "fileName": ""}}}))))

			add("duplicate · the full mail grid, editor", "POST", f("/flow-groups/%d/duplicate", PM.MailGroup), w.As("editor"), nil)
			add("duplicate · the full mail grid, admin", "POST", f("/flow-groups/%d/duplicate", PM.MailGroup), w.As("admin"), nil)
			add("duplicate · the bare mail grid, admin", "POST", f("/flow-groups/%d/duplicate", FG.Mail), w.As("admin"), nil)

			patch := func(name string, auth func(*http.Request), body any) {
				add("patch · "+name, "PATCH", f("/flow-groups/%d", PM.MailGroup), auth, body)
			}
			full := func(pm map[string]any) map[string]any { return map[string]any{"definition": grid(cell(3, pm))} }
			saved := map[string]any{"label": "Full", "connectionId": FL.Smtp, "to": []any{"a@x.it"},
				"attachments": []any{map[string]any{"datasourceId": FL.SalesDS, "fileName": "s.xlsx"}},
				"reports":     rep(map[string]any{"params": map[string]any{"region": "Sud"}, "inBody": true}),
				"dashboards":  brd(nil)}
			patch("the cell as saved, unchanged", w.As("editor"), full(saved))
			with := func(to ...any) map[string]any {
				c := map[string]any{}
				for k, v := range saved {
					c[k] = v
				}
				c["to"] = to
				return c
			}
			changed := with("a@x.it", "in@y.it")
			patch("a recipient outside the domains", w.As("editor"), full(with("a@x.it", "out@z.it")))
			patch("a recipient added", w.As("editor"), full(changed))
			patch("a second cell, wrong", w.As("editor"), map[string]any{"definition": map[string]any{"steps": []any{
				[]any{cell(3, changed)}, []any{cell(4, good(map[string]any{"reports": rep(map[string]any{"reportId": PM.HiddenReport})}))}}}})
			patch("by writer", w.As("writer"), full(saved))
			add("the mail grid at the end · editor", "GET", f("/flow-groups/%d", PM.MailGroup), w.As("editor"), nil)
			add("groups at the end · admin", "GET", f("/projects/%d/flow-groups", FL.Sales), w.As("admin"), nil)
			return s
		},
	})
}
