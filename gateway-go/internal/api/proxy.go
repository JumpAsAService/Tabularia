package api

import (
	"bytes"
	"compress/gzip"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/enginepolicy"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/objects"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/proxy.py: the routes forwarded to the engine, with the per-object checks of the
// data plane.

func init() {
	register("proxy", func(d *Deps) []server.Route {
		a := func(pattern string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, pattern, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/tasks/operations", Handler: a("/tasks/operations", d.proxyOperations)},
			{Method: http.MethodGet, Pattern: "/engines", Handler: a("/engines", d.proxyEngines)},
			{Method: http.MethodPost, Pattern: "/tasks/preview", Handler: a("/tasks/preview", d.proxyPreview)},
			{Method: http.MethodPost, Pattern: "/tasks/cache-status", Handler: a("/tasks/cache-status", d.proxyCacheStatus)},
			{Method: http.MethodPost, Pattern: "/tasks/transform-data", Handler: a("/tasks/transform-data", d.proxyTransform)},
			{Method: http.MethodPost, Pattern: "/tasks/export", Handler: a("/tasks/export", d.proxyExport)},
			{Method: http.MethodPost, Pattern: "/tasks/exports", Handler: a("/tasks/exports", d.proxyStartExport)},
			{Method: http.MethodGet, Pattern: "/tasks/exports/{task_id}", Handler: a("/tasks/exports/{task_id}", d.proxyExportStatus)},
			{Method: http.MethodGet, Pattern: "/tasks/exports/{task_id}/file", Handler: a("/tasks/exports/{task_id}/file", d.proxyExportFile)},
			{Method: http.MethodGet, Pattern: "/tasks/{task_id}", Handler: a("/tasks/{task_id}", d.proxyTaskStatus)},
			{Method: http.MethodPost, Pattern: "/files", Handler: a("/files", d.proxyUpload)},
		}
	})
}

// skipRequestHeaders are _SKIP_REQUEST_HEADERS: the hop-by-hop ones and the user's session.
var skipRequestHeaders = map[string]bool{"Host": true, "Content-Length": true, "Authorization": true, "Cookie": true,
	"X-Tabularia-Csrf": true, "Connection": true}

// passResponseHeaders are _PASS_RESPONSE_HEADERS.
var passResponseHeaders = []string{"Content-Type", "Content-Disposition", "Content-Length"}

// ── the query string, as Starlette reads it and httpx writes it again ────────

type qsPair struct{ k, v string }

// parseQSL is urllib.parse.parse_qsl(qs, keep_blank_values=True), as Starlette's
// request.query_params reads the browser's query.
func parseQSL(qs string) []qsPair {
	var out []qsPair
	for _, part := range strings.Split(qs, "&") {
		if part == "" {
			continue
		}
		k, v, _ := strings.Cut(part, "=")
		out = append(out, qsPair{unquotePlus(k), unquotePlus(v)})
	}
	return out
}

// unquotePlus is urllib's unquote_plus: «+» is a blank, a broken escape stays as it is, bytes
// that are no UTF-8 become U+FFFD.
func unquotePlus(s string) string {
	s = strings.ReplaceAll(s, "+", " ")
	var b []byte
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s)+0 && i+2 <= len(s)-1 {
			if n, err := strconv.ParseUint(s[i+1:i+3], 16, 8); err == nil {
				b = append(b, byte(n))
				i += 2
				continue
			}
		}
		b = append(b, s[i])
	}
	return strings.ToValidUTF8(string(b), "�")
}

// quotePlus is urllib's quote_plus: letters, digits and «_.-~» as they are, a blank as «+».
func quotePlus(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '_', c == '.', c == '-', c == '~':
			b.WriteByte(c)
		case c == ' ':
			b.WriteByte('+')
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// encodeQuery is httpx's encoding of a query (urlencode of the pairs, in order).
func encodeQuery(pairs []qsPair) string {
	parts := make([]string, len(pairs))
	for i, p := range pairs {
		parts[i] = quotePlus(p.k) + "=" + quotePlus(p.v)
	}
	return strings.Join(parts, "&")
}

func withQuery(path string, pairs []qsPair) string {
	if q := encodeQuery(pairs); q != "" {
		return path + "?" + q
	}
	return path
}

// ── forwarding ────────────────────────────────────────────────────────────────

// engineRequest builds the request to the engine with the browser's headers (_request_headers):
// they go over the client's own, as httpx merges them.
func (d *Deps) engineRequest(r *http.Request, method, path string, body io.Reader) (*http.Request, error) {
	req, err := d.Engine.Request(r.Context(), method, path, body)
	if err != nil {
		return nil, err
	}
	for k, vs := range r.Header {
		if skipRequestHeaders[http.CanonicalHeaderKey(k)] {
			continue
		}
		req.Header[k] = append([]string(nil), vs...)
	}
	return req, nil
}

// forward is _forward: the request to the engine, the answer streamed back with its status
// and the headers the browser needs. `content` nil: the browser's body (POST, PUT, PATCH).
func (d *Deps) forward(w http.ResponseWriter, r *http.Request, method, path string, content []byte, compressJSON bool) {
	var body io.Reader
	if content != nil {
		body = bytes.NewReader(content)
	} else if method == http.MethodPost || method == http.MethodPut || method == http.MethodPatch {
		body = r.Body
	}
	req, err := d.engineRequest(r, method, path, body)
	if err != nil {
		d.Log.Error("engine request", "err", err)
		server.InternalError(w)
		return
	}
	if content != nil {
		req.ContentLength = int64(len(content))
	}
	res, err := d.Engine.DoRaw(req)
	if err != nil {
		d.Log.Error("engine unreachable", "path", path, "err", err)
		server.InternalError(w)
		return
	}
	defer res.Body.Close()
	h := w.Header()
	for _, k := range passResponseHeaders {
		if v, ok := res.Header[k]; ok {
			h[k] = v
		}
	}
	if v := res.Header.Get("Content-Disposition"); v != "" {
		h.Set("Content-Disposition", asciiFallback(v))
	}
	if _, ok := h["Content-Type"]; !ok {
		h["Content-Type"] = nil // as Starlette: no type the engine did not give
	}
	if compressJSON && res.StatusCode == http.StatusOK && strings.HasPrefix(res.Header.Get("Content-Type"), "application/json") &&
		strings.Contains(strings.ToLower(r.Header.Get("Accept-Encoding")), "gzip") {
		h.Del("Content-Length")
		h.Set("Content-Encoding", "gzip")
		h.Set("Vary", "Accept-Encoding")
		w.WriteHeader(res.StatusCode)
		zw, _ := gzip.NewWriterLevel(w, 6)
		_, _ = io.Copy(zw, res.Body)
		_ = zw.Close()
		return
	}
	w.WriteHeader(res.StatusCode)
	_, _ = io.Copy(w, res.Body)
}

// unquote is urllib's unquote (no «+»).
func unquote(s string) string {
	var b []byte
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			if n, err := strconv.ParseUint(s[i+1:i+3], 16, 8); err == nil {
				b = append(b, byte(n))
				i += 2
				continue
			}
		}
		b = append(b, s[i])
	}
	return strings.ToValidUTF8(string(b), "�")
}

// starletteResponse is Starlette's Response(content, status, media_type): a text type gets
// its charset, no type stays none.
func starletteResponse(w http.ResponseWriter, status int, contentType string, body []byte) {
	if contentType != "" {
		if strings.HasPrefix(contentType, "text/") && !strings.Contains(strings.ToLower(contentType), "charset=") {
			contentType += "; charset=utf-8"
		}
		w.Header().Set("Content-Type", contentType)
	} else {
		w.Header()["Content-Type"] = nil
	}
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// ── the JSON bodies ──────────────────────────────────────────────────────────

// maxJSONBody is MAX_JSON_BODY_BYTES: the body of these routes is a flow's IR.
const maxJSONBody = 8 * 1024 * 1024

var errTooLarge = errors.New("too large")

// readJSON is _read_json: the cap on the declared and on the real size, then the JSON
// (an empty body is {}); NaN and Infinity are no JSON. The answer already written when ok
// is false.
func readJSON(w http.ResponseWriter, r *http.Request) (raw []byte, payload any, ok bool) {
	tooLarge := func() {
		server.JSON(w, http.StatusRequestEntityTooLarge, map[string]string{"detail": "The request body is too large (limit 8 MB)"})
	}
	if declared, present := r.Header["Content-Length"]; present && len(declared) > 0 {
		// int(declared); an unreadable size is let through
		if n, ok := pythonInt(declared[0]); ok && n > maxJSONBody {
			tooLarge()
			return nil, nil, false
		}
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, maxJSONBody+1))
	if err != nil {
		server.InternalError(w)
		return nil, nil, false
	}
	if len(raw) > maxJSONBody {
		tooLarge()
		return nil, nil, false
	}
	if len(raw) == 0 {
		return raw, pyjson.Object{}, true
	}
	text := raw
	if bytes.HasPrefix(text, []byte("\xef\xbb\xbf")) { // json.loads(bytes) reads it as utf-8-sig
		text = text[3:]
	}
	payload, err = loadsNoConstants(text)
	if err != nil {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "The request body is not valid JSON"})
		return nil, nil, false
	}
	return raw, payload, true
}

// dumps is json.dumps(payload).encode(): Python's defaults.
func dumps(v any) []byte {
	s, _ := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
	return []byte(s)
}

// readChecks are the checks of a preview: sources pinned to the engine's bucket and readable.
func (q *request) readChecks(payload any) bool {
	if q.refused(objects.EnsureReadsPinned(q.ctx, q.c, payload, q.d.Config.EngineBucket)) {
		return false
	}
	return !q.refused(objects.EnsureCanReadKeys(q.ctx, q.c, q.d.DB, &q.p.User, objects.CollectStorageKeys(payload)))
}

// ── the routes ───────────────────────────────────────────────────────────────

// proxyOperations is GET /tasks/operations: metadata only.
func (d *Deps) proxyOperations(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
	d.forward(w, r, http.MethodGet, withQuery("/tasks/operations", parseQSL(r.URL.RawQuery)), nil, false)
}

// proxyEngines is GET /engines: the engine's catalog with the installation's policy.
func (d *Deps) proxyEngines(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
	res, raw, err := d.Engine.JSON(r.Context(), http.MethodGet, "/engines", nil)
	if err != nil {
		d.Log.Error("engine unreachable", "err", err)
		server.InternalError(w)
		return
	}
	if res.StatusCode >= 400 { // an engine error: passed on as it is
		starletteResponse(w, res.StatusCode, res.Header.Get("Content-Type"), raw)
		return
	}
	catalog, err := pyjson.Loads(string(raw))
	if err != nil {
		server.InternalError(w)
		return
	}
	forbidden, err := enginepolicy.Disabled(r.Context(), d.DB)
	if err != nil {
		server.InternalError(w)
		return
	}
	if list, ok := catalog.([]any); ok && len(forbidden) > 0 {
		for i, e := range list {
			if o := pyjson.Obj(e); o != nil {
				if id, ok := o.Value("id").(string); ok && forbidden[id] {
					list[i] = o.Set("available", false).Set("disabled_by_admin", true)
				}
			}
		}
	}
	server.JSON(w, http.StatusOK, pyValue{catalog})
}

// pyValue writes any value pyjson.Loads gave as Starlette's JSONResponse does.
type pyValue struct{ v any }

func (p pyValue) MarshalJSON() ([]byte, error) {
	s, err := pyjson.Dumps(p.v, pyjson.Options{Compact: true})
	return []byte(s), err
}

var slotRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}\n?\z`)

// scopePreviewSlot is scope_preview_slot: the slot prefixed with the user; a malformed one
// removed; without a slot the body passes untouched.
func scopePreviewSlot(raw []byte, payload any, userID int64) []byte {
	o, ok := payload.(pyjson.Object)
	if !ok {
		return raw
	}
	slot, has := o.Get("slot")
	if !has {
		return raw
	}
	scoped := append(pyjson.Object(nil), o...)
	if s, ok := slot.(string); ok && slotRe.MatchString(s) {
		scoped = scoped.Set("slot", fmt.Sprintf("u%d:%s", userID, s))
	} else {
		out := scoped[:0]
		for _, kv := range scoped {
			if kv.Key != "slot" {
				out = append(out, kv)
			}
		}
		scoped = out
	}
	return dumps(scoped)
}

// proxyPreview is POST /tasks/preview.
func (d *Deps) proxyPreview(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	raw, payload, ok := readJSON(w, r)
	if !ok {
		return
	}
	q := d.req(w, r, p)
	if !q.readChecks(payload) {
		return
	}
	body := scopePreviewSlot(raw, payload, p.User.ID)
	d.forward(w, r, http.MethodPost, withQuery("/tasks/preview", parseQSL(r.URL.RawQuery)), body, false)
}

// proxyCacheStatus is POST /tasks/cache-status: checked as a preview.
func (d *Deps) proxyCacheStatus(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	raw, payload, ok := readJSON(w, r)
	if !ok {
		return
	}
	q := d.req(w, r, p)
	if !q.readChecks(payload) {
		return
	}
	d.forward(w, r, http.MethodPost, withQuery("/tasks/cache-status", parseQSL(r.URL.RawQuery)), raw, false)
}

// pyLen is len(x) of a value json.loads gave; false where Python raises a TypeError.
func pyLenOf(v any) (int, bool) {
	switch x := v.(type) {
	case []any:
		return len(x), true
	case pyjson.Object:
		return len(x), true
	case string:
		return utf8.RuneCountInString(x), true
	}
	return 0, false
}

func sortedKeys(set map[string]bool) []any {
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sortStrings(keys)
	out := make([]any, len(keys))
	for i, k := range keys {
		out[i] = k
	}
	return out
}

// labelOf is a value given as an audit row's label: a text, or its str().
func labelOf(v any) *string {
	if v == nil {
		return nil
	}
	s := pyjson.Str(v)
	return &s
}

// proxyTransform is POST /tasks/transform-data: a run without a saved flow. The server
// chooses the output key; destinations go only through /flows/{id}/runs.
func (d *Deps) proxyTransform(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	_, payload, ok := readJSON(w, r)
	if !ok {
		return
	}
	o, isObj := payload.(pyjson.Object)
	if !isObj {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "The request body must be a JSON object"})
		return
	}
	for _, k := range []string{"destination", "db_destination", "mirror", "email"} {
		if pyjson.Truthy(o.Value(k)) {
			server.JSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "Destinations are not allowed here: save the flow and use an Output node"})
			return
		}
	}
	o = o.Set("output_key", "out/"+newHex()+".parquet")
	o = o.Set("bucket", pyjson.Or(o.Value("bucket"), d.Config.EngineBucket))
	q := d.req(w, r, p)
	if q.refused(objects.EnsureReadsPinned(q.ctx, q.c, o, d.Config.EngineBucket)) {
		return
	}
	var read pyjson.Object
	for _, kv := range o {
		if kv.Key != "output_key" {
			read = append(read, kv)
		}
	}
	keys := objects.CollectStorageKeys(read)
	if q.refused(objects.EnsureCanReadKeys(q.ctx, q.c, d.DB, &p.User, keys)) ||
		q.refused(objects.EnsureCanRunKeys(q.ctx, q.c, d.DB, &p.User, keys)) {
		return
	}
	n, ok := pyLenOf(pyjson.Or(o.Value("operations"), []any{}))
	if !ok {
		server.InternalError(w)
		return
	}
	q.audit(audit.Event{Action: "transform.run", TargetType: strp("transform"), TargetLabel: labelOf(o.Value("input_key")),
		Detail: pyjson.Object{{Key: "engine", Value: o.Value("engine")}, {Key: "source_keys", Value: sortedKeys(keys)},
			{Key: "operations", Value: n}}})
	d.forward(w, r, http.MethodPost, withQuery("/tasks/transform-data", parseQSL(r.URL.RawQuery)), dumps(o), false)
}

// authorizeExport is _authorize_export: every source readable, the download audited.
func (q *request) authorizeExport(o pyjson.Object) bool {
	if q.refused(objects.EnsureReadsPinned(q.ctx, q.c, o, q.d.Config.EngineBucket)) {
		return false
	}
	keys := objects.CollectStorageKeys(o)
	if q.refused(objects.EnsureCanReadKeys(q.ctx, q.c, q.d.DB, &q.p.User, keys)) {
		return false
	}
	q.audit(audit.Event{Action: "export.download", TargetType: strp("export"),
		TargetLabel: labelOf(pyjson.Or(o.Value("filename"), o.Value("input_key"))),
		Detail: pyjson.Object{{Key: "format", Value: o.Value("format")}, {Key: "filename", Value: o.Value("filename")},
			{Key: "engine", Value: o.Value("engine")}, {Key: "source_keys", Value: sortedKeys(keys)}}})
	return true
}

// exportOwner is export_owner.
func exportOwner(userID int64) string { return fmt.Sprintf("user:%d", userID) }

func (d *Deps) exportBody(w http.ResponseWriter, r *http.Request, p *auth.Principal) (pyjson.Object, bool) {
	_, payload, ok := readJSON(w, r)
	if !ok {
		return nil, false
	}
	o, isObj := payload.(pyjson.Object)
	if !isObj {
		server.JSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "The request body must be a JSON object"})
		return nil, false
	}
	if !d.req(w, r, p).authorizeExport(o) {
		return nil, false
	}
	return o.Set("owner", exportOwner(p.User.ID)), true
}

// proxyExport is POST /tasks/export: the file comes back in the response.
func (d *Deps) proxyExport(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if o, ok := d.exportBody(w, r, p); ok {
		d.forward(w, r, http.MethodPost, withQuery("/tasks/export", parseQSL(r.URL.RawQuery)), dumps(o), false)
	}
}

// proxyStartExport is POST /tasks/exports: the browser's export, started and followed.
func (d *Deps) proxyStartExport(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	o, ok := d.exportBody(w, r, p)
	if !ok {
		return
	}
	// export_wait: code (an API token) waits its turn, the interface is told at once
	var wait int64
	if p.TokenName != "" {
		wait = d.Config.Flows.ReportsAPIWaitSeconds
	}
	o = o.Set("wait_seconds", wait)
	d.forward(w, r, http.MethodPost, withQuery("/tasks/exports", parseQSL(r.URL.RawQuery)), dumps(o), false)
}

var taskIDRe = regexp.MustCompile(`^[0-9a-f-]{36}\n?\z`)

// proxyExportStatus is GET /tasks/exports/{task_id}: only its owner's.
func (d *Deps) proxyExportStatus(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	id := r.PathValue("task_id")
	if !taskIDRe.MatchString(id) {
		server.JSON(w, http.StatusNotFound, map[string]string{"detail": "Export not found"})
		return
	}
	d.forward(w, r, http.MethodGet, withQuery("/tasks/exports/"+id, []qsPair{{"owner", exportOwner(p.User.ID)}}), nil, false)
}

// proxyExportFile is GET /tasks/exports/{task_id}/file: a JSON answer gzipped for a
// browser that takes it.
func (d *Deps) proxyExportFile(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	id := r.PathValue("task_id")
	if !taskIDRe.MatchString(id) {
		server.JSON(w, http.StatusNotFound, map[string]string{"detail": "Export not found"})
		return
	}
	d.forward(w, r, http.MethodGet, withQuery("/tasks/exports/"+id+"/file", []qsPair{{"owner", exportOwner(p.User.ID)}}), nil, true)
}

// proxyTaskStatus is GET /tasks/{task_id}: a task's state (the ids are UUIDs).
func (d *Deps) proxyTaskStatus(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
	d.forward(w, r, http.MethodGet, withQuery("/tasks/"+r.PathValue("task_id"), parseQSL(r.URL.RawQuery)), nil, false)
}

// describeBytes is config.describe_bytes.
func describeBytes(n int64) string {
	for _, u := range []struct {
		unit string
		size int64
	}{{"GB", 1 << 30}, {"MB", 1 << 20}, {"KB", 1 << 10}} {
		if n >= u.size && n%u.size == 0 {
			return fmt.Sprintf("%d %s", n/u.size, u.unit)
		}
	}
	return fmt.Sprintf("%d bytes", n)
}

// cappedBody is the upload as it arrives, cut at the ceiling.
type cappedBody struct {
	r         io.Reader
	limit     int64
	received  int64
	overtaken bool
}

func (c *cappedBody) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.received += int64(n)
	if c.limit > 0 && c.received > c.limit {
		c.overtaken = true
		return 0, errTooLarge
	}
	return n, err
}

// proxyUpload is POST /files: the upload streamed to the engine, its owner recorded.
func (d *Deps) proxyUpload(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	limit := d.Panel.UploadMaxBytes()
	tooMuch := func() {
		server.JSON(w, http.StatusRequestEntityTooLarge, map[string]string{
			"detail": fmt.Sprintf("The file is too large: the limit is %s (UPLOAD__MAX_BYTES)", describeBytes(limit))})
	}
	declared := r.Header.Get("Content-Length")
	if limit > 0 && isDigits(declared) {
		if n, err := strconv.ParseInt(declared, 10, 64); err != nil || n > limit {
			tooMuch()
			return
		}
	}
	q := d.req(w, r, p)
	can, err := q.c.CanUpload(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if !can {
		q.refuse(http.StatusForbidden, "Uploading a file needs edit permission on at least one folder")
		return
	}
	body := &cappedBody{r: r.Body, limit: limit}
	req, err := d.engineRequest(r, http.MethodPost, withQuery("/files", parseQSL(r.URL.RawQuery)), body)
	if err != nil {
		q.fail(err)
		return
	}
	res, err := d.Engine.DoRaw(req)
	if err != nil {
		if body.overtaken {
			tooMuch()
			return
		}
		q.fail(err)
		return
	}
	raw, err := io.ReadAll(res.Body)
	res.Body.Close()
	if err != nil {
		q.fail(err)
		return
	}
	if res.StatusCode < 400 {
		d.recordUpload(q, raw)
	}
	starletteResponse(w, res.StatusCode, res.Header.Get("Content-Type"), raw)
}

// recordUpload writes the uploads register's row; a failure is logged, never answered.
func (d *Deps) recordUpload(q *request, raw []byte) {
	v, err := pyjson.Loads(string(raw))
	o, isObj := v.(pyjson.Object)
	if err != nil || !isObj {
		d.Log.Warn("upload not recorded in the uploads register", "err", err)
		return
	}
	field := func(k string) string { return pyjson.Str(pyjson.Or(o.Value(k), "")) }
	if _, err := d.DB.Exec(q.ctx, `INSERT INTO uploads (dataset_id, bucket, parquet_key, raw_key, filename, owner_id, created_at)
		VALUES ($1, $2, $3, $4, '', $5, now() at time zone 'utc')`, field("dataset_id"), d.Config.EngineBucket, field("parquet_key"),
		field("raw_key"), q.p.User.ID); err != nil {
		d.Log.Warn("upload not recorded in the uploads register", "err", err)
	}
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// newHex is uuid4().hex.
func newHex() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return hex.EncodeToString(b)
}

func sortStrings(s []string) { sort.Strings(s) }

// pythonInt is int(s) of a text: blanks around, a sign, digits with single underscores
// between them; a value past an int64 reads as the largest.
func pythonInt(s string) (int64, bool) {
	s = pyStrip(s)
	neg := strings.HasPrefix(s, "-")
	if neg || strings.HasPrefix(s, "+") {
		s = s[1:]
	}
	if s == "" || s[0] == '_' || s[len(s)-1] == '_' || strings.Contains(s, "__") {
		return 0, false
	}
	var n int64
	for _, c := range strings.ReplaceAll(s, "_", "") {
		if c < '0' || c > '9' {
			return 0, false
		}
		if n > (1<<63-1)/10 {
			n = 1<<63 - 1
			continue
		}
		n = n*10 + int64(c-'0')
	}
	if neg {
		n = -n
	}
	return n, true
}

// loadsNoConstants is json.loads(raw, parse_constant=…raise): bytes that are no UTF-8 refused
// as bytes.decode refuses them, and NaN or Infinity, which can only stand outside a string
// (the other words of JSON are lowercase), refused before reading.
func loadsNoConstants(raw []byte) (any, error) {
	if !utf8.Valid(raw) {
		return nil, errors.New("the text is not UTF-8")
	}
	in := false
	for i := 0; i < len(raw); i++ {
		switch c := raw[i]; {
		case in && c == '\\':
			i++
		case c == '"':
			in = !in
		case !in && (c == 'N' || c == 'I'):
			return nil, errors.New("not a JSON value")
		}
	}
	return pyjson.Loads(string(raw))
}
