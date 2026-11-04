// Package pyurl parses addresses as the Python gateway does: urllib.parse.urlsplit of
// CPython 3.12 (the scheme, the host shown to an observer, the audit's target) and
// httpx 0.28's urlparse (the webhooks' and the SIEM's validate_url), with their errors'
// texts. A host that is not ASCII is checked as _checknetloc does (NFKC) and lower-cased as
// Python's str.lower does; httpx's IDNA encoding of such a host (the Python package idna) is
// asked of the AI service (UseIDNA): without it, ErrNotASCII.
package pyurl

import (
	"errors"
	"fmt"
	"math/big"
	"net/netip"
	"regexp"
	"strings"
	"sync/atomic"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

// ErrNotASCII: httpx's encoding of a host that is not ASCII asked with no IDNA encoder given
// (UseIDNA): only the Python package idna says it faithfully.
var ErrNotASCII = errors.New("pyurl: a host that is not ASCII")

// ErrIDNAUnavailable: the IDNA encoder could not be asked (the AI service did not answer); the
// error wraps it and the cause.
var ErrIDNAUnavailable = errors.New("pyurl: the IDNA encoding could not be asked")

// IDNAEncoder is httpx 0.28's encode_host for a host that is not ASCII:
// idna.encode(host.lower()).decode("ascii"), the Python package idna (IDNA 2008, its own
// Unicode tables), or ok=false for its IDNAError; err when it could not be asked.
type IDNAEncoder func(host string) (ascii string, ok bool, err error)

var idnaEncoder atomic.Pointer[IDNAEncoder]

// UseIDNA sets the encoder HTTPXURL asks for a host that is not ASCII (the AI service's
// /internal/python/idna-encode); nil goes back to ErrNotASCII.
func UseIDNA(e IDNAEncoder) {
	if e == nil {
		idnaEncoder.Store(nil)
		return
	}
	idnaEncoder.Store(&e)
}

// Split is urlsplit's result.
type Split struct {
	Scheme, Netloc, Path, Query, Fragment string
}

const schemeChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-."

// URLSplit is urllib.parse.urlsplit(url): a ValueError's text as the error. A netloc that is
// not ASCII is checked as _checknetloc does (NFKC).
func URLSplit(url string) (Split, error) { return urlSplit(url) }

// URLSplitNFKC is URLSplit (its name from when a netloc that is not ASCII was left to Python).
func URLSplitNFKC(url string) (Split, error) { return urlSplit(url) }

func urlSplit(url string) (Split, error) {
	url = strings.TrimLeftFunc(url, func(r rune) bool { return r <= ' ' })
	for _, b := range []string{"\t", "\r", "\n"} {
		url = strings.ReplaceAll(url, b, "")
	}
	var s Split
	if i := strings.IndexByte(url, ':'); i > 0 && isASCIIAlpha(url[0]) {
		ok := true
		for _, c := range url[:i] {
			if !strings.ContainsRune(schemeChars, c) {
				ok = false
				break
			}
		}
		if ok {
			s.Scheme, url = strings.ToLower(url[:i]), url[i+1:]
		}
	}
	if strings.HasPrefix(url, "//") {
		end := len(url)
		for _, c := range "/?#" {
			if j := strings.IndexRune(url[2:], c); j >= 0 && j+2 < end {
				end = j + 2
			}
		}
		s.Netloc, url = url[2:end], url[end:]
		open, closed := strings.Contains(s.Netloc, "["), strings.Contains(s.Netloc, "]")
		if open != closed {
			return Split{}, errors.New("Invalid IPv6 URL")
		}
		if open && closed {
			if err := checkBracketedNetloc(s.Netloc); err != nil {
				return Split{}, err
			}
		}
	}
	if i := strings.IndexByte(url, '#'); i >= 0 {
		url, s.Fragment = url[:i], url[i+1:]
	}
	if i := strings.IndexByte(url, '?'); i >= 0 {
		url, s.Query = url[:i], url[i+1:]
	}
	if !isASCII(s.Netloc) {
		if err := checkNetloc(s.Netloc); err != nil {
			return Split{}, err
		}
	}
	s.Path = url
	return s, nil
}

// checkNetloc is _checknetloc: a host whose NFKC form brings in a character that splits an
// address is refused.
func checkNetloc(netloc string) error {
	n := strings.NewReplacer("@", "", ":", "", "#", "", "?", "").Replace(netloc)
	n2 := norm.NFKC.String(n)
	if n == n2 {
		return nil
	}
	if strings.ContainsAny(n2, "/?#@:") {
		return fmt.Errorf("netloc '%s' contains invalid characters under NFKC normalization", netloc)
	}
	return nil
}

func isASCIIAlpha(c byte) bool { return (c|0x20) >= 'a' && (c|0x20) <= 'z' }

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= utf8.RuneSelf {
			return false
		}
	}
	return true
}

func rpartition(s, sep string) (string, string) {
	if i := strings.LastIndex(s, sep); i >= 0 {
		return s[:i], s[i+len(sep):]
	}
	return "", s
}

func partition(s, sep string) (string, bool, string) {
	before, after, found := strings.Cut(s, sep)
	return before, found, after
}

var ipvFuture = regexp.MustCompile(`\Av[a-fA-F0-9]+\.(?s:.+)\z`)

// checkBracketedNetloc is _check_bracketed_netloc.
func checkBracketedNetloc(netloc string) error {
	_, hostPort := rpartition(netloc, "@")
	before, open, bracketed := partition(hostPort, "[")
	var hostname string
	if open {
		if before != "" {
			return errors.New("Invalid IPv6 URL")
		}
		var port string
		hostname, _, port = partition(bracketed, "]")
		if port != "" && !strings.HasPrefix(port, ":") {
			return errors.New("Invalid IPv6 URL")
		}
	} else {
		hostname, _, _ = partition(hostPort, ":")
	}
	if strings.HasPrefix(hostname, "v") {
		if !ipvFuture.MatchString(hostname) {
			return errors.New("IPvFuture address is invalid")
		}
		return nil
	}
	ip, ok := IPAddress(hostname)
	if !ok {
		return fmt.Errorf("%s does not appear to be an IPv4 or IPv6 address", Repr(hostname))
	}
	if ip.Is4() {
		return errors.New("An IPv4 address cannot be in brackets")
	}
	return nil
}

// Hostname is SplitResult.hostname: the host of the netloc, lower-cased (an IPv6 zone
// kept as written); ok=false for None.
func (s Split) Hostname() (string, bool) {
	_, hostinfo := rpartition(s.Netloc, "@")
	var hostname string
	if _, open, bracketed := partition(hostinfo, "["); open {
		hostname, _, _ = partition(bracketed, "]")
	} else {
		hostname, _, _ = partition(hostinfo, ":")
	}
	if hostname == "" {
		return "", false
	}
	h, pct, zone := partition(hostname, "%")
	if pct {
		return Lower(h) + "%" + zone, true
	}
	return Lower(h), true
}

// UserInfo is SplitResult.username and .password ("" for None as for empty).
func (s Split) UserInfo() (string, string) {
	userinfo, _, found := strings.Cut(s.Netloc, "@")
	if !found {
		return "", ""
	}
	userinfo, _ = rpartition(s.Netloc, "@")
	user, password, _ := strings.Cut(userinfo, ":")
	return user, password
}

// HostnameOrNone is f"{parts.hostname}": «None» when there is none.
func (s Split) HostnameOrNone() string {
	if h, ok := s.Hostname(); ok {
		return h
	}
	return "None"
}

// IPAddress is ipaddress.ip_address on a string: IPv4 in four decimal parts without
// leading zeros, IPv6 with an optional zone.
func IPAddress(s string) (netip.Addr, bool) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, false
	}
	return a, true
}

// Repr is Python's repr() of a str.
func Repr(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case r == rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x7f || unicode.IsPrint(r):
			b.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// ── httpx 0.28 ─────────────────────────────────────────────────────────────

// HTTPX is httpx's ParseResult, the parts the gateway reads.
type HTTPX struct {
	Scheme string
	Host   string   // raw_host: lower-cased, %-quoted, IPv6 without brackets
	Port   *big.Int // None for the scheme's default (Python's int: no limit)
	Path   string   // %-quoted, the dot segments removed
	Query  *string  // %-quoted; nil without «?»
}

// RawPath is httpx's raw_path: what the request line carries.
func (u HTTPX) RawPath() string {
	p := u.Path
	if p == "" {
		p = "/"
	}
	if u.Query != nil {
		p += "?" + *u.Query
	}
	return p
}

var (
	urlRegex       = regexp.MustCompile(`^(?s)(?:(([a-zA-Z][a-zA-Z0-9+.-]*)?):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?`)
	authorityRegex = regexp.MustCompile(`^(?s)(?:(.*)@)?(\[.*\]|[^:@]*):?(.*)?`)
	ipv4Style      = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$`)
	ipv6Style      = regexp.MustCompile(`^(?s)\[.*\]$`)
	percentEncoded = regexp.MustCompile(`%[A-Fa-f0-9]{2}`)
)

// HTTPXURL is httpx.URL(url): httpx._urlparse.urlparse, its InvalidURL's text as the error.
func HTTPXURL(url string) (HTTPX, error) {
	if utf8.RuneCountInString(url) > 65536 {
		return HTTPX{}, errors.New("URL too long")
	}
	for i, r := range []rune(url) {
		if r < 0x20 || r == 0x7f {
			return HTTPX{}, fmt.Errorf("Invalid non-printable ASCII character in URL, %s at position %d.", Repr(string(r)), i)
		}
	}
	m := urlRegex.FindStringSubmatch(url)
	scheme, authority, path := m[1], m[3], m[4]
	queryIdx := urlRegex.FindStringSubmatchIndex(url)
	var query *string
	if queryIdx[10] >= 0 {
		q := m[5]
		query = &q
	}
	a := authorityRegex.FindStringSubmatch(authority)
	userinfo, host, portText := a[1], a[2], a[3]
	var out HTTPX
	out.Scheme = strings.ToLower(scheme)
	var err error
	if out.Host, err = encodeHost(host); err != nil {
		return HTTPX{}, err
	}
	if portText != "" {
		n, ok := pyIntText(portText)
		if !ok {
			return HTTPX{}, fmt.Errorf("Invalid port: %s", Repr(portText))
		}
		def := map[string]int64{"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}
		if d, has := def[scheme]; !has || !n.IsInt64() || d != n.Int64() {
			out.Port = n
		}
	}
	hasScheme := out.Scheme != ""
	hasAuthority := userinfo != "" || out.Host != "" || out.Port != nil
	if hasAuthority && path != "" && !strings.HasPrefix(path, "/") {
		return HTTPX{}, errors.New("For absolute URLs, path must be empty or begin with '/'")
	}
	if !hasScheme && !hasAuthority {
		if strings.HasPrefix(path, "//") {
			return HTTPX{}, errors.New("Relative URLs cannot have a path starting with '//'")
		}
		if strings.HasPrefix(path, ":") {
			return HTTPX{}, errors.New("Relative URLs cannot have a path starting with ':'")
		}
	}
	if hasScheme || hasAuthority {
		path = normalizePath(path)
	}
	out.Path = quote(path, pathSafe)
	if query != nil {
		q := quote(*query, querySafe)
		out.Query = &q
	}
	return out, nil
}

// the safe sets of httpx's PATH_SAFE and QUERY_SAFE
var pathSafe, querySafe = func() (string, string) {
	var p, q strings.Builder
	for i := 0x20; i < 0x7f; i++ {
		switch i {
		case 0x20, 0x22, 0x23, 0x3c, 0x3e:
			continue
		}
		q.WriteByte(byte(i))
		switch i {
		case 0x3f, 0x60, 0x7b, 0x7d:
			continue
		}
		p.WriteByte(byte(i))
	}
	return p.String(), q.String()
}()

// normalizePath is httpx's normalize_path: the «.» and «..» segments dropped.
func normalizePath(path string) string {
	if !strings.Contains(path, ".") {
		return path
	}
	parts := strings.Split(path, "/")
	dots := false
	for _, c := range parts {
		if c == "." || c == ".." {
			dots = true
		}
	}
	if !dots {
		return path
	}
	var out []string
	for _, c := range parts {
		switch c {
		case ".":
		case "..":
			if len(out) > 0 && !(len(out) == 1 && out[0] == "") {
				out = out[:len(out)-1]
			}
		default:
			out = append(out, c)
		}
	}
	return strings.Join(out, "/")
}

// pyIntText is int(port) on a str: whitespace around, a sign, underscores between ASCII
// digits.
func pyIntText(s string) (*big.Int, bool) {
	s = strings.TrimFunc(s, unicode.IsSpace)
	body := strings.TrimLeft(s, "+-")
	if len(s)-len(body) > 1 || body == "" || body[0] == '_' || body[len(body)-1] == '_' || strings.Contains(body, "__") {
		return nil, false
	}
	for _, c := range body {
		if (c < '0' || c > '9') && c != '_' {
			return nil, false
		}
	}
	return new(big.Int).SetString(strings.ReplaceAll(s, "_", ""), 10)
}

// encodeHost is httpx's encode_host for an ASCII host.
func encodeHost(host string) (string, error) {
	switch {
	case host == "":
		return "", nil
	case ipv4Style.MatchString(host):
		if a, ok := IPAddress(host); !ok || !a.Is4() {
			return "", fmt.Errorf("Invalid IPv4 address: %s", Repr(host))
		}
		return host, nil
	case ipv6Style.MatchString(host):
		if a, ok := IPAddress(host[1 : len(host)-1]); !ok || !a.Is6() {
			return "", fmt.Errorf("Invalid IPv6 address: %s", Repr(host))
		}
		return host[1 : len(host)-1], nil
	case isASCII(host):
		return quote(strings.ToLower(host), `!$&'()*+,;="`+"`"+`{}%|\`), nil
	}
	e := idnaEncoder.Load()
	if e == nil {
		return "", ErrNotASCII
	}
	ascii, ok, err := (*e)(host)
	if err != nil {
		return "", fmt.Errorf("%w: %v", ErrIDNAUnavailable, err)
	}
	if !ok {
		return "", fmt.Errorf("Invalid IDNA hostname: %s", Repr(host))
	}
	return ascii, nil
}

const unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

// quote is httpx's quote: %-encodes what is not safe, leaving the %XX already there.
func quote(s, safe string) string {
	var b strings.Builder
	last := 0
	enc := func(t string) {
		for _, r := range t {
			if r < utf8.RuneSelf && strings.ContainsRune(unreserved+safe, r) {
				b.WriteRune(r)
				continue
			}
			var buf [4]byte
			n := utf8.EncodeRune(buf[:], r)
			for _, x := range buf[:n] {
				fmt.Fprintf(&b, "%%%02X", x)
			}
		}
	}
	for _, loc := range percentEncoded.FindAllStringIndex(s, -1) {
		enc(s[last:loc[0]])
		b.WriteString(s[loc[0]:loc[1]])
		last = loc[1]
	}
	enc(s[last:])
	return b.String()
}

// IDNALabels is str.encode("idna") on an ASCII host (CPython 3.12's fast path): every label
// but the last 1 to 63 characters, the last under 64.
func IDNALabels(host string) error {
	if host == "" {
		return nil
	}
	labels := strings.Split(host, ".")
	for _, l := range labels[:len(labels)-1] {
		if len(l) == 0 || len(l) >= 64 {
			return errors.New("label empty or too long")
		}
	}
	if len(labels[len(labels)-1]) >= 64 {
		return errors.New("label too long")
	}
	return nil
}
