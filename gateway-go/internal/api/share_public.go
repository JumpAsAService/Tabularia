package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/loginthrottle"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/passwords"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sharelinks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// Share links, the recipient's side (docs/specs/share-links.md): the routes of /share/{token},
// with no account and no Tabularia session. Each request finds the link, checks that it may
// answer (the switch, its life, its creator's account and MANAGE), checks the link's own
// session, and then is served by the viewer's own handler **as the creator**, so that what a
// link shows is exactly what the creator's viewer would show now. Nothing here reads the app's
// session cookie, and nothing else reads the link's.

func init() {
	register("share_public", func(d *Deps) []server.Route {
		route := func(method, pattern string, fn http.HandlerFunc) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: fn}
		}
		body := func(method, pattern string, fn http.HandlerFunc) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: withBody(fn)}
		}
		return []server.Route{
			route(http.MethodGet, "/share/{token}/frame", shareFrame(d)),
			body(http.MethodPost, "/share/{token}/session", shareSession(d)),
			body(http.MethodPost, "/share/{token}/password", sharePassword(d)),
			route(http.MethodGet, "/share/{token}", shareAbout(d)),
			route(http.MethodGet, "/share/{token}/parameters", shareParameters(d)),
			body(http.MethodPost, "/share/{token}/data", shareData(d)),
			body(http.MethodPost, "/share/{token}/render", shareRender(d)),
			route(http.MethodGet, "/share/{token}/exports/{handle}", shareExport(d, false)),
			route(http.MethodGet, "/share/{token}/exports/{handle}/file", shareExport(d, true)),
			body(http.MethodPost, "/share/{token}/comment", shareComment(d)),
		}
	})
}

// shareHeaders go on every answer of a link: not kept by a cache, not indexed, no Referer that
// would carry the token elsewhere.
func shareHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Cache-Control", "no-store")
	h.Set("X-Robots-Tag", "noindex, nofollow")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("X-Content-Type-Options", "nosniff")
}

func notAvailable(w http.ResponseWriter) { detail(w, http.StatusNotFound, sharelinks.NotAvailable) }

// sharedLink is a request of a link that may answer.
type sharedLink struct {
	d        *Deps
	token    string
	link     *sharelinks.Link
	settings sharelinks.Settings
	creator  *auth.User
	item     *versioned.Item
	h        *objectsAPI
}

// openShared finds the link and checks that it may answer; otherwise it answers «not available»
// (the same for an unknown, expired, revoked, locked or suspended link) and returns nil.
func openShared(w http.ResponseWriter, r *http.Request, d *Deps) *sharedLink {
	shareHeaders(w)
	s, err := findShared(r.Context(), d, r.PathValue("token"))
	if err != nil {
		fail500(d, w, "share link", err)
		return nil
	}
	if s == nil {
		notAvailable(w)
	}
	return s
}

func findShared(ctx context.Context, d *Deps, token string) (*sharedLink, error) {
	settings, err := sharelinks.LoadSettings(ctx, d.DB)
	if err != nil || !settings.Enabled {
		return nil, err
	}
	l, err := sharelinks.ByToken(ctx, d.DB, token)
	if err != nil || !sharelinks.Live(l, sharelinks.Now()) {
		return nil, err
	}
	creator, err := loadUser(ctx, d.DB, l.CreatorID)
	if err != nil {
		return nil, err
	}
	s := &sharedLink{d: d, token: token, link: l, settings: settings, creator: creator}
	switch l.Kind {
	case sharelinks.Report:
		s.h = reportsAPI(d)
	case sharelinks.Dashboard:
		s.h = dashboardsAPI(d)
	default:
		return nil, nil
	}
	if s.item, err = s.h.kind.Get(ctx, d.DB, l.ObjectID); err != nil {
		return nil, err
	}
	why, err := suspension(ctx, d, settings, creator, s.item)
	if err != nil || why != "" {
		return nil, err
	}
	return s, nil
}

func cookiePath(token string) string { return scriptcfg.Get().App.RootPath + "/share/" + token }

func (s *sharedLink) setCookie(w http.ResponseWriter, r *http.Request, secret string, until time.Time) {
	seconds := int(time.Until(until).Seconds())
	if seconds < 0 {
		seconds = 0
	}
	c := fmt.Sprintf("%s=%s; HttpOnly; Max-Age=%d; Path=%s; SameSite=Strict", sharelinks.CookieName, secret, seconds, cookiePath(s.token))
	if auth.InHTTPS(r) {
		c += "; Secure"
	}
	w.Header().Add("Set-Cookie", c)
}

// secretOf is the session's secret this request carries for the link: the cookie for a link
// with a password, the header for an embeddable one (its frame has no first-party cookies).
func (s *sharedLink) secretOf(r *http.Request) string {
	if s.link.Embeddable {
		return r.Header.Get(sharelinks.SessionHeader)
	}
	if c, err := r.Cookie(sharelinks.CookieName); err == nil {
		return c.Value
	}
	return ""
}

// viewing checks the link's session; false when it answered 401.
func (s *sharedLink) viewing(w http.ResponseWriter, r *http.Request) bool {
	id, err := sharelinks.Session(r.Context(), s.d.DB, s.link.ID, sharelinks.PurposeView, s.secretOf(r))
	if err != nil {
		fail500(s.d, w, "share link", err)
		return false
	}
	if id == "" {
		server.JSON(w, http.StatusUnauthorized, map[string]any{"detail": "Enter the link's password", "code": "session"})
		return false
	}
	return true
}

// csrf is the app's rule for a POST with a cookie: the custom header a cross-site form cannot set.
func csrf(w http.ResponseWriter, r *http.Request) bool {
	if r.Header.Get(auth.CSRFHeader) == "" {
		detail(w, http.StatusForbidden, "The request is missing the X-Tabularia-Csrf header")
		return false
	}
	return true
}

// asCreator is the request as the viewer's handler takes it: the creator as the principal, the
// link in the audit, the object's id in the path.
func (s *sharedLink) asCreator(r *http.Request) (*http.Request, *auth.Principal) {
	p := &auth.Principal{User: *s.creator}
	r2 := reqinfo.With(r, s.d.Auth.ClientIP, p)
	reqinfo.From(r2.Context()).ShareLink = s.link.ID
	r2.SetPathValue(s.h.param, strconv.FormatInt(s.item.ID, 10))
	return r2, p
}

func (s *sharedLink) ip(r *http.Request) string { return s.d.Auth.ClientIP.Of(r) }

// GET /share/{token}/frame?dest=…: for the interface's server, which writes the page's
// frame-ancestors. `dest` is the browser's Sec-Fetch-Dest of the page, for the audit.
func shareFrame(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		s, err := findShared(r.Context(), d, r.PathValue("token"))
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if s == nil {
			server.JSON(w, http.StatusOK, map[string]any{"available": false, "embeddable": false, "frame_ancestors": "'none'"})
			return
		}
		if dest := r.URL.Query().Get("dest"); s.link.Embeddable && dest != "iframe" {
			auditShare(r.Context(), d, nil, "share_link.refused_frame", "failure", s.link, pyjson.Object{{Key: "dest", Value: pystr.Cut(dest, 20)}})
		}
		server.JSON(w, http.StatusOK, map[string]any{"available": true, "embeddable": s.link.Embeddable,
			"frame_ancestors": sharelinks.FrameAncestors(s.link)})
	}
}

var (
	decoyOnce sync.Once
	decoy     string
)

// decoyHash is compared against when there is no link, so that a wrong token costs the time of
// a wrong password.
func decoyHash() string {
	decoyOnce.Do(func() { decoy, _ = passwords.Hash("not a link: a fixed decoy for the timing") })
	return decoy
}

func throttleKey(l *sharelinks.Link) string { return " link:" + strconv.FormatInt(l.ID, 10) }

// POST /share/{token}/session
func shareSession(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		if !csrf(w, r) {
			return
		}
		ctx := r.Context()
		s, err := findShared(ctx, d, r.PathValue("token"))
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		var in struct {
			Password string `json:"password"`
		}
		_ = json.Unmarshal(rawOf(r), &in)
		if s == nil {
			if in.Password != "" {
				passwords.Verify(in.Password, decoyHash())
			}
			notAvailable(w)
			return
		}
		ip := s.ip(r)
		if s.link.Embeddable {
			secret, until, err := sharelinks.OpenSession(ctx, d.DB, s.link.ID, sharelinks.PurposeView, ip)
			if err == nil {
				err = sharelinks.Opened(ctx, d.DB, s.link.ID)
			}
			if err != nil {
				fail500(d, w, "share link", err)
				return
			}
			auditShare(ctx, d, nil, "share_link.open", "", s.link, pyjson.Object{{Key: "mode", Value: "embed"}})
			server.JSON(w, http.StatusOK, map[string]any{"session": secret, "expires_at": until})
			return
		}
		key := throttleKey(s.link)
		wait, err := loginthrottle.RequestWait(ctx, d.DB, &ip, key)
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if wait > 0 {
			seconds := loginthrottle.Seconds(wait)
			w.Header().Set("Retry-After", strconv.Itoa(seconds))
			server.JSON(w, http.StatusTooManyRequests, map[string]any{"detail": fmt.Sprintf("Too many attempts: try again in %d seconds", seconds),
				"code": "throttled", "retry_after": seconds})
			return
		}
		if s.link.PasswordHash == nil || in.Password == "" || !passwords.Verify(in.Password, *s.link.PasswordHash) {
			if err := loginthrottle.RecordFailure(ctx, d.DB, &ip, key); err != nil {
				fail500(d, w, "share link", err)
				return
			}
			locked, err := sharelinks.Failed(ctx, d.DB, s.link.ID)
			if err != nil {
				fail500(d, w, "share link", err)
				return
			}
			auditShare(ctx, d, nil, "share_link.unlock_failed", "failure", s.link, nil)
			if locked {
				auditShare(ctx, d, nil, "share_link.locked", "failure", s.link, pyjson.Object{{Key: "failures", Value: int64(sharelinks.LockAfter)}})
			}
			server.JSON(w, http.StatusUnauthorized, map[string]any{"detail": "Wrong password", "code": "wrong_password"})
			return
		}
		if err := loginthrottle.RecordSuccess(ctx, d.DB, &ip, key); err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if s.link.PasswordTemporary {
			secret, until, err := sharelinks.OpenSession(ctx, d.DB, s.link.ID, sharelinks.PurposeChange, ip)
			if err != nil {
				fail500(d, w, "share link", err)
				return
			}
			s.setCookie(w, r, secret, until)
			server.JSON(w, http.StatusOK, map[string]any{"change_password": true, "rules": passwordRulesOut()})
			return
		}
		if !s.openView(w, r, "password") {
			return
		}
		server.JSON(w, http.StatusOK, map[string]any{"change_password": false})
	}
}

func passwordRulesOut() map[string]int { return map[string]int{"min_length": 12, "max_bytes": 72} }

// openView opens a view session with its cookie, counts and audits the opening.
func (s *sharedLink) openView(w http.ResponseWriter, r *http.Request, mode string) bool {
	ctx := r.Context()
	secret, until, err := sharelinks.OpenSession(ctx, s.d.DB, s.link.ID, sharelinks.PurposeView, s.ip(r))
	if err == nil {
		err = sharelinks.Opened(ctx, s.d.DB, s.link.ID)
	}
	if err != nil {
		fail500(s.d, w, "share link", err)
		return false
	}
	s.setCookie(w, r, secret, until)
	auditShare(ctx, s.d, nil, "share_link.open", "", s.link, pyjson.Object{{Key: "mode", Value: mode}})
	return true
}

// POST /share/{token}/password: the recipient's own password, after the temporary one.
func sharePassword(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		if !csrf(w, r) {
			return
		}
		s := openShared(w, r, d)
		if s == nil {
			return
		}
		ctx := r.Context()
		if s.link.Embeddable || s.link.PasswordHash == nil {
			notAvailable(w)
			return
		}
		sid, err := sharelinks.Session(ctx, d.DB, s.link.ID, sharelinks.PurposeChange, s.secretOf(r))
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if sid == "" {
			server.JSON(w, http.StatusUnauthorized, map[string]any{"detail": "Enter the temporary password again", "code": "session"})
			return
		}
		var in struct {
			Password string `json:"password"`
		}
		if err := json.Unmarshal(rawOf(r), &in); err != nil {
			detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON")
			return
		}
		if problem := gatewaymodels.PasswordProblem(in.Password); problem != "" {
			server.JSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": sentence(problem), "code": "weak_password"})
			return
		}
		if passwords.Verify(in.Password, *s.link.PasswordHash) {
			server.JSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "Choose a password different from the temporary one",
				"code": "same_password"})
			return
		}
		hash, err := passwords.Hash(in.Password)
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		won, err := sharelinks.SetOwnPassword(ctx, d.DB, s.link.ID, *s.link.PasswordHash, hash)
		if err == nil {
			err = sharelinks.CloseSession(ctx, d.DB, sid)
		}
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if !won {
			server.JSON(w, http.StatusConflict, map[string]any{"detail": "The password of this link has already been chosen: ask whoever sent it",
				"code": "already_chosen"})
			return
		}
		auditShare(ctx, d, nil, "share_link.password_set", "", s.link, nil)
		if !s.openView(w, r, "password") {
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}
}

// projection is what the page may know of a definition: everything the viewer draws with (the
// layout, the items, the parameters), and of its datasets only their names: never a source
// (SQL, connection, datasource, view, script).
func projection(def pyjson.Object) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range def {
		if kv.Key == "datasets" {
			names := []any{}
			if list, ok := kv.Value.([]any); ok {
				for _, ds := range list {
					names = append(names, pyjson.Object{{Key: "name", Value: pyjson.Obj(ds).Value("name")}})
				}
			}
			out = append(out, pyjson.KV{Key: "datasets", Value: names})
			continue
		}
		out = append(out, kv)
	}
	return out
}

func (s *sharedLink) content(ctx context.Context) (pyjson.Object, error) {
	content, err := s.h.kind.ContentOf(ctx, s.d.DB, s.item, nil)
	if err != nil {
		return nil, err
	}
	def, ok := content.(pyjson.Object)
	if !ok {
		return nil, versioned.ErrNotAnObject
	}
	return def, nil
}

// GET /share/{token}: what the page shows.
func shareAbout(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		def, err := s.content(r.Context())
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		server.JSON(w, http.StatusOK, map[string]any{"kind": s.link.Kind, "name": s.item.Name, "description": s.item.Description,
			"label": s.link.Label, "embeddable": s.link.Embeddable, "downloads": s.link.Downloads, "params": s.link.Params,
			"has_comment": reports.HasComment(def), "expires_at": s.link.ExpiresAt, "content": projection(def)})
	}
}

// GET /share/{token}/parameters
func shareParameters(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		r2, p := s.asCreator(r)
		if s.link.Kind == sharelinks.Dashboard {
			s.h.dashboardParameters(w, r2, p)
			return
		}
		s.h.parameters(w, r2, p)
	}
}

// POST /share/{token}/data: a dashboard's numbers.
func shareData(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		if !csrf(w, r) {
			return
		}
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		if s.link.Kind != sharelinks.Dashboard {
			detail(w, http.StatusNotFound, "Not found")
			return
		}
		r2, p := s.asCreator(r)
		s.h.data(w, r2, p, bodyOf(r), rawOf(r))
	}
}

// renderIn is the body of POST /share/{token}/render, read as the viewer's.
type renderIn struct {
	format   string
	given    reports.Given
	state    pyjson.Object
	hasState bool
	clicks   []any
}

func readRenderIn(raw []byte) (*renderIn, error) {
	v, err := pyjson.Loads(string(raw))
	o, ok := v.(pyjson.Object)
	if err != nil || !ok {
		return nil, &reports.Refusal{Status: http.StatusUnprocessableEntity, Detail: "The request is not a JSON object"}
	}
	in := &renderIn{format: pystr.Str(o.Value("format"))}
	params, _ := o.Value("params").(pyjson.Object)
	if in.given, err = reports.GivenParams(params); err != nil {
		return nil, err
	}
	if st, has := o.Get("state"); has && st != nil {
		in.state, _ = st.(pyjson.Object)
		in.hasState = true
	}
	in.clicks, _ = o.Value("clicks").([]any)
	return in, nil
}

func (s *sharedLink) allows(format string) bool {
	if s.link.Kind == sharelinks.Report && format == "svg" { // the pages on screen
		return true
	}
	for _, f := range s.link.Downloads {
		if f == format {
			return true
		}
	}
	return false
}

// POST /share/{token}/render: a report's pages, or a download the link allows.
func shareRender(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		if !csrf(w, r) {
			return
		}
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		in, err := readRenderIn(rawOf(r))
		if err != nil {
			s.h.writeErr(w, r, rawOf(r), err)
			return
		}
		if !s.allows(in.format) {
			detail(w, http.StatusForbidden, "This link does not allow that download")
			return
		}
		r2, p := s.asCreator(r)
		ctx := r2.Context()
		def, err := s.content(ctx)
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		c := perm.For(d.DB, s.creator)
		var comments pyjson.Object
		var reading any
		if reports.HasComment(def) && in.format != "xlsx" {
			comments, reading, _ = s.comments(ctx, c, def, in.given, in.hasState)
			if comments == nil {
				comments = pyjson.Object{}
			}
		}
		var started *reports.Started
		if s.link.Kind == sharelinks.Dashboard {
			started, err = s.h.dashboardRender(ctx, p, c, s.item, def, in.format, in.given, in.clicks, in.state, in.hasState, "share_link", true, comments)
		} else {
			started, err = s.reportRender(ctx, p, c, def, in, comments, reading)
		}
		if err != nil {
			s.h.writeErr(w, r2, rawOf(r), err)
			return
		}
		server.JSON(w, http.StatusOK, map[string]any{"task_id": sharelinks.ExportHandle(d.Auth.Secret, s.link.ID, pystr.Str(started.TaskID)),
			"filename": started.Filename})
	}
}

func (s *sharedLink) reportRender(ctx context.Context, p *auth.Principal, c *perm.Checker, def pyjson.Object, in *renderIn,
	comments pyjson.Object, reading any) (*reports.Started, error) {
	if err := reports.CheckState(in.state, in.hasState); err != nil {
		return nil, err
	}
	name := s.item.Name
	started, dt, err := s.h.resolver(c).Render(ctx, def, reports.RenderRequest{Name: &name, Format: in.format, Given: in.given,
		State: in.state, HasState: in.hasState, Origin: "share_link", Owner: exportOwner(p.User.ID), Comments: comments, Reading: reading})
	outcome := "success"
	if err != nil {
		outcome = "failure"
	}
	if in.format != "svg" || err != nil { // the pages on screen are not a document that left
		s.h.auditRender(ctx, p, s.item, outcome, dt)
	}
	return started, err
}

// GET /share/{token}/exports/{handle}[/file]
func shareExport(d *Deps, file bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		task := sharelinks.ExportTask(d.Auth.Secret, s.link.ID, r.PathValue("handle"))
		if !taskIDRe.MatchString(task) {
			detail(w, http.StatusNotFound, "Export not found")
			return
		}
		path := "/tasks/exports/" + task
		if file {
			path += "/file"
		}
		d.forward(w, r, http.MethodGet, withQuery(path, []qsPair{{"owner", exportOwner(s.creator.ID)}}), nil, file)
	}
}

// commentsLast is what ai_last keeps: the comments the link was last given.
type commentsLast struct {
	Comments json.RawMessage     `json:"comments"`
	Given    map[string][]string `json:"given"`
	At       time.Time           `json:"at"`
}

// comments asks the AI service for the document's comments with these values, under the link's
// cap: past it (or with the AI service failing) the last ones the link was given, if any. The
// meta says which: their values and time, and whether they are the last ones.
func (s *sharedLink) comments(ctx context.Context, c *perm.Checker, def pyjson.Object, given reports.Given, reuse bool) (pyjson.Object, any, map[string]any) {
	d := s.d
	shown := map[string][]string{}
	for _, kv := range given {
		if list, ok := kv.Value.([]any); ok {
			for _, v := range list {
				shown[kv.Key] = append(shown[kv.Key], pystr.Str(v))
			}
		}
	}
	last := func() (pyjson.Object, any, map[string]any) {
		if s.link.AILast == nil {
			return nil, nil, map[string]any{"stale": true, "available": false}
		}
		var kept commentsLast
		if json.Unmarshal([]byte(*s.link.AILast), &kept) != nil {
			return nil, nil, map[string]any{"stale": true, "available": false}
		}
		v, err := pyjson.Loads(string(kept.Comments))
		o, ok := v.(pyjson.Object)
		if err != nil || !ok {
			return nil, nil, map[string]any{"stale": true, "available": false}
		}
		return o, nil, map[string]any{"stale": true, "available": true, "given": kept.Given, "at": kept.At}
	}
	if reuse { // a click on the pages: the same numbers, the comment already written for these values
		if o, rd, meta := last(); o != nil && sameGiven(meta["given"], shown) {
			meta["stale"] = false
			return o, rd, meta
		}
	}
	ok, err := sharelinks.TakeAICall(ctx, d.DB, s.link.ID, s.settings.AICommentsPerHour, sharelinks.Now())
	if err != nil || !ok {
		if err != nil {
			d.Log.Error("share link: the AI cap", "link", s.link.ID, "err", err)
		}
		return last()
	}
	doc := renderedDoc{item: s.item, definition: def, given: given}
	where, ofReport := "report", true
	if s.link.Kind == sharelinks.Dashboard {
		doc.content, doc.definition = def, pyjson.Obj(dashboards.LaidOut(def).Value("definition"))
		where, ofReport = "dashboard", false
	}
	dt := pyjson.Object{}
	values, datasets, err := s.h.resolver(c).Prepare(ctx, doc.definition, given, &dt)
	if err != nil {
		d.Log.Info("share link: the numbers of the AI comment not read", "link", s.link.ID, "err", err)
		return last()
	}
	doc.datasets, doc.params = datasets, pyjson.Object{}
	for _, kv := range values {
		doc.params = append(doc.params, pyjson.KV{Key: kv.Key, Value: reportparams.ToJSON(kv.Value)})
	}
	comments, reading, err := d.documentComments(ctx, s.creator, &doc, where, ofReport)
	if err != nil {
		d.Log.Warn("share link: the AI service did not comment", "link", s.link.ID, "err", err)
		return last()
	}
	text, _ := pyjson.Dumps(comments, pyjson.Options{Compact: true})
	at := sharelinks.Now()
	if keep, err := json.Marshal(commentsLast{Comments: json.RawMessage(text), Given: shown, At: at}); err == nil {
		if err := sharelinks.KeepAILast(ctx, d.DB, s.link.ID, string(keep)); err != nil {
			d.Log.Error("share link: the last AI comment not kept", "link", s.link.ID, "err", err)
		}
	}
	return comments, reading, map[string]any{"stale": false, "available": true, "given": shown, "at": at}
}

// POST /share/{token}/comment: the document's AI comments with these values.
func shareComment(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		shareHeaders(w)
		if !csrf(w, r) {
			return
		}
		s := openShared(w, r, d)
		if s == nil || !s.viewing(w, r) {
			return
		}
		in, err := readRenderIn(rawOf(r))
		if err != nil {
			s.h.writeErr(w, r, rawOf(r), err)
			return
		}
		r2, _ := s.asCreator(r)
		ctx := r2.Context()
		def, err := s.content(ctx)
		if err != nil {
			fail500(d, w, "share link", err)
			return
		}
		if !reports.HasComment(def) {
			server.JSON(w, http.StatusOK, map[string]any{"comments": map[string]any{}, "stale": false, "available": false})
			return
		}
		comments, _, meta := s.comments(ctx, perm.For(d.DB, s.creator), def, in.given, false)
		out := map[string]any{}
		for k, v := range meta {
			out[k] = v
		}
		if comments == nil {
			comments = pyjson.Object{}
		}
		text, _ := pyjson.Dumps(comments, pyjson.Options{Compact: true})
		out["comments"] = json.RawMessage(text)
		server.JSON(w, http.StatusOK, out)
	}
}

func sameGiven(a any, b map[string][]string) bool {
	m, ok := a.(map[string][]string)
	if !ok || len(m) != len(b) {
		return false
	}
	for k, v := range b {
		w := m[k]
		if len(w) != len(v) {
			return false
		}
		for i := range v {
			if v[i] != w[i] {
				return false
			}
		}
	}
	return true
}
