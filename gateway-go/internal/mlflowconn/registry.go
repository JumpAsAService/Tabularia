package mlflowconn

// The reading side of the tracking client: what a Predict node needs of MLflow
// (docs/specs/statistical-models.md, «Predict»): the experiments and the versions Tabularia
// registered, for the form's lists; one version and its card, for the step. Only reads, with
// the connection's credentials, from the gateway: a Job never calls MLflow.
//
// Endpoints (MLflow's REST reference, REST API version 2.0, served under /api):
//
//	POST /api/2.0/mlflow/experiments/search      {max_results, page_token}       the active experiments
//	POST /api/2.0/mlflow/runs/search             {experiment_ids, filter, …}     the runs Tabularia made
//	GET  /api/2.0/mlflow/model-versions/search?filter=…&max_results=…&page_token=…
//	GET  /api/2.0/mlflow/model-versions/get?name=…&version=…
//	GET  /api/2.0/mlflow-artifacts/artifacts/<path>                              a file, through the proxy
//
// Filters: runs/search with `tags.`tabularia.model` != ''` (a run Tabularia logged carries
// the tag); model-versions/search with `run_id IN ('…', …)` (the only list comparator of
// model versions, MLflow ≥ 1.x) or `name = '…'`. Every answer is checked again here: a server
// that ignored a filter gives the same lists.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Page sizes and the most pages read: a list stops there (the newest first where MLflow can
// order them), and says nothing of what it did not read.
const (
	pageSize = 1000
	maxPages = 5
	idChunk  = 100 // run ids per `run_id IN (…)`
)

// Experiment is an experiment's id and name.
type Experiment struct {
	ID   string
	Name string
}

// Experiments are the active experiments, by name.
func (t *Tracker) Experiments(ctx context.Context) ([]Experiment, error) {
	var out []Experiment
	token := ""
	for page := 0; page < maxPages; page++ {
		var got struct {
			Experiments []struct {
				ID    string `json:"experiment_id"`
				Name  string `json:"name"`
				Stage string `json:"lifecycle_stage"`
			} `json:"experiments"`
			Next string `json:"next_page_token"`
		}
		in := map[string]any{"max_results": pageSize, "view_type": "ACTIVE_ONLY"}
		if token != "" {
			in["page_token"] = token
		}
		if err := t.post(ctx, "/api/2.0/mlflow/experiments/search", in, &got, "listing the experiments"); err != nil {
			return nil, err
		}
		for _, e := range got.Experiments {
			if e.Stage != "deleted" && e.Name != "" {
				out = append(out, Experiment{ID: e.ID, Name: e.Name})
			}
		}
		if token = got.Next; token == "" {
			break
		}
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })
	return out, nil
}

// NotFound is a thing MLflow does not have (any more): an experiment, a version.
type NotFound struct{ What string }

func (n *NotFound) Error() string {
	return n.What + " is not on the MLflow server (deleted, or never made)"
}

// ExperimentID is the id of the active experiment named; a *NotFound when there is none (or it
// is in the trash).
func (t *Tracker) ExperimentID(ctx context.Context, name string) (string, error) {
	var got struct {
		Experiment struct {
			ID    string `json:"experiment_id"`
			Stage string `json:"lifecycle_stage"`
		} `json:"experiment"`
	}
	err := t.do(ctx, t.client, http.MethodGet, "/api/2.0/mlflow/experiments/get-by-name?experiment_name="+url.QueryEscape(name),
		nil, 0, "", &got, fmt.Sprintf("finding the experiment «%s»", name))
	if isMissing(err) || (err == nil && got.Experiment.Stage == "deleted") {
		return "", &NotFound{What: fmt.Sprintf("the experiment «%s»", name)}
	}
	if err == nil && got.Experiment.ID == "" {
		err = &Error{Action: fmt.Sprintf("finding the experiment «%s»", name), Code: "bad_answer", Status: 200}
	}
	return got.Experiment.ID, err
}

func isMissing(err error) bool {
	var e *Error
	return errors.As(err, &e) && (e.ErrorCode == "RESOURCE_DOES_NOT_EXIST" || e.Code == "not_found")
}

// RunInfo is what the lists show of a run: its experiment, tags and latest metrics.
type RunInfo struct {
	ID         string
	Experiment string
	Tags       map[string]string
	Metrics    map[string]float64
}

type runJSON struct {
	Info struct {
		RunID      string `json:"run_id"`
		Experiment string `json:"experiment_id"`
	} `json:"info"`
	Data struct {
		Tags    []Tag `json:"tags"`
		Metrics []struct {
			Key   string  `json:"key"`
			Value float64 `json:"value"`
		} `json:"metrics"`
	} `json:"data"`
}

func (r runJSON) info() RunInfo {
	out := RunInfo{ID: r.Info.RunID, Experiment: r.Info.Experiment, Tags: map[string]string{}, Metrics: map[string]float64{}}
	for _, tag := range r.Data.Tags {
		out.Tags[tag.Key] = tag.Value
	}
	for _, m := range r.Data.Metrics {
		out.Metrics[m.Key] = m.Value
	}
	return out
}

// TabulariaRuns are the active runs of the experiment that carry the tag tabularia.model (the
// runs a model step of Tabularia logged), newest first.
func (t *Tracker) TabulariaRuns(ctx context.Context, experimentID string) ([]RunInfo, error) {
	var out []RunInfo
	token := ""
	for page := 0; page < maxPages; page++ {
		var got struct {
			Runs []runJSON `json:"runs"`
			Next string    `json:"next_page_token"`
		}
		in := map[string]any{"experiment_ids": []string{experimentID}, "filter": "tags.`tabularia.model` != ''",
			"run_view_type": "ACTIVE_ONLY", "max_results": pageSize, "order_by": []string{"attributes.start_time DESC"}}
		if token != "" {
			in["page_token"] = token
		}
		if err := t.post(ctx, "/api/2.0/mlflow/runs/search", in, &got, "listing the experiment's runs"); err != nil {
			return nil, err
		}
		for _, r := range got.Runs {
			info := r.info()
			if info.Tags["tabularia.model"] != "" && (info.Experiment == "" || info.Experiment == experimentID) {
				out = append(out, info)
			}
		}
		if token = got.Next; token == "" {
			break
		}
	}
	return out, nil
}

// ModelVersion is a registered model's version.
type ModelVersion struct {
	Name    string
	Version string
	RunID   string
	Source  string
	Created int64 // milliseconds since the epoch
	Status  string
	Tags    map[string]string
}

// Number is the version's number, 0 when it is not one.
func (v ModelVersion) Number() int64 {
	n, err := strconv.ParseInt(v.Version, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

type versionJSON struct {
	Name    string          `json:"name"`
	Version json.RawMessage `json:"version"`
	RunID   string          `json:"run_id"`
	Source  string          `json:"source"`
	Created json.RawMessage `json:"creation_timestamp"`
	Status  string          `json:"status"`
	Tags    []Tag           `json:"tags"`
}

func (v versionJSON) version() ModelVersion {
	out := ModelVersion{Name: v.Name, Version: strings.Trim(string(v.Version), `"`), RunID: v.RunID, Source: v.Source, Status: v.Status,
		Tags: map[string]string{}}
	out.Created, _ = strconv.ParseInt(strings.Trim(string(v.Created), `"`), 10, 64)
	for _, tag := range v.Tags {
		out.Tags[tag.Key] = tag.Value
	}
	return out
}

// searchVersions is every version the filter gives, as far as maxPages.
func (t *Tracker) searchVersions(ctx context.Context, filter, action string) ([]ModelVersion, error) {
	var out []ModelVersion
	token := ""
	for page := 0; page < maxPages; page++ {
		q := url.Values{"filter": {filter}, "max_results": {strconv.Itoa(pageSize)}}
		if token != "" {
			q.Set("page_token", token)
		}
		var got struct {
			Versions []versionJSON `json:"model_versions"`
			Next     string        `json:"next_page_token"`
		}
		if err := t.do(ctx, t.client, http.MethodGet, "/api/2.0/mlflow/model-versions/search?"+q.Encode(), nil, 0, "", &got, action); err != nil {
			return nil, err
		}
		for _, v := range got.Versions {
			out = append(out, v.version())
		}
		if token = got.Next; token == "" {
			break
		}
	}
	return out, nil
}

var runIDText = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// VersionsOfRuns are the versions registered from these runs, as many calls as the ids need.
// An id that is not one (the filter is text) is left out.
func (t *Tracker) VersionsOfRuns(ctx context.Context, runIDs []string) ([]ModelVersion, error) {
	var ids []string
	for _, id := range runIDs {
		if runIDText.MatchString(id) {
			ids = append(ids, id)
		}
	}
	want := map[string]bool{}
	for _, id := range ids {
		want[id] = true
	}
	var out []ModelVersion
	for len(ids) > 0 {
		n := min(len(ids), idChunk)
		chunk := ids[:n]
		ids = ids[n:]
		filter := "run_id IN ('" + strings.Join(chunk, "', '") + "')"
		got, err := t.searchVersions(ctx, filter, "listing the registered models' versions")
		if err != nil {
			return nil, err
		}
		for _, v := range got {
			if want[v.RunID] {
				out = append(out, v)
			}
		}
	}
	return out, nil
}

// VersionsOf are the versions of one registered model. A name holding a quote cannot be put in
// MLflow's filter: a *NotFound.
func (t *Tracker) VersionsOf(ctx context.Context, name string) ([]ModelVersion, error) {
	if strings.ContainsAny(name, `'\`) {
		return nil, &NotFound{What: fmt.Sprintf("the registered model «%s» (a name with a quote cannot be looked up)", name)}
	}
	got, err := t.searchVersions(ctx, "name = '"+name+"'", fmt.Sprintf("listing the versions of «%s»", name))
	if err != nil {
		return nil, err
	}
	out := got[:0]
	for _, v := range got {
		if v.Name == name {
			out = append(out, v)
		}
	}
	return out, nil
}

// Version is one version of a registered model; a *NotFound when MLflow has no such version.
func (t *Tracker) Version(ctx context.Context, name, version string) (*ModelVersion, error) {
	var got struct {
		Version versionJSON `json:"model_version"`
	}
	q := url.Values{"name": {name}, "version": {version}}
	err := t.do(ctx, t.client, http.MethodGet, "/api/2.0/mlflow/model-versions/get?"+q.Encode(), nil, 0, "", &got,
		fmt.Sprintf("reading the version %s of «%s»", version, name))
	if isMissing(err) {
		return nil, &NotFound{What: fmt.Sprintf("the version %s of «%s»", version, name)}
	}
	if err != nil {
		return nil, err
	}
	v := got.Version.version()
	if v.Name == "" || v.Version == "" {
		return nil, &Error{Action: fmt.Sprintf("reading the version %s of «%s»", version, name), Code: "bad_answer", Status: 200}
	}
	return &v, nil
}

// TooLarge is a file longer than the caller reads.
type TooLarge struct {
	Name  string
	Limit int64
}

func (e *TooLarge) Error() string {
	return fmt.Sprintf("%s is larger than Tabularia reads (%d bytes)", e.Name, e.Limit)
}

// Download is one file under artifactPath (from ArtifactPath) through the artifact proxy, at
// most limit bytes: a *NotFound when it is not there, a *TooLarge when it is longer. Its
// bytes are data, never shown: they are not scrubbed.
func (t *Tracker) Download(ctx context.Context, artifactPath, name string, limit int64) ([]byte, error) {
	parts := strings.Split(strings.Trim(artifactPath, "/")+"/"+name, "/")
	for i, p := range parts {
		parts[i] = url.PathEscape(p)
	}
	action := "reading " + name
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, t.base+"/api/2.0/mlflow-artifacts/artifacts/"+strings.Join(parts, "/"), nil)
	if err != nil {
		return nil, &Error{Action: action, Code: "unreachable"}
	}
	switch t.c.Auth {
	case "basic":
		req.SetBasicAuth(t.c.User, t.c.Secret)
	case "bearer":
		req.Header.Set("Authorization", "Bearer "+t.c.Secret)
	}
	resp, err := t.uploads.Do(req)
	if err != nil {
		code, _ := classify(err)
		return nil, &Error{Action: action, Code: code}
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		e := &Error{Action: action, Status: resp.StatusCode, Code: "http_error"}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		var answer struct {
			ErrorCode string `json:"error_code"`
		}
		if json.Unmarshal(raw, &answer) == nil && errorCodeText.MatchString(answer.ErrorCode) {
			e.ErrorCode = answer.ErrorCode
		}
		switch {
		case resp.StatusCode >= 300 && resp.StatusCode < 400:
			e.Code = "redirect"
		case resp.StatusCode == http.StatusUnauthorized:
			e.Code = "unauthorized"
		case resp.StatusCode == http.StatusForbidden:
			e.Code = "forbidden"
		case resp.StatusCode == http.StatusNotFound || e.ErrorCode == "RESOURCE_DOES_NOT_EXIST":
			return nil, &NotFound{What: name}
		}
		return nil, e
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		code, _ := classify(err)
		return nil, &Error{Action: action, Code: code}
	}
	if int64(len(b)) > limit {
		return nil, &TooLarge{Name: name, Limit: limit}
	}
	return b, nil
}
