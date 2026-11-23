package bayescompile

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

// The transaction's advisory locks of Enqueue, in the two-key space of PostgreSQL's advisory locks (which never
// overlaps LoopLock's one-key space): first the person's model, then the program's key, never the other order. The
// first makes a newer version's request see the older one's queued row (the overtaking); the second makes two requests
// of one key insert its row once (the unique constraint uq_bayes_binary_cache_key is never the answer).
const (
	enqueueModelLock = 7_402_161
	enqueueKeyLock   = 7_402_162
)

// ErrAltered is a stored binary that is not the one its row records, or is gone from Tabularia's storage: the row is
// forgotten (state none) and the run compiles the program again.
var ErrAltered = errors.New("bayes compile: the stored binary is not the one recorded")

// ErrGone is a binary whose ready row went between Ready and Place (evicted, the cache emptied): the run asks again.
var ErrGone = errors.New("bayes compile: the binary is no longer in the cache")

// ErrRefused is a program a run does not compile first (Refused: a timeout under the same limits, a binary larger than
// the cache, a failure of the compiler): the step fails at once with the state's sentence.
var ErrRefused = errors.New("bayes compile: this program is not compiled again")

// stepBinaryKey is where a run's binary goes in the scripts bucket: a step's prefix, env-<…>/<step>/model (the engine's
// /scripts/binary/place takes nothing else).
var stepBinaryKey = regexp.MustCompile(`^env-[A-Za-z0-9_.-]+(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+/model$`)

// Current is the group's current image and the limits in force: what a state answers for and what a request is queued
// under.
type Current struct {
	Image        Image
	GroupVersion int64 // the version of the group's managed environment whose image it is
	Limits       Limits
}

// Storage is what the Compiler asks of the engine's routes for the binaries' bytes, which the gateway never holds.
type Storage interface {
	// Place copies Tabularia's bayes-binaries/<sha256> into the scripts bucket at key, the engine hashing it on the way;
	// ErrAltered when the stored bytes are not sha256's or are gone.
	Place(ctx context.Context, sha256, key string) error
	// Delete removes binaries from Tabularia's storage.
	Delete(ctx context.Context, sha256s []string) error
}

// Pool is what the Compiler needs of the database.
type Pool interface {
	Begin(ctx context.Context) (pgx.Tx, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// Binary is a ready program's binary for the group's current image.
type Binary struct {
	CacheKey, BinarySHA256 string
	Bytes                  int64
}

// Binaries is what a sampling step asks before its Job (flow-node): the binary of a stored program for the group's
// current image, and its copy into the step's prefix.
type Binaries interface {
	// Ready is the binary when the state is ready, nil and the state otherwise.
	Ready(ctx context.Context, programSHA256 string) (*Binary, bayesmodels.CompileState, error)
	// Need is the run's fallback: the binary when ready; ErrRefused (with the state) when the run fails at once;
	// otherwise the compile is asked for (r.By is the run's authority) and the state to wait on is returned.
	Need(ctx context.Context, r bayesmodels.CompileRequest) (*Binary, bayesmodels.CompileState, error)
	// Place copies the binary into the scripts bucket at key and records the use (least recently used). ErrAltered:
	// the stored object was not the binary (the row is forgotten, the run compiles again); ErrGone: the row went.
	Place(ctx context.Context, b *Binary, key string) error
}

// Compiler is stan-compile's side of the store (bayesmodels.Compiler and BatchCompiler) and of the runs (Binaries),
// over bayes_binaries: Enqueue writes a request's row by the rules of «The queue» (PlanEnqueue), the states answer for
// the group's current image (StateOf), and the compile loop (gateway-go/internal/api, jobs_bayes_compile.go) does the
// rest.
type Compiler struct {
	DB Pool
	// Current is the group's current image and the limits in force; nil (and no error) while the group cannot compile
	// (not shipped, disabled, being built): every state is then none and a request queues nothing.
	Current func(ctx context.Context) (*Current, error)
	Storage Storage
	Log     *slog.Logger
	// Now is the time written in the rows (naive UTC); nil is time.Now().UTC().
	Now func() time.Time
}

var (
	_ bayesmodels.Compiler      = (*Compiler)(nil)
	_ bayesmodels.BatchCompiler = (*Compiler)(nil)
	_ Binaries                  = (*Compiler)(nil)
)

func (c *Compiler) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now().UTC()
}

// Enqueue applies «The queue» to a request in one transaction: the key's row and the person's queued rows of the same
// model locked FOR UPDATE, PlanEnqueue, the row written when it is queued, and the overtaken rows deleted whatever the
// key's row is (the owner's correction of 2026-11-24 from C1: a newer save always takes the older queued version out).
func (c *Compiler) Enqueue(ctx context.Context, r bayesmodels.CompileRequest) error {
	_, err := c.enqueue(ctx, r, false)
	return err
}

// enqueue is Enqueue; with forRun, a row the run would not compile (Refused) is left as it is and returned with
// ErrRefused. It returns the key's row as it is after the request (nil: the group cannot compile now).
func (c *Compiler) enqueue(ctx context.Context, r bayesmodels.CompileRequest, forRun bool) (*Row, error) {
	cur, err := c.Current(ctx)
	if err != nil || cur == nil {
		return nil, err
	}
	fresh, err := NewRow(r.ProgramSHA256, cur.Image, cur.GroupVersion)
	if err != nil {
		return nil, err
	}
	tx, err := c.DB.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if r.By != 0 && r.ModelID != 0 {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, hashtext($2))`, int32(enqueueModelLock),
			fmt.Sprintf("%d/%d", r.By, r.ModelID)); err != nil {
			return nil, err
		}
	}
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, hashtext($2))`, int32(enqueueKeyLock), fresh.CacheKey); err != nil {
		return nil, err
	}
	// one statement, in id order as the loop locks its rows: the key's row and the overtaking's candidates
	rows, err := selectRows(ctx, tx, `SELECT `+Columns+` FROM bayes_binaries WHERE cache_key = $1
		OR (state = 'queued' AND requested_by = $2 AND model_id = $3 AND version < $4) ORDER BY id FOR UPDATE`,
		fresh.CacheKey, r.By, r.ModelID, r.Version)
	if err != nil {
		return nil, err
	}
	row := fresh
	var others []Row
	for _, x := range rows {
		if x.CacheKey == fresh.CacheKey {
			row = x
		} else {
			others = append(others, x)
		}
	}
	if forRun && Refused(&row, cur.Limits) {
		return &row, ErrRefused
	}
	p := PlanEnqueue(row, r, others, cur.Limits, c.now())
	if p.Queue {
		if p.Row.ID == 0 {
			if p.Row.ID, err = Insert(ctx, tx, p.Row); err != nil {
				return nil, err
			}
		} else if err := Write(ctx, tx, p.Row); err != nil {
			return nil, err
		}
	}
	ids := make([]int64, len(p.Overtaken))
	for i, o := range p.Overtaken {
		ids[i] = o.ID
	}
	if err := Delete(ctx, tx, ids...); err != nil {
		return nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &p.Row, nil
}

// State is the program's compile state for the group's current image.
func (c *Compiler) State(ctx context.Context, programSHA256 string) (bayesmodels.CompileState, error) {
	states, err := c.States(ctx, []string{programSHA256})
	if err != nil {
		return bayesmodels.CompileState{}, err
	}
	if s, ok := states[programSHA256]; ok {
		return s, nil
	}
	return StateOf(nil, false, 0), nil
}

// States is the compile state of each program for the group's current image in one read (the list asks once for all
// its models); a program with no row, or with no key (the group cannot compile now, a sha256 not of its form), is none.
func (c *Compiler) States(ctx context.Context, programSHA256s []string) (map[string]bayesmodels.CompileState, error) {
	out := make(map[string]bayesmodels.CompileState, len(programSHA256s))
	none := StateOf(nil, false, 0)
	for _, p := range programSHA256s {
		out[p] = none
	}
	cur, err := c.Current(ctx)
	if err != nil || cur == nil {
		return out, err
	}
	programOf := map[string]string{}
	var keys []string
	for _, p := range programSHA256s {
		if k, err := CacheKey(p, cur.Image); err == nil && programOf[k] == "" {
			programOf[k] = p
			keys = append(keys, k)
		}
	}
	found, err := c.read(ctx, keys)
	if err != nil {
		return nil, err
	}
	for _, f := range found {
		out[programOf[f.row.CacheKey]] = StateOf(&f.row, f.personRunning, cur.Limits.Slots)
	}
	return out, nil
}

// Ready is the binary when the program's state is ready for the group's current image, nil and the state otherwise.
func (c *Compiler) Ready(ctx context.Context, programSHA256 string) (*Binary, bayesmodels.CompileState, error) {
	none := StateOf(nil, false, 0)
	cur, err := c.Current(ctx)
	if err != nil || cur == nil {
		return nil, none, err
	}
	key, err := CacheKey(programSHA256, cur.Image)
	if err != nil {
		return nil, none, err
	}
	found, err := c.read(ctx, []string{key})
	if err != nil || len(found) == 0 {
		return nil, none, err
	}
	row := found[0].row
	s := StateOf(&row, found[0].personRunning, cur.Limits.Slots)
	if row.State != StateReady {
		return nil, s, nil
	}
	return &Binary{CacheKey: row.CacheKey, BinarySHA256: row.BinarySHA256, Bytes: row.BinaryBytes}, s, nil
}

// found is a row read for a state, and whether its person has another compile running (the queued sentence).
type found struct {
	row           Row
	personRunning bool
}

// read is the rows of the keys, in one statement.
func (c *Compiler) read(ctx context.Context, keys []string) ([]found, error) {
	if len(keys) == 0 {
		return nil, nil
	}
	rs, err := c.DB.Query(ctx, `SELECT `+qualifiedColumns+`, EXISTS (SELECT 1 FROM bayes_binaries o
		WHERE o.state = 'running' AND o.requested_by IS NOT DISTINCT FROM b.requested_by AND o.id <> b.id)
		FROM bayes_binaries b WHERE b.cache_key = ANY($1)`, keys)
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	var out []found
	for rs.Next() {
		var f found
		if f.row, err = Scan(withExtra{rs, &f.personRunning}); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rs.Err()
}

// Need is the run's fallback (stan-compile, «The run's fallback»): the binary when it is ready; ErrRefused with the
// state when the run fails at once (Refused); otherwise the compile is asked for and the state to wait on returned.
func (c *Compiler) Need(ctx context.Context, r bayesmodels.CompileRequest) (*Binary, bayesmodels.CompileState, error) {
	b, s, err := c.Ready(ctx, r.ProgramSHA256)
	if err != nil || b != nil {
		return b, s, err
	}
	r.Again = false // only «Compile now» asks again for a program the compiler refused
	row, err := c.enqueue(ctx, r, true)
	if errors.Is(err, ErrRefused) {
		return nil, StateOf(row, false, 0), err
	}
	if err != nil {
		return nil, bayesmodels.CompileState{}, err
	}
	b, s, err = c.Ready(ctx, r.ProgramSHA256)
	return b, s, err
}

// Place copies the binary into the scripts bucket at key (a step's prefix, env-<…>/<step>/model), its row locked
// FOR SHARE meanwhile so that the cache never evicts it under the copy, then records the use. A stored object that is
// altered or gone forgets the row and its object (ErrAltered): the run compiles again.
func (c *Compiler) Place(ctx context.Context, b *Binary, key string) error {
	if b == nil || !stepBinaryKey.MatchString(key) {
		return fmt.Errorf("bayes compile: %q is not a step's binary key", key)
	}
	tx, err := c.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	row, err := Scan(tx.QueryRow(ctx, `SELECT `+Columns+` FROM bayes_binaries WHERE cache_key = $1 AND state = 'ready'
		AND binary_sha256 = $2 FOR SHARE`, b.CacheKey, b.BinarySHA256))
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrGone
	}
	if err != nil {
		return err
	}
	placed := c.Storage.Place(ctx, b.BinarySHA256, key)
	if err := tx.Commit(ctx); err != nil { // the share lock goes: the use and a forget lock the row alone
		return err
	}
	if errors.Is(placed, ErrAltered) {
		if err := c.forget(ctx, row); err != nil {
			return err
		}
		return ErrAltered
	}
	if placed != nil {
		return placed
	}
	return c.used(ctx, row.ID)
}

// used records a use of a ready row: its own short transaction, skipping a row another run is placing (its use is
// recorded when that copy ends), so that two runs of one binary never wait on each other.
func (c *Compiler) used(ctx context.Context, id int64) error {
	tx, err := c.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	row, err := Scan(tx.QueryRow(ctx, `SELECT `+Columns+` FROM bayes_binaries WHERE id = $1 AND state = 'ready'
		FOR UPDATE SKIP LOCKED`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if row, err = Used(row, c.now()); err != nil {
		return err
	}
	if err := Write(ctx, tx, row); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// forget deletes a ready row whose stored object is altered or gone (ready → none) and the object when no other ready
// row holds the same bytes, under the loop's lock as «Empty the cache» does, so that an object counted unheld here is
// never one a round is keeping. An engine that does not delete the object leaves it: a later keep of the same bytes
// hashes and replaces it.
func (c *Compiler) forget(ctx context.Context, row Row) error {
	tx, err := c.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, int64(LoopLock)); err != nil {
		return err
	}
	var held bool
	if err := tx.QueryRow(ctx, `WITH gone AS (DELETE FROM bayes_binaries WHERE id = $1 AND state = 'ready'
		AND binary_sha256 = $2 RETURNING id) SELECT EXISTS (SELECT 1 FROM bayes_binaries WHERE state = 'ready'
		AND binary_sha256 = $2 AND id NOT IN (SELECT id FROM gone))`, row.ID, row.BinarySHA256).Scan(&held); err != nil {
		return err
	}
	if !held {
		if err := c.Storage.Delete(ctx, []string{row.BinarySHA256}); err != nil && c.Log != nil {
			c.Log.Warn("bayes compile: an altered binary was not deleted from Tabularia's storage", "sha256",
				row.BinarySHA256, "error", err.Error())
		}
	}
	if c.Log != nil {
		c.Log.Error("bayes compile: a stored binary is not the one recorded: forgotten, the run compiles it again",
			"row", row.ID, "sha256", row.BinarySHA256)
	}
	return tx.Commit(ctx)
}

// ── reading rows ─────────────────────────────────────────────────────────────

func selectRows(ctx context.Context, q pgx.Tx, sql string, args ...any) ([]Row, error) {
	rs, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rs.Close()
	var out []Row
	for rs.Next() {
		row, err := Scan(rs)
		if err != nil {
			return nil, err
		}
		out = append(out, row)
	}
	return out, rs.Err()
}

// qualifiedColumns is Columns, each column of the table's alias b.
var qualifiedColumns = regexp.MustCompile(`[a-z_0-9]+`).ReplaceAllString(Columns, "b.$0")

// withExtra reads a row of Columns followed by one more column.
type withExtra struct {
	row   pgx.Row
	extra *bool
}

func (w withExtra) Scan(dest ...any) error { return w.row.Scan(append(dest, w.extra)...) }
