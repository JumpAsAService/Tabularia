package api

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/webhooks"
)

// routes/webhooks.py: only the administrator creates and changes a webhook; the observer
// sees that they exist and whether they deliver, not where nor with what secret.
func init() {
	register("webhooks", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/webhooks/events", Handler: adminRole(d, "/webhooks/events", false, webhookEvents)},
			{Method: http.MethodGet, Pattern: "/webhooks", Handler: adminRole(d, "/webhooks", false, listWebhooks(d))},
			{Method: http.MethodPost, Pattern: "/webhooks", Handler: withBody(d, "/webhooks", createWebhook(d))},
			{Method: http.MethodPatch, Pattern: "/webhooks/{wid}", Handler: withBody(d, "/webhooks/{wid}", updateWebhook(d))},
			{Method: http.MethodPost, Pattern: "/webhooks/{wid}/rotate-secret", Handler: adminRole(d, "/webhooks/{wid}/rotate-secret", true, rotateWebhookSecret(d))},
			{Method: http.MethodDelete, Pattern: "/webhooks/{wid}", Handler: adminRole(d, "/webhooks/{wid}", true, deleteWebhook(d))},
			{Method: http.MethodPost, Pattern: "/webhooks/{wid}/test", Handler: adminRole(d, "/webhooks/{wid}/test", true, testWebhook(d))},
			{Method: http.MethodGet, Pattern: "/webhooks/{wid}/deliveries", Handler: adminRole(d, "/webhooks/{wid}/deliveries", false, listDeliveries(d))},
			{Method: http.MethodPost, Pattern: "/webhooks/deliveries/{did}/redeliver", Handler: adminRole(d, "/webhooks/deliveries/{did}/redeliver", true, redeliver(d))},
		}
	})
}

// bodyHandler is a route with a JSON body: the body as FastAPI read it, and its raw bytes
// (to hand the request to Python when only Python can answer it).
type bodyHandler = func(http.ResponseWriter, *http.Request, *auth.Principal, pyd.Body, []byte)

// withBody reads the body before the authentication (a broken JSON is a 422 for anyone,
// as FastAPI does), then require_superuser.
func withBody(d *Deps, route string, h bodyHandler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		body, early := pyd.ReadBody(raw, r.Header.Get("Content-Type"))
		if early != nil {
			early.Write(w)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(raw))
		adminRole(d, route, true, func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
			h(w, r, p, body, raw)
		}).ServeHTTP(w, r)
	})
}

func webhookEvents(w http.ResponseWriter, _ *http.Request, _ *auth.Principal) {
	server.JSON(w, http.StatusOK, webhooks.Events)
}

// WebhookOut is routes/webhooks.py WebhookOut; Secret only in WebhookWithSecret.
type WebhookOut struct {
	ID                 int64    `json:"id"`
	Name               string   `json:"name"`
	URL                string   `json:"url"`
	Events             []string `json:"events"`
	ProjectID          *int64   `json:"project_id"`
	ProjectName        *string  `json:"project_name"`
	Active             bool     `json:"active"`
	CreatedAt          string   `json:"created_at"`
	LastDeliveryStatus *string  `json:"last_delivery_status"`
	LastDeliveryAt     *string  `json:"last_delivery_at"`
}

type webhookWithSecret struct {
	WebhookOut
	Secret string `json:"secret"`
}

type webhookRow struct {
	id        int64
	name, url string
	events    string
	project   *int64
	active    bool
	created   time.Time
}

const webhookColumns = `id, name, url, events, project_id, active, created_at`

func scanWebhook(row pgx.Row) (webhookRow, error) {
	var x webhookRow
	err := row.Scan(&x.id, &x.name, &x.url, &x.events, &x.project, &x.active, &x.created)
	return x, err
}

// webhookOut is _out; ok=false for an address only Python reads faithfully.
func webhookOut(ctx context.Context, d *Deps, x webhookRow, full bool) (WebhookOut, bool, error) {
	out := WebhookOut{ID: x.id, Name: x.name, URL: x.url, Events: webhooks.EventsOf(x.events), ProjectID: x.project,
		Active: x.active, CreatedAt: pyjson.Isoformat(x.created)}
	var status *string
	var delivered *time.Time
	var created time.Time
	err := d.DB.QueryRow(ctx, `SELECT status, delivered_at, created_at FROM webhook_deliveries WHERE webhook_id = $1
		ORDER BY id DESC LIMIT 1`, x.id).Scan(&status, &delivered, &created)
	switch {
	case err == nil:
		out.LastDeliveryStatus = status
		if delivered == nil {
			delivered = &created
		}
		out.LastDeliveryAt = utcISO(delivered)
	case !errors.Is(err, pgx.ErrNoRows):
		return out, true, err
	}
	if x.project != nil && *x.project != 0 {
		var name string
		err := d.DB.QueryRow(ctx, `SELECT name FROM projects WHERE id = $1`, *x.project).Scan(&name)
		if err == nil {
			out.ProjectName = &name
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return out, true, err
		}
	}
	if !full {
		parts, err := pyurl.URLSplit(x.url)
		if err != nil {
			return out, false, nil
		}
		out.URL = parts.Scheme + "://" + parts.HostnameOrNone() + "/…"
	}
	return out, true, nil
}

// listWebhooks is GET /webhooks, by name.
func listWebhooks(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT `+webhookColumns+` FROM webhooks ORDER BY name`)
		if err != nil {
			adminFail(d, w, "reading the webhooks", err)
			return
		}
		list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (webhookRow, error) { return scanWebhook(row) })
		if err != nil {
			adminFail(d, w, "reading the webhooks", err)
			return
		}
		out := []WebhookOut{}
		for _, x := range list {
			o, ok, err := webhookOut(ctx, d, x, full)
			if err != nil {
				adminFail(d, w, "reading the webhooks", err)
				return
			}
			if !ok {
				forward(d, w, r, nil)
				return
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// webhookEventsText is _events: the events known, without repeats, at least one, every one
// known; stored as json.dumps of the list.
func webhookEventsText(items []any) (string, bool) {
	seen := map[string]bool{}
	var valid []string
	for _, it := range items {
		s := it.(string)
		if seen[s] {
			continue
		}
		seen[s] = true
		for _, e := range webhooks.Events {
			if s == e {
				valid = append(valid, s)
				break
			}
		}
	}
	if len(valid) == 0 || len(valid) != len(seen) {
		return "", false
	}
	list := make([]any, len(valid))
	for i, v := range valid {
		list[i] = v
	}
	return pyjson.Audit(list), true
}

func refuseEvents(w http.ResponseWriter) {
	adminDetail(w, http.StatusUnprocessableEntity, "Allowed events: "+strings.Join(webhooks.Events, ", ")+" (at least one)")
}

// folderExists is _folder's check: a folder id past the column's range is one no folder has.
func folderExists(ctx context.Context, d *Deps, v any) (bool, error) {
	id, ok := int4(v)
	if !ok {
		return false, nil
	}
	var n int64
	err := d.DB.QueryRow(ctx, `SELECT id FROM projects WHERE id = $1`, id).Scan(&n)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func webhookName(v any) (string, bool) {
	name := pyd.Strip(v.(string))
	return name, name != "" && pyd.Len(name) <= 120
}

// createWebhook is POST /webhooks.
func createWebhook(d *Deps) bodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.WebhookIn)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		name, ok := webhookName(b.Get("name"))
		if !ok {
			adminDetail(w, http.StatusUnprocessableEntity, "A name is required (at most 120 characters)")
			return
		}
		secret := webhooks.NewSecret()
		url, err := webhooks.ValidateURL(b.Get("url").(string))
		if errors.Is(err, webhooks.ErrNotASCII) {
			forward(d, w, r, raw)
			return
		}
		if err != nil {
			adminDetail(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		enc, err := d.Fernet.Encrypt(secret)
		if err != nil {
			adminFail(d, w, "encrypting the secret", err)
			return
		}
		events, ok := webhookEventsText(b.Get("events").([]any))
		if !ok {
			refuseEvents(w)
			return
		}
		var project *int64
		if v := b.Get("project_id"); v != nil {
			exists, err := folderExists(ctx, d, v)
			if err != nil {
				adminFail(d, w, "reading the folder", err)
				return
			}
			if !exists {
				adminDetail(w, http.StatusUnprocessableEntity, "The folder does not exist")
				return
			}
			id := v.(int64)
			project = &id
		}
		x, err := scanWebhook(d.DB.QueryRow(ctx, `INSERT INTO webhooks (name, url, secret_enc, events, project_id, active,
			created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING `+webhookColumns,
			name, url, enc, events, project, b.Get("active").(bool), p.User.ID, nowNaive()))
		if err != nil {
			adminFail(d, w, "saving the webhook", err)
			return
		}
		tt := "webhook"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "webhook.create", TargetType: &tt,
			TargetID: &x.id, TargetLabel: &x.name, Detail: pyjson.Object{
				{Key: "host", Value: hostOf(x.url)}, {Key: "events", Value: stringsAny(webhooks.EventsOf(x.events))},
				{Key: "project_id", Value: int64OrNil(x.project)}}})
		o, _, err := webhookOut(ctx, d, x, true)
		if err != nil {
			adminFail(d, w, "reading the webhook", err)
			return
		}
		server.JSON(w, http.StatusCreated, webhookWithSecret{o, secret})
	}
}

// hostOf is urlsplit(url).hostname (None when there is none).
func hostOf(url string) any {
	parts, err := pyurl.URLSplit(url)
	if err != nil {
		return nil
	}
	if h, ok := parts.Hostname(); ok {
		return h
	}
	return nil
}

func stringsAny(s []string) []any {
	out := make([]any, len(s))
	for i, v := range s {
		out[i] = v
	}
	return out
}

func int64OrNil(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

// pathID is an int path parameter that matched: past the id columns' range it is an id no
// row has (psycopg2 writes it as a literal).
func pathID(v any) int64 {
	if id, ok := int4(v); ok {
		return id
	}
	return -1
}

func readWebhook(ctx context.Context, d *Deps, id int64) (webhookRow, bool, error) {
	x, err := scanWebhook(d.DB.QueryRow(ctx, `SELECT `+webhookColumns+` FROM webhooks WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return x, false, nil
	}
	return x, err == nil, err
}

// updateWebhook is PATCH /webhooks/{wid}: only what is given; nothing is written when a
// check refuses.
func updateWebhook(d *Deps) bodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		var ps pyd.Params
		wid := ps.Path("wid", r.PathValue("wid"), pyd.Int())
		b := ps.Body(body, gatewaymodels.WebhookPatch)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		x, found, err := readWebhook(ctx, d, pathID(wid))
		if err != nil {
			adminFail(d, w, "reading the webhook", err)
			return
		}
		if !found {
			adminDetail(w, http.StatusNotFound, "Webhook not found")
			return
		}
		changes := pyjson.Object{}
		if v := b.Get("name"); v != nil {
			name, ok := webhookName(v)
			if !ok {
				adminDetail(w, http.StatusUnprocessableEntity, "A name is required (at most 120 characters)")
				return
			}
			x.name, changes = name, changes.Set("name", name)
		}
		if v := b.Get("url"); v != nil {
			url, err := webhooks.ValidateURL(v.(string))
			if errors.Is(err, webhooks.ErrNotASCII) {
				forward(d, w, r, raw)
				return
			}
			if err != nil {
				adminDetail(w, http.StatusUnprocessableEntity, err.Error())
				return
			}
			x.url, changes = url, changes.Set("host", hostOf(url))
		}
		if v := b.Get("events"); v != nil {
			events, ok := webhookEventsText(v.([]any))
			if !ok {
				refuseEvents(w)
				return
			}
			x.events, changes = events, changes.Set("events", stringsAny(webhooks.EventsOf(events)))
		}
		if b.Get("clear_project").(bool) {
			x.project, changes = nil, changes.Set("project_id", nil)
		} else if v := b.Get("project_id"); v != nil {
			exists, err := folderExists(ctx, d, v)
			if err != nil {
				adminFail(d, w, "reading the folder", err)
				return
			}
			if !exists {
				adminDetail(w, http.StatusUnprocessableEntity, "The folder does not exist")
				return
			}
			id := v.(int64)
			x.project, changes = &id, changes.Set("project_id", id)
		}
		if v, ok := b.Get("active").(bool); ok {
			x.active, changes = v, changes.Set("active", v)
		}
		if _, err := d.DB.Exec(ctx, `UPDATE webhooks SET name = $1, url = $2, events = $3, project_id = $4, active = $5
			WHERE id = $6`, x.name, x.url, x.events, x.project, x.active, x.id); err != nil {
			adminFail(d, w, "saving the webhook", err)
			return
		}
		if len(changes) > 0 {
			tt := "webhook"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "webhook.update", TargetType: &tt,
				TargetID: &x.id, TargetLabel: &x.name, Detail: changes})
		}
		o, _, err := webhookOut(ctx, d, x, true)
		if err != nil {
			adminFail(d, w, "reading the webhook", err)
			return
		}
		server.JSON(w, http.StatusOK, o)
	}
}

// oneWebhook is the path's wid validated, then _one.
func oneWebhook(d *Deps, w http.ResponseWriter, r *http.Request, ps *pyd.Params) (webhookRow, bool) {
	wid := ps.Path("wid", r.PathValue("wid"), pyd.Int())
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return webhookRow{}, false
	}
	x, found, err := readWebhook(r.Context(), d, pathID(wid))
	if err != nil {
		adminFail(d, w, "reading the webhook", err)
		return x, false
	}
	if !found {
		adminDetail(w, http.StatusNotFound, "Webhook not found")
		return x, false
	}
	return x, true
}

// rotateWebhookSecret is POST /webhooks/{wid}/rotate-secret.
func rotateWebhookSecret(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		x, ok := oneWebhook(d, w, r, &ps)
		if !ok {
			return
		}
		ctx := r.Context()
		secret := webhooks.NewSecret()
		enc, err := d.Fernet.Encrypt(secret)
		if err != nil {
			adminFail(d, w, "encrypting the secret", err)
			return
		}
		if _, err := d.DB.Exec(ctx, `UPDATE webhooks SET secret_enc = $1 WHERE id = $2`, enc, x.id); err != nil {
			adminFail(d, w, "saving the webhook", err)
			return
		}
		tt := "webhook"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "webhook.update", TargetType: &tt,
			TargetID: &x.id, TargetLabel: &x.name, Detail: pyjson.Object{{Key: "secret", Value: "rotated"}}})
		o, _, err := webhookOut(ctx, d, x, true)
		if err != nil {
			adminFail(d, w, "reading the webhook", err)
			return
		}
		server.JSON(w, http.StatusOK, webhookWithSecret{o, secret})
	}
}

// deleteWebhook is DELETE /webhooks/{wid}: with its deliveries.
func deleteWebhook(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		x, ok := oneWebhook(d, w, r, &ps)
		if !ok {
			return
		}
		ctx := r.Context()
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			adminFail(d, w, "deleting the webhook", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		if _, err = tx.Exec(ctx, `DELETE FROM webhook_deliveries WHERE webhook_id = $1`, x.id); err == nil {
			if _, err = tx.Exec(ctx, `DELETE FROM webhooks WHERE id = $1`, x.id); err == nil {
				err = tx.Commit(ctx)
			}
		}
		if err != nil {
			adminFail(d, w, "deleting the webhook", err)
			return
		}
		tt := "webhook"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "webhook.delete", TargetType: &tt,
			TargetID: &x.id, TargetLabel: &x.name})
		adminNone(w)
	}
}

// DeliveryOut is routes/webhooks.py DeliveryOut.
type DeliveryOut struct {
	ID             int64   `json:"id"`
	Event          string  `json:"event"`
	Status         string  `json:"status"`
	Attempts       int64   `json:"attempts"`
	CreatedAt      string  `json:"created_at"`
	NextAttemptAt  *string `json:"next_attempt_at"`
	DeliveredAt    *string `json:"delivered_at"`
	LastStatusCode *int64  `json:"last_status_code"`
	LastError      *string `json:"last_error"`
	Payload        *string `json:"payload"`
}

const deliveryColumns = `id, webhook_id, event, status, attempts, created_at, next_attempt_at, delivered_at, last_status_code,
	last_error, payload`

type deliveryRow struct {
	out     DeliveryOut
	webhook int64
	payload string
}

func scanDelivery(row pgx.Row) (deliveryRow, error) {
	var x deliveryRow
	var created time.Time
	var next, delivered *time.Time
	err := row.Scan(&x.out.ID, &x.webhook, &x.out.Event, &x.out.Status, &x.out.Attempts, &created, &next, &delivered,
		&x.out.LastStatusCode, &x.out.LastError, &x.payload)
	x.out.CreatedAt, x.out.NextAttemptAt, x.out.DeliveredAt = pyjson.Isoformat(created), utcISO(next), utcISO(delivered)
	return x, err
}

// deliveryOut is _delivery_out: the body sent and the receiver's answer only to whoever is
// in charge.
func deliveryOut(x deliveryRow, full bool) DeliveryOut {
	o := x.out
	if full {
		o.Payload = &x.payload
	} else {
		o.LastError = nil
	}
	return o
}

// newUUID4 is str(uuid.uuid4()).
func newUUID4() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// isoZ is webhooks._iso: naive UTC with «Z».
func isoZ(t time.Time) string {
	return strings.Replace(pyjson.Isoformat(t), "+00:00", "Z", 1)
}

func insertDelivery(ctx context.Context, d *Deps, webhook int64, event, payload string, at time.Time) (deliveryRow, error) {
	return scanDelivery(d.DB.QueryRow(ctx, `INSERT INTO webhook_deliveries (webhook_id, event, payload, status, attempts,
		next_attempt_at, created_at) VALUES ($1, $2, $3, 'pending', 0, $4, $4) RETURNING `+deliveryColumns,
		webhook, event, payload, at))
}

// testWebhook is POST /webhooks/{wid}/test: a `ping` queued (enqueue_ping).
func testWebhook(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		x, ok := oneWebhook(d, w, r, &ps)
		if !ok {
			return
		}
		at := nowNaive()
		payload := pyjson.Object{{Key: "id", Value: newUUID4()}, {Key: "event", Value: webhooks.Ping},
			{Key: "created_at", Value: isoZ(at)}, {Key: "data", Value: pyjson.Object{
				{Key: "webhook", Value: pyjson.Object{{Key: "id", Value: x.id}, {Key: "name", Value: x.name}}},
				{Key: "message", Value: "Tabularia webhook test"}}}}
		text, err := pyjson.Dumps(payload, pyjson.Options{Compact: true})
		if err != nil {
			adminFail(d, w, "the ping's body", err)
			return
		}
		row, err := insertDelivery(r.Context(), d, x.id, webhooks.Ping, text, at)
		if err != nil {
			adminFail(d, w, "queueing the ping", err)
			return
		}
		server.JSON(w, http.StatusAccepted, deliveryOut(row, true))
	}
}

// listDeliveries is GET /webhooks/{wid}/deliveries, the newest first.
func listDeliveries(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		wid := ps.Path("wid", r.PathValue("wid"), pyd.Int())
		limit := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "limit", pyd.Int().Ge(1).Le(200), int64(50))
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		x, found, err := readWebhook(ctx, d, pathID(wid))
		if err != nil {
			adminFail(d, w, "reading the webhook", err)
			return
		}
		if !found {
			adminDetail(w, http.StatusNotFound, "Webhook not found")
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		rows, err := d.DB.Query(ctx, `SELECT `+deliveryColumns+` FROM webhook_deliveries WHERE webhook_id = $1
			ORDER BY id DESC LIMIT $2`, x.id, limit.(int64))
		if err != nil {
			adminFail(d, w, "reading the deliveries", err)
			return
		}
		list, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (deliveryRow, error) { return scanDelivery(row) })
		if err != nil {
			adminFail(d, w, "reading the deliveries", err)
			return
		}
		out := make([]DeliveryOut, len(list))
		for i, x := range list {
			out[i] = deliveryOut(x, full)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// redeliver is POST /webhooks/deliveries/{did}/redeliver: a new delivery with the same
// content and a new id (resend).
func redeliver(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		var ps pyd.Params
		did := ps.Path("did", r.PathValue("did"), pyd.Int())
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		x, err := scanDelivery(d.DB.QueryRow(ctx, `SELECT `+deliveryColumns+` FROM webhook_deliveries WHERE id = $1`, pathID(did)))
		if errors.Is(err, pgx.ErrNoRows) {
			adminDetail(w, http.StatusNotFound, "Delivery not found")
			return
		}
		if err != nil {
			adminFail(d, w, "reading the delivery", err)
			return
		}
		v, err := pyjson.Loads([]byte(x.payload))
		body, isObj := v.(pyjson.Object)
		if err != nil || !isObj {
			server.InternalError(w) // json.loads failing, or `body["id"] = …` on what is not a dict
			return
		}
		body = body.Set("id", newUUID4())
		text, err := pyjson.Dumps(body, pyjson.Options{Compact: true})
		if err != nil {
			server.InternalError(w)
			return
		}
		row, err := insertDelivery(ctx, d, x.webhook, x.out.Event, text, nowNaive())
		if err != nil {
			adminFail(d, w, "queueing the delivery", err)
			return
		}
		server.JSON(w, http.StatusAccepted, deliveryOut(row, true))
	}
}
