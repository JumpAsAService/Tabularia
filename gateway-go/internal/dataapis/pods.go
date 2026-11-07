package dataapis

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The pods' side (docs/specs/data-apis.md, «Runtime»): their token, the keys and the data
// files the manifest gives them, and the usage they send back.

// RotatePodToken makes a new pod token for the data API and keeps its sha-256: the previous
// token stops at once. The token is returned to be put in the Deployment's Secret, and never
// kept.
func RotatePodToken(ctx context.Context, q Querier, apiID int64) (string, error) {
	token, err := dataapiwire.Random(dataapiwire.PodTokenPrefix)
	if err != nil {
		return "", err
	}
	tag, err := q.Exec(ctx, `UPDATE data_apis SET pod_token_hash = $2 WHERE id = $1`, apiID, dataapiwire.Hash(token))
	if err != nil {
		return "", err
	}
	if tag.RowsAffected() != 1 {
		return "", fmt.Errorf("data api %d: no such data API", apiID)
	}
	return token, nil
}

// ForgetPodToken drops the data API's pod token: nothing opens its manifest until the reconcile
// loop makes a new one (RotatePodToken), as it does at the first round after an activation.
func ForgetPodToken(ctx context.Context, q Querier, apiID int64) error {
	_, err := q.Exec(ctx, `UPDATE data_apis SET pod_token_hash = NULL WHERE id = $1`, apiID)
	return err
}

// ByPodToken is the data API `apiID` when `token` is its pod token; nil otherwise (no such
// data API, no token yet, another one's, or not a token at all).
func ByPodToken(ctx context.Context, q Querier, apiID int64, token string) (*API, error) {
	if !dataapiwire.WellFormed(token, dataapiwire.PodTokenPrefix) {
		return nil, nil
	}
	var hash *string
	err := q.QueryRow(ctx, `SELECT pod_token_hash FROM data_apis WHERE id = $1`, apiID).Scan(&hash)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if hash == nil || !dataapiwire.SameHash(*hash, dataapiwire.Hash(token)) {
		return nil, nil
	}
	return Get(ctx, q, apiID)
}

// ManifestKeys are the data API's keys as the pods check them, with their hashes; the ones
// that no longer answer are given too, so a pod can say why.
func ManifestKeys(ctx context.Context, q Querier, apiID int64) ([]dataapiwire.Key, error) {
	rows, err := q.Query(ctx, `SELECT id, prefix, hash, expires_at, revoked_at, grace_until, rate_per_minute, resources, cidrs
		FROM data_api_keys WHERE data_api_id = $1 ORDER BY id`, apiID)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (dataapiwire.Key, error) {
		var k dataapiwire.Key
		var res, cidrs string
		if err := row.Scan(&k.ID, &k.Hint, &k.Hash, &k.ExpiresAt, &k.RevokedAt, &k.GraceUntil, &k.RatePerMinute, &res, &cidrs); err != nil {
			return k, err
		}
		k.Resources, k.CIDRs = []string{}, []string{}
		if err := jsonList(res, &k.Resources); err != nil {
			return k, fmt.Errorf("key %d: its resources: %w", k.ID, err)
		}
		if err := jsonList(cidrs, &k.CIDRs); err != nil {
			return k, fmt.Errorf("key %d: its addresses: %w", k.ID, err)
		}
		return k, nil
	})
	if out == nil {
		out = []dataapiwire.Key{}
	}
	return out, err
}

// DataFile is where a datasource's data is now, and since when.
type DataFile struct {
	Bucket string
	Key    string
	// when the data was written: the run that produced it ended, the datasource was refreshed
	// or, for one made otherwise, last changed
	AsOf        time.Time
	SnapshotRun *int64
}

// Version is the data's version as the answers give it: AsOf, to the second, in UTC.
func (f DataFile) Version() string { return f.AsOf.UTC().Format("2006-01-02T15:04:05Z") }

// Fingerprint changes whenever the data file does: another object, another run, another time.
func (f DataFile) Fingerprint() string {
	run := ""
	if f.SnapshotRun != nil {
		run = fmt.Sprint(*f.SnapshotRun)
	}
	h := sha256.Sum256([]byte(f.Bucket + "\x00" + f.Key + "\x00" + run + "\x00" + f.AsOf.UTC().Format(time.RFC3339Nano)))
	return hex.EncodeToString(h[:12])
}

// DataFiles are the data files of these datasources (the ones that exist).
func DataFiles(ctx context.Context, q Querier, ids []int64) (map[int64]DataFile, error) {
	out := map[int64]DataFile{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := q.Query(ctx, `SELECT d.id, d.bucket, d.key, d.snapshot_run_id, coalesce(r.finished_at, d.refreshed_at, d.updated_at)
		FROM datasources d LEFT JOIN runs r ON r.id = d.snapshot_run_id WHERE d.id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var f DataFile
		if err := rows.Scan(&id, &f.Bucket, &f.Key, &f.SnapshotRun, &f.AsOf); err != nil {
			return nil, err
		}
		out[id] = f
	}
	return out, rows.Err()
}

// UsageWindow is how far back (and ahead, for clocks that drift) a pod's minute is taken.
const (
	UsageMaxAge   = 24 * time.Hour
	UsageMaxAhead = 5 * time.Minute
)

// RecordUsage adds a pod's counts to data_api_usage, a row per key and minute, and moves the
// keys' «last used». A key of another data API, or a minute out of the window, is left out:
// the number of entries kept is returned.
func RecordUsage(ctx context.Context, tx pgx.Tx, apiID int64, minutes []dataapiwire.UsageMinute) (int, error) {
	rows, err := tx.Query(ctx, `SELECT id FROM data_api_keys WHERE data_api_id = $1`, apiID)
	if err != nil {
		return 0, err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return 0, err
	}
	own := map[int64]bool{}
	for _, id := range ids {
		own[id] = true
	}
	now := Now()
	kept := 0
	last := map[int64]time.Time{}
	for _, m := range minutes {
		minute := m.Minute.UTC().Truncate(time.Minute)
		if !own[m.KeyID] || minute.Before(now.Add(-UsageMaxAge)) || minute.After(now.Add(UsageMaxAhead)) ||
			m.Requests < 0 || m.Rows < 0 || m.Errors < 0 || m.Throttled < 0 {
			continue
		}
		if _, err := tx.Exec(ctx, `INSERT INTO data_api_usage (data_api_id, key_id, minute, requests, rows, errors, throttled)
			VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (key_id, minute) DO UPDATE SET
			requests = data_api_usage.requests + EXCLUDED.requests, rows = data_api_usage.rows + EXCLUDED.rows,
			errors = data_api_usage.errors + EXCLUDED.errors, throttled = data_api_usage.throttled + EXCLUDED.throttled`,
			apiID, m.KeyID, minute, clampInt32(m.Requests), clampInt32(m.Rows), clampInt32(m.Errors), clampInt32(m.Throttled)); err != nil {
			return kept, err
		}
		kept++
		if m.Requests > 0 && minute.After(last[m.KeyID]) {
			last[m.KeyID] = minute
		}
	}
	for id, at := range last {
		if _, err := tx.Exec(ctx, `UPDATE data_api_keys SET last_used_at = $2 WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < $2)`,
			id, at); err != nil {
			return kept, err
		}
	}
	return kept, nil
}

// the usage columns are integers: a pod's minute never reaches past them, a bad one is capped
func clampInt32(n int64) int64 {
	if n > 1<<31-1 {
		return 1<<31 - 1
	}
	return n
}

// jsonList reads a JSON list of the table; an empty text is an empty list
func jsonList(text string, into *[]string) error {
	if text == "" {
		return nil
	}
	if err := json.Unmarshal([]byte(text), into); err != nil {
		return err
	}
	if *into == nil {
		*into = []string{}
	}
	return nil
}
