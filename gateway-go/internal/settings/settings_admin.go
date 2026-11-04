package settings

import (
	"bytes"
	"encoding/json"
	"maps"
	"math"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// adminItem is an entry of overrides.ITEMS the administration's routes read (the audit,
// the backups, the AI): its kind, the panel's range and the environment's value.
type adminItem struct {
	kind     string // float | str | timezone | bool | int
	min, max float64
	hasRange bool
	maxLen   int // Item.max_length (200 by default)
	required bool
	env      func(*config.Config) any
}

var adminItems = map[string]adminItem{
	"mcp.enabled":                   {kind: "bool", env: func(c *config.Config) any { return c.Admin.MCPEnabled }},
	"mcp.confirm_in_tabularia_only": {kind: "bool", env: func(c *config.Config) any { return c.Admin.MCPConfirmInTabulariaOnly }},
	"mcp.metadata_only":             {kind: "bool", env: func(c *config.Config) any { return c.Admin.MCPMetadataOnly }},
	"ai.metadata_only":              {kind: "bool", env: func(c *config.Config) any { return c.Admin.AIMetadataOnly }},
	"ai.default_model":              {kind: "str", maxLen: 200, env: func(c *config.Config) any { return c.Admin.AIDefaultModel }},
	"ai.comment_model":              {kind: "str", maxLen: 200, env: func(c *config.Config) any { return c.Admin.AICommentModel }},
	"ai.max_result_rows": {kind: "int", min: 1, max: 2000, hasRange: true,
		env: func(c *config.Config) any { return int64(c.Admin.AIMaxResultRows) }},
	"webhooks.timeout_seconds": {kind: "float", min: 1, max: 60, hasRange: true,
		env: func(c *config.Config) any { return c.Admin.WebhooksTimeout }},
	"engine.dbt_export_timeout_seconds": {kind: "float", min: 10, max: 3600, hasRange: true,
		env: func(c *config.Config) any { return c.Admin.DbtExportTimeout }},
	"openlineage.timeout_seconds": {kind: "float", min: 1, max: 120, hasRange: true,
		env: func(c *config.Config) any { return c.Admin.OpenLineageTimeout }},
}

func adminEnv(cfg *config.Config) map[string]any {
	out := map[string]any{}
	for k, it := range adminItems {
		out[k] = it.env(cfg)
	}
	return out
}

// applyAdmin puts one of adminItems in force, when the row passes overrides.check_one.
func (p *Panel) applyAdmin(v *values, key, raw string) {
	it, ok := adminItems[key]
	if !ok {
		return
	}
	x, ok := checkAdmin(it, raw)
	if !ok {
		p.log.Warn("setting from the panel dropped, the environment's applies", "key", key)
		return
	}
	m := maps.Clone(v.admin) // the environment's map is never written
	m[key] = x
	v.admin = m
}

// checkAdmin is overrides.check_one for the kinds of adminItems.
func checkAdmin(it adminItem, raw string) (any, bool) {
	dec := json.NewDecoder(bytes.NewReader([]byte(raw)))
	dec.UseNumber()
	var x any
	if err := dec.Decode(&x); err != nil {
		return nil, false
	}
	switch it.kind {
	case "bool":
		b, ok := x.(bool)
		return b, ok
	case "int":
		n, ok := x.(json.Number)
		if !ok || strings.ContainsAny(n.String(), ".eE") {
			return nil, false
		}
		i, err := n.Int64()
		if err != nil || (it.hasRange && (float64(i) < it.min || float64(i) > it.max)) {
			return nil, false
		}
		return i, true
	case "float":
		n, ok := x.(json.Number)
		if !ok {
			return nil, false
		}
		f, err := n.Float64()
		if err != nil || math.IsInf(f, 0) || math.IsNaN(f) || (it.hasRange && (f < it.min || f > it.max)) {
			return nil, false
		}
		return f, true
	default: // str, timezone
		s, ok := x.(string)
		if !ok {
			return nil, false
		}
		s = strings.TrimFunc(s, unicode.IsSpace)
		if utf8.RuneCountInString(s) > it.maxLen {
			return nil, false
		}
		if (it.kind == "timezone" || it.required) && s == "" {
			return nil, false
		}
		if it.kind == "timezone" {
			if _, err := config.LoadZone(s); err != nil {
				return nil, false
			}
		}
		return s, true
	}
}

// Timezone is app.timezone: the deployment's zone, a ZoneInfo name. One value with
// AppTimezone (the panel's row is read once, in settings.go's items).
func (p *Panel) Timezone() string {
	if z := p.AppTimezone(); z != "" {
		return z
	}
	return "UTC"
}

// WebhooksTimeout is webhooks.timeout_seconds: how long a receiver is waited for.
func (p *Panel) WebhooksTimeout() float64 {
	if f, ok := p.current.Load().admin["webhooks.timeout_seconds"].(float64); ok && f > 0 {
		return f
	}
	return 10
}

// OpenLineageTimeout is openlineage.timeout_seconds: how long the lineage collector is waited for.
func (p *Panel) OpenLineageTimeout() float64 {
	if f, ok := p.current.Load().admin["openlineage.timeout_seconds"].(float64); ok && f > 0 {
		return f
	}
	return 10
}

func (p *Panel) adminBool(key string) bool { b, _ := p.current.Load().admin[key].(bool); return b }

func (p *Panel) adminStr(key string) string { s, _ := p.current.Load().admin[key].(string); return s }

// MCPEnabled is mcp.enabled: off, /mcp answers 404.
func (p *Panel) MCPEnabled() bool { return p.adminBool("mcp.enabled") }

// MCPConfirmInTabulariaOnly is mcp.confirm_in_tabularia_only.
func (p *Panel) MCPConfirmInTabulariaOnly() bool { return p.adminBool("mcp.confirm_in_tabularia_only") }

// MetadataOnly is metadata_only.on(origin): ai.metadata_only for the chat, mcp.metadata_only
// for MCP.
func (p *Panel) MetadataOnly(origin string) bool {
	if origin == "mcp" {
		return p.adminBool("mcp.metadata_only")
	}
	return p.adminBool("ai.metadata_only")
}

// AIDefaultModel is ai.default_model; AICommentModel is ai.comment_model.
func (p *Panel) AIDefaultModel() string { return p.adminStr("ai.default_model") }
func (p *Panel) AICommentModel() string { return p.adminStr("ai.comment_model") }

// AIMaxResultRows is ai.max_result_rows.
func (p *Panel) AIMaxResultRows() int {
	n, _ := p.current.Load().admin["ai.max_result_rows"].(int64)
	return int(n)
}

// DbtExportTimeout is engine.dbt_export_timeout_seconds: how long the engine is waited for to
// build a dbt project.
func (p *Panel) DbtExportTimeout() float64 {
	f, _ := p.current.Load().admin["engine.dbt_export_timeout_seconds"].(float64)
	return f
}
