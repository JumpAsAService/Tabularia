package settings

import (
	"math/big"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The settings of Administration → Settings the background work reads (orchestration,
// history kept, backups, Kubernetes, scripting, notebooks, containers): each as get_settings()
// has it — the environment's value (config.PanelEnv) with, on top, the panel's when
// overrides.check keeps it (Check, the whole set at once, as apply_rows). One reader per key.

// inForce is the value of `key` in force: the panel's good one, else the environment's.
func (p *Panel) inForce(key string) any {
	v := p.current.Load()
	return Effective(p.panelEnv, v.good, key)
}

func (p *Panel) panelInt(key string) int64 {
	switch x := p.inForce(key).(type) {
	case int64:
		return x
	case *big.Int:
		if x.IsInt64() {
			return x.Int64()
		}
	case float64:
		return int64(x)
	}
	return 0
}

func (p *Panel) panelFloat(key string) float64 {
	switch x := p.inForce(key).(type) {
	case float64:
		return x
	case int64:
		return float64(x)
	}
	return 0
}

func (p *Panel) panelBool(key string) bool { b, _ := p.inForce(key).(bool); return b }

func (p *Panel) panelStr(key string) string { s, _ := p.inForce(key).(string); return s }

// goodValues is overrides.check of the rows put in force: the good ones.
func (p *Panel) goodValues(rows map[string]string) pyjson.Object {
	if !p.enabled || len(rows) == 0 {
		return nil
	}
	good, _ := Check(decodedRows(rows), p.panelEnv, nil)
	return good
}

// RefreshWaitSeconds is orchestrator.refresh_wait_seconds.
func (p *Panel) RefreshWaitSeconds() int64 { return p.panelInt("orchestrator.refresh_wait_seconds") }

// MaxConcurrent is orchestrator.max_concurrent.
func (p *Panel) MaxConcurrent() int64 { return p.panelInt("orchestrator.max_concurrent") }

// LogRetentionDays is orchestrator.log_retention_days (0 keeps them).
func (p *Panel) LogRetentionDays() int64 { return p.panelInt("orchestrator.log_retention_days") }

// ContractResultsKept is orchestrator.contract_results_kept (0 keeps them).
func (p *Panel) ContractResultsKept() int64 { return p.panelInt("orchestrator.contract_results_kept") }

// VersionsKept is orchestrator.versions_kept (0 keeps them).
func (p *Panel) VersionsKept() int64 { return p.panelInt("orchestrator.versions_kept") }

// AIChatRetentionDays is orchestrator.ai_chat_retention_days (0 keeps them).
func (p *Panel) AIChatRetentionDays() int64 { return p.panelInt("orchestrator.ai_chat_retention_days") }

// WebhooksRetentionDays is webhooks.retention_days.
func (p *Panel) WebhooksRetentionDays() int64 { return p.panelInt("webhooks.retention_days") }

// BackupTimeout is backup.timeout_seconds.
func (p *Panel) BackupTimeout() int64 { return p.panelInt("backup.timeout_seconds") }

// PollInterval is orchestrator.poll_interval_seconds.
func (p *Panel) PollInterval() float64 { return p.panelFloat("orchestrator.poll_interval_seconds") }

// QueuePoll is orchestrator.queue_poll_seconds.
func (p *Panel) QueuePoll() float64 { return p.panelFloat("orchestrator.queue_poll_seconds") }

// Heartbeat is orchestrator.heartbeat_seconds.
func (p *Panel) Heartbeat() float64 { return p.panelFloat("orchestrator.heartbeat_seconds") }

// DeadAfter is orchestrator.dead_after_seconds.
func (p *Panel) DeadAfter() float64 { return p.panelFloat("orchestrator.dead_after_seconds") }

// KubernetesTimeout is kubernetes.timeout_seconds: how long a request to the Kubernetes API
// may take.
func (p *Panel) KubernetesTimeout() float64 { return p.panelFloat("kubernetes.timeout_seconds") }

// ScriptsPollSeconds is scripts.poll_seconds.
func (p *Panel) ScriptsPollSeconds() float64 { return p.panelFloat("scripts.poll_seconds") }

// ScriptsJobTTL is scripts.job_ttl_seconds.
func (p *Panel) ScriptsJobTTL() int64 { return p.panelInt("scripts.job_ttl_seconds") }

// ScriptsLogMaxBytes is scripts.log_max_bytes.
func (p *Panel) ScriptsLogMaxBytes() int64 { return p.panelInt("scripts.log_max_bytes") }

// BuildTimeout is scripts.build_timeout_seconds.
func (p *Panel) BuildTimeout() int64 { return p.panelInt("scripts.build_timeout_seconds") }

// CheckTimeout is scripts.check_timeout_seconds.
func (p *Panel) CheckTimeout() int64 { return p.panelInt("scripts.check_timeout_seconds") }

// BuildCPU is scripts.build_cpu.
func (p *Panel) BuildCPU() string { return p.panelStr("scripts.build_cpu") }

// BuildMemory is scripts.build_memory.
func (p *Panel) BuildMemory() string { return p.panelStr("scripts.build_memory") }

// ScriptsLint is scripts.lint: the editor's checks of a Python script.
func (p *Panel) ScriptsLint() bool { return p.panelBool("scripts.lint") }

// NotebooksMaxVisitorsPerApp is notebooks.max_visitors_per_app.
func (p *Panel) NotebooksMaxVisitorsPerApp() int64 {
	return p.panelInt("notebooks.max_visitors_per_app")
}

// NotebooksMaxVisitorsCeiling is notebooks.max_visitors_ceiling.
func (p *Panel) NotebooksMaxVisitorsCeiling() int64 {
	return p.panelInt("notebooks.max_visitors_ceiling")
}

// ContainersPullCheck is containers.pull_check_seconds.
func (p *Panel) ContainersPullCheck() int64 { return p.panelInt("containers.pull_check_seconds") }

// ContainersPoll is containers.poll_seconds.
func (p *Panel) ContainersPoll() float64 { return p.panelFloat("containers.poll_seconds") }

// ContainersJobTTL is containers.job_ttl_seconds.
func (p *Panel) ContainersJobTTL() int64 { return p.panelInt("containers.job_ttl_seconds") }

// ContainersMaxCPU is containers.max_cpu.
func (p *Panel) ContainersMaxCPU() string { return p.panelStr("containers.max_cpu") }

// ContainersMaxMemory is containers.max_memory.
func (p *Panel) ContainersMaxMemory() string { return p.panelStr("containers.max_memory") }

// ContainersMaxTimeout is containers.max_timeout_seconds.
func (p *Panel) ContainersMaxTimeout() int64 { return p.panelInt("containers.max_timeout_seconds") }

// ContainersLogMaxBytes is containers.log_max_bytes.
func (p *Panel) ContainersLogMaxBytes() int64 { return p.panelInt("containers.log_max_bytes") }
