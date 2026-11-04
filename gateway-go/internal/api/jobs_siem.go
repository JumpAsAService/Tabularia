package api

import (
	"context"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/siem"
)

// services/siem.py forwarding_loop: the audit log to Splunk's HTTP Event Collector, a batch at
// a time under an advisory lock (another process that finds it taken skips the round), the
// cursor moved in the same transaction.

const (
	siemCycle      = 5 * time.Second // CYCLE
	siemDelay      = 10 * time.Second
	siemBatch      = 500
	siemMaxByte    = 900_000
	siemMaxBatches = 20
	siemMaxWait    = 300
	siemLock       = 7_402_122
)

func init() {
	RegisterJob("siem_loop", func(d *Deps) jobs.Job {
		return jobs.Job{Order: 9, Loop: func(ctx context.Context) {
			for ctx.Err() == nil {
				if _, err := siemRound(ctx, d); err != nil {
					d.Log.Error("siem: forwarding round failed", "err", err)
				}
				if !jobs.Sleep(ctx, siemCycle) {
					return
				}
			}
		}, Ticks: map[string]jobs.Tick{
			"siem": func(ctx context.Context) (any, error) { return siemRound(ctx, d) },
		}}
	})
}

type siemItem struct {
	ID                                      int64
	TS                                      time.Time
	ActorID, TargetID                       *int64
	ActorLabel, Action, Outcome             string
	TargetType, TargetLabel, Detail, IP, UA *string
}

// siemEvent is event(item, d): an audit item as a HEC event, the None values left out.
func siemEvent(it siemItem, s siemRow) pyjson.Object {
	var detail any
	if it.Detail != nil && *it.Detail != "" {
		v, err := pyjson.Loads(*it.Detail)
		if err != nil {
			v = pyjson.Object{{Key: "_raw", Value: *it.Detail}}
		}
		detail = v
	}
	opt := func(p *string) any {
		if p == nil {
			return nil
		}
		return *p
	}
	optInt := func(p *int64) any {
		if p == nil {
			return nil
		}
		return *p
	}
	body := pyjson.Object{
		{Key: "id", Value: it.ID}, {Key: "time", Value: strings.Replace(pyjson.Isoformat(it.TS), "+00:00", "Z", 1)},
		{Key: "action", Value: it.Action}, {Key: "outcome", Value: it.Outcome},
		{Key: "user", Value: it.ActorLabel}, {Key: "user_id", Value: optInt(it.ActorID)}, {Key: "src", Value: opt(it.IP)},
		{Key: "user_agent", Value: opt(it.UA)}, {Key: "target_type", Value: opt(it.TargetType)},
		{Key: "target_id", Value: optInt(it.TargetID)}, {Key: "target_label", Value: opt(it.TargetLabel)},
		{Key: "detail", Value: detail}, {Key: "app", Value: "tabularia"},
	}
	event := pyjson.Object{}
	for _, kv := range body {
		if kv.Value != nil {
			event = append(event, kv)
		}
	}
	// round(timestamp, 3)
	ts, _ := strconv.ParseFloat(strconv.FormatFloat(float64(it.TS.UnixMicro())/1e6, 'f', 3, 64), 64)
	hec := pyjson.Object{{Key: "time", Value: ts}, {Key: "host", Value: siemOr(s.Host, siem.Host)},
		{Key: "source", Value: siemOr(s.Source, siem.Source)}, {Key: "sourcetype", Value: siemOr(s.Sourcetype, siem.Sourcetype)},
		{Key: "event", Value: event}}
	if s.Index != nil && *s.Index != "" {
		hec = append(hec, pyjson.KV{Key: "index", Value: *s.Index})
	}
	return hec
}

// siemWithinTheCap is _within_the_cap: how many of the first events fit in one request.
func siemWithinTheCap(events []pyjson.Object) (int, error) {
	total := 0
	for i, e := range events {
		row, err := pyjson.Dumps(e, pyjson.Options{Compact: true})
		if err != nil {
			return 0, err
		}
		total += len(row) + 1
		if total > siemMaxByte && i > 0 {
			return i, nil
		}
	}
	return len(events), nil
}

// siemWait is wait(failures): 5, 10, 20… seconds, up to MAX_WAIT.
func siemWait(failures int64) float64 {
	return math.Min(siemMaxWait, 5*math.Pow(2, float64(max(0, failures-1))))
}

// siemRound is one_round: the items sent out this round.
func siemRound(ctx context.Context, d *Deps) (int64, error) {
	var started int64
	for range siemMaxBatches {
		more, n, err := siemBatchRound(ctx, d)
		started += n
		if err != nil || !more {
			return started, err
		}
	}
	return started, nil
}

// siemBatchRound is one pass of one_round's loop: whether to go on, and how many went.
func siemBatchRound(ctx context.Context, d *Deps) (bool, int64, error) {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return false, 0, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, siemLock).Scan(&got); err != nil || !got {
		return false, 0, err
	}
	s := siemRow{}
	err = tx.QueryRow(ctx, `SELECT enabled, url, token_enc, index, source, sourcetype, host, verify_tls, cursor_id,
		sent_total, failures, next_attempt_at FROM siem_destinations WHERE id = 1`).Scan(&s.Enabled, &s.URL, &s.TokenEnc,
		&s.Index, &s.Source, &s.Sourcetype, &s.Host, &s.VerifyTLS, &s.CursorID, &s.SentTotal, &s.Failures, &s.NextAttemptAt)
	if err == pgx.ErrNoRows {
		return false, 0, nil
	} else if err != nil {
		return false, 0, err
	}
	if !s.Enabled || s.URL == "" || s.TokenEnc == nil || *s.TokenEnc == "" {
		return false, 0, nil
	}
	if s.NextAttemptAt != nil && s.NextAttemptAt.After(jobs.Now()) {
		return false, 0, nil
	}
	rows, err := tx.Query(ctx, `SELECT id, ts, actor_id, actor_label, action, outcome, target_type, target_id, target_label,
		detail, ip, user_agent FROM audit_logs WHERE id > $1 AND ts <= $2 ORDER BY id LIMIT $3`,
		s.CursorID, jobs.Now().Add(-siemDelay), siemBatch)
	if err != nil {
		return false, 0, err
	}
	items, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (siemItem, error) {
		var it siemItem
		err := r.Scan(&it.ID, &it.TS, &it.ActorID, &it.ActorLabel, &it.Action, &it.Outcome, &it.TargetType, &it.TargetID,
			&it.TargetLabel, &it.Detail, &it.IP, &it.UA)
		return it, err
	})
	if err != nil || len(items) == 0 {
		return false, 0, err
	}
	events := make([]pyjson.Object, len(items))
	for i, it := range items {
		events[i] = siemEvent(it, s)
	}
	n, err := siemWithinTheCap(events)
	if err != nil {
		return false, 0, err
	}
	// send: «no token configured» cannot happen here (checked above)
	token, err := d.Fernet.Decrypt(*s.TokenEnc)
	if err != nil {
		return false, 0, err
	}
	res := siem.Send(ctx, siem.Destination{URL: s.URL, Token: token, VerifyTLS: s.VerifyTLS}, events[:n],
		jobs.Seconds(d.Panel.WebhooksTimeout()))
	at := jobs.Now()
	if res.OK {
		_, err = tx.Exec(ctx, `UPDATE siem_destinations SET cursor_id = $1, sent_total = $2, last_ok_at = $3, failures = 0,
			next_attempt_at = NULL WHERE id = 1`, items[n-1].ID, s.SentTotal+int64(n), at)
	} else {
		failures := s.Failures + 1
		wait := siemWait(failures)
		d.Log.Warn("siem: items not delivered, retrying", "items", n, "error", res.Message, "seconds", wait)
		_, err = tx.Exec(ctx, `UPDATE siem_destinations SET failures = $1, last_error = $2, last_error_at = $3,
			next_attempt_at = $4 WHERE id = 1`, failures, res.Message, at, at.Add(jobs.Seconds(wait)))
	}
	if err != nil {
		return false, 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, 0, err
	}
	if !res.OK {
		return false, 0, nil
	}
	return true, int64(n), nil
}
