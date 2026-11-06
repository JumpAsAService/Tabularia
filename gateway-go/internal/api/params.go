package api

import "math"

// NoSuchID stands for an integer past the ids' columns (PostgreSQL's integer) in a path:
// Python reads it, the database finds no row with it (psycopg2 writes it as a literal), so it
// is an id nothing has; -1 is as much an id nothing has, and fits the columns.
const NoSuchID int64 = -1

// inColumn is an id as the database looks it up: one past the ids' columns finds no row in
// Python (psycopg2 writes it as a literal) and would not fit pgx's integer parameter here.
func inColumn(ids []int64) []int64 {
	for i, id := range ids {
		if id < math.MinInt32 || id > math.MaxInt32 {
			ids[i] = NoSuchID
		}
	}
	return ids
}

// int32IDs turns ints a pyd model validated into ids, as pathIntsInto does: one past the
// ids' columns is NoSuchID.
func int32IDs(vals []any) []int64 {
	out := make([]int64, len(vals))
	for i, v := range vals {
		if n, ok := v.(int64); ok {
			out[i] = n
		} else {
			out[i] = NoSuchID
		}
	}
	return inColumn(out)
}
