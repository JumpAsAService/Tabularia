package api

// routes/notebook_proxy.py and services/notebook_frames.py: marimo behind the gateway,
// `/notebooks/{id}/app/…` and `/notebooks/{id}/edit/…`, HTTP and WebSocket, to the notebook's
// pod (net/http/httputil.ReverseProxy, which carries the WebSocket's upgrade), with the
// checks of who may reach it, the tickets and the notebook's own cookie on the notebooks'
// address (NOTEBOOKS__ORIGIN), the app's visitors and the pages said while a pod starts.

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/sha1" //nolint:gosec — the WebSocket handshake's accept key (RFC 6455)
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

const (
	nbCookie          = "tab_notebook"
	nbCookieSeconds   = 3600
	nbRenewAfter      = 600
	nbFrameType       = "notebook-frame"
	nbWSCheck         = 60 * time.Second
	nbFullReload      = 10
	nbFullClose       = 1013
	nbTakeTries       = 8
	nbTakePause       = 250 * time.Millisecond
	nbOpenFromTabular = "This address opens a notebook only from its page in Tabularia, once, within a minute. " +
		"Open the notebook again from Tabularia."
	whereFrame     = "frame"
	whereSame      = "same"
	whereElsewhere = "elsewhere"
	whereOff       = "off"
	framingSelf    = "'self'"
)

var (
	nbSafe        = map[string]bool{"GET": true, "HEAD": true, "OPTIONS": true}
	nbMethods     = []string{"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
	nbSkipRequest = map[string]bool{"host": true, "cookie": true, "authorization": true, "x-tabularia-csrf": true, "connection": true,
		"keep-alive": true, "te": true, "trailer": true, "transfer-encoding": true, "upgrade": true, "proxy-authorization": true,
		"proxy-connection": true, "content-length": true, "origin": true}
	nbSkipResponse = map[string]bool{"connection": true, "keep-alive": true, "te": true, "trailer": true, "transfer-encoding": true,
		"upgrade": true, "proxy-authenticate": true, "proxy-connection": true, "set-cookie": true, "server": true, "date": true,
		"x-frame-options": true}
	nbFrameAncestors = regexp.MustCompile(`(?i)frame-ancestors[^;]*`)
	nbDotSegment     = regexp.MustCompile(`(^|/)\.{1,2}(/|$)`)
)

func init() {
	register("notebook_proxy", func(d *Deps) []server.Route {
		var routes []server.Route
		for _, mode := range []string{"app", "edit"} {
			mode := mode
			routes = append(routes, server.Route{Method: http.MethodGet, Pattern: "/notebooks/{notebook_id}/" + mode,
				Handler: anonymous(d, nbWithoutSlash)})
			for _, m := range nbMethods {
				routes = append(routes, server.Route{Method: m, Pattern: "/notebooks/{notebook_id}/" + mode + "/{path...}",
					Handler: anonymous(d, func(w http.ResponseWriter, r *http.Request) { nbProxy(d, w, r, mode) })})
			}
			routes = append(routes, server.Route{Method: server.WebSocket, Pattern: "/notebooks/{notebook_id}/" + mode + "/{path...}",
				Handler: anonymous(d, func(w http.ResponseWriter, r *http.Request) { nbWebSocket(d, w, r, mode) })})
		}
		return routes
	})
}

// ── the notebooks' own address ──────────────────────────────────────────────

func nbOrigin() string { return scriptcfg.Get().Notebooks.Origin }

// IsNotebooksHost is notebook_frames.is_notebooks_host: the Host is the notebooks' own.
func IsNotebooksHost(host string) bool { return isNotebooksHostOf(host, nbOrigin()) }

func isNotebooksHostOf(host, mine string) bool {
	if mine == "" || host == "" {
		return false
	}
	host = strings.ToLower(strings.TrimSpace(host))
	def := ":80"
	if strings.HasPrefix(mine, "https:") {
		def = ":443"
	}
	host = strings.TrimSuffix(host, def)
	_, netloc := scSplitNetloc(mine)
	return host == netloc
}

// WithNotebooksHost is NotebooksHost (notebook_proxy.install): on the notebooks' own host only
// the notebooks' proxy exists — not the rest of the API, not even the routes sharing its paths
// (the editor's «Save version»); anything else is FastAPI's 404.
func WithNotebooksHost(d *Deps, next http.Handler) http.Handler {
	notFound := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if server.IsWebSocket(r) {
			nbWSRefuse(w)
			return
		}
		detail(w, http.StatusNotFound, "Not Found")
	})
	host := server.New(modules["notebook_proxy"](d), notFound)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if nbOrigin() != "" && IsNotebooksHost(r.Host) {
			host.ServeHTTP(w, r)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// nbWhere is _where.
func nbWhere(r *http.Request) string {
	if nbOrigin() != "" {
		if IsNotebooksHost(r.Host) {
			return whereFrame
		}
		return whereElsewhere
	}
	if scriptcfg.Get().Notebooks.AllowSameOrigin {
		return whereSame
	}
	return whereOff
}

// nbFromNotebooksPage is from_notebooks_page.
func nbFromNotebooksPage(r *http.Request) bool {
	sent := strings.ToLower(strings.TrimRight(strings.TrimSpace(r.Header.Get("Origin")), "/"))
	return sent != "" && sent == nbOrigin()
}

func nbFromItsPage(r *http.Request, where string) bool {
	if where == whereFrame {
		return nbFromNotebooksPage(r)
	}
	return scSameOrigin(r)
}

// ── the notebook's cookie ───────────────────────────────────────────────────

type nbClaims struct {
	UserID      int64
	SID         string
	NotebookID  int64
	Mode        string
	FrameOrigin string
	IssuedAt    int64
}

func nbCookieKey(d *Deps) []byte {
	m := hmac.New(sha256.New, d.Config.JWTSecret)
	m.Write([]byte("tabularia notebook frames"))
	return m.Sum(nil)
}

func nbFramePath(notebookID int64, mode string) string {
	return fmt.Sprintf("%s/notebooks/%d/%s/", scriptcfg.Get().App.RootPath, notebookID, mode)
}

func nbCookieValue(d *Deps, userID int64, sid string, notebookID int64, mode, frameOrigin string) string {
	now := time.Now()
	s, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"typ": nbFrameType, "sub": strconv.FormatInt(userID, 10), "sid": sid,
		"nb": notebookID, "mode": mode, "anc": frameOrigin, "iat": now.Unix(), "exp": now.Add(nbCookieSeconds * time.Second).Unix()}).
		SignedString(nbCookieKey(d))
	return s
}

func nbCookieHeader(value string, notebookID int64, mode string) string {
	return fmt.Sprintf("%s=%s; Path=%s; Max-Age=%d; HttpOnly; Secure; SameSite=None; Partitioned", nbCookie, value,
		nbFramePath(notebookID, mode), nbCookieSeconds)
}

func nbRenewed(d *Deps, c *nbClaims) string {
	if time.Now().Unix()-c.IssuedAt < nbRenewAfter {
		return ""
	}
	return nbCookieHeader(nbCookieValue(d, c.UserID, c.SID, c.NotebookID, c.Mode, c.FrameOrigin), c.NotebookID, c.Mode)
}

// nbReadCookie is read_cookie: the notebook's cookie when good and for this notebook and mode.
func nbReadCookie(d *Deps, r *http.Request, notebookID int64, mode string, anyAge bool) *nbClaims {
	c, err := r.Cookie(nbCookie)
	if err != nil || c.Value == "" || len(c.Value) > 4096 {
		return nil
	}
	opts := []jwt.ParserOption{jwt.WithValidMethods([]string{"HS256"}), jwt.WithLeeway(60 * time.Second), jwt.WithIssuedAt()}
	if anyAge {
		opts = append(opts, jwt.WithoutClaimsValidation())
	}
	data := jwt.MapClaims{}
	if _, err := jwt.NewParser(opts...).ParseWithClaims(c.Value, data, func(*jwt.Token) (any, error) { return nbCookieKey(d), nil }); err != nil {
		return nil
	}
	num := func(v any) (int64, bool) {
		switch x := v.(type) {
		case float64:
			return int64(x), x == float64(int64(x))
		case string:
			n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64)
			return n, err == nil
		}
		return 0, false
	}
	str := func(v any) (string, bool) {
		if v == nil {
			return "", false
		}
		return pythonStrOf(v), true
	}
	sub, ok1 := num(data["sub"])
	sid, ok2 := str(data["sid"])
	nb, ok3 := num(data["nb"])
	m, ok4 := str(data["mode"])
	anc, ok5 := str(data["anc"])
	iat, ok6 := num(data["iat"])
	if !(ok1 && ok2 && ok3 && ok4 && ok5 && ok6) {
		return nil
	}
	if data["typ"] != nbFrameType || nb != notebookID || m != mode {
		return nil
	}
	return &nbClaims{UserID: sub, SID: sid, NotebookID: nb, Mode: m, FrameOrigin: anc, IssuedAt: iat}
}

func pythonStrOf(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case float64:
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	}
	return fmt.Sprint(v)
}

// ── pages ───────────────────────────────────────────────────────────────────

func nbFrameHeaders(h http.Header, framing *string) {
	if framing == nil {
		return
	}
	h.Set("Content-Security-Policy", "frame-ancestors "+*framing)
	if *framing == framingSelf {
		h.Set("X-Frame-Options", "SAMEORIGIN")
	}
}

func pyEscape(s string) string {
	return strings.NewReplacer("&#34;", "&quot;", "&#39;", "&#x27;").Replace(html.EscapeString(s))
}

// nbPage is _page: a small page in the notebook's frame.
func nbPage(w http.ResponseWriter, title, message string, status int, framing *string, refresh bool, every int, extra map[string]string) {
	meta := ""
	if refresh {
		meta = fmt.Sprintf(`<meta http-equiv="refresh" content="%d">`, every)
	}
	role := "alert"
	if refresh {
		role = "status"
	}
	body := `<!doctype html><html lang="en"><head><meta charset="utf-8">` + meta +
		`<meta name="viewport" content="width=device-width, initial-scale=1"><title>` + pyEscape(title) + `</title>` +
		`<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;margin:0;display:grid;` +
		`place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}main{max-width:36rem}` +
		`p{white-space:pre-wrap}</style></head><body><main>` +
		`<h1 style="font-size:1.15rem">` + pyEscape(title) + `</h1>` +
		`<p role="` + role + `" aria-live="polite">` + pyEscape(message) + `</p>` + `</main></body></html>`
	h := w.Header()
	h.Set("Cache-Control", "no-store")
	nbFrameHeaders(h, framing)
	for k, v := range extra {
		h.Set(k, v)
	}
	h.Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

func strPtr(s string) *string { return &s }

func nbPeople(n int64) string {
	if n == 1 {
		return "1 person connected"
	}
	return fmt.Sprintf("%d people connected", n)
}

func nbFullReason(count int64) string {
	return "This app already has " + nbPeople(count) + ", the most it allows"
}

func nbStarting(w http.ResponseWriter, framing *string) {
	nbPage(w, "Starting…", "Starting the notebook. This page reloads by itself until it is ready.", 200, framing, true, 2, nil)
}

func nbFailedPage(w http.ResponseWriter, why *string, framing *string) {
	msg := "It stopped before answering."
	if why != nil && *why != "" {
		msg = *why
	}
	nbPage(w, "The notebook could not start", msg+"\n\nClose this page and open the notebook again to try once more.", 502, framing, false, 2, nil)
}

func nbJSON(w http.ResponseWriter, status int, text string, extra map[string]string) {
	for k, v := range extra {
		w.Header().Set(k, v)
	}
	detail(w, status, text)
}

// ── who, and which pod ──────────────────────────────────────────────────────

type nbFound struct {
	answered  bool
	userID    int64
	rowStatus string // "" = no session
	rowError  *string
	podIP     string
	staleApp  bool
	framing   *string
	claims    *nbClaims
}

// nbPerson is _person.
func nbPersonOfRequest(d *Deps, r *http.Request, notebookID int64, mode, where string, anyAge bool) (*auth.Principal, *string, *nbClaims, error) {
	ctx := r.Context()
	if where == whereFrame {
		claims := nbReadCookie(d, r, notebookID, mode, anyAge)
		if claims == nil {
			return nil, nil, nil, nil
		}
		p, err := d.Auth.SessionUser(ctx, r, claims.UserID, claims.SID)
		return p, strPtr(claims.FrameOrigin), claims, err
	}
	p, err := d.Auth.CookieUser(ctx, r)
	return p, strPtr(framingSelf), nil, err
}

// nbLookup is _lookup: who asks, whether they may, the pod; `answered` when the answer was
// written instead.
func nbLookup(d *Deps, w http.ResponseWriter, r *http.Request, notebookID int64, mode string, root bool, where string, anyAge bool,
	write bool) (*nbFound, error) {
	ctx := r.Context()
	p, framing, claims, err := nbPersonOfRequest(d, r, notebookID, mode, where, anyAge)
	if err != nil {
		return nil, err
	}
	answer := func(f func()) *nbFound {
		if write {
			f()
		}
		return &nbFound{answered: true}
	}
	if p == nil {
		if !root {
			return answer(func() { nbJSON(w, 401, "Invalid credentials", nil) }), nil
		}
		if where == whereFrame {
			return answer(func() { nbPage(w, "Open it from Tabularia", nbOpenFromTabular, 401, framing, false, 2, nil) }), nil
		}
		return answer(func() {
			nbPage(w, "Sign in", "Sign in to Tabularia to open this notebook.", 401, framing, false, 2, nil)
		}), nil
	}
	c := perm.For(d.DB, &p.User)
	n, err := nbGet(ctx, d.DB, notebookID, false)
	if err != nil {
		return nil, err
	}
	visible := false
	if n != nil {
		if visible, err = savedscripts.CanSee(ctx, c, d.DB, p.User.ID, n.ProjectID, n.EnvironmentID); err != nil {
			return nil, err
		}
		if visible && mode == "edit" {
			if visible, err = c.Has(ctx, n.ProjectID, perm.Edit); err != nil {
				return nil, err
			}
		}
	}
	if !visible {
		if root {
			return answer(func() {
				nbPage(w, "Not found", "There is no such notebook, or you may not open it.", 404, framing, false, 2, nil)
			}), nil
		}
		return answer(func() { nbJSON(w, 404, "Notebook not found", nil) }), nil
	}
	row, err := nbRowOf(ctx, d.DB, n.ID, mode)
	if err != nil {
		return nil, err
	}
	found := &nbFound{userID: p.User.ID, framing: framing, claims: claims}
	if row == nil {
		return found, nil
	}
	if mode == "edit" && row.UserID != p.User.ID && row.Status != "failed" {
		who := "someone else"
		if e, err := nbPersonOf(ctx, d.DB, &row.UserID); err != nil {
			return nil, err
		} else if e != nil {
			who = e.Email
		}
		message := fmt.Sprintf("«%s» is being edited by %s.", n.Name, who)
		if root {
			return answer(func() { nbPage(w, "Being edited", message, 409, framing, false, 2, nil) }), nil
		}
		return answer(func() { nbJSON(w, 409, message, nil) }), nil
	}
	found.rowStatus, found.rowError = row.Status, row.Error
	if row.PodIP != nil {
		found.podIP = *row.PodIP
	}
	found.staleApp = mode == "app" && row.Version != n.CurrentVersion
	if root && mode == "app" && row.Status == "ready" && !found.staleApp {
		count, err := nbVisitorCount(ctx, d.DB, n.ID)
		if err != nil {
			return nil, err
		}
		visiting, err := nbIsVisiting(ctx, d.DB, n.ID, p.User.ID)
		if err != nil {
			return nil, err
		}
		if count >= nbVisitorsCap(n.MaxVisitors) && !visiting {
			return answer(func() {
				nbPage(w, "This app is full", fmt.Sprintf("%s. It will open by itself as soon as someone leaves: this page checks again every %d seconds.",
					nbFullReason(count), nbFullReload), 503, framing, true, nbFullReload, map[string]string{"Retry-After": strconv.Itoa(nbFullReload)})
			}), nil
		}
	}
	if row.Status == "ready" {
		if err := nbTouch(ctx, d, row); err != nil {
			return nil, err
		}
	}
	return found, nil
}

// nbUseTicket is _use_ticket: the frame's first request on the notebooks' address.
func nbUseTicket(d *Deps, w http.ResponseWriter, r *http.Request, notebookID int64, mode string) {
	ctx := r.Context()
	q := r.URL.Query()
	ticket := ""
	if v, ok := q["ticket"]; ok && len(v) > 0 {
		ticket = v[len(v)-1]
	}
	type row struct {
		id, userID       int64
		sid, frameOrigin string
	}
	var found *row
	if ticket != "" && len([]rune(ticket)) <= 200 {
		sum := sha256.Sum256([]byte(ticket))
		var x row
		err := d.DB.QueryRow(ctx, `SELECT id, user_id, sid, frame_origin FROM notebook_tickets WHERE token_fp = $1 AND notebook_id = $2
			AND mode = $3 AND expires_at > $4 LIMIT 1`, hex.EncodeToString(sum[:]), notebookID, mode, nbNaiveNow()).
			Scan(&x.id, &x.userID, &x.sid, &x.frameOrigin)
		if err == nil {
			tag, err := d.DB.Exec(ctx, `DELETE FROM notebook_tickets WHERE id = $1`, x.id)
			if err != nil {
				fail(d, w, "notebooks: the ticket", err)
				return
			}
			if tag.RowsAffected() == 1 {
				found = &x
			}
		} else if !scNoRows(err) {
			fail(d, w, "notebooks: the ticket", err)
			return
		}
	}
	var rest []string
	for _, kv := range strings.Split(r.URL.RawQuery, "&") {
		if kv == "" {
			continue
		}
		k, v, _ := strings.Cut(kv, "=")
		ku, _ := url.QueryUnescape(strings.ReplaceAll(k, "+", " "))
		if ku == "ticket" {
			continue
		}
		vu, _ := url.QueryUnescape(strings.ReplaceAll(v, "+", " "))
		rest = append(rest, url.QueryEscape(ku)+"="+url.QueryEscape(vu))
	}
	target := "./"
	if len(rest) > 0 {
		target += "?" + strings.Join(rest, "&")
	}
	there := func(cookie string) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		if cookie != "" {
			w.Header().Add("Set-Cookie", cookie)
		}
		redirect(w, target, http.StatusSeeOther)
	}
	if found == nil {
		if nbReadCookie(d, r, notebookID, mode, false) != nil {
			there("")
			return
		}
		nbPage(w, "Open it from Tabularia", nbOpenFromTabular, 403, nil, false, 2, nil)
		return
	}
	there(nbCookieHeader(nbCookieValue(d, found.userID, found.sid, notebookID, mode, found.frameOrigin), notebookID, mode))
}

// nbStartPod is _start: the root of a notebook without a live pod; the pod's address, or
// the answer written.
func nbStartPod(d *Deps, w http.ResponseWriter, r *http.Request, notebookID int64, mode string, userID int64, framing *string) (string, bool) {
	ctx := r.Context()
	n, err := nbGet(ctx, d.DB, notebookID, false)
	if err != nil {
		fail(d, w, "notebooks: the notebook", err)
		return "", false
	}
	user, err := ssoActor(ctx, d, userID)
	if n == nil || err != nil {
		nbPage(w, "Not found", "There is no such notebook.", 404, framing, false, 2, nil)
		return "", false
	}
	row, err := nbEnsureStarted(ctx, d, r, n, mode, user)
	var be *nbBeingEdited
	var ref *auth.Refusal
	switch {
	case errors.As(err, &be):
		nbPage(w, "Being edited", be.message+".", 409, framing, false, 2, nil)
		return "", false
	case errors.As(err, &ref):
		nbPage(w, "The notebook cannot start", ref.Detail, ref.Status, framing, false, 2, nil)
		return "", false
	case err != nil:
		fail(d, w, "notebooks: starting the pod", err)
		return "", false
	}
	if row.Status == "failed" {
		nbFailedPage(w, row.Error, framing)
		return "", false
	}
	if row.Status != "ready" || row.PodIP == nil || *row.PodIP == "" {
		nbStarting(w, framing)
		return "", false
	}
	return *row.PodIP, true
}

// ── HTTP ────────────────────────────────────────────────────────────────────

func nbWithAncestors(policy, framing string) string {
	if strings.Contains(strings.ToLower(policy), "frame-ancestors") {
		return nbFrameAncestors.ReplaceAllString(policy, "frame-ancestors "+framing)
	}
	if strings.TrimSpace(policy) != "" {
		return strings.TrimRight(policy, "; ") + "; frame-ancestors " + framing
	}
	return "frame-ancestors " + framing
}

func nbOutOfBounds(mode, path string) bool {
	return nbDotSegment.MatchString(path) || (mode == "app" && strings.Trim(path, "/") == "sse")
}

// nbPathParts reads the notebook's id (a 422 when it is not one) and the path.
func nbPathParts(w http.ResponseWriter, r *http.Request) (int64, bool) {
	ids, ok := pathInts(w, r, "notebook_id")
	if !ok {
		return 0, false
	}
	return ids[0], true
}

// nbForward is _forward: the request to the pod, the answer streamed back with its headers
// filtered and framed. False when the pod was not reached (nothing written yet).
func nbForward(w http.ResponseWriter, r *http.Request, podIP string, notebookID int64, mode, path string, framing *string, renew string) bool {
	reached := true
	target := &url.URL{Scheme: "http", Host: net.JoinHostPort(podIP, strconv.Itoa(nbAppPort))}
	rp := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme, pr.Out.URL.Host = target.Scheme, target.Host
			pr.Out.URL.Path = nbBaseURL(notebookID, mode) + "/" + path
			pr.Out.URL.RawPath = ""
			pr.Out.URL.RawQuery = r.URL.RawQuery
			pr.Out.Host = target.Host
			for k := range pr.Out.Header {
				if nbSkipRequest[strings.ToLower(k)] {
					pr.Out.Header.Del(k)
				}
			}
			for _, h := range []string{"X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				if v, ok := r.Header[h]; ok {
					pr.Out.Header[h] = v
				}
			}
			if nbSafe[r.Method] {
				pr.Out.Body, pr.Out.ContentLength = http.NoBody, 0
			}
		},
		Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
			ResponseHeaderTimeout: 300 * time.Second},
		FlushInterval: -1,
		ModifyResponse: func(res *http.Response) error {
			out := http.Header{}
			hasCSP := false
			for k, vs := range res.Header {
				key := strings.ToLower(k)
				if nbSkipResponse[key] {
					continue
				}
				v := strings.Join(vs, ", ")
				if key == "content-security-policy" && framing != nil {
					v = nbWithAncestors(v, *framing)
					hasCSP = true
				}
				out.Set(k, v)
			}
			if framing != nil {
				if *framing == framingSelf {
					out.Set("X-Frame-Options", "SAMEORIGIN")
				} else if !hasCSP {
					out.Set("Content-Security-Policy", "frame-ancestors "+*framing)
				}
			}
			if renew != "" {
				out.Add("Set-Cookie", renew)
			}
			res.Header = out
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			reached = false
		},
	}
	rp.ServeHTTP(w, r)
	return reached
}

// nbProxy is _proxy: an HTTP request to the app or the editor.
func nbProxy(d *Deps, w http.ResponseWriter, r *http.Request, mode string) {
	notebookID, ok := nbPathParts(w, r)
	if !ok {
		return
	}
	path := r.PathValue("path")
	where := nbWhere(r)
	root := path == "" && r.Method == http.MethodGet
	if where == whereElsewhere || nbOutOfBounds(mode, path) {
		nbJSON(w, 404, "Not Found", nil)
		return
	}
	if where == whereOff {
		if root {
			nbPage(w, "Notebooks are not set up", nbNeedsOrigin+".", 409, strPtr(framingSelf), false, 2, nil)
		} else {
			nbJSON(w, 409, nbNeedsOrigin, nil)
		}
		return
	}
	if where == whereFrame && root && r.URL.Query().Has("ticket") {
		nbUseTicket(d, w, r, notebookID, mode)
		return
	}
	if !nbSafe[r.Method] && !nbFromItsPage(r, where) {
		nbJSON(w, 403, "This request must come from the notebook's own page", nil)
		return
	}
	ctx := r.Context()
	found, err := nbLookup(d, w, r, notebookID, mode, root, where, false, true)
	if err != nil {
		fail(d, w, "notebooks: the proxy", err)
		return
	}
	if found.answered {
		return
	}
	podIP := found.podIP
	if found.rowStatus != "ready" || (root && found.staleApp) {
		if !root {
			nbJSON(w, 503, "The notebook is starting: try again in a moment", map[string]string{"Retry-After": "2"})
			return
		}
		if found.rowStatus == "failed" {
			nbFailedPage(w, found.rowError, found.framing)
			return
		}
		if where == whereFrame && mode == "edit" && found.rowStatus == "" {
			nbPage(w, "The editor is not running", "It stopped, or it was not started. Open it again from Tabularia.", 409, found.framing, false, 2, nil)
			return
		}
		ip, ok := nbStartPod(d, w, r, notebookID, mode, found.userID, found.framing)
		if !ok {
			return
		}
		podIP = ip
	}
	renew := ""
	if found.claims != nil && (root || !nbSafe[r.Method]) {
		renew = nbRenewed(d, found.claims)
	}
	if nbForward(w, r, podIP, notebookID, mode, path, found.framing, renew) {
		return
	}
	d.Log.Info(fmt.Sprintf("notebooks: the pod of notebook %d (%s) not reached", notebookID, mode))
	row, err := nbRowOf(ctx, d.DB, notebookID, mode)
	if err != nil {
		fail(d, w, "notebooks: the session", err)
		return
	}
	gone := false
	if row != nil {
		alive, err := nbAlive(ctx, d, row)
		if err != nil {
			fail(d, w, "notebooks: the pod", err)
			return
		}
		gone = !alive
	}
	message := "The notebook is not answering: try again in a moment."
	if gone {
		message = "The notebook stopped. Reload the page to start it again."
	}
	if root {
		nbPage(w, "The notebook is not answering", message, 502, found.framing, false, 2, nil)
		return
	}
	nbJSON(w, 502, message, nil)
}

// nbWithoutSlash is without_slash: marimo answers under the address with the slash.
func nbWithoutSlash(w http.ResponseWriter, r *http.Request) {
	if nbWhere(r) == whereElsewhere {
		nbJSON(w, 404, "Not Found", nil)
		return
	}
	target := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:] + "/"
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	redirect(w, target, http.StatusTemporaryRedirect)
}

// ── WebSocket ───────────────────────────────────────────────────────────────

// nbWSRefuse is a handshake refused before it is accepted (Starlette's close before accept:
// uvicorn answers 403).
func nbWSRefuse(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusForbidden)
}

func nbWSAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")) //nolint:gosec
	return base64.StdEncoding.EncodeToString(sum[:])
}

// nbCloseFrame is a close frame from the server: unmasked, the code and the reason.
func nbCloseFrame(code int, reason string) []byte {
	payload := make([]byte, 2, 2+len(reason))
	binary.BigEndian.PutUint16(payload, uint16(code))
	payload = append(payload, reason...)
	return append([]byte{0x88, byte(len(payload))}, payload...)
}

// nbWSFull accepts the handshake and closes at once with 1013 and the reason: a refused
// handshake would reach the page without it.
func nbWSFull(w http.ResponseWriter, r *http.Request, count int64) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		nbWSRefuse(w)
		return
	}
	conn, buf, err := hj.Hijack()
	if err != nil {
		return
	}
	defer conn.Close()
	_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " +
		nbWSAccept(r.Header.Get("Sec-WebSocket-Key")) + "\r\n\r\n")
	_, _ = buf.Write(nbCloseFrame(nbFullClose, nbFullReason(count)))
	_ = buf.Flush()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _ = io.Copy(io.Discard, conn) // the browser's close answers ours
}

// nbPodStream is the pod's side of an upgraded connection, read frame by frame, so that a
// close frame of the gateway's own (the minute's check failed: 1008) goes in between two of
// the pod's frames, never inside one.
type nbPodStream struct {
	io.ReadWriteCloser
	mu      sync.Mutex
	pending []byte // a whole frame of the pod's not yet handed on
	closing []byte // the gateway's close frame, once decided
	done    bool
	br      *bufio.Reader
}

func (s *nbPodStream) closeWith(code int) {
	s.mu.Lock()
	s.closing = nbCloseFrame(code, "")
	s.mu.Unlock()
	_ = s.ReadWriteCloser.Close()
}

func (s *nbPodStream) Read(p []byte) (int, error) {
	s.mu.Lock()
	if len(s.pending) == 0 && s.closing != nil && !s.done {
		s.pending, s.done = s.closing, true
	}
	if len(s.pending) > 0 {
		n := copy(p, s.pending)
		s.pending = s.pending[n:]
		s.mu.Unlock()
		return n, nil
	}
	done := s.done
	s.mu.Unlock()
	if done {
		return 0, io.EOF
	}
	frame, err := nbReadFrame(s.br)
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		if s.closing != nil && !s.done {
			s.pending, s.done = s.closing, true
			n := copy(p, s.pending)
			s.pending = s.pending[n:]
			return n, nil
		}
		return 0, err
	}
	s.pending = frame
	n := copy(p, s.pending)
	s.pending = s.pending[n:]
	return n, nil
}

// nbReadFrame reads one whole WebSocket frame, as sent.
func nbReadFrame(r *bufio.Reader) ([]byte, error) {
	head := make([]byte, 2)
	if _, err := io.ReadFull(r, head); err != nil {
		return nil, err
	}
	frame := append([]byte{}, head...)
	length := uint64(head[1] & 0x7f)
	switch length {
	case 126:
		ext := make([]byte, 2)
		if _, err := io.ReadFull(r, ext); err != nil {
			return nil, err
		}
		frame, length = append(frame, ext...), uint64(binary.BigEndian.Uint16(ext))
	case 127:
		ext := make([]byte, 8)
		if _, err := io.ReadFull(r, ext); err != nil {
			return nil, err
		}
		frame, length = append(frame, ext...), binary.BigEndian.Uint64(ext)
	}
	if head[1]&0x80 != 0 {
		mask := make([]byte, 4)
		if _, err := io.ReadFull(r, mask); err != nil {
			return nil, err
		}
		frame = append(frame, mask...)
	}
	if length > 64*1024*1024 {
		return nil, errors.New("notebooks: a message of the pod past 64 MB")
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(r, payload); err != nil {
		return nil, err
	}
	return append(frame, payload...), nil
}

// nbWebSocket is _websocket: marimo's WebSocket carried to the pod (ReverseProxy, which
// upgrades), the person checked again every minute, the app's visitor counted while it is open.
func nbWebSocket(d *Deps, w http.ResponseWriter, r *http.Request, mode string) {
	var ps = pathIntsQuiet(r, "notebook_id")
	if ps < 0 {
		nbWSRefuse(w) // a path parameter that does not validate: the WebSocket is closed
		return
	}
	notebookID := ps
	path := r.PathValue("path")
	where := nbWhere(r)
	if where == whereElsewhere || where == whereOff || nbOutOfBounds(mode, path) || !nbFromItsPage(r, where) {
		nbWSRefuse(w)
		return
	}
	ctx := r.Context()
	found, err := nbLookup(d, w, r, notebookID, mode, false, where, false, false)
	if err != nil || found.answered || found.rowStatus != "ready" || found.podIP == "" {
		nbWSRefuse(w)
		return
	}
	var visitor int64
	if mode == "app" && path == "ws" {
		n, err := nbGet(ctx, d.DB, notebookID, false)
		if err != nil || n == nil {
			nbWSFull(w, r, 0)
			return
		}
		var count int64
		for attempt := 0; attempt < nbTakeTries; attempt++ {
			if visitor, count, err = nbTakeVisitor(ctx, d, n, found.userID); err != nil {
				fail(d, w, "notebooks: a visitor", err)
				return
			}
			if visitor != 0 {
				break
			}
			if attempt < nbTakeTries-1 {
				time.Sleep(nbTakePause)
			}
		}
		if visitor == 0 {
			nbWSFull(w, r, count)
			return
		}
		defer func() { _ = nbDropVisitor(context.WithoutCancel(ctx), d, visitor) }()
	}
	var stream *nbPodStream
	stop := make(chan struct{})
	defer close(stop)
	failed := false
	target := net.JoinHostPort(found.podIP, strconv.Itoa(nbAppPort))
	rp := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme, pr.Out.URL.Host = "http", target
			pr.Out.URL.Path, pr.Out.URL.RawPath = nbBaseURL(notebookID, mode)+"/"+path, ""
			pr.Out.URL.RawQuery = r.URL.RawQuery
			pr.Out.Host = target
			for _, h := range []string{"Cookie", "Authorization", "X-Tabularia-Csrf", "Origin", "Sec-Websocket-Extensions"} {
				pr.Out.Header.Del(h)
			}
		},
		Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 10 * time.Second}).DialContext},
		ModifyResponse: func(res *http.Response) error {
			if res.StatusCode != http.StatusSwitchingProtocols {
				return errors.New("notebooks: the pod refused the WebSocket")
			}
			rwc, ok := res.Body.(io.ReadWriteCloser)
			if !ok {
				return errors.New("notebooks: not an upgraded connection")
			}
			stream = &nbPodStream{ReadWriteCloser: rwc, br: bufio.NewReader(rwc)}
			res.Body = stream
			for k := range res.Header {
				if strings.EqualFold(k, "Sec-Websocket-Extensions") {
					res.Header.Del(k)
				}
			}
			go func() { // every minute: the person may still open it, the pod's idle time, the visitor's place
				t := time.NewTicker(nbWSCheck)
				defer t.Stop()
				for {
					select {
					case <-stop:
						return
					case <-t.C:
						bg := context.WithoutCancel(ctx)
						if visitor != 0 {
							_ = nbKeepVisitor(bg, d, visitor)
						}
						f, err := nbLookup(d, nil, r.WithContext(bg), notebookID, mode, false, nbWhere(r), true, false)
						if err != nil || f.answered || f.rowStatus != "ready" {
							stream.closeWith(1008)
							return
						}
					}
				}
			}()
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			failed = true
			d.Log.Info(fmt.Sprintf("notebooks: the WebSocket of notebook %d (%s) not opened (%v)", notebookID, mode, err))
			nbWSRefuse(w)
		},
	}
	rp.ServeHTTP(w, r)
	_ = failed
}

// pathIntsQuiet is the notebook's id from the path, -1 when it does not validate.
func pathIntsQuiet(r *http.Request, name string) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(r.PathValue(name)), 10, 64)
	if err != nil {
		return -1
	}
	return n
}

var _ = reqinfo.From
