// Package sharelinks is what a share link is made of (docs/specs/share-links.md): a report or a
// dashboard opened from outside Tabularia with a link and its own password, or embedded in the
// pages of listed origins. The tokens and the sessions are kept as hashes; the rights are the
// creator's, at every request.
package sharelinks

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// Kinds are what a link can open.
const (
	Report    = "report"
	Dashboard = "dashboard"
)

// Limits of a link.
const (
	MaxOrigins     = 10
	MaxLabel       = 200
	LockAfter      = 50 // consecutive wrong passwords lock the link
	SessionTTL     = 12 * time.Hour
	SessionIdle    = 60 * time.Minute
	ChangeTTL      = 10 * time.Minute
	PurgeAfter     = 30 * 24 * time.Hour // a revoked or expired link stays listed so long
	CookieName     = "tab_share"
	SessionHeader  = "X-Tabularia-Share-Session"
	NotAvailable   = "This link is not available"
	PurposeView    = "view"
	PurposeChange  = "change"
	MaxDaysCeiling = 365
	MaxAICeiling   = 200
)

// Downloads are the formats a link may let its reader download, by kind.
var Downloads = map[string][]string{Report: {"pdf", "xlsx"}, Dashboard: {"pdf", "png"}}

// Querier is what the package reads and writes with: a pool or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// Now is naive UTC, as every time of the gateway's tables.
func Now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

// NewSecret is 32 random bytes in base64url: 43 characters.
func NewSecret() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Hash is what the tables keep of a token or a session: its sha-256, hex.
func Hash(secret string) string {
	h := sha256.Sum256([]byte(secret))
	return hex.EncodeToString(h[:])
}

// WellFormed says whether a token can be one of ours: 43 base64url characters. Anything else
// is not looked up.
func WellFormed(token string) bool {
	if len(token) != 43 {
		return false
	}
	for _, r := range token {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return false
		}
	}
	return true
}

// Origins checks the origins of an embeddable link: 1 to 10, each scheme://host[:port], http or
// https, with no path, query, user or wildcard. They are returned as the browser writes an
// origin (lower case, no default port, no trailing slash), without duplicates.
func Origins(list []string) ([]string, error) {
	if len(list) == 0 {
		return nil, errors.New("an embeddable link needs at least one allowed origin")
	}
	if len(list) > MaxOrigins {
		return nil, fmt.Errorf("at most %d allowed origins", MaxOrigins)
	}
	seen := map[string]bool{}
	var out []string
	for _, raw := range list {
		o, err := origin(strings.TrimSpace(raw))
		if err != nil {
			return nil, err
		}
		if !seen[o] {
			seen[o] = true
			out = append(out, o)
		}
	}
	return out, nil
}

func origin(s string) (string, error) {
	bad := fmt.Errorf("«%s» is not an origin: write it as https://host or https://host:port", cut(s, 80))
	if s == "" || strings.ContainsAny(s, "*' ;,\"\\") {
		return "", bad
	}
	u, err := url.Parse(s)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return "", bad
	}
	if u.Path != "" && u.Path != "/" {
		return "", bad
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "https" && scheme != "http" {
		return "", bad
	}
	host, port := strings.ToLower(u.Hostname()), u.Port()
	if host == "" {
		return "", bad
	}
	if (scheme == "https" && port == "443") || (scheme == "http" && port == "80") {
		port = ""
	}
	if strings.Contains(host, ":") { // an IPv6 address
		host = "[" + host + "]"
	}
	if port != "" {
		return scheme + "://" + host + ":" + port, nil
	}
	return scheme + "://" + host, nil
}

func cut(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n]) + "…"
	}
	return s
}

// PublicBaseURL is SHARE__PUBLIC_BASE_URL: the address the links are written with when the
// installation exposes them on a host of their own (the chart's share.publicHost), without a
// trailing slash; "" when the links use the interface's own address. Anything but an http(s)
// origin is ignored.
func PublicBaseURL() string {
	publicOnce.Do(func() {
		if o, err := origin(strings.TrimRight(strings.TrimSpace(os.Getenv("SHARE__PUBLIC_BASE_URL")), "/")); err == nil {
			publicBase = o
		}
	})
	return publicBase
}

var (
	publicOnce sync.Once
	publicBase string
)

// FrameAncestors is the CSP directive's value for a link: its origins, or 'none'.
func FrameAncestors(l *Link) string {
	if l == nil || !l.Embeddable || len(l.Origins) == 0 {
		return "'none'"
	}
	return strings.Join(l.Origins, " ")
}

// ExportHandle binds an export's task to one link: the task id and an HMAC of both, so that a
// session of one link cannot read the files of another (they are all the creator's exports).
func ExportHandle(secret []byte, linkID int64, task string) string {
	return task + "." + exportMAC(secret, linkID, task)
}

// ExportTask is the task of a handle of this link, or "".
func ExportTask(secret []byte, linkID int64, handle string) string {
	i := strings.LastIndexByte(handle, '.')
	if i <= 0 {
		return ""
	}
	task, mac := handle[:i], handle[i+1:]
	if !hmac.Equal([]byte(mac), []byte(exportMAC(secret, linkID, task))) {
		return ""
	}
	return task
}

func exportMAC(secret []byte, linkID int64, task string) string {
	m := hmac.New(sha256.New, secret)
	fmt.Fprintf(m, "share-export:%d:%s", linkID, task)
	return base64.RawURLEncoding.EncodeToString(m.Sum(nil))[:22]
}

// ── the settings ─────────────────────────────────────────────────────────────

// Settings are the feature's, from the panel's Share links tab.
type Settings struct {
	Enabled           bool       `json:"enabled"`
	MaxDays           int        `json:"max_days"`
	AICommentsPerHour int        `json:"ai_comments_per_hour"`
	UpdatedAt         *time.Time `json:"updated_at"`
	UpdatedBy         *string    `json:"updated_by"`
}

// Defaults are the settings with the row missing.
var Defaults = Settings{Enabled: true, MaxDays: 90, AICommentsPerHour: 20}

// LoadSettings reads the one row; missing, the defaults.
func LoadSettings(ctx context.Context, q Querier) (Settings, error) {
	s := Defaults
	err := q.QueryRow(ctx, `SELECT enabled, max_days, ai_comments_per_hour, updated_at, updated_by FROM share_link_settings WHERE id = 1`).
		Scan(&s.Enabled, &s.MaxDays, &s.AICommentsPerHour, &s.UpdatedAt, &s.UpdatedBy)
	if errors.Is(err, pgx.ErrNoRows) {
		return Defaults, nil
	}
	return s, err
}

// SaveSettings writes them, checked.
func SaveSettings(ctx context.Context, q Querier, s Settings, by string) error {
	if s.MaxDays < 1 || s.MaxDays > MaxDaysCeiling {
		return fmt.Errorf("the longest life of a link is 1 to %d days", MaxDaysCeiling)
	}
	if s.AICommentsPerHour < 0 || s.AICommentsPerHour > MaxAICeiling {
		return fmt.Errorf("the AI comments per link and hour are 0 to %d", MaxAICeiling)
	}
	_, err := q.Exec(ctx, `INSERT INTO share_link_settings (id, enabled, max_days, ai_comments_per_hour, updated_at, updated_by)
		VALUES (1, $1, $2, $3, $4, $5) ON CONFLICT (id) DO UPDATE SET enabled = $1, max_days = $2, ai_comments_per_hour = $3,
		updated_at = $4, updated_by = $5`, s.Enabled, s.MaxDays, s.AICommentsPerHour, Now(), by)
	return err
}

// ── the links ────────────────────────────────────────────────────────────────

// Link is a row of share_links, its JSON columns read.
type Link struct {
	ID                int64
	TokenHint         string
	Kind              string
	ObjectID          int64
	Label             string
	CreatorID         int64
	Embeddable        bool
	Origins           []string
	PasswordHash      *string
	PasswordTemporary bool
	Failures          int
	Params            map[string][]string
	Downloads         []string
	ExpiresAt         time.Time
	RevokedAt         *time.Time
	CreatedAt         time.Time
	LastOpenedAt      *time.Time
	Openings          int64
	AIHour            *time.Time
	AICalls           int
	AILast            *string
}

const linkColumns = `id, token_hint, kind, object_id, label, creator_id, embeddable, origins, password_hash, password_temporary,
	failures, params, downloads, expires_at, revoked_at, created_at, last_opened_at, openings, ai_hour, ai_calls, ai_last`

func scanLink(row pgx.Row) (*Link, error) {
	var l Link
	var origins, params, downloads string
	err := row.Scan(&l.ID, &l.TokenHint, &l.Kind, &l.ObjectID, &l.Label, &l.CreatorID, &l.Embeddable, &origins, &l.PasswordHash,
		&l.PasswordTemporary, &l.Failures, &params, &downloads, &l.ExpiresAt, &l.RevokedAt, &l.CreatedAt, &l.LastOpenedAt, &l.Openings,
		&l.AIHour, &l.AICalls, &l.AILast)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(origins), &l.Origins)
	_ = json.Unmarshal([]byte(params), &l.Params)
	_ = json.Unmarshal([]byte(downloads), &l.Downloads)
	if l.Params == nil {
		l.Params = map[string][]string{}
	}
	return &l, nil
}

// ByToken is the link of a token, nil when there is none (or the token is not well formed).
func ByToken(ctx context.Context, q Querier, token string) (*Link, error) {
	if !WellFormed(token) {
		return nil, nil
	}
	return scanLink(q.QueryRow(ctx, `SELECT `+linkColumns+` FROM share_links WHERE token_hash = $1`, Hash(token)))
}

// ByID is the link with this id, nil when there is none.
func ByID(ctx context.Context, q Querier, id int64) (*Link, error) {
	return scanLink(q.QueryRow(ctx, `SELECT `+linkColumns+` FROM share_links WHERE id = $1`, id))
}

// List is the links of these filters, newest first: kind and object (0: any), and creator (0: any).
func List(ctx context.Context, q Querier, kind string, objectID int64) ([]*Link, error) {
	sql := `SELECT ` + linkColumns + ` FROM share_links WHERE ($1 = '' OR kind = $1) AND ($2 = 0 OR object_id = $2) ORDER BY id DESC`
	rows, err := q.Query(ctx, sql, kind, objectID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Link
	for rows.Next() {
		l, err := scanLink(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, rows.Err()
}

// State is what a list says of a link: its life, not whether the creator may still serve it.
func State(l *Link, now time.Time) string {
	switch {
	case l.RevokedAt != nil:
		return "revoked"
	case !now.Before(l.ExpiresAt):
		return "expired"
	case l.Failures >= LockAfter:
		return "locked"
	case !l.Embeddable && l.PasswordTemporary:
		return "waiting"
	}
	return "active"
}

// Live says whether the link may answer as far as its own row says.
func Live(l *Link, now time.Time) bool {
	return l != nil && l.RevokedAt == nil && now.Before(l.ExpiresAt) && l.Failures < LockAfter
}

// New is what creating a link takes.
type New struct {
	Kind         string
	ObjectID     int64
	Label        string
	CreatorID    int64
	Embeddable   bool
	Origins      []string
	PasswordHash *string
	Params       map[string][]string
	Downloads    []string
	ExpiresAt    time.Time
}

// Create writes the link and returns it with its token, the only time the token exists.
func Create(ctx context.Context, q Querier, n New) (*Link, string, error) {
	token, err := NewSecret()
	if err != nil {
		return nil, "", err
	}
	origins, _ := json.Marshal(nonNil(n.Origins))
	params, _ := json.Marshal(n.Params)
	downloads, _ := json.Marshal(nonNil(n.Downloads))
	row := q.QueryRow(ctx, `INSERT INTO share_links (token_hash, token_hint, kind, object_id, label, creator_id, embeddable, origins,
		password_hash, password_temporary, failures, params, downloads, expires_at, created_at, openings, ai_calls)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, 0, $10, $11, $12, $13, 0, 0) RETURNING `+linkColumns,
		Hash(token), token[:4], n.Kind, n.ObjectID, n.Label, n.CreatorID, n.Embeddable, string(origins), n.PasswordHash,
		string(params), string(downloads), n.ExpiresAt, Now())
	l, err := scanLink(row)
	return l, token, err
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// Revoke marks it revoked and closes its sessions.
func Revoke(ctx context.Context, q Querier, id int64) error {
	if _, err := q.Exec(ctx, `UPDATE share_links SET revoked_at = $2 WHERE id = $1 AND revoked_at IS NULL`, id, Now()); err != nil {
		return err
	}
	return CloseSessions(ctx, q, id)
}

// ResetPassword sets a new temporary password: the recipient chooses theirs again, the brake
// starts again, and every open session of the link is closed.
func ResetPassword(ctx context.Context, q Querier, id int64, hash string) error {
	if _, err := q.Exec(ctx, `UPDATE share_links SET password_hash = $2, password_temporary = true, failures = 0 WHERE id = $1`, id, hash); err != nil {
		return err
	}
	return CloseSessions(ctx, q, id)
}

// SetOwnPassword is the recipient's first choice: it holds only while the temporary password is
// still the one that was checked (two people at once: one wins).
func SetOwnPassword(ctx context.Context, q Querier, id int64, temporaryHash, hash string) (bool, error) {
	tag, err := q.Exec(ctx, `UPDATE share_links SET password_hash = $3, password_temporary = false
		WHERE id = $1 AND password_temporary AND password_hash = $2`, id, temporaryHash, hash)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// Failed counts a wrong password; it says whether this one locked the link.
func Failed(ctx context.Context, q Querier, id int64) (bool, error) {
	var n int
	if err := q.QueryRow(ctx, `UPDATE share_links SET failures = failures + 1 WHERE id = $1 RETURNING failures`, id).Scan(&n); err != nil {
		return false, err
	}
	return n == LockAfter, nil
}

// Opened counts an opening (a new session) and clears the failures.
func Opened(ctx context.Context, q Querier, id int64) error {
	_, err := q.Exec(ctx, `UPDATE share_links SET openings = openings + 1, last_opened_at = $2, failures = 0 WHERE id = $1`, id, Now())
	return err
}

// ── the sessions ─────────────────────────────────────────────────────────────

// OpenSession writes a session of the link and returns its secret.
func OpenSession(ctx context.Context, q Querier, linkID int64, purpose, ip string) (string, time.Time, error) {
	secret, err := NewSecret()
	if err != nil {
		return "", time.Time{}, err
	}
	now := Now()
	ttl := SessionTTL
	if purpose == PurposeChange {
		ttl = ChangeTTL
	}
	var ipv *string
	if ip != "" {
		if len(ip) > 64 {
			ip = ip[:64]
		}
		ipv = &ip
	}
	until := now.Add(ttl)
	_, err = q.Exec(ctx, `INSERT INTO share_link_sessions (id, link_id, purpose, created_at, last_seen_at, expires_at, ip)
		VALUES ($1, $2, $3, $4, $4, $5, $6)`, Hash(secret), linkID, purpose, now, until, ipv)
	return secret, until, err
}

// Session checks a session's secret for this link and purpose; a session past its idle time or
// its expiry is closed. It returns the session's id (its hash), "" when it does not hold.
func Session(ctx context.Context, q Querier, linkID int64, purpose, secret string) (string, error) {
	if secret == "" || len(secret) > 100 {
		return "", nil
	}
	id := Hash(secret)
	var link int64
	var p string
	var seen, until time.Time
	err := q.QueryRow(ctx, `SELECT link_id, purpose, last_seen_at, expires_at FROM share_link_sessions WHERE id = $1`, id).
		Scan(&link, &p, &seen, &until)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if link != linkID || p != purpose {
		return "", nil
	}
	now := Now()
	if !now.Before(until) || (purpose == PurposeView && now.Sub(seen) > SessionIdle) {
		_, err := q.Exec(ctx, `DELETE FROM share_link_sessions WHERE id = $1`, id)
		return "", err
	}
	if now.Sub(seen) > time.Minute { // as user_sessions: written at most once a minute
		if _, err := q.Exec(ctx, `UPDATE share_link_sessions SET last_seen_at = $2 WHERE id = $1`, id, now); err != nil {
			return "", err
		}
	}
	return id, nil
}

// CloseSession removes one session.
func CloseSession(ctx context.Context, q Querier, id string) error {
	_, err := q.Exec(ctx, `DELETE FROM share_link_sessions WHERE id = $1`, id)
	return err
}

// CloseSessions removes every session of a link.
func CloseSessions(ctx context.Context, q Querier, linkID int64) error {
	_, err := q.Exec(ctx, `DELETE FROM share_link_sessions WHERE link_id = $1`, linkID)
	return err
}

// Sweep is the orchestrator's chore: sessions past their expiry or idle time, the sessions of
// links no longer live, the links revoked or expired more than PurgeAfter ago, and the links
// whose object is gone. It returns how many links it deleted.
func Sweep(ctx context.Context, q Querier, now time.Time) (int64, error) {
	if _, err := q.Exec(ctx, `DELETE FROM share_link_sessions WHERE expires_at <= $1 OR (purpose = 'view' AND last_seen_at < $2)
		OR link_id IN (SELECT id FROM share_links WHERE revoked_at IS NOT NULL OR expires_at <= $1)`, now, now.Add(-SessionIdle)); err != nil {
		return 0, err
	}
	tag, err := q.Exec(ctx, `DELETE FROM share_links WHERE coalesce(revoked_at, expires_at) <= $1
		OR (kind = 'report' AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.id = object_id))
		OR (kind = 'dashboard' AND NOT EXISTS (SELECT 1 FROM dashboards d WHERE d.id = object_id))`, now.Add(-PurgeAfter))
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// ── the AI cap ───────────────────────────────────────────────────────────────

// TakeAICall counts one call to the AI service for this link in the current hour; false when
// the cap is reached (and nothing is counted).
func TakeAICall(ctx context.Context, q Querier, linkID int64, perHour int, now time.Time) (bool, error) {
	if perHour <= 0 {
		return false, nil
	}
	hour := now.Truncate(time.Hour)
	tag, err := q.Exec(ctx, `UPDATE share_links SET ai_hour = $2, ai_calls = CASE WHEN ai_hour = $2 THEN ai_calls + 1 ELSE 1 END
		WHERE id = $1 AND (ai_hour IS DISTINCT FROM $2 OR ai_calls < $3)`, linkID, hour, perHour)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// KeepAILast stores the last comments the link was given.
func KeepAILast(ctx context.Context, q Querier, linkID int64, last string) error {
	_, err := q.Exec(ctx, `UPDATE share_links SET ai_last = $2 WHERE id = $1`, linkID, last)
	return err
}
