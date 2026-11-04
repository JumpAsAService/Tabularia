package api

import (
	"context"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
)

// services/engine_panel_settings.py push_loop: the engine settings from the panel sent to the
// engine again every minute, so that they come back by themselves after Valkey empties or
// the engine restarts. Whoever is saving holds the lock: then the round skips.

// PUSH_EVERY; the lock is engineSettingsLock (engine_settings_write.go)
const engineSettingsPushEvery = 60 * time.Second

func init() {
	RegisterJob("engine_settings_loop", func(d *Deps) jobs.Job {
		return jobs.Job{Order: 7, Loop: func(ctx context.Context) {
			for ctx.Err() == nil {
				if _, err := engineSettingsRound(ctx, d); err != nil {
					d.Log.Info("engine settings: not sent, retrying", "err", err, "seconds", 60)
				}
				if !jobs.Sleep(ctx, engineSettingsPushEvery) {
					return
				}
			}
		}, Ticks: map[string]jobs.Tick{
			"engine_settings": func(ctx context.Context) (any, error) { return engineSettingsRound(ctx, d) },
		}}
	})
}
