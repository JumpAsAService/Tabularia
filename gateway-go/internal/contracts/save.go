package contracts

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Contract is a data_contracts row, the columns save and summary read.
type Contract struct {
	ID, DatasourceID, Version, Errors, Warnings int64
	Document                                    string
	Enabled                                     bool
	Status                                      string
	CheckedAt, BlockedAt                        *time.Time
	NotifyEmails                                *string
	NotifyConnectionID                          *int64
}

// Notify is save's `notify`: (addresses, SMTP connection), checked already; nil = as before.
type Notify struct {
	Emails       *string
	ConnectionID *int64
}

const contractColumns = `id, datasource_id, version, errors, warnings, document, enabled, status, checked_at, blocked_at,
	notify_emails, notify_connection_id`

// Get is services/contracts.py get: the datasource's contract (the first row), nil when none.
func Get(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, dsID int64) (*Contract, error) {
	var c Contract
	err := q.QueryRow(ctx, `SELECT `+contractColumns+` FROM data_contracts WHERE datasource_id = $1 LIMIT 1`, dsID).Scan(&c.ID,
		&c.DatasourceID, &c.Version, &c.Errors, &c.Warnings, &c.Document, &c.Enabled, &c.Status, &c.CheckedAt, &c.BlockedAt,
		&c.NotifyEmails, &c.NotifyConnectionID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// Save is services/contracts.py save, in the caller's transaction (it does NOT commit): creates
// or replaces the datasource's contract. A changed document is a new version and «to check»
// again. `document` is a json.loads value (pyd's), written as json.dumps(document,
// ensure_ascii=False, sort_keys=True); the datasource's row stays locked until the commit.
func Save(ctx context.Context, tx pgx.Tx, dsID int64, document any, enabled bool, userID int64, notify *Notify) (*Contract, error) {
	now := time.Now().UTC()
	text, err := pyjson.Dumps(PyjsonOf(document), pyjson.Options{SortKeys: true})
	if err != nil {
		return nil, err
	}
	if _, err := tx.Exec(ctx, `SELECT id FROM datasources WHERE id = $1 FOR UPDATE`, dsID); err != nil {
		return nil, err
	}
	c, err := Get(ctx, tx, dsID)
	if err != nil {
		return nil, err
	}
	changed := true
	if c == nil {
		var last *int64
		err := tx.QueryRow(ctx, `SELECT version FROM data_contract_versions WHERE datasource_id = $1 ORDER BY version DESC LIMIT 1`,
			dsID).Scan(&last)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		version := int64(1)
		if last != nil && *last != 0 {
			version = *last + 1
		}
		if _, err := tx.Exec(ctx, `INSERT INTO data_contracts (datasource_id, version, document, enabled, status, checked_at, errors,
			warnings, last_report, blocked_at, blocked_run_id, notify_emails, notify_connection_id, created_by, updated_by, created_at,
			updated_at) VALUES ($1, $2, $3, $4, 'pending', NULL, 0, 0, NULL, NULL, NULL, NULL, NULL, $5, NULL, $6, $6)`,
			dsID, version, text, enabled, userID, now); err != nil {
			return nil, err
		}
		if c, err = Get(ctx, tx, dsID); err != nil {
			return nil, err
		}
	} else {
		changed = c.Document != text
		if changed {
			c.Version++
		}
		if _, err := tx.Exec(ctx, `UPDATE data_contracts SET version = $1, document = $2, enabled = $3 WHERE id = $4`,
			c.Version, text, enabled, c.ID); err != nil {
			return nil, err
		}
	}
	if changed {
		if _, err := tx.Exec(ctx, `UPDATE data_contracts SET status = 'pending', checked_at = NULL, errors = 0, warnings = 0,
			last_report = NULL WHERE id = $1`, c.ID); err != nil {
			return nil, err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO data_contract_versions (datasource_id, version, document, created_by, created_at)
			VALUES ($1, $2, $3, $4, $5)`, dsID, c.Version, text, userID, now); err != nil {
			return nil, err
		}
	}
	if notify != nil {
		if _, err := tx.Exec(ctx, `UPDATE data_contracts SET notify_emails = $1, notify_connection_id = $2 WHERE id = $3`,
			notify.Emails, notify.ConnectionID, c.ID); err != nil {
			return nil, err
		}
	}
	if _, err := tx.Exec(ctx, `UPDATE data_contracts SET updated_by = $1, updated_at = $2 WHERE id = $3`, userID, now, c.ID); err != nil {
		return nil, err
	}
	return Get(ctx, tx, dsID)
}

// Summary is services/contracts.py summary: what a listing shows of a contract.
type Summary struct {
	Status    string     // effective_status: «failed» while the last update stays refused
	CheckedAt *time.Time // a UtcDateTime in the answer (pyjson.Isoformat)
	Errors    int64
	Warnings  int64
	Blocked   bool
	Version   int64
}

// SummaryOf is summary(c).
func SummaryOf(c *Contract) Summary {
	status := c.Status
	if c.BlockedAt != nil {
		status = "failed"
	}
	return Summary{Status: status, CheckedAt: c.CheckedAt, Errors: c.Errors, Warnings: c.Warnings, Blocked: c.BlockedAt != nil,
		Version: c.Version}
}
