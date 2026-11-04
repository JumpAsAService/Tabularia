package api

import (
	"log/slog"
	"net/http"
	"sort"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/openlineage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// Deps is what the ported routes are built with.
type Deps struct {
	DB     *pgxpool.Pool
	Auth   *auth.Authenticator
	Panel  *settings.Panel
	Config *config.Config
	Log    *slog.Logger
	Engine *engine.Client // core/engine_client.py
	Fernet *secret.Fernet // core/crypto.py
	// Upstream is the Python gateway behind: what a ported route cannot answer yet goes there
	// untouched (see intPath)
	Upstream http.Handler
	// AIService forwards to the AI service (APP__ROLE=ai) the assistant's routes, untouched;
	// SQL asks it for the SQL parsing (docs/specs/gateway-go.md, «The AI service»)
	AIService http.Handler
	SQL       *aisvc.Client
	// Lineage sends the OpenLineage events (services/openlineage.py); nil or off: nothing
	Lineage *openlineage.Emitter
}

// Each file of this package registers its routes in an init(), one module of the Python
// gateway per file (routes/<module>.py → <module>.go): ports done side by side never edit a
// shared list.
var modules = map[string]func(*Deps) []server.Route{}

func register(module string, routes func(*Deps) []server.Route) {
	if _, dup := modules[module]; dup {
		panic("api: module registered twice: " + module)
	}
	modules[module] = routes
}

// Routes is every ported route, module by module in name order.
func Routes(d *Deps) []server.Route {
	names := make([]string, 0, len(modules))
	for n := range modules {
		names = append(names, n)
	}
	sort.Strings(names)
	var out []server.Route
	for _, n := range names {
		out = append(out, modules[n](d)...)
	}
	return out
}
