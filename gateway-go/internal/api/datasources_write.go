package api

import (
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

const (
	maxColumnDescription     = 2000 // MAX_COLUMN_DESCRIPTION
	maxDatasourceDescription = 4000 // MAX_DATASOURCE_DESCRIPTION
	maxSortKey               = 255  // MAX_SORT_KEY
)

// openLineageOn is services/openlineage.py enabled: a rename or a move is then told to the
// collector (catalog_names, renamed), which is not ported: such requests stay Python's.
func openLineageOn() bool {
	for _, k := range []string{"OPENLINEAGE__URL", "OPENLINEAGE__FILE", "OPENLINEAGE_URL", "OPENLINEAGE_CONFIG"} {
		if os.Getenv(k) != "" {
			return true
		}
	}
	return false
}

func init() {
	register("datasources_write", func(d *Deps) []server.Route {
		patch := withBody(server.Authed(d.Auth, "/datasources/{ds_id}", updateDatasource(d)))
		return []server.Route{
			{Method: http.MethodPatch, Pattern: "/datasources/{ds_id}", Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if openLineageOn() {
					d.Upstream.ServeHTTP(w, r)
					return
				}
				patch.ServeHTTP(w, r)
			})},
			{Method: http.MethodDelete, Pattern: "/datasources/{ds_id}", Handler: server.Authed(d.Auth, "/datasources/{ds_id}", deleteDatasource(d))},
		}
	})
}

func unprocessable(detail string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: detail}
}

// cleanColumnDescriptions is clean_column_descriptions: trimmed, empty ones dropped.
func cleanColumnDescriptions(value *pyd.Object) (pyjson.Object, *auth.Refusal) {
	out := pyjson.Object{}
	for _, k := range value.Keys() {
		v, _ := value.Get(k)
		col := pyd.Strip(k)
		text := ""
		if pyd.Truthy(v) {
			text = pyd.Strip(pyd.PyStr(v))
		}
		if col == "" || text == "" {
			continue
		}
		if pyd.Len(text) > maxColumnDescription {
			return nil, unprocessable(fmt.Sprintf("The description of field '%s' is longer than %d characters", col, maxColumnDescription))
		}
		out = out.Set(col, text)
	}
	return out, nil
}

// noSortKeys is _checked_sort_keys's refusal per kind of source.
var noSortKeys = map[string]string{
	"sharepoint": "SharePoint imports keep the rows in file order: no sort keys",
	"sftp":       "SFTP imports keep the rows in file order: no sort keys",
	"api":        "API imports keep the rows in the order of the responses: no sort keys",
	"s3":         "S3 imports keep the rows in file order: no sort keys",
	"gsheets":    "Google Sheets imports keep the rows in the sheet's order: no sort keys",
}

// checkedSortKeys is _checked_sort_keys: against the columns of the last import.
func checkedSortKeys(ds *datasourceRow, keys []any) ([]any, *auth.Refusal) {
	if ds.Kind == "flow" {
		return nil, unprocessable("This datasource is published by a flow: set its sort keys on the flow's Output node")
	}
	if ds.SourceType != nil {
		if msg, ok := noSortKeys[*ds.SourceType]; ok {
			return nil, unprocessable(msg)
		}
	}
	cleaned := []any{}
	seen := map[string]bool{}
	for _, k := range keys {
		key := pyd.Strip(k.(string))
		if key == "" || seen[key] {
			continue
		}
		if pyd.Len(key) > maxSortKey {
			return nil, unprocessable(fmt.Sprintf("Sort key longer than %d characters", maxSortKey))
		}
		seen[key] = true
		cleaned = append(cleaned, key)
	}
	var known []any
	if cols, ok := loadOr(ds.Columns, "[]", []any{}).([]any); ok {
		for _, c := range cols {
			if o, ok := c.(*pyd.Object); ok {
				name, _ := o.Get("name")
				known = append(known, name)
			}
		}
	}
	var unknown []string
	for _, k := range cleaned {
		if len(known) == 0 {
			break
		}
		in := false
		for _, n := range known {
			if pyEqual(n, k) {
				in = true
				break
			}
		}
		if !in {
			unknown = append(unknown, k.(string))
		}
	}
	if len(unknown) > 0 {
		return nil, unprocessable("Not columns of this datasource: " + strings.Join(unknown, ", "))
	}
	return cleaned, nil
}

// datasourceOf is _get_ds after the parameters: the 404, then the capability.
func datasourceOf(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, v any, needed perm.Capability) (*datasourceRow, bool) {
	id, ok := rowID(v)
	var ds datasourceRow
	var err error
	if ok {
		ds, err = scanDatasource(d.DB.QueryRow(r.Context(), `SELECT `+datasourceColumns+` FROM datasources WHERE id = $1`, id))
	}
	if !ok || errors.Is(err, pgx.ErrNoRows) {
		server.Refuse(w, notFound("Datasource not found"))
		return nil, true
	}
	if err != nil {
		fail500(d, w, "datasources", err)
		return nil, true
	}
	if ref, err := c.Ensure(r.Context(), ds.ProjectID, needed); refuse(d, w, ref, err) {
		return nil, true
	}
	return &ds, false
}

// updateDatasource is PATCH /datasources/{ds_id} (update_datasource): rename, describe, move;
// a move drops a refresh schedule someone else set.
func updateDatasource(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		dsV := ps.Path("ds_id", r.PathValue("ds_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.DatasourceUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		ds, done := datasourceOf(d, w, r, c, dsV, perm.Edit)
		if done {
			return
		}
		var scheduledBy *int64
		if err := d.DB.QueryRow(ctx, `SELECT refresh_scheduled_by FROM datasources WHERE id = $1`, ds.ID).Scan(&scheduledBy); err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		fromFolder, fromName, scheduleBefore := ds.ProjectID, ds.Name, ds.RefreshSchedule
		target, clearSchedule := ds.ProjectID, false
		if pv := body.Get("project_id"); pv != nil {
			dest, ok := rowID(pv)
			if !ok || dest != ds.ProjectID {
				found := false
				var err error
				if ok {
					if found, err = exists(ctx, d.DB, "projects", dest); err != nil {
						fail500(d, w, "datasources", err)
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
				if pyd.Truthy(strOrNil(ds.RefreshSchedule)) && (scheduledBy == nil || *scheduledBy != p.User.ID) {
					clearSchedule = true
				}
			}
		}
		name := ds.Name
		if s := pyStr(body.Get("name")); s != nil {
			name = pyd.Strip(*s)
		}
		if name == "" {
			server.Refuse(w, unprocessable("The datasource needs a name"))
			return
		}
		var one int
		switch err := d.DB.QueryRow(ctx, `SELECT 1 FROM datasources WHERE project_id = $1 AND name = $2 AND id <> $3 LIMIT 1`,
			target, name, ds.ID).Scan(&one); {
		case err == nil:
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a datasource '%s' in this folder", name)})
			return
		case !errors.Is(err, pgx.ErrNoRows):
			fail500(d, w, "datasources", err)
			return
		}
		description := ds.Description
		if s := pyStr(body.Get("description")); s != nil {
			description = pyd.Strip(*s)
			if pyd.Len(description) > maxDatasourceDescription {
				server.Refuse(w, unprocessable(fmt.Sprintf("The description is longer than %d characters", maxDatasourceDescription)))
				return
			}
		}
		descriptions := ds.ColumnDescriptions
		if cd, ok := body.Get("column_descriptions").(*pyd.Object); ok {
			cleaned, ref := cleanColumnDescriptions(cd)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			descriptions, _ = pyjson.Dumps(cleaned, pyjson.Options{})
		}
		sortKeys := ds.SortKeys
		if keys, ok := body.Get("sort_keys").([]any); ok {
			checked, ref := checkedSortKeys(ds, keys)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			sortKeys, _ = pyjson.Dumps(checked, pyjson.Options{EnsureASCII: true})
		}
		_, err := d.DB.Exec(ctx, `UPDATE datasources SET name = $1, project_id = $2, description = $3, column_descriptions = $4, sort_keys = $5,
			updated_at = $6, refresh_schedule = CASE WHEN $7 THEN NULL ELSE refresh_schedule END,
			refresh_scheduled_by = CASE WHEN $7 THEN NULL ELSE refresh_scheduled_by END,
			next_refresh_at = CASE WHEN $7 THEN NULL ELSE next_refresh_at END WHERE id = $8`,
			name, target, description, descriptions, sortKeys, time.Now().UTC(), clearSchedule, ds.ID)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		detail := pyjson.Object{}
		if target != fromFolder {
			cleared := pyd.Truthy(strOrNil(scheduleBefore)) && clearSchedule
			detail = append(detail, pyjson.KV{Key: "moved_to", Value: target}, pyjson.KV{Key: "from_project", Value: fromFolder},
				pyjson.KV{Key: "schedule_cleared", Value: cleared})
		}
		if name != fromName {
			detail = append(detail, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: fromName}, {Key: "to", Value: name}}})
		}
		if len(detail) > 0 {
			kind := "datasource"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.update", TargetType: &kind, TargetID: &ds.ID,
				TargetLabel: &name, Detail: detail})
		}
		row, err := scanDatasource(d.DB.QueryRow(ctx, `SELECT `+datasourceColumns+` FROM datasources WHERE id = $1`, ds.ID))
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		out, err := datasourceOut(row, false, nil)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// deleteDatasource is DELETE /datasources/{ds_id}: refused while saved views, reports or
// dashboards read it; the runs keep their history without the reference; the contract goes
// with it, and the blob is deleted after the grace.
func deleteDatasource(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		dsV := ps.Path("ds_id", r.PathValue("ds_id"), pyd.Int())
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		ds, done := datasourceOf(d, w, r, c, dsV, perm.Edit)
		if done {
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT name FROM saved_views WHERE datasource_id = $1 ORDER BY name LIMIT 6`, ds.ID)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		var views []string
		for rows.Next() {
			var n string
			if err := rows.Scan(&n); err != nil {
				rows.Close()
				fail500(d, w, "datasources", err)
				return
			}
			views = append(views, n)
		}
		rows.Close()
		if len(views) > 0 {
			shown := views
			if len(shown) > 5 {
				shown = shown[:5]
			}
			quoted := make([]string, len(shown))
			for i, n := range shown {
				quoted[i] = "'" + n + "'"
			}
			names := strings.Join(quoted, ", ")
			if len(views) > 5 {
				names += ", …"
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("The datasource is used by saved views (%s): delete them first", names)})
			return
		}
		if refuseReadBy(d, w, ctx, c, "datasource", ds.ID, "The datasource") {
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		defer tx.Rollback(ctx)
		for _, q := range []string{
			`UPDATE runs SET datasource_id = NULL WHERE datasource_id = $1`,
			`DELETE FROM datasources WHERE id = $1`,
			`DELETE FROM data_contracts WHERE datasource_id = $1`,
			`DELETE FROM data_contract_versions WHERE datasource_id = $1`,
			`DELETE FROM data_contract_results WHERE datasource_id = $1`,
		} {
			if _, err := tx.Exec(ctx, q, ds.ID); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		if ds.Key != "" { // schedule_blob_deletion, after blob_deletion_grace_seconds
			now := time.Now().UTC()
			grace := time.Duration(d.Panel.RunStaleTimeout()+600) * time.Second
			if _, err := tx.Exec(ctx, `INSERT INTO pending_blob_deletions (bucket, key, delete_after, reason, created_at) VALUES ($1, $2, $3, $4, $5)`,
				ds.Bucket, ds.Key, now.Add(grace), fmt.Sprintf("datasource %d deleted", ds.ID), now); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		kind := "datasource"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.delete", TargetType: &kind, TargetID: &ds.ID,
			TargetLabel: &ds.Name, Detail: pyjson.Object{{Key: "kind", Value: ds.Kind}}})
		returnsNone(w)
	}
}
