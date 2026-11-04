package settings

import (
	"io"
	"log/slog"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// app.timezone: a ZoneInfo name, trimmed; anything else leaves the environment's.
func TestTheZoneFromThePanel(t *testing.T) {
	p := New(&config.Config{Admin: config.AdminConfig{Timezone: "UTC"}}, true, slog.New(slog.NewTextHandler(io.Discard, nil)))
	for raw, want := range map[string]string{
		`" Europe/Rome "`: "Europe/Rome", `"Mars/Olympus"`: "UTC", `""`: "UTC", `"Local"`: "UTC", `3`: "UTC",
	} {
		p.Apply(map[string]string{"app.timezone": raw})
		if p.Timezone() != want {
			t.Errorf("%s: %s", raw, p.Timezone())
		}
	}
	p.Apply(nil)
	if p.Timezone() != "UTC" || p.env.admin["app.timezone"] != "UTC" {
		t.Error("the environment's value must come back, untouched")
	}
}
