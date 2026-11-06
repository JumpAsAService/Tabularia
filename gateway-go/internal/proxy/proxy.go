// Package proxy forwards to the AI service (APP__ROLE=ai) the requests that are its own (the
// assistant's routes, docs/specs/gateway-go.md «The AI service»), as they came: method, path,
// query, headers (the client's address as the proxy in front set it, the cookies, the Host),
// body; the answer streamed back as it arrives (the assistant's chat).
package proxy

import (
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
)

// New is the forwarding handler towards upstream.
func New(upstream *url.URL, log *slog.Logger) http.Handler {
	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			// the Python code reads the Host (same_origin) and the
			// X-Forwarded-* headers as the proxy in front wrote them (Rewrite removes them
			// from the outgoing request: put back as they came). Behind the Go gateway,
			// uvicorn must trust it as a proxy (--forwarded-allow-ips)
			r.Out.Host = r.In.Host
			for _, h := range []string{"X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				if v, ok := r.In.Header[h]; ok {
					r.Out.Header[h] = v
				}
			}
		},
		// every piece at once: streamed answers must not wait for a buffer to fill
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Error("upstream unreachable", "path", r.URL.Path, "err", err)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte(`{"detail":"The gateway is not reachable"}`))
		},
	}
}
