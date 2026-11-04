// Package copynames is services/copy_names.py: the name a duplicated object takes, as a file
// manager gives it: «Sales» copied is «Sales (1)», again «Sales (2)»; a copy of «Sales (1)»
// is the next free «Sales (n)», never «Sales (1) (1)».
package copynames

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// pySpace is str.isspace.
func pySpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1C && r <= 0x1F)
}

// BaseName is base_name: the name without the copy number it may carry
// (`^(.*\S) \((\d+)\)$`, Unicode digits and spaces as Python's re, no newline in `.`).
func BaseName(name string) string {
	rest, ok := strings.CutSuffix(name, ")")
	if !ok {
		return name
	}
	end := len(rest)
	for end > 0 {
		r, size := utf8.DecodeLastRuneInString(rest[:end])
		if !unicode.Is(unicode.Nd, r) {
			break
		}
		end -= size
	}
	if end == len(rest) {
		return name
	}
	base, ok := strings.CutSuffix(rest[:end], " (")
	if !ok || base == "" || strings.ContainsRune(base, '\n') {
		return name
	}
	if last, _ := utf8.DecodeLastRuneInString(base); pySpace(last) {
		return name
	}
	return base
}

// CopyName is copy_name: the first «base (i)» not among the names taken.
func CopyName(name string, taken map[string]bool) string {
	base := BaseName(name)
	i := 1
	for taken[fmt.Sprintf("%s (%d)", base, i)] {
		i++
	}
	return fmt.Sprintf("%s (%d)", base, i)
}

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// likeEscape is SQLAlchemy's startswith(autoescape=True): «/» escapes «%», «_» and itself.
func likeEscape(s string) string {
	return strings.NewReplacer("/", "//", "%", "/%", "_", "/_").Replace(s)
}

// Free is free_copy_name over `table` (a table with project_id and name).
func Free(ctx context.Context, q Querier, table string, projectID int64, name string) (string, error) {
	prefix := BaseName(name) + " ("
	rows, err := q.Query(ctx, `SELECT name FROM `+pgx.Identifier{table}.Sanitize()+
		` WHERE project_id = $1 AND (name LIKE $2 || '%' ESCAPE '/')`, projectID, likeEscape(prefix))
	if err != nil {
		return "", err
	}
	taken := map[string]bool{}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			rows.Close()
			return "", err
		}
		taken[n] = true
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	return CopyName(name, taken), nil
}

// Clash is commit_copy's IntegrityError: two copies took the same name at the same moment.
func Clash(err error) bool {
	var pe *pgconn.PgError
	return errors.As(err, &pe) && pe.Code == "23505"
}

// ClashDetail is commit_copy's 409.
const ClashDetail = "Another copy took that name at the same moment: try again"
