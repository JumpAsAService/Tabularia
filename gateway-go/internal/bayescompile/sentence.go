package bayescompile

import (
	"fmt"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

// The sentences of the states (English, in the detail column and in CompileState.Detail; the page translates by the
// state and the reason).
const (
	noneSentence   = "Compiled the first time it runs."
	failedSentence = "The generated program did not compile: this is a fault of Tabularia, not of the model. " +
		"The administrator's log says where."
	waitPersonSentence = "Waiting to compile: another compile of yours is running."
)

func timeoutSentence(r Row) string {
	if r.Reason == ReasonMemory {
		return fmt.Sprintf("The program did not compile within the memory limit of %s (%s): it is not compiled again "+
			"with this limit. A simpler model, or a larger limit set by an administrator, compiles anew.",
			Bytes(r.MemoryBytes), r.FinishedAt.Format("2 Jan"))
	}
	return fmt.Sprintf("The program did not compile within the limit of %d s (%s): it is not compiled again with this "+
		"limit. A simpler model, or a longer limit set by an administrator, compiles anew.",
		r.DeadlineSeconds, r.FinishedAt.Format("2 Jan"))
}

// StateOf is a program's compile state for the page from its row of the group's current image (nil: none).
// personRunning says whether the row's person has another compile running (the queued sentence names the wait), slots
// is «Compiles at once».
func StateOf(r *Row, personRunning bool, slots int) bayesmodels.CompileState {
	if r == nil || r.State == "" || r.State == StateNone {
		return bayesmodels.CompileState{State: StateNone, Detail: noneSentence}
	}
	s := bayesmodels.CompileState{State: r.State, Detail: r.Detail}
	switch r.State {
	case StateQueued:
		s.At = r.QueuedAt
		s.Detail = "Waiting for a free place to compile (" + strconv.Itoa(slots) + " at once)."
		if personRunning {
			s.Detail = waitPersonSentence
		}
	case StateRunning:
		s.At = r.StartedAt
		s.Detail = "Compiling since " + r.StartedAt.Format("15:04") + " (usually 10–30 s)."
	case StateReady:
		s.At = r.FinishedAt
		s.Detail = "Compiled on " + r.FinishedAt.Format("2 Jan") + " at " + r.FinishedAt.Format("15:04") +
			" in " + strconv.FormatFloat(r.Seconds, 'f', 0, 64) + " s."
	case StateFailed, StateTimeout:
		s.At = r.FinishedAt
		if s.Detail == "" {
			s.Detail = failedSentence
			if r.State == StateTimeout {
				s.Detail = timeoutSentence(*r)
			}
		}
	}
	return s
}
