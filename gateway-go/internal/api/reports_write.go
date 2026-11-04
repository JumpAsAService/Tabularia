package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// bodyHandler is a route with a body (read by withBody before the authentication): the body
// and its bytes.
type bodyHandler func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte)

// bodyRoute is a route with a body: withBody, then the authentication, then the handler.
func bodyRoute(a *auth.Authenticator, method, pattern string, h bodyHandler) server.Route {
	return server.Route{Method: method, Pattern: pattern, Handler: withBody(server.Authed(a, pattern,
		func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(w, r, p, bodyOf(r), rawOf(r)) }))}
}

// toPython is a value pyd validated, as the Python code holds it (pyjson's values).
func toPython(v any) any {
	switch x := v.(type) {
	case *pyd.Object:
		out := make(pyjson.Object, 0, x.Len())
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			out = append(out, pyjson.KV{Key: k, Value: toPython(e)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = toPython(e)
		}
		return out
	case *big.Int:
		if x.IsInt64() {
			return x.Int64()
		}
		return pyjson.Int(x.String())
	}
	return v
}

// idOf is an int of a body as an id the database looks up: one past the columns finds no row.
func idOf(v any) int64 {
	switch x := v.(type) {
	case int64:
		if x < math.MinInt32 || x > math.MaxInt32 {
			return NoSuchID
		}
		return x
	}
	return NoSuchID
}

// toUpstream hands the request to the Python gateway as it came: what it reads needs sqlglot.
func (h *objectsAPI) toUpstream(w http.ResponseWriter, r *http.Request, raw []byte) {
	r2 := r.Clone(r.Context())
	r2.Body = io.NopCloser(bytes.NewReader(raw))
	r2.ContentLength = int64(len(raw))
	h.d.Upstream.ServeHTTP(w, r2)
}

// answerErr writes what an error of the services means: a refusal, a request for Python, a 500.
func (h *objectsAPI) answerErr(w http.ResponseWriter, r *http.Request, raw []byte, err error) {
	var ref *reports.Refusal
	var u *reports.Unreadable
	switch {
	case errors.Is(err, reports.ErrNeedsPython):
		h.toUpstream(w, r, raw)
	case errors.As(err, &ref):
		server.JSON(w, ref.Status, map[string]any{"detail": ref.Detail})
	case errors.As(err, &u):
		server.JSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": u.Message})
	default:
		h.fail(w, r, err)
	}
}

func (h *objectsAPI) reviewer(c *perm.Checker) *reports.Reviewer {
	return &reports.Reviewer{Q: h.d.DB, C: c, Engine: h.d.Engine, Log: h.d.Log}
}

// name is _name: the name stripped, not empty, free in the folder.
func (h *objectsAPI) name(ctx context.Context, project int64, name string, exclude int64) (string, error) {
	name = pystr.Strip(name)
	if name == "" {
		return "", &reports.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("The %s needs a name", h.kind.Label)}
	}
	var clash int64
	err := h.d.DB.QueryRow(ctx, `SELECT id FROM `+h.kind.Table+` WHERE project_id = $1 AND name = $2 LIMIT 1`, project, name).Scan(&clash)
	if err == nil && clash != exclude {
		return "", h.taken(name)
	}
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return "", err
	}
	return name, nil
}

func (h *objectsAPI) taken(name string) error {
	return &reports.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a %s '%s' in this folder", h.kind.Label, name)}
}

// checked is the kind's checked(): a report's, or a dashboard's.
func (h *objectsAPI) checked(ctx context.Context, c *perm.Checker, definition, current any) (pyjson.Object, error) {
	if h.check != nil {
		return h.check(ctx, h, c, definition, current)
	}
	return h.reviewer(c).Checked(ctx, definition, current)
}

// create is POST /projects/{project_id}/reports (create_report).
func (h *objectsAPI) create(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	pid := params.Path("project_id", r.PathValue("project_id"), pyd.Int())
	b := params.Body(body, h.createModel)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	project := idOf(pid)
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	exists, err := projectExists(ctx, h.d.DB, project)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if !exists {
		server.Refuse(w, folderNotFound)
		return
	}
	if !h.ensure(w, r, c, project, perm.Edit) {
		return
	}
	name, err := h.name(ctx, project, b.Get("name").(string), 0)
	if err != nil {
		h.answerErr(w, r, raw, err)
		return
	}
	var content any
	if cv := b.Get("content"); cv != nil {
		content = toPython(cv)
	} else {
		locale := "en"
		if p.User.Locale != nil && *p.User.Locale != "" {
			locale = *p.User.Locale
		}
		content = h.empty(locale, h.d.Config.AppTimezone)
	}
	checked, err := h.checked(ctx, c, content, nil)
	if err != nil {
		h.answerErr(w, r, raw, err)
		return
	}
	tx, err := h.d.DB.Begin(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	defer tx.Rollback(ctx)
	it := &versioned.Item{}
	if err := tx.QueryRow(ctx, `INSERT INTO `+h.kind.Table+` (name, description, project_id, owner_id, current_version,
			created_at, updated_at, views_total) VALUES ($1, $2, $3, $4, 0, $5, $5, 0) RETURNING id`,
		name, pystr.Strip(b.Get("description").(string)), project, p.User.ID, time.Now().UTC()).Scan(&it.ID); err != nil {
		if uniqueViolation(err) {
			h.answerErr(w, r, raw, h.taken(name))
			return
		}
		h.fail(w, r, err)
		return
	}
	if _, err := h.kind.AddVersion(ctx, tx, it, checked, "created", p.User.ID); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		if uniqueViolation(err) {
			h.answerErr(w, r, raw, h.taken(name))
			return
		}
		h.fail(w, r, err)
		return
	}
	h.record(ctx, p, h.kind.Label+".create", it.ID, name, pyjson.Object{{Key: "project_id", Value: project}})
	h.answerDetail(w, r, c, it.ID, http.StatusCreated)
}

func (h *objectsAPI) answerDetail(w http.ResponseWriter, r *http.Request, c *perm.Checker, id int64, status int) {
	it, err := h.kind.Get(r.Context(), h.d.DB, id)
	if err != nil || it == nil {
		h.fail(w, r, errors.Join(err, errors.New("gone")))
		return
	}
	out, err := h.detail(r.Context(), c, it)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, status, out)
}

// groupOff is a flow group whose schedule a change switched off, recorded once committed.
type groupOff struct {
	id     int64
	name   string
	detail pyjson.Object
}

// groupsOff is _groups_off (dashboards.groups_off): the flow groups that send the object,
// scheduled by someone else, switched off in `tx` when a change makes it read more.
func (h *objectsAPI) groupsOff(ctx context.Context, tx pgx.Tx, userID int64, it *versioned.Item, before, after any) ([]groupOff, error) {
	reasons := reports.Widens(before, after)
	defaults := reports.NewDefaults(before, after)
	if len(reasons) == 0 && len(defaults) == 0 {
		return nil, nil
	}
	groups, err := h.pushmail.GroupsUsing(ctx, tx, it.ID)
	if err != nil {
		return nil, err
	}
	var off []groupOff
	for _, g := range groups {
		given := h.pushmail.GivenBy(g, it.ID)
		mine := append([]string{}, reasons...)
		for _, name := range defaults {
			if !given[name] {
				mine = append(mine, fmt.Sprintf("the default of «%s» changed", name))
			}
		}
		if len(mine) == 0 {
			continue
		}
		reason := fmt.Sprintf("the %s '%s' it sends changed: %s", h.kind.Label, it.Name, joinSemi(mine))
		if g.RunSchedule == nil || *g.RunSchedule == "" || g.RunScheduledBy == nil || *g.RunScheduledBy == userID {
			continue
		}
		if _, err := tx.Exec(ctx, `UPDATE flow_groups SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL WHERE id = $1`, g.ID); err != nil {
			return nil, err
		}
		off = append(off, groupOff{g.ID, g.Name, pyjson.Object{{Key: "turned_off", Value: true}, {Key: "author", Value: *g.RunScheduledBy},
			{Key: "reason", Value: reason}}})
	}
	return off, nil
}

func joinSemi(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += "; "
		}
		out += p
	}
	return out
}

// recordGroupsOff is record_groups_off.
func (h *objectsAPI) recordGroupsOff(ctx context.Context, p *auth.Principal, off []groupOff) {
	t := "flow_group"
	for _, g := range off {
		id, name := g.id, g.name
		audit.Record(ctx, h.d.DB, h.d.Log, audit.Event{Actor: &p.User, Action: "flow_group.schedule", TargetType: &t, TargetID: &id,
			TargetLabel: &name, Detail: g.detail})
	}
}

// update is PATCH /reports/{report_id} (update_report): name, description, folder, definition;
// a change of the definition is a new version.
func (h *objectsAPI) update(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	rid := params.Path(h.param, r.PathValue(h.param), pyd.Int())
	b := params.Body(body, h.updateModel)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, idOf(rid))
	if it == nil || !h.ensure(w, r, c, it.ProjectID, perm.Edit) {
		return
	}
	target := it.ProjectID
	if pv := b.Get("project_id"); pv != nil {
		if bp := idOf(pv); bp != it.ProjectID || !isInt64(pv) {
			exists, err := projectExists(ctx, h.d.DB, bp)
			if err != nil {
				h.fail(w, r, err)
				return
			}
			if !exists {
				server.Refuse(w, &auth.Refusal{Status: http.StatusNotFound, Detail: "Destination folder not found"})
				return
			}
			if !h.ensure(w, r, c, bp, perm.Edit) {
				return
			}
			target = bp
		}
	}
	wanted := it.Name
	if nv, ok := b.Get("name").(string); ok {
		wanted = nv
	}
	name, err := h.name(ctx, target, wanted, it.ID)
	if err != nil {
		h.answerErr(w, r, raw, err)
		return
	}
	var actions []struct {
		action string
		detail pyjson.Object
	}
	add := func(action string, detail pyjson.Object) {
		actions = append(actions, struct {
			action string
			detail pyjson.Object
		}{action, detail})
	}
	if name != it.Name {
		add(h.kind.Label+".rename", pyjson.Object{{Key: "from", Value: it.Name}, {Key: "to", Value: name}})
	}
	if target != it.ProjectID {
		add(h.kind.Label+".move", pyjson.Object{{Key: "from", Value: it.ProjectID}, {Key: "to", Value: target}})
	}
	changes := pyjson.Object{}
	tx, err := h.d.DB.Begin(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	defer tx.Rollback(ctx)
	var off []groupOff
	if cv := b.Get("content"); cv != nil {
		current, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		content, err := h.checked(ctx, c, toPython(cv), current)
		if err != nil {
			h.answerErr(w, r, raw, err)
			return
		}
		// read again and held until the commit: a save that landed meanwhile is the current one
		if _, err := tx.Exec(ctx, `SELECT id FROM `+h.kind.Table+` WHERE id = $1 FOR UPDATE`, it.ID); err != nil {
			h.fail(w, r, err)
			return
		}
		fresh, err := h.kind.Get(ctx, tx, it.ID)
		if err != nil || fresh == nil {
			h.fail(w, r, errors.Join(err, errors.New("gone")))
			return
		}
		it.CurrentVersion = fresh.CurrentVersion
		now, err := h.kind.ContentOf(ctx, tx, it, nil)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		if !pystr.Eq(content, now) {
			if bv := b.Get("base_version"); bv != nil && !pystr.Eq(toPython(bv), it.CurrentVersion) {
				server.JSON(w, http.StatusConflict, map[string]any{"detail": fmt.Sprintf(
					"«%s» changed since you opened it (now version %d): reload it, then make your change again", it.Name, it.CurrentVersion)})
				return
			}
			v, err := h.kind.AddVersion(ctx, tx, it, content, "edited", p.User.ID)
			if err != nil {
				h.fail(w, r, err)
				return
			}
			changes = changes.Set("version", v)
			if off, err = h.groupsOff(ctx, tx, p.User.ID, it, now, content); err != nil {
				h.fail(w, r, err)
				return
			}
		}
	}
	description := it.Description
	if dv, ok := b.Get("description").(string); ok && pystr.Strip(dv) != it.Description {
		description = pystr.Strip(dv)
		changes = changes.Set("description", true)
	}
	if len(changes) > 0 {
		add(h.kind.Label+".update", changes)
	}
	if len(actions) == 0 {
		tx.Rollback(ctx)
		h.answerDetail(w, r, c, it.ID, http.StatusOK)
		return
	}
	if _, err := tx.Exec(ctx, `UPDATE `+h.kind.Table+` SET name = $2, project_id = $3, description = $4, updated_at = $5 WHERE id = $1`,
		it.ID, name, target, description, time.Now().UTC()); err != nil {
		if uniqueViolation(err) {
			h.answerErr(w, r, raw, h.taken(name))
			return
		}
		h.fail(w, r, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		if uniqueViolation(err) {
			h.answerErr(w, r, raw, h.taken(name))
			return
		}
		h.fail(w, r, err)
		return
	}
	for _, a := range actions {
		h.record(ctx, p, a.action, it.ID, name, a.detail)
	}
	h.recordGroupsOff(ctx, p, off)
	h.answerDetail(w, r, c, it.ID, http.StatusOK)
}

func isInt64(v any) bool {
	_, ok := v.(int64)
	return ok
}

// restore is POST /reports/{report_id}/versions/{version}/restore (restore_version): an older
// version becomes the newest, and the current one.
func (h *objectsAPI) restore(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param, "version")
	if !ok {
		return
	}
	ids = inColumn(ids)
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil || !h.ensure(w, r, c, it.ProjectID, perm.Edit) {
		return
	}
	row, err := h.kind.VersionRow(ctx, h.d.DB, it, &ids[1])
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if row == nil {
		server.Refuse(w, &auth.Refusal{Status: http.StatusNotFound, Detail: "Version not found"})
		return
	}
	if ids[1] == it.CurrentVersion {
		h.answerDetail(w, r, c, it.ID, http.StatusOK)
		return
	}
	current, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	content, err := h.checked(ctx, c, h.kind.Parsed(row), current)
	if err != nil {
		h.answerErr(w, r, nil, err)
		return
	}
	tx, err := h.d.DB.Begin(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	defer tx.Rollback(ctx)
	v, err := h.kind.AddVersion(ctx, tx, it, content, fmt.Sprintf("restored from v%d", ids[1]), p.User.ID)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	off, err := h.groupsOff(ctx, tx, p.User.ID, it, current, content)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if _, err := tx.Exec(ctx, `UPDATE `+h.kind.Table+` SET updated_at = $2 WHERE id = $1`, it.ID, time.Now().UTC()); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		h.fail(w, r, err)
		return
	}
	h.record(ctx, p, h.kind.Label+".restore", it.ID, it.Name, pyjson.Object{{Key: "from", Value: ids[1]}, {Key: "version", Value: v}})
	h.recordGroupsOff(ctx, p, off)
	h.answerDetail(w, r, c, it.ID, http.StatusOK)
}

// beingEdited is _being_edited: a definition being edited is of a saved object, or of a new one
// in a folder: EDIT on that folder. The saved object, or nil; false when it answered.
func (h *objectsAPI) beingEdited(w http.ResponseWriter, r *http.Request, c *perm.Checker, id, project any) (*versioned.Item, bool) {
	var it *versioned.Item
	if id != nil {
		if it = h.visible(w, r, c, idOf(id)); it == nil {
			return nil, false
		}
	}
	var folder int64
	switch {
	case it != nil:
		folder = it.ProjectID
	case project != nil:
		folder = idOf(project)
	default:
		server.Refuse(w, folderNotFound)
		return nil, false
	}
	exists, err := projectExists(r.Context(), h.d.DB, folder)
	if err != nil {
		h.fail(w, r, err)
		return nil, false
	}
	if !exists {
		server.Refuse(w, folderNotFound)
		return nil, false
	}
	if !h.ensure(w, r, c, folder, perm.Edit) {
		return nil, false
	}
	return it, true
}

// checkDefinition is POST /reports/check (check_report): the problems of a definition being
// edited, as a save would say them, without saving it.
func (h *objectsAPI) checkDefinition(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
	var params pyd.Params
	b := params.Body(body, h.checkModel)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it, ok := h.beingEdited(w, r, c, b.Get(h.param), b.Get("project_id"))
	if !ok {
		return
	}
	var current any
	if it != nil {
		var err error
		if current, err = h.kind.ContentOf(ctx, h.d.DB, it, nil); err != nil {
			h.fail(w, r, err)
			return
		}
	}
	problems, err := h.problems(ctx, c, toPython(b.Get("definition")), current)
	if err != nil {
		h.answerErr(w, r, raw, err)
		return
	}
	if problems == nil {
		problems = []pyjson.Object{}
	}
	server.JSON(w, http.StatusOK, map[string]any{"problems": problems})
}

// problems is the kind's review().problems.
func (h *objectsAPI) problems(ctx context.Context, c *perm.Checker, definition, current any) ([]pyjson.Object, error) {
	if h.review != nil {
		return h.review(ctx, h, c, definition, current)
	}
	rv, err := h.reviewer(c).Review(ctx, definition, current)
	if err != nil {
		return nil, err
	}
	return rv.Problems, nil
}
