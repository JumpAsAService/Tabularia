// Package enginepolicy is services/engine_policy.py: which run engines the installation
// allows to CHOOSE. The FORBIDDEN engines are stored (disabled_engines): an empty table is
// everything allowed.
package enginepolicy

import (
	"context"
	"sort"

	"github.com/jackc/pgx/v5"
)

// Known is KNOWN_ENGINES, in its order.
var Known = []string{"polars", "duckdb", "chdb", "clickhouse", "bigquery"}

// Preferred is PREFERRED_ENGINES: the engine of what nobody chose one for is the first of
// these the administrator allows.
var Preferred = []string{"duckdb", "polars", "chdb", "clickhouse", "bigquery"}

// DefaultEngine is DEFAULT_ENGINE.
const DefaultEngine = "duckdb"

// Default is default_engine: the first preferred engine among `allowed` (every one when nil).
func Default(allowed map[string]bool) string {
	for _, e := range Preferred {
		if allowed == nil || allowed[e] {
			return e
		}
	}
	return DefaultEngine
}

// IsKnown says whether the id is among Known.
func IsKnown(id string) bool {
	for _, e := range Known {
		if e == id {
			return true
		}
	}
	return false
}

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
	out := map[string]bool{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// Allowed is allowed_engines: the known ones not disabled.
func Allowed(ctx context.Context, q Querier) (map[string]bool, error) {
	off, err := Disabled(ctx, q)
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, e := range Known {
		if !off[e] {
			out[e] = true
		}
	}
	return out, nil
}

// SortedKnown is sorted(KNOWN_ENGINES) less the ones given (routes/flows.py _validate_engine's
// messages).
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
