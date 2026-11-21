package mlflowconn

// The tracking client: what a flow run's model step writes to MLflow
// (docs/specs/statistical-models.md, «MLflow» → «What is logged»). The gateway calls it after
// the step's results are imported; the Job never does, so the credentials never reach a pod.
//
// Endpoints (MLflow's REST reference, https://mlflow.org/docs/latest/api_reference/rest-api.html,
// REST API version 2.0, served under /api):
//
//	GET  /api/2.0/mlflow/experiments/get-by-name?experiment_name=…   the experiment, or RESOURCE_DOES_NOT_EXIST
//	POST /api/2.0/mlflow/experiments/create        {name, tags}       its id; RESOURCE_ALREADY_EXISTS on a race
//	POST /api/2.0/mlflow/runs/create               {experiment_id, run_name, start_time}
//	POST /api/2.0/mlflow/runs/log-batch            {run_id, metrics, params, tags}   at most 1,000 entries,
//	                                                                  100 params, 100 tags; keys and values cut
//	POST /api/2.0/mlflow/runs/update               {run_id, status, end_time}
//	POST /api/2.0/mlflow/registered-models/create  {name, tags}      RESOURCE_ALREADY_EXISTS is fine
//	POST /api/2.0/mlflow/model-versions/create     {name, source, run_id, tags}
//	PUT  /api/2.0/mlflow-artifacts/artifacts/<experiment>/<run>/artifacts/<path>   the artifact proxy
//
// A failure is an *Error: what the gateway was doing and a code, never the server's text
// (which can echo a header or a value) nor the secret.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Batch limits of runs/log-batch, from MLflow's REST reference: at most 1,000 entries in one
// request, of which at most 100 parameters and 100 tags; keys up to 250 characters, parameter
// and tag values up to 250 characters (log-batch's documented limit, the lowest of the
// versions: a longer value is cut and ends with «…»).
const (
	MaxBatchEntries = 1000
	MaxBatchParams  = 100
	MaxBatchTags    = 100
	MaxKeyLength    = 250
	MaxValueLength  = 250
)

// Error is a failed call to the tracking server, as people read it.
type Error struct {
	Action    string // what the gateway was doing: «creating the run»
	Code      string // as Check.Code
	Status    int    // the HTTP status, when there was one
	ErrorCode string // MLflow's error_code (an upper-case name), when the answer had one
}

func (e *Error) Error() string {
	var why string
	switch e.Code {
	case "timeout":
		why = "the tracking server did not answer in time"
	case "unreachable":
		why = "the tracking server could not be reached"
	case "tls_untrusted":
		why = "the tracking server's certificate is not signed by an authority Tabularia trusts (the connection's certificate authority)"
	case "tls_hostname":
		why = "the tracking server's certificate is not for its address"
	case "tls_other":
		why = "the secure connection to the tracking server failed"
	case "redirect":
		why = "the tracking server sent Tabularia to another address, which it does not follow with the connection's credentials"
	case "unauthorized":
		why = "the tracking server refused the connection's credentials (401): check its user and password or token"
	case "forbidden":
		why = "the connection's credentials are not allowed to do this on the tracking server (403)"
	case "not_found":
		why = "the tracking server does not offer this (404)"
	case "bad_answer":
		why = "the tracking server's answer is not MLflow's"
	case "batch_refused":
		why = fmt.Sprintf("the tracking server refused a key or a value, and so the whole batch (%s, HTTP %d): MLflow takes in a key "+
			"only letters, digits, _ - . / : and spaces, as a plain path (no «//», no «.» or «..» between slashes), up to 250 "+
			"characters, and an older server keeps shorter values", e.ErrorCode, e.Status)
	default:
		why = fmt.Sprintf("the tracking server answered with an error (HTTP %d)", e.Status)
		if e.ErrorCode != "" {
			why = fmt.Sprintf("the tracking server answered %s (HTTP %d)", e.ErrorCode, e.Status)
		}
	}
	if e.Action != "" {
		why += ", " + e.Action
	}
	if e.Code == "timeout" || e.Code == "unreachable" {
		why += ". " + NoProxy
	}
	return why
}

// NoProxy says, after a tracking server that does not answer, that the gateway calls it directly
// (newClient: the proxy of the environment would defeat the check of the address).
const NoProxy = "Tabularia calls the tracking server directly, never through the proxy of its environment (HTTPS_PROXY): " +
	"a server reachable only through a proxy cannot be used"

// IsCode says whether err is an *Error with that MLflow error_code.
func IsCode(err error, code string) bool {
	var e *Error
	return errors.As(err, &e) && e.ErrorCode == code
}

// Unsupported says whether a call failed because the server does not offer it at all (an
// endpoint it does not have, a feature it was started without), not because it refused.
func Unsupported(err error) bool {
	var e *Error
	if !errors.As(err, &e) {
		return false
	}
	switch e.ErrorCode {
	case "ENDPOINT_NOT_FOUND", "FEATURE_DISABLED", "NOT_IMPLEMENTED":
		return true
	}
	return e.Status == http.StatusNotFound || e.Status == http.StatusMethodNotAllowed || e.Status == http.StatusNotImplemented
}

// Tracker writes to one tracking server with one connection's credentials.
type Tracker struct {
	c       Config
	base    string
	client  *http.Client
	uploads *http.Client // artifacts: a fitted object can take minutes
}

// NewTracker is a client for c; c.Timeout bounds each JSON call (0 = 30 s), an upload has ten
// times as long (at least ten minutes).
func NewTracker(c Config) *Tracker {
	if c.Timeout <= 0 {
		c.Timeout = 30 * time.Second
	}
	up := c
	up.Timeout = max(10*c.Timeout, 10*time.Minute)
	if c.Timeout < time.Second { // a test's short timeout: uploads time out as soon
		up.Timeout = 10 * c.Timeout
	}
	return &Tracker{c: c, base: strings.TrimRight(strings.TrimSpace(c.URI), "/"), client: newClient(c), uploads: newClient(up)}
}

var errorCodeText = regexp.MustCompile(`^[A-Z][A-Z_]{0,63}$`)

// do sends one request and decodes a JSON answer into out (nil: the answer is not read).
func (t *Tracker) do(ctx context.Context, client *http.Client, method, path string, body io.Reader, size int64, contentType string,
	out any, action string) error {
	req, err := http.NewRequestWithContext(ctx, method, t.base+path, body)
	if err != nil {
		return &Error{Action: action, Code: "unreachable"}
	}
	if body != nil {
		req.ContentLength = size
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json")
	switch t.c.Auth {
	case "basic":
		req.SetBasicAuth(t.c.User, t.c.Secret)
	case "bearer":
		req.Header.Set("Authorization", "Bearer "+t.c.Secret)
	}
	resp, err := client.Do(req)
	if err != nil {
		code, _ := classify(err)
		return &Error{Action: action, Code: code}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	raw = scrub(raw, t.c.Secret)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		e := &Error{Action: action, Status: resp.StatusCode}
		switch {
		case resp.StatusCode >= 300 && resp.StatusCode < 400:
			e.Code = "redirect"
		case resp.StatusCode == http.StatusUnauthorized:
			e.Code = "unauthorized"
		case resp.StatusCode == http.StatusForbidden:
			e.Code = "forbidden"
		default:
			e.Code = "http_error"
		}
		var answer struct {
			ErrorCode string `json:"error_code"`
		}
		if json.Unmarshal(raw, &answer) == nil && errorCodeText.MatchString(answer.ErrorCode) {
			e.ErrorCode = answer.ErrorCode
		}
		if e.Code == "http_error" && resp.StatusCode == http.StatusNotFound && e.ErrorCode == "" {
			e.Code = "not_found"
		}
		return e
	}
	if out != nil && json.Unmarshal(raw, out) != nil {
		return &Error{Action: action, Code: "bad_answer", Status: resp.StatusCode}
	}
	return nil
}

func (t *Tracker) post(ctx context.Context, path string, in, out any, action string) error {
	b, err := json.Marshal(in)
	if err != nil {
		return err
	}
	return t.do(ctx, t.client, http.MethodPost, path, bytes.NewReader(b), int64(len(b)), "application/json", out, action)
}

// Tag is a key and a value: a run's, an experiment's, a model's or a version's tag.
type Tag struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// Metric is one number of a run, at step 0.
type Metric struct {
	Key       string  `json:"key"`
	Value     float64 `json:"value"`
	Timestamp int64   `json:"timestamp"`
	Step      int64   `json:"step"`
}

// Experiment is the experiment named, created when it is missing (with the tag
// tabularia.created = true). created says it was.
func (t *Tracker) Experiment(ctx context.Context, name string) (id string, created bool, err error) {
	find := func() (string, string, error) {
		var got struct {
			Experiment struct {
				ID    string `json:"experiment_id"`
				Stage string `json:"lifecycle_stage"`
			} `json:"experiment"`
		}
		err := t.do(ctx, t.client, http.MethodGet, "/api/2.0/mlflow/experiments/get-by-name?experiment_name="+url.QueryEscape(name),
			nil, 0, "", &got, fmt.Sprintf("finding the experiment «%s»", name))
		if err == nil && got.Experiment.ID == "" {
			err = &Error{Action: fmt.Sprintf("finding the experiment «%s»", name), Code: "bad_answer", Status: 200}
		}
		return got.Experiment.ID, got.Experiment.Stage, err
	}
	id, stage, err := find()
	if err == nil {
		if stage == "deleted" {
			return "", false, &Deleted{Name: name}
		}
		return id, false, nil
	}
	var e *Error
	if !errors.As(err, &e) || (e.ErrorCode != "RESOURCE_DOES_NOT_EXIST" && e.Code != "not_found") {
		return "", false, err
	}
	var made struct {
		ID string `json:"experiment_id"`
	}
	err = t.post(ctx, "/api/2.0/mlflow/experiments/create", map[string]any{"name": name, "tags": []Tag{{"tabularia.created", "true"}}},
		&made, fmt.Sprintf("creating the experiment «%s»", name))
	if IsCode(err, "RESOURCE_ALREADY_EXISTS") { // made meanwhile by another run
		id, _, err = find()
		return id, false, err
	}
	if err == nil && made.ID == "" {
		err = &Error{Action: fmt.Sprintf("creating the experiment «%s»", name), Code: "bad_answer", Status: 200}
	}
	return made.ID, err == nil, err
}

// Deleted is an experiment in MLflow's trash: a run cannot go there, and its name is taken.
type Deleted struct{ Name string }

func (d *Deleted) Error() string {
	return fmt.Sprintf("the experiment «%s» is in MLflow's trash: restore it in MLflow, or choose another name", d.Name)
}

// Run is a run created: its id and where its artifacts go.
type Run struct {
	ID          string
	ArtifactURI string
}

// CreateRun starts a run in the experiment, RUNNING.
func (t *Tracker) CreateRun(ctx context.Context, experimentID, name string, start time.Time) (*Run, error) {
	var got struct {
		Run struct {
			Info struct {
				RunID       string `json:"run_id"`
				ArtifactURI string `json:"artifact_uri"`
			} `json:"info"`
		} `json:"run"`
	}
	err := t.post(ctx, "/api/2.0/mlflow/runs/create", map[string]any{"experiment_id": experimentID, "run_name": Cut(name, MaxValueLength),
		"start_time": start.UnixMilli()}, &got, "creating the run")
	if err == nil && got.Run.Info.RunID == "" {
		err = &Error{Action: "creating the run", Code: "bad_answer", Status: 200}
	}
	if err != nil {
		return nil, err
	}
	return &Run{ID: got.Run.Info.RunID, ArtifactURI: got.Run.Info.ArtifactURI}, nil
}

// LogBatch logs the parameters, metrics and tags in as many calls as the limits need. Keys
// and values are cut to the limits. A batch the server refuses as a whole for one of its keys or
// values (INVALID_PARAMETER_VALUE) says what MLflow takes, since its own words are never shown.
func (t *Tracker) LogBatch(ctx context.Context, runID string, params []Tag, metrics []Metric, tags []Tag) error {
	for _, b := range Batches(params, metrics, tags) {
		err := t.post(ctx, "/api/2.0/mlflow/runs/log-batch", map[string]any{"run_id": runID, "params": b.Params, "metrics": b.Metrics,
			"tags": b.Tags}, nil, "logging the parameters, metrics and tags")
		var e *Error
		if errors.As(err, &e) && e.ErrorCode == "INVALID_PARAMETER_VALUE" {
			e.Code = "batch_refused"
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// Batch is one log-batch call's content.
type Batch struct {
	Params  []Tag
	Metrics []Metric
	Tags    []Tag
}

// Batches splits the entries by log-batch's limits: at most 100 parameters, 100 tags and
// 1,000 entries per call, the keys and values cut.
func Batches(params []Tag, metrics []Metric, tags []Tag) []Batch {
	params, tags = cutAll(params), cutAll(tags)
	ms := make([]Metric, len(metrics))
	for i, m := range metrics {
		m.Key = cutKey(m.Key)
		ms[i] = m
	}
	var out []Batch
	for len(params) > 0 || len(ms) > 0 || len(tags) > 0 {
		var b Batch
		n := min(len(params), MaxBatchParams)
		b.Params, params = params[:n], params[n:]
		n = min(len(tags), MaxBatchTags)
		b.Tags, tags = tags[:n], tags[n:]
		n = min(len(ms), MaxBatchEntries-len(b.Params)-len(b.Tags))
		b.Metrics, ms = ms[:n], ms[n:]
		if b.Params == nil {
			b.Params = []Tag{}
		}
		if b.Tags == nil {
			b.Tags = []Tag{}
		}
		if b.Metrics == nil {
			b.Metrics = []Metric{}
		}
		out = append(out, b)
	}
	return out
}

func cutAll(in []Tag) []Tag {
	out := make([]Tag, len(in))
	for i, t := range in {
		out[i] = Tag{Key: cutKey(t.Key), Value: Cut(t.Value, MaxValueLength)}
	}
	return out
}

// cutKey keeps the first MaxKeyLength characters of a key, without the «…» a value gets: MLflow
// refuses that character in a key, and one refused key fails the whole batch. (The flows' keys
// come already within the limit, each kept apart by a hash: this is the last guard.)
func cutKey(s string) string {
	if utf8.RuneCountInString(s) <= MaxKeyLength {
		return s
	}
	return string([]rune(s)[:MaxKeyLength])
}

// Cut keeps at most n characters of s, the last one «…» when it was cut.
func Cut(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	r := []rune(s)
	return string(r[:n-1]) + "…"
}

// EndRun closes the run: FINISHED or FAILED, with its end time.
func (t *Tracker) EndRun(ctx context.Context, runID string, failed bool, end time.Time) error {
	status := "FINISHED"
	if failed {
		status = "FAILED"
	}
	return t.post(ctx, "/api/2.0/mlflow/runs/update", map[string]any{"run_id": runID, "status": status, "end_time": end.UnixMilli()},
		nil, "closing the run")
}

// ArtifactPath is where the artifact proxy keeps a run's artifacts (the path after
// mlflow-artifacts/artifacts/), "" when the run's artifacts are not served by the tracking
// server: a store the client would have to reach itself (s3://, a file path…).
func ArtifactPath(artifactURI string) string {
	rest, ok := strings.CutPrefix(strings.TrimSpace(artifactURI), "mlflow-artifacts:")
	if !ok {
		return ""
	}
	if strings.HasPrefix(rest, "//") { // mlflow-artifacts://host:port/path
		rest = rest[2:]
		_, after, found := strings.Cut(rest, "/")
		if !found {
			return ""
		}
		rest = after
	}
	return strings.Trim(rest, "/")
}

// Upload puts one file under the run's artifacts through the proxy (artifactPath from
// ArtifactPath, name relative to it: «model/model.json»).
func (t *Tracker) Upload(ctx context.Context, artifactPath, name string, body io.Reader, size int64) error {
	parts := strings.Split(artifactPath+"/"+name, "/")
	for i, p := range parts {
		parts[i] = url.PathEscape(p)
	}
	return t.do(ctx, t.uploads, http.MethodPut, "/api/2.0/mlflow-artifacts/artifacts/"+strings.Join(parts, "/"), body, size,
		"application/octet-stream", nil, "uploading "+name)
}

// Register makes a version of the registered model `name` (created when missing) from the
// run's `model` artifacts; the version's number.
func (t *Tracker) Register(ctx context.Context, name, source, runID string, tags []Tag) (string, error) {
	action := fmt.Sprintf("registering the model «%s»", name)
	err := t.post(ctx, "/api/2.0/mlflow/registered-models/create", map[string]any{"name": name}, nil, action)
	if err != nil && !IsCode(err, "RESOURCE_ALREADY_EXISTS") {
		return "", err
	}
	var got struct {
		Version struct {
			Version json.RawMessage `json:"version"`
		} `json:"model_version"`
	}
	if err := t.post(ctx, "/api/2.0/mlflow/model-versions/create", map[string]any{"name": name, "source": source, "run_id": runID,
		"tags": cutAll(tags)}, &got, action); err != nil {
		return "", err
	}
	v := strings.Trim(string(got.Version.Version), `"`)
	if _, err := strconv.ParseInt(v, 10, 64); err != nil {
		return "", &Error{Action: action, Code: "bad_answer", Status: 200}
	}
	return v, nil
}
