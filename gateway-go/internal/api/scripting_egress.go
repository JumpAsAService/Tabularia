package api

import (
	"context"
	"log/slog"
	"net"
	"net/netip"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/egress"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// A pod of a version without internet access reaches its storage through the egress proxy
// (internal/egress, the chart's templates/egress-proxy.yaml), when the storage is outside
// the cluster: the proxy's address in the variables every HTTP library reads (requests,
// urllib, boto3, Polars, R's curl and httr), and the storage's names sent to the proxy's
// Service in /etc/hosts (hostAliases) for those that ignore them, Arrow's S3 in Python and
// R. The cluster's own names (a MinIO Service) go around the proxy, as before: the policy
// lets the pods reach the storage's namespace. Nothing else is reachable either way.

// offlineNet is what such a pod gets: the proxy's variables, and its hostAliases.
type offlineNet struct {
	env     pyjson.Object
	aliases []any
}

// lookupProxy resolves the proxy Service's name to its cluster IP; tests give their own.
var lookupProxy = func(ctx context.Context, host string) ([]netip.Addr, error) {
	return net.DefaultResolver.LookupNetIP(ctx, "ip", host)
}

// the proxy's address, kept a minute: a Service's cluster IP does not change
var proxyIP struct {
	sync.Mutex
	host, ip string
	at       time.Time
}

func egressProxyIP(ctx context.Context, host string) (string, error) {
	proxyIP.Lock()
	defer proxyIP.Unlock()
	if proxyIP.host == host && proxyIP.ip != "" && time.Since(proxyIP.at) < time.Minute {
		return proxyIP.ip, nil
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	addrs, err := lookupProxy(ctx, host)
	if err != nil {
		return "", err
	}
	if len(addrs) == 0 {
		return "", &net.DNSError{Err: "no address", Name: host}
	}
	proxyIP.host, proxyIP.ip, proxyIP.at = host, addrs[0].Unmap().String(), time.Now()
	return proxyIP.ip, nil
}

// offlineNetwork is the network of a pod without internet access: nil when the chart runs no
// egress proxy (SCRIPTS__EGRESS_PROXY), and then the policy alone decides.
func offlineNetwork(ctx context.Context, storage *scriptStorage, log *slog.Logger) *offlineNet {
	rt := scriptcfg.GetRuntime()
	if rt.EgressProxy == "" {
		return nil
	}
	proxy, err := url.Parse(rt.EgressProxy)
	if err != nil || proxy.Hostname() == "" {
		return nil
	}
	// around the proxy: the pod itself, the cluster's names (the gateway the notebooks read
	// datasources from, a storage that is a Service)
	bypass := []string{"localhost", "127.0.0.1", "::1", ".svc", ".cluster.local"}
	if g, err := url.Parse(strings.TrimSpace(rt.NotebooksGatewayURL)); err == nil && g.Hostname() != "" {
		bypass = append(bypass, g.Hostname())
	}
	for _, extra := range rt.EgressNoProxy {
		if !slices.Contains(bypass, extra) {
			bypass = append(bypass, extra)
		}
	}
	noProxy := strings.Join(bypass, ",")
	out := &offlineNet{env: pyjson.Object{
		{Key: "HTTPS_PROXY", Value: rt.EgressProxy}, {Key: "https_proxy", Value: rt.EgressProxy},
		{Key: "HTTP_PROXY", Value: rt.EgressProxy}, {Key: "http_proxy", Value: rt.EgressProxy},
		{Key: "NO_PROXY", Value: noProxy}, {Key: "no_proxy", Value: noProxy},
	}}
	names := transparentNames(storage, rt.EgressPorts)
	if len(names) == 0 {
		return out
	}
	ip, err := egressProxyIP(ctx, proxy.Hostname())
	if err != nil {
		if log != nil {
			log.Warn("the egress proxy's address could not be resolved: Arrow's S3 cannot reach the scripts bucket from a version "+
				"without internet access", "proxy", proxy.Hostname(), "err", err.Error())
		}
		return out
	}
	hostnames := make([]any, len(names))
	for i, n := range names {
		hostnames[i] = n
	}
	out.aliases = []any{pyjson.Object{{Key: "ip", Value: ip}, {Key: "hostnames", Value: hostnames}}}
	return out
}

// transparentNames is the scripts bucket's names the pods' /etc/hosts sends to the proxy:
// outside the cluster, a name (not an address), on a port the proxy's Service passes on.
func transparentNames(storage *scriptStorage, ports []int) []string {
	if storage == nil || storage.Bucket == "" {
		return nil
	}
	var names []string
	for _, hp := range egress.HostsOf(storage.Endpoint, storage.Region, storage.Bucket) {
		host, port, err := net.SplitHostPort(hp)
		if err != nil || egress.IsAddress(host) || egress.IsClusterName(host) {
			continue
		}
		if n, _ := strconv.Atoi(port); slices.Contains(ports, n) && !slices.Contains(names, host) {
			names = append(names, host)
		}
	}
	return names
}

// apply puts the network on a container's variables (after the environment's own: the
// proxy's win) and on the pod.
func (n *offlineNet) applyEnv(environment pyjson.Object) pyjson.Object {
	if n == nil {
		return environment
	}
	return merge(environment, n.env)
}

func (n *offlineNet) applyPod(pod pyjson.Object) pyjson.Object {
	if n == nil || len(n.aliases) == 0 {
		return pod
	}
	return append(pod, pyjson.KV{Key: "hostAliases", Value: n.aliases})
}

// egressProxyOut tells the panel whether the chart runs the egress proxy, and the storage
// ports its Service passes on: with the bucket's endpoint, it says how a version without
// internet access reaches the bucket (through the proxy, directly as a Service of the
// cluster, or only through scripts.noInternet.allowCIDRs). Nil without a proxy.
type egressProxyOut struct {
	Ports []int `json:"ports"`
}

func egressProxyOf() *egressProxyOut {
	rt := scriptcfg.GetRuntime()
	if rt.EgressProxy == "" {
		return nil
	}
	return &egressProxyOut{Ports: append([]int{}, rt.EgressPorts...)}
}
