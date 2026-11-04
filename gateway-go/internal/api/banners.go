package api

import (
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// BannerOut is schemas/models.py BannerOut.
type BannerOut struct {
	ID        int64   `json:"id"`
	Message   string  `json:"message"`
	Level     string  `json:"level"`
	CreatedAt *string `json:"created_at"`
	CreatedBy *int64  `json:"created_by"`
}

func init() {
	register("banners", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/banners", Handler: server.Authed(d.Auth, "/banners", listBanners(d))},
			{Method: http.MethodPost, Pattern: "/banners", Handler: withBody(superuser(d, "/banners", createBanner(d)))},
			{Method: http.MethodDelete, Pattern: "/banners/{banner_id}", Handler: superuser(d, "/banners/{banner_id}", deleteBanner(d))},
		}
	})
}

func scanBanner(row pgx.Row) (BannerOut, error) {
	var b BannerOut
	var created time.Time
	err := row.Scan(&b.ID, &b.Message, &b.Level, &created, &b.CreatedBy)
	b.CreatedAt = isoPtr(&created)
	return b, err
}

// listBanners is routes/banners.py list_banners: every banner, newest first.
func listBanners(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		rows, err := d.DB.Query(r.Context(), `SELECT id, message, level, created_at, created_by FROM banners ORDER BY id DESC`)
		if err != nil {
			fail(d, w, "reading the banners", err)
			return
		}
		out, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (BannerOut, error) { return scanBanner(row) })
		if err != nil {
			fail(d, w, "reading the banners", err)
			return
		}
		if out == nil {
			out = []BannerOut{}
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// first80 is message[:80]: the audit's label.
func first80(s string) string {
	r := []rune(s)
	if len(r) > 80 {
		return string(r[:80])
	}
	return s
}

// createBanner is routes/banners.py create_banner.
func createBanner(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.BannerCreate)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		message := pyd.Strip(body.Get("message").(string))
		if message == "" {
			detail(w, http.StatusUnprocessableEntity, "The banner message is empty")
			return
		}
		b, err := scanBanner(d.DB.QueryRow(ctx, `INSERT INTO banners (message, level, created_at, created_by) VALUES ($1, $2, $3, $4)
			RETURNING id, message, level, created_at, created_by`, message, body.Get("level").(string), time.Now().UTC(), p.User.ID))
		if err != nil {
			fail(d, w, "creating the banner", err)
			return
		}
		label := first80(message)
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "banner.create", TargetType: ptr("banner"),
			TargetID: &b.ID, TargetLabel: &label, Detail: pyjson.Object{{Key: "level", Value: b.Level}}})
		server.JSON(w, http.StatusCreated, b)
	}
}

// deleteBanner is routes/banners.py delete_banner.
func deleteBanner(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "banner_id")
		if !ok {
			return
		}
		ctx, id := r.Context(), ids[0]
		var message string
		err := d.DB.QueryRow(ctx, `DELETE FROM banners WHERE id = $1 RETURNING message`, id).Scan(&message)
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusNotFound, "Banner not found")
			return
		}
		if err != nil {
			fail(d, w, "deleting the banner", err)
			return
		}
		label := first80(message)
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "banner.delete", TargetType: ptr("banner"),
			TargetID: &id, TargetLabel: &label})
		returnsNone(w)
	}
}
