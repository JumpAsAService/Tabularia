package api

// routes/gateway_settings.py rollout_router: the rollout of the components that read a changed
// setting only when they start (services/settings_rollout.py, the panel's part; the eviction
// loop is the orchestrator's).

import (
	"context"
	"errors"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/apitokens"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// rolloutOrder is settings_rollout.ORDER: the orchestrator, which runs a rollout, last.
var rolloutOrder = []string{"worker", "preview-worker", "beat", "engine", "gateway", "orchestrator"}

// rolloutCompose is settings_rollout.COMPOSE.
var rolloutCompose = map[string]string{"worker": "worker", "preview-worker": "preview-worker", "beat": "beat", "engine": "backend",
	"gateway": "gateway", "orchestrator": "gateway"}

const envOnlySettings = "On this installation the settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)"

func init() {
	register("settings_rollout", func(d *Deps) []server.Route {
		admin := func(route string, h authedFunc) http.Handler {
			return superuser(d, route, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
				// _no_token, a dependency of the decorator: before the body's validation
				if ref := apitokens.DenyIfFromToken(r, p, noSettingsFromToken); ref != nil {
					server.Refuse(w, ref)
					return
				}
				h(w, r, p)
			})
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/settings-rollout", Handler: observer(d, "/admin/settings-rollout", rolloutState(d))},
			{Method: http.MethodPost, Pattern: "/admin/settings-rollout", Handler: withBody(admin("/admin/settings-rollout", rolloutStart(d)))},
			{Method: http.MethodDelete, Pattern: "/admin/settings-rollout/{component}",
				Handler: admin("/admin/settings-rollout/{component}", rolloutCancel(d))},
		}
	})
}

// rolloutComposeService is settings_rollout.compose_service.
func rolloutComposeService(component string) string {
	if component == "orchestrator" && scriptcfg.GetRuntime().Role == "api" {
		return "orchestrator"
	}
	return rolloutCompose[component]
}

// rolloutMode is settings_rollout.mode: kubernetes, manual or compose.
func rolloutMode() string {
	k := kube.FromEnv()
	if !k.OnKubernetes() {
		return "compose"
	}
	rt := scriptcfg.GetRuntime()
	if rt.Rollout && rt.Release != "" && fileExists(kubeAPI().TokenFile) {
		return "kubernetes"
	}
	return "manual"
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// ── the pods ────────────────────────────────────────────────────────────────

func podMeta(pod any) pyjson.Object {
	o, _ := pod.(pyjson.Object)
	m, _ := o.Get("metadata")
	mo, _ := m.(pyjson.Object)
	return mo
}

// k8sTime is datetime.fromisoformat(raw.replace("Z", "+00:00")) in UTC, naive; ok=false
// where Python gets an AttributeError or a ValueError.
func k8sTime(raw any) (time.Time, bool) {
	s, isStr := raw.(string)
	if !isStr {
		return time.Time{}, false
	}
	s = strings.ReplaceAll(s, "Z", "+00:00")
	for _, layout := range []string{"2006-01-02T15:04:05.999999999-07:00", "2006-01-02T15:04:05.999999999",
		"2006-01-02 15:04:05.999999999-07:00", "2006-01-02 15:04:05.999999999", "2006-01-02T15:04-07:00", "2006-01-02T15:04",
		"2006-01-02"} {
		if t, err := time.Parse(layout, s); err == nil {
			u := t.UTC()
			return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond()/1000*1000, time.UTC), true
		}
	}
	return time.Time{}, false
}

// podCreated is _created; the zero time stands for datetime.min.
func podCreated(pod any) (time.Time, bool) {
	v, _ := podMeta(pod).Get("creationTimestamp")
	return k8sTime(v)
}

var datetimeMin = time.Date(1, 1, 1, 0, 0, 0, 0, time.UTC)

func createdOrMin(pod any) time.Time {
	if t, ok := podCreated(pod); ok {
		return t
	}
	return datetimeMin
}

func podLeaving(pod any) bool {
	v, _ := podMeta(pod).Get("deletionTimestamp")
	return pyTruthy(v)
}

// podReady is _ready.
func podReady(pod any) (bool, error) {
	if podLeaving(pod) {
		return false, nil
	}
	o, _ := pod.(pyjson.Object)
	st, _ := o.Get("status")
	so, _ := st.(pyjson.Object)
	if !pyTruthy(st) {
		so = nil
	}
	if ph, _ := so.Get("phase"); ph != "Running" {
		return false, nil
	}
	cs, _ := so.Get("conditions")
	list, isList := cs.([]any)
	if pyTruthy(cs) && !isList {
		return false, errors.New("settings_rollout: conditions not a list")
	}
	for _, c := range list {
		co, ok := c.(pyjson.Object)
		if !ok {
			return false, errors.New("settings_rollout: a condition not a dict")
		}
		t, _ := co.Get("type")
		s, _ := co.Get("status")
		if t == "Ready" && s == "True" {
			return true, nil
		}
	}
	return false, nil
}

// podName is _name.
func podName(pod any) string {
	v, _ := podMeta(pod).Get("name")
	if !pyTruthy(v) {
		return ""
	}
	return pyjson.Str(v)
}

// rolloutPods is settings_rollout.pods: the component's pods not finished.
func rolloutPods(ctx context.Context, component string) ([]any, error) {
	rt := scriptcfg.GetRuntime()
	selector := "app.kubernetes.io/instance=" + rt.Release + ",app.kubernetes.io/component=" + component
	if rt.AppName != "" {
		selector += ",app.kubernetes.io/name=" + rt.AppName
	}
	out, err := kube.Call(ctx, kubeAPI(), http.MethodGet,
		"/api/v1/namespaces/"+rt.ReleaseNamespace()+"/pods?labelSelector="+kube.Quote(selector), nil, false)
	if err != nil {
		return nil, err
	}
	o, ok := out.(pyjson.Object)
	if !ok {
		return nil, errors.New("settings_rollout: the pods' answer is not a dict")
	}
	items, _ := o.Get("items")
	list, isList := items.([]any)
	if pyTruthy(items) && !isList {
		return nil, errors.New("settings_rollout: items not a list")
	}
	res := []any{}
	for _, p := range list {
		po, ok := p.(pyjson.Object)
		if !ok {
			return nil, errors.New("settings_rollout: a pod not a dict")
		}
		st, _ := po.Get("status")
		so, isObj := st.(pyjson.Object)
		if pyTruthy(st) && !isObj {
			return nil, errors.New("settings_rollout: a status not a dict")
		}
		ph, _ := so.Get("phase")
		if ph == "Succeeded" || ph == "Failed" {
			continue
		}
		res = append(res, p)
	}
	return res, nil
}

// rolloutOld is settings_rollout.old: the pods started before `since`, oldest first, not
// already on their way out.
func rolloutOld(pods []any, since *time.Time) []any {
	if since == nil {
		return nil
	}
	var out []any
	for _, p := range pods {
		if !podLeaving(p) && createdOrMin(p).Before(*since) {
			out = append(out, p)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return createdOrMin(out[i]).Before(createdOrMin(out[j])) })
	return out
}

// ── the state, for the panel ────────────────────────────────────────────────

type rolloutRow struct {
	changed, requested *time.Time
	by                 *string
}

// rolloutStatus is settings_rollout.status, with the masking of _rollout.
func rolloutStatus(ctx context.Context, d *Deps, full bool) (pyjson.Object, error) {
	m := rolloutMode()
	panel := d.Panel.FromPanel()
	rows := map[string]rolloutRow{}
	rs, err := d.DB.Query(ctx, `SELECT component, changed_at, requested_at, requested_by FROM settings_rollouts`)
	if err != nil {
		return nil, err
	}
	for rs.Next() {
		var c string
		var r rolloutRow
		if err := rs.Scan(&c, &r.changed, &r.requested, &r.by); err != nil {
			rs.Close()
			return nil, err
		}
		rows[c] = r
	}
	rs.Close()
	if err := rs.Err(); err != nil {
		return nil, err
	}
	// the six components' pods read together, not one after the other
	type read struct {
		pods []any
		err  error
	}
	reads := make([]read, len(rolloutOrder))
	if panel && m == "kubernetes" {
		var wg sync.WaitGroup
		for i, c := range rolloutOrder {
			wg.Add(1)
			go func() {
				defer wg.Done()
				reads[i].pods, reads[i].err = rolloutPods(ctx, c)
			}()
		}
		wg.Wait()
	}
	items := []any{}
	for i, c := range rolloutOrder {
		r := rows[c]
		var by any
		if r.by != nil {
			by = *r.by
			if *r.by != "" && !full {
				by = masking.Email(*r.by)
			}
		}
		pending := false
		var pods, ready, old, errText any
		waiting := []any{}
		switch {
		case !panel:
			// ENGINE__SETTINGS_PANEL=false: the saved values do not apply, nothing to restart for
		case m == "kubernetes":
			if e := reads[i].err; e != nil {
				var ke *kube.Error
				if !errors.As(e, &ke) {
					return nil, e
				}
				// not known: not offered for a restart (it would restart every pod), the error said
				errText = ke.Message
			} else {
				pl := reads[i].pods
				stale := rolloutOld(pl, r.changed)
				n := 0
				for _, p := range pl {
					ok, err := podReady(p)
					if err != nil {
						return nil, err
					}
					if ok {
						n++
					} else {
						waiting = append(waiting, podName(p))
					}
				}
				pods, ready, old, pending = int64(len(pl)), int64(n), int64(len(stale)), len(stale) > 0
			}
		default:
			// without pods to look at: pending from the change until the administrator says done
			pending = r.changed != nil && (r.requested == nil || r.requested.Before(*r.changed))
		}
		items = append(items, pyjson.Object{
			{Key: "component", Value: c}, {Key: "changed_at", Value: isoPtr(r.changed)},
			{Key: "requested_at", Value: isoPtr(r.requested)}, {Key: "requested_by", Value: by},
			{Key: "pending", Value: pending}, {Key: "compose_service", Value: rolloutComposeService(c)},
			{Key: "pods", Value: pods}, {Key: "ready", Value: ready}, {Key: "old", Value: old},
			{Key: "waiting_for", Value: waiting}, {Key: "error", Value: errText},
		})
	}
	namespace := ""
	if m != "compose" {
		namespace = scriptcfg.GetRuntime().ReleaseNamespace()
	}
	return pyjson.Object{{Key: "mode", Value: m}, {Key: "components", Value: items}, {Key: "namespace", Value: namespace},
		{Key: "release", Value: scriptcfg.GetRuntime().Release}}, nil
}

// sendRollout is _rollout: the state, as RolloutOut.
func sendRollout(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	full, err := isAdmin(d, r, p)
	if err != nil {
		fail(d, w, "settings rollout: the roles", err)
		return
	}
	st, err := rolloutStatus(r.Context(), d, full)
	if err != nil {
		fail(d, w, "settings rollout: the state", err)
		return
	}
	writePy(w, append(st, pyjson.KV{Key: "editable", Value: full && d.Panel.FromPanel() && p.TokenName == ""}))
}

// rolloutState is GET /admin/settings-rollout (rollout_state).
func rolloutState(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { sendRollout(d, w, r, p) }
}

// rolloutStart is POST /admin/settings-rollout (rollout_start): Kubernetes, the orchestrator
// restarts their pods; Docker Compose, the administrator says they restarted them.
func rolloutStart(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.RolloutRequest)
		if !validated(w, &ps) {
			return
		}
		if !d.Panel.FromPanel() {
			detail(w, http.StatusForbidden, envOnlySettings)
			return
		}
		asked := map[string]bool{}
		var unknown []string
		for _, c := range body.Get("components").([]any) {
			s := c.(string)
			asked[s] = true
			if rolloutCompose[s] == "" {
				unknown = append(unknown, s)
			}
		}
		if len(unknown) > 0 {
			detail(w, http.StatusUnprocessableEntity, "Unknown components: "+strings.Join(unknown, ", "))
			return
		}
		ctx := r.Context()
		now := nowNaive()
		done := []any{}
		var labels []string
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			fail(d, w, "settings rollout", err)
			return
		}
		defer tx.Rollback(ctx)
		for _, c := range rolloutOrder {
			if !asked[c] {
				continue
			}
			if _, err := tx.Exec(ctx, `INSERT INTO settings_rollouts (component, requested_at, requested_by) VALUES ($1, $2, $3)
				ON CONFLICT (component) DO UPDATE SET requested_at = EXCLUDED.requested_at, requested_by = EXCLUDED.requested_by`,
				c, now, p.User.Email); err != nil {
				fail(d, w, "settings rollout", err)
				return
			}
			done = append(done, c)
			labels = append(labels, c)
		}
		if err := tx.Commit(ctx); err != nil {
			fail(d, w, "settings rollout", err)
			return
		}
		kind, label := "settings", strings.Join(labels, ", ")
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "settings.rollout", TargetType: &kind, TargetLabel: &label,
			Detail: pyjson.Object{{Key: "components", Value: done}, {Key: "mode", Value: rolloutMode()}}})
		sendRollout(d, w, r, p)
	}
}

// rolloutCancel is DELETE /admin/settings-rollout/{component} (rollout_cancel): the pods
// already restarted stay, the others keep the old value.
func rolloutCancel(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		component := r.PathValue("component")
		tag, err := d.DB.Exec(ctx, `UPDATE settings_rollouts SET requested_at = NULL WHERE component = $1 AND requested_at IS NOT NULL`,
			component)
		if err != nil {
			fail(d, w, "settings rollout", err)
			return
		}
		if tag.RowsAffected() > 0 {
			kind := "settings"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "settings.rollout_cancel", TargetType: &kind,
				TargetLabel: &component, Detail: pyjson.Object{{Key: "component", Value: component}}})
		}
		sendRollout(d, w, r, p)
	}
}
