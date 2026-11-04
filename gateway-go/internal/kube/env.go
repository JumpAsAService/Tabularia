// The gateway's view of Kubernetes as core/config.py KubernetesSettings and on_kubernetes
// describe it: whether it runs in a pod, and the switches of the chart the administration
// panels read (KUBERNETES__*).

package kube

import (
	"os"
	"strconv"
	"strings"
)

// Settings are the KUBERNETES__* values the ported routes read.
type Settings struct {
	Scaling     bool
	ClusterView bool
	TokenFile   string
}

// FromEnv reads them, with core/config.py's defaults.
func FromEnv() Settings {
	return Settings{
		Scaling:     envBool("KUBERNETES__SCALING", false),
		ClusterView: envBool("KUBERNETES__CLUSTER_VIEW", false),
		TokenFile:   envStr("KUBERNETES__TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"),
	}
}

func envStr(name, def string) string {
	if v, ok := os.LookupEnv(name); ok {
		return v
	}
	return def
}

// envBool is pydantic's bool from text (an unreadable one stops the Python gateway at
// start: here the default holds).
func envBool(name string, def bool) bool {
	v, ok := os.LookupEnv(name)
	if !ok {
		return def
	}
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "on", "t", "true", "y", "yes":
		return true
	case "0", "off", "f", "false", "n", "no":
		return false
	}
	if b, err := strconv.ParseBool(v); err == nil {
		return b
	}
	return def
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// OnKubernetes is core/config.py on_kubernetes: KUBERNETES_SERVICE_HOST set by the kubelet,
// or the service account's token mounted.
func (s Settings) OnKubernetes() bool {
	return os.Getenv("KUBERNETES_SERVICE_HOST") != "" || exists(s.TokenFile)
}

// ClusterDisabledReason is services/cluster_view.py disabled_reason: "kubernetes" off it,
// "off" when the chart's clusterView is off, "" when the page can show the cluster.
func (s Settings) ClusterDisabledReason() string {
	if !s.OnKubernetes() {
		return "kubernetes"
	}
	if !s.ClusterView || !exists(s.TokenFile) {
		return "off"
	}
	return ""
}
