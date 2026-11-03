// Package copynames is services/copy_names.py: the name a copy takes in its folder, as a file
// manager gives it: «Sales» copied is «Sales (1)», a copy of «Sales (1)» the next free
// «Sales (n)», never «Sales (1) (1)».
package copynames

import (
	"context"
	"fmt"
	"regexp"

	"github.com/jackc/pgx/v5"
)

var numbered = regexp.MustCompile(`^(.*\S) \((\d+)\)$`)

// Base is base_name: the name without the copy number it may carry.
func Base(name string) string {
	if m := numbered.FindStringSubmatch(name); m != nil {
		return m[1]
	}
	return name
}

// Name is copy_name: the first «base (i)» not among the names taken.
func Name(name string, taken []string) string {
	used := map[string]bool{}
	for _, t := range taken {
		used[t] = true
	}
	base, i := Base(name), 1
	for used[fmt.Sprintf("%s (%d)", base, i)] {
		i++
	}
	return fmt.Sprintf("%s (%d)", base, i)
}

// Querier is a pool or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// Free is free_copy_name over `table` (name and project_id columns): only the names that could
// clash are read (`name.startswith(prefix, autoescape=True)`).
func Free(ctx context.Context, q Querier, table string, projectID int64, name string) (string, error) {
	prefix := Base(name) + " ("
	rows, err := q.Query(ctx, `SELECT name FROM `+table+` WHERE project_id = $1 AND left(name, char_length($2)) = $2`,
		projectID, prefix)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var taken []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return "", err
		}
		taken = append(taken, n)
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	return Name(name, taken), nil
}
