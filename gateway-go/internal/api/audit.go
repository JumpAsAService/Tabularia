package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/audit.py: reading the audit log and the open sessions (administrators and
// observers), closing a session (administrators). The router's dependency is
// require_observer: it runs before every route's own.
func init() {
	register("audit", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/audit", Handler: adminRole(d, "/audit", false, listAudit(d))},
			{Method: http.MethodGet, Pattern: "/audit/actions", Handler: adminRole(d, "/audit/actions", false, listAuditActions(d))},
			{Method: http.MethodGet, Pattern: "/audit/access-activity", Handler: adminRole(d, "/audit/access-activity", false, accessActivity(d))},
			{Method: http.MethodGet, Pattern: "/audit/sessions", Handler: adminRole(d, "/audit/sessions", false, activeSessions(d))},
			{Method: http.MethodDelete, Pattern: "/audit/sessions/{sid}", Handler: observerThenAdmin(d, "/audit/sessions/{sid}", revokeSession(d))},
		}
	})
}

// activeWindow is ACTIVE_WINDOW: a session used within it is «online».
const activeWindow = 15 * time.Minute

// AuditEntryOut is routes/audit.py AuditEntryOut, its fields in order.
type AuditEntryOut struct {
	ID          int64   `json:"id"`
	Ts          string  `json:"ts"`
	ActorID     *int64  `json:"actor_id"`
	ActorLabel  string  `json:"actor_label"`
	Action      string  `json:"action"`
	Outcome     string  `json:"outcome"`
	TargetType  *string `json:"target_type"`
	TargetID    *int64  `json:"target_id"`
	TargetLabel *string `json:"target_label"`
	Detail      any     `json:"detail"`
	IP          *string `json:"ip"`
	UserAgent   *string `json:"user_agent"`
}

type auditPage struct {
	Items []AuditEntryOut `json:"items"`
	Total int64           `json:"total"`
}

// listAudit is GET /audit (list_audit): newest first, paginated and filterable. An
// observer searches only over what they see, and reads it masked.
func listAudit(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		q := pyd.ParseQuery(r.URL.RawQuery)
		var ps pyd.Params
		text := ps.Query(q, "q", pyd.Optional(pyd.Str()), nil)
		action := ps.Query(q, "action", pyd.Optional(pyd.Str()), nil)
		targetType := ps.Query(q, "target_type", pyd.Optional(pyd.Str()), nil)
		outcome := ps.Query(q, "outcome", pyd.Optional(pyd.Str()), nil)
		actorID := ps.Query(q, "actor_id", pyd.Optional(pyd.Int()), nil)
		since := ps.Query(q, "since", pyd.Optional(pyd.Datetime()), nil)
		until := ps.Query(q, "until", pyd.Optional(pyd.Datetime()), nil)
		limit := ps.Query(q, "limit", pyd.Int().Ge(1).Le(200), int64(50))
		offset := ps.Query(q, "offset", pyd.Int().Ge(0), int64(0))
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		var where []string
		var args []any
		arg := func(v any) string {
			args = append(args, v)
			return fmt.Sprintf("$%d", len(args))
		}
		if s, _ := text.(string); s != "" {
			like := arg("%" + s + "%")
			if full {
				where = append(where, fmt.Sprintf("(actor_label ILIKE %[1]s OR target_label ILIKE %[1]s OR action ILIKE %[1]s OR ip ILIKE %[1]s)", like))
			} else {
				// over the IPs and the emails hidden from them, counting the rows found would
				// rebuild them by trial
				where = append(where, fmt.Sprintf("(action ILIKE %[1]s OR (target_label ILIKE %[1]s AND NOT (target_label LIKE '%%' || '@' || '%%')))", like))
			}
		}
		for _, f := range []struct {
			column string
			value  any
		}{{"action", action}, {"target_type", targetType}, {"outcome", outcome}} {
			if s, _ := f.value.(string); s != "" {
				where = append(where, f.column+" = "+arg(s))
			}
		}
		if actorID != nil {
			if id, ok := int4(actorID); ok {
				where = append(where, "actor_id = "+arg(id))
			} else {
				where = append(where, "false") // an id past the column: no row has it
			}
		}
		if since != nil {
			where = append(where, "ts >= "+arg(naiveUTC(since.(pyd.DateTime))))
		}
		if until != nil {
			where = append(where, "ts <= "+arg(naiveUTC(until.(pyd.DateTime))))
		}
		cond := ""
		if len(where) > 0 {
			cond = " WHERE " + strings.Join(where, " AND ")
		}
		lim, ok1 := int8(limit)
		off, ok2 := int8(offset)
		if !ok1 || !ok2 {
			server.InternalError(w) // PostgreSQL refuses the literal
			return
		}
		ctx := r.Context()
		var total int64
		if err := d.DB.QueryRow(ctx, "SELECT count(*) FROM audit_logs"+cond, args...).Scan(&total); err != nil {
			adminFail(d, w, "counting the audit", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT id, ts, actor_id, actor_label, action, outcome, target_type, target_id,
			target_label, detail, ip, user_agent FROM audit_logs`+cond+
			fmt.Sprintf(" ORDER BY ts DESC OFFSET %d LIMIT %d", off, lim), args...)
		if err != nil {
			adminFail(d, w, "reading the audit", err)
			return
		}
		items, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (AuditEntryOut, error) {
			var e AuditEntryOut
			var ts time.Time
			var detail *string
			err := row.Scan(&e.ID, &ts, &e.ActorID, &e.ActorLabel, &e.Action, &e.Outcome, &e.TargetType, &e.TargetID,
				&e.TargetLabel, &detail, &e.IP, &e.UserAgent)
			e.Ts = pyjson.Isoformat(ts)
			if err == nil {
				e.Detail, err = auditDetail(detail)
			}
			return e, err
		})
		if err != nil {
			adminFail(d, w, "reading the audit", err)
			return
		}
		if !full {
			for i := range items {
				items[i] = forObserver(items[i])
			}
		}
		if items == nil {
			items = []AuditEntryOut{}
		}
		server.JSON(w, http.StatusOK, auditPage{Items: items, Total: total})
	}
}

// auditDetail is _to_out's `json.loads(e.detail) if e.detail else None`, a broken text as
// {"_raw": …}; what is not a dict fails AuditEntryOut's validation (a 500).
func auditDetail(text *string) (any, error) {
	if text == nil || *text == "" {
		return nil, nil
	}
	v, err := pyjson.Loads([]byte(*text))
	if err != nil {
		return pyjson.Object{{Key: "_raw", Value: *text}}, nil
	}
	switch v.(type) {
	case nil, pyjson.Object:
		return v, nil
	}
	return nil, errors.New("audit detail is not a dict")
}

// forObserver is _to_out(full=False): WHAT was done stays; who, from where and with what go.
func forObserver(e AuditEntryOut) AuditEntryOut {
	if strings.Contains(e.ActorLabel, "@") {
		e.ActorLabel = masking.Email(e.ActorLabel)
	}
	if e.TargetLabel != nil && strings.Contains(*e.TargetLabel, "@") {
		m := masking.Email(*e.TargetLabel)
		e.TargetLabel = &m
	}
	e.IP, e.UserAgent = nil, nil
	e.Detail = detailForObserver(e.Detail, "", false)
	return e
}

// hiddenDetail is _HIDDEN: the keys of `detail` that say WHERE or WHO.
var hiddenDetail = map[string]bool{
	"host": true, "url": true, "endpoint": true, "server": true, "fingerprint": true, "path": true,
	"source_keys": true, "sql": true, "secret": true, "groups": true, "groups_added": true,
	"groups_removed": true, "group_names": true, "sub": true, "subject": true, "issuer": true,
	"to": true, "cc": true, "bcc": true, "full_name": true,
}

// detailForObserver is _detail_for_observer, at any depth.
func detailForObserver(v any, key string, keyed bool) any {
	if keyed && hiddenDetail[key] && v != nil {
		switch v.(type) {
		case int64, float64, bool, pyjson.BigInt:
		default:
			return "…"
		}
	}
	if keyed && key == "prefix" && truthy(v) {
		return "tab_…"
	}
	switch x := v.(type) {
	case pyjson.Object:
		out := make(pyjson.Object, 0, len(x))
		for _, kv := range x {
			out = append(out, pyjson.KV{Key: kv.Key, Value: detailForObserver(kv.Value, kv.Key, true)})
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = detailForObserver(e, "", false)
		}
		return out
	case string:
		if strings.Contains(x, "@") {
			return masking.Email(x)
		}
	}
	return v
}

// listAuditActions is GET /audit/actions: the actions present, for the filter.
func listAuditActions(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		rows, err := d.DB.Query(r.Context(), `SELECT DISTINCT action FROM audit_logs ORDER BY action`)
		if err != nil {
			adminFail(d, w, "reading the audit's actions", err)
			return
		}
		actions, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			adminFail(d, w, "reading the audit's actions", err)
			return
		}
		if actions == nil {
			actions = []string{}
		}
		server.JSON(w, http.StatusOK, actions)
	}
}

type accessBucket struct {
	Ts      string `json:"ts"`
	Label   string `json:"label"`
	Success int    `json:"success"`
	Failure int    `json:"failure"`
}

type accessActivityOut struct {
	Hours        int64          `json:"hours"`
	Timezone     string         `json:"timezone"`
	Buckets      []accessBucket `json:"buckets"`
	TotalSuccess int            `json:"total_success"`
	TotalFailure int            `json:"total_failure"`
}

// accessActivity is GET /audit/access-activity: the sign-ins per hour of the last `hours`,
// successful and failed, bucketed in the deployment's zone.
func accessActivity(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		hoursV := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "hours", pyd.Int().Ge(1).Le(168), int64(24))
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		hours := int(hoursV.(int64))
		zone := d.Panel.Timezone()
		loc, err := config.LoadZone(zone)
		if err != nil {
			adminFail(d, w, "the deployment's zone", err)
			return
		}
		now := time.Now()
		local := now.In(loc)
		// wall-clock hours, as Python's arithmetic on an aware datetime: the key is the naive
		// local hour
		hourStart := time.Date(local.Year(), local.Month(), local.Day(), local.Hour(), 0, 0, 0, time.UTC)
		starts := make([]time.Time, hours)
		index := map[time.Time]int{}
		for i := range starts {
			starts[i] = hourStart.Add(-time.Duration(hours-1-i) * time.Hour)
			index[starts[i]] = i
		}
		success := make([]int, hours)
		failure := make([]int, hours)
		cutoff := now.UTC().Add(-time.Duration(hours) * time.Hour)
		rows, err := d.DB.Query(r.Context(), `SELECT ts, action FROM audit_logs WHERE ts >= $1 AND action IN ($2, $3)`,
			naiveTime(cutoff), auditLogin, auditLoginFailed)
		if err != nil {
			adminFail(d, w, "reading the sign-ins", err)
			return
		}
		type tsAction struct {
			ts     time.Time
			action string
		}
		found, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (tsAction, error) {
			var x tsAction
			return x, row.Scan(&x.ts, &x.action)
		})
		if err != nil {
			adminFail(d, w, "reading the sign-ins", err)
			return
		}
		for _, f := range found {
			l := f.ts.In(loc)
			i, ok := index[time.Date(l.Year(), l.Month(), l.Day(), l.Hour(), 0, 0, 0, time.UTC)]
			if !ok {
				continue
			}
			if f.action == auditLogin {
				success[i]++
			} else {
				failure[i]++
			}
		}
		out := accessActivityOut{Hours: int64(hours), Timezone: zone, Buckets: make([]accessBucket, hours)}
		for i, s := range starts {
			aware := wallIn(s, loc)
			out.Buckets[i] = accessBucket{Ts: pyd.DateTime{Time: aware, Aware: true}.ISO(), Label: s.Format("15") + ":00",
				Success: success[i], Failure: failure[i]}
			out.TotalSuccess += success[i]
			out.TotalFailure += failure[i]
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// services/audit.py LOGIN, LOGIN_FAILED
const (
	auditLogin       = "auth.login"
	auditLoginFailed = "auth.login_failed"
)

// naiveTime is a moment as the timestamp columns hold it: UTC, without a zone.
func naiveTime(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), u.Hour(), u.Minute(), u.Second(), u.Nanosecond(), time.UTC)
}

// wallIn is a wall-clock time (held in UTC) in the zone as zoneinfo reads it with fold=0:
// in a gap or an overlap, the offset in force before the change.
func wallIn(wall time.Time, loc *time.Location) time.Time {
	_, before := wall.Add(-24 * time.Hour).In(loc).Zone()
	_, after := wall.Add(24 * time.Hour).In(loc).Zone()
	off := before
	if before != after {
		pre := wall.Add(-time.Duration(before) * time.Second)
		post := wall.Add(-time.Duration(after) * time.Second)
		_, atPre := pre.In(loc).Zone()
		_, atPost := post.In(loc).Zone()
		if atPre != before && atPost == after {
			off = after
		}
	}
	// the wall time kept with that offset, as the aware datetime holds it (in a gap too)
	return wall.Add(-time.Duration(off) * time.Second).In(time.FixedZone("", off))
}

// ActiveSession is routes/audit.py ActiveSession.
type ActiveSession struct {
	ID          string  `json:"id"`
	UserID      int64   `json:"user_id"`
	Email       string  `json:"email"`
	FullName    string  `json:"full_name"`
	IsSuperuser bool    `json:"is_superuser"`
	CreatedAt   string  `json:"created_at"`
	LastSeenAt  string  `json:"last_seen_at"`
	ExpiresAt   string  `json:"expires_at"`
	IP          *string `json:"ip"`
	UserAgent   *string `json:"user_agent"`
	Method      string  `json:"method"`
	Online      bool    `json:"online"`
	Current     bool    `json:"current"`
}

type sessionsPage struct {
	Items []ActiveSession `json:"items"`
	Total int64           `json:"total"`
}

// openSessions is sessions.validate: within the maximum length and, with the idle timeout,
// used recently (the minute the activity is written with added).
func openSessions(d *Deps) (string, []any) {
	at := nowNaive()
	cond, args := "expires_at > $1", []any{at}
	if minutes := d.Panel.SessionIdleMinutes(); minutes > 0 {
		cond += " AND last_seen_at >= $2"
		args = append(args, at.Add(-time.Duration(minutes)*time.Minute-auth.ActivityEvery))
	}
	return cond, args
}

// activeSessions is GET /audit/sessions: the open sessions, newest first, the first
// `limit` and how many in all. An observer sees that they exist and are alive, not whose
// they are nor where they come from.
func activeSessions(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		limitV := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(500), int64(100))
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		threshold := nowNaive().Add(-activeWindow)
		cond, args := openSessions(d)
		rows, err := d.DB.Query(ctx, `SELECT id, user_id, created_at, last_seen_at, expires_at, ip, user_agent, method
			FROM user_sessions WHERE `+cond+fmt.Sprintf(` ORDER BY last_seen_at DESC LIMIT %d`, limitV.(int64)), args...)
		if err != nil {
			adminFail(d, w, "reading the sessions", err)
			return
		}
		type open struct {
			id                     string
			user                   int64
			created, seen, expires time.Time
			ip, agent              *string
			method                 string
		}
		opened, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (open, error) {
			var s open
			return s, row.Scan(&s.id, &s.user, &s.created, &s.seen, &s.expires, &s.ip, &s.agent, &s.method)
		})
		if err != nil {
			adminFail(d, w, "reading the sessions", err)
			return
		}
		type person struct {
			email, name string
			superuser   bool
		}
		users := map[int64]person{}
		viaGroup := map[int64]bool{}
		if len(opened) > 0 {
			ids := make([]int64, 0, len(opened))
			for _, s := range opened {
				ids = append(ids, s.user)
			}
			if err := readSessionPeople(ctx, d, ids, func(id int64, email, name string, superuser bool) {
				users[id] = person{email, name, superuser}
			}); err != nil {
				adminFail(d, w, "reading the sessions' people", err)
				return
			}
		}
		// EFFECTIVE admins: whoever is one by membership of an administrators' group too
		grows, err := d.DB.Query(ctx, `SELECT ug.user_id FROM user_groups ug, groups g WHERE ug.group_id = g.id AND g.is_admin = true`)
		if err != nil {
			adminFail(d, w, "reading the administrators' groups", err)
			return
		}
		members, err := pgx.CollectRows(grows, pgx.RowTo[int64])
		if err != nil {
			adminFail(d, w, "reading the administrators' groups", err)
			return
		}
		for _, m := range members {
			viaGroup[m] = true
		}
		out := []ActiveSession{}
		for _, s := range opened {
			u, ok := users[s.user]
			if !ok {
				continue
			}
			a := ActiveSession{
				ID: s.id, UserID: s.user, Email: u.email, FullName: u.name, IsSuperuser: u.superuser || viaGroup[s.user],
				CreatedAt: pyjson.Isoformat(s.created), LastSeenAt: pyjson.Isoformat(s.seen), ExpiresAt: pyjson.Isoformat(s.expires),
				IP: s.ip, UserAgent: s.agent, Method: s.method, Online: !s.seen.Before(threshold),
				Current: p.SessionID != "" && s.id == p.SessionID,
			}
			if !full {
				// the observer closes nothing: telling the rows apart is enough for them
				a.ID, a.Email, a.FullName, a.IP, a.UserAgent = sessionHandle(s.id), masking.Email(u.email), "", nil, nil
			}
			out = append(out, a)
		}
		var total int64
		cond, args = openSessions(d)
		if err := d.DB.QueryRow(ctx, `SELECT count(*) FROM user_sessions WHERE `+cond, args...).Scan(&total); err != nil {
			adminFail(d, w, "counting the sessions", err)
			return
		}
		server.JSON(w, http.StatusOK, sessionsPage{Items: out, Total: total})
	}
}

func readSessionPeople(ctx context.Context, d *Deps, ids []int64, each func(int64, string, string, bool)) error {
	rows, err := d.DB.Query(ctx, `SELECT id, email, full_name, is_superuser FROM users WHERE id = ANY($1)`, ids)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var email, name string
		var su bool
		if err := rows.Scan(&id, &email, &name, &su); err != nil {
			return err
		}
		each(id, email, name, su)
	}
	return rows.Err()
}

// sessionHandle is sessions.handle: a stable name for a session, without its id.
func sessionHandle(sid string) string {
	sum := sha256.Sum256([]byte(sid))
	return "h-" + hex.EncodeToString(sum[:])[:16]
}

// revokeSession is DELETE /audit/sessions/{sid}: closes someone's session (or one's own);
// its token stops holding at the next request.
func revokeSession(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		sid := r.PathValue("sid")
		ctx := r.Context()
		var owner int64
		err := d.DB.QueryRow(ctx, `SELECT user_id FROM user_sessions WHERE id = $1`, sid).Scan(&owner)
		if errors.Is(err, pgx.ErrNoRows) {
			adminDetail(w, http.StatusNotFound, "Session not found")
			return
		}
		if err != nil {
			adminFail(d, w, "reading the session", err)
			return
		}
		var email *string
		if err := d.DB.QueryRow(ctx, `SELECT email FROM users WHERE id = $1`, owner).Scan(&email); err != nil && !errors.Is(err, pgx.ErrNoRows) {
			adminFail(d, w, "reading the session's owner", err)
			return
		}
		if _, err := d.DB.Exec(ctx, `DELETE FROM user_sessions WHERE id = $1`, sid); err != nil {
			adminFail(d, w, "closing the session", err)
			return
		}
		target := "user"
		audit.Record(ctx, d.DB, d.Log, audit.Event{
			Actor: &p.User, Action: "auth.session_revoke", TargetType: &target, TargetID: &owner, TargetLabel: email,
			Detail: pyjson.Object{{Key: "sessions", Value: 1}},
		})
		adminNone(w)
	}
}
