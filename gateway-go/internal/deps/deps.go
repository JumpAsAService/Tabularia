// Package deps is deps/auth.py's role guards: require_superuser and require_observer, the
// dependencies of the administration panels (a 403 after the authentication's 401).
package deps

import (
	"log/slog"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// Handler is a route's body, once the person passed the guard.
type Handler func(http.ResponseWriter, *http.Request, *auth.Principal)

func guard(a *auth.Authenticator, db roles.Querier, log *slog.Logger, route, detail string,
	ok func(roles.Roles) bool, h Handler) http.Handler {
	return server.Authed(a, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		rs, err := roles.Of(r.Context(), db, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			log.Error("reading the roles", "user", p.User.ID, "err", err)
			server.InternalError(w)
			return
		}
		if !ok(rs) {
			server.Refuse(w, &auth.Refusal{Status: http.StatusForbidden, Detail: detail})
			return
		}
		h(w, r, p)
	})
}

// Superuser is require_superuser: an EFFECTIVE administrator (flag or administrators' group).
func Superuser(a *auth.Authenticator, db roles.Querier, log *slog.Logger, route string, h Handler) http.Handler {
	return guard(a, db, log, route, "Admin privileges required", func(r roles.Roles) bool { return r.Admin }, h)
}

// Observer is require_observer: reads an administration panel, administrator or observer.
func Observer(a *auth.Authenticator, db roles.Querier, log *slog.Logger, route string, h Handler) http.Handler {
	return guard(a, db, log, route, "Administrative read access required", func(r roles.Roles) bool { return r.Observer }, h)
}
