package config

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// JobsConfig is what the background work reads of the environment (app/main.py
// start_background_work and the services it starts). The panel may change several of
// these: the loops read them through internal/settings, which starts from these values.
type JobsConfig struct {
	// APP__ROLE (AppSettings.role): «all» answers and works, «api» only answers,
	// «orchestrator» only works (and answers /health)
	Role string

	// ORCHESTRATOR__* (OrchestratorSettings). These and the other settings the panel may change
	// are read here to refuse a value pydantic would (a start that fails as the Python one);
	// the values in force are internal/settings' (from PanelEnv, with the panel's on top)
	RefreshWaitSeconds  int64
	LogRetentionDays    int64
	ContractResultsKept int64
	VersionsKept        int64
	AIChatRetentionDays int64
	PollInterval        float64
	QueuePoll           float64
	MaxConcurrent       int64
	Heartbeat           float64
	DeadAfter           float64

	// AUDIT__IP_RETENTION_DAYS (AuditSettings.ip_retention_days); the whole-row retention is
	// Config.AuditRetentionMinutes
	IPRetentionDays int64

	// WEBHOOKS__RETENTION_DAYS (WebhookSettings.retention_days); the timeout is
	// Admin.WebhooksTimeout
	WebhooksRetentionDays int64

	// BACKUP__PG_DUMP, BACKUP__TIMEOUT_SECONDS (BackupSettings); enabled, schedule and keep
	// are in Admin
	BackupPgDump  string
	BackupTimeout int64

	// Kube is what the Kubernetes loops read (scaling, script builds, notebooks, settings
	// rollout) that internal/scriptcfg and internal/kube do not
	Kube KubeJobsConfig
}

// KubeJobsConfig are the KUBERNETES__*, SCRIPTS__* and NOTEBOOKS__* values the Kubernetes
// loops read, with core/config.py's defaults and bounds. Some are on the panel too
// (internal/settings, settings_jobs.go): these are then the environment's.
type KubeJobsConfig struct {
	// KubernetesSettings
	Namespace                string // empty = the pod's own (NamespaceFile)
	NamespaceFile            string
	RunWorkerDeployment      string
	RunWorkerAutoscaler      string // "hpa/<name>" | "scaledobject/<name>" | ""
	RunWorkerMaxReplicas     int64
	PreviewWorkerDeployment  string
	PreviewWorkerAutoscaler  string
	PreviewWorkerMaxReplicas int64
	Timeout                  float64 // kubernetes.timeout_seconds
	Rollout                  bool
	Release                  string
	AppName                  string

	// ScriptsSettings
	RegistryInsecure     bool
	RegistryPushSecret   string
	RegistryPullSecret   string
	RegistryCAConfigMap  string
	BuilderImage         string
	BuildTimeoutSeconds  int64   // scripts.build_timeout_seconds
	BuildCPU             string  // scripts.build_cpu
	BuildMemory          string  // scripts.build_memory
	CheckTimeoutSeconds  int64   // scripts.check_timeout_seconds
	ScriptsPollSeconds   float64 // scripts.poll_seconds
	ScriptsLogMaxBytes   int64   // scripts.log_max_bytes
	ScriptsJobTTLSeconds int64   // scripts.job_ttl_seconds

	ScriptsLint bool // scripts.lint

	// NotebooksSettings
	NotebooksIdleMinutes        int64
	NotebooksStartTimeout       int64
	NotebooksMaxVisitorsPerApp  int64 // notebooks.max_visitors_per_app
	NotebooksMaxVisitorsCeiling int64 // notebooks.max_visitors_ceiling

	// ContainersSettings (max_cpu, max_memory, max_timeout_seconds are Flows')
	ContainersPullCheck   int64   // containers.pull_check_seconds
	ContainersPoll        float64 // containers.poll_seconds
	ContainersJobTTL      int64   // containers.job_ttl_seconds
	ContainersLogMaxBytes int64   // containers.log_max_bytes
}

var (
	// ScriptsSettings._build_cpu and _build_memory
	buildCPU    = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?m?$`)
	buildMemory = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$`)
)

// ValidBuildCPU is ScriptsSettings._build_cpu: a CPU quantity above zero (stripped).
func ValidBuildCPU(v string) bool {
	v = strings.TrimSpace(v)
	if !buildCPU.MatchString(v) {
		return false
	}
	f, err := strconv.ParseFloat(strings.TrimRight(v, "m"), 64)
	return err == nil && f > 0
}

// ValidBuildMemory is ScriptsSettings._build_memory.
func ValidBuildMemory(v string) bool { return buildMemory.MatchString(strings.TrimSpace(v)) }

func loadKubeJobs(r *reader) KubeJobsConfig {
	k := KubeJobsConfig{
		Namespace:                   r.str("KUBERNETES__NAMESPACE", ""),
		NamespaceFile:               r.str("KUBERNETES__NAMESPACE_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/namespace"),
		RunWorkerDeployment:         r.str("KUBERNETES__RUN_WORKER_DEPLOYMENT", ""),
		RunWorkerAutoscaler:         r.str("KUBERNETES__RUN_WORKER_AUTOSCALER", ""),
		RunWorkerMaxReplicas:        int64(r.integer("KUBERNETES__RUN_WORKER_MAX_REPLICAS", 10, 1, 0)),
		PreviewWorkerDeployment:     r.str("KUBERNETES__PREVIEW_WORKER_DEPLOYMENT", ""),
		PreviewWorkerAutoscaler:     r.str("KUBERNETES__PREVIEW_WORKER_AUTOSCALER", ""),
		PreviewWorkerMaxReplicas:    int64(r.integer("KUBERNETES__PREVIEW_WORKER_MAX_REPLICAS", 8, 1, 0)),
		Timeout:                     r.float("KUBERNETES__TIMEOUT_SECONDS", 10),
		Rollout:                     r.boolean("KUBERNETES__ROLLOUT", false),
		Release:                     r.str("KUBERNETES__RELEASE", ""),
		AppName:                     r.str("KUBERNETES__APP_NAME", ""),
		RegistryInsecure:            r.boolean("SCRIPTS__REGISTRY_INSECURE", false),
		RegistryPushSecret:          r.str("SCRIPTS__REGISTRY_PUSH_SECRET", ""),
		RegistryPullSecret:          r.str("SCRIPTS__REGISTRY_PULL_SECRET", ""),
		RegistryCAConfigMap:         r.str("SCRIPTS__REGISTRY_CA_CONFIGMAP", ""),
		BuilderImage:                r.str("SCRIPTS__BUILDER_IMAGE", "moby/buildkit:v0.33.1-rootless"),
		BuildTimeoutSeconds:         int64(r.integer("SCRIPTS__BUILD_TIMEOUT_SECONDS", 3600, 60, 0)),
		BuildCPU:                    strings.TrimSpace(r.str("SCRIPTS__BUILD_CPU", "2")),
		BuildMemory:                 strings.TrimSpace(r.str("SCRIPTS__BUILD_MEMORY", "4Gi")),
		CheckTimeoutSeconds:         int64(r.integer("SCRIPTS__CHECK_TIMEOUT_SECONDS", 300, 30, 0)),
		ScriptsPollSeconds:          r.float("SCRIPTS__POLL_SECONDS", 5),
		ScriptsLogMaxBytes:          int64(r.integer("SCRIPTS__LOG_MAX_BYTES", 262_144, 4096, 0)),
		ScriptsJobTTLSeconds:        int64(r.integer("SCRIPTS__JOB_TTL_SECONDS", 3600, 60, 0)),
		NotebooksIdleMinutes:        int64(r.integer("NOTEBOOKS__IDLE_MINUTES", 15, 1, 24*60)),
		NotebooksStartTimeout:       int64(r.integer("NOTEBOOKS__START_TIMEOUT_SECONDS", 300, 30, 3600)),
		ScriptsLint:                 r.boolean("SCRIPTS__LINT", true),
		NotebooksMaxVisitorsPerApp:  int64(r.integer("NOTEBOOKS__MAX_VISITORS_PER_APP", 10, 1, 1000)),
		NotebooksMaxVisitorsCeiling: int64(r.integer("NOTEBOOKS__MAX_VISITORS_CEILING", 50, 1, 1000)),
		ContainersPullCheck:         int64(r.integer("CONTAINERS__PULL_CHECK_SECONDS", 300, 30, 0)),
		ContainersPoll:              r.float("CONTAINERS__POLL_SECONDS", 3),
		ContainersJobTTL:            int64(r.integer("CONTAINERS__JOB_TTL_SECONDS", 3600, 60, 0)),
		ContainersLogMaxBytes:       int64(r.integer("CONTAINERS__LOG_MAX_BYTES", 262_144, 4096, 0)),
	}
	if k.ContainersPoll <= 0 {
		r.fail("CONTAINERS__POLL_SECONDS", "must be greater than 0")
		k.ContainersPoll = 3
	}
	if k.Timeout <= 0 {
		r.fail("KUBERNETES__TIMEOUT_SECONDS", "must be greater than 0")
		k.Timeout = 10
	}
	if k.ScriptsPollSeconds <= 0 {
		r.fail("SCRIPTS__POLL_SECONDS", "must be greater than 0")
		k.ScriptsPollSeconds = 5
	}
	if !ValidBuildCPU(k.BuildCPU) {
		r.fail("SCRIPTS__BUILD_CPU", "is a Kubernetes CPU quantity: 2, 1.5 or 1500m")
		k.BuildCPU = "2"
	}
	if !ValidBuildMemory(k.BuildMemory) {
		r.fail("SCRIPTS__BUILD_MEMORY", "is a Kubernetes memory quantity: 4Gi, 6144Mi")
		k.BuildMemory = "4Gi"
	}
	return k
}

// Roles are AppSettings.role's Literal.
var Roles = []string{"all", "api", "orchestrator"}

// Answers says whether this process serves the API (every role but «orchestrator»).
func (j JobsConfig) Answers() bool { return j.Role != "orchestrator" }

// Works says whether this process does the background work (every role but «api»).
func (j JobsConfig) Works() bool { return j.Role != "api" }

func loadJobs(r *reader) JobsConfig {
	j := JobsConfig{
		Role:                  r.str("APP__ROLE", "all"),
		RefreshWaitSeconds:    int64(r.integer("ORCHESTRATOR__REFRESH_WAIT_SECONDS", 600, -(1 << 62), -(1<<62)-1)),
		LogRetentionDays:      int64(r.integer("ORCHESTRATOR__LOG_RETENTION_DAYS", 30, 0, -1)),
		ContractResultsKept:   int64(r.integer("ORCHESTRATOR__CONTRACT_RESULTS_KEPT", 100, 0, -1)),
		VersionsKept:          int64(r.integer("ORCHESTRATOR__VERSIONS_KEPT", 50, 0, -1)),
		AIChatRetentionDays:   int64(r.integer("ORCHESTRATOR__AI_CHAT_RETENTION_DAYS", 90, 0, -1)),
		PollInterval:          r.float("ORCHESTRATOR__POLL_INTERVAL_SECONDS", 3.0),
		QueuePoll:             r.positive("ORCHESTRATOR__QUEUE_POLL_SECONDS", 1.0),
		MaxConcurrent:         int64(r.integer("ORCHESTRATOR__MAX_CONCURRENT", 20, 1, -1)),
		Heartbeat:             r.positive("ORCHESTRATOR__HEARTBEAT_SECONDS", 10.0),
		DeadAfter:             r.positive("ORCHESTRATOR__DEAD_AFTER_SECONDS", 60.0),
		IPRetentionDays:       int64(r.integer("AUDIT__IP_RETENTION_DAYS", 90, 0, -1)),
		WebhooksRetentionDays: int64(r.integer("WEBHOOKS__RETENTION_DAYS", 30, 1, -1)),
		BackupPgDump:          r.str("BACKUP__PG_DUMP", "pg_dump"),
		BackupTimeout:         int64(r.integer("BACKUP__TIMEOUT_SECONDS", 3600, 60, -1)),
		Kube:                  loadKubeJobs(r),
	}
	known := false
	for _, role := range Roles {
		known = known || j.Role == role
	}
	if !known {
		// a Literal pydantic refuses: the Python gateway does not start either
		r.fail("APP__ROLE", fmt.Sprintf("is not one of 'all', 'api', 'orchestrator': %q", j.Role))
	}
	return j
}
