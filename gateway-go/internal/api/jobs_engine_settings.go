package api

import (
	"context"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
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

// engineSettingsRound is one_round: the lock asked once (wait=0), then push_discarding of
// in_force, the lock released with the transaction.
func engineSettingsRound(ctx context.Context, d *Deps) (bool, error) {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, engineSettingsLock).Scan(&got); err != nil {
		return false, err
	}
	if !got {
		return false, nil
	}
	// in_force: none when the panel is turned off by the environment
	values := pyjson.Object{}
	if d.Panel.FromPanel() {
		rows, err := savedEngineSettings(ctx, tx)
		if err != nil {
			return false, err
		}
		for _, k := range sortedRowKeys(ctx, tx) {
			if fromGateway[k] {
				continue
			}
			v, ok := engineSettingValue(rows[k])
			if !ok {
				return false, errNotDict // round() of a NaN raises: the round fails
			}
			values = append(values, pyjson.KV{Key: k, Value: v})
		}
	}
	if _, err := pushDiscarding(ctx, d, values); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}
