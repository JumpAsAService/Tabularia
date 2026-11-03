package api

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// GroupOut is schemas/models.py GroupOut.
type GroupOut struct {
	ID          int64   `json:"id"`
	Name        string  `json:"name"`
	Description string  `json:"description"`
	CreatedAt   *string `json:"created_at"`
	MemberCount int64   `json:"member_count"`
	IsAdmin     bool    `json:"is_admin"`
	IsObserver  bool    `json:"is_observer"`
}

func init() {
	register("groups", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/groups", Handler: server.Authed(d.Auth, "/groups", listGroups(d))},
			{Method: http.MethodDelete, Pattern: "/groups/{group_id}", Handler: superuser(d, "/groups/{group_id}", deleteGroup(d))},
		}
	})
}

// isoPtr is a UtcDateTime field that may be null.
func isoPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := pyjson.Isoformat(*t)
	return &s
}

// listGroups is routes/groups.py list_groups: every group with its members' count. No ORDER
// BY, as the Python query.
func listGroups(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		ctx := r.Context()
		counts := map[int64]int64{}
		rows, err := d.DB.Query(ctx, `SELECT group_id, count(*) FROM user_groups GROUP BY group_id`)
		if err != nil {
			fail(d, w, "counting the members", err)
			return
		}
		for rows.Next() {
			var id, n int64
			if err := rows.Scan(&id, &n); err != nil {
				rows.Close()
				fail(d, w, "counting the members", err)
				return
			}
			counts[id] = n
		}
		rows.Close()
		rows, err = d.DB.Query(ctx, `SELECT id, name, description, created_at, is_admin, is_observer FROM groups`)
		if err != nil {
			fail(d, w, "reading the groups", err)
			return
		}
		out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (GroupOut, error) {
			var g GroupOut
			var created time.Time
			err := row.Scan(&g.ID, &g.Name, &g.Description, &created, &g.IsAdmin, &g.IsObserver)
			g.CreatedAt = isoPtr(&created)
			g.MemberCount = counts[g.ID]
			return g, err
		})
		if err != nil {
			fail(d, w, "reading the groups", err)
			return
		}
		if out == nil {
			out = []GroupOut{}
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// stillAdmin is permissions.ensure_still_admin, inside the transaction of the changes: the
// person acting must still be an active administrator, or everything is cancelled with a 409.
func stillAdmin(ctx context.Context, tx pgx.Tx, actor *auth.User) (*auth.Refusal, error) {
	var active, superuser, observer bool
	err := tx.QueryRow(ctx, `SELECT is_active, is_superuser, is_observer FROM users WHERE id = $1`, actor.ID).Scan(&active, &superuser, &observer)
	if errors.Is(err, pgx.ErrNoRows) {
		active = false
	} else if err != nil {
		return nil, err
	}
	rs, err := roles.Of(ctx, tx, actor.ID, superuser, observer)
	if err != nil {
		return nil, err
	}
	if active && rs.Admin {
		return nil, nil
	}
	return &auth.Refusal{Status: http.StatusConflict,
		Detail: "This would remove your own admin privileges: ask another administrator to do it"}, nil
}

// deleteGroup is routes/groups.py delete_group: with its memberships, its grants and what
// the administration panel gave it; an administrators' group not by its last administrator.
func deleteGroup(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "group_id")
		if !ok {
			return
		}
		ctx, id := r.Context(), ids[0]
		var name string
		var wasAdmin, wasObserver bool
		err := d.DB.QueryRow(ctx, `SELECT name, is_admin, is_observer FROM groups WHERE id = $1`, id).Scan(&name, &wasAdmin, &wasObserver)
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusNotFound, "Group not found")
			return
		}
		if err != nil {
			fail(d, w, "reading the group", err)
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "deleting the group", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		tag, err := tx.Exec(ctx, `DELETE FROM user_groups WHERE group_id = $1`, id)
		if err != nil {
			fail(d, w, "deleting the group", err)
			return
		}
		members := tag.RowsAffected()
		for _, q := range []string{
			`DELETE FROM permissions WHERE group_id = $1`,
			`DELETE FROM script_environment_groups WHERE group_id = $1`,
			`DELETE FROM container_registry_access WHERE group_id = $1`,
			`DELETE FROM groups WHERE id = $1`,
		} {
			if _, err := tx.Exec(ctx, q, id); err != nil {
				fail(d, w, "deleting the group", err)
				return
			}
		}
		if wasAdmin {
			refusal, err := stillAdmin(ctx, tx, &p.User)
			if err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail(d, w, "deleting the group", err)
			return
		}
		ev := audit.Event{Actor: &p.User, TargetType: ptr("group"), TargetID: &id, TargetLabel: &name}
		gone := ev
		gone.Action, gone.Detail = "group.delete", pyjson.Object{{Key: "members", Value: members}}
		if wasAdmin {
			gone.Detail = append(gone.Detail, pyjson.KV{Key: "admin", Value: true})
		}
		if wasObserver {
			gone.Detail = append(gone.Detail, pyjson.KV{Key: "observer", Value: true})
		}
		audit.Record(ctx, d.DB, d.Log, gone)
		if wasAdmin {
			// an administrators' group disappears: its members lose the privileges
			demote := ev
			demote.Action, demote.Detail = "group.demote", pyjson.Object{{Key: "deleted", Value: true}}
			audit.Record(ctx, d.DB, d.Log, demote)
		}
		if wasObserver {
			revoke := ev
			revoke.Action, revoke.Detail = "group.observer_revoke", pyjson.Object{{Key: "deleted", Value: true}}
			audit.Record(ctx, d.DB, d.Log, revoke)
		}
		returnsNone(w)
	}
}
