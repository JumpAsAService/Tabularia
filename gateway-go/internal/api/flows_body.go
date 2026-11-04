package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/flows.py, the routes that take a body or query parameters.

func init() {
	register("flows_body", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/flows/search", Handler: server.Authed(d.Auth, "/flows/search", d.searchFlows)},
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/flows", Handler: authedBody(d, "/projects/{project_id}/flows", d.createFlow)},
			{Method: http.MethodPatch, Pattern: "/flows/{flow_id}", Handler: authedBody(d, "/flows/{flow_id}", d.updateFlow)},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/presence", Handler: authedBody(d, "/flows/{flow_id}/presence", d.flowPresenceBeat)},
			{Method: http.MethodPut, Pattern: "/flows/{flow_id}/schedule", Handler: authedBody(d, "/flows/{flow_id}/schedule", d.setFlowSchedule)},
			{Method: http.MethodPost, Pattern: "/flows/{flow_id}/run-now", Handler: server.Authed(d.Auth, "/flows/{flow_id}/run-now", d.runFlowNow)},
		}
	})
}

// authedBody is a signed-in route with a body (withBody, then the authentication). The
// bytes stay readable in the request: a branch handed to the Python gateway sends them on.
func authedBody(d *Deps, pattern string, h func(http.ResponseWriter, *http.Request, *auth.Principal, pyd.Body)) http.Handler {
	inner := withBody(server.Authed(d.Auth, pattern, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		h(w, r, p, bodyOf(r))
	}))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			pyd.BadRequestBody().Write(w)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(raw))
		inner.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), rawBodyKey{}, raw)))
	})
}

type rawBodyKey struct{}

// rawBody gives the request back its body, as it came, for the Python gateway.
func rawBody(r *http.Request) *http.Request {
	raw, _ := r.Context().Value(rawBodyKey{}).([]byte)
	r.Body = io.NopCloser(bytes.NewReader(raw))
	r.ContentLength = int64(len(raw))
	return r
}

func anyStr(v any) *string {
	if s, ok := v.(string); ok {
		return &s
	}
	return nil
}

func anyInt(v any) *int64 {
	if n, ok := v.(int64); ok {
		return &n
	}
	return nil
}

// searchFlows is GET /flows/search: the readable flows, paginated, searched over all.
func (d *Deps) searchFlows(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	qs := pyd.ParseQuery(r.URL.RawQuery)
	qv := ps.Query(qs, "q", pyd.Optional(pyd.Str()), nil)
	limit := ps.Query(qs, "limit", pyd.Int().Ge(1).Le(200), int64(50))
	offset := ps.Query(qs, "offset", pyd.Int().Ge(0), int64(0))
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if len(readable) == 0 {
		server.JSON(w, http.StatusOK, Page[FlowOut]{Items: []FlowOut{}, Total: 0})
		return
	}
	where := `WHERE project_id = ANY($1)`
	args := []any{perm.Sorted(readable)}
	if s, _ := qv.(string); s != "" {
		where += ` AND (name ILIKE $2 OR description ILIKE $2)`
		args = append(args, "%"+s+"%")
	}
	var total int64
	if err := d.DB.QueryRow(q.ctx, `SELECT count(*) FROM flows `+where, args...).Scan(&total); err != nil {
		q.fail(err)
		return
	}
	flows, err := readFlows(q.ctx, d.DB, where+fmt.Sprintf(` ORDER BY name LIMIT %d OFFSET %d`, limit.(int64), offset.(int64)), args...)
	if err != nil {
		q.fail(err)
		return
	}
	items, err := q.withOwnerNames(flows)
	if err == nil {
		items, err = q.withLocks(flows, items)
	}
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, Page[FlowOut]{Items: items, Total: total})
}

// withOwnerNames is _with_owner_names: who created each flow and its latest run
// (run_history.latest_flow_runs).
func (q *request) withOwnerNames(flows []*flowRow) ([]FlowOut, error) {
	items := make([]FlowOut, len(flows))
	if len(flows) == 0 {
		return items, nil
	}
	ids := make([]int64, len(flows))
	for i, f := range flows {
		ids[i] = f.ID
	}
	names := map[int64]string{}
	rows, err := q.d.DB.Query(q.ctx, `SELECT id, coalesce(nullif(full_name, ''), email) FROM users
		WHERE id IN (SELECT owner_id FROM flows WHERE id = ANY($1))`, ids)
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
		names[id] = name
	}
	rows.Close()
	type last struct {
		status string
		at     time.Time
	}
	latest := map[int64]last{}
	rows, err = q.d.DB.Query(q.ctx, `SELECT r.flow_id, r.status, r.started_at FROM runs r WHERE r.id IN (
		SELECT (SELECT n.id FROM runs n WHERE n.flow_id = f.id AND n.kind = 'flow' ORDER BY n.started_at DESC LIMIT 1)
		FROM flows f WHERE f.id = ANY($1))`, ids)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id int64
		var l last
		if err := rows.Scan(&id, &l.status, &l.at); err != nil {
			rows.Close()
			return nil, err
		}
		latest[id] = l
	}
	rows.Close()
	for i, f := range flows {
		items[i] = f.out()
		if f.OwnerID != nil {
			if n, ok := names[*f.OwnerID]; ok {
				items[i].OwnerName = &n
			}
		}
		if l, ok := latest[f.ID]; ok {
			items[i].LastRunStatus, items[i].LastRunAt = strp(l.status), iso(l.at)
		}
	}
	return items, nil
}

// checkFlowName is _check_flow_name: no control character.
func checkFlowName(name string) *auth.Refusal {
	for _, c := range name {
		if c < 32 || c == 127 {
			return &auth.Refusal{Status: 422, Detail: "The flow's name cannot hold a line break, a tab or another control character"}
		}
	}
	return nil
}

// createFlow is POST /projects/{project_id}/flows (create_flow).
func (d *Deps) createFlow(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	projectID := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowCreate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	pid := int32IDs([]any{projectID})[0]
	q := d.req(w, r, p)
	if exists, err := q.projectExists(pid); err != nil {
		q.fail(err)
		return
	} else if !exists {
		q.refuse(http.StatusNotFound, "Folder not found")
		return
	}
	if !q.ensure(pid, perm.Edit) {
		return
	}
	name, definition := body.Get("name").(string), body.Get("definition").(string)
	if q.refused(checkFlowName(name), nil) || q.refused(q.authorizeDefinitionKeys(definition)) || q.refused(q.checkSave("", definition)) {
		return
	}
	engine, ref, err := q.validateEngine(anyStr(body.Get("engine")))
	if q.refused(ref, err) {
		return
	}
	prod, ref, err := q.validateProductionEngine(anyStr(body.Get("production_engine")))
	if q.refused(ref, err) {
		return
	}
	flow, err := q.insertFlow(&flowRow{Name: name, Description: body.Get("description").(string), Definition: definition,
		ProjectID: pid, OwnerID: &p.User.ID, Engine: engine, ProductionEngine: prod})
	if err == nil {
		_, err = q.snapshotVersion(flow.ID, "created")
	}
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow.create", TargetType: strp("flow"), TargetID: &flow.ID, TargetLabel: &flow.Name,
		Detail: pyjson.Object{{Key: "project_id", Value: pid}, {Key: "engine", Value: flow.Engine}}})
	if flow, err = q.reread(flow.ID); err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusCreated, flow.detail())
}

// toPython hands the request, as it came, to the Python gateway: with OpenLineage on, a
// rename or a move tells the catalog the new names (openlineage.catalog_names / renamed),
// which the Go gateway does not do yet.
func (q *request) toPython() { q.d.Upstream.ServeHTTP(q.w, rawBody(q.r)) }

// updateFlow is PATCH /flows/{flow_id} (update_flow).
func (d *Deps) updateFlow(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	flowID := ps.Path("flow_id", r.PathValue("flow_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowUpdate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	f := q.flow(int32IDs([]any{flowID})[0])
	if f == nil || !q.ensure(f.ProjectID, perm.Edit) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	name, description, definition := anyStr(body.Get("name")), anyStr(body.Get("description")), anyStr(body.Get("definition"))
	projectID, engine, production := anyInt(body.Get("project_id")), anyStr(body.Get("engine")), anyStr(body.Get("production_engine"))
	if definition != nil {
		if q.refused(q.authorizeDefinitionKeys(*definition)) || q.refused(q.checkSave(f.Definition, *definition)) {
			return
		}
	}
	if name != nil {
		n := pyStrip(*name)
		name = &n
		if n == "" {
			q.refuse(422, "The flow needs a name")
			return
		}
		if n != f.Name && q.refused(checkFlowName(n), nil) {
			return
		}
	}
	changed := []any{}
	for _, k := range []struct {
		key string
		set bool
	}{{"name", name != nil}, {"description", description != nil}, {"definition", definition != nil},
		{"project_id", projectID != nil}, {"engine", engine != nil}, {"production_engine", production != nil}} {
		if k.set {
			changed = append(changed, k.key)
		}
	}
	if (name != nil || projectID != nil) && q.d.Config.OpenLineage {
		q.toPython()
		return
	}
	moved := projectID != nil && *projectID != f.ProjectID
	if moved {
		dest := int32IDs([]any{*projectID})[0]
		if exists, err := q.projectExists(dest); err != nil {
			q.fail(err)
			return
		} else if !exists {
			q.refuse(http.StatusNotFound, "Destination folder not found")
			return
		}
		if !q.ensure(dest, perm.Edit) {
			return
		}
	}
	off := q.guardSomeoneElsesSchedule(f, definition, moved)
	if moved {
		f.ProjectID = *projectID
	}
	if name != nil {
		f.Name = *name
	}
	if description != nil {
		f.Description = *description
	}
	if definition != nil {
		f.Definition = *definition
	}
	if engine != nil {
		e, ref, err := q.validateEngine(engine)
		if q.refused(ref, err) {
			return
		}
		f.Engine = e
	}
	if production != nil {
		e, ref, err := q.validateProductionEngine(production)
		if q.refused(ref, err) {
			return
		}
		f.ProductionEngine = e
	}
	if err := q.saveFlow(f); err != nil {
		q.fail(err)
		return
	}
	if definition != nil { // only DAG changes make a version
		if _, err := q.snapshotVersion(f.ID, "edited"); err != nil {
			q.fail(err)
			return
		}
	}
	q.audit(audit.Event{Action: "flow.update", TargetType: strp("flow"), TargetID: &f.ID, TargetLabel: &f.Name,
		Detail: pyjson.Object{{Key: "changed", Value: changed}}})
	q.recordScheduleOff(f, off)
	g, err := q.reread(f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, g.detail())
}

// saveFlow writes the flow's editable columns, with a new updated_at.
func (q *request) saveFlow(f *flowRow) error {
	_, err := q.d.DB.Exec(q.ctx, `UPDATE flows SET name = $2, description = $3, definition = $4, project_id = $5, engine = $6,
		production_engine = $7, run_schedule = $8, run_scheduled_by = $9, next_run_at = $10, notify_emails = $11,
		notify_connection_id = $12, updated_at = $13 WHERE id = $1`, f.ID, f.Name, f.Description, f.Definition, f.ProjectID,
		f.Engine, f.ProductionEngine, f.RunSchedule, f.RunScheduledBy, f.NextRunAt, f.NotifyEmails, f.NotifyConnectionID,
		time.Now().UTC())
	return err
}

// presence: services/flow_presence.py
const (
	heartbeatSeconds    = 15
	presenceTTLSeconds  = 40
	maxInstancesPerFlow = 50
)

// PresenceOther is routes/flows.py PresenceOther.
type PresenceOther struct {
	UserID   int64  `json:"user_id"`
	Email    string `json:"email"`
	FullName string `json:"full_name"`
	Since    string `json:"since"`
}

// fromTimestamp is datetime.fromtimestamp(t, timezone.utc).isoformat().
func fromTimestamp(t float64) string {
	sec := math.Floor(t)
	us := math.RoundToEven((t - sec) * 1e6)
	return pyjson.Isoformat(time.Unix(int64(sec), int64(us)*1000))
}

// maskEmail is masking.mask_email.
func maskEmail(email string) string {
	text := pyStrip(email)
	if !strings.Contains(text, "@") {
		if text == "" {
			return ""
		}
		return "***"
	}
	i := strings.LastIndex(text, "@")
	local, domain := text[:i], text[i+1:]
	if local == "" {
		return "***@" + domain
	}
	r := []rune(local)
	return string(r[:1]) + "***@" + domain
}

// flowPresenceBeat is POST /flows/{flow_id}/presence: «I have this flow open», answered with
// who else has it open (flow_presence.beat).
func (d *Deps) flowPresenceBeat(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	flowID := ps.Path("flow_id", r.PathValue("flow_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.PresenceBeat)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	f := q.flow(int32IDs([]any{flowID})[0])
	if f == nil || !q.ensure(f.ProjectID, perm.View) {
		return
	}
	instance := body.Get("instance").(string)
	now := float64(time.Now().UnixNano()) / 1e9
	err := pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		tag, err := tx.Exec(q.ctx, `UPDATE flow_presence SET seen = $3 WHERE flow_id = $1 AND seen >= $4 AND instance = $2`,
			f.ID, instance, now, now-presenceTTLSeconds)
		if err != nil || tag.RowsAffected() > 0 {
			return err
		}
		// first time (or back after expiring: `since` starts again)
		if _, err := tx.Exec(q.ctx, `DELETE FROM flow_presence WHERE flow_id = $1 AND instance = $2`, f.ID, instance); err != nil {
			return err
		}
		var n int
		if err := tx.QueryRow(q.ctx, `SELECT count(*) FROM flow_presence WHERE flow_id = $1 AND seen >= $2`, f.ID, now-presenceTTLSeconds).Scan(&n); err != nil {
			return err
		}
		if n < maxInstancesPerFlow {
			_, err = tx.Exec(q.ctx, `INSERT INTO flow_presence (flow_id, instance, user_id, email, full_name, since, seen)
				VALUES ($1, $2, $3, $4, $5, $6, $6)`, f.ID, instance, p.User.ID, p.User.Email, p.User.FullName, now)
		}
		return err
	})
	if err != nil && !isUniqueViolation(err) { // the same tab beat twice together: it is there already
		q.fail(err)
		return
	}
	rows, err := d.DB.Query(q.ctx, `SELECT user_id, email, full_name, since FROM flow_presence
		WHERE flow_id = $1 AND seen >= $2 AND instance <> $3 ORDER BY since`, f.ID, now-presenceTTLSeconds, instance)
	if err != nil {
		q.fail(err)
		return
	}
	others := []PresenceOther{}
	for rows.Next() {
		var o PresenceOther
		var since float64
		if err := rows.Scan(&o.UserID, &o.Email, &o.FullName, &since); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		o.Since = fromTimestamp(since)
		others = append(others, o)
	}
	rows.Close()
	rs, err := q.c.Roles(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if rs.ObserverOnly() { // an observer reads people masked everywhere else: here too
		for i := range others {
			others[i].Email, others[i].FullName = maskEmail(others[i].Email), ""
		}
	}
	server.JSON(w, http.StatusOK, struct {
		Others           []PresenceOther `json:"others"`
		HeartbeatSeconds int             `json:"heartbeat_seconds"`
	}{others, heartbeatSeconds})
}

func isUniqueViolation(err error) bool {
	var pe interface{ SQLState() string }
	return errors.As(err, &pe) && pe.SQLState() == "23505"
}

// runnableNodeTypes is _RUNNABLE_NODE_TYPES.
var runnableNodeTypes = map[string]bool{"output": true, "refresh": true, "runflow": true, "script": true, "container": true}

// hasRunnableNodes is _has_runnable_nodes; an error where Python raises (a definition
// that is no object, a node that is no object).
func hasRunnableNodes(definition string) (bool, error) {
	if definition == "" {
		definition = "{}"
	}
	parsed, err := pyjson.Loads(definition)
	if err != nil {
		return false, nil
	}
	o, ok := parsed.(pyjson.Object)
	if !ok {
		return false, errors.New("the definition is not an object")
	}
	nodes := pyjson.Or(o.Value("nodes"), []any{})
	list, ok := nodes.([]any)
	if !ok {
		if s, isText := nodes.(string); isText {
			for range s {
				return false, errors.New("a node is not an object")
			}
		}
		return false, nil
	}
	for _, n := range list {
		if !pyjson.Truthy(n) {
			continue
		}
		node, ok := n.(pyjson.Object)
		if !ok {
			return false, errors.New("a node is not an object")
		}
		if t, ok := node.Value("type").(string); ok && runnableNodeTypes[t] {
			return true, nil
		}
	}
	return false, nil
}

// parseRecipients is notify_targets.parse_recipients.
func parseRecipients(text string) ([]string, *auth.Refusal) {
	var out []string
	for _, a := range strings.Split(strings.ReplaceAll(text, ";", ","), ",") {
		if a = pyStrip(a); a == "" {
			continue
		}
		if !strings.Contains(a, "@") || strings.HasPrefix(a, "@") || strings.HasSuffix(a, "@") {
			return nil, &auth.Refusal{Status: 422, Detail: "Invalid address: " + a}
		}
		out = append(out, a)
	}
	return out, nil
}

type connection struct {
	ID        int64
	Name      string
	ProjectID int64
	DBType    string
	Extra     string
}

func (q *request) connection(id int64) (*connection, error) {
	var c connection
	err := q.d.DB.QueryRow(q.ctx, `SELECT id, name, project_id, db_type, extra FROM connections WHERE id = $1`, id).
		Scan(&c.ID, &c.Name, &c.ProjectID, &c.DBType, &c.Extra)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &c, err
}

// allowedEmailDomains is routes/connections.py allowed_email_domains.
func allowedEmailDomains(c *connection) ([]string, *auth.Refusal) {
	extra := c.Extra
	if extra == "" {
		extra = "{}"
	}
	var raw any
	if v, err := pyjson.Loads(extra); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			raw = o.Value("allowed_domains")
		}
	}
	malformed := &auth.Refusal{Status: 422, Detail: fmt.Sprintf("The allowed domains of the SMTP connection '%s' are malformed: fix the connection", c.Name)}
	var items []any
	switch x := raw.(type) {
	case nil:
		return []string{}, nil
	case string:
		if x == "" {
			return []string{}, nil
		}
		for _, p := range strings.Split(x, ",") {
			items = append(items, p)
		}
	case []any:
		items = x
	default:
		return nil, malformed
	}
	out := []string{}
	for _, it := range items {
		s, ok := it.(string)
		if !ok {
			return nil, malformed
		}
		if pyStrip(s) != "" {
			out = append(out, strings.TrimLeft(strings.ToLower(pyStrip(s)), "@"))
		}
	}
	return out, nil
}

// ensureDeliverable is notify_targets.ensure_deliverable.
func ensureDeliverable(c *connection, recipients []string) *auth.Refusal {
	if c == nil {
		return nil
	}
	allowed, ref := allowedEmailDomains(c)
	if ref != nil || len(allowed) == 0 {
		return ref
	}
	in := map[string]bool{}
	for _, a := range allowed {
		in[a] = true
	}
	var outside []string
	for _, a := range recipients {
		if !in[strings.ToLower(a[strings.LastIndex(a, "@")+1:])] {
			outside = append(outside, a)
		}
	}
	if len(outside) > 0 {
		return &auth.Refusal{Status: 422, Detail: fmt.Sprintf("This connection can only send to %s: %s is not allowed",
			strings.Join(allowed, ", "), strings.Join(outside, ", "))}
	}
	return nil
}

// setFlowSchedule is PUT /flows/{flow_id}/schedule (set_flow_schedule).
func (d *Deps) setFlowSchedule(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	flowID := ps.Path("flow_id", r.PathValue("flow_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowScheduleUpdate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	f := q.flow(int32IDs([]any{flowID})[0])
	if f == nil || !q.ensure(f.ProjectID, perm.Run) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	spec := ""
	if s := anyStr(body.Get("cron")); s != nil {
		spec = pyStrip(*s)
	}
	if spec == "" {
		f.RunSchedule, f.RunScheduledBy, f.NextRunAt = nil, nil, nil
	} else {
		runnable, err := hasRunnableNodes(f.Definition)
		if err != nil {
			q.fail(err)
			return
		}
		if !runnable {
			q.refuse(422, "The flow has nothing to run (Output, refresh or run flow): add at least one of these nodes before scheduling it")
			return
		}
		spec, err = cron.ValidateSchedule(spec)
		if err != nil {
			q.refuse(422, err.Error())
			return
		}
		next, err := cron.NextFire(spec, time.Now(), d.Panel.Timezone())
		if err != nil {
			q.fail(err)
			return
		}
		f.RunSchedule, f.RunScheduledBy, f.NextRunAt = &spec, &p.User.ID, &next
	}
	if prod := anyStr(body.Get("production_engine")); prod != nil { // omitted = unchanged; "" = as development
		e, ref, err := q.validateProductionEngine(prod)
		if q.refused(ref, err) {
			return
		}
		f.ProductionEngine = e
	}
	emails, connID := anyStr(body.Get("notify_emails")), anyInt(body.Get("notify_connection_id"))
	if emails != nil || connID != nil {
		if q.refused(q.setFailureNotice(f, emails, connID)) {
			return
		}
	}
	if err := q.saveFlow(f); err != nil {
		q.fail(err)
		return
	}
	g, err := q.reread(f.ID)
	if err != nil {
		q.fail(err)
		return
	}
	notify := g.NotifyEmails != nil && *g.NotifyEmails != "" && g.NotifyConnectionID != nil && *g.NotifyConnectionID != 0
	q.audit(audit.Event{Action: "flow.schedule", TargetType: strp("flow"), TargetID: &g.ID, TargetLabel: &g.Name,
		Detail: pyjson.Object{{Key: "cron", Value: optAny(g.RunSchedule)}, {Key: "production_engine", Value: optAny(g.ProductionEngine)},
			{Key: "notify", Value: notify}}})
	server.JSON(w, http.StatusOK, g.detail())
}

func optAny(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

// setFailureNotice is _set_failure_notice: whom to notify when a scheduled run fails.
func (q *request) setFailureNotice(f *flowRow, emails *string, connID *int64) (*auth.Refusal, error) {
	if emails != nil {
		list, ref := parseRecipients(*emails)
		if ref != nil {
			return ref, nil
		}
		f.NotifyEmails = nil
		if joined := strings.Join(list, ", "); joined != "" {
			f.NotifyEmails = &joined
		}
	}
	if connID != nil {
		if *connID == 0 || *connID == -1 { // turns the notice off
			f.NotifyConnectionID = nil
		} else {
			c, err := q.connection(int32IDs([]any{*connID})[0])
			if err != nil {
				return nil, err
			}
			if c == nil || c.DBType != "smtp" {
				return &auth.Refusal{Status: 422, Detail: "An SMTP connection is required"}, nil
			}
			if ref, err := q.c.Ensure(q.ctx, c.ProjectID, perm.Connect); ref != nil || err != nil {
				return ref, err
			}
			f.NotifyConnectionID = &c.ID
		}
	}
	if f.NotifyEmails != nil && *f.NotifyEmails != "" && f.NotifyConnectionID != nil && *f.NotifyConnectionID != 0 {
		c, err := q.connection(*f.NotifyConnectionID)
		if err != nil {
			return nil, err
		}
		return ensureDeliverable(c, strings.Split(*f.NotifyEmails, ", ")), nil
	}
	return nil, nil
}

// alreadyRunning is the 409 of «Run now», in the asker's language.
var alreadyRunning = map[string]string{
	"en": "The flow is already queued or running: it can be run again once it has finished.",
	"it": "Il flusso è già in coda o in esecuzione: si potrà rilanciare quando avrà finito.",
	"de": "Der Flow ist bereits in der Warteschlange oder läuft: Er kann erneut gestartet werden, wenn er fertig ist.",
	"es": "El flow ya está en cola o en ejecución: se podrá lanzar de nuevo cuando termine.",
	"fr": "Le flux est déjà en file d'attente ou en cours : il pourra être relancé une fois terminé.",
}

// queueLock is orchestrator._QUEUE_LOCK, the advisory locks' namespace of the flows' queue.
const queueLock = 74610001

// runFlowNow is POST /flows/{flow_id}/run-now: the flow's orchestration queued, for an
// orchestrator to run with the caller's authority (create_orchestration_run). The Go
// gateway runs no orchestrator: one finds the row within ORCHESTRATOR__QUEUE_POLL_SECONDS,
// as from a process with APP__ROLE=api (`wake` does nothing there).
func (d *Deps) runFlowNow(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	flowID := ps.Path("flow_id", r.PathValue("flow_id"), pyd.Int())
	mode := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "mode", pyd.Str().Pattern("^(development|production)$"), "development")
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	f := q.flow(int32IDs([]any{flowID})[0])
	if f == nil || !q.ensure(f.ProjectID, perm.Run) || q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
		return
	}
	var runID int64
	running := false
	err := pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		if _, err := tx.Exec(q.ctx, `SELECT pg_advisory_xact_lock($1, $2)`, queueLock, int32(f.ID)); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(q.ctx, `SELECT 1 FROM runs WHERE kind = 'orchestration' AND flow_id = $1 AND status IN ('PENDING', 'STARTED') LIMIT 1`, f.ID).Scan(&one)
		if err == nil {
			running = true
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		return tx.QueryRow(q.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, engine_mode, input_key,
			output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ('orchestration', $1, '', 'PENDING', $2, 'manual', $3, '', '', '', '', false, '[]', $4) RETURNING id`,
			f.ID, p.User.ID, mode, time.Now().UTC()).Scan(&runID)
	})
	if err != nil {
		q.fail(err)
		return
	}
	if running {
		q.refuse(http.StatusConflict, language.Pick(alreadyRunning, r.Header.Get("Accept-Language")))
		return
	}
	q.audit(audit.Event{Action: "flow.run", TargetType: strp("flow"), TargetID: &f.ID, TargetLabel: &f.Name,
		Detail: pyjson.Object{{Key: "run_id", Value: runID}, {Key: "trigger", Value: "manual"}, {Key: "engine_mode", Value: mode}}})
	server.JSON(w, http.StatusAccepted, pyjson.Object{{Key: "status", Value: "started"}, {Key: "flow_id", Value: f.ID}, {Key: "run_id", Value: runID}})
}
