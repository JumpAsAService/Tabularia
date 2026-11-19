package mlflowconn

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// fakeMLflow is an MLflow tracking server that only answers what the test asks.
func fakeMLflow(t *testing.T, check func(r *http.Request) int, registry, artifacts bool) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if check != nil {
			if code := check(r); code != 0 {
				w.WriteHeader(code)
				_, _ = w.Write([]byte(`{"error_code":"X","message":"secret-in-body-should-never-show"}`))
				return
			}
		}
		switch {
		case r.URL.Path == "/version":
			_, _ = w.Write([]byte("2.19.0"))
		case r.URL.Path == "/api/2.0/mlflow/experiments/search" && r.Method == http.MethodPost:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["max_results"] != float64(1) {
				w.WriteHeader(400)
				return
			}
			_, _ = w.Write([]byte(`{"experiments": []}`))
		case r.URL.Path == "/api/2.0/mlflow/registered-models/search" && r.Method == http.MethodGet && registry:
			_, _ = w.Write([]byte(`{"registered_models": []}`))
		case r.URL.Path == "/api/2.0/mlflow-artifacts/artifacts" && r.Method == http.MethodGet && artifacts:
			_, _ = w.Write([]byte(`{"files": []}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func codes(r Result) map[string]string {
	m := map[string]string{}
	for _, c := range r.Checks {
		m[c.ID] = c.Code
	}
	return m
}

func TestSuccessNoAuth(t *testing.T) {
	var writes atomic.Int32
	srv := fakeMLflow(t, func(r *http.Request) int {
		if r.Method != http.MethodGet && r.URL.Path != "/api/2.0/mlflow/experiments/search" {
			writes.Add(1)
		}
		return 0
	}, true, true)
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL + "/", Auth: "none", VerifyTLS: true})
	if !res.OK || !res.Registry || !res.Artifacts || res.Version != "2.19.0" {
		t.Fatalf("%+v", res)
	}
	if writes.Load() != 0 {
		t.Fatal("the test wrote")
	}
}

func TestNoRegistryNoArtifactProxy(t *testing.T) {
	srv := fakeMLflow(t, nil, false, false)
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL, Auth: "none", VerifyTLS: true})
	c := codes(res)
	if !res.OK || res.Registry || res.Artifacts || c["registry"] != "not_found" || c["artifacts"] != "not_found" {
		t.Fatalf("%+v", res)
	}
}

func TestBasicAndBearerSendTheRightHeader(t *testing.T) {
	var seen atomic.Value
	srv := fakeMLflow(t, func(r *http.Request) int { seen.Store(r.Header.Get("Authorization")); return 0 }, true, true)
	defer srv.Close()
	Test(context.Background(), Config{URI: srv.URL, Auth: "basic", User: "ann", Secret: "pw", VerifyTLS: true})
	if got := seen.Load().(string); got != "Basic YW5uOnB3" {
		t.Fatalf("basic: %q", got)
	}
	Test(context.Background(), Config{URI: srv.URL, Auth: "bearer", Secret: "tok", VerifyTLS: true})
	if got := seen.Load().(string); got != "Bearer tok" {
		t.Fatalf("bearer: %q", got)
	}
	Test(context.Background(), Config{URI: srv.URL, Auth: "none", Secret: "ignored", VerifyTLS: true})
	if got := seen.Load().(string); got != "" {
		t.Fatalf("none sent %q", got)
	}
}

func TestWrongCredentials(t *testing.T) {
	srv := fakeMLflow(t, func(r *http.Request) int {
		if _, pw, ok := r.BasicAuth(); !ok || pw != "right" {
			return http.StatusUnauthorized
		}
		return 0
	}, true, true)
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL, Auth: "basic", User: "ann", Secret: "wrong", VerifyTLS: true})
	if res.OK || len(res.Checks) != 1 || res.Checks[0].Code != "unauthorized" || res.Checks[0].Status != 401 {
		t.Fatalf("%+v", res)
	}
	ok := Test(context.Background(), Config{URI: srv.URL, Auth: "basic", User: "ann", Secret: "right", VerifyTLS: true})
	if !ok.OK {
		t.Fatalf("%+v", ok)
	}
	forbidden := fakeMLflow(t, func(*http.Request) int { return 403 }, true, true)
	defer forbidden.Close()
	if c := codes(Test(context.Background(), Config{URI: forbidden.URL, VerifyTLS: true})); c["experiments"] != "forbidden" {
		t.Fatal(c)
	}
}

func TestNeverReturnsTheServersText(t *testing.T) {
	srv := fakeMLflow(t, func(*http.Request) int { return 500 }, true, true)
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL, Auth: "bearer", Secret: "tok-123", VerifyTLS: true})
	out, _ := json.Marshal(res)
	if res.OK || strings.Contains(string(out), "secret-in-body") || strings.Contains(string(out), "tok-123") || res.Checks[0].Code != "http_error" {
		t.Fatalf("%s", out)
	}
}

func TestNotMLflow(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<html>hi</html>")) }))
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL, VerifyTLS: true})
	if res.OK || res.Checks[0].Code != "bad_answer" {
		t.Fatalf("%+v", res)
	}
}

func TestUnreachable(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	res := Test(context.Background(), Config{URI: url, VerifyTLS: true})
	if res.OK || res.Checks[0].Code != "unreachable" {
		t.Fatalf("%+v", res)
	}
}

func TestTimeout(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer srv.Close()
	defer close(release)
	start := time.Now()
	res := Test(context.Background(), Config{URI: srv.URL, VerifyTLS: true, Timeout: 150 * time.Millisecond})
	if res.OK || res.Checks[0].Code != "timeout" || time.Since(start) > 3*time.Second {
		t.Fatalf("%+v", res)
	}
}

func TestTLS(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/2.0/mlflow/experiments/search" {
			_, _ = w.Write([]byte(`{"experiments": []}`))
			return
		}
		w.WriteHeader(404)
	}))
	defer srv.Close()
	// the certificate is signed by nobody the gateway knows
	res := Test(context.Background(), Config{URI: srv.URL, VerifyTLS: true})
	if res.OK || res.Checks[0].Code != "tls_untrusted" {
		t.Fatalf("verified: %+v", res)
	}
	// not verifying
	if res := Test(context.Background(), Config{URI: srv.URL, VerifyTLS: false}); !res.OK {
		t.Fatalf("unverified: %+v", res)
	}
	// the company's authority: the server's own certificate stands for it
	pem := string(pemOf(srv))
	if res := Test(context.Background(), Config{URI: srv.URL, VerifyTLS: true, CACert: pem}); !res.OK {
		t.Fatalf("with the authority: %+v", res)
	}
	// a name the certificate does not carry
	host := strings.Replace(srv.URL, "127.0.0.1", "localhost", 1)
	if res := Test(context.Background(), Config{URI: host, VerifyTLS: true, CACert: pem}); res.OK ||
		(res.Checks[0].Code != "tls_hostname" && res.Checks[0].Code != "tls_other") {
		t.Fatalf("hostname: %+v", res)
	}
}

func TestRedirectToAnotherOriginKeepsTheCredentials(t *testing.T) {
	var leaked atomic.Bool
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			leaked.Store(true)
		}
		_, _ = w.Write([]byte(`{"experiments": []}`))
	}))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL, Auth: "bearer", Secret: "tok", VerifyTLS: true})
	if res.OK || res.Checks[0].Code != "redirect" || leaked.Load() {
		t.Fatalf("%+v leaked=%v", res, leaked.Load())
	}
}

func TestSameOriginRedirectIsFollowed(t *testing.T) {
	var seen atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/old") {
			http.Redirect(w, r, "/api/2.0/mlflow/experiments/search", http.StatusTemporaryRedirect)
			return
		}
		seen.Store(r.Header.Get("Authorization"))
		_, _ = w.Write([]byte(`{"experiments": []}`))
	}))
	defer srv.Close()
	res := Test(context.Background(), Config{URI: srv.URL + "/old", Auth: "bearer", Secret: "tok", VerifyTLS: true})
	if !res.OK || seen.Load() != "Bearer tok" {
		t.Fatalf("%+v %v", res, seen.Load())
	}
}

func TestOptionError(t *testing.T) {
	obj := func(s string) pyjson.Object {
		v, err := pyjson.Loads(s)
		if err != nil {
			t.Fatal(err)
		}
		return v.(pyjson.Object)
	}
	for _, c := range []struct {
		extra string
		bad   bool
	}{
		{`{}`, false}, {`{"auth":"basic","verify_tls":true}`, false}, {`{"auth":"digest"}`, true}, {`{"auth":3}`, true},
		{`{"verify_tls":"yes"}`, true}, {`{"verify_tls":false,"ca_cert":"-----BEGIN CERTIFICATE-----"}`, true},
		{`{"ca_cert":"not a pem"}`, true}, {`{"ca_cert":"-----BEGIN PRIVATE KEY-----"}`, true}, {`{"ui_url":5}`, true},
	} {
		if got := OptionError(obj(c.extra)) != ""; got != c.bad {
			t.Errorf("%s: bad=%v", c.extra, got)
		}
	}
}

func TestParseOptionsIsTolerant(t *testing.T) {
	if o := ParseOptions("not json"); o.Auth != "none" || !o.VerifyTLS {
		t.Fatalf("%+v", o)
	}
	if o := ParseOptions(`{"auth":"bearer","verify_tls":false,"ca_cert":" x ","ui_url":" https://u "}`); o.Auth != "bearer" || o.VerifyTLS || o.CACert != "" || o.UIURL != "https://u" {
		t.Fatalf("%+v", o)
	}
}
