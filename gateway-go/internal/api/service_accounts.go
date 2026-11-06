package api

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// serviceDomain is service_accounts.DOMAIN: a reserved domain no identity provider asserts.
const serviceDomain = "service.invalid"

// ServiceAccountOut is routes/service_accounts.py ServiceAccountOut.
type ServiceAccountOut struct {
	ID         int64    `json:"id"`
	Name       string   `json:"name"`
	Email      string   `json:"email"`
	IsActive   bool     `json:"is_active"`
	CreatedAt  *string  `json:"created_at"`
	LastSeenAt *string  `json:"last_seen_at"`
	Groups     []string `json:"groups"`
	Tokens     int64    `json:"tokens"`
}

func init() {
	register("service_accounts", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/service-accounts", Handler: observer(d, "/service-accounts", listServiceAccounts(d))},
			{Method: http.MethodPost, Pattern: "/service-accounts", Handler: withBody(superuser(d, "/service-accounts", createServiceAccount(d)))},
			{Method: http.MethodDelete, Pattern: "/service-accounts/{account_id}", Handler: superuser(d, "/service-accounts/{account_id}", deleteServiceAccount(d))},
			{Method: http.MethodGet, Pattern: "/service-accounts/{account_id}/tokens", Handler: observer(d, "/service-accounts/{account_id}/tokens", listAccountTokens(d))},
			{Method: http.MethodPost, Pattern: "/service-accounts/{account_id}/tokens", Handler: withBody(superuser(d, "/service-accounts/{account_id}/tokens", createAccountToken(d)))},
			{Method: http.MethodDelete, Pattern: "/service-accounts/{account_id}/tokens/{token_id}", Handler: superuser(d, "/service-accounts/{account_id}/tokens/{token_id}", revokeAccountToken(d))},
		}
	})
}

// oneService is service_accounts._one: the account, when it is a service account.
func oneService(d *Deps, r *http.Request, id int64) (*userRow, error) {
	u, err := readUser(r.Context(), d.DB, id)
	if err != nil || u == nil || !u.IsService {
		return nil, err
	}
	return u, nil
}

// serviceOut is service_accounts._out: its groups (sorted) and its valid tokens.
func serviceOut(d *Deps, r *http.Request, u *userRow) (ServiceAccountOut, error) {
	ctx := r.Context()
	o := ServiceAccountOut{ID: u.ID, Name: u.FullName, Email: u.Email, IsActive: u.IsActive, CreatedAt: isoPtr(&u.CreatedAt),
		LastSeenAt: isoPtr(u.LastSeenAt)}
	rows, err := d.DB.Query(ctx, `SELECT g.name FROM groups g JOIN user_groups ug ON ug.group_id = g.id WHERE ug.user_id = $1`, u.ID)
	if err != nil {
		return o, err
	}
	if o.Groups, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
		return o, err
	}
	if o.Groups == nil {
		o.Groups = []string{}
	}
	sort.Strings(o.Groups)
	err = d.DB.QueryRow(ctx, `SELECT count(*) FROM api_tokens WHERE user_id = $1 AND expires_at > $2`, u.ID, time.Now().UTC()).Scan(&o.Tokens)
	return o, err
}

// listServiceAccounts is routes/service_accounts.py list_service_accounts: by name.
func listServiceAccounts(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		rows, err := d.DB.Query(r.Context(), `SELECT `+userRowColumns+` FROM users WHERE is_service ORDER BY full_name`)
		if err != nil {
			fail(d, w, "reading the service accounts", err)
			return
		}
		users, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*userRow, error) { return scanUserRow(row) })
		if err != nil {
			fail(d, w, "reading the service accounts", err)
			return
		}
		out := make([]ServiceAccountOut, 0, len(users))
		for _, u := range users {
			o, err := serviceOut(d, r, u)
			if err != nil {
				fail(d, w, "reading the service account", err)
				return
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

var notAlnum = regexp.MustCompile(`[^a-z0-9]+`)

// createServiceAccount is routes/service_accounts.py create_service_account: a synthetic email
// from the name, under a domain nobody receives mail at.
func createServiceAccount(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.ServiceAccountCreate)
		if !validated(w, &ps) {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		ctx := r.Context()
		name := pyd.Strip(body.Get("name").(string))
		if name == "" || pyd.Len(name) > 80 {
			detail(w, http.StatusUnprocessableEntity, "A name is required (at most 80 characters)")
			return
		}
		root := strings.Trim(notAlnum.ReplaceAllString(pyd.Lower(name), "-"), "-")
		if utf8.RuneCountInString(root) > 40 {
			root = root[:40] // ASCII only by now
		}
		if root == "" {
			root = "service"
		}
		email := root + "@" + serviceDomain
		var taken bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM users WHERE email = $1)`, email).Scan(&taken); err != nil {
			fail(d, w, "reading the users", err)
			return
		}
		if taken {
			b := make([]byte, 3)
			if _, err := rand.Read(b); err != nil {
				fail(d, w, "a random suffix", err)
				return
			}
			email = root + "-" + hex.EncodeToString(b) + "@" + serviceDomain
		}
		u, err := scanUserRow(d.DB.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser,
			is_observer, is_service, created_at) VALUES ($1, $2, NULL, true, false, false, true, $3) RETURNING `+userRowColumns,
			email, name, time.Now().UTC()))
		if err != nil {
			fail(d, w, "creating the service account", err)
			return
		}
		description := []rune(pyd.Strip(body.Get("description").(string)))
		if len(description) > 200 {
			description = description[:200]
		}
		var desc any
		if len(description) > 0 {
			desc = string(description)
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "service_account.create", TargetType: ptr("user"),
			TargetID: &u.ID, TargetLabel: &u.Email, Detail: pyjson.Object{{Key: "name", Value: name}, {Key: "description", Value: desc}}})
		o, err := serviceOut(d, r, u)
		if err != nil {
			fail(d, w, "reading the service account", err)
			return
		}
		server.JSON(w, http.StatusCreated, o)
	}
}

const serviceNotFound = "Service account not found"

// deleteServiceAccount is routes/service_accounts.py delete_service_account: the account and
// its tokens; what it created stays.
func deleteServiceAccount(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "account_id")
		if !ok {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		u, err := oneService(d, r, ids[0])
		if err != nil {
			fail(d, w, "reading the service account", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, serviceNotFound)
			return
		}
		if err := removeUser(r.Context(), d, u); err != nil {
			fail(d, w, "deleting the service account", err)
			return
		}
		audit.Record(r.Context(), d.DB, d.Log, audit.Event{Actor: &p.User, Action: "service_account.delete", TargetType: ptr("user"),
			TargetID: &u.ID, TargetLabel: &u.Email})
		returnsNone(w)
	}
}

// listAccountTokens is routes/service_accounts.py list_account_tokens: the observer sees
// neither beginning nor address.
func listAccountTokens(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "account_id")
		if !ok {
			return
		}
		ctx := r.Context()
		u, err := oneService(d, r, ids[0])
		if err != nil {
			fail(d, w, "reading the service account", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, serviceNotFound)
			return
		}
		rs, err := roles.Of(ctx, d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		list, err := apitokens.OfUser(ctx, d.DB, u.ID)
		if err != nil {
			fail(d, w, "reading the tokens", err)
			return
		}
		out := make([]APITokenOut, 0, len(list))
		for _, t := range list {
			o := tokenOut(t)
			if !rs.Admin {
				o.Prefix, o.LastUsedIP = "tab_…", nil
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// createAccountToken is routes/service_accounts.py create_account_token.
func createAccountToken(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		ids, _ := pathIntsInto(&ps, r, "account_id")
		body := ps.Body(bodyOf(r), gatewaymodels.TokenCreate)
		if !validated(w, &ps) {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		u, err := oneService(d, r, ids[0])
		if err != nil {
			fail(d, w, "reading the service account", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, serviceNotFound)
			return
		}
		createAndRecord(d, w, r, &u.User, body, &p.User)
	}
}

// revokeAccountToken is routes/service_accounts.py revoke_account_token.
func revokeAccountToken(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "account_id", "token_id")
		if !ok {
			return
		}
		ctx := r.Context()
		u, err := oneService(d, r, ids[0])
		if err != nil {
			fail(d, w, "reading the service account", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, serviceNotFound)
			return
		}
		t, err := apitokens.Revoke(ctx, d.DB, ids[1], ids[0])
		if err != nil {
			fail(d, w, "revoking the token", err)
			return
		}
		if t == nil {
			detail(w, http.StatusNotFound, "Token not found")
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "token.revoke", TargetType: ptr("user"),
			TargetID: &u.ID, TargetLabel: &u.Email, Detail: pyjson.Object{{Key: "name", Value: t.Name}, {Key: "prefix", Value: t.Prefix}}})
		returnsNone(w)
	}
}
