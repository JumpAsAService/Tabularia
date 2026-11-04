package kube

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// NoScalingToken is kube_scaling's message when the service account has no token.
const NoScalingToken = "The gateway has no service account token: is adminScaling enabled in the chart?"

// ScalingCall is kube_scaling._call: as Call, but a body is a JSON merge patch
// (application/merge-patch+json), the answer is always JSON, and a missing token says so
// in kube_scaling's words.
func ScalingCall(ctx context.Context, k scriptcfg.Kubernetes, method, path string, body any) (any, error) {
	raw, err := os.ReadFile(k.TokenFile) // read at every call: the kubelet rotates it
	token := ""
	if err == nil {
		token = strings.TrimSpace(string(raw))
	}
	if token == "" {
		return nil, &Error{0, NoScalingToken}
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
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/merge-patch+json")
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
	v, err := pyjson.Loads(string(data))
	if err != nil {
		return nil, fmt.Errorf("kube: the answer is not JSON: %w", err)
	}
	return v, nil
}
