package pod

// The statements the XMLA interface refused, kept for a day when the installation allows it
// (docs/specs/xmla.md, «The MDX that is understood»): that is how a trial with a real client
// becomes a test case. The MDX of a statement carries member names, which are values of the data,
// so nothing is kept unless the switch is on, nothing of it reaches a log, and it is read only
// with a key that is not limited to some resources. One file in the pod's data directory, a small
// one: at most maxRefused statements of maxRefusedBytes each, and none older than a day.

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	refusedFile     = "xmla-refused.json"
	maxRefused      = 100
	maxRefusedBytes = 16 << 10
	keepRefusedFor  = 24 * time.Hour
)

type refusedEntry struct {
	At        time.Time `json:"at"`
	Statement string    `json:"statement"`
	// what was not understood, as the fault told it
	Reason    string `json:"reason"`
	Truncated bool   `json:"truncated,omitempty"`
}

type refusedStore struct {
	mu   sync.Mutex
	path string
}

// refusals is the store of this pod, in its data directory.
func (s *Server) refusals() *refusedStore {
	s.xrefusedOnce.Do(func() {
		dir := s.cfg.DataDir
		if s.prep != nil && s.prep.Dir != "" {
			dir = s.prep.Dir
		}
		s.xrefused = &refusedStore{path: filepath.Join(dir, refusedFile)}
	})
	return s.xrefused
}

// read gives the entries of the last day; the caller holds the lock.
func (r *refusedStore) read(now time.Time) []refusedEntry {
	raw, err := os.ReadFile(r.path)
	if err != nil {
		return nil
	}
	var all []refusedEntry
	if json.Unmarshal(raw, &all) != nil {
		return nil // a file that does not read is as good as none
	}
	var out []refusedEntry
	for _, e := range all {
		if now.Sub(e.At) < keepRefusedFor {
			out = append(out, e)
		}
	}
	return out
}

func (r *refusedStore) add(now time.Time, statement, reason string) {
	e := refusedEntry{At: now.UTC(), Statement: statement, Reason: reason}
	if len(e.Statement) > maxRefusedBytes {
		cut := maxRefusedBytes
		for cut > 0 && !utf8.RuneStart(e.Statement[cut]) {
			cut--
		}
		e.Statement, e.Truncated = e.Statement[:cut], true
	}
	if len(e.Reason) > 1024 {
		e.Reason = e.Reason[:1024]
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	all := append(r.read(now), e)
	if len(all) > maxRefused {
		all = all[len(all)-maxRefused:]
	}
	raw, err := json.Marshal(all)
	if err != nil {
		return
	}
	// whole or not at all: a reader never meets half a file
	tmp := r.path + ".part"
	if os.WriteFile(tmp, raw, 0o600) == nil {
		if os.Rename(tmp, r.path) != nil {
			_ = os.Remove(tmp)
		}
	}
}

func (r *refusedStore) list(now time.Time) []refusedEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.read(now)
}

// GET /data/{api}/xmla/refused: the statements refused in the last day, the latest first.
func (s *Server) xmlaRefused(w http.ResponseWriter, r *http.Request) {
	snap, key, ok := s.admit(w, r)
	if !ok {
		return
	}
	if snap.m.XMLA == nil {
		s.fail(w, r, &Problem{Status: 404, Code: "xmla_off", Detail: "This data API does not answer XMLA"})
		return
	}
	if !snap.m.XMLA.KeepRefused {
		s.fail(w, r, &Problem{Status: 404, Code: "not_found", Detail: "The installation does not keep the statements XMLA refused"})
		return
	}
	if len(key.Resources) > 0 {
		s.fail(w, r, &Problem{Status: 403, Code: "key_not_allowed", Detail: "The refused statements are read with a key that is not limited to some resources"})
		return
	}
	list := s.refusals().list(s.now())
	out := make([]refusedEntry, 0, len(list))
	for i := len(list) - 1; i >= 0; i-- {
		out = append(out, list[i])
	}
	recordOf(r).rows = len(out)
	writeJSON(w, 200, map[string]any{"refused": out, "kept_hours": int(keepRefusedFor.Hours())})
}
