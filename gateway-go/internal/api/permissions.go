package api

import (
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/personalfolders"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// PermissionOut is schemas/models.py PermissionOut.
type PermissionOut struct {
	ID         int64  `json:"id"`
	ProjectID  int64  `json:"project_id"`
	UserID     *int64 `json:"user_id"`
	GroupID    *int64 `json:"group_id"`
	Capability string `json:"capability"`
}

func init() {
	register("permissions", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/permissions",
				Handler: server.Authed(d.Auth, "/projects/{project_id}/permissions", listPermissions(d))},
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/permissions",
				Handler: withBody(server.Authed(d.Auth, "/projects/{project_id}/permissions", grantPermission(d)))},
			{Method: http.MethodDelete, Pattern: "/permissions/{permission_id}",
				Handler: server.Authed(d.Auth, "/permissions/{permission_id}", revokePermission(d))},
		}
	})
}

// listPermissions is routes/permissions.py list_permissions: MANAGE on the folder. No ORDER
// BY, as the Python query: the database's order.
func listPermissions(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "project_id")
		if !ok {
			return
		}
		ctx, id := r.Context(), ids[0]
		pr, err := readProject(ctx, d.DB, id)
		if err != nil {
			fail(d, w, "reading the folder", err)
			return
		}
		if pr == nil {
			detail(w, http.StatusNotFound, "Folder not found")
			return
		}
		if refusal, err := perm.For(d.DB, &p.User).Ensure(ctx, id, perm.Manage); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT id, project_id, user_id, group_id, capability FROM permissions WHERE project_id = $1`, id)
		if err != nil {
			fail(d, w, "reading the permissions", err)
			return
		}
		out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (PermissionOut, error) {
			var o PermissionOut
			err := row.Scan(&o.ID, &o.ProjectID, &o.UserID, &o.GroupID, &o.Capability)
			return o, err
		})
		if err != nil {
			fail(d, w, "reading the permissions", err)
			return
		}
		if out == nil {
			out = []PermissionOut{}
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// revokePermission is routes/permissions.py revoke_permission: MANAGE on the folder; the
// person's own grant on their personal folder only by an administrator.
func revokePermission(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "permission_id")
		if !ok {
			return
		}
		ctx, id := r.Context(), ids[0]
		var g PermissionOut
		err := d.DB.QueryRow(ctx, `SELECT id, project_id, user_id, group_id, capability FROM permissions WHERE id = $1`, id).
			Scan(&g.ID, &g.ProjectID, &g.UserID, &g.GroupID, &g.Capability)
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusNotFound, "Permission not found")
			return
		}
		if err != nil {
			fail(d, w, "reading the permission", err)
			return
		}
		if refusal, err := perm.For(d.DB, &p.User).Ensure(ctx, g.ProjectID, perm.Manage); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		if refusal, err := personalfolders.GuardGrant(ctx, d.DB, &p.User, g.ProjectID, g.UserID, g.Capability); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		if _, err := d.DB.Exec(ctx, `DELETE FROM permissions WHERE id = $1`, id); err != nil {
			fail(d, w, "deleting the permission", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "permission.revoke", TargetType: ptr("permission"),
			TargetID: &id, TargetLabel: &g.Capability, Detail: pyjson.Object{
				{Key: "project_id", Value: g.ProjectID}, {Key: "user_id", Value: g.UserID},
				{Key: "group_id", Value: g.GroupID}, {Key: "capability", Value: g.Capability}}})
		returnsNone(w)
	}
}

// grantPermission is routes/permissions.py grant_permission: MANAGE on the folder, one subject
// (a person or a group); the same grant twice answers the one there.
func grantPermission(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		ids, _ := pathIntsInto(&ps, r, "project_id")
		body := ps.Body(bodyOf(r), gatewaymodels.PermissionCreate)
		if !validated(w, &ps) {
			return
		}
		ctx, id := r.Context(), ids[0]
		pr, err := readProject(ctx, d.DB, id)
		if err != nil {
			fail(d, w, "reading the folder", err)
			return
		}
		if pr == nil {
			detail(w, http.StatusNotFound, "Folder not found")
			return
		}
		if refusal, err := perm.For(d.DB, &p.User).Ensure(ctx, id, perm.Manage); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		userID, groupID, capability := optInt(body, "user_id"), optInt(body, "group_id"), body.Get("capability").(string)
		if (userID == nil) == (groupID == nil) {
			detail(w, http.StatusUnprocessableEntity, "Specify exactly one of user_id and group_id")
			return
		}
		for _, c := range []struct {
			id           *int64
			table, label string
		}{{userID, "users", "User not found"}, {groupID, "groups", "Group not found"}} {
			if c.id == nil {
				continue
			}
			var found bool
			if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM `+c.table+` WHERE id = $1)`, *c.id).Scan(&found); err != nil {
				fail(d, w, "reading the subject", err)
				return
			}
			if !found {
				detail(w, http.StatusNotFound, c.label)
				return
			}
		}
		var g PermissionOut
		// no exact duplicates: the same subject and capability on the same folder
		err = d.DB.QueryRow(ctx, `SELECT id, project_id, user_id, group_id, capability FROM permissions
			WHERE project_id = $1 AND user_id IS NOT DISTINCT FROM $2 AND group_id IS NOT DISTINCT FROM $3 AND capability = $4 LIMIT 1`,
			id, userID, groupID, capability).Scan(&g.ID, &g.ProjectID, &g.UserID, &g.GroupID, &g.Capability)
		if err == nil {
			server.JSON(w, http.StatusCreated, g)
			return
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			fail(d, w, "reading the permissions", err)
			return
		}
		err = d.DB.QueryRow(ctx, `INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)
			RETURNING id, project_id, user_id, group_id, capability`, id, userID, groupID, capability).
			Scan(&g.ID, &g.ProjectID, &g.UserID, &g.GroupID, &g.Capability)
		if err != nil {
			fail(d, w, "granting", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "permission.grant", TargetType: ptr("permission"),
			TargetID: &g.ID, TargetLabel: &capability, Detail: pyjson.Object{
				{Key: "project_id", Value: id}, {Key: "user_id", Value: userID},
				{Key: "group_id", Value: groupID}, {Key: "capability", Value: capability}}})
		server.JSON(w, http.StatusCreated, g)
	}
}
