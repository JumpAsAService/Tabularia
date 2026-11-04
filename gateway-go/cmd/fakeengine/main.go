// fakeengine stands in for the engine (the backend) behind a gateway under comparison
// (scripts/diffcheck.sh): it records every request the gateway sends it and answers what the
// comparison scripted. The same script goes to the engine of both stacks, so the two gateways
// get the same answers; their recorded requests are then compared.
//
//	POST   /__script    [{"method": "GET", "path": "^/engines$", "status": 200, "body": …}, …]
//	GET    /__recorded  the requests since the last call, then forgotten
//	anything else       the first rule matching method and path, or 200 {} when none does
//
// It answers on port 2719 too (a notebook editor's pod, whose file the reaper reads).
package main

import (
	"encoding/json"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

type rule struct {
	Method string          `json:"method"`
	Path   string          `json:"path"`
	Status int             `json:"status"`
	Body   json.RawMessage `json:"body"`
	Text   string          `json:"text"` // a body that is not JSON, written as it is
	Type   string          `json:"type"` // its content type (default application/json)
	// Headers are set on the answer (a Content-Disposition)
	Headers map[string]string `json:"headers"`
	// Drop closes the connection without an answer (an engine that went away mid-request)
	Drop bool `json:"drop"`
	re   *regexp.Regexp
}

type recorded struct {
	Method string `json:"method"`
	Path   string `json:"path"`
	Query  string `json:"query"`
	Body   any    `json:"body"`
	// a HEC request's credential («Splunk <token>»): the fake engine stands in for Splunk
	// too (the SIEM's suite reaches it as siem.test)
	Splunk string `json:"splunk,omitempty"`
	// a webhook's delivery (X-Tabularia-Event): its own headers, the fake engine stands in for
	// the receivers too
	Webhook map[string]string `json:"webhook,omitempty"`
}

// sentNow replaces a HEC event's `time` (the moment of sending, in seconds) by «<now>»:
// the two gateways send a few milliseconds apart.
func sentNow(body any) any {
	if m, ok := body.(map[string]any); ok {
		if t, ok := m["time"].(float64); ok && math.Abs(t-float64(time.Now().Unix())) < 600 {
			m["time"] = "<now>"
		}
	}
	return body
}

var (
	mu    sync.Mutex
	rules []rule
	log_  []recorded
)

func main() {
	http.HandleFunc("/__script", func(w http.ResponseWriter, r *http.Request) {
		var rs []rule
		if err := json.NewDecoder(r.Body).Decode(&rs); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		for i := range rs {
			re, err := regexp.Compile(rs[i].Path)
			if err != nil {
				http.Error(w, err.Error(), 400)
				return
			}
			rs[i].re = re
		}
		mu.Lock()
		rules = rs
		mu.Unlock()
	})
	http.HandleFunc("/__recorded", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		out := log_
		log_ = nil
		mu.Unlock()
		if out == nil {
			out = []recorded{}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(out)
	})
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body any
		if len(raw) > 0 && json.Unmarshal(raw, &body) != nil {
			body = string(raw)
		}
		mu.Lock()
		rec := recorded{Method: r.Method, Path: r.URL.Path, Query: r.URL.RawQuery, Body: body}
		if r.Header.Get("X-Tabularia-Event") != "" {
			rec.Webhook = map[string]string{}
			for _, h := range []string{"Content-Type", "User-Agent", "X-Tabularia-Event", "X-Tabularia-Delivery", "X-Tabularia-Signature"} {
				rec.Webhook[h] = r.Header.Get(h)
			}
		}
		if a := r.Header.Get("Authorization"); strings.HasPrefix(a, "Splunk ") {
			rec.Splunk, rec.Body = a, sentNow(body)
		}
		if r.URL.Path == "/idp/token" {
			rec.Body = idpRecord(raw)
		}
		log_ = append(log_, rec)
		var hit *rule
		for i := range rules {
			if (rules[i].Method == "" || rules[i].Method == r.Method) && rules[i].re.MatchString(r.URL.Path) {
				hit = &rules[i]
				break
			}
		}
		mu.Unlock()
		if hit != nil && hit.Drop {
			if hj, ok := w.(http.Hijacker); ok {
				if conn, _, err := hj.Hijack(); err == nil {
					conn.Close()
					return
				}
			}
		}
		if hit == nil && strings.HasPrefix(r.URL.Path, "/idp/") && idp(w, r, raw) {
			return
		}
		if hit != nil && hit.Type != "" {
			w.Header().Set("Content-Type", hit.Type)
		} else {
			w.Header().Set("Content-Type", "application/json")
		}
		if hit == nil {
			w.Write([]byte("{}"))
			return
		}
		status := hit.Status
		if status == 0 {
			status = 200
		}
		for k, v := range hit.Headers {
			w.Header().Set(k, v)
		}
		w.WriteHeader(status)
		if hit.Text != "" {
			w.Write([]byte(hit.Text))
		} else if len(hit.Body) > 0 {
			w.Write(hit.Body)
		}
	})
	addr := os.Getenv("LISTEN")
	if addr == "" {
		addr = ":8000"
	}
	// a notebook's pod stands at the fake engine too (its podIP «siem.test»): marimo's port and
	// the editor's file server, answered by the same rules and recorded with the rest
	for _, port := range []string{":2718", ":2719"} {
		go func() { _ = http.ListenAndServe(port, nil) }()
	}
	log.Fatal(http.ListenAndServe(addr, nil))
}
