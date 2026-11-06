package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/scripting.py: the package search, the editor's check of a Python file (Ruff in the AI
// service) and the outputs of a flow's Script and Container nodes. The searches that reach an
// index are in «scripting_packages», run only when asked, with DIFF_ENV="SCRIPTS__PYPI_INDEX_URL=
// http://siem.test:8000/simple/ SCRIPTS__CRAN_URL=http://siem.test:8000/cran": the indexes are
// the fake engine (each gateway keeps a list a day once read: the first search reads it).
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "scripting_runtime",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('sr-folder', '', '2026-01-01') RETURNING id`)
			ids["folder"] = folder
			w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit'), ($1, $3, 'view')`, folder,
				w.People["writer"], w.People["editor"])
			coders := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('sr-coders', '', false,
				false, '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], coders)
			env := func(key, name, language string, version, completions any) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
					memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', $2, true, '1', '1', '1Gi', '1Gi',
					60, '2026-01-01', '2026-01-01') RETURNING id`, name, language)
				w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, ids[key], coders)
				if version != nil {
					v := w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages,
						status, completions, created_at) VALUES ($1, 1, $2, 'python:3.12-slim', '', 'ready', $3, '2026-01-01') RETURNING id`,
						ids[key], version, completions)
					w.Exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, v, ids[key])
				}
			}
			env("py", "sr-py", "python", "3.12", `["polars", 1, "pandas"]`)
			env("dict", "sr-dict", "python", "3.11", `{"numpy": ["array"]}`)
			env("bad", "sr-bad", "python", "3.12", `[["x"]]`)
			env("text", "sr-text", "python", "3.12", `"ab"`)
			env("nover", "sr-nover", "python", nil, nil)
			env("r", "sr-r", "r", "4.5", nil)
			def := fmt.Sprintf(`{"nodes": [
				{"id": "n1", "type": "script", "data": {"label": "One", "environmentId": %d, "entrypoint": "main.py",
				 "files": [{"path": "main.py", "content": "print(1)"}]}},
				{"id": "c1", "type": "container", "data": {"registryId": 999999, "repository": "team/app", "tag": "v1",
				 "variables": [{"name": "B", "value": 2}, {"name": "A"}]}},
				{"id": 5, "type": "script", "data": {"scriptId": 999999}},
				{"type": "script", "data": {}},
				{"id": "n1", "type": "script", "data": {"label": "Twice", "environmentId": %d}},
				{"id": "x", "type": "filter", "data": {}}], "edges": []}`, ids["py"], ids["py"])
			ids["flow"] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
				VALUES ('sr-flow', '', $1, $2, $3, 'duckdb', '2026-01-01', '2026-01-01') RETURNING id`, folder, w.People["admin"], def)
			ids["empty"] = w.Insert(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
				VALUES ('sr-empty', '', $1, $2, 'not json', 'duckdb', '2026-01-01', '2026-01-01') RETURNING id`, folder, w.People["admin"])
			run := func(kind, status, script, key, started string, finished any) {
				w.Exec(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
					publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at, error, rows_written, script)
					VALUES ($1, $2, '', $3, $4, 'trial', '', 'scripts', $5, '', false, '[]', $6, $7, $8, $9, $10)`,
					kind, ids["flow"], status, w.People["writer"], key, started, finished, map[bool]any{true: "it broke", false: nil}[status == "FAILURE"],
					map[bool]any{true: 42, false: nil}[key != ""], script)
			}
			run("script", "SUCCESS", `{"node_id": "n1", "trial": true, "fingerprint": "old", "attempts": 2, "exit_code": 0, "log": "ok"}`,
				"out/n1.parquet", "2026-02-01 10:00:00", "2026-02-01 10:01:00.5")
			run("script", "FAILURE", `{"node_id": "n1", "trial": false, "log_expired": 1}`, "", "2026-02-02 10:00:00", "2026-02-02 10:00:30")
			run("script", "SUCCESS", `{"node_id": "n1", broken`, "out/n1b.parquet", "2026-01-31 10:00:00", nil)
			run("container", "STARTED", `{"node_id": "c1", "fingerprint": "f"}`, "", "2026-02-03 10:00:00.123456", nil)
			run("script", "SUCCESS", `{"node_id": 5, "trial": true}`, "out/5.parquet", "2026-02-04", "2026-02-04 00:00:05")
			run("script", "SUCCESS", `{"node_id": null, "x": 1}`, "", "2026-02-04", "2026-02-04")
			run("script", "SUCCESS", `{"node_id": "x"}`, "out/x.parquet", "2026-02-04", "2026-02-04")
			run("ingest", "SUCCESS", `{"node_id": "n1", "trial": true}`, "out/no.parquet", "2026-03-01", "2026-03-01")
		},
		Steps: func(w *World) []Step {
			var s []Step
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "service", "nobody"} {
				s = append(s, Step{Name: "outputs · " + p, Method: "GET", Path: fmt.Sprintf("/scripting/flows/%d/outputs", ids["flow"]), Auth: as(p)})
			}
			s = append(s,
				Step{Name: "outputs of a definition not JSON", Method: "GET", Path: fmt.Sprintf("/scripting/flows/%d/outputs", ids["empty"]), Auth: w.As("admin")},
				Step{Name: "outputs of no flow", Method: "GET", Path: "/scripting/flows/999999/outputs", Auth: w.As("admin")},
				Step{Name: "outputs of a word", Method: "GET", Path: "/scripting/flows/x/outputs", Auth: w.As("admin")},
			)
			pause := func(*World) { time.Sleep(1100 * time.Millisecond) } // script_lint.allowed: 2 a second per person
			lint := func(name, p string, body any) {
				s = append(s, Step{Name: "lint: " + name, Method: "POST", Path: "/scripting/lint", Auth: as(p), JSON: body, Before: pause})
			}
			code := "import polars\nimport numpy\nimport lib.helper\nfrom pandas import x\nfrom os import path\nimport mine\n\ny = undefined_name\nimport json\n"
			lint("Ruff and the imports", "writer", map[string]any{"environment_id": ids["py"], "path": "main.py", "content": code,
				"files": []any{"main.py", "lib/helper.py", "mine.py"}})
			lint("a dict of completions", "admin", map[string]any{"environment_id": ids["dict"], "path": "a/b.py", "content": code})
			lint("completions that are not names", "admin", map[string]any{"environment_id": ids["bad"], "path": "m.py", "content": code})
			lint("completions that are a text", "admin", map[string]any{"environment_id": ids["text"], "path": "m.py", "content": "import a\nimport b\n"})
			lint("no version", "writer", map[string]any{"environment_id": ids["nover"], "path": "m.py", "content": code})
			lint("a syntax error", "writer", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": "def (:\n  pass\n"})
			lint("nothing to say", "writer", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": "print('ok')\n"})
			lint("R", "writer", map[string]any{"environment_id": ids["r"], "path": "m.R", "content": "x <- 1"})
			lint("not a .py", "writer", map[string]any{"environment_id": ids["py"], "path": "m.txt", "content": "y = z"})
			lint("too large", "writer", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": strings.Repeat("#", 256*1024+1)})
			lint("not allowed", "editor", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": "x"})
			lint("no environment", "admin", map[string]any{"environment_id": 999999, "path": "m.py", "content": "x"})
			lint("a huge id", "admin", map[string]any{"environment_id": 99999999999, "path": "m.py", "content": "x"})
			lint("bad body", "admin", map[string]any{"environment_id": "x", "path": "", "files": make([]any, 51)})
			lint("a path too long", "admin", map[string]any{"environment_id": ids["py"], "path": strings.Repeat("a", 901), "content": ""})
			lint("nobody", "nobody", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": "x"})
			lint("a read token", "nobody", map[string]any{"environment_id": ids["py"], "path": "m.py", "content": "x"})
			s[len(s)-1].Auth = Bearer("tab_data_writer_read_000000")
			s = append(s, Step{Name: "lint: no body", Method: "POST", Path: "/scripting/lint", Auth: w.As("writer"), Before: pause})

			pkg := func(name, path, p string) {
				s = append(s, Step{Name: "packages: " + name, Method: "GET", Path: path, Auth: as(p), Engine: []EngineRule{}})
			}
			// none of these reach an index
			for _, p := range []string{"admin", "admin_group", "obs_flag", "editor", "nobody"} {
				pkg("a query too short · "+p, "/admin/scripting/packages?language=python&q=%20a%20", p)
			}
			pkg("a token", "/admin/scripting/packages?language=python&q=x", "nobody")
			s[len(s)-1].Auth = Bearer("tab_data_service_write_0000")
			pkg("no language", "/admin/scripting/packages?q=ab", "admin")
			pkg("another language", "/admin/scripting/packages?language=julia&q=ab", "admin")
			pkg("no query", "/admin/scripting/packages?language=r", "admin")
			pkg("versions: another language", "/admin/scripting/packages/julia/x", "admin")
			pkg("versions: not a name", "/admin/scripting/packages/python/-x", "admin")
			pkg("versions: not an R name", "/admin/scripting/packages/r/1x", "admin")
			pkg("versions: an observer", "/admin/scripting/packages/python/polars", "obs_group")
			return s
		},
	})

	simple := `<html><body><a href="/simple/polars/">polars</a>
<a href="/simple/Polars_Extra/">Polars_Extra</a><a data-x href="polars-ds/">polars-ds</a>
<A HREF="/simple/pola/">pola</A><a href="/simple/py-polars/">py-polars</a><a href="/simple/num&amp;py/">x</a></body></html>`
	cran := "Package: dplyr\nVersion: 1.1.4\nDepends: R\n\nPackage: DPLYR2\nVersion: 0.1\n\nPackage: dbplyr\r\nVersion: 2.5.0\n\nPackage: plyr\nVersion: 1.8.9\n"
	files := `<html><body>
<a href="x">polars-1.0.0-cp39-abi3-manylinux_2_17_x86_64.whl</a>
<a href="x">polars-1.0.0.tar.gz</a>
<a href="x" data-yanked="bad">polars-0.9.0-py3-none-any.whl</a>
<a href="x">polars-0.9.0.tar.gz</a>
<a href="x" data-yanked="">polars-0.8.0.tar.gz</a>
<a href="x">polars-1.1.0rc1-cp313-cp313-win_amd64.whl</a>
<a href="x">polars-2.0.dev1.zip</a>
<a href="x">polars-1.0-py3-none-any.whl</a>
<a href="x">other-3.0.tar.gz</a>
<a href="x">polars-x.tar.gz</a>
<a href="x">polars-1.0.0-cp311-cp311-musllinux_1_1_x86_64.whl</a>
</body></html>`
	register(Suite{
		Module: "scripting_packages",
		Asked:  true,
		Steps: func(w *World) []Step {
			var s []Step
			pkg := func(name, path string, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{}
				}
				s = append(s, Step{Name: "packages: " + name, Method: "GET", Path: path, Auth: w.As("admin"), Engine: rules})
			}
			index := EngineRule{Method: "GET", Path: "^/simple/$", Text: simple, Type: "text/html"}
			pkg("the index fails", "/admin/scripting/packages?language=python&q=polars", EngineRule{Method: "GET", Path: "^/simple/$", Status: 503})
			pkg("the index is not there", "/admin/scripting/packages?language=python&q=polars", EngineRule{Method: "GET", Path: "^/simple/$", Status: 404})
			pkg("python", "/admin/scripting/packages?language=python&q=Polars", index)
			pkg("python, read once a day", "/admin/scripting/packages?language=python&q=pol", index)
			pkg("python, nothing", "/admin/scripting/packages?language=python&q=zzz")
			pkg("python, canonical", "/admin/scripting/packages?language=python&q=polars_extra")
			pkg("r", "/admin/scripting/packages?language=r&q=PLYR", EngineRule{Method: "GET", Path: "^/cran/src/contrib/PACKAGES$", Text: cran,
				Type: "text/plain"})
			pkg("versions, wheels for 3.12", "/admin/scripting/packages/python/Polars?python=3.12",
				EngineRule{Method: "GET", Path: "^/simple/polars/$", Text: files, Type: "text/html"})
			pkg("versions, no python", "/admin/scripting/packages/python/polars",
				EngineRule{Method: "GET", Path: "^/simple/polars/$", Text: files, Type: "text/html"})
			pkg("versions, JSON", "/admin/scripting/packages/python/polars?python=3.11", EngineRule{Method: "GET", Path: "^/simple/polars/$",
				Type: "application/vnd.pypi.simple.v1+json", Body: map[string]any{"files": []any{
					map[string]any{"filename": "polars-1.0.0-cp311-cp311-manylinux1_x86_64.whl", "yanked": false},
					map[string]any{"filename": "polars-1.0.0rc2.tar.gz", "yanked": "reason"},
					map[string]any{"filename": "polars-0.5.tar.gz"}}}})
			pkg("versions, JSON without filename", "/admin/scripting/packages/python/polars", EngineRule{Method: "GET", Path: "^/simple/polars/$",
				Type: "application/json", Body: map[string]any{"files": []any{map[string]any{"x": 1}}}})
			pkg("versions, none", "/admin/scripting/packages/python/nothing", EngineRule{Method: "GET", Path: "^/simple/nothing/$", Status: 404})
			pkg("versions, only yanked", "/admin/scripting/packages/python/polars", EngineRule{Method: "GET", Path: "^/simple/polars/$",
				Text: `<a data-yanked>polars-1.0.tar.gz</a>`, Type: "text/html"})
			pkg("versions, the index fails", "/admin/scripting/packages/python/polars", EngineRule{Method: "GET", Path: "^/simple/polars/$", Status: 500})
			pkg("r versions", "/admin/scripting/packages/r/dplyr", EngineRule{Method: "GET", Path: "^/cran/src/contrib/Archive/dplyr/$",
				Text: `<a href="dplyr_1.0.0.tar.gz">x</a><a href="dplyr_0.8.5.tar.gz">x</a><a href="dplyr_1.0-10.tar.gz">x</a>` +
					`<a href="dplyr_1.1.4.tar.gz">x</a><a href="other_9.tar.gz">x</a>`, Type: "text/html"})
			pkg("r versions, no archive", "/admin/scripting/packages/r/plyr", EngineRule{Method: "GET", Path: "^/cran/", Status: 404})
			pkg("r versions, not in the index", "/admin/scripting/packages/r/nothing")
			pkg("r versions, the archive fails", "/admin/scripting/packages/r/dbplyr", EngineRule{Method: "GET", Path: "^/cran/", Status: 502})
			return s
		},
	})
}
