// Package personalfolders is services/personal_folders.py: every person's own folder, a
// top-level folder owned by the person on which they hold a grant of their own to EDIT.
// Nothing but an administrator deletes it, moves it, or takes the person's grant away.
package personalfolders

import (
	"context"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// Capability is personal_folders.CAPABILITY: what the person holds on their folder.
const Capability = "edit"

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// personalQuery is _personal_query: the folders with the personal shape, oldest first.
const personalQuery = `SELECT p.id, p.owner_id FROM projects p JOIN permissions pm ON pm.project_id = p.id
	WHERE p.parent_id IS NULL AND p.owner_id IS NOT NULL AND pm.user_id = p.owner_id AND pm.capability = 'edit'`

// All is personal_folders: {folder id: user id} of every personal folder (one per person).
func All(ctx context.Context, q Querier) (map[int64]int64, error) {
	rows, err := q.Query(ctx, personalQuery+` ORDER BY p.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	first := map[int64]int64{} // user → folder
	for rows.Next() {
		var project, user int64
		if err := rows.Scan(&project, &user); err != nil {
			return nil, err
		}
		if _, ok := first[user]; !ok {
			first[user] = project
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make(map[int64]int64, len(first))
	for user, project := range first {
		out[project] = user
	}
	return out, nil
}

// FolderID is personal_folder_id: the person's personal folder, if they have one.
func FolderID(ctx context.Context, q Querier, userID int64) (*int64, error) {
	var id int64
	err := q.QueryRow(ctx, personalQuery+` AND p.owner_id = $1 ORDER BY p.id LIMIT 1`, userID).Scan(&id, new(int64))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &id, nil
}

// Of is personal_of: whose personal folder this is, if it is one.
func Of(ctx context.Context, q Querier, projectID int64) (*int64, error) {
	var parent, owner *int64
	err := q.QueryRow(ctx, `SELECT parent_id, owner_id FROM projects WHERE id = $1`, projectID).Scan(&parent, &owner)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if parent != nil || owner == nil {
		return nil, nil
	}
	folder, err := FolderID(ctx, q, *owner)
	if err != nil || folder == nil || *folder != projectID {
		return nil, err
	}
	return owner, nil
}

// Guard is guard: refuses `what` («delete», «move») on a personal folder unless an
// administrator asks.
func Guard(ctx context.Context, q Querier, actor *auth.User, projectID int64, what string) (*auth.Refusal, error) {
	rs, err := roles.Of(ctx, q, actor.ID, actor.IsSuperuser, actor.IsObserver)
	if err != nil || rs.Admin {
		return nil, err
	}
	owner, err := Of(ctx, q, projectID)
	if err != nil || owner == nil {
		return nil, err
	}
	return &auth.Refusal{Status: http.StatusConflict, Detail: "This is a personal folder: only an administrator can " + what + " it"}, nil
}

// GuardGrant is guard_grant: refuses to take the person's own grant off their personal
// folder, unless an administrator asks.
func GuardGrant(ctx context.Context, q Querier, actor *auth.User, projectID int64, userID *int64, capability string) (*auth.Refusal, error) {
	if userID == nil || capability != Capability {
		return nil, nil
	}
	rs, err := roles.Of(ctx, q, actor.ID, actor.IsSuperuser, actor.IsObserver)
	if err != nil || rs.Admin {
		return nil, err
	}
	owner, err := Of(ctx, q, projectID)
	if err != nil || owner == nil || *owner != *userID {
		return nil, err
	}
	return &auth.Refusal{Status: http.StatusConflict,
		Detail: "This is the access of the person to their personal folder: only an administrator can remove it"}, nil
}
