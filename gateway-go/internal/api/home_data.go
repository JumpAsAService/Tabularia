package api

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// What the home page's widgets read that no other page serves (docs/specs/home.md): the
// objects a person pinned, the schedules due next, the last runs of their flows. Routes of
// the Go gateway only (server/go_only_routes.go).
//
// Every answer is filtered by what the reader may see NOW, in the query: the folders they
// may read (everything for an administrator) and, for scripts and notebooks, the
// environments they may use, exactly as Explore's «Recent» decides (recentScope). No route
// takes a user id, and an id of an object the reader cannot see gets nothing, not an error
// that would say it exists.

const (
	homeFavouritesMax = 50
	homeNextRunsMax   = 50
	homeFlowRunsAuto  = 4 // flows shown when the widget names none
)

// homeObjectsCTE are the objects of Explore, by kind and id, with what decides who sees them.
const homeObjectsCTE = `objects AS (
	SELECT 'flow' AS kind, id, name, project_id, NULL::integer AS env FROM flows
	UNION ALL SELECT 'group', id, name, project_id, NULL FROM flow_groups
	UNION ALL SELECT 'datasource', id, name, project_id, NULL FROM datasources
	UNION ALL SELECT 'view', id, name, project_id, NULL FROM saved_views
	UNION ALL SELECT 'sql', id, name, project_id, NULL FROM sql_scripts
	UNION ALL SELECT 'script', id, name, project_id, environment_id FROM saved_scripts
	UNION ALL SELECT 'notebook', id, name, project_id, environment_id FROM notebooks
	UNION ALL SELECT 'report', id, name, project_id, NULL FROM reports
	UNION ALL SELECT 'dashboard', id, name, project_id, NULL FROM dashboards
	UNION ALL SELECT 'dataapi', id, name, project_id, NULL FROM data_apis
)`

// homeVisible is the WHERE of a reader over `o` (an object of homeObjectsCTE): $1 admin,
// $2 readable folders, $3 scripts on, $4 usable environments, $5 data APIs on.
const homeVisible = `($1 OR o.project_id = ANY($2))
	AND (o.kind NOT IN ('script', 'notebook') OR ($3 AND ($1 OR o.env = ANY($4))))
	AND (o.kind <> 'dataapi' OR $5)`

func (s recentScope) args() []any {
	readable, envs := s.readable, s.envs
	if readable == nil {
		readable = []int64{}
	}
	if envs == nil {
		envs = []int64{}
	}
	return []any{s.admin, readable, s.scriptsOn, envs, s.dataAPIsOn}
}

// readerScope is what the request's person may see, as /explore/recent computes it.
func readerScope(ctx context.Context, d *Deps, p *auth.Principal) (recentScope, error) {
	checker := perm.For(d.DB, &p.User)
	admin, err := checker.IsAdmin(ctx)
	if err != nil {
		return recentScope{}, err
	}
	scope := recentScope{admin: admin, scriptsOn: d.Config.ScriptsEnabled && d.Config.ScriptsNamespace != "" && d.Config.ScriptsRegistry != "",
		dataAPIsOn: dataAPIsOn(d)}
	if admin {
		return scope, nil
	}
	readable, err := checker.Readable(ctx)
	if err != nil {
		return recentScope{}, err
	}
	scope.readable = perm.Sorted(readable)
	rows, err := d.DB.Query(ctx, `SELECT seg.environment_id FROM script_environment_groups seg
		JOIN user_groups ug ON ug.group_id = seg.group_id WHERE ug.user_id = $1`, p.User.ID)
	if err != nil {
		return recentScope{}, err
	}
	scope.envs, err = pgx.CollectRows(rows, pgx.RowTo[int64])
	return scope, err
}

type homeQuerier interface {
	rowsQuerier
	rowQuerier
}

func init() {
	register("home_data", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/home/favourites", Handler: server.Authed(d.Auth, "/home/favourites", listFavourites(d))},
			{Method: http.MethodPut, Pattern: "/home/favourites/{kind}/{object_id}", Handler: server.Authed(d.Auth, "/home/favourites/{kind}/{object_id}", setFavourite(d, true))},
			{Method: http.MethodDelete, Pattern: "/home/favourites/{kind}/{object_id}", Handler: server.Authed(d.Auth, "/home/favourites/{kind}/{object_id}", setFavourite(d, false))},
			{Method: http.MethodGet, Pattern: "/home/next-runs", Handler: server.Authed(d.Auth, "/home/next-runs", homeNextRuns(d))},
			{Method: http.MethodGet, Pattern: "/home/flow-runs", Handler: server.Authed(d.Auth, "/home/flow-runs", homeFlowRuns(d))},
		}
	})
}

// ── favourites ───────────────────────────────────────────────────────────────

// FavouriteItem is an object a person pinned: its Explore kind and where it is.
type FavouriteItem struct {
	Kind      string `json:"kind"`
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	ProjectID int64  `json:"project_id"`
}

// FavouritesOut is the answer of the three favourites routes.
type FavouritesOut struct {
	Items []FavouriteItem `json:"items"`
}

// favourites are the person's pins they may still see, in the order they pinned them. A pin
// whose object is gone or no longer readable is simply not listed (and comes back with the right).
func favourites(ctx context.Context, q rowsQuerier, userID int64, s recentScope) ([]FavouriteItem, error) {
	rows, err := q.Query(ctx, `WITH `+homeObjectsCTE+`
		SELECT o.kind, o.id, o.name, o.project_id FROM user_favourites f
		JOIN objects o ON o.kind = f.kind AND o.id = f.object_id
		WHERE f.user_id = $6 AND `+homeVisible+` ORDER BY f.position, f.id`, append(s.args(), userID)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []FavouriteItem{}
	for rows.Next() {
		var it FavouriteItem
		if err := rows.Scan(&it.Kind, &it.ID, &it.Name, &it.ProjectID); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, rows.Err()
}

// pinFavourite pins an object the person may see. found is false when there is no such
// object for them; full when they hold the most pins already.
func pinFavourite(ctx context.Context, q homeQuerier, userID int64, s recentScope, kind string, id int64) (found, full bool, err error) {
	var n int
	err = q.QueryRow(ctx, `WITH `+homeObjectsCTE+` SELECT count(*) FROM objects o WHERE o.kind = $6 AND o.id = $7 AND `+homeVisible,
		append(s.args(), kind, id)...).Scan(&n)
	if err != nil || n == 0 {
		return false, false, err
	}
	var has, count int
	if err = q.QueryRow(ctx, `SELECT count(*) FILTER (WHERE kind = $2 AND object_id = $3), count(*) FROM user_favourites WHERE user_id = $1`,
		userID, kind, id).Scan(&has, &count); err != nil {
		return true, false, err
	}
	if has > 0 {
		return true, false, nil
	}
	if count >= homeFavouritesMax {
		return true, true, nil
	}
	_, err = q.Exec(ctx, `INSERT INTO user_favourites (user_id, kind, object_id, position, created_at)
		VALUES ($1, $2, $3, (SELECT coalesce(max(position), 0) + 1 FROM user_favourites WHERE user_id = $1), $4)
		ON CONFLICT ON CONSTRAINT uq_user_favourite DO NOTHING`, userID, kind, id, time.Now().UTC())
	return true, false, err
}

func listFavourites(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		scope, err := readerScope(r.Context(), d, p)
		if err != nil {
			fail(d, w, "favourites: the reader's scope", err)
			return
		}
		items, err := favourites(r.Context(), d.DB, p.User.ID, scope)
		if err != nil {
			fail(d, w, "reading the favourites", err)
			return
		}
		server.JSON(w, http.StatusOK, FavouritesOut{Items: items})
	}
}

// setFavourite pins (PUT) or unpins (DELETE) an object; both answer the list as it is now.
func setFavourite(d *Deps, pin bool) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "object_id")
		if !ok {
			return
		}
		kind := r.PathValue("kind")
		if !homeRecentKinds[kind] {
			detail(w, http.StatusNotFound, "Not found")
			return
		}
		ctx := r.Context()
		scope, err := readerScope(ctx, d, p)
		if err != nil {
			fail(d, w, "favourites: the reader's scope", err)
			return
		}
		if pin {
			found, full, err := pinFavourite(ctx, d.DB, p.User.ID, scope, kind, ids[0])
			switch {
			case err != nil:
				fail(d, w, "pinning a favourite", err)
				return
			case !found:
				detail(w, http.StatusNotFound, "Not found")
				return
			case full:
				detail(w, http.StatusConflict, "A home page holds "+strconv.Itoa(homeFavouritesMax)+" favourites at most")
				return
			}
		} else if _, err := d.DB.Exec(ctx, `DELETE FROM user_favourites WHERE user_id = $1 AND kind = $2 AND object_id = $3`, p.User.ID, kind, ids[0]); err != nil {
			fail(d, w, "unpinning a favourite", err)
			return
		}
		items, err := favourites(ctx, d.DB, p.User.ID, scope)
		if err != nil {
			fail(d, w, "reading the favourites", err)
			return
		}
		server.JSON(w, http.StatusOK, FavouritesOut{Items: items})
	}
}

// ── the runs due next ────────────────────────────────────────────────────────

// NextRun is a schedule that is due: what will run, when, and by which cron.
type NextRun struct {
	Kind      string  `json:"kind"`
	ID        int64   `json:"id"`
	Name      string  `json:"name"`
	ProjectID int64   `json:"project_id"`
	At        *string `json:"at"`
	Schedule  string  `json:"schedule"`
}

// NextRunsOut is GET /home/next-runs' answer.
type NextRunsOut struct {
	Items []NextRun `json:"items"`
}

// nextRuns are the schedules due within `hours`, soonest first, among what the reader may
// see; mine narrows them to those the person set or owns. One late (its time already past,
// the scheduler about to take it) is listed too.
func nextRuns(ctx context.Context, q rowsQuerier, userID int64, s recentScope, hours int, mine bool) ([]NextRun, error) {
	rows, err := q.Query(ctx, `WITH due AS (
		SELECT 'flow' AS kind, id, name, project_id, NULL::integer AS env, next_run_at AS at, run_schedule AS cron, run_scheduled_by AS by, owner_id FROM flows WHERE run_schedule IS NOT NULL
		UNION ALL SELECT 'group', id, name, project_id, NULL, next_run_at, run_schedule, run_scheduled_by, owner_id FROM flow_groups WHERE run_schedule IS NOT NULL
		UNION ALL SELECT 'sql', id, name, project_id, NULL, next_run_at, run_schedule, run_scheduled_by, owner_id FROM sql_scripts WHERE run_schedule IS NOT NULL
		UNION ALL SELECT 'script', id, name, project_id, environment_id, next_run_at, run_schedule, run_scheduled_by, owner_id FROM saved_scripts WHERE run_schedule IS NOT NULL
		UNION ALL SELECT 'datasource', id, name, project_id, NULL, next_refresh_at, refresh_schedule, refresh_scheduled_by, owner_id FROM datasources WHERE refresh_schedule IS NOT NULL
	)
	SELECT o.kind, o.id, o.name, o.project_id, o.at, o.cron FROM due o
	WHERE o.at IS NOT NULL AND o.at <= $6 AND `+homeVisible+` AND (NOT $7 OR o.by = $8 OR o.owner_id = $8)
	ORDER BY o.at, o.kind, o.id LIMIT $9`,
		append(s.args(), time.Now().UTC().Add(time.Duration(hours)*time.Hour), mine, userID, homeNextRunsMax)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []NextRun{}
	for rows.Next() {
		var it NextRun
		var at *time.Time
		if err := rows.Scan(&it.Kind, &it.ID, &it.Name, &it.ProjectID, &at, &it.Schedule); err != nil {
			return nil, err
		}
		it.At = isoPtr(at)
		out = append(out, it)
	}
	return out, rows.Err()
}

// queryInt is a whole number of the query within [min, max], or its default; ok is false
// when it is there and is not one.
func queryInt(r *http.Request, name string, def, min, max int) (int, bool) {
	v := r.URL.Query().Get(name)
	if v == "" {
		return def, true
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < min || n > max {
		return 0, false
	}
	return n, true
}

func homeNextRuns(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		hours, ok := queryInt(r, "hours", 24, 1, 168)
		scope := r.URL.Query().Get("scope")
		if !ok || (scope != "" && scope != "all" && scope != "mine") {
			detail(w, http.StatusUnprocessableEntity, "hours is 1 to 168, scope is all or mine")
			return
		}
		rs, err := readerScope(r.Context(), d, p)
		if err != nil {
			fail(d, w, "next runs: the reader's scope", err)
			return
		}
		items, err := nextRuns(r.Context(), d.DB, p.User.ID, rs, hours, scope == "mine")
		if err != nil {
			fail(d, w, "reading the next runs", err)
			return
		}
		server.JSON(w, http.StatusOK, NextRunsOut{Items: items})
	}
}

// ── the last runs of flows ───────────────────────────────────────────────────

// FlowRun is one run of a flow, for a bar of the widget: how it ended and how long it took.
type FlowRun struct {
	ID        int64    `json:"id"`
	Status    string   `json:"status"`
	Trigger   string   `json:"trigger"`
	StartedAt *string  `json:"started_at"`
	Seconds   *float64 `json:"seconds"`
}

// FlowRuns are the last runs of one flow, oldest first (the order of the bars).
type FlowRuns struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	ProjectID int64     `json:"project_id"`
	Runs      []FlowRun `json:"runs"`
}

// FlowRunsOut is GET /home/flow-runs' answer.
type FlowRunsOut struct {
	Flows []FlowRuns `json:"flows"`
}

// flowRuns are the last `perFlow` runs of each flow asked for that the reader may see, in
// the order asked; with none asked, of the flows that ran most in the last 30 days (a chart
// wants a history). Only runs of the whole flow
// (not a node tried from the editor). A run still going has no seconds.
func flowRuns(ctx context.Context, q rowsQuerier, s recentScope, ids []int64, perFlow int) ([]FlowRuns, error) {
	readable := s.readable
	if readable == nil {
		readable = []int64{}
	}
	if len(ids) == 0 {
		rows, err := q.Query(ctx, `SELECT f.id FROM flows f JOIN runs r ON r.flow_id = f.id AND r.kind = 'flow' AND r.trigger_type <> 'trial'
			AND r.started_at >= now() - interval '30 days'
			WHERE ($1 OR f.project_id = ANY($2)) GROUP BY f.id ORDER BY count(*) DESC, max(r.started_at) DESC, f.id LIMIT $3`, s.admin, readable, homeFlowRunsAuto)
		if err != nil {
			return nil, err
		}
		if ids, err = pgx.CollectRows(rows, pgx.RowTo[int64]); err != nil {
			return nil, err
		}
	}
	out := []FlowRuns{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := q.Query(ctx, `SELECT id, name, project_id FROM flows WHERE id = ANY($3) AND ($1 OR project_id = ANY($2))`, s.admin, readable, ids)
	if err != nil {
		return nil, err
	}
	byID := map[int64]*FlowRuns{}
	for rows.Next() {
		f := FlowRuns{Runs: []FlowRun{}}
		if err := rows.Scan(&f.ID, &f.Name, &f.ProjectID); err != nil {
			rows.Close()
			return nil, err
		}
		byID[f.ID] = &f
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	seen := make([]int64, 0, len(byID))
	for id := range byID {
		seen = append(seen, id)
	}
	rows, err = q.Query(ctx, `SELECT flow_id, id, status, trigger_type, started_at, finished_at FROM (
			SELECT r.*, row_number() OVER (PARTITION BY r.flow_id ORDER BY r.started_at DESC, r.id DESC) AS n
			FROM runs r WHERE r.flow_id = ANY($1) AND r.kind = 'flow' AND r.trigger_type <> 'trial') x
		WHERE n <= $2 ORDER BY flow_id, started_at, id`, seen, perFlow)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var flowID int64
		var run FlowRun
		var started, finished *time.Time
		if err := rows.Scan(&flowID, &run.ID, &run.Status, &run.Trigger, &started, &finished); err != nil {
			return nil, err
		}
		run.StartedAt = isoPtr(started)
		if started != nil && finished != nil {
			sec := finished.Sub(*started).Seconds()
			if sec < 0 {
				sec = 0
			}
			run.Seconds = &sec
		}
		byID[flowID].Runs = append(byID[flowID].Runs, run)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for _, id := range ids { // the order asked, each flow once
		if f, ok := byID[id]; ok {
			out = append(out, *f)
			delete(byID, id)
		}
	}
	return out, nil
}

func homeFlowRuns(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		perFlow, ok := queryInt(r, "runs", 20, 5, 50)
		var ids []int64
		for _, part := range strings.Split(r.URL.Query().Get("flows"), ",") {
			if part = strings.TrimSpace(part); part == "" {
				continue
			}
			id, err := strconv.ParseInt(part, 10, 64)
			if err != nil || id < 1 {
				ok = false
				break
			}
			ids = append(ids, id)
		}
		if !ok || len(ids) > homeMaxFlows {
			detail(w, http.StatusUnprocessableEntity, "runs is 5 to 50, flows is at most "+strconv.Itoa(homeMaxFlows)+" ids")
			return
		}
		scope, err := readerScope(r.Context(), d, p)
		if err != nil {
			fail(d, w, "flow runs: the reader's scope", err)
			return
		}
		flows, err := flowRuns(r.Context(), d.DB, scope, ids, perFlow)
		if err != nil {
			fail(d, w, "reading the flows' runs", err)
			return
		}
		server.JSON(w, http.StatusOK, FlowRunsOut{Flows: flows})
	}
}
