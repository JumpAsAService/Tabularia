// Objects of any kind, with encoding/json rather than the Python-shaped values of Call: what
// the data APIs' reconcile loop keeps in line (docs/specs/data-apis.md, «Runtime»). An object
// is applied server-side (one PATCH that creates or updates it, owned by a field manager), so
// a round that changes nothing writes nothing.

package kube

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// Kind is where the API keeps a kind of object: its group and version, and its plural.
type Kind struct {
	GroupVersion string // "v1" for the core group, "apps/v1"…
	Plural       string
}

// The kinds the data APIs' reconcile loop handles.
var (
	Deployments             = Kind{"apps/v1", "deployments"}
	Services                = Kind{"v1", "services"}
	Secrets                 = Kind{"v1", "secrets"}
	Pods                    = Kind{"v1", "pods"}
	PodDisruptionBudgets    = Kind{"policy/v1", "poddisruptionbudgets"}
	HorizontalPodAutoscaler = Kind{"autoscaling/v2", "horizontalpodautoscalers"}
	Ingresses               = Kind{"networking.k8s.io/v1", "ingresses"}
	NetworkPolicies         = Kind{"networking.k8s.io/v1", "networkpolicies"}
)

// Path is the collection's path in a namespace.
func (k Kind) Path(namespace string) string {
	prefix := "/apis/"
	if k.GroupVersion == "v1" {
		prefix = "/api/"
	}
	return prefix + k.GroupVersion + "/namespaces/" + namespace + "/" + k.Plural
}

// Obj is an object as JSON decodes it.
type Obj = map[string]any

func request(ctx context.Context, k scriptcfg.Kubernetes, method, path, contentType string, body any) (Obj, error) {
	var payload []byte
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		payload = b
	}
	data, err := send(ctx, k, method, path, contentType, "application/json", payload)
	if err != nil {
		return nil, err
	}
	var out Obj
	if len(data) > 0 {
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, fmt.Errorf("kube: the answer is not JSON: %w", err)
		}
	}
	return out, nil
}

// Apply creates or updates the object `name` of `kind` server-side, as field manager
// `manager` (taking over the fields another manager holds): the object as the cluster has it.
func Apply(ctx context.Context, k scriptcfg.Kubernetes, namespace string, kind Kind, name, manager string, obj Obj) (Obj, error) {
	path := kind.Path(namespace) + "/" + name + "?fieldManager=" + Quote(manager) + "&force=true"
	// JSON is YAML: the apply patch's media type takes it
	return request(ctx, k, http.MethodPatch, path, "application/apply-patch+yaml", obj)
}

// Get is the object, nil when the cluster does not have it.
func Get(ctx context.Context, k scriptcfg.Kubernetes, namespace string, kind Kind, name string) (Obj, error) {
	out, err := request(ctx, k, http.MethodGet, kind.Path(namespace)+"/"+name, "", nil)
	if Status(err) == 404 {
		return nil, nil
	}
	return out, err
}

// Delete removes the object (and, in the background, what it owns); one already gone is fine.
func Delete(ctx context.Context, k scriptcfg.Kubernetes, namespace string, kind Kind, name string) error {
	_, err := request(ctx, k, http.MethodDelete, kind.Path(namespace)+"/"+name+"?propagationPolicy=Background", "", nil)
	if Status(err) == 404 {
		return nil
	}
	return err
}

// ListObjects are the objects of `kind` the label selector picks.
func ListObjects(ctx context.Context, k scriptcfg.Kubernetes, namespace string, kind Kind, selector string) ([]Obj, error) {
	out, err := request(ctx, k, http.MethodGet, kind.Path(namespace)+"?labelSelector="+Quote(selector), "", nil)
	if err != nil {
		return nil, err
	}
	items, _ := out["items"].([]any)
	list := make([]Obj, 0, len(items))
	for _, it := range items {
		if o, ok := it.(Obj); ok {
			list = append(list, o)
		}
	}
	return list, nil
}

// At is the value at a path of keys in an object, nil when it is not there.
func At(o Obj, keys ...string) any {
	var v any = o
	for _, key := range keys {
		m, ok := v.(Obj)
		if !ok {
			return nil
		}
		v = m[key]
	}
	return v
}

// Str is the text at a path of keys, "" when it is not one.
func Str(o Obj, keys ...string) string {
	s, _ := At(o, keys...).(string)
	return s
}

// Int is the number at a path of keys, 0 when it is not one.
func Int(o Obj, keys ...string) int64 {
	switch n := At(o, keys...).(type) {
	case float64:
		return int64(n)
	case json.Number:
		i, _ := n.Int64()
		return i
	}
	return 0
}
