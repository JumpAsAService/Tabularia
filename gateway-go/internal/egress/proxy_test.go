package egress

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// The egress proxy of the scripts without internet access: the storage's hosts only,
// resolved by the proxy and checked after the resolution, through CONNECT, plain HTTP or
// the transparent ports.

// fakeDNS answers the names the tests use; every other one fails, as a name the cluster's
// DNS does not know.
type fakeDNS struct {
	mu    sync.Mutex
	names map[string][]netip.Addr
	asked []string
}

func (f *fakeDNS) lookup(_ context.Context, host string) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, host)
	if a, ok := f.names[host]; ok {
		return a, nil
	}
	return nil, fmt.Errorf("no such host %s", host)
}

func (f *fakeDNS) wasAsked(host string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, h := range f.asked {
		if h == host {
			return true
		}
	}
	return false
}

// a backend that says what it received
func echoServer(t *testing.T, tlsOn bool) *httptest.Server {
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		fmt.Fprintf(w, "%s %s host=%s body=%s", r.Method, r.URL.RequestURI(), r.Host, body)
	})
	var s *httptest.Server
	if tlsOn {
		s = httptest.NewTLSServer(h)
	} else {
		s = httptest.NewServer(h)
	}
	t.Cleanup(s.Close)
	return s
}

func portOf(t *testing.T, s *httptest.Server) string {
	u, err := url.Parse(s.URL)
	if err != nil {
		t.Fatal(err)
	}
	return u.Port()
}

type harness struct {
	p       *Proxy
	dns     *fakeDNS
	logs    *syncBuffer
	forward string // the forward proxy's address
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// newHarness starts a proxy allowing `allowed`; the loopback is let in unless strict (the
// tests' backends are on it), the rest of Forbidden holds.
func newHarness(t *testing.T, strict bool, allowed ...string) *harness {
	return newHarnessWith(t, strict, nil, allowed...)
}

// newHarnessWith sets the proxy's fields with `set` before it serves.
func newHarnessWith(t *testing.T, strict bool, set func(*Proxy), allowed ...string) *harness {
	h := &harness{dns: &fakeDNS{names: map[string][]netip.Addr{}}, logs: &syncBuffer{}}
	list := map[string]bool{}
	for _, a := range allowed {
		list[a] = true
	}
	h.p = &Proxy{Allowed: func() map[string]bool { return list }, Lookup: h.dns.lookup,
		Log: slog.New(slog.NewTextHandler(h.logs, nil)), IdleTimeout: 2 * time.Second, HelloTimeout: 2 * time.Second}
	if !strict {
		h.p.Forbid = func(a netip.Addr) bool { return Forbidden(a) && !a.Unmap().IsLoopback() }
	}
	if set != nil {
		set(h.p)
	}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go h.p.ServeForward(ctx, l)
	h.forward = l.Addr().String()
	return h
}

// transparent starts a transparent port for the storage's port `port`.
func (h *harness) transparent(t *testing.T, port int) string {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go h.p.ServeTransparent(ctx, l, port)
	return l.Addr().String()
}

func (h *harness) client(t *testing.T, rootCAs *x509.CertPool) *http.Client {
	proxyURL, _ := url.Parse("http://" + h.forward)
	return &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL),
		TLSClientConfig: &tls.Config{RootCAs: rootCAs}}}
}

func loopback() []netip.Addr { return []netip.Addr{netip.MustParseAddr("127.0.0.1")} }

func TestCONNECTReachesTheStorageAndNothingElse(t *testing.T) {
	backend := echoServer(t, true)
	port := portOf(t, backend)
	// httptest's certificate is for example.com: the name the client asks for
	h := newHarness(t, false, "example.com:"+port)
	h.dns.names["example.com"] = loopback()
	h.dns.names["pypi.org"] = loopback()
	roots := x509.NewCertPool()
	roots.AddCert(backend.Certificate())
	c := h.client(t, roots)

	resp, err := c.Get("https://example.com:" + port + "/bucket/key.parquet?x-id=GetObject")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || string(body) != "GET /bucket/key.parquet?x-id=GetObject host=example.com:"+port+" body=" {
		t.Fatalf("through the tunnel: %d %q", resp.StatusCode, body)
	}
	if !h.dns.wasAsked("example.com") {
		t.Error("the proxy did not resolve the name itself")
	}
	c.CloseIdleConnections()
	h.waitLog(t, "mode=connect target=example.com:"+port)

	// the internet: 403 from the proxy, never resolved nor reached
	for _, target := range []string{"https://pypi.org/simple/", "https://example.com:" + strconv.Itoa(mustAtoi(port)+1) + "/"} {
		_, err := c.Get(target)
		if err == nil || !strings.Contains(err.Error(), "Forbidden") {
			t.Errorf("%s: %v, want the proxy's 403", target, err)
		}
	}
	if h.dns.wasAsked("pypi.org") {
		t.Error("a host not allowed was resolved")
	}
	if !strings.Contains(h.logs.String(), "egress denied") || !strings.Contains(h.logs.String(), "target=pypi.org:443") {
		t.Errorf("the refusal is not in the log: %s", h.logs.String())
	}
}

// waitLog waits for a line of the log: a tunnel's is written when it closes.
func (h *harness) waitLog(t *testing.T, part string) {
	t.Helper()
	for end := time.Now().Add(3 * time.Second); time.Now().Before(end); time.Sleep(20 * time.Millisecond) {
		if strings.Contains(h.logs.String(), part) {
			return
		}
	}
	t.Errorf("no %q in the log: %s", part, h.logs.String())
}

func mustAtoi(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}

// The resolution happens in the proxy, and the address it gives is checked before any
// connection: an allowed name that leads to the metadata, a link-local or the loopback
// address is refused, and so is such an address written as the host.
func TestAnAllowedNameLeadingToAForbiddenAddressIsRefused(t *testing.T) {
	backend := echoServer(t, false)
	port := portOf(t, backend)
	h := newHarness(t, true, "storage.example:"+port, "metadata.example:80", "169.254.169.254:80", "[fd00:ec2::254]:80",
		"linklocal.example:443", "127.0.0.1:"+port)
	h.dns.names["storage.example"] = loopback() // the loopback: refused with the real rule
	h.dns.names["metadata.example"] = []netip.Addr{netip.MustParseAddr("169.254.169.254")}
	h.dns.names["linklocal.example"] = []netip.Addr{netip.MustParseAddr("fe80::1"), netip.MustParseAddr("::ffff:169.254.1.1")}
	for _, target := range []string{"storage.example:" + port, "metadata.example:80", "169.254.169.254:80", "[fd00:ec2::254]:80",
		"linklocal.example:443", "127.0.0.1:" + port} {
		status, body := rawConnect(t, h.forward, target)
		if status != 403 || !strings.Contains(body, "not reachable") {
			t.Errorf("CONNECT %s: %d %q, want 403", target, status, body)
		}
	}
	// plain HTTP too
	req := "GET http://metadata.example/latest/meta-data/ HTTP/1.1\r\nHost: metadata.example\r\n\r\n"
	if status, _ := rawRequest(t, h.forward, req); status != 403 {
		t.Errorf("GET through the proxy to the metadata: %d", status)
	}
	if !strings.Contains(h.logs.String(), "its addresses are not allowed") {
		t.Errorf("the log does not say why: %s", h.logs.String())
	}
}

func rawConnect(t *testing.T, proxy, target string) (int, string) {
	return rawRequest(t, proxy, "CONNECT "+target+" HTTP/1.1\r\nHost: "+target+"\r\n\r\n")
}

func rawRequest(t *testing.T, proxy, request string) (int, string) {
	t.Helper()
	c, err := net.DialTimeout("tcp", proxy, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.WriteString(c, request); err != nil {
		t.Fatal(err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(c), nil)
	if err != nil {
		return 0, err.Error()
	}
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

// Plain HTTP (a MinIO in http): the request goes on as it came, Host included (an S3
// request is signed with it), its body too.
func TestPlainHTTPThroughTheProxy(t *testing.T) {
	backend := echoServer(t, false)
	port := portOf(t, backend)
	h := newHarness(t, false, "minio.example:"+port)
	h.dns.names["minio.example"] = loopback()
	c := h.client(t, nil)
	req, _ := http.NewRequest(http.MethodPut, "http://minio.example:"+port+"/scripts/out.parquet", strings.NewReader("PAR1"))
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || string(body) != "PUT /scripts/out.parquet host=minio.example:"+port+" body=PAR1" {
		t.Fatalf("%d %q", resp.StatusCode, body)
	}
	resp, err = c.Get("http://pypi.org/simple/")
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 403 || resp.Header.Get("X-Tabularia-Egress") != "denied" || !strings.Contains(string(body), "no internet access") {
		t.Errorf("http://pypi.org: %d %q", resp.StatusCode, body)
	}
	// an https:// address without CONNECT, a request that is not a proxy's, the health check
	if status, _ := rawRequest(t, h.forward, "GET https://minio.example:"+port+"/ HTTP/1.1\r\nHost: minio.example\r\n\r\n"); status != 400 {
		t.Errorf("https without CONNECT: %d", status)
	}
	if status, _ := rawRequest(t, h.forward, "GET /anything HTTP/1.1\r\nHost: x\r\n\r\n"); status != 400 {
		t.Errorf("an origin-form request: %d", status)
	}
	if status, body := rawRequest(t, h.forward, "GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"); status != 200 || body != "ok\n" {
		t.Errorf("/healthz: %d %q", status, body)
	}
}

// The transparent ports: a client sent here by its /etc/hosts speaks TLS to the storage's
// name, and checks the storage's own certificate through the proxy.
func TestTransparentTLSPassesThroughByItsServerName(t *testing.T) {
	backend := echoServer(t, true)
	port := mustAtoi(portOf(t, backend))
	h := newHarness(t, false, net.JoinHostPort("example.com", strconv.Itoa(port)))
	h.dns.names["example.com"] = loopback()
	addr := h.transparent(t, port)
	roots := x509.NewCertPool()
	roots.AddCert(backend.Certificate())
	// what /etc/hosts does: the name, at the proxy's address
	dial := func(name string) *http.Client {
		return &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", addr)
			},
			TLSClientConfig: &tls.Config{RootCAs: roots, ServerName: name},
		}}
	}
	good := dial("example.com")
	resp, err := good.Get(fmt.Sprintf("https://example.com:%d/bucket/in.parquet", port))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || !strings.HasPrefix(string(body), "GET /bucket/in.parquet host=example.com") {
		t.Fatalf("%d %q", resp.StatusCode, body)
	}
	good.CloseIdleConnections()
	// another name: the connection is closed before anything reaches anywhere
	h.dns.names["pypi.org"] = loopback()
	if _, err := dial("pypi.org").Get(fmt.Sprintf("https://pypi.org:%d/", port)); err == nil {
		t.Error("pypi.org went through")
	}
	if h.dns.wasAsked("pypi.org") {
		t.Error("a name not allowed was resolved")
	}
	h.waitLog(t, "mode=tls target=example.com:")
}

func TestTransparentPlainHTTPGoesByItsHost(t *testing.T) {
	backend := echoServer(t, false)
	port := mustAtoi(portOf(t, backend))
	hostport := net.JoinHostPort("minio.example", strconv.Itoa(port))
	h := newHarness(t, false, hostport)
	h.dns.names["minio.example"] = loopback()
	addr := h.transparent(t, port)
	status, body := rawRequest(t, addr, "PUT /scripts/out.parquet HTTP/1.1\r\nHost: "+hostport+"\r\nContent-Length: 4\r\n\r\nPAR1")
	if status != 200 || body != "PUT /scripts/out.parquet host="+hostport+" body=PAR1" {
		t.Errorf("%d %q", status, body)
	}
	for _, host := range []string{"pypi.org", fmt.Sprintf("minio.example:%d", port+1), ""} {
		status, _ := rawRequest(t, addr, "GET / HTTP/1.1\r\nHost: "+host+"\r\n\r\n")
		if status == 200 {
			t.Errorf("Host %q went through", host)
		}
	}
}

// Limits: the connections of one client, the tunnel where nothing passes.
func TestTheLimitsHold(t *testing.T) {
	backend := echoServer(t, false)
	port := portOf(t, backend)
	h := newHarnessWith(t, false, func(p *Proxy) { p.MaxPerClient, p.IdleTimeout = 1, 300*time.Millisecond }, "minio.example:"+port)
	h.dns.names["minio.example"] = loopback()
	c, err := net.Dial("tcp", h.forward)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	fmt.Fprintf(c, "CONNECT minio.example:%s HTTP/1.1\r\nHost: minio.example\r\n\r\n", port)
	r := bufio.NewReader(c)
	resp, err := http.ReadResponse(r, nil)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("the tunnel: %v %v", resp, err)
	}
	// a second connection from the same address is closed at once
	if status, _ := rawConnect(t, h.forward, "minio.example:"+port); status != 0 {
		t.Errorf("a second connection got %d", status)
	}
	if !strings.Contains(h.logs.String(), "too many connections") {
		t.Errorf("the log: %s", h.logs.String())
	}
	// nothing passes: the tunnel closes after IdleTimeout
	_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
	start := time.Now()
	if _, err := r.ReadByte(); err != io.EOF {
		t.Errorf("the idle tunnel: %v", err)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Errorf("closed after %v", d)
	}
	h.p.Wait(context.Background()) // and once closed, nothing is counted open
}
