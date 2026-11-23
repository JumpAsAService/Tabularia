package api

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func dump(m pyjson.Object) string {
	s, _ := pyjson.Dumps(m, pyjson.Options{})
	return s
}

func bayesTestImage() *ModelGroupImage {
	return &ModelGroupImage{Group: "bayes", EnvironmentID: 7, Image: "registry:5000/envs@sha256:89ab", CPURequest: "1", CPULimit: "4",
		MemoryRequest: "2Gi", MemoryLimit: "2Gi", Arch: "amd64"}
}

func bayesTestManifest(tmp string) pyjson.Object {
	env := bayesEnvironment(pyjson.Object{{Key: modelgroups.EnvInput, Value: "s3://b/env-7/s/input.parquet"}, {Key: "HOME", Value: "/tmp"}}, "b", "env-7/s")
	labels := pyjson.Object{{Key: labelModelGroup, Value: "bayes"}, {Key: labelModel, Value: "bayes_model"}}
	return bayesJobManifest(jobSettings{jobTTL: 60}, "tabularia-model-9-a1", bayesTestImage(), tmp, env, labels, 3600,
		[]any{pyjson.Object{{Key: "key", Value: "f0"}, {Key: "path", Value: "runner/run.py"}}}, "code", "env", nil)
}

// The sampling Job's manifest, as the bytes the cluster is sent: the card's /tmp, uid 10001, a read-only root, the
// allocator's variables and the compiled model's address; everything else is the model Jobs'.
func TestTheSamplingJobsManifest(t *testing.T) {
	got := dump(bayesTestManifest("3Gi"))
	want := `{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "tabularia-model-9-a1", "labels": {"tabularia.io/model-group": "bayes", "tabularia.io/model": "bayes_model", "tabularia.io/internet": "off"}}, ` +
		`"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 60, "template": {"metadata": {"labels": {"tabularia.io/model-group": "bayes", "tabularia.io/model": "bayes_model", "tabularia.io/internet": "off"}}, "spec": {` +
		`"restartPolicy": "Never", "automountServiceAccountToken": false, "securityContext": {"runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}, ` +
		`"containers": [{"name": "model", "image": "registry:5000/envs@sha256:89ab", "command": ["python", "-I", "-B", "/tabularia/code/runner/run.py"], "workingDir": "/tabularia/code", ` +
		`"env": [{"name": "TABULARIA_INPUT", "value": "s3://b/env-7/s/input.parquet"}, {"name": "HOME", "value": "/tmp"}, {"name": "TABULARIA_BINARY_IN", "value": "s3://b/env-7/s/model"}, ` +
		`{"name": "MALLOC_MMAP_THRESHOLD_", "value": "262144"}, {"name": "MALLOC_TRIM_THRESHOLD_", "value": "262144"}, {"name": "PYTHONHASHSEED", "value": "0"}], ` +
		`"envFrom": [{"secretRef": {"name": "env"}}], "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "2Gi"}}, ` +
		`"securityContext": {"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true, "capabilities": {"drop": ["ALL"]}}, ` +
		`"volumeMounts": [{"name": "code", "mountPath": "/tabularia/code", "readOnly": true}, {"name": "tmp", "mountPath": "/tmp"}]}], ` +
		`"nodeSelector": {"kubernetes.io/arch": "amd64"}, "volumes": [{"name": "code", "configMap": {"name": "code", "items": [{"key": "f0", "path": "runner/run.py"}]}}, {"name": "tmp", "emptyDir": {"sizeLimit": "3Gi"}}]}}}}`
	if got != want {
		t.Errorf("manifest:\n got %s\nwant %s", got, want)
	}
}

// Only the sizeLimit differs from a model Job's (the other groups' manifest is modelJobManifest's own, untouched).
func TestTheSamplingJobDiffersFromAModelJobOnlyInTmpAndEnvironment(t *testing.T) {
	img := bayesTestImage()
	labels := pyjson.Object{{Key: labelModelGroup, Value: "bayes"}, {Key: labelModel, Value: "bayes_model"}}
	items := []any{pyjson.Object{{Key: "key", Value: "f0"}, {Key: "path", Value: "runner/run.py"}}}
	env := pyjson.Object{{Key: modelgroups.EnvInput, Value: "s3://b/in"}}
	plain := dump(modelJobManifest(jobSettings{jobTTL: 60}, "n", img, env, labels, 3600, items, "code", "env", nil))
	got := dump(bayesJobManifest(jobSettings{jobTTL: 60}, "n", img, modelTmpLimit(img), env, labels, 3600, items, "code", "env", nil))
	if plain != got {
		t.Errorf("with the model Job's own /tmp and environment the manifests differ:\n%s\n%s", plain, got)
	}
	if !strings.Contains(plain, `"sizeLimit": "`+modelTmpLimit(img)+`"`) {
		t.Error(plain)
	}
}

func TestTheSamplingJobsFiles(t *testing.T) {
	in, out, results, artifacts, all := bayesStepKeys("env-7/s")
	if in != "env-7/s/input.parquet" || out != "env-7/s/output.parquet" || results != "env-7/s/results.parquet" || artifacts != "env-7/s/artifacts/" {
		t.Errorf("%s %s %s %s", in, out, results, artifacts)
	}
	_, _, _, _, model := modelStepKeys("env-7/s")
	if len(all) != len(model)+1 || all[len(all)-1] != "env-7/s/model" || fmt.Sprint(all[:len(model)]) != fmt.Sprint(model) {
		t.Errorf("%v", all)
	}
}

func TestTheSamplingLimitsCarryTheCardsTmp(t *testing.T) {
	limits := map[string]int{}
	bayesSetRunLimits(limits, 3600, bayesTestImage(), 3<<30, time.Unix(100, 0))
	if limits["tmp_bytes"] != 3<<30 || limits["time_budget_seconds"] != 3600 || limits["memory_bytes"] != 2<<30 || limits["started_unix"] != 100 {
		t.Errorf("%v", limits)
	}
}

// The /tmp in force: the recipe's 2 GiB, else the card's.
func TestTheSamplingTmpInForce(t *testing.T) {
	if tmp, n, err := bayesSamplingTmpOf(bayesSettingsRow{}); err != nil || tmp != "2Gi" || n != 2<<30 {
		t.Errorf("default: %q %d %v", tmp, n, err)
	}
	three := "3Gi"
	if tmp, n, err := bayesSamplingTmpOf(bayesSettingsRow{SamplingTmp: &three}); err != nil || tmp != "3Gi" || n != 3<<30 {
		t.Errorf("card: %q %d %v", tmp, n, err)
	}
}
