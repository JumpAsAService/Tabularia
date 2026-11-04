package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

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

// ProjectOut is schemas/models.py ProjectOut.
type ProjectOut struct {
	ID          int64  `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
	ParentID    *int64 `json:"parent_id"`
	OwnerID     *int64 `json:"owner_id"`
	// the person whose personal folder this is; null for every other folder
	PersonalOf *int64 `json:"personal_of"`
}

func init() {
	register("projects", func(d *Deps) []server.Route {
		routes := []server.Route{
			{Method: http.MethodGet, Pattern: "/projects", Handler: server.Authed(d.Auth, "/projects", listProjects(d))},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}", Handler: server.Authed(d.Auth, "/projects/{project_id}", getProject(d))},
			{Method: http.MethodDelete, Pattern: "/projects/{project_id}", Handler: server.Authed(d.Auth, "/projects/{project_id}", deleteProject(d))},
			{Method: http.MethodPost, Pattern: "/projects", Handler: withBody(server.Authed(d.Auth, "/projects", createProject(d)))},
		}
		if !d.Config.OpenLineage {
			// a rename or a move renames the folder's content in the lineage catalog
			// (openlineage.folder_names / renamed): with OpenLineage on, Python does it
			routes = append(routes, server.Route{Method: http.MethodPatch, Pattern: "/projects/{project_id}",
				Handler: withBody(server.Authed(d.Auth, "/projects/{project_id}", updateProject(d)))})
		}
		return routes
	})
}

const projectColumns = `id, name, description, parent_id, owner_id`

func scanProject(row pgx.Row) (*ProjectOut, error) {
	var p ProjectOut
	if err := row.Scan(&p.ID, &p.Name, &p.Description, &p.ParentID, &p.OwnerID); err != nil {
		return nil, err
	}
	return &p, nil
}

// readProject is _get_project without its 404: nil when the folder is not there.
func readProject(ctx context.Context, q perm.Querier, id int64) (*ProjectOut, error) {
	p, err := scanProject(q.QueryRow(ctx, `SELECT `+projectColumns+` FROM projects WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return p, err
}

// listProjects is routes/projects.py list_projects: the folders the person sees in the tree
// (permissions.visible_projects), by id.
func listProjects(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		personal, err := personalfolders.All(ctx, d.DB)
		if err != nil {
			fail(d, w, "reading the personal folders", err)
			return
		}
		visible, err := perm.For(d.DB, &p.User).Visible(ctx)
		if err != nil {
			fail(d, w, "reading the permissions", err)
			return
		}
		out := []ProjectOut{}
		if len(visible) > 0 {
			rows, err := d.DB.Query(ctx, `SELECT `+projectColumns+` FROM projects WHERE id = ANY($1) ORDER BY id`, perm.Sorted(visible))
			if err != nil {
				fail(d, w, "reading the folders", err)
				return
			}
			for rows.Next() {
				pr, err := scanProject(rows)
				if err != nil {
					rows.Close()
					fail(d, w, "reading the folders", err)
					return
				}
				if owner, ok := personal[pr.ID]; ok {
					pr.PersonalOf = &owner
				}
				out = append(out, *pr)
			}
			rows.Close()
			if err := rows.Err(); err != nil {
				fail(d, w, "reading the folders", err)
				return
			}
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// getProject is routes/projects.py get_project: VIEW on the folder.
func getProject(d *Deps) authedFunc {
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
		if refusal, err := perm.For(d.DB, &p.User).Ensure(ctx, id, perm.View); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		if pr.PersonalOf, err = personalfolders.Of(ctx, d.DB, id); err != nil {
			fail(d, w, "reading the personal folders", err)
			return
		}
		server.JSON(w, http.StatusOK, pr)
	}
}

// refuseOr500 answers a guard's outcome: its error as a 500, its refusal as it is.
func refuseOr500(d *Deps, w http.ResponseWriter, refusal *auth.Refusal, err error) {
	if err != nil {
		fail(d, w, "checking the request", err)
		return
	}
	server.Refuse(w, refusal)
}

// folderContents are the checks of delete_project after the subfolders, in its order: what
// keeps a folder from being deleted.
var folderContents = []struct{ table, detail string }{
	{"flows", "The folder contains flows: move or delete them first"},
	{"datasources", "The folder contains datasources: move or delete them first"},
	{"connections", "The folder contains connections: move or delete them first"},
	{"saved_views", "The folder contains saved views: move or delete them first"},
	{"flow_groups", "The folder contains flow groups: move or delete them first"},
	{"saved_scripts", "The folder contains scripts: move or delete them first"},
	{"sql_scripts", "The folder contains SQL scripts: move or delete them first"},
	{"notebooks", "The folder contains notebooks: move or delete them first"},
	{"reports", "The folder contains reports: move or delete them first"},
	{"dashboards", "The folder contains dashboards: move or delete them first"},
}

// deleteProject is routes/projects.py delete_project: MANAGE, not a personal folder (but for
// an administrator), and empty.
func deleteProject(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "project_id")
		if !ok {
			return
		}
		refusal, err := removeFolder(r.Context(), d, &p.User, ids[0])
		if err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		returnsNone(w)
	}
}

// removeFolder is the body of delete_project, also called by delete_user for the personal
// folder of an account deleted.
func removeFolder(ctx context.Context, d *Deps, actor *auth.User, id int64) (*auth.Refusal, error) {
	pr, err := readProject(ctx, d.DB, id)
	if err != nil {
		return nil, err
	}
	if pr == nil {
		return &auth.Refusal{Status: http.StatusNotFound, Detail: "Folder not found"}, nil
	}
	if refusal, err := perm.For(d.DB, actor).Ensure(ctx, id, perm.Manage); err != nil || refusal != nil {
		return refusal, err
	}
	if refusal, err := personalfolders.Guard(ctx, d.DB, actor, id, "delete"); err != nil || refusal != nil {
		return refusal, err
	}
	exists := func(q string) (bool, error) {
		var found bool
		err := d.DB.QueryRow(ctx, `SELECT EXISTS (`+q+`)`, id).Scan(&found)
		return found, err
	}
	if found, err := exists(`SELECT 1 FROM projects WHERE parent_id = $1`); err != nil || found {
		return conflict(found, "The folder contains subfolders: empty it first"), err
	}
	for _, c := range folderContents {
		if found, err := exists(`SELECT 1 FROM ` + c.table + ` WHERE project_id = $1`); err != nil || found {
			return conflict(found, c.detail), err
		}
	}
	// a webhook limited to this folder: the first one, as the Python query finds it
	var webhook string
	err = d.DB.QueryRow(ctx, `SELECT name FROM webhooks WHERE project_id = $1 LIMIT 1`, id).Scan(&webhook)
	if err == nil {
		return conflict(true, fmt.Sprintf("The webhook «%s» is limited to this folder: change its folder or delete it first", webhook)), nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	// the runs that published into this folder keep their history, without the destination
	for _, q := range []string{
		`UPDATE runs SET publish_project_id = NULL WHERE publish_project_id = $1`,
		`DELETE FROM permissions WHERE project_id = $1`,
		`DELETE FROM projects WHERE id = $1`,
	} {
		if _, err := tx.Exec(ctx, q, id); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: actor, Action: "folder.delete", TargetType: ptr("folder"),
		TargetID: &id, TargetLabel: &pr.Name, Detail: pyjson.Object{{Key: "parent_id", Value: pr.ParentID}}})
	return nil, nil
}

func conflict(found bool, text string) *auth.Refusal {
	if !found {
		return nil
	}
	return &auth.Refusal{Status: http.StatusConflict, Detail: text}
}

func ptr[T any](v T) *T { return &v }

// createProject is routes/projects.py create_project: at the top level only an administrator,
// under a folder MANAGE on it.
func createProject(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.ProjectCreate)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		name, description, parent := body.Get("name").(string), body.Get("description").(string), optInt(body, "parent_id")
		checker := perm.For(d.DB, &p.User)
		if parent == nil {
			admin, err := checker.IsAdmin(ctx)
			if err != nil {
				fail(d, w, "reading the roles", err)
				return
			}
			if !admin {
				detail(w, http.StatusForbidden, "Only an admin can create top-level folders")
				return
			}
		} else {
			pr, err := readProject(ctx, d.DB, *parent)
			if err != nil {
				fail(d, w, "reading the folder", err)
				return
			}
			if pr == nil {
				detail(w, http.StatusNotFound, "Parent folder not found")
				return
			}
			if refusal, err := checker.Ensure(ctx, *parent, perm.Manage); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
		}
		out, err := scanProject(d.DB.QueryRow(ctx, `INSERT INTO projects (name, description, parent_id, owner_id, created_at)
			VALUES ($1, $2, $3, $4, $5) RETURNING `+projectColumns, name, description, parent, p.User.ID, time.Now().UTC()))
		if err != nil {
			fail(d, w, "creating the folder", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "folder.create", TargetType: ptr("folder"),
			TargetID: &out.ID, TargetLabel: &out.Name, Detail: pyjson.Object{{Key: "parent_id", Value: out.ParentID}}})
		server.JSON(w, http.StatusCreated, out)
	}
}

// updateProject is routes/projects.py update_project: EDIT to rename or describe it; moving
// it is MANAGE on both ends, never into itself or a subfolder, and a personal folder only by
// an administrator.
func updateProject(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		ids, _ := pathIntsInto(&ps, r, "project_id")
		body := ps.Body(bodyOf(r), gatewaymodels.ProjectUpdate)
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
		checker := perm.For(d.DB, &p.User)
		if refusal, err := checker.Ensure(ctx, id, perm.Edit); err != nil || refusal != nil {
			refuseOr500(d, w, refusal, err)
			return
		}
		before := *pr
		name, description, parent := optStr(body, "name"), optStr(body, "description"), optInt(body, "parent_id")
		if name != nil {
			n := pyd.Strip(*name)
			if n == "" {
				detail(w, http.StatusUnprocessableEntity, "The folder needs a name")
				return
			}
			pr.Name = n
		}
		if description != nil {
			pr.Description = *description
		}
		if parent != nil && (pr.ParentID == nil || *parent != *pr.ParentID) {
			if refusal, err := checker.Ensure(ctx, id, perm.Manage); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
			if refusal, err := personalfolders.Guard(ctx, d.DB, &p.User, id, "move"); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
			target, err := readProject(ctx, d.DB, *parent)
			if err != nil {
				fail(d, w, "reading the folder", err)
				return
			}
			if target == nil {
				detail(w, http.StatusNotFound, "New parent folder not found")
				return
			}
			parents, err := checker.Parents(ctx)
			if err != nil {
				fail(d, w, "reading the folders", err)
				return
			}
			if *parent == id || perm.Descendants(parents, map[int64]bool{id: true})[*parent] {
				detail(w, http.StatusUnprocessableEntity, "A folder cannot be moved into itself or into one of its subfolders")
				return
			}
			if refusal, err := checker.Ensure(ctx, *parent, perm.Manage); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
			pr.ParentID = parent
		}
		// as the ORM's flush: no UPDATE when nothing changed (a new row version moves the row)
		if pr.Name != before.Name || pr.Description != before.Description || !sameID(pr.ParentID, before.ParentID) {
			if _, err := d.DB.Exec(ctx, `UPDATE projects SET name = $1, description = $2, parent_id = $3 WHERE id = $4`,
				pr.Name, pr.Description, pr.ParentID, id); err != nil {
				fail(d, w, "changing the folder", err)
				return
			}
		}
		var changed pyjson.Object
		if pr.Name != before.Name {
			changed = append(changed, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: before.Name}, {Key: "to", Value: pr.Name}}})
		}
		if pr.Description != before.Description {
			changed = append(changed, pyjson.KV{Key: "description", Value: pyjson.Object{{Key: "from", Value: before.Description}, {Key: "to", Value: pr.Description}}})
		}
		if len(changed) > 0 {
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "folder.update", TargetType: ptr("folder"),
				TargetID: &id, TargetLabel: &pr.Name, Detail: changed})
		}
		if !sameID(pr.ParentID, before.ParentID) {
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "folder.move", TargetType: ptr("folder"),
				TargetID: &id, TargetLabel: &pr.Name, Detail: pyjson.Object{{Key: "from_parent", Value: before.ParentID}, {Key: "to_parent", Value: pr.ParentID}}})
		}
		if pr.PersonalOf, err = personalfolders.Of(ctx, d.DB, id); err != nil {
			fail(d, w, "reading the personal folders", err)
			return
		}
		server.JSON(w, http.StatusOK, pr)
	}
}

func sameID(a, b *int64) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}
