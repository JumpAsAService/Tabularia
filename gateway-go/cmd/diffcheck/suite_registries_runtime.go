package main

import (
	"fmt"
	"net/http"
	"strings"
)

// routes/registries.py, what speaks to a registry: the administrators' test and tags, the flow
// editor's tags and image. The registries are at siem.test:8000, the fake engine of each stack.
// Run it with DIFF_ENV="CONTAINERS__ENABLED=true CONTAINERS__NAMESPACE=containers CONTAINERS__TRIAL_NAMESPACE=trials
// CONTAINERS__POLL_SECONDS=0.2 KUBERNETES__API_URL=http://{engine}:8000 KUBERNETES__TOKEN_FILE=/etc/hostname" (without,
// every route says 409; the cluster's test reaches the fake engine's Kubernetes).
func init() {
	ids := map[string]int64{}
	const digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	const cfg = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
	register(Suite{
		Module: "registries_runtime",
		Seed: func(w *World) {
			ids = map[string]int64{}
			team := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('rr-team', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], team)
			pw := "gAAAA-not-decryptable"
			if fernet != nil {
				pw, _ = fernet.Encrypt("s3cret")
			}
			reg := func(key, name, host, protocol, tls, ca, user string, password any, repos string, enabled bool) {
				ids[key] = w.Insert(`INSERT INTO container_registries (name, description, host, protocol, tls, ca_cert, username, password_enc,
					repositories, enabled, last_test_message, created_at, updated_at)
					VALUES ($1, '', $2, $3, $4, $5, $6, $7, $8, $9, '', '2026-01-01', '2026-01-02') RETURNING id`,
					name, host, protocol, tls, ca, user, password, repos, enabled)
			}
			reg("anon", "rr-anon", "siem.test:8000", "http", "public", "", "", nil, "team/*\nteam/app", true)
			reg("basic", "rr-basic", "siem.test:8000", "http", "public", "", "bot", pw, "*", true)
			reg("none", "rr-none", "siem.test:8000", "http", "public", "", "", nil, "team/*", true)
			reg("tls", "rr-tls", "siem.test:8000", "https", "public", "", "", nil, "team/app", true)
			reg("tlsnone", "rr-tlsnone", "siem.test:8000", "https", "none", "", "", nil, "", true)
			reg("ca", "rr-ca", "siem.test:8000", "https", "ca", "not a pem", "", nil, "", true)
			reg("refused", "rr-refused", "siem.test:9", "http", "public", "", "", nil, "a", true)
			reg("nodns", "rr-nodns", "nowhere.invalid", "http", "public", "", "", nil, "a", true)
			reg("meta", "rr-meta", "169.254.169.254", "http", "public", "", "", nil, "", true)
			reg("badhost", "rr-badhost", "bad host", "http", "public", "", "", nil, "", true)
			reg("off", "rr-off", "siem.test:8000", "http", "public", "", "", nil, "*", false)
			reg("broken", "rr-broken", "siem.test:8000", "http", "public", "", "bot", "not a token", "*", true)
			access := func(registry string, group, user any, repos string) {
				w.Exec(`INSERT INTO container_registry_access (registry_id, group_id, user_id, repositories, created_at, created_by)
					VALUES ($1, $2, $3, $4, '2026-01-01', 'a@x')`, ids[registry], group, user, repos)
			}
			access("anon", team, nil, "team/app")
			access("anon", nil, w.People["editor"], "")
			access("off", team, nil, "")
			access("basic", team, nil, "x/*")
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			v2 := func(status int, auth string) EngineRule {
				r := EngineRule{Method: "GET", Path: "^/v2/$", Status: status, Body: map[string]any{}}
				if auth != "" {
					r.Headers = map[string]string{"WWW-Authenticate": auth}
				}
				return r
			}
			tagsRule := func(status int, body any) EngineRule {
				return EngineRule{Method: "GET", Path: "^/v2/team/app/tags/list$", Status: status, Body: body}
			}
			seven := map[string]any{"name": "team/app", "tags": []any{"1", "2", 3, "4", "5", "6", "7", "8"}}
			test := func(name, key string, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{}
				}
				s = append(s, Step{Name: "test: " + name, Method: "POST", Path: fmt.Sprintf("/admin/registries/%d/test", id(key)),
					Auth: w.As("admin"), Engine: rules})
			}
			bearer := `Bearer realm="http://siem.test:8000/token",service="reg.test",scope="x"`
			test("anonymous, a repository read", "anon", v2(200, ""), tagsRule(200, seven))
			test("anonymous, no repository to read", "none", v2(200, ""))
			test("basic, refused", "basic", v2(401, `Basic realm="r"`))
			test("basic, no user", "anon", v2(401, `Basic realm="r"`))
			test("bearer, a token, pages", "anon", v2(401, bearer),
				EngineRule{Method: "GET", Path: "^/token$", Body: map[string]any{"token": "t-1"}},
				EngineRule{Method: "GET", Path: "^/v2/team/app/tags/list$", Body: map[string]any{"tags": []any{"a", "b"}},
					Headers: map[string]string{"Link": `</v2/team/app/tags/list?n=5&last=b>; rel="next"`}})
			test("bearer, the credentials first", "basic", v2(401, bearer),
				EngineRule{Method: "GET", Path: "^/token$", Body: map[string]any{"access_token": "t-2"}})
			test("bearer, refused credentials", "basic", v2(401, bearer), EngineRule{Method: "GET", Path: "^/token$", Status: 401})
			test("bearer, anonymous refused", "anon", v2(401, bearer), EngineRule{Method: "GET", Path: "^/token$", Status: 403})
			test("bearer, the token refused", "anon", v2(401, bearer), EngineRule{Method: "GET", Path: "^/token$", Status: 500})
			test("bearer, no token", "anon", v2(401, bearer), EngineRule{Method: "GET", Path: "^/token$", Body: map[string]any{"token": ""}})
			test("bearer, a token not JSON", "anon", v2(401, bearer), EngineRule{Method: "GET", Path: "^/token$", Text: "nope", Type: "text/plain"})
			test("bearer, no realm", "anon", v2(401, `Bearer service="x"`))
			test("bearer, a realm in the metadata", "anon", v2(401, `Bearer realm="http://169.254.169.254/t"`))
			test("an unknown scheme", "anon", v2(401, `Negotiate x`))
			test("no scheme", "anon", v2(401, ""))
			test("not a registry", "anon", v2(404, ""))
			test("plain HTTP to an HTTPS server", "anon", EngineRule{Method: "GET", Path: "^/v2/$", Status: 400,
				Text: "Client sent an HTTP request to an HTTPS server.", Type: "text/plain"})
			test("tags private", "anon", v2(200, ""), tagsRule(401, nil))
			test("tags not there", "anon", v2(200, ""), tagsRule(404, nil))
			test("tags not JSON", "anon", v2(200, ""), EngineRule{Method: "GET", Path: "^/v2/team/app/tags/list$", Text: "x", Type: "text/plain"})
			test("tags a text", "anon", v2(200, ""), tagsRule(200, map[string]any{"tags": "abc"}))
			test("tags a list of lists", "anon", v2(200, ""), tagsRule(200, []any{1}))
			test("TLS to a plain HTTP server", "tls")
			test("TLS unchecked to a plain HTTP server", "tlsnone")
			test("an authority that is not PEM", "ca")
			test("a port that refuses", "refused")
			test("a name that does not resolve", "nodns")
			test("a metadata address", "meta")
			test("a host that is not one", "badhost")
			test("a password that does not decrypt", "broken")
			test("a registry that does not exist", "anon")
			s[len(s)-1].Path = "/admin/registries/999999/test"
			for _, p := range []string{"admin_group", "obs_flag", "editor", "writer"} {
				s = append(s, Step{Name: "test: as " + p, Method: "POST", Path: fmt.Sprintf("/admin/registries/%d/test", id("none")), Auth: w.As(p),
					Engine: []EngineRule{v2(200, "")}})
			}
			s = append(s,
				Step{Name: "test: nobody", Method: "POST", Path: fmt.Sprintf("/admin/registries/%d/test", id("none"))},
				Step{Name: "test: a token", Method: "POST", Path: fmt.Sprintf("/admin/registries/%d/test", id("none")),
					Auth: Bearer("tab_data_service_write_0000"), Header: map[string]string{"Accept-Language": "fr"}},
				Step{Name: "test: a word for the id", Method: "POST", Path: "/admin/registries/x/test", Auth: w.As("admin")},
			)

			tags := func(name, path, p string, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{}
				}
				var auth func(*http.Request)
				if p != "nobody" {
					auth = w.As(p)
				}
				s = append(s, Step{Name: "tags: " + name, Method: "GET", Path: path, Auth: auth, Engine: rules})
			}
			admin := func(key, q string) string { return fmt.Sprintf("/admin/registries/%d/tags?%s", id(key), q) }
			editor := func(key, q string) string { return fmt.Sprintf("/registries/%d/tags?%s", id(key), q) }
			for _, p := range []string{"admin", "obs_group", "editor", "nobody"} {
				tags(p+" reads the tags", admin("anon", "repository=team/app"), p, v2(200, ""), tagsRule(200, seven))
			}
			tags("a repository not allowed", admin("anon", "repository=other/x"), "admin")
			tags("no repository", admin("anon", ""), "admin")
			tags("a repository too long", admin("anon", "repository="+strings.Repeat("a", 256)), "admin")
			tags("a bad repository name", admin("basic", "repository=Team//X"), "admin")
			tags("a repository with spaces and slashes", admin("anon", "repository=%20/team/app/%20"), "admin", v2(200, ""), tagsRule(200, seven))
			tags("the registry fails", admin("anon", "repository=team/app"), "admin", v2(500, ""))
			tags("not there", "/admin/registries/999999/tags?repository=a", "admin")
			tags("a word for the id and no repository", "/admin/registries/x/tags", "admin")
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "nobody"} {
				tags(p+" reads the editor's tags", editor("anon", "repository=team/app"), p, v2(200, ""), tagsRule(200, seven))
				tags(p+" reads tags of another repository", editor("anon", "repository=team/other"), p, v2(200, ""),
					EngineRule{Method: "GET", Path: "^/v2/team/other/tags/list$", Body: map[string]any{"tags": []any{"x"}}})
			}
			tags("writer: a registry off", editor("off", "repository=a"), "writer")
			tags("writer: a registry granted for x/*", editor("basic", "repository=y/z"), "writer")
			tags("writer: a registry not granted", editor("none", "repository=team/app"), "writer")
			tags("writer: not there", "/registries/999999/tags?repository=a", "writer")
			tags("the editor's: the registry fails", editor("anon", "repository=team/app"), "admin", v2(200, ""), tagsRule(403, nil))

			index := map[string]any{"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []any{
				map[string]any{"digest": "sha256:aaaa", "platform": map[string]any{"os": "unknown", "architecture": "unknown"}},
				map[string]any{"digest": "sha256:arm", "platform": map[string]any{"os": "linux", "architecture": "arm64", "variant": "v8"}},
				map[string]any{"digest": "sha256:amd", "platform": map[string]any{"os": "linux", "architecture": "amd64"}},
				"not a dict"}}
			manifest := map[string]any{"config": map[string]any{"digest": cfg, "size": 1234},
				"layers": []any{map[string]any{"size": 10}, map[string]any{"size": "5"}, "x"}}
			blob := map[string]any{"architecture": "amd64", "os": "linux", "created": "2026-01-01T00:00:00Z", "config": map[string]any{
				"Env": []any{"PATH=/usr/bin", "EMPTY=", "NOEQ", 5}, "Entrypoint": []any{"/bin/app", 1}, "Cmd": "go",
				"Labels": map[string]any{"a": "1", "b": 2}, "WorkingDir": "/work", "User": ""}}
			image := func(name, q, p string, rules ...EngineRule) {
				tags("image: "+name, fmt.Sprintf("/registries/%d/image?%s", id("anon"), q), p, rules...)
			}
			full := []EngineRule{v2(200, ""),
				{Method: "GET", Path: "^/v2/team/app/manifests/latest$", Body: index, Headers: map[string]string{"Docker-Content-Digest": digest}},
				{Method: "GET", Path: "^/v2/team/app/manifests/sha256:amd$", Body: manifest},
				{Method: "GET", Path: "^/v2/team/app/blobs/", Body: blob}}
			image("an index", "repository=team/app", "writer", full...)
			image("a tag", "repository=team/app&reference=v1", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/v1$", Body: manifest, Headers: map[string]string{"Docker-Content-Digest": "nope"}},
				EngineRule{Method: "GET", Path: "^/v2/team/app/blobs/", Body: map[string]any{"config": nil}})
			image("an empty reference", "repository=team/app&reference=", "admin", full...)
			image("a bad reference", "repository=team/app&reference=a:b", "admin")
			image("a reference too long", "repository=team/app&reference="+strings.Repeat("a", 201), "admin")
			image("no tag", "repository=team/app", "admin", v2(200, ""), EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Status: 404})
			image("private", "repository=team/app", "admin", v2(200, ""), EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Status: 401})
			image("a manifest not JSON", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Text: "x", Type: "text/plain"})
			image("an index of nothing", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: map[string]any{"manifests": []any{}}})
			image("no configuration", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: map[string]any{"config": map[string]any{"digest": "x"}}})
			image("a configuration too large", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: map[string]any{"config": map[string]any{"digest": cfg, "size": 3000000}}})
			image("a blob not JSON", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: manifest},
				EngineRule{Method: "GET", Path: "^/v2/team/app/blobs/", Text: "x", Type: "text/plain"})
			image("a blob missing", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: manifest},
				EngineRule{Method: "GET", Path: "^/v2/team/app/blobs/", Status: 404})
			image("created not a text", "repository=team/app", "admin", v2(200, ""),
				EngineRule{Method: "GET", Path: "^/v2/team/app/manifests/", Body: manifest},
				EngineRule{Method: "GET", Path: "^/v2/team/app/blobs/", Body: map[string]any{"created": 5}})
			image("not allowed", "repository=other/x", "writer")
			image("nobody", "repository=team/app", "nobody")

			// «Try in the cluster»
			pullName := []string{`tabularia-pull-\d+-[0-9a-f]{8}`}
			head := EngineRule{Method: "HEAD", Path: "^/v2/team/app/manifests/", Headers: map[string]string{"Docker-Content-Digest": digest}}
			running := EngineRule{Method: "GET", Path: "^/api/v1/namespaces/trials/pods$", Body: map[string]any{"items": []any{map[string]any{
				"metadata": map[string]any{"name": "p1"}, "status": map[string]any{"containerStatuses": []any{map[string]any{"state": map[string]any{
					"running": map[string]any{"startedAt": "2026-01-01T00:00:00Z"}}}}}}}}}
			pulled := EngineRule{Method: "GET", Path: "^/api/v1/namespaces/trials/pods$", Body: map[string]any{"items": []any{map[string]any{
				"metadata": map[string]any{"name": "p2"}, "status": map[string]any{}}}}}
			cluster := func(name, key, p string, body any, rules ...EngineRule) {
				if rules == nil {
					rules = []EngineRule{}
				}
				var auth func(*http.Request)
				if p != "nobody" {
					auth = w.As(p)
				}
				s = append(s, Step{Name: "cluster: " + name, Method: "POST", Path: fmt.Sprintf("/admin/registries/%d/cluster-test", id(key)), Auth: auth,
					JSON: body, Engine: rules, EngineScrub: pullName, Scrub: pullName})
			}
			cluster("anonymous, running", "anon", "admin", map[string]any{"repository": "team/app", "reference": " v1 "}, v2(200, ""), head, running)
			cluster("with credentials, an event says pulled", "basic", "admin", map[string]any{"repository": "team/app", "reference": digest},
				pulled, EngineRule{Method: "GET", Path: "^/api/v1/namespaces/trials/events$", Body: map[string]any{"items": []any{
					map[string]any{"reason": "Pulled", "message": "ok"}}}})
			cluster("the Secret there already", "basic", "admin", map[string]any{"repository": "team/app", "reference": digest}, running,
				EngineRule{Method: "POST", Path: "^/api/v1/namespaces/trials/secrets$", Status: 409},
				EngineRule{Method: "GET", Path: "^/api/v1/namespaces/trials/secrets/", Body: map[string]any{"type": "x", "metadata": map[string]any{"resourceVersion": "7"}}})
			cluster("the image not resolved", "anon", "admin", map[string]any{"repository": "team/app"}, v2(200, ""),
				EngineRule{Method: "HEAD", Path: "^/v2/team/app/manifests/", Status: 404})
			cluster("the cluster refuses", "anon", "admin", map[string]any{"repository": "team/app", "reference": digest},
				EngineRule{Method: "POST", Path: "^/apis/batch/v1/namespaces/trials/jobs$", Status: 403, Body: map[string]any{"message": "forbidden"}})
			cluster("not allowed", "anon", "admin", map[string]any{"repository": "other/x"})
			cluster("a bad body", "anon", "admin", map[string]any{"repository": strings.Repeat("a", 256), "reference": 5})
			cluster("an observer", "anon", "obs_flag", map[string]any{"repository": "team/app"})
			cluster("a token", "anon", "nobody", map[string]any{"repository": "team/app"})
			s[len(s)-1].Auth = Bearer("tab_data_service_write_0000")
			return s
		},
	})
}
