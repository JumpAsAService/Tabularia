package config

import (
	"fmt"
	"strconv"
	"strings"
)

// panelEnvSpec is one setting of overrides.ITEMS as the environment gives it: its variable's
// default and how pydantic reads it (core/config.py, the field of its section).
type panelEnvSpec struct {
	key  string
	kind string // int | float | bool | str | optint
	def  any    // int64, float64, bool, string or nil, as the Python default (an int stays an int)
	// strip: the field's validator returns v.strip() (scripts.build_cpu, containers.max_cpu…)
	strip bool
}

// panelEnvSpecs are the settings Administration → Settings may change (overrides.ITEMS), plus
// oidc.superuser_group, which one of the panel's constraints reads.
var panelEnvSpecs = []panelEnvSpec{
	{key: "jwt.access_ttl_minutes", kind: "int", def: int64(12 * 60)},
	{key: "auth.session_idle_minutes", kind: "int", def: int64(0)},
	{key: "auth.personal_tokens", kind: "bool", def: true},
	{key: "auth.api_token_max_days", kind: "int", def: int64(365)},
	{key: "oidc.groups_claim", kind: "str", def: "groups"},
	{key: "oidc.group_allowlist", kind: "str", def: ""},
	{key: "oidc.auto_create_groups", kind: "bool", def: false},
	{key: "oidc.authoritative", kind: "bool", def: true},
	{key: "oidc.allowed_email_domains", kind: "str", def: ""},
	{key: "oidc.require_allowlisted_group", kind: "bool", def: false},
	{key: "oidc.button_label", kind: "str", def: "Single sign-on"},
	{key: "oidc.discovery_ttl_seconds", kind: "int", def: int64(3600)},
	{key: "oidc.timeout_seconds", kind: "float", def: 10.0},
	{key: "oidc.login_tx_ttl_seconds", kind: "int", def: int64(600)},
	{key: "oidc.superuser_group", kind: "str", def: ""},
	{key: "app.timezone", kind: "str", def: "UTC"},
	{key: "engine.run_stale_timeout_seconds", kind: "int", def: nil}, // CELERY__TASK_TIME_LIMIT + 300
	{key: "orchestrator.refresh_wait_seconds", kind: "int", def: int64(600)},
	{key: "orchestrator.poll_interval_seconds", kind: "float", def: 3.0},
	{key: "orchestrator.queue_poll_seconds", kind: "float", def: 1.0},
	{key: "orchestrator.max_concurrent", kind: "int", def: int64(20)},
	{key: "orchestrator.queue_timeout_seconds", kind: "float", def: 900.0},
	{key: "orchestrator.heartbeat_seconds", kind: "float", def: 10.0},
	{key: "orchestrator.dead_after_seconds", kind: "float", def: 60.0},
	{key: "scheduling.worker_capacity", kind: "int", def: int64(2)},
	{key: "orchestrator.log_retention_days", kind: "int", def: int64(30)},
	{key: "orchestrator.contract_results_kept", kind: "int", def: int64(100)},
	{key: "orchestrator.versions_kept", kind: "int", def: int64(50)},
	{key: "orchestrator.ai_chat_retention_days", kind: "int", def: int64(90)},
	{key: "webhooks.retention_days", kind: "int", def: int64(30)},
	{key: "webhooks.timeout_seconds", kind: "float", def: 10.0},
	{key: "ai.metadata_only", kind: "bool", def: false},
	{key: "ai.default_model", kind: "str", def: ""},
	{key: "ai.max_result_rows", kind: "int", def: int64(200)},
	{key: "ai.max_requests", kind: "int", def: int64(12)},
	{key: "ai.max_flow_requests", kind: "int", def: int64(30)},
	{key: "ai.max_history_messages", kind: "int", def: int64(60)},
	{key: "ai.max_cost_per_turn_usd", kind: "float", def: 0.5},
	// Field(default=0) on a float: pydantic does not validate a default, it stays an int
	{key: "ai.max_cost_per_day_usd", kind: "float", def: int64(0)},
	{key: "ai.max_cost_per_day_total_usd", kind: "float", def: int64(0)},
	{key: "ai.max_input_tokens_per_turn", kind: "int", def: int64(400_000)},
	{key: "ai.max_chats_listed", kind: "int", def: int64(100)},
	{key: "ai.dbt_export_timeout_seconds", kind: "float", def: int64(180)},
	{key: "ai.comment_model", kind: "str", def: ""},
	{key: "ai.comment_keep_seconds", kind: "int", def: int64(86_400)},
	{key: "ai.comment_min_seconds", kind: "int", def: int64(3600)},
	{key: "ai.comment_wait_seconds", kind: "int", def: int64(30)},
	{key: "mcp.enabled", kind: "bool", def: true},
	{key: "mcp.confirm_in_tabularia_only", kind: "bool", def: false},
	{key: "mcp.metadata_only", kind: "bool", def: false},
	{key: "engine.dbt_export_timeout_seconds", kind: "float", def: int64(100)},
	{key: "preview.default_sample_rows", kind: "int", def: int64(0)},
	{key: "upload.max_bytes", kind: "int", def: int64(1 << 30)},
	{key: "backup.timeout_seconds", kind: "int", def: int64(3600)},
	{key: "monitoring.timeout_seconds", kind: "float", def: 3.0},
	{key: "monitoring.cache_seconds", kind: "float", def: 3.0},
	{key: "openlineage.timeout_seconds", kind: "float", def: 10.0},
	{key: "app.gateway_threads", kind: "int", def: int64(40)},
	{key: "db.pool_size", kind: "optint", def: nil},
	{key: "db.max_overflow", kind: "int", def: int64(10)},
	{key: "kubernetes.timeout_seconds", kind: "float", def: 10.0},
	{key: "scripts.poll_seconds", kind: "float", def: 5.0},
	{key: "scripts.job_ttl_seconds", kind: "int", def: int64(3600)},
	{key: "scripts.log_max_bytes", kind: "int", def: int64(262_144)},
	{key: "scripts.lint", kind: "bool", def: true},
	{key: "scripts.build_timeout_seconds", kind: "int", def: int64(3600)},
	{key: "scripts.build_cpu", kind: "str", def: "2", strip: true},
	{key: "scripts.build_memory", kind: "str", def: "4Gi", strip: true},
	{key: "scripts.check_timeout_seconds", kind: "int", def: int64(300)},
	{key: "notebooks.max_visitors_per_app", kind: "int", def: int64(10)},
	{key: "notebooks.max_visitors_ceiling", kind: "int", def: int64(50)},
	{key: "containers.pull_check_seconds", kind: "int", def: int64(300)},
	{key: "containers.poll_seconds", kind: "float", def: 3.0},
	{key: "containers.job_ttl_seconds", kind: "int", def: int64(3600)},
	{key: "containers.max_cpu", kind: "str", def: "4", strip: true},
	{key: "containers.max_memory", kind: "str", def: "16Gi", strip: true},
	{key: "containers.max_timeout_seconds", kind: "int", def: int64(86400)},
	{key: "containers.log_max_bytes", kind: "int", def: int64(262_144)},
}

// loadPanelEnv is settings_from_env() for the settings of the panel: key → the environment's
// value with the type pydantic gives it (int64, float64, bool, string, nil). The ranges are the
// readers' of each setting (config.go, config_admin.go…); here only the form.
func loadPanelEnv(r *reader) map[string]any {
	out := map[string]any{}
	for _, s := range panelEnvSpecs {
		section, field, _ := strings.Cut(s.key, ".")
		name := strings.ToUpper(section) + "__" + strings.ToUpper(field)
		def := s.def
		if s.key == "engine.run_stale_timeout_seconds" {
			def = int64(r.integer("CELERY__TASK_TIME_LIMIT", 3600, -(1<<62), -(1<<62)-1)) + 300
		}
		raw, ok := r.env(name)
		if !ok {
			out[s.key] = def
			continue
		}
		v := strings.TrimSpace(raw)
		switch s.kind {
		case "str":
			if s.strip {
				out[s.key] = v
			} else {
				out[s.key] = raw
			}
		case "bool":
			out[s.key] = r.boolean(name, def.(bool))
		case "int", "optint":
			if v == "" {
				out[s.key] = def
				continue
			}
			n, err := strconv.ParseInt(v, 10, 64)
			if err != nil {
				r.fail(name, fmt.Sprintf("is not a whole number: %q", raw))
				out[s.key] = def
				continue
			}
			out[s.key] = n
		case "float":
			if v == "" {
				out[s.key] = def
				continue
			}
			f, err := strconv.ParseFloat(v, 64)
			if err != nil {
				r.fail(name, fmt.Sprintf("is not a number: %q", raw))
				out[s.key] = def
				continue
			}
			out[s.key] = f
		}
	}
	return out
}

// PanelEnvOf is the environment's values of the panel's settings alone (the tests').
func PanelEnvOf(env Env) (map[string]any, error) {
	r := reader{env: env}
	out := loadPanelEnv(&r)
	if len(r.problems) > 0 {
		return nil, fmt.Errorf("configuration: %s", strings.Join(r.problems, "; "))
	}
	return out, nil
}
