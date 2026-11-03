package config

import (
	"fmt"
	"time"

	_ "time/tzdata" // ZoneInfo's names, whatever the image has in /usr/share/zoneinfo
)

// AdminConfig is what the administration's routes read of the environment
// (gateway/app/core/config.py): the audit, the SIEM, the webhooks, the backups, the AI.
type AdminConfig struct {
	// APP__TIMEZONE: the deployment's zone (AppSettings.timezone, a ZoneInfo name); the
	// panel may change it (app.timezone)
	Timezone string
	// APP__VERSION: the release (AppSettings.version)
	Version string
}

func loadAdmin(r *reader) AdminConfig {
	c := AdminConfig{
		Timezone: r.str("APP__TIMEZONE", "UTC"),
		Version:  r.str("APP__VERSION", "1.3.0"),
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
