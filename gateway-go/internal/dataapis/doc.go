package dataapis

// A data API's documentation, as its page shows it with no key and no pod: the description the
// pods serve (dataapiwire.OpenAPI), from the definition, the columns typed by their datasources;
// and the administration's list of every data API.

import (
	"context"
	"encoding/json"
	"strconv"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Doc is what the description of `a` is made of, and the kinds of its columns from their
// datasources (a column no longer in its datasource is not typed, as the pods say before the
// data is prepared).
func Doc(a *API, def Definition, sources map[int64]*Datasource) (dataapiwire.Doc, dataapiwire.KindOf) {
	doc := dataapiwire.Doc{Name: a.Name, Slug: a.Slug, Version: strconv.FormatInt(a.CurrentVersion, 10),
		Resources: make([]dataapiwire.Resource, 0, len(def.Resources))}
	types := map[string]map[string]string{}
	for _, r := range def.Resources {
		doc.Resources = append(doc.Resources, WireResource(r))
		cols := map[string]string{}
		if ds := sources[r.Source.ID]; ds != nil {
			for _, c := range ds.Columns {
				cols[c.Name] = c.Type
			}
		}
		types[r.Name] = cols
	}
	kindOf := func(resource, column string) (string, bool) {
		typ, ok := types[resource][column]
		if !ok {
			return "", false
		}
		return dataapiwire.KindOfPolars(typ), true
	}
	return doc, kindOf
}

// WireResource is a resource's definition as the pods read it (without its data's version and
// link, which the manifest adds).
func WireResource(r Resource) dataapiwire.Resource {
	out := dataapiwire.Resource{Name: r.Name, Columns: nonNil(r.Columns), Filters: []dataapiwire.Filter{},
		DefaultSort: nonNil(r.DefaultSort), MaxPage: r.MaxPage}
	for _, f := range r.Filters {
		out.Filters = append(out.Filters, dataapiwire.Filter{Column: f.Column, Operators: nonNil(f.Operators)})
	}
	return out
}

// AdminRow is a data API as the administration lists it: who made it, how many resources and
// answering keys it has, its pods as the reconcile loop last saw them, and its traffic of the
// last 24 hours.
type AdminRow struct {
	*API
	Resources  int
	Keys       int
	Status     *ClusterStatus
	CheckedAt  *time.Time
	Requests   int64
	Errors     int64
	Throttled  int64
	LastUsedAt *time.Time
}

// AdminList is every data API of the installation, by name.
func AdminList(ctx context.Context, q Querier, now time.Time) ([]*AdminRow, error) {
	rows, err := q.Query(ctx, `SELECT `+apiColumns+` FROM data_apis ORDER BY name, id`)
	if err != nil {
		return nil, err
	}
	out := []*AdminRow{}
	byID := map[int64]*AdminRow{}
	for rows.Next() {
		a, err := scanAPI(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		r := &AdminRow{API: a}
		out = append(out, r)
		byID[a.ID] = r
	}
	rows.Close()
	if err := rows.Err(); err != nil || len(out) == 0 {
		return out, err
	}
	ids := make([]int64, 0, len(out))
	for _, r := range out {
		ids = append(ids, r.ID)
	}
	each := func(sql string, args []any, scan func(pgxRow) error) error {
		rows, err := q.Query(ctx, sql, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			if err := scan(rows); err != nil {
				return err
			}
		}
		return rows.Err()
	}
	if err := each(`SELECT data_api_id, count(*) FROM data_api_resources WHERE data_api_id = ANY($1) GROUP BY data_api_id`,
		[]any{ids}, func(r pgxRow) error {
			var id int64
			var n int
			if err := r.Scan(&id, &n); err != nil {
				return err
			}
			byID[id].Resources = n
			return nil
		}); err != nil {
		return nil, err
	}
	// the keys that answer now: not revoked, not expired, not past their grace
	if err := each(`SELECT data_api_id, count(*), max(last_used_at) FROM data_api_keys WHERE data_api_id = ANY($1)
		AND revoked_at IS NULL AND expires_at > $2 AND (grace_until IS NULL OR grace_until > $2) GROUP BY data_api_id`,
		[]any{ids, now}, func(r pgxRow) error {
			var id int64
			var n int
			var last *time.Time
			if err := r.Scan(&id, &n, &last); err != nil {
				return err
			}
			byID[id].Keys, byID[id].LastUsedAt = n, utcPtr(last)
			return nil
		}); err != nil {
		return nil, err
	}
	if err := each(`SELECT data_api_id, cluster, checked_at FROM data_api_status WHERE data_api_id = ANY($1)`,
		[]any{ids}, func(r pgxRow) error {
			var id int64
			var cluster string
			var at time.Time
			if err := r.Scan(&id, &cluster, &at); err != nil {
				return err
			}
			var s ClusterStatus
			if json.Unmarshal([]byte(cluster), &s) == nil {
				byID[id].Status = &s
			}
			at = at.UTC()
			byID[id].CheckedAt = &at
			return nil
		}); err != nil {
		return nil, err
	}
	if err := each(`SELECT data_api_id, coalesce(sum(requests), 0), coalesce(sum(errors), 0), coalesce(sum(throttled), 0)
		FROM data_api_usage WHERE data_api_id = ANY($1) AND minute >= $2 GROUP BY data_api_id`,
		[]any{ids, now.Add(-24 * time.Hour)}, func(r pgxRow) error {
			var id, req, errs, thr int64
			if err := r.Scan(&id, &req, &errs, &thr); err != nil {
				return err
			}
			byID[id].Requests, byID[id].Errors, byID[id].Throttled = req, errs, thr
			return nil
		}); err != nil {
		return nil, err
	}
	return out, nil
}

type pgxRow interface{ Scan(dest ...any) error }

func utcPtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	u := t.UTC()
	return &u
}
