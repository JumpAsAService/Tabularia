// Package flowaccess is who may open a flow and what a save may write into one:
// services/flow_access.py, registry_access.py (the container registries granted), the
// scripting environments and saved scripts a person may use (script_nodes.can_use,
// saved_scripts.can_see), and the checks of a save (script_nodes.check_definition,
// registry_access.check_definition, container_nodes.check_definition).
//
// An Access belongs to one request, as the Python per-request memo does.
package flowaccess

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Settings are the installation's switches the checks read (SCRIPTS__*, CONTAINERS__*).
type Settings struct {
	ScriptsEnabled    bool
	ScriptsNamespace  string
	ScriptsRegistry   string
	ContainersEnabled bool
	MaxCPU            string
	MaxMemory         string
	MaxTimeoutSeconds int64
}

// Access answers one person's questions within one request.
type Access struct {
	Q    perm.Querier
	C    *perm.Checker
	User *auth.User
	Set  Settings

	grants map[int64][]string // registry → patterns; nil slice = all its repositories
	envs   map[int64]bool     // usable_environment_ids (not for an administrator)
}

// New is an Access over the request's checker.
func New(q perm.Querier, c *perm.Checker, user *auth.User, s Settings) *Access {
	return &Access{Q: q, C: c, User: user, Set: s}
}

// Refuse is an HTTPException raised by a check.
func refuse(status int, detail string) *auth.Refusal {
	return &auth.Refusal{Status: status, Detail: detail}
}

// ── registry_access ────────────────────────────────────────────────────────────

func patterns(text string) []string {
	var out []string
	for _, x := range pySplitLines(text) {
		if x = pyStrip(x); x != "" {
			out = append(out, x)
		}
	}
	return out
}

// registryGrants is registry_access._grants: registry id → the patterns granted, nil for
// all of them (a present key with a nil slice).
func (a *Access) registryGrants(ctx context.Context) (map[int64][]string, error) {
	if a.grants != nil {
		return a.grants, nil
	}
	rows, err := a.Q.Query(ctx, `SELECT registry_id, group_id, user_id, repositories FROM container_registry_access
		WHERE user_id = $1 OR group_id IN (SELECT group_id FROM user_groups WHERE user_id = $1) ORDER BY id`, a.User.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64][]string{}
	all := map[int64]bool{}
	for rows.Next() {
		var rid int64
		var gid, uid *int64
		var repos string
		if err := rows.Scan(&rid, &gid, &uid, &repos); err != nil {
			return nil, err
		}
		mine := patterns(repos)
		if len(mine) == 0 || all[rid] {
			out[rid], all[rid] = nil, true
		} else {
			out[rid] = append(out[rid], mine...)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	a.grants = out
	return out, nil
}

// Registry is a container_registries row, the columns the checks read.
type Registry struct {
	ID           int64
	Name         string
	Host         string
	Repositories string
	Enabled      bool
}

func (a *Access) registry(ctx context.Context, id int64) (*Registry, error) {
	var r Registry
	err := a.Q.QueryRow(ctx, `SELECT id, name, host, repositories, enabled FROM container_registries WHERE id = $1`, id).
		Scan(&r.ID, &r.Name, &r.Host, &r.Repositories, &r.Enabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

// CanUseRegistry is registry_access.can_use.
func (a *Access) CanUseRegistry(ctx context.Context, r *Registry) (bool, error) {
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	g, err := a.registryGrants(ctx)
	if err != nil {
		return false, err
	}
	_, ok := g[r.ID]
	return ok, nil
}

// RepositoryName is container_registry.repository_name.
func RepositoryName(host, repository string) string {
	repository = strings.Trim(pyStrip(repository), "/")
	if (host == "docker.io" || host == "index.docker.io") && !strings.Contains(repository, "/") {
		return "library/" + repository
	}
	return repository
}

// Allowed is container_registry.allowed.
func Allowed(pats []string, repository string) bool {
	for _, p := range pats {
		p = strings.Trim(pyStrip(p), "/")
		if p == "*" || p == repository || (strings.HasSuffix(p, "/*") && strings.HasPrefix(repository, p[:len(p)-1])) {
			return true
		}
	}
	return false
}

// RepositoryAllowed is registry_access.repository_allowed.
func (a *Access) RepositoryAllowed(ctx context.Context, r *Registry, repository string) (bool, error) {
	name := RepositoryName(r.Host, repository)
	if !Allowed(patterns(r.Repositories), name) {
		return false, nil
	}
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	g, err := a.registryGrants(ctx)
	if err != nil {
		return false, err
	}
	mine, ok := g[r.ID]
	if !ok {
		return false, nil
	}
	return mine == nil || Allowed(mine, name), nil
}

type reference struct {
	registry   *int64
	repository string
	label      string
}

// references is registry_access.references.
func references(definition string) []reference {
	doc := pyjson.Obj(parse(definition, "{}"))
	nodes, ok := doc.Value("nodes").([]any)
	if doc == nil || !ok {
		return nil
	}
	var out []reference
	for _, n := range nodes {
		node := pyjson.Obj(n)
		if node == nil || node.Value("type") != "container" {
			continue
		}
		d := pyjson.Obj(pyjson.Or(node.Value("data"), pyjson.Object{}))
		var rid *int64
		if v, ok := pyjson.IsInt(d.Value("registryId")); ok {
			rid = &v
		}
		out = append(out, reference{rid, pyjson.Str(pyjson.Or(d.Value("repository"), "")),
			pyjson.Str(pyjson.Or(pyjson.Or(d.Value("label"), node.Value("id")), ""))})
	}
	return out
}

// registryBlocked is registry_access.blocked.
func (a *Access) registryBlocked(ctx context.Context, definition string) (string, error) {
	if definition == "" || !strings.Contains(definition, `"container"`) {
		return "", nil
	}
	refs := references(definition)
	if len(refs) == 0 {
		return "", nil
	}
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return "", err
	}
	for _, ref := range refs {
		if ref.registry == nil {
			continue
		}
		r, err := a.registry(ctx, *ref.registry)
		if err != nil {
			return "", err
		}
		if r == nil {
			continue
		}
		if ok, err := a.CanUseRegistry(ctx, r); err != nil {
			return "", err
		} else if !ok {
			return fmt.Sprintf("This flow runs images of the container registry «%s», which you have no access to: an administrator can grant it", r.Name), nil
		}
		if ref.repository != "" {
			if ok, err := a.RepositoryAllowed(ctx, r, ref.repository); err != nil {
				return "", err
			} else if !ok {
				return fmt.Sprintf("This flow runs the image %s of «%s», which you have no access to: an administrator can grant it",
					RepositoryName(r.Host, ref.repository), r.Name), nil
			}
		}
	}
	return "", nil
}

// RegistryCheckDefinition is registry_access.check_definition.
func (a *Access) RegistryCheckDefinition(ctx context.Context, before, after string) (*auth.Refusal, error) {
	if !a.Set.ContainersEnabled {
		return nil, nil
	}
	if r := checkContainerDefinition(after, a.Set); r != nil {
		return r, nil
	}
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return nil, err
	}
	type key struct {
		rid  int64
		has  bool
		repo string
	}
	k := func(r reference) key {
		if r.registry == nil {
			return key{repo: r.repository}
		}
		return key{*r.registry, true, r.repository}
	}
	old := map[key]bool{}
	for _, r := range references(before) {
		old[k(r)] = true
	}
	for _, ref := range references(after) {
		if old[k(ref)] || (ref.registry == nil && ref.repository == "") {
			continue
		}
		var r *Registry
		if ref.registry != nil {
			var err error
			if r, err = a.registry(ctx, *ref.registry); err != nil {
				return nil, err
			}
		}
		if r == nil || !r.Enabled {
			return refuse(422, fmt.Sprintf("Container node «%s»: choose a registry that is in use", ref.label)), nil
		}
		if ok, err := a.CanUseRegistry(ctx, r); err != nil {
			return nil, err
		} else if !ok {
			return refuse(403, fmt.Sprintf("Container node «%s»: you have no access to the container registry «%s»", ref.label, r.Name)), nil
		}
		ok := false
		if ref.repository != "" {
			var err error
			if ok, err = a.RepositoryAllowed(ctx, r, ref.repository); err != nil {
				return nil, err
			}
		}
		if !ok {
			return refuse(403, fmt.Sprintf("Container node «%s»: the image «%s» is not among those of «%s» you may use",
				ref.label, ref.repository, r.Name)), nil
		}
	}
	return nil, nil
}

// ── scripting environments and saved scripts ─────────────────────────────────

// UsableEnvironments is saved_scripts.usable_environment_ids; nil for an administrator.
func (a *Access) UsableEnvironments(ctx context.Context) (map[int64]bool, error) {
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return nil, err
	}
	if a.envs != nil {
		return a.envs, nil
	}
	rows, err := a.Q.Query(ctx, `SELECT seg.environment_id FROM script_environment_groups seg
		JOIN user_groups ug ON ug.group_id = seg.group_id WHERE ug.user_id = $1`, a.User.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]bool{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	a.envs = out
	return out, nil
}

// Environment is a script_environments row, the columns the checks read.
type Environment struct {
	ID                int64
	Name              string
	Language          string
	MaxTimeoutSeconds int64
}

func (a *Access) environment(ctx context.Context, id int64) (*Environment, error) {
	var e Environment
	err := a.Q.QueryRow(ctx, `SELECT id, name, language, max_timeout_seconds FROM script_environments WHERE id = $1`, id).
		Scan(&e.ID, &e.Name, &e.Language, &e.MaxTimeoutSeconds)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &e, err
}

// CanUseEnvironment is script_nodes.can_use: an administrator, or a group of the
// environment's.
func (a *Access) CanUseEnvironment(ctx context.Context, envID int64) (bool, error) {
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	var one int
	err := a.Q.QueryRow(ctx, `SELECT 1 FROM script_environment_groups seg JOIN user_groups ug ON ug.group_id = seg.group_id
		WHERE seg.environment_id = $1 AND ug.user_id = $2 LIMIT 1`, envID, a.User.ID).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// SavedScript is a saved_scripts row, the columns the checks read.
type SavedScript struct {
	ID            int64
	Name          string
	ProjectID     int64
	EnvironmentID int64
}

// SavedScriptByID reads a saved script, nil when it does not exist.
func (a *Access) SavedScriptByID(ctx context.Context, id int64) (*SavedScript, error) {
	var s SavedScript
	err := a.Q.QueryRow(ctx, `SELECT id, name, project_id, environment_id FROM saved_scripts WHERE id = $1`, id).
		Scan(&s.ID, &s.Name, &s.ProjectID, &s.EnvironmentID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &s, err
}

// CanSeeScript is saved_scripts.can_see: its environment usable and VIEW on its folder.
func (a *Access) CanSeeScript(ctx context.Context, s *SavedScript) (bool, error) {
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	envs, err := a.UsableEnvironments(ctx)
	if err != nil || !envs[s.EnvironmentID] {
		return false, err
	}
	return a.C.Has(ctx, s.ProjectID, perm.View)
}

// ── flow_access ────────────────────────────────────────────────────────────────

func scriptEnvironments(definition string) []int64 {
	doc := pyjson.Obj(parse(definition, ""))
	nodes, ok := doc.Value("nodes").([]any)
	if doc == nil || !ok {
		return nil
	}
	var out []int64
	seen := map[int64]bool{}
	for _, n := range nodes {
		node := pyjson.Obj(n)
		if node == nil || node.Value("type") != "script" {
			continue
		}
		if eid, ok := pyjson.IsInt(pyjson.Obj(pyjson.Or(node.Value("data"), pyjson.Object{})).Value("environmentId")); ok && !seen[eid] {
			seen[eid] = true
			out = append(out, eid)
		}
	}
	return out
}

// Blocked is flow_access.blocked: why this person may not open a flow with this
// definition, "" when they may.
func (a *Access) Blocked(ctx context.Context, definition string) (string, error) {
	if definition == "" || (!strings.Contains(definition, `"container"`) && !strings.Contains(definition, `"script"`)) {
		return "", nil
	}
	if admin, err := a.C.IsAdmin(ctx); err != nil || admin {
		return "", err
	}
	if why, err := a.registryBlocked(ctx, definition); err != nil || why != "" {
		return why, err
	}
	for _, eid := range scriptEnvironments(definition) {
		env, err := a.environment(ctx, eid)
		if err != nil {
			return "", err
		}
		if env == nil {
			continue
		}
		if ok, err := a.CanUseEnvironment(ctx, env.ID); err != nil {
			return "", err
		} else if !ok {
			return fmt.Sprintf("This flow runs scripts in the environment «%s», which you have no access to: "+
				"an administrator can grant it", env.Name), nil
		}
	}
	if strings.Contains(definition, `"scriptId"`) {
		for _, node := range ScriptNodes(definition) {
			sid, ok := SavedScriptID(node)
			if !ok {
				continue
			}
			s, err := a.SavedScriptByID(ctx, sid)
			if err != nil {
				return "", err
			}
			if s == nil {
				continue
			}
			if ok, err := a.CanSeeScript(ctx, s); err != nil {
				return "", err
			} else if !ok {
				return "This flow runs a saved script you have no access to", nil
			}
		}
	}
	return "", nil
}

// EnsureCanOpen is flow_access.ensure_can_open: the 403 with the reason.
func (a *Access) EnsureCanOpen(ctx context.Context, definition string) (*auth.Refusal, error) {
	why, err := a.Blocked(ctx, definition)
	if err != nil || why == "" {
		return nil, err
	}
	return refuse(http.StatusForbidden, why), nil
}

// parse is json.loads(definition or fallback); nil when it is not JSON.
func parse(definition, fallback string) any {
	if definition == "" {
		definition = fallback
	}
	v, err := pyjson.Loads(definition)
	if err != nil {
		return nil
	}
	return v
}
