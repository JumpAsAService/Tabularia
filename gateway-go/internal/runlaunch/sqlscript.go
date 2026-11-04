package runlaunch

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sqlscripts"
)

// SQLScriptAlreadyRunning is sql_script_runs.ALREADY_RUNNING.
const SQLScriptAlreadyRunning = "The script is already writing its datasource: it can write again once it has finished"

// sqlScriptCheck is sql_script_runs.CHECK: the engine answers a submitted task quickly.
const sqlScriptCheck = 60 * time.Second

// SQLEnv is what a SQL script's write is launched with.
type SQLEnv struct {
	FlowEnv
	SQL *aisvc.Client // the AI service's parse of the SQL (sqlglot)
}

func refusal(status int, detail string) *auth.Refusal {
	return &auth.Refusal{Status: status, Detail: detail}
}

// LaunchSQLScript is services/sql_script_runs.py launch: the write of the script's current
// version into its datasource, with `user`'s authority, its run recorded and audited
// (`withRequest`: the audit row carries the request's address, as `request=request`).
// The error is a refusal (*auth.Refusal, *reports.Refusal, *sqlscripts.Refusal or
// Unreadable), the engine down (EngineDown, *reports.EngineDown) or a failure.
func LaunchSQLScript(ctx context.Context, env SQLEnv, user *auth.User, s *sqlscripts.Script, trigger string, parent *int64,
	withRequest bool) (int64, error) {
	c := perm.For(env.DB, user)
	// _checked_script
	if see, err := sqlscripts.CanSee(ctx, c, s); err != nil {
		return 0, err
	} else if !see {
		return 0, refusal(http.StatusNotFound, fmt.Sprintf("%s can no longer see the SQL script «%s»", user.Email, s.Name))
	}
	if ok, err := c.Has(ctx, s.ProjectID, perm.Run); err != nil {
		return 0, err
	} else if !ok {
		return 0, refusal(http.StatusForbidden, fmt.Sprintf("RUN missing on the folder of the SQL script «%s»", s.Name))
	}
	content, err := sqlscripts.ContentOf(ctx, env.DB, s, nil)
	if err != nil {
		return 0, err
	}
	output := sqlscripts.OutputOf(content)
	if output == nil {
		return 0, refusal(http.StatusUnprocessableEntity, fmt.Sprintf("The SQL script «%s» does not save its result as a datasource: "+
			"give it a name and a folder first", s.Name))
	}
	sql, _ := pyjson.Or(content.Value("sql"), "").(string)
	if pystr.Strip(strings.TrimRight(pystr.Strip(sqlscripts.StripComments(sql)), ";")) == "" {
		return 0, refusal(http.StatusUnprocessableEntity, fmt.Sprintf("The SQL script «%s» has no SQL yet", s.Name))
	}
	if ref, err := sqlscripts.CheckOutput(ctx, c, env.DB, user, output); ref != nil || err != nil {
		if err != nil {
			return 0, err
		}
		return 0, ref
	}
	target, err := sqlscripts.TargetOf(ctx, env.DB, output)
	if err != nil {
		return 0, err
	}
	var targetID *int64
	if target != nil {
		targetID = &target.ID
	}
	contract, err := ContractPayload(ctx, env.DB, targetID)
	if err != nil {
		return 0, err
	}
	outputKey := SnapshotKey(targetID)
	project, _ := sqlscripts.OutputProject(output)
	detail := pyjson.Object{{Key: "version", Value: s.CurrentVersion}, {Key: "trigger", Value: trigger}, {Key: "sql", Value: cut(sql, 2000)},
		{Key: "datasource", Value: pyjson.Object{{Key: "name", Value: output.Value("name")}, {Key: "project_id", Value: output.Value("project_id")}}}}
	var bucket, inputKey, engineName, path string
	var body pyjson.Object
	var dsIDs []any
	connectionID := content.Value("connection_id")
	if connectionID != nil {
		cid, _ := connectionID.(int64)
		conn, ref, err := sqlscripts.UsableConnection(ctx, c, env.DB, cid)
		if err != nil {
			return 0, err
		}
		if ref != nil {
			return 0, ref
		}
		full, err := connpayload.Load(ctx, env.DB, conn.ID)
		if err != nil || full == nil {
			return 0, fmt.Errorf("connection %d: %v", conn.ID, err)
		}
		bucket, inputKey, engineName = env.Bucket, "", conn.Name
		path = "/db/ingest"
		body = pyjson.Object{{Key: "connection", Value: connpayload.Payload(full)},
			{Key: "source", Value: pyjson.Object{{Key: "mode", Value: "sql"}, {Key: "ref", Value: sql}}},
			{Key: "bucket", Value: bucket}, {Key: "output_key", Value: outputKey}}
		detail = append(detail, pyjson.KV{Key: "connection", Value: pyjson.Object{{Key: "id", Value: conn.ID}, {Key: "name", Value: conn.Name}}})
	} else {
		rs := &reports.Resolver{Q: env.DB, C: c, Engine: env.Engine, Log: env.Log, SQL: env.SQL}
		if engineName, err = rs.PickEngine(ctx, content.Value("engine")); err != nil {
			return 0, err
		}
		resolved, err := sqlscripts.Resolve(ctx, env.DB, c, env.SQL, sql, engineName)
		if err != nil {
			return 0, err
		}
		if len(resolved.Tables) == 0 {
			return 0, &sqlscripts.Refusal{Message: "The query reads no datasource: name one in FROM"}
		}
		chain := sqlscripts.CatalogChain(resolved)
		bucket, _ = chain.Value("bucket").(string)
		inputKey, _ = chain.Value("input_key").(string)
		path = "/tasks/transform-data"
		body = append(append(pyjson.Object{}, chain...), pyjson.KV{Key: "output_key", Value: outputKey}, pyjson.KV{Key: "engine", Value: engineName})
		var named []any
		for _, n := range resolved.Tables {
			named = append(named, pyjson.Object{{Key: "id", Value: n.Datasource.ID}, {Key: "name", Value: n.Datasource.Name}})
			dsIDs = append(dsIDs, n.Datasource.ID)
		}
		detail = append(detail, pyjson.KV{Key: "engine", Value: engineName}, pyjson.KV{Key: "datasources", Value: named})
	}
	if contract != nil {
		body = append(body, pyjson.KV{Key: "contract", Value: contract})
	}
	tx, err := env.DB.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	if err := sqlscripts.InLine(ctx, tx, s.ID); err != nil {
		return 0, err
	}
	if live, err := sqlscripts.LiveRun(ctx, tx, s.ID); err != nil {
		return 0, err
	} else if live {
		return 0, refusal(http.StatusConflict, SQLScriptAlreadyRunning)
	}
	cctx, cancel := context.WithTimeout(ctx, sqlScriptCheck)
	res, raw, err := env.Engine.JSON(cctx, http.MethodPost, path, body)
	cancel()
	if err != nil {
		return 0, &EngineDown{err}
	}
	if res.StatusCode >= 400 {
		return 0, reports.EngineFailure(res.StatusCode, raw, func(d string) string { return d })
	}
	taskID, ref, err := TaskID(raw)
	if err != nil {
		return 0, err
	}
	if ref != nil {
		return 0, ref
	}
	if dsIDs == nil {
		dsIDs = []any{}
	}
	read := pyjson.Object{{Key: "version", Value: s.CurrentVersion}, {Key: "connection_id", Value: connectionID}, {Key: "datasources", Value: dsIDs}}
	description := fmt.Sprintf("Written by the SQL script «%s»", s.Name)
	if target != nil && target.Description != "" {
		description = target.Description
	}
	var runID int64
	err = tx.QueryRow(ctx, `INSERT INTO runs (kind, sql_script_id, task_id, status, launched_by, trigger_type, parent_run_id, engine, input_key,
		output_bucket, output_key, publish_name, publish_project_id, publish_overwrite, publish_description, publish_sort_keys, script, started_at)
		VALUES ('sql', $1, $2, 'PENDING', $3, $4, $5, $6, $7, $8, $9, $10, $11, true, $12, '[]', $13, $14) RETURNING id`,
		s.ID, taskID, user.ID, trigger, parent, engineName, inputKey, bucket, outputKey, output.Value("name"), project, description,
		dumps(read), time.Now().UTC()).Scan(&runID)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	actx := ctx
	if !withRequest {
		actx = context.Background()
	}
	kind := "sql_script"
	audit.Record(actx, env.DB, env.Log, audit.Event{Actor: user, Action: "sql_script.publish", TargetType: &kind, TargetID: &s.ID,
		TargetLabel: &s.Name, Detail: append(pyjson.Object{{Key: "run_id", Value: runID}}, detail...)})
	return runID, nil
}

// EngineDown is httpx.TransportError met on the way: the gateway's handler (main.py
// _service_down) answers it with a 503.
type EngineDown struct{ Err error }

func (e *EngineDown) Error() string { return "the engine is not answering: " + e.Err.Error() }
func (e *EngineDown) Unwrap() error { return e.Err }
