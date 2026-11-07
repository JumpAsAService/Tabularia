package dataapis

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// The objects of a data API as the reconcile loop applies them: what each one holds, without a
// cluster.

func clusterConfig() config.DataAPIPods {
	return config.DataAPIPods{Namespace: "tabularia-scripts", Release: "tabularia", Image: "registry/tabularia-data-api:1.5.0",
		ImagePullPolicy: "IfNotPresent", ImagePullSecrets: []string{"registry"}, GatewayURL: "http://tabularia-gateway.tabularia.svc:8000",
		ForwardedAllowIPs: "10.0.0.0/8", MaxDisk: "20Gi", IngressClass: "nginx",
		IngressAnnotations: map[string]string{"nginx.ingress.kubernetes.io/proxy-read-timeout": "60"},
		NetworkPolicy:      true,
		IngressFrom: []any{map[string]any{"namespaceSelector": map[string]any{"matchLabels": map[string]any{
			"kubernetes.io/metadata.name": "ingress-nginx"}}}},
		Egress: []any{map[string]any{"to": []any{map[string]any{"podSelector": map[string]any{}}}}}}
}

// at walks a JSON path of an object built here: maps by key, lists by index ("0", "1"…)
func at(t *testing.T, v any, path string) any {
	t.Helper()
	raw, _ := json.Marshal(v)
	var cur any
	_ = json.Unmarshal(raw, &cur)
	for _, k := range strings.Split(path, ".") {
		switch c := cur.(type) {
		case map[string]any:
			cur = c[k]
		case []any:
			i := 0
			for _, ch := range k {
				i = i*10 + int(ch-'0')
			}
			if i >= len(c) {
				return nil
			}
			cur = c[i]
		default:
			return nil
		}
	}
	return cur
}

func TestNamesAndLabels(t *testing.T) {
	if n := Name("tabularia", 12); n != "tabularia-data-api-12" {
		t.Error(n)
	}
	if n := Name(strings.Repeat("r", 80), 12); len(n) != 63 || !strings.HasSuffix(n, "-data-api-12") {
		t.Error(n)
	}
	l := Labels("tabularia", 12)
	if ParseID(l) != 12 || ParseID(map[string]any{}) != 0 || ParseID(map[string]any{LabelID: "x"}) != 0 ||
		ParseID(map[string]any{LabelID: "-3"}) != 0 {
		t.Error(l)
	}
	if s := Selector("tabularia"); s != "app.kubernetes.io/instance=tabularia,app.kubernetes.io/component=data-api" {
		t.Error(s)
	}
}

func TestDisk(t *testing.T) {
	gi := int64(1) << 30
	for _, c := range []struct {
		sizes []int64
		want  int64
	}{
		{nil, gi},                             // nothing yet: the smallest
		{[]int64{465 << 20}, 4 * gi},          // 6 × 465 MB + 256 MB = 3 GB: 4Gi (measured: 2.3 GB at the highest)
		{[]int64{60 << 20, 60 << 20}, gi},     // 976 MB with the margin: 1Gi
		{[]int64{2 * gi}, 16 * gi},            // 12.25 GB: 16Gi
		{[]int64{10 * gi}, 20 * gi},           // past the most: the most
		{[]int64{-5}, gi},                     // nonsense from the storage counts nothing
		{[]int64{(gi - (256 << 20)) / 6}, gi}, // just 1Gi
	} {
		if got := Disk(c.sizes, 20*gi); got != c.want {
			t.Errorf("%v: %d, want %d", c.sizes, got, c.want)
		}
	}
	if Quantity(2*gi) != "2Gi" || Quantity(256<<20) != "256Mi" || Quantity(gi+5) != "1073741829" {
		t.Error(Quantity(2 * gi))
	}
}

func TestDeployment(t *testing.T) {
	c := clusterConfig()
	a := &API{ID: 7, Slug: "crm", State: Active, Runtime: Runtime{Replicas: 2, MinReplicas: 2, MaxReplicas: 2, CPU: "500m", Memory: "1Gi"}}
	w := Want{API: a, TokenHash: strings.Repeat("ab", 32), Disk: 2 << 30, PollSeconds: 20}
	d := Deployment(c, w)
	checks := map[string]any{
		"metadata.name":      "tabularia-data-api-7",
		"metadata.namespace": "tabularia-scripts",
		"spec.replicas":      float64(2),
		"spec.strategy.rollingUpdate.maxUnavailable":                               float64(0),
		"spec.template.spec.automountServiceAccountToken":                          false,
		"spec.template.spec.securityContext.runAsUser":                             float64(10001),
		"spec.template.spec.securityContext.runAsNonRoot":                          true,
		"spec.template.spec.securityContext.seccompProfile.type":                   "RuntimeDefault",
		"spec.template.spec.containers.0.image":                                    "registry/tabularia-data-api:1.5.0",
		"spec.template.spec.containers.0.securityContext.readOnlyRootFilesystem":   true,
		"spec.template.spec.containers.0.securityContext.allowPrivilegeEscalation": false,
		"spec.template.spec.containers.0.securityContext.capabilities.drop.0":      "ALL",
		"spec.template.spec.containers.0.resources.requests.cpu":                   "500m",
		"spec.template.spec.containers.0.resources.limits.memory":                  "1Gi",
		"spec.template.spec.containers.0.resources.requests.ephemeral-storage":     "2Gi",
		"spec.template.spec.containers.0.resources.limits.ephemeral-storage":       "2304Mi",
		"spec.template.spec.containers.0.readinessProbe.httpGet.path":              "/readyz",
		"spec.template.spec.containers.0.livenessProbe.httpGet.path":               "/healthz",
		"spec.template.spec.volumes.0.emptyDir.sizeLimit":                          "2Gi",
		"spec.template.spec.containers.0.volumeMounts.0.mountPath":                 "/data",
		"spec.template.spec.imagePullSecrets.0.name":                               "registry",
	}
	for path, want := range checks {
		if got := at(t, d, path); got != want {
			t.Errorf("%s: %v, want %v", path, got, want)
		}
	}
	tmpl := at(t, d, "spec.template.metadata").(map[string]any)
	if tmpl["annotations"].(map[string]any)[AnnotationToken] != strings.Repeat("ab", 8) {
		t.Error(tmpl)
	}
	if tmpl["labels"].(map[string]any)[LabelID] != "7" {
		t.Error(tmpl)
	}
	env := map[string]any{}
	for _, e := range at(t, d, "spec.template.spec.containers.0.env").([]any) {
		m := e.(map[string]any)
		if m["value"] != nil {
			env[m["name"].(string)] = m["value"]
		} else {
			env[m["name"].(string)] = m["valueFrom"]
		}
	}
	if env["DATA_API__ID"] != "7" || env["DATA_API__GATEWAY_URL"] != c.GatewayURL || env["DATA_API__POLL_SECONDS"] != "20" ||
		env["DATA_API__FORWARDED_ALLOW_IPS"] != "10.0.0.0/8" || env["DATA_API__DATA_DIR"] != "/data" {
		t.Error(env)
	}
	// the token never in the Deployment: a reference to the Secret
	raw, _ := json.Marshal(d)
	if !strings.Contains(string(raw), `"secretKeyRef":{"key":"pod-token","name":"tabularia-data-api-7"}`) {
		t.Error(string(raw))
	}
	// paused: no pods; autoscaling: the HPA's count, not set here
	a.State = Paused
	if at(t, Deployment(c, w), "spec.replicas") != float64(0) {
		t.Error("paused")
	}
	a.State, a.Runtime.Autoscale, a.Runtime.MinReplicas, a.Runtime.MaxReplicas = Active, true, 1, 3
	if v := at(t, Deployment(c, w), "spec.replicas"); v != nil {
		t.Error("autoscaled:", v)
	}
	// the pod token's fingerprint changes the pods
	w2 := w
	w2.TokenHash = strings.Repeat("cd", 32)
	if fmt.Sprint(at(t, Deployment(c, w2), "spec.template.metadata.annotations")) ==
		fmt.Sprint(at(t, Deployment(c, w), "spec.template.metadata.annotations")) {
		t.Error("same pods for another token")
	}
}

func TestBudgetAndScaler(t *testing.T) {
	c := clusterConfig()
	a := &API{ID: 7, Slug: "crm", State: Active, Runtime: Runtime{Replicas: 1, MinReplicas: 1, MaxReplicas: 1, CPU: "250m", Memory: "256Mi"}}
	w := Want{API: a}
	if PodDisruptionBudget(c, w) != nil || HorizontalPodAutoscaler(c, w) != nil {
		t.Error("one pod: neither")
	}
	a.Runtime.Replicas = 2
	if p := PodDisruptionBudget(c, w); p == nil || at(t, p, "spec.maxUnavailable") != float64(1) {
		t.Error(p)
	}
	a.Runtime = Runtime{Autoscale: true, Replicas: 1, MinReplicas: 1, MaxReplicas: 3, CPU: "250m", Memory: "256Mi"}
	h := HorizontalPodAutoscaler(c, w)
	if h == nil || at(t, h, "spec.minReplicas") != float64(1) || at(t, h, "spec.maxReplicas") != float64(3) ||
		at(t, h, "spec.scaleTargetRef.name") != "tabularia-data-api-7" ||
		at(t, h, "spec.metrics.0.resource.target.averageUtilization") != float64(AutoscaleCPUPercent) {
		t.Error(h)
	}
	if PodDisruptionBudget(c, w) != nil {
		t.Error("autoscaling from one pod: no budget")
	}
	a.Runtime.MinReplicas = 2
	if PodDisruptionBudget(c, w) == nil {
		t.Error("autoscaling from two pods: a budget")
	}
	a.State = Paused
	if PodDisruptionBudget(c, w) != nil || HorizontalPodAutoscaler(c, w) != nil {
		t.Error("paused: neither")
	}
}

func TestIngressAndPolicy(t *testing.T) {
	c := clusterConfig()
	a := &API{ID: 7, Slug: "crm"}
	in := Ingress(c, a)
	if at(t, in, "spec.rules.0.http.paths.0.path") != "/data/crm/" || at(t, in, "spec.rules.0.http.paths.0.pathType") != "Prefix" ||
		at(t, in, "spec.rules.0.http.paths.1") != nil || at(t, in, "spec.rules.0.host") != nil || at(t, in, "spec.ingressClassName") != "nginx" ||
		at(t, in, "spec.rules.0.http.paths.0.backend.service.name") != "tabularia-data-api-7" || at(t, in, "spec.tls") != nil {
		t.Errorf("%v", in)
	}
	if at(t, in, "metadata.annotations").(map[string]any)["nginx.ingress.kubernetes.io/proxy-read-timeout"] != "60" {
		t.Error(in)
	}
	// a host, and a certificate for it
	c.IngressHost, c.IngressTLSSecret = "api.example.com", "api-tls"
	in = Ingress(c, a)
	if at(t, in, "spec.rules.0.host") != "api.example.com" || at(t, in, "spec.tls.0.secretName") != "api-tls" ||
		at(t, in, "spec.tls.0.hosts.0") != "api.example.com" {
		t.Error(in)
	}
	p := NetworkPolicy(c, 7)
	if at(t, p, "spec.policyTypes.0") != "Ingress" || at(t, p, "spec.policyTypes.1") != "Egress" ||
		at(t, p, "spec.podSelector.matchLabels").(map[string]any)[LabelID] != "7" ||
		at(t, p, "spec.ingress.0.from.0.namespaceSelector.matchLabels").(map[string]any)["kubernetes.io/metadata.name"] != "ingress-nginx" ||
		at(t, p, "spec.ingress.0.ports.0.port") != float64(PodPort) || at(t, p, "spec.ingress.1") != nil ||
		at(t, p, "spec.egress.0.ports.0.port") != float64(53) || at(t, p, "spec.egress.0.to") != nil ||
		at(t, p, "spec.egress.1.to.0.podSelector") == nil || at(t, p, "spec.egress.2") != nil {
		t.Errorf("%v", p)
	}
	// no ingress controller named: nothing comes in
	c.IngressFrom = nil
	if in := at(t, NetworkPolicy(c, 7), "spec.ingress"); in == nil || len(in.([]any)) != 0 {
		t.Error(in)
	}
	c.NetworkPolicy = false
	if NetworkPolicy(c, 7) != nil {
		t.Error("policies off")
	}
	s := Secret(c, 7, "tdp_x")
	if at(t, s, "stringData.pod-token") != "tdp_x" || at(t, s, "metadata.labels").(map[string]any)[LabelID] != "7" {
		t.Error(s)
	}
	svc := Service(c, 7)
	if at(t, svc, "spec.type") != "ClusterIP" || at(t, svc, "spec.ports.0.port") != float64(PodPort) {
		t.Error(svc)
	}
}
