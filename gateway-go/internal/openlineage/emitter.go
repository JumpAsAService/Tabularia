package openlineage

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// QueueMax is QUEUE_MAX: the closed runs waiting to be sent, per process; past this, dropped.
const QueueMax = 500

// Emitter is the module's sending half (_enqueue, _sender_loop, wait_idle): the events go out
// AFTER the commit that closes a run, from a goroutine with a BOUNDED queue, so that a
// collector down or slow never stops a run nor fills the memory. A lost event is not retried.
// A nil Emitter, or one with OpenLineage off, does nothing.
type Emitter struct {
	S   Settings
	DB  *pgxpool.Pool // the rename's events are built with a connection of their own
	SQL *aisvc.Client // sqlglot's reading of a query (qualified-tables, tables)
	// Timeout is the panel's openlineage.timeout_seconds (the client rebuilt when it changes)
	Timeout func() float64
	Log     *slog.Logger

	mu       sync.Mutex
	sender   *sender
	// the client (_client), built at the first send and again when the panel's timeout changes
	clientMu      sync.Mutex
	client        *libClient
	clientTimeout float64
	dropped  int
	lastWarn time.Time
}

type sender struct {
	ch         chan item
	unfinished atomic.Int64
	done       chan struct{}
}

// item is (run_id, events) of the Python queue: the events, or a function that builds them
// and the old names to remove (a rename's), called by the sending goroutine.
type item struct {
	label string // the run's id, or what the events tell
	run   bool
	ev    []pyjson.Object
	build func(ctx context.Context) ([]pyjson.Object, []OldName, error)
	stop  bool
}

// New is the emitter of this process.
func New(s Settings, db *pgxpool.Pool, sql *aisvc.Client, timeout func() float64, log *slog.Logger) *Emitter {
	if log == nil {
		log = slog.Default()
	}
	return &Emitter{S: s, DB: db, SQL: sql, Timeout: timeout, Log: log}
}

// Enabled is enabled().
func (e *Emitter) Enabled() bool { return e != nil && e.S.Enabled() }

// Describe is describe(): one line for the start log.
func (e *Emitter) Describe() string {
	s := e.S
	if !s.Enabled() {
		return "OpenLineage off (no OPENLINEAGE__URL nor OPENLINEAGE__FILE)"
	}
	var where []string
	if s.URL != "" {
		where = append(where, fmt.Sprintf("HTTP %s (%s)", s.URL, s.Endpoint))
	}
	if s.File != "" {
		where = append(where, "file "+s.File)
	}
	if s.URL == "" && s.File == "" {
		where = append(where, "the library's configuration (OPENLINEAGE_URL/OPENLINEAGE_CONFIG)")
	}
	out := ""
	for i, w := range where {
		if i > 0 {
			out += " + "
		}
		out += w
	}
	return fmt.Sprintf("OpenLineage on: events to %s, namespace «%s»", out, s.Namespace)
}

// Settings is the settings in force: the panel's timeout over the environment's.
func (e *Emitter) Settings() Settings {
	s := e.S
	if e.Timeout != nil {
		if t := e.Timeout(); t > 0 {
			s.Timeout = t
		}
	}
	return s
}

// Builder is a builder of events on q (one per request: the folders read once).
func (e *Emitter) Builder(q perm.Querier) *Builder {
	return &Builder{S: e.S, Q: q, SQL: e.SQL}
}

// enqueue is _enqueue: never waits; with the queue full the item is dropped.
func (e *Emitter) enqueue(it item) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.sender == nil {
		e.sender = &sender{ch: make(chan item, QueueMax), done: make(chan struct{})}
		go e.loop(e.sender)
	}
	s := e.sender
	s.unfinished.Add(1)
	select {
	case s.ch <- it:
		return true
	default:
		s.unfinished.Add(-1)
		e.dropped++
		if time.Since(e.lastWarn) > 60*time.Second {
			e.lastWarn = time.Now()
			e.Log.Warn(fmt.Sprintf("openlineage: queue full (%d runs waiting), dropping the events of run %s (%d dropped so far): the collector is not answering",
				QueueMax, it.label, e.dropped))
		}
		return false
	}
}

// loop is _sender_loop: takes from ITS queue and sends, until it is dismissed.
func (e *Emitter) loop(s *sender) {
	defer close(s.done)
	for it := range s.ch {
		if it.stop {
			s.unfinished.Add(-1)
			return
		}
		e.send(it)
		s.unfinished.Add(-1)
	}
}

func (e *Emitter) send(it item) {
	defer func() {
		if r := recover(); r != nil { // the run is closed already: lineage must weigh on nothing
			e.Log.Warn(fmt.Sprintf("openlineage: events of run %s not sent: %v", it.label, r))
		}
	}()
	ctx := context.Background()
	events, old := it.ev, []OldName(nil)
	if it.build != nil {
		var err error
		if events, old, err = it.build(ctx); err != nil {
			e.Log.Warn(fmt.Sprintf("openlineage: events of run %s not sent: %s", it.label, clip(err.Error(), 300)))
			return
		}
	}
	s := e.Settings()
	_, err := e.EmitNow(ctx, events)
	if err != nil {
		e.Log.Warn(fmt.Sprintf("openlineage: events of run %s not sent: %s", it.label, clip(err.Error(), 300)))
		return
	}
	switch {
	case it.run:
		e.Log.Info(fmt.Sprintf("openlineage: %d events for run %s", len(events), it.label))
	case len(old) > 0:
		e.Log.Info(fmt.Sprintf("openlineage: %d events for a %s, %d old names removed", len(events), it.label, removeOldNames(ctx, s, old, e.Log)))
	default:
		e.Log.Info(fmt.Sprintf("openlineage: %d events for %s", len(events), it.label))
	}
}

func clip(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// getClient is _get_client: the client built once, again when the timeout changed; a client
// that cannot be built is tried again at the next send.
func (e *Emitter) getClient() (*libClient, error) {
	e.clientMu.Lock()
	defer e.clientMu.Unlock()
	s := e.Settings()
	if e.client != nil && e.clientTimeout == s.Timeout {
		return e.client, nil
	}
	c, err := s.newClient(readEnviron(), e.Log)
	if err != nil {
		return nil, err
	}
	e.client, e.clientTimeout = c, s.Timeout
	return c, nil
}

// EmitNow is _emit: the events sent at once through the client, in order; how many.
func (e *Emitter) EmitNow(ctx context.Context, events []pyjson.Object) (int, error) {
	c, err := e.getClient()
	if err != nil {
		return 0, err
	}
	return c.emitAll(ctx, events)
}

// WaitIdle is wait_idle: waits for the queue to empty (at most `timeout`), then dismisses the
// goroutine. For the shutdown and the tests.
func (e *Emitter) WaitIdle(timeout time.Duration) {
	if e == nil {
		return
	}
	e.mu.Lock()
	s := e.sender
	e.sender = nil
	e.mu.Unlock()
	if s == nil {
		return
	}
	end := time.Now().Add(timeout)
	for s.unfinished.Load() > 0 && time.Now().Before(end) {
		time.Sleep(20 * time.Millisecond)
	}
	s.unfinished.Add(1)
	select { // the dismissal: the goroutine exits when it gets there
	case s.ch <- item{stop: true}:
	case <-time.After(time.Until(end)):
		s.unfinished.Add(-1) // stays: dies with the process
	}
	select {
	case <-s.done:
	case <-time.After(time.Second):
	}
}

// Pending is pending(): the events waiting to be sent in this process.
func (e *Emitter) Pending() int {
	if e == nil {
		return 0
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.sender == nil {
		return 0
	}
	return len(e.sender.ch)
}

// RunClosed is run_closed: to call AFTER the commit that closes a run. The events are built
// here, with the caller's connection (a few reads); the sending goes to the goroutine. It
// does not block, it does not fail.
func (e *Emitter) RunClosed(ctx context.Context, q perm.Querier, runID int64) {
	if !e.Enabled() {
		return
	}
	events, err := e.Builder(q).RunEvents(ctx, runID)
	if err != nil {
		e.Log.Error(fmt.Sprintf("openlineage: events of run %d not built: %v", runID, err))
		return
	}
	if len(events) > 0 {
		e.enqueue(item{label: fmt.Sprint(runID), run: true, ev: events})
	}
}

// NotebookSession is notebook_session: tells the catalog a moment of a notebook's session
// (see NotebookSessionEvents). It does not block, it does not fail.
func (e *Emitter) NotebookSession(ctx context.Context, q perm.Querier, row SessionRow, state, errText string, datasourceID *int64) {
	if !e.Enabled() {
		return
	}
	events, err := e.Builder(q).NotebookSessionEvents(ctx, row, state, errText, datasourceID)
	if err != nil {
		e.Log.Error(fmt.Sprintf("openlineage: event of a session of notebook %d not built: %v", row.NotebookID, err))
		return
	}
	if len(events) > 0 {
		e.enqueue(item{label: fmt.Sprintf("notebook %d (%s)", row.NotebookID, lower(state)), ev: events})
	}
}

func lower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		}
	}
	return string(b)
}
