// Package clientip finds the client's address as uvicorn does with --forwarded-allow-ips
// (the Python gateway's audit.client_ip reads what uvicorn resolved): X-Forwarded-For is
// believed only when the connection comes from a trusted proxy, and the chain is read from
// the right, the first address that is not a trusted proxy being the client.
package clientip

import (
	"net"
	"net/http"
	"strconv"
	"strings"
)

// Resolver holds the trusted proxies: addresses, networks (CIDR) or «*» for any.
type Resolver struct {
	any  bool
	ips  map[string]bool
	nets []*net.IPNet
}

func New(trusted []string) *Resolver {
	r := &Resolver{ips: map[string]bool{}}
	for _, t := range trusted {
		switch {
		case t == "*":
			r.any = true
		case strings.Contains(t, "/"):
			if _, n, err := net.ParseCIDR(t); err == nil {
				r.nets = append(r.nets, n)
			}
		default:
			r.ips[canonical(t)] = true
		}
	}
	return r
}

// canonical writes an address one way (IPv6 has many), as uvicorn compares address
// objects; what is not an address stays a literal
func canonical(s string) string {
	if ip := net.ParseIP(s); ip != nil {
		return ip.String()
	}
	return s
}

func (r *Resolver) trusted(host string) bool {
	if r.any || r.ips[canonical(host)] {
		return true
	}
	ip := net.ParseIP(host)
	for _, n := range r.nets {
		if ip != nil && n.Contains(ip) {
			return true
		}
	}
	return false
}

// Of is the client's address, or "" when the connection has none.
func (r *Resolver) Of(req *http.Request) string {
	host, _, err := net.SplitHostPort(req.RemoteAddr)
	if err != nil {
		host = req.RemoteAddr
	}
	xff := req.Header.Values("X-Forwarded-For")
	if len(xff) == 0 || !r.trusted(host) {
		return host
	}
	// as uvicorn: the values joined, split on commas, each trimmed (empty ones included)
	var chain []string
	for _, p := range strings.Split(strings.Join(xff, ", "), ",") {
		chain = append(chain, hostOf(strings.TrimSpace(p)))
	}
	client := chain[0]
	if !r.any {
		for i := len(chain) - 1; i >= 0; i-- {
			if !r.trusted(chain[i]) {
				client = chain[i]
				break
			}
		}
	}
	if client == "" { // an empty header: uvicorn keeps the connection's address
		return host
	}
	return client
}

// hostOf is uvicorn's _parse_host_port without the port: bare addresses, IPv4 «host:port»
// and bracketed IPv6 «[host]:port»; anything else is kept as written, so a malformed value
// is never normalised into a trusted one.
func hostOf(v string) string {
	if strings.HasPrefix(v, "[") {
		end := strings.Index(v, "]")
		if end == -1 {
			return v
		}
		rest := v[end+1:]
		if rest == "" || strings.HasPrefix(rest, ":") {
			return v[1:end] // uvicorn keeps the host even when the port does not parse
		}
		return v
	}
	if strings.Count(v, ":") == 1 {
		host, port, _ := strings.Cut(v, ":")
		if _, err := strconv.Atoi(port); err == nil {
			return host
		}
	}
	return v
}
