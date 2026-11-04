package api

// routes/notebook_sessions.py: a notebook's app or editor started and stopped, and the
// editor's «Save version» (services/notebook_runtime.py, notebook_runtime.go). While OpenLineage
// is on, starting and stopping stay the Python gateway's: a session is a run of the notebook's
// job in the lineage catalog.

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("notebook_sessions_runtime", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/notebooks/{notebook_id}/sessions",
				Handler: scWithBody(d, "/notebooks/{notebook_id}/sessions", startNotebookSession)},
			{Method: http.MethodDelete, Pattern: "/notebooks/{notebook_id}/sessions/{mode}",
				Handler: server.Authed(d.Auth, "/notebooks/{notebook_id}/sessions/{mode}", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
					stopNotebookSession(d, w, r, p)
				})},
			{Method: http.MethodPost, Pattern: "/notebooks/{notebook_id}/edit/save",
				Handler: scWithBody(d, "/notebooks/{notebook_id}/edit/save", saveFromEditor)},
		}
	})
}

// nbAnswer answers what the runtime said instead of a session: being edited (409 with the
// editor), an HTTPException, or an unexpected error.
func nbAnswer(d *Deps, w http.ResponseWriter, err error) {
	var be *nbBeingEdited
	var ref *auth.Refusal
	switch {
	case errors.As(err, &be):
		var editor any
		if be.editor != nil {
			editor = be.editor
		}
		server.JSON(w, http.StatusConflict, map[string]any{"detail": be.message, "editor": editor})
	case errors.As(err, &ref):
		server.Refuse(w, ref)
	default:
		fail(d, w, "notebooks: the session", err)
	}
}

// nbSessionJSON is _out.
func nbSessionJSON(d *Deps, r *http.Request, row *nbSession) (*nbSessionOut, error) {
	ctx := r.Context()
	user, err := nbPersonOf(ctx, d.DB, &row.UserID)
	if err != nil {
		return nil, err
	}
	out := &nbSessionOut{Mode: row.Mode, Status: row.Status, Error: row.Error, User: user,
		StartedAt: pyjson.Isoformat(row.StartedAt), LastSeenAt: pyjson.Isoformat(row.LastSeenAt)}
	if row.Mode == "app" {
		n, err := nbGet(ctx, d.DB, row.NotebookID, false)
		if err != nil {
			return nil, err
		}
		if n != nil {
			count, err := nbVisitorCount(ctx, d.DB, n.ID)
			if err != nil {
				return nil, err
			}
			capV := nbVisitorsCap(n.MaxVisitors)
			out.Visitors, out.MaxVisitors = &count, &capV
		}
	}
	return out, nil
}

// startNotebookSession is POST /notebooks/{notebook_id}/sessions (start_session): the app or
// the editor started, or the one running returned.
func startNotebookSession(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	if scOpenLineageOn() {
		scForward(d, w, r)
		return
	}
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "notebook_id")
	in := ps.Body(body, gatewaymodels.SessionStart)
	if !validated(w, &ps) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	mode := in.Get("mode").(string)
	if mode == "edit" {
		ref, err := c.Ensure(ctx, n.ProjectID, perm.Edit)
		if err != nil {
			fail(d, w, "notebooks: the permission", err)
			return
		}
		if ref != nil {
			server.Refuse(w, ref)
			return
		}
	}
	row, err := nbEnsureStarted(ctx, d, r, n, mode, &p.User)
	if err != nil {
		nbAnswer(d, w, err)
		return
	}
	out, err := nbSessionJSON(d, r, row)
	if err != nil {
		fail(d, w, "notebooks: the session", err)
		return
	}
	server.JSON(w, http.StatusAccepted, out)
}

// stopNotebookSession is DELETE /notebooks/{notebook_id}/sessions/{mode} (stop_session).
func stopNotebookSession(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if scOpenLineageOn() {
		scForward(d, w, r)
		return
	}
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "notebook_id")
	mode, _ := ps.Path("mode", r.PathValue("mode"), pyd.Literal("app", "edit")).(string)
	if !validated(w, &ps) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	row, err := nbRowOf(ctx, d.DB, n.ID, mode)
	if err != nil {
		fail(d, w, "notebooks: the session", err)
		return
	}
	if !(mode == "edit" && row != nil && row.UserID == p.User.ID) {
		ref, err := c.Ensure(ctx, n.ProjectID, perm.Edit)
		if err != nil {
			fail(d, w, "notebooks: the permission", err)
			return
		}
		if ref != nil {
			server.Refuse(w, ref)
			return
		}
	}
	if row != nil {
		if err := nbStop(ctx, d, row, &p.User, "stopped", false); err != nil {
			fail(d, w, "notebooks: stopping the session", err)
			return
		}
	}
	noContent(w)
}

// saveFromEditor is POST /notebooks/{notebook_id}/edit/save (save_from_editor): a new version
// from what marimo saved in the editor's pod, by whoever edits.
func saveFromEditor(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var ps pyd.Params
	ids, _ := pathIntsInto(&ps, r, "notebook_id")
	in := ps.Body(body, gatewaymodels.SaveIn)
	if !validated(w, &ps) {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	ref, err := c.Ensure(ctx, n.ProjectID, perm.Edit)
	if err != nil {
		fail(d, w, "notebooks: the permission", err)
		return
	}
	if ref != nil {
		server.Refuse(w, ref)
		return
	}
	// runtime.save_version
	row, err := nbRowOf(ctx, d.DB, n.ID, "edit")
	if err != nil {
		fail(d, w, "notebooks: the editor", err)
		return
	}
	if row == nil || row.UserID != p.User.ID || row.Status == "failed" {
		detail(w, http.StatusConflict, "You do not have the editor of this notebook open")
		return
	}
	if row.Status != "ready" {
		detail(w, http.StatusConflict, "The editor is still starting: try again in a moment")
		return
	}
	source, read := nbReadFile(ctx, d, row)
	if !read {
		detail(w, http.StatusBadGateway, "The editor's file could not be read from its pod: try again")
		return
	}
	content, refusal := nbCheckContent(n.Name, pyjson.Object{{Key: "source", Value: source}})
	if refusal != nil {
		server.Refuse(w, refusal)
		return
	}
	if err := nbTouch(ctx, d, row); err != nil {
		fail(d, w, "notebooks: the editor", err)
		return
	}
	current, err := nbContentOf(ctx, d.DB, n, 0)
	if err != nil {
		fail(d, w, "notebooks: the current version", err)
		return
	}
	if same, _ := pyjson.Dumps(current, pyjson.Options{SortKeys: true}); same == mustDumpsSorted(content) {
		if n.Draft != nil {
			if _, err := d.DB.Exec(ctx, `UPDATE notebooks SET draft = NULL, draft_by = NULL, draft_at = NULL WHERE id = $1`, n.ID); err != nil {
				fail(d, w, "notebooks: the draft", err)
				return
			}
		}
		nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
		return
	}
	note := ""
	if v, ok := in.Get("note").(string); ok {
		note = runeClip(strings.TrimSpace(v), 500)
	}
	if note == "" {
		note = "edited"
	}
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		fail(d, w, "notebooks: the version", err)
		return
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	version, err := nbAddVersion(ctx, tx, n.ID, content, note, p.User.ID)
	if err == nil {
		_, err = tx.Exec(ctx, `UPDATE notebooks SET current_version = $2, draft = NULL, draft_by = NULL, draft_at = NULL, updated_at = $3
			WHERE id = $1`, n.ID, version, time.Now().UTC())
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		fail(d, w, "notebooks: the version", err)
		return
	}
	kind := "notebook"
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "notebook.save", TargetType: &kind, TargetID: &n.ID,
		TargetLabel: &n.Name, Detail: pyjson.Object{{Key: "version", Value: version}}})
	nbSendDetail(d, w, r, p, n.ID, http.StatusOK)
}

func mustDumpsSorted(v any) string {
	s, _ := pyjson.Dumps(v, pyjson.Options{SortKeys: true})
	return s
}
