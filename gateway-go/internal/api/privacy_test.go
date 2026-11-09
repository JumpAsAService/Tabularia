package api

import (
	"context"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// A new installation starts with the privacy notice switched off (its sample text kept for
// the administrator to adapt); one that already has a notice keeps its own setting.
func TestPrivacyNoticeStartsSwitchedOff(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	// in a transaction rolled back at the end: the other packages' tests share the database
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `DELETE FROM privacy_notice`); err != nil {
		t.Fatal(err)
	}
	n, err := readPrivacyFrom(ctx, tx)
	if err != nil {
		t.Fatal(err)
	}
	if n.enabled || n.summary == "" || n.body == "" {
		t.Fatalf("a new notice: enabled=%v, summary %q, body %d bytes; want off, with its sample text", n.enabled, n.summary, len(n.body))
	}
	// switched on by an administrator: read again, it stays on
	if _, err := tx.Exec(ctx, `UPDATE privacy_notice SET enabled = true`); err != nil {
		t.Fatal(err)
	}
	if n, err = readPrivacyFrom(ctx, tx); err != nil || !n.enabled {
		t.Fatalf("an existing notice keeps its setting: enabled=%v err=%v", n.enabled, err)
	}
}
