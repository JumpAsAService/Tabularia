package api

// The keys of a data API (docs/specs/data-apis.md, «Keys»): made, revoked and renewed by
// whoever has MANAGE on its folder (or an administrator), from the interface. A key is shown
// once; the database keeps its sha-256 and its first characters. Routes of the Go gateway only.

import (
	"context"
	"fmt"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// dataAPIKeyOut is a key as the page lists it: never the key, never its hash.
type dataAPIKeyOut struct {
	ID            int64          `json:"id"`
	Name          string         `json:"name"`
	Hint          string         `json:"hint"` // `tdk_` and these 8 characters begin the key
	State         string         `json:"state"`
	ExpiresAt     *string        `json:"expires_at"`
	RatePerMinute int            `json:"rate_per_minute"`
	Resources     []string       `json:"resources"` // [] = every resource of the API
	CIDRs         []string       `json:"cidrs"`     // [] = from any address
	RevokedAt     *string        `json:"revoked_at"`
	GraceUntil    *string        `json:"grace_until"`
	CreatedBy     *dataAPIPerson `json:"created_by"`
	CreatedAt     *string        `json:"created_at"`
	LastUsedAt    *string        `json:"last_used_at"`
	Requests24h   int64          `json:"requests_24h"`
}

func dataAPIKeyOf(ctx context.Context, d *Deps, k *dataapis.Key, requests map[int64]int64, now time.Time) (dataAPIKeyOut, error) {
	out := dataAPIKeyOut{ID: k.ID, Name: k.Name, Hint: k.Hint, State: dataapis.KeyState(k, now), ExpiresAt: isoPtr(&k.ExpiresAt),
		RatePerMinute: k.RatePerMinute, Resources: k.Resources, CIDRs: k.CIDRs, RevokedAt: isoPtr(k.RevokedAt),
		GraceUntil: isoPtr(k.GraceUntil), CreatedAt: isoPtr(&k.CreatedAt), LastUsedAt: isoPtr(k.LastUsedAt), Requests24h: requests[k.ID]}
	if k.CreatedBy != nil {
		u, err := loadUser(ctx, d.DB, *k.CreatedBy)
		if err != nil {
			return out, err
		}
		if u != nil {
			out.CreatedBy = &dataAPIPerson{ID: u.ID, Email: u.Email, FullName: u.FullName}
		}
	}
	return out, nil
}

// fromTheInterface refuses the keys' secrets to an API token: a key is shown once, to a person.
func fromTheInterface(w http.ResponseWriter, p *auth.Principal) bool {
	if p.TokenName != "" {
		detail(w, http.StatusForbidden, "The keys of a data API are made from the interface, not with an API token")
		return false
	}
	return true
}

func auditDataAPIKey(ctx context.Context, d *Deps, actor *auth.User, action string, a *dataapis.API, k *dataapis.Key, more pyjson.Object) {
	auditDataAPI(ctx, d, actor, action, a, append(pyjson.Object{{Key: "key_id", Value: k.ID}, {Key: "key_name", Value: k.Name},
		{Key: "key_hint", Value: k.Hint}}, more...))
}

// GET /data-apis/{api_id}/keys
func listDataAPIKeys(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok {
		return
	}
	ctx := r.Context()
	keys, err := dataapis.Keys(ctx, d.DB, a.ID)
	if err != nil {
		fail(d, w, "data apis: the keys", err)
		return
	}
	requests, err := dataapis.Requests24h(ctx, d.DB, a.ID)
	if err != nil {
		fail(d, w, "data apis: the keys' requests", err)
		return
	}
	now := dataapis.Now()
	out := make([]dataAPIKeyOut, 0, len(keys))
	for _, k := range keys {
		ko, err := dataAPIKeyOf(ctx, d, k, requests, now)
		if err != nil {
			fail(d, w, "data apis: the keys", err)
			return
		}
		out = append(out, ko)
	}
	server.JSON(w, http.StatusOK, out)
}

type dataAPIKeyIn struct {
	Name          string   `json:"name"`
	ExpiresAt     string   `json:"expires_at"`
	RatePerMinute *int     `json:"rate_per_minute"`
	Resources     []string `json:"resources"`
	CIDRs         []string `json:"cidrs"`
}

// checkDataAPIKey reads what a key is made with.
func checkDataAPIKey(ctx context.Context, d *Deps, a *dataapis.API, in dataAPIKeyIn) (dataapis.NewKeyRow, *dataAPIRefusal, error) {
	var n dataapis.NewKeyRow
	bad := func(text string) (dataapis.NewKeyRow, *dataAPIRefusal, error) {
		return n, &dataAPIRefusal{http.StatusUnprocessableEntity, text}, nil
	}
	n.DataAPIID = a.ID
	n.Name = strings.TrimSpace(in.Name)
	if n.Name == "" {
		return bad("A key needs a name: who uses it")
	}
	if len([]rune(n.Name)) > dataapis.MaxKeyName {
		return bad(fmt.Sprintf("The key's name is longer than %d characters", dataapis.MaxKeyName))
	}
	expires, err := dataapis.KeyExpiry(in.ExpiresAt, d.Config.DataAPI.KeyMaxDays, dataapis.Now())
	if err != nil {
		return bad(dataapis.Sentence(err.Error()))
	}
	n.ExpiresAt = expires
	n.RatePerMinute = d.Config.DataAPI.KeyDefaultRate
	if in.RatePerMinute != nil {
		n.RatePerMinute = *in.RatePerMinute
	}
	if n.RatePerMinute < 1 || n.RatePerMinute > dataapis.MaxRate {
		return bad(fmt.Sprintf("The rate limit is from 1 to %d requests a minute", dataapis.MaxRate))
	}
	resources, err := dataapis.Resources(ctx, d.DB, a.ID)
	if err != nil {
		return n, nil, err
	}
	names := make([]string, len(resources))
	for i, r := range resources {
		names[i] = r.Name
	}
	n.Resources = []string{}
	for _, name := range in.Resources {
		name = strings.TrimSpace(name)
		if !slices.Contains(names, name) {
			return bad(fmt.Sprintf("The data API has no resource «%s»", name))
		}
		if !slices.Contains(n.Resources, name) {
			n.Resources = append(n.Resources, name)
		}
	}
	if n.CIDRs, err = dataapis.CIDRs(in.CIDRs); err != nil {
		return bad(dataapis.Sentence(err.Error()))
	}
	return n, nil, nil
}

// POST /data-apis/{api_id}/keys: a new key, shown once in the answer.
func createDataAPIKey(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok || !fromTheInterface(w, p) {
		return
	}
	ctx := r.Context()
	var in dataAPIKeyIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	n, ref, err := checkDataAPIKey(ctx, d, a, in)
	if answerRefusal(d, w, "data apis: the key", ref, err) {
		return
	}
	n.CreatedBy = p.User.ID
	k, secret, err := dataapis.InsertKey(ctx, d.DB, n)
	if err != nil {
		fail(d, w, "data apis: making the key", err)
		return
	}
	auditDataAPIKey(ctx, d, &p.User, "data_api.key_create", a, k, pyjson.Object{
		{Key: "expires_at", Value: k.ExpiresAt.Format(time.RFC3339)}, {Key: "rate_per_minute", Value: int64(k.RatePerMinute)},
		{Key: "resources", Value: anyList(k.Resources)}, {Key: "cidrs", Value: anyList(k.CIDRs)}})
	ko, err := dataAPIKeyOf(ctx, d, k, nil, dataapis.Now())
	if err != nil {
		fail(d, w, "data apis: the key", err)
		return
	}
	server.JSON(w, http.StatusCreated, map[string]any{"key": ko, "secret": secret})
}

// dataAPIKeyOfPath is the key of the path, among the data API's (404 otherwise).
func dataAPIKeyOfPath(d *Deps, w http.ResponseWriter, r *http.Request, a *dataapis.API) *dataapis.Key {
	id, err := strconv.ParseInt(r.PathValue("key_id"), 10, 32)
	if err != nil {
		detail(w, http.StatusNotFound, "Key not found")
		return nil
	}
	k, err := dataapis.KeyOf(r.Context(), d.DB, a.ID, id)
	if err != nil {
		fail(d, w, "data apis: the key", err)
		return nil
	}
	if k == nil {
		detail(w, http.StatusNotFound, "Key not found")
	}
	return k
}

// POST /data-apis/{api_id}/keys/{key_id}/revoke: refused from the next poll of the pods. Also
// where the data APIs are switched off: a key one wants to stop must always be stoppable.
func revokeDataAPIKey(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	ids, ok := pathInts(w, r, "api_id")
	if !ok {
		return
	}
	a, err := dataapis.Get(ctx, d.DB, ids[0])
	if err != nil {
		fail(d, w, "data apis: reading it", err)
		return
	}
	if a == nil {
		detail(w, http.StatusNotFound, "Data API not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	seen, err := c.Has(ctx, a.ProjectID, perm.View)
	if err == nil && !seen {
		detail(w, http.StatusNotFound, "Data API not found")
		return
	}
	if err != nil {
		fail(d, w, "data apis: the rights", err)
		return
	}
	if !dataAPIEnsureManage(d, w, ctx, c, a.ProjectID) {
		return
	}
	k := dataAPIKeyOfPath(d, w, r, a)
	if k == nil {
		return
	}
	if err := dataapis.RevokeKey(ctx, d.DB, k.ID); err != nil {
		fail(d, w, "data apis: revoking the key", err)
		return
	}
	if k.RevokedAt == nil {
		auditDataAPIKey(ctx, d, &p.User, "data_api.key_revoke", a, k, nil)
	}
	w.WriteHeader(http.StatusNoContent)
}

type dataAPIRenewIn struct {
	ExpiresAt  string `json:"expires_at"`
	GraceHours int    `json:"grace_hours"`
}

// POST /data-apis/{api_id}/keys/{key_id}/renew: a new key with the same name, rate, resources
// and addresses; the old one keeps working for the grace chosen (none: it stops now).
func renewDataAPIKey(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	a, _, ok := dataAPIManaged(d, w, r, p)
	if !ok || !fromTheInterface(w, p) {
		return
	}
	ctx := r.Context()
	old := dataAPIKeyOfPath(d, w, r, a)
	if old == nil {
		return
	}
	var in dataAPIRenewIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	now := dataapis.Now()
	switch dataapis.KeyState(old, now) {
	case dataapis.KeyRevoked:
		detail(w, http.StatusConflict, "The key is revoked: make a new one")
		return
	case dataapis.KeyGrace, dataapis.KeyReplaced:
		detail(w, http.StatusConflict, "The key was renewed already")
		return
	}
	if in.GraceHours < 0 || in.GraceHours > dataapis.MaxGraceDays*24 {
		detail(w, http.StatusUnprocessableEntity, fmt.Sprintf("The grace period is from 0 to %d hours", dataapis.MaxGraceDays*24))
		return
	}
	rate := old.RatePerMinute
	n, ref, err := checkDataAPIKey(ctx, d, a, dataAPIKeyIn{Name: old.Name, ExpiresAt: in.ExpiresAt, RatePerMinute: &rate,
		Resources: old.Resources, CIDRs: old.CIDRs})
	if ref != nil && strings.Contains(ref.text, "no resource") {
		// a resource taken out of the API since: the new key keeps the others (none left: all)
		kept := []string{}
		res, rerr := dataapis.Resources(ctx, d.DB, a.ID)
		if rerr != nil {
			fail(d, w, "data apis: the resources", rerr)
			return
		}
		for _, name := range old.Resources {
			if slices.ContainsFunc(res, func(x dataapis.Resource) bool { return x.Name == name }) {
				kept = append(kept, name)
			}
		}
		if len(kept) == 0 {
			detail(w, http.StatusConflict, "None of the resources the key could read is left: make a new key")
			return
		}
		n, ref, err = checkDataAPIKey(ctx, d, a, dataAPIKeyIn{Name: old.Name, ExpiresAt: in.ExpiresAt, RatePerMinute: &rate,
			Resources: kept, CIDRs: old.CIDRs})
	}
	if answerRefusal(d, w, "data apis: the key", ref, err) {
		return
	}
	n.CreatedBy = p.User.ID
	var k *dataapis.Key
	var secret string
	err = inTx(ctx, d, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT id FROM data_api_keys WHERE id = $1 FOR UPDATE`, old.ID); err != nil {
			return err
		}
		if k, secret, err = dataapis.InsertKey(ctx, tx, n); err != nil {
			return err
		}
		if dataapis.KeyState(old, now) == dataapis.KeyExpired {
			return nil // nothing left to end
		}
		return dataapis.EndAt(ctx, tx, old, now.Add(time.Duration(in.GraceHours)*time.Hour))
	})
	if err != nil {
		fail(d, w, "data apis: renewing the key", err)
		return
	}
	auditDataAPIKey(ctx, d, &p.User, "data_api.key_renew", a, old, pyjson.Object{{Key: "new_key_id", Value: k.ID},
		{Key: "new_key_hint", Value: k.Hint}, {Key: "grace_hours", Value: int64(in.GraceHours)},
		{Key: "expires_at", Value: k.ExpiresAt.Format(time.RFC3339)}})
	renewed, err := dataapis.KeyOf(ctx, d.DB, a.ID, old.ID)
	if err != nil {
		fail(d, w, "data apis: the key", err)
		return
	}
	ko, err := dataAPIKeyOf(ctx, d, k, nil, dataapis.Now())
	if err != nil {
		fail(d, w, "data apis: the key", err)
		return
	}
	ro, err := dataAPIKeyOf(ctx, d, renewed, nil, dataapis.Now())
	if err != nil {
		fail(d, w, "data apis: the key", err)
		return
	}
	server.JSON(w, http.StatusCreated, map[string]any{"key": ko, "secret": secret, "replaced": ro})
}
