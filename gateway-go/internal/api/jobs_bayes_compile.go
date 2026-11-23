package api

// The compile loop of the group bayes (docs/specs/bayes-stan-compile.md, «The queue», «The compile Job», «Finishing a
// compile», «The binary's way», «The cache»): on Kubernetes with the statistical models on, one round every
// SCRIPTS__POLL_SECONDS under the transaction's advisory lock bayescompile.LoopLock, asked once, so that one gateway at
// a time moves the rows of bayes_binaries:
//
//  1. every running row follows its Job (its name is fixed by the row's id and attempt, so a gateway that dies halfway
//     is replaced by another that finds the same Job): still running, or refused by the cluster (no Pod, no image, no
//     room), or ended. An end is read from the Job, its Pod and the container's termination message — the only source
//     of the binary's sha256 the gateway trusts — and becomes ready (the keep), failed, timeout, or queued again when
//     the cluster failed it (at most bayescompile.MaxAttempts);
//  2. the queued rows of an image that is no longer the group's current one leave the queue;
//  3. the cache is brought under its limit (a limit lowered since the last round);
//  4. the queued rows start, as many as «Compiles at once» leaves room for, never two of one person;
//  5. once a day, the failed and timeout rows of an older image whose program no version holds go.
//
// The Jobs that ended and their step's files in the scripts bucket are deleted after the round's commit: a round that
// fails before it finds them again.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

const (
	bcContainer = "model"
	// EnvBinaryOut is where the compile mode writes the binary: an s3:// address in the scripts bucket (the contract,
	// «The program and the binary»).
	bcEnvBinaryOut = "TABULARIA_BINARY_OUT"
	// the program's file in the compile Job's ConfigMap, beside task.json
	bcProgramFile = "program.stan"
	// binary_max_bytes: 20 times the largest binary measured (3.1 MB); the engine refuses above it too
	bcBinaryMaxBytes = 64 << 20
	// compile.json is stanc's --info and a few lines: anything larger is not the runner's
	bcCompileJSONMax = 4 << 20
	bcMaxWarnings    = 50
	bcLogLines       = 40 // the end of the log in the gateway's log when the compiler refused the program
	bcCleanEvery     = 24 * time.Hour
	bcLabelCompile   = "tabularia.io/bayes-compile"
	bcTerminationLog = "/dev/termination-log"
	// a Pod the cluster has no room for is given back to the queue after this long (at most a quarter of the
	// deadline), not left to pass the deadline it would then be blamed for
	bcPendingSeconds = 60
)

func init() {
	RegisterJob("bayes_compile_loop", func(d *Deps) jobs.Job {
		c := &bayesCompiles{d: d, s: scriptcfg.Load(config.FromOS())}
		return jobs.Job{Order: 14, Loop: c.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one round: how many rows changed state
			"bayes_compiles": func(ctx context.Context) (any, error) {
				if c.off() != "" {
					return nil, nil
				}
				return c.oneRound(ctx)
			},
		}}
	})
}

// bayesCompiles is the loop.
type bayesCompiles struct {
	d *Deps
	s *scriptcfg.Settings

	mu      sync.Mutex
	cleaned time.Time // the last daily cleaning of this process
}

// off is why the loop does nothing on this installation, "" when it runs.
func (c *bayesCompiles) off() string {
	if _, ok := modelgroups.Get(bayescontract.Group); !ok {
		return "this Tabularia ships no group bayes"
	}
	return modelsOff(c.s)
}

func (c *bayesCompiles) loop(ctx context.Context) {
	if c.off() != "" {
		return
	}
	for ctx.Err() == nil {
		every := c.d.Panel.ScriptsPollSeconds()
		if _, err := c.oneRound(ctx); err != nil && !errors.Is(err, context.Canceled) {
			c.d.Log.Warn(fmt.Sprintf("bayes compile: the compiles were not followed (%v), again in %ss", err, pyjson.FloatRepr(every)))
		}
		if !jobs.Sleep(ctx, jobs.Seconds(every)) {
			break
		}
	}
}

// bcWait is a round that cannot finish a row now (the API server or the engine did not answer, the cache has no room
// while binaries are being placed): the row stays as it was and the next round asks again.
type bcWait struct{ why string }

func (w *bcWait) Error() string { return w.why }

// bcKubeWait is a Kubernetes error as a wait (nil stays nil; any other error is returned as it is).
func bcKubeWait(err error) error {
	var ke *kube.Error
	if errors.As(err, &ke) {
		return &bcWait{"the Kubernetes API: " + ke.Message}
	}
	return err
}

// bcRound is one round: its transaction, the time, the limits in force, the group's image (nil while the group cannot
// run a Job: disabled, being built), the scripts bucket, and what goes after the commit.
type bcRound struct {
	c       *bayesCompiles
	tx      pgx.Tx
	now     time.Time
	group   modelgroups.Group
	limits  bayescompile.Limits
	img     *ModelGroupImage
	digest  string // the current image's digest, "" when img is nil
	storage *scriptStorage
	jobs    []string // the Jobs whose end was read
	keys    []string // their step's files in the scripts bucket
}

// oneRound is a round; how many rows changed state.
func (c *bayesCompiles) oneRound(ctx context.Context) (int64, error) {
	tx, err := c.d.DB.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(bayescompile.LoopLock)).Scan(&got); err != nil {
		return 0, err
	}
	if !got {
		return 0, nil
	}
	g, ok := modelgroups.Get(bayescontract.Group)
	if !ok {
		return 0, nil
	}
	r := &bcRound{c: c, tx: tx, now: jobs.NaiveNow(), group: g}
	settings, err := bcSettings(ctx, tx)
	if err != nil {
		return 0, err
	}
	if r.limits, err = bayescompile.Resolve(g.Recipe.Compile, g.Recipe.Cache, settings); err != nil {
		return 0, err
	}
	r.img, err = ReadyModelGroupImage(ctx, tx, bayescontract.Group)
	var off *ModelGroupUnavailable
	if errors.As(err, &off) {
		r.img = nil
	} else if err != nil {
		return 0, err
	}
	if r.img != nil {
		r.digest = r.img.Digest
	}
	if r.storage, err = loadScriptStorage(ctx, tx); err != nil {
		return 0, err
	}
	rows, err := bcSelect(ctx, tx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries WHERE state IN ('queued', 'running')
		ORDER BY id FOR UPDATE`)
	if err != nil {
		return 0, err
	}
	var changed int64
	var live []bayescompile.Row // the queued and running rows after the follow
	for _, row := range rows {
		if row.State == bayescompile.StateRunning {
			after, err := r.follow(ctx, row)
			var wait *bcWait
			switch {
			case errors.As(err, &wait):
				c.d.Log.Warn(fmt.Sprintf("bayes compile: %s waits (%s)", row.JobName, wait.why))
				after = row
			case err != nil:
				return 0, err
			}
			if after.State != row.State {
				changed++
				r.ended(row)
			}
			row = after
		}
		switch {
		case row.State == bayescompile.StateQueued && r.img != nil && bayescompile.Stale(row, r.digest):
			// a rebuild meanwhile: its key is no longer the current image's (state none)
			if err := bayescompile.Delete(ctx, tx, row.ID); err != nil {
				return 0, err
			}
			changed++
		case row.State == bayescompile.StateQueued || row.State == bayescompile.StateRunning:
			live = append(live, row)
		}
	}
	// a limit lowered since the last round
	if _, err := r.makeRoom(ctx, 0, ""); err != nil && !errors.Is(err, bayescompile.ErrNoRoom) {
		var wait *bcWait
		if !errors.As(err, &wait) {
			return 0, err
		}
		c.d.Log.Warn("bayes compile: the cache waits for the next round: " + wait.why)
	}
	started, err := r.startQueued(ctx, live)
	if err != nil {
		return 0, err
	}
	changed += started
	cleaned, err := r.clean(ctx)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil { // releases the lock
		return 0, err
	}
	if cleaned {
		c.mu.Lock()
		c.cleaned = time.Now()
		c.mu.Unlock()
	}
	r.afterCommit(ctx)
	return changed, nil
}

// ended records a Job whose end was read: it goes after the commit, with its step's files.
func (r *bcRound) ended(row bayescompile.Row) {
	if row.JobName != "" { // never a delete of the namespace's Jobs
		r.jobs = append(r.jobs, row.JobName)
	}
	r.keys = append(r.keys, bcStepKeys(bayescompile.StepPrefix(row.ID, row.Attempt))...)
}

// afterCommit deletes the Jobs that ended and their step's files, as best it can (their TTL takes the Jobs anyway).
func (r *bcRound) afterCommit(ctx context.Context) {
	k, ns := kubeAPI(), r.c.s.Scripts.Namespace
	for _, name := range r.jobs {
		if err := kube.DeleteJob(ctx, k, ns, name); err != nil {
			r.c.d.Log.Warn("bayes compile: Job not deleted; its TTL will", "job", name, "err", err.Error())
		}
	}
	if len(r.keys) > 0 && r.storage.ready() {
		r.c.d.deleteObjects(ctx, r.storage, r.keys)
	}
}

// bcStepKeys are the files a compile Job may leave under its prefix, deleted by exact key whatever the outcome.
func bcStepKeys(prefix string) []string {
	return []string{prefix + "model", prefix + "artifacts/compile.json", prefix + "artifacts/error.json",
		prefix + "artifacts/run.json"}
}

func bcSelect(ctx context.Context, q pgx.Tx, sql string, args ...any) ([]bayescompile.Row, error) {
	rs, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	var out []bayescompile.Row
	for rs.Next() {
		row, err := bayescompile.Scan(rs)
		if err != nil {
			return nil, err
		}
		out = append(out, row)
	}
	return out, rs.Err()
}

// bcSettings is the row of bayes_settings (id 1) as the rules read it; none is every default.
func bcSettings(ctx context.Context, q pgx.Tx) (bayescompile.Settings, error) {
	var s bayescompile.Settings
	var slots *int64
	err := q.QueryRow(ctx, `SELECT compile_cpu, compile_memory, compile_seconds, compile_tmp, compile_slots, cache_max_bytes
		FROM bayes_settings WHERE id = 1`).Scan(&s.CompileCPU, &s.CompileMemory, &s.CompileSeconds, &s.CompileTmp, &slots,
		&s.CacheMaxBytes)
	if errors.Is(err, pgx.ErrNoRows) {
		return bayescompile.Settings{}, nil
	}
	if slots != nil {
		n := int(*slots)
		s.CompileSlots = &n
	}
	return s, err
}

// ── following a running compile ──────────────────────────────────────────────

// follow is one running row's Job: the row as it is while the Job runs, or its next state.
func (r *bcRound) follow(ctx context.Context, row bayescompile.Row) (bayescompile.Row, error) {
	k, ns := kubeAPI(), r.c.s.Scripts.Namespace
	if row.JobName == "" {
		return r.clusterFailed(ctx, row, "the compile has no Job")
	}
	job, err := kube.GetJob(ctx, k, ns, row.JobName)
	if err != nil {
		return row, bcKubeWait(err)
	}
	if job == nil {
		return r.clusterFailed(ctx, row, "the compile's Job is no longer in the cluster (deleted, or removed once "+
			"finished while no gateway was running)")
	}
	outcome := kube.JobOutcome(job)
	if outcome == "" {
		return r.running(ctx, row, job)
	}
	pod, err := bcLastPod(ctx, k, ns, row.JobName)
	if err != nil {
		return row, bcKubeWait(err)
	}
	log, exitCode, ended, err := kube.JobResult(ctx, k, ns, row.JobName, 256*1024)
	var unavailable *kube.LogUnavailable
	if errors.As(err, &unavailable) {
		if transient(&unavailable.Kube) {
			return row, &bcWait{"the cluster did not give the log: " + unavailable.Kube.Message}
		}
		log, exitCode, ended = "(the cluster did not give the log: "+unavailable.Kube.Message+")", unavailable.ExitCode, unavailable.Reason
	} else if err != nil {
		return row, bcKubeWait(err)
	}
	failure := kube.JobFailure(job)
	code, isCode := pyInt(exitCode)
	isCode = isCode && exitCode != nil
	switch {
	case outcome == "succeeded":
		m, err := bayescompile.ParseMessage([]byte(kube.TerminationMessage(pod, bcContainer)), row.ProgramSHA256)
		if err != nil {
			r.c.d.Log.Error("bayes compile: the Job ended without the compile's termination message", "job", row.JobName,
				"err", err.Error())
			return r.failed(ctx, row, bayescompile.ReasonJob)
		}
		return r.keep(ctx, row, m)
	case kube.StoppedByCluster(pod) != "":
		return r.clusterFailed(ctx, row, kube.StoppedByClusterText)
	case strings.Contains(failure, "DeadlineExceeded"):
		if pod != nil && !bcRan(pod) {
			return r.clusterFailed(ctx, row, "its Pod did not start within the compile's deadline")
		}
		return r.timedOut(ctx, row, bayescompile.ReasonDeadline)
	case ended == "OOMKilled" || isCode && code == 137:
		return r.timedOut(ctx, row, bayescompile.ReasonMemory)
	case isCode && code == modelgroups.ExitRefused:
		reason := bayescompile.ReasonJob
		if e, found := modelgroups.ErrorFromLog(log); found &&
			(e.Code == bayescompile.ReasonCompileFailed || e.Code == bayescompile.ReasonProgramHash) {
			reason = e.Code
		}
		r.c.d.Log.Error("bayes compile: the runner refused the program", "job", row.JobName, "reason", reason,
			"log", bcLastLines(log, bcLogLines))
		return r.failed(ctx, row, reason)
	case !isCode && (pod == nil || !bcRan(pod)):
		why := failure
		if why == "" {
			why = "the Job failed before its container ran"
		}
		return r.clusterFailed(ctx, row, why)
	}
	r.c.d.Log.Error("bayes compile: the compile's Job failed", "job", row.JobName, "exit", pyjson.Str(exitCode),
		"why", kube.ExplainExit(exitCode, ended, r.limits.Memory), "log", bcLastLines(log, bcLogLines))
	return r.failed(ctx, row, bayescompile.ReasonJob)
}

// running is a Job that has not ended: the row as it is, unless the cluster will not run it (no Pod, an image it
// cannot download, no room for it), or did not end it within its deadline.
func (r *bcRound) running(ctx context.Context, row bayescompile.Row, job any) (bayescompile.Row, error) {
	k, ns := kubeAPI(), r.c.s.Scripts.Namespace
	age := sbAgeSeconds(job)
	why := ""
	if !kube.HasPod(job) && age >= sbNoPodSeconds {
		w, err := kube.Refused(ctx, k, ns, row.JobName)
		if err != nil {
			return row, bcKubeWait(err)
		}
		why = w
	}
	if why == "" && kube.HasPod(job) {
		cannot, unscheduled, ran, err := kube.StartState(ctx, k, ns, row.JobName)
		if err != nil {
			return row, bcKubeWait(err)
		}
		pending := min(float64(bcPendingSeconds), float64(row.DeadlineSeconds)/4)
		switch {
		case cannot != "" && age >= sbPullSeconds:
			why = cannot
		case unscheduled != "" && !ran && age >= pending:
			why = "the cluster has no room for the compile now: " + unscheduled
		}
	}
	if why == "" && age > float64(row.DeadlineSeconds+jobGraceSeconds) {
		why = "the cluster did not end the compile's Job within its deadline"
	}
	if why == "" {
		return row, nil
	}
	return r.clusterFailed(ctx, row, why)
}

// bcLastPod is the Job's newest Pod, nil when it has none.
func bcLastPod(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) (any, error) {
	pods, err := kube.JobPods(ctx, k, ns, name)
	if err != nil || len(pods) == 0 {
		return nil, err
	}
	sorted := kube.SortByCreation(pods)
	return sorted[len(sorted)-1], nil
}

// bcRan says whether the Pod's container started (it runs, or it ended after it started).
func bcRan(pod any) bool {
	for _, st := range kube.List(kube.Dict(kube.Dict(pod).Value("status")).Value("containerStatuses")) {
		state := kube.Dict(kube.Dict(st).Value("state"))
		if pyjson.Truthy(state.Value("running")) {
			return true
		}
		if t := kube.Dict(state.Value("terminated")); pyjson.Truthy(t.Value("startedAt")) {
			return true
		}
	}
	return false
}

// bcLastLines is the text's last n lines.
func bcLastLines(text string, n int) string {
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

func (r *bcRound) save(ctx context.Context, row bayescompile.Row) (bayescompile.Row, error) {
	return row, bayescompile.Write(ctx, r.tx, row)
}

func (r *bcRound) failed(ctx context.Context, row bayescompile.Row, reason string) (bayescompile.Row, error) {
	next, err := bayescompile.Failed(row, reason, r.now)
	if err != nil {
		return row, err
	}
	r.c.d.Log.Info("bayes compile: failed", "row", row.ID, "program", row.ProgramSHA256, "reason", reason)
	return r.save(ctx, next)
}

func (r *bcRound) timedOut(ctx context.Context, row bayescompile.Row, reason string) (bayescompile.Row, error) {
	next, err := bayescompile.TimedOut(row, reason, r.now)
	if err != nil {
		return row, err
	}
	r.c.d.Log.Info("bayes compile: timeout", "row", row.ID, "program", row.ProgramSHA256, "reason", reason,
		"deadline_seconds", row.DeadlineSeconds, "memory_bytes", row.MemoryBytes)
	return r.save(ctx, next)
}

func (r *bcRound) clusterFailed(ctx context.Context, row bayescompile.Row, why string) (bayescompile.Row, error) {
	next, err := bayescompile.ClusterFailed(row, why, r.now)
	if err != nil {
		return row, err
	}
	r.c.d.Log.Warn("bayes compile: the cluster failed the compile", "job", row.JobName, "why", why, "state", next.State)
	return r.save(ctx, next)
}

// ── the keep ─────────────────────────────────────────────────────────────────

// bcCompileJSON is artifacts/compile.json, read once the termination message vouches for its bytes.
type bcCompileJSON struct {
	ProgramSHA256 string          `json:"program_sha256"`
	BinarySHA256  string          `json:"binary_sha256"`
	BinaryBytes   int64           `json:"binary_bytes"`
	Info          json.RawMessage `json:"info"`
	Seconds       float64         `json:"seconds"`
	Warnings      []string        `json:"warnings"`
}

// keep is «Keep»: compile.json read and vouched for, its stanc --info cross-checked with the stored manifests, the
// binary copied by the engine into Tabularia's storage under the message's sha256, the cache's room, the row ready.
// Every step is idempotent by the row: a gateway that dies after the copy and before the commit does it again, and
// the engine finds the object already there.
func (r *bcRound) keep(ctx context.Context, row bayescompile.Row, m bayescompile.Message) (bayescompile.Row, error) {
	log := r.c.d.Log
	if !r.storage.ready() {
		return row, &bcWait{"the scripts bucket is not configured"}
	}
	v, err := bcVersionOf(ctx, r.tx, row)
	if err != nil {
		return row, err
	}
	if v == nil {
		log.Error("bayes compile: no version holds the compiled program any more", "row", row.ID)
		return r.failed(ctx, row, bayescompile.ReasonJob)
	}
	prefix := bayescompile.StepPrefix(row.ID, row.Attempt)
	body, err := r.readCompileJSON(ctx, prefix+"artifacts/compile.json")
	if err != nil {
		return row, err
	}
	sum := sha256.Sum256(body)
	if body == nil || hex.EncodeToString(sum[:]) != m.InfoSHA256 {
		// the file sits in the shared bucket: it counts only when the message vouches for its bytes
		log.Error("bayes compile: compile.json is not the one the termination message vouches for", "job", row.JobName,
			"found", body != nil)
		return r.failed(ctx, row, bayescompile.ReasonJob)
	}
	var cj bcCompileJSON
	if err := json.Unmarshal(body, &cj); err != nil || cj.ProgramSHA256 != m.ProgramSHA256 || cj.BinarySHA256 != m.BinarySHA256 ||
		cj.BinaryBytes <= 0 {
		log.Error("bayes compile: compile.json does not say what the termination message says", "job", row.JobName)
		return r.failed(ctx, row, bayescompile.ReasonJob)
	}
	prog, err := bcProgram(v)
	if err != nil {
		log.Error("bayes compile: the stored manifests could not be read", "row", row.ID, "err", err.Error())
		return r.failed(ctx, row, bayescompile.ReasonJob)
	}
	if ms := bayescodegen.CrossCheck(cj.Info, prog); len(ms) > 0 {
		said := make([]string, len(ms))
		for i, mm := range ms {
			said[i] = mm.String()
		}
		log.Error("bayes compile: stanc's --info disagrees with the manifests", "job", row.JobName, "program",
			row.ProgramSHA256, "mismatches", strings.Join(said, "; "))
		return r.failed(ctx, row, bayescompile.ReasonCrossCheck)
	}
	if cj.BinaryBytes > r.limits.CacheMaxBytes {
		return r.tooLarge(ctx, row, cj.BinaryBytes, "")
	}
	kept, refused, err := r.engineKeep(ctx, prefix+"model", m.BinarySHA256)
	if err != nil {
		return row, err
	}
	if refused != "" {
		log.Error("bayes compile: the binary was not kept", "job", row.JobName, "why", refused)
		return r.failed(ctx, row, bayescompile.ReasonJob)
	}
	if kept > r.limits.CacheMaxBytes {
		return r.tooLarge(ctx, row, kept, m.BinarySHA256)
	}
	if _, err := r.makeRoom(ctx, kept, m.BinarySHA256); err != nil {
		if errors.Is(err, bayescompile.ErrNoRoom) {
			return row, &bcWait{"no room in the cache while binaries are being placed"}
		}
		return row, err
	}
	warnings := cj.Warnings
	if len(warnings) > bcMaxWarnings {
		warnings = warnings[:bcMaxWarnings]
	}
	if warnings == nil {
		warnings = []string{}
	}
	w, _ := json.Marshal(warnings)
	seconds := cj.Seconds
	if seconds <= 0 && !row.StartedAt.IsZero() {
		seconds = r.now.Sub(row.StartedAt).Seconds()
	}
	next, err := bayescompile.Kept(row, m, kept, seconds, string(w), r.now)
	if err != nil {
		return row, err
	}
	log.Info("bayes compile: ready", "row", row.ID, "program", row.ProgramSHA256, "binary", m.BinarySHA256, "bytes", kept)
	return r.save(ctx, next)
}

// tooLarge is a binary larger than the whole cache: failed with its size (a request after the limit is raised enough
// compiles it again); its object, when the engine kept it already and no ready row holds the same bytes, goes.
func (r *bcRound) tooLarge(ctx context.Context, row bayescompile.Row, size int64, kept string) (bayescompile.Row, error) {
	if kept != "" {
		var held bool
		if err := r.tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM bayes_binaries WHERE state = 'ready' AND binary_sha256 = $1)`,
			kept).Scan(&held); err != nil {
			return row, err
		}
		if !held {
			if err := r.engineDelete(ctx, []string{kept}); err != nil {
				return row, err
			}
		}
	}
	next, err := bayescompile.TooLarge(row, size, r.limits.CacheMaxBytes, r.now)
	if err != nil {
		return row, err
	}
	return r.save(ctx, next)
}

// readCompileJSON is compile.json's bytes from the step's prefix (nil: not there); larger than bcCompileJSONMax is not
// the runner's and reads as absent.
func (r *bcRound) readCompileJSON(ctx context.Context, key string) ([]byte, error) {
	cctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	body, _, err := r.c.d.openScriptObject(cctx, r.storage, key)
	if err != nil {
		var failed *scriptFailed
		if errors.As(err, &failed) {
			return nil, &bcWait{failed.msg}
		}
		return nil, &bcWait{"the engine: " + err.Error()}
	}
	if body == nil {
		return nil, nil
	}
	defer body.Close()
	b, err := io.ReadAll(io.LimitReader(body, bcCompileJSONMax+1))
	if err != nil {
		return nil, &bcWait{"the engine: " + err.Error()}
	}
	if len(b) > bcCompileJSONMax {
		return nil, nil
	}
	return b, nil
}

// engineKeep is the engine's /scripts/binary/keep: the bytes kept, or why it refused them (another sha256, too long,
// no binary); a *bcWait when the engine did not answer.
func (r *bcRound) engineKeep(ctx context.Context, key, sha string) (int64, string, error) {
	cctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	res, raw, err := r.c.d.Engine.JSON(cctx, http.MethodPost, "/scripts/binary/keep", pyjson.Object{
		{Key: "connection", Value: r.storage.payload()}, {Key: "key", Value: key}, {Key: "sha256", Value: sha},
		{Key: "max_bytes", Value: int64(bcBinaryMaxBytes)}, {Key: "bucket", Value: r.c.d.Config.EngineBucket}})
	if err != nil {
		return 0, "", &bcWait{"the engine: " + err.Error()}
	}
	switch {
	case res.StatusCode == http.StatusConflict || res.StatusCode == http.StatusNotFound:
		return 0, engineDetail(raw), nil
	case res.StatusCode >= 400:
		return 0, "", &bcWait{fmt.Sprintf("the engine answered %d: %s", res.StatusCode, engineDetail(raw))}
	}
	var out struct {
		Bytes int64 `json:"bytes"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || out.Bytes <= 0 {
		return 0, "", &bcWait{"the engine's answer to the keep: " + runes(string(raw), 200)}
	}
	return out.Bytes, "", nil
}

// engineDelete removes binaries from Tabularia's storage; a *bcWait when the engine did not remove them all (the rows
// that held them stay, and the next round asks again).
func (r *bcRound) engineDelete(ctx context.Context, sha256s []string) error {
	if len(sha256s) == 0 {
		return nil
	}
	cctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	res, raw, err := r.c.d.Engine.JSON(cctx, http.MethodPost, "/scripts/binary/delete", pyjson.Object{
		{Key: "bucket", Value: r.c.d.Config.EngineBucket}, {Key: "sha256s", Value: stringsAny(sha256s)}})
	if err != nil {
		return &bcWait{"the engine did not delete the binaries: " + err.Error()}
	}
	var out struct {
		Failed []string `json:"failed"`
	}
	if res.StatusCode >= 400 || json.Unmarshal(raw, &out) != nil || len(out.Failed) > 0 {
		return &bcWait{fmt.Sprintf("the engine did not delete the binaries (%d): %s", res.StatusCode, runes(string(raw), 300))}
	}
	return nil
}

// ── the cache ────────────────────────────────────────────────────────────────

// makeRoom is «The cache» for an incoming binary (0 and "": none, a limit lowered since): the ready rows evicted until
// the cache holds the limit, those of an older image first, then the least recently used, never one a run is placing
// (locked FOR SHARE: FOR UPDATE SKIP LOCKED leaves it out). The rows go, and the objects no row left holds.
// bayescompile.ErrNoRoom: what could go went, the rest waits for the next round.
func (r *bcRound) makeRoom(ctx context.Context, incoming int64, incomingSHA256 string) (bayescompile.Eviction, error) {
	var used int64
	if err := r.tx.QueryRow(ctx, `SELECT coalesce(sum(binary_bytes), 0) FROM bayes_binaries WHERE state = 'ready'`).
		Scan(&used); err != nil {
		return bayescompile.Eviction{}, err
	}
	if used+incoming <= r.limits.CacheMaxBytes {
		return bayescompile.Eviction{Used: used + incoming}, nil // nothing to evict: no row locked
	}
	ready, err := bcSelect(ctx, r.tx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries WHERE state = 'ready' ORDER BY id`)
	if err != nil {
		return bayescompile.Eviction{}, err
	}
	free := map[int64]bool{}
	rs, err := r.tx.Query(ctx, `SELECT id FROM bayes_binaries WHERE state = 'ready' ORDER BY id FOR UPDATE SKIP LOCKED`)
	if err != nil {
		return bayescompile.Eviction{}, err
	}
	for rs.Next() {
		var id int64
		if err := rs.Scan(&id); err != nil {
			rs.Close()
			return bayescompile.Eviction{}, err
		}
		free[id] = true
	}
	rs.Close()
	if err := rs.Err(); err != nil {
		return bayescompile.Eviction{}, err
	}
	locked := map[int64]bool{}
	for _, row := range ready {
		if !free[row.ID] {
			locked[row.ID] = true
		}
	}
	e, err := bayescompile.MakeRoom(ready, locked, r.digest, r.limits.CacheMaxBytes, incoming, incomingSHA256)
	if errors.Is(err, bayescompile.ErrTooLarge) {
		return e, err
	}
	ids := make([]int64, len(e.Rows))
	for i, row := range e.Rows {
		ids[i] = row.ID
	}
	// the objects first: when the engine fails, every row stays (a row whose object went anyway is refused at its
	// next place, forgotten and compiled again); never an object left that no row counts
	if err := r.engineDelete(ctx, e.Objects); err != nil {
		return bayescompile.Eviction{}, err
	}
	if err := bayescompile.Delete(ctx, r.tx, ids...); err != nil {
		return e, err
	}
	if len(e.Rows) > 0 {
		r.c.d.Log.Info("bayes compile: binaries evicted from the cache", "rows", len(e.Rows), "objects", len(e.Objects),
			"used", e.Used, "limit", r.limits.CacheMaxBytes)
	}
	return e, err
}

// ── starting the queued compiles ─────────────────────────────────────────────

// startQueued starts the rows NextToStart picks: one per person, «Compiles at once» in all; how many started.
func (r *bcRound) startQueued(ctx context.Context, live []bayescompile.Row) (int64, error) {
	if r.img == nil {
		return 0, nil // the group cannot run a Job now: the queue waits
	}
	if !r.storage.ready() {
		r.c.d.Log.Warn("bayes compile: the scripts bucket is not configured: the compiles wait")
		return 0, nil
	}
	if bayescompile.CmdStanOf(r.group.Recipe) == "" {
		return 0, nil
	}
	next := bayescompile.NextToStart(live, r.limits.Slots)
	if len(next) == 0 {
		return 0, nil
	}
	secret, err := r.c.d.Fernet.Decrypt(r.storage.SecretEnc)
	if err != nil {
		r.c.d.Log.Error("bayes compile: the scripts bucket's key cannot be read: the compiles wait", "err", err.Error())
		return 0, nil
	}
	var started int64
	for _, row := range next {
		after, err := r.start(ctx, row, secret)
		var wait *bcWait
		switch {
		case errors.As(err, &wait):
			r.c.d.Log.Warn(fmt.Sprintf("bayes compile: row %d waits (%s)", row.ID, wait.why))
			continue
		case err != nil:
			return started, err
		}
		if after.State != row.State {
			started++
		}
	}
	return started, nil
}

// bcVersion is what a compile reads of a version holding the program.
type bcVersion struct {
	ModelID, Version                        int64
	SemanticHash, Program, ProgramSHA256    string
	Mapping, DataManifest, QuantityManifest string
	CodegenVersion                          int
}

// bcVersionOf is a version holding the row's program, read by its sha256 (never a program from a request): the
// request's own version when it still holds it, else the first; nil when no version holds it any more.
func bcVersionOf(ctx context.Context, q pgx.Tx, row bayescompile.Row) (*bcVersion, error) {
	var v bcVersion
	err := q.QueryRow(ctx, `SELECT model_id, version, semantic_hash, program, program_sha256, mapping, data_manifest,
		quantity_manifest, codegen_version FROM bayes_model_versions WHERE program_sha256 = $1
		ORDER BY (model_id = $2 AND version = $3) DESC, model_id, version LIMIT 1`, row.ProgramSHA256, row.ModelID, row.Version).
		Scan(&v.ModelID, &v.Version, &v.SemanticHash, &v.Program, &v.ProgramSHA256, &v.Mapping, &v.DataManifest,
			&v.QuantityManifest, &v.CodegenVersion)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &v, err
}

// bcProgram is the stored version as the cross-check reads it: the program and its three manifests.
func bcProgram(v *bcVersion) (*bayescodegen.Program, error) {
	p := &bayescodegen.Program{Text: []byte(v.Program), SHA256: v.ProgramSHA256, CodegenVersion: v.CodegenVersion}
	for _, part := range []struct {
		text string
		into any
	}{{v.Mapping, &p.Mapping}, {v.DataManifest, &p.Data}, {v.QuantityManifest, &p.Quantities}} {
		if err := json.Unmarshal([]byte(part.text), part.into); err != nil {
			return nil, err
		}
	}
	return p, nil
}

// start makes a queued row running: its program read from the versions by its sha256 and checked, the task, the Job
// with its ConfigMap and Secret. A row whose program no version holds any more leaves the queue.
func (r *bcRound) start(ctx context.Context, row bayescompile.Row, secret string) (bayescompile.Row, error) {
	v, err := bcVersionOf(ctx, r.tx, row)
	if err != nil {
		return row, err
	}
	if v == nil {
		return bayescompile.Row{}, bayescompile.Delete(ctx, r.tx, row.ID) // state none: nothing holds the program
	}
	next, err := bayescompile.Start(row, r.limits, r.now)
	if err != nil {
		return row, err
	}
	if sum := sha256.Sum256([]byte(v.Program)); hex.EncodeToString(sum[:]) != row.ProgramSHA256 {
		r.c.d.Log.Error("bayes compile: the stored program is not its sha256", "row", row.ID, "model", v.ModelID,
			"version", v.Version)
		return r.failed(ctx, next, bayescompile.ReasonProgramHash)
	}
	task := bcTask(r.group, r.img, r.limits, v, row.CmdStan, time.Now())
	if err := task.Bayes.Check(bayescontract.ModeCompile, ""); err != nil {
		r.c.d.Log.Error("bayes compile: the compile's task is not the contract's", "row", row.ID, "err", err.Error())
		return r.failed(ctx, next, bayescompile.ReasonJob)
	}
	code, err := modelgroups.CodeFiles(bayescontract.Group, task.JSON(), false)
	if err != nil {
		return row, err
	}
	code = append(code, modelgroups.File{Path: bcProgramFile, Content: []byte(v.Program)})
	data, _, items := modelgroups.ConfigMap(code) // text only: the runner, task.json and the program
	codeData := pyjson.Object{}
	itemList := make([]any, len(items))
	for i, it := range items {
		k := fmt.Sprintf("f%d", i)
		codeData = append(codeData, pyjson.KV{Key: k, Value: data[k]})
		itemList[i] = pyjson.Object{{Key: "key", Value: it["key"]}, {Key: "path", Value: it["path"]}}
	}
	js := r.c.d.jobSettings()
	manifest := bcJobManifest(js, next.JobName, r.img, r.limits, bcEnv(r.storage, bayescompile.StepPrefix(next.ID, next.Attempt)),
		bcLabels(r.img, next.ID), itemList, next.JobName+"-code", next.JobName+"-env", offlineNetwork(ctx, r.storage, r.c.d.Log))
	k, ns := kubeAPI(), r.c.s.Scripts.Namespace
	created := func() error {
		job, err := kube.CreateJob(ctx, k, ns, manifest)
		if err != nil {
			return err
		}
		labels := pyjson.Obj(pyjson.Obj(manifest.Value("metadata")).Value("labels"))
		owners := kube.OwnedBy(job)
		if err := kube.CreateObject(ctx, k, ns, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
			{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: next.JobName + "-code"}, {Key: "labels", Value: labels},
				{Key: "ownerReferences", Value: owners}}}, {Key: "data", Value: codeData}}); err != nil {
			return err
		}
		return kube.CreateObject(ctx, k, ns, "secrets", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"},
			{Key: "type", Value: "Opaque"}, {Key: "metadata", Value: pyjson.Object{{Key: "name", Value: next.JobName + "-env"},
				{Key: "labels", Value: labels}, {Key: "ownerReferences", Value: owners}}},
			{Key: "stringData", Value: pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: r.storage.AccessKey},
				{Key: "AWS_SECRET_ACCESS_KEY", Value: secret}}}})
	}()
	var ke *kube.Error
	switch {
	case errors.As(created, &ke) && transient(created):
		return row, &bcWait{"the Kubernetes API: " + ke.Message} // queued still: the same Job's name next round
	case errors.As(created, &ke):
		r.c.d.Log.Error("bayes compile: the cluster refused the compile's Job", "job", next.JobName, "err", ke.Message)
		return r.clusterFailed(ctx, next, "the cluster refused the compile's Job: "+runes(ke.Message, 300))
	case created != nil:
		return row, created
	}
	r.c.d.Log.Info("bayes compile: started", "job", next.JobName, "program", row.ProgramSHA256, "model", v.ModelID,
		"version", v.Version)
	return r.save(ctx, next)
}

// bcTask is the compile's task.json: the model alone, no step's ids, the compile's limits (the contract, «Correction
// of 2026-11-24 from the step-4 specs»).
func bcTask(g modelgroups.Group, img *ModelGroupImage, l bayescompile.Limits, v *bcVersion, cmdstan string, now time.Time) modelgroups.Task {
	return modelgroups.Task{Protocol: bayescontract.Protocol, Mode: bayescontract.ModeCompile, Group: bayescontract.Group,
		GroupVersion: img.Version, Model: bayescontract.Model, Variant: bayescontract.ModeCompile, Params: map[string]any{},
		Limits: map[string]int{"time_budget_seconds": int(l.DeadlineSeconds), "memory_bytes": int(l.MemoryBytes),
			"tmp_bytes": int(l.TmpBytes), "started_unix": int(now.Unix())},
		RecipeHash: g.RecipeHash, RunnerHash: g.RunnerHash,
		Bayes: &bayescontract.Task{Model: bayescontract.ModelRef{ID: v.ModelID, Version: int(v.Version), SemanticHash: v.SemanticHash,
			ProgramSHA256: v.ProgramSHA256, CodegenVersion: v.CodegenVersion, CmdStan: cmdstan}}}
}

// bcEnv is the compile Job's environment: where the binary and the artifacts go, /tmp for everything else.
func bcEnv(storage *scriptStorage, prefix string) pyjson.Object {
	url := func(key string) string { return "s3://" + storage.Bucket + "/" + key }
	return merge(pyjson.Object{{Key: bcEnvBinaryOut, Value: url(prefix + "model")},
		{Key: modelgroups.EnvArtifacts, Value: url(prefix + "artifacts/")},
		{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}}, storage.env())
}

func bcLabels(img *ModelGroupImage, id int64) pyjson.Object {
	return pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "models"},
		{Key: labelEnv, Value: fmt.Sprint(img.EnvironmentID)}, {Key: labelModelGroup, Value: bayescontract.Group},
		{Key: "tabularia.io/stage", Value: "compile"}, {Key: bcLabelCompile, Value: fmt.Sprint(id)}}
}

// bcJobManifest is the compile Job: modelJobManifest's shape (the image by digest, uid 10001, read-only root, no
// capability, no ServiceAccount token, internet access off, the image's architecture) with the compile's limits: CPU
// and memory requested as much as limited (a compile that starts must not be the first evicted), the deadline, /tmp
// an emptyDir of the compile's size (executable: make runs what it builds there), and the termination message read
// from its file only.
func bcJobManifest(js jobSettings, name string, img *ModelGroupImage, l bayescompile.Limits, environment, labels pyjson.Object,
	items []any, codeName, secretName string, net *offlineNet) pyjson.Object {
	environment = net.applyEnv(merge(environment, pyjson.Object{{Key: "PYTHONHASHSEED", Value: "0"}}))
	run, _ := modelgroups.Commands(modelgroupsLanguage(bayescontract.Group))
	command := make([]any, len(run))
	for i, c := range run {
		command[i] = c
	}
	resources := pyjson.Object{{Key: "cpu", Value: l.CPU}, {Key: "memory", Value: l.Memory}}
	container := pyjson.Object{{Key: "name", Value: bcContainer}, {Key: "image", Value: img.Image}, {Key: "command", Value: command},
		{Key: "workingDir", Value: modelgroups.CodeDir}, {Key: "env", Value: envList(environment)},
		{Key: "envFrom", Value: []any{pyjson.Object{{Key: "secretRef", Value: pyjson.Object{{Key: "name", Value: secretName}}}}}},
		{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: resources}, {Key: "limits", Value: resources}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
			{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}},
		{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "mountPath", Value: modelgroups.CodeDir}, {Key: "readOnly", Value: true}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}}}},
		// Kubernetes' default, written: the message is the file the runner writes, never a line of the log
		{Key: "terminationMessagePath", Value: bcTerminationLog}, {Key: "terminationMessagePolicy", Value: "File"}}
	pod := pyjson.Object{{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false},
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(scriptUID)},
			{Key: "runAsGroup", Value: int64(scriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{container}}}
	if img.Arch != "" {
		pod = append(pod, pyjson.KV{Key: "nodeSelector", Value: pyjson.Object{{Key: "kubernetes.io/arch", Value: img.Arch}}})
	}
	pod = append(pod, pyjson.KV{Key: "volumes", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap",
		Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "items", Value: items}}}},
		pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{{Key: "sizeLimit", Value: l.Tmp}}}}}})
	if js.registryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: js.registryPullSecret}}}})
	}
	pod = net.applyPod(pod)
	return jobManifestOf(name, withInternet(labels, false), l.DeadlineSeconds, js.jobTTL, pod)
}

// ── the daily cleaning ───────────────────────────────────────────────────────

// clean is the cleaning, once a day per gateway: the failed and timeout rows of an image that is no longer the
// group's current one, whose program no version holds (they hold no binary; bayescompile.Cleanable). Whether it ran.
func (r *bcRound) clean(ctx context.Context) (bool, error) {
	r.c.mu.Lock()
	due := time.Since(r.c.cleaned) >= bcCleanEvery
	r.c.mu.Unlock()
	if !due || r.img == nil {
		return false, nil
	}
	type candidate struct {
		row  bayescompile.Row
		held bool
	}
	rs, err := r.tx.Query(ctx, `SELECT `+prefixed("b.", bayescompile.Columns)+`, EXISTS (SELECT 1 FROM bayes_model_versions v
		WHERE v.program_sha256 = b.program_sha256) FROM bayes_binaries b WHERE b.state IN ('failed', 'timeout')
		AND b.image_digest <> $1`, r.digest)
	if err != nil {
		return false, err
	}
	var found []candidate
	for rs.Next() {
		var c candidate
		var row bayescompile.Row
		var held bool
		// Scan reads the row's columns; the last one is whether a version holds its program
		if row, err = bayescompile.Scan(bcHeldScanner{rs, &held}); err != nil {
			rs.Close()
			return false, err
		}
		c.row, c.held = row, held
		found = append(found, c)
	}
	rs.Close()
	if err := rs.Err(); err != nil {
		return false, err
	}
	var ids []int64
	for _, c := range found {
		if bayescompile.Cleanable(c.row, r.digest, c.held) {
			ids = append(ids, c.row.ID)
		}
	}
	if len(ids) > 0 {
		r.c.d.Log.Info("bayes compile: rows of an older image cleaned", "rows", len(ids))
	}
	return true, bayescompile.Delete(ctx, r.tx, ids...)
}

// bcHeldScanner is a row of Columns followed by one boolean more.
type bcHeldScanner struct {
	rows pgx.Rows
	held *bool
}

func (s bcHeldScanner) Scan(dest ...any) error { return s.rows.Scan(append(dest, s.held)...) }

// prefixed is a list of columns with a table's alias before each.
func prefixed(alias, columns string) string {
	parts := strings.Split(columns, ",")
	for i, p := range parts {
		parts[i] = alias + strings.TrimSpace(p)
	}
	return strings.Join(parts, ", ")
}
