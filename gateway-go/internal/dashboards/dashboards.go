// Package dashboards is services/dashboards.py: screens of live tiles kept in a folder with
// their versions. Their parameters and datasets are a report's, checked by a report's code: a
// tile is a report's item with `grid` in place of x, y, w, h (`AsReport`), and the problems are
// said in a dashboard's words (`InOurWords`).
package dashboards

import (
	"context"
	"fmt"
	"regexp"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reportdef"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reports"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

const (
	MaxTiles, MaxDatasets, MaxParameters = 40, 10, 20
	MinRefreshSeconds, MaxRefreshSeconds = 60, 24 * 3600
	GridColumns, MaxGridRow, MaxTileRows = 12, 1000, 100
)

// Kind is the dashboards' tables (models/dashboard.py).
var Kind = &versioned.Kind{Label: "dashboard", Table: "dashboards", Versions: "dashboard_versions", FK: "dashboard_id",
	Views: "dashboard_views", NotFound: "Dashboard not found", Sizes: []string{"datasets", "parameters", "tiles"},
	Empty: func() pyjson.Object { return Empty("en", "UTC") }}

// Pushmail is where a Pushmail cell names a dashboard.
var Pushmail = versioned.Pushmail{List: "dashboards", IDKey: "dashboardId"}

var (
	tileTypes      = []any{"kpi", "chart", "table", "text", "image", "comment"}
	minTile        = map[string][2]int64{"kpi": {2, 2}, "chart": {4, 4}, "table": {4, 4}, "text": {2, 1}, "image": {1, 1}, "comment": {4, 3}}
	filtersOthers  = map[string]bool{"chart": true, "table": true}
	hasTitle       = map[string]bool{"chart": true, "table": true, "comment": true}
	itemPathRe     = regexp.MustCompile(`^sections\.body\.items\[([0-9]+)\]`)
	tilePath       = regexp.MustCompile(`^tiles\[[0-9]+\]\.?(.*)$`)
	theReport      = regexp.MustCompile(`\b([Tt])he report\b`)
	theItem        = regexp.MustCompile(`\b([Tt])he item\b`)
	reportedFields = []string{"locale", "timezone", "parameters", "datasets"}
)

func get(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

// Empty is empty: a new dashboard's definition, no data yet, refreshed by hand.
func Empty(locale, zone string) pyjson.Object {
	if !reports.HasLocale(locale) {
		locale = "en"
	}
	if zone == "" {
		zone = "UTC"
	}
	return pyjson.Object{{Key: "v", Value: int64(1)}, {Key: "locale", Value: locale}, {Key: "timezone", Value: zone},
		{Key: "refresh_seconds", Value: int64(0)}, {Key: "parameters", Value: []any{}}, {Key: "datasets", Value: []any{}},
		{Key: "tiles", Value: []any{}}}
}

// AsReport is as_report: the definition as a report's, each tile an item in a row of its own
// on the largest page there is.
func AsReport(definition pyjson.Object) pyjson.Object {
	var items []any
	tiles, _ := get(definition, "tiles").([]any)
	for i, t := range tiles {
		tile, ok := t.(pyjson.Object)
		if !ok {
			continue
		}
		item := pyjson.Object{}
		for _, kv := range tile {
			if kv.Key == "grid" || kv.Key == "filters_others" {
				continue
			}
			if kv.Key == "title" {
				if typ, ok := get(tile, "type").(string); !ok || !hasTitle[typ] {
					continue
				}
			}
			item = append(item, kv)
		}
		item = item.Set("x", int64(0)).Set("y", int64(i*11)).Set("w", int64(100)).Set("h", int64(10))
		items = append(items, item)
	}
	if items == nil {
		items = []any{}
	}
	shaped := pyjson.Object{{Key: "v", Value: get(definition, "v")},
		{Key: "page", Value: pyjson.Object{{Key: "size", Value: pyjson.Object{{Key: "w", Value: int64(2000)}, {Key: "h", Value: int64(2000)}}},
			{Key: "margins", Value: []any{int64(0), int64(0), int64(0), int64(0)}}}},
		{Key: "sections", Value: pyjson.Object{{Key: "body", Value: pyjson.Object{{Key: "items", Value: items}}}}}}
	for _, key := range reportedFields {
		if v, ok := definition.Get(key); ok {
			shaped = append(shaped, pyjson.KV{Key: key, Value: v})
		}
	}
	return shaped
}

// InOurWords is _in_our_words: a report's message about a dashboard.
func InOurWords(message string) string {
	message = theReport.ReplaceAllString(message, "${1}he dashboard")
	return theItem.ReplaceAllString(message, "${1}he tile")
}

// said is _said: a problem found by a report's code, with the dashboard's words and paths.
func said(p *reportdef.Problem) *reportdef.Problem {
	where := itemPathRe.ReplaceAllString(p.Where, "tiles[$1]")
	return &reportdef.Problem{Message: InOurWords(p.Message), Where: where, Item: p.Item}
}

// problemDict is _problem_dict: the property inside the tile.
func problemDict(p *reportdef.Problem, definition any) pyjson.Object {
	d := p.Dict(definition)
	if p.Item != nil {
		if m := tilePath.FindStringSubmatch(p.Where); m != nil {
			d = d.Set("field", m[1])
		}
	}
	return d
}

func problem(message, where string, item *string) error {
	return &reportdef.Problem{Message: message, Where: where, Item: item}
}

func atMost(value any, most int, key, what string) ([]any, error) {
	if value == nil {
		return nil, nil
	}
	l, ok := value.([]any)
	if !ok {
		return nil, problem(fmt.Sprintf("The %s are a list of at most %d", what, most), key, nil)
	}
	if len(l) > most {
		return nil, problem(fmt.Sprintf("A dashboard holds at most %d %s: this one has %d", most, what, len(l)), key, nil)
	}
	return l, nil
}

func whole(value any, where, what string, lo, hi int64, item *string) (int64, error) {
	if !pystr.IsInt(value) {
		return 0, problem(what+" is a whole number", where, item)
	}
	f, _ := pystr.Number(value)
	if !(float64(lo) <= f && f <= float64(hi)) {
		return 0, problem(fmt.Sprintf("%s is between %d and %d", what, lo, hi), where, item)
	}
	return int64(f), nil
}

func keys(value any, where string, allowed []string, what string, item *string) (pyjson.Object, error) {
	o, ok := value.(pyjson.Object)
	if !ok {
		return nil, problem(what+" is an object", where, item)
	}
	allow := map[string]bool{}
	for _, a := range allowed {
		allow[a] = true
	}
	var unknown []string
	for _, kv := range o {
		if !allow[kv.Key] {
			unknown = append(unknown, kv.Key)
		}
	}
	if len(unknown) > 0 {
		sortStrings(unknown)
		return nil, problem(fmt.Sprintf("%s: unknown key «%s»", what, pystr.Cut(unknown[0], 40)), where, item)
	}
	return o, nil
}

func grid(tile pyjson.Object, at, what string, item *string) ([4]int64, error) {
	where := at + ".grid"
	g, err := keys(get(tile, "grid"), where, []string{"x", "y", "w", "h"}, what+"'s grid", item)
	if err != nil {
		return [4]int64{}, err
	}
	for _, k := range []string{"x", "y", "w", "h"} {
		if get(g, k) == nil {
			return [4]int64{}, problem(fmt.Sprintf("%s's grid: «%s» is missing", what, k), where+"."+k, item)
		}
	}
	x, err := whole(get(g, "x"), where+".x", what+"'s column", 0, GridColumns-1, item)
	if err != nil {
		return [4]int64{}, err
	}
	y, err := whole(get(g, "y"), where+".y", what+"'s row", 0, MaxGridRow, item)
	if err != nil {
		return [4]int64{}, err
	}
	w, err := whole(get(g, "w"), where+".w", what+"'s width", 1, GridColumns, item)
	if err != nil {
		return [4]int64{}, err
	}
	h, err := whole(get(g, "h"), where+".h", what+"'s height", 1, MaxTileRows, item)
	if err != nil {
		return [4]int64{}, err
	}
	if x+w > GridColumns {
		return [4]int64{}, problem(fmt.Sprintf("%s goes past the %d columns of the grid", what, GridColumns), where, item)
	}
	least := minTile[get(tile, "type").(string)]
	if w < least[0] {
		return [4]int64{}, problem(fmt.Sprintf("%s is at least %d columns wide", what, least[0]), where, item)
	}
	if h < least[1] {
		return [4]int64{}, problem(fmt.Sprintf("%s is at least %d rows high", what, least[1]), where, item)
	}
	return [4]int64{x, y, w, h}, nil
}

func tilesCheck(tiles []any) error {
	ids := map[string]bool{}
	type placed struct {
		id  string
		box [4]int64
	}
	var done []placed
	for i, t := range tiles {
		at := fmt.Sprintf("tiles[%d]", i)
		tile, ok := t.(pyjson.Object)
		if !ok {
			return problem("A tile is an object", at, nil)
		}
		id, ok := get(tile, "id").(string)
		if !ok || !reportdef.ItemID.MatchString(id) {
			return problem("A tile needs an id (letters, digits, _ and -)", at+".id", nil)
		}
		item := &id
		if ids[id] {
			return problem(fmt.Sprintf("Two tiles have the id «%s»", id), at+".id", item)
		}
		ids[id] = true
		what := fmt.Sprintf("The tile «%s»", id)
		if !pystr.In(get(tile, "type"), tileTypes...) {
			return problem(fmt.Sprintf("A tile is one of kpi, chart, table, text, image, comment: not «%s»", pystr.Cut(pystr.Str(get(tile, "type")), 20)),
				at+".type", item)
		}
		for _, k := range []string{"x", "y", "w", "h"} {
			if _, ok := tile.Get(k); ok {
				return problem(fmt.Sprintf("%s takes its place from «grid»: not «%s»", what, k), at+"."+k, item)
			}
		}
		if get(tile, "grid") == nil {
			return problem(what+": «grid» is missing", at+".grid", item)
		}
		box, err := grid(tile, at, what, item)
		if err != nil {
			return err
		}
		for _, o := range done {
			if box[0] < o.box[0]+o.box[2] && o.box[0] < box[0]+box[2] && box[1] < o.box[1]+o.box[3] && o.box[1] < box[1]+box[3] {
				return problem(fmt.Sprintf("%s lies on «%s»: two tiles do not share a cell", what, o.id), at+".grid", item)
			}
		}
		done = append(done, placed{id, box})
		typ := get(tile, "type").(string)
		if !hasTitle[typ] {
			if v := get(tile, "title"); v != nil {
				s, ok := v.(string)
				if !ok {
					return problem(what+"'s title is a text", at+".title", item)
				}
				if pystr.Len(s) > 200 {
					return problem(what+"'s title is at most 200 characters", at+".title", item)
				}
			}
		}
		if fo, ok := tile.Get("filters_others"); ok {
			if !filtersOthers[typ] {
				return problem(what+" does not filter the others: only a chart or a table does", at+".filters_others", item)
			}
			if _, ok := fo.(bool); !ok {
				return problem(what+"'s filters_others is true or false", at+".filters_others", item)
			}
		}
	}
	return nil
}

// Check is check: the definition as it will be saved, or the Problem saying what is wrong.
func Check(definition any) (pyjson.Object, error) {
	def, ok := definition.(pyjson.Object)
	if !ok {
		return nil, problem("The dashboard's definition is an object", "", nil)
	}
	allowed := map[string]bool{"v": true, "locale": true, "timezone": true, "refresh_seconds": true, "page": true, "parameters": true,
		"datasets": true, "tiles": true}
	var unknown []string
	for _, kv := range def {
		if !allowed[kv.Key] {
			unknown = append(unknown, kv.Key)
		}
	}
	if len(unknown) > 0 {
		sortStrings(unknown)
		return nil, problem(fmt.Sprintf("The dashboard: unknown key «%s»", pystr.Cut(unknown[0], 40)), "", nil)
	}
	if v := get(def, "v"); v == nil || v == "" {
		return nil, problem("The dashboard: «v» is missing", ".v", nil)
	}
	if !pystr.Eq(get(def, "v"), int64(1)) {
		return nil, problem("The dashboard's definition is version 1 (v: 1)", "v", nil)
	}
	if rs, ok := def.Get("refresh_seconds"); ok {
		f, _ := pystr.Number(rs)
		if !pystr.IsInt(rs) || f != 0 && !(MinRefreshSeconds <= f && f <= MaxRefreshSeconds) {
			return nil, problem(fmt.Sprintf("The refresh is 0 (by hand) or from %d to %d seconds", MinRefreshSeconds, MaxRefreshSeconds), "refresh_seconds", nil)
		}
	}
	if page := get(def, "page"); page != nil {
		if _, _, err := reportdef.Page(page); err != nil {
			return nil, err
		}
	}
	if _, err := atMost(get(def, "parameters"), MaxParameters, "parameters", "parameters"); err != nil {
		return nil, err
	}
	if _, err := atMost(get(def, "datasets"), MaxDatasets, "datasets", "datasets"); err != nil {
		return nil, err
	}
	tiles, err := atMost(get(def, "tiles"), MaxTiles, "tiles", "tiles")
	if err != nil {
		return nil, err
	}
	if err := tilesCheck(tiles); err != nil {
		return nil, err
	}
	shaped := AsReport(def)
	if _, err := reportdef.Check(shaped); err != nil {
		if p, ok := reportdef.AsProblem(err); ok {
			return nil, said(p)
		}
		return nil, err
	}
	// an image as it is kept: its data URL in one piece, as the check wrote it
	items := get(get(get(shaped, "sections").(pyjson.Object), "body").(pyjson.Object), "items").([]any)
	for i, t := range tiles {
		tile := t.(pyjson.Object)
		if get(tile, "type") == "image" {
			tile.Set("asset", get(items[i].(pyjson.Object), "asset"))
		}
	}
	return def, nil
}

// Review is review: every problem of a definition, as for a report.
func Review(ctx context.Context, rv *reports.Reviewer, definition any, current any) (*reports.Review, error) {
	var found []pyjson.Object
	var where *string
	checked, err := Check(definition)
	if err == nil {
		err = reports.CheckReferences(ctx, rv.Q, rv.C, checked, current)
	}
	if err != nil {
		p, ok := reportdef.AsProblem(err)
		if !ok {
			return nil, err
		}
		s := said(p)
		found = append(found, problemDict(s, definition))
		checked, where = nil, &s.Where
	}
	if def, ok := definition.(pyjson.Object); ok {
		var columns pyjson.Object
		if checked != nil {
			if columns, err = reports.KnownColumns(ctx, rv.Q, rv.C, checked); err != nil {
				return nil, err
			}
		}
		seen := map[string]bool{}
		for _, f := range found {
			seen[pairOf(f)] = true
		}
		engineFound, err := reports.EngineProblems(ctx, rv.Engine, rv.Log, AsReport(def), columns)
		if err != nil {
			return nil, err
		}
		for _, e := range engineFound {
			if !seen[pairOf(e)] {
				seen[pairOf(e)] = true
				msg, _ := e.Get("message")
				found = append(found, append(pyjson.Object{}, e...).Set("message", InOurWords(msg.(string))))
			}
		}
	}
	if len(found) > 0 {
		checked = nil
	}
	return &reports.Review{Definition: checked, Problems: found, Where: where}, nil
}

func pairOf(p pyjson.Object) string {
	return pystr.Repr([]any{get(p, "item"), get(p, "field")})
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// CheckParameters is check_parameters: the parameters of a definition being edited, written as
// a report's.
func CheckParameters(definition any) (pyjson.Object, error) {
	if def, ok := definition.(pyjson.Object); ok {
		if _, err := atMost(get(def, "parameters"), MaxParameters, "parameters", "parameters"); err != nil {
			return nil, sayErr(err)
		}
	}
	out, err := reportdef.CheckParameters(definition)
	return out, sayErr(err)
}

// CheckDataset is check_dataset: its parameters and one dataset.
func CheckDataset(definition any, name string) (pyjson.Object, error) {
	out, err := reportdef.CheckDataset(definition, name)
	return out, sayErr(err)
}

func sayErr(err error) error {
	if p, ok := reportdef.AsProblem(err); ok {
		return said(p)
	}
	return err
}
