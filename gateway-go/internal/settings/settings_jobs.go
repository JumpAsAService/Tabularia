package settings

import (
	"math"
	"regexp"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

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

// The entries of overrides.ITEMS the Kubernetes loops read (kubernetes.*, scripts.*): the
// panel's ranges, then the setting's own validation (ScriptsSettings' validators), which
// the getters below run for the CPU and memory quantities.
func init() {
	const day = 86400
	ints := map[string]struct {
		min, max int64
		env      func(*config.Config) any
	}{
		"scripts.job_ttl_seconds":       {60, 7 * day, func(c *config.Config) any { return c.Jobs.Kube.ScriptsJobTTLSeconds }},
		"scripts.log_max_bytes":         {4096, 16 * 1024 * 1024, func(c *config.Config) any { return c.Jobs.Kube.ScriptsLogMaxBytes }},
		"scripts.build_timeout_seconds": {60, 6 * 3600, func(c *config.Config) any { return c.Jobs.Kube.BuildTimeoutSeconds }},
		"scripts.check_timeout_seconds": {30, 3600, func(c *config.Config) any { return c.Jobs.Kube.CheckTimeoutSeconds }},
	}
	for k, r := range ints {
		adminItems[k] = adminItem{kind: "int", min: float64(r.min), max: float64(r.max), hasRange: true, env: r.env}
	}
	more := map[string]struct {
		min, max int64
		env      func(*config.Config) any
	}{
		"notebooks.max_visitors_per_app": {1, 1000, func(c *config.Config) any { return c.Jobs.Kube.NotebooksMaxVisitorsPerApp }},
		"notebooks.max_visitors_ceiling": {1, 1000, func(c *config.Config) any { return c.Jobs.Kube.NotebooksMaxVisitorsCeiling }},
		"containers.pull_check_seconds":  {30, 3600, func(c *config.Config) any { return c.Jobs.Kube.ContainersPullCheck }},
		"containers.job_ttl_seconds":     {60, 7 * day, func(c *config.Config) any { return c.Jobs.Kube.ContainersJobTTL }},
		"containers.max_timeout_seconds": {60, 30 * day, func(c *config.Config) any { return c.Flows.ContainersMaxTimeout }},
		"containers.log_max_bytes":       {4096, 16 * 1024 * 1024, func(c *config.Config) any { return c.Jobs.Kube.ContainersLogMaxBytes }},
	}
	for k, r := range more {
		adminItems[k] = adminItem{kind: "int", min: float64(r.min), max: float64(r.max), hasRange: true, env: r.env}
	}
	adminItems["containers.poll_seconds"] = adminItem{kind: "float", min: 1, max: 60, hasRange: true,
		env: func(c *config.Config) any { return c.Jobs.Kube.ContainersPoll }}
	adminItems["scripts.lint"] = adminItem{kind: "bool", env: func(c *config.Config) any { return c.Jobs.Kube.ScriptsLint }}
	adminItems["containers.max_cpu"] = adminItem{kind: "cpu", maxLen: 200, required: true,
		env: func(c *config.Config) any { return c.Flows.ContainersMaxCPU }}
	adminItems["containers.max_memory"] = adminItem{kind: "memory", maxLen: 200, required: true,
		env: func(c *config.Config) any { return c.Flows.ContainersMaxMemory }}
	adminItems["kubernetes.timeout_seconds"] = adminItem{kind: "float", min: 1, max: 120, hasRange: true,
		env: func(c *config.Config) any { return c.Jobs.Kube.Timeout }}
	adminItems["scripts.poll_seconds"] = adminItem{kind: "float", min: 1, max: 60, hasRange: true,
		env: func(c *config.Config) any { return c.Jobs.Kube.ScriptsPollSeconds }}
	// a text, stripped, not empty: the quantity itself is checked by the getter
	adminItems["scripts.build_cpu"] = adminItem{kind: "cpu", maxLen: 200, required: true,
		env: func(c *config.Config) any { return c.Jobs.Kube.BuildCPU }}
	adminItems["scripts.build_memory"] = adminItem{kind: "memory", maxLen: 200, required: true,
		env: func(c *config.Config) any { return c.Jobs.Kube.BuildMemory }}
}

// KubernetesTimeout is kubernetes.timeout_seconds: how long a request to the Kubernetes API
// may take.
func (p *Panel) KubernetesTimeout() float64 { return p.adminFloat("kubernetes.timeout_seconds") }

// ScriptsPollSeconds is scripts.poll_seconds: the image builds' round.
func (p *Panel) ScriptsPollSeconds() float64 { return p.adminFloat("scripts.poll_seconds") }

// ScriptsJobTTL is scripts.job_ttl_seconds.
func (p *Panel) ScriptsJobTTL() int64 { return p.adminInt("scripts.job_ttl_seconds") }

// ScriptsLogMaxBytes is scripts.log_max_bytes.
func (p *Panel) ScriptsLogMaxBytes() int64 { return p.adminInt("scripts.log_max_bytes") }

// BuildTimeout is scripts.build_timeout_seconds.
func (p *Panel) BuildTimeout() int64 { return p.adminInt("scripts.build_timeout_seconds") }

// CheckTimeout is scripts.check_timeout_seconds.
func (p *Panel) CheckTimeout() int64 { return p.adminInt("scripts.check_timeout_seconds") }

var (
	// cluster_view._CPU and _MEMORY: the sizes overrides.check_one measures
	cpuQuantity    = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)(n|u|m)?$`)
	memoryQuantity = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|k|M|G|T|e[0-9]+)?$`)
)

// minMemory is overrides.MIN_MEMORY.
const minMemory = 64 * 1024 * 1024

// BuildCPU is scripts.build_cpu: the panel's when it is more than zero and a CPU quantity
// (overrides.check_one, ScriptsSettings._build_cpu), else the environment's.
func (p *Panel) BuildCPU() string { return p.cpu("scripts.build_cpu") }

// ContainersMaxCPU is containers.max_cpu (ContainersSettings._cpu: the same form).
func (p *Panel) ContainersMaxCPU() string { return p.cpu("containers.max_cpu") }

func (p *Panel) cpu(key string) string {
	v := p.adminStr(key)
	if m := cpuQuantity.FindStringSubmatch(v); m != nil {
		f, _ := strconv.ParseFloat(m[1], 64)
		if f <= 0 { // too_small
			return p.env.admin[key].(string)
		}
	}
	if !config.ValidBuildCPU(v) {
		return p.env.admin[key].(string)
	}
	return v
}

// BuildMemory is scripts.build_memory: the panel's when it is at least 64Mi and a memory
// quantity, else the environment's.
func (p *Panel) BuildMemory() string { return p.memory("scripts.build_memory") }

// ContainersMaxMemory is containers.max_memory (ContainersSettings._memory: the same form).
func (p *Panel) ContainersMaxMemory() string { return p.memory("containers.max_memory") }

func (p *Panel) memory(key string) string {
	v := p.adminStr(key)
	if m := memoryQuantity.FindStringSubmatch(v); m != nil {
		f, _ := strconv.ParseFloat(m[1], 64)
		factor := map[string]float64{"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
		size := f
		if strings.HasPrefix(m[2], "e") {
			e, _ := strconv.Atoi(m[2][1:])
			size = f * math.Pow(10, float64(e))
		} else if x, ok := factor[m[2]]; ok {
			size = f * x
		}
		if math.Trunc(size) < minMemory {
			return p.env.admin[key].(string)
		}
	}
	if !config.ValidBuildMemory(v) {
		return p.env.admin[key].(string)
	}
	return v
}

// ScriptsLint is scripts.lint: the editor's checks of a Python script.
func (p *Panel) ScriptsLint() bool { return p.adminBool("scripts.lint") }

// NotebooksMaxVisitorsPerApp is notebooks.max_visitors_per_app.
func (p *Panel) NotebooksMaxVisitorsPerApp() int64 {
	return p.adminInt("notebooks.max_visitors_per_app")
}

// NotebooksMaxVisitorsCeiling is notebooks.max_visitors_ceiling.
func (p *Panel) NotebooksMaxVisitorsCeiling() int64 {
	return p.adminInt("notebooks.max_visitors_ceiling")
}

// ContainersPullCheck is containers.pull_check_seconds.
func (p *Panel) ContainersPullCheck() int64 { return p.adminInt("containers.pull_check_seconds") }

// ContainersPoll is containers.poll_seconds.
func (p *Panel) ContainersPoll() float64 { return p.adminFloat("containers.poll_seconds") }

// ContainersJobTTL is containers.job_ttl_seconds.
func (p *Panel) ContainersJobTTL() int64 { return p.adminInt("containers.job_ttl_seconds") }

// ContainersMaxTimeout is containers.max_timeout_seconds.
func (p *Panel) ContainersMaxTimeout() int64 { return p.adminInt("containers.max_timeout_seconds") }

// ContainersLogMaxBytes is containers.log_max_bytes.
func (p *Panel) ContainersLogMaxBytes() int64 { return p.adminInt("containers.log_max_bytes") }
