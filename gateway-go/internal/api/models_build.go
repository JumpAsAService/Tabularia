package api

// A statistical model group's version in the build loop of the scripting environments
// (jobs_script_builds.go): its check Job also runs the group's self-test, the runner on the
// reference tables of the group compared with the values computed outside Tabularia; the version
// is ready only if every value matches (exit 4 and a report in the log otherwise). The runner's
// files and the reference cases travel in a ConfigMap that goes with the Job.

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"

	"github.com/jackc/pgx/v5"
)

// mgSelftestMark separates, in the check's log, the package lock from the self-test's output.
const mgSelftestMark = "TABULARIA_SELFTEST_BEGIN"

// mgCheckScript is the managed check's command: the scripts' own check (the lock and the
// completions), then the self-test in the code's folder. `set -e` keeps the self-test's exit code.
const mgCheckScript = "set -e\n" +
	`Rscript -e "$TABULARIA_R_CHECK"` + "\n" +
	"echo\n" +
	"echo " + mgSelftestMark + "\n" +
	"cd " + modelgroups.CodeDir + "\n" +
	"Rscript " + modelgroups.CodeDir + "/runner/run.R --selftest\n"

func mgCodeName(v *sbVersion) string {
	return fmt.Sprintf("tabularia-selftest-e%d-v%d", v.EnvironmentID, v.Number)
}

// managedCheckJob is the check Job of a model group's version, and the ConfigMap's data of its code.
func (b *scriptBuilds) managedCheckJob(v *sbVersion, env *sbEnvironment, group string) (pyjson.Object, pyjson.Object, error) {
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
	container := pyjson.Object{
		{Key: "name", Value: "check"}, {Key: "image", Value: image}, {Key: "command", Value: []any{"sh", "-c", mgCheckScript}},
		{Key: "workingDir", Value: modelgroups.CodeDir},
		{Key: "env", Value: []any{pyjson.Object{{Key: "name", Value: "HOME"}, {Key: "value", Value: "/tmp"}},
			pyjson.Object{{Key: "name", Value: "TMPDIR"}, {Key: "value", Value: "/tmp"}},
			pyjson.Object{{Key: "name", Value: "TABULARIA_R_CHECK"}, {Key: "value", Value: sbRCheck}}}},
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
			pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap", Value: pyjson.Object{{Key: "name", Value: mgCodeName(v)}, {Key: "items", Value: itemList}}}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}}}},
	}
	pod = append(pod, sbNodeSelector(v)...)
	if b.cfg.RegistryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: b.cfg.RegistryPullSecret}}}})
	}
	// the self-test fits models: longer than a plain import of the packages
	return b.job(sbJobName("check", v), sbLabels("check", v), max(b.panel.CheckTimeout(), 900), pod), code, nil
}

// createSelftestCode makes the ConfigMap of the check's code, owned by its Job (it goes with it).
func (b *scriptBuilds) createSelftestCode(ctx context.Context, k scriptcfg.Kubernetes, v *sbVersion, manifest pyjson.Object, job any, code pyjson.Object) error {
	labels := pyjson.Obj(pyjson.Obj(manifest.Value("metadata")).Value("labels"))
	return kube.CreateObject(ctx, k, b.s.Scripts.Namespace, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: mgCodeName(v)}, {Key: "labels", Value: labels}, {Key: "ownerReferences", Value: kube.OwnedBy(job)}}},
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
