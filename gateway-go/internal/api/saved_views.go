package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// SavedViewOut is schemas/models.py SavedViewOut.
type SavedViewOut struct {
	ID             int64   `json:"id"`
	Name           string  `json:"name"`
	Description    string  `json:"description"`
	ProjectID      int64   `json:"project_id"`
	OwnerID        *int64  `json:"owner_id"`
	DatasourceID   int64   `json:"datasource_id"`
	DatasourceName *string `json:"datasource_name"`
	Spec           string  `json:"spec"`
	CreatedAt      *string `json:"created_at"`
	UpdatedAt      *string `json:"updated_at"`
}

const savedViewColumns = `id, name, description, project_id, owner_id, datasource_id, spec, created_at, updated_at`

func scanSavedView(row pgx.Row) (SavedViewOut, error) {
	var v SavedViewOut
	var created, updated time.Time
	err := row.Scan(&v.ID, &v.Name, &v.Description, &v.ProjectID, &v.OwnerID, &v.DatasourceID, &v.Spec, &created, &updated)
	c, u := pyjson.Isoformat(created), pyjson.Isoformat(updated)
	v.CreatedAt, v.UpdatedAt = &c, &u
	return v, err
}

func init() {
	register("saved_views", func(d *Deps) []server.Route {
		authed := func(route string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, route, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/saved-views", Handler: authed("/saved-views", listAllSavedViews(d))},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/saved-views",
				Handler: authed("/projects/{project_id}/saved-views", listProjectSavedViews(d))},
			{Method: http.MethodGet, Pattern: "/saved-views/{view_id}", Handler: authed("/saved-views/{view_id}", getSavedView(d))},
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/saved-views",
				Handler: withBody(authed("/projects/{project_id}/saved-views", createSavedView(d)))},
			{Method: http.MethodPost, Pattern: "/saved-views/{view_id}/duplicate",
				Handler: authed("/saved-views/{view_id}/duplicate", duplicateSavedView(d))},
		}
	})
}

// fail500 logs an unexpected error and answers Starlette's 500.
func fail500(d *Deps, w http.ResponseWriter, what string, err error) {
	d.Log.Error(what, "err", err)
	server.InternalError(w)
}

// exists says whether a row with this id is in the table.
func exists(ctx context.Context, q perm.Querier, table string, id int64) (bool, error) {
	var one int
	err := q.QueryRow(ctx, `SELECT 1 FROM `+pgx.Identifier{table}.Sanitize()+` WHERE id = $1`, id).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// withDatasourceNames is _with_datasource_names: the datasources' names in one query.
func withDatasourceNames(ctx context.Context, d *Deps, views []SavedViewOut) error {
	ids := []int64{}
	for _, v := range views {
		ids = append(ids, v.DatasourceID)
	}
	if len(ids) == 0 {
		return nil
	}
	rows, err := d.DB.Query(ctx, `SELECT id, name FROM datasources WHERE id = ANY($1)`, ids)
	if err != nil {
		return err
	}
	names := map[int64]string{}
	for rows.Next() {
		var id int64
		var n string
		if err := rows.Scan(&id, &n); err != nil {
			rows.Close()
			return err
		}
		names[id] = n
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for i := range views {
		if n, ok := names[views[i].DatasourceID]; ok {
			views[i].DatasourceName = &n
		}
	}
	return nil
}

// toOut is _to_out of one view: its datasource's name read on its own.
func savedViewOut(ctx context.Context, d *Deps, id int64) (SavedViewOut, error) {
	v, err := scanSavedView(d.DB.QueryRow(ctx, `SELECT `+savedViewColumns+` FROM saved_views WHERE id = $1`, id))
	if err != nil {
		return v, err
	}
	var name string
	switch err := d.DB.QueryRow(ctx, `SELECT name FROM datasources WHERE id = $1`, v.DatasourceID).Scan(&name); {
	case err == nil:
		v.DatasourceName = &name
	case !errors.Is(err, pgx.ErrNoRows):
		return v, err
	}
	return v, nil
}

func queryViews(ctx context.Context, d *Deps, where string, args ...any) ([]SavedViewOut, error) {
	rows, err := d.DB.Query(ctx, `SELECT `+savedViewColumns+` FROM saved_views WHERE `+where+` ORDER BY name`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []SavedViewOut{}
	for rows.Next() {
		v, err := scanSavedView(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, withDatasourceNames(ctx, d, out)
}

// listAllSavedViews is GET /saved-views (list_all_saved_views): the views of the readable folders.
func listAllSavedViews(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		readable, err := perm.For(d.DB, &p.User).Readable(r.Context())
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if len(readable) == 0 {
			server.JSON(w, http.StatusOK, []SavedViewOut{})
			return
		}
		views, err := queryViews(r.Context(), d, `project_id = ANY($1)`, perm.Sorted(readable))
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusOK, views)
	}
}

// refuse answers a refusal, or the 500 of an error.
func refuse(d *Deps, w http.ResponseWriter, ref *auth.Refusal, err error) bool {
	if err != nil {
		fail500(d, w, "permissions", err)
		return true
	}
	if ref != nil {
		server.Refuse(w, ref)
		return true
	}
	return false
}

func notFound(detail string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusNotFound, Detail: detail}
}

// listProjectSavedViews is GET /projects/{project_id}/saved-views.
func listProjectSavedViews(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		pid := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		id, ok := rowID(pid)
		found := false
		var err error
		if ok {
			found, err = exists(ctx, d.DB, "projects", id)
		}
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if !found {
			server.Refuse(w, notFound("Folder not found"))
			return
		}
		ref, err := perm.For(d.DB, &p.User).Ensure(ctx, id, perm.View)
		if refuse(d, w, ref, err) {
			return
		}
		views, err := queryViews(ctx, d, `project_id = $1`, id)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusOK, views)
	}
}

// viewOf is _get_view: the view of the path, or the 404 (true when answered).
func viewOf(d *Deps, w http.ResponseWriter, r *http.Request) (SavedViewOut, bool) {
	var ps pyd.Params
	vid := ps.Path("view_id", r.PathValue("view_id"), pyd.Int())
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return SavedViewOut{}, true
	}
	id, ok := rowID(vid)
	if !ok {
		server.Refuse(w, notFound("Saved view not found"))
		return SavedViewOut{}, true
	}
	v, err := scanSavedView(d.DB.QueryRow(r.Context(), `SELECT `+savedViewColumns+` FROM saved_views WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		server.Refuse(w, notFound("Saved view not found"))
		return v, true
	}
	if err != nil {
		fail500(d, w, "saved views", err)
		return v, true
	}
	return v, false
}

// getSavedView is GET /saved-views/{view_id}.
func getSavedView(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		v, done := viewOf(d, w, r)
		if done {
			return
		}
		ref, err := perm.For(d.DB, &p.User).Ensure(r.Context(), v.ProjectID, perm.View)
		if refuse(d, w, ref, err) {
			return
		}
		out, err := savedViewOut(r.Context(), d, v.ID)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// validSpec is _valid_spec: the configuration must be JSON (json.loads of a str: a BOM
// refused); "" is "{}".
func validSpec(spec string) (string, *auth.Refusal) {
	if spec == "" {
		return "{}", nil
	}
	if _, early := pyd.ReadBody([]byte(spec), "application/json"); early != nil || strings.HasPrefix(spec, "\xef\xbb\xbf") {
		return "", &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The view configuration is not valid JSON"}
	}
	return spec, nil
}

// datasourceProject is the datasource's folder, false when there is no such datasource.
func datasourceProject(ctx context.Context, d *Deps, v any) (int64, bool, error) {
	id, ok := rowID(v)
	if !ok {
		return 0, false, nil
	}
	var project int64
	err := d.DB.QueryRow(ctx, `SELECT project_id FROM datasources WHERE id = $1`, id).Scan(&project)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false, nil
	}
	return project, err == nil, err
}

func savedViewTaken(ctx context.Context, d *Deps, project int64, name string, exclude *int64) (bool, error) {
	var one int
	err := d.DB.QueryRow(ctx, `SELECT 1 FROM saved_views WHERE project_id = $1 AND name = $2 AND ($3::int IS NULL OR id <> $3) LIMIT 1`,
		project, name, exclude).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// createSavedView is POST /projects/{project_id}/saved-views (create_saved_view).
func createSavedView(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		pid := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.SavedViewCreate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		project, ok := rowID(pid)
		found := false
		var err error
		if ok {
			found, err = exists(ctx, d.DB, "projects", project)
		}
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if !found {
			server.Refuse(w, notFound("Folder not found"))
			return
		}
		checker := perm.For(d.DB, &p.User)
		if ref, err := checker.Ensure(ctx, project, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		dsProject, found, err := datasourceProject(ctx, d, body.Get("datasource_id"))
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if !found {
			server.Refuse(w, notFound("Datasource not found"))
			return
		}
		if ref, err := checker.Ensure(ctx, dsProject, perm.View); refuse(d, w, ref, err) {
			return
		}
		dsID, _ := rowID(body.Get("datasource_id"))
		name := pyd.Strip(body.Get("name").(string))
		if name == "" {
			server.Refuse(w, &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The saved view needs a name"})
			return
		}
		taken, err := savedViewTaken(ctx, d, project, name, nil)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if taken {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a saved view '%s' in this folder", name)})
			return
		}
		spec, ref := validSpec(body.Get("spec").(string))
		if ref != nil {
			server.Refuse(w, ref)
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO saved_views (name, description, project_id, owner_id, datasource_id, spec, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $7) RETURNING id`, name, body.Get("description").(string), project, p.User.ID, dsID, spec, now).Scan(&id)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		kind := "saved_view"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "saved_view.create", TargetType: &kind, TargetID: &id,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: project}, {Key: "datasource_id", Value: dsID}}})
		out, err := savedViewOut(ctx, d, id)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusCreated, out)
	}
}

// duplicateSavedView is POST /saved-views/{view_id}/duplicate: a copy as «name (i)».
func duplicateSavedView(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		src, done := viewOf(d, w, r)
		if done {
			return
		}
		ctx := r.Context()
		checker := perm.For(d.DB, &p.User)
		if ref, err := checker.Ensure(ctx, src.ProjectID, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		dsProject, found, err := datasourceProject(ctx, d, src.DatasourceID)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if !found {
			server.Refuse(w, notFound("Datasource not found"))
			return
		}
		if ref, err := checker.Ensure(ctx, dsProject, perm.View); refuse(d, w, ref, err) {
			return
		}
		name, err := copynames.Free(ctx, d.DB, "saved_views", src.ProjectID, src.Name)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO saved_views (name, description, project_id, owner_id, datasource_id, spec, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $7) RETURNING id`, name, src.Description, src.ProjectID, p.User.ID, src.DatasourceID, src.Spec, now).Scan(&id)
		if copynames.Clash(err) {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: copynames.ClashDetail})
			return
		}
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		kind := "saved_view"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "saved_view.create", TargetType: &kind, TargetID: &id,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "datasource_id", Value: src.DatasourceID},
				{Key: "duplicate_of", Value: src.ID}}})
		out, err := savedViewOut(ctx, d, id)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusCreated, out)
	}
}
