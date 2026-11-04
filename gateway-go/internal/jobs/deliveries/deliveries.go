// Package deliveries is services/webhooks.py delivery_loop: the webhooks' deliveries taken
// from the queue (each by whoever takes it first, while still due: a conditional UPDATE),
// sent to the receiver's checked address, their outcome written while still theirs, retried
// after 30 s, 2 min, 10 min, 1 h, 6 h, then failed.
package deliveries

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/api"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/siem"
)

const (
	cycle         = 2 * time.Second // CYCLE
	reclaimAfter  = 5 * time.Minute // RECLAIM_AFTER
	batch         = 20              // BATCH
	parallel      = 16              // PARALLEL
	perWebhook    = 2               // PER_WEBHOOK
	maxAttempts   = 6               // MAX_ATTEMPTS
	turnedOff     = "webhook turned off or removed"
	noResponseYet = "no complete response in time"
)

// waits is WAITS, in seconds.
var waits = []int{30, 120, 600, 3600, 21600}

func init() {
	api.RegisterJob("webhooks_loop", func(d *api.Deps) jobs.Job {
		s := &sender{db: d.DB, fernet: d.Fernet, panel: d.Panel, log: d.Log, version: d.Config.AppVersion}
		return jobs.Job{Order: 6, Loop: s.loop, Ticks: map[string]jobs.Tick{
			// deliver_due: one round only, waiting for it to end
			"webhooks": func(ctx context.Context) (any, error) { return s.deliverDue(ctx, batch), nil },
		}}
	})
}

type sender struct {
	db      *pgxpool.Pool
	fernet  *secret.Fernet
	panel   *settings.Panel
	log     *slog.Logger
	version string
}

// job is _Job: a delivery taken, with all it needs to send it.
type job struct {
	id, webhook int64
	url, secret string
	event, body string
	attempts    int
	takenAt     time.Time
}

// takeDue is _take_due: up to `limit` due deliveries, the oldest first, at most PER_WEBHOOK
// per webhook counting those in flight (`busy`).
func (s *sender) takeDue(ctx context.Context, limit int, busy map[int64]int) ([]job, error) {
	counts := map[int64]int{}
	for k, v := range busy {
		counts[k] = v
	}
	at := jobs.Now()
	if _, err := s.db.Exec(ctx, `UPDATE webhook_deliveries SET status = 'pending'
		WHERE status = 'sending' AND claimed_at < $1`, at.Add(-reclaimAfter)); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(ctx, `SELECT id, webhook_id FROM (
			SELECT id, webhook_id, next_attempt_at AS "when",
				row_number() OVER (PARTITION BY webhook_id ORDER BY next_attempt_at, id) AS n
			FROM webhook_deliveries WHERE status = 'pending' AND next_attempt_at <= $1) t
		WHERE n <= $2 ORDER BY "when", id LIMIT $3`, at, perWebhook, limit+perWebhook*len(busy))
	if err != nil {
		return nil, err
	}
	type cand struct{ id, webhook int64 }
	candidates, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (cand, error) {
		var c cand
		return c, r.Scan(&c.id, &c.webhook)
	})
	if err != nil {
		return nil, err
	}
	var out []job
	for _, c := range candidates {
		if len(out) >= limit {
			break
		}
		if counts[c.webhook] >= perWebhook {
			continue
		}
		takenAt := jobs.Now()
		tag, err := s.db.Exec(ctx, `UPDATE webhook_deliveries SET status = 'sending', claimed_at = $1, attempts = attempts + 1
			WHERE id = $2 AND status = 'pending' AND next_attempt_at <= $1`, takenAt, c.id)
		if err != nil {
			return out, err
		}
		if tag.RowsAffected() != 1 {
			continue // another process took it, or it was put off
		}
		var j job
		j.id, j.takenAt = c.id, takenAt
		if err := s.db.QueryRow(ctx, `SELECT webhook_id, event, payload, attempts FROM webhook_deliveries WHERE id = $1`,
			c.id).Scan(&j.webhook, &j.event, &j.body, &j.attempts); err != nil {
			return out, err
		}
		var secretEnc string
		var active bool
		err = s.db.QueryRow(ctx, `SELECT url, secret_enc, active FROM webhooks WHERE id = $1`, j.webhook).Scan(&j.url, &secretEnc, &active)
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && !active) {
			if _, err := s.db.Exec(ctx, `UPDATE webhook_deliveries SET status = 'failed', last_error = $3
				WHERE id = $1 AND status = 'sending' AND claimed_at = $2`, c.id, takenAt, turnedOff); err != nil {
				return out, err
			}
			continue
		} else if err != nil {
			return out, err
		}
		if j.secret, err = s.fernet.Decrypt(secretEnc); err != nil {
			return out, err
		}
		out = append(out, j)
		counts[c.webhook]++
	}
	return out, nil
}

// record is _record.
func (s *sender) record(ctx context.Context, j job, ok bool, code *int, errText *string, final bool) error {
	at := jobs.Now()
	switch {
	case ok:
		return s.closeDeliveryNullable(ctx, j, "delivered", code, errText, &at, nil)
	case final || j.attempts >= maxAttempts:
		return s.closeDeliveryNullable(ctx, j, "failed", code, errText, nil, nil)
	default:
		next := at.Add(time.Duration(waits[j.attempts-1]) * time.Second)
		return s.closeDeliveryNullable(ctx, j, "pending", code, errText, nil, &next)
	}
}

// closeDeliveryNullable is _close_delivery with _record's values: the outcome written only
// while the delivery is still whoever took it's; last_status_code and last_error always.
func (s *sender) closeDeliveryNullable(ctx context.Context, j job, status string, code *int, errText *string,
	delivered, next *time.Time) error {
	args := []any{j.id, j.takenAt, status, intOrNil(code), strOrNil(errText)}
	sets := []string{"status = $3", "last_status_code = $4", "last_error = $5"}
	if delivered != nil {
		args = append(args, *delivered)
		sets = append(sets, fmt.Sprintf("delivered_at = $%d", len(args)))
	}
	if next != nil {
		args = append(args, *next)
		sets = append(sets, fmt.Sprintf("next_attempt_at = $%d", len(args)))
	}
	_, err := s.db.Exec(ctx, `UPDATE webhook_deliveries SET `+strings.Join(sets, ", ")+`
		WHERE id = $1 AND status = 'sending' AND claimed_at = $2`, args...)
	return err
}

func intOrNil(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

func strOrNil(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

// clip is a str's [:n] in characters.
func clip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

var errInternal = errors.New("internal")

// ship is _ship: (success, code, error), or final (a _Forbidden) with its words; an
// errInternal-wrapped error is an exception _one_row words as «internal error».
func (s *sender) ship(ctx context.Context, j job) (ok bool, code *int, errText *string, final bool, internal error) {
	t, err := siem.Resolve(ctx, j.url)
	if err != nil {
		msg := err.Error()
		if strings.HasPrefix(msg, "name not resolved: ") || msg == "name without addresses" {
			m := clip(msg, 500)
			return false, nil, &m, false, nil
		}
		return false, nil, &msg, true, nil
	}
	// headers(): the delivery's id read from its body
	v, err := pyjson.Loads(j.body)
	if err != nil {
		return false, nil, nil, false, fmt.Errorf("%w: JSONDecodeError", errInternal)
	}
	o, isDict := v.(pyjson.Object)
	if !isDict {
		return false, nil, nil, false, fmt.Errorf("%w: TypeError", errInternal)
	}
	id, has := o.Get("id")
	if !has {
		return false, nil, nil, false, fmt.Errorf("%w: KeyError", errInternal)
	}
	instant := jobs.Now().Unix()
	mac := hmac.New(sha256.New, []byte(j.secret))
	fmt.Fprintf(mac, "%d.%s", instant, j.body)
	headers := [][2]string{
		{"Content-Type", "application/json"},
		{"User-Agent", "Tabularia-Webhooks/" + s.version},
		{"X-Tabularia-Event", j.event},
		{"X-Tabularia-Delivery", pyjson.Str(id)},
		{"X-Tabularia-Signature", fmt.Sprintf("t=%d,v1=%s", instant, hex.EncodeToString(mac.Sum(nil)))},
	}
	status, read, failed := siem.Exchange(ctx, t, true, headers, []byte(j.body), jobs.Seconds(s.panel.WebhooksTimeout()))
	if failed != "" {
		m := clip(failed, 500)
		return false, nil, &m, false, nil
	}
	c := status
	if status >= 200 && status < 300 {
		return true, &c, nil, false, nil
	}
	if len(read) > 300 {
		read = read[:300]
	}
	m := fmt.Sprintf("response %d: %s", status, strings.ToValidUTF8(string(read), "�"))
	return false, &c, &m, false, nil
}

// oneRow is _one_row: it never fails, the outcome is the delivery's.
func (s *sender) oneRow(ctx context.Context, j job) {
	limit := jobs.Seconds(s.panel.WebhooksTimeout() * 2)
	cctx, cancel := context.WithTimeout(ctx, limit)
	ok, code, errText, final, internal := s.ship(cctx, j)
	timedOut := cctx.Err() == context.DeadlineExceeded
	cancel()
	if ctx.Err() != nil {
		return // shutdown: the delivery stays taken and goes back in the queue after RECLAIM_AFTER
	}
	switch {
	case timedOut:
		ok, code, final = false, nil, false
		m := noResponseYet
		errText = &m
	case internal != nil:
		s.log.Error("webhook: delivery not sent", "delivery", j.id, "err", internal)
		m := "internal error" + strings.TrimPrefix(internal.Error(), errInternal.Error())
		errText = &m
	}
	if err := s.record(ctx, j, ok, code, errText, final); err != nil {
		s.log.Error("webhook: outcome of delivery not recorded", "delivery", j.id, "err", err)
	}
}

// inFlight is _InFlight: this process's deliveries in flight.
type inFlight struct {
	s     *sender
	mu    sync.Mutex
	tasks map[int64]int64 // delivery → webhook
	wg    sync.WaitGroup
	done  chan struct{}
}

func (s *sender) newFlight() *inFlight {
	return &inFlight{s: s, tasks: map[int64]int64{}, done: make(chan struct{}, parallel*4)}
}

// round is round_: only the free slots filled.
func (f *inFlight) round(ctx context.Context, limit int) int {
	f.mu.Lock()
	free := min(limit, parallel-len(f.tasks))
	busy := map[int64]int{}
	for _, w := range f.tasks {
		busy[w]++
	}
	f.mu.Unlock()
	if free <= 0 {
		return 0
	}
	taken, err := f.s.takeDue(ctx, free, busy)
	if err != nil {
		f.s.log.Error("webhook: the deliveries' queue cannot be read", "err", err)
		// what was taken before the failure is sent all the same: Python raises and loses
		// it until RECLAIM_AFTER, see the report
		return 0
	}
	for _, j := range taken {
		f.mu.Lock()
		f.tasks[j.id] = j.webhook
		f.mu.Unlock()
		f.wg.Add(1)
		go func(j job) {
			defer f.wg.Done()
			f.s.oneRow(ctx, j)
			f.mu.Lock()
			delete(f.tasks, j.id)
			f.mu.Unlock()
			select {
			case f.done <- struct{}{}:
			default:
			}
		}(j)
	}
	return len(taken)
}

// deliverDue is deliver_due: one round only, waiting for it to end; how many it tried.
func (s *sender) deliverDue(ctx context.Context, limit int) int {
	f := s.newFlight()
	n := 0
	for n < limit {
		taken := f.round(ctx, limit-n)
		if taken == 0 {
			break
		}
		n += taken
		if n < limit {
			f.wg.Wait()
		}
	}
	f.wg.Wait()
	return n
}

// loop is delivery_loop: a round as soon as a delivery ends, or every CYCLE; at shutdown
// those in flight get 5 seconds.
func (s *sender) loop(ctx context.Context) {
	sendCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	f := s.newFlight()
	for ctx.Err() == nil {
		f.round(sendCtx, parallel)
		t := time.NewTimer(cycle)
		select {
		case <-ctx.Done():
		case <-f.done:
		case <-t.C:
		}
		t.Stop()
	}
	finished := make(chan struct{})
	go func() { f.wg.Wait(); close(finished) }()
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		cancel()
		<-finished
	}
}
