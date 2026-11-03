// Package settings is gateway/app/core/overrides.py for the settings the Go gateway reads:
// the environment's values with, on top, the ones an administrator changed from
// Administration → Settings (the table gateway_settings, JSON values), read again every
// ten seconds. A value that does not pass the setting's check is dropped and the
// environment's applies; with the database down the last values stay; with
// ENGINE__SETTINGS_PANEL=false only the environment counts.
package settings

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// RereadEvery is overrides.REREAD_EVERY.
const RereadEvery = 10 * time.Second

// values in force; replaced as one object, so a reader never mixes two moments
type values struct {
	sessionIdleMinutes int
	personalTokens     bool
	apiTokenMaxDays    int
	admin              map[string]any // settings_admin.go
}

// item is an entry of overrides.ITEMS the Go gateway reads: its kind and the panel's range.
type item struct {
	kind     string // int | bool
	min, max int64
	set      func(v *values, raw any)
}

var items = map[string]item{
	"auth.session_idle_minutes": {kind: "int", min: 0, max: 24 * 60, set: func(v *values, x any) { v.sessionIdleMinutes = int(x.(int64)) }},
	"auth.personal_tokens":      {kind: "bool", set: func(v *values, x any) { v.personalTokens = x.(bool) }},
	"auth.api_token_max_days":   {kind: "int", min: 1, max: 3650, set: func(v *values, x any) { v.apiTokenMaxDays = int(x.(int64)) }},
}

// Panel holds the settings in force.
type Panel struct {
	env     values
	enabled bool
	current atomic.Pointer[values]
	log     *slog.Logger
}

// New starts from the environment.
func New(cfg *config.Config, panelEnabled bool, log *slog.Logger) *Panel {
	p := &Panel{env: values{cfg.SessionIdleMinutes, cfg.PersonalTokens, cfg.APITokenMaxDays, nil}, enabled: panelEnabled, log: log}
	p.env.admin = adminEnv(cfg)
	v := p.env
	p.current.Store(&v)
	return p
}

func (p *Panel) SessionIdleMinutes() int { return p.current.Load().sessionIdleMinutes }
func (p *Panel) PersonalTokens() bool    { return p.current.Load().personalTokens }
func (p *Panel) APITokenMaxDays() int    { return p.current.Load().apiTokenMaxDays }

// Apply puts in force the rows just read (key → JSON text): the good ones on top of the
// environment, the others dropped and logged.
func (p *Panel) Apply(rows map[string]string) {
	v := p.env
	if p.enabled {
		for key, raw := range rows {
			it, ok := items[key]
			if !ok {
				p.applyAdmin(&v, key, raw)
				continue // or a setting of the Python gateway's: not read here
			}
			x, ok := check(it, raw)
			if !ok {
				p.log.Warn("setting from the panel dropped, the environment's applies", "key", key)
				continue
			}
			it.set(&v, x)
		}
	}
	p.current.Store(&v)
}

// check is overrides.check_one for int and bool: a JSON integer (not 30.0, not true) or a
// JSON boolean, within the panel's range.
func check(it item, raw string) (any, bool) {
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
		if err != nil || i < it.min || i > it.max {
			return nil, false
		}
		return i, true
	}
	return nil, false
}

// Reload reads the table once; with the database down the last values stay.
func (p *Panel) Reload(ctx context.Context, db *pgxpool.Pool) {
	if !p.enabled {
		p.Apply(nil)
		return
	}
	rows, err := db.Query(ctx, `SELECT key, value FROM gateway_settings`)
	if err != nil {
		p.log.Debug("settings from the panel not read", "err", err)
		return
	}
	defer rows.Close()
	read := map[string]string{}
	for rows.Next() {
		var k, v string
		if err := rows.Scan(&k, &v); err != nil {
			p.log.Debug("settings from the panel not read", "err", err)
			return
		}
		read[k] = v
	}
	if rows.Err() != nil {
		return
	}
	p.Apply(read)
}

// Loop reads the table every RereadEvery until ctx ends.
func (p *Panel) Loop(ctx context.Context, db *pgxpool.Pool) {
	t := time.NewTicker(RereadEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p.Reload(ctx, db)
		}
	}
}
