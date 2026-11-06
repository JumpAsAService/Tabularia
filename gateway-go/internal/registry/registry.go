// Package registry is services/container_registry.py: what the gateway asks a container
// registry over the Docker Registry HTTP API v2 (ping, tags, digest, an image's
// configuration), with its authentication (nothing, Basic, or a Bearer token from the realm
// the registry names) and its errors in the words of whoever must fix them.
//
// The network's faults are worded as httpx words them where the words are known (measured
// in the gateway's image): a connection refused, a timeout, a plain-HTTP server spoken to in
// TLS, a certificate not trusted. The rest («{host}: <OpenSSL's or the OS's text>») cannot be
// the same bytes from Go's TLS and network stack.
package registry

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/httpxerr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Timeout is TIMEOUT_SECONDS.
const Timeout = 15 * time.Second

// MaxTags is MAX_TAGS.
const MaxTags = 1000

// ManifestTypes is MANIFEST_TYPES.
var ManifestTypes = strings.Join([]string{
	"application/vnd.oci.image.index.v1+json",
	"application/vnd.docker.distribution.manifest.list.v2+json",
	"application/vnd.oci.image.manifest.v1+json",
	"application/vnd.docker.distribution.manifest.v2+json",
}, ", ")

var (
	// HOST without its lookahead (hostLength)
	hostRe     = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:[0-9]{1,5})?$`)
	repository = regexp.MustCompile(`^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$`)
	tagRe      = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)
	digestRe   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	paramRe    = regexp.MustCompile(`(\w+)="([^"]*)"`)
	nextRe     = regexp.MustCompile(`<([^>]+)>;\s*rel="next"`)
)

var metadata = []netip.Prefix{netip.MustParsePrefix("169.254.0.0/16"), netip.MustParsePrefix("fe80::/10"),
	netip.MustParsePrefix("100.100.100.200/32"), netip.MustParsePrefix("fd00:ec2::254/128"), netip.MustParsePrefix("168.63.129.16/32")}

// Error is RegistryError: what went wrong, in the words of whoever must fix it.
type Error struct{ Message string }

func (e *Error) Error() string { return e.Message }

func fail(format string, a ...any) *Error { return &Error{fmt.Sprintf(format, a...)} }

// TransportError is an httpx.TransportError the Python code does not catch: main.py answers
// it with a 503 («A service Tabularia calls is not answering»).
type TransportError struct{ Err error }

func (e *TransportError) Error() string { return e.Err.Error() }

// Registry is container_registry.Registry.
type Registry struct {
	Host     string
	Protocol string // https | http
	TLS      string // public | ca | none
	CACert   string
	Username string
	Password string
}

// APIHost is api_host: Docker Hub's API answers elsewhere than its images' names.
func APIHost(host string) string {
	if host == "docker.io" || host == "index.docker.io" {
		return "registry-1.docker.io"
	}
	return host
}

// RepositoryName is repository_name: python → library/python on Docker Hub.
func RepositoryName(host, repo string) string {
	repo = strings.Trim(strings.TrimSpace(repo), "/")
	if (host == "docker.io" || host == "index.docker.io") && !strings.Contains(repo, "/") {
		return "library/" + repo
	}
	return repo
}

// Allowed is allowed: an exact repository, a prefix ending in /*, or *.
func Allowed(patterns []string, repo string) bool {
	for _, p := range patterns {
		p = strings.Trim(strings.TrimSpace(p), "/")
		if p == "*" || p == repo || (strings.HasSuffix(p, "/*") && strings.HasPrefix(repo, p[:len(p)-1])) {
			return true
		}
	}
	return false
}

// CheckHost is check_host.
func CheckHost(host string) error {
	before, _, _ := strings.Cut(host, ":")
	if !hostRe.MatchString(host) || len(before) < 1 || len(before) > 253 {
		return &Error{"The registry's address is a host name, with a port if needed: ghcr.io, registry.acme.internal:5000"}
	}
	return nil
}

// Resolve stands in for the name resolution (the tests); nil = the system's.
var Resolve func(ctx context.Context, name string) ([]netip.Addr, error)

// guard is _guard: an address that resolves to the cloud metadata endpoints is refused.
func guard(ctx context.Context, host string) error {
	name := host
	if strings.Count(host, ":") == 1 {
		name = host[:strings.LastIndex(host, ":")]
	}
	var addrs []netip.Addr
	var err error
	if Resolve != nil {
		addrs, err = Resolve(ctx, name)
	} else if ip, perr := netip.ParseAddr(name); perr == nil {
		addrs = []netip.Addr{ip}
	} else {
		addrs, err = net.DefaultResolver.LookupNetIP(ctx, "ip", name)
	}
	if err != nil || len(addrs) == 0 {
		return fail("The address %s does not resolve: check its name", host)
	}
	for _, ip := range addrs {
		ip = ip.Unmap().WithZone("")
		for _, m := range metadata {
			if m.Contains(ip) {
				return fail("%s resolves to a cloud metadata address, which is never a registry", host)
			}
		}
	}
	return nil
}

// tlsConfig is _verify: nil to check against the system's authorities, InsecureSkipVerify for
// plain HTTP or tls «none», the company authority alone for «ca».
func tlsConfig(r Registry) (*tls.Config, error) {
	if r.Protocol == "http" || r.TLS == "none" {
		return &tls.Config{InsecureSkipVerify: true}, nil //nolint:gosec — the administrator's choice
	}
	if r.TLS == "ca" {
		if !strings.Contains(r.CACert, "BEGIN CERTIFICATE") {
			return nil, &Error{"The authority's certificate is missing: paste it in PEM (-----BEGIN CERTIFICATE-----)"}
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM([]byte(r.CACert)) {
			return nil, &Error{"The authority's certificate cannot be read: paste it in PEM"}
		}
		return &tls.Config{RootCAs: pool}, nil
	}
	return &tls.Config{}, nil
}

// deadlineConn gives every read and write httpx's timeout: each, not the whole call.
type deadlineConn struct {
	net.Conn
	timeout time.Duration
}

func (c *deadlineConn) Read(b []byte) (int, error) {
	_ = c.Conn.SetReadDeadline(time.Now().Add(c.timeout))
	return c.Conn.Read(b)
}

func (c *deadlineConn) Write(b []byte) (int, error) {
	_ = c.Conn.SetWriteDeadline(time.Now().Add(c.timeout))
	return c.Conn.Write(b)
}

// kind names the httpx exception a Go error stands for, and its str().
func kind(err error) (string, string) { return httpxerr.Of(err) }

// explain is _explain.
func explain(err error, r Registry) *Error {
	name, text := kind(err)
	if name == "ConnectError" && (strings.Contains(text, "CERTIFICATE_VERIFY_FAILED") || strings.Contains(strings.ToLower(text), "certificate")) {
		if r.TLS == "ca" {
			return &Error{"The registry's certificate is not signed by the authority given: check that it is the one that signed the registry's certificate"}
		}
		return &Error{"The registry's certificate is not signed by a public authority: choose «Company authority» and paste your authority's certificate"}
	}
	if strings.Contains(text, "WRONG_VERSION_NUMBER") || strings.Contains(text, "wrong version number") {
		return &Error{"The registry answers in plain HTTP: choose HTTP as the protocol (internal registries only)"}
	}
	or := func(n int) string {
		if t := clip(text, n); t != "" {
			return t
		}
		return name
	}
	switch name {
	case "ConnectError", "ConnectTimeout":
		return fail("The gateway cannot reach %s: %s", r.Host, or(200))
	case "ReadTimeout":
		return fail("%s did not answer within %d s", r.Host, int(Timeout/time.Second))
	case "RemoteProtocolError":
		if r.Protocol == "http" {
			return &Error{"The registry does not answer in plain HTTP: choose HTTPS as the protocol"}
		}
	}
	return fail("%s: %s", r.Host, or(300))
}

func clip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// response is what the Python code reads of an httpx.Response.
type response struct {
	Status int
	Header http.Header
	Body   []byte
}

func (r *response) text() string { return strings.ToValidUTF8(string(r.Body), "�") }

// get is the response's header as httpx's Headers.get: several values joined by «, ».
func (r *response) get(name string) string { return strings.Join(r.Header.Values(name), ", ") }

// json is resp.json(): an error where Python raises a JSONDecodeError (a 500).
func (r *response) json() (any, error) {
	return pyjson.Loads(strings.TrimPrefix(r.text(), "\uFEFF"))
}

// Session is _Session: one conversation with a registry, the challenge read once, a token
// per scope.
type Session struct {
	r      Registry
	base   string
	client *http.Client
	scheme string // "" until the ping
	params map[string]string
	tokens map[string]string
}

// open is _Session(r) and its __aenter__.
func open(ctx context.Context, r Registry) (*Session, error) {
	conf, err := tlsConfig(r)
	if err != nil {
		return nil, err
	}
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment, // httpx's trust_env
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			c, err := (&net.Dialer{Timeout: Timeout}).DialContext(ctx, network, address)
			if err != nil {
				return nil, err
			}
			return &deadlineConn{c, Timeout}, nil
		},
		TLSClientConfig:     conf,
		TLSHandshakeTimeout: Timeout,
		ForceAttemptHTTP2:   false,
	}
	s := &Session{r: r, base: r.Protocol + "://" + APIHost(r.Host), params: map[string]string{}, tokens: map[string]string{},
		client: &http.Client{Transport: transport, CheckRedirect: func(_ *http.Request, via []*http.Request) error {
			if len(via) > 20 { // httpx's max_redirects
				return errors.New("Exceeded maximum allowed redirects.")
			}
			return nil
		}}}
	if err := guard(ctx, APIHost(r.Host)); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Session) close() { s.client.CloseIdleConnections() }

// do sends a request; a network fault is the httpx error it would be (raw, for _explain).
func (s *Session) do(ctx context.Context, method, target string, header map[string]string, basic bool) (*response, error) {
	req, err := http.NewRequestWithContext(ctx, method, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "python-httpx/0.28.1")
	req.Header.Set("Accept", "*/*")
	for k, v := range header {
		req.Header.Set(k, v)
	}
	if basic {
		req.SetBasicAuth(s.r.Username, s.r.Password)
	}
	res, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}
	return &response{Status: res.StatusCode, Header: res.Header, Body: body}, nil
}

// explained is a request whose network fault is _explain's RegistryError.
func (s *Session) explained(ctx context.Context, method, target string, header map[string]string, basic bool) (*response, error) {
	res, err := s.do(ctx, method, target, header, basic)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var redirects bool
		if ue, ok := err.(*url.Error); ok && strings.Contains(ue.Err.Error(), "maximum allowed redirects") {
			redirects = true
		}
		if redirects { // httpx.TooManyRedirects: an HTTPError, explained too
			return nil, fail("%s: Exceeded maximum allowed redirects.", s.r.Host)
		}
		return nil, explain(err, s.r)
	}
	return res, nil
}

// Ping is _Session.ping: how the registry wants to be spoken to.
func (s *Session) Ping(ctx context.Context) (string, error) {
	res, err := s.explained(ctx, http.MethodGet, s.base+"/v2/", nil, false)
	if err != nil {
		return "", err
	}
	if res.Status == 200 {
		s.scheme = "anonymous"
		if s.r.Username != "" {
			s.scheme = "basic"
		}
		return s.scheme, nil
	}
	if res.Status == 400 && strings.Contains(res.text(), "HTTPS server") {
		return "", &Error{"The registry answers in HTTPS: choose HTTPS as the protocol"}
	}
	if res.Status != 401 {
		return "", fail("%s is not a container registry (it answered %d on /v2/)", s.r.Host, res.Status)
	}
	s.scheme, s.params = challenge(res.get("www-authenticate"))
	if s.scheme != "basic" && s.scheme != "bearer" {
		named := s.scheme
		if named == "" {
			named = "none"
		}
		return "", fail("%s asks for an authentication Tabularia does not speak (%s)", s.r.Host, named)
	}
	if s.scheme == "basic" {
		if s.r.Username == "" {
			return "", &Error{"The registry asks for a user and a password"}
		}
		// not under _explain in Python: a network fault here is an httpx error that escapes
		check, err := s.do(ctx, http.MethodGet, s.base+"/v2/", nil, true)
		if err != nil {
			return "", &TransportError{err}
		}
		if check.Status == 401 || check.Status == 403 {
			return "", &Error{"The registry refused the user and the password"}
		}
	}
	return s.scheme, nil
}

// challenge is _challenge: «Bearer realm="…",service="…"» → ("bearer", {realm, service}).
func challenge(header string) (string, map[string]string) {
	h := strings.TrimSpace(header)
	scheme, rest, _ := strings.Cut(h, " ")
	params := map[string]string{}
	for _, m := range paramRe.FindAllStringSubmatch(rest, -1) {
		params[strings.ToLower(m[1])] = m[2]
	}
	return strings.ToLower(scheme), params
}

// quotePlus is urllib.parse.quote_plus.
func quotePlus(s string) string { return url.QueryEscape(s) }

// withParams is httpx's merge of `params` into a URL that may already have a query.
func withParams(realm string, params [][2]string) string {
	base, query, _ := strings.Cut(realm, "?")
	var pairs []string
	if query != "" {
		for _, kv := range strings.Split(query, "&") {
			if kv == "" {
				continue
			}
			k, v, _ := strings.Cut(kv, "=")
			ku, _ := url.QueryUnescape(k)
			vu, _ := url.QueryUnescape(v)
			pairs = append(pairs, quotePlus(ku)+"="+quotePlus(vu))
		}
	}
	for _, p := range params {
		pairs = append(pairs, quotePlus(p[0])+"="+quotePlus(p[1]))
	}
	if len(pairs) == 0 {
		return base
	}
	return base + "?" + strings.Join(pairs, "&")
}

// Token is _Session._token.
func (s *Session) Token(ctx context.Context, scope string) (string, error) {
	if t, ok := s.tokens[scope]; ok {
		return t, nil
	}
	realm := s.params["realm"]
	if !strings.HasPrefix(realm, "https://") && !strings.HasPrefix(realm, "http://") {
		return "", fail("%s names no place to ask for a token", s.r.Host)
	}
	if err := guard(ctx, netloc(realm)); err != nil {
		return "", err
	}
	var query [][2]string
	if svc := s.params["service"]; svc != "" {
		query = append(query, [2]string{"service", svc})
	}
	if scope != "" {
		query = append(query, [2]string{"scope", scope})
	}
	auth := s.r.Username != ""
	res, err := s.explained(ctx, http.MethodGet, withParams(realm, query), nil, auth)
	if err != nil {
		return "", err
	}
	if res.Status == 401 || res.Status == 403 {
		if auth {
			return "", &Error{"The registry refused the user and the password"}
		}
		return "", &Error{"The registry asks for a user and a password"}
	}
	if res.Status != 200 {
		return "", fail("%s refused a token (%d)", s.r.Host, res.Status)
	}
	body, err := res.json()
	if err != nil {
		return "", err
	}
	o, ok := body.(pyjson.Object)
	if !ok {
		return "", errors.New("registry: the token's answer is not a dict")
	}
	var token any = ""
	for _, k := range []string{"token", "access_token"} {
		if v, _ := o.Get(k); pyjson.Truthy(v) {
			token = v
			break
		}
	}
	if !pyjson.Truthy(token) {
		return "", fail("%s gave no token", s.r.Host)
	}
	t := pyjson.Str(token)
	s.tokens[scope] = t
	return t, nil
}

// netloc is urlsplit(u).netloc.
func netloc(u string) string {
	_, rest, _ := strings.Cut(u, "//")
	if i := strings.IndexAny(rest, "/?#"); i >= 0 {
		rest = rest[:i]
	}
	return rest
}

// Get is _Session.get.
func (s *Session) Get(ctx context.Context, path, scope, method string, header map[string]string) (*response, error) {
	if s.scheme == "" {
		if _, err := s.Ping(ctx); err != nil {
			return nil, err
		}
	}
	h := map[string]string{}
	for k, v := range header {
		h[k] = v
	}
	basic := false
	switch s.scheme {
	case "bearer":
		t, err := s.Token(ctx, scope)
		if err != nil {
			return nil, err
		}
		h["Authorization"] = "Bearer " + t
	case "basic":
		basic = true
	}
	target := path
	if !strings.HasPrefix(path, "http") {
		target = s.base + path
	}
	return s.explained(ctx, method, target, h, basic)
}

func notVisible(repo string, status int, anonymous bool) *Error {
	if status == 401 || status == 403 {
		if anonymous {
			return fail("%s is private: the registry asks for a user and a password to read it", repo)
		}
		return fail("The credentials cannot read %s", repo)
	}
	return fail("%s is not in the registry, or not visible with these credentials", repo)
}

// Found is ping's answer.
type Found struct {
	Auth       string
	Repository *string
	Tags       []string // nil: none read
}

// Ping is ping: the address, the certificate, the credentials and, given a repository of
// the allowlist, the right to read it.
func Ping(ctx context.Context, r Registry, repo string) (*Found, error) {
	if err := CheckHost(r.Host); err != nil {
		return nil, err
	}
	s, err := open(ctx, r)
	if err != nil {
		return nil, err
	}
	defer s.close()
	scheme, err := s.Ping(ctx)
	if err != nil {
		return nil, err
	}
	out := &Found{Auth: scheme}
	if s.scheme == "bearer" && r.Username != "" {
		if _, err := s.Token(ctx, ""); err != nil {
			return nil, err
		}
	}
	if repo != "" {
		found, err := tags(ctx, r, repo, s, 5)
		if err != nil {
			return nil, err
		}
		name := RepositoryName(r.Host, repo)
		out.Repository, out.Tags = &name, found
	}
	return out, nil
}

// Tags is tags: the tags of a repository, at most MaxTags.
func Tags(ctx context.Context, r Registry, repo string) ([]string, error) {
	return tags(ctx, r, repo, nil, MaxTags)
}

func tags(ctx context.Context, r Registry, repoGiven string, s *Session, limit int) ([]string, error) {
	repo := RepositoryName(r.Host, repoGiven)
	if !repository.MatchString(repo) {
		return nil, fail("«%s» is not a repository name", repoGiven)
	}
	if s == nil {
		if err := CheckHost(r.Host); err != nil {
			return nil, err
		}
		var err error
		if s, err = open(ctx, r); err != nil {
			return nil, err
		}
		defer s.close()
	}
	scope := "repository:" + repo + ":pull"
	out := []string{}
	next := fmt.Sprintf("/v2/%s/tags/list?n=%d", repo, min(limit, 1000))
	for next != "" && len(out) < limit {
		res, err := s.Get(ctx, next, scope, http.MethodGet, nil)
		if err != nil {
			return nil, err
		}
		if res.Status != 200 {
			return nil, notVisible(repo, res.Status, r.Username == "")
		}
		body, err := res.json()
		if err != nil {
			return nil, err
		}
		o, ok := body.(pyjson.Object)
		if !ok {
			return nil, errors.New("registry: the tags' answer is not a dict")
		}
		v, _ := o.Get("tags")
		items, err := iterate(v)
		if err != nil {
			return nil, err
		}
		for _, t := range items {
			if ts, ok := t.(string); ok {
				out = append(out, ts)
			}
		}
		next = ""
		if m := nextRe.FindStringSubmatch(res.get("link")); m != nil {
			next = m[1]
			if !strings.HasPrefix(next, "http") && !strings.HasPrefix(next, "/") {
				next = "/" + next
			}
		}
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// iterate is Python's iteration of `(v or [])`: a list's items, a dict's keys, a text's
// characters; a number or True is a TypeError.
func iterate(v any) ([]any, error) {
	if !pyjson.Truthy(v) {
		return nil, nil
	}
	switch x := v.(type) {
	case []any:
		return x, nil
	case pyjson.Object:
		out := make([]any, len(x))
		for i, kv := range x {
			out[i] = kv.Key
		}
		return out, nil
	case string:
		var out []any
		for _, c := range x {
			out = append(out, string(c))
		}
		return out, nil
	}
	return nil, errors.New("registry: not iterable")
}

// quote is urllib.parse.quote (safe "/").
func quote(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || strings.IndexByte("_.-~/", c) >= 0 {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// Digest is digest: the manifest a tag points to now (a digest is returned as it is).
func Digest(ctx context.Context, r Registry, repoGiven, reference string) (string, error) {
	repo := RepositoryName(r.Host, repoGiven)
	if digestRe.MatchString(reference) {
		return reference, nil
	}
	if !repository.MatchString(repo) || !tagRe.MatchString(reference) {
		return "", fail("«%s:%s» is not an image name", repoGiven, reference)
	}
	if err := CheckHost(r.Host); err != nil {
		return "", err
	}
	s, err := open(ctx, r)
	if err != nil {
		return "", err
	}
	defer s.close()
	res, err := s.Get(ctx, "/v2/"+repo+"/manifests/"+quote(reference), "repository:"+repo+":pull", http.MethodHead,
		map[string]string{"Accept": ManifestTypes})
	if err != nil {
		return "", err
	}
	if res.Status == 404 {
		return "", fail("%s has no tag «%s»", repo, reference)
	}
	if res.Status != 200 {
		return "", notVisible(repo, res.Status, r.Username == "")
	}
	found := res.get("docker-content-digest")
	if !digestRe.MatchString(found) {
		return "", fail("%s did not say which manifest «%s» is", r.Host, reference)
	}
	return found, nil
}

// MaxConfigBytes is MAX_CONFIG_BYTES.
const MaxConfigBytes = 2 * 1 << 20

var indexTypes = map[string]bool{"application/vnd.oci.image.index.v1+json": true,
	"application/vnd.docker.distribution.manifest.list.v2+json": true}

func dict(v any) pyjson.Object {
	if !pyjson.Truthy(v) {
		return nil
	}
	o, _ := v.(pyjson.Object)
	return o
}

func get(o pyjson.Object, k string) any { v, _ := o.Get(k); return v }

// platform is _platform ("/".join of the truthy ones: a text each, or a TypeError).
func platform(p pyjson.Object) (string, error) {
	var parts []string
	for _, k := range []string{"os", "architecture", "variant"} {
		if v := get(p, k); pyjson.Truthy(v) {
			s, ok := v.(string)
			if !ok {
				return "", errors.New("registry: a platform's part is not a text")
			}
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, "/"), nil
}

// pyInt is int(x) of a JSON value: an error where Python raises.
func pyInt(v any) (int64, error) {
	switch x := v.(type) {
	case nil:
		return 0, nil
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case int64:
		return x, nil
	case float64:
		return int64(x), nil
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(strings.ReplaceAll(x, "_", "")), 10, 64)
		return n, err
	}
	return 0, errors.New("registry: not a number")
}

// ImageConfig is image_config: what an image does when nothing overrides it.
func ImageConfig(ctx context.Context, r Registry, repoGiven, reference string) (pyjson.Object, error) {
	repo := RepositoryName(r.Host, repoGiven)
	if !repository.MatchString(repo) || !(tagRe.MatchString(reference) || digestRe.MatchString(reference)) {
		return nil, fail("«%s:%s» is not an image name", repoGiven, reference)
	}
	if err := CheckHost(r.Host); err != nil {
		return nil, err
	}
	s, err := open(ctx, r)
	if err != nil {
		return nil, err
	}
	defer s.close()
	scope := "repository:" + repo + ":pull"
	manifest := func(ref string) (any, string, error) {
		res, err := s.Get(ctx, "/v2/"+repo+"/manifests/"+quote(ref), scope, http.MethodGet, map[string]string{"Accept": ManifestTypes})
		if err != nil {
			return nil, "", err
		}
		if res.Status == 404 {
			return nil, "", fail("%s has no tag «%s»", repo, ref)
		}
		if res.Status != 200 {
			return nil, "", notVisible(repo, res.Status, r.Username == "")
		}
		v, err := res.json()
		if err != nil {
			return nil, "", fail("%s answered with a manifest that is not JSON", r.Host)
		}
		return v, res.get("docker-content-digest"), nil
	}
	raw, found, err := manifest(reference)
	if err != nil {
		return nil, err
	}
	body, ok := raw.(pyjson.Object)
	if !ok {
		return nil, errors.New("registry: the manifest is not a dict")
	}
	var platforms []any
	_, hasManifests := body.Get("manifests")
	if mt, _ := body.Get("mediaType"); (isStr(mt) && indexTypes[mt.(string)]) || hasManifests {
		items, err := iterate(get(body, "manifests"))
		if err != nil {
			return nil, err
		}
		var entries []pyjson.Object
		for _, m := range items {
			mo, ok := m.(pyjson.Object)
			if !ok {
				continue
			}
			if pv := get(mo, "platform"); pyjson.Truthy(pv) && dict(pv) == nil {
				return nil, errors.New("registry: a platform not a dict")
			}
			os := get(dict(get(mo, "platform")), "os")
			if os == nil || os == "unknown" {
				continue
			}
			entries = append(entries, mo)
		}
		for _, m := range entries {
			p, ok := get(m, "platform").(pyjson.Object)
			if !ok {
				return nil, errors.New("registry: a platform not a dict")
			}
			text, err := platform(p)
			if err != nil {
				return nil, err
			}
			platforms = append(platforms, text)
		}
		if len(entries) == 0 {
			return nil, fail("%s:%s names no image for any platform", repo, reference)
		}
		chosen := entries[0]
		for _, m := range entries {
			p := get(m, "platform").(pyjson.Object)
			if get(p, "os") == "linux" && get(p, "architecture") == "amd64" {
				chosen = m
				break
			}
		}
		dg, has := chosen.Get("digest")
		if !has {
			return nil, errors.New("registry: a manifest without its digest") // KeyError
		}
		raw, _, err = manifest(pyjson.Str(dg))
		if err != nil {
			return nil, err
		}
		if body, ok = raw.(pyjson.Object); !ok {
			return nil, errors.New("registry: the manifest is not a dict")
		}
	}
	config := dict(get(body, "config"))
	cd := get(config, "digest")
	cds := ""
	if pyjson.Truthy(cd) {
		cds = pyjson.Str(cd)
	}
	if !digestRe.MatchString(cds) {
		return nil, fail("%s:%s has no configuration Tabularia can read", repo, reference)
	}
	size, err := pyInt(orNil(get(config, "size")))
	if err != nil {
		return nil, err
	}
	if size > MaxConfigBytes {
		return nil, fail("the configuration of %s:%s is too large to read", repo, reference)
	}
	res, err := s.Get(ctx, "/v2/"+repo+"/blobs/"+cds, scope, http.MethodGet, nil)
	if err != nil {
		return nil, err
	}
	if res.Status != 200 {
		return nil, notVisible(repo, res.Status, r.Username == "")
	}
	bv, err := res.json()
	if err != nil {
		return nil, fail("the configuration of %s:%s is not JSON", repo, reference)
	}
	blob, ok := bv.(pyjson.Object)
	if !ok {
		return nil, errors.New("registry: the configuration is not a dict")
	}
	c := dict(get(blob, "config"))
	if pyjson.Truthy(get(blob, "config")) && c == nil {
		return nil, errors.New("registry: the configuration's config is not a dict")
	}
	env := []any{}
	envItems, err := iterate(get(c, "Env"))
	if err != nil {
		return nil, err
	}
	for _, item := range envItems {
		name, value, _ := strings.Cut(pyjson.Str(item), "=")
		env = append(env, pyjson.Object{{Key: "name", Value: name}, {Key: "value", Value: value}})
	}
	if platforms == nil {
		platforms = []any{}
		if pyjson.Truthy(get(blob, "architecture")) {
			text, err := platform(blob)
			if err != nil {
				return nil, err
			}
			platforms = []any{text}
		}
	}
	strs := func(key string) ([]any, error) {
		items, err := iterate(get(c, key))
		out := []any{}
		for _, x := range items {
			out = append(out, pyjson.Str(x))
		}
		return out, err
	}
	entrypoint, err := strs("Entrypoint")
	if err != nil {
		return nil, err
	}
	cmd, err := strs("Cmd")
	if err != nil {
		return nil, err
	}
	labels := pyjson.Object{}
	if lv := get(c, "Labels"); pyjson.Truthy(lv) {
		lo, ok := lv.(pyjson.Object)
		if !ok {
			return nil, errors.New("registry: the labels are not a dict")
		}
		for _, kv := range lo {
			labels = append(labels, pyjson.KV{Key: kv.Key, Value: pyjson.Str(kv.Value)})
		}
	}
	var total int64
	layers, err := iterate(get(body, "layers"))
	if err != nil {
		return nil, err
	}
	for _, x := range layers {
		if xo, ok := x.(pyjson.Object); ok {
			n, err := pyInt(orNil(get(xo, "size")))
			if err != nil {
				return nil, err
			}
			total += n
		}
	}
	var sizeOut any
	if total != 0 {
		sizeOut = total
	}
	var digest any
	if digestRe.MatchString(found) {
		digest = found
	}
	return pyjson.Object{
		{Key: "repository", Value: repo}, {Key: "reference", Value: reference}, {Key: "digest", Value: digest},
		{Key: "platforms", Value: platforms}, {Key: "created", Value: get(blob, "created")},
		{Key: "entrypoint", Value: entrypoint}, {Key: "cmd", Value: cmd}, {Key: "env", Value: env}, {Key: "labels", Value: labels},
		{Key: "working_dir", Value: orNil(get(c, "WorkingDir"))}, {Key: "user", Value: orNil(get(c, "User"))},
		{Key: "size", Value: sizeOut},
	}, nil
}

// orNil is `x or None`.
func orNil(v any) any {
	if !pyjson.Truthy(v) {
		return nil
	}
	return v
}

func isStr(v any) bool { _, ok := v.(string); return ok }
