// Package config reads the gateway's environment: the variables the Python gateway reads
// (gateway/app/core/config.py), with the same names and defaults, plus the Go gateway's
// own GATEWAY_GO__*. Names are matched without regard to case, as pydantic-settings does.
package config

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Config is what the gateway needs today; it grows with the routes ported.
type Config struct {
	// GATEWAY_GO__LISTEN: where the Go gateway listens (the Python one's port in production)
	Listen string
	// GATEWAY_GO__UPSTREAM: the Python gateway that used to answer the routes not ported yet;
	// optional and unused (every route is Go's)
	Upstream *url.URL
	// GATEWAY_GO__AI_SERVICE: the AI service (the Python image with APP__ROLE=ai), which answers
	// the assistant's routes and the SQL parsing (docs/specs/gateway-go.md, «The AI service»);
	// the gateway does not start without it (cmd/gateway)
	AIService *url.URL
	// GATEWAY_GO__FORWARDED_ALLOW_IPS: the proxies whose X-Forwarded-For is believed, as
	// uvicorn's --forwarded-allow-ips (127.0.0.1 by default; «*» = any)
	ForwardedAllowIPs []string

	// DB__URL, or DB__HOST/PORT/USER/PASSWORD/NAME
	DSN string

	// JWT__SECRET, JWT__ALGORITHM
	JWTSecret    []byte
	JWTAlgorithm string

	// AUTH__SESSION_IDLE_MINUTES, AUTH__PERSONAL_TOKENS, AUTH__API_TOKEN_MAX_DAYS,
	// AUTH__COOKIE_DOMAIN: the starting values; an administrator may change the first three
	// from the panel (gateway_settings)
	SessionIdleMinutes int
	PersonalTokens     bool
	APITokenMaxDays    int
	CookieDomain       string

	// JWT__ACCESS_TTL_MINUTES: a session's maximum length (the panel may change it)
	AccessTTLMinutes int

	// AUTH__FIRST_RUN_SETUP and APP__ENV_NAME: the first administrator from the sign-in page,
	// never in production (services/first_run.py)
	FirstRunSetup bool
	EnvName       string

	// APP__NAME, APP__VERSION, APP__CODENAME, APP__TIMEZONE: what /system/info tells (the
	// time zone the panel may change)
	AppName, AppVersion, AppCodename string
	// PREVIEW__DEFAULT_SAMPLE_ROWS: the automatic development sample (the panel may change it)
	PreviewDefaultSampleRows int

	// MONITORING__NODE_EXPORTER_URL, MONITORING__TIMEOUT_SECONDS, MONITORING__CACHE_SECONDS:
	// the host's memory badge (/system/memory)
	NodeExporterURL                    string
	MonitoringTimeout, MonitoringCache float64

	// SCRIPTS__ENABLED, SCRIPTS__NAMESPACE, SCRIPTS__REGISTRY: whether the scripting
	// environments can be built here (script_builds.disabled_reason)
	ScriptsEnabled                    bool
	ScriptsNamespace, ScriptsRegistry string

	// OIDC__ISSUER and OIDC__CLIENT_ID (SSO on when both are set), OIDC__POST_LOGIN_URL,
	// OIDC__BUTTON_LABEL
	OIDCIssuer, OIDCClientID, OIDCPostLoginURL, OIDCButtonLabel string

	// AUDIT__RETENTION_MINUTES: how long the audit is kept (0 = forever), told by the privacy notice
	AuditRetentionMinutes int

	// ENGINE__SETTINGS_PANEL: false = the panel's values are ignored, the environment only
	SettingsPanel bool

	// ENGINE__BASE_URL, ENGINE__BUCKET: the engine, and its storage's bucket (only a string
	// in the payloads: the gateway never touches the storage)
	EngineBaseURL string
	EngineBucket  string

	// OpenLineage is services/openlineage.py enabled(): OPENLINEAGE__URL or OPENLINEAGE__FILE,
	// or the library's OPENLINEAGE_URL / OPENLINEAGE_CONFIG. The routes whose lineage events are
	// not ported yet leave those requests to the Python gateway while it is on.
	OpenLineage bool

	// SECURITY__FERNET_KEY, or the one created in SECURITY__KEYS_DIR (fernet.key)
	FernetKey string
	KeysDir   string

	// APP__TIMEZONE: the installation's day (a report opened «today», a new report's zone)
	AppTimezone string
	AppLocation *time.Location
	// REPORTS__API_WAIT_SECONDS: how long the API waits for a report's file (5–3600)
	ReportsAPIWaitSeconds int

	Admin AdminConfig // config_admin.go
	Flows FlowsConfig // config_flows.go
	Jobs  JobsConfig  // config_jobs.go
	// DataAPI: the data APIs' switch and caps (config_dataapi.go)
	DataAPI DataAPIConfig

	// PanelEnv: the environment's value of every setting of Administration → Settings
	// (overrides.ITEMS), by its key, as pydantic types it (config_panel.go)
	PanelEnv map[string]any
	// KUBERNETES__TOKEN_FILE: the service account's token; the panel shows the Kubernetes
	// settings only where it is mounted (routes/gateway_settings.py available)
	KubernetesTokenFile string
}

// Env reads a variable without regard to case, as pydantic-settings does.
type Env func(name string) (string, bool)

// FromOS is the process's environment.
func FromOS() Env {
	vars := map[string]string{}
	for _, kv := range os.Environ() {
		k, v, _ := strings.Cut(kv, "=")
		vars[strings.ToUpper(k)] = v
	}
	return func(name string) (string, bool) {
		v, ok := vars[strings.ToUpper(name)]
		return v, ok
	}
}

// Load reads the configuration; an unparsable or missing required value is an error that
// names the variable.
func Load(env Env) (*Config, error) {
	r := reader{env: env}
	c := &Config{
		Listen:                   r.str("GATEWAY_GO__LISTEN", ":8000"),
		ForwardedAllowIPs:        splitList(r.str("GATEWAY_GO__FORWARDED_ALLOW_IPS", "127.0.0.1")),
		JWTSecret:                []byte(r.str("JWT__SECRET", "change-me-in-production")),
		JWTAlgorithm:             r.str("JWT__ALGORITHM", "HS256"),
		SessionIdleMinutes:       r.integer("AUTH__SESSION_IDLE_MINUTES", 0, 0, -1),
		PersonalTokens:           r.boolean("AUTH__PERSONAL_TOKENS", true),
		APITokenMaxDays:          r.integer("AUTH__API_TOKEN_MAX_DAYS", 365, 1, 3650),
		CookieDomain:             r.str("AUTH__COOKIE_DOMAIN", ""),
		SettingsPanel:            r.boolean("ENGINE__SETTINGS_PANEL", true),
		AccessTTLMinutes:         r.integer("JWT__ACCESS_TTL_MINUTES", 12*60, math.MinInt, math.MaxInt),
		ScriptsEnabled:           r.boolean("SCRIPTS__ENABLED", false),
		ScriptsNamespace:         r.str("SCRIPTS__NAMESPACE", ""),
		ScriptsRegistry:          r.str("SCRIPTS__REGISTRY", ""),
		OIDCIssuer:               r.str("OIDC__ISSUER", ""),
		OIDCClientID:             r.str("OIDC__CLIENT_ID", ""),
		OIDCPostLoginURL:         r.str("OIDC__POST_LOGIN_URL", "http://localhost:3000/auth/callback"),
		OIDCButtonLabel:          r.str("OIDC__BUTTON_LABEL", "Single sign-on"),
		AuditRetentionMinutes:    r.integer("AUDIT__RETENTION_MINUTES", 0, 0, -1),
		FirstRunSetup:            r.boolean("AUTH__FIRST_RUN_SETUP", false),
		EnvName:                  r.str("APP__ENV_NAME", "development"),
		AppName:                  r.str("APP__NAME", "Tabularia"),
		AppVersion:               r.str("APP__VERSION", "1.4.2"),
		AppCodename:              r.str("APP__CODENAME", "Romulus"),
		AppTimezone:              r.str("APP__TIMEZONE", "UTC"),
		PreviewDefaultSampleRows: r.integer("PREVIEW__DEFAULT_SAMPLE_ROWS", 0, 0, -1),
		NodeExporterURL:          r.str("MONITORING__NODE_EXPORTER_URL", "http://node-exporter:9100/metrics"),
		MonitoringTimeout:        r.float("MONITORING__TIMEOUT_SECONDS", 3),
		MonitoringCache:          r.float("MONITORING__CACHE_SECONDS", 3),
		EngineBaseURL:            r.str("ENGINE__BASE_URL", "http://localhost:8000"),
		EngineBucket:             r.str("ENGINE__BUCKET", "data-prep"),
		FernetKey:                r.str("SECURITY__FERNET_KEY", ""),
		KeysDir:                  r.str("SECURITY__KEYS_DIR", ""),
		// ReportsSettings.api_wait_seconds
		ReportsAPIWaitSeconds: r.integer("REPORTS__API_WAIT_SECONDS", 120, 5, 3600),
	}
	// AppSettings._valid_timezone: a zone ZoneInfo knows
	if loc, err := time.LoadLocation(c.AppTimezone); err != nil || c.AppTimezone == "" || c.AppTimezone == "Local" {
		r.fail("APP__TIMEZONE", fmt.Sprintf("is not a time zone: %q", c.AppTimezone))
	} else {
		c.AppLocation = loc
	}
	c.Flows = loadFlows(&r)
	for _, name := range []string{"OPENLINEAGE__URL", "OPENLINEAGE__FILE", "OPENLINEAGE_URL", "OPENLINEAGE_CONFIG"} {
		if r.str(name, "") != "" {
			c.OpenLineage = true
		}
	}
	c.Admin = loadAdmin(&r)
	c.Jobs = loadJobs(&r)
	c.DataAPI = loadDataAPI(&r)
	c.PanelEnv = loadPanelEnv(&r)
	c.KubernetesTokenFile = r.str("KUBERNETES__TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")
	if c.FernetKey == "" && c.KeysDir != "" {
		// Settings._key_from_keys_dir: the key a first service created in the shared directory
		key, err := readOrCreate(c.KeysDir, "fernet.key", newFernetKey)
		if err != nil {
			r.fail("SECURITY__KEYS_DIR", err.Error())
		}
		c.FernetKey = key
	}
	if c.FernetKey == "" {
		// check_required_secrets: the gateway does not start without it
		r.fail("SECURITY__FERNET_KEY", "is not set: generate one with: openssl rand -base64 32 | tr '+/' '-_'")
	}
	// optional, and only read: no request goes to the Python gateway any more (cmd/gateway
	// says so when it is set)
	if upstream := r.str("GATEWAY_GO__UPSTREAM", ""); upstream != "" {
		if u, err := url.Parse(upstream); err != nil || u.Scheme == "" || u.Host == "" {
			r.fail("GATEWAY_GO__UPSTREAM", fmt.Sprintf("is not an address: %q", upstream))
		} else {
			c.Upstream = u
		}
	}
	if ai := r.str("GATEWAY_GO__AI_SERVICE", ""); ai != "" {
		if u, err := url.Parse(ai); err != nil || u.Scheme == "" || u.Host == "" {
			r.fail("GATEWAY_GO__AI_SERVICE", fmt.Sprintf("is not an address: %q", ai))
		} else {
			c.AIService = u
		}
	}
	c.DSN = dsn(&r)
	if c.JWTAlgorithm != "HS256" && c.JWTAlgorithm != "HS384" && c.JWTAlgorithm != "HS512" {
		r.fail("JWT__ALGORITHM", fmt.Sprintf("%q is not supported (HS256, HS384, HS512)", c.JWTAlgorithm))
	}
	if len(r.problems) > 0 {
		return nil, fmt.Errorf("configuration: %s", strings.Join(r.problems, "; "))
	}
	return c, nil
}

// DSN is the database's address alone, for a process that reads nothing else of the
// configuration (APP__ROLE=egress-proxy).
func DSN(env Env) string {
	r := reader{env: env}
	return dsn(&r)
}

// dsn is app/core/config.py's DbSettings.dsn: DB__URL wins; otherwise the parts, with user
// and password escaped (a generated password often holds «@», «/», «%» or a space).
func dsn(r *reader) string {
	if u := r.str("DB__URL", ""); u != "" {
		return u
	}
	u := url.URL{
		Scheme: "postgresql",
		User:   url.UserPassword(r.str("DB__USER", "tabularia"), r.str("DB__PASSWORD", "tabularia")),
		Host:   fmt.Sprintf("%s:%d", r.str("DB__HOST", "localhost"), r.integer("DB__PORT", 5432, 1, 65535)),
		Path:   "/" + r.str("DB__NAME", "tabularia"),
	}
	return u.String()
}

type reader struct {
	env      Env
	problems []string
}

func (r *reader) fail(name, why string) { r.problems = append(r.problems, name+" "+why) }

func (r *reader) str(name, def string) string {
	if v, ok := r.env(name); ok {
		return v
	}
	return def
}

// integer: max < min means no maximum (pydantic's `ge` alone)
func (r *reader) integer(name string, def, min, max int) int {
	v, ok := r.env(name)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil {
		r.fail(name, fmt.Sprintf("is not a whole number: %q", v))
		return def
	}
	if n < min || (max >= min && n > max) {
		r.fail(name, fmt.Sprintf("is out of range: %d", n))
		return def
	}
	return n
}

// float: a number as pydantic reads it
func (r *reader) float(name string, def float64) float64 {
	v, ok := r.env(name)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		r.fail(name, fmt.Sprintf("is not a number: %q", v))
		return def
	}
	return f
}

// SSOEnabled is OidcSettings.enabled: on only when configured.
func (c *Config) SSOEnabled() bool { return c.OIDCIssuer != "" && c.OIDCClientID != "" }

// IsProduction is Settings.is_production.
func (c *Config) IsProduction() bool {
	e := strings.ToLower(c.EnvName)
	return e == "production" || e == "prod"
}

// boolean: the spellings pydantic accepts
func (r *reader) boolean(name string, def bool) bool {
	v, ok := r.env(name)
	if !ok {
		return def
	}
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "on", "t", "true", "y", "yes":
		return true
	case "0", "off", "f", "false", "n", "no":
		return false
	}
	r.fail(name, fmt.Sprintf("is not true or false: %q", v))
	return def
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// readOrCreate is core/keys.py read_or_create: the secret in its file, created once by
// whichever service gets there first (published with a hard link, which fails if another
// process published first: its value wins).
func readOrCreate(dir, name string, make func() (string, error)) (string, error) {
	path := filepath.Join(dir, name)
	if b, err := os.ReadFile(path); err == nil && strings.TrimSpace(string(b)) != "" {
		return strings.TrimSpace(string(b)), nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("cannot write %s (%v): SECURITY__KEYS_DIR must be writable by the services", path, err)
	}
	value, err := make()
	if err != nil {
		return "", err
	}
	tmp := filepath.Join(dir, fmt.Sprintf(".%s.%d.tmp", name, os.Getpid()))
	if err := os.WriteFile(tmp, []byte(value+"\n"), 0o600); err != nil {
		return "", fmt.Errorf("cannot write %s (%v): SECURITY__KEYS_DIR must be writable by the services", path, err)
	}
	_ = os.Link(tmp, path)
	_ = os.Remove(tmp)
	b, err := os.ReadFile(path)
	if err != nil || strings.TrimSpace(string(b)) == "" {
		return "", fmt.Errorf("%s is empty: remove it and start again", path)
	}
	return strings.TrimSpace(string(b)), nil
}

// newFernetKey is Fernet.generate_key(): 32 random bytes, URL-safe base64.
func newFernetKey() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(raw), nil
}
