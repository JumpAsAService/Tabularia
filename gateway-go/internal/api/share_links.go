package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dashboards"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/passwords"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/roles"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sharelinks"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// Share links, the creators' side (docs/specs/share-links.md): making one from the viewer of a
// report or a dashboard, the list of an object's links, a new temporary password, revoking; and
// the Administration panel's tab with the switch. Routes of the Go gateway only
// (server/go_only_routes.go). Whoever has MANAGE on the object's folder, or an administrator.

func init() {
	register("share_links", func(d *Deps) []server.Route {
		authed := func(method, pattern string, fn func(http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, fn)}
		}
		return []server.Route{
			authed(http.MethodGet, "/share-links", shareLinksOfObject(d)),
			authed(http.MethodPost, "/share-links", createShareLink(d)),
			authed(http.MethodPost, "/share-links/{link_id}/password", resetShareLinkPassword(d)),
			authed(http.MethodPost, "/share-links/{link_id}/revoke", revokeShareLink(d)),
			{Method: http.MethodGet, Pattern: "/admin/share-links", Handler: adminRole(d, "/admin/share-links", false, adminShareLinks(d))},
			{Method: http.MethodPut, Pattern: "/admin/share-links/settings", Handler: adminRole(d, "/admin/share-links/settings", true,
				saveShareLinkSettings(d))},
		}
	})
}

func shareKind(kind string) *versioned.Kind {
	switch kind {
	case sharelinks.Report:
		return reports.Kind
	case sharelinks.Dashboard:
		return dashboards.Kind
	}
	return nil
}

// mayManage says whether this person may make and manage the links of an object: an
// administrator, or MANAGE on its folder.
func mayManage(ctx context.Context, d *Deps, u *auth.User, it *versioned.Item) (bool, error) {
	if it == nil {
		return false, nil
	}
	return perm.For(d.DB, u).Has(ctx, it.ProjectID, perm.Manage)
}

// linkPerson is who made a link, as the lists show them.
type linkPerson struct {
	ID       int64  `json:"id"`
	Email    string `json:"email"`
	FullName string `json:"full_name"`
}

// linkOut is a link as the lists show it: never its token, its password or its sessions.
type linkOut struct {
	ID           int64               `json:"id"`
	Hint         string              `json:"hint"`
	Kind         string              `json:"kind"`
	ObjectID     int64               `json:"object_id"`
	ObjectName   *string             `json:"object_name,omitempty"`
	ProjectID    *int64              `json:"project_id,omitempty"`
	Label        string              `json:"label"`
	CreatedBy    *linkPerson         `json:"created_by"`
	Embeddable   bool                `json:"embeddable"`
	Origins      []string            `json:"origins"`
	Downloads    []string            `json:"downloads"`
	Params       map[string][]string `json:"params"`
	ExpiresAt    time.Time           `json:"expires_at"`
	RevokedAt    *time.Time          `json:"revoked_at"`
	CreatedAt    time.Time           `json:"created_at"`
	LastOpenedAt *time.Time          `json:"last_opened_at"`
	Openings     int64               `json:"openings"`
	State        string              `json:"state"`
	// Suspended says why a live link does not answer now: "creator_inactive", "creator_rights",
	// "object_gone", "disabled"; "" when it answers
	Suspended string `json:"suspended"`
}

type shareOut struct {
	d        *Deps
	ctx      context.Context
	settings sharelinks.Settings
	people   map[int64]*auth.User
	items    map[string]*versioned.Item
}

func (o *shareOut) person(id int64) (*auth.User, error) {
	if u, ok := o.people[id]; ok {
		return u, nil
	}
	u, err := loadUser(o.ctx, o.d.DB, id)
	if err != nil {
		return nil, err
	}
	o.people[id] = u
	return u, nil
}

func (o *shareOut) item(kind string, id int64) (*versioned.Item, error) {
	key := kind + ":" + strconv.FormatInt(id, 10)
	if it, ok := o.items[key]; ok {
		return it, nil
	}
	k := shareKind(kind)
	if k == nil {
		return nil, nil
	}
	it, err := k.Get(o.ctx, o.d.DB, id)
	if err != nil {
		return nil, err
	}
	o.items[key] = it
	return it, nil
}

// suspension is why a link whose row is live does not answer: "" when it does.
func suspension(ctx context.Context, d *Deps, s sharelinks.Settings, creator *auth.User, it *versioned.Item) (string, error) {
	switch {
	case !s.Enabled:
		return "disabled", nil
	case it == nil:
		return "object_gone", nil
	case creator == nil || !creator.IsActive:
		return "creator_inactive", nil
	}
	ok, err := mayManage(ctx, d, creator, it)
	if err != nil {
		return "", err
	}
	if !ok {
		return "creator_rights", nil
	}
	return "", nil
}

func (o *shareOut) of(l *sharelinks.Link, withObject bool) (linkOut, error) {
	now := sharelinks.Now()
	out := linkOut{ID: l.ID, Hint: l.TokenHint, Kind: l.Kind, ObjectID: l.ObjectID, Label: l.Label, Embeddable: l.Embeddable,
		Origins: l.Origins, Downloads: l.Downloads, Params: l.Params, ExpiresAt: l.ExpiresAt, RevokedAt: l.RevokedAt,
		CreatedAt: l.CreatedAt, LastOpenedAt: l.LastOpenedAt, Openings: l.Openings, State: sharelinks.State(l, now)}
	if out.Origins == nil {
		out.Origins = []string{}
	}
	if out.Downloads == nil {
		out.Downloads = []string{}
	}
	creator, err := o.person(l.CreatorID)
	if err != nil {
		return out, err
	}
	if creator != nil {
		out.CreatedBy = &linkPerson{ID: creator.ID, Email: creator.Email, FullName: creator.FullName}
	}
	it, err := o.item(l.Kind, l.ObjectID)
	if err != nil {
		return out, err
	}
	if withObject && it != nil {
		out.ObjectName, out.ProjectID = &it.Name, &it.ProjectID
	}
	if out.State == "active" || out.State == "waiting" {
		if out.Suspended, err = suspension(o.ctx, o.d, o.settings, creator, it); err != nil {
			return out, err
		}
	}
	return out, nil
}

func newShareOut(ctx context.Context, d *Deps) (*shareOut, error) {
	s, err := sharelinks.LoadSettings(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	return &shareOut{d: d, ctx: ctx, settings: s, people: map[int64]*auth.User{}, items: map[string]*versioned.Item{}}, nil
}

// objectOfQuery is the object a request names (kind, object id) when this person sees it.
func objectOfQuery(ctx context.Context, d *Deps, u *auth.User, kind string, id int64) (*versioned.Item, error) {
	k := shareKind(kind)
	if k == nil {
		return nil, nil
	}
	it, _, err := k.GetVisible(ctx, d.DB, perm.For(d.DB, u), id)
	return it, err
}

// GET /share-links?kind=report&object_id=4: what the dialog of an object needs.
func shareLinksOfObject(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx, q := r.Context(), r.URL.Query()
		id, _ := strconv.ParseInt(q.Get("object_id"), 10, 64)
		it, err := objectOfQuery(ctx, d, &p.User, q.Get("kind"), id)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if it == nil {
			detail(w, http.StatusNotFound, "Not found")
			return
		}
		o, err := newShareOut(ctx, d)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		manage, err := mayManage(ctx, d, &p.User, it)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		links := []linkOut{}
		if manage {
			list, err := sharelinks.List(ctx, d.DB, q.Get("kind"), it.ID)
			if err != nil {
				fail500(d, w, "share links", err)
				return
			}
			for _, l := range list {
				lo, err := o.of(l, false)
				if err != nil {
					fail500(d, w, "share links", err)
					return
				}
				links = append(links, lo)
			}
		}
		server.JSON(w, http.StatusOK, map[string]any{"enabled": o.settings.Enabled, "can_manage": manage,
			"can_create": manage && o.settings.Enabled && p.TokenName == "", "max_days": o.settings.MaxDays,
			"downloads": sharelinks.Downloads[q.Get("kind")], "public_base_url": sharelinks.PublicBaseURL(), "links": links})
	}
}

// shareLinkIn is the body of POST /share-links.
type shareLinkIn struct {
	Kind       string              `json:"kind"`
	ObjectID   int64               `json:"object_id"`
	Label      string              `json:"label"`
	ExpiresAt  string              `json:"expires_at"`
	Embeddable bool                `json:"embeddable"`
	Origins    []string            `json:"origins"`
	Confirmed  bool                `json:"confirmed"` // an embeddable link: «anyone who sees the host pages sees the data»
	Password   string              `json:"password"`
	Params     map[string][]string `json:"params"`
	Downloads  []string            `json:"downloads"`
}

func checkParams(params map[string][]string) error {
	if len(params) > 100 {
		return errors.New("too many parameters")
	}
	for name, values := range params {
		if name == "" || len(name) > 100 || len(values) > 1000 {
			return errors.New("the parameters' values are not valid")
		}
		for _, v := range values {
			if len(v) > 1000 {
				return errors.New("a parameter's value is too long")
			}
		}
	}
	return nil
}

func checkDownloads(kind string, list []string) ([]string, error) {
	allowed := map[string]bool{}
	for _, f := range sharelinks.Downloads[kind] {
		allowed[f] = true
	}
	seen := map[string]bool{}
	out := []string{}
	for _, f := range list {
		if !allowed[f] {
			return nil, errors.New("a download that this kind of object does not have: " + f)
		}
		if !seen[f] {
			seen[f] = true
			out = append(out, f)
		}
	}
	return out, nil
}

// expiryOf is the link's expiry: in the future, within the panel's longest life (and a day of
// margin, for the end of the last day in any time zone).
func expiryOf(text string, maxDays int, now time.Time) (time.Time, error) {
	t, err := time.Parse(time.RFC3339, strings.TrimSpace(text))
	if err != nil {
		return time.Time{}, errors.New("the expiry is a date and time (RFC 3339)")
	}
	t = t.UTC().Truncate(time.Second)
	if !t.After(now.Add(time.Minute)) {
		return time.Time{}, errors.New("the expiry is in the past")
	}
	if t.After(now.Add(time.Duration(maxDays+1) * 24 * time.Hour)) {
		return time.Time{}, errors.New("the expiry is further than the longest life of a link (" + strconv.Itoa(maxDays) + " days)")
	}
	return t, nil
}

func decodeJSON(r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<20))
	return dec.Decode(v)
}

func auditShare(ctx context.Context, d *Deps, actor *auth.User, action, outcome string, l *sharelinks.Link, more pyjson.Object) {
	label := l.Label
	target := "share_link"
	dt := append(pyjson.Object{{Key: "kind", Value: l.Kind}, {Key: "object_id", Value: l.ObjectID}}, more...)
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: actor, ActorLabel: shareActorLabel(actor, l), Action: action, Outcome: outcome,
		TargetType: &target, TargetID: &l.ID, TargetLabel: &label, Detail: dt})
}

// shareActorLabel is who an event of a link is written as: the person who acted, or the link
// itself for its recipient (who has no account).
func shareActorLabel(actor *auth.User, l *sharelinks.Link) string {
	if actor != nil {
		return ""
	}
	return "share link #" + strconv.FormatInt(l.ID, 10)
}

// POST /share-links
func createShareLink(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		if p.TokenName != "" {
			detail(w, http.StatusForbidden, "Share links are made from the interface, not with an API token")
			return
		}
		var in shareLinkIn
		if err := decodeJSON(r, &in); err != nil {
			detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON")
			return
		}
		s, err := sharelinks.LoadSettings(ctx, d.DB)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if !s.Enabled {
			detail(w, http.StatusForbidden, "Share links are switched off on this installation")
			return
		}
		it, err := objectOfQuery(ctx, d, &p.User, in.Kind, in.ObjectID)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if it == nil {
			detail(w, http.StatusNotFound, "Not found")
			return
		}
		if ok, err := mayManage(ctx, d, &p.User, it); err != nil {
			fail500(d, w, "share links", err)
			return
		} else if !ok {
			detail(w, http.StatusForbidden, "Only an administrator or whoever has MANAGE on its folder can share it")
			return
		}
		label := strings.TrimSpace(in.Label)
		if len([]rune(label)) > sharelinks.MaxLabel {
			detail(w, http.StatusUnprocessableEntity, "The label is longer than 200 characters")
			return
		}
		now := sharelinks.Now()
		expires, err := expiryOf(in.ExpiresAt, s.MaxDays, now)
		if err != nil {
			detail(w, http.StatusUnprocessableEntity, sentence(err.Error()))
			return
		}
		if err := checkParams(in.Params); err != nil {
			detail(w, http.StatusUnprocessableEntity, sentence(err.Error()))
			return
		}
		downloads, err := checkDownloads(in.Kind, in.Downloads)
		if err != nil {
			detail(w, http.StatusUnprocessableEntity, sentence(err.Error()))
			return
		}
		n := sharelinks.New{Kind: in.Kind, ObjectID: it.ID, Label: label, CreatorID: p.User.ID, Embeddable: in.Embeddable,
			Params: in.Params, Downloads: downloads, ExpiresAt: expires}
		if n.Params == nil {
			n.Params = map[string][]string{}
		}
		if in.Embeddable {
			if !in.Confirmed {
				detail(w, http.StatusUnprocessableEntity, "An embeddable link asks no password: confirm that whoever sees the host pages may see this data")
				return
			}
			if n.Origins, err = sharelinks.Origins(in.Origins); err != nil {
				detail(w, http.StatusUnprocessableEntity, sentence(err.Error()))
				return
			}
		} else {
			if problem := gatewaymodels.PasswordProblem(in.Password); problem != "" {
				detail(w, http.StatusUnprocessableEntity, sentence("the temporary password: "+problem))
				return
			}
			hash, err := passwords.Hash(in.Password)
			if err != nil {
				fail500(d, w, "share links", err)
				return
			}
			n.PasswordHash = &hash
		}
		l, token, err := sharelinks.Create(ctx, d.DB, n)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		mode := "password"
		if l.Embeddable {
			mode = "embed"
		}
		auditShare(ctx, d, &p.User, "share_link.create", "", l, pyjson.Object{{Key: "mode", Value: mode},
			{Key: "expires_at", Value: l.ExpiresAt.Format(time.RFC3339)}, {Key: "origins", Value: anyList(l.Origins)},
			{Key: "downloads", Value: anyList(l.Downloads)}})
		o, err := newShareOut(ctx, d)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		lo, err := o.of(l, false)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		server.JSON(w, http.StatusCreated, map[string]any{"link": lo, "token": token, "path": "/share/" + token})
	}
}

func anyList(s []string) []any {
	out := make([]any, len(s))
	for i, v := range s {
		out[i] = v
	}
	return out
}

func sentence(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[:1])) + string(r[1:])
}

// managedLink is the link of the path when this person may manage it (404 otherwise: a link
// one may not manage is not one they know of).
func managedLink(w http.ResponseWriter, r *http.Request, d *Deps, p *auth.Principal) *sharelinks.Link {
	ctx := r.Context()
	id, err := strconv.ParseInt(r.PathValue("link_id"), 10, 64)
	if err != nil {
		detail(w, http.StatusNotFound, "Share link not found")
		return nil
	}
	l, err := sharelinks.ByID(ctx, d.DB, id)
	if err != nil {
		fail500(d, w, "share links", err)
		return nil
	}
	if l == nil {
		detail(w, http.StatusNotFound, "Share link not found")
		return nil
	}
	rs, err := roles.Of(ctx, d.DB, p.User.ID, p.User.IsSuperuser, p.User.IsObserver)
	if err != nil {
		fail500(d, w, "share links", err)
		return nil
	}
	if rs.Admin {
		return l
	}
	k := shareKind(l.Kind)
	var it *versioned.Item
	if k != nil {
		if it, err = k.Get(ctx, d.DB, l.ObjectID); err != nil {
			fail500(d, w, "share links", err)
			return nil
		}
	}
	ok, err := mayManage(ctx, d, &p.User, it)
	if err != nil {
		fail500(d, w, "share links", err)
		return nil
	}
	if !ok {
		detail(w, http.StatusNotFound, "Share link not found")
		return nil
	}
	return l
}

// POST /share-links/{link_id}/password: a new temporary password; the open sessions close.
func resetShareLinkPassword(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		if p.TokenName != "" {
			detail(w, http.StatusForbidden, "Share links are managed from the interface, not with an API token")
			return
		}
		l := managedLink(w, r, d, p)
		if l == nil {
			return
		}
		var in struct {
			Password string `json:"password"`
		}
		if err := decodeJSON(r, &in); err != nil {
			detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON")
			return
		}
		if l.Embeddable {
			detail(w, http.StatusUnprocessableEntity, "An embeddable link has no password")
			return
		}
		if l.RevokedAt != nil {
			detail(w, http.StatusConflict, "The link is revoked")
			return
		}
		if problem := gatewaymodels.PasswordProblem(in.Password); problem != "" {
			detail(w, http.StatusUnprocessableEntity, sentence("the temporary password: "+problem))
			return
		}
		hash, err := passwords.Hash(in.Password)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if err := sharelinks.ResetPassword(r.Context(), d.DB, l.ID, hash); err != nil {
			fail500(d, w, "share links", err)
			return
		}
		auditShare(r.Context(), d, &p.User, "share_link.password_reset", "", l, nil)
		w.WriteHeader(http.StatusNoContent)
	}
}

// POST /share-links/{link_id}/revoke
func revokeShareLink(d *Deps) func(http.ResponseWriter, *http.Request, *auth.Principal) {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		l := managedLink(w, r, d, p)
		if l == nil {
			return
		}
		if err := sharelinks.Revoke(r.Context(), d.DB, l.ID); err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if l.RevokedAt == nil {
			auditShare(r.Context(), d, &p.User, "share_link.revoke", "", l, nil)
		}
		w.WriteHeader(http.StatusNoContent)
	}
}

// GET /admin/share-links: the switch, the limits, every link of the installation.
func adminShareLinks(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		o, err := newShareOut(ctx, d)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		list, err := sharelinks.List(ctx, d.DB, "", 0)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		links := []linkOut{}
		for _, l := range list {
			lo, err := o.of(l, true)
			if err != nil {
				fail500(d, w, "share links", err)
				return
			}
			links = append(links, lo)
		}
		server.JSON(w, http.StatusOK, map[string]any{"settings": o.settings, "public_base_url": sharelinks.PublicBaseURL(), "links": links})
	}
}

// PUT /admin/share-links/settings
func saveShareLinkSettings(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		var in sharelinks.Settings
		if err := decodeJSON(r, &in); err != nil {
			detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON")
			return
		}
		before, err := sharelinks.LoadSettings(ctx, d.DB)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		if err := sharelinks.SaveSettings(ctx, d.DB, in, p.User.Email); err != nil {
			detail(w, http.StatusUnprocessableEntity, sentence(err.Error()))
			return
		}
		target := "share_link_settings"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "share_link.settings", TargetType: &target,
			Detail: pyjson.Object{{Key: "enabled", Value: in.Enabled}, {Key: "max_days", Value: int64(in.MaxDays)},
				{Key: "ai_comments_per_hour", Value: int64(in.AICommentsPerHour)}, {Key: "was_enabled", Value: before.Enabled}}})
		after, err := sharelinks.LoadSettings(ctx, d.DB)
		if err != nil {
			fail500(d, w, "share links", err)
			return
		}
		server.JSON(w, http.StatusOK, after)
	}
}
