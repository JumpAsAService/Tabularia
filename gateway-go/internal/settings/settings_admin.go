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
	"app.timezone": {kind: "timezone", maxLen: 200, env: func(c *config.Config) any { return c.Admin.Timezone }},
	"webhooks.timeout_seconds": {kind: "float", min: 1, max: 60, hasRange: true,
		env: func(c *config.Config) any { return c.Admin.WebhooksTimeout }},
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

// Timezone is app.timezone: the deployment's zone, a ZoneInfo name.
func (p *Panel) Timezone() string {
	if z, ok := p.current.Load().admin["app.timezone"].(string); ok {
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
