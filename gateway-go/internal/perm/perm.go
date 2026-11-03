// Package perm is services/permissions.py: what a person may do on a folder, with the grants
// inherited down the tree. A superuser (by flag or by an administrators' group) may do
// everything; a grant to the person or to one of their groups holds on the folder and on all
// its descendants; VIEW < RUN < EDIT < MANAGE, CONNECT apart, MANAGE includes everything.
//
// A Checker belongs to one request, as the Python per-request memo does: the tree, the
// grants and the answers are read once and reused; a route that changes grants or folders
// makes a new one (the Python memo is forgotten at every flush).
package perm

import (
	"context"
	"fmt"
	"net/http"
	"sort"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
)

// Capability is models/permission.py Capability.
type Capability string

const (
	View    Capability = "view"
	Run     Capability = "run"
	Edit    Capability = "edit"
	Connect Capability = "connect"
	Manage  Capability = "manage"
)

var rank = map[Capability]int{View: 0, Run: 1, Edit: 2, Manage: 3}

// Satisfies is grant_satisfies: does a granted capability give the needed one?
func Satisfies(granted, needed Capability) bool {
	switch {
	case granted == Manage:
		return true
	case needed == Connect:
		return granted == Connect
	case granted == Connect:
		return needed == Connect
	}
	g, okG := rank[granted]
	n, okN := rank[needed]
	return okG && okN && g >= n
}

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

type grant struct {
	project int64
	cap     Capability
}

// Checker answers one person's questions within one request.
type Checker struct {
	q       Querier
	user    *auth.User
	roles   *roles.Roles
	parents map[int64]*int64
	grants  []grant
	granted map[Capability]map[int64]bool
}

// For starts the answers of `user` over `q`.
func For(q Querier, user *auth.User) *Checker {
	return &Checker{q: q, user: user, granted: map[Capability]map[int64]bool{}}
}

// Roles is the person's effective roles (permissions.is_admin, is_observer).
func (c *Checker) Roles(ctx context.Context) (roles.Roles, error) {
	if c.roles == nil {
		r, err := roles.Of(ctx, c.q, c.user.ID, c.user.IsSuperuser, c.user.IsObserver)
		if err != nil {
			return roles.Roles{}, err
		}
		c.roles = &r
	}
	return *c.roles, nil
}

// IsAdmin is permissions.is_admin.
func (c *Checker) IsAdmin(ctx context.Context) (bool, error) {
	r, err := c.Roles(ctx)
	return r.Admin, err
}

// Parents is folder_parents: the tree as {id: parent id}.
func (c *Checker) Parents(ctx context.Context) (map[int64]*int64, error) {
	if c.parents != nil {
		return c.parents, nil
	}
	rows, err := c.q.Query(ctx, `SELECT id, parent_id FROM projects`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	parents := map[int64]*int64{}
	for rows.Next() {
		var id int64
		var parent *int64
		if err := rows.Scan(&id, &parent); err != nil {
			return nil, err
		}
		parents[id] = parent
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	c.parents = parents
	return parents, nil
}

// Ancestors is ancestor_ids: the folder→root chain, itself included, robust to a cycle.
func Ancestors(parents map[int64]*int64, id int64) []int64 {
	var chain []int64
	seen := map[int64]bool{}
	cur := &id
	for cur != nil {
		p, ok := parents[*cur]
		if !ok || seen[*cur] {
			break
		}
		chain = append(chain, *cur)
		seen[*cur] = true
		cur = p
	}
	return chain
}

// Descendants is descendant_ids: every descendant of the roots, the roots included.
func Descendants(parents map[int64]*int64, roots map[int64]bool) map[int64]bool {
	children := map[int64][]int64{}
	for id, parent := range parents {
		if parent != nil {
			children[*parent] = append(children[*parent], id)
		}
	}
	out := map[int64]bool{}
	var stack []int64
	for r := range roots {
		stack = append(stack, r)
	}
	for len(stack) > 0 {
		id := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if out[id] {
			continue
		}
		out[id] = true
		stack = append(stack, children[id]...)
	}
	return out
}

// subjectGrants is _subject_grants: the grants to the person and to their groups.
func (c *Checker) subjectGrants(ctx context.Context) ([]grant, error) {
	if c.grants != nil {
		return c.grants, nil
	}
	rows, err := c.q.Query(ctx, `SELECT project_id, capability FROM permissions
		WHERE user_id = $1 OR group_id IN (SELECT group_id FROM user_groups WHERE user_id = $1)`, c.user.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []grant{}
	for rows.Next() {
		var g grant
		var cp string
		if err := rows.Scan(&g.project, &cp); err != nil {
			return nil, err
		}
		g.cap = Capability(cp)
		out = append(out, g)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	c.grants = out
	return out, nil
}

// grantedTo is _granted: the folders where the capability holds, granted or inherited,
// without their ancestors. For a person who is not an administrator.
func (c *Checker) grantedTo(ctx context.Context, needed Capability) (map[int64]bool, error) {
	if g, ok := c.granted[needed]; ok {
		return g, nil
	}
	parents, err := c.Parents(ctx)
	if err != nil {
		return nil, err
	}
	grants, err := c.subjectGrants(ctx)
	if err != nil {
		return nil, err
	}
	roots := map[int64]bool{}
	for _, g := range grants {
		if _, ok := parents[g.project]; ok && Satisfies(g.cap, needed) {
			roots[g.project] = true
		}
	}
	out := Descendants(parents, roots)
	c.granted[needed] = out
	return out, nil
}

// Has is has_capability.
func (c *Checker) Has(ctx context.Context, projectID int64, needed Capability) (bool, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil || admin {
		return admin, err
	}
	g, err := c.grantedTo(ctx, needed)
	return g[projectID], err
}

// Projects is _granted_project_ids: every folder for an administrator.
func (c *Checker) Projects(ctx context.Context, needed Capability) (map[int64]bool, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	if admin {
		parents, err := c.Parents(ctx)
		if err != nil {
			return nil, err
		}
		out := make(map[int64]bool, len(parents))
		for id := range parents {
			out[id] = true
		}
		return out, nil
	}
	g, err := c.grantedTo(ctx, needed)
	if err != nil {
		return nil, err
	}
	out := make(map[int64]bool, len(g))
	for id := range g {
		out[id] = true
	}
	return out, nil
}

// Readable is readable_project_ids: whose CONTENT the person may read (VIEW or above).
func (c *Checker) Readable(ctx context.Context) (map[int64]bool, error) { return c.Projects(ctx, View) }

// Visible is visible_project_ids: the readable folders and their ancestors, these only to
// make the tree navigable (their content is not readable).
func (c *Checker) Visible(ctx context.Context) (map[int64]bool, error) {
	parents, err := c.Parents(ctx)
	if err != nil {
		return nil, err
	}
	visible, err := c.Readable(ctx)
	if err != nil {
		return nil, err
	}
	for id := range copySet(visible) {
		for _, a := range Ancestors(parents, id) {
			visible[a] = true
		}
	}
	return visible, nil
}

// CanUpload is can_upload: EDIT somewhere.
func (c *Checker) CanUpload(ctx context.Context) (bool, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil || admin {
		return admin, err
	}
	g, err := c.grantedTo(ctx, Edit)
	return len(g) > 0, err
}

// Ensure is deps/permissions.py ensure_can: the 403 that names the capability and the folder.
func (c *Checker) Ensure(ctx context.Context, projectID int64, needed Capability) (*auth.Refusal, error) {
	ok, err := c.Has(ctx, projectID, needed)
	if err != nil {
		return nil, err
	}
	if !ok {
		return &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("Missing '%s' permission on folder %d", needed, projectID)}, nil
	}
	return nil, nil
}

// Sorted returns a set's ids in order (for `IN` clauses and stable answers).
func Sorted(set map[int64]bool) []int64 {
	out := make([]int64, 0, len(set))
	for id := range set {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func copySet(s map[int64]bool) map[int64]bool {
	out := make(map[int64]bool, len(s))
	for k := range s {
		out[k] = true
	}
	return out
}
