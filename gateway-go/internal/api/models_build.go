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
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"

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
// R packages in the lock, which is where the card reads their versions.
func mgCheckScriptWith(language string, systemLibraries bool) string {
	check := `Rscript -e "$TABULARIA_R_CHECK"`
	if language == "python" {
		check = `python -I -c "$TABULARIA_PY_MANAGED_CHECK"`
	}
	_, selftest := modelgroups.Commands(language)
	if systemLibraries {
		check += "\ncat " + scriptimage.SystemLibrariesFile
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

func mgCodeName(v *sbVersion, recheck bool) string {
	if recheck {
		return fmt.Sprintf("tabularia-reselftest-e%d-v%d", v.EnvironmentID, v.Number)
	}
	return fmt.Sprintf("tabularia-selftest-e%d-v%d", v.EnvironmentID, v.Number)
}

// mgCheckStage is the stage of a check Job: the one of a new version, or the one that runs the
// self-test again on the image in use (recheck).
func mgCheckStage(recheck bool) string {
	if recheck {
		return "recheck"
	}
	return "check"
}

// managedCheckJob is the check Job of a model group's version, and the ConfigMap's data of its code.
func (b *scriptBuilds) managedCheckJob(v *sbVersion, env *sbEnvironment, group string, recheck bool) (pyjson.Object, pyjson.Object, error) {
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
			pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap", Value: pyjson.Object{{Key: "name", Value: mgCodeName(v, recheck)}, {Key: "items", Value: itemList}}}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}}}},
	}
	pod = append(pod, sbNodeSelector(v)...)
	if b.cfg.RegistryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: b.cfg.RegistryPullSecret}}}})
	}
	// the self-test fits models: longer than a plain import of the packages
	return b.job(sbJobName(mgCheckStage(recheck), v), sbLabels(mgCheckStage(recheck), v), max(b.panel.CheckTimeout(), 900), pod), code, nil
}

// createSelftestCode makes the ConfigMap of the check's code, owned by its Job (it goes with it).
func (b *scriptBuilds) createSelftestCode(ctx context.Context, k scriptcfg.Kubernetes, v *sbVersion, manifest pyjson.Object, job any, code pyjson.Object, recheck bool) error {
	labels := pyjson.Obj(pyjson.Obj(manifest.Value("metadata")).Value("labels"))
	return kube.CreateObject(ctx, k, b.s.Scripts.Namespace, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: mgCodeName(v, recheck)}, {Key: "labels", Value: labels}, {Key: "ownerReferences", Value: kube.OwnedBy(job)}}},
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

// mgQueueStale queues a new version for every enabled group whose newest version was built
// from another recipe than the one this Tabularia ships (or has none): «Rebuild needed» without
// a click. A group whose newest version failed on the recipe shipped is left to the
// administrator's Rebuild. It returns how many it queued.
func mgQueueStale(ctx context.Context, tx pgx.Tx, log *slog.Logger) (int64, error) {
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
		err := tx.QueryRow(ctx, `SELECT status, recipe_hash FROM script_environment_versions WHERE environment_id = $1 ORDER BY number DESC LIMIT 1`, env).Scan(&status, &hash)
		if err != nil && !scNoRows(err) {
			return queued, err
		}
		if err == nil && (mgInProgress(status) || hash != nil && *hash == g.RecipeHash) {
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

// recheckRunners starts, for each enabled group, the self-test again on the image in use when the
// runner this Tabularia ships is not the one the version was self-tested with (RunnerHash): an
// upgrade changed the runner's files, the common library, the schema or the catalogue, and the
// image does not need to be built again for it. Only the check Job runs, on the same image; the
// version stays ready, and so usable, until the check says otherwise (stopping every flow that
// uses the group for a check that nearly always passes would cost more than it protects), and
// the card says it is being checked (`rechecking`). No Job while a version is being built: that
// one is self-tested with the new runner anyway.
func (b *scriptBuilds) recheckRunners(ctx context.Context, tx pgx.Tx) error {
	for _, g := range modelgroups.Groups() {
		var env int64
		if err := tx.QueryRow(ctx, `SELECT environment_id FROM model_groups WHERE id = $1 AND enabled AND environment_id IS NOT NULL`, g.ID).Scan(&env); err != nil {
			if scNoRows(err) {
				continue
			}
			return err
		}
		if err := mgLockEnvironment(ctx, tx, env); err != nil {
			return err
		}
		if n, err := mgInFlight(ctx, tx, env); err != nil {
			return err
		} else if n != 0 {
			continue
		}
		var v sbVersion
		err := tx.QueryRow(ctx, `SELECT v.id, v.environment_id, v.number, v.image_ref, v.arch FROM script_environments e
			JOIN script_environment_versions v ON v.id = e.current_version_id WHERE e.id = $1 AND v.status = 'ready' AND v.job_name IS NULL
			AND v.image_ref IS NOT NULL AND v.recipe_hash = $2 AND v.runner_hash IS DISTINCT FROM $3`, env, g.RecipeHash, g.RunnerHash).Scan(
			&v.ID, &v.EnvironmentID, &v.Number, &v.ImageRef, &v.Arch)
		if scNoRows(err) {
			continue
		}
		if err != nil {
			return err
		}
		e, err := sbEnvironmentOf(ctx, tx, env)
		if err != nil || e == nil {
			return err
		}
		manifest, code, err := b.managedCheckJob(&v, e, g.ID, true)
		if err != nil {
			return err
		}
		k := kubeAPI()
		job, err := kube.CreateJob(ctx, k, b.s.Scripts.Namespace, manifest)
		var ke *kube.Error
		switch {
		case errors.As(err, &ke) && ke.Status == 409: // an old one of the name is still going away: next round
			_ = kube.DeleteJob(ctx, k, b.s.Scripts.Namespace, sbJobName("recheck", &v))
			continue
		case errors.As(err, &ke):
			b.log.Warn(fmt.Sprintf("models: the self-test of the group %s cannot start again (%s)", g.ID, ke.Message))
			continue
		case err != nil:
			return err
		}
		if err := b.createSelftestCode(ctx, k, &v, manifest, job, code, true); err != nil {
			b.log.Warn(fmt.Sprintf("models: the code of the self-test of the group %s not made (%v)", g.ID, err))
			_ = kube.DeleteJob(ctx, k, b.s.Scripts.Namespace, sbJobName("recheck", &v))
			continue
		}
		if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET job_name = $2 WHERE id = $1`, v.ID, sbJobName("recheck", &v)); err != nil {
			return err
		}
		b.log.Info(fmt.Sprintf("models: the runner of the group %s changed: version %d is self-tested again, and serves meanwhile", g.ID, v.Number))
	}
	return nil
}

// advanceRecheck follows the Job that runs the self-test again on a version in use. It passes: the
// version records the runner it was checked with. It fails: the version is failed (the group says
// so, with the values that differ), as a build whose self-test fails. A Job that is gone is asked
// again at the next round.
func (b *scriptBuilds) advanceRecheck(ctx context.Context, tx pgx.Tx, v *sbVersion, env *sbEnvironment) error {
	group := mgGroupOfManaged(env.ManagedBy)
	g, known := modelgroups.Get(group)
	if !known || v.JobName == nil {
		v.JobName = nil
		return nil
	}
	k, ns, name := kubeAPI(), b.s.Scripts.Namespace, *v.JobName
	job, err := kube.GetJob(ctx, k, ns, name)
	if err != nil {
		return err
	}
	if job == nil {
		v.JobName = nil
		return nil
	}
	outcome := kube.JobOutcome(job)
	if outcome == "" {
		return nil // still running: its own deadline ends it
	}
	log, exitCode, _, err := kube.JobResult(ctx, k, ns, name, 8<<20)
	var unavailable *kube.LogUnavailable
	if errors.As(err, &unavailable) {
		log, exitCode = fmt.Sprintf("(the cluster did not give the log: %s)", unavailable.Kube.Message), unavailable.ExitCode
	} else if err != nil {
		return err
	}
	if err := kube.DeleteJob(ctx, k, ns, name); err != nil {
		return err
	}
	b.finishManagedCheck(v, env, outcome, kube.JobFailure(job), log, exitCode)
	if v.Status == "ready" {
		_, err = tx.Exec(ctx, `UPDATE script_environment_versions SET runner_hash = $2 WHERE id = $1`, v.ID, g.RunnerHash)
		b.log.Info(fmt.Sprintf("models: the group %s passed its self-test again with the new runner (version %d)", group, v.Number))
		return err
	}
	msg := "The runner of the statistical models changed with this version of Tabularia, and the image in use no longer passes its self-test. " + deref(v.Error)
	v.Error = &msg
	return nil
}
