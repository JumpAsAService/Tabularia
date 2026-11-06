// Package loginthrottle is services/login_throttle.py: a brake on repeated sign-in attempts,
// counted in the table login_attempts (shared by the replicas) by (address, email) and, more
// loosely, by address alone.
package loginthrottle

import (
	"context"
	"errors"
	"math"
	"strings"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	Threshold        = 5
	Window           = 300.0 // seconds
	Block            = 60.0
	MaxBlock         = 900.0
	AddressThreshold = 30
	// All is the email of the per-address row: emails are stripped, so no login produces it
	All = " *"
)

// key is _key: «?» for no address, the email stripped and lower-cased.
func key(ip *string, email string) (string, string) {
	a := "?"
	if ip != nil && *ip != "" {
		a = *ip
	}
	return a, strings.ToLower(strings.TrimFunc(email, unicode.IsSpace))
}

func now() time.Time { return time.Now().UTC() }

func wait(ctx context.Context, db *pgxpool.Pool, ip, email string, threshold int) (float64, error) {
	var failures int
	var last time.Time
	err := db.QueryRow(ctx, `SELECT failures, last_at FROM login_attempts WHERE ip = $1 AND email = $2`, ip, email).Scan(&failures, &last)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	elapsed := now().Sub(last).Seconds()
	if failures < threshold || elapsed > Window {
		return 0, nil
	}
	// doubles at every error past the threshold, with a cap
	block := math.Min(Block*math.Pow(2, float64(failures-threshold)), MaxBlock)
	return math.Max(0, block-elapsed), nil
}

// RequestWait is request_wait: seconds to wait before trying again; 0 = go ahead.
func RequestWait(ctx context.Context, db *pgxpool.Pool, ip *string, email string) (float64, error) {
	a, e := key(ip, email)
	one, err := wait(ctx, db, a, e, Threshold)
	if err != nil {
		return 0, err
	}
	all, err := wait(ctx, db, a, All, AddressThreshold)
	return math.Max(one, all), err
}

// RecordFailure is record_failure: one more error for (address, email) and for the address.
func RecordFailure(ctx context.Context, db *pgxpool.Pool, ip *string, email string) error {
	a, e := key(ip, email)
	if err := count(ctx, db, a, e); err != nil {
		return err
	}
	return count(ctx, db, a, All)
}

// count is _count: the database increments, so two replicas recording together lose nothing.
func count(ctx context.Context, db *pgxpool.Pool, ip, email string) error {
	at := now()
	increment := func() (int64, error) {
		tag, err := db.Exec(ctx, `UPDATE login_attempts SET failures = CASE WHEN last_at < $1 THEN 1 ELSE failures + 1 END,
			last_at = $2 WHERE ip = $3 AND email = $4`, at.Add(-Window*time.Second), at, ip, email)
		return tag.RowsAffected(), err
	}
	n, err := increment()
	if err != nil || n > 0 {
		return err
	}
	_, err = db.Exec(ctx, `INSERT INTO login_attempts (ip, email, failures, last_at) VALUES ($1, $2, 1, $3)`, ip, email, at)
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		// another replica wrote the first error a moment before
		_, err = increment()
	}
	return err
}

// RecordSuccess is record_success: the (address, email) count starts again.
func RecordSuccess(ctx context.Context, db *pgxpool.Pool, ip *string, email string) error {
	a, e := key(ip, email)
	_, err := db.Exec(ctx, `DELETE FROM login_attempts WHERE ip = $1 AND email = $2`, a, e)
	return err
}

// Seconds is the «Try again in N seconds» of a wait: int(wait) + 1.
func Seconds(wait float64) int { return int(wait) + 1 }
