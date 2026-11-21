package api

// A statistical model group's version in the build loop of the scripting environments
// (jobs_script_builds.go): its check Job also runs the group's self-test, the runner on the
// reference tables of the group compared with the values computed outside Tabularia; the version
// is ready only if every value matches (exit 4 and a report in the log otherwise). The runner's
// files and the reference cases travel in a ConfigMap that goes with the Job.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"

	"github.com/jackc/pgx/v5"
)

// mgSelftestMark separates, in the check's log, the package lock from the self-test's output.
const mgSelftestMark = "TABULARIA_SELFTEST_BEGIN"

// mgCheckScript is the managed check's command for a group of the language: the lock (R: the
// scripts' own check, with the completions; Python: mgPyCheck, the scripting environments' check
// imports polars, which a group's image does not hold), then the self-test in the code's folder.
// `set -e` keeps the self-test's exit code.
func mgCheckScript(language string) string { return mgCheckScriptWith(language, false) }

// mgCheckScriptWith is mgCheckScript for a group whose image records its system libraries
// (`system_libraries`, scriptimage.SystemLibrariesFile): their `package@version` lines follow the
// R packages in the lock, which is where the card reads their versions. An image built before the
// record existed (bc564373) has no such file: the line reads it only when it is there, so that
// `set -e` does not stop the check (and the self-test) on a missing record; the card then shows
// the library without a version.
func mgCheckScriptWith(language string, systemLibraries bool) string {
	check := `Rscript -e "$TABULARIA_R_CHECK"`
	if language == "python" {
		check = `python -I -c "$TABULARIA_PY_MANAGED_CHECK"`
	}
	_, selftest := modelgroups.Commands(language)
	if systemLibraries {
		f := scriptimage.SystemLibrariesFile
		check += "\n[ -f " + f + " ] && cat " + f + " || true"
	}
	return "set -e\n" + check + "\n" +
		"echo\n" +
		"echo " + mgSelftestMark + "\n" +
		"cd " + modelgroups.CodeDir + "\n" +
		strings.Join(selftest, " ") + "\n"
}

// hasSystemLibraries says whether the group's recipe names system libraries (its image records them).
func hasSystemLibraries(group string) bool {
	g, ok := modelgroups.Get(group)
	return ok && len(g.Recipe.SystemLibraries) > 0
}

// mgPyCheck is a Python group's lock: one name==version per installed distribution; it fails
// unless pyarrow reads S3 (the input, the results and the artifacts are s3:// addresses).
const mgPyCheck = "from pyarrow.fs import S3FileSystem\n" +
	"import importlib.metadata as m\n" +
	"names = {f\"{d.metadata['Name']}=={d.version}\" for d in m.distributions() if d.metadata['Name']}\n" +
	"print('\\n'.join(sorted(names, key=str.lower)))\n"

// mgCodeName is the ConfigMap of the code of a new version's check: one per version, checked once.
func mgCodeName(v *sbVersion) string {
	return fmt.Sprintf("tabularia-selftest-e%d-v%d", v.EnvironmentID, v.Number)
}

// mgRecheckCodeName is the ConfigMap of the code of one recheck of a version in use: a name of its
// own each time. A version is rechecked again (a recheck that could not run, a later runner), the
// ConfigMap of the previous one goes only when the cluster's garbage collector gets to it, and one
// of the same name still there would count as made (CreateObject) and then go, or hold the code of
// another runner.
func mgRecheckCodeName(v *sbVersion) string {
	return fmt.Sprintf("tabularia-reselftest-e%d-v%d-%s", v.EnvironmentID, v.Number, strconv.FormatInt(time.Now().UnixNano(), 36))
}

// mgCheckStage is the stage of a check Job: the one of a new version, or the one that runs the
// self-test again on the image in use (recheck).
func mgCheckStage(recheck bool) string {
	if recheck {
		return "recheck"
	}
	return "check"
}

// managedCheckJob is the check Job of a model group's version, and the ConfigMap's data of its code
// (the ConfigMap codeName).
func (b *scriptBuilds) managedCheckJob(v *sbVersion, env *sbEnvironment, group string, recheck bool, codeName string) (pyjson.Object, pyjson.Object, error) {
	files, err := modelgroups.CodeFiles(group, nil, true)
	if err != nil {
		return nil, nil, err
	}
	data, items := modelgroups.ConfigMap(files)
	code := pyjson.Object{}
	itemList := []any{}
	for i := range files {
		k := fmt.Sprintf("f%d", i)
		code = append(code, pyjson.KV{Key: k, Value: data[k]})
		itemList = append(itemList, pyjson.Object{{Key: "key", Value: items[i]["key"]}, {Key: "path", Value: items[i]["path"]}})
	}
	var image any
	if v.ImageRef != nil {
		image = *v.ImageRef
	}
	language := modelgroupsLanguage(group)
	environment := []any{pyjson.Object{{Key: "name", Value: "HOME"}, {Key: "value", Value: "/tmp"}},
		pyjson.Object{{Key: "name", Value: "TMPDIR"}, {Key: "value", Value: "/tmp"}}}
	if language == "python" {
		environment = append(environment, pyjson.Object{{Key: "name", Value: "TABULARIA_PY_MANAGED_CHECK"}, {Key: "value", Value: mgPyCheck}},
			pyjson.Object{{Key: "name", Value: "PYTHONHASHSEED"}, {Key: "value", Value: "0"}})
	} else {
		environment = append(environment, pyjson.Object{{Key: "name", Value: "TABULARIA_R_CHECK"}, {Key: "value", Value: sbRCheck}})
	}
	container := pyjson.Object{
		{Key: "name", Value: "check"}, {Key: "image", Value: image}, {Key: "command", Value: []any{"sh", "-c", mgCheckScriptWith(language, hasSystemLibraries(group))}},
		{Key: "workingDir", Value: modelgroups.CodeDir},
		{Key: "env", Value: environment},
		{Key: "resources", Value: pyjson.Object{
			{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: env.CPURequest}, {Key: "memory", Value: env.MemoryRequest}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: env.CPULimit}, {Key: "memory", Value: env.MemLimit}}}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
			{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}},
		{Key: "volumeMounts", Value: []any{
			pyjson.Object{{Key: "name", Value: "code"}, {Key: "mountPath", Value: modelgroups.CodeDir}, {Key: "readOnly", Value: true}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}}}},
	}
	pod := pyjson.Object{
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(sbScriptUID)},
			{Key: "runAsGroup", Value: int64(sbScriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{container}},
		{Key: "volumes", Value: []any{
			pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap", Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "items", Value: itemList}}}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}}}},
	}
	pod = append(pod, sbNodeSelector(v)...)
	if b.cfg.RegistryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: b.cfg.RegistryPullSecret}}}})
	}
	// the self-test fits models: longer than a plain import of the packages
	return b.job(sbJobName(mgCheckStage(recheck), v), sbLabels(mgCheckStage(recheck), v), max(b.panel.CheckTimeout(), 900), pod), code, nil
}

// createSelftestCode makes the ConfigMap codeName of the check's code, owned by its Job (it goes with it).
func (b *scriptBuilds) createSelftestCode(ctx context.Context, k scriptcfg.Kubernetes, manifest pyjson.Object, job any, code pyjson.Object, codeName string) error {
	labels := pyjson.Obj(pyjson.Obj(manifest.Value("metadata")).Value("labels"))
	return kube.CreateObject(ctx, k, b.s.Scripts.Namespace, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "labels", Value: labels}, {Key: "ownerReferences", Value: kube.OwnedBy(job)}}},
		{Key: "data", Value: code}})
}

// finishManagedCheck ends a model group's check: ready if the image starts and the self-test
// passed, failed with the reason otherwise.
func (b *scriptBuilds) finishManagedCheck(v *sbVersion, env *sbEnvironment, outcome, reason, log string, exitCode any) {
	lockPart, selftestPart, hasSelftest := strings.Cut(log, mgSelftestMark)
	if !hasSelftest {
		lockPart, selftestPart = log, ""
	}
	if outcome == "failed" && !hasSelftest {
		// the image does not start as a script would, before the self-test
		if reason == "" {
			reason = "it ended with an error"
		}
		b.fail(v, strings.TrimRightFunc(fmt.Sprintf("The image was built but does not start as a script would: %s\n%s", reason, kube.Tail(log, 8000)), isSpace), nil)
		return
	}
	var tail *string
	if hasSelftest {
		t := strings.TrimSpace(kube.Tail(selftestPart, 64<<10))
		tail = &t
	}
	v.Selftest = tail
	report, found := modelgroups.SelftestFromLog(selftestPart)
	switch {
	case found && !report.Passed():
		b.fail(v, mgSelftestFailure(report), nil)
	case !found || outcome == "failed":
		why := reason
		if why == "" {
			why = "it ended with an error"
		}
		if isExit(exitCode, modelgroups.ExitSelftestFailed) {
			why = "exit code 4"
		}
		b.fail(v, strings.TrimRightFunc(fmt.Sprintf("The image was built but the self-test of the group did not run to its end: %s\n%s",
			why, kube.Tail(selftestPart, 8000)), isSpace), nil)
	default:
		lock, completions := sbSplitCheckOutput(lockPart)
		now := jobs.NaiveNow()
		v.Status, v.Lock, v.Completions, v.FinishedAt, v.JobName, v.Error = "ready", &lock, completions, &now, nil, nil
	}
}

func isSpace(r rune) bool { return r == ' ' || r == '\n' || r == '\t' || r == '\r' }

func isExit(v any, code int64) bool {
	switch x := v.(type) {
	case int64:
		return x == code
	case float64:
		return x == float64(code)
	}
	return false
}

// mgSelftestFailure is the sentence of a version that did not pass its self-test: the values
// that differ from the reference ones.
func mgSelftestFailure(r *modelgroups.SelftestReport) string {
	var bad []string
	for _, c := range r.Checks {
		if c.Status != "ok" {
			line := "- " + c.Name
			if c.Detail != "" {
				line += ": " + c.Detail
			}
			bad = append(bad, line)
		}
	}
	more := ""
	if len(bad) > 15 {
		more = fmt.Sprintf("\n… and %d more", len(bad)-15)
		bad = bad[:15]
	}
	return fmt.Sprintf("The self-test of the group failed: %d of %d checks do not match the reference values.\n%s%s",
		r.Failed+r.Skipped, r.OK+r.Failed+r.Skipped, strings.Join(bad, "\n"), more)
}

// mgBaseSentence is why a group's image cannot be built here (its base is not among the base
// images offered), "" when it can.
func mgBaseSentence(g modelgroups.Group) string {
	images, err := scriptcfg.Get().Images()
	if err != nil {
		return "SCRIPTS__BASE_IMAGES is not valid JSON"
	}
	if _, err := modelgroups.BaseImage(g.Recipe, images); err != nil {
		return err.Error()
	}
	return ""
}

// ── two releases on one database ─────────────────────────────────────────────
//
// During a rolling upgrade the replica of the previous release and the new one take turns on the
// build loop's lock; after a `helm rollback` the release rolled back to finds the versions the newer
// one queued. A version's row holds its base image and packages, but its build's Dockerfile takes the
// system libraries from the recipe the gateway ships, and its check's self-test is the runner the
// gateway ships: a gateway of another release would build and check another image than the one
// queued (found on kind, 2026-11-21: the previous release's replica built the new recipe's packages
// without its GSL, and Rlibeemd did not compile). So a gateway makes the Jobs of a model group's
// version only when it ships the recipe and the runner the version was queued with
// (modelgroups.Group.Ships). The outcome of a Job is read by any gateway: the Job is what it is,
// whoever made it.
//
// A version of another release waits for that release's gateway, which takes it at its next round.
// When none comes (a rollback), it is failed once it has waited otherReleaseWait, saying why, and the same
// round queues the recipe of the gateway that failed it (mgQueueStale); a built image waits for its Job
// to go (the Job's TTL), then fails the same way.
//
// The limit, kept on purpose (recording the Dockerfile's hash would take a column): two releases that
// ship the same recipe and runner but write the Dockerfile differently (scriptimage) build each other's
// versions; Build again after the upgrade gives the group the new release's.

// otherReleaseWait is MODELS__OTHER_RELEASE_WAIT_SECONDS (ten minutes by default): how long a version
// of another release waits for a gateway of that release before it is failed (from its queueing), and
// how long a version another release made is left unanswered by a version of this recipe
// (mgQueueStale). It is a setting of its own, tied to none of the scripts' settings: what it must
// outlast is the time the replicas of two releases run side by side (the new one becoming ready, the
// old one stopping, a gateway's loop taking its turn every scripts.poll_seconds), which neither a
// build's timeout nor a Job's TTL says anything about.
func (b *scriptBuilds) otherReleaseWait() time.Duration {
	return time.Duration(b.cfg.ModelsOtherReleaseWait) * time.Second
}

// mgOtherReleaseSentence is why a version of another release, which no gateway of that release built
// or checked, is failed.
const mgOtherReleaseSentence = "This version was queued by another version of Tabularia (another recipe or runner of the group), " +
	"and no gateway of that version came to build it: this version of Tabularia builds its own"

// mgOfAnotherRelease says whether the version is a model group's version this gateway does not make
// the Jobs of: queued with another recipe or another runner than the ones it ships, or of a group it
// does not ship. An environment of the scripting itself is never.
func mgOfAnotherRelease(env *sbEnvironment, v *sbVersion) bool {
	group := mgGroupOfManaged(env.ManagedBy)
	if group == "" {
		return false
	}
	g, ok := modelgroups.Get(group)
	return !ok || !g.Ships(v.RecipeHash, v.RunnerHash)
}

// waitForAnotherRelease leaves a queued version of another release to that release's gateway, and
// fails it once it has waited longer than such a gateway would have let it.
func (b *scriptBuilds) waitForAnotherRelease(v *sbVersion) {
	if jobs.NaiveNow().Sub(v.CreatedAt) >= b.otherReleaseWait() {
		b.fail(v, mgOtherReleaseSentence, nil)
	}
}

// mgQueueStale queues a new version for every enabled group whose newest version was built
// from another recipe than the one this Tabularia ships (or has none): «Rebuild needed» without
// a click. A group whose newest version failed on the recipe shipped is left to the
// administrator's Rebuild, and one whose newest version another release made less than
// otherReleaseWait ago is left to that release for now. It returns how many it queued.
func mgQueueStale(ctx context.Context, tx pgx.Tx, log *slog.Logger, otherReleaseWait time.Duration) (int64, error) {
	var queued int64
	for _, g := range modelgroups.Groups() {
		var env int64
		if err := tx.QueryRow(ctx, `SELECT environment_id FROM model_groups WHERE id = $1 AND enabled AND environment_id IS NOT NULL`, g.ID).Scan(&env); err != nil {
			if scNoRows(err) {
				continue
			}
			return queued, err
		}
		if err := mgLockEnvironment(ctx, tx, env); err != nil {
			return queued, err
		}
		var status string
		var hash *string
		var created time.Time
		err := tx.QueryRow(ctx, `SELECT status, recipe_hash, created_at FROM script_environment_versions WHERE environment_id = $1
			ORDER BY number DESC LIMIT 1`, env).Scan(&status, &hash, &created)
		if err != nil && !scNoRows(err) {
			return queued, err
		}
		if err == nil && (mgInProgress(status) || hash != nil && *hash == g.RecipeHash) {
			continue
		}
		if err == nil && hash != nil && jobs.NaiveNow().Sub(created) < otherReleaseWait {
			// another release made it a moment ago: the other replica of a rolling upgrade, not a
			// version for this recipe to answer (the two would queue in turn)
			continue
		}
		if why := mgBaseSentence(g); why != "" {
			log.Warn("models: the group " + g.ID + " cannot be built again: " + why)
			continue
		}
		n, err := mgNewVersion(ctx, tx, env, g, "tabularia")
		if err != nil {
			return queued, err
		}
		log.Info(fmt.Sprintf("models: the group %s has a new recipe: version %d queued", g.ID, n))
		queued++
	}
	return queued, nil
}

// mgRecheckRetries is how many times a recheck that could not run is tried again, and
// mgRecheckDelay how long after: as a Script or model node with the most retries (MaxRetries) and
// the delay its retries have when it says none. A recheck that runs and passes or fails ends them.
const (
	mgRecheckRetries = scriptnodes.MaxRetries
	mgRecheckDelay   = scriptnodes.DefaultRetryDelaySeconds * time.Second
)

// recheckRunners starts, for each enabled group, the self-test again on the image in use when the
// runner this Tabularia ships is not the one the version was self-tested with (RunnerHash): an
// upgrade changed the runner's files, the common library, the schema or the catalogue, and the
// image does not need to be built again for it. Only the check Job runs, on the same image; the
// version stays ready, and so usable, until the self-test says otherwise (stopping every flow that
// uses the group for a check that nearly always passes would cost more than it protects), and
// the card says it is being checked (`rechecking`). No Job while a version is being built: that
// one is self-tested with the new runner anyway. A recheck that could not run (recheckLater) is
// started again after mgRecheckDelay, at most mgRecheckRetries times for the same runner.
//
// Each group in a savepoint of its own: what one group's recheck could not do (an answer of the
// API that is not JSON, the runner's files not readable) is logged and tried again at the next
// round, without undoing the round's other work, the outcomes of the Jobs the round already read
// and deleted (advance) among them.
func (b *scriptBuilds) recheckRunners(ctx context.Context, tx pgx.Tx) error {
	for _, g := range modelgroups.Groups() {
		sp, err := tx.Begin(ctx)
		if err != nil {
			return err
		}
		if err := b.recheckGroup(ctx, sp, g); err != nil {
			if rerr := sp.Rollback(ctx); rerr != nil || ctx.Err() != nil {
				return errors.Join(err, rerr, ctx.Err())
			}
			b.log.Warn(fmt.Sprintf("models: the self-test of the group %s cannot start again yet (%v)", g.ID, err))
			continue
		}
		if err := sp.Commit(ctx); err != nil {
			return err
		}
	}
	return nil
}

// recheckGroup is recheckRunners for one group.
func (b *scriptBuilds) recheckGroup(ctx context.Context, tx pgx.Tx, g modelgroups.Group) error {
	var env int64
	if err := tx.QueryRow(ctx, `SELECT environment_id FROM model_groups WHERE id = $1 AND enabled AND environment_id IS NOT NULL`, g.ID).Scan(&env); err != nil {
		if scNoRows(err) {
			return nil
		}
		return err
	}
	if err := mgLockEnvironment(ctx, tx, env); err != nil {
		return err
	}
	if n, err := mgInFlight(ctx, tx, env); err != nil {
		return err
	} else if n != 0 {
		return nil
	}
	var v sbVersion
	err := tx.QueryRow(ctx, `SELECT v.id, v.environment_id, v.number, v.image_ref, v.arch FROM script_environments e
		JOIN script_environment_versions v ON v.id = e.current_version_id WHERE e.id = $1 AND v.status = 'ready' AND v.job_name IS NULL
		AND v.image_ref IS NOT NULL AND v.recipe_hash = $2 AND v.runner_hash IS DISTINCT FROM $3
		AND (v.recheck_runner IS DISTINCT FROM $3 OR v.recheck_attempts <= $4 AND (v.recheck_after IS NULL OR v.recheck_after <= $5))`,
		env, g.RecipeHash, g.RunnerHash, mgRecheckRetries, jobs.NaiveNow()).Scan(&v.ID, &v.EnvironmentID, &v.Number, &v.ImageRef, &v.Arch)
	if scNoRows(err) {
		return nil
	}
	if err != nil {
		return err
	}
	// the attempts counted are the ones of this runner
	if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET recheck_runner = $2, recheck_attempts = 0, recheck_after = NULL,
		recheck_error = NULL WHERE id = $1 AND recheck_runner IS DISTINCT FROM $2`, v.ID, g.RunnerHash); err != nil {
		return err
	}
	e, err := sbEnvironmentOf(ctx, tx, env)
	if err != nil || e == nil {
		return err
	}
	codeName := mgRecheckCodeName(&v)
	manifest, code, err := b.managedCheckJob(&v, e, g.ID, true, codeName)
	if err != nil {
		return err
	}
	k := kubeAPI()
	job, err := kube.CreateJob(ctx, k, b.s.Scripts.Namespace, manifest)
	var ke *kube.Error
	switch {
	case errors.As(err, &ke) && ke.Status == 409: // an old one of the name is still going away: next round
		_ = kube.DeleteJob(ctx, k, b.s.Scripts.Namespace, sbJobName("recheck", &v))
		return nil
	case errors.As(err, &ke) && mgClusterBusy(ke): // the API server is restarting: next round
		b.log.Warn(fmt.Sprintf("models: the self-test of the group %s cannot start again yet (%s)", g.ID, ke.Message))
		return nil
	case errors.As(err, &ke):
		return b.recheckLater(ctx, tx, &v, g, "the cluster refused its Job: "+ke.Message)
	case err != nil:
		return err
	}
	if err := b.createSelftestCode(ctx, k, manifest, job, code, codeName); err != nil {
		b.log.Warn(fmt.Sprintf("models: the code of the self-test of the group %s not made (%v)", g.ID, err))
		_ = kube.DeleteJob(ctx, k, b.s.Scripts.Namespace, sbJobName("recheck", &v))
		if errors.As(err, &ke) && !mgClusterBusy(ke) {
			// a refusal (a quota of ConfigMaps full, a policy) that the next round would meet
			// again: counted, after mgRecheckDelay, and said on the card, as a refused Job
			return b.recheckLater(ctx, tx, &v, g, "the cluster refused the ConfigMap of its code: "+ke.Message)
		}
		return nil
	}
	if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET job_name = $2 WHERE id = $1`, v.ID, sbJobName("recheck", &v)); err != nil {
		return err
	}
	b.log.Info(fmt.Sprintf("models: the runner of the group %s changed: version %d is self-tested again, and serves meanwhile", g.ID, v.Number))
	return nil
}

// mgClusterBusy says whether the API server's answer is one of a cluster that cannot answer now
// (unreachable, too many requests, a server error): the next round tries again, uncounted.
func mgClusterBusy(ke *kube.Error) bool {
	return ke.Status == 0 || ke.Status == 429 || ke.Status >= 500
}

// advanceRecheck follows the Job that runs the self-test again on a version in use. The self-test
// reports and passes: the version records the runner it was checked with. It reports values that
// differ: the version is failed (the group says so, with the values), as a build whose self-test
// fails. Anything else says nothing of the image (a Pod the cluster refuses or never schedules, an
// image it cannot pull, the deadline, a node gone, a log it does not give, the Job gone): the
// version stays ready and serves, and the recheck is tried again later (recheckLater).
func (b *scriptBuilds) advanceRecheck(ctx context.Context, tx pgx.Tx, v *sbVersion, env *sbEnvironment) error {
	group := mgGroupOfManaged(env.ManagedBy)
	g, known := modelgroups.Get(group)
	if !known || v.JobName == nil {
		v.JobName = nil
		return nil
	}
	why, err := b.followRecheck(ctx, tx, v, g, *v.JobName)
	var ke *kube.Error
	if errors.As(err, &ke) && ke.Status != 0 && ke.Status != 429 && ke.Status < 500 { // the cluster refuses what the loop asks of it
		why, err = "the cluster refused it: "+ke.Message, nil
	}
	if err != nil || why == "" {
		return err
	}
	v.JobName = nil
	return b.recheckLater(ctx, tx, v, g, why)
}

// followRecheck is one look at a recheck's Job: "" while it runs or once it reported (the version
// written), else why it did not run to a report.
//
// The Job runs the code of the runner it was started for (recheck_runner), which may not be this
// gateway's: a `helm rollback` to the release that had already self-tested the image, a later
// upgrade while the recheck ran, or the two replicas of a rolling upgrade taking turns on the loop's
// lock. Its result says nothing of this runner: it is not judged here (no version failed for values
// another runner's code found, no runner recorded that never ran on the image) nor counted as an
// attempt; the Job is left to end, then discarded, and recheckRunners starts this runner's own
// recheck if the version was not self-tested with it.
func (b *scriptBuilds) followRecheck(ctx context.Context, tx pgx.Tx, v *sbVersion, g modelgroups.Group, name string) (string, error) {
	k, ns := kubeAPI(), b.s.Scripts.Namespace
	var started *string
	if err := tx.QueryRow(ctx, `SELECT recheck_runner FROM script_environment_versions WHERE id = $1`, v.ID).Scan(&started); err != nil {
		return "", err
	}
	ours := started != nil && *started == g.RunnerHash
	job, err := kube.GetJob(ctx, k, ns, name)
	if err != nil {
		return "", err
	}
	if job == nil {
		if !ours {
			return "", b.discardRecheck(ctx, tx, v, g, started)
		}
		return "its Job is no longer in the cluster", nil
	}
	outcome := kube.JobOutcome(job)
	if outcome == "" {
		// a Pod the cluster refuses, or an image the nodes cannot download, would otherwise wait
		// for the whole deadline: as a build's stage (advance)
		why := ""
		if !kube.HasPod(job) && sbAgeSeconds(job) >= sbNoPodSeconds {
			if why, err = kube.Refused(ctx, k, ns, name); err != nil {
				return "", err
			}
		}
		if why == "" && sbAgeSeconds(job) >= sbPullSeconds {
			if why, err = kube.PullFailure(ctx, k, ns, name); err != nil {
				return "", err
			}
		}
		if why == "" {
			return "", nil // still running: its own deadline ends it
		}
		if err := kube.DeleteJob(ctx, k, ns, name); err != nil {
			return "", err
		}
		if !ours {
			return "", b.discardRecheck(ctx, tx, v, g, started)
		}
		return "it cannot start: " + why, nil
	}
	if !ours { // ended: what it found is of another runner
		if err := kube.DeleteJob(ctx, k, ns, name); err != nil {
			return "", err
		}
		return "", b.discardRecheck(ctx, tx, v, g, started)
	}
	log, exitCode, _, err := kube.JobResult(ctx, k, ns, name, 8<<20)
	var unavailable *kube.LogUnavailable
	if errors.As(err, &unavailable) {
		log, exitCode = "", unavailable.ExitCode
	} else if err != nil {
		return "", err
	}
	if err := kube.DeleteJob(ctx, k, ns, name); err != nil {
		return "", err
	}
	_, selftestPart, ran := strings.Cut(log, mgSelftestMark)
	report, found := modelgroups.SelftestFromLog(selftestPart)
	switch {
	case ran && found && !report.Passed(): // the self-test ran and values differ
		b.finishManagedCheck(v, nil, outcome, kube.JobFailure(job), log, exitCode)
		msg := "The runner of the statistical models changed with this version of Tabularia, and the image in use no longer passes its self-test. " + deref(v.Error)
		v.Error = &msg
		return "", nil
	case ran && found && outcome == "succeeded":
		b.finishManagedCheck(v, nil, outcome, "", log, exitCode)
		// the runner the Job ran (ours, so this gateway's)
		if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET runner_hash = $2, recheck_runner = NULL, recheck_attempts = 0,
			recheck_after = NULL, recheck_error = NULL WHERE id = $1`, v.ID, *started); err != nil {
			return "", err
		}
		b.log.Info(fmt.Sprintf("models: the group %s passed its self-test again with the new runner (version %d)", g.ID, v.Number))
		return "", nil
	case unavailable != nil:
		return "the cluster did not give its log: " + unavailable.Kube.Message, nil
	}
	how := ""
	if outcome == "failed" {
		if how = kube.JobFailure(job); how == "" {
			how = "it ended with an error"
		}
		how = " (" + how + ")"
	}
	switch {
	case !ran:
		return "it stopped before the self-test began" + how, nil
	case !found:
		return "the self-test ended without its report" + how, nil
	}
	return "the self-test passed, but its Job failed" + how, nil
}

// discardRecheck forgets a recheck started for another runner (started) than this gateway's,
// its Job gone: not an attempt of this runner, and the version keeps what it was self-tested with.
// The same round's recheckRunners starts this runner's recheck when the version needs one.
func (b *scriptBuilds) discardRecheck(ctx context.Context, tx pgx.Tx, v *sbVersion, g modelgroups.Group, started *string) error {
	v.JobName = nil
	if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET recheck_runner = NULL, recheck_attempts = 0, recheck_after = NULL,
		recheck_error = NULL WHERE id = $1`, v.ID); err != nil {
		return err
	}
	b.log.Info(fmt.Sprintf("models: the self-test of the group %s (version %d) was started for another runner (%s) than this Tabularia's: "+
		"its result is not used", g.ID, v.Number, deref(started)))
	return nil
}

// recheckLater records a recheck of the version in use that did not run to a report: the version
// stays ready and serves, and the recheck starts again after mgRecheckDelay, as long as fewer than
// 1 + mgRecheckRetries of the same runner could not run. Past them, nothing more: the version still
// serves, self-tested with its previous runner, and the card says why (a Rebuild checks it).
func (b *scriptBuilds) recheckLater(ctx context.Context, tx pgx.Tx, v *sbVersion, g modelgroups.Group, why string) error {
	var attempts int64
	err := tx.QueryRow(ctx, `UPDATE script_environment_versions SET recheck_attempts = CASE WHEN recheck_runner IS NOT DISTINCT FROM $2
		THEN recheck_attempts + 1 ELSE 1 END, recheck_runner = $2, recheck_after = $3, recheck_error = $4 WHERE id = $1 RETURNING recheck_attempts`,
		v.ID, g.RunnerHash, jobs.NaiveNow().Add(mgRecheckDelay), why).Scan(&attempts)
	if err != nil {
		return err
	}
	if attempts > mgRecheckRetries {
		b.log.Warn(fmt.Sprintf("models: the self-test of the group %s with the new runner could not run %d times (%s): version %d serves on, "+
			"self-tested with its previous runner; Rebuild the group to check it", g.ID, attempts, why, v.Number))
		return nil
	}
	b.log.Warn(fmt.Sprintf("models: the self-test of the group %s with the new runner could not run (%s): version %d serves on, "+
		"tried again in %s (%d of %d)", g.ID, why, v.Number, mgRecheckDelay, attempts, mgRecheckRetries+1))
	return nil
}
