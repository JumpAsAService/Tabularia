package bayescompile

import (
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

// The states a row of bayes_binaries holds; CompileNone is no row (never asked, evicted, a new image, overtaken).
const (
	StateNone    = bayesmodels.CompileNone
	StateQueued  = bayesmodels.CompileQueued
	StateRunning = bayesmodels.CompileRunning
	StateReady   = bayesmodels.CompileReady
	StateFailed  = bayesmodels.CompileFailed
	StateTimeout = bayesmodels.CompileTimeout
)

// The reasons of a row that is failed or timeout (the column reason; the page translates by it).
const (
	ReasonCompileFailed = "compile_failed" // stanc or the C++ compiler refused the program (exit 3)
	ReasonProgramHash   = "program_hash"   // the program in the ConfigMap was not the stored one (exit 3)
	ReasonCrossCheck    = "crosscheck"     // stanc's --info disagreed with the manifests (bayescodegen.CrossCheck)
	ReasonJob           = "job"            // exit 0 without a valid termination message, any other exit, the keep refused
	ReasonTooLarge      = "too_large"      // the binary is larger than the whole cache
	ReasonCluster       = "cluster"        // the cluster failed it MaxAttempts times
	ReasonDeadline      = "deadline"       // timeout: the Job passed its deadline
	ReasonMemory        = "memory"         // timeout: the container was killed for memory (the owner, 2026-11-24)
)

// LoopLock is the transaction's advisory lock of the compile loop's round (gateway-go/internal/api,
// jobs_bayes_compile.go): one round at a time across the gateways. «Empty the cache» takes the same lock, so that the
// objects it deletes are never counted as unheld while a round keeps a binary of the same bytes.
const LoopLock = 7_402_160

// MaxAttempts is how many times a compile runs when the cluster fails it (a Pod evicted, a node lost, an image not
// pulled, the cluster busy): then failed with the cluster's reason.
const MaxAttempts = 3

// ErrTransition is a change of state the states' table does not have: the row is left as it was.
var ErrTransition = errors.New("bayes compile: not a transition of the states")

// Row is a row of bayes_binaries as the rules read it; a NULL column is its zero value.
type Row struct {
	ID              int64
	CacheKey        string
	ProgramSHA256   string
	CmdStan         string
	ImageDigest     string
	Arch            string
	GroupVersion    int64
	State           string
	Reason          string
	Detail          string // the sentence, English
	RequestedBy     int64  // whose turn it counts in
	ModelID         int64  // the request that queued it
	Version         int64
	Attempt         int // 0 for the first run
	JobName         string
	DeadlineSeconds int64 // the limits it ran under, recorded at its start
	MemoryBytes     int64
	BinarySHA256    string // from the termination message only
	BinaryBytes     int64
	InfoSHA256      string
	Warnings        string // stanc's lines, JSON
	Seconds         float64
	QueuedAt        time.Time
	StartedAt       time.Time
	FinishedAt      time.Time
	LastUsedAt      time.Time
}

// transitions is the states' table: none → queued (Enqueue); queued → running (the loop starts its Job); running →
// ready | failed | timeout; running → queued (the cluster failed it, attempts left); ready → none (evicted, emptied, the
// stored object altered); failed → queued («Compile now», or a run or a save after the cluster's failure); timeout →
// queued (only under a raised limit: MayRetry); queued → none (overtaken, or its image is no longer the group's).
// Nothing else: a failed or timeout row is deleted only by the cleaning of an image no longer current (Cleanable),
// never moved to none; a running row is never dropped; nothing becomes ready, failed or timeout but a running row.
var transitions = map[[2]string]bool{
	{StateNone, StateQueued}:     true,
	{StateQueued, StateRunning}:  true,
	{StateRunning, StateReady}:   true,
	{StateRunning, StateFailed}:  true,
	{StateRunning, StateTimeout}: true,
	{StateRunning, StateQueued}:  true,
	{StateReady, StateNone}:      true,
	{StateFailed, StateQueued}:   true,
	{StateTimeout, StateQueued}:  true,
	{StateQueued, StateNone}:     true,
}

// Allowed says whether the states' table has the change from → to.
func Allowed(from, to string) bool { return transitions[[2]string{from, to}] }

func move(r *Row, to string) error {
	from := r.State
	if from == "" {
		from = StateNone
	}
	if !Allowed(from, to) {
		return fmt.Errorf("%w: %s → %s (row %d)", ErrTransition, from, to, r.ID)
	}
	r.State = to
	return nil
}

// MayRetry says whether a program that passed a limit may be compiled again: only when the limit it passed is larger
// now (the owner's rule, 2026-11-24). A deadline passed at 300 s is not tried again at 300 s, nor at 200 s; a compile
// killed for memory at 3 GiB is not tried again at 3 GiB. A binary larger than the whole cache (failed, too_large) is
// the same rule on the cache's limit: compiled again once the limit holds its recorded size (the owner, correction of
// 2026-11-24 from C1). Any other row answers true (the rule is about the limits alone).
func MayRetry(r Row, now Limits) bool {
	switch {
	case r.State == StateFailed && r.Reason == ReasonTooLarge:
		return r.BinaryBytes > 0 && r.BinaryBytes <= now.CacheMaxBytes
	case r.State != StateTimeout:
		return true
	}
	switch r.Reason {
	case ReasonDeadline:
		return now.DeadlineSeconds > r.DeadlineSeconds
	case ReasonMemory:
		return now.MemoryBytes > r.MemoryBytes
	}
	return false // a timeout with no reason of the rule's: never by itself
}

// JobName is the compile Job's name, fixed by the row and its attempt, so a gateway that dies halfway is replaced by
// another that finds the same Job (at most 14 + 19 + 1 + 19 characters: under Kubernetes' 63).
func JobName(id int64, attempt int) string {
	return "bayes-compile-" + strconv.FormatInt(id, 10) + "-" + strconv.Itoa(attempt)
}

// StepPrefix is the compile's prefix in the scripts bucket: the binary at StepPrefix+"model" (TABULARIA_BINARY_OUT),
// artifacts/compile.json under it; its files leave the bucket by exact key whatever the outcome.
func StepPrefix(id int64, attempt int) string {
	return "bayes-compile/" + strconv.FormatInt(id, 10) + "-" + strconv.Itoa(attempt) + "/"
}

// Start makes a queued row running under the limits of this compile: its Job's name, the time, and the deadline and
// memory recorded (what a timeout is compared with afterwards, whatever the limits become meanwhile).
func Start(r Row, now Limits, at time.Time) (Row, error) {
	if err := move(&r, StateRunning); err != nil {
		return r, err
	}
	r.JobName = JobName(r.ID, r.Attempt)
	r.StartedAt = at
	r.DeadlineSeconds = now.DeadlineSeconds
	r.MemoryBytes = now.MemoryBytes
	r.Reason, r.Detail = "", ""
	return r, nil
}

// Kept makes a running row ready once its binary is in Tabularia's storage under the termination message's sha256 and
// the cache made room for it; its use starts now (least recently used).
func Kept(r Row, m Message, binaryBytes int64, seconds float64, warnings string, at time.Time) (Row, error) {
	if m.ProgramSHA256 != r.ProgramSHA256 {
		return r, fmt.Errorf("%w: the message's program is not the row's", ErrMessage)
	}
	if binaryBytes <= 0 {
		return r, fmt.Errorf("bayes compile: a binary of %d bytes", binaryBytes)
	}
	if err := move(&r, StateReady); err != nil {
		return r, err
	}
	r.Reason, r.Detail = "", ""
	r.BinarySHA256, r.BinaryBytes, r.InfoSHA256 = m.BinarySHA256, binaryBytes, m.InfoSHA256
	r.Warnings, r.Seconds = warnings, seconds
	r.FinishedAt, r.LastUsedAt = at, at
	return r, nil
}

// Failed makes a running row failed for a reason of Tabularia's (compile_failed, program_hash, crosscheck, job): not
// retried by itself, since the same program on the same image fails the same way; «Compile now» may queue it again.
func Failed(r Row, reason string, at time.Time) (Row, error) {
	switch reason {
	case ReasonCompileFailed, ReasonProgramHash, ReasonCrossCheck, ReasonJob:
	default:
		return r, fmt.Errorf("bayes compile: %q is not a reason of the compiler", reason)
	}
	if err := move(&r, StateFailed); err != nil {
		return r, err
	}
	r.Reason, r.Detail, r.FinishedAt = reason, failedSentence, at
	return r, nil
}

// TooLarge makes a running row failed because its binary is larger than the whole cache: nothing is kept. Its size is
// recorded: a request after the administrator raised the limit enough queues it again by itself (MayRetry).
func TooLarge(r Row, binaryBytes, cacheMaxBytes int64, at time.Time) (Row, error) {
	if binaryBytes <= 0 {
		return r, fmt.Errorf("bayes compile: a binary of %d bytes", binaryBytes)
	}
	if err := move(&r, StateFailed); err != nil {
		return r, err
	}
	r.Reason, r.FinishedAt, r.BinaryBytes = ReasonTooLarge, at, binaryBytes
	r.Detail = fmt.Sprintf("The cache's limit (%s) is smaller than this model's binary (%s): an administrator can raise it.",
		Bytes(cacheMaxBytes), Bytes(binaryBytes))
	return r, nil
}

// TimedOut makes a running row timeout: the Job passed its deadline (ReasonDeadline) or was killed for memory
// (ReasonMemory). The limits it ran under stay recorded, and MayRetry keeps it so until the limit is raised.
func TimedOut(r Row, reason string, at time.Time) (Row, error) {
	if reason != ReasonDeadline && reason != ReasonMemory {
		return r, fmt.Errorf("bayes compile: %q is not a limit", reason)
	}
	if r.DeadlineSeconds <= 0 || r.MemoryBytes <= 0 {
		return r, fmt.Errorf("bayes compile: row %d has no limits recorded", r.ID)
	}
	if err := move(&r, StateTimeout); err != nil {
		return r, err
	}
	r.Reason, r.FinishedAt = reason, at
	r.Detail = timeoutSentence(r)
	return r, nil
}

// ClusterFailed is the cluster's failure of a running row (why: the cluster's words, Evicted, DisruptionTarget, an
// image not pulled…): not a failure of the program, so it goes back to queued with its next attempt, keeping its place
// (queued_at), and becomes failed with the cluster's reason after MaxAttempts.
func ClusterFailed(r Row, why string, at time.Time) (Row, error) {
	if r.Attempt+1 >= MaxAttempts {
		if err := move(&r, StateFailed); err != nil {
			return r, err
		}
		r.Reason, r.FinishedAt = ReasonCluster, at
		r.Detail = fmt.Sprintf("The cluster could not run the compile (%d attempts): %s.", MaxAttempts, clip(why, 200))
		return r, nil
	}
	if err := move(&r, StateQueued); err != nil {
		return r, err
	}
	r.Attempt++
	r.JobName, r.StartedAt = "", time.Time{}
	r.Reason, r.Detail = "", ""
	return r, nil
}

// Used records a use of a ready row's binary (it was placed for a run): the cache's least recently used order.
func Used(r Row, at time.Time) (Row, error) {
	if r.State != StateReady {
		return r, fmt.Errorf("%w: a use of a %s row", ErrTransition, r.State)
	}
	r.LastUsedAt = at
	return r, nil
}

// Refused says whether a run that needs this program fails at once instead of compiling it first (the run's
// fallback): a timeout under the same or lower limits, a binary still larger than the cache's limit, or a failure of
// the compiler. A row the cluster failed, none, a timeout under a raised limit and a binary the raised cache now holds
// are compiled first; queued and running are waited for.
func Refused(r *Row, now Limits) bool {
	if r == nil {
		return false
	}
	switch r.State {
	case StateTimeout:
		return !MayRetry(*r, now)
	case StateFailed:
		switch r.Reason {
		case ReasonCluster:
			return false
		case ReasonTooLarge:
			return !MayRetry(*r, now)
		}
		return true
	}
	return false
}

// Cleanable says whether the loop's daily cleaning deletes a row: a failed or timeout row (it holds no binary) of an
// image that is no longer the group's current one, whose program no version holds any more.
func Cleanable(r Row, currentDigest string, programHeld bool) bool {
	return (r.State == StateFailed || r.State == StateTimeout) && r.ImageDigest != currentDigest && !programHeld
}

// Stale says whether a queued row is of an image that is no longer the group's current one: the loop makes it none
// instead of starting it (a rebuild meanwhile).
func Stale(r Row, currentDigest string) bool {
	return r.State == StateQueued && r.ImageDigest != currentDigest
}

func clip(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}
