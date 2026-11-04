package api

// routes/notebook_data.py: what a notebook's pod reads, with its notebook token. Ported: the
// datasources it may read, a datasource's download link, the reports it may render. Left to
// the Python gateway: rendering a report (services/reports.render and the export's file),
// and every read while OpenLineage is on (a session's first read is told to the catalog).
//
// Note: a request handed to the Python gateway comes from the Go gateway's address, which
// the token's check of the pod's address (NOTEBOOKS__POD_IP_CHECK) refuses there.

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

func init() {
	register("notebook_data", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/notebook-data/datasources", Handler: ndCaller(d, false, ndDatasources)},
			{Method: http.MethodPost, Pattern: "/notebook-data/read", Handler: ndCaller(d, true, ndRead)},
			{Method: http.MethodGet, Pattern: "/notebook-data/reports", Handler: ndCaller(d, false, ndReports)},
		}
	})
}

// ndCall is the pod calling: its session, its person, its notebook.
type ndCall struct {
	sessionMode string
	jobName     string
	user        *auth.User
	notebookID  int64
	notebook    string
}

type ndHandler func(d *Deps, w http.ResponseWriter, r *http.Request, call *ndCall, body pyd.Body)

func ndEnded(w http.ResponseWriter, why string) {
	w.Header().Set("WWW-Authenticate", "Bearer")
	server.JSON(w, http.StatusUnauthorized, map[string]string{"detail": why})
}

// ndCaller is notebook_caller, in FastAPI's order: the body read first (with one), then the
// token, then the handler.
func ndCaller(d *Deps, readBody bool, h ndHandler) http.Handler {
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := bodyOf(r)
		ctx := r.Context()
		const ended = "This notebook's session has ended: open the notebook again"
		scheme, token, _ := strings.Cut(r.Header.Get("Authorization"), " ")
		token = pyd.Strip(token)
		if strings.ToLower(scheme) != "bearer" || !strings.HasPrefix(token, "nbk_") {
			ndEnded(w, ended)
			return
		}
		sum := sha256.Sum256([]byte(token))
		var call ndCall
		var userID int64
		var podIP *string
		err := d.DB.QueryRow(ctx, `SELECT s.mode, s.job_name, s.user_id, s.notebook_id, s.pod_ip FROM notebook_sessions s
			WHERE s.token_fp = $1 AND s.status IN ('starting', 'ready')`, hex.EncodeToString(sum[:])).
			Scan(&call.sessionMode, &call.jobName, &userID, &call.notebookID, &podIP)
		if err != nil && !scNoRows(err) {
			fail(d, w, "notebook data: the session", err)
			return
		}
		if scNoRows(err) {
			ndEnded(w, ended)
			return
		}
		var user auth.User
		err = d.DB.QueryRow(ctx, `SELECT id, email, full_name, is_active, is_superuser, is_observer, is_service FROM users WHERE id = $1`,
			userID).Scan(&user.ID, &user.Email, &user.FullName, &user.IsActive, &user.IsSuperuser, &user.IsObserver, &user.IsService)
		if err != nil && !scNoRows(err) {
			fail(d, w, "notebook data: the person", err)
			return
		}
		missingUser := scNoRows(err)
		err = d.DB.QueryRow(ctx, `SELECT name FROM notebooks WHERE id = $1`, call.notebookID).Scan(&call.notebook)
		if err != nil && !scNoRows(err) {
			fail(d, w, "notebook data: the notebook", err)
			return
		}
		if missingUser || scNoRows(err) {
			ndEnded(w, ended)
			return
		}
		if scriptcfg.Get().Notebooks.PodIPCheck {
			forwarded := r.Header.Get("X-Forwarded-For") != "" || r.Header.Get("Forwarded") != "" || r.Header.Get("X-Real-Ip") != ""
			for _, h := range []string{"X-Forwarded-For", "Forwarded", "X-Real-Ip"} {
				if _, ok := r.Header[h]; ok {
					forwarded = true
				}
			}
			if forwarded || podIP == nil || *podIP == "" || d.Auth.ClientIP.Of(r) != *podIP {
				ndEnded(w, "This notebook token holds only from its own pod")
				return
			}
		}
		if !user.IsActive {
			detail(w, http.StatusForbidden, fmt.Sprintf("The account this notebook reads with, %s, is deactivated", user.Email))
			return
		}
		call.user = &user
		h(d, w, reqinfo.With(r, d.Auth.ClientIP, &auth.Principal{User: user}), &call, body)
	})
	if readBody {
		return withBody(inner)
	}
	return inner
}

// ndReadable is _readable: the datasources the person may read, by name.
func ndReadable(d *Deps, r *http.Request, user *auth.User) ([]sqlscripts.Datasource, error) {
	return sqlscripts.ReadableDatasources(r.Context(), perm.For(d.DB, user), d.DB)
}

// ndColumns is _columns: the names of the columns, those that have one.
func ndColumns(raw string) []any {
	out := []any{}
	if raw == "" {
		raw = "[]"
	}
	v, err := pyjson.Loads(raw)
	if err != nil {
		return out
	}
	list, _ := v.([]any)
	for _, c := range list {
		if o, ok := c.(pyjson.Object); ok {
			if n := scGet(o, "name"); sqlscripts.Truthy(n) {
				out = append(out, n)
			}
		}
	}
	return out
}

// ndDatasources is GET /notebook-data/datasources (list_datasources).
func ndDatasources(d *Deps, w http.ResponseWriter, r *http.Request, call *ndCall, _ pyd.Body) {
	folders, err := sqlscripts.Folders(r.Context(), d.DB)
	if err != nil {
		fail(d, w, "notebook data: the folders", err)
		return
	}
	paths := sqlscripts.Paths(folders, "/")
	list, err := ndReadable(d, r, call.user)
	if err != nil {
		fail(d, w, "notebook data: the datasources", err)
		return
	}
	out := []any{}
	for _, ds := range list {
		folder := paths[ds.ProjectID]
		path := ds.Name
		if folder != "" {
			path = folder + "/" + ds.Name
		}
		out = append(out, pyjson.Object{{Key: "id", Value: ds.ID}, {Key: "name", Value: ds.Name}, {Key: "folder", Value: folder},
			{Key: "path", Value: path}, {Key: "rows", Value: ds.Rows}, {Key: "columns", Value: ndColumns(ds.Columns)}})
	}
	server.JSON(w, http.StatusOK, out)
}

func ndNorm(path string) string {
	parts := strings.Split(strings.Trim(pyd.Strip(path), "/"), "/")
	for i, p := range parts {
		parts[i] = pyd.Strip(p)
	}
	return strings.Join(parts, "/")
}

// ndIsDigits is str.isdigit of a stripped text (ASCII digits: what a path holds).
var ndIsDigits = regexp.MustCompile(`^[0-9]+$`)

type ndItem struct {
	ID        int64
	Name      string
	ProjectID int64
}

// ndPick is _pick: one of `readable` by id, by path or by a name only one has.
func ndPick(d *Deps, r *http.Request, readable []ndItem, wanted any, noun string) (*ndItem, *scRefusal, error) {
	asText, isText := wanted.(string)
	id, isInt := wanted.(int64)
	if isText && ndIsDigits.MatchString(pyd.Strip(asText)) {
		fmt.Sscan(pyd.Strip(asText), &id)
		isInt = true
	}
	if isInt {
		for i := range readable {
			if readable[i].ID == id {
				return &readable[i], nil, nil
			}
		}
		return nil, &scRefusal{http.StatusNotFound, fmt.Sprintf("There is no %s %s this notebook may read", noun, pyjson.Str(wanted))}, nil
	}
	name := ndNorm(asText)
	if name == "" {
		return nil, &scRefusal{http.StatusUnprocessableEntity, fmt.Sprintf("Name the %s: «Folder/Name», or its id", noun)}, nil
	}
	folders, err := sqlscripts.Folders(r.Context(), d.DB)
	if err != nil {
		return nil, nil, err
	}
	paths := sqlscripts.Paths(folders, "/")
	if strings.Contains(name, "/") {
		for i := range readable {
			if ndNorm(paths[readable[i].ProjectID]+"/"+readable[i].Name) == name {
				return &readable[i], nil, nil
			}
		}
		return nil, &scRefusal{http.StatusNotFound, fmt.Sprintf("There is no %s «%s» this notebook may read", noun, name)}, nil
	}
	var same []ndItem
	for _, x := range readable {
		if pyd.Strip(x.Name) == name {
			same = append(same, x)
		}
	}
	if len(same) == 1 {
		return &same[0], nil, nil
	}
	if len(same) == 0 {
		return nil, &scRefusal{http.StatusNotFound, fmt.Sprintf("There is no %s «%s» this notebook may read", noun, name)}, nil
	}
	var all []string
	for _, x := range same {
		all = append(all, paths[x.ProjectID]+"/"+x.Name)
	}
	sort.Strings(all)
	if len(all) > 5 {
		all = all[:5]
	}
	return nil, &scRefusal{http.StatusConflict, fmt.Sprintf("Several %ss are called «%s»: name its folder too (%s)", noun, name,
		strings.Join(all, ", "))}, nil
}

// ndRead is POST /notebook-data/read (read_datasource): a link to download the
// datasource's parquet, valid for a few minutes.
func ndRead(d *Deps, w http.ResponseWriter, r *http.Request, call *ndCall, body pyd.Body) {
	if scOpenLineageOn() {
		scForward(d, w, r)
		return
	}
	var params pyd.Params
	in := params.Body(body, gatewaymodels.ReadIn)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx := r.Context()
	list, err := ndReadable(d, r, call.user)
	if err != nil {
		fail(d, w, "notebook data: the datasources", err)
		return
	}
	items := make([]ndItem, len(list))
	for i, x := range list {
		items[i] = ndItem{x.ID, x.Name, x.ProjectID}
	}
	picked, no, err := ndPick(d, r, items, in.Get("datasource"), "datasource")
	if err != nil {
		fail(d, w, "notebook data: the datasource", err)
		return
	}
	if no != nil {
		no.send(w)
		return
	}
	var ds sqlscripts.Datasource
	for _, x := range list {
		if x.ID == picked.ID {
			ds = x
		}
	}
	c := perm.For(d.DB, call.user)
	if refusal, err := objects.EnsureReadsPinned(ctx, c, map[string]any{"bucket": ds.Bucket, "key": ds.Key}, d.Config.EngineBucket); scAnswer(d, w, "notebook data: the bucket", refusal, err) {
		return
	}
	if refusal, err := objects.EnsureCanReadKeys(ctx, c, d.DB, call.user, map[string]bool{ds.Key: true}); scAnswer(d, w, "notebook data: the key", refusal, err) {
		return
	}
	query := "bucket=" + url.QueryEscape(ds.Bucket) + "&key=" + url.QueryEscape(ds.Key) + "&expires=600"
	req, err := d.Engine.Request(ctx, http.MethodGet, "/files/presigned?"+query, nil)
	if err != nil {
		fail(d, w, "notebook data: the engine", err)
		return
	}
	res, err := d.Engine.Do(req)
	if err != nil {
		fail(d, w, "notebook data: the engine", err)
		return
	}
	raw := make([]byte, 0)
	buf := make([]byte, 32*1024)
	for {
		n, err := res.Body.Read(buf)
		raw = append(raw, buf[:n]...)
		if err != nil {
			break
		}
	}
	res.Body.Close()
	answer, _ := pyjson.Loads(string(raw))
	if res.StatusCode >= 400 {
		var why string
		if o, ok := answer.(pyjson.Object); ok {
			if v := scGet(o, "detail"); sqlscripts.Truthy(v) {
				why = pyjson.Str(v)
			}
		} else if answer != nil {
			fail(d, w, "notebook data: the engine's refusal is not an object", nil)
			return
		}
		if why == "" {
			why = fmt.Sprintf("the engine answered %d", res.StatusCode)
		}
		status := http.StatusBadGateway
		if res.StatusCode == 404 {
			status = http.StatusNotFound
		}
		detail(w, status, fmt.Sprintf("The data of «%s» cannot be read: %s", ds.Name, why))
		return
	}
	o, ok := answer.(pyjson.Object)
	link, has := o.Get("url")
	if !ok || !has {
		fail(d, w, "notebook data: the engine's link", nil) // a KeyError in Python
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: call.user, Action: "notebook.read", TargetType: scPtrStr("datasource"),
		TargetID: &ds.ID, TargetLabel: &ds.Name, Detail: pyjson.Object{{Key: "notebook", Value: call.notebookID},
			{Key: "notebook_name", Value: call.notebook}, {Key: "mode", Value: call.sessionMode}}})
	if err := ndRecord(r, d, call, ds.ID); err != nil {
		d.Log.Warn("notebooks: the read not recorded", "datasource", ds.ID, "notebook", call.notebookID, "err", err)
	}
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "url", Value: link}, {Key: "id", Value: ds.ID}, {Key: "name", Value: ds.Name},
		{Key: "rows", Value: ds.Rows}})
}

// ndRecord is notebook_reads.record: the session's first read of the datasource kept.
func ndRecord(r *http.Request, d *Deps, call *ndCall, datasourceID int64) error {
	ctx := r.Context()
	now := time.Now().UTC()
	tag, err := d.DB.Exec(ctx, `UPDATE notebook_reads SET last_session = $1, read_at = $2 WHERE notebook_id = $3 AND datasource_id = $4
		AND mode = $5 AND last_session != $1`, call.jobName, now, call.notebookID, datasourceID, call.sessionMode)
	if err != nil || tag.RowsAffected() > 0 {
		return err
	}
	var one int64
	err = d.DB.QueryRow(ctx, `SELECT id FROM notebook_reads WHERE notebook_id = $1 AND datasource_id = $2 AND mode = $3`,
		call.notebookID, datasourceID, call.sessionMode).Scan(&one)
	if err == nil || err != pgx.ErrNoRows {
		return err
	}
	_, err = d.DB.Exec(ctx, `INSERT INTO notebook_reads (notebook_id, datasource_id, last_session, mode, first_read_at, read_at)
		VALUES ($1, $2, $3, $4, $5, $5)`, call.notebookID, datasourceID, call.jobName, call.sessionMode, now)
	if copynames.Clash(err) {
		return nil
	}
	return err
}

// ndReports is GET /notebook-data/reports (list_reports): the reports the notebook may
// render, with their parameters.
func ndReports(d *Deps, w http.ResponseWriter, r *http.Request, call *ndCall, _ pyd.Body) {
	ctx := r.Context()
	c := perm.For(d.DB, call.user)
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		fail(d, w, "notebook data: the roles", err)
		return
	}
	sql := `SELECT r.id, r.name, r.project_id, v.content FROM reports r LEFT JOIN report_versions v
		ON v.report_id = r.id AND v.version = r.current_version`
	var args []any
	if !admin {
		readable, err := c.Readable(ctx)
		if err != nil {
			fail(d, w, "notebook data: the folders", err)
			return
		}
		if len(readable) == 0 {
			server.JSON(w, http.StatusOK, []any{})
			return
		}
		sql += ` WHERE r.project_id = ANY($1)`
		args = append(args, perm.Sorted(readable))
	}
	rows, err := d.DB.Query(ctx, sql+` ORDER BY r.name`, args...)
	if err != nil {
		fail(d, w, "notebook data: the reports", err)
		return
	}
	type rep struct {
		id, project int64
		name        string
		content     *string
	}
	var list []rep
	for rows.Next() {
		var x rep
		if err := rows.Scan(&x.id, &x.name, &x.project, &x.content); err != nil {
			rows.Close()
			fail(d, w, "notebook data: a report", err)
			return
		}
		list = append(list, x)
	}
	rows.Close()
	folders, err := sqlscripts.Folders(ctx, d.DB)
	if err != nil {
		fail(d, w, "notebook data: the folders", err)
		return
	}
	paths := sqlscripts.Paths(folders, "/")
	out := []any{}
	for _, x := range list {
		params := []any{}
		if x.content != nil {
			if v, err := pyjson.Loads(*x.content); err == nil {
				def, _ := v.(pyjson.Object)
				ps, _ := scGet(def, "parameters").([]any)
				for _, p := range ps {
					o, ok := p.(pyjson.Object)
					if !ok {
						continue
					}
					params = append(params, pyjson.Object{{Key: "name", Value: scGet(o, "name")}, {Key: "type", Value: scGet(o, "type")},
						{Key: "multi", Value: sqlscripts.Truthy(scGet(o, "multi"))}, {Key: "required", Value: sqlscripts.Truthy(scGet(o, "required"))},
						{Key: "default", Value: scGet(o, "default")}})
				}
			}
		}
		folder := paths[x.project]
		path := x.name
		if folder != "" {
			path = folder + "/" + x.name
		}
		out = append(out, pyjson.Object{{Key: "id", Value: x.id}, {Key: "name", Value: x.name}, {Key: "folder", Value: folder},
			{Key: "path", Value: path}, {Key: "parameters", Value: params}})
	}
	server.JSON(w, http.StatusOK, out)
}
