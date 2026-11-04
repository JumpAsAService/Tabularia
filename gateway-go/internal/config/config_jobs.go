package config

import "fmt"

// JobsConfig is what the background work reads of the environment (app/main.py
// start_background_work and the services it starts). The panel may change several of
// these: the loops read them through internal/settings, which starts from these values.
type JobsConfig struct {
	// APP__ROLE (AppSettings.role): «all» answers and works, «api» only answers,
	// «orchestrator» only works (and answers /health)
	Role string

	// ORCHESTRATOR__* (OrchestratorSettings)
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
