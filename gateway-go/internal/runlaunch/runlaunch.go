// Package runlaunch is the launching half of routes/runs.py, shared by the routes (a
// datasource's creation and refresh, a flow's run, a SQL script's write) and by the
// background work (the scheduler, the flow groups): the parquet's key (snapshot_key), the
// data contract sent with the data (contracts.payload), a datasource's import queued on the
// engine (launch_ingest_run) and the advisory lock of «one refresh per datasource».
package runlaunch

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// DB is what the launch reads and writes with: a pool, a connection or a transaction.
type DB interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// RefreshLock is _REFRESH_LOCK: the advisory locks' namespace of «one refresh per datasource».
const RefreshLock = 74610002

// PlaceholderTask is PLACEHOLDER_TASK: the task id of a run row written before its task.
const PlaceholderTask = "pending-"

// LiveIngest is models/run.py LIVE_INGEST.
const LiveIngest = "kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE')"

// Hex8 is uuid.uuid4().hex[:8]; Hex32 the whole hex.
func Hex8() string { return randomHex(4) }

// Hex32 is uuid.uuid4().hex.
func Hex32() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return hex.EncodeToString(b)
}

func randomHex(n int) string { return Hex32()[:2*n] }

// SnapshotKey is snapshot_key: a datasource's parquet, in a folder of its own (`new` while
// the datasource does not exist yet), the writing moment in the name.
func SnapshotKey(dsID *int64) string {
	folder := "new"
	if dsID != nil && *dsID != 0 {
		folder = fmt.Sprint(*dsID)
	}
	return fmt.Sprintf("datasets/%s/%s-%s.parquet", folder, time.Now().UTC().Format("20060102T150405Z"), Hex8())
}

// ContractPayload is services/contracts.py payload: the datasource's contract, when on and
// with rules, with its version; nil when nothing is to be evaluated. err for what Python
// raises (a document that is no dict).
func ContractPayload(ctx context.Context, q DB, dsID *int64) (pyjson.Object, error) {
	if dsID == nil {
		return nil, nil
	}
	var enabled bool
	var doc string
	var version int64
	err := q.QueryRow(ctx, `SELECT enabled, document, version FROM data_contracts WHERE datasource_id = $1 ORDER BY id LIMIT 1`, *dsID).
		Scan(&enabled, &doc, &version)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && !enabled) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	v, lerr := pyjson.Loads(doc)
	if lerr != nil {
		return nil, nil
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, errors.New("the contract's document is not a dict") // doc.get raising
	}
	if !pyjson.Truthy(o.Value("rules")) {
		return nil, nil
	}
	return append(pyjson.Object{}, o...).Set("version", version), nil
}

// Source is the datasource an import refreshes: the columns launch_ingest_run reads.
type Source struct {
	ID         int64
	Bucket     string
	Key        string
	SourceType *string
	SourceRef  *string
	SortKeys   string
}

// Env is what an import is launched with.
type Env struct {
	DB     DB
	Engine *engine.Client
	Bucket string // ENGINE__BUCKET
}

// templateKeys is _TEMPLATE_KEYS.
var templateKeys = []string{"timezone", "variables", "last_refresh_initial", "mode", "keys"}

// templatePart is _template_part: the placeholders' zone, variables and refresh mode, when
// the datasource has them.
func templatePart(ref pyjson.Object) pyjson.Object {
	out := pyjson.Object{}
	for _, k := range templateKeys {
		if v, ok := ref.Get(k); ok {
			out = append(out, pyjson.KV{Key: k, Value: v})
		}
	}
	return out
}

// pick is {k: ref[k] for k in keys if k in ref}.
func pick(ref pyjson.Object, keys ...string) pyjson.Object {
	out := pyjson.Object{}
	for _, k := range keys {
		if v, ok := ref.Get(k); ok {
			out = append(out, pyjson.KV{Key: k, Value: v})
		}
	}
	return out
}

func merge(objs ...pyjson.Object) pyjson.Object {
	out := pyjson.Object{}
	for _, o := range objs {
		for _, kv := range o {
			out = out.Set(kv.Key, kv.Value)
		}
	}
	return out
}

// LastSuccessfulRefresh is _last_successful_refresh: when the last successful refresh
// started (ISO, UTC), "" when none.
func LastSuccessfulRefresh(ctx context.Context, q DB, dsID int64) (string, error) {
	var started time.Time
	err := q.QueryRow(ctx, `SELECT started_at FROM runs WHERE datasource_id = $1 AND kind = 'ingest' AND status = 'SUCCESS'
		ORDER BY started_at DESC LIMIT 1`, dsID).Scan(&started)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return pyjson.Isoformat(started), nil
}

// refreshContext is _refresh_context: {last_refresh} when templates or variables may use it,
// the current snapshot in append mode.
func refreshContext(ctx context.Context, q DB, ds *Source, ref pyjson.Object, fields ...string) (pyjson.Object, error) {
	out := pyjson.Object{}
	templated := pyjson.Truthy(ref.Value("variables"))
	for _, f := range fields {
		if strings.Contains(pyjson.Str(pyjson.Or(ref.Value(f), "")), "{") {
			templated = true
		}
	}
	if templated {
		last, err := LastSuccessfulRefresh(ctx, q, ds.ID)
		if err != nil {
			return nil, err
		}
		if last != "" {
			out = append(out, pyjson.KV{Key: "last_refresh", Value: last})
		}
	}
	if ref.Value("mode") == "append" && ds.Key != "" {
		out = append(out, pyjson.KV{Key: "previous", Value: pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}}})
	}
	return out, nil
}

// ErrPython is what Python raises outside any handler on the way (a source_ref that is not a
// JSON dict, an engine answer that is not one): the route's 500.
var ErrPython = errors.New("runlaunch: an exception nobody catches")

func sourceRef(ds *Source) (pyjson.Object, error) {
	text := "{}"
	if ds.SourceRef != nil && *ds.SourceRef != "" {
		text = *ds.SourceRef
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return nil, ErrPython
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return nil, ErrPython
	}
	return o, nil
}

// SubmitTask posts a task to the engine (timeout CHECK) and returns its id: the engine's
// refusal as `HTTPException(resp.status_code, resp.text[:500])`, a 502 without a task id.
func SubmitTask(ctx context.Context, e *engine.Client, path string, body pyjson.Object) (string, *auth.Refusal, error) {
	ctx, cancel := context.WithTimeout(ctx, engine.Check)
	defer cancel()
	res, raw, err := e.JSON(ctx, http.MethodPost, path, body)
	if err != nil {
		return "", nil, &EngineDown{err}
	}
	if res.StatusCode >= 400 {
		return "", &auth.Refusal{Status: res.StatusCode, Detail: textCut(raw, 500)}, nil
	}
	return TaskID(raw)
}

// TaskID is resp.json().get("task_id") checked: a 502 when it is missing or empty.
func TaskID(raw []byte) (string, *auth.Refusal, error) {
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return "", nil, ErrPython
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return "", nil, ErrPython
	}
	t := o.Value("task_id")
	if !pyjson.Truthy(t) {
		return "", &auth.Refusal{Status: http.StatusBadGateway, Detail: "The engine did not return a task_id"}, nil
	}
	switch x := t.(type) {
	case string:
		return x, nil, nil
	case int64, pyjson.Int:
		return pyjson.Str(x), nil, nil // the number PostgreSQL casts into the text column
	case bool:
		return "true", nil, nil
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64), nil, nil
	}
	return "", nil, ErrPython // a task id the column refuses
}

// textCut is httpx's resp.text[:n].
func textCut(raw []byte, n int) string {
	s := strings.ToValidUTF8(string(raw), "�")
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}

// Ingest is launch_ingest_run: a datasource's import queued on the engine, its run recorded
// (committed). Every refresh writes a new parquet; the datasource points at it only once it
// succeeds.
func Ingest(ctx context.Context, env Env, userID int64, ds *Source, conn *connpayload.Conn, trigger string, parent *int64) (int64, *auth.Refusal, error) {
	outputKey := SnapshotKey(&ds.ID)
	contract, err := ContractPayload(ctx, env.DB, &ds.ID)
	if err != nil {
		return 0, nil, err
	}
	withContract := func(o pyjson.Object) pyjson.Object {
		if contract != nil {
			o = append(o, pyjson.KV{Key: "contract", Value: contract})
		}
		return o
	}
	payload := connpayload.Payload(conn)
	var path string
	var body pyjson.Object
	switch conn.DBType {
	case "sharepoint":
		ref, err := sourceRef(ds)
		if err != nil {
			return 0, nil, err
		}
		pathV, ok := ref.Get("path")
		if !ok {
			pathV = ""
		}
		sheet, ok := ref.Get("sheet")
		if !ok {
			sheet = ""
		}
		rc, err := refreshContext(ctx, env.DB, ds, ref, "path")
		if err != nil {
			return 0, nil, err
		}
		path = "/sharepoint/ingest"
		body = merge(pyjson.Object{{Key: "connection", Value: payload},
			{Key: "source", Value: merge(pyjson.Object{{Key: "path", Value: pathV}, {Key: "sheet", Value: sheet}}, pick(ref, "format", "csv"), templatePart(ref))},
			{Key: "bucket", Value: env.Bucket}, {Key: "output_key", Value: outputKey}}, rc)
	case "api":
		ref, err := sourceRef(ds)
		if err != nil {
			return 0, nil, err
		}
		last, err := LastSuccessfulRefresh(ctx, env.DB, ds.ID)
		if err != nil {
			return 0, nil, err
		}
		path = "/http-api/ingest"
		body = pyjson.Object{{Key: "connection", Value: payload}, {Key: "source", Value: ref}, {Key: "bucket", Value: env.Bucket},
			{Key: "output_key", Value: outputKey}}
		if last != "" {
			body = append(body, pyjson.KV{Key: "last_refresh", Value: last})
		}
		if ref.Value("mode") == "append" && ds.Key != "" {
			body = append(body, pyjson.KV{Key: "previous", Value: pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}}})
		}
	case "gsheets":
		ref, err := sourceRef(ds)
		if err != nil {
			return 0, nil, err
		}
		path = "/gsheets/ingest"
		body = pyjson.Object{{Key: "connection", Value: payload}, {Key: "source", Value: pick(ref, "spreadsheet_id", "sheet_id", "sheet", "range")},
			{Key: "bucket", Value: env.Bucket}, {Key: "output_key", Value: outputKey}}
	case "sftp", "s3":
		ref, err := sourceRef(ds)
		if err != nil {
			return 0, nil, err
		}
		keys := []string{"folder", "pattern", "format", "sheet", "csv"}
		path = "/sftp/ingest"
		if conn.DBType == "s3" {
			keys = append([]string{"bucket"}, keys...)
			path = "/s3/ingest"
		}
		rc, err := refreshContext(ctx, env.DB, ds, ref, "folder", "pattern")
		if err != nil {
			return 0, nil, err
		}
		body = merge(pyjson.Object{{Key: "connection", Value: payload}, {Key: "source", Value: merge(pick(ref, keys...), templatePart(ref))},
			{Key: "bucket", Value: env.Bucket}, {Key: "output_key", Value: outputKey}}, rc)
	default:
		sortKeys := ds.SortKeys
		if sortKeys == "" {
			sortKeys = "[]"
		}
		sk, err := pyjson.Loads(sortKeys)
		if err != nil {
			return 0, nil, ErrPython
		}
		var mode, ref any
		if ds.SourceType != nil {
			mode = *ds.SourceType
		}
		if ds.SourceRef != nil {
			ref = *ds.SourceRef
		}
		path = "/db/ingest"
		body = pyjson.Object{{Key: "connection", Value: payload},
			{Key: "source", Value: pyjson.Object{{Key: "mode", Value: mode}, {Key: "ref", Value: ref}, {Key: "sort_keys", Value: sk}}},
			{Key: "bucket", Value: env.Bucket}, {Key: "output_key", Value: outputKey}}
	}
	taskID, ref, err := SubmitTask(ctx, env.Engine, path, withContract(body))
	if err != nil || ref != nil {
		return 0, ref, err
	}
	var id int64
	err = env.DB.QueryRow(ctx, `INSERT INTO runs (kind, flow_id, datasource_id, task_id, status, launched_by, trigger_type, parent_run_id,
		input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at)
		VALUES ('ingest', NULL, $1, $2, 'PENDING', $3, $4, $5, $6, $7, $8, '', false, '[]', $9) RETURNING id`,
		ds.ID, taskID, userID, trigger, parent, conn.DBType+"://"+conn.Host+"/"+conn.Database, env.Bucket, outputKey,
		time.Now().UTC()).Scan(&id)
	return id, nil, err
}

// InLine is in_line_for_the_datasource: the transaction waits for every other starter of
// this datasource's refresh until it ends.
func InLine(ctx context.Context, tx pgx.Tx, dsID int64) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, $2)`, RefreshLock, dsID)
	return err
}

// LiveRefresh is live_refresh: the datasource's import still running or queued.
func LiveRefresh(ctx context.Context, q DB, dsID int64) (bool, error) {
	var id int64
	err := q.QueryRow(ctx, `SELECT id FROM runs WHERE datasource_id = $1 AND `+LiveIngest+` ORDER BY id DESC LIMIT 1`, dsID).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}
