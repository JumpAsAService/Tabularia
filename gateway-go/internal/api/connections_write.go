package api

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("connections_write", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/duplicate",
				Handler: server.Authed(d.Auth, "/connections/{conn_id}/duplicate", duplicateConnection(d))},
			{Method: http.MethodDelete, Pattern: "/connections/{conn_id}", Handler: server.Authed(d.Auth, "/connections/{conn_id}", deleteConnection(d))},
		}
	})
}

// connectionOf is _get_connection after the path's validation, then CONNECT on its folder.
func connectionOf(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker) (*ConnectionOut, bool) {
	var ps pyd.Params
	v := ps.Path("conn_id", r.PathValue("conn_id"), pyd.Int())
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return nil, true
	}
	id, ok := rowID(v)
	var conn ConnectionOut
	var err error
	if ok {
		conn, err = scanConnection(d.DB.QueryRow(r.Context(), `SELECT `+connectionColumns+` FROM connections WHERE id = $1`, id))
	}
	if !ok || errors.Is(err, pgx.ErrNoRows) {
		server.Refuse(w, notFound("Connection not found"))
		return nil, true
	}
	if err != nil {
		fail500(d, w, "connections", err)
		return nil, true
	}
	if ref, err := c.Ensure(r.Context(), conn.ProjectID, perm.Connect); refuse(d, w, ref, err) {
		return nil, true
	}
	return &conn, false
}

// duplicateConnection is POST /connections/{conn_id}/duplicate: a copy as «name (i)», its
// password and accepted host key along.
func duplicateConnection(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		src, done := connectionOf(d, w, r, perm.For(d.DB, &p.User))
		if done {
			return
		}
		name, err := copynames.Free(ctx, d.DB, "connections", src.ProjectID, src.Name)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
			SELECT $1, description, project_id, $2, db_type, host, port, username, password_encrypted, database, db_schema, extra,
			private_key_encrypted, host_key_type, host_key_fingerprint, $3, $3 FROM connections WHERE id = $4 RETURNING id`,
			name, p.User.ID, now, src.ID).Scan(&id)
		if copynames.Clash(err) {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: copynames.ClashDetail})
			return
		}
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		kind := "connection"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "connection.create", TargetType: &kind, TargetID: &id,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "db_type", Value: src.DBType},
				{Key: "host", Value: src.Host}, {Key: "duplicate_of", Value: src.ID}}})
		out, err := scanConnection(d.DB.QueryRow(ctx, `SELECT `+connectionColumns+` FROM connections WHERE id = $1`, id))
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		server.JSON(w, http.StatusCreated, out)
	}
}

// deleteConnection is DELETE /connections/{conn_id}: refused while datasources use it, flows
// send their notices through it, reports or dashboards query it.
func deleteConnection(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		conn, done := connectionOf(d, w, r, c)
		if done {
			return
		}
		var one int
		switch err := d.DB.QueryRow(ctx, `SELECT 1 FROM datasources WHERE connection_id = $1 LIMIT 1`, conn.ID).Scan(&one); {
		case err == nil:
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: "The connection is used by one or more datasources: delete or move them first"})
			return
		case !errors.Is(err, pgx.ErrNoRows):
			fail500(d, w, "connections", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT name FROM flows WHERE notify_connection_id = $1 ORDER BY name LIMIT 6`, conn.ID)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		var flows []string
		for rows.Next() {
			var n string
			if err := rows.Scan(&n); err != nil {
				rows.Close()
				fail500(d, w, "connections", err)
				return
			}
			flows = append(flows, "'"+n+"'")
		}
		rows.Close()
		if len(flows) > 0 {
			names := strings.Join(flows[:min(5, len(flows))], ", ")
			if len(flows) > 5 {
				names += ", …"
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict,
				Detail: fmt.Sprintf("The connection sends the failure notices of flows (%s): change their schedule first", names)})
			return
		}
		if refuseReadBy(d, w, ctx, c, "connection", conn.ID, "The connection") {
			return
		}
		if _, err := d.DB.Exec(ctx, `DELETE FROM connections WHERE id = $1`, conn.ID); err != nil {
			fail500(d, w, "connections", err)
			return
		}
		kind := "connection"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "connection.delete", TargetType: &kind, TargetID: &conn.ID,
			TargetLabel: &conn.Name, Detail: pyjson.Object{{Key: "db_type", Value: conn.DBType}}})
		returnsNone(w)
	}
}
