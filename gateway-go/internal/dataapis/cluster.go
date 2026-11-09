package dataapis

// The Kubernetes objects of a data API (docs/specs/data-apis.md, «Runtime»), as the
// orchestrator's reconcile loop applies them in DATA_API__NAMESPACE: a Deployment of the pods,
// a Service only the ingress controller reaches, a Secret with the pod token, a
// PodDisruptionBudget when it has more than one pod, an HPA when it autoscales, an Ingress with
// /data/{slug}/ only, and a NetworkPolicy. Every one is labelled with the release and the data
// API's id, so the loop finds the ones it must remove. Pure functions: what is applied is
// decided here, and tested without a cluster.

import (
	"math/bits"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The labels and annotations of the objects.
const (
	LabelID        = "tabularia.org/data-api"
	LabelComponent = "app.kubernetes.io/component"
	LabelInstance  = "app.kubernetes.io/instance"
	LabelName      = "app.kubernetes.io/name"
	LabelManagedBy = "app.kubernetes.io/managed-by"
	Component      = "data-api"
	// on the pods: the pod token's fingerprint (a new one restarts them) and the disk
	AnnotationToken = "tabularia.org/pod-token"
	// FieldManager owns the fields the loop applies
	FieldManager = "tabularia-data-apis"
	// the key of the Secret that holds the pod token
	SecretKey = "pod-token"
	// the pods' port, and the paths Kubernetes asks (never published by the Ingress)
	PodPort = 8000
)

// The disk of the data files: the smallest a pod gets, and what is added to the parquets'
// need for the program, the logs and DuckDB's own files.
const (
	MinDisk    = int64(1) << 30
	DiskMargin = int64(256) << 20
	// a version change holds the file served, the new parquet downloaded, the new file and
	// what DuckDB spills while sorting: measured on kind (42 million rows, a parquet of 465 MB,
	// a pod of 1Gi) at five times the parquet at its highest (docs/specs/data-apis.md, «Runtime»)
	DiskFactor = 6
)

// Name is the name of every object of the data API.
func Name(release string, id int64) string {
	suffix := "-data-api-" + strconv.FormatInt(id, 10)
	if len(release)+len(suffix) > 63 {
		release = release[:63-len(suffix)]
	}
	return release + suffix
}

// Labels are what every object of the data API carries.
func Labels(release string, id int64) map[string]any {
	return map[string]any{LabelName: "tabularia-data-api", LabelInstance: release, LabelComponent: Component,
		LabelManagedBy: "tabularia", LabelID: strconv.FormatInt(id, 10)}
}

// Selector picks every object of every data API of the release.
func Selector(release string) string {
	return LabelInstance + "=" + release + "," + LabelComponent + "=" + Component
}

func podLabels(release string, id int64) map[string]any {
	return map[string]any{LabelInstance: release, LabelComponent: Component, LabelID: strconv.FormatInt(id, 10)}
}

// Disk is what a pod of a data API gets for its data files: DiskFactor times its parquets,
// plus DiskMargin, rounded up to a power of two (so a refresh that grows the data seldom
// changes it, and a change restarts the pods), at least MinDisk, at most `maxDisk`.
func Disk(parquetBytes []int64, maxDisk int64) int64 {
	var need int64 = DiskMargin
	for _, b := range parquetBytes {
		need += DiskFactor * max(b, 0)
	}
	size := MinDisk
	if need > size {
		size = int64(1) << (64 - bits.LeadingZeros64(uint64(need-1)))
	}
	return min(size, max(maxDisk, MinDisk))
}

// Quantity writes bytes as a Kubernetes quantity: Gi or Mi when it is a whole number of them.
func Quantity(b int64) string {
	switch {
	case b%(1<<30) == 0:
		return strconv.FormatInt(b>>30, 10) + "Gi"
	case b%(1<<20) == 0:
		return strconv.FormatInt(b>>20, 10) + "Mi"
	}
	return strconv.FormatInt(b, 10)
}

// Want is what the objects of one data API are made from.
type Want struct {
	API *API
	// the sha-256 of the pod token the Secret holds (data_apis.pod_token_hash)
	TokenHash string
	// the disk of each pod, in bytes (Disk)
	Disk int64
	// DATA_API__POLL_SECONDS
	PollSeconds int
}

// Serving says whether the pods run: an active data API. A paused one keeps its objects with
// no pods.
func (w Want) Serving() bool { return w.API.State == Active }

func meta(c config.DataAPIPods, id int64) map[string]any {
	return map[string]any{"name": Name(c.Release, id), "namespace": c.Namespace, "labels": Labels(c.Release, id)}
}

// Secret holds the pod token.
func Secret(c config.DataAPIPods, id int64, token string) map[string]any {
	return map[string]any{"apiVersion": "v1", "kind": "Secret", "metadata": meta(c, id), "type": "Opaque",
		"stringData": map[string]any{SecretKey: token}}
}

// Service is the pods' address inside the cluster: the Ingress's backend.
func Service(c config.DataAPIPods, id int64) map[string]any {
	return map[string]any{"apiVersion": "v1", "kind": "Service", "metadata": meta(c, id), "spec": map[string]any{
		"type": "ClusterIP", "selector": podLabels(c.Release, id),
		"ports": []any{map[string]any{"name": "http", "port": PodPort, "targetPort": "http", "protocol": "TCP"}}}}
}

// Deployment runs the pods: non-root, read-only root, no capabilities, the runtime's CPU and
// memory, the data files in an emptyDir of `w.Disk`; zero of them while paused, the HPA's
// count while autoscaling.
func Deployment(c config.DataAPIPods, w Want) map[string]any {
	a := w.API
	rt := a.Runtime
	disk := Quantity(w.Disk)
	resources := map[string]any{
		"requests": map[string]any{"cpu": rt.CPU, "memory": rt.Memory, "ephemeral-storage": disk},
		// the logs and the writable layer beside the data files
		"limits": map[string]any{"cpu": rt.CPU, "memory": rt.Memory, "ephemeral-storage": Quantity(w.Disk + DiskMargin)},
	}
	env := []any{
		map[string]any{"name": "DATA_API__ID", "value": strconv.FormatInt(a.ID, 10)},
		map[string]any{"name": "DATA_API__GATEWAY_URL", "value": c.GatewayURL},
		map[string]any{"name": "DATA_API__POD_TOKEN", "valueFrom": map[string]any{"secretKeyRef": map[string]any{
			"name": Name(c.Release, a.ID), "key": SecretKey}}},
		map[string]any{"name": "DATA_API__POLL_SECONDS", "value": strconv.Itoa(max(w.PollSeconds, 1))},
		map[string]any{"name": "DATA_API__DATA_DIR", "value": "/data"},
		map[string]any{"name": "DATA_API__PORT", "value": strconv.Itoa(PodPort)},
		map[string]any{"name": "DATA_API__FORWARDED_ALLOW_IPS", "value": c.ForwardedAllowIPs},
	}
	probe := func(path string, period, failures int) map[string]any {
		return map[string]any{"httpGet": map[string]any{"path": path, "port": "http"}, "periodSeconds": period,
			"timeoutSeconds": 3, "failureThreshold": failures}
	}
	container := map[string]any{
		"name": "data-api", "image": c.Image, "imagePullPolicy": c.ImagePullPolicy, "env": env,
		"ports":     []any{map[string]any{"name": "http", "containerPort": PodPort, "protocol": "TCP"}},
		"resources": resources,
		// ready once every resource is prepared, with a fresh manifest; alive while it answers
		"readinessProbe": probe("/readyz", 5, 1),
		"livenessProbe":  probe("/healthz", 10, 6),
		"securityContext": map[string]any{"allowPrivilegeEscalation": false, "readOnlyRootFilesystem": true,
			"runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001,
			"capabilities": map[string]any{"drop": []any{"ALL"}}},
		"volumeMounts": []any{map[string]any{"name": "data", "mountPath": "/data"},
			map[string]any{"name": "tmp", "mountPath": "/tmp"}},
	}
	pod := map[string]any{
		"automountServiceAccountToken": false,
		"enableServiceLinks":           false,
		// SIGTERM: out of the Service, the requests in flight, the last usage (dataapi/main.go)
		"terminationGracePeriodSeconds": dataapiwire.PodTerminationGraceSeconds,
		"securityContext": map[string]any{"runAsNonRoot": true, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
			"seccompProfile": map[string]any{"type": "RuntimeDefault"}},
		"containers": []any{container},
		"volumes": []any{
			map[string]any{"name": "data", "emptyDir": map[string]any{"sizeLimit": disk}},
			map[string]any{"name": "tmp", "emptyDir": map[string]any{"sizeLimit": "64Mi"}},
		},
		// the replicas on different nodes when they can be
		"topologySpreadConstraints": []any{map[string]any{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname",
			"whenUnsatisfiable": "ScheduleAnyway", "labelSelector": map[string]any{"matchLabels": podLabels(c.Release, a.ID)}}},
	}
	if len(c.ImagePullSecrets) > 0 {
		secrets := []any{}
		for _, s := range c.ImagePullSecrets {
			secrets = append(secrets, map[string]any{"name": s})
		}
		pod["imagePullSecrets"] = secrets
	}
	labels := Labels(c.Release, a.ID)
	spec := map[string]any{
		"selector":             map[string]any{"matchLabels": podLabels(c.Release, a.ID)},
		"revisionHistoryLimit": 2,
		// a new pod is ready (its data prepared) before an old one stops
		"strategy": map[string]any{"type": "RollingUpdate", "rollingUpdate": map[string]any{"maxSurge": 1, "maxUnavailable": 0}},
		"template": map[string]any{
			"metadata": map[string]any{"labels": labels, "annotations": map[string]any{AnnotationToken: tokenMark(w.TokenHash)}},
			"spec":     pod,
		},
	}
	// autoscaling, the HPA owns the count: the loop does not set it
	switch {
	case !w.Serving():
		spec["replicas"] = 0
	case !rt.Autoscale:
		spec["replicas"] = max(rt.Replicas, 1)
	}
	return map[string]any{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(c, a.ID), "spec": spec}
}

// tokenMark is what of the token's fingerprint the pods carry: enough to change with it
func tokenMark(hash string) string {
	if len(hash) > 16 {
		return hash[:16]
	}
	return hash
}

// PodDisruptionBudget keeps one pod of several answering through a drain; nil when the data
// API has (at most) one pod, or none.
func PodDisruptionBudget(c config.DataAPIPods, w Want) map[string]any {
	rt := w.API.Runtime
	if !w.Serving() || (rt.Autoscale && rt.MinReplicas < 2) || (!rt.Autoscale && rt.Replicas < 2) {
		return nil
	}
	return map[string]any{"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta(c, w.API.ID),
		"spec": map[string]any{"maxUnavailable": 1, "selector": map[string]any{"matchLabels": podLabels(c.Release, w.API.ID)}}}
}

// AutoscaleCPUPercent is the CPU the HPA keeps the pods at, of their request.
const AutoscaleCPUPercent = 70

// HorizontalPodAutoscaler scales the pods on CPU between the runtime's minimum and maximum;
// nil when the data API does not autoscale, or is paused.
func HorizontalPodAutoscaler(c config.DataAPIPods, w Want) map[string]any {
	rt := w.API.Runtime
	if !w.Serving() || !rt.Autoscale {
		return nil
	}
	return map[string]any{"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": meta(c, w.API.ID),
		"spec": map[string]any{
			"scaleTargetRef": map[string]any{"apiVersion": "apps/v1", "kind": "Deployment", "name": Name(c.Release, w.API.ID)},
			"minReplicas":    max(rt.MinReplicas, 1), "maxReplicas": max(rt.MaxReplicas, rt.MinReplicas, 1),
			"metrics": []any{map[string]any{"type": "Resource", "resource": map[string]any{"name": "cpu",
				"target": map[string]any{"type": "Utilization", "averageUtilization": AutoscaleCPUPercent}}}},
		}}
}

// IngressHost is the host the data APIs are published on (DATA_API__INGRESS_HOST, empty for
// any): the app's own, or the chart's dataApi.publicHost, a host with nothing else of Tabularia
// on it, whose certificate is DATA_API__INGRESS_TLS_SECRET.
func IngressHost(c config.DataAPIPods) string { return c.IngressHost }

// Ingress publishes /data/{slug}/ and nothing else of the pods (not /healthz nor /readyz).
func Ingress(c config.DataAPIPods, a *API) map[string]any {
	m := meta(c, a.ID)
	if len(c.IngressAnnotations) > 0 {
		ann := map[string]any{}
		for k, v := range c.IngressAnnotations {
			ann[k] = v
		}
		m["annotations"] = ann
	}
	rule := map[string]any{"http": map[string]any{"paths": []any{map[string]any{
		"path": "/data/" + a.Slug + "/", "pathType": "Prefix",
		"backend": map[string]any{"service": map[string]any{"name": Name(c.Release, a.ID), "port": map[string]any{"name": "http"}}},
	}}}}
	host := IngressHost(c)
	if host != "" {
		rule["host"] = host
	}
	spec := map[string]any{"rules": []any{rule}}
	if c.IngressClass != "" {
		spec["ingressClassName"] = c.IngressClass
	}
	if c.IngressTLSSecret != "" && host != "" {
		spec["tls"] = []any{map[string]any{"hosts": []any{host}, "secretName": c.IngressTLSSecret}}
	}
	return map[string]any{"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": m, "spec": spec}
}

// NetworkPolicy lets in only the ingress controller (DATA_API__NETWORK_POLICY_INGRESS_FROM), on
// the pods' port; out, DNS and the rules of DATA_API__NETWORK_POLICY_EGRESS (the gateway's
// manifest and usage, the object storage), nothing else. Nil when the policies are off.
func NetworkPolicy(c config.DataAPIPods, id int64) map[string]any {
	if !c.NetworkPolicy {
		return nil
	}
	ingress := []any{}
	if len(c.IngressFrom) > 0 {
		ingress = append(ingress, map[string]any{"from": c.IngressFrom,
			"ports": []any{map[string]any{"protocol": "TCP", "port": PodPort}}})
	}
	egress := []any{map[string]any{"ports": []any{map[string]any{"protocol": "UDP", "port": 53},
		map[string]any{"protocol": "TCP", "port": 53}}}}
	egress = append(egress, c.Egress...)
	return map[string]any{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(c, id),
		"spec": map[string]any{
			"podSelector": map[string]any{"matchLabels": podLabels(c.Release, id)},
			"policyTypes": []any{"Ingress", "Egress"},
			"ingress":     ingress,
			"egress":      egress,
		}}
}

// ParseID is the data API an object belongs to, by its label; 0 when it has none.
func ParseID(labels map[string]any) int64 {
	s, _ := labels[LabelID].(string)
	id, err := strconv.ParseInt(s, 10, 64)
	if err != nil || id < 1 {
		return 0
	}
	return id
}
