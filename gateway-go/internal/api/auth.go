// Package api holds the routes ported from the Python gateway, each citing the one it ports.
package api

import (
	"log/slog"
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// MeOut is schemas/models.py MeOut, its fields in the same order (the JSON's keys follow).
// `created_at`, `last_seen_at`, `admin_groups` and `observer_groups` are never filled by
// /auth/me: MeOut inherits them from UserOut and routes/auth.py passes only the others.
type MeOut struct {
	ID                 int64    `json:"id"`
	Email              string   `json:"email"`
	FullName           string   `json:"full_name"`
	IsActive           bool     `json:"is_active"`
	IsSuperuser        bool     `json:"is_superuser"` // EFFECTIVE: the flag or an administrators' group
	CreatedAt          *string  `json:"created_at"`
	LastSeenAt         *string  `json:"last_seen_at"`
	SSOOnly            bool     `json:"sso_only"` // no local password: the identity provider manages it
	Groups             []string `json:"groups"`
	AdminGroups        []string `json:"admin_groups"`
	IsObserver         bool     `json:"is_observer"` // EFFECTIVE
	ObserverGroups     []string `json:"observer_groups"`
	IsService          bool     `json:"is_service"`
	SessionIdleMinutes int      `json:"session_idle_minutes"`
	PersonalTokens     bool     `json:"personal_tokens"`
	APITokenMaxDays    int      `json:"api_token_max_days"`
	Locale             *string  `json:"locale"`
}

// Settings are the panel's values /auth/me tells the interface.
type Settings interface {
	SessionIdleMinutes() int
	PersonalTokens() bool
	APITokenMaxDays() int
}

func init() {
	register("auth", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodGet, Pattern: "/auth/me", Handler: Me(d.Auth, d.DB, d.Panel, d.Log)}}
	})
}

// Me is GET /auth/me (routes/auth.py `me`): oneself, with the effective roles, the group
// names and the settings the page needs.
func Me(a *auth.Authenticator, db *pgxpool.Pool, set Settings, log *slog.Logger) http.Handler {
	return server.Authed(a, "/auth/me", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		u := p.User
		rs, err := roles.Of(r.Context(), db, u.ID, u.IsSuperuser, u.IsObserver)
		if err != nil {
			log.Error("reading the roles", "user", u.ID, "err", err)
			server.JSON(w, http.StatusInternalServerError, map[string]string{"detail": "Internal Server Error"})
			return
		}
		groups, err := groupNames(r, db, u.ID)
		if err != nil {
			log.Error("reading the groups", "user", u.ID, "err", err)
			server.JSON(w, http.StatusInternalServerError, map[string]string{"detail": "Internal Server Error"})
			return
		}
		server.JSON(w, http.StatusOK, MeOut{
			ID: u.ID, Email: u.Email, FullName: u.FullName, IsActive: u.IsActive,
			IsSuperuser: rs.Admin, SSOOnly: u.HashedPassword == nil,
			Groups: groups, AdminGroups: []string{}, IsObserver: rs.Observer, ObserverGroups: []string{},
			IsService: u.IsService, SessionIdleMinutes: set.SessionIdleMinutes(),
			// no personal tokens for the observer (routes/api_tokens.py)
			PersonalTokens:  set.PersonalTokens() && !rs.ObserverOnly(),
			APITokenMaxDays: set.APITokenMaxDays(), Locale: u.Locale,
		})
	})
}

// groupNames is permissions.user_group_ids, then the names: in the groups' order of creation.
func groupNames(r *http.Request, db *pgxpool.Pool, userID int64) ([]string, error) {
	rows, err := db.Query(r.Context(), `SELECT g.name FROM groups g JOIN user_groups ug ON ug.group_id = g.id
		WHERE ug.user_id = $1 ORDER BY g.id`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	names := []string{}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		names = append(names, n)
	}
	return names, rows.Err()
}
