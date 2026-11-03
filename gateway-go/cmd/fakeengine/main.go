// fakeengine stands in for the engine (the backend) behind a gateway under comparison
// (scripts/diffcheck.sh): it records every request the gateway sends it and answers what the
// comparison scripted. The same script goes to the engine of both stacks, so the two gateways
// get the same answers; their recorded requests are then compared.
//
//	POST   /__script    [{"method": "GET", "path": "^/engines$", "status": 200, "body": …}, …]
//	GET    /__recorded  the requests since the last call, then forgotten
//	anything else       the first rule matching method and path, or 200 {} when none does
package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"sync"
)

type rule struct {
	Method string          `json:"method"`
	Path   string          `json:"path"`
	Status int             `json:"status"`
	Body   json.RawMessage `json:"body"`
	Text   string          `json:"text"` // a body that is not JSON, written as it is
	Type   string          `json:"type"` // its content type (default application/json)
	re     *regexp.Regexp
}

type recorded struct {
	Method string `json:"method"`
	Path   string `json:"path"`
	Query  string `json:"query"`
	Body   any    `json:"body"`
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
		log_ = append(log_, recorded{r.Method, r.URL.Path, r.URL.RawQuery, body})
		var hit *rule
		for i := range rules {
			if (rules[i].Method == "" || rules[i].Method == r.Method) && rules[i].re.MatchString(r.URL.Path) {
				hit = &rules[i]
				break
			}
		}
		mu.Unlock()
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
	log.Fatal(http.ListenAndServe(addr, nil))
}
