package api

import (
	"context"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/backups.py: the automatic backup of this database (services/db_backup.py). Read by
// administrators and observers; changed, started and downloaded by administrators, never
// with an API token. On Kubernetes the backups run (the orchestrators' pg_dump, the engine's
// listing): those answers stay Python's until the orchestrator is ported; with Docker
// Compose the section is greyed out (disabled_reason «kubernetes») and answered here.
func init() {
	register("backups", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/backups", Handler: adminRole(d, "/admin/backups", false, backupState(d))},
			{Method: http.MethodPut, Pattern: "/admin/backups", Handler: backupChange(d)},
			{Method: http.MethodPost, Pattern: "/admin/backups/run", Handler: adminRole(d, "/admin/backups/run", true, backupNow(d))},
			{Method: http.MethodGet, Pattern: "/admin/backups/{name}/download", Handler: adminRole(d, "/admin/backups/{name}/download", true, backupDownload(d))},
		}
	})
}

// noBackupFromToken is api_tokens.NO_BACKUP_FROM_TOKEN.
var noBackupFromToken = map[string]string{
	"en": "An API token cannot change, run or download the database backups: do it from the interface",
	"it": "Un token API non cambia, non avvia e non scarica i backup del database: si fa dall'interfaccia",
	"fr": "Un jeton d'API ne peut pas modifier, lancer ni télécharger les sauvegardes de la base : faites-le depuis l'interface",
	"de": "Ein API-Token kann die Datenbank-Backups nicht ändern, starten oder herunterladen: Das geht über die Oberfläche",
	"es": "Un token de API no puede cambiar, lanzar ni descargar las copias de seguridad de la base de datos: hazlo desde la interfaz",
}

// backupName is db_backup.NAME.
var backupName = regexp.MustCompile(`^tabularia-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.dump$`)

// backupAlive is db_backup.ALIVE: a running backup without a heartbeat for this long is dead.
const backupAlive = 120 * time.Second

type backupRow struct {
	Enabled                                              *bool
	Schedule                                             *string
	Keep                                                 *int64
	NextAt, RequestedAt, RunningSince, AliveAt, LastOKAt *time.Time
	LastErrorAt                                          *time.Time
	LastName, LastError                                  *string
	LastSize                                             *int64
	LastSeconds                                          *float64
}

// readBackupRow is db_backup.row: the row, made when it is not there yet (a GET writes it).
func readBackupRow(ctx context.Context, d *Deps) (backupRow, error) {
	var r backupRow
	read := func() error {
		return d.DB.QueryRow(ctx, `SELECT enabled, schedule, keep, next_at, requested_at, running_since, alive_at, last_ok_at,
			last_error_at, last_name, last_error, last_size, last_seconds FROM database_backups WHERE id = 1`).Scan(
			&r.Enabled, &r.Schedule, &r.Keep, &r.NextAt, &r.RequestedAt, &r.RunningSince, &r.AliveAt, &r.LastOKAt,
			&r.LastErrorAt, &r.LastName, &r.LastError, &r.LastSize, &r.LastSeconds)
	}
	err := read()
	if errors.Is(err, pgx.ErrNoRows) {
		if _, err = d.DB.Exec(ctx, `INSERT INTO database_backups (id) VALUES (1) ON CONFLICT (id) DO NOTHING`); err == nil {
			err = read()
		}
	}
	return r, err
}

type backupEffective struct {
	enabled  bool
	schedule string
	keep     int64
}

// effective is db_backup.effective: the administrators' choice, or the installation's.
func (d *Deps) backupEffective(r backupRow) (backupEffective, pyjson.Object, pyjson.Object) {
	c := d.Config.Admin
	e := backupEffective{c.BackupEnabled, c.BackupSchedule, int64(c.BackupKeep)}
	if r.Enabled != nil {
		e.enabled = *r.Enabled
	}
	if r.Schedule != nil && *r.Schedule != "" {
		e.schedule = *r.Schedule
	}
	if r.Keep != nil && *r.Keep != 0 {
		e.keep = *r.Keep
	}
	defaults := pyjson.Object{{Key: "enabled", Value: c.BackupEnabled}, {Key: "schedule", Value: c.BackupSchedule},
		{Key: "keep", Value: int64(c.BackupKeep)}}
	changed := pyjson.Object{{Key: "enabled", Value: r.Enabled != nil}, {Key: "schedule", Value: r.Schedule != nil},
		{Key: "keep", Value: r.Keep != nil}}
	return e, defaults, changed
}

type backupItem struct {
	Name     string  `json:"name"`
	Size     int64   `json:"size"`
	Modified *string `json:"modified"`
}

// BackupOut is routes/backups.py BackupOut.
type BackupOut struct {
	Available      bool          `json:"available"`
	DisabledReason *string       `json:"disabled_reason"`
	Enabled        bool          `json:"enabled"`
	Schedule       string        `json:"schedule"`
	Keep           int64         `json:"keep"`
	Timezone       string        `json:"timezone"`
	Defaults       pyjson.Object `json:"defaults"`
	Changed        pyjson.Object `json:"changed"`
	NextAt         *string       `json:"next_at"`
	RunningSince   *string       `json:"running_since"`
	RequestedAt    *string       `json:"requested_at"`
	LastOKAt       *string       `json:"last_ok_at"`
	LastName       *string       `json:"last_name"`
	LastSize       *int64        `json:"last_size"`
	LastSeconds    *float64      `json:"last_seconds"`
	LastError      *string       `json:"last_error"`
	LastErrorAt    *string       `json:"last_error_at"`
	Bucket         *string       `json:"bucket"`
	Prefix         *string       `json:"prefix"`
	SameAsData     *bool         `json:"same_as_data"`
	Items          []backupItem  `json:"items"`
	StorageError   *string       `json:"storage_error"`
	Editable       bool          `json:"editable"`
}

// backupOut is _out with Docker Compose's disabled_reason: no listing asked of the engine.
func backupOut(ctx context.Context, d *Deps, full bool) (BackupOut, error) {
	r, err := readBackupRow(ctx, d)
	if err != nil {
		return BackupOut{}, err
	}
	e, defaults, changed := d.backupEffective(r)
	reason := "kubernetes"
	out := BackupOut{
		DisabledReason: &reason, Enabled: e.enabled, Schedule: e.schedule, Keep: e.keep, Timezone: d.Panel.Timezone(),
		Defaults: defaults, Changed: changed, RequestedAt: isoPtr(r.RequestedAt), LastOKAt: isoPtr(r.LastOKAt),
		LastName: r.LastName, LastSize: r.LastSize, LastSeconds: r.LastSeconds, LastErrorAt: isoPtr(r.LastErrorAt),
		Items: []backupItem{}, Editable: full,
	}
	if e.enabled {
		out.NextAt = isoPtr(r.NextAt)
	}
	if r.RunningSince != nil { // db_backup.running
		alive := r.AliveAt
		if alive == nil {
			alive = r.RunningSince
		}
		if !alive.Before(nowNaive().Add(-backupAlive)) {
			out.RunningSince = isoPtr(r.RunningSince)
		}
	}
	if full {
		out.LastError = r.LastError
	}
	return out, nil
}

// onKubernetes answers false here: on a cluster the request goes to Python.
func backupsOnKubernetes(d *Deps, w http.ResponseWriter, r *http.Request, raw []byte) bool {
	if d.Config.Admin.OnKubernetes {
		forwardToPython(d, w, r, raw)
		return true
	}
	return false
}

// backupState is GET /admin/backups.
func backupState(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if backupsOnKubernetes(d, w, r, nil) {
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		out, err := backupOut(r.Context(), d, full)
		if err != nil {
			adminFail(d, w, "reading the backups", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// backupChange is PUT /admin/backups: only what changes; reset=[…] puts those back to the
// installation's value.
func backupChange(d *Deps) http.Handler {
	return adminBody(d, "/admin/backups", func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		if denyFromToken(w, r, p, noBackupFromToken) {
			return
		}
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.BackupUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		if backupsOnKubernetes(d, w, r, raw) {
			return
		}
		ctx := r.Context()
		if _, err := readBackupRow(ctx, d); err != nil {
			adminFail(d, w, "reading the backups", err)
			return
		}
		changes := pyjson.Object{}
		for _, n := range b.Get("reset").([]any) {
			name := n.(string)
			if name != "enabled" && name != "schedule" && name != "keep" {
				adminDetail(w, http.StatusUnprocessableEntity, "Not a backup setting: "+name)
				return
			}
			changes = changes.Set(name, nil)
		}
		if v, ok := b.Get("enabled").(bool); ok {
			changes = changes.Set("enabled", v)
		}
		if v, ok := b.Get("schedule").(string); ok {
			spec, err := cron.ValidateSchedule(v)
			if err != nil {
				adminDetail(w, http.StatusUnprocessableEntity, err.Error())
				return
			}
			changes = changes.Set("schedule", spec)
		}
		if v, ok := b.Get("keep").(int64); ok {
			changes = changes.Set("keep", v)
		}
		if len(changes) > 0 {
			if err := saveBackupChanges(ctx, d, changes, p.User.Email); err != nil {
				adminFail(d, w, "saving the backups' settings", err)
				return
			}
			tt := "database_backup"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "backup.update", TargetType: &tt, Detail: changes})
		}
		out, err := backupOut(ctx, d, true)
		if err != nil {
			adminFail(d, w, "reading the backups", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	})
}

// saveBackupChanges writes the changes, then replan: the next backup from the schedule now
// in force.
func saveBackupChanges(ctx context.Context, d *Deps, changes pyjson.Object, by string) error {
	set := "updated_at = $1, updated_by = $2"
	args := []any{nowNaive(), by}
	for _, kv := range changes {
		args = append(args, kv.Value)
		set += ", " + kv.Key + " = $" + strconv.Itoa(len(args))
	}
	if _, err := d.DB.Exec(ctx, `UPDATE database_backups SET `+set+` WHERE id = 1`, args...); err != nil {
		return err
	}
	r, err := readBackupRow(ctx, d)
	if err != nil {
		return err
	}
	e, _, _ := d.backupEffective(r)
	var next *time.Time
	var forSchedule *string
	if e.enabled {
		n, err := cron.NextFire(e.schedule, nowNaive(), d.Panel.Timezone())
		if err != nil {
			return err
		}
		n = naiveTime(n)
		next, forSchedule = &n, &e.schedule
	}
	_, err = d.DB.Exec(ctx, `UPDATE database_backups SET next_at = $1, next_for = $2 WHERE id = 1`, next, forSchedule)
	return err
}

// backupNow is POST /admin/backups/run: «Back up now»; never with Docker Compose.
func backupNow(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if denyFromToken(w, r, p, noBackupFromToken) {
			return
		}
		if backupsOnKubernetes(d, w, r, nil) {
			return
		}
		adminDetail(w, http.StatusConflict, "Backups are not available on this installation")
	}
}

// backupDownload is GET /admin/backups/{name}/download: the file streamed from the engine,
// and the download in the audit.
func backupDownload(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if denyFromToken(w, r, p, noBackupFromToken) {
			return
		}
		name := r.PathValue("name")
		if !backupName.MatchString(name) {
			adminDetail(w, http.StatusNotFound, "No such backup")
			return
		}
		ctx := r.Context()
		req, err := d.Engine.Request(ctx, http.MethodGet, "/backups/objects/"+name, nil)
		if err != nil {
			adminFail(d, w, "asking the engine", err)
			return
		}
		res, err := d.Engine.Do(req)
		if err != nil { // main.py _service_down
			adminDetail(w, http.StatusServiceUnavailable, "The engine is not answering: try again in a moment")
			return
		}
		defer res.Body.Close()
		if res.StatusCode >= 400 {
			_, _ = io.Copy(io.Discard, res.Body)
			if res.StatusCode == http.StatusNotFound {
				adminDetail(w, http.StatusNotFound, "No such backup")
			} else {
				adminDetail(w, http.StatusBadGateway, "The backup could not be read")
			}
			return
		}
		tt := "database_backup"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "backup.download", TargetType: &tt, TargetLabel: &name})
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`"`)
		if cl := res.Header.Get("Content-Length"); cl != "" {
			w.Header().Set("Content-Length", cl)
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		buf := make([]byte, 64*1024)
		for {
			n, err := res.Body.Read(buf)
			if n > 0 {
				if _, werr := w.Write(buf[:n]); werr != nil {
					return
				}
				if flusher != nil {
					flusher.Flush()
				}
			}
			if err != nil {
				return
			}
		}
	}
}
