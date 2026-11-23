package api

// A Bayesian model node (`bayes_model`) as a step of a flow run or of a trial
// (docs/specs/bayes-flow-node.md). This file holds the place of the step; its run is lot F2:
// until then the step refuses, before any run row, input or Job exists.

import (
	"context"
	"fmt"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// bayesNodeType is the node's type; the runs it makes have the kind of the models' (`model`), so that
// the run page, the closing of orphans, the report and ModelRunDetail read them as they are.
const bayesNodeType = "bayes_model"

// bayesRunKind is the kind of the run a node of this type makes, as it is written in `runs.kind`.
func bayesRunKind(nodeType string) string {
	if nodeType == bayesNodeType {
		return "model"
	}
	return nodeType
}

// bayesBuiltSentence is what a Bayesian model node says until its step is built.
func bayesBuiltSentence(node pyjson.Object) string {
	return fmt.Sprintf("Bayesian model «%s»: not built yet, the node cannot run in this version of Tabularia", modelLabel(node))
}

// planBayes is «Before anything» for a Bayesian model node: the step, or why it cannot run
// (a *scriptFailed), before any row, input or Job exists. Lot F2 gives it its checks.
func (q *queue) planBayes(_ context.Context, _ *auth.User, node pyjson.Object) error {
	return &scriptFailed{bayesBuiltSentence(node)}
}

// runBayes runs a Bayesian model node: refused «not built» until lot F2.
func (q *queue) runBayes(ctx context.Context, r *scriptNodeRun) (*flowresolver.Source, error) {
	if err := q.planBayes(ctx, r.user, r.node); err != nil {
		return nil, err
	}
	return nil, &scriptFailed{bayesBuiltSentence(r.node)}
}
