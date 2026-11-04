package api

import (
	"math/big"
)

// rowID is a validated integer as a row id: false when no row can have it (beyond a
// PostgreSQL integer: psycopg compares it as a bigint and finds nothing).
func rowID(v any) (int64, bool) {
	switch n := v.(type) {
	case int64:
		return n, n >= -1<<31 && n <= 1<<31-1
	case *big.Int:
		return 0, false
	}
	return 0, false
}

// pyStr is a validated string field (nil for None).
func pyStr(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}
