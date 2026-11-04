package api

// routes/registries.py: what speaks to a registry (services/container_registry.py, ported in
// internal/registry): the administrators' test and tags, the flow editor's tags and an image's
// configuration, and the test of a pull from the cluster (container_cluster.try_pull).

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/registry"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("registries_runtime", func(d *Deps) []server.Route {
		guarded := func(pattern string, admin bool, noToken map[string]string, h authedFunc) http.Handler {
			return server.Authed(d.Auth, pattern, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
				if scRequire(d, w, r, p, admin, noToken) {
					h(w, r, p)
				}
			})
		}
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/admin/registries/{registry_id}/test",
				Handler: guarded("/admin/registries/{registry_id}/test", true, regNoRegistryFromToken, registryTest(d))},
			{Method: http.MethodGet, Pattern: "/admin/registries/{registry_id}/tags",
				Handler: guarded("/admin/registries/{registry_id}/tags", false, nil, registryTags(d, false))},
			{Method: http.MethodGet, Pattern: "/registries/{registry_id}/tags",
				Handler: server.Authed(d.Auth, "/registries/{registry_id}/tags", registryTags(d, true))},
			{Method: http.MethodGet, Pattern: "/registries/{registry_id}/image",
				Handler: server.Authed(d.Auth, "/registries/{registry_id}/image", registryImage(d))},
		}
	})
}

// regClient is client_of: what container_registry needs, the password decrypted.
func regClient(d *Deps, r *regRow) (registry.Registry, error) {
	c := registry.Registry{Host: r.Host, Protocol: r.Protocol, TLS: r.TLS, CACert: r.CACert, Username: r.Username}
	if r.PasswordEnc != nil && *r.PasswordEnc != "" {
		plain, err := d.Fernet.Decrypt(*r.PasswordEnc)
		if err != nil {
			return c, err
		}
		c.Password = plain
	}
	return c, nil
}

// regServiceDown is main.py's answer to an httpx.TransportError the route did not catch.
func regServiceDown(w http.ResponseWriter) {
	detail(w, http.StatusServiceUnavailable, "A service Tabularia calls is not answering: try again in a moment")
}

// regUnexpected answers an error that is not a RegistryError as Python's would be answered.
func regUnexpected(d *Deps, w http.ResponseWriter, what string, err error) {
	var te *registry.TransportError
	if errors.As(err, &te) {
		d.Log.Warn(what+": a service not answering", "err", err)
		regServiceDown(w)
		return
	}
	fail(d, w, what, err)
}

// registryTest is POST /admin/registries/{registry_id}/test (trial): the address, the
// certificate, the credentials and, with an allowlist that names a repository, the right to
// read it; kept on the registry.
func registryTest(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "registry_id")
		if !ok || !regIsOn(w) {
			return
		}
		ctx := r.Context()
		reg, ok := regOr404(d, w, ctx, ids[0])
		if !ok {
			return
		}
		repo := ""
		for _, x := range regPatterns(reg.Repositories) {
			if !regContainsStar(x) {
				repo = x
				break
			}
		}
		client, err := regClient(d, reg)
		if err != nil {
			fail(d, w, "registries: the password", err)
			return
		}
		out := pyjson.Object{}
		found, err := registry.Ping(ctx, client, repo)
		var re *registry.Error
		switch {
		case err == nil:
			who := "anonymously"
			if reg.Username != "" {
				who = "as " + reg.Username
			}
			msg := "The registry answers and lets Tabularia in " + who + "; name a repository in the allowlist to test reading it"
			if repo != "" {
				msg = fmt.Sprintf("Read %s %s: %d tags shown", *found.Repository, who, len(found.Tags))
			}
			var repoOut, tags any
			if found.Repository != nil {
				repoOut = *found.Repository
				list := []any{}
				for _, t := range found.Tags {
					list = append(list, t)
				}
				tags = list
			}
			out = pyjson.Object{{Key: "ok", Value: true}, {Key: "message", Value: msg}, {Key: "auth", Value: found.Auth},
				{Key: "repository", Value: repoOut}, {Key: "tags", Value: tags}}
		case errors.As(err, &re):
			out = pyjson.Object{{Key: "ok", Value: false}, {Key: "message", Value: re.Message}, {Key: "auth", Value: nil},
				{Key: "repository", Value: nil}, {Key: "tags", Value: nil}}
		default:
			regUnexpected(d, w, "registries: the test", err)
			return
		}
		okv := out[0].Value.(bool)
		msg := out[1].Value.(string)
		if _, err := d.DB.Exec(ctx, `UPDATE container_registries SET last_test_at = $1, last_test_ok = $2, last_test_message = $3
			WHERE id = $4`, nowNaive(), okv, msg, reg.ID); err != nil {
			fail(d, w, "registries: the test's result", err)
			return
		}
		outcome := "success"
		if !okv {
			outcome = "failure"
		}
		kind := "container_registry"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.test", Outcome: outcome, TargetType: &kind,
			TargetID: &reg.ID, TargetLabel: &reg.Name, Detail: pyjson.Object{{Key: "ok", Value: okv},
				{Key: "message", Value: runeClip(msg, 500)}, {Key: "repository", Value: out[3].Value}}})
		writePy(w, out)
	}
}

func regContainsStar(s string) bool {
	for _, c := range s {
		if c == '*' {
			return true
		}
	}
	return false
}

// runeClip is text[:n] on a Python str (code points).
func runeClip(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// regQuery reads the routes' query parameters after their path one, as FastAPI.
func regQuery(w http.ResponseWriter, r *http.Request, withReference bool) (int64, string, string, bool) {
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "registry_id")
	q := pyd.ParseQuery(r.URL.RawQuery)
	repo, _ := ps.Query(q, "repository", pyd.Str().MaxLen(255)).(string)
	ref := ""
	if withReference {
		ref, _ = ps.Query(q, "reference", pyd.Str().MaxLen(200), "latest").(string)
	}
	if !validated(w, &ps) {
		return 0, "", "", false
	}
	return ids[0], repo, ref, true
}

// regUsable is the editor's lookup: a registry this person may use, or a 404 that says
// nothing about the others; then the repository among the images they may use.
func regUsable(d *Deps, w http.ResponseWriter, ctx context.Context, p *auth.Principal, id int64, repo string) (*regRow, string, bool) {
	reg, err := regGet(ctx, d.DB, id)
	if err != nil {
		fail(d, w, "registries: the registry", err)
		return nil, "", false
	}
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		fail(d, w, "registries: the roles", err)
		return nil, "", false
	}
	var grants map[int64][]string
	var all map[int64]bool
	if reg != nil && reg.Enabled && !admin {
		if grants, all, err = regGrants(ctx, d.DB, p.User.ID); err != nil {
			fail(d, w, "registries: the grants", err)
			return nil, "", false
		}
	}
	_, granted := grants[id]
	if reg == nil || !reg.Enabled || !(admin || granted || all[id]) {
		detail(w, http.StatusNotFound, "Container registry not found")
		return nil, "", false
	}
	name := registry.RepositoryName(reg.Host, repo)
	// registry_access.repository_allowed
	ok := registry.Allowed(regPatterns(reg.Repositories), name) && (admin || all[id] || registry.Allowed(grants[id], name))
	if !ok {
		detail(w, http.StatusForbidden, fmt.Sprintf("%s is not among the images of %s you may use", name, reg.Name))
		return nil, "", false
	}
	return reg, name, true
}

// registryTags is GET /admin/registries/{registry_id}/tags (tag) and, for the flow editor,
// GET /registries/{registry_id}/tags (editor_tag).
func registryTags(d *Deps, editor bool) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		id, repo, _, ok := regQuery(w, r, false)
		if !ok || !regIsOn(w) {
			return
		}
		ctx := r.Context()
		var reg *regRow
		var name string
		if editor {
			if reg, name, ok = regUsable(d, w, ctx, p, id, repo); !ok {
				return
			}
		} else {
			if reg, ok = regOr404(d, w, ctx, id); !ok {
				return
			}
			name = registry.RepositoryName(reg.Host, repo)
			if !registry.Allowed(regPatterns(reg.Repositories), name) {
				detail(w, http.StatusForbidden, fmt.Sprintf("%s is not among the repositories allowed for %s", name, reg.Name))
				return
			}
		}
		client, err := regClient(d, reg)
		if err != nil {
			fail(d, w, "registries: the password", err)
			return
		}
		tags, err := registry.Tags(ctx, client, name)
		var re *registry.Error
		if errors.As(err, &re) {
			detail(w, http.StatusBadGateway, re.Message)
			return
		}
		if err != nil {
			regUnexpected(d, w, "registries: the tags", err)
			return
		}
		list := []any{}
		for _, t := range tags {
			list = append(list, t)
		}
		writePy(w, pyjson.Object{{Key: "repository", Value: name}, {Key: "tags", Value: list}})
	}
}

// registryImage is GET /registries/{registry_id}/image (editor_image): the configuration of
// an image this person may use.
func registryImage(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		id, repo, ref, ok := regQuery(w, r, true)
		if !ok || !regIsOn(w) {
			return
		}
		ctx := r.Context()
		reg, name, ok := regUsable(d, w, ctx, p, id, repo)
		if !ok {
			return
		}
		client, err := regClient(d, reg)
		if err != nil {
			fail(d, w, "registries: the password", err)
			return
		}
		if ref == "" {
			ref = "latest"
		}
		out, err := registry.ImageConfig(ctx, client, name, ref)
		var re *registry.Error
		if errors.As(err, &re) {
			detail(w, http.StatusBadGateway, re.Message)
			return
		}
		if err != nil {
			regUnexpected(d, w, "registries: the image", err)
			return
		}
		// ImageConfigOut: created, working_dir and user are texts or null (a 500 otherwise)
		for _, k := range []string{"created", "working_dir", "user"} {
			if v, _ := out.Get(k); v != nil {
				if _, isStr := v.(string); !isStr {
					fail(d, w, "registries: the image's "+k+" is not a text", errors.New("response validation"))
					return
				}
			}
		}
		writePy(w, out)
	}
}
