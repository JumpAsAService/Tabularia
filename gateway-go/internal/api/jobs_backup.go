package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// services/db_backup.py backup_loop: on its schedule (or when «Back up now» asked) one
// orchestrator takes the backup — the row's conditional UPDATE — runs pg_dump of this
// database and streams it to the engine, which stores it; then the oldest beyond the number
// to keep go. The one at work beats every 30 s; a claim without a beat for 120 s is dead.

const (
	backupTick      = 15 * time.Second // TICK_SECONDS
	backupHeartbeat = 30 * time.Second // HEARTBEAT
	backupChunk     = 1024 * 1024      // CHUNK
	scheduleActor   = "backup schedule"
	pgVersionsDir   = "/usr/lib/postgresql" // VERSIONS_DIR
)

func init() {
	RegisterJob("backup_loop", func(d *Deps) jobs.Job {
		return jobs.Job{Order: 5, Loop: func(ctx context.Context) {
			if reason := backupDisabledReason(d); reason != "" {
				d.Log.Info("backup: not available here", "reason", reason)
				return
			}
			for ctx.Err() == nil {
				if _, err := backupRound(ctx, d); err != nil {
					d.Log.Error("backup: a round failed", "err", err)
				}
				if !jobs.Sleep(ctx, backupTick) {
					return
				}
			}
		}, Ticks: map[string]jobs.Tick{
			"backup": func(ctx context.Context) (any, error) {
				out, err := backupRound(ctx, d)
				if out == nil {
					return nil, err
				}
				return out, err
			},
		}}
	})
}

// backupDisabledReason is disabled_reason: "kubernetes" off it, "pg_dump" when the program is
// not in the image (the database is PostgreSQL).
func backupDisabledReason(d *Deps) string {
	if !d.Config.Admin.OnKubernetes {
		return "kubernetes"
	}
	if _, err := exec.LookPath(d.Config.Jobs.BackupPgDump); err != nil {
		return "pg_dump"
	}
	return ""
}

// backupRound is one_round: the outcome of a backup made, nil when none was due.
func backupRound(ctx context.Context, d *Deps) (pyjson.Object, error) {
	if backupDisabledReason(d) != "" {
		return nil, nil
	}
	keep, who, err := backupClaim(ctx, d)
	if err != nil || keep == 0 {
		return nil, err
	}
	return backupMake(ctx, d, keep, who)
}

func backupNext(d *Deps, schedule string, after time.Time) (time.Time, error) {
	return cron.NextFire(schedule, after, d.Panel.Timezone())
}

// backupClaim is claim: the next backup planned, and taken when due. keep 0: not won.
func backupClaim(ctx context.Context, d *Deps) (int64, *string, error) {
	at := jobs.Now()
	r, err := readBackupRow(ctx, d)
	if err != nil {
		return 0, nil, err
	}
	var nextFor *string
	if err := d.DB.QueryRow(ctx, `SELECT next_for FROM database_backups WHERE id = 1`).Scan(&nextFor); err != nil {
		return 0, nil, err
	}
	eff, _, _ := d.backupEffective(r)
	if eff.enabled && (r.NextAt == nil || nextFor == nil || *nextFor != eff.schedule) {
		nxt, err := backupNext(d, eff.schedule, at)
		if err != nil {
			return 0, nil, err
		}
		if _, err := d.DB.Exec(ctx, `UPDATE database_backups SET next_at = $1, next_for = $2 WHERE id = 1`, nxt, eff.schedule); err != nil {
			return 0, nil, err
		}
		r.NextAt = &nxt
	} else if !eff.enabled && r.NextAt != nil {
		if _, err := d.DB.Exec(ctx, `UPDATE database_backups SET next_at = NULL, next_for = NULL WHERE id = 1`); err != nil {
			return 0, nil, err
		}
		r.NextAt = nil
	}
	// the request and its author as they are now (refresh after the commit)
	var requestedBy *string
	if err := d.DB.QueryRow(ctx, `SELECT requested_at, requested_by FROM database_backups WHERE id = 1`).Scan(&r.RequestedAt,
		&requestedBy); err != nil {
		return 0, nil, err
	}
	due := r.RequestedAt != nil || (eff.enabled && r.NextAt != nil && !r.NextAt.After(at))
	if !due {
		return 0, nil, nil
	}
	var who *string
	if r.RequestedAt != nil {
		who = requestedBy
	}
	var next any
	var nextSchedule any
	if eff.enabled {
		n, err := backupNext(d, eff.schedule, at)
		if err != nil {
			return 0, nil, err
		}
		next, nextSchedule = n, eff.schedule
	}
	tag, err := d.DB.Exec(ctx, `UPDATE database_backups SET running_since = $1, alive_at = $1, claimed_by = $2,
		requested_at = NULL, requested_by = NULL, next_at = $3, next_for = $4
		WHERE id = 1 AND (running_since IS NULL OR coalesce(alive_at, running_since) < $5)
		AND (requested_at IS NOT NULL OR next_at <= $1)`, at, jobs.Instance, next, nextSchedule, at.Add(-backupAlive))
	if err != nil || tag.RowsAffected() != 1 {
		return 0, nil, err
	}
	return eff.keep, who, nil
}

// errBackupFailed is BackupFailed: a backup that did not make it, with the reason to show.
type errBackupFailed struct{ reason string }

func (e *errBackupFailed) Error() string { return e.reason }

// backupMake is make: dump, store, rotate; the outcome on the row and in the audit log.
func backupMake(ctx context.Context, d *Deps, keep int64, who *string) (pyjson.Object, error) {
	started := time.Now()
	name := jobs.Now().Format("tabularia-2006-01-02T15-04-05Z.dump")
	beatCtx, stopBeat := context.WithCancel(ctx)
	go func() {
		for jobs.Sleep(beatCtx, backupHeartbeat) {
			if _, err := d.DB.Exec(beatCtx, `UPDATE database_backups SET alive_at = $1 WHERE id = 1 AND claimed_by = $2`,
				jobs.Now(), jobs.Instance); err != nil {
				d.Log.Info("backup: heartbeat not written", "err", err)
			}
		}
	}()
	size, err := backupUpload(ctx, d, name)
	var removed int
	if err == nil {
		removed, err = backupRotate(ctx, d, keep)
	}
	stopBeat()
	actor := scheduleActor
	if who != nil && *who != "" {
		actor = *who
	}
	tt := "database_backup"
	if ctx.Err() != nil {
		// the orchestrator stopping: said, not lost in silence
		reason := "Stopped before the end: the process running it stopped (a restart or an upgrade). Run it now"
		bg := context.Background()
		backupClose(bg, d, `last_error = $1, last_error_at = $2`, reason, jobs.Now())
		at := jobs.Now()
		audit.Record(bg, d.DB, d.Log, audit.Event{ActorLabel: actor, Action: "backup.run", Outcome: "failure", TargetType: &tt,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "name", Value: name}, {Key: "error", Value: reason}}, At: &at})
		return nil, ctx.Err()
	}
	if err != nil {
		var bf *errBackupFailed
		reason := err.Error()
		if !errors.As(err, &bf) {
			reason = "RuntimeError: " + runeClip(reason, 500)
		}
		d.Log.Warn("backup: failed", "reason", reason)
		backupClose(ctx, d, `last_error = $1, last_error_at = $2`, runeClip(reason, 2000), jobs.Now())
		at := jobs.Now()
		audit.Record(ctx, d.DB, d.Log, audit.Event{ActorLabel: actor, Action: "backup.run", Outcome: "failure", TargetType: &tt,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "name", Value: name}, {Key: "error", Value: runeClip(reason, 500)}}, At: &at})
		return pyjson.Object{{Key: "ok", Value: false}, {Key: "error", Value: reason}}, nil
	}
	// round(time.monotonic() - started, 1)
	seconds, _ := strconv.ParseFloat(strconv.FormatFloat(time.Since(started).Seconds(), 'f', 1, 64), 64)
	d.Log.Info("backup: stored", "name", name, "bytes", size, "seconds", seconds, "removed", removed)
	backupClose(ctx, d, `last_ok_at = $1, last_name = $2, last_size = $3, last_seconds = $4`, jobs.Now(), name, size, seconds)
	at := jobs.Now()
	audit.Record(ctx, d.DB, d.Log, audit.Event{ActorLabel: actor, Action: "backup.run", Outcome: "success", TargetType: &tt,
		TargetLabel: &name, Detail: pyjson.Object{{Key: "name", Value: name}, {Key: "size", Value: size}, {Key: "removed", Value: removed}},
		At: &at})
	return pyjson.Object{{Key: "ok", Value: true}, {Key: "name", Value: name}, {Key: "size", Value: size},
		{Key: "removed", Value: removed}}, nil
}

// backupClose is _close: the claim let go, with the outcome's columns.
func backupClose(ctx context.Context, d *Deps, sets string, args ...any) {
	if _, err := d.DB.Exec(ctx, `UPDATE database_backups SET running_since = NULL, alive_at = NULL, claimed_by = NULL, `+
		sets+` WHERE id = 1`, args...); err != nil {
		d.Log.Error("backup: the outcome not recorded", "err", err)
	}
}

func runeClip(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// pgDumpFor is pg_dump_for: the server's own version's pg_dump when the image has it.
func pgDumpFor(d *Deps, major int) string {
	if major > 0 {
		own := filepath.Join(pgVersionsDir, strconv.Itoa(major), "bin", "pg_dump")
		if info, err := os.Stat(own); err == nil && info.Mode()&0o111 != 0 {
			return own
		}
	}
	return d.Config.Jobs.BackupPgDump
}

// pgDumpCommand is _pg_env_and_args: the password in the environment, never on the line.
func pgDumpCommand(ctx context.Context, d *Deps) (*exec.Cmd, error) {
	major := 0
	var v string
	if err := d.DB.QueryRow(ctx, `SHOW server_version_num`).Scan(&v); err == nil {
		if n, err := strconv.Atoi(v); err == nil {
			major = n / 10000
		}
	}
	u, err := url.Parse(d.Config.DSN)
	if err != nil {
		return nil, err
	}
	password, _ := u.User.Password()
	env := append(os.Environ(), "PGPASSWORD="+password, "PGCONNECT_TIMEOUT=10")
	for key, values := range u.Query() {
		if strings.HasPrefix(key, "ssl") && len(values) > 0 {
			env = append(env, "PG"+strings.ToUpper(key)+"="+values[len(values)-1])
		}
	}
	host, port := u.Hostname(), u.Port()
	if host == "" {
		host = "localhost"
	}
	if port == "" {
		port = "5432"
	}
	cmd := exec.CommandContext(ctx, pgDumpFor(d, major), "--format=custom", "--host", host, "--port", port,
		"--username", u.User.Username(), strings.TrimPrefix(u.Path, "/"))
	cmd.Env = env
	return cmd, nil
}

// dumpReader is _pg_dump: what pg_dump writes, as it writes it; a failure is the error of the
// last read, so that the upload it feeds breaks instead of completing.
type dumpReader struct {
	cmd    *exec.Cmd
	out    io.Reader
	stderr *bytes.Buffer
	size   int64
	done   bool
}

func (r *dumpReader) Read(p []byte) (int, error) {
	if r.done {
		return 0, io.EOF
	}
	if len(p) > backupChunk {
		p = p[:backupChunk]
	}
	n, err := r.out.Read(p)
	r.size += int64(n)
	if err == io.EOF {
		r.done = true
		if werr := r.cmd.Wait(); werr != nil {
			msg := strings.TrimSpace(strings.ToValidUTF8(r.stderr.String(), "�"))
			if rs := []rune(msg); len(rs) > 800 {
				msg = string(rs[len(rs)-800:])
			}
			return n, &errBackupFailed{"pg_dump failed: " + msg}
		}
	}
	return n, err
}

// httpxName is type(e).__name__ of the httpx exception a transport failure raises.
func httpxName(err error) string {
	var ne net.Error
	var op *net.OpError
	switch {
	case errors.As(err, &op) && op.Op == "dial":
		if op.Timeout() {
			return "ConnectTimeout"
		}
		return "ConnectError"
	case errors.As(err, &ne) && ne.Timeout():
		return "ReadTimeout"
	case errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF):
		return "RemoteProtocolError"
	}
	return "ConnectError"
}

// backupEngineError is _engine_error: the answer's detail, or its text.
func backupEngineError(raw []byte) string {
	text := strings.ToValidUTF8(string(raw), "�")
	if v, err := pyd.LoadsStr(string(raw)); err == nil {
		if o, ok := v.(*pyd.Object); ok {
			if det, _ := o.Get("detail"); pyd.Truthy(det) {
				return runeClip(pyjson.Str(det), 800)
			}
		}
	}
	return runeClip(text, 800)
}

// backupUpload is _upload: the dump streamed to the engine; its size.
func backupUpload(ctx context.Context, d *Deps, name string) (int64, error) {
	limit := d.Panel.BackupTimeout()
	cctx, cancel := context.WithTimeout(ctx, time.Duration(limit)*time.Second)
	defer cancel()
	cmd, err := pgDumpCommand(cctx, d)
	if err != nil {
		return 0, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.StdoutPipe()
	if err != nil {
		return 0, err
	}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	dump := &dumpReader{cmd: cmd, out: out, stderr: &stderr}
	defer func() {
		if !dump.done {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
		}
	}()
	req, err := d.Engine.Request(cctx, http.MethodPut, "/backups/objects/"+name, dump)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	res, err := d.Engine.Do(req)
	if err != nil {
		var bf *errBackupFailed
		switch {
		case errors.As(err, &bf):
			return 0, bf
		case cctx.Err() == context.DeadlineExceeded && ctx.Err() == nil:
			return 0, &errBackupFailed{fmt.Sprintf("the backup did not end within %d s (BACKUP__TIMEOUT_SECONDS)", limit)}
		}
		return 0, &errBackupFailed{fmt.Sprintf("the engine could not be reached to store the backup (%s)", httpxName(err))}
	}
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode >= 400 {
		return 0, &errBackupFailed{backupEngineError(raw)}
	}
	return dump.size, nil
}

// backupRotate is _rotate: the oldest beyond `keep` removed, as the engine lists them.
func backupRotate(ctx context.Context, d *Deps, keep int64) (int, error) {
	cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	res, raw, err := d.Engine.JSON(cctx, http.MethodGet, "/backups", nil)
	listErr := ""
	switch {
	case err != nil:
		listErr = fmt.Sprintf("The engine could not be reached (%s)", httpxName(err))
	case res.StatusCode >= 400:
		listErr = backupEngineError(raw)
	}
	if listErr != "" {
		return 0, &errBackupFailed{"the backup was stored, but the old ones could not be listed: " + listErr}
	}
	v, err := pyd.LoadsStr(string(raw))
	if err != nil {
		return 0, err
	}
	found, ok := v.(*pyd.Object)
	if !ok {
		return 0, errors.New("the listing is not a dict")
	}
	if e, _ := found.Get("error"); pyd.Truthy(e) {
		return 0, &errBackupFailed{"the backup was stored, but the old ones could not be listed: " + pyjson.Str(e)}
	}
	items, _ := found.Get("items")
	var names []string
	if list, ok := items.([]any); ok {
		for _, it := range list {
			o, ok := it.(*pyd.Object)
			if !ok {
				return 0, errors.New("an item is not a dict")
			}
			n, _ := o.Get("name")
			s, _ := n.(string)
			if backupName.MatchString(s) {
				names = append(names, s)
			}
		}
	}
	if int64(len(names)) <= keep {
		return 0, nil
	}
	old := names[keep:]
	for _, n := range old {
		res, raw, err := d.Engine.JSON(cctx, http.MethodDelete, "/backups/objects/"+n, nil)
		if err != nil {
			return 0, fmt.Errorf("%s", httpxName(err))
		}
		if res.StatusCode >= 400 {
			return 0, &errBackupFailed{fmt.Sprintf("the backup was stored, but %s could not be removed: %s", n, backupEngineError(raw))}
		}
	}
	return len(old), nil
}
