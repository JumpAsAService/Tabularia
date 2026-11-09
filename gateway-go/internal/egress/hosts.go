// Package egress is the egress proxy of the scripts and notebooks without internet access
// (docs/scripting.md, «Internet access»): the only way out of their pods, towards the
// storage and nothing else. It runs as a workload of its own (APP__ROLE=egress-proxy, the
// chart's templates/egress-proxy.yaml), and it lets through only the hosts of the storage
// the pods really use: everything else gets 403.
//
// Two ways in, both checked against the same list:
//   - a forward proxy (HTTPS_PROXY, HTTP_PROXY, which the gateway gives those pods): CONNECT
//     for HTTPS, an absolute-form request for plain HTTP. requests, urllib, boto3, Polars,
//     R's curl and httr use it;
//   - transparent ports, for the libraries that ignore those variables (Arrow's S3, in Python
//     and in R): the pods' /etc/hosts sends the storage's names to this proxy's Service
//     (hostAliases), on the storage's own port; the proxy reads the name the client asked
//     for (the TLS SNI, or the HTTP Host) and passes the bytes through untouched, TLS
//     included, so the client still checks the storage's own certificate.
//
// The proxy resolves the names itself and checks every address it connects to: never a
// loopback, link-local (the cloud metadata), multicast or unspecified address, whatever the
// name says.
package egress

import (
	"net"
	"net/netip"
	"net/url"
	"sort"
	"strconv"
	"strings"
)

// HostsOf is what a pod reaches of an S3 storage: its endpoint as host:port, and the bucket's
// virtual-hosted name (bucket.host) beside it. An empty endpoint is AWS's, in the region.
// Nothing when the endpoint does not parse.
func HostsOf(endpoint, region, bucket string) []string {
	endpoint = strings.TrimSpace(endpoint)
	bucket = strings.ToLower(strings.TrimSpace(bucket))
	var hosts []string
	add := func(host string, port int) {
		hosts = append(hosts, net.JoinHostPort(host, strconv.Itoa(port)))
		if bucket != "" && validBucket(bucket) && !IsAddress(host) {
			hosts = append(hosts, net.JoinHostPort(bucket+"."+host, strconv.Itoa(port)))
		}
	}
	if endpoint == "" {
		region = strings.ToLower(strings.TrimSpace(region))
		if region == "" {
			region = "us-east-1"
		}
		add("s3.amazonaws.com", 443)
		if validLabel(region) {
			add("s3."+region+".amazonaws.com", 443)
			add("s3.dualstack."+region+".amazonaws.com", 443)
		}
		return unique(hosts)
	}
	host, port, ok := Endpoint(endpoint)
	if !ok {
		return nil
	}
	add(host, port)
	return unique(hosts)
}

// Endpoint is an endpoint's host (lower case, without brackets) and port (the scheme's when
// it has none).
func Endpoint(endpoint string) (string, int, bool) {
	u, err := url.Parse(strings.TrimSpace(endpoint))
	if err != nil || u.Host == "" {
		return "", 0, false
	}
	host := strings.TrimSuffix(strings.ToLower(u.Hostname()), ".")
	if host == "" {
		return "", 0, false
	}
	port := 0
	switch strings.ToLower(u.Scheme) {
	case "https":
		port = 443
	case "http":
		port = 80
	default:
		return "", 0, false
	}
	if p := u.Port(); p != "" {
		n, err := strconv.Atoi(p)
		if err != nil || n < 1 || n > 65535 {
			return "", 0, false
		}
		port = n
	}
	return host, port, true
}

// IsClusterName says whether a host is a Service of the cluster (a name without dots, or
// *.svc, *.svc.cluster.local and the like): those the pods reach directly, through their
// namespace in the NetworkPolicy, never through the proxy.
func IsClusterName(host string) bool {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if IsAddress(host) {
		return false
	}
	return !strings.Contains(host, ".") || strings.HasSuffix(host, ".svc") || strings.Contains(host, ".svc.")
}

// IsAddress says whether a host is an IP address rather than a name.
func IsAddress(host string) bool {
	_, err := netip.ParseAddr(strings.Trim(host, "[]"))
	return err == nil
}

// Forbidden is an address the proxy never connects to, whatever name led to it: loopback,
// link-local (169.254.169.254, the clouds' metadata, is one), multicast, unspecified, and
// the metadata addresses that are not link-local.
func Forbidden(a netip.Addr) bool {
	a = a.Unmap()
	return !a.IsValid() || a.IsLoopback() || a.IsUnspecified() || a.IsLinkLocalUnicast() || a.IsLinkLocalMulticast() ||
		a.IsMulticast() || a.IsInterfaceLocalMulticast() || metadata[a] || a == netip.AddrFrom4([4]byte{255, 255, 255, 255}) ||
		netip.MustParsePrefix("0.0.0.0/8").Contains(a)
}

// the metadata services outside the link-local ranges: AWS's IPv6 one, Alibaba's
var metadata = map[netip.Addr]bool{
	netip.MustParseAddr("fd00:ec2::254"):   true,
	netip.MustParseAddr("100.100.100.200"): true,
}

func validLabel(s string) bool {
	if s == "" || len(s) > 63 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
			return false
		}
	}
	return true
}

// validBucket: an S3 bucket name that can be a DNS name (lower case, digits, dots, hyphens)
func validBucket(s string) bool {
	if len(s) < 3 || len(s) > 63 {
		return false
	}
	for _, part := range strings.Split(s, ".") {
		if !validLabel(part) {
			return false
		}
	}
	return true
}

func unique(list []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range list {
		if !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	sort.Strings(out)
	return out
}
