package config

import (
	"fmt"
	"strconv"
	"strings"
	"time"
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
	// OpenLineage on: OPENLINEAGE__URL or __FILE, or the library's OPENLINEAGE_URL /
	// OPENLINEAGE_CONFIG (services/openlineage.py enabled)
	OpenLineage bool
	// UPLOAD__MAX_BYTES (0 = no ceiling; the panel may change it), REPORTS__API_WAIT_SECONDS
	UploadMaxBytes        int64
	ReportsAPIWaitSeconds int64
	// APP__TIMEZONE: the zone the cron expressions are read in (an IANA name);
	// SCHEDULING__WORKER_CAPACITY: the runs at once the schedules' load is measured on
	Timezone       string
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
		Timezone:              r.timezone("APP__TIMEZONE", "UTC"),
		RunStaleTimeout:       int64(r.integer("ENGINE__RUN_STALE_TIMEOUT_SECONDS", r.integer("CELERY__TASK_TIME_LIMIT", 3600, -(1<<62), -(1<<62)-1)+300, -(1 << 62), -(1<<62)-1)),
		QueueTimeoutSeconds:   r.float("ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS", 900),
		WorkerCapacity:        int64(r.integer("SCHEDULING__WORKER_CAPACITY", 2, -(1 << 62), -(1<<62)-1)),
		ReportsAPIWaitSeconds: int64(r.integer("REPORTS__API_WAIT_SECONDS", 120, 5, 3600)),
		OpenLineage: r.str("OPENLINEAGE__URL", "") != "" || r.str("OPENLINEAGE__FILE", "") != "" ||
			r.str("OPENLINEAGE_URL", "") != "" || r.str("OPENLINEAGE_CONFIG", "") != "",
	}
}

// timezone is AppSettings._valid_timezone: an IANA name, or the gateway does not start.
func (r *reader) timezone(name, def string) string {
	v := r.str(name, def)
	if !ValidZone(v) {
		r.fail(name, fmt.Sprintf("is not valid: '%s' — use an IANA name (e.g. 'Europe/Rome', 'UTC')", v))
		return def
	}
	return v
}

// ValidZone is ZoneInfo(name) not raising: an IANA name («Local» is Go's, not one).
func ValidZone(name string) bool {
	if name == "" || name == "Local" {
		return false
	}
	_, err := time.LoadLocation(name)
	return err == nil
}

// float is a number of the environment, greater than zero (pydantic's gt=0).
func (r *reader) float(name string, def float64) float64 {
	v, ok := r.env(name)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil || f <= 0 {
		r.fail(name, fmt.Sprintf("is not a number greater than 0: %q", v))
		return def
	}
	return f
}
