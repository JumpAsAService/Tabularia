package egress

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// Proxy is the egress proxy. Allowed says which host:port it may reach now (the storage's,
// HostsOf); it is asked at every connection, so a change of the scripts bucket holds at once.
type Proxy struct {
	Allowed func() map[string]bool
	Log     *slog.Logger
	// Lookup resolves a name (net.DefaultResolver.LookupNetIP); tests give their own
	Lookup func(ctx context.Context, host string) ([]netip.Addr, error)
	// Forbid is an address never connected to (nil = Forbidden); tests let their own loopback in
	Forbid func(netip.Addr) bool
	// at most this many connections at once, and this many from one address (0 = 1024, 256)
	MaxConns, MaxPerClient int
	// connecting to the storage; the first bytes of a client (its CONNECT, its request's
	// headers, its TLS hello); a tunnel where nothing passes either way (0 = 10 s, 15 s, 5 min)
	DialTimeout, HelloTimeout, IdleTimeout time.Duration

	once    sync.Once
	mu      sync.Mutex
	total   int
	clients map[string]int
	// the plain HTTP requests' connections to the storage, kept between requests; each new
	// one checked by dial
	transport *http.Transport
}

const maxHello = 64 << 10 // the most read of a client before knowing where it goes

func (p *Proxy) init() {
	p.once.Do(func() {
		if p.Log == nil {
			p.Log = slog.Default()
		}
		if p.Lookup == nil {
			p.Lookup = func(ctx context.Context, host string) ([]netip.Addr, error) {
				return net.DefaultResolver.LookupNetIP(ctx, "ip", host)
			}
		}
		if p.Forbid == nil {
			p.Forbid = Forbidden
		}
		if p.MaxConns <= 0 {
			p.MaxConns = 1024
		}
		if p.MaxPerClient <= 0 {
			p.MaxPerClient = 256
		}
		if p.DialTimeout <= 0 {
			p.DialTimeout = 10 * time.Second
		}
		if p.HelloTimeout <= 0 {
			p.HelloTimeout = 15 * time.Second
		}
		if p.IdleTimeout <= 0 {
			p.IdleTimeout = 5 * time.Minute
		}
		p.clients = map[string]int{}
		p.transport = &http.Transport{
			Proxy: nil,
			DialContext: func(ctx context.Context, _, addr string) (net.Conn, error) {
				return p.dial(ctx, addr)
			},
			ResponseHeaderTimeout: 5 * time.Minute, IdleConnTimeout: 30 * time.Second, MaxIdleConnsPerHost: 16,
		}
	})
}

// errDenied is a host the proxy does not serve; errAddress a host whose addresses are all
// forbidden
var (
	errDenied  = errors.New("not the storage")
	errAddress = errors.New("its addresses are not allowed (loopback, link-local, metadata)")
)

// allowed says whether host:port may be reached.
func (p *Proxy) allowed(hostport string) bool {
	host, port, err := net.SplitHostPort(hostport)
	if err != nil {
		return false
	}
	key := net.JoinHostPort(strings.TrimSuffix(strings.ToLower(host), "."), port)
	return p.Allowed != nil && p.Allowed()[key]
}

// dial connects to an allowed host:port: the proxy resolves the name, keeps only the
// addresses it may reach, and checks again the address the socket really connects to.
func (p *Proxy) dial(ctx context.Context, hostport string) (net.Conn, error) {
	p.init()
	if !p.allowed(hostport) {
		return nil, errDenied
	}
	host, port, _ := net.SplitHostPort(hostport)
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	ctx, cancel := context.WithTimeout(ctx, p.DialTimeout)
	defer cancel()
	var addrs []netip.Addr
	if a, err := netip.ParseAddr(host); err == nil {
		addrs = []netip.Addr{a}
	} else {
		found, err := p.Lookup(ctx, host)
		if err != nil {
			return nil, fmt.Errorf("resolving %s: %w", host, err)
		}
		addrs = found
	}
	d := net.Dialer{Control: func(_, address string, _ syscall.RawConn) error {
		ap, err := netip.ParseAddrPort(address)
		if err != nil || p.Forbid(ap.Addr()) {
			return errAddress
		}
		return nil
	}}
	var last error = errAddress
	for _, a := range addrs {
		if p.Forbid(a) {
			continue
		}
		conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort(a.Unmap().String(), port))
		if err == nil {
			return conn, nil
		}
		last = err
	}
	return nil, last
}

// Wait returns when no connection is open any more, or when ctx ends.
func (p *Proxy) Wait(ctx context.Context) {
	p.init()
	t := time.NewTicker(200 * time.Millisecond)
	defer t.Stop()
	for {
		p.mu.Lock()
		open := p.total
		p.mu.Unlock()
		if open == 0 {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// admit counts a connection in, or says no when the proxy or that client has too many.
func (p *Proxy) admit(client string) (func(), bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.total >= p.MaxConns || p.clients[client] >= p.MaxPerClient {
		return nil, false
	}
	p.total++
	p.clients[client]++
	return func() {
		p.mu.Lock()
		defer p.mu.Unlock()
		p.total--
		if p.clients[client]--; p.clients[client] <= 0 {
			delete(p.clients, client)
		}
	}, true
}

func clientOf(addr net.Addr) string {
	if ap, err := netip.ParseAddrPort(addr.String()); err == nil {
		return ap.Addr().Unmap().String()
	}
	return addr.String()
}

// limited is a listener whose connections over the limits are closed at once.
type limited struct {
	net.Listener
	p *Proxy
}

type countedConn struct {
	net.Conn
	done func()
	once sync.Once
}

func (c *countedConn) Close() error {
	c.once.Do(c.done)
	return c.Conn.Close()
}

// CloseWrite half-closes the connection, as *net.TCPConn does (pipe).
func (c *countedConn) CloseWrite() error {
	if tc, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return tc.CloseWrite()
	}
	return c.Close()
}

func (l limited) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		client := clientOf(c.RemoteAddr())
		done, ok := l.p.admit(client)
		if !ok {
			l.p.Log.Warn("egress refused: too many connections", "client", client)
			_ = c.Close()
			continue
		}
		return &countedConn{Conn: c, done: done}, nil
	}
}

// ServeForward answers the forward proxy on l (HTTPS_PROXY, HTTP_PROXY) until ctx ends.
func (p *Proxy) ServeForward(ctx context.Context, l net.Listener) error {
	p.init()
	srv := &http.Server{Handler: http.HandlerFunc(p.forward), ReadHeaderTimeout: p.HelloTimeout, IdleTimeout: 2 * time.Minute,
		ErrorLog: slog.NewLogLogger(p.Log.Handler(), slog.LevelDebug)}
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	err := srv.Serve(limited{l, p})
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (p *Proxy) deny(w http.ResponseWriter, r *http.Request, client, target string, why error) {
	p.Log.Warn("egress denied", "client", client, "mode", mode(r), "target", target, "why", why.Error())
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Tabularia-Egress", "denied")
	w.WriteHeader(http.StatusForbidden)
	fmt.Fprintf(w, "Tabularia's egress proxy: %s is not reachable. This scripting environment's version has no internet "+
		"access: its scripts reach the storage and nothing else (Administration → Scripting environments).\n", target)
}

func mode(r *http.Request) string {
	if r.Method == http.MethodConnect {
		return "connect"
	}
	return "http"
}

func (p *Proxy) forward(w http.ResponseWriter, r *http.Request) {
	client := clientOf(addrOf(r.RemoteAddr))
	switch {
	case r.Method == http.MethodConnect:
		target := r.Host
		if _, _, err := net.SplitHostPort(target); err != nil {
			target = net.JoinHostPort(target, "443")
		}
		if !p.allowed(target) {
			p.deny(w, r, client, target, errDenied)
			return
		}
		up, err := p.dial(r.Context(), target)
		if err != nil {
			p.fail(w, r, client, target, err)
			return
		}
		hj, ok := w.(http.Hijacker)
		if !ok {
			_ = up.Close()
			http.Error(w, "the connection cannot be taken over", http.StatusInternalServerError)
			return
		}
		conn, buf, err := hj.Hijack()
		if err != nil {
			_ = up.Close()
			return
		}
		_ = conn.SetDeadline(time.Time{})
		if _, err := conn.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n")); err != nil {
			_ = conn.Close()
			_ = up.Close()
			return
		}
		// what the client sent after its CONNECT, already read
		var early []byte
		if n := buf.Reader.Buffered(); n > 0 {
			early, _ = buf.Reader.Peek(n)
		}
		p.pipe(conn, up, early, client, "connect", target)
	case r.URL.IsAbs():
		if !strings.EqualFold(r.URL.Scheme, "http") {
			http.Error(w, "only http:// requests here; https goes through CONNECT", http.StatusBadRequest)
			return
		}
		target := r.URL.Host
		if r.URL.Port() == "" {
			target = net.JoinHostPort(r.URL.Hostname(), "80")
		}
		if !p.allowed(target) {
			p.deny(w, r, client, target, errDenied)
			return
		}
		start := time.Now()
		var failed error
		rp := &httputil.ReverseProxy{
			// the request as it came, Host and all: an S3 request is signed with it
			Rewrite:   func(pr *httputil.ProxyRequest) { pr.Out.URL.Host = target },
			Transport: p.transport,
			ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
				failed = err
				p.fail(w, r, client, target, err)
			},
		}
		rp.ServeHTTP(w, r)
		if failed == nil {
			p.Log.Info("egress", "client", client, "mode", "http", "target", target, "method", r.Method,
				"seconds", time.Since(start).Seconds())
		}
	case r.URL.Path == "/healthz":
		_, _ = io.WriteString(w, "ok\n")
	default:
		http.Error(w, "a proxy: send CONNECT host:port, or an absolute http:// address", http.StatusBadRequest)
	}
}

// fail answers a target that is allowed but could not be reached (or only at forbidden
// addresses): 403 for these, 502 otherwise.
func (p *Proxy) fail(w http.ResponseWriter, r *http.Request, client, target string, err error) {
	if errors.Is(err, errDenied) || errors.Is(err, errAddress) {
		p.deny(w, r, client, target, err)
		return
	}
	p.Log.Warn("egress unreachable", "client", client, "mode", mode(r), "target", target, "err", err.Error())
	http.Error(w, fmt.Sprintf("Tabularia's egress proxy could not reach %s: %v", target, err), http.StatusBadGateway)
}

func addrOf(s string) net.Addr {
	if ap, err := netip.ParseAddrPort(s); err == nil {
		return net.TCPAddrFromAddrPort(ap)
	}
	return &net.TCPAddr{}
}

// ServeTransparent answers on l the clients sent here by their /etc/hosts, for the storage's
// port `port`: it reads the name they asked for (TLS SNI, or the HTTP Host header) and
// passes their bytes through to that name, when it is allowed.
func (p *Proxy) ServeTransparent(ctx context.Context, l net.Listener, port int) error {
	p.init()
	go func() {
		<-ctx.Done()
		_ = l.Close()
	}()
	ll := limited{l, p}
	for {
		c, err := ll.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				continue
			}
			return err
		}
		go p.transparent(ctx, c, port)
	}
}

func (p *Proxy) transparent(ctx context.Context, c net.Conn, port int) {
	client := clientOf(c.RemoteAddr())
	_ = c.SetReadDeadline(time.Now().Add(p.HelloTimeout))
	var seen bytes.Buffer
	in := io.TeeReader(io.LimitReader(c, maxHello), &seen)
	first := make([]byte, 1)
	if _, err := io.ReadFull(in, first); err != nil {
		_ = c.Close()
		return
	}
	rest := io.MultiReader(bytes.NewReader(first), in)
	var host, how string
	if first[0] == 0x16 { // a TLS handshake record
		how = "tls"
		host = serverName(rest)
	} else {
		how = "plain"
		if req, err := http.ReadRequest(bufio.NewReader(rest)); err == nil {
			host = req.Host
			if h, hp, err := net.SplitHostPort(host); err == nil {
				if hp != strconv.Itoa(port) {
					host = "" // a port other than the one this listener serves
				} else {
					host = h
				}
			}
		}
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	target := net.JoinHostPort(host, strconv.Itoa(port))
	if host == "" {
		p.Log.Warn("egress denied", "client", client, "mode", how, "why", "no name to go to (SNI or Host)")
		_ = c.Close()
		return
	}
	if !p.allowed(target) {
		p.Log.Warn("egress denied", "client", client, "mode", how, "target", target, "why", errDenied.Error())
		if how == "plain" {
			_, _ = io.WriteString(c, "HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\nX-Tabularia-Egress: denied\r\n\r\n"+
				"Tabularia's egress proxy: "+target+" is not reachable from a scripting environment's version without internet access.\n")
		}
		_ = c.Close()
		return
	}
	up, err := p.dial(ctx, target)
	if err != nil {
		p.Log.Warn("egress unreachable", "client", client, "mode", how, "target", target, "err", err.Error())
		_ = c.Close()
		return
	}
	_ = c.SetReadDeadline(time.Time{})
	p.pipe(c, up, seen.Bytes(), client, how, target)
}

// serverName is the name a TLS ClientHello asks for, "" without one: crypto/tls reads the
// hello, and the handshake stops right there.
func serverName(r io.Reader) string {
	var name string
	stop := errors.New("hello read")
	_ = tls.Server(helloConn{r: r}, &tls.Config{GetConfigForClient: func(h *tls.ClientHelloInfo) (*tls.Config, error) {
		name = h.ServerName
		return nil, stop
	}}).Handshake()
	return name
}

// helloConn reads from r and writes nowhere: the alert of the stopped handshake never goes
// to the client, whose bytes are passed on as they are.
type helloConn struct {
	net.Conn
	r io.Reader
}

func (h helloConn) Read(b []byte) (int, error)       { return h.r.Read(b) }
func (h helloConn) Write(b []byte) (int, error)      { return len(b), nil }
func (h helloConn) Close() error                     { return nil }
func (h helloConn) SetDeadline(time.Time) error      { return nil }
func (h helloConn) SetReadDeadline(time.Time) error  { return nil }
func (h helloConn) SetWriteDeadline(time.Time) error { return nil }
func (h helloConn) LocalAddr() net.Addr              { return &net.TCPAddr{} }
func (h helloConn) RemoteAddr() net.Addr             { return &net.TCPAddr{} }

// pipe passes the bytes both ways, `early` first to the storage, until both sides are done
// or nothing passes for IdleTimeout; then it writes the connection's line in the log.
func (p *Proxy) pipe(client, up net.Conn, early []byte, who, how, target string) {
	start := time.Now()
	var sent, received atomic.Int64
	if len(early) > 0 {
		n, err := up.Write(early)
		sent.Add(int64(n))
		if err != nil {
			_ = client.Close()
			_ = up.Close()
			return
		}
	}
	var wg sync.WaitGroup
	copyIdle := func(dst, src net.Conn, count *atomic.Int64) {
		defer wg.Done()
		buf := make([]byte, 32<<10)
		for {
			_ = src.SetReadDeadline(time.Now().Add(p.IdleTimeout))
			n, err := src.Read(buf)
			if n > 0 {
				_ = dst.SetWriteDeadline(time.Now().Add(p.IdleTimeout))
				if _, werr := dst.Write(buf[:n]); werr != nil {
					break
				}
				count.Add(int64(n))
			}
			if err != nil {
				break
			}
		}
		// the other side learns this one is done; the other direction may still be going
		if tc, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = tc.CloseWrite()
		} else {
			_ = dst.Close()
		}
	}
	wg.Add(2)
	go copyIdle(up, client, &sent)
	go copyIdle(client, up, &received)
	wg.Wait()
	_ = client.Close()
	_ = up.Close()
	p.Log.Info("egress", "client", who, "mode", how, "target", target, "sent", sent.Load(), "received", received.Load(),
		"seconds", time.Since(start).Seconds())
}
