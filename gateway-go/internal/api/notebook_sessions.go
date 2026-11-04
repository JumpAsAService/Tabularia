package api

// routes/notebook_sessions.py: the live pods of a notebook (its sessions) and the frame's
// ticket; starting and stopping a pod and «Save version» are in notebook_sessions_runtime.go.

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("notebook_sessions", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/notebooks/{notebook_id}/sessions", Handler: server.Authed(d.Auth,
				"/notebooks/{notebook_id}/sessions", func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
					listNotebookSessions(d, w, r, p)
				})},
			{Method: http.MethodPost, Pattern: "/notebooks/{notebook_id}/ticket",
				Handler: scWithBody(d, "/notebooks/{notebook_id}/ticket", frameTicket)},
		}
	})
}

const (
	nbTicketSeconds = 60
	nbNeedsOrigin   = "Notebooks need an address of their own (NOTEBOOKS__ORIGIN): an administrator must set it"
)

type nbSessionOut struct {
	Mode        string    `json:"mode"`
	Status      string    `json:"status"`
	Error       *string   `json:"error"`
	User        *nbPerson `json:"user"`
	StartedAt   string    `json:"started_at"`
	LastSeenAt  string    `json:"last_seen_at"`
	Visitors    *int64    `json:"visitors"`
	MaxVisitors *int64    `json:"max_visitors"`
}

// nbVisitorsCap is notebook_runtime.visitors_cap.
func nbVisitorsCap(own *int64) int64 {
	v, ceiling := notebooksVisitors()
	if own != nil && *own != 0 {
		v = *own
	}
	return max(1, min(v, ceiling))
}

// listNotebookSessions is GET /notebooks/{notebook_id}/sessions (list_sessions): the live
// pods (and a failed one, saying why); the editor's only to whoever may edit.
func listNotebookSessions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "notebook_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	editable, err := c.Has(ctx, n.ProjectID, perm.Edit)
	if err != nil {
		fail(d, w, "notebooks: the permission", err)
		return
	}
	rows, err := d.DB.Query(ctx, `SELECT `+nbSessionColumns+` FROM notebook_sessions WHERE notebook_id = $1 ORDER BY mode`, n.ID)
	if err != nil {
		fail(d, w, "notebooks: the sessions", err)
		return
	}
	var list []*nbSession
	for rows.Next() {
		x, err := nbScanSession(rows)
		if err != nil {
			rows.Close()
			fail(d, w, "notebooks: a session", err)
			return
		}
		list = append(list, x)
	}
	rows.Close()
	out := []nbSessionOut{}
	for _, x := range list {
		if x.Mode == "edit" && !editable {
			continue
		}
		if x.Status == "starting" && scOpenLineageOn() {
			scForward(d, w, r) // a session that fails tells the lineage catalog: Python's, while OpenLineage is on
			return
		}
		// notebook_runtime.refresh: a starting session moved forward
		if err := nbRefresh(ctx, d, x, false); err != nil {
			fail(d, w, "notebooks: the session's pod", err)
			return
		}
		o, err := nbSessionJSON(d, r, x)
		if err != nil {
			fail(d, w, "notebooks: the session", err)
			return
		}
		out = append(out, *o)
	}
	server.JSON(w, http.StatusOK, out)
}

var nbPlainOrigin = regexp.MustCompile(`^(https?://[a-z0-9.\-]+(:[0-9]{1,5})?|https?://\[[0-9a-f:.]+\](:[0-9]{1,5})?)$`)

// scSameOrigin is deps/auth.same_origin: the request comes from a page of Tabularia's own
// origin (its `Origin` names the host it was sent to, or one of APP__CORS_ORIGINS).
func scSameOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" || origin == "null" {
		return false
	}
	for _, o := range scriptcfg.Get().App.CORSOrigins {
		if strings.TrimRight(origin, "/") == strings.TrimRight(o, "/") {
			return true
		}
	}
	host := strings.ToLower(r.Host)
	_, netloc := scSplitNetloc(origin)
	return host != "" && strings.ToLower(netloc) == host
}

// scSplitNetloc is urlsplit's scheme and netloc.
func scSplitNetloc(u string) (string, string) {
	scheme := ""
	if i := strings.Index(u, ":"); i > 0 {
		scheme, u = u[:i], u[i+1:]
	}
	if !strings.HasPrefix(u, "//") {
		return scheme, ""
	}
	netloc := u[2:]
	if i := strings.IndexAny(netloc, "/?#"); i >= 0 {
		netloc = netloc[:i]
	}
	return scheme, netloc
}

// frameTicket is POST /notebooks/{notebook_id}/ticket (frame_ticket): a ticket that opens
// the app or the editor in the page's frame, on the notebooks' own address.
func frameTicket(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body) {
	var params pyd.Params
	nid := params.Path("notebook_id", r.PathValue("notebook_id"), pyd.Int())
	in := params.Body(body, gatewaymodels.TicketIn)
	if resp := params.Response(); resp != nil {
		resp.Write(w)
		return
	}
	id, ok := nid.(int64)
	if !ok {
		id = -1
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	n, ok := nbVisible(d, w, r, c, p, id)
	if !ok {
		return
	}
	mode := in.Get("mode").(string)
	if mode == "edit" && !scEnsure(d, w, ctx, c, n.ProjectID, perm.Edit) {
		return
	}
	cfg := scriptcfg.Get()
	if cfg.Notebooks.Origin == "" {
		if !cfg.Notebooks.AllowSameOrigin {
			detail(w, http.StatusConflict, nbNeedsOrigin)
			return
		}
		server.JSON(w, http.StatusOK, pyjson.Object{{Key: "url", Value: nil}, {Key: "same_origin", Value: true}, {Key: "expires_in", Value: 0}})
		return
	}
	page := strings.ToLower(strings.TrimRight(strings.TrimSpace(r.Header.Get("Origin")), "/"))
	if p.SessionID == "" || !nbPlainOrigin.MatchString(page) || !scSameOrigin(r) {
		detail(w, http.StatusForbidden, "A notebook opens from its page in Tabularia, signed in in the browser")
		return
	}
	// notebook_frames.issue: the expired tickets go, a new one is kept by its fingerprint
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		fail(d, w, "notebooks: a ticket", err)
		return
	}
	ticket := base64.RawURLEncoding.EncodeToString(raw)
	sum := sha256.Sum256([]byte(ticket))
	now := time.Now().UTC()
	if _, err := d.DB.Exec(ctx, `DELETE FROM notebook_tickets WHERE expires_at < $1`, now); err != nil {
		fail(d, w, "notebooks: the old tickets", err)
		return
	}
	if _, err := d.DB.Exec(ctx, `INSERT INTO notebook_tickets (token_fp, notebook_id, mode, user_id, sid, frame_origin, expires_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7)`, hex.EncodeToString(sum[:]), n.ID, mode, p.User.ID, p.SessionID, page,
		now.Add(nbTicketSeconds*time.Second)); err != nil {
		fail(d, w, "notebooks: the ticket", err)
		return
	}
	url := fmt.Sprintf("%s%s/notebooks/%d/%s/?ticket=%s", cfg.Notebooks.Origin, cfg.App.RootPath, n.ID, mode, ticket)
	server.JSON(w, http.StatusOK, pyjson.Object{{Key: "url", Value: url}, {Key: "same_origin", Value: false},
		{Key: "expires_in", Value: nbTicketSeconds}})
}
