// Package scriptcfg reads the settings of the scripting environments, the container
// registries, the notebooks and the Kubernetes API (app/core/config.py ScriptsSettings,
// ContainersSettings, NotebooksSettings, KubernetesSettings), with the same names and
// defaults. Read once, from the process's environment, as get_settings() caches them.
package scriptcfg

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"sync"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// Scripts is ScriptsSettings.
type Scripts struct {
	Enabled        bool
	Namespace      string
	TrialNamespace string
	BuildNamespace string
	Registry       string
	BaseImages     string
	PyPIIndexURL   string
	CRANURL        string
	Lint           bool
}

// Containers is ContainersSettings, what the routes read.
type Containers struct {
	Enabled          bool
	Namespace        string
	TrialNamespace   string
	MaxCPU           string
	MaxMemory        string
	MaxTimeoutSecond int
}

// Notebooks is NotebooksSettings, what the routes read.
type Notebooks struct {
	MaxVisitorsPerApp  int
	MaxVisitorsCeiling int
	Origin             string
	AllowSameOrigin    bool
	PodIPCheck         bool
}

// Kubernetes is KubernetesSettings: how the API is reached.
type Kubernetes struct {
	APIURL         string
	TokenFile      string
	CAFile         string
	TimeoutSeconds float64
}

// App is the part of AppSettings the notebooks read.
type App struct {
	CORSOrigins []string // APP__CORS_ORIGINS, a JSON list
	RootPath    string   // APP__ROOT_PATH, as its validator writes it ("/api" or "")
}

// Settings is everything above.
type Settings struct {
	App        App
	Scripts    Scripts
	Containers Containers
	Notebooks  Notebooks
	Kubernetes Kubernetes
}

var (
	once    sync.Once
	current *Settings
)

// Get is the process's settings.
func Get() *Settings {
	once.Do(func() { current = Load(config.FromOS()) })
	return current
}

// Load reads them from `env`. A value that does not parse keeps its default: the Python
// gateway would not have started, and the Go one runs behind it.
func Load(env config.Env) *Settings {
	str := func(name, def string) string {
		if v, ok := env(name); ok {
			return v
		}
		return def
	}
	boolean := func(name string, def bool) bool {
		if v, ok := env(name); ok {
			switch strings.ToLower(strings.TrimSpace(v)) {
			case "1", "true", "yes", "on", "t", "y":
				return true
			case "0", "false", "no", "off", "f", "n":
				return false
			}
		}
		return def
	}
	integer := func(name string, def int) int {
		if v, ok := env(name); ok {
			if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
				return n
			}
		}
		return def
	}
	number := func(name string, def float64) float64 {
		if v, ok := env(name); ok {
			if n, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
				return n
			}
		}
		return def
	}
	cors := []string{"http://localhost:3000", "http://127.0.0.1:3000"}
	if v, ok := env("APP__CORS_ORIGINS"); ok {
		var list []string
		if json.Unmarshal([]byte(v), &list) == nil {
			cors = list
		}
	}
	root := strings.Trim(strings.TrimSpace(str("APP__ROOT_PATH", "")), "/")
	if root != "" {
		root = "/" + root
	}
	return &Settings{
		App: App{CORSOrigins: cors, RootPath: root},
		Scripts: Scripts{
			Enabled: boolean("SCRIPTS__ENABLED", false), Namespace: str("SCRIPTS__NAMESPACE", ""),
			TrialNamespace: str("SCRIPTS__TRIAL_NAMESPACE", ""), BuildNamespace: str("SCRIPTS__BUILD_NAMESPACE", ""),
			Registry: str("SCRIPTS__REGISTRY", ""), BaseImages: str("SCRIPTS__BASE_IMAGES", ""),
			PyPIIndexURL: str("SCRIPTS__PYPI_INDEX_URL", ""), CRANURL: str("SCRIPTS__CRAN_URL", "https://cran.r-project.org"),
			Lint: boolean("SCRIPTS__LINT", true),
		},
		Containers: Containers{
			Enabled: boolean("CONTAINERS__ENABLED", false), Namespace: str("CONTAINERS__NAMESPACE", ""),
			TrialNamespace: str("CONTAINERS__TRIAL_NAMESPACE", ""),
			MaxCPU:         strings.TrimSpace(str("CONTAINERS__MAX_CPU", "4")), MaxMemory: strings.TrimSpace(str("CONTAINERS__MAX_MEMORY", "16Gi")),
			MaxTimeoutSecond: integer("CONTAINERS__MAX_TIMEOUT_SECONDS", 86400),
		},
		Notebooks: Notebooks{
			MaxVisitorsPerApp: integer("NOTEBOOKS__MAX_VISITORS_PER_APP", 10), MaxVisitorsCeiling: integer("NOTEBOOKS__MAX_VISITORS_CEILING", 50),
			Origin: normalOrigin(str("NOTEBOOKS__ORIGIN", "")), AllowSameOrigin: boolean("NOTEBOOKS__ALLOW_SAME_ORIGIN", false),
			PodIPCheck: boolean("NOTEBOOKS__POD_IP_CHECK", true),
		},
		Kubernetes: Kubernetes{
			APIURL:         str("KUBERNETES__API_URL", "https://kubernetes.default.svc"),
			TokenFile:      str("KUBERNETES__TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
			CAFile:         str("KUBERNETES__CA_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
			TimeoutSeconds: number("KUBERNETES__TIMEOUT_SECONDS", 10),
		},
	}
}

// DisabledReason is script_builds.disabled_reason: why the scripting environments cannot
// be built here ("kubernetes" | "configuration"), "" when they can.
func (s *Settings) DisabledReason() string {
	if !s.Scripts.Enabled {
		return "kubernetes"
	}
	if s.Scripts.Namespace == "" || s.Scripts.Registry == "" {
		return "configuration"
	}
	return ""
}

// Image is one language version offered, and its base image.
type Image struct{ Version, Image string }

// DefaultBaseImages is DEFAULT_SCRIPT_BASE_IMAGES, in its order.
var DefaultBaseImages = map[string][]Image{
	"python": {{"3.11", "python:3.11-slim"}, {"3.12", "python:3.12-slim"}, {"3.13", "python:3.13-slim"}, {"3.14", "python:3.14-slim"}},
	"r":      {{"4.5", "rocker/r-ver:4.5.3"}, {"4.6", "rocker/r-ver:4.6.0"}},
}

// Images is ScriptsSettings.images(): {language: [version → image]} in the order given; an
// error when SCRIPTS__BASE_IMAGES is not JSON (a ValueError in Python).
func (s *Settings) Images() (map[string][]Image, error) {
	if strings.TrimSpace(s.Scripts.BaseImages) == "" {
		return DefaultBaseImages, nil
	}
	dec := json.NewDecoder(strings.NewReader(s.Scripts.BaseImages))
	var parsed map[string]json.RawMessage
	if err := dec.Decode(&parsed); err != nil {
		return nil, fmt.Errorf("SCRIPTS__BASE_IMAGES is not valid JSON (%v)", err)
	}
	out := map[string][]Image{}
	for _, lang := range []string{"python", "r"} {
		out[lang] = []Image{}
		raw, ok := parsed[lang]
		if !ok || string(raw) == "null" {
			continue
		}
		d := json.NewDecoder(strings.NewReader(string(raw)))
		t, err := d.Token()
		if err != nil || t != json.Delim('{') {
			continue
		}
		for d.More() {
			k, _ := d.Token()
			var v any
			_ = d.Decode(&v)
			out[lang] = append(out[lang], Image{Version: fmt.Sprint(k), Image: fmt.Sprint(v)})
		}
	}
	return out, nil
}

// normalOrigin is NotebooksSettings._an_origin: scheme://host[:port], the default port left
// out. The Python gateway refuses to start with any other value.
func normalOrigin(v string) string {
	v = strings.TrimRight(strings.TrimSpace(v), "/")
	if v == "" {
		return ""
	}
	scheme, rest, ok := strings.Cut(v, "://")
	if !ok {
		return v
	}
	scheme = strings.ToLower(scheme)
	host, port, hasPort := strings.Cut(rest, ":")
	if strings.HasPrefix(rest, "[") {
		if i := strings.Index(rest, "]"); i > 0 {
			host, port = rest[:i+1], strings.TrimPrefix(rest[i+1:], ":")
			hasPort = port != ""
		}
	}
	host = strings.ToLower(host)
	def := map[string]string{"https": "443", "http": "80"}[scheme]
	if hasPort {
		if p, err := strconv.Atoi(port); err == nil {
			port = strconv.Itoa(p)
		}
		if port != def {
			return scheme + "://" + host + ":" + port
		}
	}
	return scheme + "://" + host
}

// Reload reads the process's settings again from its environment: cmd/jobsonce, whose rounds
// may set variables of their own (an installation where a feature is on).
func Reload() {
	once.Do(func() {})
	current = Load(config.FromOS())
}
