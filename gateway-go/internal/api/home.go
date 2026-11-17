package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The home page: the grid of widgets each person arranges for themselves
// (docs/specs/home.md). Routes of the Go gateway only (server/go_only_routes.go).
//
// A preference of the person, as the flow editor's menu: no id in the address, an observer
// keeps one too, a read-only API token cannot change it (auth's level check). The layout
// is the widgets' types, places and options, nothing else: what a widget shows comes from
// the endpoints of the pages it mirrors, each with its own rights, so a layout never
// grants anything. Without a saved layout the answer is the default one, held here and
// nowhere else: it is the same on Docker Compose and on Kubernetes, and holds only
// widgets that exist on both.

const (
	homeVersion       = 1
	homeMaxWidgets    = 30
	homeMaxBytes      = 64 << 10
	homeColumns       = 12
	homeMaxRow        = 500
	homeMaxHeight     = 40
	homeNoteMaxChars  = 4000
	homeMaxShortcuts  = 16
	homeMaxFlows      = 5
	homeShortcutChars = 200
)

// HomeWidget is one widget of the grid: its type, its place (12 columns, rows of one unit)
// and its options, which each type validates.
type HomeWidget struct {
	ID      string         `json:"id"`
	Type    string         `json:"type"`
	X       int            `json:"x"`
	Y       int            `json:"y"`
	W       int            `json:"w"`
	H       int            `json:"h"`
	Options map[string]any `json:"options"`
}

// HomeOut is the answer of the three routes: the layout, whether it is the default one,
// and the widget types this person may add.
type HomeOut struct {
	V         int          `json:"v"`
	Widgets   []HomeWidget `json:"widgets"`
	IsDefault bool         `json:"is_default"`
	Available []string     `json:"available"`
}

// homeType is a widget type: who may use it, its smallest size, and how its options are
// checked and brought to their canonical form (defaults filled in). `clean` returns the
// reason a value is refused.
type homeType struct {
	admin      bool
	minW, minH int
	clean      func(o map[string]any, admin bool) (map[string]any, error)
}

// homeTypes are the widgets of the first version, in the catalogue's order.
var homeTypeOrder = []string{"clock", "recent", "favourites", "next_runs", "failed_runs", "flow_durations", "shortcuts", "note"}

var homeTypes = map[string]homeType{
	"clock":          {minW: 2, minH: 2, clean: cleanClock},
	"recent":         {minW: 3, minH: 4, clean: cleanRecent},
	"favourites":     {minW: 3, minH: 4, clean: noOptions},
	"next_runs":      {minW: 3, minH: 4, clean: cleanNextRuns},
	"failed_runs":    {minW: 3, minH: 4, clean: cleanFailedRuns},
	"flow_durations": {minW: 4, minH: 5, clean: cleanFlowDurations},
	"shortcuts":      {minW: 2, minH: 2, clean: cleanShortcuts},
	"note":           {minW: 2, minH: 2, clean: cleanNote},
}

// homeRecentKinds are Explore's kinds a «Recent» widget may be narrowed to.
var homeRecentKinds = map[string]bool{"flow": true, "flow_group": true, "sql_script": true, "script": true,
	"notebook": true, "report": true, "dashboard": true, "saved_view": true, "datasource": true, "data_api": true}

// homeAdminPaths are the pages of the administrators: a shortcut to one of them is theirs alone.
var homeAdminPaths = []string{"/admin", "/queue", "/monitoring", "/performance", "/cluster", "/audit"}

var homeWidgetID = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,31}$`)

func init() {
	register("home", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/home", Handler: server.Authed(d.Auth, "/home", getHome(d))},
			{Method: http.MethodPut, Pattern: "/home", Handler: withBody(server.Authed(d.Auth, "/home", putHome(d)))},
			{Method: http.MethodPost, Pattern: "/home/reset", Handler: server.Authed(d.Auth, "/home/reset", resetHome(d))},
		}
	})
}

// ── options, type by type ────────────────────────────────────────────────────

// optionKeys refuses a key the type does not know: a typo must not be saved in silence.
func optionKeys(o map[string]any, known ...string) error {
	for k := range o {
		ok := false
		for _, n := range known {
			ok = ok || k == n
		}
		if !ok {
			return fmt.Errorf("unknown option %q", k)
		}
	}
	return nil
}

// homeInt is an integer option within [min, max], or its default when absent.
func homeInt(o map[string]any, key string, def, min, max int) (int, error) {
	v, has := o[key]
	if !has || v == nil {
		return def, nil
	}
	f, ok := v.(float64)
	if !ok || f != float64(int(f)) {
		return 0, fmt.Errorf("%s must be a whole number", key)
	}
	if int(f) < min || int(f) > max {
		return 0, fmt.Errorf("%s must be between %d and %d", key, min, max)
	}
	return int(f), nil
}

func homeBool(o map[string]any, key string, def bool) (bool, error) {
	v, has := o[key]
	if !has || v == nil {
		return def, nil
	}
	b, ok := v.(bool)
	if !ok {
		return false, fmt.Errorf("%s must be true or false", key)
	}
	return b, nil
}

// homeScope is «mine» (what the person owns or started) or «all» (everything they may see).
func homeScope(o map[string]any) (string, error) {
	v, has := o["scope"]
	if !has || v == nil {
		return "all", nil
	}
	if s, ok := v.(string); ok && (s == "mine" || s == "all") {
		return s, nil
	}
	return "", errors.New(`scope must be "mine" or "all"`)
}

func noOptions(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

// cleanClock: `zone` is an IANA name, or empty for the browser's own zone.
func cleanClock(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "zone", "hour12", "seconds"); err != nil {
		return nil, err
	}
	zone := ""
	if v, has := o["zone"]; has && v != nil {
		s, ok := v.(string)
		if !ok || len(s) > 64 {
			return nil, errors.New("zone must be a time zone's name")
		}
		if s != "" {
			if _, err := time.LoadLocation(s); err != nil || s == "Local" {
				return nil, fmt.Errorf("unknown time zone %q", s)
			}
		}
		zone = s
	}
	hour12, err := homeBool(o, "hour12", false)
	if err != nil {
		return nil, err
	}
	seconds, err := homeBool(o, "seconds", false)
	if err != nil {
		return nil, err
	}
	return map[string]any{"zone": zone, "hour12": hour12, "seconds": seconds}, nil
}

// cleanRecent: how many, and of which kinds (none named: every kind).
func cleanRecent(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "limit", "kinds"); err != nil {
		return nil, err
	}
	limit, err := homeInt(o, "limit", 8, 5, 20)
	if err != nil {
		return nil, err
	}
	kinds := []string{}
	if v, has := o["kinds"]; has && v != nil {
		list, ok := v.([]any)
		if !ok {
			return nil, errors.New("kinds must be a list")
		}
		seen := map[string]bool{}
		for _, k := range list {
			s, ok := k.(string)
			if !ok || !homeRecentKinds[s] {
				return nil, fmt.Errorf("unknown kind %v", k)
			}
			if !seen[s] {
				kinds = append(kinds, s)
			}
			seen[s] = true
		}
	}
	return map[string]any{"limit": limit, "kinds": kinds}, nil
}

func cleanNextRuns(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "scope", "hours"); err != nil {
		return nil, err
	}
	scope, err := homeScope(o)
	if err != nil {
		return nil, err
	}
	hours, err := homeInt(o, "hours", 24, 1, 168)
	if err != nil {
		return nil, err
	}
	return map[string]any{"scope": scope, "hours": hours}, nil
}

func cleanFailedRuns(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "scope", "days"); err != nil {
		return nil, err
	}
	scope, err := homeScope(o)
	if err != nil {
		return nil, err
	}
	days, err := homeInt(o, "days", 7, 1, 30)
	if err != nil {
		return nil, err
	}
	return map[string]any{"scope": scope, "days": days}, nil
}

// cleanFlowDurations: up to five flows by id; none named means the flows the person ran
// last. The ids are only kept here: the widget's data endpoint checks who may read them.
func cleanFlowDurations(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "flow_ids", "runs"); err != nil {
		return nil, err
	}
	ids := []int64{}
	if v, has := o["flow_ids"]; has && v != nil {
		list, ok := v.([]any)
		if !ok || len(list) > homeMaxFlows {
			return nil, fmt.Errorf("flow_ids must be a list of at most %d flows", homeMaxFlows)
		}
		seen := map[int64]bool{}
		for _, x := range list {
			f, ok := x.(float64)
			if !ok || f != float64(int64(f)) || f < 1 {
				return nil, errors.New("flow_ids must be flows' ids")
			}
			if !seen[int64(f)] {
				ids = append(ids, int64(f))
			}
			seen[int64(f)] = true
		}
	}
	runs, err := homeInt(o, "runs", 20, 5, 50)
	if err != nil {
		return nil, err
	}
	return map[string]any{"flow_ids": ids, "runs": runs}, nil
}

// homeInternalPath is a page of this installation: a path, never an address of elsewhere.
func homeInternalPath(s string) bool {
	if s == "" || len(s) > homeShortcutChars || s[0] != '/' || strings.HasPrefix(s, "//") || !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f || r == '\\' {
			return false
		}
	}
	return true
}

// homeAdminPath says whether a shortcut leads to a page of the administrators.
func homeAdminPath(s string) bool {
	path := s
	if i := strings.IndexAny(path, "?#"); i >= 0 {
		path = path[:i]
	}
	for _, p := range homeAdminPaths {
		if path == p || strings.HasPrefix(path, p+"/") {
			return true
		}
	}
	return false
}

// cleanShortcuts: buttons to pages of the installation. A shortcut to an administration
// page is refused to whoever is not an administrator.
func cleanShortcuts(o map[string]any, admin bool) (map[string]any, error) {
	if err := optionKeys(o, "items"); err != nil {
		return nil, err
	}
	items := []string{}
	if v, has := o["items"]; has && v != nil {
		list, ok := v.([]any)
		if !ok || len(list) > homeMaxShortcuts {
			return nil, fmt.Errorf("items must be a list of at most %d shortcuts", homeMaxShortcuts)
		}
		seen := map[string]bool{}
		for _, x := range list {
			s, ok := x.(string)
			if !ok || !homeInternalPath(s) {
				return nil, errors.New("a shortcut must be a page of this installation, such as /flows")
			}
			if homeAdminPath(s) && !admin {
				return nil, errors.New("a shortcut to an administration page is for administrators")
			}
			if !seen[s] {
				items = append(items, s)
			}
			seen[s] = true
		}
	}
	return map[string]any{"items": items}, nil
}

func cleanNote(o map[string]any, _ bool) (map[string]any, error) {
	if err := optionKeys(o, "text"); err != nil {
		return nil, err
	}
	text := ""
	if v, has := o["text"]; has && v != nil {
		s, ok := v.(string)
		if !ok || utf8.RuneCountInString(s) > homeNoteMaxChars {
			return nil, fmt.Errorf("text must be at most %d characters", homeNoteMaxChars)
		}
		text = s
	}
	return map[string]any{"text": text}, nil
}

// ── the layout ───────────────────────────────────────────────────────────────

// homeAvailable are the types a person may add, in the catalogue's order.
func homeAvailable(admin bool) []string {
	out := []string{}
	for _, name := range homeTypeOrder {
		if admin || !homeTypes[name].admin {
			out = append(out, name)
		}
	}
	return out
}

// cleanHome validates a layout sent by the browser and returns it in its canonical form;
// the error is the 422's text.
func cleanHome(raw []byte, admin bool) ([]HomeWidget, error) {
	if len(raw) > homeMaxBytes {
		return nil, fmt.Errorf("the layout is larger than %d KB", homeMaxBytes>>10)
	}
	var in struct {
		V       *int              `json:"v"`
		Widgets *[]map[string]any `json:"widgets"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		return nil, errors.New("the layout must be an object with v and widgets")
	}
	if in.V == nil || *in.V != homeVersion {
		return nil, fmt.Errorf("v must be %d", homeVersion)
	}
	if in.Widgets == nil {
		return nil, errors.New("widgets is required")
	}
	if len(*in.Widgets) > homeMaxWidgets {
		return nil, fmt.Errorf("at most %d widgets", homeMaxWidgets)
	}
	out := make([]HomeWidget, 0, len(*in.Widgets))
	seen := map[string]bool{}
	for i, m := range *in.Widgets {
		where := fmt.Sprintf("widget %d", i+1)
		for k := range m {
			switch k {
			case "id", "type", "x", "y", "w", "h", "options":
			default:
				return nil, fmt.Errorf("%s: unknown field %q", where, k)
			}
		}
		id, _ := m["id"].(string)
		if !homeWidgetID.MatchString(id) {
			return nil, fmt.Errorf("%s: id must be 1 to 32 lowercase letters, digits or dashes", where)
		}
		if seen[id] {
			return nil, fmt.Errorf("%s: the id %q is used twice", where, id)
		}
		seen[id] = true
		name, _ := m["type"].(string)
		typ, known := homeTypes[name]
		if !known {
			return nil, fmt.Errorf("%s: unknown type %q", where, name)
		}
		if typ.admin && !admin {
			return nil, fmt.Errorf("%s: the widget %q is for administrators", where, name)
		}
		x, errX := homeInt(m, "x", -1, 0, homeColumns-1)
		y, errY := homeInt(m, "y", -1, 0, homeMaxRow)
		w, errW := homeInt(m, "w", -1, typ.minW, homeColumns)
		h, errH := homeInt(m, "h", -1, typ.minH, homeMaxHeight)
		for _, err := range []error{errX, errY, errW, errH} {
			if err != nil {
				return nil, fmt.Errorf("%s: %v", where, err)
			}
		}
		if x < 0 || y < 0 || w < 0 || h < 0 {
			return nil, fmt.Errorf("%s: x, y, w and h are required", where)
		}
		if x+w > homeColumns {
			return nil, fmt.Errorf("%s: it does not fit the %d columns", where, homeColumns)
		}
		options := map[string]any{}
		if v, has := m["options"]; has && v != nil {
			o, ok := v.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("%s: options must be an object", where)
			}
			options = o
		}
		clean, err := typ.clean(options, admin)
		if err != nil {
			return nil, fmt.Errorf("%s (%s): %v", where, name, err)
		}
		out = append(out, HomeWidget{ID: id, Type: name, X: x, Y: y, W: w, H: h, Options: clean})
	}
	return out, nil
}

// homeForReader is a saved layout as its owner may see it NOW: a widget whose type they may
// no longer use is dropped, and so is a shortcut to an administration page; a widget whose
// saved options no longer pass (a type changed between versions) falls back to its defaults.
func homeForReader(saved []HomeWidget, admin bool) []HomeWidget {
	out := make([]HomeWidget, 0, len(saved))
	for _, wd := range saved {
		typ, known := homeTypes[wd.Type]
		if !known || (typ.admin && !admin) {
			continue
		}
		options := wd.Options
		if options == nil {
			options = map[string]any{}
		}
		if wd.Type == "shortcuts" && !admin {
			if items, ok := options["items"].([]any); ok {
				kept := []any{}
				for _, x := range items {
					if s, ok := x.(string); ok && !homeAdminPath(s) {
						kept = append(kept, s)
					}
				}
				options = map[string]any{"items": kept}
			}
		}
		clean, err := typ.clean(options, admin)
		if err != nil {
			clean, _ = typ.clean(map[string]any{}, admin)
		}
		wd.Options = clean
		out = append(out, wd)
	}
	return out
}

// defaultHome is the layout of whoever has not arranged theirs: the same on every
// installation, Docker Compose or Kubernetes, and made of widgets both have. An
// administrator's differs by the shortcuts to the administration alone.
func defaultHome(admin bool) []HomeWidget {
	shortcuts := []string{"/explore", "/flows", "/datasources", "/viewer", "/reports", "/dashboards", "/runs"}
	if admin {
		shortcuts = append(shortcuts, "/admin", "/queue", "/audit")
	}
	layout := []HomeWidget{
		{ID: "clock", Type: "clock", X: 0, Y: 0, W: 3, H: 3},
		{ID: "shortcuts", Type: "shortcuts", X: 3, Y: 0, W: 9, H: 3, Options: map[string]any{"items": toAny(shortcuts)}},
		{ID: "recent", Type: "recent", X: 0, Y: 3, W: 6, H: 8},
		{ID: "favourites", Type: "favourites", X: 6, Y: 3, W: 6, H: 8},
		{ID: "failed-runs", Type: "failed_runs", X: 0, Y: 11, W: 6, H: 7},
		{ID: "next-runs", Type: "next_runs", X: 6, Y: 11, W: 6, H: 7},
		{ID: "flow-durations", Type: "flow_durations", X: 0, Y: 18, W: 8, H: 8},
		{ID: "note", Type: "note", X: 8, Y: 18, W: 4, H: 8},
	}
	return homeForReader(layout, admin) // the options in their canonical form
}

func toAny(list []string) []any {
	out := make([]any, len(list))
	for i, s := range list {
		out[i] = s
	}
	return out
}

// ── the database ─────────────────────────────────────────────────────────────

// readHome is the person's saved layout; found is false when they never changed it, or when
// what is saved cannot be read (the default then).
func readHome(ctx context.Context, q rowQuerier, userID int64) (widgets []HomeWidget, found bool, err error) {
	var raw string
	err = q.QueryRow(ctx, `SELECT definition FROM user_home WHERE user_id = $1`, userID).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	var saved struct {
		Widgets []HomeWidget `json:"widgets"`
	}
	if json.Unmarshal([]byte(raw), &saved) != nil || saved.Widgets == nil {
		return nil, false, nil
	}
	return saved.Widgets, true, nil
}

func writeHome(ctx context.Context, q rowQuerier, userID int64, widgets []HomeWidget) error {
	b, err := json.Marshal(map[string]any{"v": homeVersion, "widgets": widgets})
	if err != nil {
		return err
	}
	_, err = q.Exec(ctx, `INSERT INTO user_home (user_id, definition, updated_at) VALUES ($1, $2, $3)
		ON CONFLICT (user_id) DO UPDATE SET definition = EXCLUDED.definition, updated_at = EXCLUDED.updated_at`,
		userID, string(b), time.Now().UTC())
	return err
}

func deleteHome(ctx context.Context, q rowQuerier, userID int64) error {
	_, err := q.Exec(ctx, `DELETE FROM user_home WHERE user_id = $1`, userID)
	return err
}

// homeAnswer is what the three routes return for a person.
func homeAnswer(ctx context.Context, q rowQuerier, userID int64, admin bool) (HomeOut, error) {
	saved, found, err := readHome(ctx, q, userID)
	if err != nil {
		return HomeOut{}, err
	}
	out := HomeOut{V: homeVersion, IsDefault: !found, Available: homeAvailable(admin)}
	if found {
		out.Widgets = homeForReader(saved, admin)
	} else {
		out.Widgets = defaultHome(admin)
	}
	return out, nil
}

// ── the routes ───────────────────────────────────────────────────────────────

func getHome(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		admin, err := isAdmin(d, r, p)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		out, err := homeAnswer(r.Context(), d.DB, p.User.ID, admin)
		if err != nil {
			fail(d, w, "reading the home page", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

func putHome(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		admin, err := isAdmin(d, r, p)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		widgets, err := cleanHome(rawOf(r), admin)
		if err != nil {
			detail(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		if err := writeHome(r.Context(), d.DB, p.User.ID, widgets); err != nil {
			fail(d, w, "saving the home page", err)
			return
		}
		server.JSON(w, http.StatusOK, HomeOut{V: homeVersion, Widgets: widgets, Available: homeAvailable(admin)})
	}
}

func resetHome(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		admin, err := isAdmin(d, r, p)
		if err != nil {
			fail(d, w, "reading the roles", err)
			return
		}
		if err := deleteHome(r.Context(), d.DB, p.User.ID); err != nil {
			fail(d, w, "resetting the home page", err)
			return
		}
		server.JSON(w, http.StatusOK, HomeOut{V: homeVersion, Widgets: defaultHome(admin), IsDefault: true, Available: homeAvailable(admin)})
	}
}
