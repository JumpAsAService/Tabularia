package bayesmodels

import (
	"context"
	"log/slog"
	"time"
)

// Compiler is what stan-compile gives the store. Enqueue is called after a version's transaction has
// committed; it never blocks the save. State answers for one program, whatever the version that holds it.
type Compiler interface {
	Enqueue(ctx context.Context, r CompileRequest) error
	State(ctx context.Context, programSHA256 string) (CompileState, error)
}

// CompileRequest asks for one version's program. stan-compile reads the program from
// bayes_model_versions by its sha256 (any version holding it), never from the request.
type CompileRequest struct {
	ModelID, Version int64
	ProgramSHA256    string
	By               int64 // the person who saved: the compile Job's authority and the audit's actor
	// Again is «Compile now» (set only by the route): it queues again a program the compiler refused
	// (stan-compile's «The queue»); a program that passed a limit still waits for the limit to be raised.
	Again bool
}

// The states of a program's compile.
const (
	CompileNone    = "none"
	CompileQueued  = "queued"
	CompileRunning = "running"
	CompileReady   = "ready"
	CompileFailed  = "failed"
	// CompileTimeout: the compile passed its deadline or its memory limit; it is not compiled again
	// until that limit is raised (the owner's rule, stan-compile).
	CompileTimeout = "timeout"
)

// CompileState is where a program's compile is.
type CompileState struct {
	State  string    // CompileNone, CompileQueued, CompileRunning, CompileReady, CompileFailed, CompileTimeout
	Detail string    // a sentence for the page (stan-compile's words)
	At     time.Time // when the state was reached
}

// NoCompiler is the Compiler until stan-compile is built: it enqueues nothing and every program
// is CompileNone (the page says «compiled the first time it runs»).
type NoCompiler struct{}

// Enqueue does nothing.
func (NoCompiler) Enqueue(context.Context, CompileRequest) error { return nil }

// State is always CompileNone.
func (NoCompiler) State(context.Context, string) (CompileState, error) {
	return CompileState{State: CompileNone}, nil
}

// afterCommit asks for the compile of a version just committed, whatever its program's state: Enqueue does nothing
// for a program that is ready or on its way, or that passed a limit not raised since, but it always takes the person's
// older queued version of the model out of the queue (the owner's correction of 2026-11-24 from C1, stan-compile). An
// error is logged and never fails the save: the run compiles when nothing else did. It says whether Enqueue answered.
func afterCommit(ctx context.Context, c Compiler, log *slog.Logger, r CompileRequest) bool {
	if c == nil {
		return false
	}
	if err := c.Enqueue(ctx, r); err != nil {
		logError(log, "bayes models: asking for the compile", err, r)
		return false
	}
	return true
}

func logError(log *slog.Logger, msg string, err error, r CompileRequest) {
	if log == nil {
		return
	}
	log.Error(msg, "error", err.Error(), "model_id", r.ModelID, "version", r.Version, "program_sha256", r.ProgramSHA256)
}

// BatchCompiler is a Compiler that answers for many programs in one read: a list asks it once
// instead of once per model.
type BatchCompiler interface {
	States(ctx context.Context, programSHA256s []string) (map[string]CompileState, error)
}

// States is the compile state of each distinct program, with a BatchCompiler in one read and
// otherwise one State per distinct program. A program with no entry is CompileNone.
func States(ctx context.Context, c Compiler, programs []string) (map[string]CompileState, error) {
	seen := map[string]bool{}
	var distinct []string
	for _, p := range programs {
		if p != "" && !seen[p] {
			seen[p] = true
			distinct = append(distinct, p)
		}
	}
	if len(distinct) == 0 {
		return map[string]CompileState{}, nil
	}
	if b, ok := c.(BatchCompiler); ok {
		return b.States(ctx, distinct)
	}
	out := make(map[string]CompileState, len(distinct))
	for _, p := range distinct {
		st, err := c.State(ctx, p)
		if err != nil {
			return nil, err
		}
		out[p] = st
	}
	return out, nil
}

// States is always empty: every program is CompileNone.
func (NoCompiler) States(context.Context, []string) (map[string]CompileState, error) {
	return map[string]CompileState{}, nil
}
