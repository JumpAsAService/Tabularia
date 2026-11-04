package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// noSettingsFromToken is api_tokens.NO_SETTINGS_FROM_TOKEN.
var noSettingsFromToken = map[string]string{
	"en": "An API token cannot change the settings: do it from the interface",
	"it": "Un token API non cambia le impostazioni: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier les réglages : faites-le depuis l'interface",
	"de": "Ein API-Token kann die Einstellungen nicht ändern: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar los ajustes: hazlo desde la interfaz",
}

const (
	engineSettingsLock = 7_402_118 // engine_panel_settings.LOCK
	secretMax          = 1024      // routes/engine_settings.py SECRET_MAX
	nothingChanged     = "The engine is not responding: nothing was changed"
	envOnlyEngine      = "On this installation the engine settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)"
)

// clickhouseSecrets is CLICKHOUSE_SECRETS.
var clickhouseSecrets = map[string]string{"clickhouse_external.password": "secret", "clickhouse_external.ai_password": "secret"}

func init() {
	register("engine_settings_write", func(d *Deps) []server.Route {
		admin := func(route string, h deps.Handler) http.Handler {
			return withBody(deps.Superuser(d.Auth, d.DB, d.Log, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
				// _no_token, a dependency of the decorator: before the body's validation
				if ref := apitokens.DenyIfFromToken(r, p, noSettingsFromToken); ref != nil {
					server.Refuse(w, ref)
					return
				}
				h(w, r, p)
			}))
		}
		return []server.Route{
			{Method: http.MethodPut, Pattern: "/admin/engine-settings", Handler: admin("/admin/engine-settings", writeEngineSettings(d))},
			{Method: http.MethodPost, Pattern: "/admin/engine-settings/clickhouse-check",
				Handler: admin("/admin/engine-settings/clickhouse-check", clickhouseCheck(d))},
		}
	})
}

// lockSettings is engine_panel_settings.lock: the advisory lock until the transaction ends,
// asked again every 100 ms for 10 s.
func lockSettings(ctx context.Context, tx pgx.Tx, key int64) (bool, error) {
	end := time.Now().Add(10 * time.Second)
	for {
		var got bool
		if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, key).Scan(&got); err != nil {
			return false, err
		}
		if got || time.Now().After(end) {
			return got, nil
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// sealed is _sealed: the passwords typed in clear, encrypted; (values, errors).
func sealed(d *Deps, kinds map[string]string, values *pyd.Object) (pyjson.Object, []any, error) {
	out, errs := pyjson.Object{}, []any{}
	for _, k := range values.Keys() {
		v, _ := values.Get(k)
		if kinds[k] == "secret" && v != nil {
			s, ok := v.(string)
			if !ok {
				errs = append(errs, pyjson.Object{{Key: "key", Value: k}, {Key: "code", Value: "text"}})
				continue
			}
			if pyd.Len(s) > secretMax {
				errs = append(errs, pyjson.Object{{Key: "key", Value: k}, {Key: "code", Value: "too_long"}})
				continue
			}
			enc, err := d.Fernet.Encrypt(s)
			if err != nil {
				return nil, nil, err
			}
			v = enc
		}
		out = append(out, pyjson.KV{Key: k, Value: v})
	}
	return out, errs, nil
}

// fromGatewayValues is engine_panel_settings.from_gateway: the gateway's upload limit, as
// overrides.check keeps it (an integer within the panel's range).
func fromGatewayValues(ctx context.Context, d *Deps) pyjson.Object {
	var raw string
	if err := d.DB.QueryRow(ctx, `SELECT value FROM gateway_settings WHERE key = 'upload.max_bytes'`).Scan(&raw); err != nil {
		return pyjson.Object{}
	}
	v, err := pyd.LoadsStr(raw)
	if n, ok := v.(int64); err == nil && ok && n >= 1<<20 && n <= 1<<40 {
		return pyjson.Object{{Key: "upload.max_bytes", Value: n}}
	}
	return pyjson.Object{}
}

// pushSettings is engine_panel_settings.push: PUT /settings with the values (the gateway's
// own taken from its table), the hard limit allowed and what changed.
func pushSettings(ctx context.Context, d *Deps, values pyjson.Object, changed []string, withGateway bool) (int, any, []byte, error) {
	send := pyjson.Object{}
	for _, kv := range values {
		if !fromGateway[kv.Key] {
			send = append(send, kv)
		}
	}
	if withGateway && d.Panel.FromPanel() {
		send = append(send, fromGatewayValues(ctx, d)...)
	}
	ch := make([]any, len(changed))
	for i, c := range changed {
		ch[i] = c
	}
	body, err := pyjson.Dumps(pyjson.Object{{Key: "values", Value: send}, {Key: "max_task_time_limit", Value: d.Panel.RunStaleTimeout() - panelMargin},
		{Key: "changed", Value: ch}}, pyjson.Options{})
	if err != nil {
		return 0, nil, nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, engine.Check)
	defer cancel()
	req, err := d.Engine.Request(ctx, http.MethodPut, "/settings", bytes.NewReader([]byte(body)))
	if err != nil {
		return 0, nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := d.Engine.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer res.Body.Close()
	var b bytes.Buffer
	if _, err := b.ReadFrom(res.Body); err != nil {
		return 0, nil, nil, err
	}
	parsed, _ := pyd.LoadsStr(b.String())
	return res.StatusCode, parsed, b.Bytes(), nil
}

// errNotDict is `.get` on an answer that is not a dict: an AttributeError, a 500.
var errNotDict = errors.New("the engine's answer is not a dict")

// errorKeys is `(r.json().get("detail") or {}).get("errors") or []` as the set of their keys.
func errorKeys(parsed any, raw []byte) (map[any]bool, error) {
	o, ok := parsed.(*pyd.Object)
	if !ok {
		if _, err := pyd.LoadsStr(string(raw)); err != nil {
			return nil, err
		}
		return nil, errNotDict
	}
	detail, _ := o.Get("detail")
	keys := map[any]bool{}
	if !pyd.Truthy(detail) {
		return keys, nil
	}
	do, ok := detail.(*pyd.Object)
	if !ok {
		return nil, errNotDict
	}
	errs, _ := do.Get("errors")
	if !pyd.Truthy(errs) {
		return keys, nil
	}
	list, ok := errs.([]any)
	if !ok {
		return nil, errNotDict
	}
	for _, e := range list {
		eo, ok := e.(*pyd.Object)
		if !ok {
			return nil, errNotDict
		}
		k, _ := eo.Get("key")
		if ks, isStr := k.(string); isStr {
			keys[ks] = true
		} else {
			keys[k] = true
		}
	}
	return keys, nil
}

// errEngineRefuses is push_discarding's RuntimeError or a raise_for_status: «not responding».
var errEngineRefuses = errors.New("the engine refuses the settings")

// pushDiscarding is engine_panel_settings.push_discarding: sent again without the ones the
// engine refuses, until it accepts; the ones it applies.
func pushDiscarding(ctx context.Context, d *Deps, values pyjson.Object) (pyjson.Object, error) {
	current := append(pyjson.Object{}, values...)
	withGateway := true
	for i := 0; i < len(values)+2; i++ {
		status, parsed, raw, err := pushSettings(ctx, d, current, nil, withGateway)
		if err != nil {
			return nil, errEngineRefuses
		}
		if status != http.StatusUnprocessableEntity {
			if status < 200 || status > 299 {
				return nil, errEngineRefuses
			}
			return current, nil
		}
		keys, err := errorKeys(parsed, raw)
		if err != nil {
			return nil, err
		}
		if withGateway && keys["upload.max_bytes"] {
			withGateway = false
			continue
		}
		bad := map[string]bool{}
		for _, kv := range current {
			if keys[kv.Key] {
				bad[kv.Key] = true
			}
		}
		if len(bad) == 0 {
			return nil, errEngineRefuses
		}
		kept := pyjson.Object{}
		for _, kv := range current {
			if !bad[kv.Key] {
				kept = append(kept, kv)
			}
		}
		current = kept
	}
	return nil, errEngineRefuses
}

// columnsOf is engine_panel_settings.columns: (value, value_json) of a row.
func columnsOf(v any) (int64, *string) {
	switch x := v.(type) {
	case int64:
		return x, nil
	case float64:
		s := pyjson.FloatRepr(x)
		return int64(math.RoundToEven(x)), &s
	case bool:
		n := int64(0)
		if x {
			n = 1
		}
		s := "false"
		if x {
			s = "true"
		}
		return n, &s
	}
	s, _ := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
	n := int64(0)
	if pyd.Truthy(v) {
		n = 1
	}
	return n, &s
}

// maskedSetting is _masked: a secret only as set or not.
func maskedSetting(kind string, v any) any {
	if kind == "secret" && v != nil {
		return pyd.Truthy(v)
	}
	return v
}

// writeEngineSettings is PUT /admin/engine-settings (routes/engine_settings.py write): the
// engine checks and applies them first, all or none, then they are saved.
func writeEngineSettings(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.EngineSettingsSettingsUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		if !d.Panel.FromPanel() {
			server.Refuse(w, &auth.Refusal{Status: http.StatusForbidden, Detail: envOnlyEngine})
			return
		}
		ctx := r.Context()
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		defer tx.Rollback(ctx)
		if got, err := lockSettings(ctx, tx, engineSettingsLock); err != nil || !got {
			if err != nil {
				fail500(d, w, "engine settings", err)
				return
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "Another change to the settings is in progress: try again in a moment"})
			return
		}
		cat, status, err := engineCatalog(ctx, d)
		if err != nil || status != 0 {
			detail := nothingChanged
			if status == http.StatusNotFound {
				detail = engineSettingsOld + ": nothing was changed"
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: detail})
			return
		}
		kinds, ok := catalogKinds(cat)
		if !ok {
			server.InternalError(w)
			return
		}
		values := body.Get("values").(*pyd.Object)
		unknown := []any{}
		for _, k := range values.Keys() {
			if _, known := kinds[k]; !known {
				unknown = append(unknown, pyjson.Object{{Key: "key", Value: k}, {Key: "code", Value: "unknown"}})
			}
		}
		asked, wrong, err := sealed(d, kinds, values)
		if err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		if len(unknown)+len(wrong) > 0 {
			writePyStatus(w, http.StatusUnprocessableEntity, pyjson.Object{{Key: "detail", Value: pyjson.Object{{Key: "errors", Value: append(unknown, wrong...)}}}})
			return
		}
		rows, err := savedEngineSettings(ctx, tx)
		if err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		before := pyjson.Object{}
		for _, k := range sortedRowKeys(ctx, tx) {
			if fromGateway[k] {
				continue
			}
			v, ok := engineSettingValue(rows[k])
			if !ok {
				server.InternalError(w)
				return
			}
			before = append(before, pyjson.KV{Key: k, Value: v})
		}
		after := append(pyjson.Object{}, before...)
		for _, kv := range asked {
			if kv.Value == nil {
				after = without(after, kv.Key)
			} else {
				after = after.Set(kv.Key, kv.Value)
			}
		}
		type change struct {
			key      string
			from, to any
		}
		var changes []change
		for _, kv := range asked {
			b, _ := before.Get(kv.Key)
			a, _ := after.Get(kv.Key)
			if !pyEqual(b, a) {
				changes = append(changes, change{kv.Key, b, a})
			}
		}
		if len(changes) == 0 {
			_ = tx.Rollback(ctx)
			readEngineSettings(d)(w, r, p)
			return
		}
		changedKeys := make([]string, len(changes))
		for i, c := range changes {
			changedKeys[i] = c.key
		}
		valid, err := pushDiscarding(ctx, d, before)
		if errors.Is(err, errEngineRefuses) {
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: nothingChanged})
			return
		}
		if err != nil {
			server.InternalError(w)
			return
		}
		sending := pyjson.Object{}
		for _, kv := range valid {
			if _, isAsked := asked.Get(kv.Key); !isAsked {
				sending = append(sending, kv)
			}
		}
		for _, kv := range asked {
			if kv.Value != nil {
				sending = sending.Set(kv.Key, kv.Value)
			}
		}
		status, parsed, raw, err := pushSettings(ctx, d, sending, changedKeys, true)
		if err == nil && status == http.StatusUnprocessableEntity {
			keys, kerr := errorKeys(parsed, raw)
			if kerr != nil {
				server.InternalError(w)
				return
			}
			onlyGateway := len(keys) > 0
			for k := range keys {
				if ks, _ := k.(string); !fromGateway[ks] {
					onlyGateway = false
				}
			}
			if onlyGateway {
				status, parsed, raw, err = pushSettings(ctx, d, sending, changedKeys, false)
			}
		}
		if err != nil {
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: nothingChanged})
			return
		}
		switch {
		case status == http.StatusUnprocessableEntity:
			if parsed == nil {
				server.InternalError(w)
				return
			}
			out, _ := pyd.Dumps(parsed)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write(out)
			return
		case status == http.StatusNotFound:
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: engineSettingsOld + ": nothing was changed"})
			return
		case status >= 400:
			server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: fmt.Sprintf("The engine answered %d: nothing was changed", status)})
			return
		}
		// the components that read them at start: rollout.mark_changed
		restart := map[string][]string{}
		if _, err := pyd.LoadsStr(string(raw)); err != nil {
			server.InternalError(w) // r.json() of an answer that is not JSON
			return
		}
		if o, ok := parsed.(*pyd.Object); ok {
			items, _ := o.Get("items")
			list, _ := items.([]any)
			for _, e := range list {
				io, ok := e.(*pyd.Object)
				if !ok {
					server.InternalError(w)
					return
				}
				k, has := io.Get("key")
				if !has {
					server.InternalError(w) // v["key"]
					return
				}
				ks, _ := k.(string)
				rv, _ := io.Get("rollout")
				var comps []string
				if rl, ok := rv.([]any); ok {
					for _, c := range rl {
						if cs, ok := c.(string); ok {
							comps = append(comps, cs)
						}
					}
				}
				restart[ks] = comps
			}
		} else if parsed != nil {
			server.InternalError(w)
			return
		}
		now := time.Now().UTC()
		for _, c := range changes {
			for _, comp := range restart[c.key] {
				if !rolloutComponent[comp] {
					continue
				}
				if _, err := tx.Exec(ctx, `INSERT INTO settings_rollouts (component, changed_at) VALUES ($1, $2)
					ON CONFLICT (component) DO UPDATE SET changed_at = EXCLUDED.changed_at`, comp, now); err != nil {
					fail500(d, w, "engine settings", err)
					return
				}
			}
		}
		for _, c := range changes {
			if v, in := after.Get(c.key); in {
				value, js := columnsOf(v)
				if _, err := tx.Exec(ctx, `INSERT INTO engine_settings (key, value, value_json, updated_at, updated_by) VALUES ($1, $2, $3, $4, $5)
					ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, value_json = EXCLUDED.value_json,
					updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by`, c.key, value, js, now, p.User.Email); err != nil {
					fail500(d, w, "engine settings", err)
					return
				}
			} else if _, saved := rows[c.key]; saved {
				if _, err := tx.Exec(ctx, `DELETE FROM engine_settings WHERE key = $1`, c.key); err != nil {
					fail500(d, w, "engine settings", err)
					return
				}
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail500(d, w, "engine settings", err)
			return
		}
		detail := pyjson.Object{}
		for _, c := range changes {
			detail = append(detail, pyjson.KV{Key: c.key, Value: pyjson.Object{{Key: "from", Value: maskedSetting(kinds[c.key], c.from)},
				{Key: "to", Value: maskedSetting(kinds[c.key], c.to)}}})
		}
		kind, label := "engine_settings", "engine"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "engine_settings.update", TargetType: &kind, TargetLabel: &label,
			Detail: pyjson.Object{{Key: "changes", Value: detail}}})
		readEngineSettings(d)(w, r, p)
	}
}

// rolloutComponent is settings_rollout.ORDER.
var rolloutComponent = map[string]bool{"worker": true, "preview-worker": true, "beat": true, "engine": true, "gateway": true, "orchestrator": true}

// sortedRowKeys is the order `saved()` reads the rows in: the table's, by its primary key
// scan as PostgreSQL returns them.
func sortedRowKeys(ctx context.Context, q interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
}) []string {
	rows, err := q.Query(ctx, `SELECT key FROM engine_settings`)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var k string
		if rows.Scan(&k) == nil {
			out = append(out, k)
		}
	}
	return out
}

func without(o pyjson.Object, key string) pyjson.Object {
	out := pyjson.Object{}
	for _, kv := range o {
		if kv.Key != key {
			out = append(out, kv)
		}
	}
	return out
}

// catalogKinds is {v["key"]: v.get("kind", "int")} of the catalog's items, the gateway's own
// left out; false where Python raises.
func catalogKinds(cat any) (map[string]string, bool) {
	o, ok := cat.(*pyd.Object)
	if !ok {
		return nil, false
	}
	items, has := o.Get("items")
	list, isList := items.([]any)
	if !has || !isList {
		return nil, false
	}
	kinds := map[string]string{}
	for _, e := range list {
		io, ok := e.(*pyd.Object)
		if !ok {
			return nil, false
		}
		k, has := io.Get("key")
		ks, isStr := k.(string)
		if !has || !isStr {
			return nil, false
		}
		if fromGateway[ks] {
			continue
		}
		kind := "int"
		if kv, has := io.Get("kind"); has {
			kind, _ = kv.(string)
		}
		kinds[ks] = kind
	}
	return kinds, true
}

// writePyStatus is a JSONResponse(status_code=…, content=…).
func writePyStatus(w http.ResponseWriter, status int, v any) {
	b, err := pyjson.Dumps(v, pyjson.Options{Compact: true})
	if err != nil {
		server.InternalError(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(b))
}

// clickhouseCheck is POST /admin/engine-settings/clickhouse-check: the engine tries the
// external ClickHouse with these values, saving nothing.
func clickhouseCheck(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.EngineSettingsCheckRequest)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		values := pyd.NewObject()
		given := body.Get("values").(*pyd.Object)
		for _, k := range given.Keys() {
			if v, _ := given.Get(k); v != nil {
				values.Set(k, v)
			}
		}
		sealedValues, wrong, err := sealed(d, clickhouseSecrets, values)
		if err != nil {
			fail500(d, w, "clickhouse check", err)
			return
		}
		if len(wrong) > 0 {
			writePyStatus(w, http.StatusUnprocessableEntity, pyjson.Object{{Key: "detail", Value: pyjson.Object{{Key: "errors", Value: wrong}}}})
			return
		}
		raw, _ := pyjson.Dumps(pyjson.Object{{Key: "values", Value: sealedValues}}, pyjson.Options{})
		ctx, cancel := context.WithTimeout(r.Context(), 150*time.Second) // CHECK_TIMEOUT
		defer cancel()
		req, err := d.Engine.Request(ctx, http.MethodPost, "/settings/clickhouse-check", bytes.NewReader([]byte(raw)))
		if err != nil {
			fail500(d, w, "clickhouse check", err)
			return
		}
		req.Header.Set("Content-Type", "application/json")
		res, err := d.Engine.Do(req)
		if err != nil {
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: "The engine is not responding: the connection was not tested"})
			return
		}
		defer res.Body.Close()
		var b bytes.Buffer
		_, _ = b.ReadFrom(res.Body)
		switch {
		case res.StatusCode == http.StatusNotFound:
			server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: engineSettingsOld})
		case res.StatusCode == http.StatusUnprocessableEntity:
			parsed, err := pyd.LoadsStr(b.String())
			if err != nil {
				server.InternalError(w)
				return
			}
			out, _ := pyd.Dumps(parsed)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write(out)
		case res.StatusCode >= 400:
			server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: fmt.Sprintf("The engine answered %d: the connection was not tested", res.StatusCode)})
		default:
			parsed, err := pyd.LoadsStr(b.String())
			if err != nil {
				server.InternalError(w)
				return
			}
			out, err := pyd.Dumps(parsed)
			if err != nil {
				server.InternalError(w)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(out)
		}
	}
}
