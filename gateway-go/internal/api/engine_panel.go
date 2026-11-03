package api

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
)

// panelMargin is engine_panel_settings.MARGIN.
const panelMargin = 300

// engineGet is a GET to the engine with CHECK's timeout, raise_for_status and .json():
// (value, status) with status 0 when it answered 2xx, or err when it could not be asked or
// read (httpx.HTTPError, ValueError).
func engineGet(ctx context.Context, d *Deps, path string) (any, int, error) {
	ctx, cancel := context.WithTimeout(ctx, engine.Check)
	defer cancel()
	res, raw, err := d.Engine.JSON(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, 0, err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 {
		return nil, res.StatusCode, nil
	}
	v, err := pyd.LoadsStr(string(raw))
	return v, 0, err
}

// engineCatalog is engine_panel_settings.catalog: the engine's settings, their limits and
// values, asked with the hard limit the gateway allows.
func engineCatalog(ctx context.Context, d *Deps) (any, int, error) {
	return engineGet(ctx, d, fmt.Sprintf("/settings?max_task_time_limit=%d", d.Panel.RunStaleTimeout()-panelMargin))
}

// engineSettingRow is a row of engine_settings.
type engineSettingRow struct {
	Value     int64
	ValueJSON *string
	UpdatedAt time.Time
	UpdatedBy *string
}

// savedEngineSettings is engine_panel_settings.saved.
func savedEngineSettings(ctx context.Context, q perm.Querier) (map[string]engineSettingRow, error) {
	rows, err := q.Query(ctx, `SELECT key, value, value_json, updated_at, updated_by FROM engine_settings`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]engineSettingRow{}
	for rows.Next() {
		var k string
		var r engineSettingRow
		if err := rows.Scan(&k, &r.Value, &r.ValueJSON, &r.UpdatedAt, &r.UpdatedBy); err != nil {
			return nil, err
		}
		out[k] = r
	}
	return out, rows.Err()
}

// maskEmail is services/masking.py mask_email: `alice@x.it` → `a***@x.it`.
func maskEmail(email string) string {
	text := pyd.Strip(email)
	i := strings.LastIndex(text, "@")
	if i < 0 {
		if text == "" {
			return ""
		}
		return "***"
	}
	local, domain := text[:i], text[i+1:]
	if local == "" {
		return "***@" + domain
	}
	_, size := utf8.DecodeRuneInString(local)
	return local[:size] + "***@" + domain
}

// updatedBy is `(by if full else mask_email(by)) if by else None`.
func updatedBy(by *string, full bool) any {
	if by == nil || *by == "" {
		return nil
	}
	if full {
		return *by
	}
	return maskEmail(*by)
}
