// Package kube is the gateway's client of the Kubernetes API (services/kube_jobs.py `_call`,
// the same as services/kube_scaling.py): a few JSON requests with the pod's service
// account, no client library. The API's address, the token file and the authority are
// KubernetesSettings'.
package kube

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// Error is KubeError: the API did not do what was asked; Status 0 when it was not reached.
type Error struct {
	Status  int
	Message string
}

func (e *Error) Error() string { return e.Message }

// NoToken is kube_jobs' message when the service account has no token.
const NoToken = "No service account token: is scripts.enabled, containers.enabled, clusterView or settingsRollout set in the Helm chart?"

// Call is kube_jobs._call: `body` (nil: none) sent as json.dumps writes it; the answer's
// JSON (or its text, with `text`). Every failure is an *Error.
func Call(ctx context.Context, k scriptcfg.Kubernetes, method, path string, body any, text bool) (any, error) {
	raw, err := os.ReadFile(k.TokenFile) // read at every call: the kubelet rotates it
	token := ""
	if err == nil {
		token = strings.TrimSpace(string(raw))
	}
	if token == "" {
		return nil, &Error{0, NoToken}
	}
	var reader io.Reader
	if body != nil {
		s, err := pyjson.Dumps(body, pyjson.Options{EnsureASCII: true})
		if err != nil {
			return nil, err
		}
		reader = strings.NewReader(s)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(k.APIURL, "/")+path, reader)
	if err != nil {
		return nil, &Error{0, "The Kubernetes API is not reachable (UnsupportedProtocol)"}
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if text {
		req.Header.Set("Accept", "*/*") // a Pod's log: the API server refuses text/plain (406)
	} else {
		req.Header.Set("Accept", "application/json")
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := &http.Client{Timeout: time.Duration(k.TimeoutSeconds * float64(time.Second)), Transport: transport(k.CAFile)}
	res, err := client.Do(req)
	if err != nil {
		return nil, &Error{0, fmt.Sprintf("The Kubernetes API is not reachable (%s)", httpxName(err))}
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, &Error{0, "The Kubernetes API is not reachable (ReadError)"}
	}
	if res.StatusCode >= 400 {
		reason := string(data)
		if v, err := pyjson.Loads(string(data)); err == nil {
			if o, ok := v.(pyjson.Object); ok {
				if m, _ := o.Get("message"); truthy(m) {
					reason = pyjson.Str(m)
				}
			}
		}
		r := []rune(reason)
		if len(r) > 300 {
			r = r[:300]
		}
		return nil, &Error{res.StatusCode, fmt.Sprintf("Kubernetes answered %d: %s", res.StatusCode, string(r))}
	}
	if text {
		return string(data), nil
	}
	v, err := pyjson.Loads(string(data))
	if err != nil {
		return nil, fmt.Errorf("kube: the answer is not JSON: %w", err) // a JSONDecodeError in Python: a 500
	}
	return v, nil
}

func truthy(v any) bool {
	s, ok := v.(string)
	return v != nil && (!ok || s != "")
}

// transport trusts the service account's authority when its file is there (kube_scaling._ssl),
// the system's otherwise.
func transport(caFile string) *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.Proxy = http.ProxyFromEnvironment
	if pem, err := os.ReadFile(caFile); err == nil && caFile != "" {
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(pem)
		t.TLSClientConfig = &tls.Config{RootCAs: pool}
	}
	return t
}

// httpxName names a failure as the httpx exception it would have been.
func httpxName(err error) string {
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		var op *net.OpError
		if errors.As(err, &op) && op.Op == "dial" {
			return "ConnectTimeout"
		}
		return "ReadTimeout"
	}
	var op *net.OpError
	if errors.As(err, &op) && op.Op == "dial" {
		return "ConnectError"
	}
	var dns *net.DNSError
	if errors.As(err, &dns) {
		return "ConnectError"
	}
	return "RemoteProtocolError"
}

// Quote is urllib.parse.quote(s): '/' and the unreserved characters kept, the rest %XX.
func Quote(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || strings.IndexByte("_.-~/", c) >= 0 {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func jobs(namespace string) string { return "/apis/batch/v1/namespaces/" + namespace + "/jobs" }

// DeleteJobs is kube_jobs.delete_jobs: the Jobs of the namespace with the label selector.
func DeleteJobs(ctx context.Context, k scriptcfg.Kubernetes, namespace, selector string) error {
	_, err := Call(ctx, k, http.MethodDelete, jobs(namespace)+"?labelSelector="+Quote(selector)+"&propagationPolicy=Background", nil, false)
	return err
}

// DeleteJob is kube_jobs.delete_job: one already gone is fine.
func DeleteJob(ctx context.Context, k scriptcfg.Kubernetes, namespace, name string) error {
	_, err := Call(ctx, k, http.MethodDelete, jobs(namespace)+"/"+name+"?propagationPolicy=Background", nil, false)
	var ke *Error
	if errors.As(err, &ke) && ke.Status == 404 {
		return nil
	}
	return err
}

// Status is the status of an *Error, -1 for any other error.
func Status(err error) int {
	var ke *Error
	if errors.As(err, &ke) {
		return ke.Status
	}
	return -1
}
