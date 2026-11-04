package api

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
)

// routes/runs.py _launch_flow_run as the orchestrator launches an Output node of a flow, over
// internal/runlaunch (the routes' launch too).

// jobFlow is a flows row, the columns the orchestration reads.
type jobFlow struct {
	ID, ProjectID    int64
	Name, Definition string
	Engine           string
	ProductionEngine *string
}

func loadJobFlow(ctx context.Context, q perm.Querier, id int64) (*jobFlow, error) {
	var f jobFlow
	err := q.QueryRow(ctx, `SELECT id, project_id, name, definition, engine, production_engine FROM flows WHERE id = $1`, id).
		Scan(&f.ID, &f.ProjectID, &f.Name, &f.Definition, &f.Engine, &f.ProductionEngine)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &f, nil
}

// resolveRunEngine is resolve_run_engine.
func resolveRunEngine(f *jobFlow, mode string) string {
	return runlaunch.ResolveRunEngine(&runlaunch.Flow{Engine: f.Engine, ProductionEngine: f.ProductionEngine}, mode)
}

// refusalError is an HTTPException raised inside the orchestration: str(e) is
// «status: detail» (Starlette's HTTPException.__str__).
type refusalError struct {
	Status int
	Detail string
}

func (e *refusalError) Error() string { return fmt.Sprintf("%d: %s", e.Status, e.Detail) }

func refusalOf(r *auth.Refusal) error { return &refusalError{Status: r.Status, Detail: r.Detail} }

// errorDetail is `getattr(e, "detail", None) or str(e)`.
func errorDetail(err error) string {
	var r *refusalError
	if errors.As(err, &r) && r.Detail != "" {
		return r.Detail
	}
	var h *httpError
	if errors.As(err, &h) && h.Detail != "" {
		return h.Detail
	}
	return err.Error()
}

// pyStrOfError is str(e): an HTTPException as «status: detail».
func pyStrOfError(err error) string {
	var h *httpError
	if errors.As(err, &h) {
		return fmt.Sprintf("%d: %s", h.Status, h.Detail)
	}
	return err.Error()
}

// launchFlowRun is _launch_flow_run for an orchestration's Output: the run's id.
func (d *Deps) launchFlowRun(ctx context.Context, user *auth.User, flow *jobFlow, req pyjson.Object, trigger string, parent *int64,
	engineMode string) (int64, error) {
	return d.launchFlowRunTo(ctx, user, flow, req, trigger, parent, engineMode, nil, nil)
}

// launchFlowRunTo is _launch_flow_run (runlaunch.LaunchFlowRun) with `script_destination`:
// (the engine's payload, its summary) of a Script or Container node's input. A refusal comes
// back as the HTTPException it is in Python (refusalError).
func (d *Deps) launchFlowRunTo(ctx context.Context, user *auth.User, flow *jobFlow, req pyjson.Object, trigger string, parent *int64,
	engineMode string, scriptDest pyjson.Object, scriptSummary *string) (int64, error) {
	v, errs := gatewaymodels.RunCreate.Type().Validate(pydconv.ToPyd(req))
	if len(errs) > 0 {
		// pydantic's ValidationError: its text is not reproduced
		return 0, fmt.Errorf("%d validation error for RunCreate", len(errs))
	}
	o := runlaunch.FlowRunOptions{TriggerType: trigger, ParentRunID: parent, EngineMode: engineMode}
	if scriptDest != nil {
		o.ScriptDestination = &runlaunch.ScriptDestination{Payload: scriptDest, Summary: deref(scriptSummary)}
	}
	f := &runlaunch.Flow{ID: flow.ID, ProjectID: flow.ProjectID, Name: flow.Name, Engine: flow.Engine, ProductionEngine: flow.ProductionEngine}
	id, ref, err := runlaunch.LaunchFlowRun(ctx, d.flowEnv(), user, f, v.(*pyd.ModelValue), o)
	if ref != nil {
		return 0, refusalOf(ref)
	}
	return id, err
}
