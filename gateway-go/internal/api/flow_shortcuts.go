package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The flow editor's right-click menu: the transformations a person keeps there, in their
// order (users.flow_shortcuts, migration 0038). A route of the Go gateway only: it has no
// Python twin (server/go_only_routes.go). A preference of the person, not of the data: an
// observer keeps one as well; a read-only API token cannot change it (auth's level check).

// FlowShortcutOps are the operations a flow holds as transformation nodes: the engine's
// registry (backend/app/engine/operations.py) without `foreach`, a container.
var FlowShortcutOps = []any{
	"select", "reorder", "drop", "rename", "cast", "compute", "sql", "filter", "sort", "limit",
	"unique", "sample", "fill_null", "drop_nulls", "group_by", "pivot", "unpivot", "join", "union",
}

// FlowShortcutsMax is how many the menu holds.
const FlowShortcutsMax = 8

// flowShortcutsChange is the PUT's body, validated as pydantic would: `shortcuts` is
// required, null (back to the interface's defaults) or a list of at most eight known
// operations, each once.
var flowShortcutsChange = pyd.NewModel("FlowShortcutsChange",
	pyd.F("shortcuts", pyd.Optional(pyd.List(pyd.Literal(FlowShortcutOps...))).MaxLen(FlowShortcutsMax)),
).FieldValidator(shortcutsOnce, "shortcuts")

func shortcutsOnce(v any) (any, error) {
	list, _ := v.([]any)
	seen := map[any]bool{}
	for _, o := range list {
		if seen[o] {
			return nil, errors.New("an operation appears more than once")
		}
		seen[o] = true
	}
	return v, nil
}

// flowShortcutsOut is the answer of both routes: null while never chosen.
type flowShortcutsOut struct {
	Shortcuts []string `json:"shortcuts"`
}

type rowQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

func init() {
	register("flow_shortcuts", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/users/me/flow-shortcuts", Handler: server.Authed(d.Auth, "/users/me/flow-shortcuts", getMyFlowShortcuts(d))},
			{Method: http.MethodPut, Pattern: "/users/me/flow-shortcuts", Handler: withBody(server.Authed(d.Auth, "/users/me/flow-shortcuts", setMyFlowShortcuts(d)))},
		}
	})
}

// readFlowShortcuts is the person's saved menu; nil when never chosen, or when the column
// holds something that is not a list of names (the interface's defaults then too).
func readFlowShortcuts(ctx context.Context, q rowQuerier, userID int64) ([]string, error) {
	var raw *string
	if err := q.QueryRow(ctx, `SELECT flow_shortcuts FROM users WHERE id = $1`, userID).Scan(&raw); err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, nil
	}
	var list []string
	if json.Unmarshal([]byte(*raw), &list) != nil || list == nil {
		return nil, nil
	}
	return list, nil
}

// writeFlowShortcuts saves the menu; nil clears it (back to the defaults).
func writeFlowShortcuts(ctx context.Context, q rowQuerier, userID int64, list []string) error {
	var value *string
	if list != nil {
		b, err := json.Marshal(list)
		if err != nil {
			return err
		}
		s := string(b)
		value = &s
	}
	_, err := q.Exec(ctx, `UPDATE users SET flow_shortcuts = $1 WHERE id = $2 AND flow_shortcuts IS DISTINCT FROM $1`, value, userID)
	return err
}

func getMyFlowShortcuts(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		list, err := readFlowShortcuts(r.Context(), d.DB, p.User.ID)
		if err != nil {
			fail(d, w, "reading the flow shortcuts", err)
			return
		}
		server.JSON(w, http.StatusOK, flowShortcutsOut{Shortcuts: list})
	}
}

func setMyFlowShortcuts(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), flowShortcutsChange)
		if !validated(w, &ps) {
			return
		}
		list := shortcutList(body.Get("shortcuts"))
		if err := writeFlowShortcuts(r.Context(), d.DB, p.User.ID, list); err != nil {
			fail(d, w, "saving the flow shortcuts", err)
			return
		}
		server.JSON(w, http.StatusOK, flowShortcutsOut{Shortcuts: list})
	}
}

// shortcutList is the validated field as names: nil for null, an empty list stays empty.
func shortcutList(v any) []string {
	items, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, o := range items {
		out = append(out, o.(string))
	}
	return out
}
