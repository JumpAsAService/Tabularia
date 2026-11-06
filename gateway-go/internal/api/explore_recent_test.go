package api

import (
	"context"
	"os"
	"reflect"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Explore's «Recent»: what a person saved a version of, or owns, newest first; only in the
// folders they may read (everything for an administrator); the scripts only with their
// environment. One query whatever the number of objects.
func TestRecentObjects(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	// in a transaction rolled back at the end: the other packages' tests share the database
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := tx.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	user := func(email string) int64 {
		return one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, false, false, false, now()) RETURNING id`, email)
	}
	ann, bob := user("rc-ann@x.test"), user("rc-bob@x.test")
	folder := func(name string) int64 {
		return one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, name)
	}
	open, closed := folder("rc-open"), folder("rc-closed")
	flow := func(name string, project, owner int64, ago string) int64 {
		return one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
			VALUES ($1, '', $2, $3, '{}', 'duckdb', now() - $4::interval, now() - $4::interval) RETURNING id`, name, project, owner, ago)
	}
	mine := flow("rc mine, old", open, ann, "3 days")
	saved := flow("rc bob's, saved by ann", open, bob, "10 days")
	one(`INSERT INTO flow_versions (flow_id, version, definition, note, created_by, created_at)
		VALUES ($1, 1, '{}', '', $2, now() - interval '1 hour') RETURNING id`, saved, ann)
	hidden := flow("rc mine, closed folder", closed, ann, "1 day")
	flow("rc bob's alone", open, bob, "1 minute") // neither owned nor saved by ann
	report := one(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ('rc report', '', $1, $2, 1, now() - interval '5 days', now() - interval '5 days') RETURNING id`, open, ann)
	// a newer version of her own report: the later moment counts, once
	one(`INSERT INTO report_versions (report_id, version, content, note, created_by, created_at)
		VALUES ($1, 2, '{}', '', $2, now() - interval '2 hours') RETURNING id`, report, ann)

	names := func(items []RecentItem) []string {
		out := []string{}
		for _, it := range items {
			out = append(out, it.Kind+" "+it.Name)
		}
		return out
	}
	got, err := recentObjects(ctx, tx, ann, recentScope{readable: []int64{open}}, 20)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"flow rc bob's, saved by ann", "report rc report", "flow rc mine, old"}
	if !reflect.DeepEqual(names(got), want) {
		t.Errorf("reader: %v, want %v", names(got), want)
	}
	if got[0].ID != saved || got[2].ID != mine || got[0].ProjectID != open || got[0].At == nil {
		t.Errorf("ids, folder, time: %+v", got)
	}
	// an administrator sees the closed folder too
	got, err = recentObjects(ctx, tx, ann, recentScope{admin: true}, 20)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 4 || got[3].ID != hidden && got[2].ID != hidden {
		t.Errorf("admin: %v", names(got))
	}
	// the limit, and nobody else's list
	if got, _ = recentObjects(ctx, tx, ann, recentScope{admin: true}, 2); len(got) != 2 {
		t.Errorf("limit: %v", names(got))
	}
	if got, _ = recentObjects(ctx, tx, bob, recentScope{readable: []int64{open}}, 20); !reflect.DeepEqual(names(got)[:1], []string{"flow rc bob's alone"}) || len(got) != 2 {
		t.Errorf("bob: %v", names(got))
	}
	// nothing readable: nothing
	if got, _ = recentObjects(ctx, tx, ann, recentScope{}, 20); len(got) != 0 {
		t.Errorf("no folder: %v", names(got))
	}
}
