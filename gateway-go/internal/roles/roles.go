// Package roles is the effective roles of services/permissions.py: an administrator by the
// personal flag or an administrators' group, an observer by being an administrator, by the
// flag or by an observers' group. Computed at every request, never stored: leaving a group
// takes the role away at once.
package roles

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Roles of one person.
type Roles struct {
	Admin    bool // permissions.is_admin
	Observer bool // permissions.is_observer
}

// ObserverOnly is permissions.is_observer_only: reads the panels, does not administer.
func (r Roles) ObserverOnly() bool { return r.Observer && !r.Admin }

// Of reads the groups once for both questions.
func Of(ctx context.Context, q Querier, userID int64, isSuperuser, isObserver bool) (Roles, error) {
	var adminG, observerG bool
	err := q.QueryRow(ctx, `SELECT
			EXISTS (SELECT 1 FROM user_groups ug JOIN groups g ON g.id = ug.group_id WHERE ug.user_id = $1 AND g.is_admin),
			EXISTS (SELECT 1 FROM user_groups ug JOIN groups g ON g.id = ug.group_id WHERE ug.user_id = $1 AND g.is_observer)`,
		userID).Scan(&adminG, &observerG)
	if err != nil {
		return Roles{}, err
	}
	admin := isSuperuser || adminG
	return Roles{Admin: admin, Observer: admin || isObserver || observerG}, nil
}
