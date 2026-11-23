package api

// The card of the group `bayes` in Administration → Statistical models (docs/specs/bayes-stan-compile.md, «The
// administration card», and the image spec's): the compile limits, «Compiles at once», the cache of binaries, the
// sampling Job's /tmp, the last compiles, and «Empty the cache». The settings live in the one row of bayes_settings;
// a NULL column is the recipe's default, so a default Tabularia changes is followed by whoever never set it.
//
//	GET  /admin/models/bayes/compile      the limits in force with their defaults, the cache, the last compiles
//	PUT  /admin/models/bayes/compile      the settings (a field left out or null is the default)
//	POST /admin/models/bayes/cache/empty  every binary not being placed, objects first, then rows
//
// Administrators act, observers read, an API token never changes anything (models_admin.go's guard).

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/jackc/pgx/v5"
)

func init() {
	register("models_admin_bayes", func(d *Deps) []server.Route {
		route := func(method, pattern string, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern, mgGuarded(d, admin, h))}
		}
		return []server.Route{
			route(http.MethodGet, "/admin/models/bayes/compile", false, bayesCardGet),
			route(http.MethodPut, "/admin/models/bayes/compile", true, bayesCardPut),
			route(http.MethodPost, "/admin/models/bayes/cache/empty", true, bayesCacheEmpty),
		}
	})
}

// The ranges of the card. A memory below 2 GiB or a deadline below 60 s is refused with the measured peaks and times
// quoted (docs/specs/bayes-image.md: 1.3–1.9 GiB, 11–45 s; the 1,497-node budget 130.5 s).
const (
	bcardMaxCPU        = 16
	bcardMaxMemory     = 64 << 30
	bcardMaxSeconds    = 3600
	bcardMinTmp        = 64 << 20
	bcardMaxTmp        = 16 << 30
	bcardMaxSlots      = 16
	bcardMinCache      = 64 << 20
	bcardMaxCache      = 1 << 40
	bcardMinSampleTmp  = 256 << 20
	bcardMaxSampleTmp  = 256 << 30
	bcardCompilesShown = 20
)

var bcardCPU = regexp.MustCompile(`^[1-9][0-9]?$`)

// bayesCardIn is PUT's body: every setting of the card, a null (or a field left out) being the recipe's default.
type bayesCardIn struct {
	CompileCPU     *string `json:"compile_cpu"`
	CompileMemory  *string `json:"compile_memory"`
	CompileSeconds *int64  `json:"compile_seconds"`
	CompileTmp     *string `json:"compile_tmp"`
	CompileSlots   *int64  `json:"compile_slots"`
	CacheMax       *string `json:"cache_max"`
	SamplingTmp    *string `json:"sampling_tmp"`
}

// bayesCardValues are the settings as the card shows them, in force or by default.
type bayesCardValues struct {
	CompileCPU     string `json:"compile_cpu"`
	CompileMemory  string `json:"compile_memory"`
	CompileSeconds int64  `json:"compile_seconds"`
	CompileTmp     string `json:"compile_tmp"`
	CompileSlots   int64  `json:"compile_slots"`
	CacheMax       string `json:"cache_max"`
	SamplingTmp    string `json:"sampling_tmp"`
}

type bayesCardCache struct {
	UsedBytes  int64   `json:"used_bytes"`
	Binaries   int64   `json:"binaries"`
	OldestUse  *string `json:"oldest_use"`
	OlderImage struct {
		Binaries int64 `json:"binaries"`
		Bytes    int64 `json:"bytes"`
	} `json:"older_image"`
}

type bayesCardCompile struct {
	ID              int64    `json:"id"`
	State           string   `json:"state"`
	Reason          *string  `json:"reason"`
	Detail          *string  `json:"detail"`
	ModelID         *int64   `json:"model_id"` // only to a reader who sees the model
	ModelName       *string  `json:"model_name"`
	Version         *int64   `json:"version"`
	Seconds         *float64 `json:"seconds"`
	At              *string  `json:"at"`
	DeadlineSeconds *int64   `json:"deadline_seconds"` // the limits it ran under: what an administrator needs before raising one
	MemoryBytes     *int64   `json:"memory_bytes"`
	BinaryBytes     *int64   `json:"binary_bytes"`
}

// ── the settings row ─────────────────────────────────────────────────────────

// bayesSettingsRow is the row of bayes_settings (id 1); a NULL column stays nil.
type bayesSettingsRow struct {
	bayescompile.Settings
	SamplingTmp *string
}

type bcardQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

func bayesSettingsRead(ctx context.Context, q bcardQuerier) (bayesSettingsRow, error) {
	var s bayesSettingsRow
	var slots *int64
	err := q.QueryRow(ctx, `SELECT compile_cpu, compile_memory, compile_seconds, compile_tmp, compile_slots, cache_max_bytes, sampling_tmp
		FROM bayes_settings WHERE id = 1`).Scan(&s.CompileCPU, &s.CompileMemory, &s.CompileSeconds, &s.CompileTmp, &slots, &s.CacheMaxBytes,
		&s.SamplingTmp)
	if errors.Is(err, pgx.ErrNoRows) {
		return bayesSettingsRow{}, nil
	}
	if slots != nil {
		n := int(*slots)
		s.CompileSlots = &n
	}
	return s, err
}

// bcardQuantity writes whole mebibytes or gibibytes the way the recipe does (2Gi, 512Mi, 1536Mi).
func bcardQuantity(n int64) string {
	if n >= 1<<30 && n%(1<<30) == 0 {
		return strconv.FormatInt(n>>30, 10) + "Gi"
	}
	if n%(1<<20) == 0 {
		return strconv.FormatInt(n>>20, 10) + "Mi"
	}
	return strconv.FormatInt(n, 10)
}

// bayesCardDefaults are the recipe's values, with «Compiles at once» at bayescompile.DefaultSlots.
func bayesCardDefaults(g modelgroups.Group) (bayesCardValues, error) {
	if g.Recipe.Compile == nil || g.Recipe.Cache == nil {
		return bayesCardValues{}, errors.New("the recipe of the group bayes has no compile or cache defaults")
	}
	return bayesCardValues{CompileCPU: g.Recipe.Compile.CPU, CompileMemory: g.Recipe.Compile.Memory, CompileSeconds: g.Recipe.Compile.Seconds,
		CompileTmp: g.Recipe.Compile.Tmp, CompileSlots: bayescompile.DefaultSlots, CacheMax: bcardQuantity(g.Recipe.Cache.MaxBytes),
		SamplingTmp: g.Recipe.Resources.Tmp}, nil
}

// bayesCardInForce is each setting the administrator set, else the default.
func bayesCardInForce(def bayesCardValues, s bayesSettingsRow) bayesCardValues {
	v := def
	if s.CompileCPU != nil {
		v.CompileCPU = *s.CompileCPU
	}
	if s.CompileMemory != nil {
		v.CompileMemory = *s.CompileMemory
	}
	if s.CompileSeconds != nil {
		v.CompileSeconds = *s.CompileSeconds
	}
	if s.CompileTmp != nil {
		v.CompileTmp = *s.CompileTmp
	}
	if s.CompileSlots != nil {
		v.CompileSlots = int64(*s.CompileSlots)
	}
	if s.CacheMaxBytes != nil {
		v.CacheMax = bcardQuantity(*s.CacheMaxBytes)
	}
	if s.SamplingTmp != nil {
		v.SamplingTmp = *s.SamplingTmp
	}
	return v
}

func (v bayesCardValues) object() pyjson.Object {
	return pyjson.Object{{Key: "compile_cpu", Value: v.CompileCPU}, {Key: "compile_memory", Value: v.CompileMemory},
		{Key: "compile_seconds", Value: v.CompileSeconds}, {Key: "compile_tmp", Value: v.CompileTmp},
		{Key: "compile_slots", Value: v.CompileSlots}, {Key: "cache_max", Value: v.CacheMax}, {Key: "sampling_tmp", Value: v.SamplingTmp}}
}

// ── the reads ────────────────────────────────────────────────────────────────

func bayesCardGroup(w http.ResponseWriter) (modelgroups.Group, bool) {
	g, ok := modelgroups.Get(bayescontract.Group)
	if !ok {
		detail(w, http.StatusNotFound, "There is no model group bayes")
	}
	return g, ok
}

func bayesCardGet(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	if g, ok := bayesCardGroup(w); ok {
		bayesCardAnswer(d, w, r, p, g, nil)
	}
}

// bayesCardAnswer is the card's whole state; extra is added to the body (what «Empty the cache» did).
func bayesCardAnswer(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, g modelgroups.Group, extra map[string]any) {
	ctx := r.Context()
	def, err := bayesCardDefaults(g)
	if err != nil {
		fail(d, w, "models: the bayes card", err)
		return
	}
	s, err := bayesSettingsRead(ctx, d.DB)
	if err != nil {
		fail(d, w, "models: the bayes card's settings", err)
		return
	}
	var cache bayesCardCache
	var oldest *time.Time
	if err := d.DB.QueryRow(ctx, `SELECT count(*), coalesce(sum(binary_bytes), 0), min(last_used_at) FROM bayes_binaries WHERE state = 'ready'`).
		Scan(&cache.Binaries, &cache.UsedBytes, &oldest); err != nil {
		fail(d, w, "models: the bayes card's cache", err)
		return
	}
	if oldest != nil {
		o := pyjson.Isoformat(*oldest)
		cache.OldestUse = &o
	}
	// the binaries of an image that is not the group's current one wait to be evicted first
	if img, err := ReadyModelGroupImage(ctx, d.DB, bayescontract.Group); err == nil && img != nil {
		if err := d.DB.QueryRow(ctx, `SELECT count(*), coalesce(sum(binary_bytes), 0) FROM bayes_binaries
			WHERE state = 'ready' AND image_digest <> $1`, img.Digest).Scan(&cache.OlderImage.Binaries, &cache.OlderImage.Bytes); err != nil {
			fail(d, w, "models: the bayes card's older image", err)
			return
		}
	} else {
		var off *ModelGroupUnavailable
		if err != nil && !errors.As(err, &off) {
			fail(d, w, "models: the bayes card's image", err)
			return
		}
	}
	compiles, err := bayesCardCompiles(ctx, d, &p.User)
	if err != nil {
		fail(d, w, "models: the bayes card's compiles", err)
		return
	}
	admin, err := perm.For(d.DB, &p.User).IsAdmin(ctx)
	if err != nil {
		fail(d, w, "models: the roles", err)
		return
	}
	body := map[string]any{"limits": bayesCardInForce(def, s), "defaults": def, "cache": cache, "compiles": compiles,
		"editable": admin && p.TokenName == ""}
	for k, v := range extra {
		body[k] = v
	}
	server.JSON(w, http.StatusOK, body)
}

// bayesCardCompiles are the last compiles. A model's name and version go only to a reader who sees the model.
func bayesCardCompiles(ctx context.Context, d *Deps, u *auth.User) ([]bayesCardCompile, error) {
	rs, err := d.DB.Query(ctx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries
		ORDER BY coalesce(finished_at, started_at, queued_at) DESC NULLS LAST, id DESC LIMIT $1`, bcardCompilesShown)
	if err != nil {
		return nil, err
	}
	var rows []bayescompile.Row
	for rs.Next() {
		row, err := bayescompile.Scan(rs)
		if err != nil {
			rs.Close()
			return nil, err
		}
		rows = append(rows, row)
	}
	rs.Close()
	if err := rs.Err(); err != nil {
		return nil, err
	}
	c := perm.For(d.DB, u)
	usable, err := ModelGroupUsable(ctx, d.DB, c, u.ID, bayescontract.Group)
	if err != nil {
		return nil, err
	}
	seen := map[int64]*bayesmodels.Model{}
	visible := func(id int64) (*bayesmodels.Model, error) {
		if id == 0 {
			return nil, nil
		}
		if m, ok := seen[id]; ok {
			return m, nil
		}
		var shown *bayesmodels.Model
		m, err := bayesmodels.Get(ctx, d.DB, id)
		if err == nil && m != nil {
			ok, cerr := bayesmodels.CanSee(ctx, c, m, usable)
			if cerr != nil {
				return nil, cerr
			}
			if ok {
				shown = m
			}
		} else if err != nil {
			return nil, err
		}
		seen[id] = shown
		return shown, nil
	}
	out := make([]bayesCardCompile, 0, len(rows))
	for _, row := range rows {
		o := bayesCardCompile{ID: row.ID, State: row.State}
		if row.Reason != "" {
			o.Reason = &row.Reason
		}
		if row.Detail != "" {
			o.Detail = &row.Detail
		}
		if m, err := visible(row.ModelID); err != nil {
			return nil, err
		} else if m != nil {
			id, name, v := m.ID, m.Name, row.Version
			o.ModelID, o.ModelName, o.Version = &id, &name, &v
		}
		if row.Seconds > 0 {
			s := row.Seconds
			o.Seconds = &s
		}
		for _, t := range []time.Time{row.FinishedAt, row.StartedAt, row.QueuedAt} {
			if !t.IsZero() {
				at := pyjson.Isoformat(t)
				o.At = &at
				break
			}
		}
		if row.DeadlineSeconds > 0 {
			v := row.DeadlineSeconds
			o.DeadlineSeconds = &v
		}
		if row.MemoryBytes > 0 {
			v := row.MemoryBytes
			o.MemoryBytes = &v
		}
		if row.BinaryBytes > 0 {
			v := row.BinaryBytes
			o.BinaryBytes = &v
		}
		out = append(out, o)
	}
	return out, nil
}

// ── the settings ─────────────────────────────────────────────────────────────

// bcardCheck validates a body against the ranges and returns the row to store: a value equal to the default is
// stored as NULL, so that it keeps following the recipe.
func bcardCheck(in bayesCardIn, def bayesCardValues) (bayesSettingsRow, string) {
	var s bayesSettingsRow
	quantity := func(field string, v *string, min, max int64, hint string) (*string, int64, string) {
		if v == nil || strings.TrimSpace(*v) == "" {
			return nil, 0, ""
		}
		q := strings.TrimSpace(*v)
		n, err := bayescompile.QuantityBytes(q)
		if err != nil {
			return nil, 0, fmt.Sprintf("%s: «%s» is not a whole number of Mi or Gi (for example 512Mi or 3Gi)", field, q)
		}
		if n < min || n > max {
			return nil, 0, fmt.Sprintf("%s: «%s» is not between %s and %s%s", field, q, bayescompile.Bytes(min), bayescompile.Bytes(max), hint)
		}
		return &q, n, ""
	}
	if in.CompileCPU != nil && strings.TrimSpace(*in.CompileCPU) != "" {
		c := strings.TrimSpace(*in.CompileCPU)
		n, _ := strconv.Atoi(c)
		if !bcardCPU.MatchString(c) || n > bcardMaxCPU {
			return s, fmt.Sprintf("compile_cpu: «%s» is not a whole number of cores between 1 and %d (a compile is one process)", c, bcardMaxCPU)
		}
		s.CompileCPU = &c
	}
	var refusal string
	if s.CompileMemory, _, refusal = quantity("compile_memory", in.CompileMemory, bayescompile.MinMemoryBytes, bcardMaxMemory,
		": the measured peaks of a compile are 1.3 to 1.9 GiB"); refusal != "" {
		return s, refusal
	}
	if in.CompileSeconds != nil {
		n := *in.CompileSeconds
		if n < bayescompile.MinDeadlineSeconds || n > bcardMaxSeconds {
			return s, fmt.Sprintf("compile_seconds: %d is not between %d and %d s: a compile takes 11 to 45 s, the 1,497-node budget 130.5 s",
				n, bayescompile.MinDeadlineSeconds, bcardMaxSeconds)
		}
		s.CompileSeconds = &n
	}
	if s.CompileTmp, _, refusal = quantity("compile_tmp", in.CompileTmp, bcardMinTmp, bcardMaxTmp, ": a compile writes about 3 MB"); refusal != "" {
		return s, refusal
	}
	if in.CompileSlots != nil {
		n := *in.CompileSlots
		if n < 1 || n > bcardMaxSlots {
			return s, fmt.Sprintf("compile_slots: %d is not between 1 and %d", n, bcardMaxSlots)
		}
		v := int(n)
		s.CompileSlots = &v
	}
	var cacheBytes int64
	if _, cacheBytes, refusal = quantity("cache_max", in.CacheMax, bcardMinCache, bcardMaxCache, ""); refusal != "" {
		return s, refusal
	} else if cacheBytes > 0 {
		s.CacheMaxBytes = &cacheBytes
	}
	if s.SamplingTmp, _, refusal = quantity("sampling_tmp", in.SamplingTmp, bcardMinSampleTmp, bcardMaxSampleTmp,
		": the draws of a run take about 18 bytes a value"); refusal != "" {
		return s, refusal
	}
	// a value equal to the default follows the recipe
	same := func(set *string, d string) *string {
		if set != nil && *set == d {
			return nil
		}
		return set
	}
	s.CompileCPU, s.CompileMemory, s.CompileTmp, s.SamplingTmp = same(s.CompileCPU, def.CompileCPU), same(s.CompileMemory, def.CompileMemory),
		same(s.CompileTmp, def.CompileTmp), same(s.SamplingTmp, def.SamplingTmp)
	if s.CompileSeconds != nil && *s.CompileSeconds == def.CompileSeconds {
		s.CompileSeconds = nil
	}
	if s.CompileSlots != nil && int64(*s.CompileSlots) == def.CompileSlots {
		s.CompileSlots = nil
	}
	if s.CacheMaxBytes != nil && bcardQuantity(*s.CacheMaxBytes) == def.CacheMax {
		s.CacheMaxBytes = nil
	}
	return s, ""
}

func bayesCardPut(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := bayesCardGroup(w)
	if !ok {
		return
	}
	var in bayesCardIn
	if !decodeDataAPIBody(w, r, &in) {
		return
	}
	def, err := bayesCardDefaults(g)
	if err != nil {
		fail(d, w, "models: the bayes card", err)
		return
	}
	next, refusal := bcardCheck(in, def)
	if refusal != "" {
		detail(w, http.StatusUnprocessableEntity, refusal)
		return
	}
	ctx := r.Context()
	var before, after bayesCardValues
	err = scInTx(ctx, d, func(tx pgx.Tx) error {
		was, err := bayesSettingsRead(ctx, tx)
		if err != nil {
			return err
		}
		before, after = bayesCardInForce(def, was), bayesCardInForce(def, next)
		var slots *int64
		if next.CompileSlots != nil {
			n := int64(*next.CompileSlots)
			slots = &n
		}
		_, err = tx.Exec(ctx, `INSERT INTO bayes_settings (id, compile_cpu, compile_memory, compile_seconds, compile_tmp, compile_slots,
			cache_max_bytes, sampling_tmp, updated_by, updated_at) VALUES (1, $1, $2, $3, $4, $5, $6, $7, $8, $9)
			ON CONFLICT (id) DO UPDATE SET compile_cpu = $1, compile_memory = $2, compile_seconds = $3, compile_tmp = $4,
			compile_slots = $5, cache_max_bytes = $6, sampling_tmp = $7, updated_by = $8, updated_at = $9`,
			next.CompileCPU, next.CompileMemory, next.CompileSeconds, next.CompileTmp, slots, next.CacheMaxBytes, next.SamplingTmp,
			p.User.Email, time.Now().UTC())
		return err
	})
	if err != nil {
		fail(d, w, "models: the bayes card's settings", err)
		return
	}
	if before != after {
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.bayes_compile_settings", TargetType: scPtrStr("model_group"),
			TargetLabel: scPtrStr(bayescontract.Group), Detail: pyjson.Object{{Key: "before", Value: before.object()},
				{Key: "after", Value: after.object()}}})
	}
	bayesCardAnswer(d, w, r, p, g, nil)
}

// ── «Empty the cache» ────────────────────────────────────────────────────────

// bayesCacheEmpty deletes every ready binary no run is placing: under the loop's lock (waiting for it, never skipping),
// the objects first and then the rows, as the loop's eviction does, so that an engine that fails leaves every row in
// place. A row a run is placing (FOR SHARE) stays; the next run of every model compiles again.
func bayesCacheEmpty(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	g, ok := bayesCardGroup(w)
	if !ok {
		return
	}
	ctx := r.Context()
	var evicted bayescompile.Eviction
	var kept int
	var storage error
	err := scInTx(ctx, d, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, bayescompile.LoopLock); err != nil {
			return err
		}
		ready, err := bcSelect(ctx, tx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries WHERE state = 'ready' ORDER BY id`)
		if err != nil {
			return err
		}
		free := map[int64]bool{}
		rs, err := tx.Query(ctx, `SELECT id FROM bayes_binaries WHERE state = 'ready' ORDER BY id FOR UPDATE SKIP LOCKED`)
		if err != nil {
			return err
		}
		for rs.Next() {
			var id int64
			if err := rs.Scan(&id); err != nil {
				rs.Close()
				return err
			}
			free[id] = true
		}
		rs.Close()
		if err := rs.Err(); err != nil {
			return err
		}
		locked := map[int64]bool{}
		for _, row := range ready {
			if !free[row.ID] {
				locked[row.ID] = true
			}
		}
		evicted, err = bayescompile.MakeRoom(ready, locked, "", 0, 0, "")
		if err != nil && !errors.Is(err, bayescompile.ErrNoRoom) {
			return err
		}
		kept = len(locked)
		if err := bcDeleteBinaries(ctx, d, evicted.Objects); err != nil {
			storage = err // the rows stay: nothing is deleted
			evicted, kept = bayescompile.Eviction{}, 0
			return nil
		}
		ids := make([]int64, len(evicted.Rows))
		for i, row := range evicted.Rows {
			ids[i] = row.ID
		}
		return bayescompile.Delete(ctx, tx, ids...)
	})
	if err != nil {
		fail(d, w, "models: emptying the bayes cache", err)
		return
	}
	if storage != nil {
		d.Log.Warn("bayes cache: the storage did not delete the binaries", "err", storage.Error())
		detail(w, http.StatusBadGateway, "The storage did not delete the binaries: nothing was removed, try again in a moment")
		return
	}
	var freed int64
	for _, row := range evicted.Rows {
		freed += row.BinaryBytes
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "models.bayes_cache_empty", TargetType: scPtrStr("model_group"),
		TargetLabel: scPtrStr(bayescontract.Group), Detail: pyjson.Object{{Key: "binaries", Value: len(evicted.Rows)},
			{Key: "bytes", Value: freed}, {Key: "objects", Value: len(evicted.Objects)}, {Key: "kept", Value: kept}}})
	bayesCardAnswer(d, w, r, p, g, map[string]any{"emptied": map[string]any{"binaries": len(evicted.Rows), "bytes": freed, "kept": kept}})
}
