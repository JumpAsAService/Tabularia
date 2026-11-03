package api

import (
	"math"
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

const (
	// routes/engine_settings.py OLD_ENGINE
	engineSettingsOld     = "The engine is an older version that does not support the settings panel: update it"
	engineSettingsNoReply = "The engine is not responding: the settings cannot be read right now"
)

// fromGateway is engine_panel_settings.FROM_GATEWAY: set in the gateway's own box.
var fromGateway = map[string]bool{"upload.max_bytes": true}

func init() {
	register("engine_settings", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/engine-settings",
				Handler: deps.Observer(d.Auth, d.DB, d.Log, "/admin/engine-settings", readEngineSettings(d))},
		}
	})
}

// engineSettingValue is engine_panel_settings.value: the JSON one when there is one (its
// integer when they disagree: an older gateway wrote only that), else the integer.
func engineSettingValue(r engineSettingRow) (any, bool) {
	if r.ValueJSON == nil {
		return r.Value, true
	}
	v, err := pyd.LoadsStr(*r.ValueJSON)
	if err != nil {
		return nil, true
	}
	switch n := v.(type) {
	case int64:
		if r.Value != n {
			return r.Value, true
		}
	case float64:
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return nil, false // round() of them raises
		}
		if r.Value != int64(math.RoundToEven(n)) {
			return r.Value, true
		}
	}
	return v, true
}

// readEngineSettings is GET /admin/engine-settings (routes/engine_settings.py _state).
func readEngineSettings(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		cat, status, err := engineCatalog(ctx, d)
		if err != nil || status != 0 {
			detail := engineSettingsNoReply
			if status == http.StatusNotFound {
				detail = engineSettingsOld
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: detail})
			return
		}
		rows, err := savedEngineSettings(ctx, d.DB)
		if err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		full, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
		if err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		catalog, ok := cat.(*pyd.Object)
		if !ok {
			server.InternalError(w)
			return
		}
		list, ok := catalog.Get("items")
		elems, isList := list.([]any)
		if !ok || !isList {
			server.InternalError(w) // cat["items"], iterated
			return
		}
		items := []any{}
		for _, e := range elems {
			v, isDict := e.(*pyd.Object)
			if !isDict {
				server.InternalError(w)
				return
			}
			item, ok := engineSettingOut(d, v, rows, full)
			if !ok {
				server.InternalError(w)
				return
			}
			if item != nil {
				items = append(items, item)
			}
		}
		unavailable, ok := validated(pyd.List(pyd.Str()), orDefault(catalog, "unavailable", []any{}))
		refresh, ok2 := validated(pyd.Float(), orDefault(catalog, "refresh_seconds", int64(10)))
		if !ok || !ok2 {
			server.InternalError(w)
			return
		}
		writePy(w, pyjson.Object{
			{Key: "items", Value: items}, {Key: "editable", Value: full && d.Panel.FromPanel() && p.TokenName == ""},
			{Key: "panel_enabled", Value: d.Panel.FromPanel()}, {Key: "unavailable", Value: unavailable},
			{Key: "refresh_seconds", Value: refresh},
		})
	}
}

// engineSettingOut is one SettingOut of _state; nil for the gateway's own, false when the
// catalog's item does not fit (a KeyError, a validation error: a 500).
func engineSettingOut(d *Deps, v *pyd.Object, rows map[string]engineSettingRow, full bool) (pyjson.Object, bool) {
	need := func(k string) (any, bool) { return v.Get(k) }
	keyV, ok := need("key")
	if !ok {
		return nil, false
	}
	if ks, isStr := keyV.(string); isStr && fromGateway[ks] {
		return nil, true
	}
	out := pyjson.Object{}
	field := func(name string, t *pyd.Type, value any) bool {
		x, ok := validated(t, value)
		out = append(out, pyjson.KV{Key: name, Value: x})
		return ok
	}
	required := func(k string) any {
		x, has := need(k)
		if !has {
			ok = false
		}
		return x
	}
	key, category, unit := required("key"), required("category"), required("unit")
	minV, maxV, envVar, envValue, applied := required("min"), required("max"), required("env_var"), required("env_value"), required("value")
	if !ok {
		return nil, false
	}
	kindV := orDefault(v, "kind", "int")
	good := field("key", pyd.Str(), key) && field("category", pyd.Str(), category) && field("unit", pyd.Str(), unit) &&
		field("kind", pyd.Str(), kindV) && field("choices", pyd.List(pyd.Str()), orDefault(v, "choices", []any{})) &&
		field("optional", pyd.Bool(), orDefault(v, "optional", false)) && field("format", pyd.Str(), orDefault(v, "format", "")) &&
		field("rollout", pyd.List(pyd.Str()), orDefault(v, "rollout", []any{})) && field("min", pyd.Float(), minV) &&
		field("max", pyd.Float(), maxV) && field("env_var", pyd.Str(), envVar)
	if !good {
		return nil, false
	}
	out = append(out, pyjson.KV{Key: "env_value", Value: toPyjson(envValue)})
	var value, at, by any
	ks, _ := key.(string)
	if row, has := rows[ks]; has {
		if d.Panel.FromPanel() {
			x, ok := engineSettingValue(row)
			if !ok {
				return nil, false
			}
			kind, _ := v.Get("kind")
			if kind == "secret" && x != nil {
				x = pyd.Truthy(x)
			}
			value = toPyjson(x)
		}
		at = pyjson.Isoformat(row.UpdatedAt)
		by = updatedBy(row.UpdatedBy, full)
	}
	return append(out, pyjson.KV{Key: "value", Value: value}, pyjson.KV{Key: "applied", Value: toPyjson(applied)},
		pyjson.KV{Key: "updated_at", Value: at}, pyjson.KV{Key: "updated_by", Value: by}), true
}
