// Package enginepolicy is services/engine_policy.py: which run engines the installation
// allows to choose. The FORBIDDEN ones are stored (disabled_engines); an empty table is
// everything allowed.
package enginepolicy

import (
	"context"
	"sort"

	"github.com/jackc/pgx/v5"
)

// Known is KNOWN_ENGINES, in sync with the engine's catalog.
var Known = []string{"polars", "duckdb", "chdb", "clickhouse", "bigquery"}

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// Disabled is disabled_engines.
func Disabled(ctx context.Context, q Querier) (map[string]bool, error) {
	rows, err := q.Query(ctx, `SELECT engine_id FROM disabled_engines`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// IsKnown says whether the engine is in the catalog.
func IsKnown(e string) bool {
	for _, k := range Known {
		if k == e {
			return true
		}
	}
	return false
}

// SortedKnown is sorted(KNOWN_ENGINES), less the ones given.
func SortedKnown(except map[string]bool) []string {
	var out []string
	for _, k := range Known {
		if !except[k] {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}
