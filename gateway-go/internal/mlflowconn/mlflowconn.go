// Package mlflowconn is the `mlflow` kind of connection: a tracking server outside Tabularia
// (docs/specs/statistical-models.md, «MLflow»). It reads the kind's options (`extra`), checks
// them, and runs the test of the connection dialog: a few calls that only READ, with the
// connection's credentials.
//
// Endpoints (MLflow's REST reference, https://mlflow.org/docs/latest/api_reference/rest-api.html;
// the REST API is served under /api and its version is 2.0):
//
//	POST /api/2.0/mlflow/experiments/search          {"max_results": 1}   the credentials work
//	GET  /api/2.0/mlflow/registered-models/search?max_results=1            the registry exists
//	GET  /api/2.0/mlflow-artifacts/artifacts                              the artifact proxy exists
//	GET  /version                                                         the server's version (not in
//	                                                                       the REST reference: best effort)
//
// The test never returns what the server wrote: only a code per check (the interface says it
// in the person's language), the HTTP status and the version text (short, plain). The
// credentials go to the tracking server's origin and nowhere else: a redirect to another
// origin is not followed.
package mlflowconn

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtls"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Auths are the authentication methods (`extra.auth`).
var Auths = []string{"none", "basic", "bearer"}

// Options are the kind's options, as read from `extra` (tolerant: a broken one gives the
// defaults).
type Options struct {
	Auth      string
	VerifyTLS bool
	CACert    string
	UIURL     string
}

// ParseOptions reads `extra`.
func ParseOptions(extra string) Options {
	o := Options{Auth: "none", VerifyTLS: true}
	if strings.TrimSpace(extra) == "" {
		return o
	}
	var raw map[string]any
	if json.Unmarshal([]byte(extra), &raw) != nil {
		return o
	}
	if a, ok := raw["auth"].(string); ok && in(a, Auths) {
		o.Auth = a
	}
	if v, ok := raw["verify_tls"].(bool); ok {
		o.VerifyTLS = v
	}
	if s, ok := raw["ca_cert"].(string); ok && o.VerifyTLS {
		o.CACert = strings.TrimSpace(s)
	}
	if s, ok := raw["ui_url"].(string); ok {
		o.UIURL = strings.TrimSpace(s)
	}
	return o
}

func in(v string, list []string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// OptionError is what is wrong with the options ("" = nothing). The address of the interface
// (`ui_url`) is checked by the caller, which owns the address checks.
func OptionError(data pyjson.Object) string {
	if a, has := data.Get("auth"); has && a != nil && a != "" {
		if s, ok := a.(string); !ok || !in(s, Auths) {
			return "auth must be none, basic or bearer"
		}
	}
	verify := true
	if v, has := data.Get("verify_tls"); has && v != nil {
		b, ok := v.(bool)
		if !ok {
			return "verify_tls must be true or false"
		}
		verify = b
	}
	if ca := data.Value("ca_cert"); ca != nil && ca != "" {
		text, ok := ca.(string)
		if !ok {
			return "The certificate authority must be text (PEM)"
		}
		if !verify {
			return "The certificate authority is used only to verify: turn the verification on, or leave it empty"
		}
		if e := dbtls.CAError(text); e != "" {
			return e
		}
	}
	if u := data.Value("ui_url"); u != nil {
		if _, ok := u.(string); !ok {
			return "ui_url must be text"
		}
	}
	return ""
}

// ── the test ─────────────────────────────────────────────────────────────────

// Config is what a test needs: the credentials in clear, from the form or decrypted.
type Config struct {
	URI       string // the tracking URI
	Auth      string // none | basic | bearer
	User      string
	Secret    string
	VerifyTLS bool
	CACert    string
	Timeout   time.Duration // per call; 0 = 10 s
}

// Check is the outcome of one call.
type Check struct {
	ID     string `json:"id"`               // version | experiments | registry | artifacts
	OK     bool   `json:"ok"`               // the call answered as expected
	Code   string `json:"code"`             // ok | unreachable | timeout | tls_untrusted | tls_hostname | tls_other | unauthorized | forbidden | not_found | redirect | http_error | bad_answer
	Status int    `json:"status,omitempty"` // the HTTP status, when there was one
	Detail string `json:"detail,omitempty"` // the version; or the origin a redirect points to
}

// Result is the answer of a test. OK: the credentials work (the experiments check passed);
// the registry and the artifact proxy are told apart in Checks.
type Result struct {
	OK        bool    `json:"ok"`
	Version   string  `json:"version"`
	Registry  bool    `json:"registry"`  // registered models can be created
	Artifacts bool    `json:"artifacts"` // files can be uploaded through the server
	Checks    []Check `json:"checks"`
}

// Test runs the calls. It always returns a Result: a failure is a Check with its code.
func Test(ctx context.Context, c Config) Result {
	client := newClient(c)
	base := strings.TrimRight(strings.TrimSpace(c.URI), "/")
	res := Result{Checks: []Check{}}

	// the experiments' search first: it decides whether the credentials work
	exp := call(ctx, client, c, http.MethodPost, base+"/api/2.0/mlflow/experiments/search", []byte(`{"max_results": 1}`), "experiments")
	if exp.OK {
		// an answer that is not JSON with an `experiments` list is not an MLflow server
		if !hasKey(exp.body, "experiments") {
			exp.Check.OK, exp.Check.Code = false, "bad_answer"
		}
	}
	res.Checks = append(res.Checks, exp.Check)
	if !exp.Check.OK {
		return res
	}
	res.OK = true

	ver := call(ctx, client, c, http.MethodGet, base+"/version", nil, "version")
	if ver.OK {
		v := strings.TrimSpace(string(ver.body))
		if len(v) > 40 || !versionText.MatchString(v) {
			ver.Check.OK, ver.Check.Code = false, "bad_answer"
		} else {
			ver.Check.Detail, res.Version = v, v
		}
	}
	res.Checks = append(res.Checks, ver.Check)

	reg := call(ctx, client, c, http.MethodGet, base+"/api/2.0/mlflow/registered-models/search?max_results=1", nil, "registry")
	res.Registry = reg.OK
	res.Checks = append(res.Checks, reg.Check)

	art := call(ctx, client, c, http.MethodGet, base+"/api/2.0/mlflow-artifacts/artifacts", nil, "artifacts")
	res.Artifacts = art.OK
	res.Checks = append(res.Checks, art.Check)
	return res
}

var versionText = regexp.MustCompile(`^[0-9A-Za-z][0-9A-Za-z.+_-]*$`)

type outcome struct {
	Check
	body []byte
}

func hasKey(body []byte, key string) bool {
	var m map[string]json.RawMessage
	if json.Unmarshal(body, &m) != nil {
		return false
	}
	_, ok := m[key]
	return ok
}

// origin is scheme://host:port with the default port made explicit.
func origin(u *url.URL) string {
	port := u.Port()
	if port == "" {
		port = "80"
		if u.Scheme == "https" {
			port = "443"
		}
	}
	return strings.ToLower(u.Scheme) + "://" + strings.ToLower(u.Hostname()) + ":" + port
}

var errCrossOrigin = errors.New("redirect to another origin")

func newClient(c Config) *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	cfg := &tls.Config{MinVersion: tls.VersionTLS12}
	switch {
	case !c.VerifyTLS:
		cfg.InsecureSkipVerify = true //nolint:gosec — the person's choice, recorded in the connection and the audit
	case strings.TrimSpace(c.CACert) != "":
		pool, err := x509.SystemCertPool()
		if err != nil || pool == nil {
			pool = x509.NewCertPool()
		}
		pool.AppendCertsFromPEM([]byte(c.CACert))
		cfg.RootCAs = pool
	}
	t.TLSClientConfig = cfg
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &http.Client{Transport: t, Timeout: timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			// credentials stay on the tracking server's origin
			if len(via) >= 3 || origin(req.URL) != origin(via[0].URL) {
				return errCrossOrigin
			}
			return nil
		}}
}

// call makes one request and classifies the answer. It never keeps more than 64 KiB of it.
func call(ctx context.Context, client *http.Client, c Config, method, target string, body []byte, id string) outcome {
	out := outcome{Check: Check{ID: id}}
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, target, rd)
	if err != nil {
		out.Code = "unreachable"
		return out
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	switch c.Auth {
	case "basic":
		req.SetBasicAuth(c.User, c.Secret)
	case "bearer":
		req.Header.Set("Authorization", "Bearer "+c.Secret)
	}
	resp, err := client.Do(req)
	if err != nil {
		out.Code, out.Detail = classify(err)
		return out
	}
	defer resp.Body.Close()
	out.Status = resp.StatusCode
	out.body, _ = io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		out.OK, out.Code = true, "ok"
	case resp.StatusCode >= 300 && resp.StatusCode < 400:
		out.Code = "redirect"
		if loc, err := resp.Location(); err == nil {
			out.Detail = origin(loc)
		}
	case resp.StatusCode == http.StatusUnauthorized:
		out.Code = "unauthorized"
	case resp.StatusCode == http.StatusForbidden:
		out.Code = "forbidden"
	case resp.StatusCode == http.StatusNotFound:
		out.Code = "not_found"
	default:
		out.Code = "http_error"
	}
	out.body = scrub(out.body, c.Secret)
	return out
}

// scrub drops the answer when it echoes the secret (a body is never shown, but never kept
// with it either).
func scrub(b []byte, secret string) []byte {
	if secret != "" && bytes.Contains(b, []byte(secret)) {
		return nil
	}
	return b
}

// classify names a transport error. The detail is an origin or empty: never the error's
// text, which can hold the address, a path or a header.
func classify(err error) (code, detail string) {
	if errors.Is(err, errCrossOrigin) {
		return "redirect", ""
	}
	var unknown x509.UnknownAuthorityError
	var host x509.HostnameError
	var invalid x509.CertificateInvalidError
	var verify *tls.CertificateVerificationError
	switch {
	case errors.As(err, &host):
		return "tls_hostname", ""
	case errors.As(err, &unknown):
		return "tls_untrusted", ""
	case errors.As(err, &invalid), errors.As(err, &verify):
		return "tls_other", ""
	}
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
		return "timeout", ""
	}
	if strings.Contains(err.Error(), "tls:") || strings.Contains(err.Error(), "x509:") {
		return "tls_other", ""
	}
	return "unreachable", ""
}

// String is a one-line reading of a Result, for logs and the audit: no secret in it.
func (r Result) String() string {
	parts := make([]string, 0, len(r.Checks))
	for _, c := range r.Checks {
		parts = append(parts, fmt.Sprintf("%s=%s", c.ID, c.Code))
	}
	return strings.Join(parts, " ")
}
