package api

// routes/scripting.py: the scripting environments (Admin → Scripting environments) and
// what the flow editor reads of them. Ported: the section, the bucket and its test, the
// environments, their versions and variables, the editor's list and completions. Left to
// the Python gateway: the package search (it reads PyPI or CRAN), the lint (Ruff runs in
// the Python process), a Script node's trial (run by the Python process that answers) and
// the outputs of a flow's nodes (services/script_runs).

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("scripting", func(d *Deps) []server.Route {
		observer := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
					if scRequire(d, w, r, p, false, nil) {
						h(d, w, r, p)
					}
				})}
		}
		// require_superuser, then _no_token (the route's `dependencies`), then the body
		admin := func(method, pattern string, h scBodyHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: scWithBody(d, pattern,
				func(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
					if scRequire(d, w, r, p, true, scNoScriptingFromToken) {
						h(d, w, r, p, body)
					}
				})}
		}
		person := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			observer(http.MethodGet, "/admin/scripting", readScripting),
			admin(http.MethodPut, "/admin/scripting/storage", writeStorage),
			admin(http.MethodPost, "/admin/scripting/storage/test", trialStorage),
			observer(http.MethodGet, "/admin/scripting/environments/{environment_id}", readEnvironment),
			admin(http.MethodPost, "/admin/scripting/environments", createEnvironment),
			admin(http.MethodPut, "/admin/scripting/environments/{environment_id}", changeEnvironment),
			admin(http.MethodPost, "/admin/scripting/environments/{environment_id}/rebuild", rebuildEnvironment),
			admin(http.MethodPost, "/admin/scripting/environments/{environment_id}/versions/{version_id}/current", makeCurrent),
			admin(http.MethodDelete, "/admin/scripting/environments/{environment_id}", deleteEnvironment),
			admin(http.MethodPut, "/admin/scripting/environments/{environment_id}/variables/{name}", writeVariable),
			admin(http.MethodDelete, "/admin/scripting/environments/{environment_id}/variables/{name}", deleteVariable),
			person(http.MethodGet, "/scripting", editorEnvironments),
			person(http.MethodGet, "/scripting/environments/{environment_id}/completions", editorCompletions),
		}
	})
}

// scNoScriptingFromToken is api_tokens.NO_SCRIPTING_FROM_TOKEN.
var scNoScriptingFromToken = map[string]string{
	"en": "An API token cannot change the scripting environments or their bucket: do it from the interface",
	"it": "Un token API non cambia gli ambienti di scripting né il loro bucket: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier les environnements de script ni leur bucket : faites-le depuis l'interface",
	"de": "Ein API-Token kann die Skriptumgebungen und ihren Bucket nicht ändern: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar los entornos de scripting ni su bucket: hazlo desde la interfaz",
}

// scRequire is require_observer (admin false) or require_superuser, then, with a table,
// deny_if_from_token. False once answered.
func scRequire(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, admin bool, noToken map[string]string) bool {
	c := perm.For(d.DB, &p.User)
	roles, err := c.Roles(r.Context())
	if err != nil {
		scFailed(d, w, "reading the roles", err)
		return false
	}
	if admin && !roles.Admin {
		scRefuse(w, http.StatusForbidden, "Admin privileges required")
		return false
	}
	if !admin && !roles.Observer {
		scRefuse(w, http.StatusForbidden, "Administrative read access required")
		return false
	}
	if noToken != nil {
		if refusal := apitokens.DenyIfFromToken(r, p, noToken); refusal != nil {
			server.Refuse(w, refusal)
			return false
		}
	}
	return true
}

// scriptingIsOn is _is_on: the 409 of an installation that cannot build environments.
func scriptingIsOn(w http.ResponseWriter) bool {
	switch scriptcfg.Get().DisabledReason() {
	case "kubernetes":
		scRefuse(w, http.StatusConflict, "Scripting environments need Kubernetes (SCRIPTS__ENABLED)")
		return false
	case "configuration":
		scRefuse(w, http.StatusConflict, "Scripting environments are not configured (SCRIPTS__NAMESPACE and SCRIPTS__REGISTRY)")
		return false
	}
	return true
}

func scNullable(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// ── the bucket ────────────────────────────────────────────────────────────────

type scStorage struct {
	Endpoint, Region, Bucket, AccessKey string
	SecretKeyEnc                        *string
	UpdatedAt                           *time.Time
	UpdatedBy                           *string
	exists                              bool
}

// scStorageOf is _storage: the row, or an empty one.
func scStorageOf(ctx context.Context, q perm.Querier) (*scStorage, error) {
	s := &scStorage{}
	err := q.QueryRow(ctx, `SELECT endpoint, region, bucket, access_key, secret_key_enc, updated_at, updated_by FROM script_storage
		WHERE id = 1`).Scan(&s.Endpoint, &s.Region, &s.Bucket, &s.AccessKey, &s.SecretKeyEnc, &s.UpdatedAt, &s.UpdatedBy)
	if scNoRows(err) {
		return &scStorage{}, nil
	}
	s.exists = err == nil
	return s, err
}

type storageOut struct {
	Endpoint     string  `json:"endpoint"`
	Region       string  `json:"region"`
	Bucket       string  `json:"bucket"`
	AccessKey    string  `json:"access_key"`
	SecretKeySet bool    `json:"secret_key_set"`
	UpdatedAt    *string `json:"updated_at"`
	UpdatedBy    *string `json:"updated_by"`
}

func (s *scStorage) out() storageOut {
	return storageOut{s.Endpoint, s.Region, s.Bucket, s.AccessKey, s.SecretKeyEnc != nil && *s.SecretKeyEnc != "", isoPtr(s.UpdatedAt), s.UpdatedBy}
}

// scURLSplit is urllib.parse.urlsplit's scheme and hostname.
func scURLSplit(u string) (scheme, host string) {
	if i := strings.Index(u, ":"); i > 0 {
		ok := true
		for j, c := range u[:i] {
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || j > 0 && (c >= '0' && c <= '9' || c == '+' || c == '-' || c == '.')) {
				ok = false
			}
		}
		if ok {
			scheme, u = strings.ToLower(u[:i]), u[i+1:]
		}
	}
	if !strings.HasPrefix(u, "//") {
		return scheme, ""
	}
	netloc := u[2:]
	if i := strings.IndexAny(netloc, "/?#"); i >= 0 {
		netloc = netloc[:i]
	}
	if i := strings.LastIndex(netloc, "@"); i >= 0 {
		netloc = netloc[i+1:]
	}
	if strings.HasPrefix(netloc, "[") {
		if i := strings.Index(netloc, "]"); i >= 0 {
			return scheme, strings.ToLower(netloc[1:i])
		}
		return scheme, ""
	}
	if i := strings.Index(netloc, ":"); i >= 0 {
		netloc = netloc[:i]
	}
	return scheme, strings.ToLower(netloc)
}

// writeStorage is PUT /admin/scripting/storage (write_storage): only what changes; the
// secret key: a text replaces it, "" removes it.
func writeStorage(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	in := params.Body(body, gatewaymodels.StorageUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	s, err := scStorageOf(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "scripting: the bucket", err)
		return
	}
	changes := pyjson.Object{}
	fields := []struct {
		name string
		into *string
	}{{"endpoint", &s.Endpoint}, {"region", &s.Region}, {"bucket", &s.Bucket}, {"access_key", &s.AccessKey}}
	for _, f := range fields {
		if v, ok := in.Get(f.name).(string); ok && pyd.Strip(v) != *f.into {
			changes = append(changes, pyjson.KV{Key: f.name, Value: pyjson.Object{{Key: "from", Value: *f.into}, {Key: "to", Value: pyd.Strip(v)}}})
			*f.into = pyd.Strip(v)
		}
	}
	if s.Endpoint != "" {
		if scheme, host := scURLSplit(s.Endpoint); (scheme != "http" && scheme != "https") || host == "" {
			scRefuse(w, http.StatusUnprocessableEntity, "The endpoint is an http(s) address (empty = AWS)")
			return
		}
	}
	if v, ok := in.Get("secret_key").(string); ok {
		if pyd.Strip(v) != "" {
			enc, err := d.Fernet.Encrypt(pyd.Strip(v))
			if err != nil {
				scFailed(d, w, "scripting: encrypting the key", err)
				return
			}
			s.SecretKeyEnc = &enc
			changes = append(changes, pyjson.KV{Key: "secret_key", Value: "changed"})
		} else {
			s.SecretKeyEnc = nil
			changes = append(changes, pyjson.KV{Key: "secret_key", Value: "removed"})
		}
	}
	if len(changes) == 0 {
		server.JSON(w, http.StatusOK, s.out())
		return
	}
	now := time.Now().UTC()
	s.UpdatedAt, s.UpdatedBy = &now, &p.User.Email
	_, err = d.DB.Exec(ctx, `INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc, updated_at, updated_by)
		VALUES (1, $1, $2, $3, $4, $5, $6, $7) ON CONFLICT (id) DO UPDATE SET endpoint = $1, region = $2, bucket = $3,
		access_key = $4, secret_key_enc = $5, updated_at = $6, updated_by = $7`,
		s.Endpoint, s.Region, s.Bucket, s.AccessKey, s.SecretKeyEnc, now, p.User.Email)
	if err != nil {
		scFailed(d, w, "scripting: saving the bucket", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.storage_update", TargetType: scPtrStr("scripting"),
		TargetLabel: scNullable(s.Bucket), Detail: pyjson.Object{{Key: "changes", Value: changes}}})
	server.JSON(w, http.StatusOK, s.out())
}

type storageTestOut struct {
	OK      bool   `json:"ok"`
	Message string `json:"message"`
	CanList any    `json:"can_list"`
}

// trialStorage is POST /admin/scripting/storage/test (trial_storage): a probe object
// written, read and deleted by the engine with the key.
func trialStorage(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ctx := r.Context()
	s, err := scStorageOf(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "scripting: the bucket", err)
		return
	}
	if s.Bucket == "" || s.AccessKey == "" || s.SecretKeyEnc == nil || *s.SecretKeyEnc == "" {
		scRefuse(w, http.StatusUnprocessableEntity, "The bucket, the access key and the secret key are needed")
		return
	}
	resp, raw, err := d.Engine.JSON(ctx, http.MethodPost, "/scripts/storage/test", pyjson.Object{{Key: "connection", Value: pyjson.Object{
		{Key: "endpoint_url", Value: s.Endpoint}, {Key: "access_key", Value: s.AccessKey}, {Key: "secret_key_encrypted", Value: *s.SecretKeyEnc},
		{Key: "region", Value: s.Region}, {Key: "bucket", Value: s.Bucket}}}})
	if err != nil {
		scFailed(d, w, "scripting: the engine", err)
		return
	}
	var out storageTestOut
	if resp.StatusCode >= 400 {
		detail := string(raw)
		if v, err := pyjson.Loads(string(raw)); err == nil {
			o, ok := v.(pyjson.Object)
			if !ok {
				scFailed(d, w, "scripting: the engine's refusal is not an object", nil)
				return
			}
			if dv, has := o.Get("detail"); has {
				detail = pyjson.Str(dv)
			}
		}
		out = storageTestOut{OK: false, Message: scClip(detail, 500)}
	} else {
		v, err := pyjson.Loads(string(raw))
		o, isObj := v.(pyjson.Object)
		if err != nil || !isObj {
			scFailed(d, w, "scripting: the engine's answer", err)
			return
		}
		okV, errs1 := pyd.Bool().Validate(scGet(o, "ok"))
		msgV, errs2 := pyd.Str().Validate(scGet(o, "message"))
		_, hasOK := o.Get("ok")
		_, hasMsg := o.Get("message")
		var canList any
		var errs3 []*pyd.Error
		if cl, has := o.Get("can_list"); has {
			canList, errs3 = pyd.Optional(pyd.Bool()).Validate(cl)
		}
		if !hasOK || !hasMsg || errs1 != nil || errs2 != nil || errs3 != nil {
			scFailed(d, w, "scripting: the engine's answer does not validate", nil) // TestOut(**…): a ValidationError
			return
		}
		out = storageTestOut{OK: okV.(bool), Message: msgV.(string), CanList: canList}
	}
	outcome := "success"
	if !out.OK {
		outcome = "failure"
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.storage_test", Outcome: outcome,
		TargetType: scPtrStr("scripting"), TargetLabel: scPtrStr(s.Bucket), Detail: pyjson.Object{{Key: "can_list", Value: out.CanList}}})
	server.JSON(w, http.StatusOK, out)
}

// ── environments ──────────────────────────────────────────────────────────────

type scVersion struct {
	ID              int64   `json:"id"`
	Number          int64   `json:"number"`
	LanguageVersion string  `json:"language_version"`
	BaseImage       string  `json:"base_image"`
	Packages        string  `json:"packages"`
	Status          string  `json:"status"`
	ImageRef        *string `json:"image_ref"`
	Lock            *string `json:"lock"`
	BuildLog        *string `json:"build_log"`
	Error           *string `json:"error"`
	CreatedAt       string  `json:"created_at"`
	CreatedBy       *string `json:"created_by"`
	StartedAt       *string `json:"started_at"`
	FinishedAt      *string `json:"finished_at"`
	Current         bool    `json:"current"`
	completions     *string
}

// scVersions is _versions: newest first.
func scVersions(ctx context.Context, q perm.Querier, envID int64, current *int64) ([]scVersion, error) {
	rows, err := q.Query(ctx, `SELECT id, number, language_version, base_image, packages, status, image_ref, lock, build_log, error,
		created_at, created_by, started_at, finished_at, completions FROM script_environment_versions WHERE environment_id = $1
		ORDER BY number DESC`, envID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []scVersion{}
	for rows.Next() {
		var v scVersion
		var created time.Time
		var started, finished *time.Time
		if err := rows.Scan(&v.ID, &v.Number, &v.LanguageVersion, &v.BaseImage, &v.Packages, &v.Status, &v.ImageRef, &v.Lock,
			&v.BuildLog, &v.Error, &created, &v.CreatedBy, &started, &finished, &v.completions); err != nil {
			return nil, err
		}
		v.CreatedAt, v.StartedAt, v.FinishedAt = pyjson.Isoformat(created), isoPtr(started), isoPtr(finished)
		v.Current = current != nil && *current == v.ID
		out = append(out, v)
	}
	return out, rows.Err()
}

// scEnvGroups is _group_names: the groups' ids and names, by name.
func scEnvGroups(ctx context.Context, q perm.Querier, envID int64) ([]int64, []string, error) {
	rows, err := q.Query(ctx, `SELECT g.id, g.name FROM groups g JOIN script_environment_groups eg ON eg.group_id = g.id
		WHERE eg.environment_id = $1 ORDER BY g.name`, envID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	ids, names := []int64{}, []string{}
	for rows.Next() {
		var id int64
		var n string
		if err := rows.Scan(&id, &n); err != nil {
			return nil, nil, err
		}
		ids, names = append(ids, id), append(names, n)
	}
	return ids, names, rows.Err()
}

type scVariable struct {
	Name          string  `json:"name"`
	Secret        bool    `json:"secret"`
	Value         *string `json:"value"`
	FromTabularia bool    `json:"from_tabularia"`
	UpdatedAt     string  `json:"updated_at"`
	UpdatedBy     *string `json:"updated_by"`
}

// scEnvVariables is _variables: by name, a secret one without its value.
func scEnvVariables(ctx context.Context, q perm.Querier, envID int64) ([]scVariable, error) {
	rows, err := q.Query(ctx, `SELECT name, secret, value, from_tabularia, updated_at, updated_by FROM script_environment_variables
		WHERE environment_id = $1 ORDER BY name`, envID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []scVariable{}
	for rows.Next() {
		var v scVariable
		var value string
		var at time.Time
		if err := rows.Scan(&v.Name, &v.Secret, &value, &v.FromTabularia, &at, &v.UpdatedBy); err != nil {
			return nil, err
		}
		if !v.Secret {
			v.Value = &value
		}
		v.UpdatedAt = pyjson.Isoformat(at)
		out = append(out, v)
	}
	return out, rows.Err()
}

// flowsUsingEnvironment is flows_using: the flows with a Script node on the environment,
// each name once, sorted.
func flowsUsingEnvironment(ctx context.Context, q perm.Querier, envID int64) ([]string, error) {
	rows, err := q.Query(ctx, `SELECT name, definition FROM flows WHERE definition LIKE '%' || '"script"' || '%'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	seen := map[string]bool{}
	out := []string{}
	for rows.Next() {
		var name, def string
		if err := rows.Scan(&name, &def); err != nil {
			return nil, err
		}
		if def == "" {
			def = "{}"
		}
		v, err := pyjson.Loads(def)
		if err != nil {
			continue
		}
		doc, ok := v.(pyjson.Object)
		if !ok {
			return nil, fmt.Errorf("flow %q: the definition is not an object", name)
		}
		nodes, _ := scGet(doc, "nodes").([]any)
		for _, n := range nodes {
			o, ok := n.(pyjson.Object)
			if !ok || scGet(o, "type") != "script" {
				continue
			}
			data, _ := scGet(o, "data").(pyjson.Object)
			if pyjson.Equal(scGet(data, "environmentId"), envID) {
				if !seen[name] {
					seen[name] = true
					out = append(out, name)
				}
				break
			}
		}
	}
	sort.Strings(out)
	return out, rows.Err()
}

type environmentSummary struct {
	ID              int64    `json:"id"`
	Name            string   `json:"name"`
	Description     string   `json:"description"`
	Language        string   `json:"language"`
	Enabled         bool     `json:"enabled"`
	LanguageVersion *string  `json:"language_version"`
	CurrentNumber   *int64   `json:"current_number"`
	LatestNumber    *int64   `json:"latest_number"`
	LatestStatus    *string  `json:"latest_status"`
	Variables       int64    `json:"variables"`
	Groups          []string `json:"groups"`
	UpdatedAt       string   `json:"updated_at"`
}

type environmentOut struct {
	environmentSummary
	Packages          string       `json:"packages"`
	CPURequest        string       `json:"cpu_request"`
	CPULimit          string       `json:"cpu_limit"`
	MemoryRequest     string       `json:"memory_request"`
	MemoryLimit       string       `json:"memory_limit"`
	MaxTimeoutSeconds int64        `json:"max_timeout_seconds"`
	GroupIDs          []int64      `json:"group_ids"`
	VariableList      []scVariable `json:"variable_list"`
	Versions          []scVersion  `json:"versions"`
	FlowsUsing        []string     `json:"flows_using"`
}

// scSummary is _summary.
func scSummary(ctx context.Context, q perm.Querier, e *scEnvironment, versions []scVersion) (environmentSummary, error) {
	out := environmentSummary{ID: e.ID, Name: e.Name, Description: e.Description, Language: e.Language, Enabled: e.Enabled,
		UpdatedAt: pyjson.Isoformat(e.UpdatedAt)}
	if len(versions) > 0 {
		out.LanguageVersion, out.LatestNumber, out.LatestStatus = &versions[0].LanguageVersion, &versions[0].Number, &versions[0].Status
	}
	for i := range versions {
		if versions[i].Current {
			out.CurrentNumber = &versions[i].Number
			break
		}
	}
	var err error
	if _, out.Groups, err = scEnvGroups(ctx, q, e.ID); err != nil {
		return out, err
	}
	err = q.QueryRow(ctx, `SELECT count(*) FROM script_environment_variables WHERE environment_id = $1`, e.ID).Scan(&out.Variables)
	return out, err
}

// scEnvOut is _out.
func scEnvOut(ctx context.Context, q perm.Querier, e *scEnvironment) (*environmentOut, error) {
	versions, err := scVersions(ctx, q, e.ID, e.CurrentVersionID)
	if err != nil {
		return nil, err
	}
	sum, err := scSummary(ctx, q, e, versions)
	if err != nil {
		return nil, err
	}
	out := &environmentOut{environmentSummary: sum, CPURequest: e.CPURequest, CPULimit: e.CPULimit, MemoryRequest: e.MemoryRequest,
		MemoryLimit: e.MemoryLimit, MaxTimeoutSeconds: e.MaxTimeoutSeconds, Versions: versions}
	if len(versions) > 0 {
		out.Packages = versions[0].Packages
	}
	if out.GroupIDs, _, err = scEnvGroups(ctx, q, e.ID); err != nil {
		return nil, err
	}
	if out.VariableList, err = scEnvVariables(ctx, q, e.ID); err != nil {
		return nil, err
	}
	out.FlowsUsing, err = flowsUsingEnvironment(ctx, q, e.ID)
	return out, err
}

func scSendEnv(d *Deps, w http.ResponseWriter, ctx context.Context, id int64, status int) {
	e, err := scGetEnv(ctx, d.DB, id)
	if err == nil && e == nil {
		err = fmt.Errorf("environment %d is gone", id)
	}
	if err != nil {
		scFailed(d, w, "scripting: reading the environment", err)
		return
	}
	out, err := scEnvOut(ctx, d.DB, e)
	if err != nil {
		scFailed(d, w, "scripting: the environment", err)
		return
	}
	server.JSON(w, status, out)
}

// scVersionKeys is the sort key of a language version: its dot-separated parts, the
// digits as numbers. A list mixing numbers and texts at one place is a TypeError in Python.
func scVersionLess(a, b string) (bool, bool) {
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(pa) && i < len(pb); i++ {
		na, ea := strconv.Atoi(pa[i])
		nb, eb := strconv.Atoi(pb[i])
		da, db := scIsDigits(pa[i]) && ea == nil, scIsDigits(pb[i]) && eb == nil
		switch {
		case da && db:
			if na != nb {
				return na < nb, true
			}
		case !da && !db:
			if pa[i] != pb[i] {
				return pa[i] < pb[i], true
			}
		default:
			return false, false
		}
	}
	return len(pa) < len(pb), true
}

func scIsDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

type scriptingOut struct {
	DisabledReason *string              `json:"disabled_reason"`
	Languages      pyjson.Object        `json:"languages"`
	Registry       string               `json:"registry"`
	Storage        storageOut           `json:"storage"`
	Environments   []environmentSummary `json:"environments"`
	Editable       bool                 `json:"editable"`
}

// readScripting is GET /admin/scripting (read).
func readScripting(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	cfg := scriptcfg.Get()
	languages := pyjson.Object{}
	images, err := cfg.Images()
	if err != nil {
		languages = pyjson.Object{{Key: "python", Value: []string{}}, {Key: "r", Value: []string{}}}
	} else {
		for _, lang := range []string{"python", "r"} {
			var vs []string
			for _, im := range images[lang] {
				vs = append(vs, im.Version)
			}
			if vs == nil {
				vs = []string{}
			}
			mixed := false
			sort.SliceStable(vs, func(i, j int) bool {
				less, ok := scVersionLess(vs[i], vs[j])
				if !ok {
					mixed = true
				}
				return less
			})
			if mixed {
				scFailed(d, w, "scripting: language versions that cannot be ordered", nil)
				return
			}
			languages = append(languages, pyjson.KV{Key: lang, Value: vs})
		}
	}
	storage, err := scStorageOf(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "scripting: the bucket", err)
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT `+scEnvColumns+` FROM script_environments ORDER BY lower(name)`)
	if err != nil {
		scFailed(d, w, "scripting: the environments", err)
		return
	}
	var envs []scEnvironment
	for rows.Next() {
		e, err := scScanEnv(rows)
		if err != nil {
			rows.Close()
			scFailed(d, w, "scripting: an environment", err)
			return
		}
		envs = append(envs, *e)
	}
	rows.Close()
	out := scriptingOut{DisabledReason: scNullable(cfg.DisabledReason()), Languages: languages, Registry: cfg.Scripts.Registry,
		Storage: storage.out(), Environments: []environmentSummary{}}
	for i := range envs {
		versions, err := scVersions(ctx, d.DB, envs[i].ID, envs[i].CurrentVersionID)
		if err != nil {
			scFailed(d, w, "scripting: the versions", err)
			return
		}
		sum, err := scSummary(ctx, d.DB, &envs[i], versions)
		if err != nil {
			scFailed(d, w, "scripting: an environment", err)
			return
		}
		out.Environments = append(out.Environments, sum)
	}
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		scFailed(d, w, "scripting: the roles", err)
		return
	}
	out.Editable = admin && p.TokenName == ""
	server.JSON(w, http.StatusOK, out)
}

// scEnvironmentOr404 is _environment.
func scEnvironmentOr404(d *Deps, w http.ResponseWriter, ctx context.Context, id int64) (*scEnvironment, bool) {
	e, err := scGetEnv(ctx, d.DB, id)
	if err != nil {
		scFailed(d, w, "scripting: the environment", err)
		return nil, false
	}
	if e == nil {
		scRefuse(w, http.StatusNotFound, "Scripting environment not found")
		return nil, false
	}
	return e, true
}

// readEnvironment is GET /admin/scripting/environments/{environment_id}.
func readEnvironment(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "environment_id")
	if !ok {
		return
	}
	e, ok := scEnvironmentOr404(d, w, r.Context(), ids[0])
	if !ok {
		return
	}
	out, err := scEnvOut(r.Context(), d.DB, e)
	if err != nil {
		scFailed(d, w, "scripting: the environment", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// scEnvName is _name: stripped, not empty, no other environment with it (case aside).
func scEnvName(ctx context.Context, q perm.Querier, name string, id int64) (string, *auth.Refusal, error) {
	name = pyd.Strip(name)
	if name == "" {
		return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The environment needs a name"}, nil
	}
	var clash int64
	err := q.QueryRow(ctx, `SELECT id FROM script_environments WHERE lower(name) = $1`, pyd.Lower(name)).Scan(&clash)
	if err != nil && !scNoRows(err) {
		return "", nil, err
	}
	if err == nil && clash != id {
		return "", &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("An environment named «%s» already exists", name)}, nil
	}
	return name, nil, nil
}

// scLanguageVersion is _language_version: the base image of a version offered.
func scLanguageVersion(lang, version string) (string, *auth.Refusal, error) {
	images, err := scriptcfg.Get().Images()
	if err != nil {
		return "", nil, err
	}
	var offered []string
	for _, im := range images[lang] {
		if im.Version == version {
			return im.Image, nil, nil
		}
		offered = append(offered, im.Version)
	}
	list := strings.Join(offered, ", ")
	if list == "" {
		list = "none"
	}
	return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("%s %s is not offered here (available: %s)",
		lang, version, list)}, nil
}

var (
	scPySpec        = `(==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+`
	scPyRequirement = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?\s*(` + scPySpec + `(\s*,\s*` + scPySpec + `)*)?\n?$`)
	scRPackage      = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]*(@[0-9][0-9.-]*)?\n?$`)
	scCPU           = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?m?\n?$`)
	scMemory        = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|K|M|G|T)?\n?$`)
	scVariableName  = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*\n?$`)
)

// scPackages is _packages (script_builds.parse_packages): the text stripped, or the 422 of
// the line that is not a package.
func scPackages(lang, text string) (string, *scRefusal) {
	pattern := scRPackage
	if lang == "python" {
		pattern = scPyRequirement
	}
	n := 0
	for i, raw := range scriptnodes.SplitLines(text) {
		line, _, _ := strings.Cut(raw, "#")
		line = pyd.Strip(line)
		if line == "" {
			continue
		}
		if !pattern.MatchString(line) || strings.HasPrefix(strings.ToUpper(line), "TABULARIA_") {
			return "", &scRefusal{http.StatusUnprocessableEntity, pyjson.Object{{Key: "code", Value: "package"},
				{Key: "line", Value: int64(i + 1)}, {Key: "text", Value: line}}}
		}
		n++
	}
	if n > 200 {
		return "", &scRefusal{http.StatusUnprocessableEntity, "at most 200 packages"}
	}
	return pyd.Strip(text), nil
}

// scRefusal is an HTTPException whose detail may be a dict.
type scRefusal struct {
	status int
	detail any
}

func (e *scRefusal) send(w http.ResponseWriter) { scRefuseWith(w, e.status, e.detail) }

func scFromRefusal(r *auth.Refusal) *scRefusal {
	if r == nil {
		return nil
	}
	return &scRefusal{r.Status, r.Detail}
}

// scResources is _resources: every quantity stripped, or the 422 of the first that is not one.
func scResources(e *scEnvironment) *scRefusal {
	for _, f := range []struct {
		name string
		into *string
		re   *regexp.Regexp
	}{{"cpu_request", &e.CPURequest, scCPU}, {"cpu_limit", &e.CPULimit, scCPU}, {"memory_request", &e.MemoryRequest, scMemory},
		{"memory_limit", &e.MemoryLimit, scMemory}} {
		v := pyd.Strip(*f.into)
		if !f.re.MatchString(v) {
			return &scRefusal{http.StatusUnprocessableEntity, fmt.Sprintf("%s: «%s» is not a Kubernetes quantity", f.name, v)}
		}
		*f.into = v
	}
	return nil
}

// scSetGroups is _groups: the environment's groups become `ids`, or the 422 of an unknown one.
func scSetGroups(ctx context.Context, tx pgx.Tx, envID int64, ids []int64) (*scRefusal, error) {
	wanted := map[int64]bool{}
	for _, id := range ids {
		wanted[id] = true
	}
	if len(wanted) > 0 {
		var found int
		if err := tx.QueryRow(ctx, `SELECT count(*) FROM groups WHERE id = ANY($1)`, perm.Sorted(wanted)).Scan(&found); err != nil {
			return nil, err
		}
		if found != len(wanted) {
			return &scRefusal{http.StatusUnprocessableEntity, "Unknown group"}, nil
		}
	}
	if _, err := tx.Exec(ctx, `DELETE FROM script_environment_groups WHERE environment_id = $1 AND NOT (group_id = ANY($2))`,
		envID, perm.Sorted(wanted)); err != nil {
		return nil, err
	}
	_, err := tx.Exec(ctx, `INSERT INTO script_environment_groups (environment_id, group_id) SELECT $1, g FROM unnest($2::int[]) g
		ON CONFLICT DO NOTHING`, envID, perm.Sorted(wanted))
	return nil, err
}

// scNewVersion is _new_version: the environment's row held, the next number, queued.
func scNewVersion(ctx context.Context, tx pgx.Tx, e *scEnvironment, lv, packages, who string) (int64, *scRefusal, error) {
	if _, err := tx.Exec(ctx, `SELECT id FROM script_environments WHERE id = $1 FOR UPDATE`, e.ID); err != nil {
		return 0, nil, err
	}
	var number int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(max(number), 0) + 1 FROM script_environment_versions WHERE environment_id = $1`,
		e.ID).Scan(&number); err != nil {
		return 0, nil, err
	}
	image, refusal, err := scLanguageVersion(e.Language, lv)
	if err != nil || refusal != nil {
		return 0, scFromRefusal(refusal), err
	}
	_, err = tx.Exec(ctx, `INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages,
		status, created_at, created_by) VALUES ($1, $2, $3, $4, $5, 'queued', $6, $7)`, e.ID, number, lv, image, packages,
		time.Now().UTC(), who)
	return number, nil, err
}

func scLines(s string) []string {
	out := scriptnodes.SplitLines(s)
	if out == nil {
		out = []string{}
	}
	return out
}

// createEnvironment is POST /admin/scripting/environments (create_environment).
func createEnvironment(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	in := params.Body(body, gatewaymodels.EnvironmentCreate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	lang, lv := in.Get("language").(string), in.Get("language_version").(string)
	if lang != "python" && lang != "r" {
		scRefuse(w, http.StatusUnprocessableEntity, "The language is python or r")
		return
	}
	if _, refusal, err := scLanguageVersion(lang, lv); scAnswer(d, w, "scripting: the images", refusal, err) {
		return
	}
	name, refusal, err := scEnvName(ctx, d.DB, in.Get("name").(string), 0)
	if scAnswer(d, w, "scripting: the name", refusal, err) {
		return
	}
	e := &scEnvironment{Name: name, Description: pyd.Strip(in.Get("description").(string)), Language: lang,
		Enabled: in.Get("enabled").(bool), CPURequest: in.Get("cpu_request").(string), CPULimit: in.Get("cpu_limit").(string),
		MemoryRequest: in.Get("memory_request").(string), MemoryLimit: in.Get("memory_limit").(string),
		MaxTimeoutSeconds: in.Get("max_timeout_seconds").(int64)}
	if no := scResources(e); no != nil {
		no.send(w)
		return
	}
	packages, no := scPackages(lang, in.Get("packages").(string))
	if no != nil {
		no.send(w)
		return
	}
	var groupIDs []int64
	for _, g := range in.Get("group_ids").([]any) {
		n, _ := g.(int64)
		groupIDs = append(groupIDs, n)
	}
	var stop *scRefusal
	var number int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO script_environments (name, description, language, enabled, current_version_id,
			cpu_request, cpu_limit, memory_request, memory_limit, max_timeout_seconds, created_at, created_by, updated_at, updated_by)
			VALUES ($1, $2, $3, $4, NULL, $5, $6, $7, $8, $9, $10, $11, $10, $11) RETURNING id`, e.Name, e.Description, e.Language,
			e.Enabled, e.CPURequest, e.CPULimit, e.MemoryRequest, e.MemoryLimit, e.MaxTimeoutSeconds, now, p.User.Email).Scan(&e.ID); err != nil {
			return err
		}
		var err error
		if stop, err = scSetGroups(ctx, tx, e.ID, groupIDs); err != nil || stop != nil {
			return scOr(err)
		}
		number, stop, err = scNewVersion(ctx, tx, e, lv, packages, p.User.Email)
		if err != nil || stop != nil {
			return scOr(err)
		}
		return nil
	})
	if stop != nil {
		stop.send(w)
		return
	}
	if err != nil {
		scFailed(d, w, "scripting: creating the environment", err)
		return
	}
	_ = number
	groups := []any{}
	for _, g := range groupIDs {
		groups = append(groups, g)
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.environment_create", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: pyjson.Object{{Key: "language", Value: lang}, {Key: "language_version", Value: lv},
			{Key: "packages", Value: scLines(packages)}, {Key: "groups", Value: groups}}})
	scSendEnv(d, w, ctx, e.ID, http.StatusCreated)
}

// scOr is the error that ends a transaction: a refusal rolls it back too.
func scOr(err error) error {
	if err != nil {
		return err
	}
	return scRefused
}

// changeEnvironment is PUT /admin/scripting/environments/{environment_id} (change_environment).
func changeEnvironment(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	eid := params.Path("environment_id", r.PathValue("environment_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.EnvironmentUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	id, ok := eid.(int64)
	if !ok {
		id = -1
	}
	e, ok := scEnvironmentOr404(d, w, ctx, id)
	if !ok {
		return
	}
	changes := pyjson.Object{}
	if v, ok := in.Get("name").(string); ok && pyd.Strip(v) != e.Name {
		name, refusal, err := scEnvName(ctx, d.DB, v, e.ID)
		if scAnswer(d, w, "scripting: the name", refusal, err) {
			return
		}
		changes = append(changes, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: e.Name}, {Key: "to", Value: name}}})
		e.Name = name
	}
	texts := []struct {
		name string
		into *string
	}{{"description", &e.Description}}
	for _, f := range texts {
		if v, ok := in.Get(f.name).(string); ok && pyd.Strip(v) != *f.into {
			changes = append(changes, pyjson.KV{Key: f.name, Value: pyjson.Object{{Key: "from", Value: *f.into}, {Key: "to", Value: pyd.Strip(v)}}})
			*f.into = pyd.Strip(v)
		}
	}
	if v, ok := in.Get("enabled").(bool); ok && v != e.Enabled {
		changes = append(changes, pyjson.KV{Key: "enabled", Value: pyjson.Object{{Key: "from", Value: e.Enabled}, {Key: "to", Value: v}}})
		e.Enabled = v
	}
	for _, f := range []struct {
		name string
		into *string
	}{{"cpu_request", &e.CPURequest}, {"cpu_limit", &e.CPULimit}, {"memory_request", &e.MemoryRequest}, {"memory_limit", &e.MemoryLimit}} {
		if v, ok := in.Get(f.name).(string); ok && pyd.Strip(v) != *f.into {
			changes = append(changes, pyjson.KV{Key: f.name, Value: pyjson.Object{{Key: "from", Value: *f.into}, {Key: "to", Value: pyd.Strip(v)}}})
			*f.into = pyd.Strip(v)
		}
	}
	if v, ok := in.Get("max_timeout_seconds").(int64); ok && v != e.MaxTimeoutSeconds {
		changes = append(changes, pyjson.KV{Key: "max_timeout_seconds", Value: pyjson.Object{{Key: "from", Value: e.MaxTimeoutSeconds}, {Key: "to", Value: v}}})
		e.MaxTimeoutSeconds = v
	}
	if no := scResources(e); no != nil {
		no.send(w)
		return
	}
	var groupIDs []int64
	setGroups := false
	if list, ok := in.Get("group_ids").([]any); ok {
		setGroups = true
		uniq := map[int64]bool{}
		for _, g := range list {
			n, _ := g.(int64)
			groupIDs = append(groupIDs, n)
			uniq[n] = true
		}
		before, _, err := scEnvGroups(ctx, d.DB, e.ID)
		if err != nil {
			scFailed(d, w, "scripting: the groups", err)
			return
		}
		after := perm.Sorted(uniq)
		sortedBefore := append([]int64(nil), before...)
		sort.Slice(sortedBefore, func(i, j int) bool { return sortedBefore[i] < sortedBefore[j] })
		if !pyjson.Equal(scInts(after), scInts(sortedBefore)) {
			changes = append(changes, pyjson.KV{Key: "groups", Value: pyjson.Object{{Key: "from", Value: before}, {Key: "to", Value: after}}})
		}
		// _groups checks them before the packages are read
		if len(uniq) > 0 {
			var found int
			if err := d.DB.QueryRow(ctx, `SELECT count(*) FROM groups WHERE id = ANY($1)`, after).Scan(&found); err != nil {
				scFailed(d, w, "scripting: the groups", err)
				return
			}
			if found != len(uniq) {
				scRefuse(w, http.StatusUnprocessableEntity, "Unknown group")
				return
			}
		}
	}
	versions, err := scVersions(ctx, d.DB, e.ID, e.CurrentVersionID)
	if err != nil {
		scFailed(d, w, "scripting: the versions", err)
		return
	}
	var latest *scVersion
	if len(versions) > 0 {
		latest = &versions[0]
	}
	var lv *string
	if v, ok := in.Get("language_version").(string); ok {
		lv = &v
	} else if latest != nil {
		lv = &latest.LanguageVersion
	}
	pk := ""
	if v, ok := in.Get("packages").(string); ok {
		var no *scRefusal
		if pk, no = scPackages(e.Language, v); no != nil {
			no.send(w)
			return
		}
	} else if latest != nil {
		pk = latest.Packages
	}
	var stop *scRefusal
	var number int64
	build := false
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		if setGroups {
			var err error
			if stop, err = scSetGroups(ctx, tx, e.ID, groupIDs); err != nil || stop != nil {
				return scOr(err)
			}
		}
		if lv == nil {
			stop = &scRefusal{http.StatusUnprocessableEntity, "The environment needs a language version"}
			return scRefused
		}
		if latest == nil || *lv != latest.LanguageVersion || pk != latest.Packages {
			var err error
			if number, stop, err = scNewVersion(ctx, tx, e, *lv, pk, p.User.Email); err != nil || stop != nil {
				return scOr(err)
			}
			build = true
			changes = append(changes, pyjson.KV{Key: "build", Value: pyjson.Object{{Key: "language_version", Value: *lv},
				{Key: "packages", Value: scLines(pk)}}})
		}
		if len(changes) == 0 {
			return scRefused // nothing to save: the session is not committed
		}
		_, err := tx.Exec(ctx, `UPDATE script_environments SET name = $1, description = $2, enabled = $3, cpu_request = $4,
			cpu_limit = $5, memory_request = $6, memory_limit = $7, max_timeout_seconds = $8, updated_at = $9, updated_by = $10
			WHERE id = $11`, e.Name, e.Description, e.Enabled, e.CPURequest, e.CPULimit, e.MemoryRequest, e.MemoryLimit,
			e.MaxTimeoutSeconds, time.Now().UTC(), p.User.Email, e.ID)
		return err
	})
	if stop != nil {
		stop.send(w)
		return
	}
	if err == scRefused {
		scSendEnv(d, w, ctx, e.ID, http.StatusOK)
		return
	}
	if err != nil {
		scFailed(d, w, "scripting: changing the environment", err)
		return
	}
	detail := pyjson.Object{{Key: "changes", Value: changes}}
	if build {
		detail = append(detail, pyjson.KV{Key: "version", Value: number})
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.environment_update", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: detail})
	scSendEnv(d, w, ctx, e.ID, http.StatusOK)
}

func scInts(ids []int64) []any {
	out := make([]any, len(ids))
	for i, v := range ids {
		out[i] = v
	}
	return out
}

// rebuildEnvironment is POST …/environments/{environment_id}/rebuild (rebuild): a new
// version from the latest's language version and packages.
func rebuildEnvironment(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "environment_id")
	if !ok || !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	e, ok := scEnvironmentOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	versions, err := scVersions(ctx, d.DB, e.ID, e.CurrentVersionID)
	if err != nil {
		scFailed(d, w, "scripting: the versions", err)
		return
	}
	if len(versions) == 0 {
		scRefuse(w, http.StatusUnprocessableEntity, "The environment has no version to build again")
		return
	}
	latest := versions[0]
	if latest.Status == "queued" || latest.Status == "building" || latest.Status == "checking" {
		scRefuse(w, http.StatusConflict, fmt.Sprintf("Version %d is still being built", latest.Number))
		return
	}
	var stop *scRefusal
	var number int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if number, stop, err = scNewVersion(ctx, tx, e, latest.LanguageVersion, latest.Packages, p.User.Email); err != nil || stop != nil {
			return scOr(err)
		}
		_, err = tx.Exec(ctx, `UPDATE script_environments SET updated_at = $1, updated_by = $2 WHERE id = $3`, time.Now().UTC(), p.User.Email, e.ID)
		return err
	})
	if stop != nil {
		stop.send(w)
		return
	}
	if err != nil {
		scFailed(d, w, "scripting: the rebuild", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.environment_update", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: pyjson.Object{{Key: "changes", Value: pyjson.Object{{Key: "build", Value: "rebuild"}}},
			{Key: "version", Value: number}}})
	scSendEnv(d, w, ctx, e.ID, http.StatusOK)
}

// makeCurrent is POST …/versions/{version_id}/current (make_current).
func makeCurrent(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "environment_id", "version_id")
	if !ok || !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	e, ok := scEnvironmentOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	var envID, number int64
	var status string
	err := d.DB.QueryRow(ctx, `SELECT environment_id, number, status FROM script_environment_versions WHERE id = $1`, ids[1]).Scan(&envID, &number, &status)
	if err != nil && !scNoRows(err) {
		scFailed(d, w, "scripting: the version", err)
		return
	}
	if scNoRows(err) || envID != e.ID {
		scRefuse(w, http.StatusNotFound, "Version not found")
		return
	}
	if status != "ready" {
		scRefuse(w, http.StatusConflict, "Only a version built and checked can run scripts")
		return
	}
	if e.CurrentVersionID != nil && *e.CurrentVersionID == ids[1] {
		scSendEnv(d, w, ctx, e.ID, http.StatusOK)
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE script_environments SET current_version_id = $1, updated_at = $2, updated_by = $3 WHERE id = $4`,
		ids[1], time.Now().UTC(), p.User.Email, e.ID); err != nil {
		scFailed(d, w, "scripting: the current version", err)
		return
	}
	var from any
	if e.CurrentVersionID != nil {
		var n int64
		if err := d.DB.QueryRow(ctx, `SELECT number FROM script_environment_versions WHERE id = $1`, *e.CurrentVersionID).Scan(&n); err == nil {
			from = n
		}
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.version_current", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: pyjson.Object{{Key: "from", Value: from}, {Key: "to", Value: number}}})
	scSendEnv(d, w, ctx, e.ID, http.StatusOK)
}

// deleteEnvironment is DELETE /admin/scripting/environments/{environment_id}: refused
// while flows, scripts or notebooks use it; its builds stopped first.
func deleteEnvironment(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "environment_id")
	if !ok {
		return
	}
	ctx := r.Context()
	e, ok := scEnvironmentOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	flows, err := flowsUsingEnvironment(ctx, d.DB, e.ID)
	if err != nil {
		scFailed(d, w, "scripting: the flows", err)
		return
	}
	if len(flows) > 0 {
		scRefuseWith(w, http.StatusConflict, pyjson.Object{{Key: "code", Value: "in_use"}, {Key: "flows", Value: flows}})
		return
	}
	scripts, err := savedscripts.UsingEnvironment(ctx, d.DB, "saved_scripts", e.ID)
	if err != nil {
		scFailed(d, w, "scripting: the scripts", err)
		return
	}
	if len(scripts) > 0 {
		scRefuseWith(w, http.StatusConflict, pyjson.Object{{Key: "code", Value: "in_use"}, {Key: "flows", Value: []any{}}, {Key: "scripts", Value: scripts}})
		return
	}
	notebooks, err := savedscripts.UsingEnvironment(ctx, d.DB, "notebooks", e.ID)
	if err != nil {
		scFailed(d, w, "scripting: the notebooks", err)
		return
	}
	if len(notebooks) > 0 {
		scRefuseWith(w, http.StatusConflict, pyjson.Object{{Key: "code", Value: "in_use"}, {Key: "flows", Value: []any{}},
			{Key: "scripts", Value: []any{}}, {Key: "notebooks", Value: notebooks}})
		return
	}
	cfg := scriptcfg.Get()
	if cfg.DisabledReason() == "" {
		// script_builds.cancel_environment: the builds and checks of the environment
		build := cfg.Scripts.BuildNamespace
		if build == "" {
			build = cfg.Scripts.Namespace
		}
		namespaces := []string{build}
		if cfg.Scripts.Namespace != build {
			namespaces = append(namespaces, cfg.Scripts.Namespace)
		}
		for _, ns := range namespaces {
			if err := kube.DeleteJobs(ctx, cfg.Kubernetes, ns, fmt.Sprintf("tabularia.io/environment=%d", e.ID)); err != nil {
				if st := kube.Status(err); st == -1 {
					scFailed(d, w, "scripting: the cluster's answer", err)
					return
				} else if st != 404 {
					scRefuse(w, http.StatusBadGateway, "The builds in progress could not be stopped: "+err.Error())
					return
				}
				break
			}
		}
	}
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		for _, t := range []string{"script_environment_variables", "script_environment_groups", "script_environment_versions"} {
			if _, err := tx.Exec(ctx, fmt.Sprintf(`DELETE FROM %s WHERE environment_id = $1`, t), e.ID); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `DELETE FROM script_environments WHERE id = $1`, e.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "scripting: deleting the environment", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.environment_delete", TargetType: scPtrStr("script_environment"),
		TargetID: scPtrInt(ids[0]), TargetLabel: &e.Name})
	scNoContent(w)
}

// scVarName is _variable_name.
func scVarName(name string) *scRefusal {
	if !scVariableName.MatchString(name) || len([]rune(name)) > 128 {
		return &scRefusal{http.StatusUnprocessableEntity, "A variable name is in capitals, digits and _, and does not start with a digit"}
	}
	if strings.HasPrefix(name, "TABULARIA_") || strings.HasPrefix(name, "AWS_") {
		return &scRefusal{http.StatusUnprocessableEntity, "Names starting with TABULARIA_ or AWS_ are set by Tabularia"}
	}
	return nil
}

// writeVariable is PUT …/environments/{environment_id}/variables/{name} (write_variable).
func writeVariable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	eid := params.Path("environment_id", r.PathValue("environment_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.ScriptingVariableSet)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !scriptingIsOn(w) {
		return
	}
	ctx := r.Context()
	id, ok := eid.(int64)
	if !ok {
		id = -1
	}
	e, ok := scEnvironmentOr404(d, w, ctx, id)
	if !ok {
		return
	}
	name := r.PathValue("name")
	if no := scVarName(name); no != nil {
		no.send(w)
		return
	}
	var varID int64
	var secret, fromTab bool
	err := d.DB.QueryRow(ctx, `SELECT id, secret, from_tabularia FROM script_environment_variables WHERE environment_id = $1 AND name = $2`,
		e.ID, name).Scan(&varID, &secret, &fromTab)
	if err != nil && !scNoRows(err) {
		scFailed(d, w, "scripting: the variable", err)
		return
	}
	isNew := scNoRows(err)
	value, hasValue := in.Get("value").(string)
	wantSecret, wantFrom := in.Get("secret").(bool), in.Get("from_tabularia").(bool)
	if wantFrom && (!hasValue || !scVariableName.MatchString(pyd.Strip(value))) {
		scRefuse(w, http.StatusUnprocessableEntity, "Give the name of the Tabularia variable to hand over")
		return
	}
	if !hasValue && (isNew || !secret || !wantSecret || fromTab != wantFrom) {
		scRefuse(w, http.StatusUnprocessableEntity, "The variable needs a value")
		return
	}
	newSecret := wantSecret && !wantFrom
	now := time.Now().UTC()
	var plain any = nil
	var enc any = nil
	keep := !hasValue
	if hasValue {
		if newSecret {
			token, err := d.Fernet.Encrypt(value)
			if err != nil {
				scFailed(d, w, "scripting: encrypting the value", err)
				return
			}
			plain, enc = "", token
		} else if wantFrom {
			plain = pyd.Strip(value)
		} else {
			plain = value
		}
	}
	stored := ""
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		if isNew {
			if _, err := tx.Exec(ctx, `INSERT INTO script_environment_variables (environment_id, name, secret, value, value_enc,
				from_tabularia, updated_at, updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
				e.ID, name, newSecret, plain, enc, wantFrom, now, p.User.Email); err != nil {
				return err
			}
		} else if keep {
			if _, err := tx.Exec(ctx, `UPDATE script_environment_variables SET secret = $1, from_tabularia = $2, updated_at = $3,
				updated_by = $4 WHERE id = $5`, newSecret, wantFrom, now, p.User.Email, varID); err != nil {
				return err
			}
		} else if _, err := tx.Exec(ctx, `UPDATE script_environment_variables SET secret = $1, from_tabularia = $2, value = $3,
				value_enc = $4, updated_at = $5, updated_by = $6 WHERE id = $7`, newSecret, wantFrom, plain, enc, now, p.User.Email, varID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE script_environments SET updated_at = $1, updated_by = $2 WHERE id = $3`, now, p.User.Email, e.ID); err != nil {
			return err
		}
		return tx.QueryRow(ctx, `SELECT value FROM script_environment_variables WHERE environment_id = $1 AND name = $2`, e.ID, name).Scan(&stored)
	})
	if err != nil {
		scFailed(d, w, "scripting: saving the variable", err)
		return
	}
	detail := pyjson.Object{{Key: "name", Value: name}, {Key: "secret", Value: newSecret}, {Key: "from_tabularia", Value: wantFrom},
		{Key: "new", Value: isNew}}
	if wantFrom {
		detail = append(detail, pyjson.KV{Key: "tabularia_variable", Value: stored})
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.variable_set", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: detail})
	scSendEnv(d, w, ctx, e.ID, http.StatusOK)
}

// deleteVariable is DELETE …/environments/{environment_id}/variables/{name} (delete_variable).
func deleteVariable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "environment_id")
	if !ok {
		return
	}
	ctx := r.Context()
	e, ok := scEnvironmentOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	name := r.PathValue("name")
	tag, err := d.DB.Exec(ctx, `DELETE FROM script_environment_variables WHERE id = (SELECT id FROM script_environment_variables
		WHERE environment_id = $1 AND name = $2 LIMIT 1)`, e.ID, name)
	if err != nil {
		scFailed(d, w, "scripting: deleting the variable", err)
		return
	}
	if tag.RowsAffected() == 0 {
		scRefuse(w, http.StatusNotFound, "Variable not found")
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE script_environments SET updated_at = $1, updated_by = $2 WHERE id = $3`, time.Now().UTC(), p.User.Email, e.ID); err != nil {
		scFailed(d, w, "scripting: the environment", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "scripting.variable_delete", TargetType: scPtrStr("script_environment"),
		TargetID: &e.ID, TargetLabel: &e.Name, Detail: pyjson.Object{{Key: "name", Value: name}}})
	scSendEnv(d, w, ctx, e.ID, http.StatusOK)
}

// ── for the flow editor ───────────────────────────────────────────────────────

type editorVariable struct {
	Name          string  `json:"name"`
	Secret        bool    `json:"secret"`
	Value         *string `json:"value"`
	FromTabularia bool    `json:"from_tabularia"`
}

type editorEnvironment struct {
	ID                int64            `json:"id"`
	Name              string           `json:"name"`
	Description       string           `json:"description"`
	Language          string           `json:"language"`
	LanguageVersion   *string          `json:"language_version"`
	Version           *int64           `json:"version"`
	Ready             bool             `json:"ready"`
	MaxTimeoutSeconds int64            `json:"max_timeout_seconds"`
	Variables         []editorVariable `json:"variables"`
	Packages          []string         `json:"packages"`
}

// scTabulariaVariables is script_runs.TABULARIA_VARIABLES.
var scTabulariaVariables = []string{"TABULARIA_INPUT", "TABULARIA_OUTPUT", "TABULARIA_STEP_ID", "TABULARIA_RUN_ID",
	"TABULARIA_FLOW_ID", "TABULARIA_FLOW_NAME", "TABULARIA_NODE_ID", "TABULARIA_ATTEMPT"}

// scCurrentVersion is the environment's version in use: (language version, number, status,
// lock, completions), nil when none.
type scCurrent struct {
	LanguageVersion string
	Number          int64
	Status          string
	Lock            *string
	Completions     *string
}

func scCurrentOf(ctx context.Context, q perm.Querier, e *scEnvironment) (*scCurrent, error) {
	if e.CurrentVersionID == nil || *e.CurrentVersionID == 0 {
		return nil, nil
	}
	var c scCurrent
	err := q.QueryRow(ctx, `SELECT language_version, number, status, lock, completions FROM script_environment_versions WHERE id = $1`,
		*e.CurrentVersionID).Scan(&c.LanguageVersion, &c.Number, &c.Status, &c.Lock, &c.Completions)
	if scNoRows(err) {
		return nil, nil
	}
	return &c, err
}

// editorEnvironments is GET /scripting (editor_environments): the environments this
// person may write Script nodes with.
func editorEnvironments(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	storage, err := scStorageOf(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "scripting: the bucket", err)
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT `+scEnvColumns+` FROM script_environments ORDER BY lower(name)`)
	if err != nil {
		scFailed(d, w, "scripting: the environments", err)
		return
	}
	var envs []scEnvironment
	for rows.Next() {
		e, err := scScanEnv(rows)
		if err != nil {
			rows.Close()
			scFailed(d, w, "scripting: an environment", err)
			return
		}
		envs = append(envs, *e)
	}
	rows.Close()
	out := []editorEnvironment{}
	for i := range envs {
		e := &envs[i]
		ok, err := scriptnodes.CanUse(ctx, c, d.DB, p.User.ID, e.ID)
		if err != nil {
			scFailed(d, w, "scripting: who may use it", err)
			return
		}
		if !ok {
			continue
		}
		cur, err := scCurrentOf(ctx, d.DB, e)
		if err != nil {
			scFailed(d, w, "scripting: the version in use", err)
			return
		}
		vars, err := scEnvVariables(ctx, d.DB, e.ID)
		if err != nil {
			scFailed(d, w, "scripting: the variables", err)
			return
		}
		ev := editorEnvironment{ID: e.ID, Name: e.Name, Description: e.Description, Language: e.Language,
			MaxTimeoutSeconds: e.MaxTimeoutSeconds, Variables: []editorVariable{}, Packages: []string{}}
		for _, v := range vars {
			ev.Variables = append(ev.Variables, editorVariable{v.Name, v.Secret, v.Value, v.FromTabularia})
		}
		if cur != nil {
			ev.LanguageVersion, ev.Version = &cur.LanguageVersion, &cur.Number
			ev.Ready = e.Enabled && cur.Status == "ready"
			if cur.Lock != nil {
				ev.Packages = scLines(*cur.Lock)
			}
		}
		out = append(out, ev)
	}
	cfg := scriptcfg.Get()
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "disabled_reason", Value: scNullable(cfg.DisabledReason())},
		{Key: "storage_ready", Value: storage.Bucket != "" && storage.AccessKey != "" && storage.SecretKeyEnc != nil && *storage.SecretKeyEnc != ""},
		{Key: "environments", Value: out}, {Key: "tabularia_variables", Value: scTabulariaVariables}, {Key: "lint", Value: cfg.Scripts.Lint}})
}

// editorCompletions is GET /scripting/environments/{environment_id}/completions: {module:
// [public names]} of the version in use.
func editorCompletions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "environment_id")
	if !ok {
		return
	}
	ctx := r.Context()
	e, ok := scEnvironmentOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	can, err := scriptnodes.CanUse(ctx, perm.For(d.DB, &p.User), d.DB, p.User.ID, e.ID)
	if err != nil {
		scFailed(d, w, "scripting: who may use it", err)
		return
	}
	if !can {
		scRefuse(w, http.StatusForbidden, "You cannot write scripts for this environment")
		return
	}
	cur, err := scCurrentOf(ctx, d.DB, e)
	if err != nil {
		scFailed(d, w, "scripting: the version in use", err)
		return
	}
	if cur == nil || cur.Completions == nil || *cur.Completions == "" {
		server.JSON(w, http.StatusOK, pyjson.Object{})
		return
	}
	v, err := pyjson.Loads(*cur.Completions)
	if err != nil {
		server.JSON(w, http.StatusOK, pyjson.Object{})
		return
	}
	if _, isObj := v.(pyjson.Object); !isObj {
		scFailed(d, w, "scripting: the completions are not an object", nil) // the answer's validation, a 500
		return
	}
	server.JSON(w, http.StatusOK, v)
}
