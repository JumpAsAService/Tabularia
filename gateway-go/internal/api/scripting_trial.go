package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/scripting.py trial_node: the editor's «Run» of a Script or Container node of the saved
// flow (script_runs.run_trial): the chain before it on the sources' development sample, the
// node as saved, the nodes after it not run. It answers at once with the run to follow; the
// trial goes on in this process, beating while it runs.

const trialBeatSeconds = 20 // TRIAL_BEAT_SECONDS

func init() {
	register("scripting_trial", func(d *Deps) []server.Route {
		pattern := "/scripting/flows/{flow_id}/nodes/{node_id}/run"
		return []server.Route{{Method: http.MethodPost, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, d.trialNode)}}
	})
}

// trialNode is POST /scripting/flows/{flow_id}/nodes/{node_id}/run.
func (d *Deps) trialNode(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "flow_id")
	if !ok {
		return
	}
	nodeID := r.PathValue("node_id")
	q := d.req(w, r, p)
	flow, err := loadJobFlow(q.ctx, d.DB, ids[0])
	if err != nil {
		q.fail(err)
		return
	}
	if flow == nil {
		q.refuse(http.StatusNotFound, "Flow not found")
		return
	}
	if !q.ensure(flow.ProjectID, perm.Run) || q.refused(q.acc.EnsureCanOpen(q.ctx, flow.Definition)) {
		return
	}
	text := flow.Definition
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		q.fail(err)
		return
	}
	definition, isDict := v.(pyjson.Object)
	if !isDict {
		q.fail(errors.New("a flow's definition that is not a dict"))
		return
	}
	var node pyjson.Object
	for _, n := range objectList(definition.Value("nodes")) {
		if n.Value("id") == nodeID {
			node = n
			break
		}
	}
	kind, _ := node.Value("type").(string)
	if node == nil || (kind != "script" && kind != "container") {
		q.refuse(http.StatusNotFound, "The node is not in the saved flow: save the flow first")
		return
	}
	var label string
	var detail pyjson.Object
	if kind == "script" {
		resolved, saved, _, err := d.resolvedNode(q.ctx, node)
		if err != nil {
			q.fail(err)
			return
		}
		node = resolved
		if _, byRef := scriptnodes.SavedScriptID(node); byRef && saved == nil {
			q.refuse(http.StatusUnprocessableEntity, "The node runs a saved script that no longer exists")
			return
		}
		var env *scriptEnv
		if id, ok := nodeData(node).Value("environmentId").(int64); ok {
			if env, err = loadScriptEnv(q.ctx, d.DB, id); err != nil {
				q.fail(err)
				return
			}
		}
		if env == nil {
			q.refuse(http.StatusUnprocessableEntity, "Choose the node's environment, and save the flow")
			return
		}
		use, err := scriptnodes.CanUse(q.ctx, q.c, d.DB, p.User.ID, env.ID)
		if err != nil {
			q.fail(err)
			return
		}
		if !use {
			q.refuse(http.StatusForbidden, fmt.Sprintf("You cannot run scripts in the environment «%s»", env.Name))
			return
		}
		label, detail = scriptnodes.Label(node), pyjson.Object{{Key: "environment", Value: env.Name}}
	} else {
		if !scriptcfg.Load(config.FromOS()).Containers.Enabled {
			q.refuse(http.StatusConflict, "Containers are off on this installation")
			return
		}
		data := nodeData(node)
		if _, isInt := data.Value("registryId").(int64); !isInt || !pyjson.Truthy(data.Value("repository")) {
			q.refuse(http.StatusUnprocessableEntity, "Choose the node's registry and image, and save the flow")
			return
		}
		label, detail = flowaccess.ContainerLabel(node), pyjson.Object{{Key: "registry", Value: data.Value("registryId")},
			{Key: "image", Value: data.Value("repository")}, {Key: "tag", Value: pyjson.Or(data.Value("tag"), "latest")}}
	}
	// a trial of the node still going is offered again; one left behind by a process that
	// stopped is closed now
	live, err := idsOf(q.ctx, d.DB, `SELECT id FROM runs WHERE flow_id = $1 AND kind = $2 AND trigger_type = 'trial'
		AND status IN ('PENDING', 'STARTED')`, flow.ID, kind)
	if err != nil {
		q.fail(err)
		return
	}
	sc := scriptcfg.Load(config.FromOS())
	for _, id := range live {
		run, err := q.readRun(id)
		if err != nil || run == nil {
			if err != nil {
				q.fail(err)
				return
			}
			continue
		}
		info, ok := scriptJSON(run.Script)
		if !ok {
			q.fail(errors.New("a trial's script is not JSON"))
			return
		}
		if pyjson.Obj(info).Value("node_id") != nodeID {
			continue
		}
		if run, err = q.closeIfOrphaned(run, sc); err != nil {
			q.fail(err)
			return
		}
		if run.Status == "PENDING" || run.Status == "STARTED" {
			server.JSON(w, http.StatusAccepted, pyjson.Object{{Key: "run_id", Value: run.ID}})
			return
		}
	}
	budget, err := budgetSeconds(nodeData(node))
	if err != nil {
		q.fail(err)
		return
	}
	var runID int64
	if err := d.DB.QueryRow(q.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at) VALUES ($1, $2, '', 'PENDING', $3, 'trial',
		'', '', '', '', false, '[]', $4, $5) RETURNING id`, kind, flow.ID, p.User.ID, pyDumps(pyjson.Object{{Key: "node_id", Value: nodeID},
		{Key: "label", Value: label}, {Key: "trial", Value: true}, {Key: "budget_seconds", Value: budget}}), jobs.Now()).Scan(&runID); err != nil {
		q.fail(err)
		return
	}
	action := "scripting.trial"
	if kind == "container" {
		action = "container.trial"
	}
	q.audit(audit.Event{Action: action, TargetType: strp("flow"), TargetID: &flow.ID, TargetLabel: &flow.Name,
		Detail: append(pyjson.Object{{Key: "run_id", Value: runID}, {Key: "node", Value: nodeID}}, detail...)})
	user := p.User
	go d.runTrial(runID, &user, nodeID)
	server.JSON(w, http.StatusAccepted, pyjson.Object{{Key: "run_id", Value: runID}})
}

// runTrial is run_trial: the trial carried to its end, beating while it runs.
func (d *Deps) runTrial(runID int64, user *auth.User, nodeID string) {
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	// _beat_trial is a task of the event loop: it first runs when the trial first waits on
	// the network (a call to the registry, the engine, Kubernetes); a trial refused before
	// that never beats
	var once sync.Once
	beat := func() { once.Do(func() { go d.beatTrial(ctx, runID) }) }
	if err := d.trialRun(ctx, runID, user, nodeID, beat); err != nil {

		var why string
		if m := errorDetail(err); m != "" {
			why = m
		} else {
			why = pyErrorName(err)
		}
		if _, err2 := d.DB.Exec(context.Background(), `UPDATE runs SET status = 'FAILURE', finished_at = $2, error = $3 WHERE id = $1
			AND status NOT IN ('SUCCESS', 'FAILURE')`, runID, jobs.Now(), runes(why, 2000)); err2 != nil {
			d.Log.Error("trial: its outcome not written", "run", runID, "err", err2)
		}
		d.Log.Info("trial ended with an error", "run", runID, "err", err)
	}
}

// beatTrial is _beat_trial: the trial says it is alive while its process runs it.
func (d *Deps) beatTrial(ctx context.Context, runID int64) {
	for {
		if _, err := d.DB.Exec(ctx, `UPDATE runs SET heartbeat_at = $2 WHERE id = $1 AND status NOT IN ('SUCCESS', 'FAILURE')`,
			runID, jobs.Now()); err != nil && ctx.Err() == nil {
			d.Log.Debug("trial: beat not written", "run", runID, "err", err)
		}
		if !jobs.Sleep(ctx, trialBeatSeconds*time.Second) {
			return
		}
	}
}

// trialRun is _run_trial's body: the node run as a trial, the Script and Container nodes
// before it standing in with their latest output.
func (d *Deps) trialRun(ctx context.Context, runID int64, user *auth.User, nodeID string, firstIO func()) error {
	q := newQueue(d)
	req := d.jobRequest(ctx, user)
	run, err := req.readRun(runID)
	if err != nil || run == nil || run.FlowID == nil {
		return err
	}
	current, err := loadUser(ctx, d.DB, user.ID)
	if err != nil || current == nil {
		return err
	}
	flow, err := loadJobFlow(ctx, d.DB, *run.FlowID)
	if err != nil || flow == nil {
		return err
	}
	text := flow.Definition
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return &pyJSONError{err}
	}
	definition := pyjson.Obj(v)
	var node pyjson.Object
	for _, n := range objectList(definition.Value("nodes")) {
		if n.Value("id") == nodeID {
			node = n
			break
		}
	}
	if node == nil || node.Value("type") != run.Kind {
		return &scriptFailed{"the node is no longer in the saved flow"}
	}
	// the Script and Container nodes before this one: their latest output stands in for them
	var nodes []savedNode
	for _, n := range append(flowaccess.ScriptNodes(definition), flowaccess.ContainerNodes(definition)...) {
		if id := n.Value("id"); pyjson.Truthy(id) {
			nodes = append(nodes, savedNode{id: id})
		}
	}
	latest, err := latestOutputs(ctx, d.DB, flow.ID, nodes)
	if err != nil {
		return err
	}
	upstream := map[string]*[2]any{}
	for _, n := range nodes {
		if e := latest[jsonKey(n.id)]; e != nil && e.output != nil {
			upstream[nodeKey(n.id)] = &[2]any{plainValue(e.output.Value("bucket")), plainValue(e.output.Value("key"))}
		}
	}
	resolveDS := func(i any) *[2]any {
		n, ok := pyjson.IsInt(i)
		if !ok {
			return nil
		}
		var b, k string
		if d.DB.QueryRow(ctx, `SELECT bucket, key FROM datasources WHERE id = $1`, n).Scan(&b, &k) != nil || k == "" {
			return nil
		}
		return &[2]any{b, k}
	}
	r := &scriptNodeRun{q: q, user: current, flow: flow, definition: definition, node: node, trigger: "trial", mode: "development",
		outputs: upstream, resolveDS: resolveDS, trial: runID, firstIO: firstIO}
	if run.Kind == "container" {
		_, err = q.runContainer(ctx, r)
	} else {
		_, err = q.runScript(ctx, r)
	}
	return err
}

// plainValue is a column read as a pointer, as its value (nil for NULL).
func plainValue(v any) any {
	if p, ok := v.(*string); ok {
		if p == nil {
			return nil
		}
		return *p
	}
	return v
}
