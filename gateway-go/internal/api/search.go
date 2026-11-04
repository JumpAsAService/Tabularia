package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// SearchHit is schemas/models.py SearchHit.
type SearchHit struct {
	Kind         string  `json:"kind"`
	ID           int64   `json:"id"`
	Name         string  `json:"name"`
	ProjectID    *int64  `json:"project_id"`
	ProjectName  *string `json:"project_name"`
	Detail       string  `json:"detail"`
	Rows         *int64  `json:"rows"`
	LockedReason *string `json:"locked_reason"`
}

// counts is SearchOut.counts: a dict, in the order the kinds first appear.
type counts struct {
	keys []string
	n    map[string]int
}

func (c *counts) add(kind string) {
	if _, ok := c.n[kind]; !ok {
		c.keys = append(c.keys, kind)
	}
	c.n[kind]++
}

func (c *counts) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, k := range c.keys {
		if i > 0 {
			b.WriteByte(',')
		}
		key, _ := json.Marshal(k)
		b.Write(key)
		b.WriteByte(':')
		b.WriteString(jsonInt(c.n[k]))
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

func jsonInt(n int) string { v, _ := json.Marshal(n); return string(v) }

// SearchOut is schemas/models.py SearchOut.
type SearchOut struct {
	Items  []SearchHit `json:"items"`
	Total  int         `json:"total"`
	Counts *counts     `json:"counts"`
}

// kindOrder is search._KIND_ORDER: the containers first, then what is opened most often.
var kindOrder = map[string]int{"folder": 0, "flow": 1, "group": 2, "datasource": 3, "view": 4, "script": 5, "sql": 6,
	"notebook": 7, "report": 8, "dashboard": 9, "connection": 10}

func init() {
	register("search", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodGet, Pattern: "/search", Handler: server.Authed(d.Auth, "/search", searchEverything(d))}}
	})
}

// likePattern is search._like: the special characters of LIKE made literal.
func likePattern(q string) string {
	return "%" + strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(q) + "%"
}

// searchEverything is routes/search.py search_everything: by NAME over every kind, each with
// its own permission (the navigable folders, the readable content, CONNECT for connections).
//
// A flow that runs containers or scripts may be shown greyed out (flow_access.blocked): that
// service belongs to the flows' port, and until it is in Go such a search, by someone who is
// not an administrator, is answered by the Python gateway.
func searchEverything(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		query := pyd.ParseQuery(r.URL.RawQuery)
		q := ps.Query(query, "q", pyd.Str(), "")
		kind := ps.Query(query, "kind", pyd.Optional(pyd.Str()), nil)
		limit := ps.Query(query, "limit", pyd.Int().Ge(1).Le(200), 30)
		offset := ps.Query(query, "offset", pyd.Int().Ge(0), 0)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		term := pyd.Strip(q.(string))
		out := SearchOut{Items: []SearchHit{}, Counts: &counts{n: map[string]int{}}}
		if term == "" {
			server.JSON(w, http.StatusOK, out)
			return
		}
		hits, forward, err := searchHits(ctx, d, p, term)
		if err != nil {
			fail(d, w, "searching", err)
			return
		}
		if forward {
			d.Upstream.ServeHTTP(w, r)
			return
		}
		for _, h := range hits {
			out.Counts.add(h.Kind)
		}
		if k, ok := kind.(string); ok && k != "" {
			kept := hits[:0]
			for _, h := range hits {
				if h.Kind == k {
					kept = append(kept, h)
				}
			}
			hits = kept
		}
		// whoever STARTS with the term first, then the kind, then the name
		lower := pyd.Lower(term)
		sort.SliceStable(hits, func(i, j int) bool {
			a, b := hits[i], hits[j]
			sa, sb := strings.HasPrefix(pyd.Lower(a.Name), lower), strings.HasPrefix(pyd.Lower(b.Name), lower)
			if sa != sb {
				return sa
			}
			ka, kb := kindRank(a.Kind), kindRank(b.Kind)
			if ka != kb {
				return ka < kb
			}
			return pyd.Lower(a.Name) < pyd.Lower(b.Name)
		})
		out.Total = len(hits)
		// hits[offset:offset + limit]
		lo := bounded(offset, len(hits))
		hi := lo + int(limit.(int64))
		if hi > len(hits) {
			hi = len(hits)
		}
		out.Items = append(out.Items, hits[lo:hi]...)
		server.JSON(w, http.StatusOK, out)
	}
}

func kindRank(k string) int {
	if r, ok := kindOrder[k]; ok {
		return r
	}
	return 9
}

// bounded is a Python slice index: an offset past the end is the end.
func bounded(offset any, n int) int {
	o, ok := offset.(int64)
	if !ok || o > int64(n) {
		return n
	}
	return int(o)
}

// searchHits runs the queries of search_everything in its order; `forward` when a flow found
// needs flow_access.blocked.
func searchHits(ctx context.Context, d *Deps, p *auth.Principal, term string) ([]SearchHit, bool, error) {
	pattern := likePattern(term)
	checker := perm.For(d.DB, &p.User)
	admin, err := checker.IsAdmin(ctx)
	if err != nil {
		return nil, false, err
	}
	readable, err := checker.Readable(ctx)
	if err != nil {
		return nil, false, err
	}
	navigable, err := checker.Visible(ctx)
	if err != nil {
		return nil, false, err
	}
	connectable, err := checker.Projects(ctx, perm.Connect)
	if err != nil {
		return nil, false, err
	}
	folderNames := map[int64]string{}
	rows, err := d.DB.Query(ctx, `SELECT id, name FROM projects`)
	if err != nil {
		return nil, false, err
	}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			rows.Close()
			return nil, false, err
		}
		folderNames[id] = name
	}
	rows.Close()
	where := func(id *int64) *string {
		if id == nil {
			return nil
		}
		if n, ok := folderNames[*id]; ok {
			return &n
		}
		return nil
	}
	var hits []SearchHit
	// each query: id, name, the folder, and an optional detail, an optional rows count
	scan := func(kind, q string, withDetail, withRows bool, args ...any) error {
		rows, err := d.DB.Query(ctx, q, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			h := SearchHit{Kind: kind}
			dest := []any{&h.ID, &h.Name, &h.ProjectID}
			var det *string
			if withDetail {
				dest = append(dest, &det)
			}
			if withRows {
				dest = append(dest, &h.Rows)
			}
			if err := rows.Scan(dest...); err != nil {
				return err
			}
			if det != nil {
				h.Detail = *det
			}
			h.ProjectName = where(h.ProjectID)
			hits = append(hits, h)
		}
		return rows.Err()
	}
	if len(navigable) > 0 {
		if err := scan("folder", `SELECT id, name, parent_id FROM projects WHERE id = ANY($1) AND name ILIKE $2 ESCAPE '\'`,
			false, false, perm.Sorted(navigable), pattern); err != nil {
			return nil, false, err
		}
	}
	if len(readable) > 0 {
		ids := perm.Sorted(readable)
		// the flows: who may not open one is told why (flow_access.blocked)
		var needsCheck bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM flows WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'
			AND (definition LIKE '%"container"%' OR definition LIKE '%"script"%'))`, ids, pattern).Scan(&needsCheck); err != nil {
			return nil, false, err
		}
		if needsCheck && !admin {
			return nil, true, nil
		}
		for _, k := range []struct{ kind, q string }{
			{"flow", `SELECT id, name, project_id, engine FROM flows WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'`},
			{"group", `SELECT id, name, project_id FROM flow_groups WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'`},
			{"datasource", `SELECT id, name, project_id, rows FROM datasources WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'`},
			{"view", `SELECT id, name, project_id FROM saved_views WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'`},
		} {
			if err := scan(k.kind, k.q, k.kind == "flow", k.kind == "datasource", ids, pattern); err != nil {
				return nil, false, err
			}
		}
	}
	scriptsOn := d.Config.ScriptsEnabled && d.Config.ScriptsNamespace != "" && d.Config.ScriptsRegistry != ""
	// scripts and notebooks: VIEW on the folder AND the use of their environment
	envs := []int64{}
	if !admin {
		rows, err := d.DB.Query(ctx, `SELECT seg.environment_id FROM script_environment_groups seg
			JOIN user_groups ug ON ug.group_id = seg.group_id WHERE ug.user_id = $1`, p.User.ID)
		if err != nil {
			return nil, false, err
		}
		if envs, err = pgx.CollectRows(rows, pgx.RowTo[int64]); err != nil {
			return nil, false, err
		}
	}
	visible := func(kind, table string, withEnv bool) error {
		q := `SELECT id, name, project_id FROM ` + table + ` WHERE name ILIKE $1 ESCAPE '\'`
		args := []any{pattern}
		if !admin {
			if len(readable) == 0 || (withEnv && len(envs) == 0) {
				return nil
			}
			q += ` AND project_id = ANY($2)`
			args = append(args, perm.Sorted(readable))
			if withEnv {
				q += ` AND environment_id = ANY($3)`
				args = append(args, envs)
			}
		}
		return scan(kind, q+` ORDER BY name`, false, false, args...)
	}
	if scriptsOn {
		if err := visible("script", "saved_scripts", true); err != nil {
			return nil, false, err
		}
	}
	if err := visible("sql", "sql_scripts", false); err != nil {
		return nil, false, err
	}
	if scriptsOn {
		if err := visible("notebook", "notebooks", true); err != nil {
			return nil, false, err
		}
	}
	for _, k := range []struct{ kind, table string }{{"report", "reports"}, {"dashboard", "dashboards"}} {
		if err := visible(k.kind, k.table, false); err != nil {
			return nil, false, err
		}
	}
	// CONNECT is orthogonal to VIEW: another set
	if len(connectable) > 0 {
		if err := scan("connection", `SELECT id, name, project_id, db_type FROM connections WHERE project_id = ANY($1) AND name ILIKE $2 ESCAPE '\'`,
			true, false, perm.Sorted(connectable), pattern); err != nil {
			return nil, false, err
		}
	}
	return hits, false, nil
}
