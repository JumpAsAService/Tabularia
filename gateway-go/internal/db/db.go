// Package db opens the pool towards the gateway's PostgreSQL and checks that its schema is
// the one this code was written for. The schema belongs to the Python gateway's Alembic
// migrations: this gateway never creates or alters a table.
package db

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"

	"github.com/jackc/pgx/v5/pgxpool"
)

// SchemaRevision is the Alembic head the Go code reads and writes
// (gateway/app/db/migrations/versions). A database behind it is refused at start: a column
// read here may not be there yet. One ahead of it is accepted with a warning (from 1.4.2):
// the migrations of an LTS patch only add (docs/lts.md), so the gateway of the previous
// patch still reads and writes what it knows, and a rollback between patches works.
const SchemaRevision = "0042"

// Open makes the pool. maxConns 0 = pgx's default.
func Open(ctx context.Context, dsn string, maxConns int32) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("the database address: %w", err)
	}
	if maxConns > 0 {
		cfg.MaxConns = maxConns
	}
	// the columns are naive UTC (gateway-datetime-utc): the session in UTC, so that a
	// timestamp written by Python and one written here mean the same moment
	cfg.ConnConfig.RuntimeParams["timezone"] = "UTC"
	return pgxpool.NewWithConfig(ctx, cfg)
}

// CheckSchema refuses a database the migrations have not brought to SchemaRevision, and
// accepts one ahead of it with a warning in the log (see SchemaRevision).
func CheckSchema(ctx context.Context, pool *pgxpool.Pool, log *slog.Logger) error {
	var rev string
	if err := pool.QueryRow(ctx, `SELECT version_num FROM alembic_version`).Scan(&rev); err != nil {
		return fmt.Errorf("the database has no schema revision (run the migrations): %w", err)
	}
	newer, err := CompareRevision(rev, SchemaRevision)
	if err != nil {
		return err
	}
	if newer && log != nil {
		log.Warn(fmt.Sprintf("database at %s, this gateway reads %s: running, the migrations of a patch only add", rev, SchemaRevision),
			"database_revision", rev, "gateway_revision", SchemaRevision)
	}
	return nil
}

// CompareRevision says whether the database's revision `db` may be read by code written for
// `code`: the same (false, nil), ahead of it (true, nil), or not (an error that says why).
// The revisions are numbered in one chain (0002, 0003…, docs/database-migrations.md); one
// that is not a number is accepted only when it is the code's own.
func CompareRevision(db, code string) (newer bool, err error) {
	if db == code {
		return false, nil
	}
	have, errDB := strconv.Atoi(db)
	want, errCode := strconv.Atoi(code)
	switch {
	case errDB != nil || errCode != nil:
		return false, fmt.Errorf("the database is at schema revision %q, this gateway reads %s", db, code)
	case have < want:
		return false, fmt.Errorf("the database is at schema revision %s, this gateway reads %s: run the migrations first "+
			"(python -m app.db.migra, the chart's migrate init container)", db, code)
	}
	return true, nil
}
