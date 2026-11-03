package api

import (
	"net/http"
	"strconv"
	"strings"
	"time"

	"golang.org/x/text/cases"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// ObjectRow is ReportRow and DashboardRow: a row of the tab, where it lives, who made it, who
// reads it.
type ObjectRow struct {
	ObjectOut
	FolderPath   []string `json:"folder_path"` // from the root down to its folder
	OwnerName    *string  `json:"owner_name"`  // full name, else email; null when the account is gone
	People30d    int64    `json:"people_30d"`
	Opens30d     int64    `json:"opens_30d"`
	ViewsTotal   int64    `json:"views_total"`
	LastViewedAt *string  `json:"last_viewed_at"`
}

// page is schemas/models.py Page.
type page struct {
	Items []ObjectRow `json:"items"`
	Total int64       `json:"total"`
}

// windowDays is report_views.WINDOW_DAYS: today and the 29 days before it.
const windowDays = 30

// likePattern is routes/search.py _like: the searched text with LIKE's characters made literal.
func likePattern(q string) string {
	r := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	return "%" + r.Replace(q) + "%"
}

// search is GET /reports/search (search_reports, search_dashboards): the ones this person
// sees, a page at a time; `trending` first those opened by the most people in the window.
func (h *objectsAPI) search(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var params pyd.Params
	query := pyd.ParseQuery(r.URL.RawQuery)
	qv := params.Query(query, "q", pyd.Optional(pyd.Str()), nil)
	sort := params.Query(query, "sort", pyd.Literal("trending", "name", "updated"), "trending")
	limit := params.Query(query, "limit", pyd.Int().Ge(1).Le(200), 50)
	offset := params.Query(query, "offset", pyd.Int().Ge(0), 0)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	ctx, c := r.Context(), perm.For(h.d.DB, &p.User)
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	k := h.kind
	day := time.Now().UTC().In(h.d.Config.AppLocation)
	start := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, time.UTC).AddDate(0, 0, -(windowDays - 1))
	from := ` FROM ` + k.Table + ` o LEFT OUTER JOIN (SELECT ` + k.FK + ` AS oid, count(DISTINCT user_id) AS people,
		count(id) AS opens FROM ` + k.Views + ` WHERE day >= $1 GROUP BY ` + k.FK + `) recent ON recent.oid = o.id`
	args := []any{start.Format("2006-01-02")}
	var where []string
	if !admin {
		readable, err := c.Readable(ctx)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		if len(readable) == 0 {
			server.JSON(w, http.StatusOK, page{Items: []ObjectRow{}, Total: 0})
			return
		}
		args = append(args, perm.Sorted(readable))
		where = append(where, `o.project_id = ANY($2)`)
	}
	parents, err := c.Parents(ctx)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	names := map[int64]string{}
	rows, err := h.d.DB.Query(ctx, `SELECT id, name FROM projects`)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	for rows.Next() {
		var id int64
		var n string
		if err := rows.Scan(&id, &n); err != nil {
			rows.Close()
			h.fail(w, r, err)
			return
		}
		names[id] = n
	}
	rows.Close()
	if q, ok := qv.(string); ok && pystr.Strip(q) != "" {
		text := pystr.Strip(q)
		fold := cases.Fold()
		needle := fold.String(text)
		var in []int64
		for pid := range names {
			for _, a := range perm.Ancestors(parents, pid) {
				if n, ok := names[a]; ok && strings.Contains(fold.String(n), needle) {
					in = append(in, pid)
					break
				}
			}
		}
		if in == nil {
			in = []int64{}
		}
		n := len(args)
		args = append(args, likePattern(text), in)
		where = append(where, `(o.name ILIKE $`+itoa(n+1)+` ESCAPE '\' OR o.description ILIKE $`+itoa(n+1)+` ESCAPE '\'
			OR o.project_id = ANY($`+itoa(n+2)+`))`)
	}
	if len(where) > 0 {
		from += ` WHERE ` + strings.Join(where, ` AND `)
	}
	var total int64
	if err := h.d.DB.QueryRow(ctx, `SELECT count(*)`+from, args...).Scan(&total); err != nil {
		h.fail(w, r, err)
		return
	}
	order := map[string]string{
		"trending": `coalesce(recent.people, 0) DESC, coalesce(recent.opens, 0) DESC, o.views_total DESC, o.updated_at DESC, o.id DESC`,
		"name":     `lower(o.name), o.id`,
		"updated":  `o.updated_at DESC, o.id DESC`,
	}[sort.(string)]
	n := len(args)
	args = append(args, limit, offset)
	rows, err = h.d.DB.Query(ctx, `SELECT o.id, o.name, o.description, o.project_id, o.owner_id, o.current_version, o.created_at,
		o.updated_at, o.views_total, o.last_viewed_at, coalesce(recent.people, 0), coalesce(recent.opens, 0)`+from+
		` ORDER BY `+order+` LIMIT $`+itoa(n+1)+` OFFSET $`+itoa(n+2), args...)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	type found struct {
		it            versioned.Item
		people, opens int64
	}
	var list []found
	for rows.Next() {
		var f found
		it := &f.it
		if err := rows.Scan(&it.ID, &it.Name, &it.Description, &it.ProjectID, &it.OwnerID, &it.CurrentVersion, &it.CreatedAt,
			&it.UpdatedAt, &it.ViewsTotal, &it.LastViewedAt, &f.people, &f.opens); err != nil {
			rows.Close()
			h.fail(w, r, err)
			return
		}
		list = append(list, f)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		h.fail(w, r, err)
		return
	}
	ids := make([]int64, len(list))
	var owners []int64
	for i, f := range list {
		ids[i] = f.it.ID
		if f.it.OwnerID != nil {
			owners = append(owners, *f.it.OwnerID)
		}
	}
	sizes, err := k.SizesOf(ctx, h.d.DB, ids)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	shown, err := shownEmail(ctx, c)
	if err != nil {
		h.fail(w, r, err)
		return
	}
	ownerName := map[int64]string{}
	if len(owners) > 0 {
		rows, err = h.d.DB.Query(ctx, `SELECT id, full_name, email FROM users WHERE id = ANY($1)`, owners)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		for rows.Next() {
			var id int64
			var full *string
			var email string
			if err := rows.Scan(&id, &full, &email); err != nil {
				rows.Close()
				h.fail(w, r, err)
				return
			}
			if full == nil || *full == "" {
				ownerName[id] = shown(email)
			} else {
				ownerName[id] = *full
			}
		}
		rows.Close()
	}
	items := []ObjectRow{}
	for _, f := range list {
		s, ok := sizes[f.it.ID]
		if !ok {
			s = make([]int64, len(k.Sizes))
		}
		o, err := h.out(ctx, c, &f.it, s)
		if err != nil {
			h.fail(w, r, err)
			return
		}
		chain := perm.Ancestors(parents, f.it.ProjectID)
		path := []string{}
		for i := len(chain) - 1; i >= 0; i-- {
			if n, ok := names[chain[i]]; ok {
				path = append(path, n)
			}
		}
		row := ObjectRow{ObjectOut: o, FolderPath: path, People30d: f.people, Opens30d: f.opens, ViewsTotal: f.it.ViewsTotal}
		if f.it.OwnerID != nil {
			if n, ok := ownerName[*f.it.OwnerID]; ok {
				row.OwnerName = &n
			}
		}
		if f.it.LastViewedAt != nil {
			s := pyjson.Isoformat(*f.it.LastViewedAt)
			row.LastViewedAt = &s
		}
		items = append(items, row)
	}
	server.JSON(w, http.StatusOK, page{Items: items, Total: total})
}

func itoa(n int) string { return strconv.Itoa(n) }
