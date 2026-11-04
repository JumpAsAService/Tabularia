package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// The comparison «a un giro»: a Step with a Tick runs ONE round of a background loop on both
// stacks instead of a request — the Python service's round in stack A (ticks/ticks.py, in
// the gateway's image) and the Go one in stack B (cmd/jobsonce) — at the same frozen moment.
// Then, as after a request, it compares what the round answered (its result, or that it
// failed), the requests sent to the engine (which stands in for Kubernetes, Splunk and the
// webhooks' receivers too) and the whole content of both databases.
//
// A loop's suite is an ordinary Suite named «jobs_<loop>»; its Steps set Tick (and Method
// "TICK", for the report). scripts/diffcheck.sh starts the two round servers.

var tickClient = &http.Client{Timeout: 10 * time.Minute}

func (w *World) askTick(s *side, st Step) answer {
	if s.ticks == "" {
		fail("%s: no round server (DIFF_%s_TICKS): run scripts/diffcheck.sh", s.name, strings.ToUpper(s.name[:1]))
	}
	raw, _ := json.Marshal(map[string]any{"tick": st.Tick, "now": st.Now, "env": st.Env})
	res, err := tickClient.Post(s.ticks+"/tick", "application/json", bytes.NewReader(raw))
	if err != nil {
		fail("%s round %s: %v", s.name, st.Tick, err)
	}
	out, _ := io.ReadAll(res.Body)
	res.Body.Close()
	a := answer{Status: res.StatusCode, Headers: map[string]string{}}
	var parsed map[string]any
	if json.Unmarshal(out, &parsed) != nil {
		a.Body = string(out)
	} else if e, failed := parsed["error"]; failed {
		// the two languages word an exception each their own way: that it failed is compared,
		// the text shown
		fmt.Printf("        %s round %s failed: %v\n", s.name, st.Tick, e)
		a.Body = "<the round failed>"
	} else {
		var result any = parsed["result"]
		for _, path := range st.Mask {
			result = maskPath(result, strings.Split(path, "."))
		}
		result = normalize(result)
		for _, path := range st.Unordered {
			if path == "" {
				result = sortPath(result, nil)
				continue
			}
			result = sortPath(result, strings.Split(path, "."))
		}
		a.Body = result
	}
	w.finish(s, st, &a)
	return a
}
