package api

import (
	"bytes"
	"context"
	"errors"
	"math"
	"math/big"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// contractEngineWait is ENGINE_WAIT_SECONDS: the engine gives up at 120 s.
const contractEngineWait = 150 * time.Second

// ContractOut is routes/contracts.py ContractOut.
type ContractOut struct {
	DatasourceID       int64       `json:"datasource_id"`
	Version            int64       `json:"version"`
	Enabled            bool        `json:"enabled"`
	Document           *pyd.Object `json:"document"`
	Status             string      `json:"status"`
	CheckedAt          *string     `json:"checked_at"`
	Errors             int64       `json:"errors"`
	Warnings           int64       `json:"warnings"`
	Blocked            bool        `json:"blocked"`
	BlockedAt          *string     `json:"blocked_at"`
	BlockedRunID       *int64      `json:"blocked_run_id"`
	Report             *pyd.Object `json:"report"`
	UpdatedAt          *string     `json:"updated_at"`
	CheckError         *string     `json:"check_error"`
	Editable           bool        `json:"editable"`
	NotifyEmails       string      `json:"notify_emails"`
	NotifyConnectionID *int64      `json:"notify_connection_id"`
}

// ContractResultOut is routes/contracts.py ContractResultOut.
type ContractResultOut struct {
	ID              int64       `json:"id"`
	ContractVersion int64       `json:"contract_version"`
	Trigger         string      `json:"trigger"`
	RunID           *int64      `json:"run_id"`
	Outcome         string      `json:"outcome"`
	Blocked         bool        `json:"blocked"`
	Errors          int64       `json:"errors"`
	Warnings        int64       `json:"warnings"`
	Rows            *int64      `json:"rows"`
	EvaluatedAt     string      `json:"evaluated_at"`
	Report          *pyd.Object `json:"report"`
}

type contractRow struct {
	ID, DatasourceID, Version, Errors, Warnings int64
	Document                                    string
	Enabled                                     bool
	Status                                      string
	CheckedAt, BlockedAt                        *time.Time
	BlockedRunID, NotifyConnectionID            *int64
	LastReport, NotifyEmails                    *string
	UpdatedAt                                   time.Time
}

// contractOf is services/contracts.py get (the first row, as .first()).
func contractOf(ctx context.Context, q perm.Querier, dsID int64) (*contractRow, error) {
	var c contractRow
	err := q.QueryRow(ctx, `SELECT id, datasource_id, version, errors, warnings, document, enabled, status, checked_at, blocked_at,
		blocked_run_id, notify_connection_id, last_report, notify_emails, updated_at FROM data_contracts WHERE datasource_id = $1 LIMIT 1`,
		dsID).Scan(&c.ID, &c.DatasourceID, &c.Version, &c.Errors, &c.Warnings, &c.Document, &c.Enabled, &c.Status, &c.CheckedAt,
		&c.BlockedAt, &c.BlockedRunID, &c.NotifyConnectionID, &c.LastReport, &c.NotifyEmails, &c.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// contractLoads is _loads: json.loads of a text, None for an empty one or a JSONDecodeError.
func contractLoads(text *string) any {
	if text == nil || *text == "" {
		return nil
	}
	v, err := pyd.LoadsStr(*text)
	if err != nil {
		return nil
	}
	return v
}

func emptyRules() *pyd.Object {
	o := pyd.NewObject()
	o.Set("rules", []any{})
	return o
}

// contractDocument is `_loads(c.document) or {"rules": []}`.
func contractDocument(text string) any {
	v := contractLoads(&text)
	if !pyd.Truthy(v) {
		return emptyRules()
	}
	return v
}

// dictOrFail is a value for a `dict[str, Any]` field of the answer (None allowed when
// optional): anything else fails the response model, a 500.
func dictOrFail(v any, optional bool) (*pyd.Object, error) {
	if o, ok := v.(*pyd.Object); ok {
		return o, nil
	}
	if v == nil && optional {
		return nil, nil
	}
	return nil, errResponse
}

// contractOut is _out.
func contractOut(c *contractRow, checkError *string) (ContractOut, error) {
	doc, err := dictOrFail(contractDocument(c.Document), false)
	if err != nil {
		return ContractOut{}, err
	}
	report, err := dictOrFail(contractLoads(c.LastReport), true)
	if err != nil {
		return ContractOut{}, err
	}
	status := c.Status
	if c.BlockedAt != nil {
		status = "failed"
	}
	emails := ""
	if c.NotifyEmails != nil {
		emails = *c.NotifyEmails
	}
	return ContractOut{DatasourceID: c.DatasourceID, Version: c.Version, Enabled: c.Enabled, Document: doc, Status: status,
		CheckedAt: isoPtr(c.CheckedAt), Errors: c.Errors, Warnings: c.Warnings, Blocked: c.BlockedAt != nil,
		BlockedAt: isoPtr(c.BlockedAt), BlockedRunID: c.BlockedRunID, Report: report, UpdatedAt: isoPtr(&c.UpdatedAt),
		CheckError: checkError, Editable: true, NotifyEmails: emails, NotifyConnectionID: c.NotifyConnectionID}, nil
}

func init() {
	register("contracts", func(d *Deps) []server.Route {
		authed := func(route string, h func(http.ResponseWriter, *http.Request, *auth.Principal)) http.Handler {
			return server.Authed(d.Auth, route, h)
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/datasources/{ds_id}/contract", Handler: authed("/datasources/{ds_id}/contract", getContract(d))},
			{Method: http.MethodGet, Pattern: "/datasources/{ds_id}/contract/history",
				Handler: authed("/datasources/{ds_id}/contract/history", contractHistory(d))},
			{Method: http.MethodDelete, Pattern: "/datasources/{ds_id}/contract",
				Handler: authed("/datasources/{ds_id}/contract", deleteContract(d))},
			{Method: http.MethodPost, Pattern: "/datasources/{ds_id}/contract/check",
				Handler: authed("/datasources/{ds_id}/contract/check", checkContract(d))},
			{Method: http.MethodPost, Pattern: "/datasources/{ds_id}/contract/proposal",
				Handler: authed("/datasources/{ds_id}/contract/proposal", proposeContract(d))},
		}
	})
}

// contractDatasource is _datasource: the datasource of the path (404) with the capability
// on its folder (403); the path validated first (pyd.Params given, so a query can join it).
func contractDatasource(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, query func(*pyd.Params),
	needed perm.Capability) (*datasourceRow, *perm.Checker, bool) {
	var ps pyd.Params
	v := ps.Path("ds_id", r.PathValue("ds_id"), pyd.Int())
	if query != nil {
		query(&ps) // FastAPI validates the path, then the query
	}
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return nil, nil, true
	}
	id, ok := rowID(v)
	if !ok {
		server.Refuse(w, notFound("Datasource not found"))
		return nil, nil, true
	}
	ds, err := scanDatasource(d.DB.QueryRow(r.Context(), `SELECT `+datasourceColumns+` FROM datasources WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		server.Refuse(w, notFound("Datasource not found"))
		return nil, nil, true
	}
	if err != nil {
		fail500(d, w, "contracts", err)
		return nil, nil, true
	}
	checker := perm.For(d.DB, &p.User)
	if ref, err := checker.Ensure(r.Context(), ds.ProjectID, needed); refuse(d, w, ref, err) {
		return nil, nil, true
	}
	return &ds, checker, false
}

// contractOrRefuse is _contract: the 404 when the datasource has none.
func contractOrRefuse(d *Deps, w http.ResponseWriter, ctx context.Context, dsID int64) (*contractRow, bool) {
	c, err := contractOf(ctx, d.DB, dsID)
	if err != nil {
		fail500(d, w, "contracts", err)
		return nil, true
	}
	if c == nil {
		server.Refuse(w, notFound("This datasource has no data contract"))
		return nil, true
	}
	return c, false
}

// getContract is GET /datasources/{ds_id}/contract: whoever only reads gets no addresses.
func getContract(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ds, checker, done := contractDatasource(d, w, r, p, nil, perm.View)
		if done {
			return
		}
		c, done := contractOrRefuse(d, w, r.Context(), ds.ID)
		if done {
			return
		}
		out, err := contractOut(c, nil)
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		if out.Editable, err = checker.Has(r.Context(), ds.ProjectID, perm.Edit); err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		if !out.Editable {
			out.NotifyEmails, out.NotifyConnectionID = "", nil
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// contractHistory is GET /datasources/{ds_id}/contract/history (limit clamped to 1…200).
func contractHistory(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var limitV, withReportV any
		ds, _, done := contractDatasource(d, w, r, p, func(ps *pyd.Params) {
			query := pyd.ParseQuery(r.URL.RawQuery)
			limitV = ps.Query(query, "limit", pyd.Int(), 30)
			withReportV = ps.Query(query, "with_report", pyd.Bool(), false)
		}, perm.View)
		if done {
			return
		}
		limit := int64(200)
		switch n := limitV.(type) {
		case int64:
			limit = max(1, min(n, 200))
		case *big.Int:
			if n.Sign() < 0 {
				limit = 1
			}
		}
		rows, err := d.DB.Query(r.Context(), `SELECT id, contract_version, trigger, run_id, outcome, blocked, errors, warnings, rows,
			evaluated_at, report FROM data_contract_results WHERE datasource_id = $1 ORDER BY evaluated_at DESC, id DESC LIMIT $2`, ds.ID, limit)
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		defer rows.Close()
		out := []ContractResultOut{}
		for rows.Next() {
			var o ContractResultOut
			var at time.Time
			var report string
			if err := rows.Scan(&o.ID, &o.ContractVersion, &o.Trigger, &o.RunID, &o.Outcome, &o.Blocked, &o.Errors, &o.Warnings,
				&o.Rows, &at, &report); err != nil {
				fail500(d, w, "contracts", err)
				return
			}
			o.EvaluatedAt = pyjson.Isoformat(at)
			if withReportV.(bool) {
				if o.Report, err = dictOrFail(contractLoads(&report), true); err != nil {
					fail500(d, w, "contracts", err)
					return
				}
			}
			out = append(out, o)
		}
		if err := rows.Err(); err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// deleteContract is DELETE /datasources/{ds_id}/contract: the history stays.
func deleteContract(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ds, _, done := contractDatasource(d, w, r, p, nil, perm.Edit)
		if done {
			return
		}
		ctx := r.Context()
		if _, done := contractOrRefuse(d, w, ctx, ds.ID); done {
			return
		}
		if _, err := d.DB.Exec(ctx, `DELETE FROM data_contracts WHERE datasource_id = $1`, ds.ID); err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		kind := "datasource"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "contract.delete", TargetType: &kind, TargetID: &ds.ID,
			TargetLabel: &ds.Name})
		returnsNone(w)
	}
}

// contractSnapshot is _snapshot: what the engine evaluates.
func contractSnapshot(ds *datasourceRow) pyjson.Object {
	produced := ds.RefreshedAt
	if produced == nil {
		produced = &ds.UpdatedAt
	}
	return pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}, {Key: "snapshot_at", Value: *isoPtr(produced)}}
}

// askContractEngine is _ask_engine: (answer, "") or (nil, why there is none). A success
// whose body is not JSON is the exception Python does not catch (err).
func askContractEngine(ctx context.Context, d *Deps, path string, body pyjson.Object) (any, *string, error) {
	raw, _ := pyjson.Dumps(body, pyjson.Options{})
	ctx, cancel := context.WithTimeout(ctx, contractEngineWait)
	defer cancel()
	req, err := d.Engine.Request(ctx, http.MethodPost, path, bytes.NewReader([]byte(raw)))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := d.Engine.Do(req)
	var answer []byte
	if err == nil {
		var b bytes.Buffer
		_, err = b.ReadFrom(res.Body)
		res.Body.Close()
		answer = b.Bytes()
	}
	if err != nil {
		d.Log.Warn("engine not reachable", "path", path, "err", err)
		why := "The engine cannot be reached"
		return nil, &why, nil
	}
	if res.StatusCode >= 400 {
		why := engineText(answer, 300)
		if v, err := pyd.LoadsStr(string(answer)); err == nil {
			if o, ok := v.(*pyd.Object); ok {
				detail, _ := o.Get("detail")
				if pyd.Truthy(detail) {
					why = engineText([]byte(pyd.PyStr(detail)), 300)
				}
			}
		}
		return nil, &why, nil
	}
	v, err := pyd.LoadsStr(string(answer))
	if err != nil {
		return nil, nil, err
	}
	return v, nil, nil
}

// pyInt is int(x) of a json.loads value: an int, a float truncated, a numeric text.
func pyInt(v any) (int64, bool) {
	switch x := v.(type) {
	case nil:
		return 0, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case int64:
		return x, true
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return 0, false
		}
		return int64(x), true
	case string:
		n, err := strconv.ParseInt(pyd.Strip(x), 10, 64)
		return n, err == nil
	}
	return 0, false
}

// recordResult is services/contracts.py record_result for a check asked by hand or at the
// save (never blocked, never notified: neither trigger notifies), in the caller's transaction.
func recordResult(ctx context.Context, tx pgx.Tx, c *contractRow, report *pyd.Object, trigger, snapshotKey string) error {
	o, _ := toPyjson(report).(pyjson.Object)
	_, err := recordContractResult(ctx, tx, c, o, trigger, nil, snapshotKey, false)
	return err
}

// checkContract is POST /datasources/{ds_id}/contract/check: the contract checked now on
// the data the datasource serves; the report kept only if the contract did not change.
func checkContract(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ds, _, done := contractDatasource(d, w, r, p, nil, perm.Edit)
		if done {
			return
		}
		ctx := r.Context()
		c, done := contractOrRefuse(d, w, ctx, ds.ID)
		if done {
			return
		}
		snap := contractSnapshot(ds)
		document := contractDocument(c.Document)
		report, why, err := askContractEngine(ctx, d, "/contracts/evaluate", append(snap, pyjson.KV{Key: "document", Value: toPyjson(document)}))
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		c, done = contractOrRefuse(d, w, ctx, ds.ID)
		if done {
			return
		}
		now := contractLoads(&c.Document)
		if !pyd.Truthy(now) {
			now = pyd.NewObject()
		}
		if report != nil && pyEqual(now, document) {
			rep, ok := report.(*pyd.Object)
			if !ok { // report.get on something that is not a dict: AttributeError
				server.InternalError(w)
				return
			}
			tx, err := d.DB.Begin(ctx)
			if err != nil {
				fail500(d, w, "contracts", err)
				return
			}
			defer tx.Rollback(ctx)
			if err := recordResult(ctx, tx, c, rep, "manual", ds.Key); err != nil {
				fail500(d, w, "contracts", err)
				return
			}
			if err := tx.Commit(ctx); err != nil {
				fail500(d, w, "contracts", err)
				return
			}
			if c, err = contractOf(ctx, d.DB, ds.ID); err != nil || c == nil {
				fail500(d, w, "contracts", err)
				return
			}
		}
		out, err := contractOut(c, why)
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// proposeContract is POST /datasources/{ds_id}/contract/proposal: a contract proposed from
// the current data, not saved.
func proposeContract(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ds, _, done := contractDatasource(d, w, r, p, nil, perm.Edit)
		if done {
			return
		}
		answer, why, err := askContractEngine(r.Context(), d, "/contracts/profile",
			pyjson.Object{{Key: "bucket", Value: ds.Bucket}, {Key: "key", Value: ds.Key}})
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		if answer == nil {
			detail := "Data profile not available"
			if why != nil && *why != "" {
				detail = *why
			}
			server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: detail})
			return
		}
		o, ok := answer.(*pyd.Object)
		if !ok {
			server.InternalError(w)
			return
		}
		proposal, _ := o.Get("proposal")
		if !pyd.Truthy(proposal) {
			proposal = emptyRules()
		}
		profile, _ := o.Get("profile")
		out := pyd.NewObject()
		out.Set("document", proposal)
		out.Set("profile", profile)
		b, err := pyd.Dumps(out)
		if err != nil {
			server.InternalError(w)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(b)
	}
}
