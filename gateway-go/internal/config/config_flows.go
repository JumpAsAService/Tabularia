package config

import (
	"fmt"
	_ "time/tzdata" // the zones, whatever the image holds
)

// FlowsConfig is what the flows' routes read of the environment (gateway/app/core/config.py).
type FlowsConfig struct {
	// SCRIPTS__ENABLED, SCRIPTS__NAMESPACE, SCRIPTS__REGISTRY: script_builds.disabled_reason
	ScriptsEnabled   bool
	ScriptsNamespace string
	ScriptsRegistry  string
	// CONTAINERS__*: the Container node's checks
	ContainersEnabled    bool
	ContainersMaxCPU     string
	ContainersMaxMemory  string
	ContainersMaxTimeout int64
	// UPLOAD__MAX_BYTES (0 = no ceiling; the panel may change it), REPORTS__API_WAIT_SECONDS
	UploadMaxBytes        int64
	ReportsAPIWaitSeconds int64
	// SCHEDULING__WORKER_CAPACITY: the runs at once the schedules' load is measured on
	WorkerCapacity int64
	// ENGINE__RUN_STALE_TIMEOUT_SECONDS (CELERY__TASK_TIME_LIMIT + 300 by default),
	// ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS: the panel may change both
	RunStaleTimeout     int64
	QueueTimeoutSeconds float64
}

func loadFlows(r *reader) FlowsConfig {
	return FlowsConfig{
		ScriptsEnabled:        r.boolean("SCRIPTS__ENABLED", false),
		ScriptsNamespace:      r.str("SCRIPTS__NAMESPACE", ""),
		ScriptsRegistry:       r.str("SCRIPTS__REGISTRY", ""),
		ContainersEnabled:     r.boolean("CONTAINERS__ENABLED", false),
		ContainersMaxCPU:      r.str("CONTAINERS__MAX_CPU", "4"),
		ContainersMaxMemory:   r.str("CONTAINERS__MAX_MEMORY", "16Gi"),
		ContainersMaxTimeout:  int64(r.integer("CONTAINERS__MAX_TIMEOUT_SECONDS", 86400, 60, -1)),
		UploadMaxBytes:        int64(r.integer("UPLOAD__MAX_BYTES", 1<<30, 0, -1)),
		RunStaleTimeout:       int64(r.integer("ENGINE__RUN_STALE_TIMEOUT_SECONDS", r.integer("CELERY__TASK_TIME_LIMIT", 3600, -(1<<62), -(1<<62)-1)+300, -(1 << 62), -(1<<62)-1)),
		QueueTimeoutSeconds:   r.positive("ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS", 900),
		WorkerCapacity:        int64(r.integer("SCHEDULING__WORKER_CAPACITY", 2, -(1 << 62), -(1<<62)-1)),
		ReportsAPIWaitSeconds: int64(r.integer("REPORTS__API_WAIT_SECONDS", 120, 5, 3600)),
	}
}

// positive is a float greater than zero (pydantic's gt=0).
func (r *reader) positive(name string, def float64) float64 {
	f := r.float(name, def)
	if f <= 0 {
		r.fail(name, fmt.Sprintf("is not a number greater than 0: %v", f))
		return def
	}
	return f
}
