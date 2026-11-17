package api

import (
	"context"
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

func homeTypesOf(ws []HomeWidget) []string {
	out := []string{}
	for _, w := range ws {
		out = append(out, w.Type)
	}
	return out
}

// The layout the browser sends: what is accepted comes back in its canonical form (the
// defaults filled in), what is refused says why.
func TestHomeLayoutValidation(t *testing.T) {
	good := `{"v":1,"widgets":[
		{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":3},
		{"id":"b","type":"recent","x":3,"y":0,"w":6,"h":6,"options":{"limit":12,"kinds":["flow","report","flow"]}},
		{"id":"c","type":"flow_runs","x":0,"y":6,"w":8,"h":8,"options":{"flow_ids":[4,9,4]}},
		{"id":"d","type":"shortcuts","x":8,"y":6,"w":4,"h":3,"options":{"items":["/flows","/viewer?ds=3"]}},
		{"id":"e","type":"note","x":0,"y":14,"w":4,"h":4,"options":{"text":"<b>hello</b>"}},
		{"id":"f","type":"clock","x":4,"y":14,"w":3,"h":3,"options":{"zone":"Europe/Rome","hour12":true}}
	]}`
	ws, err := cleanHome([]byte(good), false)
	if err != nil {
		t.Fatal(err)
	}
	if len(ws) != 6 {
		t.Fatalf("%d widgets", len(ws))
	}
	want := map[string]map[string]any{
		"a": {"zone": "", "hour12": false, "seconds": false},
		"b": {"limit": 12, "kinds": []string{"flow", "report"}},
		"c": {"flow_ids": []int64{4, 9}, "runs": 20},
		"d": {"items": []string{"/flows", "/viewer?ds=3"}},
		"e": {"text": "<b>hello</b>"}, // kept as text: the page never renders it as HTML
		"f": {"zone": "Europe/Rome", "hour12": true, "seconds": false},
	}
	for _, w := range ws {
		if !reflect.DeepEqual(w.Options, want[w.ID]) {
			t.Errorf("%s: options %#v, want %#v", w.ID, w.Options, want[w.ID])
		}
	}

	one := func(widget string) string { return `{"v":1,"widgets":[` + widget + `]}` }
	bad := []struct{ body, why string }{
		{`[]`, "must be an object"},
		{`{"v":2,"widgets":[]}`, "v must be 1"},
		{`{"v":1}`, "widgets is required"},
		{`{"v":1,"widgets":[],"extra":1}`, "must be an object"},
		{one(`{"id":"a","type":"weather","x":0,"y":0,"w":3,"h":3}`), `unknown type "weather"`},
		{one(`{"id":"A b","type":"clock","x":0,"y":0,"w":3,"h":3}`), "id must be"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":3},{"id":"a","type":"note","x":3,"y":0,"w":3,"h":3}`), "used twice"},
		{one(`{"id":"a","type":"clock","x":10,"y":0,"w":3,"h":3}`), "does not fit"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":1,"h":3}`), "w must be between 2 and 12"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":99}`), "h must be between"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3}`), "are required"},
		{one(`{"id":"a","type":"clock","x":0.5,"y":0,"w":3,"h":3}`), "whole number"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":3,"user_id":7}`), `unknown field "user_id"`},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":3,"options":{"zone":"Mars/Olympus"}}`), "unknown time zone"},
		{one(`{"id":"a","type":"clock","x":0,"y":0,"w":3,"h":3,"options":{"colour":"red"}}`), `unknown option "colour"`},
		{one(`{"id":"a","type":"recent","x":0,"y":0,"w":6,"h":6,"options":{"limit":500}}`), "limit must be between 5 and 20"},
		{one(`{"id":"a","type":"recent","x":0,"y":0,"w":6,"h":6,"options":{"kinds":["users"]}}`), "unknown kind"},
		{one(`{"id":"a","type":"next_runs","x":0,"y":0,"w":6,"h":6,"options":{"scope":"everyone"}}`), "scope must be"},
		{one(`{"id":"a","type":"flow_runs","x":0,"y":0,"w":6,"h":6,"options":{"flow_ids":[1,2,3,4,5,6]}}`), "at most 5"},
		{one(`{"id":"a","type":"flow_runs","x":0,"y":0,"w":6,"h":6,"options":{"flow_ids":["1 OR 1=1"]}}`), "flows' ids"},
		{one(`{"id":"a","type":"shortcuts","x":0,"y":0,"w":4,"h":3,"options":{"items":["https://elsewhere.example/"]}}`), "a page of this installation"},
		{one(`{"id":"a","type":"shortcuts","x":0,"y":0,"w":4,"h":3,"options":{"items":["//elsewhere.example"]}}`), "a page of this installation"},
		{one(`{"id":"a","type":"shortcuts","x":0,"y":0,"w":4,"h":3,"options":{"items":["javascript:alert(1)"]}}`), "a page of this installation"},
		{one(`{"id":"a","type":"shortcuts","x":0,"y":0,"w":4,"h":3,"options":{"items":["/admin?tab=users"]}}`), "for administrators"},
		{one(`{"id":"a","type":"runs_by_day","x":0,"y":0,"w":6,"h":6,"options":{"days":3}}`), "days must be between 7 and 90"},
		{one(`{"id":"a","type":"datasources","x":0,"y":0,"w":6,"h":6,"options":{"ids":[1,2,3,4,5,6,7,8,9,10,11]}}`), "at most 10"},
		{one(`{"id":"a","type":"note","x":0,"y":0,"w":4,"h":3,"options":{"text":"` + strings.Repeat("x", homeNoteMaxChars+1) + `"}}`), "at most 4000"},
	}
	for _, c := range bad {
		if _, err := cleanHome([]byte(c.body), false); err == nil || !strings.Contains(err.Error(), c.why) {
			t.Errorf("%.80s: %v, want %q", c.body, err, c.why)
		}
	}
	// the limits of the whole layout
	many := make([]string, homeMaxWidgets+1)
	for i := range many {
		many[i] = `{"id":"n` + string(rune('a'+i%26)) + string(rune('a'+i/26)) + `","type":"note","x":0,"y":0,"w":2,"h":2}`
	}
	if _, err := cleanHome([]byte(one(strings.Join(many, ","))), false); err == nil || !strings.Contains(err.Error(), "at most 30 widgets") {
		t.Errorf("31 widgets: %v", err)
	}
	if _, err := cleanHome([]byte(one(strings.Join(many[:homeMaxWidgets], ","))), false); err != nil {
		t.Errorf("30 widgets: %v", err)
	}
	if _, err := cleanHome([]byte(`{"v":1,"widgets":[],"pad":"`+strings.Repeat(" ", homeMaxBytes)+`"}`), false); err == nil || !strings.Contains(err.Error(), "larger than 64 KB") {
		t.Errorf("too large: %v", err)
	}
	// an administrator may keep a shortcut to the administration
	if _, err := cleanHome([]byte(one(`{"id":"a","type":"shortcuts","x":0,"y":0,"w":4,"h":3,"options":{"items":["/admin?tab=users","/audit"]}}`)), true); err != nil {
		t.Errorf("admin shortcuts: %v", err)
	}
}

// Which pages are the administrators'.
func TestHomeAdminPaths(t *testing.T) {
	for path, want := range map[string]bool{"/admin": true, "/admin?tab=users": true, "/admin/x": true, "/queue#top": true,
		"/audit": true, "/cluster": true, "/administrators": false, "/flows": false, "/": false, "/viewer?next=/admin": false} {
		if got := homeAdminPath(path); got != want {
			t.Errorf("%s: %v", path, got)
		}
	}
}

// The default layout: valid by the same rules as a saved one, the same for everyone but
// for the administration's shortcuts, and made only of widgets every installation has.
func TestHomeDefaultLayout(t *testing.T) {
	for _, admin := range []bool{false, true} {
		def := defaultHome(admin)
		b, _ := json.Marshal(map[string]any{"v": 1, "widgets": def})
		again, err := cleanHome(b, admin)
		if err != nil {
			t.Fatalf("admin=%v: the default layout is refused: %v", admin, err)
		}
		b2, _ := json.Marshal(again)
		b1, _ := json.Marshal(def)
		if string(b1) != string(b2) {
			t.Errorf("admin=%v: not canonical:\n%s\n%s", admin, b1, b2)
		}
		// no two widgets on the same cell
		taken := map[[2]int]string{}
		for _, w := range def {
			for x := w.X; x < w.X+w.W; x++ {
				for y := w.Y; y < w.Y+w.H; y++ {
					if other, ok := taken[[2]int{x, y}]; ok {
						t.Fatalf("admin=%v: %s and %s overlap", admin, other, w.ID)
					}
					taken[[2]int{x, y}] = w.ID
				}
			}
		}
	}
	user, admin := defaultHome(false), defaultHome(true)
	if !reflect.DeepEqual(homeTypesOf(user), homeTypesOf(admin)) || len(user) != 9 {
		t.Errorf("types: %v / %v", homeTypesOf(user), homeTypesOf(admin))
	}
	for _, w := range user {
		if w.Type == "shortcuts" {
			for _, s := range w.Options["items"].([]string) {
				if homeAdminPath(s) {
					t.Errorf("a non-administrator's default holds %s", s)
				}
			}
		}
		// nothing that only one kind of installation has (the cluster, the data APIs)
		if homeTypes[w.Type].admin {
			t.Errorf("the default holds an administration widget: %s", w.Type)
		}
	}
	// the administrators' widgets are offered to them alone, and are never in the default
	if got := homeAvailable(true); len(got) != len(homeTypeOrder) || got[len(got)-1] != "backups" {
		t.Errorf("available to an administrator: %v", got)
	}
	for _, name := range homeAvailable(false) {
		if homeTypes[name].admin {
			t.Errorf("%s offered to a non-administrator", name)
		}
	}
	for _, name := range homeTypeOrder {
		if _, ok := homeTypes[name]; !ok {
			t.Errorf("%s is in the catalogue and has no type", name)
		}
	}
	if len(homeTypes) != len(homeTypeOrder) {
		t.Errorf("%d types, %d in the catalogue", len(homeTypes), len(homeTypeOrder))
	}
	if _, err := cleanHome([]byte(`{"v":1,"widgets":[{"id":"a","type":"queue","x":0,"y":0,"w":4,"h":4}]}`), false); err == nil || !strings.Contains(err.Error(), "for administrators") {
		t.Errorf("a non-administrator saved the queue: %v", err)
	}
}

// A saved layout, read by someone whose rights changed since: what is no longer theirs is
// dropped, the rest stays.
func TestHomeForReader(t *testing.T) {
	homeTypes["admin_probe"] = homeType{admin: true, minW: 2, minH: 2, clean: noOptions}
	defer delete(homeTypes, "admin_probe")
	saved := []HomeWidget{
		{ID: "a", Type: "admin_probe", X: 0, Y: 0, W: 3, H: 3},
		{ID: "b", Type: "shortcuts", X: 3, Y: 0, W: 4, H: 3, Options: map[string]any{"items": []any{"/flows", "/admin?tab=users", "/audit"}}},
		{ID: "c", Type: "gone_in_this_version", X: 0, Y: 3, W: 3, H: 3},
		{ID: "d", Type: "recent", X: 0, Y: 6, W: 6, H: 6, Options: map[string]any{"limit": float64(999)}}, // no longer valid
	}
	got := homeForReader(saved, false)
	if !reflect.DeepEqual(homeTypesOf(got), []string{"shortcuts", "recent"}) {
		t.Fatalf("non-admin: %v", homeTypesOf(got))
	}
	if !reflect.DeepEqual(got[0].Options["items"], []string{"/flows"}) || got[1].Options["limit"] != 8 {
		t.Errorf("non-admin options: %#v %#v", got[0].Options, got[1].Options)
	}
	got = homeForReader(saved, true)
	if !reflect.DeepEqual(homeTypesOf(got), []string{"admin_probe", "shortcuts", "recent"}) || len(got[1].Options["items"].([]string)) != 3 {
		t.Errorf("admin: %v %#v", homeTypesOf(got), got[1].Options)
	}
	// and a non-administrator cannot save it
	if _, err := cleanHome([]byte(`{"v":1,"widgets":[{"id":"a","type":"admin_probe","x":0,"y":0,"w":3,"h":3}]}`), false); err == nil || !strings.Contains(err.Error(), "for administrators") {
		t.Errorf("saving an administration widget: %v", err)
	}
}

// In the database: nothing saved is the default; a layout is each person's own; reset
// brings the default back.
func TestHomeStorage(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	user := func(email string) int64 {
		var id int64
		if err := tx.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, false, false, false, now()) RETURNING id`, email).Scan(&id); err != nil {
			t.Fatal(err)
		}
		return id
	}
	ann, bob := user("home-ann@x.test"), user("home-bob@x.test")

	out, err := homeAnswer(ctx, tx, ann, false)
	if err != nil || !out.IsDefault || len(out.Widgets) != 9 || out.V != 1 {
		t.Fatalf("nothing saved: %+v %v", out, err)
	}
	var rows int
	if tx.QueryRow(ctx, `SELECT count(*) FROM user_home`).Scan(&rows); rows != 0 {
		t.Errorf("reading wrote %d rows", rows)
	}

	ws, err := cleanHome([]byte(`{"v":1,"widgets":[{"id":"n","type":"note","x":0,"y":0,"w":4,"h":4,"options":{"text":"ann's"}},
		{"id":"f","type":"recent","x":4,"y":0,"w":8,"h":6,"options":{"kinds":["flow","view"]}}]}`), false)
	if err != nil {
		t.Fatal(err)
	}
	if err := writeHome(ctx, tx, ann, ws); err != nil {
		t.Fatal(err)
	}
	out, err = homeAnswer(ctx, tx, ann, false)
	if err != nil || out.IsDefault || !reflect.DeepEqual(homeTypesOf(out.Widgets), []string{"note", "recent"}) ||
		out.Widgets[0].Options["text"] != "ann's" || !reflect.DeepEqual(out.Widgets[1].Options["kinds"], []string{"flow", "view"}) {
		t.Fatalf("after saving: %+v %v", out, err)
	}
	// saved again: one row, replaced
	if err := writeHome(ctx, tx, ann, ws[:1]); err != nil {
		t.Fatal(err)
	}
	if tx.QueryRow(ctx, `SELECT count(*) FROM user_home WHERE user_id = $1`, ann).Scan(&rows); rows != 1 {
		t.Errorf("%d rows for one person", rows)
	}
	if out, _ = homeAnswer(ctx, tx, ann, false); len(out.Widgets) != 1 {
		t.Errorf("replaced: %+v", out.Widgets)
	}
	// nobody else's
	if out, _ = homeAnswer(ctx, tx, bob, false); !out.IsDefault {
		t.Errorf("bob sees ann's: %+v", out)
	}
	// something unreadable in the column: the default, not an error
	if _, err := tx.Exec(ctx, `INSERT INTO user_home (user_id, definition, updated_at) VALUES ($1, 'not json', now())`, bob); err != nil {
		t.Fatal(err)
	}
	if out, err = homeAnswer(ctx, tx, bob, false); err != nil || !out.IsDefault {
		t.Errorf("unreadable: %+v %v", out, err)
	}
	// reset
	if err := deleteHome(ctx, tx, ann); err != nil {
		t.Fatal(err)
	}
	if out, _ = homeAnswer(ctx, tx, ann, false); !out.IsDefault || len(out.Widgets) != 9 {
		t.Errorf("after reset: %+v", out)
	}
	// the account goes, its home page with it
	if err := writeHome(ctx, tx, ann, ws); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `DELETE FROM users WHERE id = $1`, ann); err != nil {
		t.Fatal(err)
	}
	if tx.QueryRow(ctx, `SELECT count(*) FROM user_home WHERE user_id = $1`, ann).Scan(&rows); rows != 0 {
		t.Errorf("the home page outlived the account")
	}
}
