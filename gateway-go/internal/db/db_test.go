package db

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// From 1.4.2 a gateway runs on a database a later patch has migrated (its migrations only add),
// so that a rollback between patches of the LTS line works; it still refuses an older one.
func TestCompareRevision(t *testing.T) {
	cases := []struct {
		db, code string
		newer    bool
		refused  string // a part of the error; "" = accepted
	}{
		{"0042", "0042", false, ""},
		{"0043", "0042", true, ""},
		{"0100", "0042", true, ""},
		{"0041", "0042", false, "run the migrations first"},
		{"0001", "0042", false, "schema revision 0001, this gateway reads 0042"},
		{"abc123", "0042", false, `"abc123"`},
		{"", "0042", false, `""`},
		{"0042", "0042x", false, "0042x"},
	}
	for _, c := range cases {
		newer, err := CompareRevision(c.db, c.code)
		if c.refused == "" {
			if err != nil || newer != c.newer {
				t.Errorf("%s for %s: (%v, %v), want (%v, nil)", c.db, c.code, newer, err, c.newer)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), c.refused) || newer {
			t.Errorf("%s for %s: (%v, %v), want a refusal with %q", c.db, c.code, newer, err, c.refused)
		}
	}
}

// CheckSchema on a database: one at a later revision starts with the warning in the log, one
// behind is refused. A schema of its own, so the shared test database keeps its revision.
func TestCheckSchemaOnADatabase(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	admin, err := Open(ctx, dsn, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer admin.Close()
	if _, err := admin.Exec(ctx, `DROP SCHEMA IF EXISTS zz_schema_check CASCADE; CREATE SCHEMA zz_schema_check;
		CREATE TABLE zz_schema_check.alembic_version (version_num varchar(32) NOT NULL)`); err != nil {
		t.Fatal(err)
	}
	defer admin.Exec(ctx, `DROP SCHEMA IF EXISTS zz_schema_check CASCADE`)
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatal(err)
	}
	cfg.ConnConfig.RuntimeParams["search_path"] = "zz_schema_check"
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	next, _ := strconv.Atoi(SchemaRevision)
	for _, c := range []struct {
		rev           string
		refused, warn bool
	}{
		{SchemaRevision, false, false},
		{fmt.Sprintf("%04d", next+1), false, true},
		{fmt.Sprintf("%04d", next-1), true, false},
	} {
		if _, err := admin.Exec(ctx, `DELETE FROM zz_schema_check.alembic_version;
			INSERT INTO zz_schema_check.alembic_version VALUES ('`+c.rev+`')`); err != nil {
			t.Fatal(err)
		}
		var out bytes.Buffer
		err := CheckSchema(ctx, pool, slog.New(slog.NewTextHandler(&out, nil)))
		if (err != nil) != c.refused {
			t.Errorf("revision %s: %v", c.rev, err)
		}
		want := fmt.Sprintf("database at %s, this gateway reads %s: running, the migrations of a patch only add", c.rev, SchemaRevision)
		if got := strings.Contains(out.String(), want); got != c.warn {
			t.Errorf("revision %s: the log %q (the warning expected: %v)", c.rev, out.String(), c.warn)
		}
	}
}
