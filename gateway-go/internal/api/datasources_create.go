package api

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/contracts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/datasources.py: the datasources a connection brings (a database, SharePoint, SFTP,
// S3, an HTTP API, Google Sheets), created with their first import; their copy, refresh,
// schedule and import history.

func init() {
	register("datasources_create", func(d *Deps) []server.Route {
		a := func(route string, h authedFunc) http.Handler { return server.Authed(d.Auth, route, h) }
		b := func(route string, h authedFunc) http.Handler { return withBody(a(route, h)) }
		create := func(kind string, h dsCreator) server.Route {
			route := "/projects/{project_id}/datasources/" + kind
			return server.Route{Method: http.MethodPost, Pattern: route, Handler: b(route, createDatasource(d, h))}
		}
		return []server.Route{
			create("database", dbDatasource),
			create("sharepoint", sharePointDatasource),
			create("sftp", sftpDatasource),
			create("s3", s3Datasource),
			create("api", apiDatasource),
			create("gsheets", gsheetsDatasource),
			{Method: http.MethodPost, Pattern: "/datasources/{ds_id}/duplicate", Handler: a("/datasources/{ds_id}/duplicate", duplicateDatasource(d))},
			{Method: http.MethodPost, Pattern: "/datasources/{ds_id}/refresh", Handler: a("/datasources/{ds_id}/refresh", refreshDatasource(d))},
			{Method: http.MethodPut, Pattern: "/datasources/{ds_id}/schedule", Handler: b("/datasources/{ds_id}/schedule", setDatasourceSchedule(d))},
			{Method: http.MethodGet, Pattern: "/datasources/{ds_id}/runs", Handler: a("/datasources/{ds_id}/runs", listDatasourceRuns(d))},
		}
	})
}

// newDatasource is what a create route saves, once its checks passed.
type newDatasource struct {
	name, description, sourceType string
	sourceRef                     string
	sortKeys                      string
	detail                        pyjson.Object // the audit's, after project_id and connection_id
}

// dsCreator is one kind's checks: the datasource to save, or a refusal. `taken` is the 409 of
// a name already in the folder, which each kind checks at its own moment.
type dsCreator struct {
	model *pyd.Model
	build func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error)
}

func dsTaken(ctx context.Context, d *Deps, project int64) (func(string) *auth.Refusal, *error) {
	var failure error
	return func(name string) *auth.Refusal {
		var one int
		err := d.DB.QueryRow(ctx, `SELECT 1 FROM datasources WHERE project_id = $1 AND name = $2 LIMIT 1`, project, name).Scan(&one)
		if err == nil {
			return &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a datasource '%s' in this folder", name)}
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			failure = err
			return &auth.Refusal{} // read failure below
		}
		return nil
	}, &failure
}

// cleanSortKeys is clean_sort_keys.
func cleanSortKeys(keys []any) ([]any, *auth.Refusal) {
	cleaned := []any{}
	seen := map[string]bool{}
	for _, k := range keys {
		key := pystr.Strip(k.(string))
		if key != "" && !seen[key] {
			if pystr.Len(key) > maxSortKey {
				return nil, unprocessable(fmt.Sprintf("Sort key longer than %d characters", maxSortKey))
			}
			seen[key] = true
			cleaned = append(cleaned, key)
		}
	}
	return cleaned, nil
}

var dbDatasource = dsCreator{gatewaymodels.DbDatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType == "s3" {
			return nil, unprocessable("An S3 connection is not a database: read its files with an «S3 folder» datasource"), nil
		}
		if connpayload.NotDatabases[conn.DBType] {
			return nil, unprocessable(fmt.Sprintf("A %s connection is not a database", conn.DBType)), nil
		}
		st, ref := strField(body, "source_type"), strField(body, "source_ref")
		if st != "table" && st != "sql" {
			return nil, unprocessable("source_type must be 'table' or 'sql'"), nil
		}
		if pystr.Strip(ref) == "" {
			return nil, unprocessable("The source (table or SQL) is empty"), nil
		}
		name := pystr.Strip(strField(body, "name"))
		if name == "" {
			return nil, unprocessable("The datasource needs a name"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		keys, _ := body.Get("sort_keys").([]any)
		cleaned, r := cleanSortKeys(keys)
		if r != nil {
			return nil, r, nil
		}
		sk, _ := pyjson.Dumps(cleaned, pyjson.Options{EnsureASCII: true})
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: st, sourceRef: ref, sortKeys: sk,
			detail: pyjson.Object{{Key: "source_type", Value: st}, {Key: "source_ref", Value: ref}}}, nil, nil
	}}

// fileRef validates a file datasource's templates with the engine (validate_file_templates)
// and gives the stored JSON.
func fileRef(ctx context.Context, d *Deps, conn *connpayload.Conn, ref pyjson.Object, fields ...string) (string, *auth.Refusal, error) {
	dynamic := false
	for _, f := range fields {
		s := pyjson.Str(pyjson.Or(ref.Value(f), ""))
		if containsAny(s, "{}") {
			dynamic = true
		}
	}
	for _, k := range []string{"timezone", "variables", "last_refresh_initial", "mode", "keys"} {
		if _, ok := ref.Get(k); ok {
			dynamic = true
		}
	}
	if dynamic {
		if r, err := validateRef(ctx, d, conn, ref); r != nil || err != nil {
			return "", r, err
		}
	}
	text, err := pyjson.Dumps(ref, pyjson.Options{EnsureASCII: true})
	return text, nil, err
}

// validateRef is `_engine_inspect(…, "validate", source=ref)`, its 400 as a 422.
func validateRef(ctx context.Context, d *Deps, conn *connpayload.Conn, ref pyjson.Object) (*auth.Refusal, error) {
	_, r, err := engineInspect(ctx, d, connpayload.Payload(conn), "validate", "", "en", pyjson.Object{{Key: "source", Value: ref}})
	if r != nil && r.Status == http.StatusBadRequest {
		r = unprocessable(r.Detail)
	}
	return r, err
}

func containsAny(s, chars string) bool {
	for _, c := range chars {
		for _, x := range s {
			if x == c {
				return true
			}
		}
	}
	return false
}

func withFields(o pyjson.Object, more pyjson.Object) pyjson.Object {
	for _, kv := range more {
		o = o.Set(kv.Key, kv.Value)
	}
	return o
}

var sharePointDatasource = dsCreator{gatewaymodels.SharePointDatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType != "sharepoint" {
			return nil, unprocessable("A SharePoint connection is required"), nil
		}
		name, path, sheet := pystr.Strip(strField(body, "name")), pystr.Strip(strField(body, "path")), pystr.Strip(strField(body, "sheet"))
		if name == "" || path == "" {
			return nil, unprocessable("Name and path are required"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		format := strField(body, "format")
		ref := withFields(pyjson.Object{{Key: "path", Value: path}, {Key: "sheet", Value: sheet}, {Key: "format", Value: format},
			{Key: "csv", Value: fieldDump(body.Get("csv"))}}, templateFields(body))
		text, r, err := fileRef(ctx, d, conn, ref, "path")
		if r != nil || err != nil {
			return nil, r, err
		}
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: "sharepoint", sourceRef: text, sortKeys: "[]",
			detail: pyjson.Object{{Key: "source_type", Value: "sharepoint"}, {Key: "path", Value: path}, {Key: "format", Value: format},
				{Key: "sheet", Value: sheet}}}, nil, nil
	}}

// fieldDump is a validated field's model_dump(): a model given, or its default dict.
func fieldDump(v any) any {
	if mv, ok := v.(*pyd.ModelValue); ok {
		return pyDump(mv)
	}
	return pydconv.FromPyd(v)
}

var sftpDatasource = dsCreator{gatewaymodels.SftpDatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType != "sftp" {
			return nil, unprocessable("An SFTP connection is required"), nil
		}
		name, pattern := pystr.Strip(strField(body, "name")), pystr.Strip(strField(body, "pattern"))
		if name == "" || pattern == "" {
			return nil, unprocessable("Name and file pattern are required"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		folder := pystr.Strip(strField(body, "folder"))
		ref := withFields(pyjson.Object{{Key: "folder", Value: folder}, {Key: "pattern", Value: pattern},
			{Key: "format", Value: strField(body, "format")}, {Key: "sheet", Value: pystr.Strip(strField(body, "sheet"))},
			{Key: "csv", Value: fieldDump(body.Get("csv"))}}, templateFields(body))
		text, r, err := fileRef(ctx, d, conn, ref, "folder", "pattern")
		if r != nil || err != nil {
			return nil, r, err
		}
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: "sftp", sourceRef: text, sortKeys: "[]",
			detail: pyjson.Object{{Key: "source_type", Value: "sftp"}, {Key: "folder", Value: folder}, {Key: "pattern", Value: pattern}}}, nil, nil
	}}

var s3Datasource = dsCreator{gatewaymodels.S3DatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType != "s3" {
			return nil, unprocessable("An S3 connection is required"), nil
		}
		name := pystr.Strip(strField(body, "name"))
		if name == "" || pystr.Strip(strField(body, "pattern")) == "" {
			return nil, unprocessable("Name and file pattern are required"), nil
		}
		bucket := pystr.Strip(strField(body, "bucket"))
		if bucket == "" && pystr.Strip(conn.Database) == "" {
			return nil, unprocessable("No bucket: set one on the datasource or as the connection's default"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		folder, pattern := strings.Trim(pystr.Strip(strField(body, "folder")), "/"), pystr.Strip(strField(body, "pattern"))
		ref := withFields(pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "folder", Value: folder}, {Key: "pattern", Value: pattern},
			{Key: "format", Value: strField(body, "format")}, {Key: "sheet", Value: pystr.Strip(strField(body, "sheet"))},
			{Key: "csv", Value: fieldDump(body.Get("csv"))}}, templateFields(body))
		text, r, err := fileRef(ctx, d, conn, ref, "folder", "pattern")
		if r != nil || err != nil {
			return nil, r, err
		}
		shown := bucket
		if shown == "" {
			shown = conn.Database
		}
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: "s3", sourceRef: text, sortKeys: "[]",
			detail: pyjson.Object{{Key: "source_type", Value: "s3"}, {Key: "bucket", Value: shown}, {Key: "folder", Value: folder},
				{Key: "pattern", Value: pattern}}}, nil, nil
	}}

var apiDatasource = dsCreator{gatewaymodels.ApiDatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType != "api" {
			return nil, unprocessable("An API connection is required"), nil
		}
		name := pystr.Strip(strField(body, "name"))
		if name == "" {
			return nil, unprocessable("The datasource needs a name"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		ref := pyjson.Object{}
		for _, kv := range apiDump(body) {
			if kv.Key != "name" && kv.Key != "description" && kv.Key != "connection_id" {
				ref = append(ref, kv)
			}
		}
		keys := []any{}
		ks, _ := ref.Value("keys").([]any)
		for _, k := range ks {
			if s := pystr.Strip(k.(string)); s != "" {
				keys = append(keys, s)
			}
		}
		ref = ref.Set("keys", keys)
		if r, err := validateRef(ctx, d, conn, ref); r != nil || err != nil {
			return nil, r, err
		}
		text, err := pyjson.Dumps(ref, pyjson.Options{EnsureASCII: true})
		if err != nil {
			return nil, nil, err
		}
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: "api", sourceRef: text, sortKeys: "[]",
			detail: pyjson.Object{{Key: "source_type", Value: "api"}, {Key: "method", Value: ref.Value("method")},
				{Key: "path", Value: ref.Value("path")}, {Key: "mode", Value: ref.Value("mode")}}}, nil, nil
	}}

var gsheetsDatasource = dsCreator{gatewaymodels.GsheetsDatasourceCreate,
	func(ctx context.Context, d *Deps, body *pyd.ModelValue, conn *connpayload.Conn, taken func(string) *auth.Refusal) (*newDatasource, *auth.Refusal, error) {
		if conn.DBType != "gsheets" {
			return nil, unprocessable("A Google Sheets connection is required"), nil
		}
		name := pystr.Strip(strField(body, "name"))
		if name == "" {
			return nil, unprocessable("The datasource needs a name"), nil
		}
		sheetID := body.Get("sheet_id")
		if sheetID == nil && pystr.Strip(strField(body, "sheet")) == "" {
			return nil, unprocessable("Choose the spreadsheet's tab"), nil
		}
		if r := taken(name); r != nil {
			return nil, r, nil
		}
		ref := pyjson.Object{{Key: "spreadsheet_id", Value: strField(body, "spreadsheet_id")},
			{Key: "spreadsheet_name", Value: pystr.Strip(strField(body, "spreadsheet_name"))}, {Key: "sheet_id", Value: portParam(sheetID)},
			{Key: "sheet", Value: pystr.Strip(strField(body, "sheet"))}, {Key: "range", Value: strings.ToUpper(pystr.Strip(strField(body, "range")))}}
		text, err := pyjson.Dumps(ref, pyjson.Options{EnsureASCII: true})
		if err != nil {
			return nil, nil, err
		}
		return &newDatasource{name: name, description: strField(body, "description"), sourceType: "gsheets", sourceRef: text, sortKeys: "[]",
			detail: pyjson.Object{{Key: "source_type", Value: "gsheets"}, {Key: "spreadsheet_id", Value: ref.Value("spreadsheet_id")},
				{Key: "sheet", Value: ref.Value("sheet")}, {Key: "range", Value: ref.Value("range")}}}, nil, nil
	}}

// ingestEnv is what an import is launched with.
func (d *Deps) ingestEnv(db runlaunch.DB) runlaunch.Env {
	return runlaunch.Env{DB: db, Engine: d.Engine, Bucket: d.Config.EngineBucket}
}

// launchFailure answers what stopped an import's launch: the engine's refusal, or the 500.
func launchFailure(d *Deps, w http.ResponseWriter, ref *auth.Refusal, err error) {
	if err != nil {
		launchErr(d, w, err)
		return
	}
	server.Refuse(w, ref)
}

// createDatasource is the six POST /projects/{project_id}/datasources/<kind>: EDIT on the
// folder, CONNECT on the connection's; the first import starts at once, and a datasource
// whose import cannot start is not kept.
func createDatasource(d *Deps, kind dsCreator) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		pv := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
		body := ps.Body(bodyOf(r), kind.model)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		project, ok := rowID(pv)
		found := false
		if ok {
			var err error
			if found, err = exists(ctx, d.DB, "projects", project); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		if !found {
			server.Refuse(w, notFound("Folder not found"))
			return
		}
		if ref, err := c.Ensure(ctx, project, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		var conn *connpayload.Conn
		if cid, ok := rowID(body.Get("connection_id")); ok {
			var err error
			if conn, err = connpayload.Load(ctx, d.DB, cid); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		if conn == nil {
			server.Refuse(w, notFound("Connection not found"))
			return
		}
		if ref, err := c.Ensure(ctx, conn.ProjectID, perm.Connect); refuse(d, w, ref, err) {
			return
		}
		taken, failure := dsTaken(ctx, d, project)
		nd, ref, err := kind.build(ctx, d, body, conn, taken)
		if *failure != nil {
			fail500(d, w, "datasources", *failure)
			return
		}
		if err != nil || ref != nil {
			launchFailure(d, w, ref, err)
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys,
			column_descriptions, kind, flow_id, connection_id, source_type, source_ref, refreshed_at, refresh_schedule, refresh_scheduled_by,
			next_refresh_at, snapshot_run_id, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, '', NULL, '[]', $6, '{}', 'database', NULL,
			$7, $8, $9, NULL, NULL, NULL, NULL, NULL, $10, $10) RETURNING id`,
			nd.name, nd.description, project, p.User.ID, d.Config.EngineBucket, nd.sortKeys, conn.ID, nd.sourceType, nd.sourceRef, now).Scan(&id)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		st, sr := nd.sourceType, nd.sourceRef
		src := &runlaunch.Source{ID: id, Bucket: d.Config.EngineBucket, SourceType: &st, SourceRef: &sr, SortKeys: nd.sortKeys}
		if _, ref, err := runlaunch.Ingest(ctx, d.ingestEnv(d.DB), p.User.ID, src, conn, "manual", nil); err != nil || ref != nil {
			// engine down or request refused: no half datasource
			if _, derr := d.DB.Exec(ctx, `DELETE FROM datasources WHERE id = $1`, id); derr != nil {
				fail500(d, w, "datasources", derr)
				return
			}
			launchFailure(d, w, ref, err)
			return
		}
		kindName := "datasource"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.create", TargetType: &kindName, TargetID: &id,
			TargetLabel: &nd.name, Detail: append(pyjson.Object{{Key: "project_id", Value: project}, {Key: "connection_id", Value: conn.ID}},
				nd.detail...)})
		answerDatasource(d, w, ctx, id, http.StatusCreated, nil)
	}
}

// answerDatasource is `return _to_out(ds, contract=…)`.
func answerDatasource(d *Deps, w http.ResponseWriter, ctx context.Context, id int64, status int, contract *ContractSummary) {
	row, err := scanDatasource(d.DB.QueryRow(ctx, `SELECT `+datasourceColumns+` FROM datasources WHERE id = $1`, id))
	if err != nil {
		fail500(d, w, "datasources", err)
		return
	}
	out, err := datasourceOut(row, false, contract)
	if err != nil {
		fail500(d, w, "datasources", err)
		return
	}
	server.JSON(w, status, out)
}

// dsByPath is _get_ds after the path's validation.
func dsByPath(d *Deps, w http.ResponseWriter, r *http.Request, v any) (*datasourceRow, bool) {
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
	return &ds, false
}

// copyTimeout is _COPY_TIMEOUT: a copy on the storage is one request however big the file.
const copyTimeout = 900 * time.Second

// duplicateDatasource is POST /datasources/{ds_id}/duplicate: a copy as «name (i)», its data
// copied to a file of its own; the contract comes along, the schedule does not.
func duplicateDatasource(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "ds_id")
		if !ok {
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		src, done := dsByPath(d, w, r, ids[0])
		if done {
			return
		}
		if ref, err := c.Ensure(ctx, src.ProjectID, perm.Edit); refuse(d, w, ref, err) {
			return
		}
		if src.ConnectionID != nil {
			conn, err := connpayload.Load(ctx, d.DB, *src.ConnectionID)
			if err != nil {
				fail500(d, w, "datasources", err)
				return
			}
			if conn != nil {
				if ref, err := c.Ensure(ctx, conn.ProjectID, perm.Connect); refuse(d, w, ref, err) {
					return
				}
			}
		}
		contract, err := contracts.Get(ctx, d.DB, src.ID)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		var document any
		if contract != nil {
			if document, err = pyd.LoadsStr(contract.Document); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		name, err := copynames.Free(ctx, d.DB, "datasources", src.ProjectID, src.Name)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys,
			column_descriptions, kind, flow_id, connection_id, source_type, source_ref, refreshed_at, refresh_schedule, refresh_scheduled_by,
			next_refresh_at, snapshot_run_id, created_at, updated_at)
			SELECT $1, description, project_id, $2, bucket, '', rows, columns, sort_keys, column_descriptions, kind, NULL, connection_id,
			source_type, source_ref, refreshed_at, NULL, NULL, NULL, NULL, $3, $3 FROM datasources WHERE id = $4 RETURNING id`,
			name, p.User.ID, now, src.ID).Scan(&id)
		if copynames.Clash(err) {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: copynames.ClashDetail})
			return
		}
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		key := ""
		if src.Key != "" {
			key = runlaunch.SnapshotKey(&id)
			if failure := copyFile(ctx, d, src.Bucket, src.Key, key); failure != "" {
				// nothing half made: no datasource without its data
				if _, err := d.DB.Exec(ctx, `DELETE FROM datasources WHERE id = $1`, id); err != nil {
					fail500(d, w, "datasources", err)
					return
				}
				d.Log.Warn("duplicate of a datasource: the copy failed", "datasource", src.ID, "key", src.Key, "failure", failure)
				server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: "The data could not be copied, nothing was created: " + failure})
				return
			}
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		defer tx.Rollback(ctx)
		var summary *ContractSummary
		if contract != nil {
			saved, err := contracts.Save(ctx, tx, id, document, contract.Enabled, p.User.ID, nil)
			if err != nil {
				fail500(d, w, "datasources", err)
				return
			}
			if contract.Enabled { // lists show only contracts that are on
				s := contracts.SummaryOf(saved)
				summary = &ContractSummary{Status: s.Status, CheckedAt: isoPtr(s.CheckedAt), Errors: s.Errors, Warnings: s.Warnings,
					Blocked: s.Blocked, Version: s.Version}
			}
		}
		if key != "" {
			if _, err := tx.Exec(ctx, `UPDATE datasources SET key = $1 WHERE id = $2`, key, id); err != nil {
				fail500(d, w, "datasources", err)
				return
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		kind := "datasource"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.create", TargetType: &kind, TargetID: &id,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "kind", Value: src.Kind},
				{Key: "duplicate_of", Value: src.ID}}})
		answerDatasource(d, w, ctx, id, http.StatusCreated, summary)
	}
}

// copyFile is the engine's /files/copy: "" when done, else why not (the 502's words).
func copyFile(ctx context.Context, d *Deps, bucket, source, target string) string {
	ctx, cancel := context.WithTimeout(ctx, copyTimeout)
	defer cancel()
	query := "bucket=" + url.QueryEscape(bucket) + "&source_key=" + url.QueryEscape(source) + "&target_key=" + url.QueryEscape(target)
	res, _, err := d.Engine.JSON(ctx, http.MethodPost, "/files/copy?"+query, nil)
	if err != nil {
		return fmt.Sprintf("engine unreachable (%s)", httpxErrorName(err))
	}
	if res.StatusCode >= 400 {
		if res.StatusCode == http.StatusNotFound {
			return "the data file is missing"
		}
		return fmt.Sprintf("engine status %d", res.StatusCode)
	}
	return ""
}

// httpxErrorName is type(e).__name__ of the httpx error a failed request raises.
func httpxErrorName(err error) string {
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
		return "ReadTimeout"
	}
	return "ConnectError"
}

// refreshDatasource is POST /datasources/{ds_id}/refresh: RUN on the folder, CONNECT on the
// connection's; one refresh at a time.
func refreshDatasource(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "ds_id")
		if !ok {
			return
		}
		q := d.req(w, r, p)
		ds, done := dsByPath(d, w, r, ids[0])
		if done {
			return
		}
		if ds.Kind != "database" {
			server.Refuse(w, unprocessable("Only database datasources can be refreshed"))
			return
		}
		conn, ok := dsConnection(d, w, q, ds)
		if !ok {
			return
		}
		// the last import, reconciled first (outside the lock)
		last, err := lastIngest(q, ds.ID)
		if err != nil {
			q.fail(err)
			return
		}
		if last != nil && !terminal[last.Status] {
			if py, err := q.needsPython(last); err != nil {
				q.fail(err)
				return
			} else if py {
				d.Upstream.ServeHTTP(w, r)
				return
			}
			if _, err := q.reconcile(last, progressOf{}); err != nil {
				q.fail(err)
				return
			}
		}
		tx, err := d.DB.Begin(q.ctx)
		if err != nil {
			q.fail(err)
			return
		}
		defer tx.Rollback(q.ctx)
		if err := runlaunch.InLine(q.ctx, tx, ds.ID); err != nil {
			q.fail(err)
			return
		}
		if live, err := runlaunch.LiveRefresh(q.ctx, tx, ds.ID); err != nil {
			q.fail(err)
			return
		} else if live {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "A refresh is already running for this datasource"})
			return
		}
		src := &runlaunch.Source{ID: ds.ID, Bucket: ds.Bucket, Key: ds.Key, SourceType: ds.SourceType, SourceRef: ds.SourceRef, SortKeys: ds.SortKeys}
		runID, ref, err := runlaunch.Ingest(q.ctx, d.ingestEnv(tx), p.User.ID, src, conn, "manual", nil)
		if err != nil || ref != nil {
			launchFailure(d, w, ref, err)
			return
		}
		if err := tx.Commit(q.ctx); err != nil {
			q.fail(err)
			return
		}
		kind := "datasource"
		audit.Record(q.ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.refresh", TargetType: &kind, TargetID: &ds.ID,
			TargetLabel: &ds.Name, Detail: pyjson.Object{{Key: "run_id", Value: runID}}})
		run, err := q.readRun(runID)
		if err != nil {
			q.fail(err)
			return
		}
		server.JSON(w, http.StatusAccepted, run.out())
	}
}

// dsConnection is RUN on the datasource's folder, then its connection (a 409 when gone) and
// CONNECT on its folder.
func dsConnection(d *Deps, w http.ResponseWriter, q *request, ds *datasourceRow) (*connpayload.Conn, bool) {
	if !q.ensure(ds.ProjectID, perm.Run) {
		return nil, false
	}
	var conn *connpayload.Conn
	if ds.ConnectionID != nil && *ds.ConnectionID != 0 {
		var err error
		if conn, err = connpayload.Load(q.ctx, d.DB, *ds.ConnectionID); err != nil {
			q.fail(err)
			return nil, false
		}
	}
	if conn == nil {
		server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "The connection of this datasource no longer exists"})
		return nil, false
	}
	if !q.ensure(conn.ProjectID, perm.Connect) {
		return nil, false
	}
	return conn, true
}

// lastIngest is the datasource's last import (by start).
func lastIngest(q *request, dsID int64) (*runRow, error) {
	var x runRow
	err := q.d.DB.QueryRow(q.ctx, `SELECT `+runColumns+` FROM runs r WHERE r.datasource_id = $1 AND r.kind = 'ingest'
		ORDER BY r.started_at DESC LIMIT 1`, dsID).Scan(x.dest()...)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &x, nil
}

// setDatasourceSchedule is PUT /datasources/{ds_id}/schedule: the scheduled refresh, with the
// authority of whoever sets it; an empty cron turns it off.
func setDatasourceSchedule(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		dv := ps.Path("ds_id", r.PathValue("ds_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.ScheduleUpdate)
		if !validated(w, &ps) {
			return
		}
		q := d.req(w, r, p)
		ds, done := dsByPath(d, w, r, dv)
		if done {
			return
		}
		if ds.Kind != "database" {
			server.Refuse(w, unprocessable("Only database datasources can be scheduled"))
			return
		}
		if _, ok := dsConnection(d, w, q, ds); !ok {
			return
		}
		spec := ""
		if s := pyStr(body.Get("cron")); s != nil {
			spec = pystr.Strip(*s)
		}
		now := time.Now().UTC()
		var schedule *string
		var by *int64
		var next *time.Time
		if spec != "" {
			valid, err := cron.ValidateSchedule(spec)
			if err != nil {
				server.Refuse(w, unprocessable(err.Error()))
				return
			}
			n, err := cron.NextFire(valid, now, d.Panel.AppTimezone())
			if err != nil {
				q.fail(err)
				return
			}
			schedule, by, next = &valid, &p.User.ID, &n
		}
		if _, err := d.DB.Exec(q.ctx, `UPDATE datasources SET refresh_schedule = $1, refresh_scheduled_by = $2, next_refresh_at = $3,
			updated_at = $4 WHERE id = $5`, schedule, by, next, now, ds.ID); err != nil {
			q.fail(err)
			return
		}
		kind := "datasource"
		var cronV any
		if schedule != nil {
			cronV = *schedule
		}
		audit.Record(q.ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "datasource.schedule", TargetType: &kind, TargetID: &ds.ID,
			TargetLabel: &ds.Name, Detail: pyjson.Object{{Key: "cron", Value: cronV}}})
		answerDatasource(d, w, q.ctx, ds.ID, http.StatusOK, nil)
	}
}

// listDatasourceRuns is GET /datasources/{ds_id}/runs: the last 50 imports, reconciled.
func listDatasourceRuns(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "ds_id")
		if !ok {
			return
		}
		q := d.req(w, r, p)
		ds, done := dsByPath(d, w, r, ids[0])
		if done {
			return
		}
		if !q.ensure(ds.ProjectID, perm.View) {
			return
		}
		rows, err := d.DB.Query(q.ctx, `SELECT `+runColumns+` FROM runs r WHERE r.datasource_id = $1 AND r.kind = 'ingest'
			ORDER BY r.started_at DESC LIMIT 50`, ds.ID)
		if err != nil {
			q.fail(err)
			return
		}
		var runs []*runRow
		for rows.Next() {
			var x runRow
			if err := rows.Scan(x.dest()...); err != nil {
				rows.Close()
				q.fail(err)
				return
			}
			runs = append(runs, &x)
		}
		rows.Close()
		for _, run := range runs {
			if py, err := q.needsPython(run); err != nil {
				q.fail(err)
				return
			} else if py {
				d.Upstream.ServeHTTP(w, r)
				return
			}
		}
		progress := progressOf{}
		out := []RunOut{}
		for _, run := range runs {
			x, err := q.reconcile(run, progress)
			if err != nil {
				q.fail(err)
				return
			}
			out = append(out, runOut(x, progress))
		}
		server.JSON(w, http.StatusOK, out)
	}
}
