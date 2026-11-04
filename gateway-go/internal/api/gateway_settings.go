package api

import (
	"context"
	"errors"
	"math/big"
	"net/http"
	"os"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// routes/gateway_settings.py: the gateway's settings in Administration → Settings (read and
// write; the rollout's routes are not here). Which ones and their checks: settings.Items and
// settings.Check (core/overrides.py).

const (
	gatewaySettingsLock = 7_402_120 // routes/gateway_settings.py LOCK
	envOnlyGateway      = "On this installation the settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)"
	settingsBusy        = "Another change to the settings is in progress: try again in a moment"
	staleKey            = "engine.run_stale_timeout_seconds"
)

func init() {
	register("gateway_settings", func(d *Deps) []server.Route {
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
			{Method: http.MethodGet, Pattern: "/admin/gateway-settings", Handler: observer(d, "/admin/gateway-settings", readGatewaySettings(d))},
			{Method: http.MethodPut, Pattern: "/admin/gateway-settings", Handler: admin("/admin/gateway-settings", writeGatewaySettings(d))},
		}
	})
}

// gatewaySettingRow is a row of gateway_settings.
type gatewaySettingRow struct {
	Key       string
	Value     string
	UpdatedAt time.Time
	UpdatedBy *string
}

// gatewaySettingRows is _rows: every row, in the table's order (the query has none).
func gatewaySettingRows(ctx context.Context, q perm.Querier) ([]gatewaySettingRow, error) {
	rows, err := q.Query(ctx, `SELECT key, value, updated_at, updated_by FROM gateway_settings`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []gatewaySettingRow
	for rows.Next() {
		var r gatewaySettingRow
		if err := rows.Scan(&r.Key, &r.Value, &r.UpdatedAt, &r.UpdatedBy); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// decodeSetting is _decode: json.loads of the value, None when it is not JSON.
func decodeSetting(r gatewaySettingRow) any {
	v, err := pyd.LoadsStr(r.Value)
	if err != nil {
		return nil
	}
	return v
}

// tokenMounted is Path(kubernetes.token_file).exists(), asked at every request.
func tokenMounted(d *Deps) bool {
	_, err := os.Stat(d.Config.KubernetesTokenFile)
	return err == nil
}

func floatOrNone(v any) any {
	switch x := v.(type) {
	case int64:
		return float64(x)
	case float64:
		return x
	}
	return nil
}

// gatewaySettingsState is _state: the items this installation has, with the environment's
// value, the panel's, the one applied here and, for the rollout ones, the one this process
// started with. The rows just read are put in force at once (apply_rows).
func gatewaySettingsState(ctx context.Context, d *Deps, p *auth.Principal) (*pyd.Object, error) {
	rows, err := gatewaySettingRows(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	byKey := map[string]gatewaySettingRow{}
	inForce, raw := pyjson.Object{}, map[string]string{}
	for _, r := range rows {
		byKey[r.Key] = r
		if v := decodeSetting(r); v != nil {
			inForce = append(inForce, pyjson.KV{Key: r.Key, Value: v})
			raw[r.Key] = r.Value
		}
	}
	d.Panel.Apply(raw)
	env := d.Panel.PanelEnv()
	applied := pyjson.Object{}
	if d.Panel.FromPanel() {
		applied, _ = settings.Check(inForce, env, nil)
	}
	full, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	mounted := tokenMounted(d)
	items := []any{}
	for k := range settings.Items {
		it := &settings.Items[k]
		if !settings.Available(it, d.Config, mounted) {
			continue
		}
		o := pyd.NewObject()
		o.Set("key", it.Key)
		o.Set("group", it.Group)
		o.Set("kind", it.Kind)
		o.Set("unit", it.Unit)
		o.Set("min", floatOrNone(it.Min))
		o.Set("max", floatOrNone(it.Max))
		var maxLength any
		if it.Kind == "str" || it.Kind == "timezone" || it.Kind == "cpu" || it.Kind == "memory" {
			maxLength = int64(it.MaxLength)
		}
		o.Set("max_length", maxLength)
		comps := []any{}
		for _, c := range it.Rollout {
			comps = append(comps, c)
		}
		o.Set("rollout", comps)
		o.Set("env_var", it.EnvVar())
		o.Set("env_value", env[it.Key])
		r, saved := byKey[it.Key]
		var value any
		if saved && d.Panel.FromPanel() {
			value = decodeSetting(r)
		}
		o.Set("value", value)
		o.Set("applied", settings.Effective(env, applied, it.Key))
		var started any
		if len(it.Rollout) > 0 {
			started = d.Panel.StartedWith(it.Key)
		}
		o.Set("started_with", started)
		var at, by any
		if saved {
			at = pyjson.Isoformat(r.UpdatedAt)
			by = updatedBy(r.UpdatedBy, full)
		}
		o.Set("updated_at", at)
		o.Set("updated_by", by)
		items = append(items, o)
	}
	out := pyd.NewObject()
	out.Set("items", items)
	out.Set("editable", full && d.Panel.FromPanel() && p.TokenName == "")
	out.Set("panel_enabled", d.Panel.FromPanel())
	out.Set("refresh_seconds", settings.RereadEvery.Seconds())
	return out, nil
}

func answerGatewaySettings(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	out, err := gatewaySettingsState(r.Context(), d, p)
	if err != nil {
		fail500(d, w, "gateway settings", err)
		return
	}
	b, err := pyd.Dumps(out)
	if err != nil { // a saved value Starlette cannot write (NaN): a 500
		server.InternalError(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(b)
}

// readGatewaySettings is GET /admin/gateway-settings: administrators and observers.
func readGatewaySettings(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		answerGatewaySettings(d, w, r, p)
	}
}

func settingsErrors(w http.ResponseWriter, errs []pyjson.Object) {
	list := make([]any, len(errs))
	for i, e := range errs {
		list[i] = e
	}
	writePyStatus(w, http.StatusUnprocessableEntity, pyjson.Object{{Key: "detail", Value: pyjson.Object{{Key: "errors", Value: list}}}})
}

// staleBelowHardLimit is _stale_below_hard_limit: the run's stale threshold must stay above
// the engine's hard time limit plus the margin. (error, ok); ok false is an exception Python
// does not catch (a 500).
func staleBelowHardLimit(ctx context.Context, d *Deps, stale any) (pyjson.Object, bool) {
	cat, status, err := engineCatalog(ctx, d)
	if err != nil || status != 0 {
		return pyjson.Object{{Key: "key", Value: staleKey}, {Key: "code", Value: "engine_unreachable"}}, true
	}
	o, isDict := cat.(*pyd.Object)
	if !isDict {
		return nil, false
	}
	// cat.get("items", []), iterated: a list; an empty text or dict iterates nothing, anything
	// else fails (.get on a key or a character, or a value that does not iterate)
	list := []any{}
	if v, has := o.Get("items"); has {
		switch x := v.(type) {
		case []any:
			list = x
		case *pyd.Object:
			if x.Len() > 0 {
				return nil, false
			}
		case string:
			if x != "" {
				return nil, false
			}
		default:
			return nil, false
		}
	}
	var hard any
	found := false
	for _, e := range list {
		io, ok := e.(*pyd.Object)
		if !ok {
			return nil, false
		}
		if k, _ := io.Get("key"); k != "celery.task_time_limit" {
			continue
		}
		hard, _ = io.Get("value")
		if hard == nil {
			hard, _ = io.Get("env_value")
		}
		found = true
		break
	}
	if !found || hard == nil {
		return nil, true
	}
	var limit any
	switch h := hard.(type) {
	case bool:
		n := int64(panelMargin)
		if h {
			n++
		}
		limit = n
	case int64:
		limit = new(big.Int).Add(big.NewInt(h), big.NewInt(panelMargin))
	case *big.Int:
		limit = new(big.Int).Add(h, big.NewInt(panelMargin))
	case float64:
		limit = h + panelMargin
	default:
		return nil, false // str + int: a TypeError
	}
	if b, ok := limit.(*big.Int); ok && b.IsInt64() {
		limit = b.Int64()
	}
	if pyNumberLess(stale, limit) {
		return pyjson.Object{{Key: "key", Value: staleKey}, {Key: "code", Value: "stale_below_hard_limit"}, {Key: "limit", Value: toPyjsonInt(limit)}}, true
	}
	return nil, true
}

func toPyjsonInt(v any) any {
	if b, ok := v.(*big.Int); ok {
		return pyjson.Int(b.String())
	}
	return v
}

// pyNumberLess is `a < b` of two Python numbers.
func pyNumberLess(a, b any) bool {
	ra, _ := pyNumber(a)
	rb, _ := pyNumber(b)
	if ra == nil || rb == nil {
		fa, _ := a.(float64)
		fb, _ := b.(float64)
		return fa < fb
	}
	return ra.Cmp(rb) < 0
}

// writeGatewaySettings is PUT /admin/gateway-settings (routes/gateway_settings.py write): all
// or none, a 422 naming which and why; saved, in force at once here.
func writeGatewaySettings(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.GatewaySettingsSettingsUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		if !d.Panel.FromPanel() {
			server.Refuse(w, &auth.Refusal{Status: http.StatusForbidden, Detail: envOnlyGateway})
			return
		}
		ctx := r.Context()
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail500(d, w, "gateway settings", err)
			return
		}
		defer tx.Rollback(ctx)
		if got, err := lockSettings(ctx, tx, gatewaySettingsLock); err != nil || !got {
			if err != nil {
				fail500(d, w, "gateway settings", err)
				return
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: settingsBusy})
			return
		}
		values := body.Get("values").(*pyd.Object)
		mounted := tokenMounted(d)
		var refused []pyjson.Object
		for _, k := range values.Keys() {
			it := settings.PerKey[k]
			switch {
			case it == nil:
				refused = append(refused, pyjson.Object{{Key: "key", Value: k}, {Key: "code", Value: "unknown"}})
			case !settings.Available(it, d.Config, mounted):
				refused = append(refused, pyjson.Object{{Key: "key", Value: k}, {Key: "code", Value: "unavailable"}})
			}
		}
		if len(refused) > 0 {
			settingsErrors(w, refused)
			return
		}
		env := d.Panel.PanelEnv()
		rows, err := gatewaySettingRows(ctx, tx)
		if err != nil {
			fail500(d, w, "gateway settings", err)
			return
		}
		saved := pyjson.Object{} // {k: _decode(r)}: raw_before too
		inRows := map[string]bool{}
		for _, r := range rows {
			saved = saved.Set(r.Key, decodeSetting(r))
			inRows[r.Key] = true
		}
		before, _ := settings.Check(saved, env, nil)
		after := append(pyjson.Object{}, before...)
		changed := map[string]bool{}
		for _, k := range values.Keys() {
			v, _ := values.Get(k)
			if v == nil {
				after = without(after, k)
			} else {
				after = after.Set(k, v)
				changed[k] = true
			}
		}
		good, all := settings.Check(after, env, changed)
		var errs []pyjson.Object
		for _, e := range all {
			k, _ := e.Get("key")
			ks, _ := k.(string)
			_, asked := values.Get(ks)
			_, held := before.Get(ks)
			if asked || held {
				errs = append(errs, e)
			}
		}
		if _, asked := values.Get(staleKey); len(errs) == 0 && asked {
			stale := settings.Effective(env, good, staleKey)
			e, ok := staleBelowHardLimit(ctx, d, stale)
			if !ok {
				server.InternalError(w)
				return
			}
			if e != nil {
				errs = append(errs, e)
			}
		}
		if len(errs) > 0 {
			settingsErrors(w, errs)
			return
		}
		type change struct {
			key      string
			from, to any
		}
		var changes []change
		for _, k := range values.Keys() {
			from, _ := saved.Get(k)
			to, isGood := good.Get(k)
			if !pyEqual(from, to) || inRows[k] != isGood {
				changes = append(changes, change{k, from, to})
			}
		}
		if len(changes) == 0 {
			_ = tx.Rollback(ctx)
			answerGatewaySettings(d, w, r, p)
			return
		}
		at := time.Now().UTC()
		marked := map[string]bool{}
		for _, c := range changes {
			for _, comp := range settings.PerKey[c.key].Rollout {
				if marked[comp] || !rolloutComponent[comp] {
					continue
				}
				marked[comp] = true
				if _, err := tx.Exec(ctx, `INSERT INTO settings_rollouts (component, changed_at) VALUES ($1, $2)
					ON CONFLICT (component) DO UPDATE SET changed_at = EXCLUDED.changed_at`, comp, at); err != nil {
					fail500(d, w, "gateway settings", err)
					return
				}
			}
		}
		for _, c := range changes {
			if v, in := good.Get(c.key); in {
				text, err := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
				if err != nil {
					fail500(d, w, "gateway settings", err)
					return
				}
				if _, err := tx.Exec(ctx, `INSERT INTO gateway_settings (key, value, updated_at, updated_by) VALUES ($1, $2, $3, $4)
					ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at,
					updated_by = EXCLUDED.updated_by`, c.key, text, at, p.User.Email); err != nil {
					fail500(d, w, "gateway settings", err)
					return
				}
			} else if inRows[c.key] {
				if _, err := tx.Exec(ctx, `DELETE FROM gateway_settings WHERE key = $1`, c.key); err != nil {
					fail500(d, w, "gateway settings", err)
					return
				}
			}
		}
		if err := tx.Commit(ctx); err != nil {
			fail500(d, w, "gateway settings", err)
			return
		}
		detail := pyjson.Object{}
		for _, c := range changes {
			detail = append(detail, pyjson.KV{Key: c.key, Value: pyjson.Object{{Key: "from", Value: toPyjson(bigToPyjson(c.from))},
				{Key: "to", Value: bigToPyjson(c.to)}}})
		}
		kind, label := "settings", "gateway"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "settings.update", TargetType: &kind, TargetLabel: &label,
			Detail: pyjson.Object{{Key: "changes", Value: detail}}})
		d.Panel.Reload(ctx, d.DB) // this process at once; the others within REREAD_EVERY
		has := func(k string) bool {
			for _, c := range changes {
				if c.key == k {
					return true
				}
			}
			return false
		}
		if has("app.timezone") {
			if n, err := recomputeSchedules(ctx, d); err != nil {
				fail500(d, w, "gateway settings", err)
				return
			} else {
				d.Log.Info("settings: the schedules' time zone changed, next slots computed again", "n", n)
			}
		}
		for k := range fromGateway {
			if has(k) {
				if _, err := engineSettingsRound(ctx, d); err != nil {
					d.Log.Info("settings: the upload limit not sent to the engine yet", "err", err)
				}
				break
			}
		}
		answerGatewaySettings(d, w, r, p)
	}
}

// bigToPyjson writes json.loads' big integers (and those inside lists and dicts) as pyjson's.
func bigToPyjson(v any) any {
	switch x := v.(type) {
	case *big.Int:
		return pyjson.Int(x.String())
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = bigToPyjson(e)
		}
		return out
	case *pyd.Object:
		o := pyd.NewObject()
		for _, k := range x.Keys() {
			e, _ := x.Get(k)
			o.Set(k, bigToPyjson(e))
		}
		return o
	}
	return v
}

// recomputeSchedules is scheduler.recompute_all: the next slot of every schedule again, read
// in the new zone (one that is no longer valid is left to the tick).
func recomputeSchedules(ctx context.Context, d *Deps) (int, error) {
	now := time.Now().UTC()
	zone := d.Panel.AppTimezone()
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	n := 0
	for _, t := range []struct{ table, schedule, next string }{{"datasources", "refresh_schedule", "next_refresh_at"},
		{"flows", "run_schedule", "next_run_at"}, {"flow_groups", "run_schedule", "next_run_at"},
		{"saved_scripts", "run_schedule", "next_run_at"}, {"sql_scripts", "run_schedule", "next_run_at"}} {
		rows, err := tx.Query(ctx, `SELECT id, `+t.schedule+` FROM `+t.table+` WHERE `+t.schedule+` IS NOT NULL`)
		if err != nil {
			return 0, err
		}
		type due struct {
			id   int64
			next time.Time
		}
		var all []due
		for rows.Next() {
			var id int64
			var spec string
			if err := rows.Scan(&id, &spec); err != nil {
				rows.Close()
				return 0, err
			}
			next, err := cron.NextFire(spec, now, zone)
			if err != nil {
				continue
			}
			all = append(all, due{id, next})
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return 0, err
		}
		for _, x := range all {
			if _, err := tx.Exec(ctx, `UPDATE `+t.table+` SET `+t.next+` = $1 WHERE id = $2`, x.next, x.id); err != nil {
				return 0, err
			}
			n++
		}
	}
	return n, tx.Commit(ctx)
}

// engineSettingsRound is engine_panel_settings.one_round: the engine's saved settings sent
// again (with the gateway's upload limit), unless someone is saving them: false then, as
// one_round returns. The settings' own loop runs it too (jobs_engine_settings.go).
func engineSettingsRound(ctx context.Context, d *Deps) (bool, error) {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(engineSettingsLock)).Scan(&got); err != nil {
		return false, err
	}
	if !got {
		return false, nil
	}
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
				return false, errors.New("an engine setting cannot be read")
			}
			values = append(values, pyjson.KV{Key: k, Value: v})
		}
	}
	if _, err := pushDiscarding(ctx, d, values); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}
