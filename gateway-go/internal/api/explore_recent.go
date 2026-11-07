package api

import (
	"context"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// Explore's top level, «Recent»: the objects this person changed last. A route of the Go
// gateway only (server/go_only_routes.go).
//
// What «changed by me» can be read from: nothing records who opened what, and the audit log
// is subject to retention, so it is not a record to build on. The versioned objects (flows,
// SQL scripts, scripts, notebooks, reports, dashboards) keep who saved each version
// (`*_versions.created_by`); every object keeps its owner and its last change
// (`owner_id`, `updated_at`). An object is recent for a person when they saved a version
// of it, or when it is theirs, at the later of the two moments. One query for all the kinds;
// only what the person may read now (the folder, and for scripts and notebooks their
// environment, as /search decides).

// RecentItem is one object of the list: its Explore kind, where it is, when it was touched.
type RecentItem struct {
	Kind      string  `json:"kind"`
	ID        int64   `json:"id"`
	Name      string  `json:"name"`
	ProjectID int64   `json:"project_id"`
	At        *string `json:"at"`
}

// RecentOut is GET /explore/recent's answer.
type RecentOut struct {
	Items []RecentItem `json:"items"`
}

func init() {
	register("explore_recent", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/explore/recent", Handler: server.Authed(d.Auth, "/explore/recent", exploreRecent(d))},
		}
	})
}

// recentScope is what the person may see: everything (an administrator), or the readable
// folders and, for scripts and notebooks, the usable environments.
type recentScope struct {
	admin     bool
	readable  []int64
	scriptsOn bool
	envs      []int64
	// dataAPIsOn: the installation has data APIs (Kubernetes, switched on)
	dataAPIsOn bool
}

type rowsQuerier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

const recentSQL = `
WITH touched AS (
	SELECT 'flow' AS kind, id, updated_at AS at FROM flows WHERE owner_id = $1
	UNION ALL SELECT 'flow', flow_id, max(created_at) FROM flow_versions WHERE created_by = $1 GROUP BY flow_id
	UNION ALL SELECT 'group', id, updated_at FROM flow_groups WHERE owner_id = $1
	UNION ALL SELECT 'datasource', id, updated_at FROM datasources WHERE owner_id = $1
	UNION ALL SELECT 'view', id, updated_at FROM saved_views WHERE owner_id = $1
	UNION ALL SELECT 'sql', id, updated_at FROM sql_scripts WHERE owner_id = $1
	UNION ALL SELECT 'sql', script_id, max(created_at) FROM sql_script_versions WHERE created_by = $1 GROUP BY script_id
	UNION ALL SELECT 'script', id, updated_at FROM saved_scripts WHERE owner_id = $1
	UNION ALL SELECT 'script', script_id, max(created_at) FROM saved_script_versions WHERE created_by = $1 GROUP BY script_id
	UNION ALL SELECT 'notebook', id, updated_at FROM notebooks WHERE owner_id = $1
	UNION ALL SELECT 'notebook', notebook_id, max(created_at) FROM notebook_versions WHERE created_by = $1 GROUP BY notebook_id
	UNION ALL SELECT 'report', id, updated_at FROM reports WHERE owner_id = $1
	UNION ALL SELECT 'report', report_id, max(created_at) FROM report_versions WHERE created_by = $1 GROUP BY report_id
	UNION ALL SELECT 'dashboard', id, updated_at FROM dashboards WHERE owner_id = $1
	UNION ALL SELECT 'dashboard', dashboard_id, max(created_at) FROM dashboard_versions WHERE created_by = $1 GROUP BY dashboard_id
	UNION ALL SELECT 'dataapi', id, updated_at FROM data_apis WHERE creator_id = $1
	UNION ALL SELECT 'dataapi', data_api_id, max(created_at) FROM data_api_versions WHERE created_by = $1 GROUP BY data_api_id
), last AS (
	SELECT kind, id, max(at) AS at FROM touched GROUP BY kind, id
), objects AS (
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
)
SELECT l.kind, l.id, o.name, o.project_id, l.at
FROM last l JOIN objects o ON o.kind = l.kind AND o.id = l.id
WHERE ($2 OR o.project_id = ANY($3))
	AND (o.kind NOT IN ('script', 'notebook') OR ($4 AND ($2 OR o.env = ANY($5))))
	AND (o.kind <> 'dataapi' OR $7)
ORDER BY l.at DESC NULLS LAST, l.kind, l.id
LIMIT $6`

// recentObjects is the list for a person, newest first.
func recentObjects(ctx context.Context, q rowsQuerier, userID int64, s recentScope, limit int64) ([]RecentItem, error) {
	readable, envs := s.readable, s.envs
	if readable == nil {
		readable = []int64{}
	}
	if envs == nil {
		envs = []int64{}
	}
	rows, err := q.Query(ctx, recentSQL, userID, s.admin, readable, s.scriptsOn, envs, limit, s.dataAPIsOn)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []RecentItem{}
	for rows.Next() {
		var it RecentItem
		var at *time.Time
		if err := rows.Scan(&it.Kind, &it.ID, &it.Name, &it.ProjectID, &at); err != nil {
			return nil, err
		}
		it.At = isoPtr(at)
		out = append(out, it)
	}
	return out, rows.Err()
}

// exploreRecent is GET /explore/recent?limit=: at most `limit` objects (1–20, default 6).
func exploreRecent(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		query := pyd.ParseQuery(r.URL.RawQuery)
		limit := ps.Query(query, "limit", pyd.Int().Ge(1).Le(20), int64(6))
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		checker := perm.For(d.DB, &p.User)
		admin, err := checker.IsAdmin(ctx)
		if err != nil {
			fail(d, w, "recent: the roles", err)
			return
		}
		scope := recentScope{admin: admin, scriptsOn: d.Config.ScriptsEnabled && d.Config.ScriptsNamespace != "" && d.Config.ScriptsRegistry != "",
			dataAPIsOn: dataAPIsOn(d)}
		if !admin {
			readable, err := checker.Readable(ctx)
			if err != nil {
				fail(d, w, "recent: the readable folders", err)
				return
			}
			scope.readable = perm.Sorted(readable)
			rows, err := d.DB.Query(ctx, `SELECT seg.environment_id FROM script_environment_groups seg
				JOIN user_groups ug ON ug.group_id = seg.group_id WHERE ug.user_id = $1`, p.User.ID)
			if err != nil {
				fail(d, w, "recent: the environments", err)
				return
			}
			if scope.envs, err = pgx.CollectRows(rows, pgx.RowTo[int64]); err != nil {
				fail(d, w, "recent: the environments", err)
				return
			}
		}
		items, err := recentObjects(ctx, d.DB, p.User.ID, scope, limit.(int64))
		if err != nil {
			fail(d, w, "recent: the objects", err)
			return
		}
		server.JSON(w, http.StatusOK, RecentOut{Items: items})
	}
}
