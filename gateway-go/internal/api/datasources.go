package api

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// ContractSummary is schemas/models.py ContractSummary (services/contracts.py summary).
type ContractSummary struct {
	Status    string  `json:"status"`
	CheckedAt *string `json:"checked_at"`
	Errors    int64   `json:"errors"`
	Warnings  int64   `json:"warnings"`
	Blocked   bool    `json:"blocked"`
	Version   int64   `json:"version"`
}

// DatasourceOut is schemas/models.py DatasourceOut, its fields in order.
type DatasourceOut struct {
	ID                 int64            `json:"id"`
	Name               string           `json:"name"`
	Description        string           `json:"description"`
	ProjectID          int64            `json:"project_id"`
	OwnerID            *int64           `json:"owner_id"`
	Bucket             string           `json:"bucket"`
	Key                string           `json:"key"`
	Rows               *int64           `json:"rows"`
	Columns            []any            `json:"columns"`
	ColumnDescriptions *pyd.Object      `json:"column_descriptions"`
	DescribedColumns   int              `json:"described_columns"`
	TotalColumns       int              `json:"total_columns"`
	AIReady            bool             `json:"ai_ready"`
	SortKeys           []any            `json:"sort_keys"`
	Kind               string           `json:"kind"`
	FlowID             *int64           `json:"flow_id"`
	ConnectionID       *int64           `json:"connection_id"`
	SourceType         *string          `json:"source_type"`
	SourceRef          *string          `json:"source_ref"`
	RefreshedAt        *string          `json:"refreshed_at"`
	Refreshing         bool             `json:"refreshing"`
	RefreshSchedule    *string          `json:"refresh_schedule"`
	NextRefreshAt      *string          `json:"next_refresh_at"`
	UpdatedAt          *string          `json:"updated_at"`
	Contract           *ContractSummary `json:"contract"`
}

// datasourceRow is a datasources row, the columns _to_out reads.
type datasourceRow struct {
	ID, ProjectID                          int64
	Name, Description, Bucket, Key, Kind   string
	OwnerID, Rows, FlowID, ConnectionID    *int64
	Columns, SortKeys, ColumnDescriptions  string
	SourceType, SourceRef, RefreshSchedule *string
	RefreshedAt, NextRefreshAt             *time.Time
	UpdatedAt                              time.Time
}

const datasourceColumns = `id, project_id, name, description, bucket, key, kind, owner_id, rows, flow_id, connection_id,
	columns, sort_keys, column_descriptions, source_type, source_ref, refresh_schedule, refreshed_at, next_refresh_at, updated_at`

func scanDatasource(row pgx.Row) (datasourceRow, error) {
	var d datasourceRow
	err := row.Scan(&d.ID, &d.ProjectID, &d.Name, &d.Description, &d.Bucket, &d.Key, &d.Kind, &d.OwnerID, &d.Rows, &d.FlowID,
		&d.ConnectionID, &d.Columns, &d.SortKeys, &d.ColumnDescriptions, &d.SourceType, &d.SourceRef, &d.RefreshSchedule,
		&d.RefreshedAt, &d.NextRefreshAt, &d.UpdatedAt)
	return d, err
}

func queryDatasources(ctx context.Context, q perm.Querier, tail string, args ...any) ([]datasourceRow, error) {
	rows, err := q.Query(ctx, `SELECT `+datasourceColumns+` FROM datasources `+tail, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []datasourceRow{}
	for rows.Next() {
		d, err := scanDatasource(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

func isoPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := pyjson.Isoformat(*t)
	return &s
}

// errResponse is a value the response model refuses: FastAPI's ResponseValidationError, a 500.
var errResponse = errors.New("the answer does not fit its response model")

// loadOr is `json.loads(text or default)` with a JSONDecodeError giving `fallback`.
func loadOr(text, def string, fallback any) any {
	if text == "" {
		text = def
	}
	v, err := pyd.LoadsStr(text)
	if err != nil {
		return fallback
	}
	return v
}

// columnDescriptions is _load_column_descriptions: {str(k): str(v)}, {} when not a dict.
func columnDescriptions(text string) *pyd.Object {
	out := pyd.NewObject()
	if o, ok := loadOr(text, "{}", nil).(*pyd.Object); ok {
		for _, k := range o.Keys() {
			v, _ := o.Get(k)
			out.Set(k, pyd.PyStr(v))
		}
	}
	return out
}

// hashable says whether a json.loads value can be a dict key (`x in descs` raises otherwise).
func hashable(v any) bool {
	switch v.(type) {
	case []any, *pyd.Object:
		return false
	}
	return true
}

// datasourceOut is routes/datasources.py _to_out.
func datasourceOut(d datasourceRow, refreshing bool, contract *ContractSummary) (DatasourceOut, error) {
	cols, ok := loadOr(d.Columns, "[]", []any{}).([]any)
	if !ok {
		return DatasourceOut{}, errResponse // a list is what list[dict] takes; a dict iterated gives its keys
	}
	descs := columnDescriptions(d.ColumnDescriptions)
	withDesc := make([]any, len(cols))
	for i, c := range cols {
		withDesc[i] = c
		o, isDict := c.(*pyd.Object)
		if !isDict {
			continue
		}
		name, _ := o.Get("name")
		if !hashable(name) {
			return DatasourceOut{}, errResponse
		}
		if s, isStr := name.(string); isStr {
			if text, in := descs.Get(s); in {
				copied := pyd.NewObject()
				for _, k := range o.Keys() {
					v, _ := o.Get(k)
					copied.Set(k, v)
				}
				copied.Set("description", text)
				withDesc[i] = copied
			}
		}
	}
	for _, c := range withDesc {
		if _, isDict := c.(*pyd.Object); !isDict {
			return DatasourceOut{}, errResponse // list[dict]
		}
	}
	keys, isList := loadOr(d.SortKeys, "[]", []any{}).([]any)
	if !isList {
		keys = []any{}
	}
	for _, k := range keys {
		if _, isStr := k.(string); !isStr {
			return DatasourceOut{}, errResponse // list[str]
		}
	}
	described, totals := countDescribed(withDesc, descs)
	return DatasourceOut{
		ID: d.ID, Name: d.Name, Description: d.Description, ProjectID: d.ProjectID, OwnerID: d.OwnerID, Bucket: d.Bucket,
		Key: d.Key, Rows: d.Rows, Columns: withDesc, ColumnDescriptions: descs, DescribedColumns: described,
		TotalColumns: totals, AIReady: aiReady(d.Description, described, totals), SortKeys: keys, Kind: d.Kind,
		FlowID: d.FlowID, ConnectionID: d.ConnectionID, SourceType: d.SourceType, SourceRef: d.SourceRef,
		RefreshedAt: isoPtr(d.RefreshedAt), Refreshing: refreshing, RefreshSchedule: d.RefreshSchedule,
		NextRefreshAt: isoPtr(d.NextRefreshAt), UpdatedAt: isoPtr(&d.UpdatedAt), Contract: contract,
	}, nil
}

// countDescribed is services/documented.py count_described.
func countDescribed(cols []any, descs *pyd.Object) (described, totals int) {
	for _, c := range cols {
		o, ok := c.(*pyd.Object)
		if !ok {
			continue
		}
		name, _ := o.Get("name")
		if !pyd.Truthy(name) {
			continue
		}
		totals++
		var text any
		if s, isStr := name.(string); isStr {
			text, _ = descs.Get(s)
		}
		if !pyd.Truthy(text) {
			text, _ = o.Get("description")
		}
		if s, isStr := text.(string); isStr && pyd.Strip(s) != "" {
			described++
		}
	}
	return
}

// aiReady is is_ai_ready.
func aiReady(description string, described, totals int) bool {
	return pyd.Strip(description) != "" && totals > 0 && described == totals
}

// datasourcesOut is _to_out_many: who is importing and the contracts' state, one query each.
func datasourcesOut(ctx context.Context, q perm.Querier, rows []datasourceRow) ([]DatasourceOut, error) {
	out := []DatasourceOut{}
	if len(rows) == 0 {
		return out, nil
	}
	ids := make([]int64, len(rows))
	for i, d := range rows {
		ids[i] = d.ID
	}
	busy := map[int64]bool{}
	r, err := q.Query(ctx, `SELECT datasource_id FROM runs WHERE datasource_id = ANY($1)
		AND kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE')`, ids)
	if err != nil {
		return nil, err
	}
	for r.Next() {
		var id *int64
		if err := r.Scan(&id); err != nil {
			r.Close()
			return nil, err
		}
		if id != nil {
			busy[*id] = true
		}
	}
	if err := r.Err(); err != nil {
		return nil, err
	}
	state, err := contractSummaries(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	for _, d := range rows {
		o, err := datasourceOut(d, busy[d.ID], state[d.ID])
		if err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, nil
}

// contractSummaries is services/contracts.py summaries: the enabled contracts' state.
func contractSummaries(ctx context.Context, q perm.Querier, ids []int64) (map[int64]*ContractSummary, error) {
	r, err := q.Query(ctx, `SELECT datasource_id, status, checked_at, errors, warnings, blocked_at IS NOT NULL, version
		FROM data_contracts WHERE datasource_id = ANY($1) AND enabled = true`, ids)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	out := map[int64]*ContractSummary{}
	for r.Next() {
		var id int64
		var c ContractSummary
		var checked *time.Time
		if err := r.Scan(&id, &c.Status, &checked, &c.Errors, &c.Warnings, &c.Blocked, &c.Version); err != nil {
			return nil, err
		}
		if c.Blocked {
			c.Status = "failed" // effective_status
		}
		c.CheckedAt = isoPtr(checked)
		out[id] = &c
	}
	return out, r.Err()
}

func init() {
	register("datasources", func(d *Deps) []server.Route {
		authed := func(route string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, route, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/datasources", Handler: authed("/datasources", listAllDatasources(d))},
			{Method: http.MethodGet, Pattern: "/datasources/search", Handler: authed("/datasources/search", searchDatasources(d))},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/datasources",
				Handler: authed("/projects/{project_id}/datasources", listProjectDatasources(d))},
		}
	})
}

func answerDatasources(d *Deps, w http.ResponseWriter, r *http.Request, rows []datasourceRow, err error) {
	if err != nil {
		fail500(d, w, "datasources", err)
		return
	}
	out, err := datasourcesOut(r.Context(), d.DB, rows)
	if err != nil {
		fail500(d, w, "datasources", err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// listAllDatasources is GET /datasources: those of the READABLE folders.
func listAllDatasources(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		readable, err := perm.For(d.DB, &p.User).Readable(r.Context())
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		if len(readable) == 0 {
			server.JSON(w, http.StatusOK, []DatasourceOut{})
			return
		}
		rows, err := queryDatasources(r.Context(), d.DB, `WHERE project_id = ANY($1) ORDER BY name`, perm.Sorted(readable))
		answerDatasources(d, w, r, rows, err)
	}
}

// Page is schemas/models.py Page.
type Page[T any] struct {
	Items []T   `json:"items"`
	Total int64 `json:"total"`
}

// searchQuery is the q/limit/offset of the paginated searches, validated as FastAPI does.
func searchQuery(r *http.Request, w http.ResponseWriter) (q string, limit, offset int64, done bool) {
	var ps pyd.Params
	query := pyd.ParseQuery(r.URL.RawQuery)
	qv := ps.Query(query, "q", pyd.Optional(pyd.Str()), nil)
	lv := ps.Query(query, "limit", pyd.Int().Ge(1).Le(200), 50)
	ov := ps.Query(query, "offset", pyd.Int().Ge(0), 0)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return "", 0, 0, true
	}
	if s, ok := qv.(string); ok {
		q = s
	}
	off, ok := ov.(int64)
	if !ok { // beyond a bigint pydantic keeps it, PostgreSQL refuses it: the 500
		server.InternalError(w)
		return "", 0, 0, true
	}
	return q, lv.(int64), off, false
}

// searchDatasources is GET /datasources/search: paginated, searched over the whole set.
func searchDatasources(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		q, limit, offset, done := searchQuery(r, w)
		if done {
			return
		}
		ctx := r.Context()
		readable, err := perm.For(d.DB, &p.User).Readable(ctx)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		if len(readable) == 0 {
			server.JSON(w, http.StatusOK, Page[DatasourceOut]{Items: []DatasourceOut{}})
			return
		}
		where, args := `WHERE project_id = ANY($1)`, []any{perm.Sorted(readable)}
		if q != "" {
			where += ` AND (name ILIKE $2 OR description ILIKE $2)`
			args = append(args, "%"+q+"%")
		}
		var total int64
		if err := d.DB.QueryRow(ctx, `SELECT count(*) FROM datasources `+where, args...).Scan(&total); err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		n := len(args)
		rows, err := queryDatasources(ctx, d.DB, where+` ORDER BY name LIMIT $`+itoa(n+1)+` OFFSET $`+itoa(n+2), append(args, limit, offset)...)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		items, err := datasourcesOut(ctx, d.DB, rows)
		if err != nil {
			fail500(d, w, "datasources", err)
			return
		}
		server.JSON(w, http.StatusOK, Page[DatasourceOut]{Items: items, Total: total})
	}
}

func itoa(n int) string { return strconv.Itoa(n) }

// listProjectDatasources is GET /projects/{project_id}/datasources.
func listProjectDatasources(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		project, done := folderOf(d, w, r, "project_id")
		if done {
			return
		}
		ref, err := perm.For(d.DB, &p.User).Ensure(r.Context(), project, perm.View)
		if refuse(d, w, ref, err) {
			return
		}
		rows, err := queryDatasources(r.Context(), d.DB, `WHERE project_id = $1 ORDER BY name`, project)
		answerDatasources(d, w, r, rows, err)
	}
}

// folderOf validates an integer path parameter naming a folder and answers its 422 or
// «Folder not found» (session.get(Project, …) is None); true when answered.
func folderOf(d *Deps, w http.ResponseWriter, r *http.Request, name string) (int64, bool) {
	var ps pyd.Params
	v := ps.Path(name, r.PathValue(name), pyd.Int())
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return 0, true
	}
	id, ok := rowID(v)
	found := false
	var err error
	if ok {
		found, err = exists(r.Context(), d.DB, "projects", id)
	}
	if err != nil {
		fail500(d, w, "folders", err)
		return 0, true
	}
	if !found {
		server.Refuse(w, notFound("Folder not found"))
		return 0, true
	}
	return id, false
}
