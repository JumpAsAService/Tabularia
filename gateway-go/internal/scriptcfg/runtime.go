package scriptcfg

import (
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// Runtime is the rest of KubernetesSettings and AppSettings the runtime routes read (the
// settings' rollout, the notebooks' pods): read once, as get_settings() caches them.
type Runtime struct {
	// KUBERNETES__ROLLOUT, KUBERNETES__RELEASE, KUBERNETES__APP_NAME: the pods the panel restarts
	Rollout bool
	Release string
	AppName string
	// KUBERNETES__NAMESPACE (empty: the pod's own, from KUBERNETES__NAMESPACE_FILE)
	Namespace     string
	NamespaceFile string
	// APP__ROLE: all | api | orchestrator | ai
	Role string
	// NOTEBOOKS__MAX_LIVE, NOTEBOOKS__START_TIMEOUT_SECONDS, NOTEBOOKS__MAX_LIFETIME_HOURS,
	// NOTEBOOKS__GATEWAY_URL: the notebooks' pods
	NotebooksMaxLive, NotebooksStartTimeout, NotebooksMaxLifetimeHours int64
	NotebooksGatewayURL                                                string
	// SCRIPTS__REGISTRY_PULL_SECRET, SCRIPTS__JOB_TTL_SECONDS: what the pods' Jobs carry
	ScriptsPullSecret string
	ScriptsJobTTL     int64
	// CONTAINERS__PULL_CHECK_SECONDS, CONTAINERS__POLL_SECONDS, CONTAINERS__JOB_TTL_SECONDS:
	// the registries' «Try in the cluster»
	ContainersPullCheck int64
	ContainersPoll      float64
	ContainersJobTTL    int64
	// SCRIPTS__EGRESS_PROXY, SCRIPTS__EGRESS_PORTS, SCRIPTS__EGRESS_NO_PROXY: the egress proxy
	// of the versions without internet access (internal/egress, the chart's
	// templates/egress-proxy.yaml): its address as HTTPS_PROXY ("" = none), the storage
	// ports its Service passes on transparently, and what goes around it besides the
	// cluster's own names
	EgressProxy   string
	EgressPorts   []int
	EgressNoProxy []string
}

var (
	rtOnce sync.Once
	rt     *Runtime
)

// GetRuntime is the process's Runtime.
func GetRuntime() *Runtime {
	rtOnce.Do(func() { rt = LoadRuntime(config.FromOS()) })
	return rt
}

// LoadRuntime reads it from `env`, with core/config.py's defaults.
func LoadRuntime(env config.Env) *Runtime {
	str := func(name, def string) string {
		if v, ok := env(name); ok {
			return v
		}
		return def
	}
	rollout := false
	if v, ok := env("KUBERNETES__ROLLOUT"); ok {
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "1", "true", "yes", "on", "t", "y":
			rollout = true
		}
	}
	integer := func(name string, def int64) int64 {
		if v, ok := env(name); ok {
			if n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64); err == nil {
				return n
			}
		}
		return def
	}
	number := func(name string, def float64) float64 {
		if v, ok := env(name); ok {
			if f, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
				return f
			}
		}
		return def
	}
	return &Runtime{
		ContainersPullCheck: integer("CONTAINERS__PULL_CHECK_SECONDS", 300), ContainersPoll: number("CONTAINERS__POLL_SECONDS", 3),
		ContainersJobTTL: integer("CONTAINERS__JOB_TTL_SECONDS", 3600),
		NotebooksMaxLive: integer("NOTEBOOKS__MAX_LIVE", 20), NotebooksStartTimeout: integer("NOTEBOOKS__START_TIMEOUT_SECONDS", 300),
		NotebooksMaxLifetimeHours: integer("NOTEBOOKS__MAX_LIFETIME_HOURS", 12), NotebooksGatewayURL: str("NOTEBOOKS__GATEWAY_URL", ""),
		ScriptsPullSecret: str("SCRIPTS__REGISTRY_PULL_SECRET", ""), ScriptsJobTTL: integer("SCRIPTS__JOB_TTL_SECONDS", 3600),
		Rollout: rollout, Release: str("KUBERNETES__RELEASE", ""), AppName: str("KUBERNETES__APP_NAME", ""),
		Namespace:     str("KUBERNETES__NAMESPACE", ""),
		NamespaceFile: str("KUBERNETES__NAMESPACE_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/namespace"),
		Role:          str("APP__ROLE", "all"),
		EgressProxy:   strings.TrimSpace(str("SCRIPTS__EGRESS_PROXY", "")),
		EgressPorts:   ports(str("SCRIPTS__EGRESS_PORTS", "")),
		EgressNoProxy: list(str("SCRIPTS__EGRESS_NO_PROXY", "")),
	}
}

// ports is "443,80,9000": the ports that parse.
func ports(s string) []int {
	var out []int
	for _, p := range list(s) {
		if n, err := strconv.Atoi(p); err == nil && n > 0 && n < 65536 {
			out = append(out, n)
		}
	}
	return out
}

// list is a comma-separated list, without empty items.
func list(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// ReleaseNamespace is cluster_view.release_namespace: KUBERNETES__NAMESPACE, or the pod's
// own from its service account ("" when neither).
func (r *Runtime) ReleaseNamespace() string {
	if r.Namespace != "" {
		return r.Namespace
	}
	b, err := os.ReadFile(r.NamespaceFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// ReloadRuntime reads the Runtime again from the process's environment (see Reload).
func ReloadRuntime() {
	rtOnce.Do(func() {})
	rt = LoadRuntime(config.FromOS())
}
