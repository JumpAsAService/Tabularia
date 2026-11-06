// Package db opens the pool towards the gateway's PostgreSQL and checks that its schema is
// the one this code was written for. The schema belongs to the Python gateway's Alembic
// migrations: this gateway never creates or alters a table.
package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// SchemaRevision is the Alembic head the Go code reads and writes
// (gateway/app/db/migrations/versions). A database at another revision is refused at
// start: a column read here may have moved.
const SchemaRevision = "0037"

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

// CheckSchema refuses a database the migrations have not brought to SchemaRevision.
func CheckSchema(ctx context.Context, pool *pgxpool.Pool) error {
	var rev string
	if err := pool.QueryRow(ctx, `SELECT version_num FROM alembic_version`).Scan(&rev); err != nil {
		return fmt.Errorf("the database has no schema revision (run the migrations): %w", err)
	}
	if rev != SchemaRevision {
		return fmt.Errorf("the database is at schema revision %s, this gateway reads %s", rev, SchemaRevision)
	}
	return nil
}
