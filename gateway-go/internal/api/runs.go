package api

import (
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/runs.py: the run history.

func init() {
	register("runs", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/runs", Handler: server.Authed(d.Auth, "/runs", d.searchRuns)},
			{Method: http.MethodGet, Pattern: "/runs/activity", Handler: server.Authed(d.Auth, "/runs/activity", d.runsActivity)},
		}
	})
}

// RunOut is schemas/models.py RunOut, its fields in order.
type RunOut struct {
	ID            int64        `json:"id"`
	Kind          string       `json:"kind"`
	FlowID        *int64       `json:"flow_id"`
	FlowGroupID   *int64       `json:"flow_group_id"`
	SavedScriptID *int64       `json:"saved_script_id"`
	SQLScriptID   *int64       `json:"sql_script_id"`
	Status        string       `json:"status"`
	LaunchedBy    *int64       `json:"launched_by"`
	TriggerType   string       `json:"trigger_type"`
	Engine        *string      `json:"engine"`
	OutputKey     string       `json:"output_key"`
	RowsWritten   *int64       `json:"rows_written"`
	Error         *string      `json:"error"`
	ErrorDetail   *string      `json:"error_detail"`
	PublishName   *string      `json:"publish_name"`
	DatasourceID  *int64       `json:"datasource_id"`
	Destination   *string      `json:"destination"`
	Mirror        *string      `json:"mirror"`
	Email         *string      `json:"email"`
	Notes         *string      `json:"notes"`
	Script        *string      `json:"script"`
	ParentRunID   *int64       `json:"parent_run_id"`
	StartedAt     *string      `json:"started_at"`
	FinishedAt    *string      `json:"finished_at"`
	Progress      *runProgress `json:"progress"`
}

// runProgress is RunProgress.
type runProgress struct {
	Phase   string `json:"phase"`
	Seconds int64  `json:"seconds"`
	Rows    *int64 `json:"rows"`
	File    *int64 `json:"file"`
	Files   *int64 `json:"files"`
	Call    *int64 `json:"call"`
	Calls   *int64 `json:"calls"`
	Pages   *int64 `json:"pages"`
}

// RunSearchOut is RunOut with the names the search resolves.
type RunSearchOut struct {
	RunOut
	FlowName       *string `json:"flow_name"`
	SourceName     *string `json:"source_name"`
	GroupName      *string `json:"group_name"`
	ScriptName     *string `json:"script_name"`
	SQLScriptName  *string `json:"sql_script_name"`
	LaunchedByName *string `json:"launched_by_name"`
}

// runRow is the `runs` row (models/run.py).
type runRow struct {
	ID                                              int64
	Kind                                            string
	FlowID, FlowGroupID, SavedScriptID, SQLScriptID *int64
	GroupCells                                      *string
	TaskID, Status                                  string
	LaunchedBy                                      *int64
	TriggerType                                     string
	Engine                                          *string
	ParentRunID                                     *int64
	InputKey, OutputBucket, OutputKey               string
	RowsWritten                                     *int64
	Error, ErrorDetail, PublishName                 *string
	PublishProjectID                                *int64
	PublishDescription                              string
	PublishOverwrite                                bool
	PublishSortKeys                                 string
	DatasourceID                                    *int64
	Destination, Mirror, Email, Notes, Script       *string
	EngineMode, ClaimedBy                           *string
	HeartbeatAt, EngineStartedAt, FinishedAt        *time.Time
	StartedAt                                       time.Time
}

const runColumns = `r.id, r.kind, r.flow_id, r.flow_group_id, r.saved_script_id, r.sql_script_id, r.group_cells, r.task_id, r.status,
	r.launched_by, r.trigger_type, r.engine, r.parent_run_id, r.input_key, r.output_bucket, r.output_key, r.rows_written, r.error,
	r.error_detail, r.publish_name, r.publish_project_id, r.publish_description, r.publish_overwrite, r.publish_sort_keys,
	r.datasource_id, r.destination, r.mirror, r.email, r.notes, r.script, r.engine_mode, r.claimed_by, r.heartbeat_at,
	r.engine_started_at, r.finished_at, r.started_at`

func (x *runRow) dest() []any {
	return []any{&x.ID, &x.Kind, &x.FlowID, &x.FlowGroupID, &x.SavedScriptID, &x.SQLScriptID, &x.GroupCells, &x.TaskID, &x.Status,
		&x.LaunchedBy, &x.TriggerType, &x.Engine, &x.ParentRunID, &x.InputKey, &x.OutputBucket, &x.OutputKey, &x.RowsWritten,
		&x.Error, &x.ErrorDetail, &x.PublishName, &x.PublishProjectID, &x.PublishDescription, &x.PublishOverwrite,
		&x.PublishSortKeys, &x.DatasourceID, &x.Destination, &x.Mirror, &x.Email, &x.Notes, &x.Script, &x.EngineMode,
		&x.ClaimedBy, &x.HeartbeatAt, &x.EngineStartedAt, &x.FinishedAt, &x.StartedAt}
}

func (x *runRow) out() RunOut {
	return RunOut{ID: x.ID, Kind: x.Kind, FlowID: x.FlowID, FlowGroupID: x.FlowGroupID, SavedScriptID: x.SavedScriptID,
		SQLScriptID: x.SQLScriptID, Status: x.Status, LaunchedBy: x.LaunchedBy, TriggerType: x.TriggerType, Engine: x.Engine,
		OutputKey: x.OutputKey, RowsWritten: x.RowsWritten, Error: x.Error, ErrorDetail: x.ErrorDetail, PublishName: x.PublishName,
		DatasourceID: x.DatasourceID, Destination: x.Destination, Mirror: x.Mirror, Email: x.Email, Notes: x.Notes,
		Script: x.Script, ParentRunID: x.ParentRunID, StartedAt: iso(x.StartedAt), FinishedAt: isoPtr(x.FinishedAt)}
}

const runJoins = ` LEFT JOIN flows f ON r.flow_id = f.id LEFT JOIN datasources ds ON r.datasource_id = ds.id
	LEFT JOIN flow_groups g ON r.flow_group_id = g.id LEFT JOIN saved_scripts ss ON r.saved_script_id = ss.id
	LEFT JOIN sql_scripts sq ON r.sql_script_id = sq.id`

// runsVisibility is the condition search_runs and runs_activity add for whoever is not an
// administrator: the runs of the readable folders, or launched by the person.
func (q *request) runsVisibility(args *[]any) (string, error) {
	admin, err := q.c.IsAdmin(q.ctx)
	if err != nil || admin {
		return "", err
	}
	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		return "", err
	}
	envs, err := q.acc.UsableEnvironments(q.ctx)
	if err != nil {
		return "", err
	}
	envIDs := perm.Sorted(envs)
	if len(envIDs) == 0 {
		envIDs = []int64{-1}
	}
	*args = append(*args, perm.Sorted(readable), envIDs, q.p.User.ID)
	n := len(*args)
	r, e, u := n-2, n-1, n
	return fmt.Sprintf(` AND (f.project_id = ANY($%d) OR ds.project_id = ANY($%d) OR g.project_id = ANY($%d)
		OR (ss.project_id = ANY($%d) AND ss.environment_id = ANY($%d)) OR sq.project_id = ANY($%d) OR r.launched_by = $%d)`,
		r, r, r, r, e, r, u), nil
}

// searchRuns is GET /runs (search_runs): the visible runs, paginated, filtered by state and
// searched over all of them. It does not reconcile: a search has no effects.
func (d *Deps) searchRuns(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	qs := pyd.ParseQuery(r.URL.RawQuery)
	status := ps.Query(qs, "status", pyd.Optional(pyd.Str()), nil)
	text := ps.Query(qs, "q", pyd.Optional(pyd.Str()), nil)
	limit := ps.Query(qs, "limit", pyd.Int().Ge(1).Le(200), int64(50))
	offset := ps.Query(qs, "offset", pyd.Int().Ge(0), int64(0))
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	var args []any
	where := ` WHERE true`
	vis, err := q.runsVisibility(&args)
	if err != nil {
		q.fail(err)
		return
	}
	where += vis
	if s, _ := status.(string); s != "" {
		args = append(args, s)
		where += fmt.Sprintf(` AND r.status = $%d`, len(args))
	}
	if s, _ := text.(string); s != "" {
		args = append(args, "%"+s+"%")
		n := len(args)
		where += fmt.Sprintf(` AND (r.error ILIKE $%[1]d OR r.error_detail ILIKE $%[1]d OR f.name ILIKE $%[1]d OR ds.name ILIKE $%[1]d
			OR g.name ILIKE $%[1]d OR ss.name ILIKE $%[1]d OR sq.name ILIKE $%[1]d OR u.full_name ILIKE $%[1]d OR u.email ILIKE $%[1]d)`, n)
	}
	from := ` FROM runs r` + runJoins + ` LEFT JOIN users u ON r.launched_by = u.id`
	var total int64
	if err := d.DB.QueryRow(q.ctx, `SELECT count(*)`+from+where, args...).Scan(&total); err != nil {
		q.fail(err)
		return
	}
	rows, err := d.DB.Query(q.ctx, `SELECT `+runColumns+`, f.name, ds.name, g.name, ss.name, sq.name,
		CASE WHEN u.id IS NULL THEN NULL ELSE coalesce(nullif(u.full_name, ''), u.email) END`+from+where+
		fmt.Sprintf(` ORDER BY r.started_at DESC LIMIT %d OFFSET %d`, limit.(int64), offset.(int64)), args...)
	if err != nil {
		q.fail(err)
		return
	}
	items := []RunSearchOut{}
	for rows.Next() {
		var x runRow
		var it RunSearchOut
		if err := rows.Scan(append(x.dest(), &it.FlowName, &it.SourceName, &it.GroupName, &it.ScriptName, &it.SQLScriptName,
			&it.LaunchedByName)...); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		it.RunOut = x.out()
		items = append(items, it)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		q.fail(err)
		return
	}
	server.JSON(w, http.StatusOK, Page[RunSearchOut]{Items: items, Total: total})
}

// strptime's %Y-%m-%d: four digits, a month and a day of one or two (a day may be « 5»).
var ymd = regexp.MustCompile(`^(\d{4})-(1[0-2]|0[1-9]|[1-9])-(3[01]|[12]\d|0[1-9]|[1-9]| [1-9])$`)

func parseDay(s string) (time.Time, bool) {
	m := ymd.FindStringSubmatch(s)
	if m == nil {
		return time.Time{}, false
	}
	y, _ := strconv.Atoi(m[1])
	mo, _ := strconv.Atoi(m[2])
	da, _ := strconv.Atoi(strings.TrimSpace(m[3]))
	t := time.Date(y, time.Month(mo), da, 0, 0, 0, 0, time.UTC)
	if t.Day() != da || y < 1 {
		return time.Time{}, false
	}
	return t, true
}

type activityBucket struct {
	Key       string `json:"key"`
	Total     int64  `json:"total"`
	Success   int64  `json:"success"`
	Failure   int64  `json:"failure"`
	Scheduled int64  `json:"scheduled"`
	Manual    int64  `json:"manual"`
}

// runsActivity is GET /runs/activity: the runs counted by local day, or by hour of a day.
func (d *Deps) runsActivity(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	qs := pyd.ParseQuery(r.URL.RawQuery)
	daysV := ps.Query(qs, "days", pyd.Int().Ge(1).Le(730), int64(180))
	dayV := ps.Query(qs, "day", pyd.Optional(pyd.Str()), nil)
	offV := ps.Query(qs, "tz_offset", pyd.Int(), int64(0))
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	q := d.req(w, r, p)
	offMin, ok := offV.(int64)
	if !ok || offMin > 1e9 || offMin < -1e9 { // timedelta(minutes=…) overflows: Python's 500
		server.InternalError(w)
		return
	}
	off := time.Duration(offMin) * time.Minute
	nowLocal := time.Now().UTC().Add(-off)
	var start, end time.Time
	granularity := "day"
	if day, _ := dayV.(string); day != "" {
		d0, ok := parseDay(day)
		if !ok {
			q.refuse(422, "Invalid date (expected YYYY-MM-DD)")
			return
		}
		start, end, granularity = d0, d0.AddDate(0, 0, 1), "hour"
	} else {
		today := time.Date(nowLocal.Year(), nowLocal.Month(), nowLocal.Day(), 0, 0, 0, 0, time.UTC)
		end = today.AddDate(0, 0, 1)
		start = end.AddDate(0, 0, -int(daysV.(int64)))
	}
	args := []any{start.Add(off), end.Add(off)}
	vis, err := q.runsVisibility(&args)
	if err != nil {
		q.fail(err)
		return
	}
	format := "YYYY-MM-DD"
	if granularity == "hour" {
		format = "HH24"
	}
	cell := fmt.Sprintf(`to_char(r.started_at - INTERVAL '%d seconds', '%s')`, int64(off/time.Second), format)
	rows, err := d.DB.Query(q.ctx, `SELECT `+cell+` AS cell, count(*), sum(CASE WHEN r.status = 'SUCCESS' THEN 1 ELSE 0 END),
		sum(CASE WHEN r.status = 'FAILURE' THEN 1 ELSE 0 END), sum(CASE WHEN r.trigger_type = 'schedule' THEN 1 ELSE 0 END)
		FROM runs r`+runJoins+` WHERE r.started_at >= $1 AND r.started_at < $2 AND r.parent_run_id IS NULL`+vis+` GROUP BY cell`, args...)
	if err != nil {
		q.fail(err)
		return
	}
	buckets := []activityBucket{}
	for rows.Next() {
		var b activityBucket
		var s, f, sc *int64
		if err := rows.Scan(&b.Key, &b.Total, &s, &f, &sc); err != nil {
			rows.Close()
			q.fail(err)
			return
		}
		for _, x := range []struct {
			to   *int64
			from *int64
		}{{&b.Success, s}, {&b.Failure, f}, {&b.Scheduled, sc}} {
			if x.from != nil {
				*x.to = *x.from
			}
		}
		b.Manual = b.Total - b.Scheduled
		buckets = append(buckets, b)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		q.fail(err)
		return
	}
	sort.Slice(buckets, func(i, j int) bool { return buckets[i].Key < buckets[j].Key })
	from, to := "00", "23"
	if granularity == "day" {
		from, to = start.Format("2006-01-02"), end.AddDate(0, 0, -1).Format("2006-01-02")
	}
	server.JSON(w, http.StatusOK, struct {
		Granularity string           `json:"granularity"`
		FromKey     string           `json:"from_key"`
		ToKey       string           `json:"to_key"`
		Buckets     []activityBucket `json:"buckets"`
	}{granularity, from, to, buckets})
}

var _ = pgx.ErrNoRows
