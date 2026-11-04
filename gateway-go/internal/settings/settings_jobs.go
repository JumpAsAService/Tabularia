package settings

import "github.com/JumpAsAService/Tabularia/gateway-go/internal/config"

// The entries of overrides.ITEMS the background work reads (orchestration, history kept,
// backups), with the panel's ranges: one reader per key, below.
func init() {
	const day = 86400
	ints := map[string][3]int64{ // min, max
		"orchestrator.refresh_wait_seconds":   {60, day},
		"orchestrator.max_concurrent":         {1, 200},
		"orchestrator.log_retention_days":     {0, 3650},
		"orchestrator.contract_results_kept":  {0, 100_000},
		"orchestrator.versions_kept":          {0, 10_000},
		"orchestrator.ai_chat_retention_days": {0, 3650},
		"webhooks.retention_days":             {1, 3650},
		"backup.timeout_seconds":              {60, day},
	}
	env := map[string]func(*config.Config) any{
		"orchestrator.refresh_wait_seconds":   func(c *config.Config) any { return c.Jobs.RefreshWaitSeconds },
		"orchestrator.max_concurrent":         func(c *config.Config) any { return c.Jobs.MaxConcurrent },
		"orchestrator.log_retention_days":     func(c *config.Config) any { return c.Jobs.LogRetentionDays },
		"orchestrator.contract_results_kept":  func(c *config.Config) any { return c.Jobs.ContractResultsKept },
		"orchestrator.versions_kept":          func(c *config.Config) any { return c.Jobs.VersionsKept },
		"orchestrator.ai_chat_retention_days": func(c *config.Config) any { return c.Jobs.AIChatRetentionDays },
		"webhooks.retention_days":             func(c *config.Config) any { return c.Jobs.WebhooksRetentionDays },
		"backup.timeout_seconds":              func(c *config.Config) any { return c.Jobs.BackupTimeout },
	}
	for k, r := range ints {
		adminItems[k] = adminItem{kind: "int", min: float64(r[0]), max: float64(r[1]), hasRange: true, env: env[k]}
	}
	floats := map[string]struct {
		min, max float64
		env      func(*config.Config) any
	}{
		"orchestrator.poll_interval_seconds": {0.5, 60, func(c *config.Config) any { return c.Jobs.PollInterval }},
		"orchestrator.queue_poll_seconds":    {0.2, 30, func(c *config.Config) any { return c.Jobs.QueuePoll }},
		"orchestrator.heartbeat_seconds":     {2, 120, func(c *config.Config) any { return c.Jobs.Heartbeat }},
		"orchestrator.dead_after_seconds":    {15, 3600, func(c *config.Config) any { return c.Jobs.DeadAfter }},
	}
	for k, f := range floats {
		adminItems[k] = adminItem{kind: "float", min: f.min, max: f.max, hasRange: true, env: f.env}
	}
}

func (p *Panel) adminInt(key string) int64 { n, _ := p.current.Load().admin[key].(int64); return n }

func (p *Panel) adminFloat(key string) float64 {
	f, _ := p.current.Load().admin[key].(float64)
	return f
}

// RefreshWaitSeconds is orchestrator.refresh_wait_seconds.
func (p *Panel) RefreshWaitSeconds() int64 { return p.adminInt("orchestrator.refresh_wait_seconds") }

// MaxConcurrent is orchestrator.max_concurrent.
func (p *Panel) MaxConcurrent() int64 { return p.adminInt("orchestrator.max_concurrent") }

// LogRetentionDays is orchestrator.log_retention_days (0 keeps them).
func (p *Panel) LogRetentionDays() int64 { return p.adminInt("orchestrator.log_retention_days") }

// ContractResultsKept is orchestrator.contract_results_kept (0 keeps them).
func (p *Panel) ContractResultsKept() int64 { return p.adminInt("orchestrator.contract_results_kept") }

// VersionsKept is orchestrator.versions_kept (0 keeps them).
func (p *Panel) VersionsKept() int64 { return p.adminInt("orchestrator.versions_kept") }

// AIChatRetentionDays is orchestrator.ai_chat_retention_days (0 keeps them).
func (p *Panel) AIChatRetentionDays() int64 { return p.adminInt("orchestrator.ai_chat_retention_days") }

// WebhooksRetentionDays is webhooks.retention_days.
func (p *Panel) WebhooksRetentionDays() int64 { return p.adminInt("webhooks.retention_days") }

// BackupTimeout is backup.timeout_seconds.
func (p *Panel) BackupTimeout() int64 { return p.adminInt("backup.timeout_seconds") }

// PollInterval is orchestrator.poll_interval_seconds.
func (p *Panel) PollInterval() float64 { return p.adminFloat("orchestrator.poll_interval_seconds") }

// QueuePoll is orchestrator.queue_poll_seconds.
func (p *Panel) QueuePoll() float64 { return p.adminFloat("orchestrator.queue_poll_seconds") }

// Heartbeat is orchestrator.heartbeat_seconds.
func (p *Panel) Heartbeat() float64 { return p.adminFloat("orchestrator.heartbeat_seconds") }

// DeadAfter is orchestrator.dead_after_seconds.
func (p *Panel) DeadAfter() float64 { return p.adminFloat("orchestrator.dead_after_seconds") }
