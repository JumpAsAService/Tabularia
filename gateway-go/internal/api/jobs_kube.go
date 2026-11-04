package api

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// services/kube_jobs.py, the part a Script or Container node's run uses (script_runs._attempt):
// a Job created with its ConfigMap and Secret, followed to its end, its log read, deleted.

func kjStatus(err error) int { return kube.Status(err) }

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
	job, err := kube.CreateJob(ctx, k, ns, manifest)
	if err != nil {
		return false, "", nil, "", err
	}
	started := time.Now()
	deadline := started.Add(time.Duration(timeout+jobGraceSeconds) * time.Second)
	var pullSince, logSince time.Time
	next := waitsOf(pollSeconds)
	defer func() {
		if err := kube.DeleteJob(context.Background(), k, ns, name); err != nil {
			jobsLog("Job not deleted; its TTL will", name, err)
		}
	}()
	owners := kube.OwnedBy(job)
	if codeName != "" {
		if err := kube.CreateObject(ctx, k, ns, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
			{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "labels", Value: labels}, {Key: "ownerReferences", Value: owners}}},
			{Key: "data", Value: code}}); err != nil {
			return false, "", nil, "", err
		}
	}
	if secretName != "" {
		if err := kube.CreateObject(ctx, k, ns, "secrets", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"},
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
			current, err := kube.GetJob(ctx, k, ns, name)
			if err != nil {
				return false, false, "", nil, "", err
			}
			if current == nil {
				return true, false, "", nil, fmt.Sprintf("the Job %s is gone from the cluster before it ended", name), nil
			}
			outcome := kube.JobOutcome(current)
			status := kube.Dict(kube.Dict(current).Value("status"))
			any4 := false
			for _, key := range []string{"active", "ready", "succeeded", "failed"} {
				any4 = any4 || pyjson.Truthy(status.Value(key))
			}
			if outcome == "" && !any4 && now.Sub(started).Seconds() > noPodSeconds {
				if why, _ := kube.Refused(ctx, k, ns, name); why != "" {
					return true, false, "", nil, why, nil
				}
			}
			if outcome == "" && pyjson.Truthy(status.Value("active")) {
				why, waiting, hasRun, err := kube.StartState(ctx, k, ns, name)
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
			log, exitCode, ended, err := kube.JobResult(ctx, k, ns, name, logMax)
			var lu *kube.LogUnavailable
			if errors.As(err, &lu) {
				if !transient(&lu.Kube) {
					return false, false, "", nil, "", err
				}
				if logSince.IsZero() {
					logSince = now
				}
				if now.Sub(logSince).Seconds() < logRetrySeconds {
					return false, false, "", nil, "", nil
				}
				log, exitCode, ended = fmt.Sprintf("(the cluster did not give the log: %s)", lu.Error()), lu.ExitCode, lu.Reason
			} else if err != nil {
				return false, false, "", nil, "", err
			}
			if outcome == "succeeded" {
				return true, true, log, exitCode, "", nil
			}
			reason := kube.JobFailure(current)
			switch {
			case strings.Contains(reason, "DeadlineExceeded"):
				if unscheduled != "" && !ran {
					reason = fmt.Sprintf("its Pod never started within its timeout (%d s): %s", timeout, unscheduled)
				} else {
					reason = fmt.Sprintf("it ran longer than its timeout (%d s)", timeout)
				}
			case exitCode != nil || ended != "":
				reason = kube.ExplainExit(exitCode, ended, kjMemoryLimit(container))
			case strings.Contains(reason, "BackoffLimitExceeded") && ran:
				reason = kube.StoppedByClusterText
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
			log, exitCode, _, err := kube.JobResult(ctx, k, ns, name, logMax)
			if err != nil {
				log, exitCode = fmt.Sprintf("(the cluster did not give the log: %s)", err), nil
				var lu *kube.LogUnavailable
				if errors.As(err, &lu) {
					exitCode = lu.ExitCode
				}
			}
			return false, log, exitCode, "the cluster did not end the Job within its timeout", nil
		}
	}
}

func kjMemoryLimit(container pyjson.Object) string {
	return textOrEmpty(pyjson.Obj(pyjson.Obj(container.Value("resources")).Value("limits")).Value("memory"))
}

// jobsLog is a warning of the background work with no Deps at hand.
var jobsLog = func(msg, name string, err error) {}
