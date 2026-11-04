package scriptcfg

import (
	"os"
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
	return &Runtime{
		Rollout: rollout, Release: str("KUBERNETES__RELEASE", ""), AppName: str("KUBERNETES__APP_NAME", ""),
		Namespace:     str("KUBERNETES__NAMESPACE", ""),
		NamespaceFile: str("KUBERNETES__NAMESPACE_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/namespace"),
		Role:          str("APP__ROLE", "all"),
	}
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
