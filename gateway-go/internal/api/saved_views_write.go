package api

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

func init() {
	register("saved_views_write", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPatch, Pattern: "/saved-views/{view_id}",
				Handler: withBody(server.Authed(d.Auth, "/saved-views/{view_id}", updateSavedView(d)))},
			{Method: http.MethodDelete, Pattern: "/saved-views/{view_id}",
				Handler: server.Authed(d.Auth, "/saved-views/{view_id}", deleteSavedView(d))},
		}
	})
}

// readingObject is a report or a dashboard whose current version reads an object.
type readingObject struct {
	id, projectID int64
	name          string
}

// pointsTo is services/reports.py _POINTS_TO.
var pointsTo = map[string][2]string{"sql_script": {"sql_script", "script_id"}, "view": {"view", "view_id"},
	"datasource": {"datasource", "datasource_id"}, "connection": {"sql", "connection_id"}}

// objectsReading is reports_reading / dashboards_reading: the objects one of whose datasets
// (in the current version) names the object.
func objectsReading(ctx context.Context, q perm.Querier, k *versioned.Kind, kind string, objectID int64) ([]readingObject, error) {
	sourceKind, key := pointsTo[kind][0], pointsTo[kind][1]
	rows, err := q.Query(ctx, `SELECT o.id, o.project_id, o.name, v.content FROM `+k.Table+` o JOIN `+k.Versions+` v
		ON o.id = v.`+k.FK+` AND o.current_version = v.version WHERE v.content LIKE '%' || $1 || '%'`, `"`+key+`"`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []readingObject
	for rows.Next() {
		var o readingObject
		var content string
		if err := rows.Scan(&o.id, &o.projectID, &o.name, &content); err != nil {
			return nil, err
		}
		parsed, err := pyd.LoadsStr(content)
		if err != nil {
			continue // _parsed: empty(), which reads nothing
		}
		def, ok := parsed.(*pyd.Object)
		if !ok {
			return nil, errNotDict
		}
		datasets, _ := def.Get("datasets")
		if !pyd.Truthy(datasets) {
			continue
		}
		list, ok := datasets.([]any)
		if !ok {
			return nil, errNotDict
		}
		for _, e := range list {
			ds, ok := e.(*pyd.Object)
			if !ok {
				continue
			}
			src, _ := ds.Get("source")
			if !pyd.Truthy(src) {
				continue
			}
			so, ok := src.(*pyd.Object)
			if !ok {
				return nil, errNotDict
			}
			sk, _ := so.Get("kind")
			sid, _ := so.Get(key)
			if sk == sourceKind && pyEqual(sid, objectID) {
				out = append(out, o)
				break
			}
		}
	}
	return out, rows.Err()
}

// notReadBy is not_read_by_reports / not_read_by_dashboards: the 409 that names the ones
// this person sees and counts the others.
func notReadBy(ctx context.Context, c *perm.Checker, found []readingObject, what, label string, dashboard bool) (*pyjson.Object, error) {
	if len(found) == 0 {
		return nil, nil
	}
	var seen []string
	for _, o := range found {
		ok, err := c.Has(ctx, o.projectID, perm.View) // can_see: administrators see every one
		if err != nil {
			return nil, err
		}
		if ok {
			seen = append(seen, o.name)
		}
	}
	sort.Strings(seen)
	others := len(found) - len(seen)
	quoted := make([]string, 0, 10)
	for i, n := range seen {
		if i == 10 {
			break
		}
		quoted = append(quoted, "«"+n+"»")
	}
	who := strings.Join(quoted, ", ")
	if len(seen) > 10 {
		who += " …"
	}
	plural := label + "s"
	switch {
	case len(seen) == 0:
		word := plural
		if others == 1 {
			word = label
		}
		who = fmt.Sprintf("%d %s you cannot see", others, word)
	case others > 0:
		who = fmt.Sprintf("the %s %s and %d you cannot see", plural, who, others)
	default:
		who = fmt.Sprintf("the %s %s", plural, who)
	}
	names := make([]any, len(seen))
	for i, n := range seen {
		names[i] = n
	}
	detail := pyjson.Object{{Key: "message", Value: fmt.Sprintf("%s is read by %s: change them first", what, who)},
		{Key: "flows", Value: []any{}}, {Key: "groups", Value: []any{}}}
	if dashboard {
		detail = append(detail, pyjson.KV{Key: "reports", Value: []any{}}, pyjson.KV{Key: "dashboards", Value: names})
	} else {
		detail = append(detail, pyjson.KV{Key: "reports", Value: names})
	}
	return &detail, nil
}

// refuseReadBy answers the 409 of notReadBy (true when answered).
func refuseReadBy(d *Deps, w http.ResponseWriter, ctx context.Context, c *perm.Checker, kind string, id int64, what string) bool {
	for _, k := range []struct {
		kind      *versioned.Kind
		label     string
		dashboard bool
	}{{reports.Kind, "report", false}, {dashboards.Kind, "dashboard", true}} {
		found, err := objectsReading(ctx, d.DB, k.kind, kind, id)
		if err != nil {
			fail500(d, w, "references", err)
			return true
		}
		detail, err := notReadBy(ctx, c, found, what, k.label, k.dashboard)
		if err != nil {
			fail500(d, w, "references", err)
			return true
		}
		if detail != nil {
			writePyStatus(w, http.StatusConflict, pyjson.Object{{Key: "detail", Value: *detail}})
			return true
		}
	}
	return false
}

// deleteSavedView is DELETE /saved-views/{view_id}: refused while reports or dashboards read it.
func deleteSavedView(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		v, done := viewOf(d, w, r)
		if done {
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		if ref, err := c.Ensure(ctx, v.ProjectID, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		if refuseReadBy(d, w, ctx, c, "view", v.ID, "The saved view") {
			return
		}
		if _, err := d.DB.Exec(ctx, `DELETE FROM saved_views WHERE id = $1`, v.ID); err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		kind := "saved_view"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "saved_view.delete", TargetType: &kind, TargetID: &v.ID,
			TargetLabel: &v.Name})
		returnsNone(w)
	}
}

// viewReads is _groups_off's read(): what a report reads through the view, (operations,
// engine), or nothing when the datasource is gone.
func viewReads(spec string, ds *reports.Datasource) any {
	if ds == nil {
		return []any{}
	}
	if spec == "" {
		spec = "{}"
	}
	value, err := pyjson.Loads(spec)
	o, ok := value.(pyjson.Object)
	if err != nil || !ok {
		o = pyjson.Object{}
	}
	engine, _ := o.Get("engine")
	return []any{reports.ViewOperations(o, ds), engine}
}

// updateSavedView is PATCH /saved-views/{view_id}: rename, describe, reconfigure, move; a
// change of what a report reads through it switches off the schedules others set.
func updateSavedView(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		vid := ps.Path("view_id", r.PathValue("view_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.SavedViewUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		id, ok := rowID(vid)
		var v SavedViewOut
		var err error
		if ok {
			v, err = scanSavedView(d.DB.QueryRow(ctx, `SELECT `+savedViewColumns+` FROM saved_views WHERE id = $1`, id))
		}
		if !ok || err == pgx.ErrNoRows {
			server.Refuse(w, notFound("Saved view not found"))
			return
		}
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		c := perm.For(d.DB, &p.User)
		if ref, err := c.Ensure(ctx, v.ProjectID, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		target := v.ProjectID
		if pv := body.Get("project_id"); pv != nil {
			dest, ok := rowID(pv)
			if !ok || dest != v.ProjectID {
				found := false
				if ok {
					if found, err = exists(ctx, d.DB, "projects", dest); err != nil {
						fail500(d, w, "saved views", err)
						return
					}
				}
				if !found {
					server.Refuse(w, notFound("Destination folder not found"))
					return
				}
				if ref, err := c.Ensure(ctx, dest, perm.Edit); refuse(d, w, ref, err) {
					return
				}
				target = dest
			}
		}
		name := v.Name
		if s := pyStr(body.Get("name")); s != nil {
			name = pyd.Strip(*s)
		}
		if name == "" {
			server.Refuse(w, &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The name cannot be empty"})
			return
		}
		taken, err := savedViewTaken(ctx, d, target, name, &v.ID)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if taken {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a saved view '%s' in this folder", name)})
			return
		}
		description := v.Description
		if s := pyStr(body.Get("description")); s != nil {
			description = *s
		}
		spec := v.Spec
		var off []groupOff
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		defer tx.Rollback(ctx)
		if s := pyStr(body.Get("spec")); s != nil {
			checked, ref := validSpec(*s)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			if off, err = viewGroupsOff(ctx, tx, p.User.ID, v.ID, name, v.DatasourceID, v.Spec, checked); err != nil {
				fail500(d, w, "saved views", err)
				return
			}
			spec = checked
		}
		if _, err := tx.Exec(ctx, `UPDATE saved_views SET name = $1, project_id = $2, description = $3, spec = $4, updated_at = $5 WHERE id = $6`,
			name, target, description, spec, time.Now().UTC(), v.ID); err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		if err := tx.Commit(ctx); err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		kind := "saved_view"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "saved_view.update", TargetType: &kind, TargetID: &v.ID,
			TargetLabel: &name})
		t := "flow_group"
		for _, g := range off {
			gid, gname := g.id, g.name
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "flow_group.schedule", TargetType: &t, TargetID: &gid,
				TargetLabel: &gname, Detail: g.detail})
		}
		out, err := savedViewOut(ctx, d, v.ID)
		if err != nil {
			fail500(d, w, "saved views", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// viewGroupsOff is routes/saved_views.py _groups_off with reports.turn_off_groups: the flow
// groups sending a report or a dashboard that reads the view, scheduled by someone else.
func viewGroupsOff(ctx context.Context, tx pgx.Tx, userID, viewID int64, viewName string, dsID int64, before, after string) ([]groupOff, error) {
	ds, err := reports.GetDatasource(ctx, tx, dsID)
	if err != nil {
		return nil, err
	}
	if pyjsonEqual(viewReads(before, ds), viewReads(after, ds)) {
		return nil, nil
	}
	var off []groupOff
	done := map[int64]bool{}
	for _, k := range []struct {
		kind  *versioned.Kind
		pm    versioned.Pushmail
		label string
	}{{reports.Kind, reports.Pushmail, "report"}, {dashboards.Kind, dashboards.Pushmail, "dashboard"}} {
		found, err := objectsReading(ctx, tx, k.kind, "view", viewID)
		if err != nil {
			return nil, err
		}
		for _, o := range found {
			groups, err := k.pm.GroupsUsing(ctx, tx, o.id)
			if err != nil {
				return nil, err
			}
			for _, g := range groups {
				if done[g.ID] || g.RunSchedule == nil || *g.RunSchedule == "" || g.RunScheduledBy == nil || *g.RunScheduledBy == userID {
					continue
				}
				done[g.ID] = true
				reason := fmt.Sprintf("the saved view '%s' its %s '%s' reads changed: it reads something else", viewName, k.label, o.name)
				if _, err := tx.Exec(ctx, `UPDATE flow_groups SET run_schedule = NULL, run_scheduled_by = NULL, next_run_at = NULL WHERE id = $1`, g.ID); err != nil {
					return nil, err
				}
				off = append(off, groupOff{g.ID, g.Name, pyjson.Object{{Key: "turned_off", Value: true}, {Key: "author", Value: *g.RunScheduledBy},
					{Key: "reason", Value: reason}}})
			}
		}
	}
	return off, nil
}

// pyjsonEqual is Python's == on pyjson values (dicts regardless of order, 1 == 1.0 == True).
func pyjsonEqual(a, b any) bool {
	switch x := a.(type) {
	case pyjson.Object:
		y, ok := b.(pyjson.Object)
		if !ok || len(x) != len(y) {
			return false
		}
		for _, kv := range x {
			o, in := y.Get(kv.Key)
			if !in || !pyjsonEqual(kv.Value, o) {
				return false
			}
		}
		return true
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !pyjsonEqual(x[i], y[i]) {
				return false
			}
		}
		return true
	case int:
		return pyEqual(int64(x), normalNumber(b))
	}
	return pyEqual(a, normalNumber(b))
}

func normalNumber(v any) any {
	if n, ok := v.(int); ok {
		return int64(n)
	}
	return v
}
