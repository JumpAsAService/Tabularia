package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// services/kube_jobs.py, the part a Script or Container node's run uses (script_runs._attempt):
// a Job created with its ConfigMap and Secret, followed to its end, its log read, deleted.

func kjJobs(ns string) string { return "/apis/batch/v1/namespaces/" + ns + "/jobs" }

func kjStatus(err error) int { return kube.Status(err) }

// kjCreateJob is create_job: one of the same name already there counts as created.
func kjCreateJob(ctx context.Context, k scriptcfg.Kubernetes, ns string, manifest pyjson.Object) (pyjson.Object, error) {
	v, err := kube.Call(ctx, k, http.MethodPost, kjJobs(ns), manifest, false)
	if err == nil {
		return pyjson.Obj(v), nil
	}
	if kjStatus(err) != 409 {
		return nil, err
	}
	name := pyjson.Str(pyjson.Obj(manifest.Value("metadata")).Value("name"))
	v, err = kube.Call(ctx, k, http.MethodGet, kjJobs(ns)+"/"+name, nil, false)
	return pyjson.Obj(v), err
}

// kjOwnedBy is owned_by.
func kjOwnedBy(job pyjson.Object) []any {
	meta := pyjson.Obj(job.Value("metadata"))
	return []any{pyjson.Object{{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"}, {Key: "name", Value: meta.Value("name")},
		{Key: "uid", Value: meta.Value("uid")}}}
}

// kjCreateObject is create_object.
func kjCreateObject(ctx context.Context, k scriptcfg.Kubernetes, ns, kind string, manifest pyjson.Object) error {
	_, err := kube.Call(ctx, k, http.MethodPost, "/api/v1/namespaces/"+ns+"/"+kind, manifest, false)
	if err != nil && kjStatus(err) != 409 {
		return err
	}
	return nil
}

// kjGetJob is get_job: nil when gone.
func kjGetJob(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) (pyjson.Object, error) {
	v, err := kube.Call(ctx, k, http.MethodGet, kjJobs(ns)+"/"+name, nil, false)
	if kjStatus(err) == 404 {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	o := pyjson.Obj(v)
	if o == nil {
		o = pyjson.Object{}
	}
	return o, nil
}

func kjConditions(job pyjson.Object) []pyjson.Object {
	return objectList(pyjson.Obj(job.Value("status")).Value("conditions"))
}

// kjOutcome is job_outcome: "succeeded", "failed" or "" while it runs.
func kjOutcome(job pyjson.Object) string {
	for _, c := range kjConditions(job) {
		if c.Value("status") == "True" && (c.Value("type") == "Complete" || c.Value("type") == "SuccessCriteriaMet") {
			return "succeeded"
		}
		if c.Value("status") == "True" && (c.Value("type") == "Failed" || c.Value("type") == "FailureTarget") {
			return "failed"
		}
	}
	if pyjson.Truthy(pyjson.Obj(job.Value("status")).Value("succeeded")) {
		return "succeeded"
	}
	return ""
}

func joinTruthy(sep string, vs ...any) string {
	var out []string
	for _, v := range vs {
		if pyjson.Truthy(v) {
			out = append(out, pyjson.Str(v))
		}
	}
	return strings.Join(out, sep)
}

// kjFailure is job_failure.
func kjFailure(job pyjson.Object) string {
	for _, c := range kjConditions(job) {
		if (c.Value("type") == "Failed" || c.Value("type") == "FailureTarget") && c.Value("status") == "True" {
			return joinTruthy(" — ", c.Value("reason"), c.Value("message"))
		}
	}
	return ""
}

func kjPods(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) ([]pyjson.Object, error) {
	v, err := kube.Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+ns+"/pods?labelSelector="+kube.Quote("job-name="+name), nil, false)
	if err != nil {
		return nil, err
	}
	return objectList(pyjson.Obj(v).Value("items")), nil
}

// kjEvents is job_events.
func kjEvents(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) ([][2]string, error) {
	v, err := kube.Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+ns+"/events?fieldSelector="+kube.Quote("involvedObject.name="+name),
		nil, false)
	if err != nil {
		return nil, err
	}
	var out [][2]string
	for _, e := range objectList(pyjson.Obj(v).Value("items")) {
		out = append(out, [2]string{textOrEmpty(e.Value("reason")), textOrEmpty(e.Value("message"))})
	}
	return out, nil
}

// kjRefused is refused.
func kjRefused(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) string {
	events, err := kjEvents(ctx, k, ns, name)
	if err != nil {
		return ""
	}
	for i := len(events) - 1; i >= 0; i-- {
		if events[i][0] == "FailedCreate" {
			if strings.Contains(events[i][1], "exceeded quota") {
				return fmt.Sprintf("the namespace %s has no room left (its ResourceQuota is full): %s", ns, runes(events[i][1], 300))
			}
			return "the cluster refused to create its Pod: " + runes(events[i][1], 300)
		}
	}
	return ""
}

// kjLogUnavailable is LogUnavailable.
type kjLogUnavailable struct {
	err      error
	exitCode any
	reason   string
}

func (e *kjLogUnavailable) Error() string { return e.err.Error() }
func (e *kjLogUnavailable) Unwrap() error { return e.err }

// kjStoppedByCluster is stopped_by_cluster.
func kjStoppedByCluster(pod pyjson.Object) string {
	status := pyjson.Obj(pod.Value("status"))
	if status.Value("reason") == "Evicted" {
		return "Evicted"
	}
	for _, c := range objectList(status.Value("conditions")) {
		if c.Value("type") == "DisruptionTarget" && c.Value("status") == "True" {
			return "DisruptionTarget"
		}
	}
	return ""
}

// kjPodWaitingReason is pod_waiting_reason.
func kjPodWaitingReason(pod pyjson.Object) string {
	for _, s := range objectList(pyjson.Obj(pod.Value("status")).Value("containerStatuses")) {
		waiting := pyjson.Obj(pyjson.Obj(s.Value("state")).Value("waiting"))
		if len(waiting) > 0 {
			return joinTruthy(" — ", waiting.Value("reason"), waiting.Value("message"))
		}
	}
	return ""
}

// kjTail is tail: the last `max` bytes of the text.
func kjTail(text string, max int) string {
	if len(text) <= max {
		return text
	}
	return "…\n" + strings.ToValidUTF8(text[len(text)-max:], "")
}

// kjResult is job_result: (the end of the log of the last Pod, its exit code, the runtime's reason).
func kjResult(ctx context.Context, k scriptcfg.Kubernetes, ns, name string, maxBytes int) (string, any, string, error) {
	items, err := kjPods(ctx, k, ns, name)
	if err != nil {
		return "", nil, "", err
	}
	if len(items) == 0 {
		return "", nil, "", nil
	}
	sort.SliceStable(items, func(i, j int) bool {
		return textOrEmpty(pyjson.Obj(items[i].Value("metadata")).Value("creationTimestamp")) <
			textOrEmpty(pyjson.Obj(items[j].Value("metadata")).Value("creationTimestamp"))
	})
	pod := items[len(items)-1]
	var exitCode any
	reason := ""
	for _, st := range objectList(pyjson.Obj(pod.Value("status")).Value("containerStatuses")) {
		terminated := pyjson.Obj(pyjson.Obj(st.Value("state")).Value("terminated"))
		if code, has := terminated.Get("exitCode"); has {
			exitCode, reason = code, textOrEmpty(terminated.Value("reason"))
		}
	}
	if stopped := kjStoppedByCluster(pod); stopped != "" {
		reason = stopped
	}
	podName := pyjson.Str(pyjson.Obj(pod.Value("metadata")).Value("name"))
	v, err := kube.Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+ns+"/pods/"+podName+"/log?tailLines=5000", nil, true)
	if err != nil {
		if s := kjStatus(err); s == 400 || s == 404 {
			return kjPodWaitingReason(pod), exitCode, reason, nil
		}
		return "", nil, "", &kjLogUnavailable{err, exitCode, reason}
	}
	return kjTail(v.(string), maxBytes), exitCode, reason, nil
}

const kjStoppedByClusterText = "the cluster stopped its Pod before the end (a node drained or short of resources, " +
	"a preemption): run it again, or give the node Retries"

// kjExplainExit is explain_exit.
func kjExplainExit(exitCode any, reason, memoryLimit string) string {
	limit := ""
	if memoryLimit != "" {
		limit = " (" + memoryLimit + ")"
	}
	code := pyjson.Str(exitCode)
	switch {
	case reason == "Evicted" || reason == "DisruptionTarget":
		return kjStoppedByClusterText
	case reason == "OOMKilled":
		return fmt.Sprintf("it ran out of memory: it went over its memory limit%s and the cluster stopped it (exit code %s)", limit, code)
	case pyjson.Equal(exitCode, int64(137)):
		return fmt.Sprintf("it was killed (exit code 137): most often because it went over its memory limit%s", limit)
	case pyjson.Equal(exitCode, int64(143)):
		return "it was stopped (SIGTERM, exit code 143): most often the cluster stopped its Pod (a node drained)"
	}
	return "it ended with exit code " + code
}

var (
	kjPullFailures   = map[string]bool{"ErrImagePull": true, "ImagePullBackOff": true, "InvalidImageName": true, "ErrImageNeverPull": true}
	kjCreateFailures = map[string]bool{"CreateContainerError": true, "CreateContainerConfigError": true}
	kjDigest         = regexp.MustCompile(`(?i)sha256:[0-9a-f]{64}`)
	kjPullReasons    = []struct {
		re  *regexp.Regexp
		why string
	}{
		{regexp.MustCompile(`(?i)x509|certificate`), "the nodes do not trust the registry's certificate: its authority must be installed on the nodes"},
		{regexp.MustCompile(`(?i)HTTP response to HTTPS client`), "the registry answers in plain HTTP and the nodes expect HTTPS: the nodes must allow that registry over HTTP"},
		{regexp.MustCompile(`(?i)\b(401|403)\b|unauthorized|denied|no basic auth|authorization failed|authentication required`),
			"the registry refused the cluster's credentials: the pull Secret is missing, wrong or expired"},
		{regexp.MustCompile(`(?i)no match for platform|no matching manifest`), "the image has no version for the nodes' architecture (built for arm64 only, or amd64 only)"},
		{regexp.MustCompile(`(?i)manifest unknown|not found`), "the image is not in the registry (any more)"},
		{regexp.MustCompile(`(?i)no such host|i/o timeout|connection refused|network is unreachable|dial tcp`), "the nodes cannot reach the registry"},
	}
)

// kjExplainPull is explain_pull.
func kjExplainPull(detail string) string {
	said := kjDigest.ReplaceAllString(detail, "sha256:…")
	for _, p := range kjPullReasons {
		if p.re.MatchString(said) {
			return fmt.Sprintf("the cluster could not download the image: %s (%s)", p.why, runes(pyStripText(detail), 300))
		}
	}
	d := runes(pyStripText(detail), 300)
	if d == "" {
		d = "no reason given"
	}
	return "the cluster could not download the image: " + d
}

// kjStartState is start_state: (why it cannot start, why it is not scheduled, whether it ran).
func kjStartState(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) (string, string, bool, error) {
	pods, err := kjPods(ctx, k, ns, name)
	if err != nil {
		return "", "", false, err
	}
	unscheduled, ran := "", false
	for _, pod := range pods {
		status := pyjson.Obj(pod.Value("status"))
		for _, c := range objectList(status.Value("conditions")) {
			if c.Value("type") == "PodScheduled" && c.Value("status") == "False" {
				unscheduled = joinTruthy(" — ", c.Value("reason"), c.Value("message"))
			}
		}
		for _, st := range objectList(status.Value("containerStatuses")) {
			state := pyjson.Obj(st.Value("state"))
			ran = ran || pyjson.Truthy(state.Value("running")) || pyjson.Truthy(state.Value("terminated"))
			waiting := pyjson.Obj(state.Value("waiting"))
			r, _ := waiting.Value("reason").(string)
			if kjCreateFailures[r] {
				return runes("the cluster could not start its container: "+pyjson.Str(pyjson.Or(waiting.Value("message"), r)), 400),
					unscheduled, ran, nil
			}
			if !kjPullFailures[r] {
				continue
			}
			detail := textOrEmpty(waiting.Value("message"))
			if r == "ImagePullBackOff" || detail == "" {
				events, err := kjEvents(ctx, k, ns, pyjson.Str(pyjson.Obj(pod.Value("metadata")).Value("name")))
				if err != nil {
					return "", "", false, err
				}
				var said []string
				seen := map[string]bool{}
				for _, e := range events {
					if (e[0] == "Failed" || e[0] == "FailedToRetrieveImagePullSecret") && !strings.Contains(e[1], "Back-off") &&
						e[1] != "Error: ImagePullBackOff" && !seen[e[1]] {
						seen[e[1]] = true
						said = append(said, e[1])
					}
				}
				if len(said) > 0 {
					detail = strings.Join(said, " · ")
				}
			}
			return kjExplainPull(detail), unscheduled, ran, nil
		}
	}
	return "", unscheduled, ran, nil
}

// transient is script_runs.transient: an answer worth asking again.
func transient(err error) bool {
	s := kjStatus(err)
	return s == 0 || s == 429 || s >= 500
}

const (
	noPodSeconds = 10 // NO_POD_SECONDS
	// PULL_FAILURE_SECONDS is pullFailureSeconds (registries_cluster.go)
	logRetrySeconds = 30  // LOG_RETRY_SECONDS
	jobGraceSeconds = 180 // GRACE_SECONDS
)

// kjAttempt is script_runs._attempt: one Job, (succeeded, log, exit code, the reason it failed).
// `code` nil: no ConfigMap (a Container node).
func kjAttempt(ctx context.Context, k scriptcfg.Kubernetes, name string, manifest pyjson.Object, code pyjson.Object, secret pyjson.Object,
	timeout int64, ns string, pollSeconds float64, logMax int) (bool, string, any, string, error) {
	labels := pyjson.Obj(pyjson.Obj(manifest.Value("metadata")).Value("labels"))
	pod := pyjson.Obj(pyjson.Obj(pyjson.Obj(manifest.Value("spec")).Value("template")).Value("spec"))
	container := objectList(pod.Value("containers"))[0]
	codeName := ""
	if code != nil {
		codeName = pyjson.Str(pyjson.Obj(pyjson.Obj(objectList(pod.Value("volumes"))[0]).Value("configMap")).Value("name"))
	}
	secretName := ""
	if from := objectList(container.Value("envFrom")); len(from) > 0 {
		secretName = pyjson.Str(pyjson.Obj(from[0].Value("secretRef")).Value("name"))
	}
	job, err := kjCreateJob(ctx, k, ns, manifest)
	if err != nil {
		return false, "", nil, "", err
	}
	started := time.Now()
	deadline := started.Add(time.Duration(timeout+jobGraceSeconds) * time.Second)
	var pullSince, logSince time.Time
	next := waitsOf(pollSeconds)
	defer func() {
		if err := kjDeleteJob(context.Background(), k, ns, name); err != nil {
			jobsLog("Job not deleted; its TTL will", name, err)
		}
	}()
	owners := kjOwnedBy(job)
	if codeName != "" {
		if err := kjCreateObject(ctx, k, ns, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
			{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "labels", Value: labels}, {Key: "ownerReferences", Value: owners}}},
			{Key: "data", Value: code}}); err != nil {
			return false, "", nil, "", err
		}
	}
	if secretName != "" {
		if err := kjCreateObject(ctx, k, ns, "secrets", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"},
			{Key: "type", Value: "Opaque"}, {Key: "metadata", Value: pyjson.Object{{Key: "name", Value: secretName}, {Key: "labels", Value: labels},
				{Key: "ownerReferences", Value: owners}}}, {Key: "stringData", Value: secret}}); err != nil {
			return false, "", nil, "", err
		}
	}
	unscheduled, ran := "", false
	for {
		if !jobs.Sleep(ctx, jobs.Seconds(next())) {
			return false, "", nil, "", ctx.Err()
		}
		now := time.Now()
		done, ok, log, exit, reason, err := func() (bool, bool, string, any, string, error) {
			current, err := kjGetJob(ctx, k, ns, name)
			if err != nil {
				return false, false, "", nil, "", err
			}
			if current == nil {
				return true, false, "", nil, fmt.Sprintf("the Job %s is gone from the cluster before it ended", name), nil
			}
			outcome := kjOutcome(current)
			status := pyjson.Obj(current.Value("status"))
			any4 := false
			for _, key := range []string{"active", "ready", "succeeded", "failed"} {
				any4 = any4 || pyjson.Truthy(status.Value(key))
			}
			if outcome == "" && !any4 && now.Sub(started).Seconds() > noPodSeconds {
				if why := kjRefused(ctx, k, ns, name); why != "" {
					return true, false, "", nil, why, nil
				}
			}
			if outcome == "" && pyjson.Truthy(status.Value("active")) {
				why, waiting, hasRun, err := kjStartState(ctx, k, ns, name)
				if err != nil {
					return false, false, "", nil, "", err
				}
				if waiting != "" {
					unscheduled = waiting
				}
				ran = ran || hasRun
				switch {
				case why == "":
					pullSince = time.Time{}
				case pullSince.IsZero():
					pullSince = now
				case now.Sub(pullSince).Seconds() >= pullFailureSeconds:
					return true, false, "", nil, why, nil
				}
			}
			if outcome == "" {
				return false, false, "", nil, "", nil
			}
			log, exitCode, ended, err := kjResult(ctx, k, ns, name, logMax)
			var lu *kjLogUnavailable
			if errors.As(err, &lu) {
				if !transient(lu.err) {
					return false, false, "", nil, "", err
				}
				if logSince.IsZero() {
					logSince = now
				}
				if now.Sub(logSince).Seconds() < logRetrySeconds {
					return false, false, "", nil, "", nil
				}
				log, exitCode, ended = fmt.Sprintf("(the cluster did not give the log: %s)", lu.err), lu.exitCode, lu.reason
			} else if err != nil {
				return false, false, "", nil, "", err
			}
			if outcome == "succeeded" {
				return true, true, log, exitCode, "", nil
			}
			reason := kjFailure(current)
			switch {
			case strings.Contains(reason, "DeadlineExceeded"):
				if unscheduled != "" && !ran {
					reason = fmt.Sprintf("its Pod never started within its timeout (%d s): %s", timeout, unscheduled)
				} else {
					reason = fmt.Sprintf("it ran longer than its timeout (%d s)", timeout)
				}
			case exitCode != nil || ended != "":
				reason = kjExplainExit(exitCode, ended, kjMemoryLimit(container))
			case strings.Contains(reason, "BackoffLimitExceeded") && ran:
				reason = kjStoppedByClusterText
			}
			if reason == "" {
				reason = "the Job failed"
			}
			return true, false, log, exitCode, reason, nil
		}()
		if err != nil {
			var ke *kube.Error
			if errors.As(err, &ke) && transient(err) && !now.After(deadline) {
				continue // one answer of the API that failed is not the Job failing
			}
			return false, "", nil, "", err
		}
		if done {
			return ok, log, exit, reason, nil
		}
		if now.After(deadline) {
			log, exitCode, _, err := kjResult(ctx, k, ns, name, logMax)
			if err != nil {
				log, exitCode = fmt.Sprintf("(the cluster did not give the log: %s)", err), nil
				var lu *kjLogUnavailable
				if errors.As(err, &lu) {
					exitCode = lu.exitCode
				}
			}
			return false, log, exitCode, "the cluster did not end the Job within its timeout", nil
		}
	}
}

func kjMemoryLimit(container pyjson.Object) string {
	return textOrEmpty(pyjson.Obj(pyjson.Obj(container.Value("resources")).Value("limits")).Value("memory"))
}

func kjDeleteJob(ctx context.Context, k scriptcfg.Kubernetes, ns, name string) error {
	return kube.DeleteJob(ctx, k, ns, name)
}

// jobsLog is a warning of the background work with no Deps at hand.
var jobsLog = func(msg, name string, err error) {}
