package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// PrivacyOut is routes/privacy.py PrivacyOut.
type PrivacyOut struct {
	Enabled         bool    `json:"enabled"`
	Summary         string  `json:"summary"`
	Body            string  `json:"body"`
	SummaryTemplate string  `json:"summary_template"`
	BodyTemplate    string  `json:"body_template"`
	URL             string  `json:"url"`
	UpdatedAt       *string `json:"updated_at"`
}

func init() {
	register("privacy", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/privacy-notice", Handler: http.HandlerFunc(getPrivacy(d))},
			{Method: http.MethodPut, Pattern: "/privacy-notice", Handler: withBody(superuser(d, "/privacy-notice", updatePrivacy(d)))},
		}
	})
}

// duration is privacy._duration: the retention window as a person says it.
func duration(minutes int) string {
	plural := func(n int, unit string) string {
		if n == 1 {
			return fmt.Sprintf("%d %s", n, unit)
		}
		return fmt.Sprintf("%d %ss", n, unit)
	}
	switch {
	case minutes <= 0:
		return "none — entries are kept until an administrator removes them"
	case minutes%1440 == 0:
		return plural(minutes/1440, "day")
	case minutes%60 == 0:
		return plural(minutes/60, "hour")
	}
	return plural(minutes, "minute")
}

type privacyNotice struct {
	id                 int64
	enabled            bool
	summary, body, url string
	updatedAt          time.Time
}

func (d *Deps) privacyOut(n *privacyNotice) PrivacyOut {
	back := func(s string) string {
		return strings.ReplaceAll(s, "{audit_retention}", duration(d.Config.AuditRetentionMinutes))
	}
	return PrivacyOut{Enabled: n.enabled, Summary: back(n.summary), Body: back(n.body), SummaryTemplate: n.summary,
		BodyTemplate: n.body, URL: n.url, UpdatedAt: isoPtr(&n.updatedAt)}
}

// readPrivacy is privacy._read: the single row, created at the first read with the starting
// text.
func readPrivacy(ctx context.Context, d *Deps) (*privacyNotice, error) {
	return readPrivacyFrom(ctx, d.DB)
}

// privacyDefaultEnabled: a new installation starts with the notice switched off. The
// sample text is there for the administrator to adapt and switch on; a bar on the sign-in
// page that speaks for an organization nobody has asked is not a default. An installation
// whose notice was already created keeps its own setting.
const privacyDefaultEnabled = false

// readPrivacyFrom reads the one notice, creating it (switched off) the first time.
func readPrivacyFrom(ctx context.Context, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}) (*privacyNotice, error) {
	var n privacyNotice
	scan := func(row pgx.Row) error {
		return row.Scan(&n.id, &n.enabled, &n.summary, &n.body, &n.url, &n.updatedAt)
	}
	err := scan(db.QueryRow(ctx, `SELECT id, enabled, summary, body, url, updated_at FROM privacy_notice LIMIT 1`))
	if errors.Is(err, pgx.ErrNoRows) {
		err = scan(db.QueryRow(ctx, `INSERT INTO privacy_notice (id, enabled, summary, body, url, updated_at)
			VALUES (1, $1, $2, $3, '', $4) RETURNING id, enabled, summary, body, url, updated_at`,
			privacyDefaultEnabled, privacyDefaultSummary, privacyDefaultText, time.Now().UTC()))
	}
	return &n, err
}

// getPrivacy is routes/privacy.py get_privacy: PUBLIC, read before signing in.
func getPrivacy(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		n, err := readPrivacy(r.Context(), d)
		if err != nil {
			fail(d, w, "reading the privacy notice", err)
			return
		}
		server.JSON(w, http.StatusOK, d.privacyOut(n))
	}
}

// updatePrivacy is routes/privacy.py update_privacy: what is given changes, the audit says
// whether the notice is on. It records no address (the Python call passes no request).
func updatePrivacy(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.PrivacyUpdate)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		n, err := readPrivacy(ctx, d)
		if err != nil {
			fail(d, w, "reading the privacy notice", err)
			return
		}
		if v := optBool(body, "enabled"); v != nil {
			n.enabled = *v
		}
		for _, f := range []struct {
			name string
			to   *string
		}{{"summary", &n.summary}, {"body", &n.body}, {"url", &n.url}} {
			if v := optStr(body, f.name); v != nil {
				*f.to = *v
			}
		}
		err = d.DB.QueryRow(ctx, `UPDATE privacy_notice SET enabled = $1, summary = $2, body = $3, url = $4, updated_at = $5,
			updated_by = $6 WHERE id = $7 RETURNING updated_at`, n.enabled, n.summary, n.body, n.url, time.Now().UTC(), p.User.ID, n.id).Scan(&n.updatedAt)
		if err != nil {
			fail(d, w, "saving the privacy notice", err)
			return
		}
		audit.Record(r.Context(), d.DB, d.Log, audit.Event{Actor: &p.User, Action: "privacy.update",
			TargetType: ptr("privacy_notice"), TargetID: &n.id, Detail: pyjson.Object{{Key: "enabled", Value: n.enabled}}})
		server.JSON(w, http.StatusOK, d.privacyOut(n))
	}
}
