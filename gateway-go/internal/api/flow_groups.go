package api

import (
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/openlineage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/flow_groups.py: groups of flows, a grid of steps run one after another, the cells
// of a step in parallel. A Pushmail cell new or changed is checked as the group is saved
// (services/pushmail.py pushmail_errors, internal/api/jobs_pushmail.go).

func init() {
	register("flow_groups", func(d *Deps) []server.Route {
		a := func(pattern string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, pattern, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/flow-groups", Handler: a("/flow-groups", d.listFlowGroups)},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/flow-groups", Handler: a("/projects/{project_id}/flow-groups", d.listProjectFlowGroups)},
			{Method: http.MethodGet, Pattern: "/flow-groups/{group_id}", Handler: a("/flow-groups/{group_id}", d.getFlowGroup)},
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/flow-groups", Handler: authedBody(d, "/projects/{project_id}/flow-groups", d.createFlowGroup)},
			{Method: http.MethodPost, Pattern: "/flow-groups/{group_id}/duplicate", Handler: a("/flow-groups/{group_id}/duplicate", d.duplicateFlowGroup)},
			{Method: http.MethodPatch, Pattern: "/flow-groups/{group_id}", Handler: authedBody(d, "/flow-groups/{group_id}", d.updateFlowGroup)},
			{Method: http.MethodDelete, Pattern: "/flow-groups/{group_id}", Handler: a("/flow-groups/{group_id}", d.deleteFlowGroup)},
			{Method: http.MethodPost, Pattern: "/flow-groups/{group_id}/run-now", Handler: a("/flow-groups/{group_id}/run-now", d.runFlowGroupNow)},
			{Method: http.MethodPut, Pattern: "/flow-groups/{group_id}/schedule", Handler: authedBody(d, "/flow-groups/{group_id}/schedule", d.setFlowGroupSchedule)},
			{Method: http.MethodGet, Pattern: "/flow-groups/{group_id}/runs", Handler: a("/flow-groups/{group_id}/runs", d.listFlowGroupRuns)},
			{Method: http.MethodGet, Pattern: "/flow-groups/{group_id}/runs/{run_id}", Handler: a("/flow-groups/{group_id}/runs/{run_id}", d.getFlowGroupRun)},
		}
	})
}

type groupRow struct {
	ID             int64
	Name           string
	Description    string
	ProjectID      int64
	OwnerID        *int64
	Definition     string
	RunSchedule    *string
	RunScheduledBy *int64
	NextRunAt      *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

const groupColumns = `id, name, description, project_id, owner_id, definition, run_schedule, run_scheduled_by, next_run_at,
	created_at, updated_at`

func scanGroup(row pgx.Row) (*groupRow, error) {
	var g groupRow
	err := row.Scan(&g.ID, &g.Name, &g.Description, &g.ProjectID, &g.OwnerID, &g.Definition, &g.RunSchedule, &g.RunScheduledBy,
		&g.NextRunAt, &g.CreatedAt, &g.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &g, err
}

func (q *request) groups(where string, args ...any) ([]*groupRow, error) {
	rows, err := q.d.DB.Query(q.ctx, `SELECT `+groupColumns+` FROM flow_groups `+where, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []*groupRow{}
	for rows.Next() {
		g, err := scanGroup(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// group is _get_group.
func (q *request) group(id int64) *groupRow {
	g, err := scanGroup(q.d.DB.QueryRow(q.ctx, `SELECT `+groupColumns+` FROM flow_groups WHERE id = $1`, id))
	if err != nil {
		q.fail(err)
		return nil
	}
	if g == nil {
		q.refuse(http.StatusNotFound, "Flow group not found")
	}
	return g
}

// cell is a FlowGroupCell.
type cell struct {
	kind      string
	id        int64
	idText    string // the id as Python prints it (an int too large for int64 too)
	container *pyd.Object
	pushmail  *pyd.ModelValue
}

// groupDef is a validated FlowGroupDefinition: its steps, its cells (nil: an empty one).
type groupDef struct {
	mv    *pyd.ModelValue
	steps [][]*cell
}

func newGroupDef(v any) (*groupDef, []*pyd.Error) {
	mv, errs := flowgroups.Model(v)
	if errs != nil {
		return nil, errs
	}
	return groupDefOf(mv), nil
}

// groupDefOf reads a validated grid's cells.
func groupDefOf(mv *pyd.ModelValue) *groupDef {
	g := &groupDef{mv: mv}
	steps, _ := mv.Get("steps").([]any)
	for _, row := range steps {
		cells, _ := row.([]any)
		var r []*cell
		for _, c := range cells {
			cm, ok := c.(*pyd.ModelValue)
			if !ok {
				r = append(r, nil)
				continue
			}
			x := &cell{kind: cm.Get("kind").(string), idText: fmt.Sprint(cm.Get("id"))}
			x.id, _ = cm.Get("id").(int64)
			x.container, _ = cm.Get("container").(*pyd.Object)
			x.pushmail, _ = cm.Get("pushmail").(*pyd.ModelValue)
			r = append(r, x)
		}
		g.steps = append(g.steps, r)
	}
	return g
}

// definitionOf is services/flow_groups.definition_of (internal/flowgroups.Parse).
func definitionOf(text string) *groupDef { return groupDefOf(flowgroups.Parse(text)) }

func (g *groupDef) cells() []*cell {
	var out []*cell
	for _, row := range g.steps {
		for _, c := range row {
			if c != nil {
				out = append(out, c)
			}
		}
	}
	return out
}

// json is model_dump(mode="json") as the answers and model_dump_json() write it.
func (g *groupDef) dump() any { return fromPyd(g.mv.Dump()) }

func (g *groupDef) dumpJSON() string {
	s, _ := pyjson.Dumps(g.dump(), pyjson.Options{Compact: true})
	return s
}

// containerNode is _container_node.
func containerNode(c *cell) pyjson.Object {
	data := pyjson.Object{}
	if c.container != nil {
		data = fromPyd(c.container).(pyjson.Object)
	}
	return pyjson.Object{{Key: "id", Value: "container-cell-" + c.idText}, {Key: "type", Value: "container"}, {Key: "data", Value: data}}
}

// cellKey is _cell_key: what a cell runs.
func cellKey(c *cell) string {
	switch c.kind {
	case "container":
		return c.kind + "\x00" + c.idText + "\x00" + flowaccess.ContainerFingerprint(containerNode(c))
	case "pushmail":
		pm := ""
		if c.pushmail != nil {
			pm, _ = pyjson.Dumps(fromPyd(c.pushmail.Dump()), pyjson.Options{Compact: true})
		}
		return c.kind + "\x00" + c.idText + "\x00" + pm
	}
	return c.kind + "\x00" + c.idText
}

var kindName = map[string]string{"flow": "flow", "refresh": "datasource", "script": "saved script", "container": "container",
	"pushmail": "pushmail", "sql": "SQL script"}

// groupObject is a flow, datasource, saved script or SQL script a cell names.
type groupObject struct {
	name, kind, definition string
	project, environment   int64
	sqlOutput              bool
}

// objects is _objects: what the cells name, by kind.
func (q *request) groupObjects(g *groupDef) (map[string]map[int64]*groupObject, error) {
	ids := map[string][]int64{}
	for _, c := range g.cells() {
		ids[c.kind] = append(ids[c.kind], c.id)
	}
	out := map[string]map[int64]*groupObject{}
	queries := map[string]string{
		"flow":    `SELECT id, name, project_id, definition, '', 0, '' FROM flows WHERE id = ANY($1)`,
		"refresh": `SELECT id, name, project_id, '', kind, 0, '' FROM datasources WHERE id = ANY($1)`,
		"script":  `SELECT id, name, project_id, '', '', environment_id, '' FROM saved_scripts WHERE id = ANY($1)`,
		"sql": `SELECT s.id, s.name, s.project_id, '', '', 0, coalesce(v.content, '') FROM sql_scripts s
			LEFT JOIN sql_script_versions v ON v.script_id = s.id AND v.version = s.current_version WHERE s.id = ANY($1)`,
	}
	for kind, sql := range queries {
		out[kind] = map[int64]*groupObject{}
		if len(ids[kind]) == 0 {
			continue
		}
		rows, err := q.d.DB.Query(q.ctx, sql, ids[kind])
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			var o groupObject
			var content string
			if err := rows.Scan(&id, &o.name, &o.project, &o.definition, &o.kind, &o.environment, &content); err != nil {
				rows.Close()
				return nil, err
			}
			if kind == "sql" {
				o.sqlOutput = sqlOutputOf(content)
			}
			out[kind][id] = &o
		}
		rows.Close()
	}
	return out, nil
}

// sqlOutputOf is sql_script_runs.output_of(sql_scripts.content_of(…)) is not None.
func sqlOutputOf(content string) bool {
	if content == "" {
		return false
	}
	v, err := pyjson.Loads(content)
	o, ok := v.(pyjson.Object)
	if err != nil || !ok {
		return false
	}
	out, ok := o.Value("output").(pyjson.Object)
	_, isInt := pyjson.IsInt(out.Value("project_id"))
	return ok && pyjson.Truthy(out.Value("name")) && isInt
}

// sees is _sees.
func (q *request) sees(kind string, o *groupObject, readable map[int64]bool) (bool, error) {
	switch kind {
	case "script":
		return q.acc.CanSeeScript(q.ctx, &flowaccess.SavedScript{Name: o.name, ProjectID: o.project, EnvironmentID: o.environment})
	case "sql":
		admin, err := q.c.IsAdmin(q.ctx)
		if err != nil || admin {
			return admin, err
		}
		return q.c.Has(q.ctx, o.project, perm.View)
	}
	return readable[o.project], nil
}

// containerErrors is _container_errors.
func (q *request) containerErrors(c *cell) ([]string, error) {
	node := containerNode(c)
	if r := flowaccess.CheckContainer(node, true, q.d.flowSettings()); r != nil {
		return []string{r.Detail}, nil
	}
	d := node.Value("data").(pyjson.Object)
	var reg *flowaccess.Registry
	if rid, ok := pyjson.IsInt(d.Value("registryId")); ok {
		var err error
		if reg, err = q.acc.RegistryByID(q.ctx, rid); err != nil {
			return nil, err
		}
	}
	label := flowaccess.ContainerLabel(node)
	if reg == nil || !reg.Enabled {
		return []string{fmt.Sprintf("container «%s»: choose a registry that is in use", label)}, nil
	}
	admin, err := q.c.IsAdmin(q.ctx)
	if err != nil || admin {
		return nil, err
	}
	use, err := q.acc.CanUseRegistry(q.ctx, reg)
	if err != nil {
		return nil, err
	}
	allowed := false
	if use {
		if allowed, err = q.acc.RepositoryAllowed(q.ctx, reg, pyjson.Str(pyjson.Or(d.Value("repository"), ""))); err != nil {
			return nil, err
		}
	}
	if !allowed {
		return []string{fmt.Sprintf("container «%s»: the image is not among those of «%s» you may use", label, reg.Name)}, nil
	}
	return nil, nil
}

// checked is _checked: every cell new or changed must exist, be visible and runnable by
// whoever saves; a 422 lists the first ten problems.
func (q *request) checked(def, before *groupDef) (*auth.Refusal, error) {
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		return nil, err
	}
	objects, err := q.groupObjects(def)
	if err != nil {
		return nil, err
	}
	already := map[string]bool{}
	if before != nil {
		for _, c := range before.cells() {
			already[cellKey(c)] = true
		}
	}
	var errs []string
	for n, row := range def.steps {
		inStep := map[string]bool{}
		for _, c := range row {
			if c == nil {
				continue
			}
			k := c.kind + "\x00" + c.idText
			if inStep[k] {
				errs = append(errs, fmt.Sprintf("step %d: the same %s twice", n+1, kindName[c.kind]))
				continue
			}
			inStep[k] = true
			if already[cellKey(c)] {
				continue
			}
			if c.kind == "container" {
				ce, err := q.containerErrors(c)
				if err != nil {
					return nil, err
				}
				for _, e := range ce {
					errs = append(errs, fmt.Sprintf("step %d: %s", n+1, e))
				}
				continue
			}
			if c.kind == "pushmail" {
				var cfg pyjson.Object
				if c.pushmail != nil {
					cfg, _ = fromPyd(c.pushmail.Dump()).(pyjson.Object)
				}
				pe, err := q.d.pushmailErrors(q.ctx, &q.p.User, cfg, readable)
				if err != nil {
					return nil, err
				}
				for _, e := range pe {
					errs = append(errs, fmt.Sprintf("step %d: %s", n+1, e))
				}
				continue
			}
			o := objects[c.kind][c.id]
			see := false
			if o != nil {
				if see, err = q.sees(c.kind, o, readable); err != nil {
					return nil, err
				}
			}
			switch {
			case o == nil || !see:
				errs = append(errs, fmt.Sprintf("step %d: %s %s not found", n+1, kindName[c.kind], c.idText))
			case c.kind == "refresh" && o.kind != "database":
				errs = append(errs, fmt.Sprintf("step %d: '%s' is published by a flow, it cannot be refreshed", n+1, o.name))
			case c.kind == "sql" && !o.sqlOutput:
				errs = append(errs, fmt.Sprintf("step %d: '%s' does not save its result as a datasource: give it one on its page first", n+1, o.name))
			default:
				run, err := q.c.Has(q.ctx, o.project, perm.Run)
				if err != nil {
					return nil, err
				}
				if !run {
					errs = append(errs, fmt.Sprintf("step %d: adding '%s' needs RUN on its folder", n+1, o.name))
				} else if c.kind == "flow" {
					why, err := q.acc.Blocked(q.ctx, o.definition)
					if err != nil {
						return nil, err
					}
					if why != "" {
						errs = append(errs, fmt.Sprintf("step %d: '%s' runs images or scripts you have no access to", n+1, o.name))
					}
				}
			}
		}
	}
	if len(errs) > 0 {
		if len(errs) > 10 {
			errs = errs[:10]
		}
		return &auth.Refusal{Status: 422, Detail: strings.Join(errs, "; ")}, nil
	}
	return nil, nil
}

// cellLabel is FlowGroupCellLabel.
type cellLabel struct {
	Name      *string `json:"name"`
	ProjectID *int64  `json:"project_id"`
	Missing   bool    `json:"missing"`
	Hidden    bool    `json:"hidden"`
}

// labels is _labels: every cell's label, for the reader.
func (q *request) labels(def *groupDef) ([][]*cellLabel, error) {
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		return nil, err
	}
	objects, err := q.groupObjects(def)
	if err != nil {
		return nil, err
	}
	out := [][]*cellLabel{}
	for _, row := range def.steps {
		labels := []*cellLabel{}
		for _, c := range row {
			switch {
			case c == nil:
				labels = append(labels, nil)
			case c.kind == "container":
				d := pyjson.Object{}
				if c.container != nil {
					d = fromPyd(c.container).(pyjson.Object)
				}
				image := pyjson.Str(pyjson.Or(d.Value("repository"), "?")) + ":" + pyjson.Str(pyjson.Or(d.Value("tag"), "latest"))
				labels = append(labels, &cellLabel{Name: strp(pyjson.Str(pyjson.Or(d.Value("label"), image)))})
			case c.kind == "pushmail":
				name := ""
				if c.pushmail != nil {
					name = pyStrip(c.pushmail.Get("label").(string))
				}
				if name == "" {
					name = "Pushmail"
				}
				labels = append(labels, &cellLabel{Name: &name})
			default:
				o := objects[c.kind][c.id]
				if o == nil {
					labels = append(labels, &cellLabel{Missing: true})
					continue
				}
				see, err := q.sees(c.kind, o, readable)
				if err != nil {
					return nil, err
				}
				if !see {
					labels = append(labels, &cellLabel{Hidden: true})
				} else {
					p := o.project
					labels = append(labels, &cellLabel{Name: strp(o.name), ProjectID: &p})
				}
			}
		}
		out = append(out, labels)
	}
	return out, nil
}

// FlowGroupOut is schemas/models.py FlowGroupOut.
type FlowGroupOut struct {
	ID             int64   `json:"id"`
	Name           string  `json:"name"`
	Description    string  `json:"description"`
	ProjectID      int64   `json:"project_id"`
	OwnerID        *int64  `json:"owner_id"`
	Definition     pyValue `json:"definition"`
	RunSchedule    *string `json:"run_schedule"`
	RunScheduledBy *int64  `json:"run_scheduled_by"`
	NextRunAt      *string `json:"next_run_at"`
	CreatedAt      *string `json:"created_at"`
	UpdatedAt      *string `json:"updated_at"`
}

// FlowGroupDetail is FlowGroupOut with the cells' labels and what the reader may do.
type FlowGroupDetail struct {
	FlowGroupOut
	Cells   [][]*cellLabel `json:"cells"`
	CanEdit bool           `json:"can_edit"`
	CanRun  bool           `json:"can_run"`
}

func (g *groupRow) out() FlowGroupOut {
	return FlowGroupOut{ID: g.ID, Name: g.Name, Description: g.Description, ProjectID: g.ProjectID, OwnerID: g.OwnerID,
		Definition: pyValue{definitionOf(g.Definition).dump()}, RunSchedule: g.RunSchedule, RunScheduledBy: g.RunScheduledBy,
		NextRunAt: isoPtr(g.NextRunAt), CreatedAt: iso(g.CreatedAt), UpdatedAt: iso(g.UpdatedAt)}
}

// detail is _detail.
func (q *request) detail(g *groupRow) (*FlowGroupDetail, error) {
	cells, err := q.labels(definitionOf(g.Definition))
	if err != nil {
		return nil, err
	}
	edit, err := q.c.Has(q.ctx, g.ProjectID, perm.Edit)
	if err != nil {
		return nil, err
	}
	run, err := q.c.Has(q.ctx, g.ProjectID, perm.Run)
	if err != nil {
		return nil, err
	}
	return &FlowGroupDetail{g.out(), cells, edit, run}, nil
}

func (q *request) answerDetail(status int, id int64) {
	g, err := scanGroup(q.d.DB.QueryRow(q.ctx, `SELECT `+groupColumns+` FROM flow_groups WHERE id = $1`, id))
	if err == nil && g == nil {
		err = errors.New("the flow group is gone")
	}
	var out *FlowGroupDetail
	if err == nil {
		out, err = q.detail(g)
	}
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(q.w, status, out)
}

func groupOuts(gs []*groupRow) []FlowGroupOut {
	out := make([]FlowGroupOut, len(gs))
	for i, g := range gs {
		out[i] = g.out()
	}
	return out
}

// listFlowGroups is GET /flow-groups.
func (d *Deps) listFlowGroups(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	q := d.req(w, r, p)
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if len(readable) == 0 {
		server.JSON(w, http.StatusOK, []FlowGroupOut{})
		return
	}
	gs, err := q.groups(`WHERE project_id = ANY($1) ORDER BY name`, perm.Sorted(readable))
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, groupOuts(gs))
}

// listProjectFlowGroups is GET /projects/{project_id}/flow-groups.
func (d *Deps) listProjectFlowGroups(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
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
	gs, err := q.groups(`WHERE project_id = $1 ORDER BY name`, ids[0])
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, groupOuts(gs))
}

// containerBlocked is _container_blocked.
func (q *request) containerBlocked(def *groupDef) (string, error) {
	var nodes []any
	for _, c := range def.cells() {
		if c.kind == "container" {
			nodes = append(nodes, containerNode(c))
		}
	}
	if len(nodes) == 0 {
		return "", nil
	}
	text, _ := pyjson.Dumps(pyjson.Object{{Key: "nodes", Value: nodes}}, pyjson.Options{EnsureASCII: true})
	return q.acc.RegistryBlocked(q.ctx, text)
}

// getFlowGroup is GET /flow-groups/{group_id}.
func (d *Deps) getFlowGroup(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	g := q.group(ids[0])
	if g == nil || !q.ensure(g.ProjectID, perm.View) {
		return
	}
	why, err := q.containerBlocked(definitionOf(g.Definition))
	if err != nil {
		q.fail(err)
		return
	}
	if why != "" {
		q.refuse(http.StatusForbidden, strings.ReplaceAll(why, "This flow", "This flow group"))
		return
	}
	q.answerDetail(http.StatusOK, g.ID)
}

func (q *request) groupNameTaken(project int64, name string, exclude int64) (bool, error) {
	var one int
	err := q.d.DB.QueryRow(q.ctx, `SELECT 1 FROM flow_groups WHERE project_id = $1 AND name = $2 AND id <> $3 LIMIT 1`, project, name, exclude).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func cellCount(def *groupDef) int { return len(def.cells()) }

// createFlowGroup is POST /projects/{project_id}/flow-groups.
func (d *Deps) createFlowGroup(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	pidV := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowGroupCreate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	pid := int32IDs([]any{pidV})[0]
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
	name := pyStrip(body.Get("name").(string))
	if name == "" {
		q.refuse(422, "The flow group needs a name")
		return
	}
	if taken, err := q.groupNameTaken(pid, name, 0); err != nil {
		q.fail(err)
		return
	} else if taken {
		q.refuse(http.StatusConflict, fmt.Sprintf("There is already a flow group '%s' in this folder", name))
		return
	}
	def, _ := newGroupDef(asInput(body.Get("definition")))
	if q.refused(q.checked(def, nil)) {
		return
	}
	now := time.Now().UTC()
	var id int64
	err := d.DB.QueryRow(q.ctx, `INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id`, name, pyStrip(body.Get("description").(string)), pid, p.User.ID,
		def.dumpJSON(), now).Scan(&id)
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow_group.create", TargetType: strp("flow_group"), TargetID: &id, TargetLabel: &name,
		Detail: pyjson.Object{{Key: "project_id", Value: pid}, {Key: "steps", Value: len(def.steps)}, {Key: "cells", Value: cellCount(def)}}})
	q.answerDetail(http.StatusCreated, id)
}

// duplicateFlowGroup is POST /flow-groups/{group_id}/duplicate.
func (d *Deps) duplicateFlowGroup(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	src := q.group(ids[0])
	if src == nil || !q.ensure(src.ProjectID, perm.Edit) {
		return
	}
	def := definitionOf(src.Definition)
	if q.refused(q.checked(def, nil)) {
		return
	}
	name, err := copynames.Free(q.ctx, d.DB, "flow_groups", src.ProjectID, src.Name)
	if err != nil {
		q.fail(err)
		return
	}
	now := time.Now().UTC()
	var id int64
	err = d.DB.QueryRow(q.ctx, `INSERT INTO flow_groups (name, description, project_id, owner_id, definition, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id`, name, src.Description, src.ProjectID, p.User.ID, def.dumpJSON(), now).Scan(&id)
	if isUniqueViolation(err) {
		q.refuse(http.StatusConflict, "Another copy took that name at the same moment: try again")
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow_group.create", TargetType: strp("flow_group"), TargetID: &id, TargetLabel: &name,
		Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "steps", Value: len(def.steps)},
			{Key: "cells", Value: cellCount(def)}, {Key: "duplicate_of", Value: src.ID}}})
	q.answerDetail(http.StatusCreated, id)
}

// updateFlowGroup is PATCH /flow-groups/{group_id}.
func (d *Deps) updateFlowGroup(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	gidV := ps.Path("group_id", r.PathValue("group_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowGroupUpdate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	g := q.group(int32IDs([]any{gidV})[0])
	if g == nil || !q.ensure(g.ProjectID, perm.Edit) {
		return
	}
	newName, newDesc, newProject := anyStr(body.Get("name")), anyStr(body.Get("description")), anyInt(body.Get("project_id"))
	var newDef *groupDef
	if v := body.Get("definition"); v != nil {
		newDef, _ = newGroupDef(asInput(v))
	}
	before := definitionOf(g.Definition)
	var lineageNames openlineage.Names
	if newName != nil || newProject != nil {
		var err error
		if lineageNames, err = q.d.Lineage.CatalogNames(q.ctx, q.d.DB, openlineage.Ref{Kind: openlineage.KindFlowGroup, ID: g.ID,
			ProjectID: g.ProjectID, Name: g.Name}); err != nil {
			q.fail(err)
			return
		}
	}
	detail := pyjson.Object{}
	var off pyjson.Object
	target := g.ProjectID
	if newProject != nil && *newProject != g.ProjectID {
		dest := int32IDs([]any{*newProject})[0]
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
		target = *newProject
		detail = detail.Set("moved_to", target)
		if g.RunSchedule != nil && *g.RunSchedule != "" && (g.RunScheduledBy == nil || *g.RunScheduledBy != p.User.ID) {
			off = pyjson.Object{{Key: "turned_off", Value: true}, {Key: "reason", Value: "the flow group was moved to another folder"},
				{Key: "author", Value: optInt64(g.RunScheduledBy)}}
			g.RunSchedule, g.RunScheduledBy, g.NextRunAt = nil, nil, nil
			detail = detail.Set("schedule_cleared", true)
		}
	}
	name := g.Name
	if newName != nil {
		name = pyStrip(*newName)
	}
	if name == "" {
		q.refuse(422, "The flow group needs a name")
		return
	}
	if taken, err := q.groupNameTaken(target, name, g.ID); err != nil {
		q.fail(err)
		return
	} else if taken {
		q.refuse(http.StatusConflict, fmt.Sprintf("There is already a flow group '%s' in this folder", name))
		return
	}
	if newDef != nil {
		if q.refused(q.checked(newDef, before)) {
			return
		}
		old := map[string]bool{}
		for _, c := range before.cells() {
			old[cellKey(c)] = true
		}
		added := map[string]bool{}
		for _, c := range newDef.cells() {
			if !old[cellKey(c)] {
				added[cellKey(c)] = true
			}
		}
		if len(added) > 0 && g.RunSchedule != nil && *g.RunSchedule != "" && (g.RunScheduledBy == nil || *g.RunScheduledBy != p.User.ID) {
			off = pyjson.Object{{Key: "turned_off", Value: true}, {Key: "author", Value: optInt64(g.RunScheduledBy)},
				{Key: "reason", Value: fmt.Sprintf("someone else added %d cells", len(added))}}
			g.RunSchedule, g.RunScheduledBy, g.NextRunAt = nil, nil, nil
			detail = detail.Set("schedule_cleared", true)
		}
		g.Definition = newDef.dumpJSON()
		detail = detail.Set("steps", len(newDef.steps)).Set("cells", cellCount(newDef))
	}
	g.Name, g.ProjectID = name, target
	if newDesc != nil {
		g.Description = pyStrip(*newDesc)
	}
	if _, err := d.DB.Exec(q.ctx, `UPDATE flow_groups SET name = $2, description = $3, project_id = $4, definition = $5,
		run_schedule = $6, run_scheduled_by = $7, next_run_at = $8, updated_at = $9 WHERE id = $1`, g.ID, g.Name, g.Description,
		g.ProjectID, g.Definition, g.RunSchedule, g.RunScheduledBy, g.NextRunAt, time.Now().UTC()); err != nil {
		q.fail(err)
		return
	}
	q.d.Lineage.Renamed(q.ctx, q.d.DB, lineageNames)
	var auditDetail pyjson.Object
	if len(detail) > 0 {
		auditDetail = detail
	}
	q.audit(audit.Event{Action: "flow_group.update", TargetType: strp("flow_group"), TargetID: &g.ID, TargetLabel: &g.Name, Detail: auditDetail})
	if len(off) > 0 {
		q.audit(audit.Event{Action: "flow_group.schedule", TargetType: strp("flow_group"), TargetID: &g.ID, TargetLabel: &g.Name, Detail: off})
	}
	q.answerDetail(http.StatusOK, g.ID)
}

// groupQueueLock is orchestrator._GROUP_QUEUE_LOCK.
const groupQueueLock = 74610004

// deleteFlowGroup is DELETE /flow-groups/{group_id}: not while it is queued or running.
func (d *Deps) deleteFlowGroup(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	g := q.group(ids[0])
	if g == nil || !q.ensure(g.ProjectID, perm.Edit) {
		return
	}
	live := false
	err := pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		if _, err := tx.Exec(q.ctx, `SELECT pg_advisory_xact_lock($1, $2)`, groupQueueLock, int32(g.ID)); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(q.ctx, `SELECT 1 FROM runs WHERE flow_group_id = $1 AND status NOT IN ('SUCCESS', 'FAILURE') LIMIT 1`, g.ID).Scan(&one)
		if err == nil {
			live = true
			return errRollback
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		for _, sql := range []string{
			`UPDATE runs SET parent_run_id = NULL WHERE parent_run_id IN (SELECT id FROM runs WHERE flow_group_id = $1)`,
			`DELETE FROM runs WHERE flow_group_id = $1`,
			`DELETE FROM flow_groups WHERE id = $1`,
		} {
			if _, err := tx.Exec(q.ctx, sql, g.ID); err != nil {
				return err
			}
		}
		return nil
	})
	if live {
		q.refuse(http.StatusConflict, "The flow group is queued or running: delete it once it has finished")
		return
	}
	if err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow_group.delete", TargetType: strp("flow_group"), TargetID: &g.ID, TargetLabel: &g.Name})
	returnsNone(w)
}

var groupAlreadyRunning = map[string]string{
	"en": "The flow group is already queued or running: it can be run again once it has finished.",
	"it": "Il gruppo di flussi è già in coda o in esecuzione: si potrà rilanciare quando avrà finito.",
	"de": "Die Flow-Gruppe ist bereits in der Warteschlange oder läuft: Sie kann erneut gestartet werden, wenn sie fertig ist.",
	"es": "El grupo de flujos ya está en cola o en ejecución: se podrá lanzar de nuevo cuando termine.",
	"fr": "Le groupe de flux est déjà en file d'attente ou en cours : il pourra être relancé une fois terminé.",
}

// runFlowGroupNow is POST /flow-groups/{group_id}/run-now: a run queued for an
// orchestrator (create_group_run), its flows in production mode.
func (d *Deps) runFlowGroupNow(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	g := q.group(ids[0])
	if g == nil || !q.ensure(g.ProjectID, perm.Run) {
		return
	}
	if cellCount(definitionOf(g.Definition)) == 0 {
		q.refuse(422, "The flow group is empty: add a flow or a datasource first")
		return
	}
	var runID int64
	outcome := ""
	err := pgx.BeginFunc(q.ctx, d.DB, func(tx pgx.Tx) error {
		if _, err := tx.Exec(q.ctx, `SELECT pg_advisory_xact_lock($1, $2)`, groupQueueLock, int32(g.ID)); err != nil {
			return err
		}
		var one int
		if err := tx.QueryRow(q.ctx, `SELECT 1 FROM flow_groups WHERE id = $1`, g.ID).Scan(&one); errors.Is(err, pgx.ErrNoRows) {
			outcome = "gone"
			return nil
		} else if err != nil {
			return err
		}
		err := tx.QueryRow(q.ctx, `SELECT 1 FROM runs WHERE kind = 'orchestration' AND flow_group_id = $1 AND status IN ('PENDING', 'STARTED') LIMIT 1`, g.ID).Scan(&one)
		if err == nil {
			outcome = "running"
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		return tx.QueryRow(q.ctx, `INSERT INTO runs (kind, flow_group_id, task_id, status, launched_by, trigger_type, engine_mode, input_key,
			output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
			VALUES ('orchestration', $1, '', 'PENDING', $2, 'manual', 'production', '', '', '', '', false, '[]', $3) RETURNING id`,
			g.ID, p.User.ID, time.Now().UTC()).Scan(&runID)
	})
	switch {
	case err != nil:
		q.fail(err)
		return
	case outcome == "gone":
		q.refuse(http.StatusNotFound, "Flow group not found")
		return
	case outcome == "running":
		q.refuse(http.StatusConflict, language.Pick(groupAlreadyRunning, r.Header.Get("Accept-Language")))
		return
	}
	q.audit(audit.Event{Action: "flow_group.run", TargetType: strp("flow_group"), TargetID: &g.ID, TargetLabel: &g.Name,
		Detail: pyjson.Object{{Key: "run_id", Value: runID}, {Key: "trigger", Value: "manual"}}})
	server.JSON(w, http.StatusAccepted, pyjson.Object{{Key: "status", Value: "started"}, {Key: "flow_group_id", Value: g.ID}, {Key: "run_id", Value: runID}})
}

// setFlowGroupSchedule is PUT /flow-groups/{group_id}/schedule.
func (d *Deps) setFlowGroupSchedule(w http.ResponseWriter, r *http.Request, p *auth.Principal, b pyd.Body) {
	var ps pyd.Params
	gidV := ps.Path("group_id", r.PathValue("group_id"), pyd.Int())
	body := ps.Body(b, gatewaymodels.FlowGroupScheduleUpdate)
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	g := q.group(int32IDs([]any{gidV})[0])
	if g == nil || !q.ensure(g.ProjectID, perm.Run) {
		return
	}
	spec := ""
	if s := anyStr(body.Get("cron")); s != nil {
		spec = pyStrip(*s)
	}
	if spec == "" {
		g.RunSchedule, g.RunScheduledBy, g.NextRunAt = nil, nil, nil
	} else {
		if cellCount(definitionOf(g.Definition)) == 0 {
			q.refuse(422, "The flow group is empty: add a flow or a datasource before scheduling it")
			return
		}
		var err error
		if spec, err = cron.ValidateSchedule(spec); err != nil {
			q.refuse(422, err.Error())
			return
		}
		next, err := cron.NextFire(spec, time.Now(), d.Panel.AppTimezone())
		if err != nil {
			q.fail(err)
			return
		}
		g.RunSchedule, g.RunScheduledBy, g.NextRunAt = &spec, &p.User.ID, &next
	}
	if _, err := d.DB.Exec(q.ctx, `UPDATE flow_groups SET run_schedule = $2, run_scheduled_by = $3, next_run_at = $4, updated_at = $5
		WHERE id = $1`, g.ID, g.RunSchedule, g.RunScheduledBy, g.NextRunAt, time.Now().UTC()); err != nil {
		q.fail(err)
		return
	}
	q.audit(audit.Event{Action: "flow_group.schedule", TargetType: strp("flow_group"), TargetID: &g.ID, TargetLabel: &g.Name,
		Detail: pyjson.Object{{Key: "cron", Value: optAny(g.RunSchedule)}}})
	q.answerDetail(http.StatusOK, g.ID)
}

// FlowGroupRunOut is schemas/models.py FlowGroupRunOut.
type FlowGroupRunOut struct {
	ID          int64   `json:"id"`
	Status      string  `json:"status"`
	TriggerType string  `json:"trigger_type"`
	LaunchedBy  *int64  `json:"launched_by"`
	Error       *string `json:"error"`
	StartedAt   *string `json:"started_at"`
	FinishedAt  *string `json:"finished_at"`
	Cells       pyValue `json:"cells"`
}

// runsOut is _runs_out: a cell's error and run only for whoever may read its object.
func (q *request) groupRunsOut(runs []*runRow) ([]FlowGroupRunOut, error) {
	grids := make([][]any, len(runs))
	var named []pyjson.Object
	for i, run := range runs {
		var grid []any
		if run.GroupCells != nil && *run.GroupCells != "" {
			if v, err := pyjson.Loads(*run.GroupCells); err == nil {
				grid, _ = v.([]any)
			}
		}
		if grid == nil {
			grid = []any{}
		}
		grids[i] = grid
		for _, row := range grid {
			if cells, ok := row.([]any); ok {
				for j, c := range cells {
					if o, ok := c.(pyjson.Object); ok {
						named = append(named, o)
						_ = j
					}
				}
			}
		}
	}
	ids := map[string][]int64{}
	for _, c := range named {
		if id, ok := pyjson.IsInt(c.Value("id")); ok {
			if k, _ := c.Value("kind").(string); k != "" {
				ids[k] = append(ids[k], id)
			}
		}
	}
	folderOf := map[string]int64{}
	scripts := map[int64]*flowaccess.SavedScript{}
	for kind, table := range map[string]string{"flow": "flows", "refresh": "datasources", "sql": "sql_scripts", "script": "saved_scripts"} {
		if len(ids[kind]) == 0 {
			continue
		}
		extra := "0"
		if kind == "script" {
			extra = "environment_id"
		}
		rows, err := q.d.DB.Query(q.ctx, `SELECT id, project_id, `+extra+`, name FROM `+table+` WHERE id = ANY($1)`, ids[kind])
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id, project, env int64
			var name string
			if err := rows.Scan(&id, &project, &env, &name); err != nil {
				rows.Close()
				return nil, err
			}
			if kind == "script" {
				scripts[id] = &flowaccess.SavedScript{ID: id, Name: name, ProjectID: project, EnvironmentID: env}
			} else {
				folderOf[fmt.Sprintf("%s:%d", kind, id)] = project
			}
		}
		rows.Close()
	}
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		return nil, err
	}
	hide := func(o pyjson.Object) pyjson.Object { return o.Set("error", nil).Set("run_id", nil) }
	for gi, grid := range grids {
		for ri, row := range grid {
			cells, ok := row.([]any)
			if !ok {
				continue
			}
			for ci, c := range cells {
				o, ok := c.(pyjson.Object)
				if !ok {
					continue
				}
				kind, _ := o.Value("kind").(string)
				if kind == "container" || kind == "pushmail" {
					continue
				}
				id, isInt := pyjson.IsInt(o.Value("id"))
				if kind == "script" {
					s := scripts[id]
					see := false
					if s != nil && isInt {
						if see, err = q.acc.CanSeeScript(q.ctx, s); err != nil {
							return nil, err
						}
					}
					if !see {
						cells[ci] = hide(o)
					}
					continue
				}
				folder, found := folderOf[fmt.Sprintf("%s:%d", kind, id)]
				if !found || !isInt || !readable[folder] {
					cells[ci] = hide(o)
				}
			}
			grid[ri] = cells
		}
		grids[gi] = grid
	}
	// FlowGroupRunOut.cells is list[list[Optional[dict]]]: a grid holding anything else is a
	// validation error, Python's 500
	for _, grid := range grids {
		for _, row := range grid {
			cells, ok := row.([]any)
			if !ok {
				return nil, errors.New("a group run's row is not a list")
			}
			for _, c := range cells {
				if _, isObj := c.(pyjson.Object); c != nil && !isObj {
					return nil, errors.New("a group run's cell is not a dict")
				}
			}
		}
	}
	out := make([]FlowGroupRunOut, len(runs))
	for i, run := range runs {
		out[i] = FlowGroupRunOut{ID: run.ID, Status: run.Status, TriggerType: run.TriggerType, LaunchedBy: run.LaunchedBy,
			Error: run.Error, StartedAt: iso(run.StartedAt), FinishedAt: isoPtr(run.FinishedAt), Cells: pyValue{grids[i]}}
	}
	return out, nil
}

// listFlowGroupRuns is GET /flow-groups/{group_id}/runs: the latest 50, newest first.
func (d *Deps) listFlowGroupRuns(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	g := q.group(ids[0])
	if g == nil || !q.ensure(g.ProjectID, perm.View) {
		return
	}
	rows, err := d.DB.Query(q.ctx, `SELECT `+runColumns+` FROM runs r WHERE r.flow_group_id = $1 ORDER BY r.id DESC LIMIT 50`, g.ID)
	if err != nil {
		q.fail(err)
		return
	}
	var runs []*runRow
	for rows.Next() {
		var x runRow
		if err := rows.Scan(x.dest()...); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		runs = append(runs, &x)
	}
	rows.Close()
	out, err := q.groupRunsOut(runs)
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, out)
}

// getFlowGroupRun is GET /flow-groups/{group_id}/runs/{run_id}.
func (d *Deps) getFlowGroupRun(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "group_id", "run_id")
	if !ok {
		return
	}
	q := d.req(w, r, p)
	g := q.group(ids[0])
	if g == nil || !q.ensure(g.ProjectID, perm.View) {
		return
	}
	run, err := q.readRun(ids[1])
	if err != nil {
		q.fail(err)
		return
	}
	if run == nil || run.FlowGroupID == nil || *run.FlowGroupID != g.ID {
		q.refuse(http.StatusNotFound, "Run not found")
		return
	}
	out, err := q.groupRunsOut([]*runRow{run})
	if err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, out[0])
}

var _ = sort.Strings

// asInput is a validated model field as input again: a nested model's dump, a default as it is.
func asInput(v any) any {
	if mv, ok := v.(*pyd.ModelValue); ok {
		return mv.Dump()
	}
	return v
}
