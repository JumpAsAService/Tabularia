package api

// routes/notebooks.py: marimo notebooks kept as objects of a folder, with their versions.
// Ported: every route; while OpenLineage is on, the change and the delete stay the Python
// gateway's (a rename tells the catalog, a delete ends the live sessions' runs there).

import (
	"context"
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
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("notebooks", func(d *Deps) []server.Route {
		plain := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		body := func(method, pattern string, h scBodyHandler) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: scWithBody(d, pattern, h)}
		}
		return []server.Route{
			plain(http.MethodGet, "/notebooks", listNotebooks),
			plain(http.MethodGet, "/projects/{project_id}/notebooks", listProjectNotebooks),
			plain(http.MethodGet, "/notebooks/{notebook_id}", getNotebook),
			body(http.MethodPost, "/projects/{project_id}/notebooks", createNotebook),
			body(http.MethodPatch, "/notebooks/{notebook_id}", updateNotebook),
			plain(http.MethodDelete, "/notebooks/{notebook_id}/draft", discardDraft),
			plain(http.MethodPost, "/notebooks/{notebook_id}/duplicate", duplicateNotebook),
			plain(http.MethodDelete, "/notebooks/{notebook_id}", deleteNotebook),
			plain(http.MethodGet, "/notebooks/{notebook_id}/versions", listNotebookVersions),
			plain(http.MethodGet, "/notebooks/{notebook_id}/versions/{version}", getNotebookVersion),
			plain(http.MethodPost, "/notebooks/{notebook_id}/versions/{version}/restore", restoreNotebookVersion),
		}
	})
}

// nbMaxSourceBytes is notebooks.MAX_SOURCE_BYTES.
const nbMaxSourceBytes = scriptnodes.MaxCodeBytes

type nbRow struct {
	ID             int64
	Name           string
	Description    string
	ProjectID      int64
	EnvironmentID  int64
	CurrentVersion int64
	Draft          *string
	DraftBy        *int64
	DraftAt        *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
	MaxVisitors    *int64
}

const nbColumns = `id, name, description, project_id, environment_id, current_version, draft, draft_by, draft_at, created_at,
	updated_at, max_visitors`

func nbScan(row pgx.Row) (*nbRow, error) {
	var n nbRow
	if err := row.Scan(&n.ID, &n.Name, &n.Description, &n.ProjectID, &n.EnvironmentID, &n.CurrentVersion, &n.Draft, &n.DraftBy,
		&n.DraftAt, &n.CreatedAt, &n.UpdatedAt, &n.MaxVisitors); err != nil {
		return nil, err
	}
	return &n, nil
}

func nbGet(ctx context.Context, q perm.Querier, id int64, lock bool) (*nbRow, error) {
	sql := `SELECT ` + nbColumns + ` FROM notebooks WHERE id = $1`
	if lock {
		sql += ` FOR UPDATE`
	}
	n, err := nbScan(q.QueryRow(ctx, sql, id))
	if scNoRows(err) {
		return nil, nil
	}
	return n, err
}

type notebookOut struct {
	ID              int64   `json:"id"`
	Name            string  `json:"name"`
	Description     string  `json:"description"`
	ProjectID       int64   `json:"project_id"`
	EnvironmentID   int64   `json:"environment_id"`
	EnvironmentName *string `json:"environment_name"`
	Language        *string `json:"language"`
	CurrentVersion  int64   `json:"current_version"`
	CreatedAt       string  `json:"created_at"`
	UpdatedAt       string  `json:"updated_at"`
	Editable        bool    `json:"editable"`
}

type nbPerson struct {
	ID       int64  `json:"id"`
	Email    string `json:"email"`
	FullName string `json:"full_name"`
}

type notebookDetail struct {
	notebookOut
	Content         pyjson.Object `json:"content"`
	Author          *nbPerson     `json:"author"`
	Draft           *string       `json:"draft"`
	DraftBy         *string       `json:"draft_by"`
	DraftAt         *string       `json:"draft_at"`
	MaxVisitors     *int64        `json:"max_visitors"`
	VisitorsDefault int           `json:"visitors_default"`
	VisitorsCeiling int           `json:"visitors_ceiling"`
}

func nbOutOf(ctx context.Context, c *perm.Checker, q perm.Querier, n *nbRow) (notebookOut, error) {
	out := notebookOut{ID: n.ID, Name: n.Name, Description: n.Description, ProjectID: n.ProjectID, EnvironmentID: n.EnvironmentID,
		CurrentVersion: n.CurrentVersion, CreatedAt: pyjson.Isoformat(n.CreatedAt), UpdatedAt: pyjson.Isoformat(n.UpdatedAt)}
	env, err := scGetEnv(ctx, q, n.EnvironmentID)
	if err != nil {
		return out, err
	}
	if env != nil {
		out.EnvironmentName, out.Language = &env.Name, &env.Language
	}
	out.Editable, err = c.Has(ctx, n.ProjectID, perm.Edit)
	return out, err
}

// nbEmpty is notebooks.empty_content.
func nbEmpty() pyjson.Object { return pyjson.Object{{Key: "source", Value: ""}} }

// nbPersonOf is a user as Person/Author: nil when the account is gone.
func nbPersonOf(ctx context.Context, q perm.Querier, id *int64) (*nbPerson, error) {
	if id == nil || *id == 0 {
		return nil, nil
	}
	var p nbPerson
	var full *string
	err := q.QueryRow(ctx, `SELECT id, email, full_name FROM users WHERE id = $1`, *id).Scan(&p.ID, &p.Email, &full)
	if scNoRows(err) {
		return nil, nil
	}
	if full != nil {
		p.FullName = *full
	}
	return &p, err
}

func nbDetailOf(ctx context.Context, c *perm.Checker, q perm.Querier, n *nbRow) (*notebookDetail, error) {
	out, err := nbOutOf(ctx, c, q, n)
	if err != nil {
		return nil, err
	}
	var createdBy *int64
	exists := false
	err = q.QueryRow(ctx, `SELECT created_by FROM notebook_versions WHERE notebook_id = $1 AND version = $2`, n.ID, n.CurrentVersion).Scan(&createdBy)
	if err == nil {
		exists = true
	} else if !scNoRows(err) {
		return nil, err
	}
	var author *nbPerson
	if exists {
		if author, err = nbPersonOf(ctx, q, createdBy); err != nil {
			return nil, err
		}
	}
	content, err := nbContentOf(ctx, q, n, 0)
	if err != nil {
		return nil, err
	}
	limits := scriptcfg.Get().Notebooks
	det := &notebookDetail{notebookOut: out, Content: content, Author: author, MaxVisitors: n.MaxVisitors,
		VisitorsDefault: min(limits.MaxVisitorsPerApp, limits.MaxVisitorsCeiling), VisitorsCeiling: limits.MaxVisitorsCeiling}
	if out.Editable && n.Draft != nil {
		det.Draft, det.DraftAt = n.Draft, isoPtr(n.DraftAt)
		if by, err := nbPersonOf(ctx, q, n.DraftBy); err != nil {
			return nil, err
		} else if by != nil {
			det.DraftBy = &by.Email
		}
	}
	return det, nil
}

// nbContentOf is notebooks.content_of (`version` 0: the current one).
func nbContentOf(ctx context.Context, q perm.Querier, n *nbRow, version int64) (pyjson.Object, error) {
	_, raw, err := savedscripts.VersionRow(ctx, q, "notebook_versions", "notebook_id", n.ID, n.CurrentVersion, version)
	if err != nil {
		return nil, err
	}
	return savedscripts.Over(nbEmpty(), raw)
}

func nbSendDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, id int64, status int) {
	ctx := r.Context()
	n, err := nbGet(ctx, d.DB, id, false)
	if err == nil && n == nil {
		err = fmt.Errorf("notebook %d is gone", id)
	}
	if err != nil {
		scFailed(d, w, "notebooks: reading the notebook again", err)
		return
	}
	det, err := nbDetailOf(ctx, perm.For(d.DB, &p.User), d.DB, n)
	if err != nil {
		scFailed(d, w, "notebooks: the notebook's detail", err)
		return
	}
	server.JSON(w, status, det)
}

func nbVisible(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, p *auth.Principal, id int64) (*nbRow, bool) {
	ctx := r.Context()
	n, err := nbGet(ctx, d.DB, id, false)
	if err != nil {
		scFailed(d, w, "notebooks: reading the notebook", err)
		return nil, false
	}
	if n != nil {
		ok, err := savedscripts.CanSee(ctx, c, d.DB, p.User.ID, n.ProjectID, n.EnvironmentID)
		if err != nil {
			scFailed(d, w, "notebooks: who sees it", err)
			return nil, false
		}
		if ok {
			return n, true
		}
	}
	scRefuse(w, http.StatusNotFound, "Notebook not found")
	return nil, false
}

func nbNeedOn(w http.ResponseWriter) bool {
	if !scriptsOn() {
		scRefuse(w, http.StatusConflict, "Notebooks need the scripting environments (Kubernetes)")
		return false
	}
	return true
}

// nbEnvironment is _environment: one this person may use, and in Python.
func nbEnvironment(ctx context.Context, c *perm.Checker, q perm.Querier, userID, id int64) (*scEnvironment, *auth.Refusal, error) {
	env, refusal, err := scriptEnvironment(ctx, c, q, userID, id)
	if err != nil || refusal != nil {
		return nil, refusal, err
	}
	if env.Language != "python" {
		return nil, &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("A notebook needs a Python environment: «%s» is for %s",
			env.Name, strings.ToUpper(env.Language))}, nil
	}
	return env, nil, nil
}

// nbMaxVisitors is _max_visitors: within the installation's ceiling.
func nbMaxVisitors(v *int64) *auth.Refusal {
	ceiling := scriptcfg.Get().Notebooks.MaxVisitorsCeiling
	if v != nil && *v > int64(ceiling) {
		return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("At most %d people at once: the most this installation "+
			"allows (NOTEBOOKS__MAX_VISITORS_CEILING)", ceiling)}
	}
	return nil
}

// nbCheckContent is notebooks.check_content.
func nbCheckContent(name string, content any) (pyjson.Object, *auth.Refusal) {
	refuse := func(m string) *auth.Refusal {
		return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("Notebook «%s»: %s", name, m)}
	}
	c, ok := content.(pyjson.Object)
	if !ok {
		return nil, refuse("its content is not an object")
	}
	source, ok := scGet(c, "source").(string)
	if !ok {
		return nil, refuse("its source is the text of a marimo file")
	}
	if pyd.Strip(source) == "" {
		return nil, refuse("its source is empty")
	}
	if len(source) > nbMaxSourceBytes {
		return nil, refuse(fmt.Sprintf("its source is larger than %d KB", nbMaxSourceBytes/1024))
	}
	return pyjson.Object{{Key: "source", Value: source}}, nil
}

// nbStarter is notebooks.starter_source.
func nbStarter(name string) string {
	title := pyd.Strip(name)
	if title == "" {
		title = "Notebook"
	}
	return "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    import marimo as mo\n    return (mo,)\n\n\n" +
		"@app.cell\ndef _(mo):\n    mo.md(" + pyjson.Repr("# "+title) + ")\n    return\n\n\n" +
		"@app.cell\ndef _():\n    # import tabularia\n" +
		"    # df = tabularia.read(\"Sample/orders\")  # any datasource the app's author can read\n    # df\n    return\n\n\n" +
		"if __name__ == \"__main__\":\n    app.run()\n"
}

// nbNotBeingEdited is _not_being_edited: 409 while marimo's editor is open on it.
func nbNotBeingEdited(ctx context.Context, q perm.Querier, n *nbRow) (*auth.Refusal, error) {
	var userID int64
	err := q.QueryRow(ctx, `SELECT user_id FROM notebook_sessions WHERE notebook_id = $1 AND mode = 'edit'
		AND status IN ('starting', 'ready')`, n.ID).Scan(&userID)
	if scNoRows(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	who := "someone"
	if p, err := nbPersonOf(ctx, q, &userID); err != nil {
		return nil, err
	} else if p != nil {
		who = p.Email
	}
	return &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("«%s» is open in marimo's editor by %s: save or close it there first",
		n.Name, who)}, nil
}

// ── lists ─────────────────────────────────────────────────────────────────────

func nbMany(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, p *auth.Principal, projects []int64) {
	ctx := r.Context()
	where, args, ok, err := savedscripts.VisibleFilter(ctx, c, d.DB, p.User.ID, projects)
	if err != nil {
		scFailed(d, w, "notebooks: the list", err)
		return
	}
	out := []notebookOut{}
	if ok {
		rows, err := d.DB.Query(ctx, `SELECT `+nbColumns+` FROM notebooks`+where+` ORDER BY name`, args...)
		if err != nil {
			scFailed(d, w, "notebooks: the list", err)
			return
		}
		var list []nbRow
		for rows.Next() {
			n, err := nbScan(rows)
			if err != nil {
				rows.Close()
				scFailed(d, w, "notebooks: a notebook", err)
				return
			}
			list = append(list, *n)
		}
		rows.Close()
		for i := range list {
			o, err := nbOutOf(ctx, c, d.DB, &list[i])
			if err != nil {
				scFailed(d, w, "notebooks: a notebook", err)
				return
			}
			out = append(out, o)
		}
	}
	server.JSON(w, http.StatusOK, out)
}

// listNotebooks is GET /notebooks (list_notebooks).
func listNotebooks(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if !scriptsOn() {
		server.JSON(w, http.StatusOK, []any{})
		return
	}
	nbMany(d, w, r, perm.For(d.DB, &p.User), p, nil)
}

// listProjectNotebooks is GET /projects/{project_id}/notebooks.
func listProjectNotebooks(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, ids[0])
	if err != nil {
		scFailed(d, w, "notebooks: the folder", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, ids[0], perm.View) {
		return
	}
	if !scriptsOn() {
		server.JSON(w, http.StatusOK, []any{})
		return
	}
	nbMany(d, w, r, c, p, []int64{ids[0]})
}

// getNotebook is GET /notebooks/{notebook_id} (get_notebook).
func getNotebook(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id")
	if !ok {
		return
	}
	if !scriptsOn() {
		scRefuse(w, http.StatusNotFound, "Notebook not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	det, err := nbDetailOf(r.Context(), c, d.DB, n)
	if err != nil {
		scFailed(d, w, "notebooks: the notebook's detail", err)
		return
	}
	server.JSON(w, http.StatusOK, det)
}

// ── create, change, copy, delete ──────────────────────────────────────────────

func nbAddVersion(ctx context.Context, tx pgx.Tx, id int64, content pyjson.Object, note string, userID int64) (int64, error) {
	return savedscripts.AddVersion(ctx, tx, "notebooks", "notebook_versions", "notebook_id", id, content, note, userID)
}

// createNotebook is POST /projects/{project_id}/notebooks (create_notebook).
func createNotebook(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	pid := params.Path("project_id", r.PathValue("project_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.NotebookCreate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !nbNeedOn(w) {
		return
	}
	project, ok := pid.(int64)
	if !ok {
		project = -1
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, project)
	if err != nil {
		scFailed(d, w, "notebooks: the folder", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, project, perm.Edit) {
		return
	}
	envID, _ := in.Get("environment_id").(int64)
	env, refusal, err := nbEnvironment(ctx, c, d.DB, p.User.ID, envID)
	if scAnswer(d, w, "notebooks: the environment", refusal, err) {
		return
	}
	name, refusal, err := scObjectName(ctx, d.DB, "notebooks", "notebook", "notebook", project, in.Get("name").(string), 0)
	if scAnswer(d, w, "notebooks: the name", refusal, err) {
		return
	}
	var maxVisitors *int64
	if v, ok := in.Get("max_visitors").(int64); ok {
		maxVisitors = &v
	}
	if refusal := nbMaxVisitors(maxVisitors); refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var content any = pyjson.Object{{Key: "source", Value: nbStarter(name)}}
	if raw := in.Get("content"); raw != nil {
		content = scFromPyd(raw)
	}
	checked, refusal := nbCheckContent(name, content)
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var id int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO notebooks (name, description, project_id, owner_id, environment_id, current_version,
			draft, draft_by, draft_at, created_at, updated_at, max_visitors) VALUES ($1, $2, $3, $4, $5, 0, NULL, NULL, NULL, $6, $6, $7)
			RETURNING id`, name, pyd.Strip(in.Get("description").(string)), project, p.User.ID, env.ID, now, maxVisitors).Scan(&id); err != nil {
			return err
		}
		v, err := nbAddVersion(ctx, tx, id, checked, "created", p.User.ID)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE notebooks SET current_version = $1 WHERE id = $2`, v, id)
		return err
	})
	if err != nil {
		scFailed(d, w, "notebooks: creating the notebook", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.create", TargetType: scPtrStr("notebook"),
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: project}, {Key: "environment", Value: env.Name}}})
	nbSendDetail(d, w, r, p, id, http.StatusCreated)
}

// updateNotebook is PATCH /notebooks/{notebook_id} (update_notebook).
func updateNotebook(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	if scOpenLineageOn() {
		scForward(d, w, r)
		return
	}
	var params pyd.Params
	nid := params.Path("notebook_id", r.PathValue("notebook_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.NotebookUpdate)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	if !nbNeedOn(w) {
		return
	}
	id, ok := nid.(int64)
	if !ok {
		id = -1
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, id)
	if !ok || !scEnsure(d, w, ctx, c, n.ProjectID, perm.Edit) {
		return
	}
	var stop *auth.Refusal
	var changes pyjson.Object
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		// read again and held until the commit: a save of a moment ago is the current version
		var err error
		if n, err = nbGet(ctx, tx, n.ID, true); err != nil {
			return err
		}
		changes = pyjson.Object{}
		target := n.ProjectID
		if v, ok := in.Get("project_id").(int64); ok && v != n.ProjectID {
			exists, err := scFolderExists(ctx, tx, v)
			if err != nil {
				return err
			}
			if !exists {
				stop = &auth.Refusal{Status: http.StatusNotFound, Detail: "Destination folder not found"}
				return scRefused
			}
			if stop, err = c.Ensure(ctx, v, perm.Edit); err != nil || stop != nil {
				return scOr(err)
			}
			target = v
		}
		wanted := n.Name
		if v, ok := in.Get("name").(string); ok {
			wanted = v
		}
		name, refusal, err := scObjectName(ctx, tx, "notebooks", "notebook", "notebook", target, wanted, n.ID)
		if err != nil || refusal != nil {
			stop = refusal
			return scOr(err)
		}
		if name != n.Name {
			changes = append(changes, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: n.Name}, {Key: "to", Value: name}}})
		}
		if target != n.ProjectID {
			changes = append(changes, pyjson.KV{Key: "project_id", Value: pyjson.Object{{Key: "from", Value: n.ProjectID}, {Key: "to", Value: target}}})
		}
		envID := n.EnvironmentID
		if v, ok := in.Get("environment_id").(int64); ok && v != n.EnvironmentID {
			env, refusal, err := nbEnvironment(ctx, c, tx, p.User.ID, v)
			if err != nil || refusal != nil {
				stop = refusal
				return scOr(err)
			}
			changes = append(changes, pyjson.KV{Key: "environment", Value: env.Name})
			envID = env.ID
		} else if env, err := scGetEnv(ctx, tx, n.EnvironmentID); err != nil {
			return err
		} else if env == nil {
			stop = &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "Its scripting environment no longer exists: choose another"}
			return scRefused
		}
		var newContent pyjson.Object
		if raw := in.Get("content"); raw != nil {
			checked, refusal := nbCheckContent(name, scFromPyd(raw))
			if refusal != nil {
				stop = refusal
				return scRefused
			}
			current, err := nbContentOf(ctx, tx, n, 0)
			if err != nil {
				return err
			}
			if !pyjson.Equal(checked, current) {
				if stop, err = nbNotBeingEdited(ctx, tx, n); err != nil || stop != nil {
					return scOr(err)
				}
				if base, ok := in.Get("base_version").(int64); ok && base != n.CurrentVersion {
					stop = &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("«%s» changed since you opened it (now "+
						"version %d): reload it, then make your change again", n.Name, n.CurrentVersion)}
					return scRefused
				}
				newContent = checked
			}
		}
		description := n.Description
		if v, ok := in.Get("description").(string); ok && pyd.Strip(v) != n.Description {
			description = pyd.Strip(v)
			changes = append(changes, pyjson.KV{Key: "description", Value: true})
		}
		maxVisitors := n.MaxVisitors
		if in.IsSet("max_visitors") {
			var v *int64
			if x, ok := in.Get("max_visitors").(int64); ok {
				v = &x
			}
			if !scSameInt(v, n.MaxVisitors) {
				if stop = nbMaxVisitors(v); stop != nil {
					return scRefused
				}
				changes = append(changes, pyjson.KV{Key: "max_visitors", Value: pyjson.Object{{Key: "from", Value: scIntOrNil(n.MaxVisitors)},
					{Key: "to", Value: scIntOrNil(v)}}})
				maxVisitors = v
			}
		}
		draft, draftBy, draftAt := n.Draft, n.DraftBy, n.DraftAt
		current := n.CurrentVersion
		if newContent != nil {
			v, err := nbAddVersion(ctx, tx, n.ID, newContent, "edited", p.User.ID)
			if err != nil {
				return err
			}
			current = v
			changes = append(changes, pyjson.KV{Key: "version", Value: v})
			draft, draftBy, draftAt = nil, nil, nil
		}
		if len(changes) == 0 {
			return errNbNothing
		}
		_, err = tx.Exec(ctx, `UPDATE notebooks SET name = $1, project_id = $2, environment_id = $3, description = $4, max_visitors = $5,
			current_version = $6, draft = $7, draft_by = $8, draft_at = $9, updated_at = $10 WHERE id = $11`,
			name, target, envID, description, maxVisitors, current, draft, draftBy, draftAt, time.Now().UTC(), n.ID)
		n.Name = name
		return err
	})
	if stop != nil {
		server.Refuse(w, stop)
		return
	}
	if err == errNbNothing {
		nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
		return
	}
	if err != nil {
		scFailed(d, w, "notebooks: changing the notebook", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.update", TargetType: scPtrStr("notebook"),
		TargetID: &n.ID, TargetLabel: &n.Name, Detail: changes})
	nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
}

var errNbNothing = fmt.Errorf("nothing changed")

func scSameInt(a, b *int64) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}

func scIntOrNil(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

// discardDraft is DELETE /notebooks/{notebook_id}/draft (discard_draft).
func discardDraft(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, n.ProjectID, perm.Edit) {
		return
	}
	if n.Draft == nil {
		nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
		return
	}
	if _, err := d.DB.Exec(ctx, `UPDATE notebooks SET draft = NULL, draft_by = NULL, draft_at = NULL WHERE id = $1`, n.ID); err != nil {
		scFailed(d, w, "notebooks: the draft", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.update", TargetType: scPtrStr("notebook"),
		TargetID: &n.ID, TargetLabel: &n.Name, Detail: pyjson.Object{{Key: "draft", Value: "discarded"}}})
	nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
}

// duplicateNotebook is POST /notebooks/{notebook_id}/duplicate (duplicate_notebook).
func duplicateNotebook(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id")
	if !ok || !nbNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	src, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, src.ProjectID, perm.Edit) {
		return
	}
	var id int64
	var name string
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if name, err = copynames.Free(ctx, tx, "notebooks", src.ProjectID, src.Name); err != nil {
			return err
		}
		content, err := nbContentOf(ctx, tx, src, 0)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		if err := tx.QueryRow(ctx, `INSERT INTO notebooks (name, description, project_id, owner_id, environment_id, current_version,
			created_at, updated_at, max_visitors) VALUES ($1, $2, $3, $4, $5, 0, $6, $6, $7) RETURNING id`,
			name, src.Description, src.ProjectID, p.User.ID, src.EnvironmentID, now, src.MaxVisitors).Scan(&id); err != nil {
			return err
		}
		v, err := nbAddVersion(ctx, tx, id, content, "created", p.User.ID)
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE notebooks SET current_version = $1 WHERE id = $2`, v, id)
		return err
	})
	if copynames.Clash(err) {
		scRefuse(w, http.StatusConflict, copynames.ClashDetail)
		return
	}
	if err != nil {
		scFailed(d, w, "notebooks: the copy", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.create", TargetType: scPtrStr("notebook"),
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "duplicate_of", Value: src.ID}}})
	nbSendDetail(d, w, r, p, id, http.StatusCreated)
}

// deleteNotebook is DELETE /notebooks/{notebook_id} (delete_notebook): its live sessions
// go now, their Jobs with the reaper's next round.
func deleteNotebook(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if scOpenLineageOn() {
		scForward(d, w, r) // notebook_runtime.forget ends the sessions' runs in the catalog
		return
	}
	ids, ok := scPathInts(w, r, "notebook_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, n.ProjectID, perm.Edit) {
		return
	}
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		for _, t := range []string{"notebook_visitors", "notebook_sessions", "notebook_tickets", "notebook_versions"} {
			if _, err := tx.Exec(ctx, fmt.Sprintf(`DELETE FROM %s WHERE notebook_id = $1`, t), n.ID); err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `DELETE FROM notebooks WHERE id = $1`, n.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "notebooks: deleting the notebook", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.delete", TargetType: scPtrStr("notebook"),
		TargetID: scPtrInt(ids[0]), TargetLabel: &n.Name})
	scNoContent(w)
}

// ── versions ──────────────────────────────────────────────────────────────────

func listNotebookVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id")
	if !ok {
		return
	}
	n, ok := nbVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	out, err := scVersionsOf(r.Context(), d.DB, "notebook_versions", "notebook_id", n.ID, n.CurrentVersion)
	if err != nil {
		scFailed(d, w, "notebooks: the versions", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

func getNotebookVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	n, ok := nbVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	exists, _, err := savedscripts.VersionRow(ctx, d.DB, "notebook_versions", "notebook_id", n.ID, n.CurrentVersion, ids[1])
	if err != nil {
		scFailed(d, w, "notebooks: the version", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Version not found")
		return
	}
	content, err := nbContentOf(ctx, d.DB, n, ids[1])
	if err != nil {
		scFailed(d, w, "notebooks: the version's content", err)
		return
	}
	server.JSON(w, http.StatusOK, content)
}

// restoreNotebookVersion is POST /notebooks/{notebook_id}/versions/{version}/restore: the
// app then runs with the rights of whoever restores it.
func restoreNotebookVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := scPathInts(w, r, "notebook_id", "version")
	if !ok || !nbNeedOn(w) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, n.ProjectID, perm.Edit) {
		return
	}
	version := ids[1]
	exists, _, err := savedscripts.VersionRow(ctx, d.DB, "notebook_versions", "notebook_id", n.ID, n.CurrentVersion, version)
	if err != nil {
		scFailed(d, w, "notebooks: the version", err)
		return
	}
	if !exists {
		scRefuse(w, http.StatusNotFound, "Version not found")
		return
	}
	if version == n.CurrentVersion {
		nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
		return
	}
	if refusal, err := nbNotBeingEdited(ctx, d.DB, n); scAnswer(d, w, "notebooks: the editor", refusal, err) {
		return
	}
	old, err := nbContentOf(ctx, d.DB, n, version)
	if err != nil {
		scFailed(d, w, "notebooks: the version's content", err)
		return
	}
	content, refusal := nbCheckContent(n.Name, old)
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	var newVersion int64
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		var err error
		if newVersion, err = nbAddVersion(ctx, tx, n.ID, content, fmt.Sprintf("restored from v%d", version), p.User.ID); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE notebooks SET current_version = $1, updated_at = $2 WHERE id = $3`, newVersion, time.Now().UTC(), n.ID)
		return err
	})
	if err != nil {
		scFailed(d, w, "notebooks: restoring the version", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.restore", TargetType: scPtrStr("notebook"),
		TargetID: &n.ID, TargetLabel: &n.Name, Detail: pyjson.Object{{Key: "from", Value: version}, {Key: "version", Value: newVersion}}})
	nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
}
