package api

import (
	"context"
	"errors"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/contracts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/contracts.py: writing a datasource's data contract (put_contract) and its export in
// the ODCS format (export_odcs). The form of the document: internal/contracts.

func init() {
	register("contracts_write", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodPut, Pattern: "/datasources/{ds_id}/contract",
				Handler: withBody(server.Authed(d.Auth, "/datasources/{ds_id}/contract", putContract(d)))},
			{Method: http.MethodGet, Pattern: "/datasources/{ds_id}/contract/odcs",
				Handler: server.Authed(d.Auth, "/datasources/{ds_id}/contract/odcs", exportODCS(d))},
		}
	})
}

// contractDatasourceByID is _datasource after the validation: 404, then the capability.
func contractDatasourceByID(d *Deps, w http.ResponseWriter, ctx context.Context, p *auth.Principal, v any,
	needed perm.Capability) (*datasourceRow, bool) {
	id, ok := rowID(v)
	if !ok {
		server.Refuse(w, notFound("Datasource not found"))
		return nil, true
	}
	ds, err := scanDatasource(d.DB.QueryRow(ctx, `SELECT `+datasourceColumns+` FROM datasources WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		server.Refuse(w, notFound("Datasource not found"))
		return nil, true
	}
	if err != nil {
		fail500(d, w, "contracts", err)
		return nil, true
	}
	if ref, err := perm.For(d.DB, &p.User).Ensure(ctx, ds.ProjectID, needed); refuse(d, w, ref, err) {
		return nil, true
	}
	return &ds, false
}

// smtpConnection is notify_targets.smtp_connection: an SMTP connection the person may use.
func smtpConnection(ctx context.Context, d *Deps, p *auth.Principal, id int64) (*connection, *auth.Refusal, error) {
	c, err := connectionRow(ctx, d, id)
	if err != nil {
		return nil, nil, err
	}
	if c == nil || c.DBType != "smtp" {
		return nil, &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: "An SMTP connection is required"}, nil
	}
	ref, err := perm.For(d.DB, &p.User).Ensure(ctx, c.ProjectID, perm.Connect)
	return c, ref, err
}

func connectionRow(ctx context.Context, d *Deps, id int64) (*connection, error) {
	var c connection
	err := d.DB.QueryRow(ctx, `SELECT id, name, project_id, db_type, extra FROM connections WHERE id = $1`, id).
		Scan(&c.ID, &c.Name, &c.ProjectID, &c.DBType, &c.Extra)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &c, err
}

// contractNotify is _notify_targets' answer: whom to notify, checked.
type contractNotify struct {
	emails *string
	connID *int64
}

// contractNotifyTargets is _notify_targets: nil = do not touch them.
func contractNotifyTargets(ctx context.Context, d *Deps, p *auth.Principal, dsID int64, emailsIn *string,
	connIn *int64) (*contractNotify, *auth.Refusal, error) {
	if emailsIn == nil && connIn == nil {
		return nil, nil, nil
	}
	current, err := contractOf(ctx, d.DB, dsID)
	if err != nil {
		return nil, nil, err
	}
	var beforeEmails *string
	var beforeConn *int64
	if current != nil {
		beforeEmails, beforeConn = current.NotifyEmails, current.NotifyConnectionID
	}
	emails, conn := beforeEmails, beforeConn
	if emailsIn != nil {
		list, ref := parseRecipients(*emailsIn)
		if ref != nil {
			return nil, ref, nil
		}
		emails = nil
		if joined := strings.Join(list, ", "); joined != "" {
			emails = &joined
		}
	}
	if connIn != nil {
		conn = connIn
		if *connIn == 0 || *connIn == -1 {
			conn = nil
		}
	}
	same := func(a, b *string) bool { return (a == nil) == (b == nil) && (a == nil || *a == *b) }
	sameID := func(a, b *int64) bool { return (a == nil) == (b == nil) && (a == nil || *a == *b) }
	hasEmails := emails != nil && *emails != ""
	hasConn := conn != nil && *conn != 0
	switch {
	case hasEmails && hasConn:
		if !same(emails, beforeEmails) || !sameID(conn, beforeConn) {
			if _, ref, err := smtpConnection(ctx, d, p, *conn); ref != nil || err != nil {
				return nil, ref, err
			}
		}
		c, err := connectionRow(ctx, d, *conn)
		if err != nil {
			return nil, nil, err
		}
		if ref := ensureDeliverable(c, strings.Split(*emails, ", ")); ref != nil {
			return nil, ref, nil
		}
	case hasConn && !sameID(conn, beforeConn):
		if _, ref, err := smtpConnection(ctx, d, p, *conn); ref != nil || err != nil {
			return nil, ref, err
		}
	}
	return &contractNotify{emails, conn}, nil, nil
}

// putContract is PUT /datasources/{ds_id}/contract: creates or replaces the contract, then
// checks it on the current data (a check that blocks nothing).
func putContract(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		idV := ps.Path("ds_id", r.PathValue("ds_id"), pyd.Int())
		body := ps.Body(bodyOf(r), gatewaymodels.ContractIn)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		docIn := body.Get("document").(*pyd.Object)
		document, err := contracts.ValidateDocument(ctx, docIn, d.SQL.Compiles)
		var invalid *contracts.Invalid
		if errors.As(err, &invalid) {
			errs := make([]any, len(invalid.Errors))
			for i, e := range invalid.Errors {
				errs[i] = e
			}
			writePyStatus(w, http.StatusUnprocessableEntity, pyjson.Object{{Key: "detail", Value: pyjson.Object{
				{Key: "message", Value: "The contract is not valid"}, {Key: "errors", Value: errs}}}})
			return
		}
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		ds, done := contractDatasourceByID(d, w, ctx, p, idV, perm.Edit)
		if done {
			return
		}
		notify, ref, err := contractNotifyTargets(ctx, d, p, ds.ID, optStr(body, "notify_emails"), optInt(body, "notify_connection_id"))
		if refuse(d, w, ref, err) {
			return
		}
		enabled := body.Get("enabled").(bool)
		c, toCheck, err := saveContract(ctx, d, ds, document, enabled, p.User.ID, notify)
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		snap := contractSnapshot(ds)
		rules, _ := document.Get("rules")
		kind := "datasource"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "contract.save", TargetType: &kind, TargetID: &ds.ID,
			TargetLabel: &ds.Name, Detail: pyjson.Object{{Key: "version", Value: c.Version}, {Key: "rules", Value: len(rules.([]any))},
				{Key: "enabled", Value: c.Enabled}, {Key: "notify", Value: c.NotifyEmails != nil && *c.NotifyEmails != "" &&
					c.NotifyConnectionID != nil && *c.NotifyConnectionID != 0}}})
		var report any
		var why *string
		if toCheck {
			if report, why, err = askContractEngine(ctx, d, "/contracts/evaluate",
				append(snap, pyjson.KV{Key: "document", Value: contracts.PyjsonOf(document)})); err != nil {
				fail500(d, w, "contracts", err)
				return
			}
		}
		c, done = contractOrRefuse(d, w, ctx, ds.ID)
		if done {
			return
		}
		if report != nil {
			now, err := pyd.LoadsStr(c.Document)
			if err != nil {
				server.InternalError(w)
				return
			}
			if pyEqual(now, document) {
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
				if err := recordResult(ctx, tx, c, rep, "save", ds.Key); err != nil {
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
		}
		out, err := contractOut(c, why)
		if err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// saveContract is contracts.save and the commit after it. Answers the contract saved and
// whether it is to be checked now (on, «to check», with rules).
func saveContract(ctx context.Context, d *Deps, ds *datasourceRow, document *pyd.Object, enabled bool, userID int64,
	notify *contractNotify) (*contractRow, bool, error) {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback(ctx)
	var n *contracts.Notify
	if notify != nil {
		n = &contracts.Notify{Emails: notify.emails, ConnectionID: notify.connID}
	}
	if _, err := contracts.Save(ctx, tx, ds.ID, document, enabled, userID, n); err != nil {
		return nil, false, err
	}
	c, err := contractOf(ctx, tx, ds.ID)
	if err != nil {
		return nil, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, false, err
	}
	rules, _ := document.Get("rules")
	return c, c.Enabled && c.Status == "pending" && len(rules.([]any)) > 0, nil
}

var odcsName = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// exportODCS is GET /datasources/{ds_id}/contract/odcs: the contract as an ODCS YAML file.
func exportODCS(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ds, _, done := contractDatasource(d, w, r, p, nil, perm.View)
		if done {
			return
		}
		ctx := r.Context()
		c, done := contractOrRefuse(d, w, ctx, ds.ID)
		if done {
			return
		}
		var createdAt time.Time
		if err := d.DB.QueryRow(ctx, `SELECT created_at FROM data_contracts WHERE id = $1`, c.ID).Scan(&createdAt); err != nil {
			fail500(d, w, "contracts", err)
			return
		}
		cols, err1 := pyd.LoadsStr(orText(ds.Columns, "[]"))
		descs, err2 := pyd.LoadsStr(orText(ds.ColumnDescriptions, "{}"))
		if err1 != nil || err2 != nil {
			cols, descs = []any{}, pyd.NewObject()
		}
		columns, _ := cols.([]any)
		descriptions, _ := descs.(*pyd.Object)
		if descriptions == nil {
			descriptions = pyd.NewObject()
		}
		document, ok := contractDocument(c.Document).(*pyd.Object)
		if !ok { // document.get on a list: AttributeError
			server.InternalError(w)
			return
		}
		odcs, err := contracts.ToODCS(contracts.Input{DatasourceID: ds.ID, Name: ds.Name, Description: ds.Description,
			Columns: columns, ColumnDescriptions: descriptions, Document: document, Version: c.Version, Enabled: c.Enabled,
			CreatedAt: &createdAt})
		if err != nil {
			server.InternalError(w)
			return
		}
		text, err := contracts.DumpYAML(odcs)
		if err != nil {
			server.InternalError(w)
			return
		}
		name := strings.Trim(odcsName.ReplaceAllString(ds.Name, "_"), "._")
		if name == "" {
			name = "datasource_" + itoa(int(ds.ID))
		}
		w.Header().Set("Content-Type", "application/yaml")
		w.Header().Set("Content-Disposition", `attachment; filename="`+name+`.odcs.yaml"`)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(text))
	}
}

func orText(s, def string) string {
	if s == "" {
		return def
	}
	return s
}
