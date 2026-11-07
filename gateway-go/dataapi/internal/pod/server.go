package pod

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Server is a pod: the manifest it serves, the prepared tables, the keys' buckets and counts.
type Server struct {
	cfg      Config
	log      *slog.Logger
	gateway  *Gateway
	prep     *Preparer
	limiter  *Limiter
	usage    *Usage
	clientIP *clientip.Resolver
	now      func() time.Time

	snap atomic.Pointer[snapshot]

	mu     sync.RWMutex
	tables map[string]*Table
	// a generation that failed to prepare, and when to try it again
	failed map[string]failure
	seq    atomic.Int64

	stopping atomic.Bool
	wake     chan struct{}
}

type snapshot struct {
	m         *dataapiwire.Manifest
	keys      *Keys
	resources map[string]*dataapiwire.Resource
	at        time.Time // when the pod received it
}

type failure struct {
	next  time.Time
	wait  time.Duration
	error string
}

// NewServer is a pod with nothing loaded yet.
func NewServer(cfg Config, log *slog.Logger, gateway *Gateway, prep *Preparer) *Server {
	return &Server{cfg: cfg, log: log, gateway: gateway, prep: prep, limiter: NewLimiter(time.Now), usage: NewUsage(),
		clientIP: clientip.New(cfg.ForwardedAllowIPs), now: time.Now, tables: map[string]*Table{}, failed: map[string]failure{},
		wake: make(chan struct{}, 1)}
}

// ── the manifest ──────────────────────────────────────────────────────────────

// Apply takes a manifest in: the keys at once, the data as it gets prepared.
func (s *Server) Apply(m *dataapiwire.Manifest) {
	snap := &snapshot{m: m, keys: NewKeys(m.Keys), resources: map[string]*dataapiwire.Resource{}, at: s.now()}
	for i := range m.Resources {
		snap.resources[m.Resources[i].Name] = &m.Resources[i]
	}
	s.snap.Store(snap)
	// what is no longer published, or no longer readable on the creator's authority, stops now
	s.mu.Lock()
	for name, t := range s.tables {
		r := snap.resources[name]
		if m.State != dataapiwire.StateActive || r == nil || r.Unavailable == dataapiwire.UnavailableRights {
			delete(s.tables, name)
			t.retire()
		}
	}
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// pending are the resources whose served data is not the manifest's
func (s *Server) pending() []dataapiwire.Resource {
	snap := s.snap.Load()
	if snap == nil || snap.m.State != dataapiwire.StateActive {
		return nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := []dataapiwire.Resource{}
	for _, r := range snap.m.Resources {
		if !r.Available {
			continue
		}
		if t := s.tables[r.Name]; t != nil && t.Generation == Generation(r) {
			continue
		}
		if f, ok := s.failed[r.Name+"/"+Generation(r)]; ok && s.now().Before(f.next) {
			continue
		}
		out = append(out, r)
	}
	return out
}

// PrepareAll prepares, one after the other, what the manifest serves and the pod does not yet.
func (s *Server) PrepareAll(ctx context.Context) {
	for _, r := range s.pending() {
		if ctx.Err() != nil {
			return
		}
		// the latest manifest's link: one taken before a long preparation may have expired
		if latest := s.snap.Load().resources[r.Name]; latest != nil && latest.Available && Generation(*latest) == Generation(r) {
			r = *latest
		}
		s.prepareOne(ctx, r)
	}
}

func (s *Server) prepareOne(ctx context.Context, r dataapiwire.Resource) {
	started := s.now()
	snap := s.snap.Load()
	serveMB, prepareMB, prepareThreads := s.cfg.Budget(len(snap.m.Resources))
	name := fmt.Sprintf("r%d", s.seq.Add(1))
	// serving, one thread per query: a lookup touches one block, and the requests run side by
	// side on their own connections (more threads per query made a lookup three times slower)
	t, err := s.prep.Prepare(ctx, r, name, Budgeted{ServeMB: serveMB, PrepareMB: prepareMB, PrepareThreads: prepareThreads,
		ServeThreads: 1})
	key := r.Name + "/" + Generation(r)
	if err != nil {
		s.mu.Lock()
		f := s.failed[key]
		f.wait = min(max(f.wait*2, 30*time.Second), 10*time.Minute)
		f.next, f.error = s.now().Add(f.wait), err.Error()
		s.failed[key] = f
		s.mu.Unlock()
		s.log.Error("prepare", "resource", r.Name, "version", r.Version, "err", err.Error(), "retry_in_s", int(f.wait.Seconds()))
		return
	}
	// the manifest may have moved on meanwhile: the table goes in only if it is still wanted
	now := s.snap.Load()
	want := now.resources[r.Name]
	if now.m.State != dataapiwire.StateActive || want == nil || !want.Available || Generation(*want) != t.Generation {
		t.retire()
		return
	}
	s.mu.Lock()
	old := s.tables[r.Name]
	s.tables[r.Name] = t
	delete(s.failed, key)
	s.mu.Unlock()
	if old != nil {
		old.retire()
	}
	s.log.Info("prepared", "resource", r.Name, "version", r.Version, "ms", time.Since(started).Milliseconds())
}

func (s *Server) table(name string) *Table {
	s.mu.RLock()
	t := s.tables[name]
	s.mu.RUnlock()
	if t == nil || !t.acquire() {
		return nil
	}
	return t
}

// stale says whether the manifest is too old to be trusted with the keys
func (s *Server) stale(snap *snapshot) bool { return s.now().Sub(snap.at) > s.cfg.ManifestMaxAge }

// Ready says whether the pod serves: an active data API, a fresh manifest, and every resource
// available prepared.
func (s *Server) Ready() bool {
	snap := s.snap.Load()
	if s.stopping.Load() || snap == nil || snap.m.State != dataapiwire.StateActive || s.stale(snap) {
		return false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, r := range snap.m.Resources {
		if r.Available && s.tables[r.Name] == nil {
			return false
		}
	}
	return true
}

// Run polls the gateway and prepares the data until `ctx` ends; then the last usage goes.
func (s *Server) Run(ctx context.Context) {
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-s.wake:
				s.PrepareAll(ctx)
			case <-time.After(30 * time.Second): // a failed preparation's next try
				s.PrepareAll(ctx)
			}
		}
	}()
	poll := s.cfg.Poll
	for {
		if m, err := s.gateway.Manifest(ctx); err != nil {
			if ctx.Err() == nil {
				s.log.Error("manifest", "err", err.Error())
			}
		} else {
			s.Apply(m)
			if m.PollSeconds > 0 {
				poll = time.Duration(m.PollSeconds) * time.Second
			}
		}
		s.sendUsage(ctx)
		select {
		case <-ctx.Done():
			return
		case <-time.After(poll):
		}
	}
}

func (s *Server) sendUsage(ctx context.Context) {
	list := s.usage.Take()
	if len(list) == 0 {
		return
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Minute.Before(list[j].Minute) })
	if len(list) > dataapiwire.MaxUsageMinutes {
		s.usage.Return(list[dataapiwire.MaxUsageMinutes:])
		list = list[:dataapiwire.MaxUsageMinutes]
	}
	if err := s.gateway.SendUsage(ctx, dataapiwire.Usage{Pod: s.cfg.PodName, Minutes: list}); err != nil {
		s.log.Error("usage", "err", err.Error())
		s.usage.Return(list)
	}
}

// FlushUsage sends what is left, at the end.
func (s *Server) FlushUsage(ctx context.Context) { s.sendUsage(ctx) }

// Stop takes the pod out of service (readiness first) and closes the tables.
func (s *Server) Stop() {
	s.stopping.Store(true)
}

// Close retires every table.
func (s *Server) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for name, t := range s.tables {
		delete(s.tables, name)
		t.retire()
	}
}

// ── HTTP ──────────────────────────────────────────────────────────────────────

// what a request did, for its log line and the usage
type record struct {
	key      *KeyRule
	resource string
	rows     int
	code     string
}

type recordKey struct{}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

// Handler is the pod's HTTP server.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "alive"}) })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if s.Ready() {
			writeJSON(w, 200, map[string]string{"status": "ready"})
		} else {
			writeJSON(w, 503, map[string]string{"status": "not ready"})
		}
	})
	mux.HandleFunc("/data/{api}/health", s.health)
	mux.HandleFunc("/data/{api}/openapi.json", s.openAPI)
	mux.HandleFunc("/data/{api}/{resource}", s.data)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		problem(w, &Problem{Status: 404, Code: "not_found", Detail: "Nothing here: the data are at /data/{api}/{resource}"})
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		rec := &record{}
		sw := &statusWriter{ResponseWriter: w, status: 200}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			problem(sw, &Problem{Status: 405, Code: "method_not_allowed", Detail: "A data API is read-only: GET only"})
		} else {
			mux.ServeHTTP(sw, r.WithContext(context.WithValue(r.Context(), recordKey{}, rec)))
		}
		if r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			return
		}
		attrs := []any{"method", r.Method, "path", r.URL.Path, "status", sw.status, "rows", rec.rows,
			"ms", float64(time.Since(started).Microseconds()) / 1000, "client", s.clientIP.Of(r)}
		if rec.key != nil {
			attrs = append(attrs, "key", rec.key.Hint, "key_id", rec.key.ID)
			s.usage.Record(rec.key.ID, s.now(), sw.status, rec.rows)
		}
		if rec.resource != "" {
			attrs = append(attrs, "resource", rec.resource)
		}
		if rec.code != "" {
			attrs = append(attrs, "code", rec.code)
		}
		s.log.Info("request", attrs...)
	})
}

func recordOf(r *http.Request) *record {
	if rec, ok := r.Context().Value(recordKey{}).(*record); ok {
		return rec
	}
	return &record{}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	b, err := marshal(v)
	if err != nil {
		status, b = 500, []byte(`{"detail":"The answer could not be written","code":"internal"}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	w.WriteHeader(status)
	_, _ = w.Write(b)
}

// marshal is JSON without HTML escaping
func marshal(v any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), nil
}

func problem(w http.ResponseWriter, p *Problem) {
	if p.RetryAfter > 0 {
		w.Header().Set("Retry-After", strconv.Itoa(p.RetryAfter))
	}
	if p.Status == 401 {
		w.Header().Set("WWW-Authenticate", `Bearer realm="data API"`)
	}
	writeJSON(w, p.Status, map[string]string{"detail": p.Detail, "code": p.Code})
}

func (s *Server) fail(w http.ResponseWriter, r *http.Request, p *Problem) {
	recordOf(r).code = p.Code
	problem(w, p)
}

// GET /data/{api}/health: whether the data API answers, without a key.
func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	snap := s.snap.Load()
	if snap == nil || r.PathValue("api") != snap.m.Slug {
		if snap == nil {
			writeJSON(w, 503, map[string]string{"status": "loading"})
			return
		}
		s.fail(w, r, &Problem{Status: 404, Code: "not_found", Detail: "No such data API"})
		return
	}
	status, code := "ready", 200
	switch {
	case snap.m.State != dataapiwire.StateActive:
		status, code = snap.m.State, 503
	case s.stale(snap):
		status, code = "stale", 503
	case !s.Ready():
		status, code = "loading", 503
	}
	writeJSON(w, code, map[string]string{"status": status})
}

// admit is what every keyed route checks first: the data API, its state, the key, its
// address and its rate.
func (s *Server) admit(w http.ResponseWriter, r *http.Request) (*snapshot, *KeyRule, bool) {
	snap := s.snap.Load()
	if snap == nil {
		s.fail(w, r, &Problem{Status: 503, Code: "loading", Detail: "The data API is starting: try again in a moment", RetryAfter: 5})
		return nil, nil, false
	}
	if r.PathValue("api") != snap.m.Slug {
		s.fail(w, r, &Problem{Status: 404, Code: "not_found", Detail: "No such data API"})
		return nil, nil, false
	}
	switch {
	case snap.m.State == dataapiwire.StateSuspended:
		s.fail(w, r, &Problem{Status: 503, Code: "api_suspended",
			Detail: "This data API is suspended: its creator may no longer publish it"})
		return nil, nil, false
	case snap.m.State != dataapiwire.StateActive:
		s.fail(w, r, &Problem{Status: 503, Code: "api_paused", Detail: "This data API is paused"})
		return nil, nil, false
	case s.stale(snap):
		s.fail(w, r, &Problem{Status: 503, Code: "manifest_stale",
			Detail: "This data API cannot check its keys at the moment: try again later", RetryAfter: 30})
		return nil, nil, false
	}
	key, p := snap.keys.Check(KeyOf(r), s.now())
	if p != nil {
		recordOf(r).key = key // a revoked or expired key is still counted as its own
		s.fail(w, r, p)
		return nil, nil, false
	}
	recordOf(r).key = key
	if !key.AllowsAddress(s.clientIP.Of(r)) {
		s.fail(w, r, &Problem{Status: 403, Code: "address_not_allowed", Detail: "This key may not be used from this address"})
		return nil, nil, false
	}
	if ok, wait := s.limiter.Take(key.ID, PerPod(key.RatePerMinute, snap.m.Replicas)); !ok {
		s.fail(w, r, &Problem{Status: 429, Code: "rate_limited",
			Detail:     fmt.Sprintf("This key's limit is %d requests a minute: try again in %d seconds", key.RatePerMinute, wait),
			RetryAfter: max(wait, 1)})
		return nil, nil, false
	}
	return snap, key, true
}

// GET /data/{api}/openapi.json: the description of the resources this key may read.
func (s *Server) openAPI(w http.ResponseWriter, r *http.Request) {
	snap, key, ok := s.admit(w, r)
	if !ok {
		return
	}
	types := map[string]*Table{}
	for _, res := range snap.m.Resources {
		if t := s.table(res.Name); t != nil {
			types[res.Name] = t
			defer t.release()
		}
	}
	writeJSON(w, 200, OpenAPI(snap.m, key, types))
}

// GET /data/{api}/{resource}
func (s *Server) data(w http.ResponseWriter, r *http.Request) {
	snap, key, ok := s.admit(w, r)
	if !ok {
		return
	}
	name := r.PathValue("resource")
	res := snap.resources[name]
	if res == nil {
		s.fail(w, r, &Problem{Status: 404, Code: "unknown_resource", Detail: fmt.Sprintf("This data API has no resource «%s»", name)})
		return
	}
	recordOf(r).resource = name
	if !key.AllowsResource(name) {
		s.fail(w, r, &Problem{Status: 403, Code: "key_not_allowed", Detail: fmt.Sprintf("This key may not read «%s»", name)})
		return
	}
	if res.Unavailable == dataapiwire.UnavailableRights {
		s.fail(w, r, &Problem{Status: 403, Code: "resource_unavailable",
			Detail: fmt.Sprintf("«%s» is not available: its publisher may no longer read its data", name)})
		return
	}
	t := s.table(name)
	if t == nil {
		s.fail(w, r, &Problem{Status: 503, Code: "loading", Detail: fmt.Sprintf("«%s» is still loading: try again in a moment", name),
			RetryAfter: 5})
		return
	}
	defer t.release()
	q, p := ParseQuery(r.URL.RawQuery, res, t)
	if p != nil {
		s.fail(w, r, p)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.QueryTimeout)
	defer cancel()
	page, p := s.page(ctx, q, t)
	if p != nil {
		s.fail(w, r, p)
		return
	}
	recordOf(r).rows = page.rows
	if q.Format == "csv" {
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="%s.csv"`, name))
		w.Header().Set("X-Data-Version", t.Version)
		if page.next != nil {
			w.Header().Set("X-Next-Cursor", *page.next)
		}
		if page.count != nil {
			w.Header().Set("X-Total-Count", strconv.FormatInt(*page.count, 10))
		}
		w.Header().Set("Content-Length", strconv.Itoa(page.body.Len()))
		w.WriteHeader(200)
		_, _ = w.Write(page.body.Bytes())
		return
	}
	var tail bytes.Buffer
	tail.WriteString(`],"next":`)
	next, _ := marshal(page.next)
	tail.Write(next)
	version, _ := marshal(t.Version)
	tail.WriteString(`,"version":`)
	tail.Write(version)
	if page.count != nil {
		tail.WriteString(`,"count":` + strconv.FormatInt(*page.count, 10))
	}
	tail.WriteString("}")
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(`{"data":[`)+page.body.Len()+tail.Len()))
	w.WriteHeader(200)
	_, _ = w.Write([]byte(`{"data":[`))
	_, _ = w.Write(page.body.Bytes())
	_, _ = w.Write(tail.Bytes())
}

type page struct {
	body  bytes.Buffer // the rows, as JSON objects joined by commas, or CSV with its header
	rows  int
	next  *string
	count *int64
}

func (s *Server) page(ctx context.Context, q *Query, t *Table) (*page, *Problem) {
	out := &page{}
	queryFailed := func(err error) *Problem {
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return &Problem{Status: 503, Code: "query_timeout",
				Detail: fmt.Sprintf("The query took longer than %d seconds: narrow it with a filter", int(s.cfg.QueryTimeout.Seconds()))}
		}
		if ctx.Err() != nil {
			return &Problem{Status: 503, Code: "cancelled", Detail: "The request was cancelled"}
		}
		s.log.Error("query", "resource", t.Resource, "err", err.Error())
		return &Problem{Status: 500, Code: "internal", Detail: "The data API could not answer: try again"}
	}
	if q.Count {
		text, args := q.CountSQL()
		var n int64
		rows, err := t.Query(ctx, text, args...)
		if err != nil {
			return nil, queryFailed(err)
		}
		if rows.Next() {
			err = rows.Scan(&n)
		}
		if cerr := rows.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			return nil, queryFailed(err)
		}
		out.count = &n
	}
	text, args, err := q.PageSQL(t)
	if err != nil {
		var p *Problem
		if errors.As(err, &p) {
			return nil, p
		}
		return nil, queryFailed(err)
	}
	rows, err := t.Query(ctx, text, args...)
	if err != nil {
		return nil, queryFailed(err)
	}
	defer rows.Close()
	width := 1 + len(q.Sort) + len(q.Columns)
	values := make([]any, width)
	holders := make([]any, width)
	for i := range values {
		holders[i] = &values[i]
	}
	var cw *csv.Writer
	if q.Format == "csv" {
		cw = csv.NewWriter(&out.body)
		header := make([]string, len(q.Columns))
		for i, c := range q.Columns {
			header[i] = c.Name
		}
		_ = cw.Write(header)
	}
	var last Position
	more := false
	cells := make([]string, len(q.Columns))
	for rows.Next() {
		if out.rows == q.Limit {
			more = true
			break
		}
		if err := rows.Scan(holders...); err != nil {
			return nil, queryFailed(err)
		}
		row, _ := values[0].(int64)
		last = Position{Row: row}
		if !q.FileOrdered(t) {
			for i, k := range q.Sort {
				last.Values = append(last.Values, k.Column.CursorText(values[1+i]))
			}
		}
		base := 1 + len(q.Sort)
		if cw != nil {
			for i, c := range q.Columns {
				cells[i] = c.CSVText(values[base+i])
			}
			_ = cw.Write(cells)
		} else {
			if out.rows > 0 {
				out.body.WriteByte(',')
			}
			out.body.WriteByte('{')
			for i, c := range q.Columns {
				if i > 0 {
					out.body.WriteByte(',')
				}
				k, _ := marshal(c.Name)
				out.body.Write(k)
				out.body.WriteByte(':')
				v, err := marshal(c.Plain(values[base+i]))
				if err != nil {
					return nil, queryFailed(err)
				}
				out.body.Write(v)
			}
			out.body.WriteByte('}')
		}
		out.rows++
	}
	if err := rows.Err(); err != nil {
		return nil, queryFailed(err)
	}
	if cw != nil {
		cw.Flush()
	}
	if more {
		next := EncodeCursor(t.Generation, q.Raw, last)
		out.next = &next
	}
	return out, nil
}
