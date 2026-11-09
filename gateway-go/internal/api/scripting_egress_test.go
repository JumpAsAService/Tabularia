package api

import (
	"context"
	"net/netip"
	"reflect"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// A version without internet access reaches a storage outside the cluster through the egress
// proxy (scripting_egress.go): the proxy's variables for the libraries that read them, the
// storage's names at the proxy's Service in /etc/hosts for Arrow, which does not.

// withEgressProxy sets the chart's variables of the egress proxy, and a cluster DNS that
// knows its Service.
func withEgressProxy(t *testing.T, proxy string) {
	t.Helper()
	t.Cleanup(scriptcfg.ReloadRuntime)
	t.Setenv("SCRIPTS__EGRESS_PROXY", proxy)
	t.Setenv("SCRIPTS__EGRESS_PORTS", "443,80,9000")
	t.Setenv("SCRIPTS__EGRESS_NO_PROXY", ".corp.internal")
	t.Setenv("NOTEBOOKS__GATEWAY_URL", "http://tabularia-gateway.tabularia.svc:8000")
	scriptcfg.ReloadRuntime()
	before := lookupProxy
	t.Cleanup(func() { lookupProxy = before })
	lookupProxy = func(_ context.Context, host string) ([]netip.Addr, error) {
		if host != "tabularia-egress-proxy.tabularia.svc" {
			t.Errorf("resolved %q", host)
		}
		return []netip.Addr{netip.MustParseAddr("10.96.0.50")}, nil
	}
	proxyIP.Lock()
	proxyIP.ip = ""
	proxyIP.Unlock()
}

// what a script's Job gives its pod: the container's variables, and the pod's hostAliases
func podNetwork(t *testing.T, manifest pyjson.Object) (map[string]string, any) {
	t.Helper()
	spec := pyjson.Obj(pyjson.Obj(pyjson.Obj(manifest.Value("spec")).Value("template")).Value("spec"))
	vars := map[string]string{}
	for _, c := range spec.Value("containers").([]any) {
		for _, e := range pyjson.Obj(c).Value("env").([]any) {
			vars[pyjson.Str(pyjson.Obj(e).Value("name"))] = pyjson.Str(pyjson.Obj(e).Value("value"))
		}
		break
	}
	return vars, spec.Value("hostAliases")
}

func TestAScriptWithoutInternetReachesAStorageOutsideTheClusterThroughTheProxy(t *testing.T) {
	withEgressProxy(t, "http://tabularia-egress-proxy.tabularia.svc:3128")
	env := &scriptEnv{ID: 3, Name: "Python", Language: "python", CPURequest: "100m", CPULimit: "1", MemoryRequest: "128Mi", MemoryLimit: "1Gi"}
	image := "registry:5000/envs@sha256:abc"
	node := pyjson.Object{{Key: "id", Value: "s1"}, {Key: "data", Value: pyjson.Object{{Key: "entrypoint", Value: "main.py"}}}}
	cases := []struct {
		endpoint string
		aliases  []any // the names sent to the proxy; nil = no hostAliases
	}{
		{"https://s3.fr-par.scw.cloud", []any{"s3.fr-par.scw.cloud", "scripts.s3.fr-par.scw.cloud"}},
		{"http://minio.example.com:9000", []any{"minio.example.com", "scripts.minio.example.com"}},
		{"", []any{"s3.amazonaws.com", "s3.dualstack.us-east-1.amazonaws.com", "s3.us-east-1.amazonaws.com",
			"scripts.s3.amazonaws.com", "scripts.s3.dualstack.us-east-1.amazonaws.com", "scripts.s3.us-east-1.amazonaws.com"}},
		// a Service of the cluster: directly, through its namespace, as before
		{"http://minio.deps.svc.cluster.local:9000", nil},
		// an address cannot be put in /etc/hosts; a port the proxy's Service does not pass on
		{"http://203.0.113.7:9000", nil},
		{"https://s3.example.com:8443", nil},
	}
	for _, c := range cases {
		storage := &scriptStorage{Endpoint: c.endpoint, Bucket: "scripts", AccessKey: "k", SecretEnc: "x"}
		for _, internet := range []bool{true, false} {
			version := &scriptVersion{Number: 2, Status: "ready", ImageRef: &image, InternetAccess: internet}
			var offline *offlineNet
			if !internet {
				offline = offlineNetwork(context.Background(), storage, nil)
			}
			// the environment's own HTTPS_PROXY does not win over the proxy's
			own := pyjson.Object{{Key: "HTTPS_PROXY", Value: "http://elsewhere:8080"}, {Key: "TABULARIA_INPUT", Value: "s3://scripts/in"}}
			vars, aliases := podNetwork(t, scriptJobManifest(jobSettings{jobTTL: 60}, "tabularia-script-9-a1", env, version, node, own,
				pyjson.Object{}, 900, []any{}, "code", "env", offline))
			if internet {
				if vars["HTTPS_PROXY"] != "http://elsewhere:8080" || vars["NO_PROXY"] != "" || aliases != nil {
					t.Errorf("%q with internet: %v %v", c.endpoint, vars, aliases)
				}
				continue
			}
			for _, name := range []string{"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"} {
				if vars[name] != "http://tabularia-egress-proxy.tabularia.svc:3128" {
					t.Errorf("%q: %s = %q", c.endpoint, name, vars[name])
				}
			}
			want := "localhost,127.0.0.1,::1,.svc,.cluster.local,tabularia-gateway.tabularia.svc,.corp.internal"
			if vars["NO_PROXY"] != want || vars["no_proxy"] != want || vars["TABULARIA_INPUT"] != "s3://scripts/in" {
				t.Errorf("%q: %v", c.endpoint, vars)
			}
			if c.aliases == nil {
				if aliases != nil {
					t.Errorf("%q: hostAliases %v", c.endpoint, aliases)
				}
				continue
			}
			wantAliases := []any{pyjson.Object{{Key: "ip", Value: "10.96.0.50"}, {Key: "hostnames", Value: c.aliases}}}
			if !reflect.DeepEqual(aliases, wantAliases) {
				t.Errorf("%q: hostAliases %v, want %v", c.endpoint, aliases, wantAliases)
			}
		}
	}
}

// Without an egress proxy in the chart nothing changes: the policy alone decides.
func TestWithoutAnEgressProxyThePodIsAsBefore(t *testing.T) {
	withEgressProxy(t, "")
	storage := &scriptStorage{Endpoint: "https://s3.fr-par.scw.cloud", Bucket: "scripts"}
	if n := offlineNetwork(context.Background(), storage, nil); n != nil {
		t.Errorf("%+v", n)
	}
	pod := pyjson.Object{{Key: "restartPolicy", Value: "Never"}}
	var none *offlineNet
	if got := none.applyPod(pod); !reflect.DeepEqual(got, pod) {
		t.Error(got)
	}
}

func TestANotebookWithoutInternetGoesThroughTheProxyToo(t *testing.T) {
	withEgressProxy(t, "http://tabularia-egress-proxy.tabularia.svc:3128")
	env := &scEnvironment{ID: 4, Name: "Notebooks", Language: "python", CPURequest: "100m", CPULimit: "1", MemoryRequest: "128Mi", MemoryLimit: "1Gi"}
	storage := &scriptStorage{Endpoint: "https://s3.fr-par.scw.cloud", Bucket: "scripts"}
	img := &nbImage{env: env, versionID: 7, imageRef: "registry:5000/envs@sha256:abc", internet: false,
		offline: offlineNetwork(context.Background(), storage, nil)}
	vars, aliases := podNetwork(t, nbJobManifest("nb-1-app-abc", 1, "app", img, "files", "token",
		[]kv{{"HTTPS_PROXY", "http://elsewhere:8080"}, {"MY_VAR", "1"}}))
	if vars["HTTPS_PROXY"] != "http://tabularia-egress-proxy.tabularia.svc:3128" || vars["MY_VAR"] != "1" ||
		!strings.Contains(vars["NO_PROXY"], "tabularia-gateway.tabularia.svc") || vars["TABULARIA_URL"] != "http://tabularia-gateway.tabularia.svc:8000" {
		t.Errorf("%v", vars)
	}
	if aliases == nil {
		t.Error("no hostAliases")
	}
}

func TestThePanelKnowsTheEgressProxy(t *testing.T) {
	withEgressProxy(t, "http://tabularia-egress-proxy.tabularia.svc:3128")
	if got := egressProxyOf(); got == nil || !reflect.DeepEqual(got.Ports, []int{443, 80, 9000}) {
		t.Errorf("%+v", got)
	}
	withEgressProxy(t, "")
	if got := egressProxyOf(); got != nil {
		t.Errorf("without the proxy: %+v", got)
	}
}
