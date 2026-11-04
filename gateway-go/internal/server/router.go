package server

import (
	"bytes"
	"encoding/base64"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// The whole HTTP surface of the Python gateway, as uvicorn, Starlette and FastAPI serve it
// (app/main.py): the middlewares in their order (the notebooks' host outside, in internal/api;
// then CORS, then the body's ceiling), the routes tried in the order FastAPI declares them
// (fastapi_routes_gen.go), and what the framework answers when none fits: 405 with Allow,
// the trailing slash's redirect, 404, a WebSocket handshake refused. No request is ever
// forwarded anywhere: a route the table names and Go does not answer is a programming error.

// pyRoute is one route of the FastAPI app, as testdata/gen_routes.py reads it.
type pyRoute struct {
	ws      bool
	path    string   // as declared: `/flows/{flow_id}`, `/ai/models/{model_id:path}`
	methods []string // sorted; Starlette's set
	regex   string   // Starlette's (compile_path)
}

// App is the FastAPI application a handler stands for.
type App int

const (
	// MainApp is app.main's application: CORS, the trailing slash's redirect at the root.
	MainApp App = iota
	// NotebooksHostApp is the notebooks' own host (routes/notebook_proxy.py install): their
	// routes alone, no CORS, no redirect.
	NotebooksHostApp
)

// Options are the settings of the framework's behaviour that the environment changes.
type Options struct {
	App App
	// CORSOrigins is APP__CORS_ORIGINS (MainApp only).
	CORSOrigins []string
	// RootPath is APP__ROOT_PATH as its validator writes it ("" or "/api"): stripped from a
	// path that starts with it (get_route_path); with one, no trailing slash's redirect.
	RootPath string
	// Scheme is the scheme uvicorn puts in the scope (X-Forwarded-Proto from a trusted
	// proxy, «http» otherwise): the redirect's address. Nil: «http».
	Scheme func(*http.Request) string
	Log    *slog.Logger
}

type route struct {
	pyRoute
	re       *regexp.Regexp
	names    []string
	allow    string
	handlers map[string]http.Handler // by method; WebSocket for a WebSocket route
}

type router struct {
	opt    Options
	routes []*route
}

// New answers every request as the Python gateway's application `opt.App` does, with the
// handlers of `routes` (the Go routes, by method and pattern).
func New(routes []Route, opt Options) http.Handler {
	table := fastAPIRoutes
	if opt.App == NotebooksHostApp {
		table = notebooksHostRoutes
	}
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	byKey := map[string]map[string]http.Handler{}
	for _, r := range routes {
		key := FastAPIPath(r.Pattern)
		if r.Method == WebSocket {
			key = "ws " + key
		}
		if byKey[key] == nil {
			byKey[key] = map[string]http.Handler{}
		}
		h := r.Handler
		if r.Method != WebSocket && !(r.Method == http.MethodPost && r.Pattern == "/files") {
			h = BodyLimit(h)
		}
		byKey[key][r.Method] = h
	}
	rt := &router{opt: opt}
	for _, p := range table {
		re := regexp.MustCompile(goRegex(p.regex))
		key := p.path
		if p.ws {
			key = "ws " + key
		}
		rt.routes = append(rt.routes, &route{pyRoute: p, re: re, names: re.SubexpNames(), allow: strings.Join(p.methods, ", "),
			handlers: byKey[key]})
	}
	return rt
}

// goRegex is a Starlette path regex for RE2: Python's «$» also matches before a final newline.
func goRegex(py string) string { return strings.TrimSuffix(py, "$") + `\n?\z` }

type match int

const (
	none match = iota
	partial
	full
)

func (rt *router) path(r *http.Request) string {
	raw, _, _ := strings.Cut(r.RequestURI, "?")
	return Unquote(raw)
}

// routePath is get_route_path: the path without the root path when it starts with it.
func (rt *router) routePath(path string) string {
	root := rt.opt.RootPath
	if root == "" || !strings.HasPrefix(path, root) {
		return path
	}
	if path == root {
		return ""
	}
	if path[len(root)] == '/' {
		return path[len(root):]
	}
	return path
}

func (rt *router) find(path, method string, ws bool) (match, *route, []string) {
	var first *route
	var firstValues []string
	for _, r := range rt.routes {
		if r.ws != ws {
			continue
		}
		m := r.re.FindStringSubmatch(path)
		if m == nil {
			continue
		}
		if ws || contains(r.methods, method) {
			return full, r, m
		}
		if first == nil {
			first, firstValues = r, m
		}
	}
	if first != nil {
		return partial, first, firstValues
	}
	return none, nil, nil
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func (rt *router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := rt.path(r)
	if IsWebSocket(r) {
		rt.serveWebSocket(w, r, path)
		return
	}
	if rt.opt.App == MainApp {
		if origin, ok := firstHeader(r, "Origin"); ok {
			if r.Method == http.MethodOptions && len(r.Header.Values("Access-Control-Request-Method")) > 0 {
				rt.preflight(w, r, origin)
				return
			}
			w = &corsWriter{ResponseWriter: w, origin: origin, allowed: contains(rt.opt.CORSOrigins, origin)}
			defer w.(*corsWriter).finish()
		}
	}
	// BodyLimitMiddleware: the declared size, before anything else (the upload exempt)
	if r.ContentLength > MaxBody && !(r.Method == http.MethodPost && rt.routePath(path) == "/files") {
		refuseTooLarge(w)
		return
	}
	if r.Method == http.MethodHead {
		hw := &headWriter{ResponseWriter: w, header: http.Header{}}
		defer hw.finish()
		w = hw
	}
	routePath := rt.routePath(path)
	m, found, values := rt.find(routePath, r.Method, false)
	switch m {
	case full:
		h := found.handlers[r.Method]
		if h == nil && r.Method == http.MethodHead {
			h = found.handlers[http.MethodGet] // Starlette's Route answers HEAD with its GET
		}
		if h == nil {
			rt.opt.Log.Error("a route of the Python gateway with no Go handler", "method", r.Method, "path", found.path)
			InternalError(w)
			return
		}
		for i, name := range found.names {
			if name != "" {
				r.SetPathValue(name, values[i])
			}
		}
		h.ServeHTTP(w, r)
		return
	case partial:
		MethodNotAllowed(found.allow).ServeHTTP(w, r)
		return
	}
	if rt.opt.App == MainApp && rt.opt.RootPath == "" && routePath != "/" {
		other := strings.TrimRight(path, "/")
		if !strings.HasSuffix(routePath, "/") {
			other = path + "/"
		}
		if m, _, _ := rt.find(rt.routePath(other), r.Method, false); m != none {
			rt.redirectSlash(w, r, other)
			return
		}
	}
	NotFound().ServeHTTP(w, r)
}

// ── the trailing slash ──────────────────────────────────────────────────────

var hostRE = regexp.MustCompile(`(?i)^([a-z0-9.-]+|\[[a-f0-9]*:[a-f0-9.:]+\])(?::[0-9]+)?$`)

// redirectSlash is the Router's RedirectResponse to URL(scope=redirect_scope): a 307 to the
// same address with the slash added or taken away.
func (rt *router) redirectSlash(w http.ResponseWriter, r *http.Request, path string) {
	scheme := "http"
	if rt.opt.Scheme != nil {
		scheme = rt.opt.Scheme(r)
	}
	netloc, hasNetloc := "", false
	if h, ok := firstHeader(r, "Host"); ok && hostRE.MatchString(h) {
		netloc, hasNetloc = h, true
	} else if a, ok := r.Context().Value(http.LocalAddrContextKey).(net.Addr); ok {
		if host, port, err := net.SplitHostPort(a.String()); err == nil {
			def := map[string]string{"http": "80", "https": "443", "ws": "80", "wss": "443"}[scheme]
			netloc, hasNetloc = host, true
			if port != def {
				netloc = host + ":" + port
			}
		}
	}
	_, query, _ := strings.Cut(r.RequestURI, "?")
	var url string
	if hasNetloc {
		// urlunsplit: a path not starting with «/» gets one after a netloc
		if path != "" && path[0] != '/' {
			path = "/" + path
		}
		url = scheme + "://" + netloc + path
		if query != "" {
			url += "?" + query
		}
	} else {
		url = path
		if query != "" {
			url += "?" + query
		}
	}
	w.Header().Set("Location", QuoteURL(url))
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusTemporaryRedirect)
}

// QuoteURL is quote(url, safe=":/%#?=@[]!$&'()*+,;"): Starlette's RedirectResponse.
func QuoteURL(s string) string {
	const safe = ":/%#?=@[]!$&'()*+,;" + "_.-~"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x80 && (c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.IndexByte(safe, c) >= 0) {
			b.WriteByte(c)
			continue
		}
		b.WriteString("%" + strings.ToUpper(strconv.FormatInt(int64(c)|0x100, 16)[1:]))
	}
	return b.String()
}

// ── CORS (starlette.middleware.cors, as app/main.py sets it) ────────────────

var corsMethods = []string{"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}

func firstHeader(r *http.Request, name string) (string, bool) {
	v := r.Header.Values(name)
	if len(v) == 0 {
		return "", false
	}
	return v[0], true
}

// preflight is CORSMiddleware.preflight_response, with credentials and every method and header
// allowed.
func (rt *router) preflight(w http.ResponseWriter, r *http.Request, origin string) {
	h := w.Header()
	h.Set("Vary", "Origin")
	h.Set("Access-Control-Allow-Methods", strings.Join(corsMethods, ", "))
	h.Set("Access-Control-Max-Age", "600")
	h.Set("Access-Control-Allow-Credentials", "true")
	var failures []string
	if contains(rt.opt.CORSOrigins, origin) {
		h.Set("Access-Control-Allow-Origin", origin)
	} else {
		failures = append(failures, "origin")
	}
	method, _ := firstHeader(r, "Access-Control-Request-Method")
	if !contains(corsMethods, method) {
		failures = append(failures, "method")
	}
	if v, ok := firstHeader(r, "Access-Control-Request-Headers"); ok {
		h.Set("Access-Control-Allow-Headers", v)
	}
	if _, ok := firstHeader(r, "Access-Control-Request-Private-Network"); ok {
		failures = append(failures, "private-network")
	}
	status, text := http.StatusOK, "OK"
	if len(failures) > 0 {
		status, text = http.StatusBadRequest, "Disallowed CORS "+strings.Join(failures, ", ")
	}
	h.Set("Content-Type", "text/plain; charset=utf-8")
	h.Set("Content-Length", strconv.Itoa(len(text)))
	w.WriteHeader(status)
	_, _ = w.Write([]byte(text))
}

// corsWriter is CORSMiddleware.send for a request with an Origin: the simple headers on the
// answer, and the origin mirrored when it is allowed.
type corsWriter struct {
	http.ResponseWriter
	origin  string
	allowed bool
	done    bool
}

func (c *corsWriter) headers() {
	if c.done {
		return
	}
	c.done = true
	h := c.ResponseWriter.Header()
	h.Set("Access-Control-Allow-Credentials", "true")
	h.Set("Access-Control-Expose-Headers", "Content-Disposition")
	if !c.allowed {
		return
	}
	// an answer the AI service wrote (forwarded as it came) has been through the same
	// middleware there: once is what the Python gateway does
	already := h.Get("Access-Control-Allow-Origin") == c.origin
	h.Set("Access-Control-Allow-Origin", c.origin)
	if already {
		return
	}
	if v := h.Values("Vary"); len(v) > 0 {
		h.Set("Vary", v[0]+", Origin")
	} else {
		h.Set("Vary", "Origin")
	}
}

func (c *corsWriter) WriteHeader(status int) {
	if status >= 200 { // an informational answer is not the response's start
		c.headers()
	}
	c.ResponseWriter.WriteHeader(status)
}

func (c *corsWriter) Write(b []byte) (int, error) {
	c.headers()
	return c.ResponseWriter.Write(b)
}

// finish: an answer the handler left empty (net/http writes its header after the handler).
func (c *corsWriter) finish() { c.headers() }

func (c *corsWriter) Flush() {
	c.headers()
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (c *corsWriter) Unwrap() http.ResponseWriter { return c.ResponseWriter }

// headWriter is uvicorn's answer to HEAD: the headers the application gave, no body; the
// Content-Length of the body it would have sent, as Starlette's Response writes it.
type headWriter struct {
	http.ResponseWriter
	header  http.Header
	status  int
	n       int
	flushed bool
}

func (h *headWriter) Header() http.Header {
	if h.flushed {
		return h.ResponseWriter.Header()
	}
	return h.header
}

func (h *headWriter) WriteHeader(status int) {
	if h.status == 0 && status >= 200 {
		h.status = status
	}
}

func (h *headWriter) Write(b []byte) (int, error) {
	if h.status == 0 {
		h.status = http.StatusOK
	}
	h.n += len(b)
	return len(b), nil
}

func (h *headWriter) start(withLength bool) {
	if h.flushed {
		return
	}
	h.flushed = true
	dst := h.ResponseWriter.Header()
	for k, v := range h.header {
		dst[k] = v
	}
	if withLength && dst.Get("Content-Length") == "" && dst.Get("Transfer-Encoding") == "" && h.status != http.StatusNoContent &&
		h.status != http.StatusNotModified {
		dst.Set("Content-Length", strconv.Itoa(h.n))
	}
	if h.status == 0 {
		h.status = http.StatusOK
	}
	h.ResponseWriter.WriteHeader(h.status)
}

func (h *headWriter) Flush() {
	h.start(false)
	if f, ok := h.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (h *headWriter) finish() { h.start(true) }

func (h *headWriter) Unwrap() http.ResponseWriter { return h.ResponseWriter }

func refuseTooLarge(w http.ResponseWriter) {
	body := []byte(`{"detail":"Request body too large (limit 8 MB)"}`)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(http.StatusRequestEntityTooLarge)
	_, _ = w.Write(body)
}

// ── WebSocket ───────────────────────────────────────────────────────────────

// serveWebSocket is a handshake: checked as the websockets library checks it (uvicorn's
// sans-io protocol), then routed among the WebSocket routes; none: Starlette closes before
// accepting, and uvicorn answers 403.
func (rt *router) serveWebSocket(w http.ResponseWriter, r *http.Request, path string) {
	if handshakeRefused(w, r) {
		return
	}
	m, found, values := rt.find(rt.routePath(path), r.Method, true)
	if m == full {
		if h := found.handlers[WebSocket]; h != nil {
			for i, name := range found.names {
				if name != "" {
					r.SetPathValue(name, values[i])
				}
			}
			h.ServeHTTP(w, r)
			return
		}
		rt.opt.Log.Error("a WebSocket route of the Python gateway with no Go handler", "path", found.path)
	}
	WebSocketRefused(w, http.StatusForbidden, "")
}

// WebSocketRefused is ServerProtocol.reject: a short plain text, the connection closed.
func WebSocketRefused(w http.ResponseWriter, status int, text string) {
	h := w.Header()
	h.Set("Date", time.Now().UTC().Format(http.TimeFormat))
	h.Set("Connection", "close")
	h.Set("Content-Length", strconv.Itoa(len(text)))
	h.Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(text))
}

// handshakeRefused is ServerProtocol.process_request's checks (method, protocol, Connection,
// Upgrade, key, version) and accept's answers to them.
func handshakeRefused(w http.ResponseWriter, r *http.Request) bool {
	fail := func(status int, why string) bool {
		WebSocketRefused(w, status, "Failed to open a WebSocket connection: "+why+".\n")
		return true
	}
	if r.Method != http.MethodGet {
		WebSocketRefused(w, http.StatusMethodNotAllowed, "Failed to open a WebSocket connection: unsupported HTTP method: "+r.Method+".\n")
		w.Header().Set("Allow", "GET")
		return true
	}
	// the protocol is never checked: uvicorn hands the request on as HTTP/1.1
	var upgrades []string
	for _, v := range r.Header.Values("Upgrade") {
		for _, t := range strings.Split(v, ",") {
			if t = strings.TrimSpace(t); t != "" {
				upgrades = append(upgrades, t)
			}
		}
	}
	if !(len(upgrades) == 1 && strings.EqualFold(upgrades[0], "websocket")) {
		text := "Failed to open a WebSocket connection: invalid Upgrade header: " + strings.Join(upgrades, ", ") + ".\n\n" +
			"You cannot access a WebSocket server directly with a browser. You need a WebSocket client.\n"
		w.Header().Set("Upgrade", "websocket")
		WebSocketRefused(w, http.StatusUpgradeRequired, text)
		return true
	}
	keys := r.Header.Values("Sec-WebSocket-Key")
	switch {
	case len(keys) == 0:
		return fail(http.StatusBadRequest, "missing Sec-WebSocket-Key header")
	case len(keys) > 1:
		return fail(http.StatusBadRequest, "invalid Sec-WebSocket-Key header: multiple values")
	}
	if raw, err := base64.StdEncoding.Strict().DecodeString(keys[0]); err != nil || len(raw) != 16 {
		why := "invalid Sec-WebSocket-Key header: " + keys[0]
		if keys[0] == "" {
			why = "empty Sec-WebSocket-Key header"
		}
		return fail(http.StatusBadRequest, why)
	}
	versions := r.Header.Values("Sec-WebSocket-Version")
	switch {
	case len(versions) == 0:
		return fail(http.StatusBadRequest, "missing Sec-WebSocket-Version header")
	case len(versions) > 1:
		return fail(http.StatusBadRequest, "invalid Sec-WebSocket-Version header: multiple values")
	case versions[0] != "13":
		why := "invalid Sec-WebSocket-Version header: " + versions[0]
		if versions[0] == "" {
			why = "empty Sec-WebSocket-Version header"
		}
		return fail(http.StatusBadRequest, why)
	}
	return false
}

// ── the path ────────────────────────────────────────────────────────────────

// Unquote is urllib.parse.unquote(s): %XX decoded where both digits are hex, the bytes read
// as UTF-8 with each invalid sequence (its maximal part) replaced by U+FFFD. uvicorn decodes
// the request's path so; Starlette routes on it.
func Unquote(s string) string {
	if !strings.Contains(s, "%") {
		return s
	}
	var out strings.Builder
	var buf bytes.Buffer
	flush := func() {
		if buf.Len() > 0 {
			out.WriteString(decodeReplace(buf.Bytes()))
			buf.Reset()
		}
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == '%' && i+2 < len(s) && isHex(s[i+1]) && isHex(s[i+2]) {
			v, _ := strconv.ParseUint(s[i+1:i+3], 16, 8)
			buf.WriteByte(byte(v))
			i += 2
			continue
		}
		if c >= 0x80 {
			buf.WriteByte(c)
			continue
		}
		flush()
		out.WriteByte(c)
	}
	flush()
	return out.String()
}

func isHex(c byte) bool { return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F' }

// decodeReplace is bytes.decode("utf-8", "replace"): one U+FFFD per maximal subpart of an
// ill-formed sequence (Unicode 3.9, U+FFFD substitution of maximal subparts), as CPython.
func decodeReplace(b []byte) string {
	var out strings.Builder
	for len(b) > 0 {
		r, size := utf8.DecodeRune(b)
		if r != utf8.RuneError || size > 1 {
			out.WriteRune(r)
			b = b[size:]
			continue
		}
		out.WriteRune(utf8.RuneError)
		b = b[maximalSubpart(b):]
	}
	return out.String()
}

// maximalSubpart is how many bytes of an ill-formed sequence at the start of b one U+FFFD
// stands for: the lead byte and the continuation bytes that could still have completed it.
func maximalSubpart(b []byte) int {
	c := b[0]
	var need int
	lo, hi := byte(0x80), byte(0xBF)
	switch {
	case c >= 0xC2 && c <= 0xDF:
		need = 1
	case c == 0xE0:
		need, lo = 2, 0xA0
	case c >= 0xE1 && c <= 0xEC || c == 0xEE || c == 0xEF:
		need = 2
	case c == 0xED:
		need, hi = 2, 0x9F
	case c == 0xF0:
		need, lo = 3, 0x90
	case c >= 0xF1 && c <= 0xF3:
		need = 3
	case c == 0xF4:
		need, hi = 3, 0x8F
	default:
		return 1
	}
	n := 1
	for i := 0; i < need && n < len(b); i++ {
		x := b[n]
		if i == 0 {
			if x < lo || x > hi {
				return n
			}
		} else if x < 0x80 || x > 0xBF {
			return n
		}
		n++
	}
	return n
}

// PythonRoutes are the routes of the Python gateway's application `app`, [method, path] as
// FastAPI declares them (a WebSocket route's method is WebSocket; HEAD only where Starlette
// adds it to a GET of its own): what the Go routes must answer, all of them.
func PythonRoutes(app App) [][2]string {
	table := fastAPIRoutes
	if app == NotebooksHostApp {
		table = notebooksHostRoutes
	}
	var out [][2]string
	for _, p := range table {
		if p.ws {
			out = append(out, [2]string{WebSocket, p.path})
			continue
		}
		for _, m := range p.methods {
			out = append(out, [2]string{m, p.path})
		}
	}
	return out
}
