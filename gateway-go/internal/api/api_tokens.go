package api

import (
	"math"
	"math/big"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// APITokenOut is routes/api_tokens.py TokenOut.
type APITokenOut struct {
	ID         int64   `json:"id"`
	Name       string  `json:"name"`
	Prefix     string  `json:"prefix"`
	Scope      string  `json:"scope"`
	CreatedAt  string  `json:"created_at"`
	ExpiresAt  string  `json:"expires_at"`
	LastUsedAt *string `json:"last_used_at"`
	LastUsedIP *string `json:"last_used_ip"`
}

// APITokenCreated is TokenCreated: the only time the secret is seen.
type APITokenCreated struct {
	APITokenOut
	Token string `json:"token"`
}

// AdminTokenOut is routes/api_tokens.py AdminTokenOut.
type AdminTokenOut struct {
	APITokenOut
	UserID    int64  `json:"user_id"`
	UserEmail string `json:"user_email"`
	UserName  string `json:"user_name"`
	IsService bool   `json:"is_service"`
}

func init() {
	register("api_tokens", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/auth/tokens", Handler: server.Authed(d.Auth, "/auth/tokens", myTokens(d))},
			{Method: http.MethodPost, Pattern: "/auth/tokens", Handler: withBody(server.Authed(d.Auth, "/auth/tokens", createMyToken(d)))},
			{Method: http.MethodDelete, Pattern: "/auth/tokens/{token_id}", Handler: server.Authed(d.Auth, "/auth/tokens/{token_id}", revokeMyToken(d))},
			{Method: http.MethodGet, Pattern: "/admin/api-tokens", Handler: observer(d, "/admin/api-tokens", allTokens(d))},
			{Method: http.MethodDelete, Pattern: "/admin/api-tokens/{token_id}", Handler: superuser(d, "/admin/api-tokens/{token_id}", revokeAnyToken(d))},
		}
	})
}

// tokenOut is token_out.
func tokenOut(t *apitokens.Token) APITokenOut {
	return APITokenOut{ID: t.ID, Name: t.Name, Prefix: t.Prefix, Scope: t.Scope, CreatedAt: pyjson.Isoformat(t.CreatedAt),
		ExpiresAt: pyjson.Isoformat(t.ExpiresAt), LastUsedAt: isoPtr(t.LastUsedAt), LastUsedIP: t.LastUsedIP}
}

// createAndRecord is routes/api_tokens.py create_and_record: the token of `owner` asked by
// `asker`, written in the audit; whoever only observes gets a read-only one.
func createAndRecord(d *Deps, w http.ResponseWriter, r *http.Request, owner *auth.User, body *pyd.ModelValue, asker *auth.User) {
	ctx := r.Context()
	scope := body.Get("scope").(string)
	rs, err := roles.Of(ctx, d.DB, owner.ID, owner.IsSuperuser, owner.IsObserver)
	if err != nil {
		fail(d, w, "reading the roles", err)
		return
	}
	if rs.ObserverOnly() {
		scope = "read"
	}
	var days int64
	switch v := body.Get("expires_in_days").(type) {
	case int64:
		days = v
	case *big.Int:
		days = math.MaxInt64
		if v.Sign() < 0 {
			days = math.MinInt64
		}
	}
	t, secret, refusal, err := apitokens.Create(ctx, d.DB, owner.ID, body.Get("name").(string), scope, days, d.Panel.APITokenMaxDays(), asker.ID)
	if err != nil || refusal != nil {
		refuseOr500(d, w, refusal, err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: asker, Action: "token.create", TargetType: ptr("user"),
		TargetID: &owner.ID, TargetLabel: &owner.Email, Detail: pyjson.Object{{Key: "name", Value: t.Name},
			{Key: "scope", Value: t.Scope}, {Key: "prefix", Value: t.Prefix}, {Key: "expires_at", Value: t.ExpiresAt}}})
	server.JSON(w, http.StatusCreated, APITokenCreated{APITokenOut: tokenOut(t), Token: secret})
}

// myTokens is routes/api_tokens.py my_tokens.
func myTokens(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		list, err := apitokens.OfUser(r.Context(), d.DB, p.User.ID)
		if err != nil {
			fail(d, w, "reading the tokens", err)
			return
		}
		out := make([]APITokenOut, 0, len(list))
		for _, t := range list {
			out = append(out, tokenOut(t))
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// createMyToken is routes/api_tokens.py create_my_token: for one's own scripts, never from a
// token, not while personal tokens are off (but for a service account), not for an observer.
func createMyToken(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.TokenCreate)
		if !validated(w, &ps) {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		if !d.Panel.PersonalTokens() && !p.User.IsService {
			detail(w, http.StatusForbidden, "Personal tokens are turned off on this installation")
			return
		}
		rs, err := roles.Of(r.Context(), d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		if rs.ObserverOnly() {
			detail(w, http.StatusForbidden, "Observers do not have personal tokens")
			return
		}
		createAndRecord(d, w, r, &p.User, body, &p.User)
	}
}

// revokeMyToken is routes/api_tokens.py revoke_my_token: one of one's own (the others': 404).
func revokeMyToken(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "token_id")
		if !ok {
			return
		}
		ctx := r.Context()
		t, err := apitokens.Revoke(ctx, d.DB, ids[0], p.User.ID)
		if err != nil {
			fail(d, w, "revoking the token", err)
			return
		}
		if t == nil {
			detail(w, http.StatusNotFound, "Token not found")
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "token.revoke", TargetType: ptr("user"),
			TargetID: &p.User.ID, TargetLabel: &p.User.Email, Detail: pyjson.Object{{Key: "name", Value: t.Name}, {Key: "prefix", Value: t.Prefix}}})
		returnsNone(w)
	}
}

// allTokens is routes/api_tokens.py all_tokens: every valid token with its owner; to the
// observer without who and from where.
func allTokens(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		rs, err := roles.Of(ctx, d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT t.id, t.user_id, t.name, t.prefix, t.scope, t.created_at, t.expires_at, t.last_used_at,
			t.last_used_ip, u.email, u.full_name, u.is_service FROM api_tokens t JOIN users u ON u.id = t.user_id
			WHERE t.expires_at > $1 ORDER BY t.created_at DESC LIMIT 500`, time.Now().UTC())
		if err != nil {
			fail(d, w, "reading the tokens", err)
			return
		}
		out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (AdminTokenOut, error) {
			var t apitokens.Token
			var o AdminTokenOut
			err := row.Scan(&t.ID, &t.UserID, &t.Name, &t.Prefix, &t.Scope, &t.CreatedAt, &t.ExpiresAt, &t.LastUsedAt, &t.LastUsedIP,
				&o.UserEmail, &o.UserName, &o.IsService)
			o.APITokenOut, o.UserID = tokenOut(&t), t.UserID
			if !rs.Admin {
				o.LastUsedIP, o.Prefix = nil, "tab_…"
				if !o.IsService {
					o.UserEmail, o.UserName = masking.Email(o.UserEmail), ""
				}
			}
			return o, err
		})
		if err != nil {
			fail(d, w, "reading the tokens", err)
			return
		}
		if out == nil {
			out = []AdminTokenOut{}
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// revokeAnyToken is routes/api_tokens.py revoke_any_token: an administrator withdraws a token.
func revokeAnyToken(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "token_id")
		if !ok {
			return
		}
		ctx := r.Context()
		t, err := apitokens.Revoke(ctx, d.DB, ids[0], 0)
		if err != nil {
			fail(d, w, "revoking the token", err)
			return
		}
		if t == nil {
			detail(w, http.StatusNotFound, "Token not found")
			return
		}
		owner, err := readUser(ctx, d.DB, t.UserID)
		if err != nil {
			fail(d, w, "reading the owner", err)
			return
		}
		var label *string
		if owner != nil {
			label = &owner.Email
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "token.revoke", TargetType: ptr("user"),
			TargetID: &t.UserID, TargetLabel: label, Detail: pyjson.Object{{Key: "name", Value: t.Name}, {Key: "prefix", Value: t.Prefix}}})
		returnsNone(w)
	}
}
