// Package personalfolders is services/personal_folders.py: every person's own folder, a
// top-level folder owned by the person on which they hold a grant of their own to EDIT.
// Nothing but an administrator deletes it, moves it, or takes the person's grant away.
package personalfolders

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"

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

// FolderName is folder_name: the part of the email before the @; the whole address when
// there is none.
func FolderName(email string) string {
	local, _, _ := strings.Cut(email, "@")
	if l := strings.TrimFunc(local, unicode.IsSpace); l != "" {
		return l
	}
	if e := strings.TrimFunc(email, unicode.IsSpace); e != "" {
		return e
	}
	return "personal"
}

// Ensure is ensure: the person's personal folder, created when they have none and should (a
// person signing in with their own credentials, active, not a service account, not only an
// observer). It never fails the caller: an error is logged and the next sign-in tries again.
// `via` says, in the audit, what created it («sso», «sign-in», «first-run»; "" = the panel).
func Ensure(ctx context.Context, db *pgxpool.Pool, log *slog.Logger, userID int64, actor *auth.User, actorLabel, via string) *int64 {
	id, created, name, err := ensure(ctx, db, userID)
	if err != nil {
		log.Error("could not create the personal folder", "user", userID, "err", err)
		return nil
	}
	if created {
		detail := pyjson.Object{{Key: "parent_id", Value: nil}, {Key: "personal", Value: true}, {Key: "user_id", Value: userID}}
		if via != "" {
			detail = append(detail, pyjson.KV{Key: "via", Value: via})
		}
		audit.Record(ctx, db, log, audit.Event{Actor: actor, ActorLabel: actorLabel, Action: "folder.create",
			TargetType: ptr("folder"), TargetID: id, TargetLabel: &name, Detail: detail})
	}
	return id
}

func ptr[T any](v T) *T { return &v }

func ensure(ctx context.Context, db *pgxpool.Pool, userID int64) (*int64, bool, string, error) {
	var u auth.User
	err := db.QueryRow(ctx, `SELECT id, email, is_active, is_superuser, is_observer, is_service FROM users WHERE id = $1`, userID).
		Scan(&u.ID, &u.Email, &u.IsActive, &u.IsSuperuser, &u.IsObserver, &u.IsService)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, false, "", nil
	}
	if err != nil {
		return nil, false, "", err
	}
	rs, err := roles.Of(ctx, db, u.ID, u.IsSuperuser, u.IsObserver)
	if err != nil {
		return nil, false, "", err
	}
	if u.IsService || !u.IsActive || rs.ObserverOnly() {
		return nil, false, "", nil
	}
	if existing, err := FolderID(ctx, db, userID); err != nil || existing != nil {
		return existing, false, "", err
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		return nil, false, "", err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	// two sign-ins of the same account at once: the second waits for the first, finds its folder
	if _, err := tx.Exec(ctx, `SELECT id FROM users WHERE id = $1 FOR UPDATE`, userID); err != nil {
		return nil, false, "", err
	}
	if existing, err := FolderID(ctx, tx, userID); err != nil || existing != nil {
		if err == nil {
			err = tx.Commit(ctx)
		}
		return existing, false, "", err
	}
	name := FolderName(u.Email)
	rows, err := tx.Query(ctx, `SELECT name FROM projects WHERE parent_id IS NULL AND left(name, length($1)) = $1`, name)
	if err != nil {
		return nil, false, "", err
	}
	names, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, false, "", err
	}
	taken := map[string]bool{}
	for _, n := range names {
		taken[n] = true
	}
	if taken[name] {
		name = copynames.CopyName(name, taken)
	}
	var id int64
	if err := tx.QueryRow(ctx, `INSERT INTO projects (name, description, owner_id, created_at) VALUES ($1, '', $2, $3) RETURNING id`,
		name, userID, time.Now().UTC()).Scan(&id); err != nil {
		return nil, false, "", err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit')`, id, userID); err != nil {
		return nil, false, "", err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, false, "", err
	}
	return &id, true, name, nil
}
