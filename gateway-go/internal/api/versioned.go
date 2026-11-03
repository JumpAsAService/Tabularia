package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// objectsAPI is what routes/reports.py and routes/dashboards.py share: an object of a folder
// with its versions, seen with VIEW on its folder (404 to whoever lacks it), changed with EDIT.
type objectsAPI struct {
	d        *Deps
	kind     *versioned.Kind
	pushmail versioned.Pushmail
	param    string // the path parameter of its id: "report_id"
	// openAudit is the audit of an opening in the viewer ("": none, as for a report)
	openAudit string
}

// ObjectOut is routes/reports.py ReportOut and routes/dashboards.py DashboardOut.
type ObjectOut struct {
	ID             int64  `json:"id"`
	Name           string `json:"name"`
	Description    string `json:"description"`
	ProjectID      int64  `json:"project_id"`
	CurrentVersion int64  `json:"current_version"`
	Datasets       int64  `json:"datasets"` // of the current version
	Parameters     int64  `json:"parameters"`
	Tiles          *int64 `json:"tiles,omitempty"` // a dashboard's
	CreatedAt      string `json:"created_at"`
	UpdatedAt      string `json:"updated_at"`
	Editable       bool   `json:"editable"` // EDIT on its folder
}

// ObjectDetail is ReportDetail and DashboardDetail: with the definition.
type ObjectDetail struct {
	ObjectOut
	Content json.RawMessage `json:"content"`
}

// VersionOut is routes/reports.py VersionOut.
type VersionOut struct {
	Version   int64   `json:"version"`
	Note      string  `json:"note"`
	CreatedAt string  `json:"created_at"`
	CreatedBy *string `json:"created_by"`
	Current   bool    `json:"current"`
}

func (h *objectsAPI) fail(w http.ResponseWriter, r *http.Request, err error) {
	h.d.Log.Error(h.kind.Table, "path", r.URL.Path, "err", err)
	server.InternalError(w)
}

// out is _out; `sizes` in the order of kind.Sizes.
func (h *objectsAPI) out(ctx context.Context, c *perm.Checker, it *versioned.Item, sizes []int64) (ObjectOut, error) {
	editable, err := c.Has(ctx, it.ProjectID, perm.Edit)
	o := ObjectOut{ID: it.ID, Name: it.Name, Description: it.Description, ProjectID: it.ProjectID,
		CurrentVersion: it.CurrentVersion, Datasets: sizes[0], Parameters: sizes[1],
		CreatedAt: pyjson.Isoformat(it.CreatedAt), UpdatedAt: pyjson.Isoformat(it.UpdatedAt), Editable: editable}
	if len(sizes) > 2 {
		o.Tiles = &sizes[2]
	}
	return o, err
}

// pyLen is len(content.get(key) or []) of a definition read back.
func pyLen(content pyjson.Object, key string) int64 {
	v, _ := content.Get(key)
	switch x := v.(type) {
	case []any:
		return int64(len(x))
	case pyjson.Object:
		return int64(len(x))
	case string:
		return int64(len([]rune(x)))
	}
	return 0
}

// contentJSON writes a definition as the answer carries it.
func contentJSON(content any) (json.RawMessage, error) {
	s, err := pyjson.Dumps(content, pyjson.Options{Compact: true})
	return json.RawMessage(s), err
}

// detail is _detail.
func (h *objectsAPI) detail(ctx context.Context, c *perm.Checker, it *versioned.Item) (*ObjectDetail, error) {
	read, err := h.kind.ContentOf(ctx, h.d.DB, it, nil)
	if err != nil {
		return nil, err
	}
	content, err := versioned.Object(read)
	if err != nil {
		return nil, err
	}
	sizes := make([]int64, len(h.kind.Sizes))
	for i, key := range h.kind.Sizes {
		sizes[i] = pyLen(content, key)
	}
	o, err := h.out(ctx, c, it, sizes)
	if err != nil {
		return nil, err
	}
	raw, err := contentJSON(content)
	if err != nil {
		return nil, err
	}
	return &ObjectDetail{ObjectOut: o, Content: raw}, nil
}

// many is _many.
func (h *objectsAPI) many(ctx context.Context, c *perm.Checker, found []*versioned.Item) ([]ObjectOut, error) {
	ids := make([]int64, len(found))
	for i, it := range found {
		ids[i] = it.ID
	}
	sizes, err := h.kind.SizesOf(ctx, h.d.DB, ids)
	if err != nil {
		return nil, err
	}
	out := []ObjectOut{}
	for _, it := range found {
		s, ok := sizes[it.ID]
		if !ok {
			s = make([]int64, len(h.kind.Sizes))
		}
		o, err := h.out(ctx, c, it, s)
		if err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, nil
}

// shownEmail is _shown_email: an observer-only account reads the emails masked.
func shownEmail(ctx context.Context, c *perm.Checker) (func(string) string, error) {
	r, err := c.Roles(ctx)
	if err != nil {
		return nil, err
	}
	if r.ObserverOnly() {
		return masking.Email, nil
	}
	return func(e string) string { return e }, nil
}

func projectExists(ctx context.Context, q perm.Querier, id int64) (bool, error) {
	var one int
	err := q.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, id).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

var folderNotFound = &auth.Refusal{Status: http.StatusNotFound, Detail: "Folder not found"}

// record is _record: the audit of an action on the object.
func (h *objectsAPI) record(ctx context.Context, p *auth.Principal, action string, id int64, label string, detail pyjson.Object) {
	audit.Record(ctx, h.d.DB, h.d.Log, audit.Event{Actor: &p.User, Action: action, TargetType: &h.kind.Label, TargetID: &id,
		TargetLabel: &label, Detail: detail})
}

// visible is the common start: the object of the path, seen by the person; nil when it answered.
func (h *objectsAPI) visible(w http.ResponseWriter, r *http.Request, c *perm.Checker, id int64) *versioned.Item {
	it, refusal, err := h.kind.GetVisible(r.Context(), h.d.DB, c, id)
	if err != nil {
		h.fail(w, r, err)
		return nil
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return nil
	}
	return it
}

// ensure is ensure_can; false when it answered.
func (h *objectsAPI) ensure(w http.ResponseWriter, r *http.Request, c *perm.Checker, project int64, cap perm.Capability) bool {
	refusal, err := c.Ensure(r.Context(), project, cap)
	if err != nil {
		h.fail(w, r, err)
		return false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return false
	}
	return true
}

// list is GET /reports (list_reports): every one this person sees.
func (h *objectsAPI) list(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	found, err := h.kind.Visible(ctx, h.d.DB, c, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	out, err := h.many(ctx, c, found)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// listProject is GET /projects/{project_id}/reports (list_project_reports).
func (h *objectsAPI) listProject(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	exists, err := projectExists(ctx, h.d.DB, ids[0])
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if !exists {
		server.Refuse(w, folderNotFound)
		return
	}
	if !h.ensure(w, r, c, ids[0], perm.View) {
		return
	}
	found, err := h.kind.Visible(ctx, h.d.DB, c, []int64{ids[0]})
	if err != nil {
		h.fail(w, r, err)
		return
	}
	out, err := h.many(ctx, c, found)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// get is GET /reports/{report_id} (get_report).
func (h *objectsAPI) get(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	c := perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
		return
	}
	out, err := h.detail(r.Context(), c, it)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// opened is POST /reports/{report_id}/views (report_opened, dashboard_opened): the viewer
// opened it; a call with an API token counts nothing, a script is not a reader.
func (h *objectsAPI) opened(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	c := perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
		return
	}
	if p.TokenScope == "" {
		if err := h.kind.RecordView(r.Context(), h.d.DB, h.d.Config.AppLocation, it.ID, p.User.ID); err != nil {
			h.fail(w, r, err)
			return
		}
		if h.openAudit != "" {
			h.record(r.Context(), p, h.openAudit, it.ID, it.Name, nil)
		}
	}
	noContent(w)
}

// noContent is FastAPI's answer of a 204 route: no body, and the JSON content type all the same.
func noContent(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNoContent)
}

// remove is DELETE /reports/{report_id} (delete_report).
func (h *objectsAPI) remove(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil || !h.ensure(w, r, c, it.ProjectID, perm.Edit) {
		return
	}
	// a flow group that sends it would fail at its Pushmail cell: it is taken out there first
	groups, err := h.pushmail.GroupsUsing(ctx, h.d.DB, it.ID)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	if len(groups) > 0 {
		names := make([]string, len(groups))
		for i, g := range groups {
			names[i] = g.Name
		}
		server.JSON(w, http.StatusConflict, map[string]any{"detail": usedBy{
			Message: fmt.Sprintf("The %s is sent by the flow groups %s: remove it from there first", h.kind.Label, quotedList(names)),
			Flows:   []string{}, Groups: names}})
		return
	}
	if err := h.kind.Delete(ctx, h.d.DB, it.ID); err != nil {
		h.fail(w, r, err)
		return
	}
	h.record(ctx, p, h.kind.Label+".delete", ids[0], it.Name, nil)
	noContent(w)
}

// usedBy is the 409 of an object still in use: what says it, and who uses it.
type usedBy struct {
	Message string   `json:"message"`
	Flows   []string `json:"flows"`
	Groups  []string `json:"groups"`
}

// quotedList is `", ".join(f"«{n}»" for n in names[:10]) + (" …" if len(names) > 10 else "")`.
func quotedList(names []string) string {
	shown := names
	if len(shown) > 10 {
		shown = shown[:10]
	}
	parts := make([]string, len(shown))
	for i, n := range shown {
		parts[i] = "«" + n + "»"
	}
	s := strings.Join(parts, ", ")
	if len(names) > 10 {
		s += " …"
	}
	return s
}

func uniqueViolation(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "23505"
}

// duplicate is POST /reports/{report_id}/duplicate (duplicate_report): a copy in its folder, as
// «name (i)», from the current version.
func (h *objectsAPI) duplicate(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	src := h.visible(w, r, c, ids[0])
	if src == nil || !h.ensure(w, r, c, src.ProjectID, perm.Edit) {
		return
	}
	name, err := copynames.Free(ctx, h.d.DB, h.kind.Table, src.ProjectID, src.Name)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	content, err := h.kind.ContentOf(ctx, h.d.DB, src, nil)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	tx, err := h.d.DB.Begin(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	defer tx.Rollback(ctx)
	cp := &versioned.Item{}
	if err := tx.QueryRow(ctx, `INSERT INTO `+h.kind.Table+` (name, description, project_id, owner_id, current_version,
			created_at, updated_at, views_total) VALUES ($1, $2, $3, $4, 0, $5, $5, 0) RETURNING id`,
		name, src.Description, src.ProjectID, p.User.ID, time.Now().UTC()).Scan(&cp.ID); err != nil {
		if uniqueViolation(err) { // commit_copy: another copy took that name
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "Another copy took that name at the same moment: try again"})
			return
		}
		h.fail(w, r, err)
		return
	}
	if _, err := h.kind.AddVersion(ctx, tx, cp, content, "created", p.User.ID); err != nil {
		h.fail(w, r, err)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		h.fail(w, r, err)
		return
	}
	if cp, err = h.kind.Get(ctx, h.d.DB, cp.ID); err != nil || cp == nil {
		h.fail(w, r, errors.Join(err, errors.New("the copy is gone")))
		return
	}
	h.record(ctx, p, h.kind.Label+".duplicate", cp.ID, cp.Name,
		pyjson.Object{{Key: "project_id", Value: cp.ProjectID}, {Key: "duplicate_of", Value: src.ID}})
	out, err := h.detail(ctx, c, cp)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusCreated, out)
}

// versions is GET /reports/{report_id}/versions (list_versions): every version, the newest
// first, with its author's email as this person reads it.
func (h *objectsAPI) versions(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param)
	if !ok {
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
		return
	}
	out, err := h.listVersions(ctx, c, it)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

func (h *objectsAPI) listVersions(ctx context.Context, c *perm.Checker, it *versioned.Item) ([]VersionOut, error) {
	rows, err := h.d.DB.Query(ctx, `SELECT version, note, created_at, created_by FROM `+h.kind.Versions+` WHERE `+h.kind.FK+` = $1
		ORDER BY version DESC`, it.ID)
	if err != nil {
		return nil, err
	}
	var found []versioned.Version
	for rows.Next() {
		var x versioned.Version
		if err := rows.Scan(&x.Version, &x.Note, &x.CreatedAt, &x.CreatedBy); err != nil {
			rows.Close()
			return nil, err
		}
		found = append(found, x)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	authors := map[int64]string{}
	var who []int64
	for _, x := range found {
		if x.CreatedBy != nil && *x.CreatedBy != 0 {
			who = append(who, *x.CreatedBy)
		}
	}
	if len(found) > 0 {
		shown, err := shownEmail(ctx, c)
		if err != nil {
			return nil, err
		}
		urows, err := h.d.DB.Query(ctx, `SELECT id, email FROM users WHERE id = ANY($1)`, who)
		if err != nil {
			return nil, err
		}
		for urows.Next() {
			var uid int64
			var email string
			if err := urows.Scan(&uid, &email); err != nil {
				urows.Close()
				return nil, err
			}
			authors[uid] = shown(email)
		}
		urows.Close()
		if err := urows.Err(); err != nil {
			return nil, err
		}
	}
	out := []VersionOut{}
	for _, x := range found {
		var by *string
		if x.CreatedBy != nil {
			if a, ok := authors[*x.CreatedBy]; ok {
				by = &a
			}
		}
		out = append(out, VersionOut{Version: x.Version, Note: x.Note, CreatedAt: pyjson.Isoformat(x.CreatedAt),
			CreatedBy: by, Current: x.Version == it.CurrentVersion})
	}
	return out, nil
}

// version is GET /reports/{report_id}/versions/{version} (get_version): its definition.
func (h *objectsAPI) version(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, h.param, "version")
	if !ok {
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	it := h.visible(w, r, c, ids[0])
	if it == nil {
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
	// response_model=dict: anything else fails the answer's validation, a 500
	content, err := versioned.Object(h.kind.Parsed(row))
	if err != nil {
		h.fail(w, r, err)
		return
	}
	raw, err := contentJSON(content)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	server.JSON(w, http.StatusOK, raw)
}
