package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/loginthrottle"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/passwords"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/personalfolders"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sessions"
)

// UserOut is schemas/models.py UserOut: here `is_superuser` and `is_observer` are the
// PERSONAL flags, the groups say the rest.
type UserOut struct {
	ID             int64    `json:"id"`
	Email          string   `json:"email"`
	FullName       string   `json:"full_name"`
	IsActive       bool     `json:"is_active"`
	IsSuperuser    bool     `json:"is_superuser"`
	CreatedAt      *string  `json:"created_at"`
	LastSeenAt     *string  `json:"last_seen_at"`
	SSOOnly        bool     `json:"sso_only"`
	Groups         []string `json:"groups"`
	AdminGroups    []string `json:"admin_groups"`
	IsObserver     bool     `json:"is_observer"`
	ObserverGroups []string `json:"observer_groups"`
	IsService      bool     `json:"is_service"`
}

func init() {
	register("users", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/users", Handler: observer(d, "/users", listUsers(d))},
			{Method: http.MethodPost, Pattern: "/users", Handler: withBody(superuser(d, "/users", createUser(d)))},
			{Method: http.MethodPut, Pattern: "/users/me/locale", Handler: withBody(server.Authed(d.Auth, "/users/me/locale", setMyLocale(d)))},
			{Method: http.MethodGet, Pattern: "/users/password-rules", Handler: server.Authed(d.Auth, "/users/password-rules", passwordRules)},
			{Method: http.MethodPost, Pattern: "/users/me/password", Handler: withBody(server.Authed(d.Auth, "/users/me/password", changeMyPassword(d)))},
			{Method: http.MethodPatch, Pattern: "/users/{user_id}", Handler: withBody(superuser(d, "/users/{user_id}", updateUser(d)))},
			{Method: http.MethodDelete, Pattern: "/users/{user_id}", Handler: superuser(d, "/users/{user_id}", deleteUser(d))},
			{Method: http.MethodPut, Pattern: "/users/{user_id}/groups/{group_id}", Handler: superuser(d, "/users/{user_id}/groups/{group_id}", addToGroup(d))},
			{Method: http.MethodDelete, Pattern: "/users/{user_id}/groups/{group_id}", Handler: superuser(d, "/users/{user_id}/groups/{group_id}", removeFromGroup(d))},
		}
	})
}

// userRow is the users columns the routes read.
type userRow struct {
	auth.User
	CreatedAt time.Time
}

const userRowColumns = `id, email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, locale, last_seen_at, created_at`

func scanUserRow(row pgx.Row) (*userRow, error) {
	var u userRow
	err := row.Scan(&u.ID, &u.Email, &u.FullName, &u.HashedPassword, &u.IsActive, &u.IsSuperuser, &u.IsObserver, &u.IsService,
		&u.Locale, &u.LastSeenAt, &u.CreatedAt)
	return &u, err
}

// readUser is session.get(User, id): nil when there is none.
func readUser(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, id int64) (*userRow, error) {
	u, err := scanUserRow(q.QueryRow(ctx, `SELECT `+userRowColumns+` FROM users WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return u, err
}

// groupNamesOf is users._group_names (each person's group names, sorted) with the names of
// the administrators' and the observers' groups.
type groupNamesOf struct {
	perUser           map[int64][]string
	admins, observers map[string]bool
}

func readGroupNames(ctx context.Context, d *Deps) (*groupNamesOf, error) {
	g := &groupNamesOf{perUser: map[int64][]string{}, admins: map[string]bool{}, observers: map[string]bool{}}
	rows, err := d.DB.Query(ctx, `SELECT ug.user_id, g.name FROM user_groups ug, groups g WHERE ug.group_id = g.id`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			rows.Close()
			return nil, err
		}
		g.perUser[id] = append(g.perUser[id], name)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, names := range g.perUser {
		sort.Strings(names)
	}
	rows, err = d.DB.Query(ctx, `SELECT name, is_admin, is_observer FROM groups WHERE is_admin OR is_observer`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var name string
		var admin, obs bool
		if err := rows.Scan(&name, &admin, &obs); err != nil {
			rows.Close()
			return nil, err
		}
		g.admins[name] = g.admins[name] || admin
		g.observers[name] = g.observers[name] || obs
	}
	rows.Close()
	return g, rows.Err()
}

// userOut is users._to_out; `g` nil for an account just created (no group yet).
func userOut(u *userRow, g *groupNamesOf) UserOut {
	out := UserOut{ID: u.ID, Email: u.Email, FullName: u.FullName, IsActive: u.IsActive, IsSuperuser: u.IsSuperuser,
		CreatedAt: isoPtr(&u.CreatedAt), LastSeenAt: isoPtr(u.LastSeenAt), SSOOnly: u.HashedPassword == nil,
		Groups: []string{}, AdminGroups: []string{}, IsObserver: u.IsObserver, ObserverGroups: []string{}, IsService: u.IsService}
	if g != nil {
		for _, name := range g.perUser[u.ID] {
			out.Groups = append(out.Groups, name)
			if g.admins[name] {
				out.AdminGroups = append(out.AdminGroups, name)
			}
			if g.observers[name] {
				out.ObserverGroups = append(out.ObserverGroups, name)
			}
		}
	}
	return out
}

// listUsers is routes/users.py list_users: an observer sees it without the personal data.
// No ORDER BY, as the Python query.
func listUsers(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		g, err := readGroupNames(ctx, d)
		if err != nil {
			fail(d, w, "reading the groups", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT `+userRowColumns+` FROM users`)
		if err != nil {
			fail(d, w, "reading the users", err)
			return
		}
		users, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (*userRow, error) { return scanUserRow(row) })
		if err != nil {
			fail(d, w, "reading the users", err)
			return
		}
		rs, err := roles.Of(ctx, d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		out := make([]UserOut, 0, len(users))
		for _, u := range users {
			o := userOut(u, g)
			if !rs.Admin {
				o.Email, o.FullName = masking.Email(o.Email), ""
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// passwordRules is routes/users.py password_rules: core/passwords.py's rule for the forms.
func passwordRules(w http.ResponseWriter, _ *http.Request, _ *auth.Principal) {
	known := []string{"12345678", "123456789012", "admin", "admin123", "administrator", "change-me", "change-me-2026",
		"change-me-in-production", "changeme", "password", "tabularia"}
	server.JSON(w, http.StatusOK, struct {
		MinLength int      `json:"min_length"`
		MaxBytes  int      `json:"max_bytes"`
		Known     []string `json:"known"`
	}{12, 72, known})
}

const emailTaken = "This email is already registered"

// createUser is routes/users.py create_user: not with a token; the roles given at creation
// are their own audit events; the personal folder is born with the account.
func createUser(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.UserCreate)
		if !validated(w, &ps) {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		ctx := r.Context()
		email := body.Get("email").(string)
		var taken bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM users WHERE email = $1)`, email).Scan(&taken); err != nil {
			fail(d, w, "reading the users", err)
			return
		}
		if taken {
			detail(w, http.StatusConflict, emailTaken)
			return
		}
		hash, err := passwords.Hash(body.Get("password").(string))
		if err != nil {
			fail(d, w, "hashing the password", err)
			return
		}
		superuser, observer := body.Get("is_superuser").(bool), body.Get("is_observer").(bool)
		u, err := scanUserRow(d.DB.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser,
			is_observer, is_service, created_at) VALUES ($1, $2, $3, true, $4, $5, false, $6) RETURNING `+userRowColumns,
			email, body.Get("full_name").(string), hash, superuser, observer, time.Now().UTC()))
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			detail(w, http.StatusConflict, emailTaken)
			return
		}
		if err != nil {
			fail(d, w, "creating the user", err)
			return
		}
		ev := audit.Event{Actor: &p.User, TargetType: ptr("user"), TargetID: &u.ID, TargetLabel: &u.Email}
		created := ev
		created.Action = "user.create"
		audit.Record(ctx, d.DB, d.Log, created)
		atCreation := pyjson.Object{{Key: "at_creation", Value: true}}
		if superuser {
			e := ev
			e.Action, e.Detail = "user.promote", atCreation
			audit.Record(ctx, d.DB, d.Log, e)
		}
		if observer {
			e := ev
			e.Action, e.Detail = "user.observer_grant", atCreation
			audit.Record(ctx, d.DB, d.Log, e)
		}
		personalfolders.Ensure(ctx, d.DB, d.Log, u.ID, &p.User, "", "")
		server.JSON(w, http.StatusCreated, userOut(u, nil))
	}
}

// setMyLocale is routes/users.py set_my_locale: the interface's language, which the emails
// to this person speak.
func setMyLocale(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.LocaleChange)
		if !validated(w, &ps) {
			return
		}
		locale := body.Get("locale").(string)
		if p.User.Locale == nil || *p.User.Locale != locale {
			if _, err := d.DB.Exec(r.Context(), `UPDATE users SET locale = $1 WHERE id = $2`, locale, p.User.ID); err != nil {
				fail(d, w, "saving the language", err)
				return
			}
		}
		noContent(w)
	}
}

// tooMany is the 429 of the brake, with Retry-After.
func tooMany(w http.ResponseWriter, text string, wait float64) {
	n := loginthrottle.Seconds(wait)
	w.Header().Set("Retry-After", strconv.Itoa(n))
	detail(w, http.StatusTooManyRequests, fmt.Sprintf(text, n))
}

// changeMyPassword is routes/users.py change_my_password: the current password asked again,
// under its own brake; the other sessions and the API tokens close, this session stays.
func changeMyPassword(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.PasswordChange)
		if !validated(w, &ps) {
			return
		}
		if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
			server.Refuse(w, refusal)
			return
		}
		ctx, u := r.Context(), &p.User
		if u.HashedPassword == nil {
			detail(w, http.StatusConflict, "This account signs in with the identity provider: its password is managed there")
			return
		}
		rs, err := roles.Of(ctx, d.DB, u.ID, u.IsSuperuser, u.IsObserver)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		if rs.ObserverOnly() {
			detail(w, http.StatusForbidden, "An observer account cannot change its password")
			return
		}
		key := fmt.Sprintf("password-change:%d", u.ID)
		ev := audit.Event{Actor: u, Action: "auth.password_change", TargetType: ptr("user"), TargetID: &u.ID, TargetLabel: &u.Email}
		wait, err := loginthrottle.RequestWait(ctx, d.DB, nil, key)
		if err != nil {
			fail(d, w, "reading the attempts", err)
			return
		}
		if wait > 0 {
			e := ev
			e.Outcome, e.Detail = "failure", pyjson.Object{{Key: "reason", Value: "throttled"}}
			audit.Record(ctx, d.DB, d.Log, e)
			tooMany(w, "Too many attempts. Try again in %d seconds.", wait)
			return
		}
		if !passwords.Verify(body.Get("current_password").(string), *u.HashedPassword) {
			e := ev
			e.Outcome, e.Detail = "failure", pyjson.Object{{Key: "reason", Value: "wrong current password"}}
			audit.Record(ctx, d.DB, d.Log, e)
			if err := loginthrottle.RecordFailure(ctx, d.DB, nil, key); err != nil {
				fail(d, w, "counting the attempt", err)
				return
			}
			// 400 and not 401: a 401 would sign the browser out
			detail(w, http.StatusBadRequest, "The current password is not correct")
			return
		}
		if err := loginthrottle.RecordSuccess(ctx, d.DB, nil, key); err != nil {
			fail(d, w, "counting the attempt", err)
			return
		}
		hash, err := passwords.Hash(body.Get("new_password").(string))
		if err != nil {
			fail(d, w, "hashing the password", err)
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "changing the password", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		var closed, withdrawn int64
		_, err = tx.Exec(ctx, `UPDATE users SET hashed_password = $1 WHERE id = $2`, hash, u.ID)
		if err == nil {
			closed, err = sessions.CloseAll(ctx, tx, u.ID, p.SessionID)
		}
		if err == nil {
			withdrawn, err = apitokens.RevokeAll(ctx, tx, u.ID)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			fail(d, w, "changing the password", err)
			return
		}
		ev.Detail = pyjson.Object{{Key: "sessions", Value: closed}, {Key: "tokens", Value: withdrawn}}
		audit.Record(ctx, d.DB, d.Log, ev)
		noContent(w)
	}
}

// updateUser is routes/users.py update_user: the name, a password, the account on or off, the
// two roles. A token sets no password and gives no role; a new password or a disabled account
// closes the sessions and withdraws the tokens; an administrator keeps their own role.
func updateUser(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		ids, _ := pathIntsInto(&ps, r, "user_id")
		body := ps.Body(bodyOf(r), gatewaymodels.UserUpdate)
		if !validated(w, &ps) {
			return
		}
		ctx, id := r.Context(), ids[0]
		u, err := readUser(ctx, d.DB, id)
		if err != nil {
			fail(d, w, "reading the user", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, "User not found")
			return
		}
		fullName, password := optStr(body, "full_name"), optStr(body, "password")
		active, superuser, observer := optBool(body, "is_active"), optBool(body, "is_superuser"), optBool(body, "is_observer")
		if password != nil {
			if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoTokenFromToken); refusal != nil {
				server.Refuse(w, refusal)
				return
			}
			if u.IsService {
				detail(w, http.StatusUnprocessableEntity, "A service account signs in only with a token: no password")
				return
			}
		}
		wasAdmin, wasObserver, wasActive := u.IsSuperuser, u.IsObserver, u.IsActive
		wasName := u.FullName
		if (superuser != nil && *superuser && !wasAdmin) || (observer != nil && *observer && !wasObserver) || (active != nil && *active && !u.IsActive) {
			if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoRolesFromToken); refusal != nil {
				server.Refuse(w, refusal)
				return
			}
		}
		closeSessions := password != nil || (active != nil && !*active)
		var renamed pyjson.Object
		if fullName != nil && *fullName != u.FullName {
			renamed = pyjson.Object{{Key: "from", Value: u.FullName}, {Key: "to", Value: *fullName}}
		}
		if fullName != nil {
			u.FullName = *fullName
		}
		if password != nil {
			hash, err := passwords.Hash(*password)
			if err != nil {
				fail(d, w, "hashing the password", err)
				return
			}
			u.HashedPassword = &hash
		}
		if active != nil {
			u.IsActive = *active
		}
		if superuser != nil {
			u.IsSuperuser = *superuser
		}
		if observer != nil {
			u.IsObserver = *observer
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "changing the user", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		// as the ORM's flush: no UPDATE when nothing changed
		if password != nil || u.FullName != wasName || u.IsActive != wasActive || u.IsSuperuser != wasAdmin || u.IsObserver != wasObserver {
			if _, err := tx.Exec(ctx, `UPDATE users SET full_name = $1, hashed_password = $2, is_active = $3, is_superuser = $4,
				is_observer = $5 WHERE id = $6`, u.FullName, u.HashedPassword, u.IsActive, u.IsSuperuser, u.IsObserver, id); err != nil {
				fail(d, w, "changing the user", err)
				return
			}
		}
		if id == p.User.ID {
			if refusal, err := stillAdmin(ctx, tx, &p.User); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
		}
		var closed, withdrawn int64
		if closeSessions {
			except := ""
			if id == p.User.ID {
				except = p.SessionID
			}
			if closed, err = sessions.CloseAll(ctx, tx, id, except); err == nil {
				withdrawn, err = apitokens.RevokeAll(ctx, tx, id)
			}
			if err != nil {
				fail(d, w, "closing the sessions", err)
				return
			}
		}
		if u.IsObserver && !wasObserver {
			n, err := apitokens.WithdrawIfObserver(ctx, tx, []int64{id})
			if err != nil {
				fail(d, w, "withdrawing the tokens", err)
				return
			}
			withdrawn += n
		}
		if err := tx.Commit(ctx); err != nil {
			fail(d, w, "changing the user", err)
			return
		}
		ev := audit.Event{Actor: &p.User, TargetType: ptr("user"), TargetID: &id, TargetLabel: &u.Email}
		record := func(action string, detail pyjson.Object) {
			e := ev
			e.Action, e.Detail = action, detail
			audit.Record(ctx, d.DB, d.Log, e)
		}
		if renamed != nil {
			record("user.update", pyjson.Object{{Key: "full_name", Value: renamed}})
		}
		if u.IsActive != wasActive {
			record(map[bool]string{true: "user.reactivate", false: "user.deactivate"}[u.IsActive], nil)
		}
		if closed > 0 || withdrawn > 0 {
			reason := "observer"
			if password != nil {
				reason = "password"
			} else if active != nil && !*active {
				reason = "deactivated"
			}
			record("auth.session_revoke", pyjson.Object{{Key: "sessions", Value: closed}, {Key: "tokens", Value: withdrawn}, {Key: "reason", Value: reason}})
		}
		if password != nil {
			record("auth.password_change", pyjson.Object{{Key: "by_admin", Value: true}})
		}
		// session.refresh(user): the row as it is now
		if u, err = readUser(ctx, d.DB, id); err != nil || u == nil {
			fail(d, w, "reading the user", fmt.Errorf("user %d: %v", id, err))
			return
		}
		if u.IsSuperuser != wasAdmin {
			record(map[bool]string{true: "user.promote", false: "user.demote"}[u.IsSuperuser], nil)
		}
		if u.IsObserver != wasObserver {
			record(map[bool]string{true: "user.observer_grant", false: "user.observer_revoke"}[u.IsObserver], nil)
		}
		g, err := readGroupNames(ctx, d)
		if err != nil {
			fail(d, w, "reading the groups", err)
			return
		}
		server.JSON(w, http.StatusOK, userOut(u, g))
	}
}

// deleteUser is routes/users.py delete_user: never oneself; the content stays, the audit is
// not touched but for the reference to the account; an empty personal folder goes with it.
func deleteUser(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "user_id")
		if !ok {
			return
		}
		ctx, id := r.Context(), ids[0]
		if id == p.User.ID {
			detail(w, http.StatusConflict, "You cannot delete your own account")
			return
		}
		u, err := readUser(ctx, d.DB, id)
		if err != nil {
			fail(d, w, "reading the user", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, "User not found")
			return
		}
		personal, err := personalfolders.FolderID(ctx, d.DB, id)
		if err != nil {
			fail(d, w, "reading the personal folder", err)
			return
		}
		if err := removeUser(ctx, d, u); err != nil {
			fail(d, w, "deleting the user", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "user.delete", TargetType: ptr("user"),
			TargetID: &id, TargetLabel: &u.Email})
		if personal != nil {
			// an empty personal folder goes with the account; one holding anything stays
			if _, err := removeFolder(ctx, d, &p.User, *personal); err != nil {
				fail(d, w, "deleting the personal folder", err)
				return
			}
		}
		returnsNone(w)
	}
}

// removeUser is routes/users.py remove_user: the account and what is only theirs, in one
// transaction; every other reference to it is reset.
func removeUser(ctx context.Context, d *Deps, u *userRow) error {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	email := pyd.Lower(pyd.Strip(u.Email))
	type statement struct {
		q    string
		args []any
	}
	byID := func(q string) statement { return statement{q, []any{u.ID}} }
	statements := []statement{
		byID(`DELETE FROM ai_chat_turns WHERE chat_id IN (SELECT id FROM ai_chats WHERE user_id = $1)`),
		byID(`DELETE FROM ai_chats WHERE user_id = $1`),
		byID(`DELETE FROM agent_drafts WHERE user_id = $1`),
		byID(`DELETE FROM agent_plans WHERE user_id = $1`),
		byID(`DELETE FROM permissions WHERE user_id = $1`),
		byID(`DELETE FROM container_registry_access WHERE user_id = $1`),
		byID(`DELETE FROM user_groups WHERE user_id = $1`),
		byID(`DELETE FROM user_sessions WHERE user_id = $1`),
		byID(`DELETE FROM api_tokens WHERE user_id = $1`),
		byID(`DELETE FROM uploads WHERE owner_id = $1`),
		byID(`DELETE FROM notebook_sessions WHERE user_id = $1`),
		byID(`DELETE FROM notebook_visitors WHERE user_id = $1`),
		byID(`DELETE FROM notebook_tickets WHERE user_id = $1`),
		byID(`DELETE FROM report_views WHERE user_id = $1`),
		byID(`DELETE FROM dashboard_views WHERE user_id = $1`),
		// ip_retention.forget_user: the account's addresses, and its failed sign-ins
		{`UPDATE audit_logs SET ip = NULL, user_agent = NULL WHERE actor_id = $1 OR lower(actor_label) = $2`, []any{u.ID, email}},
		{`DELETE FROM login_attempts WHERE email = $1`, []any{email}},
		byID(`UPDATE audit_logs SET actor_id = NULL WHERE actor_id = $1`),
	}
	for _, ref := range []string{"projects.owner_id", "flows.owner_id", "datasources.owner_id", "connections.owner_id",
		"runs.launched_by", "api_tokens.created_by", "webhooks.created_by", "flow_versions.created_by", "saved_views.owner_id",
		"flow_groups.owner_id", "flows.run_scheduled_by", "datasources.refresh_scheduled_by", "flow_groups.run_scheduled_by",
		"saved_scripts.owner_id", "saved_scripts.run_scheduled_by", "saved_script_versions.created_by", "sql_scripts.owner_id",
		"sql_scripts.run_scheduled_by", "sql_script_versions.created_by", "reports.owner_id", "report_versions.created_by",
		"dashboards.owner_id", "dashboard_versions.created_by", "notebooks.owner_id", "notebooks.draft_by",
		"notebook_versions.created_by", "ai_models.updated_by", "ai_providers.updated_by", "banners.created_by",
		"disabled_engines.disabled_by", "privacy_notice.updated_by"} {
		table, column, _ := strings.Cut(ref, ".")
		statements = append(statements, byID(fmt.Sprintf(`UPDATE %s SET %s = NULL WHERE %s = $1`, table, column, column)))
	}
	statements = append(statements, byID(`DELETE FROM users WHERE id = $1`))
	for _, s := range statements {
		if _, err := tx.Exec(ctx, s.q, s.args...); err != nil {
			return fmt.Errorf("%s: %w", s.q, err)
		}
	}
	return tx.Commit(ctx)
}

// membershipDetail is audit.membership_detail.
func membershipDetail(name string, id int64, admin, observer bool) pyjson.Object {
	o := pyjson.Object{{Key: "group", Value: name}, {Key: "group_id", Value: id}}
	if admin {
		o = append(o, pyjson.KV{Key: "admin", Value: true})
	}
	if observer {
		o = append(o, pyjson.KV{Key: "observer", Value: true})
	}
	return o
}

type groupFlags struct {
	name            string
	admin, observer bool
}

func readGroupFlags(ctx context.Context, d *Deps, id int64) (*groupFlags, error) {
	var g groupFlags
	err := d.DB.QueryRow(ctx, `SELECT name, is_admin, is_observer FROM groups WHERE id = $1`, id).Scan(&g.name, &g.admin, &g.observer)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &g, err
}

// addToGroup is routes/users.py add_to_group: joining an administrators' or observers' group
// is receiving that role (not with a token).
func addToGroup(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "user_id", "group_id")
		if !ok {
			return
		}
		ctx, uid, gid := r.Context(), ids[0], ids[1]
		u, err := readUser(ctx, d.DB, uid)
		if err != nil {
			fail(d, w, "reading the user", err)
			return
		}
		if u == nil {
			detail(w, http.StatusNotFound, "User not found")
			return
		}
		g, err := readGroupFlags(ctx, d, gid)
		if err != nil {
			fail(d, w, "reading the group", err)
			return
		}
		if g == nil {
			detail(w, http.StatusNotFound, "Group not found")
			return
		}
		var member bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM user_groups WHERE user_id = $1 AND group_id = $2)`, uid, gid).Scan(&member); err != nil {
			fail(d, w, "reading the membership", err)
			return
		}
		if member {
			returnsNone(w)
			return
		}
		if g.admin || g.observer {
			if refusal := apitokens.DenyIfFromToken(r, p, apitokens.NoRolesFromToken); refusal != nil {
				server.Refuse(w, refusal)
				return
			}
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "adding to the group", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		_, err = tx.Exec(ctx, `INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, uid, gid)
		if err == nil && g.observer {
			_, err = apitokens.WithdrawIfObserver(ctx, tx, []int64{uid})
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			fail(d, w, "adding to the group", err)
			return
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "group.member_add", TargetType: ptr("user"),
			TargetID: &uid, TargetLabel: &u.Email, Detail: membershipDetail(g.name, gid, g.admin, g.observer)})
		if g.admin {
			// joining an administrators' group IS a promotion
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "group.admin_join", TargetType: ptr("user"),
				TargetID: &uid, TargetLabel: &u.Email, Detail: pyjson.Object{{Key: "group", Value: g.name}}})
		}
		returnsNone(w)
	}
}

// removeFromGroup is routes/users.py remove_from_group: nothing to do without the
// membership; never one's own last way to the administrators' role.
func removeFromGroup(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "user_id", "group_id")
		if !ok {
			return
		}
		ctx, uid, gid := r.Context(), ids[0], ids[1]
		var member bool
		if err := d.DB.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM user_groups WHERE user_id = $1 AND group_id = $2)`, uid, gid).Scan(&member); err != nil {
			fail(d, w, "reading the membership", err)
			return
		}
		if !member {
			returnsNone(w)
			return
		}
		g, err := readGroupFlags(ctx, d, gid)
		if err != nil {
			fail(d, w, "reading the group", err)
			return
		}
		u, err := readUser(ctx, d.DB, uid)
		if err != nil {
			fail(d, w, "reading the user", err)
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "removing from the group", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		if _, err := tx.Exec(ctx, `DELETE FROM user_groups WHERE user_id = $1 AND group_id = $2`, uid, gid); err != nil {
			fail(d, w, "removing from the group", err)
			return
		}
		if uid == p.User.ID {
			if refusal, err := stillAdmin(ctx, tx, &p.User); err != nil || refusal != nil {
				refuseOr500(d, w, refusal, err)
				return
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail(d, w, "removing from the group", err)
			return
		}
		var label *string
		if u != nil {
			label = &u.Email
		}
		detailOf := pyjson.Object{{Key: "group_id", Value: gid}}
		if g != nil {
			detailOf = membershipDetail(g.name, gid, g.admin, g.observer)
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "group.member_remove", TargetType: ptr("user"),
			TargetID: &uid, TargetLabel: label, Detail: detailOf})
		if g != nil && g.admin && u != nil {
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "group.admin_leave", TargetType: ptr("user"),
				TargetID: &uid, TargetLabel: &u.Email, Detail: pyjson.Object{{Key: "group", Value: g.name}}})
		}
		returnsNone(w)
	}
}
