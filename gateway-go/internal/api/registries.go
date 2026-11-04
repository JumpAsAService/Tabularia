package api

// routes/registries.py: the container registries (Administration → Container registries)
// and what the flow editor reads of them. Ported: the list, a registry, creating, changing
// and deleting one (its pull Secrets removed from the cluster), its variables, the editor's
// list. Left to the Python gateway: the routes that speak to a registry or make the cluster
// pull an image (test, cluster-test, tags, image: services/container_registry.py and
// container_cluster.py).

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("registries", func(d *Deps) []server.Route {
		observer := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
					if scRequire(d, w, r, p, false, nil) {
						h(d, w, r, p)
					}
				})}
		}
		admin := func(method, pattern string, h scBodyHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: scWithBody(d, pattern,
				func(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
					if scRequire(d, w, r, p, true, regNoRegistryFromToken) {
						h(d, w, r, p, body)
					}
				})}
		}
		return []server.Route{
			observer(http.MethodGet, "/admin/registries", listRegistries),
			observer(http.MethodGet, "/admin/registries/{registry_id}", readRegistry),
			admin(http.MethodPost, "/admin/registries", createRegistry),
			admin(http.MethodPut, "/admin/registries/{registry_id}", updateRegistry),
			admin(http.MethodDelete, "/admin/registries/{registry_id}", deleteRegistry),
			admin(http.MethodPut, "/admin/registries/{registry_id}/variables/{name}", writeRegistryVariable),
			admin(http.MethodDelete, "/admin/registries/{registry_id}/variables/{name}", deleteRegistryVariable),
			{Method: http.MethodGet, Pattern: "/registries", Handler: server.Authed(d.Auth, "/registries",
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { editorRegistries(d, w, r, p) })},
		}
	})
}

// regNoRegistryFromToken is api_tokens.NO_REGISTRY_FROM_TOKEN.
var regNoRegistryFromToken = map[string]string{
	"en": "An API token cannot change the container registries: do it from the interface",
	"it": "Un token API non cambia i container registry: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier les registres de conteneurs : faites-le depuis l'interface",
	"de": "Ein API-Token kann die Container-Registries nicht ändern: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar los registros de contenedores: hazlo desde la interfaz",
}

const regMaxRepositories = 200

var (
	// container_registry.HOST without its lookahead (checked apart: regHostLength)
	regHost       = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:[0-9]{1,5})?$`)
	regRepository = regexp.MustCompile(`^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$`)
)

// regDisabled is disabled_reason: "kubernetes" when the section is off here.
func regDisabled() string {
	if scriptcfg.Get().Containers.Enabled {
		return ""
	}
	return "kubernetes"
}

func regIsOn(w http.ResponseWriter) bool {
	if regDisabled() != "" {
		scRefuse(w, http.StatusConflict, "Container registries need Kubernetes (CONTAINERS__ENABLED)")
		return false
	}
	return true
}

type regRow struct {
	ID                                int64
	Name, Description, Host, Protocol string
	TLS, CACert, Username             string
	PasswordEnc                       *string
	Repositories                      string
	Enabled                           bool
	LastTestAt                        *time.Time
	LastTestOK                        *bool
	LastTestMessage                   string
	CreatedAt                         time.Time
	CreatedBy                         *string
	UpdatedAt                         time.Time
	UpdatedBy                         *string
}

const regColumns = `id, name, description, host, protocol, tls, ca_cert, username, password_enc, repositories, enabled,
	last_test_at, last_test_ok, last_test_message, created_at, created_by, updated_at, updated_by`

func regScan(row pgx.Row) (*regRow, error) {
	var r regRow
	err := row.Scan(&r.ID, &r.Name, &r.Description, &r.Host, &r.Protocol, &r.TLS, &r.CACert, &r.Username, &r.PasswordEnc,
		&r.Repositories, &r.Enabled, &r.LastTestAt, &r.LastTestOK, &r.LastTestMessage, &r.CreatedAt, &r.CreatedBy, &r.UpdatedAt, &r.UpdatedBy)
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// regPatterns is registry_access.patterns / repositories_of: the non-blank lines.
func regPatterns(text string) []string {
	out := []string{}
	for _, x := range scriptnodes.SplitLines(text) {
		if pyd.Strip(x) != "" {
			out = append(out, x)
		}
	}
	return out
}

// regStripped is registry_access.patterns: the lines stripped.
func regStripped(text string) []string {
	out := []string{}
	for _, x := range regPatterns(text) {
		out = append(out, pyd.Strip(x))
	}
	return out
}

type regAccess struct {
	GroupID      *int64   `json:"group_id"`
	UserID       *int64   `json:"user_id"`
	Repositories []string `json:"repositories"`
	Name         string   `json:"name"`
}

// regAccessOf is _access: who may use the registry, by the rows' order.
func regAccessOf(ctx context.Context, q perm.Querier, id int64) ([]regAccess, error) {
	rows, err := q.Query(ctx, `SELECT a.group_id, a.user_id, a.repositories, g.name, u.email FROM container_registry_access a
		LEFT JOIN groups g ON g.id = a.group_id LEFT JOIN users u ON u.id = a.user_id WHERE a.registry_id = $1 ORDER BY a.id`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []regAccess{}
	for rows.Next() {
		var a regAccess
		var repos string
		var group, email *string
		if err := rows.Scan(&a.GroupID, &a.UserID, &repos, &group, &email); err != nil {
			return nil, err
		}
		a.Repositories = regStripped(repos)
		switch {
		case a.GroupID != nil && *a.GroupID != 0:
			a.Name = fmt.Sprintf("#%d", *a.GroupID)
			if group != nil {
				a.Name = *group
			}
		case email != nil:
			a.Name = *email
		case a.UserID != nil:
			a.Name = fmt.Sprintf("#%d", *a.UserID)
		default:
			a.Name = "#None"
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

type regVariable struct {
	Name      string  `json:"name"`
	Secret    bool    `json:"secret"`
	Value     *string `json:"value"`
	UpdatedAt *string `json:"updated_at"`
	UpdatedBy *string `json:"updated_by"`
}

func regVariables(ctx context.Context, q perm.Querier, id int64) ([]regVariable, error) {
	rows, err := q.Query(ctx, `SELECT name, secret, value, updated_at, updated_by FROM container_registry_variables
		WHERE registry_id = $1 ORDER BY name`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []regVariable{}
	for rows.Next() {
		var v regVariable
		var value string
		var at time.Time
		if err := rows.Scan(&v.Name, &v.Secret, &value, &at, &v.UpdatedBy); err != nil {
			return nil, err
		}
		if !v.Secret {
			v.Value = &value
		}
		v.UpdatedAt = isoPtr(&at)
		out = append(out, v)
	}
	return out, rows.Err()
}

type registryOut struct {
	ID              int64         `json:"id"`
	Name            string        `json:"name"`
	Description     string        `json:"description"`
	Host            string        `json:"host"`
	Protocol        string        `json:"protocol"`
	TLS             string        `json:"tls"`
	CACert          string        `json:"ca_cert"`
	Username        string        `json:"username"`
	PasswordSet     bool          `json:"password_set"`
	Repositories    []string      `json:"repositories"`
	Enabled         bool          `json:"enabled"`
	Access          []regAccess   `json:"access"`
	Variables       []regVariable `json:"variables"`
	LastTestAt      *string       `json:"last_test_at"`
	LastTestOK      *bool         `json:"last_test_ok"`
	LastTestMessage string        `json:"last_test_message"`
	CreatedAt       *string       `json:"created_at"`
	CreatedBy       *string       `json:"created_by"`
	UpdatedAt       *string       `json:"updated_at"`
	UpdatedBy       *string       `json:"updated_by"`
}

func regOut(ctx context.Context, q perm.Querier, r *regRow) (*registryOut, error) {
	access, err := regAccessOf(ctx, q, r.ID)
	if err != nil {
		return nil, err
	}
	vars, err := regVariables(ctx, q, r.ID)
	if err != nil {
		return nil, err
	}
	return &registryOut{ID: r.ID, Name: r.Name, Description: r.Description, Host: r.Host, Protocol: r.Protocol, TLS: r.TLS,
		CACert: r.CACert, Username: r.Username, PasswordSet: r.PasswordEnc != nil && *r.PasswordEnc != "",
		Repositories: regPatterns(r.Repositories), Enabled: r.Enabled, Access: access, Variables: vars,
		LastTestAt: isoPtr(r.LastTestAt), LastTestOK: r.LastTestOK, LastTestMessage: r.LastTestMessage,
		CreatedAt: isoPtr(&r.CreatedAt), CreatedBy: r.CreatedBy, UpdatedAt: isoPtr(&r.UpdatedAt), UpdatedBy: r.UpdatedBy}, nil
}

func regGet(ctx context.Context, q perm.Querier, id int64) (*regRow, error) {
	r, err := regScan(q.QueryRow(ctx, `SELECT `+regColumns+` FROM container_registries WHERE id = $1`, id))
	if scNoRows(err) {
		return nil, nil
	}
	return r, err
}

func regOr404(d *Deps, w http.ResponseWriter, ctx context.Context, id int64) (*regRow, bool) {
	r, err := regGet(ctx, d.DB, id)
	if err != nil {
		scFailed(d, w, "registries: the registry", err)
		return nil, false
	}
	if r == nil {
		scRefuse(w, http.StatusNotFound, "Container registry not found")
		return nil, false
	}
	return r, true
}

func regSend(d *Deps, w http.ResponseWriter, ctx context.Context, id int64, status int) {
	r, err := regGet(ctx, d.DB, id)
	if err == nil && r == nil {
		err = fmt.Errorf("registry %d is gone", id)
	}
	if err != nil {
		scFailed(d, w, "registries: reading the registry", err)
		return
	}
	out, err := regOut(ctx, d.DB, r)
	if err != nil {
		scFailed(d, w, "registries: the registry", err)
		return
	}
	server.JSON(w, status, out)
}

// listRegistries is GET /admin/registries (listing).
func listRegistries(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	rows, err := d.DB.Query(ctx, `SELECT `+regColumns+` FROM container_registries ORDER BY lower(name)`)
	if err != nil {
		scFailed(d, w, "registries: the list", err)
		return
	}
	var list []regRow
	for rows.Next() {
		x, err := regScan(rows)
		if err != nil {
			rows.Close()
			scFailed(d, w, "registries: a registry", err)
			return
		}
		list = append(list, *x)
	}
	rows.Close()
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		scFailed(d, w, "registries: the roles", err)
		return
	}
	out := []*registryOut{}
	for i := range list {
		o, err := regOut(ctx, d.DB, &list[i])
		if err != nil {
			scFailed(d, w, "registries: a registry", err)
			return
		}
		out = append(out, o)
	}
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "disabled_reason", Value: scNullable(regDisabled())},
		{Key: "editable", Value: admin && p.TokenName == ""}, {Key: "registries", Value: out}})
}

// readRegistry is GET /admin/registries/{registry_id} (read).
func readRegistry(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "registry_id")
	if !ok {
		return
	}
	if _, ok := regOr404(d, w, r.Context(), ids[0]); ok {
		regSend(d, w, r.Context(), ids[0], http.StatusOK)
	}
}

// regName is _name.
func regName(ctx context.Context, q perm.Querier, name string, id int64) (string, *auth.Refusal, error) {
	name = pyd.Strip(name)
	if name == "" {
		return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The registry needs a name"}, nil
	}
	var clash int64
	err := q.QueryRow(ctx, `SELECT id FROM container_registries WHERE lower(name) = $1`, pyd.Lower(name)).Scan(&clash)
	if err != nil && !scNoRows(err) {
		return "", nil, err
	}
	if err == nil && clash != id {
		return "", &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("A registry named «%s» already exists", name)}, nil
	}
	return name, nil, nil
}

func regUnprocessable(detail string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: detail}
}

// regHostOf is _host: lower-case, without a pasted scheme or slash, a host name with a port.
func regHostOf(host string) (string, *auth.Refusal) {
	host = strings.ToLower(pyd.Strip(host))
	for _, prefix := range []string{"https://", "http://"} {
		host = strings.TrimPrefix(host, prefix)
	}
	host = strings.TrimRight(host, "/")
	// HOST's lookahead: 1 to 253 characters, then «:» or the end
	length := len(host) >= 1 && len([]rune(host)) <= 253
	if i := strings.Index(host, ":"); i >= 1 && i <= 253 {
		length = true
	}
	if !length || !regHost.MatchString(host) {
		return "", regUnprocessable("The registry's address is a host name, with a port if needed: ghcr.io, registry.acme.internal:5000")
	}
	return host, nil
}

// regCheckCA is container_registry.check_ca: a PEM authority that can be read.
func regCheckCA(pemText string) *auth.Refusal {
	if !strings.Contains(pemText, "BEGIN CERTIFICATE") {
		return regUnprocessable("The authority's certificate is missing: paste it in PEM (-----BEGIN CERTIFICATE-----)")
	}
	rest, found := []byte(pemText), false
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		if _, err := x509.ParseCertificate(block.Bytes); err != nil {
			return regUnprocessable("The authority's certificate cannot be read: paste it in PEM")
		}
		found = true
	}
	if !found {
		return regUnprocessable("The authority's certificate cannot be read: paste it in PEM")
	}
	return nil
}

// regTrust is _trust: (protocol, tls, ca_cert) as they are saved.
func regTrust(protocol, tls, ca string) (string, string, string, *auth.Refusal) {
	if protocol != "https" && protocol != "http" {
		return "", "", "", regUnprocessable("The protocol is https or http")
	}
	if tls != "public" && tls != "ca" && tls != "none" {
		return "", "", "", regUnprocessable("The certificate is checked against the public authorities (public), your company's authority (ca), or not at all (none)")
	}
	if protocol == "http" {
		return protocol, "none", "", nil
	}
	ca = pyd.Strip(ca)
	if tls == "ca" {
		if r := regCheckCA(ca); r != nil {
			return "", "", "", r
		}
		return protocol, tls, ca, nil
	}
	return protocol, tls, "", nil
}

// regRepositoryName is container_registry.repository_name.
func regRepositoryName(host, repository string) string {
	repository = strings.Trim(pyd.Strip(repository), "/")
	if (host == "docker.io" || host == "index.docker.io") && !strings.Contains(repository, "/") {
		return "library/" + repository
	}
	return repository
}

// regRepositories is _repositories: one allowed repository per line.
func regRepositories(host string, lines []string) (string, *auth.Refusal) {
	out := []string{}
	seen := map[string]bool{}
	for _, raw := range lines {
		line := strings.Trim(pyd.Strip(raw), "/")
		if line == "" {
			continue
		}
		if line != "*" {
			star := strings.HasSuffix(line, "/*")
			base := line
			if star {
				base = line[:len(line)-2]
			} else {
				base = regRepositoryName(host, base)
			}
			if !regRepository.MatchString(base) {
				return "", regUnprocessable(fmt.Sprintf("«%s» is not a repository: team/forecast, team/* or *", line))
			}
			if star {
				line = base + "/*"
			} else {
				line = base
			}
		}
		if !seen[line] {
			seen[line] = true
			out = append(out, line)
		}
	}
	if len(out) > regMaxRepositories {
		return "", regUnprocessable(fmt.Sprintf("At most %d repositories", regMaxRepositories))
	}
	return strings.Join(out, "\n"), nil
}

func regStrings(v any) []string {
	var out []string
	list, _ := v.([]any)
	for _, x := range list {
		s, _ := x.(string)
		out = append(out, s)
	}
	return out
}

type regWant struct {
	group, user *int64
	repos       []string
}

func regWanted(v any) []regWant {
	var out []regWant
	list, _ := v.([]any)
	for _, x := range list {
		mv, ok := x.(*pyd.ModelValue)
		if !ok {
			continue
		}
		var a regWant
		if g, ok := mv.Get("group_id").(int64); ok {
			a.group = &g
		}
		if u, ok := mv.Get("user_id").(int64); ok {
			a.user = &u
		}
		a.repos = regStrings(mv.Get("repositories"))
		out = append(out, a)
	}
	return out
}

// regSetAccess is _set_access: who may use the registry is replaced, or the 422.
func regSetAccess(ctx context.Context, tx pgx.Tx, id int64, host string, wanted []regWant, who string) (*auth.Refusal, error) {
	known := func(table string, ids map[int64]bool) (map[int64]bool, error) {
		out := map[int64]bool{}
		if len(ids) == 0 {
			return out, nil
		}
		rows, err := tx.Query(ctx, fmt.Sprintf(`SELECT id FROM %s WHERE id = ANY($1)`, table), perm.Sorted(ids))
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var n int64
			if err := rows.Scan(&n); err != nil {
				return nil, err
			}
			out[n] = true
		}
		return out, rows.Err()
	}
	groupIDs, userIDs := map[int64]bool{}, map[int64]bool{}
	for _, a := range wanted {
		if a.group != nil {
			groupIDs[*a.group] = true
		}
		if a.user != nil {
			userIDs[*a.user] = true
		}
	}
	knownGroups, err := known("groups", groupIDs)
	if err != nil {
		return nil, err
	}
	knownUsers, err := known("users", userIDs)
	if err != nil {
		return nil, err
	}
	type row struct {
		group, user *int64
		repos       string
	}
	var rows []row
	seen := map[string]bool{}
	for _, a := range wanted {
		if (a.group == nil) == (a.user == nil) {
			return regUnprocessable("Each access names a group or a user"), nil
		}
		if a.group != nil && !knownGroups[*a.group] {
			return regUnprocessable("Unknown group"), nil
		}
		if a.user != nil && !knownUsers[*a.user] {
			return regUnprocessable("Unknown user"), nil
		}
		key := fmt.Sprintf("%v/%v", regDeref(a.group), regDeref(a.user))
		if seen[key] {
			return regUnprocessable("The same group or user is listed twice"), nil
		}
		seen[key] = true
		repos, refusal := regRepositories(host, a.repos)
		if refusal != nil {
			return refusal, nil
		}
		rows = append(rows, row{a.group, a.user, repos})
	}
	if _, err := tx.Exec(ctx, `DELETE FROM container_registry_access WHERE registry_id = $1`, id); err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	for _, x := range rows {
		if _, err := tx.Exec(ctx, `INSERT INTO container_registry_access (registry_id, group_id, user_id, repositories, created_at,
			created_by) VALUES ($1, $2, $3, $4, $5, $6)`, id, x.group, x.user, x.repos, now, who); err != nil {
			return nil, err
		}
	}
	return nil, nil
}

func regDeref(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

// regAccessAudit is _access_audit.
func regAccessAudit(items []regAccess) []any {
	out := []any{}
	for _, a := range items {
		key := "user"
		if a.GroupID != nil && *a.GroupID != 0 {
			key = "group"
		}
		var repos any = a.Repositories
		if len(a.Repositories) == 0 {
			repos = "all"
		}
		out = append(out, pyjson.Object{{Key: key, Value: a.Name}, {Key: "repositories", Value: repos}})
	}
	return out
}

// createRegistry is POST /admin/registries (create).
func createRegistry(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	in := params.Body(body, gatewaymodels.RegistryCreate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !regIsOn(w) {
		return
	}
	ctx := r.Context()
	host, refusal := regHostOf(in.Get("host").(string))
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	protocol, tls, ca, refusal := regTrust(in.Get("protocol").(string), in.Get("tls").(string), in.Get("ca_cert").(string))
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	name, refusal, err := regName(ctx, d.DB, in.Get("name").(string), 0)
	if scAnswer(d, w, "registries: the name", refusal, err) {
		return
	}
	var password *string
	if pw := in.Get("password").(string); pw != "" {
		enc, err := d.Fernet.Encrypt(pw)
		if err != nil {
			scFailed(d, w, "registries: encrypting the password", err)
			return
		}
		password = &enc
	}
	repos, refusal := regRepositories(host, regStrings(in.Get("repositories")))
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var id int64
	var stop *auth.Refusal
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO container_registries (name, description, host, protocol, tls, ca_cert, username,
			password_enc, repositories, enabled, last_test_at, last_test_ok, last_test_message, created_at, created_by, updated_at,
			updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NULL, NULL, '', $11, $12, $11, $12) RETURNING id`,
			name, pyd.Strip(in.Get("description").(string)), host, protocol, tls, ca, pyd.Strip(in.Get("username").(string)), password,
			repos, in.Get("enabled").(bool), now, p.User.Email).Scan(&id); err != nil {
			return err
		}
		var err error
		if stop, err = regSetAccess(ctx, tx, id, host, regWanted(in.Get("access")), p.User.Email); err != nil || stop != nil {
			return scOr(err)
		}
		return nil
	})
	if stop != nil {
		server.Refuse(w, stop)
		return
	}
	if err != nil {
		scFailed(d, w, "registries: creating the registry", err)
		return
	}
	saved, err := regGet(ctx, d.DB, id)
	if err != nil {
		scFailed(d, w, "registries: reading the registry", err)
		return
	}
	access, err := regAccessOf(ctx, d.DB, id)
	if err != nil {
		scFailed(d, w, "registries: the access", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.create", TargetType: scPtrStr("container_registry"),
		TargetID: &id, TargetLabel: &saved.Name, Detail: pyjson.Object{{Key: "host", Value: saved.Host}, {Key: "protocol", Value: saved.Protocol},
			{Key: "tls", Value: saved.TLS}, {Key: "username", Value: saved.Username}, {Key: "password", Value: password != nil},
			{Key: "repositories", Value: regPatterns(saved.Repositories)}, {Key: "access", Value: regAccessAudit(access)}}})
	regSend(d, w, ctx, id, http.StatusCreated)
}

// updateRegistry is PUT /admin/registries/{registry_id} (update).
func updateRegistry(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	rid := params.Path("registry_id", r.PathValue("registry_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.RegistryUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !regIsOn(w) {
		return
	}
	ctx := r.Context()
	id, ok := rid.(int64)
	if !ok {
		id = -1
	}
	reg, ok := regOr404(d, w, ctx, id)
	if !ok {
		return
	}
	changes := pyjson.Object{}
	change := func(field string, into *string, value string) {
		if *into != value {
			changes = append(changes, pyjson.KV{Key: field, Value: pyjson.Object{{Key: "from", Value: *into}, {Key: "to", Value: value}}})
			*into = value
		}
	}
	if v, ok := in.Get("name").(string); ok {
		name, refusal, err := regName(ctx, d.DB, v, reg.ID)
		if scAnswer(d, w, "registries: the name", refusal, err) {
			return
		}
		change("name", &reg.Name, name)
	}
	if v, ok := in.Get("description").(string); ok {
		change("description", &reg.Description, pyd.Strip(v))
	}
	if v, ok := in.Get("host").(string); ok {
		host, refusal := regHostOf(v)
		if refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		change("host", &reg.Host, host)
	}
	pv, hasP := in.Get("protocol").(string)
	tv, hasT := in.Get("tls").(string)
	cv, hasC := in.Get("ca_cert").(string)
	if hasP || hasT || hasC {
		if pv == "" {
			pv = reg.Protocol
		}
		if tv == "" {
			tv = reg.TLS
		}
		if !hasC {
			cv = reg.CACert
		}
		protocol, tls, ca, refusal := regTrust(pv, tv, cv)
		if refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		change("protocol", &reg.Protocol, protocol)
		change("tls", &reg.TLS, tls)
		if ca != reg.CACert {
			reg.CACert = ca
			if ca != "" {
				changes = append(changes, pyjson.KV{Key: "ca_cert", Value: "changed"})
			} else {
				changes = append(changes, pyjson.KV{Key: "ca_cert", Value: "removed"})
			}
		}
	}
	if v, ok := in.Get("username").(string); ok {
		change("username", &reg.Username, pyd.Strip(v))
	}
	if v, ok := in.Get("password").(string); ok {
		if v != "" {
			enc, err := d.Fernet.Encrypt(v)
			if err != nil {
				scFailed(d, w, "registries: encrypting the password", err)
				return
			}
			reg.PasswordEnc = &enc
			changes = append(changes, pyjson.KV{Key: "password", Value: "changed"})
		} else {
			reg.PasswordEnc = nil
			changes = append(changes, pyjson.KV{Key: "password", Value: "removed"})
		}
	}
	if v := in.Get("repositories"); v != nil {
		allowed, refusal := regRepositories(reg.Host, regStrings(v))
		if refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		if allowed != reg.Repositories {
			to := []string{}
			for _, x := range strings.Split(allowed, "\n") {
				if x != "" {
					to = append(to, x)
				}
			}
			changes = append(changes, pyjson.KV{Key: "repositories", Value: pyjson.Object{{Key: "from", Value: regPatterns(reg.Repositories)},
				{Key: "to", Value: to}}})
			reg.Repositories = allowed
		}
	}
	if v, ok := in.Get("enabled").(bool); ok && v != reg.Enabled {
		changes = append(changes, pyjson.KV{Key: "enabled", Value: pyjson.Object{{Key: "from", Value: reg.Enabled}, {Key: "to", Value: v}}})
		reg.Enabled = v
	}
	var stop *auth.Refusal
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		if v := in.Get("access"); v != nil {
			before, err := regAccessOf(ctx, tx, reg.ID)
			if err != nil {
				return err
			}
			if stop, err = regSetAccess(ctx, tx, reg.ID, reg.Host, regWanted(v), p.User.Email); err != nil || stop != nil {
				return scOr(err)
			}
			after, err := regAccessOf(ctx, tx, reg.ID)
			if err != nil {
				return err
			}
			if b, a := regAccessAudit(before), regAccessAudit(after); !pyjson.Equal(b, a) {
				changes = append(changes, pyjson.KV{Key: "access", Value: pyjson.Object{{Key: "from", Value: b}, {Key: "to", Value: a}}})
			}
		}
		if len(changes) == 0 {
			return scRefused // nothing changed: nothing is committed
		}
		for _, k := range []string{"host", "protocol", "tls", "ca_cert", "username", "password"} {
			if _, has := changes.Get(k); has {
				reg.LastTestAt, reg.LastTestOK, reg.LastTestMessage = nil, nil, ""
				break
			}
		}
		_, err := tx.Exec(ctx, `UPDATE container_registries SET name = $1, description = $2, host = $3, protocol = $4, tls = $5,
			ca_cert = $6, username = $7, password_enc = $8, repositories = $9, enabled = $10, last_test_at = $11, last_test_ok = $12,
			last_test_message = $13, updated_at = $14, updated_by = $15 WHERE id = $16`, reg.Name, reg.Description, reg.Host, reg.Protocol,
			reg.TLS, reg.CACert, reg.Username, reg.PasswordEnc, reg.Repositories, reg.Enabled, reg.LastTestAt, reg.LastTestOK,
			reg.LastTestMessage, time.Now().UTC(), p.User.Email, reg.ID)
		return err
	})
	if stop != nil {
		server.Refuse(w, stop)
		return
	}
	if err == scRefused {
		regSend(d, w, ctx, reg.ID, http.StatusOK)
		return
	}
	if err != nil {
		scFailed(d, w, "registries: changing the registry", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.update", TargetType: scPtrStr("container_registry"),
		TargetID: &reg.ID, TargetLabel: &reg.Name, Detail: changes})
	regSend(d, w, ctx, reg.ID, http.StatusOK)
}

// deleteRegistry is DELETE /admin/registries/{registry_id} (delete): its credentials leave
// the cluster with it.
func deleteRegistry(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "registry_id")
	if !ok {
		return
	}
	ctx := r.Context()
	reg, ok := regOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	cfg := scriptcfg.Get()
	if regDisabled() == "" {
		// container_cluster.remove_pull_secrets: in each of the containers' namespaces
		var namespaces []string
		for _, ns := range []string{cfg.Containers.Namespace, cfg.Containers.TrialNamespace} {
			if ns != "" && (len(namespaces) == 0 || namespaces[0] != ns) {
				namespaces = append(namespaces, ns)
			}
		}
		for _, ns := range namespaces {
			_, err := kube.Call(ctx, cfg.Kubernetes, http.MethodDelete, fmt.Sprintf("/api/v1/namespaces/%s/secrets/tabularia-registry-%d", ns, reg.ID), nil, false)
			if err != nil && kube.Status(err) != 404 {
				if kube.Status(err) == -1 {
					scFailed(d, w, "registries: the cluster's answer", err)
					return
				}
				d.Log.Warn("registry: pull Secrets not removed", "registry", reg.ID, "err", err)
				break
			}
		}
	}
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		for _, t := range []string{"container_registry_access", "container_registry_variables"} {
			if _, err := tx.Exec(ctx, fmt.Sprintf(`DELETE FROM %s WHERE registry_id = $1`, t), reg.ID); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `DELETE FROM container_registries WHERE id = $1`, reg.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "registries: deleting the registry", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.delete", TargetType: scPtrStr("container_registry"),
		TargetID: scPtrInt(ids[0]), TargetLabel: &reg.Name})
	scNoContent(w)
}

// writeRegistryVariable is PUT /admin/registries/{registry_id}/variables/{name}.
func writeRegistryVariable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	rid := params.Path("registry_id", r.PathValue("registry_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.RegistriesVariableSet)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !regIsOn(w) {
		return
	}
	ctx := r.Context()
	id, ok := rid.(int64)
	if !ok {
		id = -1
	}
	reg, ok := regOr404(d, w, ctx, id)
	if !ok {
		return
	}
	name := r.PathValue("name")
	if no := scVarName(name); no != nil {
		no.send(w)
		return
	}
	var varID int64
	var secret bool
	err := d.DB.QueryRow(ctx, `SELECT id, secret FROM container_registry_variables WHERE registry_id = $1 AND name = $2`,
		reg.ID, name).Scan(&varID, &secret)
	if err != nil && !scNoRows(err) {
		scFailed(d, w, "registries: the variable", err)
		return
	}
	isNew := scNoRows(err)
	value, hasValue := in.Get("value").(string)
	wantSecret := in.Get("secret").(bool)
	if !hasValue && (isNew || !secret || !wantSecret) {
		scRefuse(w, http.StatusUnprocessableEntity, "The variable needs a value")
		return
	}
	var plain, enc any
	if hasValue {
		if wantSecret {
			token, err := d.Fernet.Encrypt(value)
			if err != nil {
				scFailed(d, w, "registries: encrypting the value", err)
				return
			}
			plain, enc = "", token
		} else {
			plain, enc = value, nil
		}
	}
	now := time.Now().UTC()
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		switch {
		case isNew:
			_, err = tx.Exec(ctx, `INSERT INTO container_registry_variables (registry_id, name, secret, value, value_enc, updated_at,
				updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7)`, reg.ID, name, wantSecret, plain, enc, now, p.User.Email)
		case !hasValue:
			_, err = tx.Exec(ctx, `UPDATE container_registry_variables SET secret = $1, updated_at = $2, updated_by = $3 WHERE id = $4`,
				wantSecret, now, p.User.Email, varID)
		default:
			_, err = tx.Exec(ctx, `UPDATE container_registry_variables SET secret = $1, value = $2, value_enc = $3, updated_at = $4,
				updated_by = $5 WHERE id = $6`, wantSecret, plain, enc, now, p.User.Email, varID)
		}
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE container_registries SET updated_at = $1, updated_by = $2 WHERE id = $3`, now, p.User.Email, reg.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "registries: saving the variable", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.variable_set", TargetType: scPtrStr("container_registry"),
		TargetID: &reg.ID, TargetLabel: &reg.Name, Detail: pyjson.Object{{Key: "name", Value: name}, {Key: "secret", Value: wantSecret},
			{Key: "new", Value: isNew}}})
	regSend(d, w, ctx, reg.ID, http.StatusOK)
}

// deleteRegistryVariable is DELETE /admin/registries/{registry_id}/variables/{name}.
func deleteRegistryVariable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, _ pyd.Body) {
	ids, ok := scPathInts(w, r, "registry_id")
	if !ok {
		return
	}
	ctx := r.Context()
	reg, ok := regOr404(d, w, ctx, ids[0])
	if !ok {
		return
	}
	name := r.PathValue("name")
	tag, err := d.DB.Exec(ctx, `DELETE FROM container_registry_variables WHERE id = (SELECT id FROM container_registry_variables
		WHERE registry_id = $1 AND name = $2 LIMIT 1)`, reg.ID, name)
	if err != nil {
		scFailed(d, w, "registries: deleting the variable", err)
		return
	}
	if tag.RowsAffected() == 0 {
		scRefuse(w, http.StatusNotFound, "Variable not found")
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE container_registries SET updated_at = $1, updated_by = $2 WHERE id = $3`,
		time.Now().UTC(), p.User.Email, reg.ID); err != nil {
		scFailed(d, w, "registries: the registry", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "registry.variable_delete", TargetType: scPtrStr("container_registry"),
		TargetID: &reg.ID, TargetLabel: &reg.Name, Detail: pyjson.Object{{Key: "name", Value: name}}})
	regSend(d, w, ctx, reg.ID, http.StatusOK)
}

// ── for the flow editor ───────────────────────────────────────────────────────

// regGrants is registry_access._grants: registry → the patterns granted, nil for all.
func regGrants(ctx context.Context, q perm.Querier, userID int64) (map[int64][]string, map[int64]bool, error) {
	rows, err := q.Query(ctx, `SELECT a.registry_id, a.repositories FROM container_registry_access a
		WHERE a.user_id = $1 OR a.group_id IN (SELECT group_id FROM user_groups WHERE user_id = $1)`, userID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	out, all := map[int64][]string{}, map[int64]bool{}
	for rows.Next() {
		var id int64
		var text string
		if err := rows.Scan(&id, &text); err != nil {
			return nil, nil, err
		}
		mine := regStripped(text)
		if len(mine) == 0 || all[id] {
			all[id] = true
			out[id] = nil
			continue
		}
		out[id] = append(out[id], mine...)
	}
	return out, all, rows.Err()
}

// regIntersect is _intersect: the repositories two patterns both allow, as one pattern.
func regIntersect(a, b string) string {
	if a == "*" {
		return b
	}
	if b == "*" || a == b {
		return a
	}
	for _, pair := range [][2]string{{a, b}, {b, a}} {
		wide, narrow := pair[0], pair[1]
		if strings.HasSuffix(wide, "/*") && (strings.HasPrefix(narrow, wide[:len(wide)-1]) || narrow == wide[:len(wide)-2]) {
			if narrow != wide[:len(wide)-2] {
				return narrow
			}
			return ""
		}
	}
	return ""
}

type editorRegistry struct {
	ID           int64            `json:"id"`
	Name         string           `json:"name"`
	Description  string           `json:"description"`
	Host         string           `json:"host"`
	Repositories []string         `json:"repositories"`
	Variables    []editorVariable `json:"variables"`
}

// editorRegistries is GET /registries (editor_listing): the registries this person may
// choose in a Container node, and which repositories.
func editorRegistries(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		scFailed(d, w, "registries: the roles", err)
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT `+regColumns+` FROM container_registries WHERE enabled = true ORDER BY name`)
	if err != nil {
		scFailed(d, w, "registries: the list", err)
		return
	}
	var list []regRow
	for rows.Next() {
		x, err := regScan(rows)
		if err != nil {
			rows.Close()
			scFailed(d, w, "registries: a registry", err)
			return
		}
		list = append(list, *x)
	}
	rows.Close()
	var grants map[int64][]string
	var all map[int64]bool
	if !admin {
		if grants, all, err = regGrants(ctx, d.DB, p.User.ID); err != nil {
			scFailed(d, w, "registries: the grants", err)
			return
		}
	}
	out := []editorRegistry{}
	for i := range list {
		reg := &list[i]
		allowed := regStripped(reg.Repositories)
		repos := allowed
		if !admin {
			mine, granted := grants[reg.ID]
			if !granted {
				continue
			}
			if !all[reg.ID] {
				repos = []string{}
				for _, a := range allowed {
					for _, b := range mine {
						if both := regIntersect(a, b); both != "" && !regContains(repos, both) {
							repos = append(repos, both)
						}
					}
				}
			}
		}
		vars, err := regVariables(ctx, d.DB, reg.ID)
		if err != nil {
			scFailed(d, w, "registries: the variables", err)
			return
		}
		ev := editorRegistry{ID: reg.ID, Name: reg.Name, Description: reg.Description, Host: reg.Host, Repositories: repos,
			Variables: []editorVariable{}}
		for _, v := range vars {
			ev.Variables = append(ev.Variables, editorVariable{Name: v.Name, Secret: v.Secret, Value: v.Value})
		}
		out = append(out, ev)
	}
	storage, err := scStorageOf(ctx, d.DB)
	if err != nil {
		scFailed(d, w, "registries: the bucket", err)
		return
	}
	cfg := scriptcfg.Get()
	// EditorVariable of the registries has no from_tabularia: written without it
	type regEditorVariable struct {
		Name   string  `json:"name"`
		Secret bool    `json:"secret"`
		Value  *string `json:"value"`
	}
	type regEditorRegistry struct {
		ID           int64               `json:"id"`
		Name         string              `json:"name"`
		Description  string              `json:"description"`
		Host         string              `json:"host"`
		Repositories []string            `json:"repositories"`
		Variables    []regEditorVariable `json:"variables"`
	}
	registries := []regEditorRegistry{}
	for _, e := range out {
		x := regEditorRegistry{e.ID, e.Name, e.Description, e.Host, e.Repositories, []regEditorVariable{}}
		for _, v := range e.Variables {
			x.Variables = append(x.Variables, regEditorVariable{v.Name, v.Secret, v.Value})
		}
		registries = append(registries, x)
	}
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "disabled_reason", Value: scNullable(regDisabled())},
		{Key: "registries", Value: registries},
		{Key: "limits", Value: pyjson.Object{{Key: "max_cpu", Value: cfg.Containers.MaxCPU}, {Key: "max_memory", Value: cfg.Containers.MaxMemory},
			{Key: "max_timeout_seconds", Value: cfg.Containers.MaxTimeoutSecond}}},
		{Key: "storage_ready", Value: storage.Bucket != "" && storage.AccessKey != "" && storage.SecretKeyEnc != nil && *storage.SecretKeyEnc != ""},
		{Key: "tabularia_variables", Value: scTabulariaVariables}})
}

func regContains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
