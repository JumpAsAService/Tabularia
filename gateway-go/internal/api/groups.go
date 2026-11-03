package api

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
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
			{Method: http.MethodPost, Pattern: "/groups", Handler: withBody(superuser(d, "/groups", createGroup(d)))},
			{Method: http.MethodPatch, Pattern: "/groups/{group_id}", Handler: withBody(superuser(d, "/groups/{group_id}", updateGroup(d)))},
		}
	})
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

const groupTaken = "There is already a group with this name"

// createGroup is routes/groups.py create_group: a name nobody has (the unique index decides
// between two at once).
func createGroup(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.GroupCreate)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		name, description := body.Get("name").(string), body.Get("description").(string)
		var taken bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM groups WHERE name = $1)`, name).Scan(&taken); err != nil {
			fail(d, w, "reading the groups", err)
			return
		}
		if taken {
			detail(w, http.StatusConflict, groupTaken)
			return
		}
		g := GroupOut{Name: name, Description: description}
		var created time.Time
		err := d.DB.QueryRow(ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at)
			VALUES ($1, $2, false, false, $3) RETURNING id, created_at`, name, description, time.Now().UTC()).Scan(&g.ID, &created)
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			detail(w, http.StatusConflict, groupTaken)
			return
		}
		if err != nil {
			fail(d, w, "creating the group", err)
			return
		}
		g.CreatedAt = isoPtr(&created)
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "group.create", TargetType: ptr("group"),
			TargetID: &g.ID, TargetLabel: &g.Name})
		server.JSON(w, http.StatusCreated, g)
	}
}

// updateGroup is routes/groups.py update_group: the description and the two flags. Giving a
// role is not done with a token; taking the administrators' role away not from oneself's last
// way to it; the members who become only observers lose their personal tokens.
func updateGroup(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		ids, _ := pathIntsInto(&ps, r, "group_id")
		body := ps.Body(bodyOf(r), gatewaymodels.GroupUpdate)
		if !validated(w, &ps) {
			return
		}
		ctx, id := r.Context(), ids[0]
		g := GroupOut{ID: id}
		var created time.Time
		err := d.DB.QueryRow(ctx, `SELECT name, description, created_at, is_admin, is_observer FROM groups WHERE id = $1`, id).
			Scan(&g.Name, &g.Description, &created, &g.IsAdmin, &g.IsObserver)
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusNotFound, "Group not found")
			return
		}
		if err != nil {
			fail(d, w, "reading the group", err)
			return
		}
		g.CreatedAt = isoPtr(&created)
		wasAdmin, wasObserver := g.IsAdmin, g.IsObserver
		description, isAdmin, isObserver := optStr(body, "description"), optBool(body, "is_admin"), optBool(body, "is_observer")
		if (isAdmin != nil && *isAdmin && !wasAdmin) || (isObserver != nil && *isObserver && !wasObserver) {
			// every member would receive the role: a way in that would stay after a leaked token
			if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoRolesFromToken); refusal != nil {
				server.Refuse(w, refusal)
				return
			}
		}
		var described pyjson.Object
		if description != nil && *description != g.Description {
			described = pyjson.Object{{Key: "from", Value: g.Description}, {Key: "to", Value: *description}}
			g.Description = *description
		}
		if isAdmin != nil {
			g.IsAdmin = *isAdmin
		}
		if isObserver != nil {
			g.IsObserver = *isObserver
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "changing the group", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		if _, err := tx.Exec(ctx, `UPDATE groups SET description = $1, is_admin = $2, is_observer = $3 WHERE id = $4`,
			g.Description, g.IsAdmin, g.IsObserver, id); err != nil {
			fail(d, w, "changing the group", err)
			return
		}
		if wasAdmin && !g.IsAdmin {
			if refusal, err := stillAdmin(ctx, tx, &p.User); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
		}
		if g.IsObserver && !wasObserver {
			members, err := memberIDs(ctx, tx, id)
			if err == nil {
				_, err = apitokens.WithdrawIfObserver(ctx, tx, members)
			}
			if err != nil {
				fail(d, w, "withdrawing the tokens", err)
				return
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail(d, w, "changing the group", err)
			return
		}
		if err := d.DB.QueryRow(ctx, `SELECT count(*) FROM user_groups WHERE group_id = $1`, id).Scan(&g.MemberCount); err != nil {
			fail(d, w, "counting the members", err)
			return
		}
		ev := audit.Event{Actor: &p.User, TargetType: ptr("group"), TargetID: &id, TargetLabel: &g.Name}
		members := pyjson.Object{{Key: "members", Value: g.MemberCount}}
		if described != nil {
			e := ev
			e.Action, e.Detail = "group.update", pyjson.Object{{Key: "description", Value: described}}
			audit.Record(ctx, d.DB, d.Log, e)
		}
		if g.IsAdmin != wasAdmin {
			e := ev
			e.Action, e.Detail = map[bool]string{true: "group.promote", false: "group.demote"}[g.IsAdmin], members
			audit.Record(ctx, d.DB, d.Log, e)
		}
		if g.IsObserver != wasObserver {
			e := ev
			e.Action, e.Detail = map[bool]string{true: "group.observer_grant", false: "group.observer_revoke"}[g.IsObserver], members
			audit.Record(ctx, d.DB, d.Log, e)
		}
		server.JSON(w, http.StatusOK, g)
	}
}

// memberIDs are a group's members, as the Python query reads them.
func memberIDs(ctx context.Context, q perm.Querier, groupID int64) ([]int64, error) {
	rows, err := q.Query(ctx, `SELECT user_id FROM user_groups WHERE group_id = $1`, groupID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[int64])
}
