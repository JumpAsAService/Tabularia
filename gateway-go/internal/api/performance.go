package api

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"sort"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/deps"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// maxPerformanceRuns is routes/performance.py MAX_RUNS.
const maxPerformanceRuns = 20_000

func init() {
	register("performance", func(d *Deps) []server.Route {
		obs := func(route string, h deps.Handler) http.Handler { return deps.Observer(d.Auth, d.DB, d.Log, route, h) }
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/performance/runs", Handler: obs("/admin/performance/runs", runsPerformance(d))},
			{Method: http.MethodGet, Pattern: "/admin/performance/previews", Handler: obs("/admin/performance/previews", previewsPerformance(d))},
			{Method: http.MethodGet, Pattern: "/admin/performance/warehouse", Handler: obs("/admin/performance/warehouse", warehousePerformance(d))},
		}
	})
}

// writePy answers a value as Starlette writes a dict: Python's float repr, keys in order.
func writePy(w http.ResponseWriter, v any) {
	b, err := pyjson.Dumps(v, pyjson.Options{Compact: true})
	if err != nil {
		server.InternalError(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(b))
}

// median is statistics.median.
func median(v []float64) any {
	if len(v) == 0 {
		return nil
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	n := len(s)
	if n%2 == 1 {
		return s[n/2]
	}
	return (s[n/2-1] + s[n/2]) / 2
}

// pct is _pct: the value at round(q·(n-1)), Python's round (half to even).
func pct(v []float64, q float64) any {
	if len(v) == 0 {
		return nil
	}
	s := append([]float64(nil), v...)
	sort.Float64s(s)
	return s[min(len(s)-1, int(math.RoundToEven(q*float64(len(s)-1))))]
}

// seconds is timedelta.total_seconds(): whole microseconds over 10**6.
func seconds(d time.Duration) float64 { return float64(d.Microseconds()) / 1e6 }

type perfRun struct {
	kind, status                      string
	flowID, datasourceID, rowsWritten *int64
	errText                           *string
	started, finished, engineStarted  *time.Time
}

type perfGroup struct {
	kind                 string
	id                   *int64
	name                 any
	runs, failures, rows int64
	durations, waits     []float64
	lastError            any
	lastRunAt            *time.Time
}

func names(ctx context.Context, d *Deps, table string) (map[int64]string, error) {
	rows, err := d.DB.Query(ctx, `SELECT id, name FROM `+table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]string{}
	for rows.Next() {
		var id int64
		var n string
		if err := rows.Scan(&id, &n); err != nil {
			return nil, err
		}
		out[id] = n
	}
	return out, rows.Err()
}

func sumFloats(v []float64) any {
	if len(v) == 0 {
		return int64(0) // sum([]) is the int 0
	}
	t := 0.0
	for _, x := range v {
		t += x
	}
	return t
}

// runsPerformance is GET /admin/performance/runs: the runs' aggregates over the window.
func runsPerformance(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		daysV := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "days", pyd.Int().Ge(1).Le(90), 7)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		days := daysV.(int64)
		ctx := r.Context()
		since := time.Now().UTC().Add(-time.Duration(days) * 24 * time.Hour)
		rs, err := d.DB.Query(ctx, `SELECT kind, status, flow_id, datasource_id, rows_written, error, started_at, finished_at,
			engine_started_at FROM runs WHERE started_at >= $1 ORDER BY started_at DESC LIMIT $2`, since, maxPerformanceRuns)
		if err != nil {
			fail500(d, w, "performance", err)
			return
		}
		var runs []perfRun
		for rs.Next() {
			var p perfRun
			if err := rs.Scan(&p.kind, &p.status, &p.flowID, &p.datasourceID, &p.rowsWritten, &p.errText, &p.started, &p.finished,
				&p.engineStarted); err != nil {
				rs.Close()
				fail500(d, w, "performance", err)
				return
			}
			runs = append(runs, p)
		}
		rs.Close()
		if err := rs.Err(); err != nil {
			fail500(d, w, "performance", err)
			return
		}
		flows, err := names(ctx, d, "flows")
		if err != nil {
			fail500(d, w, "performance", err)
			return
		}
		sources, err := names(ctx, d, "datasources")
		if err != nil {
			fail500(d, w, "performance", err)
			return
		}
		type key struct {
			kind string
			id   int64
			none bool
		}
		groups := map[key]*perfGroup{}
		var order []*perfGroup
		for _, run := range runs {
			kind, ident := "flow", run.flowID
			if run.kind == "ingest" {
				kind, ident = "ingest", run.datasourceID
			}
			k := key{kind: kind, none: ident == nil}
			if ident != nil {
				k.id = *ident
			}
			g := groups[k]
			if g == nil {
				g = &perfGroup{kind: kind, id: ident}
				src := flows
				if kind == "ingest" {
					src = sources
				}
				if ident != nil {
					if n, ok := src[*ident]; ok {
						g.name = n
					}
				}
				groups[k] = g
				order = append(order, g)
			}
			g.runs++
			if g.lastRunAt == nil {
				g.lastRunAt = run.started
			}
			if run.status == "FAILURE" {
				g.failures++
				if !pyd.Truthy(g.lastError) {
					text := ""
					if run.errText != nil {
						text = engineText([]byte(*run.errText), 240)
					}
					g.lastError = text
				}
			}
			if run.status == "SUCCESS" && run.started != nil && run.finished != nil {
				from := run.started
				if run.engineStarted != nil {
					from = run.engineStarted
				}
				g.durations = append(g.durations, seconds(run.finished.Sub(*from)))
				if run.rowsWritten != nil {
					g.rows += *run.rowsWritten
				}
			}
			if run.started != nil && run.engineStarted != nil {
				g.waits = append(g.waits, math.Max(0, seconds(run.engineStarted.Sub(*run.started))))
			}
		}
		type item struct {
			obj   pyjson.Object
			total float64
		}
		items := make([]item, 0, len(order))
		for _, g := range order {
			var maxS any
			if len(g.durations) > 0 {
				m := g.durations[0]
				for _, x := range g.durations[1:] {
					if x > m {
						m = x
					}
				}
				maxS = m
			}
			var last any
			if g.lastRunAt != nil {
				last = pyjson.Isoformat(*g.lastRunAt)
			}
			var id any
			if g.id != nil {
				id = *g.id
			}
			total := sumFloats(g.durations)
			tf, _ := total.(float64)
			items = append(items, item{total: tf, obj: pyjson.Object{
				{Key: "kind", Value: g.kind}, {Key: "id", Value: id}, {Key: "name", Value: g.name}, {Key: "runs", Value: g.runs},
				{Key: "failures", Value: g.failures}, {Key: "rows", Value: g.rows}, {Key: "last_error", Value: g.lastError},
				{Key: "last_run_at", Value: last}, {Key: "median_s", Value: median(g.durations)}, {Key: "p95_s", Value: pct(g.durations, 0.95)},
				{Key: "max_s", Value: maxS}, {Key: "total_s", Value: total}, {Key: "median_wait_s", Value: median(g.waits)},
				{Key: "p95_wait_s", Value: pct(g.waits, 0.95)},
			}})
		}
		sort.SliceStable(items, func(i, j int) bool { return -items[i].total < -items[j].total })
		var waits []float64
		failures := 0
		for _, run := range runs {
			if run.engineStarted != nil && run.started != nil {
				waits = append(waits, math.Max(0, seconds(run.engineStarted.Sub(*run.started))))
			}
			if run.status == "FAILURE" {
				failures++
			}
		}
		list := make([]any, 0, min(len(items), 100))
		for i := 0; i < len(items) && i < 100; i++ {
			list = append(list, items[i].obj)
		}
		writePy(w, pyjson.Object{
			{Key: "days", Value: days}, {Key: "truncated", Value: len(runs) >= maxPerformanceRuns},
			{Key: "totals", Value: pyjson.Object{{Key: "runs", Value: len(runs)}, {Key: "failures", Value: failures},
				{Key: "median_wait_s", Value: median(waits)}, {Key: "p95_wait_s", Value: pct(waits, 0.95)}}},
			{Key: "items", Value: list},
		})
	}
}

// performanceEngine is _engine: the engine's observability, a 502 when it refuses.
func performanceEngine(d *Deps, w http.ResponseWriter, r *http.Request, path, query string) {
	res, raw, err := d.Engine.JSON(r.Context(), http.MethodGet, path+"?"+query, nil)
	if err != nil {
		fail500(d, w, "performance", err)
		return
	}
	if res.StatusCode >= 400 {
		server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway,
			Detail: fmt.Sprintf("The engine is not responding on %s (%d)", path, res.StatusCode)})
		return
	}
	engineJSON(w, raw)
}

// previewsPerformance is GET /admin/performance/previews.
func previewsPerformance(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		limit := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(50), 25)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		performanceEngine(d, w, r, "/observability/previews", fmt.Sprintf("limit=%d", limit))
	}
}

// warehousePerformance is GET /admin/performance/warehouse.
func warehousePerformance(d *Deps) deps.Handler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		q := pyd.ParseQuery(r.URL.RawQuery)
		minutes := ps.Query(q, "minutes", pyd.Int().Ge(5).Le(1440), 60)
		limit := ps.Query(q, "limit", pyd.Int().Ge(1).Le(50), 20)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		// httpx writes the params in the order given: minutes, then limit
		performanceEngine(d, w, r, "/observability/warehouse", fmt.Sprintf("minutes=%d&limit=%d", minutes, limit))
	}
}
