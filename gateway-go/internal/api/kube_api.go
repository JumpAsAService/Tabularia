package api

import (
	"sync/atomic"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// kubePanel is the panel every call to the Kubernetes API reads its timeout from: set when
// the routes or the loops are made (Routes, Jobs).
var kubePanel atomic.Pointer[settings.Panel]

// kubeAPI is how the Kubernetes API is reached now (core/config.py KubernetesSettings):
// KUBERNETES__* from the environment, kubernetes.timeout_seconds as the panel has it — the
// one setting of it an administrator may change, which kube_jobs._call reads at every call.
func kubeAPI() scriptcfg.Kubernetes {
	k := scriptcfg.Get().Kubernetes
	if p := kubePanel.Load(); p != nil {
		k.TimeoutSeconds = p.KubernetesTimeout()
	}
	return k
}

// The panel's values of the scripting, notebooks and containers settings the routes read
// (overrides.ITEMS): one reader each, the panel's getter; the environment's when no panel is
// in force (a unit test that made no routes).

func scriptsLint() bool {
	if p := kubePanel.Load(); p != nil {
		return p.ScriptsLint()
	}
	return scriptcfg.Get().Scripts.Lint
}

// notebooksVisitors is (notebooks.max_visitors_per_app, notebooks.max_visitors_ceiling).
func notebooksVisitors() (int64, int64) {
	if p := kubePanel.Load(); p != nil {
		return p.NotebooksMaxVisitorsPerApp(), p.NotebooksMaxVisitorsCeiling()
	}
	n := scriptcfg.Get().Notebooks
	return int64(n.MaxVisitorsPerApp), int64(n.MaxVisitorsCeiling)
}

// containersPull is (containers.pull_check_seconds, containers.poll_seconds,
// containers.job_ttl_seconds).
func containersPull() (int64, float64, int64) {
	if p := kubePanel.Load(); p != nil {
		return p.ContainersPullCheck(), p.ContainersPoll(), p.ContainersJobTTL()
	}
	rt := scriptcfg.GetRuntime()
	return rt.ContainersPullCheck, rt.ContainersPoll, rt.ContainersJobTTL
}

// containersLimits is (containers.max_cpu, containers.max_memory, containers.max_timeout_seconds).
func containersLimits() (string, string, int64) {
	if p := kubePanel.Load(); p != nil {
		return p.ContainersMaxCPU(), p.ContainersMaxMemory(), p.ContainersMaxTimeout()
	}
	c := scriptcfg.Get().Containers
	return c.MaxCPU, c.MaxMemory, int64(c.MaxTimeoutSecond)
}

// scriptsJobTTL is scripts.job_ttl_seconds.
func scriptsJobTTL() int64 {
	if p := kubePanel.Load(); p != nil {
		return p.ScriptsJobTTL()
	}
	return scriptcfg.GetRuntime().ScriptsJobTTL
}
