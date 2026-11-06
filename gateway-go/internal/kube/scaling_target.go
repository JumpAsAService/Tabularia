package kube

import (
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// What services/kube_scaling.py knows of a kind of worker, shared by the panel's routes
// (Admin → Workers) and the loop that puts the bounds back (internal/jobs/scaling).

// ScalingTarget is kube_scaling.Target.
type ScalingTarget struct {
	Kind, Deployment, Autoscaler, AutoscalerName string // Autoscaler: "hpa", "scaledobject" or "" (none)
	Ceiling                                      int64  // the highest maxReplicas the panel may set
}

// Floor is Target.floor: KEDA can scale to zero pods, an HPA keeps one.
func (t ScalingTarget) Floor() int64 {
	if t.Autoscaler == "scaledobject" {
		return 0
	}
	return 1
}

// TargetOf is kube_scaling.target(kind); an error (Python's AttributeError) for another kind.
func TargetOf(cfg config.KubeJobsConfig, kind string) (ScalingTarget, error) {
	var auto, deployment string
	var ceiling int64
	switch kind {
	case "run":
		auto, deployment, ceiling = cfg.RunWorkerAutoscaler, cfg.RunWorkerDeployment, cfg.RunWorkerMaxReplicas
	case "preview":
		auto, deployment, ceiling = cfg.PreviewWorkerAutoscaler, cfg.PreviewWorkerDeployment, cfg.PreviewWorkerMaxReplicas
	default: // getattr(k, f"{kind}_worker_autoscaler"): an AttributeError
		return ScalingTarget{}, fmt.Errorf("'KubernetesSettings' object has no attribute '%s_worker_autoscaler'", kind)
	}
	aKind, aName, _ := strings.Cut(strings.TrimSpace(auto), "/")
	aKind, aName = strings.ToLower(strings.TrimSpace(aKind)), strings.TrimSpace(aName)
	if (aKind != "hpa" && aKind != "scaledobject") || aName == "" {
		aKind, aName = "", ""
	}
	return ScalingTarget{kind, strings.TrimSpace(deployment), aKind, aName, ceiling}, nil
}

// Check is kube_scaling.check: why these bounds cannot be set, as the code the panel
// translates; nil when they can.
func Check(t ScalingTarget, lo, hi int64) pyjson.Object {
	switch {
	case t.Autoscaler == "":
		return pyjson.Object{{Key: "code", Value: "no_autoscaler"}}
	case lo < t.Floor():
		return pyjson.Object{{Key: "code", Value: "min"}, {Key: "limit", Value: t.Floor()}}
	case hi < max(lo, 1):
		return pyjson.Object{{Key: "code", Value: "order"}, {Key: "limit", Value: max(lo, 1)}}
	case hi > t.Ceiling:
		return pyjson.Object{{Key: "code", Value: "ceiling"}, {Key: "limit", Value: t.Ceiling}}
	}
	return nil
}

// ScalingNamespace is kube_scaling._namespace.
func ScalingNamespace(cfg config.KubeJobsConfig) (string, error) {
	if cfg.Namespace != "" {
		return cfg.Namespace, nil
	}
	raw, err := os.ReadFile(cfg.NamespaceFile)
	if err != nil {
		return "", &Error{Status: 0, Message: "The pod's namespace is unknown: set KUBERNETES__NAMESPACE"}
	}
	return strings.TrimSpace(string(raw)), nil
}

// ScalingPaths is kube_scaling._paths: the Deployment's and the autoscaler's ("" for none).
func ScalingPaths(cfg config.KubeJobsConfig, t ScalingTarget) (string, string, error) {
	ns, err := ScalingNamespace(cfg)
	if err != nil {
		return "", "", err
	}
	deployment := "/apis/apps/v1/namespaces/" + ns + "/deployments/" + t.Deployment
	switch t.Autoscaler {
	case "hpa":
		return deployment, "/apis/autoscaling/v2/namespaces/" + ns + "/horizontalpodautoscalers/" + t.AutoscalerName, nil
	case "scaledobject":
		return deployment, "/apis/keda.sh/v1alpha1/namespaces/" + ns + "/scaledobjects/" + t.AutoscalerName, nil
	}
	return deployment, "", nil
}

// PyInt is int(v) of a JSON value, with Python's error texts.
func PyInt(v any) (int64, error) {
	switch x := v.(type) {
	case int64:
		return x, nil
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case float64:
		if math.IsInf(x, 0) || math.IsNaN(x) {
			return 0, fmt.Errorf("cannot convert float %v to integer", x)
		}
		return int64(x), nil
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(strings.ReplaceAll(x, "_", "")), 10, 64)
		if err != nil {
			return 0, fmt.Errorf("invalid literal for int() with base 10: %s", pyjson.Repr(x))
		}
		return n, nil
	}
	return 0, fmt.Errorf("int() argument must be a string, a bytes-like object or a real number, not '%s'", PyType(v))
}

// PyType is type(v).__name__ of a JSON value, as the errors above name it.
func PyType(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case pyjson.Object:
		return "dict"
	case []any:
		return "list"
	}
	return "object"
}

// Bounds is kube_scaling._bounds: the autoscaler's minimum and maximum.
func Bounds(t ScalingTarget, obj any) (int64, int64, error) {
	o, isDict := obj.(pyjson.Object)
	if !isDict {
		return 0, 0, fmt.Errorf("'%s' object has no attribute 'get'", PyType(obj))
	}
	specV := o.Value("spec")
	spec, isDict := specV.(pyjson.Object)
	if !isDict && pyjson.Truthy(specV) {
		return 0, 0, fmt.Errorf("'%s' object has no attribute 'get'", PyType(specV))
	}
	get := func(key string, def int64) (int64, error) {
		v, ok := spec.Get(key)
		if !ok {
			return def, nil
		}
		return PyInt(v)
	}
	minKey, maxKey, minDef, maxDef := "minReplicaCount", "maxReplicaCount", int64(0), int64(100) // KEDA's defaults
	if t.Autoscaler == "hpa" {
		minKey, maxKey, minDef, maxDef = "minReplicas", "maxReplicas", 1, 1
	}
	lo, err := get(minKey, minDef)
	if err != nil {
		return 0, 0, err
	}
	hi, err := get(maxKey, maxDef)
	return lo, hi, err
}

// Patch is kube_scaling._patch.
func Patch(t ScalingTarget, lo, hi int64) pyjson.Object {
	if t.Autoscaler == "hpa" {
		return pyjson.Object{{Key: "spec", Value: pyjson.Object{{Key: "minReplicas", Value: lo}, {Key: "maxReplicas", Value: hi}}}}
	}
	return pyjson.Object{{Key: "spec", Value: pyjson.Object{{Key: "minReplicaCount", Value: lo}, {Key: "maxReplicaCount", Value: hi}}}}
}
