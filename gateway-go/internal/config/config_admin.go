package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	_ "time/tzdata" // ZoneInfo's names, whatever the image has in /usr/share/zoneinfo

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
)

// AdminConfig is what the administration's routes read of the environment
// (gateway/app/core/config.py): the audit, the SIEM, the webhooks, the backups, the AI.
type AdminConfig struct {
	// APP__TIMEZONE: the deployment's zone (AppSettings.timezone, a ZoneInfo name); the
	// panel may change it (app.timezone)
	Timezone string
	// APP__VERSION: the release (AppSettings.version)
	Version string
	// WEBHOOKS__TIMEOUT_SECONDS: how long a receiver (a webhook's, Splunk's) is waited for
	WebhooksTimeout float64
	// BACKUP__ENABLED, BACKUP__SCHEDULE, BACKUP__KEEP: the installation's backup defaults
	BackupEnabled  bool
	BackupSchedule string
	BackupKeep     int
	// on_kubernetes(): KUBERNETES_SERVICE_HOST, or the service account's token
	// (KUBERNETES__TOKEN_FILE) mounted
	OnKubernetes bool
	// AI__BASE_URL, AI__SECRET_KEY: the environment's AI provider (on when both are set)
	AIBaseURL, AISecretKey string
	// MCP__ENABLED, MCP__CONFIRM_IN_TABULARIA_ONLY, MCP__METADATA_ONLY, AI__METADATA_ONLY:
	// the starting values; the panel may change them
	MCPEnabled, MCPConfirmInTabulariaOnly, MCPMetadataOnly, AIMetadataOnly bool
	// AI__DEFAULT_MODEL, AI__COMMENT_MODEL (the panel may change them)
	AIDefaultModel, AICommentModel string
	// AI__MAX_RESULT_ROWS: the rows an assistant query returns (the panel may change it)
	AIMaxResultRows int
	// APP__CORS_ORIGINS: the interface's origins (a JSON list)
	CORSOrigins []string
}

func loadAdmin(r *reader) AdminConfig {
	c := AdminConfig{
		Timezone: r.str("APP__TIMEZONE", "UTC"),
		Version:  r.str("APP__VERSION", "1.3.0"),
	}
	c.WebhooksTimeout = r.adminFloat("WEBHOOKS__TIMEOUT_SECONDS", 10, 0, 60)
	c.BackupEnabled = r.boolean("BACKUP__ENABLED", true)
	c.BackupKeep = r.integer("BACKUP__KEEP", 14, 1, 1000)
	c.BackupSchedule = strings.TrimSpace(r.str("BACKUP__SCHEDULE", "0 3 * * *"))
	if !cron.IsValid(c.BackupSchedule) {
		r.fail("BACKUP__SCHEDULE", fmt.Sprintf("is not a cron expression: %q", c.BackupSchedule))
	}
	_, token := os.Stat(r.str("KUBERNETES__TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"))
	c.OnKubernetes = r.str("KUBERNETES_SERVICE_HOST", "") != "" || token == nil
	c.AIBaseURL, c.AISecretKey = r.str("AI__BASE_URL", ""), r.str("AI__SECRET_KEY", "")
	c.MCPEnabled = r.boolean("MCP__ENABLED", true)
	c.MCPConfirmInTabulariaOnly = r.boolean("MCP__CONFIRM_IN_TABULARIA_ONLY", false)
	c.MCPMetadataOnly = r.boolean("MCP__METADATA_ONLY", false)
	c.AIMetadataOnly = r.boolean("AI__METADATA_ONLY", false)
	c.AIDefaultModel, c.AICommentModel = r.str("AI__DEFAULT_MODEL", ""), r.str("AI__COMMENT_MODEL", "")
	c.AIMaxResultRows = r.integer("AI__MAX_RESULT_ROWS", 200, 1, 2000)
	c.CORSOrigins = []string{"http://localhost:3000", "http://127.0.0.1:3000"}
	if v, ok := r.env("APP__CORS_ORIGINS"); ok {
		if err := json.Unmarshal([]byte(v), &c.CORSOrigins); err != nil {
			r.fail("APP__CORS_ORIGINS", "is not a JSON list of addresses")
		}
	}
	if _, err := LoadZone(c.Timezone); err != nil {
		// AppSettings._valid_timezone: the gateway does not start with a zone ZoneInfo lacks
		r.fail("APP__TIMEZONE", fmt.Sprintf("is not a known time zone: %q", c.Timezone))
	}
	return c
}

// LoadZone is zoneinfo.ZoneInfo(name): an IANA name; Go's own spellings («Local», «») are
// not ZoneInfo's.
func LoadZone(name string) (*time.Location, error) {
	if name == "" || name == "Local" {
		return nil, fmt.Errorf("no such zone: %q", name)
	}
	return time.LoadLocation(name)
}

// adminFloat reads a number in (min, max] (pydantic's gt and le), as pydantic's float from a str.
func (r *reader) adminFloat(name string, def, gt, le float64) float64 {
	v, ok := r.env(name)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		r.fail(name, fmt.Sprintf("is not a number: %q", v))
		return def
	}
	if !(f > gt && f <= le) {
		r.fail(name, fmt.Sprintf("is out of range: %v", f))
		return def
	}
	return f
}
