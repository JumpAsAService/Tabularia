// Package apitokens is services/api_tokens.py: creating, listing and revoking the API tokens
// (`tab_` and 40 random characters, kept only as their SHA-256 fingerprint), and the refusals
// a request made with a token gets where a token may not act.
package apitokens

import (
	"context"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// NoTokenFromToken is NO_TOKEN_FROM_TOKEN.
var NoTokenFromToken = map[string]string{
	"en": "An API token cannot create credentials: do it from the interface",
	"it": "Un token API non può creare credenziali: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas créer d'identifiants : faites-le depuis l'interface",
	"de": "Ein API-Token kann keine Zugangsdaten erstellen: Das geht über die Oberfläche",
	"es": "Un token de API no puede crear credenciales: hazlo desde la interfaz",
}

// NoRolesFromToken is NO_ROLES_FROM_TOKEN.
var NoRolesFromToken = map[string]string{
	"en": "An API token cannot make an account administrator or observer, nor reactivate it: do it from the interface",
	"it": "Un token API non rende un account amministratore od osservatore, né lo riattiva: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas rendre un compte administrateur ou observateur, ni le réactiver : faites-le depuis l'interface",
	"de": "Ein API-Token kann ein Konto weder zum Administrator oder Beobachter machen noch reaktivieren: Das geht über die Oberfläche",
	"es": "Un token de API no puede hacer una cuenta administradora u observadora, ni reactivarla: hazlo desde la interfaz",
}

// DenyIfFromToken is routes/api_tokens.py deny_if_from_token: a token creates no credentials
// and grants no roles; those are done from a session.
func DenyIfFromToken(r *http.Request, p *auth.Principal, table map[string]string) *auth.Refusal {
	if p.TokenName == "" {
		return nil
	}
	return &auth.Refusal{Status: http.StatusForbidden, Detail: language.Pick(table, r.Header.Get("Accept-Language"))}
}

// RevokeAll is revoke_all without its commit: every token of the person; how many.
func RevokeAll(ctx context.Context, q Querier, userID int64) (int64, error) {
	tag, err := q.Exec(ctx, `DELETE FROM api_tokens WHERE user_id = $1`, userID)
	return tag.RowsAffected(), err
}

// WithdrawIfObserver is withdraw_if_observer, in the caller's transaction: the personal
// tokens of whoever is now ONLY an observer (a service account's stay).
func WithdrawIfObserver(ctx context.Context, q Querier, userIDs []int64) (int64, error) {
	var n int64
	for _, id := range userIDs {
		var superuser, observer, service bool
		err := q.QueryRow(ctx, `SELECT is_superuser, is_observer, is_service FROM users WHERE id = $1`, id).Scan(&superuser, &observer, &service)
		if err == pgx.ErrNoRows {
			continue
		}
		if err != nil {
			return n, err
		}
		if service {
			continue
		}
		rs, err := roles.Of(ctx, q, id, superuser, observer)
		if err != nil {
			return n, err
		}
		if rs.ObserverOnly() {
			k, err := RevokeAll(ctx, q, id)
			if err != nil {
				return n, err
			}
			n += k
		}
	}
	return n, nil
}
