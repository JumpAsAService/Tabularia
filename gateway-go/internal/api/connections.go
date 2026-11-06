package api

import (
	"context"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// sqlServerDocsURL is routes/connections.py SQLSERVER_DOCS_URL.
const sqlServerDocsURL = "https://github.com/JumpAsAService/Tabularia/blob/main/docs/sqlserver.md"

// driverDocs is _DRIVER_DOCS.
var driverDocs = map[string]string{"sqlserver": sqlServerDocsURL}

// ConnectionOut is schemas/models.py ConnectionOut: never the password, only whether it is set.
type ConnectionOut struct {
	ID                 int64   `json:"id"`
	Name               string  `json:"name"`
	Description        string  `json:"description"`
	ProjectID          int64   `json:"project_id"`
	OwnerID            *int64  `json:"owner_id"`
	DBType             string  `json:"db_type"`
	Host               string  `json:"host"`
	Port               *int64  `json:"port"`
	Username           string  `json:"username"`
	Database           string  `json:"database"`
	DBSchema           string  `json:"db_schema"`
	Extra              string  `json:"extra"`
	HasPassword        bool    `json:"has_password"`
	HasPrivateKey      bool    `json:"has_private_key"`
	HostKeyType        string  `json:"host_key_type"`
	HostKeyFingerprint string  `json:"host_key_fingerprint"`
	UpdatedAt          *string `json:"updated_at"`
}

const connectionColumns = `id, name, description, project_id, owner_id, db_type, host, port, username, database, db_schema, extra,
	password_encrypted <> '', private_key_encrypted <> '', host_key_type, host_key_fingerprint, updated_at`

func scanConnection(row pgx.Row) (ConnectionOut, error) {
	var c ConnectionOut
	var updated time.Time
	err := row.Scan(&c.ID, &c.Name, &c.Description, &c.ProjectID, &c.OwnerID, &c.DBType, &c.Host, &c.Port, &c.Username,
		&c.Database, &c.DBSchema, &c.Extra, &c.HasPassword, &c.HasPrivateKey, &c.HostKeyType, &c.HostKeyFingerprint, &updated)
	c.UpdatedAt = isoPtr(&updated)
	return c, err
}

func queryConnections(ctx context.Context, q perm.Querier, tail string, args ...any) ([]ConnectionOut, error) {
	rows, err := q.Query(ctx, `SELECT `+connectionColumns+` FROM connections `+tail, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ConnectionOut{}
	for rows.Next() {
		c, err := scanConnection(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func init() {
	register("connections", func(d *Deps) []server.Route {
		authed := func(route string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, route, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/connections", Handler: authed("/connections", listAllConnections(d))},
			{Method: http.MethodGet, Pattern: "/connections/search", Handler: authed("/connections/search", searchConnections(d))},
			{Method: http.MethodGet, Pattern: "/connections/drivers", Handler: authed("/connections/drivers", connectionDrivers(d))},
			{Method: http.MethodGet, Pattern: "/projects/{project_id}/connections",
				Handler: authed("/projects/{project_id}/connections", listProjectConnections(d))},
		}
	})
}

func answerConnections(d *Deps, w http.ResponseWriter, rows []ConnectionOut, err error) {
	if err != nil {
		fail500(d, w, "connections", err)
		return
	}
	server.JSON(w, http.StatusOK, rows)
}

// listAllConnections is GET /connections: those of the folders with CONNECT.
func listAllConnections(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		connectable, err := perm.For(d.DB, &p.User).Projects(r.Context(), perm.Connect)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		if len(connectable) == 0 {
			server.JSON(w, http.StatusOK, []ConnectionOut{})
			return
		}
		rows, err := queryConnections(r.Context(), d.DB, `WHERE project_id = ANY($1) ORDER BY name`, perm.Sorted(connectable))
		answerConnections(d, w, rows, err)
	}
}

// searchConnections is GET /connections/search: paginated, over name, host and database.
func searchConnections(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		q, limit, offset, done := searchQuery(r, w)
		if done {
			return
		}
		ctx := r.Context()
		connectable, err := perm.For(d.DB, &p.User).Projects(ctx, perm.Connect)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		if len(connectable) == 0 {
			server.JSON(w, http.StatusOK, Page[ConnectionOut]{Items: []ConnectionOut{}})
			return
		}
		where, args := `WHERE project_id = ANY($1)`, []any{perm.Sorted(connectable)}
		if q != "" {
			where += ` AND (name ILIKE $2 OR host ILIKE $2 OR database ILIKE $2)`
			args = append(args, "%"+q+"%")
		}
		var total int64
		if err := d.DB.QueryRow(ctx, `SELECT count(*) FROM connections `+where, args...).Scan(&total); err != nil {
			fail500(d, w, "connections", err)
			return
		}
		n := len(args)
		items, err := queryConnections(ctx, d.DB, where+` ORDER BY name LIMIT $`+itoa(n+1)+` OFFSET $`+itoa(n+2), append(args, limit, offset)...)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		server.JSON(w, http.StatusOK, Page[ConnectionOut]{Items: items, Total: total})
	}
}

// listProjectConnections is GET /projects/{project_id}/connections: CONNECT on the folder.
func listProjectConnections(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		project, done := folderOf(d, w, r, "project_id")
		if done {
			return
		}
		ref, err := perm.For(d.DB, &p.User).Ensure(r.Context(), project, perm.Connect)
		if refuse(d, w, ref, err) {
			return
		}
		rows, err := queryConnections(r.Context(), d.DB, `WHERE project_id = $1 ORDER BY name`, project)
		answerConnections(d, w, rows, err)
	}
}

// connectionDrivers is GET /connections/drivers: the optional drivers the engine has. An
// engine that does not say (an error, not a dict) marks nothing.
func connectionDrivers(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		var drivers any
		if res, raw, err := d.Engine.JSON(ctx, http.MethodGet, "/db/drivers", nil); err == nil && res.StatusCode == http.StatusOK {
			drivers, _ = pyd.LoadsStr(string(raw))
		}
		out := pyjson.Object{}
		if o, ok := drivers.(*pyd.Object); ok {
			for _, k := range o.Keys() {
				info, _ := o.Get(k)
				io, isDict := info.(*pyd.Object)
				if !isDict {
					continue
				}
				installed, _ := io.Get("installed")
				out = append(out, pyjson.KV{Key: k, Value: pyjson.Object{{Key: "installed", Value: pyd.Truthy(installed)},
					{Key: "docs", Value: driverDocs[k]}}})
			}
		}
		b, _ := pyjson.Dumps(out, pyjson.Options{Compact: true})
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(b))
	}
}
