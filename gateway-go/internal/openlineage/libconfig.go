package openlineage

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The library's own configuration (openlineage-python's OpenLineageClient.config): the
// openlineage.yml file and the OPENLINEAGE__… variables, read when its client is built. It
// decides the transport when Tabularia's OPENLINEAGE__URL/OPENLINEAGE__FILE are not set, and
// the tags, the environment's facet, the source code's facet, the dataset names' reduction and
// the filters always.

// errConfig is the ValueError OpenLineageConfig.from_dict raises for a field it does not know.
var errConfig = errors.New("Failed to parse OpenLineage config.")

const envPrefix = "OPENLINEAGE__"

type libConfig struct {
	transport    any // config.transport: None, a dict…
	hasTransport bool

	envVars    []string // facets.environment_variables (a list)
	envVarsStr *string  // … when it is a text: `name in text` is a substring check
	scl        struct {
		disabled                                any
		repoURL, version, tag, branch, prNumber any
	}
	filters                         []libFilter
	jobTags                         pyjson.Object
	runTags                         pyjson.Object
	reducing                        any
	disabledTrimmers, extraTrimmers []string
}

// environ is os.environ as a map, in its order.
type environ struct {
	keys []string
	vals map[string]string
}

func readEnviron() environ {
	e := environ{vals: map[string]string{}}
	for _, kv := range os.Environ() {
		k, v, _ := strings.Cut(kv, "=")
		if _, dup := e.vals[k]; !dup {
			e.keys = append(e.keys, k)
		}
		e.vals[k] = v
	}
	return e
}

func (e environ) get(k string) (string, bool) { v, ok := e.vals[k]; return v, ok }

func (e *environ) set(k, v string) {
	if _, ok := e.vals[k]; !ok {
		e.keys = append(e.keys, k)
	}
	e.vals[k] = v
}

// aliasEnvVars is OpenLineageClient._alias_env_vars: OPENLINEAGE_URL (and its key and endpoint)
// written as the transport «default_http» of the dynamic configuration, unless one is there.
func aliasEnvVars(e *environ) {
	url, ok := e.get("OPENLINEAGE_URL")
	if !ok || url == "" {
		return
	}
	prefix := "OPENLINEAGE__TRANSPORT__TRANSPORTS__DEFAULT_HTTP"
	for _, k := range e.keys {
		if strings.HasPrefix(k, prefix) {
			return
		}
	}
	apiKey, _ := e.get("OPENLINEAGE_API_KEY")
	endpoint, _ := e.get("OPENLINEAGE_ENDPOINT")
	e.set(prefix+"__TYPE", "http")
	e.set(prefix+"__URL", url)
	auth, _ := pyjson.Dumps(pyjson.Object{{Key: "type", Value: "api_key"}, {Key: "apiKey", Value: apiKey}}, pyjson.Options{EnsureASCII: true})
	if apiKey != "" {
		e.set(prefix+"__AUTH", auth)
	}
	if endpoint != "" {
		e.set(prefix+"__ENDPOINT", endpoint)
	}
	if t, _ := e.get("OPENLINEAGE__TRANSPORT__TYPE"); t == "async_http" {
		if v, _ := e.get("OPENLINEAGE__TRANSPORT__URL"); v == "" {
			e.set("OPENLINEAGE__TRANSPORT__URL", url)
		}
		if v, _ := e.get("OPENLINEAGE__TRANSPORT__AUTH"); apiKey != "" && v == "" {
			e.set("OPENLINEAGE__TRANSPORT__AUTH", auth)
		}
		if v, _ := e.get("OPENLINEAGE__TRANSPORT__ENDPOINT"); endpoint != "" && v == "" {
			e.set("OPENLINEAGE__TRANSPORT__ENDPOINT", endpoint)
		}
	}
}

func pyTypeName(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case string:
		return "str"
	case bool:
		return "bool"
	case int64:
		return "int"
	case float64:
		return "float"
	case []any:
		return "list"
	case pyjson.Object:
		return "dict"
	}
	return "object"
}

// envConfig is _load_config_from_env_variables: every OPENLINEAGE__ variable, its name split on
// «__» and lower-cased, its value read as JSON when it is JSON; in reverse order of (name,
// value), so that a shorter name (an upper level) is written last and wins.
func envConfig(e environ, log *slog.Logger) (pyjson.Object, error) {
	type kv struct{ k, v string }
	var vars []kv
	for _, k := range e.keys {
		if strings.HasPrefix(k, envPrefix) {
			vars = append(vars, kv{k, e.vals[k]})
		}
	}
	sort.SliceStable(vars, func(i, j int) bool {
		if vars[i].k != vars[j].k {
			return vars[i].k > vars[j].k
		}
		return vars[i].v > vars[j].v
	})
	config := pyjson.Object{}
	for _, x := range vars {
		keys := strings.Split(x.k[len(envPrefix):], "__")
		var value any = x.v
		if v, err := pyjson.Loads(x.v); err == nil {
			value = v
		} else if strings.ContainsAny(x.v, "{}[]") {
			log.Warn(fmt.Sprintf("OpenLineage failed to parse value of environment variable `%s` as JSON: `%s`. Treating as plain string. Error: %v",
				x.k, x.v, err))
		}
		var err error
		if config, err = insertInto(config, keys, value); err != nil {
			return nil, err
		}
	}
	return config, nil
}

// insertInto is _insert_into_config.
func insertInto(config pyjson.Object, path []string, value any) (pyjson.Object, error) {
	key := strings.ToLower(path[0])
	if len(path) == 1 {
		return config.Set(key, value), nil
	}
	cur, has := config.Get(key)
	if !has {
		cur = pyjson.Object{}
	}
	sub, ok := cur.(pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("'%s' object does not support item assignment", pyTypeName(cur))
	}
	sub, err := insertInto(sub, path[1:], value)
	if err != nil {
		return nil, err
	}
	return config.Set(key, sub), nil
}

// deepMerge is deep_merge_dicts: b's values over a's, dicts merged level by level.
func deepMerge(a, b pyjson.Object) pyjson.Object {
	out := append(pyjson.Object{}, a...)
	for _, kv := range b {
		if old, has := out.Get(kv.Key); has {
			od, ok1 := old.(pyjson.Object)
			nd, ok2 := kv.Value.(pyjson.Object)
			if ok1 && ok2 {
				out = out.Set(kv.Key, deepMerge(od, nd))
				continue
			}
		}
		out = out.Set(kv.Key, kv.Value)
	}
	return out
}

// yamlConfigPath is _find_yaml_config_path: OPENLINEAGE_CONFIG, then openlineage.yml in the
// working directory, then in ~/.openlineage.
func yamlConfigPath(e environ) string {
	readable := func(p string) bool {
		st, err := os.Stat(p)
		if err != nil || !st.Mode().IsRegular() {
			return false
		}
		f, err := os.Open(p)
		if err != nil {
			return false
		}
		f.Close()
		return true
	}
	if p, _ := e.get("OPENLINEAGE_CONFIG"); p != "" && readable(p) {
		return p
	}
	if wd, err := os.Getwd(); err == nil {
		if p := filepath.Join(wd, "openlineage.yml"); readable(p) {
			return p
		}
	}
	home, _ := e.get("HOME")
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	if home != "" {
		if p := filepath.Join(home, ".openlineage", "openlineage.yml"); readable(p) {
			return p
		}
	}
	return ""
}

// loadLibConfig is OpenLineageClient.config (no user-defined configuration): the file under
// the variables (deep_merge_dicts(variables, file): the file's values win).
func loadLibConfig(e environ, log *slog.Logger) (*libConfig, error) {
	var cfg any = pyjson.Object{}
	if path := yamlConfigPath(e); path != "" {
		var content any
		raw, err := os.ReadFile(path)
		if err == nil {
			content, err = yamlSafeLoad(string(raw))
		}
		switch {
		case err != nil:
			log.Error(fmt.Sprintf("Couldn't read content of the OpenLineage config file `%s`: %v", path, err))
		case !pyjson.Truthy(content):
			log.Error(fmt.Sprintf("Empty OpenLineage config file: `%s`", path))
		default:
			if o, ok := content.(pyjson.Object); ok {
				cfg = deepMerge(o, pyjson.Object{})
			} else {
				cfg = content
			}
		}
	}
	env, err := envConfig(e, log)
	if err != nil {
		return nil, err
	}
	if len(env) > 0 {
		o, ok := cfg.(pyjson.Object)
		if !ok {
			return nil, fmt.Errorf("'%s' object has no attribute 'items'", pyTypeName(cfg))
		}
		cfg = deepMerge(env, o)
	}
	params, ok := cfg.(pyjson.Object)
	if !ok {
		return &libConfig{}, nil // a file that is not a mapping: no section found in it
	}
	return parseLibConfig(params)
}

func keysIn(o pyjson.Object, allowed ...string) bool {
	for _, kv := range o {
		found := false
		for _, a := range allowed {
			found = found || kv.Key == a
		}
		if !found {
			return false
		}
	}
	return true
}

func splitIntoList(v any) ([]string, error) {
	switch x := v.(type) {
	case nil:
		return nil, nil
	case []any:
		out := make([]string, 0, len(x))
		for _, s := range x {
			out = append(out, pyjson.Str(s))
		}
		return out, nil
	case string:
		var out []string
		for _, p := range strings.Split(x, ";") {
			if p = strings.TrimSpace(p); p != "" {
				out = append(out, p)
			}
		}
		return out, nil
	}
	return nil, fmt.Errorf("'%s' object has no attribute 'split'", pyTypeName(v))
}

// parseLibConfig is OpenLineageConfig.from_dict.
func parseLibConfig(params pyjson.Object) (*libConfig, error) {
	c := &libConfig{}
	c.scl.disabled = true
	c.reducing = false
	if t, ok := params.Get("transport"); ok {
		c.transport, c.hasTransport = t, true
	}
	if f, ok := params.Get("facets"); ok {
		facets, isDict := f.(pyjson.Object)
		if !isDict {
			return nil, errConfig
		}
		scl, _ := facets.Get("source_code_location")
		if !keysIn(facets, "environment_variables", "source_code_location") {
			return nil, errConfig
		}
		if pyjson.Truthy(scl) {
			s, isDict := scl.(pyjson.Object)
			if !isDict || !keysIn(s, "disabled", "repo_url", "version", "tag", "branch", "pull_request_number") {
				return nil, errConfig
			}
			if v, ok := s.Get("disabled"); ok {
				c.scl.disabled = v
			}
			c.scl.repoURL, c.scl.version, c.scl.tag = s.Value("repo_url"), s.Value("version"), s.Value("tag")
			c.scl.branch, c.scl.prNumber = s.Value("branch"), s.Value("pull_request_number")
		}
		if ev, ok := facets.Get("environment_variables"); ok {
			switch x := ev.(type) {
			case []any:
				for _, v := range x {
					c.envVars = append(c.envVars, pyjson.Str(v))
				}
			case string:
				c.envVarsStr = &x
			}
		}
	}
	if f, ok := params.Get("filters"); ok {
		list, isList := f.([]any)
		if !isList {
			return nil, errConfig
		}
		for _, x := range list {
			o, isDict := x.(pyjson.Object)
			if !isDict || !keysIn(o, "type", "match", "regex") {
				return nil, errConfig
			}
			c.filters = append(c.filters, libFilter{typ: o.Value("type"), match: o.Value("match"), regex: o.Value("regex")})
		}
	}
	if t, ok := params.Get("tags"); ok {
		tags, isDict := t.(pyjson.Object)
		if !isDict {
			return nil, fmt.Errorf("'%s' object has no attribute 'get'", pyTypeName(t))
		}
		for _, k := range []string{"job", "run"} {
			v, has := tags.Get(k)
			if !has {
				continue
			}
			o, isDict := v.(pyjson.Object)
			if !isDict {
				return nil, fmt.Errorf("'%s' object has no attribute 'items'", pyTypeName(v))
			}
			if k == "job" {
				c.jobTags = o
			} else {
				c.runTags = o
			}
		}
	}
	if d, ok := params.Get("dataset"); ok {
		ds, isDict := d.(pyjson.Object)
		if !isDict || !keysIn(ds, "reducing_enabled", "disabled_trimmers", "extra_trimmers") {
			return nil, errConfig
		}
		var err error
		if c.disabledTrimmers, err = splitIntoList(ds.Value("disabled_trimmers")); err != nil {
			return nil, err
		}
		if c.extraTrimmers, err = splitIntoList(ds.Value("extra_trimmers")); err != nil {
			return nil, err
		}
		if v, ok := ds.Get("reducing_enabled"); ok {
			c.reducing = v
		}
	}
	return c, nil
}
