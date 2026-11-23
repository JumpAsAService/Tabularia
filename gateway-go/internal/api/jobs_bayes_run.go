package api

// The sampling Job of the group bayes, from the gateway (docs/specs/bayes-runner.md, «The Job, from the gateway»): a model
// Job (modelJobManifest) with four differences kept here, so that no other group's manifest changes: the /tmp the card
// sets, the address of the compiled model, the allocator's variables, and the step's files with the compiled model's.

import (
	"context"
	"errors"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const (
	// bayesEnvBinaryIn is the address the runner fetches the compiled model from (s3://<scripts bucket>/<step prefix>/model).
	bayesEnvBinaryIn = "TABULARIA_BINARY_IN"
	// bayesMallocThreshold is glibc's mmap and trim threshold in the Job's environment: K2 and X3's measures (the peaks of the
	// same run varied by 20 and 8 MB without them). Never CmdStan's own environment, which the runner starts with PATH alone.
	bayesMallocThreshold = "262144"
)

// bayesSamplingTmp is the sampling Job's /tmp in force: the card's setting (bayes_settings.sampling_tmp) else the recipe's
// resources.tmp, as a Kubernetes quantity (the emptyDir's sizeLimit) and in bytes (the task's tmp_bytes).
func bayesSamplingTmp(ctx context.Context, q bcardQuerier) (string, int64, error) {
	s, err := bayesSettingsRead(ctx, q)
	if err != nil {
		return "", 0, err
	}
	return bayesSamplingTmpOf(s)
}

// bayesSamplingTmpOf is bayesSamplingTmp for the settings already read.
func bayesSamplingTmpOf(s bayesSettingsRow) (string, int64, error) {
	g, ok := modelgroups.Get(bayescontract.Group)
	if !ok {
		return "", 0, errors.New("there is no model group bayes")
	}
	def, err := bayesCardDefaults(g)
	if err != nil {
		return "", 0, err
	}
	tmp := bayesCardInForce(def, s).SamplingTmp
	return tmp, int64(mgBytes(tmp)), nil
}

// bayesSetRunLimits is setRunLimits with the sampling /tmp in force instead of the model files' formula.
func bayesSetRunLimits(limits map[string]int, timeout int64, img *ModelGroupImage, tmpBytes int64, now time.Time) {
	setRunLimits(limits, timeout, img, now)
	limits["tmp_bytes"] = int(tmpBytes)
}

// bayesEnvironment adds to a model Job's environment what the sampling Job needs besides it: where the compiled model is
// (`<prefix>/model`, in the step's prefix) and the allocator's two variables.
func bayesEnvironment(base pyjson.Object, bucket, prefix string) pyjson.Object {
	return merge(base, pyjson.Object{{Key: bayesEnvBinaryIn, Value: "s3://" + bucket + "/" + prefix + "/model"},
		{Key: "MALLOC_MMAP_THRESHOLD_", Value: bayesMallocThreshold}, {Key: "MALLOC_TRIM_THRESHOLD_", Value: bayesMallocThreshold}})
}

// bayesStepKeys are modelStepKeys' files and the compiled model's, deleted by exact key at the step's end.
func bayesStepKeys(prefix string) (in, out, results, artifacts string, all []string) {
	in, out, results, artifacts, all = modelStepKeys(prefix)
	return in, out, results, artifacts, append(all, prefix+"/model")
}

// bayesJobManifest is the sampling Job: modelJobManifest's (the image by digest, the group's CPU and memory, uid 10001,
// read-only root, no capability, no ServiceAccount token, internet access off) with /tmp an emptyDir of `tmp` (a
// quantity: the card's sampling /tmp), which every model Job's already is: an emptyDir is executable, and the compiled
// model runs from it.
func bayesJobManifest(js jobSettings, name string, img *ModelGroupImage, tmp string, environment, labels pyjson.Object,
	timeout int64, items []any, codeName, secretName string, net *offlineNet) pyjson.Object {
	m := modelJobManifest(js, name, img, environment, labels, timeout, items, codeName, secretName, net)
	pod := pyjson.Obj(pyjson.Obj(pyjson.Obj(m.Value("spec")).Value("template")).Value("spec"))
	for _, v := range objectList(pod.Value("volumes")) {
		if v.Value("name") == "tmp" {
			pyjson.Obj(v.Value("emptyDir")).Set("sizeLimit", tmp)
		}
	}
	return m
}
