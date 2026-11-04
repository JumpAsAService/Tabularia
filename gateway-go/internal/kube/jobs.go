// The rest of services/kube_jobs.py: a Job created, followed, its Pods and log read, the
// reasons it cannot start or why it ended, in words. The background loops of the scripting
// images (script_builds) and of the notebooks' pods (notebook_runtime) use them.

package kube

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// Dict is `(v or {})` of a JSON value: the object, or an empty one (nil) for anything else.
func Dict(v any) pyjson.Object {
	o, _ := v.(pyjson.Object)
	return o
}

// List is `(v or [])` of a JSON value.
func List(v any) []any {
	l, _ := v.([]any)
	return l
}

// Text is a value of the API's JSON used as a text in a message (str()); "" when falsy.
func Text(v any) string {
	if !pyjson.Truthy(v) {
		return ""
	}
	return pyjson.Str(v)
}

// joinTruthy is " — ".join(x for x in (a, b) if x).
func joinTruthy(vs ...any) string {
	var parts []string
	for _, v := range vs {
		if pyjson.Truthy(v) {
			parts = append(parts, pyjson.Str(v))
		}
	}
	return strings.Join(parts, " — ")
}

// CreateJob is kube_jobs.create_job: the Job as the cluster has it; one of the same name
// already there counts as created.
func CreateJob(ctx context.Context, k scriptcfg.Kubernetes, namespace string, manifest pyjson.Object) (any, error) {
	out, err := Call(ctx, k, http.MethodPost, jobs(namespace), manifest, false)
	if err == nil {
		return out, nil
	}
	if Status(err) != 409 {
		return nil, err
	}
	name := pyjson.Str(Dict(manifest.Value("metadata")).Value("name"))
	return Call(ctx, k, http.MethodGet, jobs(namespace)+"/"+name, nil, false)
}

// GetJob is kube_jobs.get_job: nil when the cluster does not have it.
func GetJob(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) (any, error) {
	out, err := Call(ctx, k, http.MethodGet, jobs(namespace)+"/"+name, nil, false)
	if Status(err) == 404 {
		return nil, nil
	}
	return out, err
}

// JobPods is kube_jobs.job_pods.
func JobPods(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) ([]any, error) {
	out, err := Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+namespace+"/pods?labelSelector="+Quote("job-name="+name), nil, false)
	if err != nil {
		return nil, err
	}
	return List(Dict(out).Value("items")), nil
}

// ListJobs is kube_jobs.list_jobs.
func ListJobs(ctx context.Context, k scriptcfg.Kubernetes, namespace, selector string) ([]any, error) {
	out, err := Call(ctx, k, http.MethodGet, jobs(namespace)+"?labelSelector="+Quote(selector), nil, false)
	if err != nil {
		return nil, err
	}
	return List(Dict(out).Value("items")), nil
}

// PodLog is kube_jobs.pod_log: "" when the container never started.
func PodLog(ctx context.Context, k scriptcfg.Kubernetes, namespace, pod, container string, tailLines int) (string, error) {
	out, err := Call(ctx, k, http.MethodGet, fmt.Sprintf("/api/v1/namespaces/%s/pods/%s/log?container=%s&tailLines=%d",
		namespace, pod, Quote(container), tailLines), nil, true)
	if s := Status(err); s == 400 || s == 404 {
		return "", nil
	} else if err != nil {
		return "", err
	}
	return out.(string), nil
}

// Event is one of kube_jobs.job_events.
type Event struct{ Reason, Message string }

// JobEvents is kube_jobs.job_events: what the cluster said about the object.
func JobEvents(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) ([]Event, error) {
	out, err := Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+namespace+"/events?fieldSelector="+
		Quote("involvedObject.name="+name), nil, false)
	if err != nil {
		return nil, err
	}
	var events []Event
	for _, e := range List(Dict(out).Value("items")) {
		o := Dict(e)
		events = append(events, Event{Text(o.Value("reason")), Text(o.Value("message"))})
	}
	return events, nil
}

// Refused is kube_jobs.refused: why the cluster does not create the Job's Pod, "" when it
// is not refusing (or its events cannot be read).
func Refused(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) (string, error) {
	events, err := JobEvents(ctx, k, namespace, name)
	var ke *Error
	if errors.As(err, &ke) {
		return "", nil
	} else if err != nil {
		return "", err
	}
	for i := len(events) - 1; i >= 0; i-- {
		e := events[i]
		if e.Reason == "FailedCreate" {
			if strings.Contains(e.Message, "exceeded quota") {
				return fmt.Sprintf("the namespace %s has no room left (its ResourceQuota is full): %s", namespace, pystr.Cut(e.Message, 300)), nil
			}
			return "the cluster refused to create its Pod: " + pystr.Cut(e.Message, 300), nil
		}
	}
	return "", nil
}

// HasPod is kube_jobs.has_pod: the Job has, or had, a Pod.
func HasPod(job any) bool {
	status := Dict(Dict(job).Value("status"))
	for _, k := range []string{"active", "ready", "succeeded", "failed"} {
		if pyjson.Truthy(status.Value(k)) {
			return true
		}
	}
	return false
}

// JobOutcome is kube_jobs.job_outcome: "succeeded", "failed" or "" while it runs.
func JobOutcome(job any) string {
	status := Dict(Dict(job).Value("status"))
	for _, c := range List(status.Value("conditions")) {
		o := Dict(c)
		if o.Value("status") == "True" && (o.Value("type") == "Complete" || o.Value("type") == "SuccessCriteriaMet") {
			return "succeeded"
		}
		if o.Value("status") == "True" && (o.Value("type") == "Failed" || o.Value("type") == "FailureTarget") {
			return "failed"
		}
	}
	if pyjson.Truthy(status.Value("succeeded")) {
		return "succeeded"
	}
	return ""
}

// JobFailure is kube_jobs.job_failure: why the Job failed, in the cluster's words.
func JobFailure(job any) string {
	for _, c := range List(Dict(Dict(job).Value("status")).Value("conditions")) {
		o := Dict(c)
		if (o.Value("type") == "Failed" || o.Value("type") == "FailureTarget") && o.Value("status") == "True" {
			return joinTruthy(o.Value("reason"), o.Value("message"))
		}
	}
	return ""
}

// LogUnavailable is kube_jobs.LogUnavailable: the Pod's log could not be read; what its
// status said of the end comes with it.
type LogUnavailable struct {
	Kube     Error
	ExitCode any // None when the container never ended
	Reason   string
}

// Unwrap makes it a KubeError too (errors.As), as LogUnavailable subclasses it.
func (l *LogUnavailable) Unwrap() error { return &l.Kube }

func (l *LogUnavailable) Error() string { return l.Kube.Message }

// SortByCreation orders the objects by metadata.creationTimestamp, as `sorted(…, key=…)`.
func SortByCreation(items []any) []any {
	out := append([]any(nil), items...)
	key := func(v any) string { return Text(Dict(Dict(v).Value("metadata")).Value("creationTimestamp")) }
	sort.SliceStable(out, func(i, j int) bool { return key(out[i]) < key(out[j]) })
	return out
}

// JobResult is kube_jobs.job_result: the end of the log of the Job's last Pod, its exit
// code (nil when the container never ended, or there is no Pod) and the runtime's reason
// for the end. A log that cannot be read is a *LogUnavailable.
func JobResult(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string, maxBytes int) (string, any, string, error) {
	pods, err := JobPods(ctx, k, namespace, name)
	if err != nil {
		return "", nil, "", err
	}
	items := SortByCreation(pods)
	if len(items) == 0 {
		return "", nil, "", nil
	}
	pod := items[len(items)-1]
	var exitCode any
	reason := ""
	for _, st := range List(Dict(Dict(pod).Value("status")).Value("containerStatuses")) {
		terminated := Dict(Dict(Dict(st).Value("state")).Value("terminated"))
		if code, ok := terminated.Get("exitCode"); ok {
			exitCode, reason = code, Text(terminated.Value("reason"))
		}
	}
	if stopped := StoppedByCluster(pod); stopped != "" {
		reason = stopped
	}
	podName := pyjson.Str(Dict(Dict(pod).Value("metadata")).Value("name"))
	out, err := Call(ctx, k, http.MethodGet, "/api/v1/namespaces/"+namespace+"/pods/"+podName+"/log?tailLines=5000", nil, true)
	if err != nil {
		var ke *Error
		if !errors.As(err, &ke) {
			return "", nil, "", err
		}
		if ke.Status == 400 || ke.Status == 404 { // the container never started
			return PodWaitingReason(pod), exitCode, reason, nil
		}
		return "", nil, "", &LogUnavailable{Kube: *ke, ExitCode: exitCode, Reason: reason}
	}
	return Tail(out.(string), maxBytes), exitCode, reason, nil
}

// StoppedByCluster is kube_jobs.stopped_by_cluster: "Evicted" or "DisruptionTarget" when
// the cluster took the Pod away, "" otherwise.
func StoppedByCluster(pod any) string {
	status := Dict(Dict(pod).Value("status"))
	if status.Value("reason") == "Evicted" {
		return "Evicted"
	}
	for _, c := range List(status.Value("conditions")) {
		o := Dict(c)
		if o.Value("type") == "DisruptionTarget" && o.Value("status") == "True" {
			return "DisruptionTarget"
		}
	}
	return ""
}

// StoppedByClusterText is kube_jobs.STOPPED_BY_CLUSTER.
const StoppedByClusterText = "the cluster stopped its Pod before the end (a node drained or short of resources, " +
	"a preemption): run it again, or give the node Retries"

// isNumber is `v == n` of a JSON value (137.0 == 137 too, never a boolean).
func isNumber(v any, n int64) bool {
	switch x := v.(type) {
	case int64:
		return x == n
	case float64:
		return x == float64(n)
	}
	return false
}

// ExplainExit is kube_jobs.explain_exit: why a container ended badly.
func ExplainExit(exitCode any, reason, memoryLimit string) string {
	limit := ""
	if memoryLimit != "" {
		limit = " (" + memoryLimit + ")"
	}
	code := pyjson.Str(exitCode)
	switch {
	case reason == "Evicted" || reason == "DisruptionTarget":
		return StoppedByClusterText
	case reason == "OOMKilled":
		return fmt.Sprintf("it ran out of memory: it went over its memory limit%s and the cluster stopped it (exit code %s)", limit, code)
	case isNumber(exitCode, 137):
		return fmt.Sprintf("it was killed (exit code 137): most often because it went over its memory limit%s", limit)
	case isNumber(exitCode, 143):
		return "it was stopped (SIGTERM, exit code 143): most often the cluster stopped its Pod (a node drained)"
	}
	return "it ended with exit code " + code
}

var (
	pullFailures   = map[string]bool{"ErrImagePull": true, "ImagePullBackOff": true, "InvalidImageName": true, "ErrImageNeverPull": true}
	createFailures = map[string]bool{"CreateContainerError": true, "CreateContainerConfigError": true}
	pullReasons    = []struct {
		re  *regexp.Regexp
		why string
	}{
		{regexp.MustCompile(`(?i)x509|certificate`),
			"the nodes do not trust the registry's certificate: its authority must be installed on the nodes"},
		{regexp.MustCompile(`(?i)HTTP response to HTTPS client`),
			"the registry answers in plain HTTP and the nodes expect HTTPS: the nodes must allow that registry over HTTP"},
		{regexp.MustCompile(`(?i)\b(401|403)\b|unauthorized|denied|no basic auth|authorization failed|authentication required`),
			"the registry refused the cluster's credentials: the pull Secret is missing, wrong or expired"},
		{regexp.MustCompile(`(?i)no match for platform|no matching manifest`),
			"the image has no version for the nodes' architecture (built for arm64 only, or amd64 only)"},
		{regexp.MustCompile(`(?i)manifest unknown|not found`),
			"the image is not in the registry (any more)"},
		{regexp.MustCompile(`(?i)no such host|i/o timeout|connection refused|network is unreachable|dial tcp`),
			"the nodes cannot reach the registry"},
	}
	digestRe = regexp.MustCompile(`(?i)sha256:[0-9a-f]{64}`)
)

// ExplainPull is kube_jobs.explain_pull.
func ExplainPull(detail string) string {
	said := digestRe.ReplaceAllString(detail, "sha256:…")
	shown := pystr.Cut(pystr.Strip(detail), 300)
	for _, p := range pullReasons {
		if p.re.MatchString(said) {
			return fmt.Sprintf("the cluster could not download the image: %s (%s)", p.why, shown)
		}
	}
	if shown == "" {
		shown = "no reason given"
	}
	return "the cluster could not download the image: " + shown
}

// PullFailure is kube_jobs.pull_failure.
func PullFailure(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) (string, error) {
	cannot, _, _, err := StartState(ctx, k, namespace, name)
	return cannot, err
}

// StartState is kube_jobs.start_state: (why the Job's Pod cannot start, why it is not
// scheduled yet, whether its container has run).
func StartState(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) (string, string, bool, error) {
	pods, err := JobPods(ctx, k, namespace, name)
	if err != nil {
		return "", "", false, err
	}
	unscheduled, ran := "", false
	for _, pod := range pods {
		status := Dict(Dict(pod).Value("status"))
		for _, c := range List(status.Value("conditions")) {
			o := Dict(c)
			if o.Value("type") == "PodScheduled" && o.Value("status") == "False" {
				unscheduled = joinTruthy(o.Value("reason"), o.Value("message"))
			}
		}
		for _, st := range List(status.Value("containerStatuses")) {
			state := Dict(Dict(st).Value("state"))
			ran = ran || pyjson.Truthy(state.Value("running")) || pyjson.Truthy(state.Value("terminated"))
			waiting := Dict(state.Value("waiting"))
			why, _ := waiting.Value("reason").(string)
			if createFailures[why] {
				msg := Text(waiting.Value("message"))
				if msg == "" {
					msg = why
				}
				return pystr.Cut("the cluster could not start its container: "+msg, 400), unscheduled, ran, nil
			}
			if !pullFailures[why] {
				continue
			}
			detail := Text(waiting.Value("message"))
			if why == "ImagePullBackOff" || detail == "" {
				events, err := JobEvents(ctx, k, namespace, pyjson.Str(Dict(Dict(pod).Value("metadata")).Value("name")))
				if err != nil {
					return "", "", false, err
				}
				var said []string
				seen := map[string]bool{}
				for _, e := range events {
					if (e.Reason == "Failed" || e.Reason == "FailedToRetrieveImagePullSecret") &&
						!strings.Contains(e.Message, "Back-off") && e.Message != "Error: ImagePullBackOff" && !seen[e.Message] {
						seen[e.Message] = true
						said = append(said, e.Message)
					}
				}
				if joined := strings.Join(said, " · "); joined != "" {
					detail = joined
				}
			}
			return ExplainPull(detail), unscheduled, ran, nil
		}
	}
	return "", unscheduled, ran, nil
}

// PodWaitingReason is kube_jobs.pod_waiting_reason.
func PodWaitingReason(pod any) string {
	for _, s := range List(Dict(Dict(pod).Value("status")).Value("containerStatuses")) {
		waiting := Dict(Dict(Dict(s).Value("state")).Value("waiting"))
		if len(waiting) > 0 {
			return joinTruthy(waiting.Value("reason"), waiting.Value("message"))
		}
	}
	return ""
}

// Tail is kube_jobs.tail: the last max_bytes of the text, «…» before when cut (a character
// cut in half is left out, as decode(errors="ignore") does).
func Tail(text string, maxBytes int) string {
	if len(text) <= maxBytes {
		return text
	}
	data := text[len(text)-maxBytes:]
	var b strings.Builder
	for len(data) > 0 {
		r, size := utf8.DecodeRuneInString(data)
		if r != utf8.RuneError || size > 1 {
			b.WriteString(data[:size])
		}
		data = data[size:]
	}
	return "…\n" + b.String()
}

// OwnedBy is kube_jobs.owned_by: the ownerReferences that make an object go with its Job.
func OwnedBy(job any) []any {
	meta := Dict(Dict(job).Value("metadata"))
	return []any{pyjson.Object{{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"}, {Key: "name", Value: meta.Value("name")},
		{Key: "uid", Value: meta.Value("uid")}}}
}

// CreateObject is kube_jobs.create_object: a ConfigMap or a Secret ("configmaps",
// "secrets"); one of the same name already there is fine.
func CreateObject(ctx context.Context, k scriptcfg.Kubernetes, namespace, kind string, manifest pyjson.Object) error {
	_, err := Call(ctx, k, http.MethodPost, "/api/v1/namespaces/"+namespace+"/"+kind, manifest, false)
	if Status(err) == 409 {
		return nil
	}
	return err
}
