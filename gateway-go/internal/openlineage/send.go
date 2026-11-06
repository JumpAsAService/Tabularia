package openlineage

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// ErrNotSent is an event the collector did not take, worded as the client's requests/urllib3
// stack words it where the words are known: a 4xx or 5xx answer («400 Client Error: Bad
// Request for url: …»), the retries spent on the statuses retried. A refused connection is
// worded by urllib3 with an object's memory address: here without it.
type ErrNotSent struct{ Message string }

func (e *ErrNotSent) Error() string { return e.Message }

// Emit is _emit with a client built now (openlineage._get_client): each event through the
// client, in order; how many.
func (s Settings) Emit(ctx context.Context, events []pyjson.Object) (int, error) {
	c, err := s.newClient(readEnviron(), slog.Default())
	if err != nil {
		return 0, err
	}
	return c.emitAll(ctx, events)
}

// ── the HTTP transport (HttpConfig, HttpTransport) ──────────────────────────────────────────

type retryCfg struct {
	total, connect, read, status *int
	backoff, backoffMax          float64
	forcelist                    map[int]bool
	post                         bool // POST among allowed_methods (or every method)
	raiseOnStatus, retryAfter    bool
}

func (r retryCfg) totalOr(def int) int {
	if r.total == nil {
		return def
	}
	return *r.total
}

var defaultRetry = pyjson.Object{{Key: "total", Value: int64(5)}, {Key: "read", Value: int64(5)}, {Key: "connect", Value: int64(5)},
	{Key: "backoff_factor", Value: 0.3}, {Key: "status_forcelist", Value: []any{int64(500), int64(502), int64(503), int64(504)}},
	{Key: "allowed_methods", Value: []any{"HEAD", "POST"}}}

func intOrNone(v any) *int {
	switch x := v.(type) {
	case int64:
		n := int(x)
		return &n
	case float64:
		n := int(x)
		return &n
	case bool:
		n := 0
		if x {
			n = 1
		}
		return &n
	}
	return nil
}

func numberOf(v any, def float64) float64 {
	switch x := v.(type) {
	case int64:
		return float64(x)
	case float64:
		return x
	case bool:
		if x {
			return 1
		}
		return 0
	}
	return def
}

// retryFrom is urllib3.util.Retry(**retry).
func retryFrom(o pyjson.Object) retryCfg {
	r := retryCfg{total: intOrNone(o.Value("total")), connect: intOrNone(o.Value("connect")), read: intOrNone(o.Value("read")),
		status: intOrNone(o.Value("status")), backoff: numberOf(o.Value("backoff_factor"), 0), backoffMax: numberOf(o.Value("backoff_max"), 120),
		forcelist: map[int]bool{}, post: true, raiseOnStatus: true, retryAfter: true}
	if _, ok := o.Get("total"); !ok {
		ten := 10
		r.total = &ten
	}
	if l, ok := o.Value("status_forcelist").([]any); ok {
		for _, s := range l {
			if n := intOrNone(s); n != nil {
				r.forcelist[*n] = true
			}
		}
	}
	if am, ok := o.Get("allowed_methods"); ok && am != nil && am != false {
		r.post = false
		if l, ok := am.([]any); ok {
			for _, m := range l {
				r.post = r.post || strings.ToUpper(pyjson.Str(m)) == "POST"
			}
		}
	}
	if v, ok := o.Get("raise_on_status"); ok {
		r.raiseOnStatus = pyjson.Truthy(v)
	}
	if v, ok := o.Get("respect_retry_after_header"); ok {
		r.retryAfter = pyjson.Truthy(v)
	}
	return r
}

type tokenProvider interface {
	bearer(ctx context.Context) (string, error)
}

type noToken struct{}

func (noToken) bearer(context.Context) (string, error) { return "", nil }

type apiKeyToken struct{ key string }

func (t apiKeyToken) bearer(context.Context) (string, error) { return "Bearer " + t.key, nil }

// jwtToken is JwtTokenProvider: an API key exchanged for a token at an endpoint, kept until
// shortly before it expires.
type jwtToken struct {
	apiKey, endpoint, expiresField, grant, response string
	fields                                          []string
	buffer                                          float64
	mu                                              sync.Mutex
	cached                                          string
	expiry                                          float64
}

func (t *jwtToken) bearer(ctx context.Context) (string, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := float64(time.Now().UnixNano()) / 1e9
	if t.cached != "" && t.expiry != 0 && now < t.expiry-t.buffer {
		return "Bearer " + t.cached, nil
	}
	form := url.Values{"grant_type": {t.grant}, "response_type": {t.response}, "apikey": {t.apiKey}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", perr("Failed to fetch JWT token from %s: %v", t.endpoint, err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return "", perr("Failed to fetch JWT token from %s: %v", t.endpoint, err)
	}
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode >= 400 {
		return "", perr("Failed to fetch JWT token from %s: %s", t.endpoint, statusError(res, t.endpoint))
	}
	v, err := pyjson.Loads(string(raw))
	o, ok := v.(pyjson.Object)
	if err != nil || !ok {
		return "", perr("Failed to fetch JWT token from %s: the answer is not a JSON object", t.endpoint)
	}
	token := ""
	found := false
	for _, f := range t.fields {
		for _, kv := range o {
			if kv.Key == f || strings.EqualFold(kv.Key, f) {
				if kv.Value != nil {
					token = pyjson.Str(kv.Value)
				}
				found = true
				break
			}
		}
		if found {
			break
		}
	}
	if token == "" {
		return "", perr("Failed to fetch JWT token from %s: Token not found in response. Tried fields: %s", t.endpoint, pyjson.Repr(stringsAny(t.fields)))
	}
	t.cached, t.expiry = token, 0
	for _, kv := range o {
		k := strings.ToLower(kv.Key)
		f := strings.ToLower(t.expiresField)
		if kv.Key == t.expiresField || k == f || strings.ReplaceAll(k, "_", "") == strings.ReplaceAll(f, "_", "") {
			t.expiry = now + numberOf(kv.Value, 0)
			break
		}
	}
	if t.expiry == 0 {
		if parts := strings.Split(token, "."); len(parts) == 3 {
			if payload, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "=")); err == nil {
				var claims map[string]any
				if json.Unmarshal(payload, &claims) == nil {
					if exp, ok := claims["exp"].(float64); ok {
						t.expiry = exp
					}
				}
			}
		}
	}
	return "Bearer " + token, nil
}

func stringsAny(s []string) []any {
	out := make([]any, len(s))
	for i, x := range s {
		out[i] = x
	}
	return out
}

func firstOf(o pyjson.Object, keys ...string) any {
	for _, k := range keys {
		if v := o.Value(k); pyjson.Truthy(v) {
			return v
		}
	}
	return nil
}

// tokenProviderFrom is create_token_provider.
func tokenProviderFrom(auth any) (tokenProvider, error) {
	o, _ := auth.(pyjson.Object)
	typ, has := o.Get("type")
	if !has {
		return noToken{}, nil
	}
	switch typ {
	case "api_key", "openlineage.client.transport.http.ApiKeyTokenProvider":
		k := firstOf(o, "apiKey", "apikey", "api_key")
		if k == nil {
			return nil, keyError("apiKey is required for HTTP Transport when auth type is `api_key`.")
		}
		return apiKeyToken{pyjson.Str(k)}, nil
	case "jwt", "openlineage.client.transport.http.JwtTokenProvider":
		k := firstOf(o, "apiKey", "apikey", "api_key")
		if k == nil {
			return nil, keyError("apiKey is required for JWT token provider.")
		}
		ep := firstOf(o, "token_endpoint", "tokenEndpoint")
		if ep == nil {
			return nil, keyError("tokenEndpoint is required for JWT token provider.")
		}
		t := &jwtToken{apiKey: pyjson.Str(k), endpoint: pyjson.Str(ep), fields: []string{"token", "access_token"}, buffer: 120,
			expiresField: "expires_in", grant: "urn:ietf:params:oauth:grant-type:jwt-bearer", response: "token"}
		if f := firstOf(o, "tokenFields", "token_fields"); f != nil {
			if l, ok := f.([]any); ok {
				t.fields = nil
				for _, x := range l {
					t.fields = append(t.fields, pyjson.Str(x))
				}
			} else {
				t.fields = []string{pyjson.Str(f)}
			}
		}
		if v := firstOf(o, "expiresInField", "expires_in_field"); v != nil {
			t.expiresField = pyjson.Str(v)
		}
		if v := firstOf(o, "grantType", "grant_type"); v != nil {
			t.grant = pyjson.Str(v)
		}
		if v := firstOf(o, "responseType", "response_type"); v != nil {
			t.response = pyjson.Str(v)
		}
		if v := firstOf(o, "tokenRefreshBuffer", "token_refresh_buffer"); v != nil {
			t.buffer = numberOf(v, 120)
		}
		return t, nil
	case "openlineage.client.transport.http.TokenProvider":
		return noToken{}, nil
	}
	return nil, perr("Failed to import %s", pyjson.Str(typ))
}

type httpConfig struct {
	url, endpoint string
	timeout       any
	verify        bool
	auth          tokenProvider
	gzip          bool
	headers       pyjson.Object
	retry         retryCfg
	maxQueue      int
}

// httpConfigFrom is HttpConfig.from_dict (AsyncHttpConfig.from_dict: the same, with its queue).
func httpConfigFrom(params pyjson.Object, class string, _ environ) (*httpConfig, error) {
	u, ok := params.Get("url")
	if !ok {
		return nil, perr("`url` key not passed to %s", class)
	}
	c := &httpConfig{url: strings.TrimSpace(pyjson.Str(u)), endpoint: "api/v1/lineage", timeout: 5.0, verify: true, maxQueue: 10000}
	if v, ok := params.Get("endpoint"); ok {
		c.endpoint = pyjson.Str(v)
	}
	if v, ok := params.Get("timeout"); ok {
		c.timeout = v
	}
	if v, ok := params.Get("verify"); ok {
		c.verify = pyjson.Truthy(v)
	}
	auth, err := tokenProviderFrom(params.Value("auth"))
	if err != nil {
		return nil, err
	}
	c.auth = auth
	if v := params.Value("compression"); pyjson.Truthy(v) {
		if v != "gzip" {
			return nil, perr("%s is not a valid HttpCompression", pyjson.Repr(v))
		}
		c.gzip = true
	}
	if h, ok := params.Value("custom_headers").(pyjson.Object); ok {
		c.headers = h
	}
	retry := defaultRetry
	if v, ok := params.Get("retry"); ok {
		if o, isDict := v.(pyjson.Object); isDict {
			retry = mergeFlat(defaultRetry, o)
		}
	}
	c.retry = retryFrom(retry)
	if v := intOrNone(params.Value("max_queue_size")); v != nil {
		c.maxQueue = *v
	}
	return c, nil
}

func mergeFlat(a, b pyjson.Object) pyjson.Object {
	out := append(pyjson.Object{}, a...)
	for _, kv := range b {
		out = out.Set(kv.Key, kv.Value)
	}
	return out
}

// validURL is HttpTransport's (urllib3.parse_url) and AsyncHttpTransport's (httpx.URL) check:
// a scheme and a host.
func (c *httpConfig) validURL(async bool) error {
	u, err := urlsplit(c.url)
	if err == nil && u.scheme != "" && u.hostname != "" {
		return nil
	}
	if async {
		return perr("Need valid url for OpenLineageClient, passed %s. Exception: Need valid url for OpenLineageClient, passed %s", c.url, c.url)
	}
	if err != nil {
		return perr("Need valid url for OpenLineageClient, passed %s. Exception: %v", c.url, err)
	}
	return perr("Need valid url for OpenLineageClient, passed %s", c.url)
}

// target is urljoin(url, endpoint).
func (c *httpConfig) target() string {
	base, err := url.Parse(c.url)
	if err != nil {
		return c.url
	}
	ref, err := url.Parse(c.endpoint)
	if err != nil {
		return c.url
	}
	return base.ResolveReference(ref).String()
}

func (c *httpConfig) client() (*http.Client, error) {
	timeout := 0.0
	switch t := c.timeout.(type) {
	case nil:
	case int64, float64:
		timeout = numberOf(t, 5)
	default:
		return nil, perr("Timeout value connect was %s, but it must be an int, float or None.", pyjson.Str(t))
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if !c.verify {
		tr.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // verify: false, as the configuration asks
	}
	return &http.Client{Timeout: time.Duration(timeout * float64(time.Second)), Transport: tr}, nil
}

func (c *httpConfig) request(ctx context.Context, body string) (*http.Request, error) {
	var reader io.Reader = strings.NewReader(body)
	if c.gzip {
		var buf bytes.Buffer
		zw, _ := gzip.NewWriterLevel(&buf, 3)
		zw.Write([]byte(body))
		zw.Close()
		reader = &buf
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.target(), reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	bearer, err := c.auth.bearer(ctx)
	if err != nil {
		return nil, err
	}
	if bearer != "" {
		req.Header.Set("Authorization", bearer)
	}
	for _, kv := range c.headers {
		req.Header.Set(kv.Key, pyjson.Str(kv.Value))
	}
	if c.gzip {
		req.Header.Set("Content-Encoding", "gzip")
	}
	return req, nil
}

// postOnce is one POST, without retries: the status, or the error.
func (c *httpConfig) postOnce(ctx context.Context, body string) (int, error) {
	client, err := c.client()
	if err != nil {
		return 0, err
	}
	req, err := c.request(ctx, body)
	if err != nil {
		return 0, err
	}
	res, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	io.Copy(io.Discard, res.Body)
	res.Body.Close()
	return res.StatusCode, nil
}

func statusError(res *http.Response, target string) string {
	kind := "Client"
	if res.StatusCode >= 500 {
		kind = "Server"
	}
	reason := strings.TrimSpace(strings.TrimPrefix(res.Status, strconv.Itoa(res.StatusCode)))
	return fmt.Sprintf("%d %s Error: %s for url: %s", res.StatusCode, kind, reason, target)
}

type httpTransport struct {
	transportMeta
	cfg *httpConfig
	log *slog.Logger
}

func (*httpTransport) class() string { return "HttpTransport" }
func (*httpTransport) kind() string  { return "http" }

// emit is HttpTransport.emit through requests and urllib3's Retry: the connection's errors,
// the read's and the statuses retried as the counters allow, with their backoff.
func (t *httpTransport) emit(ctx context.Context, ev pyjson.Object) error {
	c := t.cfg
	body, err := pyjson.Dumps(ev, pyjson.Options{SortKeys: true, EnsureASCII: true})
	if err != nil {
		return err
	}
	client, err := c.client()
	if err != nil {
		return err
	}
	target := c.target()
	u, _ := url.Parse(target)
	port := u.Port()
	if port == "" {
		port = map[string]string{"https": "443"}[u.Scheme]
		if port == "" {
			port = "80"
		}
	}
	pool := "HTTPConnectionPool"
	if u.Scheme == "https" {
		pool = "HTTPSConnectionPool"
	}
	where := fmt.Sprintf("%s(host='%s', port=%s): Max retries exceeded with url: %s", pool, u.Hostname(), port, u.RequestURI())
	r := c.retry
	total, connect, read, status := copyInt(r.total), copyInt(r.connect), copyInt(r.read), copyInt(r.status)
	exhausted := func() bool {
		for _, n := range []*int{total, connect, read, status} {
			if n != nil && *n < 0 {
				return true
			}
		}
		return false
	}
	failures := 0
	sleep := func(retryAfter time.Duration) {
		if retryAfter > 0 {
			time.Sleep(retryAfter)
			return
		}
		if failures <= 1 {
			return
		}
		d := math.Min(r.backoffMax, r.backoff*math.Pow(2, float64(failures-1)))
		time.Sleep(time.Duration(d * float64(time.Second)))
	}
	for {
		req, err := c.request(ctx, body)
		if err != nil {
			return err
		}
		res, err := client.Do(req)
		if err != nil {
			var op *net.OpError
			isConnect := errors.As(err, &op) && op.Op == "dial"
			if !isConnect && !r.post {
				return &ErrNotSent{fmt.Sprintf("('Connection aborted.', %v)", err)}
			}
			dec(total)
			if isConnect {
				dec(connect)
			} else {
				dec(read)
			}
			failures++
			if exhausted() {
				var ne net.Error
				if errors.As(err, &ne) && ne.Timeout() {
					return &ErrNotSent{fmt.Sprintf("%s (Caused by ReadTimeoutError(\"%s\"))", where, "Read timed out.")}
				}
				return &ErrNotSent{fmt.Sprintf("%s (Caused by NewConnectionError('Failed to establish a new connection: %s'))", where, connError(err))}
			}
			sleep(0)
			continue
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
		if r.post && r.forcelist[res.StatusCode] {
			dec(total)
			dec(status)
			failures++
			if !exhausted() {
				var after time.Duration
				if r.retryAfter && (res.StatusCode == 413 || res.StatusCode == 429 || res.StatusCode == 503) {
					if n, err := strconv.Atoi(strings.TrimSpace(res.Header.Get("Retry-After"))); err == nil && n > 0 {
						after = time.Duration(n) * time.Second
					}
				}
				sleep(after)
				continue
			}
			if r.raiseOnStatus {
				return &ErrNotSent{fmt.Sprintf("%s (Caused by ResponseError('too many %d error responses'))", where, res.StatusCode)}
			}
		}
		if res.StatusCode >= 400 {
			return &ErrNotSent{statusError(res, target)}
		}
		return nil
	}
}

func copyInt(p *int) *int {
	if p == nil {
		return nil
	}
	n := *p
	return &n
}

func dec(p *int) {
	if p != nil {
		*p--
	}
}

// connError words a refused connection as urllib3 does, but for the object's address.
func connError(err error) string {
	if n, ok := errnoOf(err); ok {
		msg := syscall.Errno(n).Error()
		return fmt.Sprintf("[Errno %d] %s", n, strings.ToUpper(msg[:1])+msg[1:])
	}
	return err.Error()
}

// errnoOf is the system's error number under an error.
func errnoOf(err error) (int, bool) {
	var e syscall.Errno
	if errors.As(err, &e) {
		return int(e), true
	}
	return 0, false
}

func sortStrings(s []string) { sort.Strings(s) }
