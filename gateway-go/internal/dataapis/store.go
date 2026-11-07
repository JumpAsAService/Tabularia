package dataapis

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	perm.Querier
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// Now is naive UTC, as every time of the gateway's tables.
func Now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

// API is a row of data_apis.
type API struct {
	ID             int64
	Name           string
	Slug           string
	Description    string
	ProjectID      int64
	CreatorID      int64
	State          string
	Runtime        Runtime
	CurrentVersion int64
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

const apiColumns = `id, name, slug, description, project_id, creator_id, state, runtime, current_version, created_at, updated_at`

func scanAPI(row pgx.Row) (*API, error) {
	var a API
	var runtime string
	if err := row.Scan(&a.ID, &a.Name, &a.Slug, &a.Description, &a.ProjectID, &a.CreatorID, &a.State, &runtime,
		&a.CurrentVersion, &a.CreatedAt, &a.UpdatedAt); err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(runtime), &a.Runtime) // an unreadable runtime is the zero one: the page shows it to be set again
	return &a, nil
}

// Get is the data API with this id; nil when there is none.
func Get(ctx context.Context, q Querier, id int64) (*API, error) {
	a, err := scanAPI(q.QueryRow(ctx, `SELECT `+apiColumns+` FROM data_apis WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

// Visible are the data APIs this person sees (VIEW on their folder; an administrator every
// one), in `projects` when not nil, by name.
func Visible(ctx context.Context, q Querier, c *perm.Checker, projects []int64) ([]*API, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	sql := `SELECT ` + apiColumns + ` FROM data_apis`
	var args []any
	if !admin {
		readable, err := c.Readable(ctx)
		if err != nil {
			return nil, err
		}
		if projects != nil {
			only := map[int64]bool{}
			for _, p := range projects {
				if readable[p] {
					only[p] = true
				}
			}
			readable = only
		}
		if len(readable) == 0 {
			return []*API{}, nil
		}
		sql += ` WHERE project_id = ANY($1)`
		args = append(args, perm.Sorted(readable))
	} else if projects != nil {
		sql += ` WHERE project_id = ANY($1)`
		args = append(args, projects)
	}
	rows, err := q.Query(ctx, sql+` ORDER BY name, id`, args...)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (*API, error) { return scanAPI(r) })
	if out == nil {
		out = []*API{}
	}
	return out, err
}

// Resources are a data API's current resources, in their order.
func Resources(ctx context.Context, q Querier, apiID int64) ([]Resource, error) {
	rows, err := q.Query(ctx, `SELECT name, source_kind, datasource_id, columns, filters, default_sort, max_page
		FROM data_api_resources WHERE data_api_id = $1 ORDER BY position, id`, apiID)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Resource, error) {
		var r Resource
		var ds *int64
		var cols, filters, sort string
		if err := row.Scan(&r.Name, &r.Source.Kind, &ds, &cols, &filters, &sort, &r.MaxPage); err != nil {
			return r, err
		}
		if ds != nil {
			r.Source.ID = *ds
		}
		_ = json.Unmarshal([]byte(cols), &r.Columns)
		_ = json.Unmarshal([]byte(filters), &r.Filters)
		_ = json.Unmarshal([]byte(sort), &r.DefaultSort)
		return r, nil
	})
	if out == nil {
		out = []Resource{}
	}
	return out, err
}

// CurrentDefinition is what the data API publishes now.
func CurrentDefinition(ctx context.Context, q Querier, a *API) (Definition, error) {
	res, err := Resources(ctx, q, a.ID)
	return Definition{Resources: res, Runtime: a.Runtime}, err
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

func dumps(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

// Insert makes a data API, in the draft state, with its first version (`note`).
func Insert(ctx context.Context, tx pgx.Tx, name, slug, description string, projectID, creatorID int64, def Definition,
	note string) (*API, error) {
	now := Now()
	a, err := scanAPI(tx.QueryRow(ctx, `INSERT INTO data_apis (name, slug, description, project_id, creator_id, state, runtime,
		current_version, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $8) RETURNING `+apiColumns,
		name, slug, description, projectID, creatorID, Draft, dumps(def.Runtime), now))
	if err != nil {
		return nil, err
	}
	if err := SaveDefinition(ctx, tx, a, def, note, creatorID); err != nil {
		return nil, err
	}
	return a, nil
}

// SaveDefinition writes a new version of the definition, which becomes the current one: the
// resources' rows (kept by name, so a key's resources stay theirs) and the runtime.
func SaveDefinition(ctx context.Context, tx pgx.Tx, a *API, def Definition, note string, userID int64) error {
	if _, err := tx.Exec(ctx, `SELECT id FROM data_apis WHERE id = $1 FOR UPDATE`, a.ID); err != nil {
		return err
	}
	for i := range def.Resources {
		r := &def.Resources[i]
		r.Columns, r.Filters, r.DefaultSort = nonNil(r.Columns), nonNil(r.Filters), nonNil(r.DefaultSort)
	}
	def.Resources = nonNil(def.Resources)
	var last int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(max(version), 0) FROM data_api_versions WHERE data_api_id = $1`, a.ID).Scan(&last); err != nil {
		return err
	}
	now := Now()
	if _, err := tx.Exec(ctx, `INSERT INTO data_api_versions (data_api_id, version, content, note, created_at, created_by)
		VALUES ($1, $2, $3, $4, $5, $6)`, a.ID, last+1, dumps(def), note, now, userID); err != nil {
		return err
	}
	names := make([]string, len(def.Resources))
	for i, r := range def.Resources {
		names[i] = r.Name
	}
	if _, err := tx.Exec(ctx, `DELETE FROM data_api_resources WHERE data_api_id = $1 AND NOT (name = ANY($2))`, a.ID, names); err != nil {
		return err
	}
	for i, r := range def.Resources {
		if _, err := tx.Exec(ctx, `INSERT INTO data_api_resources (data_api_id, position, name, source_kind, datasource_id, columns,
			filters, default_sort, max_page) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
			ON CONFLICT (data_api_id, name) DO UPDATE SET position = EXCLUDED.position, source_kind = EXCLUDED.source_kind,
			datasource_id = EXCLUDED.datasource_id, columns = EXCLUDED.columns, filters = EXCLUDED.filters,
			default_sort = EXCLUDED.default_sort, max_page = EXCLUDED.max_page`,
			a.ID, i, r.Name, r.Source.Kind, r.Source.ID, dumps(r.Columns), dumps(r.Filters), dumps(r.DefaultSort), r.MaxPage); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(ctx, `UPDATE data_apis SET current_version = $2, runtime = $3, updated_at = $4 WHERE id = $1`,
		a.ID, last+1, dumps(def.Runtime), now); err != nil {
		return err
	}
	a.CurrentVersion, a.Runtime, a.UpdatedAt = last+1, def.Runtime, now
	return nil
}

// Version is a row of data_api_versions.
type Version struct {
	Version   int64
	Note      string
	CreatedAt time.Time
	CreatedBy *string // the email of who saved it, while the account exists
	Content   Definition
}

// Versions are a data API's versions, newest first, without their content.
func Versions(ctx context.Context, q Querier, apiID int64) ([]Version, error) {
	rows, err := q.Query(ctx, `SELECT v.version, v.note, v.created_at, u.email FROM data_api_versions v
		LEFT JOIN users u ON u.id = v.created_by WHERE v.data_api_id = $1 ORDER BY v.version DESC`, apiID)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (Version, error) {
		var v Version
		return v, r.Scan(&v.Version, &v.Note, &v.CreatedAt, &v.CreatedBy)
	})
	if out == nil {
		out = []Version{}
	}
	return out, err
}

// VersionContent is a version's definition; false when the data API has no such version.
func VersionContent(ctx context.Context, q Querier, apiID, version int64) (Definition, bool, error) {
	var text string
	err := q.QueryRow(ctx, `SELECT content FROM data_api_versions WHERE data_api_id = $1 AND version = $2`, apiID, version).Scan(&text)
	if errors.Is(err, pgx.ErrNoRows) {
		return Definition{}, false, nil
	}
	if err != nil {
		return Definition{}, false, err
	}
	var def Definition
	_ = json.Unmarshal([]byte(text), &def)
	def.Resources = nonNil(def.Resources)
	return def, true, nil
}

// SetState changes the state; nothing else.
func SetState(ctx context.Context, q Querier, id int64, state string) error {
	_, err := q.Exec(ctx, `UPDATE data_apis SET state = $2, updated_at = $3 WHERE id = $1`, id, state, Now())
	return err
}

// Delete removes the data API: its versions, resources, keys and usage go with it.
func Delete(ctx context.Context, q Querier, id int64) error {
	_, err := q.Exec(ctx, `DELETE FROM data_apis WHERE id = $1`, id)
	return err
}

// Count is how many data APIs the installation has.
func Count(ctx context.Context, q Querier) (int, error) {
	var n int
	err := q.QueryRow(ctx, `SELECT count(*) FROM data_apis`).Scan(&n)
	return n, err
}

// ActivePods is the most pods the active data APIs may run together, but for `except`.
func ActivePods(ctx context.Context, q Querier, except int64) (int, error) {
	rows, err := q.Query(ctx, `SELECT runtime FROM data_apis WHERE state = $1 AND id <> $2`, Active, except)
	if err != nil {
		return 0, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return 0, err
	}
	n := 0
	for _, text := range list {
		var rt Runtime
		_ = json.Unmarshal([]byte(text), &rt)
		n += rt.Pods()
	}
	return n, nil
}

// SlugTaken says whether another data API has this address.
func SlugTaken(ctx context.Context, q Querier, slug string, except int64) (bool, error) {
	var found bool
	err := q.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM data_apis WHERE slug = $1 AND id <> $2)`, slug, except).Scan(&found)
	return found, err
}

// NameTaken says whether another data API of the folder has this name.
func NameTaken(ctx context.Context, q Querier, projectID int64, name string, except int64) (bool, error) {
	var found bool
	err := q.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM data_apis WHERE project_id = $1 AND name = $2 AND id <> $3)`,
		projectID, name, except).Scan(&found)
	return found, err
}

// PublishersOf are the names of the data APIs a datasource is published by, at most `limit`.
func PublishersOf(ctx context.Context, q Querier, datasourceID int64, limit int) ([]string, error) {
	rows, err := q.Query(ctx, `SELECT DISTINCT a.name FROM data_api_resources r JOIN data_apis a ON a.id = r.data_api_id
		WHERE r.datasource_id = $1 ORDER BY a.name LIMIT $2`, datasourceID, limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

// Datasources reads what the checks need of these datasources (the ones that exist).
func Datasources(ctx context.Context, q Querier, ids []int64) (map[int64]*Datasource, error) {
	out := map[int64]*Datasource{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := q.Query(ctx, `SELECT id, name, project_id, columns FROM datasources WHERE id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var ds Datasource
		var cols string
		if err := rows.Scan(&ds.ID, &ds.Name, &ds.ProjectID, &cols); err != nil {
			return nil, err
		}
		var raw []struct {
			Name  string `json:"name"`
			Dtype string `json:"dtype"`
		}
		_ = json.Unmarshal([]byte(cols), &raw)
		for _, c := range raw {
			ds.Columns = append(ds.Columns, Column{Name: c.Name, Type: c.Dtype})
		}
		out[ds.ID] = &ds
	}
	return out, rows.Err()
}

// ── keys ──────────────────────────────────────────────────────────────────────

// Key is a row of data_api_keys: never its hash.
type Key struct {
	ID            int64
	DataAPIID     int64
	Name          string
	Hint          string
	ExpiresAt     time.Time
	RatePerMinute int
	Resources     []string
	CIDRs         []string
	RevokedAt     *time.Time
	GraceUntil    *time.Time
	CreatedBy     *int64
	CreatedAt     time.Time
	LastUsedAt    *time.Time
}

const keyColumns = `id, data_api_id, name, prefix, expires_at, rate_per_minute, resources, cidrs, revoked_at, grace_until,
	created_by, created_at, last_used_at`

func scanKey(row pgx.Row) (*Key, error) {
	var k Key
	var res, cidrs string
	if err := row.Scan(&k.ID, &k.DataAPIID, &k.Name, &k.Hint, &k.ExpiresAt, &k.RatePerMinute, &res, &cidrs, &k.RevokedAt,
		&k.GraceUntil, &k.CreatedBy, &k.CreatedAt, &k.LastUsedAt); err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(res), &k.Resources)
	_ = json.Unmarshal([]byte(cidrs), &k.CIDRs)
	k.Resources, k.CIDRs = nonNil(k.Resources), nonNil(k.CIDRs)
	return &k, nil
}

// Keys are a data API's keys, newest first.
func Keys(ctx context.Context, q Querier, apiID int64) ([]*Key, error) {
	rows, err := q.Query(ctx, `SELECT `+keyColumns+` FROM data_api_keys WHERE data_api_id = $1 ORDER BY created_at DESC, id DESC`, apiID)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (*Key, error) { return scanKey(r) })
	if out == nil {
		out = []*Key{}
	}
	return out, err
}

// KeyOf is the key `keyID` of the data API `apiID`; nil when it has none such.
func KeyOf(ctx context.Context, q Querier, apiID, keyID int64) (*Key, error) {
	k, err := scanKey(q.QueryRow(ctx, `SELECT `+keyColumns+` FROM data_api_keys WHERE id = $1 AND data_api_id = $2`, keyID, apiID))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return k, err
}

// NewKeyRow is what a key is made with.
type NewKeyRow struct {
	DataAPIID     int64
	Name          string
	ExpiresAt     time.Time
	RatePerMinute int
	Resources     []string
	CIDRs         []string
	CreatedBy     int64
}

// InsertKey makes a key: the row, and the key itself, returned once and never kept.
func InsertKey(ctx context.Context, q Querier, n NewKeyRow) (*Key, string, error) {
	key, hint, hash, err := NewKey()
	if err != nil {
		return nil, "", err
	}
	k, err := scanKey(q.QueryRow(ctx, `INSERT INTO data_api_keys (data_api_id, name, prefix, hash, expires_at, rate_per_minute,
		resources, cidrs, created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING `+keyColumns,
		n.DataAPIID, n.Name, hint, hash, n.ExpiresAt, n.RatePerMinute, dumps(nonNil(n.Resources)), dumps(nonNil(n.CIDRs)),
		n.CreatedBy, Now()))
	if err != nil {
		return nil, "", err
	}
	return k, key, nil
}

// RevokeKey stops a key now (its grace, if any, with it).
func RevokeKey(ctx context.Context, q Querier, keyID int64) error {
	_, err := q.Exec(ctx, `UPDATE data_api_keys SET revoked_at = $2, grace_until = NULL WHERE id = $1 AND revoked_at IS NULL`, keyID, Now())
	return err
}

// EndAt keeps a renewed key working until `until` (no later than its expiry); at once
// (`until` not after now) it is revoked.
func EndAt(ctx context.Context, q Querier, k *Key, until time.Time) error {
	now := Now()
	if !until.After(now) {
		return RevokeKey(ctx, q, k.ID)
	}
	if until.After(k.ExpiresAt) {
		until = k.ExpiresAt
	}
	_, err := q.Exec(ctx, `UPDATE data_api_keys SET grace_until = $2 WHERE id = $1`, k.ID, until)
	return err
}

// Requests24h are the requests of the last 24 hours of each key of a data API.
func Requests24h(ctx context.Context, q Querier, apiID int64) (map[int64]int64, error) {
	rows, err := q.Query(ctx, `SELECT key_id, sum(requests)::bigint FROM data_api_usage WHERE data_api_id = $1 AND minute > $2
		GROUP BY key_id`, apiID, Now().Add(-24*time.Hour))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]int64{}
	for rows.Next() {
		var id, n int64
		if err := rows.Scan(&id, &n); err != nil {
			return nil, err
		}
		out[id] = n
	}
	return out, rows.Err()
}
