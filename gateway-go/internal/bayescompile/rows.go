package bayescompile

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// The table bayes_binaries as the loop (lot C2) and the Compiler (lot C3) read and write it: a Row is one row, a NULL
// column its zero value. These are the only functions of the package that touch the database; the rules above decide,
// the callers lock (the loop's advisory lock LoopLock, the rows FOR UPDATE) and call these.

// Columns are bayes_binaries' columns in the order Scan reads them.
const Columns = `id, cache_key, program_sha256, cmdstan, image_digest, arch, group_version, state, reason, detail,
	requested_by, model_id, version, attempt, job_name, deadline_seconds, memory_bytes, binary_sha256, binary_bytes,
	info_sha256, warnings, seconds, queued_at, started_at, finished_at, last_used_at`

// Execer is what Write, Insert and Delete need of a pool or a transaction.
type Execer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Scan reads a row selected with Columns.
func Scan(s pgx.Row) (Row, error) {
	var r Row
	var reason, detail, jobName, binarySHA, infoSHA, warnings *string
	var requestedBy, modelID, version, deadline, memory, binaryBytes *int64
	var seconds *float64
	var queued, started, finished, used *time.Time
	if err := s.Scan(&r.ID, &r.CacheKey, &r.ProgramSHA256, &r.CmdStan, &r.ImageDigest, &r.Arch, &r.GroupVersion, &r.State,
		&reason, &detail, &requestedBy, &modelID, &version, &r.Attempt, &jobName, &deadline, &memory, &binarySHA,
		&binaryBytes, &infoSHA, &warnings, &seconds, &queued, &started, &finished, &used); err != nil {
		return Row{}, err
	}
	str := func(p *string) string {
		if p == nil {
			return ""
		}
		return *p
	}
	num := func(p *int64) int64 {
		if p == nil {
			return 0
		}
		return *p
	}
	at := func(p *time.Time) time.Time {
		if p == nil {
			return time.Time{}
		}
		return *p
	}
	r.Reason, r.Detail, r.JobName, r.BinarySHA256, r.InfoSHA256, r.Warnings =
		str(reason), str(detail), str(jobName), str(binarySHA), str(infoSHA), str(warnings)
	r.RequestedBy, r.ModelID, r.Version, r.DeadlineSeconds, r.MemoryBytes, r.BinaryBytes =
		num(requestedBy), num(modelID), num(version), num(deadline), num(memory), num(binaryBytes)
	if seconds != nil {
		r.Seconds = *seconds
	}
	r.QueuedAt, r.StartedAt, r.FinishedAt, r.LastUsedAt = at(queued), at(started), at(finished), at(used)
	return r, nil
}

// args are the columns after id, in Columns' order, a zero value written as NULL (seconds too, but on a ready row).
func args(r Row) []any {
	str := func(s string) any {
		if s == "" {
			return nil
		}
		return s
	}
	num := func(n int64) any {
		if n == 0 {
			return nil
		}
		return n
	}
	at := func(t time.Time) any {
		if t.IsZero() {
			return nil
		}
		return t
	}
	var seconds any
	if r.Seconds != 0 || r.State == StateReady {
		seconds = r.Seconds
	}
	return []any{r.CacheKey, r.ProgramSHA256, r.CmdStan, r.ImageDigest, r.Arch, r.GroupVersion, r.State, str(r.Reason),
		str(r.Detail), num(r.RequestedBy), num(r.ModelID), num(r.Version), r.Attempt, str(r.JobName), num(r.DeadlineSeconds),
		num(r.MemoryBytes), str(r.BinarySHA256), num(r.BinaryBytes), str(r.InfoSHA256), str(r.Warnings), seconds,
		at(r.QueuedAt), at(r.StartedAt), at(r.FinishedAt), at(r.LastUsedAt)}
}

// Insert writes a new row (its ID is ignored) and returns its id. A row of the same cache_key already there is the
// unique constraint's error (uq_bayes_binary_cache_key): the caller reads it again.
func Insert(ctx context.Context, q Execer, r Row) (int64, error) {
	var id int64
	err := q.QueryRow(ctx, `INSERT INTO bayes_binaries (cache_key, program_sha256, cmdstan, image_digest, arch,
		group_version, state, reason, detail, requested_by, model_id, version, attempt, job_name, deadline_seconds,
		memory_bytes, binary_sha256, binary_bytes, info_sha256, warnings, seconds, queued_at, started_at, finished_at,
		last_used_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
		$21, $22, $23, $24, $25) RETURNING id`, args(r)...).Scan(&id)
	return id, err
}

// Write updates every column of the row with its id.
func Write(ctx context.Context, q Execer, r Row) error {
	_, err := q.Exec(ctx, `UPDATE bayes_binaries SET cache_key = $2, program_sha256 = $3, cmdstan = $4,
		image_digest = $5, arch = $6, group_version = $7, state = $8, reason = $9, detail = $10, requested_by = $11,
		model_id = $12, version = $13, attempt = $14, job_name = $15, deadline_seconds = $16, memory_bytes = $17,
		binary_sha256 = $18, binary_bytes = $19, info_sha256 = $20, warnings = $21, seconds = $22, queued_at = $23,
		started_at = $24, finished_at = $25, last_used_at = $26 WHERE id = $1`, append([]any{r.ID}, args(r)...)...)
	return err
}

// Delete removes rows by id: their state becomes none (overtaken, a stale image, evicted, cleaned).
func Delete(ctx context.Context, q Execer, ids ...int64) error {
	if len(ids) == 0 {
		return nil
	}
	_, err := q.Exec(ctx, `DELETE FROM bayes_binaries WHERE id = ANY($1)`, ids)
	return err
}
