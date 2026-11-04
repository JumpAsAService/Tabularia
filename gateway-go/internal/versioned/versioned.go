// Package versioned is what services/reports.py and services/dashboards.py share: an object of
// a folder kept with its versions (models/report.py, models/dashboard.py), seen with VIEW on its
// folder (administrators: every one; whoever cannot see it gets 404, as if it did not exist),
// and the openings of its viewer (services/report_views.py).
package versioned

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Kind names the tables of one kind of object.
type Kind struct {
	Label    string // "report": the audit's target type
	Table    string // "reports"
	Versions string // "report_versions"
	FK       string // "report_id", in the versions and the views
	Views    string // "report_views"
	NotFound string // get_visible's 404
	// Sizes are the definition's lists sizes_of counts, in order
	Sizes []string
	// Empty is the definition of an object without a readable version (empty())
	Empty func() pyjson.Object
}

// Item is the object's row.
type Item struct {
	ID             int64
	Name           string
	Description    string
	ProjectID      int64
	OwnerID        *int64
	CurrentVersion int64
	CreatedAt      time.Time
	UpdatedAt      time.Time
	ViewsTotal     int64
	LastViewedAt   *time.Time
}

const columns = `id, name, description, project_id, owner_id, current_version, created_at, updated_at, views_total, last_viewed_at`

func scan(row pgx.Row) (*Item, error) {
	var r Item
	err := row.Scan(&r.ID, &r.Name, &r.Description, &r.ProjectID, &r.OwnerID, &r.CurrentVersion, &r.CreatedAt, &r.UpdatedAt,
		&r.ViewsTotal, &r.LastViewedAt)
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// Get is session.get: nil when there is none.
func (k *Kind) Get(ctx context.Context, q perm.Querier, id int64) (*Item, error) {
	r, err := scan(q.QueryRow(ctx, `SELECT `+columns+` FROM `+k.Table+` WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

// CanSee is can_see.
func CanSee(ctx context.Context, c *perm.Checker, r *Item) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	return c.Has(ctx, r.ProjectID, perm.View)
}

// GetVisible is get_visible: the object, or the 404 of one that does not exist.
func (k *Kind) GetVisible(ctx context.Context, q perm.Querier, c *perm.Checker, id int64) (*Item, *auth.Refusal, error) {
	r, err := k.Get(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	notFound := &auth.Refusal{Status: http.StatusNotFound, Detail: k.NotFound}
	if r == nil {
		return nil, notFound, nil
	}
	ok, err := CanSee(ctx, c, r)
	if err != nil {
		return nil, nil, err
	}
	if !ok {
		return nil, notFound, nil
	}
	return r, nil, nil
}

// Visible is visible_query: the objects this person sees (in `projects` when not nil), by
// name; none when there are none.
func (k *Kind) Visible(ctx context.Context, q perm.Querier, c *perm.Checker, projects []int64) ([]*Item, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	sql := `SELECT ` + columns + ` FROM ` + k.Table
	var args []any
	if !admin {
		readable, err := c.Readable(ctx)
		if err != nil {
			return nil, err
		}
		if projects != nil {
			in := map[int64]bool{}
			for _, p := range projects {
				if readable[p] {
					in[p] = true
				}
			}
			readable = in
		}
		if len(readable) == 0 {
			return nil, nil
		}
		sql += ` WHERE project_id = ANY($1)`
		args = append(args, perm.Sorted(readable))
	} else if projects != nil {
		sql += ` WHERE project_id = ANY($1)`
		args = append(args, projects)
	}
	rows, err := q.Query(ctx, sql+` ORDER BY name`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Item
	for rows.Next() {
		r, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// SizesOf is sizes_of: the lengths of the current versions' lists (k.Sizes), counted by the
// database: a definition holds its images, and a list has no use for them.
func (k *Kind) SizesOf(ctx context.Context, q perm.Querier, ids []int64) (map[int64][]int64, error) {
	out := map[int64][]int64{}
	if len(ids) == 0 {
		return out, nil
	}
	sql := `SELECT v.` + k.FK
	for _, key := range k.Sizes {
		part := `CAST(v.content AS JSON) -> '` + key + `'`
		sql += `, CASE WHEN json_typeof(` + part + `) = 'array' THEN json_array_length(` + part + `) ELSE 0 END`
	}
	sql += ` FROM ` + k.Versions + ` v JOIN ` + k.Table + ` o ON o.id = v.` + k.FK + ` AND o.current_version = v.version
		WHERE o.id = ANY($1)`
	rows, err := q.Query(ctx, sql, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		sizes := make([]int64, len(k.Sizes))
		dest := []any{&id}
		for i := range sizes {
			dest = append(dest, &sizes[i])
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, err
		}
		out[id] = sizes
	}
	return out, rows.Err()
}

// Version is a row of the versions.
type Version struct {
	Version   int64
	Content   string
	Note      string
	CreatedAt time.Time
	CreatedBy *int64
}

// VersionRow is version_row: a version of the object (nil: the current one); nil when it has
// none such.
func (k *Kind) VersionRow(ctx context.Context, q perm.Querier, r *Item, version *int64) (*Version, error) {
	v := r.CurrentVersion
	if version != nil {
		v = *version
	}
	var row Version
	err := q.QueryRow(ctx, `SELECT version, content, note, created_at, created_by FROM `+k.Versions+`
		WHERE `+k.FK+` = $1 AND version = $2 LIMIT 1`, r.ID, v).
		Scan(&row.Version, &row.Content, &row.Note, &row.CreatedAt, &row.CreatedBy)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &row, nil
}

// ErrNotAnObject is a stored definition that reads as JSON but not as an object: the Python
// code fails where it reads it as one (`content.get`), a 500.
var ErrNotAnObject = errors.New("a definition is not a JSON object")

// Parsed is _parsed: the version's definition as json.loads reads it (an object, or whatever
// else the text holds), the empty one when there is no version or its text is not JSON.
func (k *Kind) Parsed(row *Version) any {
	if row == nil {
		return k.Empty()
	}
	v, err := pyjson.Loads(row.Content)
	if err != nil {
		return k.Empty()
	}
	return v
}

// ContentOf is content_of: a version's definition (nil: the current one).
func (k *Kind) ContentOf(ctx context.Context, q perm.Querier, r *Item, version *int64) (any, error) {
	row, err := k.VersionRow(ctx, q, r, version)
	if err != nil {
		return nil, err
	}
	return k.Parsed(row), nil
}

// Object is the definition where the Python code reads it as a dict.
func Object(v any) (pyjson.Object, error) {
	obj, ok := v.(pyjson.Object)
	if !ok {
		return nil, ErrNotAnObject
	}
	return obj, nil
}

// Dumps is json.dumps(content) as a version is stored: ASCII, «, » and «: ».
func Dumps(v any) (string, error) { return pyjson.Dumps(v, pyjson.Options{EnsureASCII: true}) }

// AddVersion is add_version in `tx`: the next version, which becomes the current one; the
// object's row stays locked until the commit, so that two saves take the next numbers in turn.
func (k *Kind) AddVersion(ctx context.Context, tx pgx.Tx, r *Item, content any, note string, userID int64) (int64, error) {
	if _, err := tx.Exec(ctx, `SELECT id FROM `+k.Table+` WHERE id = $1 FOR UPDATE`, r.ID); err != nil {
		return 0, err
	}
	var last int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(max(version), 0) FROM `+k.Versions+` WHERE `+k.FK+` = $1`, r.ID).Scan(&last); err != nil {
		return 0, err
	}
	text, err := Dumps(content)
	if err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO `+k.Versions+` (`+k.FK+`, version, content, note, created_at, created_by)
		VALUES ($1, $2, $3, $4, $5, $6)`, r.ID, last+1, text, note, time.Now().UTC(), userID); err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, `UPDATE `+k.Table+` SET current_version = $2 WHERE id = $1`, r.ID, last+1); err != nil {
		return 0, err
	}
	r.CurrentVersion = last + 1
	return last + 1, nil
}

// RecordView is report_views.record (dashboards.record_open): `userID` opened the object now.
// A person counts once a day, the day being the installation's (`loc`); the latest opening is
// noted either way. Commits.
func (k *Kind) RecordView(ctx context.Context, db interface {
	Begin(context.Context) (pgx.Tx, error)
}, loc *time.Location, id, userID int64) error {
	now := time.Now().UTC()
	day := now.In(loc).Format("2006-01-02")
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var seen int64
	err = tx.QueryRow(ctx, `SELECT id FROM `+k.Views+` WHERE `+k.FK+` = $1 AND user_id = $2 AND day = $3 LIMIT 1`,
		id, userID, day).Scan(&seen)
	counted := false
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		// the same opening at the same moment, from another tab: not counted twice
		tag, err := tx.Exec(ctx, `INSERT INTO `+k.Views+` (`+k.FK+`, user_id, day, at) VALUES ($1, $2, $3, $4)
			ON CONFLICT DO NOTHING`, id, userID, day, now)
		if err != nil {
			return err
		}
		counted = tag.RowsAffected() == 1
	case err != nil:
		return err
	}
	q := `UPDATE ` + k.Table + ` SET last_viewed_at = $2 WHERE id = $1`
	if counted {
		// in the database: two openings at once both count
		q = `UPDATE ` + k.Table + ` SET last_viewed_at = $2, views_total = views_total + 1 WHERE id = $1`
	}
	if _, err := tx.Exec(ctx, q, id, now); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Delete removes the object with its versions and its openings, in one transaction.
func (k *Kind) Delete(ctx context.Context, db interface {
	Begin(context.Context) (pgx.Tx, error)
}, id int64) error {
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	for _, q := range []string{`DELETE FROM ` + k.Versions + ` WHERE ` + k.FK + ` = $1`, `DELETE FROM ` + k.Views + ` WHERE ` + k.FK + ` = $1`,
		`DELETE FROM ` + k.Table + ` WHERE id = $1`} {
		if _, err := tx.Exec(ctx, q, id); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
