package api

// The statistical models' groups in Administration (docs/specs/statistical-models.md, «Model
// groups and their images»): an administrator enables a group, which creates its managed
// scripting environment (script_environments.managed_by = model_group:<id>) and queues its
// version; the build loop of the scripting environments (jobs_script_builds.go) builds the
// image and runs the group's self-test in the check Job. The routes here list the groups, enable
// and disable them, build again, set their resources and limits, and say who may use them;
// GET /models/groups is what the flow editor's palette reads.
//
// Only an administrator acts, and not with an API token; an observer reads the section. Every
// action is audited (models.group_enable, models.group_disable, models.group_rebuild,
// models.group_settings, models.group_access).
//
// For the nodes in runs: ReadyModelGroupImage says which image a group's Job runs, and
// ModelGroupUsable whether a person may use the group (models_groups_image.go).

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// mgManagedPrefix starts script_environments.managed_by of a group's environment.
const mgManagedPrefix = "model_group:"

func mgManagedBy(group string) string { return mgManagedPrefix + group }

// mgGroupOfManaged is the group a managed_by value names, "" for an ordinary environment.
func mgGroupOfManaged(managedBy *string) string {
	if managedBy == nil || !strings.HasPrefix(*managedBy, mgManagedPrefix) {
		return ""
	}
	return strings.TrimPrefix(*managedBy, mgManagedPrefix)
}

var mgNoModelsFromToken = map[string]string{
	"en": "An API token cannot change the statistical models: do it from the interface",
	"it": "Un token API non cambia i modelli statistici: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier les modèles statistiques : faites-le depuis l'interface",
	"de": "Ein API-Token kann die statistischen Modelle nicht ändern: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar los modelos estadísticos: hazlo desde la interfaz",
}

// mgGuarded is what an administrator's route (admin: and not with an API token) or an observer's
// (read-only) runs before its handler.
func mgGuarded(d *Deps, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	var noToken map[string]string
	if admin {
		noToken = mgNoModelsFromToken
	}
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if scRequire(d, w, r, p, admin, noToken) {
			h(d, w, r, p)
		}
	}
}

func init() {
	register("models_admin", func(d *Deps) []server.Route {
		route := func(method, pattern string, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, mgGuarded(d, admin, h))}
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/models/groups", Handler: server.Authed(d.Auth, "/models/groups",
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { mgPalette(d, w, r, p) })},
			route(http.MethodGet, "/admin/models", false, mgList),
			route(http.MethodGet, "/admin/models/{group_id}", false, mgDetail),
			route(http.MethodPost, "/admin/models/{group_id}/enable", true, mgEnable),
			route(http.MethodPost, "/admin/models/{group_id}/disable", true, mgDisable),
			route(http.MethodPost, "/admin/models/{group_id}/rebuild", true, mgRebuild),
			route(http.MethodPut, "/admin/models/{group_id}/settings", true, mgSettings),
			route(http.MethodPut, "/admin/models/{group_id}/access", true, mgAccess),
		}
	})
}

// ── what a group is now ──────────────────────────────────────────────────────

// mgVersion is a row of script_environment_versions of a group's environment.
type mgVersion struct {
	ID, Number            int64
	Status                string
	RecipeHash            *string
	RunnerHash            *string
	ImageRef, Arch, Error *string
	Packages              int // the installed packages, from the lock
	Lock, BuildLog        *string
	SystemLock            *string // the lock's lines of the group's system libraries only: the list has no lock, and its card names their versions
	Selftest              *string
	JobName               *string // a ready version with a Job: its self-test is being run again (a new runner)
	// the rechecks of the version in use, for the runner RecheckRunner, that could not run: how
	// many, not before when the next, why the last (recheckLater)
	RecheckRunner         *string
	RecheckAttempts       int64
	RecheckAfter          *time.Time
	RecheckError          *string
	CreatedAt             time.Time
	CreatedBy             *string
	StartedAt, FinishedAt *time.Time
	PackagesText          string // the packages as queued: for a group with weights, with its `# tabularia:` lines
}

// mgState is a group as the database has it: the row of model_groups, its managed
// environment and the versions of that, newest first.
type mgState struct {
	Group      modelgroups.Group
	HasRow     bool
	Enabled    bool
	Everyone   bool
	EnvID      int64 // 0: never enabled
	MaxRows    *int64
	EnabledBy  *string
	EnabledAt  *time.Time
	CPULimit   string
	Memory     string
	MaxSeconds int64
	CurrentID  *int64
	Versions   []mgVersion
	// Accepted: the valid acceptance of the licence of the weights the audit holds (nil: none, and for a group without weights)
	Accepted *mgAcceptance
}

// mgLoad reads a group. withText: the versions' lock, build log and self-test as well; keep:
// at most that many of the newest versions (0: all). The environment's current version is always
// among them, read by its id when it is older than those kept: however many newer versions failed
// since, the version in use still serves, and the list, the palette and the detail say the same.
func mgLoad(ctx context.Context, q perm.Querier, g modelgroups.Group, withText bool, keep int) (*mgState, error) {
	s := &mgState{Group: g, Everyone: true}
	var err error
	if s.Accepted, err = mgAuditAcceptance(ctx, q, g); err != nil {
		return nil, err
	}
	var env, seconds *int64
	var cpu, memory *string
	err = q.QueryRow(ctx, `SELECT m.enabled, m.everyone, m.environment_id, m.max_rows, m.enabled_by, m.enabled_at,
		e.cpu_limit, e.memory_limit, e.max_timeout_seconds, e.current_version_id
		FROM model_groups m LEFT JOIN script_environments e ON e.id = m.environment_id WHERE m.id = $1`, g.ID).Scan(
		&s.Enabled, &s.Everyone, &env, &s.MaxRows, &s.EnabledBy, &s.EnabledAt, &cpu, &memory, &seconds, &s.CurrentID)
	if scNoRows(err) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	s.HasRow = true
	if env != nil {
		s.EnvID = *env
	}
	if cpu != nil {
		s.CPULimit = *cpu
	}
	if memory != nil {
		s.Memory = *memory
	}
	if seconds != nil {
		s.MaxSeconds = *seconds
	}
	if s.EnvID == 0 {
		return s, nil
	}
	text := "NULL::text, NULL::text, NULL::text"
	if withText {
		text = "lock, build_log, selftest"
	}
	limit := ""
	if keep > 0 {
		limit = fmt.Sprintf(" LIMIT %d", keep)
	}
	sysLike := []string{}
	for _, p := range g.Recipe.SystemLibraries {
		sysLike = append(sysLike, p+"@%")
	}
	columns := `SELECT id, number, status, recipe_hash, runner_hash, image_ref, arch, error,
		coalesce(array_length(regexp_split_to_array(nullif(lock, ''), E'\n'), 1), 0), ` + text + `,
		(SELECT string_agg(l, E'\n') FROM unnest(regexp_split_to_array(coalesce(lock, ''), E'\n')) AS l WHERE l LIKE ANY ($2)),
		job_name, recheck_runner, recheck_attempts, recheck_after, recheck_error, created_at, created_by, started_at, finished_at,
		coalesce(packages, '')
		FROM script_environment_versions`
	rows, err := q.Query(ctx, columns+` WHERE environment_id = $1 ORDER BY number DESC`+limit, s.EnvID, sysLike)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	scan := func(row pgx.Row) (mgVersion, error) {
		var v mgVersion
		err := row.Scan(&v.ID, &v.Number, &v.Status, &v.RecipeHash, &v.RunnerHash, &v.ImageRef, &v.Arch, &v.Error, &v.Packages,
			&v.Lock, &v.BuildLog, &v.Selftest, &v.SystemLock, &v.JobName, &v.RecheckRunner, &v.RecheckAttempts, &v.RecheckAfter, &v.RecheckError,
			&v.CreatedAt, &v.CreatedBy, &v.StartedAt, &v.FinishedAt, &v.PackagesText)
		return v, err
	}
	hasCurrent := false
	for rows.Next() {
		v, err := scan(rows)
		if err != nil {
			return nil, err
		}
		hasCurrent = hasCurrent || s.CurrentID != nil && v.ID == *s.CurrentID
		s.Versions = append(s.Versions, v)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rows.Close()
	if s.CurrentID != nil && !hasCurrent {
		// older than every version kept: it goes last, the list stays newest first
		v, err := scan(q.QueryRow(ctx, columns+` WHERE environment_id = $1 AND id = $3`, s.EnvID, sysLike, *s.CurrentID))
		if err != nil && !scNoRows(err) {
			return nil, err
		}
		if err == nil {
			s.Versions = append(s.Versions, v)
		}
	}
	return s, nil
}

func mgInProgress(status string) bool {
	return status == "queued" || status == "building" || status == "checking"
}

// current is the version the group's Jobs run: the environment's current one, when it is ready
// and built from the recipe this Tabularia ships.
func (s *mgState) current() *mgVersion {
	for i := range s.Versions {
		v := &s.Versions[i]
		if s.CurrentID != nil && v.ID == *s.CurrentID && v.Status == "ready" && v.RecipeHash != nil && *v.RecipeHash == s.Group.RecipeHash {
			return v
		}
	}
	return nil
}

// recheck is where the self-test of a ready version with the runner this Tabularia ships is:
// "rechecking" (its Job runs), "recheck_retrying" (one could not run, the next starts at
// RecheckAfter), "recheck_stopped" (as many as are tried could not run: the version serves,
// self-tested with its previous runner), "" when there is none (passed, or not started yet).
func (v *mgVersion) recheck(g modelgroups.Group) string {
	switch {
	case v.Status != "ready":
		return ""
	case v.JobName != nil:
		return "rechecking"
	case v.RunnerHash != nil && *v.RunnerHash == g.RunnerHash, v.RecheckRunner == nil || *v.RecheckRunner != g.RunnerHash, v.RecheckAttempts == 0:
		return ""
	case v.RecheckAttempts > mgRecheckRetries:
		return "recheck_stopped"
	}
	return "recheck_retrying"
}

// state is what the card says: not_enabled, building, checking, ready, failed or
// rebuild_needed (the shipped recipe changed and the new version is not queued yet), licence_pending
// (the same, for a group whose weights have a licence no administrator has accepted: nothing is
// queued until one does); for a ready
// group whose version in use is being self-tested again with a new runner, rechecking or
// recheck_retrying (it serves meanwhile).
func (s *mgState) state() string {
	if !s.Enabled {
		return "not_enabled"
	}
	if len(s.Versions) == 0 {
		return "building"
	}
	switch latest := s.Versions[0]; {
	case latest.Status == "checking":
		return "checking"
	case mgInProgress(latest.Status):
		return "building"
	case s.current() != nil:
		if r := s.current().recheck(s.Group); r == "rechecking" || r == "recheck_retrying" {
			return r
		}
		return "ready"
	case latest.Status == "failed":
		return "failed"
	}
	if s.Group.Recipe.Weights != nil && s.Accepted == nil {
		// a build is due and Tabularia does not start one without an acceptance of the licence in the audit
		return "licence_pending"
	}
	return "rebuild_needed"
}

// effective limits: the administrator's, else the recipe's.
func (s *mgState) maxRows() int64 {
	if s.MaxRows != nil {
		return *s.MaxRows
	}
	return s.Group.Recipe.MaxRows
}

func (s *mgState) maxSeconds() int64 {
	if s.MaxSeconds > 0 {
		return s.MaxSeconds
	}
	return mgDefaultSeconds(s.Group)
}

func mgDefaultSeconds(g modelgroups.Group) int64 {
	if g.Recipe.MaxSeconds > 0 {
		return g.Recipe.MaxSeconds
	}
	return 3600
}

// ── the answers ──────────────────────────────────────────────────────────────

type mgVersionOut struct {
	Number  int64  `json:"number"`
	Status  string `json:"status"`
	Current bool   `json:"current"`
	Stale   bool   `json:"stale"` // built from another recipe than the one shipped
	// Rechecking: the runner changed and the version in use, still serving, is being self-tested again on the same image
	Rechecking bool `json:"rechecking"`
	// Recheck: rechecking, recheck_retrying (one could not run: tried again from RecheckAfter),
	// recheck_stopped (none could: it serves, self-tested with its previous runner), or ""; the
	// attempts that could not run and why the last one did not
	Recheck         string  `json:"recheck"`
	RecheckAttempts int64   `json:"recheck_attempts"`
	RecheckMax      int64   `json:"recheck_max_attempts"`
	RecheckAfter    *string `json:"recheck_after"`
	RecheckError    *string `json:"recheck_error"`
	Error           *string `json:"error"`
	ImageRef        *string `json:"image_ref"`
	Packages        int     `json:"packages"`
	CreatedAt       string  `json:"created_at"`
	CreatedBy       *string `json:"created_by"`
	StartedAt       *string `json:"started_at"`
	FinishedAt      *string `json:"finished_at"`
	Lock            *string `json:"lock,omitempty"`
	BuildLog        *string `json:"build_log,omitempty"`
	Selftest        *string `json:"selftest,omitempty"`
}

type mgLimits struct {
	CPULimit          string `json:"cpu_limit"`
	MemoryLimit       string `json:"memory_limit"`
	MaxTimeoutSeconds int64  `json:"max_timeout_seconds"`
	MaxRows           int64  `json:"max_rows"`
}

type mgGroupOut struct {
	ID             string         `json:"id"`
	Language       string         `json:"language"`
	Base           string         `json:"base"`
	Models         []string       `json:"models"`
	Packages       int            `json:"packages"` // the recipe's, pinned
	Enabled        bool           `json:"enabled"`
	State          string         `json:"state"`
	Ready          bool           `json:"ready"`
	CurrentVersion *int64         `json:"current_version"`
	Limits         mgLimits       `json:"limits"`
	Defaults       mgLimits       `json:"defaults"`
	Everyone       bool           `json:"everyone"`
	GroupIDs       []int64        `json:"group_ids"`
	Groups         []string       `json:"groups"`
	EnabledBy      *string        `json:"enabled_by"`
	EnabledAt      *string        `json:"enabled_at"`
	Versions       []mgVersionOut `json:"versions"`

	// the Debian libraries the group's image installs besides the fixed list (an empty list for most groups)
	SystemLibraries []modelgroups.SystemLibrary `json:"system_libraries"`

	// the licence of the group's weights (docs/specs/foundation-forecast.md); null for a group without weights
	Licence *mgLicenceOut `json:"licence"`
}

func mgOut(ctx context.Context, q perm.Querier, s *mgState, retentionMinutes int) (mgGroupOut, error) {
	g := s.Group
	def := mgLimits{CPULimit: g.Recipe.Resources.CPU, MemoryLimit: g.Recipe.Resources.Memory,
		MaxTimeoutSeconds: mgDefaultSeconds(g), MaxRows: g.Recipe.MaxRows}
	lim := def
	if s.EnvID != 0 {
		lim = mgLimits{CPULimit: s.CPULimit, MemoryLimit: s.Memory, MaxTimeoutSeconds: s.maxSeconds(), MaxRows: s.maxRows()}
	}
	out := mgGroupOut{ID: g.ID, Language: g.Recipe.Language, Base: g.Recipe.Base, Models: g.Models, Packages: len(g.Recipe.Packages),
		Enabled: s.Enabled, State: s.state(), Limits: lim, Defaults: def, Everyone: s.Everyone, GroupIDs: []int64{}, Groups: []string{},
		EnabledBy: s.EnabledBy, EnabledAt: isoPtr(s.EnabledAt), Versions: []mgVersionOut{},
		SystemLibraries: g.SystemLibraries(nil), Licence: mgLicenceView(s, retentionMinutes)}
	cur := s.current()
	out.Ready = cur != nil
	if cur != nil {
		n := cur.Number
		out.CurrentVersion = &n
	}
	// the versions of the system libraries are the ones the version in use was built with (also
	// when the recipe has changed since and a new one is wanted: the old one still serves)
	for i := range s.Versions {
		if v := &s.Versions[i]; s.CurrentID != nil && v.ID == *s.CurrentID && v.Status == "ready" {
			lock := v.SystemLock
			if v.Lock != nil {
				lock = v.Lock
			}
			out.SystemLibraries = g.SystemLibraries(lock)
		}
	}
	if s.EnvID != 0 {
		ids, names, err := scEnvGroups(ctx, q, s.EnvID)
		if err != nil {
			return out, err
		}
		out.GroupIDs, out.Groups = ids, names
	}
	for _, v := range s.Versions {
		o := mgVersionOut{Number: v.Number, Status: v.Status, Current: s.CurrentID != nil && *s.CurrentID == v.ID,
			Stale: v.RecipeHash == nil || *v.RecipeHash != g.RecipeHash, Rechecking: v.Status == "ready" && v.JobName != nil, Error: v.Error, ImageRef: v.ImageRef, Packages: v.Packages,
			CreatedAt: pyjson.Isoformat(v.CreatedAt), CreatedBy: v.CreatedBy, StartedAt: isoPtr(v.StartedAt), FinishedAt: isoPtr(v.FinishedAt),
			Lock: v.Lock, BuildLog: v.BuildLog, Selftest: v.Selftest, RecheckMax: mgRecheckRetries + 1}
		if o.Recheck = v.recheck(g); o.Recheck == "recheck_retrying" || o.Recheck == "recheck_stopped" {
			o.RecheckAttempts, o.RecheckError = v.RecheckAttempts, v.RecheckError
			if o.Recheck == "recheck_retrying" {
				o.RecheckAfter = isoPtr(v.RecheckAfter)
			}
		}
		out.Versions = append(out.Versions, o)
	}
	return out, nil
}

// mgDisabledReason is why the models cannot be built here: kubernetes (Compose), or
// not_configured (the scripting machinery has no namespace or registry); "" when they can.
func mgDisabledReason() string {
	switch scriptcfg.Get().DisabledReason() {
	case "kubernetes":
		return "kubernetes"
	case "configuration":
		return "not_configured"
	}
	return ""
}

func mgStorageReady(ctx context.Context, q perm.Querier) (bool, error) {
	s, err := scStorageOf(ctx, q)
	if err != nil {
		return false, err
	}
	return s.Bucket != "" && s.AccessKey != "" && s.SecretKeyEnc != nil && *s.SecretKeyEnc != "", nil
}

// mgIsOn is the 409 of an installation that cannot build the groups.
func mgIsOn(w http.ResponseWriter) bool {
	switch mgDisabledReason() {
	case "kubernetes":
		detail(w, http.StatusConflict, "The statistical models need Kubernetes (SCRIPTS__ENABLED)")
		return false
	case "not_configured":
		detail(w, http.StatusConflict, "The statistical models need the scripting environments configured (SCRIPTS__NAMESPACE and SCRIPTS__REGISTRY)")
		return false
	}
	return true
}

func mgGroupOr404(w http.ResponseWriter, r *http.Request) (modelgroups.Group, bool) {
	g, ok := modelgroups.Get(r.PathValue("group_id"))
	if !ok {
		detail(w, http.StatusNotFound, "Model group not found")
	}
	return g, ok
}

// mgPalette is GET /models/groups: what the flow editor's palette reads. Whoever may edit a
// flow asks (an administrator, or someone with EDIT on a folder).
func mgPalette(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		fail(d, w, "models: the roles", err)
		return
	}
	if !admin {
		folders, err := c.Projects(ctx, perm.Edit)
		if err != nil {
			fail(d, w, "models: the folders", err)
			return
		}
		if len(folders) == 0 {
			detail(w, http.StatusForbidden, "Edit access to a folder is required")
			return
		}
	}
	reason := mgDisabledReason()
	if reason == "" {
		if ok, err := mgStorageReady(ctx, d.DB); err != nil {
			fail(d, w, "models: the bucket", err)
			return
		} else if !ok {
			reason = "not_configured" // no bucket: a model Job could not read its input
		}
	}
	type entry struct {
		ID                string `json:"id"`
		Enabled           bool   `json:"enabled"`
		Usable            bool   `json:"usable"`
		Ready             bool   `json:"ready"`
		MaxTimeoutSeconds int64  `json:"max_timeout_seconds"`
		MaxRows           int64  `json:"max_rows"`
		// Sizes: the sizes of the weights the version in use holds (what the node's form offers); null for a
		// group without weights. LicencePending: it waits for an administrator to accept the licence
		Sizes          []string `json:"sizes"`
		LicencePending bool     `json:"licence_pending"`
	}
	groups := []entry{}
	for _, g := range modelgroups.Groups() {
		s, err := mgLoad(ctx, d.DB, g, false, 5)
		if err != nil {
			fail(d, w, "models: a group", err)
			return
		}
		e := entry{ID: g.ID, Enabled: s.Enabled && reason == "", Ready: s.current() != nil, MaxTimeoutSeconds: mgDefaultSeconds(g), MaxRows: g.Recipe.MaxRows}
		if s.EnvID != 0 {
			e.MaxTimeoutSeconds, e.MaxRows = s.maxSeconds(), s.maxRows()
		}
		if g.Recipe.Weights != nil {
			e.LicencePending = s.state() == "licence_pending"
			if cur := s.current(); cur != nil {
				e.Sizes = mgSizesOfVersion(g, cur)
			}
		}
		if e.Enabled {
			e.Usable = s.Everyone || admin
			if !e.Usable {
				if e.Usable, err = scriptnodes.CanUse(ctx, c, d.DB, p.User.ID, s.EnvID); err != nil {
					fail(d, w, "models: who may use a group", err)
					return
				}
			}
		}
		groups = append(groups, e)
	}
	var why any
	if reason != "" {
		why = reason
	}
	server.JSON(w, http.StatusOK, map[string]any{"disabled_reason": why, "groups": groups})
}

// mgList is GET /admin/models: every group shipped, with its latest versions (no logs).
func mgList(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	out := []mgGroupOut{}
	for _, g := range modelgroups.Groups() {
		s, err := mgLoad(ctx, d.DB, g, false, 5)
		if err != nil {
			fail(d, w, "models: a group", err)
			return
		}
		o, err := mgOut(ctx, d.DB, s, d.Config.AuditRetentionMinutes)
		if err != nil {
			fail(d, w, "models: a group", err)
			return
		}
		out = append(out, o)
	}
	mgSend(d, w, r, p, map[string]any{"groups": out})
}

// mgSend adds what every answer of the section says besides its groups.
func mgSend(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body map[string]any) {
	ctx := r.Context()
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		fail(d, w, "models: the roles", err)
		return
	}
	ready, err := mgStorageReady(ctx, d.DB)
	if err != nil {
		fail(d, w, "models: the bucket", err)
		return
	}
	var why any
	if reason := mgDisabledReason(); reason != "" {
		why = reason
	}
	body["disabled_reason"] = why
	body["storage_ready"] = ready
	body["editable"] = admin && p.TokenName == "" && why == nil
	server.JSON(w, http.StatusOK, body)
}

// mgDetail is GET /admin/models/{group_id}: one group with all its versions, their lock, build
// log and self-test.
func mgDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok {
		return
	}
	mgAnswer(d, w, r, p, g)
}

// mgAnswer sends a group with its versions, texts and all.
func mgAnswer(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, g modelgroups.Group) {
	ctx := r.Context()
	s, err := mgLoad(ctx, d.DB, g, true, 20)
	if err != nil {
		fail(d, w, "models: the group", err)
		return
	}
	o, err := mgOut(ctx, d.DB, s, d.Config.AuditRetentionMinutes)
	if err != nil {
		fail(d, w, "models: the group", err)
		return
	}
	mgSend(d, w, r, p, map[string]any{"group": o})
}

// ── enabling, disabling, building again ──────────────────────────────────────

// mgMinCPURequest is the least CPU a group's Job asks for, in millicores: a limit below it is refused
// (a Pod whose request is above its limit is not created).
const mgMinCPURequest = 50

// mgHalf is the request that goes with a limit: half of it (the recipe's rule), in the
// units a Kubernetes quantity has, never above the limit.
func mgHalf(quantity string, cpu bool) string {
	if cpu {
		milli := int64(math.Round(mgCores(quantity) * 1000))
		return fmt.Sprintf("%dm", min(max(milli/2, mgMinCPURequest), milli))
	}
	m := sbMemoryParts.FindStringSubmatch(quantity)
	if m == nil {
		return quantity
	}
	size, _ := strconv.ParseFloat(m[1], 64)
	return fmt.Sprintf("%dMi", max(int64(size*sbBytes[m[2]]/2/(1<<20)), 64))
}

func mgCores(cpu string) float64 { return sbCores(cpu) }

// mgNewVersion queues the group's next version in the transaction: the environment's row held,
// the next number, built from the recipe this Tabularia ships. Without internet: a model Job
// reaches the scripts bucket and nothing else.
//
// A group with weights records, in the packages text, the sizes it is built with and the acceptance
// of the licence it relies on (acc, which it needs); for the others acc is ignored and the packages
// are the recipe's.
func mgNewVersion(ctx context.Context, tx pgx.Tx, envID int64, g modelgroups.Group, who string, acc *mgAcceptance) (int64, error) {
	if g.Recipe.Weights != nil && acc == nil {
		return 0, errors.New("a group with weights is queued with the acceptance of its licence")
	}
	if _, err := tx.Exec(ctx, `SELECT id FROM script_environments WHERE id = $1 FOR UPDATE`, envID); err != nil {
		return 0, err
	}
	images, err := scriptcfg.Get().Images()
	if err != nil {
		return 0, err
	}
	base, err := modelgroups.BaseImage(g.Recipe, images)
	if err != nil {
		return 0, err
	}
	var number int64
	if err := tx.QueryRow(ctx, `SELECT coalesce(max(number), 0) + 1 FROM script_environment_versions WHERE environment_id = $1`, envID).Scan(&number); err != nil {
		return 0, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		created_at, created_by, internet_access, recipe_hash, runner_hash) VALUES ($1, $2, $3, $4, $5, 'queued', $6, $7, false, $8, $9)`,
		envID, number, g.Recipe.Base, base, mgPackagesText(g, acc), time.Now().UTC(), who, g.RecipeHash, g.RunnerHash)
	return number, err
}

// mgLockEnvironment holds the environment's row until the transaction ends: whoever queues a
// version (an administrator's click, the build loop's round) looks at the versions only after
// it, so two of them never queue the same one.
func mgLockEnvironment(ctx context.Context, tx pgx.Tx, envID int64) error {
	_, err := tx.Exec(ctx, `SELECT id FROM script_environments WHERE id = $1 FOR UPDATE`, envID)
	return err
}

// mgInFlight is the number of a version of the environment being built, 0 for none.
func mgInFlight(ctx context.Context, tx pgx.Tx, envID int64) (int64, error) {
	var n int64
	err := tx.QueryRow(ctx, `SELECT number FROM script_environment_versions WHERE environment_id = $1
		AND status IN ('queued', 'building', 'checking') ORDER BY number DESC LIMIT 1`, envID).Scan(&n)
	if scNoRows(err) {
		return 0, nil
	}
	return n, err
}

// mgEnsureEnvironment is the group's managed environment, made when there is none.
//
// Two enables of the same group at once (a double click, two administrators) wait for each other
// on a lock of the group held until the transaction ends: the second finds the environment the
// first made, instead of failing on the unique managed_by.
//
// The environment's name is unique among all the environments, and an administrator may have
// named an ordinary one «Statistical models: <group>» before the group was enabled. The managed
// environment's name is shown nowhere (the Scripting section hides it), so it takes the first
// free one of «Statistical models: <group>», «… (2)», «… (3)»…: enabling never stops on a name
// the administrator cannot see, and nobody has to rename anything. The other way round, an
// ordinary environment given the managed one's name later moves it aside (mgYieldName).
func mgEnsureEnvironment(ctx context.Context, tx pgx.Tx, g modelgroups.Group, who string) (int64, error) {
	// the lock's two keys: the table's own oid and the group (a pair never used by another lock)
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock('script_environments'::regclass::oid::int, hashtext($1))`, mgManagedBy(g.ID)); err != nil {
		return 0, err
	}
	var id int64
	err := tx.QueryRow(ctx, `SELECT id FROM script_environments WHERE managed_by = $1`, mgManagedBy(g.ID)).Scan(&id)
	if err == nil {
		return id, nil
	}
	if !scNoRows(err) {
		return 0, err
	}
	cpu, memory := g.Recipe.Resources.CPU, g.Recipe.Resources.Memory
	now := time.Now().UTC()
	for {
		name, err := mgFreeName(ctx, tx, mgEnvironmentName(g.ID))
		if err != nil {
			return 0, err
		}
		err = tx.QueryRow(ctx, `INSERT INTO script_environments (name, description, language, enabled, current_version_id, cpu_request, cpu_limit,
			memory_request, memory_limit, max_timeout_seconds, created_at, created_by, updated_at, updated_by, managed_by)
			VALUES ($1, $2, $3, true, NULL, $4, $5, $6, $7, $8, $9, $10, $9, $10, $11) ON CONFLICT DO NOTHING RETURNING id`,
			name, "Managed by Tabularia: the image of the statistical model group "+g.ID, g.Recipe.Language,
			mgHalf(cpu, true), cpu, mgHalf(memory, false), memory, mgDefaultSeconds(g), now, who, mgManagedBy(g.ID)).Scan(&id)
		if !scNoRows(err) {
			return id, err
		}
		// a conflict: the group's environment made meanwhile without the lock (a gateway of the
		// previous version, during an upgrade), or one of that name by an administrator
		err = tx.QueryRow(ctx, `SELECT id FROM script_environments WHERE managed_by = $1`, mgManagedBy(g.ID)).Scan(&id)
		if !scNoRows(err) {
			return id, err
		}
	}
}

// mgEnvironmentName is the name a group's managed environment takes when it is free.
func mgEnvironmentName(group string) string { return "Statistical models: " + group }

// mgFreeName is the first of base, «base (2)», «base (3)»… that no environment has, also in
// another case: the rule of the environments' names (scEnvName).
func mgFreeName(ctx context.Context, q perm.Querier, base string) (string, error) {
	for n := 1; ; n++ {
		name := base
		if n > 1 {
			name = fmt.Sprintf("%s (%d)", base, n)
		}
		var taken bool
		if err := q.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM script_environments WHERE lower(name) = lower($1))`, name).Scan(&taken); err != nil {
			return "", err
		}
		if !taken {
			return name, nil
		}
	}
}

// mgYieldName moves the managed environment id (of managedBy) out of the name an administrator
// gives an ordinary environment: the managed environment's name is shown nowhere, so it takes the
// next free «Statistical models: <group> (n)» and the administrator's name is never refused for a
// name nobody sees. As mgEnsureEnvironment, under the group's lock.
func mgYieldName(ctx context.Context, db mgBeginner, id int64, managedBy string) error {
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock('script_environments'::regclass::oid::int, hashtext($1))`, managedBy); err != nil {
		return err
	}
	name, err := mgFreeName(ctx, tx, mgEnvironmentName(mgGroupOfManaged(&managedBy)))
	if err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `UPDATE script_environments SET name = $2 WHERE id = $1 AND managed_by IS NOT NULL`, id, name); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// mgBeginner is a database a transaction is begun on (the pool).
type mgBeginner interface {
	Begin(ctx context.Context) (pgx.Tx, error)
}

// mgEnable is POST /admin/models/{group_id}/enable: the group's environment made (or enabled
// again) and, unless the version in use is ready on the recipe shipped, its version queued.
func mgEnable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok || !mgIsOn(w) {
		return
	}
	if why := mgBaseSentence(g); why != "" {
		detail(w, http.StatusConflict, why)
		return
	}
	var act *mgLicenceAct
	if g.Recipe.Weights != nil {
		in, ok := mgDecodeBuild(w, r)
		if !ok {
			return
		}
		act = &mgLicenceAct{in: in, actor: &p.User, log: d.Log}
	}
	ctx := r.Context()
	who := p.User.Email
	var queued int64
	var reused bool
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		envID, err := mgEnsureEnvironment(ctx, tx, g, who)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		if _, err := tx.Exec(ctx, `INSERT INTO model_groups (id, environment_id, enabled, everyone, enabled_by, enabled_at, updated_by, updated_at)
			VALUES ($1, $2, true, true, $3, $4, $3, $4) ON CONFLICT (id) DO UPDATE SET environment_id = $2, enabled = true,
			enabled_by = $3, enabled_at = $4, updated_by = $3, updated_at = $4`, g.ID, envID, who, now); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE script_environments SET enabled = true, updated_at = $1, updated_by = $2 WHERE id = $3`, now, who, envID); err != nil {
			return err
		}
		queued, reused, err = mgQueueIfNeeded(ctx, tx, envID, g, who, act)
		return err
	})
	if err != nil {
		if mgAnswerRefusal(w, err) {
			return
		}
		fail(d, w, "models: enabling a group", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.group_enable", TargetType: scPtrStr("model_group"),
		TargetLabel: &g.ID, Detail: pyjson.Object{{Key: "version", Value: queued}, {Key: "reused", Value: reused}}})
	mgAnswer(d, w, r, p, g)
}

// mgQueueIfNeeded queues a version when the environment has no ready current version built from
// the shipped recipe and none is being built. It returns the number queued (0: none) and
// whether the version in use already serves. A group with weights needs the click's acceptance
// (act) to queue one: written in the audit here, in this transaction, so that the version's lines and
// the audit's row name the same act; a refusal (*mgRefusal) undoes the enabling.
func mgQueueIfNeeded(ctx context.Context, tx pgx.Tx, envID int64, g modelgroups.Group, who string, act *mgLicenceAct) (int64, bool, error) {
	if err := mgLockEnvironment(ctx, tx, envID); err != nil {
		return 0, false, err
	}
	var served bool
	err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM script_environments e JOIN script_environment_versions v ON v.id = e.current_version_id
		WHERE e.id = $1 AND v.status = 'ready' AND v.recipe_hash = $2)`, envID, g.RecipeHash).Scan(&served)
	if err != nil || served {
		return 0, served, err
	}
	if n, err := mgInFlight(ctx, tx, envID); err != nil || n != 0 {
		return 0, false, err
	}
	var acc *mgAcceptance
	if g.Recipe.Weights != nil {
		var err error
		if acc, err = act.accept(ctx, tx, g); err != nil {
			return 0, false, err
		}
	}
	n, err := mgNewVersion(ctx, tx, envID, g, who, acc)
	return n, false, err
}

// mgDisable is POST /admin/models/{group_id}/disable: the nodes leave the palette and the
// steps that use them stop with a sentence; the images stay, and enabling again uses the
// current version at once.
func mgDisable(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	var found bool
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		var env int64
		err := tx.QueryRow(ctx, `UPDATE model_groups SET enabled = false, updated_by = $2, updated_at = $3 WHERE id = $1 AND enabled
			RETURNING environment_id`, g.ID, p.User.Email, time.Now().UTC()).Scan(&env)
		if scNoRows(err) {
			return nil
		}
		if err != nil {
			return err
		}
		found = true
		_, err = tx.Exec(ctx, `UPDATE script_environments SET enabled = false, updated_at = $1, updated_by = $2 WHERE id = $3`, time.Now().UTC(), p.User.Email, env)
		return err
	})
	if err != nil {
		fail(d, w, "models: disabling a group", err)
		return
	}
	if !found {
		detail(w, http.StatusConflict, "The group is not enabled")
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.group_disable", TargetType: scPtrStr("model_group"), TargetLabel: &g.ID})
	mgAnswer(d, w, r, p, g)
}

// mgRebuild is POST /admin/models/{group_id}/rebuild: a new version from the recipe shipped.
func mgRebuild(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok || !mgIsOn(w) {
		return
	}
	if why := mgBaseSentence(g); why != "" {
		detail(w, http.StatusConflict, why)
		return
	}
	var act *mgLicenceAct
	if g.Recipe.Weights != nil {
		in, ok := mgDecodeBuild(w, r)
		if !ok {
			return
		}
		act = &mgLicenceAct{in: in, actor: &p.User, log: d.Log}
	}
	ctx := r.Context()
	var number, busy int64
	var enabled bool
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		var env int64
		err := tx.QueryRow(ctx, `SELECT environment_id, enabled FROM model_groups WHERE id = $1`, g.ID).Scan(&env, &enabled)
		if scNoRows(err) || err == nil && !enabled {
			enabled = false
			return nil
		}
		if err != nil {
			return err
		}
		if err = mgLockEnvironment(ctx, tx, env); err != nil {
			return err
		}
		if busy, err = mgInFlight(ctx, tx, env); err != nil || busy != 0 {
			return err
		}
		var acc *mgAcceptance
		if act != nil {
			if acc, err = act.accept(ctx, tx, g); err != nil {
				return err
			}
		}
		if number, err = mgNewVersion(ctx, tx, env, g, p.User.Email, acc); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE model_groups SET updated_by = $2, updated_at = $3 WHERE id = $1`, g.ID, p.User.Email, time.Now().UTC())
		return err
	})
	if err != nil {
		if mgAnswerRefusal(w, err) {
			return
		}
		fail(d, w, "models: building a group again", err)
		return
	}
	if !enabled {
		detail(w, http.StatusConflict, "Enable the group first")
		return
	}
	if busy != 0 {
		detail(w, http.StatusConflict, fmt.Sprintf("Version %d is still being built", busy))
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.group_rebuild", TargetType: scPtrStr("model_group"),
		TargetLabel: &g.ID, Detail: pyjson.Object{{Key: "version", Value: number}}})
	mgAnswer(d, w, r, p, g)
}

// ── resources, limits, who may use it ────────────────────────────────────────

type mgSettingsIn struct {
	CPULimit          string `json:"cpu_limit"`
	MemoryLimit       string `json:"memory_limit"`
	MaxTimeoutSeconds int64  `json:"max_timeout_seconds"`
	MaxRows           *int64 `json:"max_rows"` // null: the recipe's
}

const mgMaxRowsCeiling = 1_000_000_000

// mgSettings is PUT /admin/models/{group_id}/settings: CPU and memory (limits; the requests are
// half), the longest run and max_rows. They apply to the next run, without a build.
func mgSettings(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok {
		return
	}
	var in mgSettingsIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	in.CPULimit, in.MemoryLimit = strings.TrimSpace(in.CPULimit), strings.TrimSpace(in.MemoryLimit)
	switch {
	case !scCPU.MatchString(in.CPULimit) || sbCores(in.CPULimit) <= 0:
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("cpu_limit: «%s» is not a Kubernetes quantity", in.CPULimit))
		return
	case math.Round(sbCores(in.CPULimit)*1000) < mgMinCPURequest:
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("cpu_limit: «%s» is less than %dm, the least CPU a model's Job asks for",
			in.CPULimit, mgMinCPURequest))
		return
	case !scMemory.MatchString(in.MemoryLimit) || mgBytes(in.MemoryLimit) < 256<<20:
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("memory_limit: «%s» is not a Kubernetes quantity of at least 256Mi", in.MemoryLimit))
		return
	case in.MaxTimeoutSeconds < 60 || in.MaxTimeoutSeconds > 86400:
		detail(w, http.StatusUnprocessableEntity, "max_timeout_seconds is between 60 and 86400")
		return
	case in.MaxRows != nil && (*in.MaxRows < 1 || *in.MaxRows > mgMaxRowsCeiling):
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("max_rows is between 1 and %d", mgMaxRowsCeiling))
		return
	}
	ctx := r.Context()
	var before, after mgLimits
	var notEnabled bool
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		s, err := mgLoad(ctx, tx, g, false, 1)
		if err != nil {
			return err
		}
		if s.EnvID == 0 {
			notEnabled = true
			return nil
		}
		before = mgLimits{CPULimit: s.CPULimit, MemoryLimit: s.Memory, MaxTimeoutSeconds: s.maxSeconds(), MaxRows: s.maxRows()}
		now := time.Now().UTC()
		if _, err := tx.Exec(ctx, `UPDATE script_environments SET cpu_request = $1, cpu_limit = $2, memory_request = $3, memory_limit = $4,
			max_timeout_seconds = $5, updated_at = $6, updated_by = $7 WHERE id = $8`, mgHalf(in.CPULimit, true), in.CPULimit,
			mgHalf(in.MemoryLimit, false), in.MemoryLimit, in.MaxTimeoutSeconds, now, p.User.Email, s.EnvID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE model_groups SET max_rows = $2, updated_by = $3, updated_at = $4 WHERE id = $1`,
			g.ID, in.MaxRows, p.User.Email, now); err != nil {
			return err
		}
		after = before
		after.CPULimit, after.MemoryLimit, after.MaxTimeoutSeconds = in.CPULimit, in.MemoryLimit, in.MaxTimeoutSeconds
		after.MaxRows = g.Recipe.MaxRows
		if in.MaxRows != nil {
			after.MaxRows = *in.MaxRows
		}
		return nil
	})
	if err != nil {
		fail(d, w, "models: the settings", err)
		return
	}
	if notEnabled {
		detail(w, http.StatusConflict, "Enable the group first")
		return
	}
	if before != after {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.group_settings", TargetType: scPtrStr("model_group"),
			TargetLabel: &g.ID, Detail: pyjson.Object{{Key: "before", Value: mgLimitsObject(before)}, {Key: "after", Value: mgLimitsObject(after)}}})
	}
	mgAnswer(d, w, r, p, g)
}

func mgLimitsObject(l mgLimits) pyjson.Object {
	return pyjson.Object{{Key: "cpu_limit", Value: l.CPULimit}, {Key: "memory_limit", Value: l.MemoryLimit},
		{Key: "max_timeout_seconds", Value: l.MaxTimeoutSeconds}, {Key: "max_rows", Value: l.MaxRows}}
}

func mgBytes(quantity string) float64 {
	m := sbMemoryParts.FindStringSubmatch(quantity)
	if m == nil {
		return 0
	}
	size, _ := strconv.ParseFloat(m[1], 64)
	return size * sbBytes[m[2]]
}

type mgAccessIn struct {
	Everyone bool    `json:"everyone"`
	GroupIDs []int64 `json:"group_ids"`
}

// mgAccess is PUT /admin/models/{group_id}/access: everyone who can edit flows, or the user
// groups listed.
func mgAccess(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := mgGroupOr404(w, r)
	if !ok {
		return
	}
	var in mgAccessIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	ctx := r.Context()
	var stop *scRefusal
	var notEnabled bool
	var before []int64
	var beforeEveryone bool
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		s, err := mgLoad(ctx, tx, g, false, 1)
		if err != nil {
			return err
		}
		if s.EnvID == 0 {
			notEnabled = true
			return nil
		}
		beforeEveryone = s.Everyone
		if before, _, err = scEnvGroups(ctx, tx, s.EnvID); err != nil {
			return err
		}
		ids := in.GroupIDs
		if in.Everyone {
			ids = nil // the groups are not kept once everyone may use it: listing them again is a choice
		}
		if stop, err = scSetGroups(ctx, tx, s.EnvID, ids); err != nil || stop != nil {
			return scOr(err)
		}
		_, err = tx.Exec(ctx, `UPDATE model_groups SET everyone = $2, updated_by = $3, updated_at = $4 WHERE id = $1`,
			g.ID, in.Everyone, p.User.Email, time.Now().UTC())
		return err
	})
	if stop != nil {
		stop.send(w)
		return
	}
	if err != nil {
		fail(d, w, "models: the access", err)
		return
	}
	if notEnabled {
		detail(w, http.StatusConflict, "Enable the group first")
		return
	}
	ids := []any{}
	for _, id := range in.GroupIDs {
		ids = append(ids, id)
	}
	if in.Everyone {
		ids = []any{}
	}
	was := []any{}
	for _, id := range before {
		was = append(was, id)
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.group_access", TargetType: scPtrStr("model_group"),
		TargetLabel: &g.ID, Detail: pyjson.Object{{Key: "everyone", Value: in.Everyone}, {Key: "groups", Value: ids},
			{Key: "before_everyone", Value: beforeEveryone}, {Key: "before_groups", Value: was}}})
	mgAnswer(d, w, r, p, g)
}
