package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/registries.py: the container registries. Off without CONTAINERS__ENABLED; run it
// also with DIFF_ENV="CONTAINERS__ENABLED=true CONTAINERS__NAMESPACE=containers
// CONTAINERS__TRIAL_NAMESPACE=trials KUBERNETES__API_URL=http://{engine}:8000
// KUBERNETES__TOKEN_FILE=/etc/hostname": the pull Secrets' removal reaches the fake engine.
func init() {
	maskColumns("container_registries", "password_enc")
	maskColumns("container_registry_variables", "value_enc")
	ids := map[string]int64{}
	// a self-signed authority, for tls "ca"
	const ca = `-----BEGIN CERTIFICATE-----
MIIBfjCCASOgAwIBAgIUU/WYaCHUNO5NHp9t9Xa8REUlLzEwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJdGVzdC1yb290MB4XDTI2MTEwMzIxMTMxMVoXDTM2MTAzMTIx
MTMxMVowFDESMBAGA1UEAwwJdGVzdC1yb290MFkwEwYHKoZIzj0CAQYIKoZIzj0D
AQcDQgAEs69EEaOjXxjR8dpiW9AjM/GDfQ/oZ89cJTika+6lFWXu6ExHCZGN/r4U
0WF9na8n/OCJyyZqbxm2vpFr2WJvxKNTMFEwHQYDVR0OBBYEFNsrQWm4y5h6bfl5
9qXPvn3Yo9FsMB8GA1UdIwQYMBaAFNsrQWm4y5h6bfl59qXPvn3Yo9FsMA8GA1Ud
EwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDSQAwRgIhAPhN2qOiEF8WfDEaz3b0zRUm
srEZYNmdY9hd1D4XOLx3AiEAk/Ty6ZJyEvrOfm4xrPxLjrM7EouO21NAep0Rgce7
cgw=
-----END CERTIFICATE-----`
	register(Suite{
		Module: "registries",
		Seed: func(w *World) {
			ids = map[string]int64{}
			team := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('rg-team', '', false, false,
				'2026-01-01') RETURNING id`)
			ids["team"] = team
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], team)
			reg := func(key, name, host, repos string, enabled bool) {
				ids[key] = w.Insert(`INSERT INTO container_registries (name, description, host, protocol, tls, ca_cert, username, password_enc,
					repositories, enabled, last_test_at, last_test_ok, last_test_message, created_at, created_by, updated_at, updated_by)
					VALUES ($1, 'about', $2, 'https', 'public', '', 'bot', 'gAAAA-fixed', $3, $4, '2026-01-05', true, 'Read x', '2026-01-01',
					'a@x', '2026-01-02', 'b@x') RETURNING id`, name, host, repos, enabled)
			}
			reg("r1", "Rg-Hub", "docker.io", "library/python\nteam/*\n  \nsolo", true)
			reg("r2", "rg-ghcr", "ghcr.io", "*", true)
			reg("r3", "rg-off", "off.example.com", "*", false)
			reg("r4", "rg-alone", "alone.example.com", "a/b", true)
			access := func(registry string, group, user any, repos string) {
				w.Exec(`INSERT INTO container_registry_access (registry_id, group_id, user_id, repositories, created_at, created_by)
					VALUES ($1, $2, $3, $4, '2026-01-01', 'a@x')`, ids[registry], group, user, repos)
			}
			access("r1", team, nil, "team/*\nlibrary/python")
			access("r1", nil, w.People["editor"], "")
			access("r2", team, nil, "team/x\nother/*")
			access("r2", Groups["Analysts"], nil, "*")
			access("r3", team, nil, "")
			variable := func(registry, name string, secret bool) {
				var enc any
				value := "v-" + name
				if secret {
					enc, value = "gAAAA-fixed", ""
				}
				w.Exec(`INSERT INTO container_registry_variables (registry_id, name, secret, value, value_enc, updated_at, updated_by)
					VALUES ($1, $2, $3, $4, $5, '2026-01-03', 'c@x')`, ids[registry], name, secret, value, enc)
			}
			variable("r1", "PLAIN", false)
			variable("r1", "HIDDEN", true)
			w.Token("admin", "tab_rg_admin_write_000000000", "write", time.Hour)
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			add := func(name, method, path, p string, body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: as(p), JSON: body})
			}
			for _, p := range []string{"admin", "admin_group", "obs_flag", "obs_group", "editor", "writer", "nobody"} {
				add(p+" lists the registries", "GET", "/admin/registries", p, nil)
				add(p+" reads a registry", "GET", fmt.Sprintf("/admin/registries/%d", id("r1")), p, nil)
				add(p+" reads the editor's list", "GET", "/registries", p, nil)
			}
			add("a registry that does not exist", "GET", "/admin/registries/999999", "admin", nil)
			add("a registry by a word", "GET", "/admin/registries/x", "admin", nil)
			s = append(s, Step{Name: "a token lists", Method: "GET", Path: "/admin/registries", Auth: Bearer("tab_rg_admin_write_000000000")})

			create := func(name, p string, body any) { add(name, "POST", "/admin/registries", p, body) }
			create("admin creates one", "admin", map[string]any{"name": " rg-new ", "host": " HTTPS://Registry.Example.com:5000/ ",
				"username": " u ", "password": "p", "repositories": []any{" team/x ", "team/*", "team/x", "", "*"},
				"access": []any{map[string]any{"group_id": id("team"), "repositories": []any{"team/x"}},
					map[string]any{"user_id": w.People["editor"]}}})
			create("admin creates it again", "admin", map[string]any{"name": "RG-NEW", "host": "x.io"})
			create("admin creates one on Docker Hub", "admin", map[string]any{"name": "rg-dh", "host": "index.docker.io",
				"repositories": []any{"python", "/team/app/"}, "protocol": "http", "tls": "ca", "ca_cert": "junk"})
			create("admin creates one with its authority", "admin", map[string]any{"name": "rg-ca", "host": "ca.io", "tls": "ca", "ca_cert": " " + ca + " "})
			create("admin creates one with a bad authority", "admin", map[string]any{"name": "rg-ca2", "host": "ca.io", "tls": "ca",
				"ca_cert": "-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----"})
			create("admin creates one without an authority", "admin", map[string]any{"name": "rg-ca3", "host": "ca.io", "tls": "ca"})
			create("admin creates one with tls none", "admin", map[string]any{"name": "rg-none", "host": "none.io", "tls": "none", "ca_cert": "x"})
			for _, bad := range []map[string]any{
				{"name": "b", "host": "bad host"}, {"name": "b", "host": "-x.io"}, {"name": "b", "host": "x.io:123456"},
				{"name": "b", "host": strings.Repeat("a", 254)}, {"name": "b", "host": strings.Repeat("a", 250) + ":5000"},
				{"name": "b", "host": "x.io", "protocol": "ftp"}, {"name": "b", "host": "x.io", "tls": "maybe"},
				{"name": "b", "host": "x.io", "repositories": []any{"Team/X"}}, {"name": "b", "host": "x.io", "repositories": []any{"a//b"}},
				{"name": " ", "host": "x.io"},
				{"name": "rg-acc1", "host": "x.io", "access": []any{map[string]any{}}},
				{"name": "rg-acc2", "host": "x.io", "access": []any{map[string]any{"group_id": 999999}}},
				{"name": "rg-acc3", "host": "x.io", "access": []any{map[string]any{"user_id": 999999}}},
				{"name": "rg-acc4", "host": "x.io", "access": []any{map[string]any{"group_id": id("team")}, map[string]any{"group_id": id("team")}}},
				{"name": "rg-acc5", "host": "x.io", "access": []any{map[string]any{"group_id": id("team"), "user_id": 1}}},
				{"name": "rg-acc6", "host": "x.io", "access": []any{map[string]any{"group_id": id("team"), "repositories": []any{"BAD"}}}},
			} {
				create(fmt.Sprintf("admin creates a bad one %v", bad), "admin", bad)
			}
			create("admin creates one after the bad ones", "admin", map[string]any{"name": "rg-after", "host": "after.io"})
			create("too many repositories", "admin", map[string]any{"name": "b", "host": "x.io", "repositories": func() []any {
				out := []any{}
				for i := 0; i < 201; i++ {
					out = append(out, fmt.Sprintf("r%d", i))
				}
				return out
			}()})
			create("an observer creates one", "obs_flag", map[string]any{"name": "b", "host": "x.io"})
			s = append(s, Step{Name: "a token creates one", Method: "POST", Path: "/admin/registries", Auth: Bearer("tab_rg_admin_write_000000000"),
				JSON: map[string]any{"name": "b"}, Header: map[string]string{"Accept-Language": "de"}})

			update := func(name, key, p string, body any) {
				add(name, "PUT", fmt.Sprintf("/admin/registries/%v", key), p, body)
			}
			r1, r2, r4 := fmt.Sprint(id("r1")), fmt.Sprint(id("r2")), fmt.Sprint(id("r4"))
			update("admin changes nothing", r1, "admin", map[string]any{})
			update("admin sets the same access", r1, "admin", map[string]any{"access": []any{
				map[string]any{"group_id": id("team"), "repositories": []any{"team/*", "library/python"}},
				map[string]any{"user_id": w.People["editor"]}}})
			update("admin changes the description", r1, "admin", map[string]any{"description": " d "})
			update("admin changes the host and repositories", r4, "admin", map[string]any{"host": "docker.io", "repositories": []any{"python"}})
			update("admin sets the password", r4, "admin", map[string]any{"password": "x"})
			update("admin removes the password", r4, "admin", map[string]any{"password": ""})
			update("admin goes http", r4, "admin", map[string]any{"protocol": "http"})
			update("admin goes back https with a ca", r4, "admin", map[string]any{"protocol": "https", "tls": "ca", "ca_cert": ca})
			update("admin keeps the ca, empty tls", r4, "admin", map[string]any{"tls": ""})
			update("admin removes the ca", r4, "admin", map[string]any{"tls": "public"})
			update("admin changes access and enables", r2, "admin", map[string]any{"enabled": false, "access": []any{
				map[string]any{"user_id": w.People["writer"], "repositories": []any{"x/*"}}}})
			update("admin renames onto another", r2, "admin", map[string]any{"name": "rg-hub"})
			update("admin gives a bad host", r2, "admin", map[string]any{"host": "a b"})
			update("admin gives bad access", r2, "admin", map[string]any{"access": []any{map[string]any{"group_id": 999999}}})
			update("admin changes one that does not exist", "999999", "admin", map[string]any{})
			update("an observer changes", r1, "obs_flag", map[string]any{})

			setVar := func(name, key, v, p string, body any) {
				add(name, "PUT", fmt.Sprintf("/admin/registries/%s/variables/%s", key, v), p, body)
			}
			setVar("admin sets a plain one", r1, "NEW_ONE", "admin", map[string]any{"value": " v "})
			setVar("admin sets a secret one", r1, "NEW_SECRET", "admin", map[string]any{"value": "s", "secret": true})
			setVar("admin keeps the secret", r1, "HIDDEN", "admin", map[string]any{"secret": true})
			setVar("admin keeps a plain one", r1, "PLAIN", "admin", map[string]any{})
			setVar("admin makes the secret plain", r1, "HIDDEN", "admin", map[string]any{"value": "now plain"})
			setVar("admin sets a reserved one", r1, "TABULARIA_X", "admin", map[string]any{"value": "x"})
			setVar("admin sets a bad name", r1, "9X", "admin", map[string]any{"value": "x"})
			add("admin deletes a variable", "DELETE", fmt.Sprintf("/admin/registries/%s/variables/PLAIN", r1), "admin", nil)
			add("admin deletes it again", "DELETE", fmt.Sprintf("/admin/registries/%s/variables/PLAIN", r1), "admin", nil)

			add("admin deletes r4", "DELETE", fmt.Sprintf("/admin/registries/%s", r4), "admin", nil)
			add("admin deletes r4 again", "DELETE", fmt.Sprintf("/admin/registries/%s", r4), "admin", nil)
			add("an observer deletes r1", "DELETE", fmt.Sprintf("/admin/registries/%s", r1), "obs_flag", nil)
			for _, p := range []string{"admin", "writer", "editor", "obs_flag"} {
				add(p+" lists at the end", "GET", "/registries", p, nil)
			}
			add("the admin list at the end", "GET", "/admin/registries", "admin", nil)
			add("forwarded: the tags", "GET", fmt.Sprintf("/admin/registries/%s/tags?repository=team/x", r1), "admin", nil)
			add("forwarded: the editor's tags", "GET", fmt.Sprintf("/registries/%s/tags?repository=team/x", r1), "writer", nil)
			return s
		},
	})
}
