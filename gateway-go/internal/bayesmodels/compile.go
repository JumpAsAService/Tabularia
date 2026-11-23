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
}

// The states of a program's compile.
const (
	CompileNone    = "none"
	CompileQueued  = "queued"
	CompileRunning = "running"
	CompileReady   = "ready"
	CompileFailed  = "failed"
)

// CompileState is where a program's compile is.
type CompileState struct {
	State  string    // CompileNone, CompileQueued, CompileRunning, CompileReady, CompileFailed
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

// afterCommit asks for the compile of a version just committed, unless its program has a binary
// already or one is on its way (the cache answers: a layout-only save, a restore, a duplicate). An
// error is logged and never fails the save: the run compiles when nothing else did.
func afterCommit(ctx context.Context, c Compiler, log *slog.Logger, r CompileRequest) bool {
	if c == nil {
		return false
	}
	s, err := c.State(ctx, r.ProgramSHA256)
	if err != nil {
		logError(log, "bayes models: the compile state", err, r)
	} else {
		switch s.State {
		case CompileReady, CompileQueued, CompileRunning:
			return false
		}
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
