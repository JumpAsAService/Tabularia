package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/enginepolicy"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/flows.py: the saved flows (the editor's DAGs), held in the folders.

func init() {
	register("flows", func(d *Deps) []server.Route {
		a := func(pattern string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, pattern, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/flows", Handler: a("/flows", d.listAllFlows)},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/flows", Handler: a("/projects/{project_id}/flows", d.listFlows)},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}", Handler: a("/flows/{flow_id}", d.getFlow)},
			{Method: http.MethodDelete, Pattern: "/flows/{flow_id}", Handler: a("/flows/{flow_id}", d.deleteFlow)},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/duplicate", Handler: a("/flows/{flow_id}/duplicate", d.duplicateFlow)},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/versions", Handler: a("/flows/{flow_id}/versions", d.listFlowVersions)},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/versions/{version}/promote",
				Handler: a("/flows/{flow_id}/versions/{version}/promote", d.promoteFlowVersion)},
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}/stats", Handler: a("/flows/{flow_id}/stats", d.flowStats)},
			{Method: http.MethodDelete, Pattern: "/flows/{flow_id}/presence/{instance}",
				Handler: a("/flows/{flow_id}/presence/{instance}", d.flowPresenceLeave)},
		}
	})
}

// forward is a method no client sends: a pattern registered with it only keeps a more
// general ported pattern from taking its paths, which still go to the Python gateway.
const forward = "FORWARD"

var notAllowed = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
	server.JSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
})

// flowRow is the `flows` row (models/flow.py).
type flowRow struct {
	ID                 int64
	Name               string
	Description        string
	ProjectID          int64
	OwnerID            *int64
	Definition         string
	Engine             string
	ProductionEngine   *string
	RunSchedule        *string
	RunScheduledBy     *int64
	NextRunAt          *time.Time
	NotifyEmails       *string
	NotifyConnectionID *int64
	CreatedAt          time.Time
	UpdatedAt          time.Time
}

const flowColumns = `id, name, description, project_id, owner_id, definition, engine, production_engine, run_schedule,
	run_scheduled_by, next_run_at, notify_emails, notify_connection_id, created_at, updated_at`

func scanFlow(row pgx.Row) (*flowRow, error) {
	var f flowRow
	err := row.Scan(&f.ID, &f.Name, &f.Description, &f.ProjectID, &f.OwnerID, &f.Definition, &f.Engine, &f.ProductionEngine,
		&f.RunSchedule, &f.RunScheduledBy, &f.NextRunAt, &f.NotifyEmails, &f.NotifyConnectionID, &f.CreatedAt, &f.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &f, err
}

func readFlows(ctx context.Context, q perm.Querier, where string, args ...any) ([]*flowRow, error) {
	rows, err := q.Query(ctx, `SELECT `+flowColumns+` FROM flows `+where, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []*flowRow{}
	for rows.Next() {
		f, err := scanFlow(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// FlowOut is schemas/models.py FlowOut, its fields in the same order.
type FlowOut struct {
	ID                 int64   `json:"id"`
	Name               string  `json:"name"`
	Description        string  `json:"description"`
	ProjectID          int64   `json:"project_id"`
	OwnerID            *int64  `json:"owner_id"`
	OwnerName          *string `json:"owner_name"`
	Engine             string  `json:"engine"`
	ProductionEngine   *string `json:"production_engine"`
	RunSchedule        *string `json:"run_schedule"`
	NextRunAt          *string `json:"next_run_at"`
	NotifyEmails       *string `json:"notify_emails"`
	NotifyConnectionID *int64  `json:"notify_connection_id"`
	CreatedAt          *string `json:"created_at"`
	UpdatedAt          *string `json:"updated_at"`
	LastRunStatus      *string `json:"last_run_status"`
	LastRunAt          *string `json:"last_run_at"`
	LockedReason       *string `json:"locked_reason"`
}

// FlowDetail is FlowOut with the definition.
type FlowDetail struct {
	FlowOut
	Definition string `json:"definition"`
}

func iso(t time.Time) *string { return isoPtr(&t) }

func (f *flowRow) out() FlowOut {
	return FlowOut{ID: f.ID, Name: f.Name, Description: f.Description, ProjectID: f.ProjectID, OwnerID: f.OwnerID,
		Engine: f.Engine, ProductionEngine: f.ProductionEngine, RunSchedule: f.RunSchedule, NextRunAt: isoPtr(f.NextRunAt),
		NotifyEmails: f.NotifyEmails, NotifyConnectionID: f.NotifyConnectionID, CreatedAt: iso(f.CreatedAt), UpdatedAt: iso(f.UpdatedAt)}
}

func (f *flowRow) detail() FlowDetail { return FlowDetail{f.out(), f.Definition} }

// request is what a flows' route works with: the person, their permissions, the access to
// the panel-granted things (flowaccess), the request.
type request struct {
	d   *Deps
	w   http.ResponseWriter
	r   *http.Request
	ctx context.Context
	p   *auth.Principal
	c   *perm.Checker
	acc *flowaccess.Access
}

func (d *Deps) req(w http.ResponseWriter, r *http.Request, p *auth.Principal) *request {
	c := perm.For(d.DB, &p.User)
	return &request{d: d, w: w, r: r, ctx: r.Context(), p: p, c: c,
		acc: flowaccess.New(d.DB, c, &p.User, d.flowSettings())}
}

func (d *Deps) flowSettings() flowaccess.Settings {
	f := d.Config.Flows
	return flowaccess.Settings{ScriptsEnabled: f.ScriptsEnabled, ScriptsNamespace: f.ScriptsNamespace,
		ScriptsRegistry: f.ScriptsRegistry, ContainersEnabled: f.ContainersEnabled, MaxCPU: f.ContainersMaxCPU,
		MaxMemory: f.ContainersMaxMemory, MaxTimeoutSeconds: f.ContainersMaxTimeout}
}

// fail answers an unexpected error as Starlette does (a 500, plain text).
func (q *request) fail(err error) {
	q.d.Log.Error("route failed", "path", q.r.URL.Path, "err", err)
	server.InternalError(q.w)
}

// refused answers a refusal; true when there was one (or an error).
func (q *request) refused(ref *auth.Refusal, err error) bool {
	if err != nil {
		q.fail(err)
		return true
	}
	if ref != nil {
		server.Refuse(q.w, ref)
		return true
	}
	return false
}

func (q *request) refuse(status int, detail string) {
	server.Refuse(q.w, &auth.Refusal{Status: status, Detail: detail})
}

// ensure is deps/permissions.py ensure_can.
func (q *request) ensure(projectID int64, c perm.Capability) bool {
	ref, err := q.c.Ensure(q.ctx, projectID, c)
	return !q.refused(ref, err)
}

// flow is routes/flows.py _get_flow: the 404 when it does not exist.
func (q *request) flow(id int64) *flowRow {
	f, err := scanFlow(q.d.DB.QueryRow(q.ctx, `SELECT `+flowColumns+` FROM flows WHERE id = $1`, id))
	if err != nil {
		q.fail(err)
		return nil
	}
	if f == nil {
		q.refuse(http.StatusNotFound, "Flow not found")
	}
	return f
}

func (q *request) projectExists(id int64) (bool, error) {
	var one int
	err := q.d.DB.QueryRow(q.ctx, `SELECT 1 FROM projects WHERE id = $1`, id).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func (q *request) audit(e audit.Event) {
	if e.Actor == nil {
		e.Actor = &q.p.User
	}
	audit.Record(q.ctx, q.d.DB, q.d.Log, e)
}

func strp(s string) *string { return &s }

// withLocks is _with_locks: each item says whether this person can open it.
func (q *request) withLocks(flows []*flowRow, items []FlowOut) ([]FlowOut, error) {
	if items == nil {
		items = make([]FlowOut, len(flows))
		for i, f := range flows {
			items[i] = f.out()
		}
	}
	for i, f := range flows {
		why, err := q.acc.Blocked(q.ctx, f.Definition)
		if err != nil {
			return nil, err
		}
		if why != "" {
			items[i].LockedReason = strp(why)
		}
	}
	return items, nil
}

// listAllFlows is GET /flows (list_all_flows): every flow in the READABLE folders.
func (d *Deps) listAllFlows(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	q := d.req(w, r, p)
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if len(readable) == 0 {
		server.JSON(w, http.StatusOK, []FlowOut{})
		return
	}
	flows, err := readFlows(q.ctx, d.DB, `WHERE project_id = ANY($1) ORDER BY name`, perm.Sorted(readable))
	if err != nil {
		q.fail(err)
		return
	}
	items, err := q.withLocks(flows, nil)
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, items)
}

// listFlows is GET /projects/{project_id}/flows (list_flows).
func (d *Deps) listFlows(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "project_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	if exists, err := q.projectExists(ids[0]); err != nil {
		q.fail(err)
		return
	} else if !exists {
		q.refuse(http.StatusNotFound, "Folder not found")
		return
	}
	if !q.ensure(ids[0], perm.View) {
		return
	}
	flows, err := readFlows(q.ctx, d.DB, `WHERE project_id = $1 ORDER BY name`, ids[0])
	if err != nil {
		q.fail(err)
		return
	}
	items, err := q.withLocks(flows, nil)
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, items)
}

// getFlow is GET /flows/{flow_id} (get_flow).
func (d *Deps) getFlow(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return
	}
	// a flow that runs images of a registry this person may not use does not open
	if q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	server.JSON(w, http.StatusOK, f.detail())
}

// deleteFlow is DELETE /flows/{flow_id} (delete_flow): the run history dies with the flow,
// the published datasources survive without their provenance.
func (d *Deps) deleteFlow(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.Edit) {
		return
	}
	err := pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		for _, stmt := range []string{
			`UPDATE datasources SET flow_id = NULL WHERE flow_id = $1`,
			// runs this flow's runs started for other objects stay, no longer attached
			`UPDATE runs SET parent_run_id = NULL WHERE parent_run_id IN (SELECT id FROM runs WHERE flow_id = $1)`,
			`DELETE FROM runs WHERE flow_id = $1`,
			`DELETE FROM flow_versions WHERE flow_id = $1`,
			`DELETE FROM dbt_ai_texts WHERE flow_id = $1`,
		} {
			if _, err := tx.Exec(q.ctx, stmt, f.ID); err != nil {
				return err
			}
		}
		if err := forgetFlowChats(q.ctx, tx, f.ID); err != nil {
			return err
		}
		_, err := tx.Exec(q.ctx, `DELETE FROM flows WHERE id = $1`, f.ID)
		return err
	})
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow.delete", TargetType: strp("flow"), TargetID: &f.ID, TargetLabel: &f.Name})
	returnsNone(w)
}

// forgetFlowChats is ai_chats.forget_flow: the flow editor's conversations about it.
func forgetFlowChats(ctx context.Context, tx pgx.Tx, flowID int64) error {
	rows, err := tx.Query(ctx, `SELECT id FROM ai_chats WHERE kind = 'flow' AND flow_id = $1`, flowID)
	if err != nil {
		return err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil || len(ids) == 0 {
		return err
	}
	if _, err := tx.Exec(ctx, `DELETE FROM ai_chat_turns WHERE chat_id = ANY($1)`, ids); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `DELETE FROM ai_chats WHERE id = ANY($1)`, ids)
	return err
}

// validateEngine is _validate_engine: in the catalog and not disabled by the
// administrator. `raw` is what was asked (nil: none).
func (q *request) validateEngine(raw *string) (string, *auth.Refusal, error) {
	e := "polars"
	shown := "None"
	if raw != nil {
		shown = *raw
		if *raw != "" {
			e = *raw
		}
	}
	e = strings.ToLower(pyStrip(e))
	if !enginepolicy.IsKnown(e) {
		return "", &auth.Refusal{Status: 422, Detail: fmt.Sprintf("Engine not available: '%s'. Choose from: %s.",
			shown, strings.Join(enginepolicy.SortedKnown(nil), ", "))}, nil
	}
	forbidden, err := enginepolicy.Disabled(q.ctx, q.d.DB)
	if err != nil {
		return "", nil, err
	}
	if forbidden[e] {
		return "", &auth.Refusal{Status: 422, Detail: fmt.Sprintf("The engine '%s' has been disabled by the administrator. Choose from: %s.",
			e, strings.Join(enginepolicy.SortedKnown(forbidden), ", "))}, nil
	}
	return e, nil, nil
}

// validateProductionEngine is _validate_production_engine: empty is none.
func (q *request) validateProductionEngine(raw *string) (*string, *auth.Refusal, error) {
	if raw == nil || pyStrip(*raw) == "" {
		return nil, nil, nil
	}
	e, ref, err := q.validateEngine(raw)
	if ref != nil || err != nil {
		return nil, ref, err
	}
	return &e, nil, nil
}

// datasourceIDs is _datasource_ids: every `datasourceId` however deep.
func datasourceIDs(payload any) map[int64]bool {
	found := map[int64]bool{}
	var walk func(any)
	walk = func(n any) {
		switch x := n.(type) {
		case pyjson.Object:
			for _, kv := range x {
				if kv.Key == "datasourceId" {
					if id, ok := flowaccess.Digits(kv.Value); ok {
						found[id] = true
						continue
					}
				}
				walk(kv.Value)
			}
		case []any:
			for _, e := range x {
				walk(e)
			}
		}
	}
	walk(payload)
	return found
}

// authorizeDefinitionKeys is _authorize_definition_keys: the storage keys and the
// datasources a definition names must be readable by whoever saves it.
func (q *request) authorizeDefinitionKeys(definition string) (*auth.Refusal, error) {
	if definition == "" {
		return nil, nil
	}
	parsed, err := pyjson.Loads(definition)
	if err != nil {
		return &auth.Refusal{Status: 422, Detail: "The flow definition is not valid JSON"}, nil
	}
	if ref, err := objects.EnsureCanReadKeys(q.ctx, q.c, q.d.DB, &q.p.User, objects.CollectStorageKeys(parsed)); ref != nil || err != nil {
		return ref, err
	}
	ids := datasourceIDs(parsed)
	if len(ids) == 0 {
		return nil, nil
	}
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		return nil, err
	}
	rows, err := q.d.DB.Query(q.ctx, `SELECT project_id FROM datasources WHERE id = ANY($1)`, perm.Sorted(ids))
	if err != nil {
		return nil, err
	}
	projects, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	for _, pid := range projects {
		if !readable[pid] {
			return &auth.Refusal{Status: 403, Detail: "The flow reads a datasource you cannot read"}, nil
		}
	}
	return nil, nil
}

// checkSave is what every save of a definition goes through after the keys:
// script_nodes.check_definition, then registry_access.check_definition.
func (q *request) checkSave(before, after string) (*auth.Refusal, error) {
	if ref, err := q.acc.ScriptCheckDefinition(q.ctx, before, after); ref != nil || err != nil {
		return ref, err
	}
	return q.acc.RegistryCheckDefinition(q.ctx, before, after)
}

// insertFlow writes a new flow and returns it as read back.
func (q *request) insertFlow(f *flowRow) (*flowRow, error) {
	now := time.Now().UTC()
	return scanFlow(q.d.DB.QueryRow(q.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine,
		production_engine, run_schedule, run_scheduled_by, next_run_at, notify_emails, notify_connection_id, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, NULL, NULL, NULL, NULL, $8, $8) RETURNING `+flowColumns,
		f.Name, f.Description, f.ProjectID, f.OwnerID, f.Definition, f.Engine, f.ProductionEngine, now))
}

// snapshotVersion is _snapshot_version: the flow's current definition as a new version,
// unless the same as the last; the flow's row locked until the commit.
func (q *request) snapshotVersion(flowID int64, note string) (*flowRow, error) {
	var out *flowRow
	err := pgx.BeginFunc(q.ctx, q.d.DB, func(tx pgx.Tx) error {
		f, err := scanFlow(tx.QueryRow(q.ctx, `SELECT `+flowColumns+` FROM flows WHERE id = $1 FOR UPDATE`, flowID))
		if err != nil || f == nil {
			return err
		}
		out = f
		var last int64
		var lastDef string
		err = tx.QueryRow(q.ctx, `SELECT version, definition FROM flow_versions WHERE flow_id = $1 ORDER BY version DESC LIMIT 1`,
			flowID).Scan(&last, &lastDef)
		if err == nil && lastDef == f.Definition {
			return nil
		}
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		_, err = tx.Exec(q.ctx, `INSERT INTO flow_versions (flow_id, version, definition, note, created_at, created_by)
			VALUES ($1, $2, $3, $4, $5, $6)`, flowID, last+1, f.Definition, note, time.Now().UTC(), q.p.User.ID)
		return err
	})
	return out, err
}

// duplicateFlow is POST /flows/{flow_id}/duplicate (duplicate_flow): a copy in its
// folder, as «name (i)», under the same rules as a new flow.
func (d *Deps) duplicateFlow(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	src := q.flow(ids[0])
	if src == nil || !q.ensure(src.ProjectID, perm.Edit) {
		return
	}
	if q.refused(q.acc.EnsureCanOpen(q.ctx, src.Definition)) || q.refused(q.authorizeDefinitionKeys(src.Definition)) ||
		q.refused(q.checkSave("", src.Definition)) {
		return
	}
	name, err := copynames.Free(q.ctx, d.DB, "flows", src.ProjectID, src.Name)
	if err != nil {
		q.fail(err)
		return
	}
	engine, ref, err := q.validateEngine(&src.Engine)
	if q.refused(ref, err) {
		return
	}
	prod, ref, err := q.validateProductionEngine(src.ProductionEngine)
	if q.refused(ref, err) {
		return
	}
	flow, err := q.insertFlow(&flowRow{Name: name, Description: src.Description, Definition: src.Definition,
		ProjectID: src.ProjectID, OwnerID: &p.User.ID, Engine: engine, ProductionEngine: prod})
	if err != nil {
		q.fail(err)
		return
	}
	if _, err := q.snapshotVersion(flow.ID, fmt.Sprintf("copy of '%s'", src.Name)); err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow.create", TargetType: strp("flow"), TargetID: &flow.ID, TargetLabel: &flow.Name,
		Detail: pyjson.Object{{Key: "project_id", Value: flow.ProjectID}, {Key: "engine", Value: flow.Engine},
			{Key: "duplicate_of", Value: src.ID}}})
	// the answer is the object as the route left it: refreshed after the version's commit
	if flow, err = q.reread(flow.ID); err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusCreated, flow.detail())
}

func (q *request) reread(id int64) (*flowRow, error) {
	return scanFlow(q.d.DB.QueryRow(q.ctx, `SELECT `+flowColumns+` FROM flows WHERE id = $1`, id))
}

// FlowVersionOut is schemas/models.py FlowVersionOut.
type FlowVersionOut struct {
	Version       int64   `json:"version"`
	Note          string  `json:"note"`
	CreatedAt     *string `json:"created_at"`
	CreatedBy     *int64  `json:"created_by"`
	CreatedByName *string `json:"created_by_name"`
	IsCurrent     bool    `json:"is_current"`
}

// listFlowVersions is GET /flows/{flow_id}/versions: the history, the newest first.
func (d *Deps) listFlowVersions(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	rows, err := d.DB.Query(q.ctx, `SELECT v.version, v.note, v.created_at, v.created_by, coalesce(nullif(u.full_name, ''), u.email)
		FROM flow_versions v LEFT JOIN users u ON u.id = v.created_by WHERE v.flow_id = $1 ORDER BY v.version DESC`, f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	out := []FlowVersionOut{}
	for rows.Next() {
		var v FlowVersionOut
		var at time.Time
		if err := rows.Scan(&v.Version, &v.Note, &at, &v.CreatedBy, &v.CreatedByName); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		v.CreatedAt = iso(at)
		out = append(out, v)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		q.fail(err)
		return
	}
	for i := range out {
		out[i].IsCurrent = out[i].Version == out[0].Version
	}
	server.JSON(w, http.StatusOK, out)
}

// promoteFlowVersion is POST /flows/{flow_id}/versions/{version}/promote: an old
// version's definition becomes the flow's, recorded as a new version.
func (d *Deps) promoteFlowVersion(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id", "version")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.Edit) {
		return
	}
	var target string
	err := d.DB.QueryRow(q.ctx, `SELECT definition FROM flow_versions WHERE flow_id = $1 AND version = $2 ORDER BY id LIMIT 1`,
		f.ID, ids[1]).Scan(&target)
	if errors.Is(err, pgx.ErrNoRows) {
		q.refuse(http.StatusNotFound, "Version not found")
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	if q.refused(q.authorizeDefinitionKeys(target)) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) ||
		q.refused(q.acc.EnsureCanOpen(q.ctx, target)) || q.refused(q.checkSave(f.Definition, target)) {
		return
	}
	off := q.guardSomeoneElsesSchedule(f, &target, false)
	err = pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		_, err := tx.Exec(q.ctx, `UPDATE flows SET definition = $2, updated_at = $3, run_schedule = $4, run_scheduled_by = $5,
			next_run_at = $6 WHERE id = $1`, f.ID, target, time.Now().UTC(), f.RunSchedule, f.RunScheduledBy, f.NextRunAt)
		return err
	})
	if err != nil {
		q.fail(err)
		return
	}
	f, err = q.snapshotVersion(f.ID, fmt.Sprintf("promoted from v%d", ids[1]))
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow.promote", TargetType: strp("flow"), TargetID: &f.ID, TargetLabel: &f.Name,
		Detail: pyjson.Object{{Key: "from_version", Value: ids[1]}}})
	q.recordScheduleOff(f, off)
	if f, err = q.reread(f.ID); err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, f.detail())
}

// guardSomeoneElsesSchedule is _guard_someone_elses_schedule: someone else's edit that
// makes the flow reach more, or a move, switches the author's schedule off (on `f`, to be
// written by the caller). The audit detail, or nil.
func (q *request) guardSomeoneElsesSchedule(f *flowRow, newDefinition *string, moved bool) pyjson.Object {
	if f.RunSchedule == nil || *f.RunSchedule == "" || f.RunScheduledBy == nil || *f.RunScheduledBy == q.p.User.ID {
		return nil
	}
	reason := ""
	if moved {
		reason = "the flow was moved to another folder"
	} else if newDefinition != nil {
		if n := len(flowaccess.Widens(f.Definition, *newDefinition)); n > 0 {
			reason = fmt.Sprintf("the definition now reads or writes more (%d new reference(s))", n)
		}
	}
	if reason == "" {
		return nil
	}
	author := *f.RunScheduledBy
	f.RunSchedule, f.RunScheduledBy, f.NextRunAt = nil, nil, nil
	return pyjson.Object{{Key: "turned_off", Value: true}, {Key: "reason", Value: reason}, {Key: "author", Value: author}}
}

// recordScheduleOff is _record_schedule_off.
func (q *request) recordScheduleOff(f *flowRow, detail pyjson.Object) {
	if len(detail) > 0 {
		q.audit(audit.Event{Action: "flow.schedule", TargetType: strp("flow"), TargetID: &f.ID, TargetLabel: &f.Name, Detail: detail})
	}
}

// FlowStatsOut is schemas/models.py FlowStatsOut.
type FlowStatsOut struct {
	RunCount           int64    `json:"run_count"`
	SuccessCount       int64    `json:"success_count"`
	FailureCount       int64    `json:"failure_count"`
	LastRunAt          *string  `json:"last_run_at"`
	AvgDurationSeconds *float64 `json:"avg_duration_seconds"`
}

// flowStats is GET /flows/{flow_id}/stats: counts and average time from the run history.
func (d *Deps) flowStats(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return
	}
	var out FlowStatsOut
	var last *time.Time
	err := d.DB.QueryRow(q.ctx, `SELECT count(*), count(*) FILTER (WHERE status = 'SUCCESS'),
		count(*) FILTER (WHERE status = 'FAILURE'), max(started_at) FROM runs WHERE flow_id = $1`, f.ID).
		Scan(&out.RunCount, &out.SuccessCount, &out.FailureCount, &last)
	if err != nil {
		q.fail(err)
		return
	}
	out.LastRunAt = isoPtr(last)
	// the last 500 FINISHED runs, the average computed as Python does (in order, then round)
	rows, err := d.DB.Query(q.ctx, `SELECT started_at, finished_at FROM runs WHERE flow_id = $1 AND finished_at IS NOT NULL
		ORDER BY started_at DESC LIMIT 500`, f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	var sum float64
	n := 0
	for rows.Next() {
		var s, e *time.Time
		if err := rows.Scan(&s, &e); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		if s != nil && e != nil && !e.Before(*s) {
			sum += float64(e.Sub(*s).Microseconds()) / 1e6
			n++
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		q.fail(err)
		return
	}
	if n > 0 {
		avg := pyRound(sum/float64(n), 2)
		out.AvgDurationSeconds = &avg
	}
	server.JSON(w, http.StatusOK, out)
}

// pyRound is Python's round(x, ndigits) of a float: the exact value rounded half to even.
func pyRound(x float64, ndigits int) float64 {
	v, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', ndigits, 64), 64)
	return v
}

// flowPresenceLeave is DELETE /flows/{flow_id}/presence/{instance}: the tab closed.
func (d *Deps) flowPresenceLeave(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathIDs(w, r, "flow_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	f := q.flow(ids[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return
	}
	if _, err := d.DB.Exec(q.ctx, `DELETE FROM flow_presence WHERE flow_id = $1 AND instance = $2`, f.ID, r.PathValue("instance")); err != nil {
		q.fail(err)
		return
	}
	returnsNone(w)
}

// pyStrip is str.strip().
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return strings.ContainsRune(" \t\n\r\v\f\x1c\x1d\x1e\x1f\u0085                 　", r)
	})
}
