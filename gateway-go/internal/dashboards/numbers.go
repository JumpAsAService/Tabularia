package dashboards

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportdef"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportparams"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
)

const (
	MaxClickValues, MaxClickText = 100, 1000
	TilesSeconds                 = 300
)

func refuse(status int, format string, args ...any) error {
	return &reports.Refusal{Status: status, Detail: fmt.Sprintf(format, args...)}
}

// InOurWordsErr is in_our_words: a report's refusal, about a dashboard.
func InOurWordsErr(err error) error {
	var r *reports.Refusal
	var u *reports.Unreadable
	switch {
	case errors.As(err, &r):
		switch d := r.Detail.(type) {
		case pyjson.Object:
			if m, ok := d.Get("message"); ok {
				return &reports.Refusal{Status: r.Status, Detail: append(pyjson.Object{}, d...).Set("message", InOurWords(pystr.Str(m)))}
			}
			return r
		case string:
			return &reports.Refusal{Status: r.Status, Detail: InOurWords(d)}
		}
		return r
	case errors.As(err, &u):
		return &reports.Refusal{Status: 422, Detail: InOurWords(u.Message)}
	}
	return err
}

// Asked is Asked: what a request for numbers asks.
type Asked struct {
	Given    reports.Given
	Tiles    []any // nil: every one
	HasTiles bool
	Pages    pyjson.Object
	State    pyjson.Object
	HasState bool
	Clicks   []any
	Refresh  bool
	Counting []any
}

func tilesByID(def pyjson.Object) map[string]pyjson.Object {
	out := map[string]pyjson.Object{}
	for _, t := range list(get(def, "tiles")) {
		if o, ok := t.(pyjson.Object); ok {
			if id, ok := get(o, "id").(string); ok {
				out[id] = o
			}
		}
	}
	return out
}

func list(v any) []any {
	l, _ := v.([]any)
	return l
}

func levelsOf(tile pyjson.Object, on string) int {
	if fo, ok := tile.Get("filters_others"); ok && fo == false {
		return 0
	}
	switch get(tile, "type") {
	case "chart":
		if on != "rows" || !pystr.Truthy(get(tile, "category")) || get(tile, "kind") == "gauge" {
			return 0
		}
		if pystr.Truthy(get(tile, "split_by")) {
			return 2
		}
		return 1
	case "table":
	default:
		return 0
	}
	if on == "rows" {
		return len(list(get(tile, "groups")))
	}
	m, _ := get(tile, "matrix").(pyjson.Object)
	levels := get(m, "columns_by")
	if l, ok := levels.([]any); ok {
		return len(l)
	}
	if pystr.Truthy(levels) {
		return 1
	}
	return 0
}

func clickValue(v any) bool {
	switch x := v.(type) {
	case string:
		return pystr.Len(x) <= MaxClickText
	case float64:
		return !math.IsInf(x, 0) && !math.IsNaN(x)
	case nil, bool, int64, pyjson.Int:
		return true
	}
	return false
}

// CheckedClicks is _checked_clicks: the clicks as the engine takes them, or a 422.
func CheckedClicks(def pyjson.Object, clicks []any) ([]any, error) {
	tiles := tilesByID(def)
	seen := map[string]bool{}
	out := []any{}
	for _, cv := range clicks {
		click, ok := cv.(pyjson.Object)
		if ok {
			for _, kv := range click {
				if kv.Key != "tile" && kv.Key != "on" && kv.Key != "values" {
					ok = false
				}
			}
		}
		if !ok {
			return nil, refuse(422, "A click is a tile and its values, and nothing else")
		}
		tileV := get(click, "tile")
		on := or(get(click, "on"), "rows")
		values := get(click, "values")
		tileID, isStr := tileV.(string)
		tile, known := tiles[tileID]
		if !isStr || !known {
			return nil, refuse(422, "The dashboard has no tile «%s»", pystr.Cut(pystr.Str(tileV), 64))
		}
		onS, _ := on.(string)
		if onS != "rows" && onS != "columns" {
			return nil, refuse(422, "The tile «%s»: a click is on its rows or on its columns", tileID)
		}
		most := levelsOf(tile, onS)
		if most == 0 {
			return nil, refuse(422, "The tile «%s» cannot filter the others", tileID)
		}
		if seen[tileID+"\x00"+onS] {
			return nil, refuse(422, "The tile «%s» has two clicks: send its values together", tileID)
		}
		seen[tileID+"\x00"+onS] = true
		vs, isList := values.([]any)
		if !isList || len(vs) == 0 {
			return nil, refuse(422, "The click on «%s» needs its values", tileID)
		}
		if len(vs) > MaxClickValues {
			return nil, refuse(422, "The click on «%s» has %d values: at most %d", tileID, len(vs), MaxClickValues)
		}
		for _, v := range vs {
			path, isPath := v.([]any)
			if !isPath {
				path = []any{v}
			}
			good := len(path) >= 1 && len(path) <= most
			for _, x := range path {
				if !clickValue(x) {
					good = false
				}
			}
			if !good {
				return nil, refuse(422, "The click on «%s»: a value is a text of at most %d characters, a number, true, false or null, "+
					"or a list of at most %d of them", tileID, MaxClickText, most)
			}
		}
		out = append(out, pyjson.Object{{Key: "tile", Value: tileID}, {Key: "on", Value: onS}, {Key: "values", Value: vs}})
	}
	return out, nil
}

func or(v, def any) any {
	if pystr.Truthy(v) {
		return v
	}
	return def
}

// CheckAsked is check_asked: 422 for what a browser may not ask.
func CheckAsked(def pyjson.Object, asked *Asked) error {
	clicks, err := CheckedClicks(def, asked.Clicks)
	if err != nil {
		return err
	}
	asked.Clicks = clicks
	known := map[string]bool{}
	for _, t := range list(get(def, "tiles")) {
		if o, ok := t.(pyjson.Object); ok {
			if id, ok := get(o, "id").(string); ok {
				known[id] = true
			}
		}
	}
	var ids []string
	for _, t := range asked.Tiles {
		ids = append(ids, pystr.Str(t))
	}
	for _, kv := range asked.Pages {
		ids = append(ids, kv.Key)
	}
	for _, id := range ids {
		if !known[id] {
			return refuse(422, "The dashboard has no tile «%s»", pystr.Cut(id, 64))
		}
	}
	for _, kv := range asked.Pages {
		if n, _ := pystr.Number(kv.Value); n < 0 {
			return refuse(422, "The tile «%s»: a page is 0 or more", kv.Key)
		}
	}
	return reports.CheckState(asked.State, asked.HasState)
}

// Plan is _Plan: a request, its rights and values checked.
type Plan struct {
	Shaped   any
	Params   pyjson.Object
	Datasets pyjson.Object
	Refused  pyjson.Object
}

// ResolveEach is reports.resolve_each: one dataset at a time; for those it cannot read, what
// stopped it, as {"code", "error"}.
func ResolveEach(ctx context.Context, rs *reports.Resolver, def pyjson.Object, values pyjson.Object) (pyjson.Object, pyjson.Object, error) {
	engines := rs.NewEngines()
	resolved, refused := pyjson.Object{}, pyjson.Object{}
	for _, d := range list(or(get(def, "datasets"), []any{})) {
		name := pystr.Str(get(d.(pyjson.Object), "name"))
		got, err := rs.ResolveDatasets(ctx, def, values, map[string]bool{name: true}, engines)
		if err != nil {
			var u *reports.Unreadable
			var r *reports.Refusal
			switch {
			case errors.As(err, &u):
				refused = refused.Set(name, pyjson.Object{{Key: "code", Value: "no_access"},
					{Key: "error", Value: fmt.Sprintf("You cannot read the dataset «%s» this tile needs: ask whoever manages its data", name)}})
				continue
			case errors.As(err, &r):
				if o, ok := r.Detail.(pyjson.Object); r.Status >= 500 || (ok && has(o, "parameter")) {
					return nil, nil, err
				}
				refused = refused.Set(name, pyjson.Object{{Key: "code", Value: "dataset"}, {Key: "error", Value: pystr.Cut(reports.Text(r), 2000)}})
				continue
			}
			return nil, nil, err
		}
		for _, kv := range got {
			resolved = resolved.Set(kv.Key, kv.Value)
		}
	}
	return resolved, refused, nil
}

func has(o pyjson.Object, k string) bool {
	_, ok := o.Get(k)
	return ok
}

// PlanOf is _plan: with the person's rights, whoever asks; a dataset they may not read is only
// its tiles' refusal.
func PlanOf(ctx context.Context, rs *reports.Resolver, def pyjson.Object, asked *Asked, cache pyjson.Object) (*Plan, error) {
	shaped := AsReport(def)
	values, err := rs.ValuesFor(ctx, shaped, asked.Given)
	if err != nil {
		return nil, InOurWordsErr(err)
	}
	datasets, refused, err := ResolveEach(ctx, rs, shaped, values)
	if err != nil {
		return nil, InOurWordsErr(err)
	}
	if err := rs.CheckDatasetChoices(ctx, shaped, values, cache, true); err != nil {
		return nil, InOurWordsErr(err)
	}
	params := pyjson.Object{}
	for _, kv := range values {
		params = append(params, pyjson.KV{Key: kv.Key, Value: reportparams.ToJSON(kv.Value)})
	}
	return &Plan{Shaped: reportdef.WithoutImages(shaped), Params: params, Datasets: datasets, Refused: refused}, nil
}

// CacheOf is cache_of: what the engine's cache is told of a saved dashboard; `refresh` asks the
// engine for a fresh reading.
func CacheOf(ctx context.Context, eng *engine.Client, def pyjson.Object, id int64, refresh bool) pyjson.Object {
	interval := get(def, "refresh_seconds")
	if !pystr.IsInt(interval) {
		interval = int64(0)
	}
	cache := pyjson.Object{{Key: "refresh_seconds", Value: interval}}
	if refresh {
		cctx, cancel := context.WithTimeout(ctx, engine.Check)
		defer cancel()
		res, raw, err := eng.JSON(cctx, http.MethodPost, "/reports/tiles/refresh", pyjson.Object{{Key: "dashboard", Value: id}})
		if err == nil && res.StatusCode == 200 {
			if v, err := pyjson.Loads(string(raw)); err == nil {
				if fresh := get(objOf(v), "fresh"); pystr.Truthy(fresh) {
					cache = append(cache, pyjson.KV{Key: "fresh", Value: fresh})
				}
			}
		}
	}
	return cache
}

func objOf(v any) pyjson.Object {
	o, _ := v.(pyjson.Object)
	return o
}

// SlotOf is slot_of: a definition being edited, by person and tiles.
func SlotOf(userID int64, where string, asked *Asked) string {
	text := "null"
	if asked.HasTiles {
		var ids []string
		for _, t := range asked.Tiles {
			ids = append(ids, pystr.Str(t))
		}
		sort.Strings(ids)
		parts := make([]any, len(ids))
		for i, s := range ids {
			parts[i] = s
		}
		text, _ = pyjson.Dumps(parts, pyjson.Options{EnsureASCII: true})
		if len(parts) == 0 {
			text = "[]"
		}
	}
	sum := sha256.Sum256([]byte(text))
	return fmt.Sprintf("u%d:dashboard:%s:%s", userID, where, hex.EncodeToString(sum[:])[:16])
}

// Computed is _computed: the tiles' numbers from the engine.
func Computed(ctx context.Context, eng *engine.Client, plan *Plan, asked *Asked, title string, slot string, cache pyjson.Object) (pyjson.Object, error) {
	var tiles any
	if asked.HasTiles {
		tiles = asked.Tiles
	}
	var state any
	if asked.HasState {
		state = asked.State
	}
	pages := asked.Pages
	if pages == nil {
		pages = pyjson.Object{}
	}
	shaped := plan.Shaped.(pyjson.Object)
	body := pyjson.Object{{Key: "definition", Value: shaped}, {Key: "datasets", Value: plan.Datasets}, {Key: "refused", Value: plan.Refused},
		{Key: "params", Value: plan.Params}, {Key: "tiles", Value: tiles}, {Key: "pages", Value: pages}, {Key: "state", Value: state},
		{Key: "clicks", Value: asked.Clicks}, {Key: "locale", Value: or(get(shaped, "locale"), "en")}, {Key: "title", Value: pystr.Cut(title, 300)}}
	if cache != nil {
		body = append(body, pyjson.KV{Key: "cache", Value: cache})
	} else {
		body = append(body, pyjson.KV{Key: "slot", Value: slot})
	}
	cctx, cancel := context.WithTimeout(ctx, TilesSeconds*time.Second)
	defer cancel()
	res, raw, err := eng.JSON(cctx, http.MethodPost, "/reports/tiles", body)
	if err != nil {
		return nil, &reports.EngineDown{Err: err}
	}
	switch {
	case res.StatusCode == 409:
		return nil, refuse(409, "A newer request for the same tiles took the place of this one")
	case res.StatusCode == 404 || res.StatusCode == 405:
		return nil, refuse(503, "The engine cannot read dashboards yet: update it to the gateway's version")
	case res.StatusCode >= 400:
		return nil, reports.EngineFailure(res.StatusCode, raw, pystr.Sentence)
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, reports.ErrInternal
	}
	answer, ok := v.(pyjson.Object)
	if !ok {
		return nil, reports.ErrInternal
	}
	return answer, nil
}

// Finished is _finished: what the engine does not need to know, a tile's title and an image's
// picture, put in the answer.
func Finished(def pyjson.Object, answer pyjson.Object) (pyjson.Object, error) {
	tiles := map[string]pyjson.Object{}
	for _, t := range list(or(get(def, "tiles"), []any{})) {
		o := objOf(t)
		tiles[pystr.Str(get(o, "id"))] = o
	}
	got, has := answer.Get("tiles")
	if !has {
		got = pyjson.Object{}
	}
	tilesOut, ok := got.(pyjson.Object)
	if !ok {
		return nil, reports.ErrInternal
	}
	for _, kv := range tilesOut {
		out, ok := kv.Value.(pyjson.Object)
		if !ok {
			return nil, reports.ErrInternal
		}
		if _, isErr := out.Get("error"); isErr {
			continue
		}
		tile, known := tiles[kv.Key]
		if !known {
			return nil, reports.ErrInternal
		}
		out = out.Set("title", or(get(tile, "title"), or(get(tile, "label"), "")))
		if get(out, "type") == "image" && !pystr.Truthy(get(out, "hidden")) {
			out = out.Set("src", or(get(tile, "asset"), nil))
		}
		tilesOut = tilesOut.Set(kv.Key, out)
	}
	return answer.Set("tiles", tilesOut), nil
}

// Numbers is numbers: the numbers of a definition the person may open, tile by tile. `saved`
// (not 0): the dashboard's id, whose numbers are shared; otherwise `editing` names the
// definition being edited.
func Numbers(ctx context.Context, rs *reports.Resolver, userID int64, def pyjson.Object, asked *Asked, title string, saved int64,
	editing string) (pyjson.Object, error) {
	if err := CheckAsked(def, asked); err != nil {
		return nil, err
	}
	var cache pyjson.Object
	if saved != 0 {
		cache = CacheOf(ctx, rs.Engine, def, saved, asked.Refresh)
	}
	plan, err := PlanOf(ctx, rs, def, asked, cache)
	if err != nil {
		return nil, err
	}
	slot := ""
	if saved == 0 {
		slot = SlotOf(userID, editing, asked)
	}
	answer, err := Computed(ctx, rs.Engine, plan, asked, title, slot, cache)
	if err != nil {
		return nil, err
	}
	return Finished(def, answer)
}

// TilesOut is the answer's model: its tiles and what was cut, nothing else.
func TilesOut(answer pyjson.Object) (pyjson.Object, error) {
	tiles, okT := answer.Get("tiles")
	truncated, okC := answer.Get("truncated")
	if !okT || !okC {
		return nil, reports.ErrInternal
	}
	tr, ok := truncated.(pyjson.Object)
	if !ok {
		return nil, reports.ErrInternal
	}
	clean := pyjson.Object{}
	for _, kv := range tr {
		b, isBool := kv.Value.(bool)
		if !isBool {
			n, isNum := pystr.Number(kv.Value)
			if !isNum || (n != 0 && n != 1) {
				return nil, reports.ErrInternal
			}
			b = n == 1
		}
		clean = append(clean, pyjson.KV{Key: kv.Key, Value: b})
	}
	return pyjson.Object{{Key: "tiles", Value: tiles}, {Key: "truncated", Value: clean}}, nil
}

// ── a document ───────────────────────────────────────────────────────────────

// LaidOut is laid_out: what the engine lays a document out from.
func LaidOut(def pyjson.Object) pyjson.Object {
	var tiles []any
	for _, t := range list(or(get(def, "tiles"), []any{})) {
		o, ok := t.(pyjson.Object)
		if !ok {
			continue
		}
		tiles = append(tiles, pyjson.Object{{Key: "id", Value: get(o, "id")}, {Key: "title", Value: or(get(o, "title"), "")},
			{Key: "grid", Value: get(o, "grid")}})
	}
	if tiles == nil {
		tiles = []any{}
	}
	return pyjson.Object{{Key: "definition", Value: AsReport(def)}, {Key: "page", Value: get(def, "page")}, {Key: "tiles", Value: tiles}}
}

// ReadClick is read_click: a value of a click as an address writes it.
func ReadClick(text string) any {
	if text == "{none}" {
		return nil
	}
	if strings.HasPrefix(text, `\`) {
		return text[1:]
	}
	if strings.HasPrefix(text, "[") {
		v, err := pyjson.Loads(text)
		if err != nil {
			return text
		}
		if path, ok := v.([]any); ok && len(path) > 0 {
			for _, x := range path {
				switch x.(type) {
				case nil, string, int64, pyjson.Int, float64, bool:
				default:
					return text
				}
			}
			return path
		}
	}
	return text
}

// InTheAddress is in_the_address: a query string's parameters and clicks.
func InTheAddress(given reports.Given) (reports.Given, []any) {
	params := reports.Given{}
	var clicks []any
	for _, kv := range given {
		if !strings.HasPrefix(kv.Key, "click.") {
			params = append(params, kv)
			continue
		}
		name := strings.TrimPrefix(kv.Key, "click.")
		on := "rows"
		if strings.HasSuffix(name, ".columns") {
			on = "columns"
			name = strings.TrimSuffix(name, ".columns")
		}
		var values []any
		for _, v := range kv.Value.([]any) {
			values = append(values, ReadClick(pystr.Str(v)))
		}
		clicks = append(clicks, pyjson.Object{{Key: "tile", Value: name}, {Key: "on", Value: on}, {Key: "values", Value: values}})
	}
	return params, clicks
}
