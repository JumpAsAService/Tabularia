package api

import (
	"net/http"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

const (
	workersOldEngine = "The engine is an older version that does not report its workers: update it"
	workersNoEngine  = "The engine is not responding: the workers cannot be read right now"
)

// noWorkerFromToken is api_tokens.NO_WORKER_FROM_TOKEN.
var noWorkerFromToken = map[string]string{
	"en": "An API token cannot change the workers or their scaling: do it from the interface",
	"it": "Un token API non cambia i worker né la loro scala: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier les workers ni leur mise à l'échelle : faites-le depuis l'interface",
	"de": "Ein API-Token kann die Worker und ihre Skalierung nicht ändern: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar los workers ni su escalado: hazlo desde la interfaz",
}

// workerOut is routes/workers.py WorkerOut, to validate what the engine reports.
var workerOut = pyd.NewModel("WorkerOut",
	pyd.F("name", pyd.Str()),
	pyd.F("queues", pyd.List(pyd.Str())).DefaultJSON("[]"),
	pyd.F("processes", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("target", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("started_with", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("busy", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("waiting", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("memory_used", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("memory_limit", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("memory_per_process", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("memory_share", pyd.Optional(pyd.Float())).Default(nil),
	pyd.F("memory_floor", pyd.Optional(pyd.Int())).Default(nil),
	pyd.F("autoscale", pyd.Bool()).Default(false),
	pyd.F("started_at", pyd.Optional(pyd.Float())).Default(nil),
	pyd.F("seen_at", pyd.Optional(pyd.Float())).Default(nil),
)

var workerQueues = map[string]string{"run": "celery", "preview": "preview"}

func init() {
	register("workers", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/workers", Handler: deps.Observer(d.Auth, d.DB, d.Log, "/admin/workers", readWorkers(d))},
			{Method: http.MethodPut, Pattern: "/admin/workers/{kind}/scaling",
				Handler: withBody(deps.Superuser(d.Auth, d.DB, d.Log, "/admin/workers/{kind}/scaling", setScaling(d, true)))},
			{Method: http.MethodDelete, Pattern: "/admin/workers/{kind}/scaling",
				Handler: deps.Superuser(d.Auth, d.DB, d.Log, "/admin/workers/{kind}/scaling", setScaling(d, false))},
		}
	})
}

// validated is a value through a pydantic type, false when it does not fit (a 500).
func fitsType(t *pyd.Type, v any) (any, bool) {
	out, errs := t.Validate(v)
	if errs != nil {
		return nil, false
	}
	if mv, ok := out.(*pyd.ModelValue); ok {
		return toPyjson(mv.Dump()), true
	}
	return toPyjson(out), true
}

func dictGet(o *pyd.Object, k string) any {
	if o == nil {
		return nil
	}
	v, _ := o.Get(k)
	return v
}

// workersState is routes/workers.py _state.
func workersState(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	refused := func(status int) {
		detail := workersNoEngine
		if status == http.StatusNotFound {
			detail = workersOldEngine
		}
		server.Refuse(w, &auth.Refusal{Status: http.StatusServiceUnavailable, Detail: detail})
	}
	cat, status, err := engineCatalog(ctx, d)
	if err != nil || status != 0 {
		refused(status)
		return
	}
	liveV, status, err := engineGet(ctx, d, "/workers")
	if err != nil || status != 0 {
		refused(status)
		return
	}
	full, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		fail500(d, w, "workers", err)
		return
	}
	fromToken := p.TokenName != ""
	catalog, ok := cat.(*pyd.Object)
	live, ok2 := liveV.(*pyd.Object)
	if !ok || !ok2 {
		server.InternalError(w) // .get on something that is not a dict
		return
	}
	items := map[string]*pyd.Object{}
	if list, has := catalog.Get("items"); has {
		elems, isList := list.([]any)
		if !isList {
			server.InternalError(w)
			return
		}
		for _, e := range elems {
			o, isDict := e.(*pyd.Object)
			key, hasKey := (*pyd.Object)(nil), false
			_ = key
			if isDict {
				var kv any
				kv, hasKey = o.Get("key")
				if ks, isStr := kv.(string); hasKey && isStr {
					items[ks] = o
					continue
				}
			}
			server.InternalError(w) // v["key"] of an item without one
			return
		}
	}
	saved, err := savedEngineSettings(ctx, d.DB)
	if err != nil {
		fail500(d, w, "workers", err)
		return
	}
	workers, hasWorkers := live.Get("workers")
	queuedV, _ := live.Get("queued")
	groups := []any{}
	for _, kind := range []string{"run", "preview"} {
		key := "workers." + kind + "_concurrency"
		item := items[key]
		row, hasRow := saved[key]
		var queued any
		if pyd.Truthy(queuedV) {
			q, isDict := queuedV.(*pyd.Object)
			if !isDict {
				server.InternalError(w)
				return
			}
			queued = dictGet(q, workerQueues[kind])
		}
		// ConcurrencyOut(...) and the WorkerOut(**w), arguments of GroupOut, are checked as they
		// are built; the scaling is awaited next; GroupOut checks `queued` last
		min, ok2 := fitsType(pyd.Int(), orDefault(item, "min", int64(1)))
		max, ok3 := fitsType(pyd.Int(), orDefault(item, "max", int64(32)))
		applied, ok4 := fitsType(pyd.Optional(pyd.Int()), dictGet(item, "value"))
		if !ok2 || !ok3 || !ok4 {
			server.InternalError(w)
			return
		}
		var value, at, by any
		if hasRow {
			if d.Panel.FromPanel() {
				value = row.Value
			}
			at = pyjson.Isoformat(row.UpdatedAt)
			by = updatedBy(row.UpdatedBy, full)
		}
		list := []any{}
		if pyd.Truthy(workers) {
			ws, isList := workers.([]any)
			if !isList {
				server.InternalError(w)
				return
			}
			for _, e := range ws {
				o, isDict := e.(*pyd.Object)
				if !isDict {
					server.InternalError(w)
					return
				}
				if k, _ := o.Get("kind"); k != kind {
					continue
				}
				out, ok := fitsType(pyd.ModelOf(workerOut), o)
				if !ok {
					server.InternalError(w)
					return
				}
				list = append(list, out)
			}
		}
		scaling, err := workerScaling(ctx, d, kind, full)
		if err != nil {
			fail500(d, w, "workers: the cluster", err)
			return
		}
		queuedOut, ok := fitsType(pyd.Optional(pyd.Int()), queued)
		if !ok {
			server.InternalError(w)
			return
		}
		groups = append(groups, pyjson.Object{
			{Key: "kind", Value: kind}, {Key: "queue", Value: workerQueues[kind]}, {Key: "queued", Value: queuedOut},
			{Key: "concurrency", Value: pyjson.Object{{Key: "key", Value: key}, {Key: "min", Value: min}, {Key: "max", Value: max},
				{Key: "value", Value: value}, {Key: "applied", Value: applied}, {Key: "updated_at", Value: at}, {Key: "updated_by", Value: by}}},
			{Key: "workers", Value: list},
			{Key: "scaling", Value: scaling},
		})
	}
	tick, _ := live.Get("tick_seconds")
	if !pyd.Truthy(tick) {
		tick = int64(10)
	}
	tickOut, ok := fitsType(pyd.Float(), tick)
	if !ok {
		server.InternalError(w)
		return
	}
	writePy(w, pyjson.Object{
		{Key: "groups", Value: groups}, {Key: "workers_known", Value: hasWorkers && workers != nil},
		{Key: "concurrency_editable", Value: full && d.Panel.FromPanel() && !fromToken},
		{Key: "scaling_editable", Value: full && kube.FromEnv().Scaling && !fromToken},
		{Key: "panel_enabled", Value: d.Panel.FromPanel()}, {Key: "tick_seconds", Value: tickOut},
	})
}

func orDefault(o *pyd.Object, k string, def any) any {
	if o == nil {
		return def
	}
	if v, ok := o.Get(k); ok {
		return v
	}
	return def
}

// noScaling is ScalingOut(mode="none", on_kubernetes=on_kubernetes()).
func noScaling(onKubernetes bool) pyjson.Object {
	out := pyjson.Object{{Key: "mode", Value: "none"}, {Key: "on_kubernetes", Value: onKubernetes}}
	for _, k := range []string{"deployment", "autoscaler", "autoscaler_name", "replicas", "current", "ready", "desired", "min", "max",
		"floor", "ceiling"} {
		out = append(out, pyjson.KV{Key: k, Value: nil})
	}
	out = append(out, pyjson.KV{Key: "saved", Value: false})
	for _, k := range []string{"saved_min", "saved_max", "updated_at", "updated_by", "error"} {
		out = append(out, pyjson.KV{Key: k, Value: nil})
	}
	return out
}

// readWorkers is GET /admin/workers.
func readWorkers(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { workersState(d, w, r, p) }
}

// setScaling is PUT and DELETE /admin/workers/{kind}/scaling (scala, give_back): a token
// refused first (_no_token), then the parameters, then the 409 of _scaling_on.
func setScaling(d *Deps, put bool) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if p.TokenName != "" {
			server.Refuse(w, &auth.Refusal{Status: http.StatusForbidden, Detail: language.Pick(noWorkerFromToken, r.Header.Get("Accept-Language"))})
			return
		}
		var ps pyd.Params
		kindV := ps.Path("kind", r.PathValue("kind"), pyd.Literal("run", "preview"))
		var body *pyd.ModelValue
		if put {
			body = ps.Body(bodyOf(r), gatewaymodels.ScalingIn)
		}
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		if !kube.FromEnv().Scaling {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "On this installation the workers are not scaled from the panel: " +
				"it works on Kubernetes, with adminScaling.enabled in the Helm chart (KUBERNETES__SCALING=true)"})
			return
		}
		kind := kindV.(string)
		if put {
			changeScaling(d, w, r, p, kind, body.Get("min_replicas").(int64), body.Get("max_replicas").(int64))
		} else {
			giveBackScaling(d, w, r, p, kind)
		}
	}
}
