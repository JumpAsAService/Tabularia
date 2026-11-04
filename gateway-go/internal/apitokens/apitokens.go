// Package apitokens is services/api_tokens.py: creating, listing and revoking the API tokens
// (`tab_` and 40 random characters, kept only as their SHA-256 fingerprint), and the refusals
// a request made with a token gets where a token may not act.
package apitokens

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

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
		if errors.Is(err, pgx.ErrNoRows) {
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

// Prefix is PREFIX: what tells a token from a session's JWT.
const Prefix = "tab_"

// Token is an api_tokens row.
type Token struct {
	ID         int64
	UserID     int64
	Name       string
	Prefix     string
	Scope      string
	CreatedAt  time.Time
	ExpiresAt  time.Time
	LastUsedAt *time.Time
	LastUsedIP *string
}

// Columns are the columns Scan reads, in its order.
const Columns = `id, user_id, name, prefix, scope, created_at, expires_at, last_used_at, last_used_ip`

// Scan reads a row of Columns.
func Scan(row pgx.Row) (*Token, error) {
	var t Token
	err := row.Scan(&t.ID, &t.UserID, &t.Name, &t.Prefix, &t.Scope, &t.CreatedAt, &t.ExpiresAt, &t.LastUsedAt, &t.LastUsedIP)
	return &t, err
}

func now() time.Time {
	u := time.Now().UTC()
	return u.Truncate(time.Microsecond)
}

// Create is create: the token of `userID` (asked by `createdBy`, 0 = nobody) and its secret,
// never seen again. The refusals are 422s, as the Python HTTPExceptions.
func Create(ctx context.Context, q Querier, userID int64, name, scope string, days int64, maxDays int, createdBy int64) (*Token, string, *auth.Refusal, error) {
	name = strings.TrimFunc(name, unicode.IsSpace)
	if name == "" || utf8.RuneCountInString(name) > 80 {
		return nil, "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The token name is required (at most 80 characters)"}, nil
	}
	if scope != "read" && scope != "write" {
		return nil, "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "Token scope: 'read' or 'write'"}, nil
	}
	if days < 1 || days > int64(maxDays) {
		return nil, "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("The expiry must be from 1 to %d days", maxDays)}, nil
	}
	b := make([]byte, 30)
	if _, err := rand.Read(b); err != nil {
		return nil, "", nil, err
	}
	secret := Prefix + base64.RawURLEncoding.EncodeToString(b)
	sum := sha256.Sum256([]byte(secret))
	at := now()
	var by *int64
	if createdBy != 0 {
		by = &createdBy
	}
	t, err := Scan(q.QueryRow(ctx, `INSERT INTO api_tokens (user_id, name, prefix, token_hash, scope, created_at, created_by, expires_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING `+Columns,
		userID, name, secret[:12], hex.EncodeToString(sum[:]), scope, at, by, at.Add(time.Duration(days)*24*time.Hour)))
	if err != nil {
		return nil, "", nil, err
	}
	return t, secret, nil, nil
}

// OfUser is of_user: a person's tokens still valid, newest first.
func OfUser(ctx context.Context, q interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
}, userID int64) ([]*Token, error) {
	rows, err := q.Query(ctx, `SELECT `+Columns+` FROM api_tokens WHERE user_id = $1 AND expires_at > $2 ORDER BY created_at DESC`, userID, now())
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (*Token, error) { return Scan(r) })
}

// Revoke is revoke: deletes the token (of `userID` when not 0); the row as it was, or nil.
func Revoke(ctx context.Context, q Querier, tokenID, userID int64) (*Token, error) {
	t, err := Scan(q.QueryRow(ctx, `SELECT `+Columns+` FROM api_tokens WHERE id = $1`, tokenID))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if userID != 0 && t.UserID != userID {
		return nil, nil
	}
	_, err = q.Exec(ctx, `DELETE FROM api_tokens WHERE id = $1`, tokenID)
	return t, err
}
