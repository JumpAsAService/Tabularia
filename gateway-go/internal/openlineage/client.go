package openlineage

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// libClient is the OpenLineageClient services/openlineage.py builds (_get_client): the
// transport Tabularia configures (OPENLINEAGE__URL, OPENLINEAGE__FILE) or, without one, the
// library's own; and, in any case, the library's configuration around each event (tags,
// facets, the datasets' reduction, the filters).
type libClient struct {
	transport ltransport
	cfg       *libConfig
	env       environ
	log       *slog.Logger
	reducer   *datasetReducer
}

// newClient is _get_client's building: OpenLineageClient(transport=…) with Tabularia's
// transport, OpenLineageClient() otherwise.
func (s Settings) newClient(env environ, log *slog.Logger) (*libClient, error) {
	aliasEnvVars(&env)
	f := &factory{env: env, log: log}
	var transport ltransport
	var err error
	if s.URL != "" || s.File != "" {
		var configs []any
		if s.URL != "" {
			http := pyjson.Object{{Key: "type", Value: "http"}, {Key: "url", Value: s.URL}, {Key: "endpoint", Value: s.Endpoint},
				{Key: "timeout", Value: s.Timeout},
				{Key: "retry", Value: pyjson.Object{{Key: "total", Value: int64(2)}, {Key: "connect", Value: int64(2)}, {Key: "read", Value: int64(2)},
					{Key: "backoff_factor", Value: 0.3}, {Key: "status_forcelist", Value: []any{int64(500), int64(502), int64(503), int64(504)}},
					{Key: "allowed_methods", Value: []any{"HEAD", "POST"}}}}}
			if s.APIKey != "" {
				http = append(http, pyjson.KV{Key: "auth", Value: pyjson.Object{{Key: "type", Value: "api_key"}, {Key: "apiKey", Value: s.APIKey}}})
			}
			configs = append(configs, http)
		}
		if s.File != "" {
			configs = append(configs, pyjson.Object{{Key: "type", Value: "file"}, {Key: "log_file_path", Value: s.File}, {Key: "append", Value: true}})
		}
		if len(configs) == 1 {
			transport, err = f.create(configs[0])
		} else {
			transport, err = f.create(pyjson.Object{{Key: "type", Value: "composite"}, {Key: "transports", Value: configs},
				{Key: "continue_on_failure", Value: true}})
		}
		if err != nil {
			return nil, err
		}
	}
	// OpenLineageClient.__init__: the transport resolved (_resolve_transport), then the filters
	// read from the configuration: a configuration the library cannot read fails the client
	cfg, err := loadLibConfig(env, log)
	if f.disabled() {
		log.Info("OpenLineage is disabled. No events will be emitted.")
		transport = &noopTransport{}
	}
	if err != nil {
		return nil, err
	}
	if transport == nil {
		if cfg.hasTransport {
			t, isDict := cfg.transport.(pyjson.Object)
			if !isDict && pyjson.Truthy(cfg.transport) {
				return nil, perr("'%s' object has no attribute 'get'", pyTypeName(cfg.transport))
			}
			if pyjson.Truthy(t.Value("type")) {
				if transport, err = f.create(t); err != nil {
					return nil, err
				}
			}
		}
	}
	if transport == nil {
		if u, _ := env.get("OPENLINEAGE_URL"); u != "" { // _http_transport_from_env_variables
			cfg := pyjson.Object{{Key: "url", Value: u}, {Key: "endpoint", Value: "api/v1/lineage"}}
			if ep, ok := env.get("OPENLINEAGE_ENDPOINT"); ok {
				cfg = cfg.Set("endpoint", ep)
			}
			if k, _ := env.get("OPENLINEAGE_API_KEY"); k != "" {
				cfg = cfg.Set("auth", pyjson.Object{{Key: "type", Value: "api_key"}, {Key: "apiKey", Value: k}})
			}
			hc, err := httpConfigFrom(cfg, "HttpConfig", env)
			if err != nil {
				return nil, err
			}
			if err := hc.validURL(false); err != nil {
				return nil, err
			}
			transport = &httpTransport{cfg: hc, log: log}
		} else {
			log.Warn("Couldn't find any OpenLineage transport configuration; will print events to console.")
			transport = &consoleTransport{log: log}
		}
	}
	c := &libClient{transport: transport, cfg: cfg, env: env, log: log}
	for _, fc := range cfg.filters {
		fc.compile(log)
	}
	return c, nil
}

// emitAll is _emit: every event through the client; the first that fails stops the rest.
func (c *libClient) emitAll(ctx context.Context, events []pyjson.Object) (int, error) {
	n := 0
	for _, e := range events {
		if err := c.emit(ctx, e); err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

// emit is OpenLineageClient.emit.
func (c *libClient) emit(ctx context.Context, e pyjson.Object) error {
	_, isRun := e.Get("eventType")
	_, isJob := e.Get("job")
	e = clone(e).(pyjson.Object)
	if isRun {
		e = c.environmentFacets(e)
	}
	e = c.tags(e, isRun, isJob)
	if isRun {
		e = c.sourceCodeLocation(e)
		if pyjson.Truthy(c.cfg.reducing) {
			if c.reducer == nil {
				c.reducer = newReducer(c.cfg, c.log)
			}
			e = c.reducer.reduce(e)
		}
	}
	if _, noop := c.transport.(*noopTransport); noop {
		return nil
	}
	if isRun {
		job, _ := e.Value("job").(pyjson.Object)
		name := text(job.Value("name"))
		for _, f := range c.cfg.filters {
			if f.drops(name) {
				return nil
			}
		}
	}
	return c.transport.emit(ctx, e)
}

func clone(v any) any {
	switch x := v.(type) {
	case pyjson.Object:
		out := make(pyjson.Object, len(x))
		for i, kv := range x {
			out[i] = pyjson.KV{Key: kv.Key, Value: clone(kv.Value)}
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = clone(e)
		}
		return out
	}
	return v
}

func setIn(o pyjson.Object, key string, f func(pyjson.Object) pyjson.Object) pyjson.Object {
	sub, _ := o.Value(key).(pyjson.Object)
	return o.Set(key, f(sub))
}

// environmentFacets is add_environment_facets: the variables the configuration names, as
// they are in the environment (in its order).
func (c *libClient) environmentFacets(e pyjson.Object) pyjson.Object {
	named := func(k string) bool {
		if c.cfg.envVarsStr != nil {
			return strings.Contains(*c.cfg.envVarsStr, k)
		}
		for _, n := range c.cfg.envVars {
			if n == k {
				return true
			}
		}
		return false
	}
	var vars []any
	found := map[string]bool{}
	for _, k := range c.env.keys {
		if named(k) {
			vars = append(vars, pyjson.Object{{Key: "name", Value: k}, {Key: "value", Value: c.env.vals[k]}})
			found[k] = true
		}
	}
	var missing []string
	for _, n := range c.cfg.envVars {
		if !found[n] {
			missing = append(missing, n)
		}
	}
	if len(missing) > 0 {
		c.log.Warn(fmt.Sprintf("The following environment variables are missing: %v when adding to OpenLineage event", missing))
	}
	if len(vars) == 0 {
		return e
	}
	return setIn(e, "run", func(run pyjson.Object) pyjson.Object {
		return setIn(run, "facets", func(f pyjson.Object) pyjson.Object {
			if f == nil {
				f = pyjson.Object{}
			}
			return f.Set("environmentVariables", facet(std("1-0-0", "EnvironmentVariablesRunFacet"), pyjson.KV{Key: "environmentVariables", Value: vars}))
		})
	})
}

func tagList(tags pyjson.Object, source string) []any {
	out := []any{}
	for _, kv := range tags {
		out = append(out, pyjson.Object{{Key: "key", Value: kv.Key}, {Key: "value", Value: kv.Value}, {Key: "source", Value: source}})
	}
	return out
}

// tags is update_event_tags_facets: the job's tags of the configuration on a RunEvent's or a
// JobEvent's job, the run's (and the client's version) on a RunEvent's run.
func (c *libClient) tags(e pyjson.Object, isRun, isJob bool) pyjson.Object {
	if (isRun || isJob) && len(c.cfg.jobTags) > 0 {
		e = setIn(e, "job", func(job pyjson.Object) pyjson.Object {
			return setIn(job, "facets", func(f pyjson.Object) pyjson.Object {
				if f == nil {
					f = pyjson.Object{}
				}
				return f.Set("tags", facet(std("1-0-0", "TagsJobFacet"), pyjson.KV{Key: "tags", Value: tagList(c.cfg.jobTags, "USER")}))
			})
		})
	}
	if isRun {
		tags := append(tagList(c.cfg.runTags, "USER"), any(pyjson.Object{{Key: "key", Value: "openlineage_client_version"},
			{Key: "value", Value: ClientVersion}, {Key: "source", Value: "OPENLINEAGE_CLIENT"}}))
		e = setIn(e, "run", func(run pyjson.Object) pyjson.Object {
			return setIn(run, "facets", func(f pyjson.Object) pyjson.Object {
				if f == nil {
					f = pyjson.Object{}
				}
				return f.Set("tags", facet(std("1-0-0", "TagsRunFacet"), pyjson.KV{Key: "tags", Value: tags}))
			})
		})
	}
	return e
}

var githubPR = regexp.MustCompile(`^refs/pull/(\d+)/(?:merge|head)`)

// sourceCodeLocation is add_source_code_location_facet. The gateway runs outside any git
// repository: only what the configuration says, and the CI's pull request.
func (c *libClient) sourceCodeLocation(e pyjson.Object) pyjson.Object {
	s := c.cfg.scl
	if pyjson.Truthy(s.disabled) {
		return e
	}
	pr := s.prNumber
	if pr == nil {
		if v, _ := c.env.get("CI_MERGE_REQUEST_IID"); v != "" {
			pr = v
		} else if v, _ := c.env.get("GITHUB_REF"); v != "" {
			if m := githubPR.FindStringSubmatch(v); m != nil {
				pr = m[1]
			}
		}
	}
	if !pyjson.Truthy(s.repoURL) {
		return e
	}
	return setIn(e, "job", func(job pyjson.Object) pyjson.Object {
		return setIn(job, "facets", func(f pyjson.Object) pyjson.Object {
			if f == nil {
				f = pyjson.Object{}
			}
			if _, has := f.Get("sourceCodeLocation"); has {
				return f
			}
			fields := []pyjson.KV{{Key: "type", Value: "git"}, {Key: "url", Value: s.repoURL}, {Key: "repoUrl", Value: s.repoURL}}
			for _, kv := range []pyjson.KV{{Key: "version", Value: s.version}, {Key: "branch", Value: s.branch}, {Key: "tag", Value: s.tag},
				{Key: "pullRequestNumber", Value: pr}} {
				if kv.Value != nil {
					fields = append(fields, kv)
				}
			}
			return f.Set("sourceCodeLocation", facet(std("1-1-0", "SourceCodeLocationJobFacet"), fields...))
		})
	})
}

// libFilter is a FilterConfig and the Filter create_filter makes of it.
type libFilter struct {
	typ, match, regex any
	re                *regexp.Regexp
	kind              string
}

func (f *libFilter) compile(log *slog.Logger) {
	switch {
	case !pyjson.Truthy(f.typ):
		log.Warn("OpenLineage filter config must have a `type`.")
	case f.typ == "exact" && pyjson.Truthy(f.match):
		f.kind = "exact"
	case f.typ == "regex" && pyjson.Truthy(f.regex):
		// re.match: anchored at the start (Python's syntax, where RE2 reads it the same)
		if re, err := regexp.Compile(`^(?:` + pyjson.Str(f.regex) + `)`); err == nil {
			f.kind, f.re = "regex", re
		} else {
			log.Warn(fmt.Sprintf("OpenLineage filter regex not usable here: %v", err))
		}
	default:
		log.Warn(fmt.Sprintf("Unsupported OpenLineage filter type: `%s`", pyjson.Str(f.typ)))
	}
}

func (f *libFilter) drops(jobName string) bool {
	switch f.kind {
	case "exact":
		return pyjson.Str(f.match) == jobName
	case "regex":
		return f.re.MatchString(jobName)
	}
	return false
}
