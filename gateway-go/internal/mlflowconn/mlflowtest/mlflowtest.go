// Package mlflowtest is a fake MLflow tracking server for the tests: the REST calls the
// tracking client makes (mlflowconn/tracking.go), with their state kept in memory and every
// call recorded. Not a server to run: only httptest.
package mlflowtest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Leak is in the text of every error the fake answers: a client that shows a server's text
// shows it.
const Leak = "server-text-that-must-never-show"

// Run is a run the fake keeps.
type Run struct {
	ID, Experiment, Name, Status string
	Params                       map[string]string
	Metrics                      map[string]float64
	Tags                         map[string]string
	Batches                      [][3]int // per log-batch call: params, metrics, tags
	Ended                        bool
}

// Version is a registered model's version.
type Version struct {
	Version, Source, RunID string
	Tags                   map[string]string
	Created                int64 // creation_timestamp, milliseconds
}

// Server is the fake. Set its options before the calls.
type Server struct {
	*httptest.Server
	mu sync.Mutex

	Authorization string        // the header expected ("" = none checked); another one is answered 401
	NoProxy       bool          // runs keep their artifacts in s3://, and PUT is unknown
	NoRegistry    bool          // the registry's endpoints are unknown (404 ENDPOINT_NOT_FOUND)
	Delay         time.Duration // every call waits this long first
	// Fail answers a status for a request (0: go on), with MLflow's error body.
	Fail func(r *http.Request) int

	Calls       []string // "METHOD path" of every call
	Experiments map[string]string
	ExpTags     map[string]map[string]string
	Deleted     map[string]bool // experiments in the trash, by name
	Runs        map[string]*Run
	Artifacts   map[string][]byte // by path after mlflow-artifacts/artifacts/: binary files as well as text
	ContentType map[string]string // the Content-Type of each file as it was PUT
	Streams     map[string]int64  // files made up on the fly, by path: their size (Stream)
	NoLength    bool              // a file is answered without its Content-Length (chunked)
	Models      map[string][]Version
	clock       int64 // the creation timestamps given, in order
}

// New starts a fake; it stops with the test.
func New(t interface{ Cleanup(func()) }) *Server {
	s := &Server{Experiments: map[string]string{}, ExpTags: map[string]map[string]string{}, Deleted: map[string]bool{},
		Runs: map[string]*Run{}, Artifacts: map[string][]byte{}, ContentType: map[string]string{}, Streams: map[string]int64{}, Models: map[string][]Version{}}
	s.Server = httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(s.Close)
	return s
}

// Paths are the calls made, in order.
func (s *Server) Paths() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.Calls...)
}

// OnlyRun is the one run made (nil when there is not exactly one).
func (s *Server) OnlyRun() *Run {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.Runs) != 1 {
		return nil
	}
	for _, r := range s.Runs {
		return r
	}
	return nil
}

func answer(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func refuse(w http.ResponseWriter, status int, code string) {
	answer(w, status, map[string]string{"error_code": code, "message": Leak})
}

func (s *Server) serve(w http.ResponseWriter, r *http.Request) {
	if s.Delay > 0 {
		select {
		case <-time.After(s.Delay):
		case <-r.Context().Done():
			return
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Calls = append(s.Calls, r.Method+" "+r.URL.Path)
	if s.Authorization != "" && r.Header.Get("Authorization") != s.Authorization {
		refuse(w, http.StatusUnauthorized, "UNAUTHENTICATED")
		return
	}
	if s.Fail != nil {
		if code := s.Fail(r); code != 0 {
			refuse(w, code, "PERMISSION_DENIED")
			return
		}
	}
	var in map[string]any
	if r.Method == http.MethodPost {
		_ = json.NewDecoder(r.Body).Decode(&in)
	}
	str := func(k string) string { v, _ := in[k].(string); return v }
	path := r.URL.Path
	switch {
	case path == "/api/2.0/mlflow/experiments/get-by-name" && r.Method == http.MethodGet:
		name := r.URL.Query().Get("experiment_name")
		id, ok := s.Experiments[name]
		if !ok {
			refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
			return
		}
		stage := "active"
		if s.Deleted[name] {
			stage = "deleted"
		}
		answer(w, 200, map[string]any{"experiment": map[string]any{"experiment_id": id, "name": name, "lifecycle_stage": stage}})
	case path == "/api/2.0/mlflow/experiments/create":
		name := str("name")
		if _, ok := s.Experiments[name]; ok {
			refuse(w, http.StatusBadRequest, "RESOURCE_ALREADY_EXISTS")
			return
		}
		id := fmt.Sprint(len(s.Experiments) + 1)
		s.Experiments[name] = id
		s.ExpTags[name] = tags(in["tags"])
		answer(w, 200, map[string]any{"experiment_id": id})
	case path == "/api/2.0/mlflow/experiments/search":
		var list []map[string]any
		for name, id := range s.Experiments {
			if !s.Deleted[name] {
				list = append(list, map[string]any{"experiment_id": id, "name": name, "lifecycle_stage": "active"})
			}
		}
		answer(w, 200, map[string]any{"experiments": list})
	case path == "/api/2.0/mlflow/runs/search":
		ids, _ := in["experiment_ids"].([]any)
		filter := str("filter")
		var list []map[string]any
		for i := len(s.Runs); i >= 1; i-- { // newest first
			run := s.Runs[fmt.Sprintf("run%d", i)]
			if run == nil || len(ids) != 1 || ids[0] != run.Experiment {
				continue
			}
			if filter == "tags.`tabularia.model` != ''" && run.Tags["tabularia.model"] == "" {
				continue
			}
			list = append(list, runJSON(run))
		}
		answer(w, 200, map[string]any{"runs": list})
	case path == "/api/2.0/mlflow/model-versions/search" && r.Method == http.MethodGet && !s.NoRegistry:
		filter := r.URL.Query().Get("filter")
		var list []map[string]any
		for name, versions := range s.Models {
			for _, v := range versions {
				if matches(filter, name, v) {
					list = append(list, versionJSON(name, v))
				}
			}
		}
		answer(w, 200, map[string]any{"model_versions": list})
	case path == "/api/2.0/mlflow/model-versions/get" && r.Method == http.MethodGet && !s.NoRegistry:
		name, number := r.URL.Query().Get("name"), r.URL.Query().Get("version")
		for _, v := range s.Models[name] {
			if v.Version == number {
				answer(w, 200, map[string]any{"model_version": versionJSON(name, v)})
				return
			}
		}
		refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
	case strings.HasPrefix(path, "/api/2.0/mlflow-artifacts/artifacts/") && r.Method == http.MethodGet && !s.NoProxy:
		key := strings.TrimPrefix(path, "/api/2.0/mlflow-artifacts/artifacts/")
		w.Header().Set("Content-Type", "application/octet-stream")
		if size, ok := s.Streams[key]; ok { // made up while it is sent, never held
			if !s.NoLength {
				w.Header().Set("Content-Length", fmt.Sprint(size))
			}
			chunk := make([]byte, 64<<10)
			for off := int64(0); off < size; {
				n := min(int64(len(chunk)), size-off)
				fill(chunk[:n], off)
				if _, err := w.Write(chunk[:n]); err != nil {
					return
				}
				off += n
			}
			return
		}
		b, ok := s.Artifacts[key]
		if !ok {
			refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
			return
		}
		if !s.NoLength {
			w.Header().Set("Content-Length", fmt.Sprint(len(b)))
		} else if f, ok := w.(http.Flusher); ok {
			f.Flush() // the headers go out before the body: chunked, no length
		}
		_, _ = w.Write(b)
	case path == "/api/2.0/mlflow/runs/create":
		id := fmt.Sprintf("run%d", len(s.Runs)+1)
		exp := str("experiment_id")
		s.Runs[id] = &Run{ID: id, Experiment: exp, Name: str("run_name"), Status: "RUNNING", Params: map[string]string{},
			Metrics: map[string]float64{}, Tags: map[string]string{}}
		uri := fmt.Sprintf("mlflow-artifacts:/%s/%s/artifacts", exp, id)
		if s.NoProxy {
			uri = fmt.Sprintf("s3://bucket/mlruns/%s/%s/artifacts", exp, id)
		}
		answer(w, 200, map[string]any{"run": map[string]any{"info": map[string]any{"run_id": id, "experiment_id": exp, "artifact_uri": uri,
			"status": "RUNNING"}}})
	case path == "/api/2.0/mlflow/runs/log-batch":
		run := s.Runs[str("run_id")]
		if run == nil {
			refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
			return
		}
		params, _ := in["params"].([]any)
		metrics, _ := in["metrics"].([]any)
		tagList, _ := in["tags"].([]any)
		if len(params) > 100 || len(tagList) > 100 || len(params)+len(metrics)+len(tagList) > 1000 {
			refuse(w, http.StatusBadRequest, "INVALID_PARAMETER_VALUE")
			return
		}
		// one key MLflow refuses fails the whole batch
		for _, list := range [][]any{params, metrics, tagList} {
			for _, e := range list {
				o, _ := e.(map[string]any)
				if k, _ := o["key"].(string); BadKey(k) {
					refuse(w, http.StatusBadRequest, "INVALID_PARAMETER_VALUE")
					return
				}
			}
		}
		for k, v := range tags(in["params"]) {
			if len([]rune(v)) > 250 || len([]rune(k)) > 250 {
				refuse(w, http.StatusBadRequest, "INVALID_PARAMETER_VALUE")
				return
			}
			run.Params[k] = v
		}
		for k, v := range tags(in["tags"]) {
			run.Tags[k] = v
		}
		for _, m := range metrics {
			o, _ := m.(map[string]any)
			k, _ := o["key"].(string)
			v, _ := o["value"].(float64)
			run.Metrics[k] = v
		}
		run.Batches = append(run.Batches, [3]int{len(params), len(metrics), len(tagList)})
		answer(w, 200, map[string]any{})
	case path == "/api/2.0/mlflow/runs/update":
		run := s.Runs[str("run_id")]
		if run == nil {
			refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
			return
		}
		run.Status, run.Ended = str("status"), in["end_time"] != nil
		answer(w, 200, map[string]any{"run_info": map[string]any{"run_id": run.ID, "status": run.Status}})
	case strings.HasPrefix(path, "/api/2.0/mlflow-artifacts/artifacts/") && r.Method == http.MethodPut && !s.NoProxy:
		b, _ := io.ReadAll(r.Body)
		if r.ContentLength != int64(len(b)) {
			refuse(w, http.StatusBadRequest, "INVALID_PARAMETER_VALUE")
			return
		}
		key := strings.TrimPrefix(path, "/api/2.0/mlflow-artifacts/artifacts/")
		s.Artifacts[key] = b
		s.ContentType[key] = r.Header.Get("Content-Type")
		answer(w, 200, map[string]any{})
	case strings.HasPrefix(path, "/api/2.0/mlflow/registered-models/") || strings.HasPrefix(path, "/api/2.0/mlflow/model-versions/"):
		if s.NoRegistry {
			refuse(w, http.StatusNotFound, "ENDPOINT_NOT_FOUND")
			return
		}
		name := str("name")
		switch path {
		case "/api/2.0/mlflow/registered-models/create":
			if _, ok := s.Models[name]; ok {
				refuse(w, http.StatusBadRequest, "RESOURCE_ALREADY_EXISTS")
				return
			}
			s.Models[name] = []Version{}
			answer(w, 200, map[string]any{"registered_model": map[string]any{"name": name}})
		case "/api/2.0/mlflow/model-versions/create":
			versions, ok := s.Models[name]
			if !ok {
				refuse(w, http.StatusNotFound, "RESOURCE_DOES_NOT_EXIST")
				return
			}
			s.clock++
			v := Version{Version: fmt.Sprint(len(versions) + 1), Source: str("source"), RunID: str("run_id"), Tags: tags(in["tags"]),
				Created: 1_700_000_000_000 + s.clock}
			s.Models[name] = append(versions, v)
			answer(w, 200, map[string]any{"model_version": map[string]any{"name": name, "version": v.Version, "status": "READY"}})
		default:
			refuse(w, http.StatusNotFound, "ENDPOINT_NOT_FOUND")
		}
	default:
		refuse(w, http.StatusNotFound, "ENDPOINT_NOT_FOUND")
	}
}

func tags(v any) map[string]string {
	out := map[string]string{}
	list, _ := v.([]any)
	for _, e := range list {
		o, _ := e.(map[string]any)
		k, _ := o["key"].(string)
		val, _ := o["value"].(string)
		out[k] = val
	}
	return out
}

func runJSON(run *Run) map[string]any {
	var tagList, metrics []map[string]any
	for k, v := range run.Tags {
		tagList = append(tagList, map[string]any{"key": k, "value": v})
	}
	for k, v := range run.Metrics {
		metrics = append(metrics, map[string]any{"key": k, "value": v})
	}
	return map[string]any{"info": map[string]any{"run_id": run.ID, "experiment_id": run.Experiment, "status": run.Status},
		"data": map[string]any{"tags": tagList, "metrics": metrics}}
}

func versionJSON(name string, v Version) map[string]any {
	var tagList []map[string]any
	for k, val := range v.Tags {
		tagList = append(tagList, map[string]any{"key": k, "value": val})
	}
	return map[string]any{"name": name, "version": v.Version, "run_id": v.RunID, "source": v.Source, "status": "READY",
		"creation_timestamp": v.Created, "tags": tagList}
}

// keyText is the characters MLflow 2.22 takes in a key (validation.py, `^[/\w.\- :]*$`, \w as
// Python's: any letter or digit).
var keyText = regexp.MustCompile(`^[\p{L}\p{N}\p{M}_/.\- :]*$`)

// BadKey says whether MLflow 2.22 refuses the key of a parameter, a metric or a tag: a character it
// does not take, more than 250 characters, or a path that is not its own normal form
// (posixpath.normpath: no empty, `.` or `..` segment, no `/` at either end).
func BadKey(k string) bool {
	if !keyText.MatchString(k) || len([]rune(k)) > 250 || k == "" {
		return true
	}
	if strings.HasPrefix(k, "/") || strings.HasSuffix(k, "/") {
		return true
	}
	for _, segment := range strings.Split(k, "/") {
		if segment == "" || segment == "." || segment == ".." {
			return true
		}
	}
	return false
}

// matches is the filters the client sends: `run_id IN ('a', 'b')`, `name = 'x'` (or between double
// quotes) and `name LIKE 'x_y'`, read as MLflow 2.22 reads them: the value between its quotes, no
// escape undone; LIKE's `_` is any one character and `%` any text.
func matches(filter, name string, v Version) bool {
	if rest, ok := strings.CutPrefix(filter, "run_id IN ("); ok {
		for _, id := range strings.Split(strings.TrimSuffix(rest, ")"), ",") {
			if strings.Trim(strings.TrimSpace(id), "'") == v.RunID {
				return true
			}
		}
		return false
	}
	if rest, ok := strings.CutPrefix(filter, "name = "); ok {
		return len(rest) >= 2 && (rest[0] == '\'' || rest[0] == '"') && rest[len(rest)-1] == rest[0] && rest[1:len(rest)-1] == name
	}
	if rest, ok := strings.CutPrefix(filter, "name LIKE '"); ok && strings.HasSuffix(rest, "'") {
		var re strings.Builder
		for _, c := range strings.TrimSuffix(rest, "'") {
			switch c {
			case '_':
				re.WriteString(".")
			case '%':
				re.WriteString(".*")
			default:
				re.WriteString(regexp.QuoteMeta(string(c)))
			}
		}
		return regexp.MustCompile("^(?s:" + re.String() + ")$").MatchString(name)
	}
	return filter == ""
}

// Register adds a version as Tabularia's registration leaves it: the run (in the experiment,
// with its tags and metrics), the model's files under the run's artifacts, the version and its
// tags. The run's id.
func (s *Server) Register(experiment, model string, runTags map[string]string, metrics map[string]float64,
	files map[string][]byte, versionTags map[string]string) (runID, version string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	exp, ok := s.Experiments[experiment]
	if !ok {
		exp = fmt.Sprint(len(s.Experiments) + 1)
		s.Experiments[experiment] = exp
	}
	runID = fmt.Sprintf("run%d", len(s.Runs)+1)
	run := &Run{ID: runID, Experiment: exp, Status: "FINISHED", Params: map[string]string{}, Metrics: map[string]float64{},
		Tags: map[string]string{}, Ended: true}
	for k, v := range runTags {
		run.Tags[k] = v
	}
	for k, v := range metrics {
		run.Metrics[k] = v
	}
	s.Runs[runID] = run
	for name, b := range files {
		s.Artifacts[fmt.Sprintf("%s/%s/artifacts/model/%s", exp, runID, name)] = b
	}
	s.clock++
	version = fmt.Sprint(len(s.Models[model]) + 1)
	tags := map[string]string{}
	for k, v := range versionTags {
		tags[k] = v
	}
	s.Models[model] = append(s.Models[model], Version{Version: version, RunID: runID, Tags: tags, Created: 1_700_000_000_000 + s.clock,
		Source: fmt.Sprintf("mlflow-artifacts:/%s/%s/artifacts/model", exp, runID)})
	return runID, version
}

// fill is the bytes of a made-up file: a function of the offset only, so that a reader can
// check what it got without the file ever being held.
func fill(p []byte, off int64) {
	for i := range p {
		p[i] = byte((off+int64(i))*31 + 7)
	}
}

// Stream makes a file of that size under the path (after mlflow-artifacts/artifacts/) that the
// fake sends in chunks, never holding it: a model file of some megabytes for a test of
// streaming. PatternSHA256 is its hash.
func (s *Server) Stream(path string, size int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Streams[path] = size
}

// Pattern is the first size bytes of a made-up file, for a test that needs them whole (small).
func Pattern(size int64) []byte {
	b := make([]byte, size)
	fill(b, 0)
	return b
}

// PatternSHA256 is the hash of a made-up file of that size, computed in chunks.
func PatternSHA256(size int64) string {
	h := sha256.New()
	chunk := make([]byte, 64<<10)
	for off := int64(0); off < size; {
		n := min(int64(len(chunk)), size-off)
		fill(chunk[:n], off)
		h.Write(chunk[:n])
		off += n
	}
	return hex.EncodeToString(h.Sum(nil))
}
