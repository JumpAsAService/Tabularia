package siem

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
)

// readBytes is READ_BYTES: at most this much of the answer is read.
const readBytes = 4096

// Channel is CHANNEL: the HEC channel, one per process.
var Channel = newUUID()

func newUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// Destination is what sending needs of the siem_destinations row.
type Destination struct {
	URL       string
	Token     string // decrypted
	VerifyTLS bool
}

// Result is send's (success, HTTP code, message).
type Result struct {
	OK      bool
	Code    *int
	Message string
}

func fail(msg string) Result { return Result{Message: clip(msg, 500)} }

// clip is a str's [:n]: n characters, not bytes.
func clip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n])
}

// Send is send(d, events): one request with the events one per line, to the address
// checked by webhooks._resolve; it never fails, the outcome is the result.
func Send(ctx context.Context, d Destination, events []pyjson.Object, timeout time.Duration) Result {
	lines := make([]string, len(events))
	for i, e := range events {
		s, err := pyjson.Dumps(e, pyjson.Options{Compact: true})
		if err != nil {
			return fail("ValueError: " + err.Error())
		}
		lines[i] = s
	}
	body := strings.Join(lines, "\n")
	endpoint, err := Endpoint(d.URL)
	if err != nil {
		return fail("invalid address: " + err.Error())
	}
	target, err := Resolve(ctx, endpoint)
	if err != nil {
		return fail(err.Error())
	}
	return post(ctx, target, d, body, timeout)
}

// Target is webhooks._resolve's (url, host, ip): where to connect and what to say.
type Target struct {
	Scheme, Host, IP, RawPath string
	Port                      int
	Netloc                    string // the Host header: httpx's netloc
}

// Resolve is webhooks._resolve: the host as httpx connects to it, resolved once; a name
// with any forbidden address is forbidden. The errors' texts are _Forbidden's and
// _Unresolved's.
func Resolve(ctx context.Context, url string) (Target, error) {
	u, err := pyurl.HTTPXURL(url)
	if err == nil {
		err = readHost(u)
	}
	if err != nil {
		return Target{}, errors.New("invalid address: " + err.Error())
	}
	t := Target{Scheme: u.Scheme, Host: u.Host, RawPath: u.RawPath()}
	defPort := 80
	if u.Scheme == "https" {
		defPort = 443
	}
	t.Port = defPort
	if u.Port != nil {
		t.Port = int(u.Port.Int64())
	}
	t.Netloc = u.Host
	if strings.Contains(u.Host, ":") {
		t.Netloc = "[" + u.Host + "]"
	}
	if u.Port != nil {
		t.Netloc += ":" + u.Port.String()
	}
	var addrs []netip.Addr
	if ip, ok := pyurl.IPAddress(strings.Trim(u.Host, "[]")); ok {
		addrs = []netip.Addr{ip}
	} else {
		found, err := net.DefaultResolver.LookupNetIP(ctx, "ip", u.Host)
		if err != nil {
			return Target{}, errors.New("name not resolved: " + gaiError(err))
		}
		addrs = found
	}
	if len(addrs) == 0 {
		return Target{}, errors.New("name without addresses")
	}
	for _, ip := range addrs {
		if Forbidden(ip) {
			return Target{}, fmt.Errorf("address not allowed for a webhook: %s", pyIP(ip))
		}
	}
	t.IP = pyIP(addrs[0])
	return t, nil
}

func readHost(u pyurl.HTTPX) error {
	if _, ok := pyurl.IPAddress(strings.Trim(u.Host, "[]")); !ok {
		if err := pyurl.IDNALabels(u.Host); err != nil {
			return err
		}
	}
	if u.Port != nil && !(u.Port.Sign() > 0 && u.Port.IsInt64() && u.Port.Int64() < 65536) {
		return fmt.Errorf("port out of range: %s", u.Port.String())
	}
	return nil
}

// gaiError is str(socket.gaierror) as glibc words it: an unknown name, or a resolver that
// did not answer.
func gaiError(err error) string {
	var dns *net.DNSError
	if errors.As(err, &dns) && dns.IsNotFound {
		return "[Errno -2] Name or service not known"
	}
	return "[Errno -3] Temporary failure in name resolution"
}

// pyIP is str(ipaddress.ip_address(…)): an IPv6 written compressed, its zone kept.
func pyIP(ip netip.Addr) string {
	if ip.Is4In6() {
		return ip.String() // «::ffff:1.2.3.4», as Python writes it
	}
	return ip.String()
}

var metadata = []netip.Prefix{
	netip.MustParsePrefix("100.100.100.200/32"), netip.MustParsePrefix("fd00:ec2::254/128"),
	netip.MustParsePrefix("168.63.129.16/32"),
}

var reserved6 = func() []netip.Prefix {
	var out []netip.Prefix
	for _, p := range []string{"::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4", "4000::/3", "6000::/3",
		"8000::/3", "a000::/3", "c000::/3", "e000::/4", "f000::/5", "f800::/6", "fe00::/9"} {
		out = append(out, netip.MustParsePrefix(p))
	}
	return out
}()

// Forbidden is webhooks._forbidden_ip: loopback, link-local, multicast, unspecified,
// reserved, the clouds' metadata; an IPv4-mapped or 6to4 address as the IPv4 inside.
func Forbidden(ip netip.Addr) bool {
	ip = ip.WithZone("")
	if ip.Is4In6() {
		ip = ip.Unmap()
	}
	if ip.Is6() && netip.MustParsePrefix("2002::/16").Contains(ip) {
		b := ip.As16()
		if Forbidden(netip.AddrFrom4([4]byte{b[2], b[3], b[4], b[5]})) {
			return true
		}
	}
	for _, m := range metadata {
		if m.Contains(ip) {
			return true
		}
	}
	if ip.Is4() {
		b := ip.As4()
		return b[0] == 127 || (b[0] == 169 && b[1] == 254) || b[0] >= 224 || ip == netip.IPv4Unspecified()
	}
	if ip.IsLoopback() || ip.IsUnspecified() || netip.MustParsePrefix("fe80::/10").Contains(ip) ||
		netip.MustParsePrefix("ff00::/8").Contains(ip) {
		return true
	}
	for _, p := range reserved6 {
		if p.Contains(ip) {
			return true
		}
	}
	return false
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

func post(ctx context.Context, t Target, d Destination, body string, timeout time.Duration) Result {
	address := net.JoinHostPort(strings.Trim(t.IP, "[]"), strconv.Itoa(t.Port))
	tlsConf := &tls.Config{ServerName: t.Host, InsecureSkipVerify: !d.VerifyTLS} //nolint:gosec — the administrator's choice
	transport := &http.Transport{
		Proxy: nil, // trust_env=False: no system proxy
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			c, err := (&net.Dialer{Timeout: timeout}).DialContext(ctx, "tcp", address)
			if err != nil {
				return nil, err
			}
			return &deadlineConn{c, timeout}, nil
		},
		TLSClientConfig:     tlsConf,
		TLSHandshakeTimeout: timeout,
		DisableCompression:  true,
		ForceAttemptHTTP2:   false,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse // follow_redirects=False
	}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.Scheme+"://"+t.Netloc+t.RawPath, strings.NewReader(body))
	if err != nil {
		return fail("InvalidURL: " + err.Error())
	}
	req.Host = t.Netloc
	req.Header.Set("Authorization", "Splunk "+d.Token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Splunk-Request-Channel", Channel)
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", "python-httpx/0.28.1")
	res, err := client.Do(req)
	if err != nil {
		return fail(httpxError(err))
	}
	defer res.Body.Close()
	var read []byte
	buf := make([]byte, 65536)
	for len(read) < readBytes {
		n, err := res.Body.Read(buf)
		read = append(read, buf[:n]...)
		if err == io.EOF {
			break
		}
		if err != nil {
			return fail(httpxError(err))
		}
	}
	return answer(res.StatusCode, read)
}

// answer is send's reading of Splunk's answer: a 2xx whose JSON says code 0 (or no JSON
// object) is a success; otherwise its text and code, or the first 500 bytes.
func answer(status int, read []byte) Result {
	code := status
	text := clip(strings.ToValidUTF8(string(read[:min(len(read), 500)]), "�"), 500)
	parsed, err := pyjson.Loads(read)
	obj, isObj := parsed.(pyjson.Object)
	if err != nil {
		isObj = false
	}
	if status >= 200 && status < 300 {
		c, has := obj.Get("code")
		if !isObj || !has || pyEqualsZero(c) {
			msg := "Success"
			if isObj && len(obj) > 0 {
				if t, ok := obj.Get("text"); ok {
					msg = pyjson.Str(t)
				}
			}
			return Result{OK: true, Code: &code, Message: msg}
		}
	}
	if isObj {
		if t, ok := obj.Get("text"); ok && truthy(t) {
			c, _ := obj.Get("code")
			text = pyjson.Str(t) + " (code " + pyjson.Str(c) + ")"
		}
	}
	return Result{Code: &code, Message: clip(fmt.Sprintf("response %d: %s", status, text), 500)}
}

// pyEqualsZero is `x == 0` in Python: 0, 0.0, False.
func pyEqualsZero(v any) bool {
	switch x := v.(type) {
	case int64:
		return x == 0
	case float64:
		return x == 0
	case bool:
		return !x
	}
	return false
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		return x != ""
	case pyjson.Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}

// httpxError is f"{type(e).__name__}: {e}" of the exception httpx raises, for the network's
// faults whose words are known (measured in the gateway's image): a connection refused or
// unreachable, a timeout, a server that closes without answering.
func httpxError(err error) string {
	var ne net.Error
	var op *net.OpError
	switch {
	case errors.As(err, &op) && op.Op == "dial":
		if op.Timeout() {
			return "ConnectTimeout: "
		}
		return "ConnectError: All connection attempts failed"
	case errors.As(err, &ne) && ne.Timeout():
		if errors.Is(err, os.ErrDeadlineExceeded) && strings.Contains(err.Error(), "TLS handshake") {
			return "ConnectTimeout: _ssl.c:983: The handshake operation timed out"
		}
		return "ReadTimeout: "
	case errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF):
		return "RemoteProtocolError: Server disconnected without sending a response."
	}
	var cert *tls.CertificateVerificationError
	if errors.As(err, &cert) {
		return "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: " + cert.Err.Error()
	}
	return "ConnectError: " + err.Error()
}
