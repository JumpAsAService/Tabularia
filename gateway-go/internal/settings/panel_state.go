package settings

import (
	"sort"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// PanelEnv is settings_from_env() for the panel's settings: key → the environment's value.
func (p *Panel) PanelEnv() map[string]any { return p.panelEnv }

// decodedRows is read_rows of the rows in force: the ones that are JSON (the others ignored),
// in the keys' order (the good values do not depend on it).
func decodedRows(rows map[string]string) pyjson.Object {
	keys := make([]string, 0, len(rows))
	for k := range rows {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := pyjson.Object{}
	for _, k := range keys {
		if v, err := pyd.LoadsStr(rows[k]); err == nil {
			out = append(out, pyjson.KV{Key: k, Value: v})
		}
	}
	return out
}

// RememberStart is overrides.remember_start: what this process starts with, for the settings
// read only then (the ones with a rollout). Called once, after the first reading of the table.
func (p *Panel) RememberStart() {
	good := pyjson.Object{}
	if p.enabled {
		good, _ = Check(decodedRows(p.current.Load().rows), p.panelEnv, nil)
	}
	at := map[string]any{}
	for k := range Items {
		if len(Items[k].Rollout) > 0 {
			at[Items[k].Key] = Effective(p.panelEnv, good, Items[k].Key)
		}
	}
	p.startedWith.Store(&at)
}

// StartedWith is overrides.started_with (nil before RememberStart).
func (p *Panel) StartedWith(key string) any {
	if m := p.startedWith.Load(); m != nil {
		return (*m)[key]
	}
	return nil
}
