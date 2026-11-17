package api

import (
	"context"
	"os"
	"reflect"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// What the home page's widgets read: only what the reader may see now, in every list.
func TestHomeData(t *testing.T) {
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
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := tx.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	user := func(email string) int64 {
		return one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, '', 'x', true, false, false, false, now()) RETURNING id`, email)
	}
	ann, bob := user("hd-ann@x.test"), user("hd-bob@x.test")
	folder := func(name string) int64 {
		return one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, name)
	}
	open, closed := folder("hd-open"), folder("hd-closed")
	// next: when it is due, in minutes from now (nil: not scheduled)
	flow := func(name string, project, owner int64, next any) int64 {
		return one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at, run_schedule, run_scheduled_by, next_run_at)
			VALUES ($1, '', $2, $3, '{}', 'duckdb', now(), now(), CASE WHEN $4::int IS NULL THEN NULL ELSE '0 6 * * *' END, $3,
				now() + make_interval(mins => $4::int)) RETURNING id`, name, project, owner, next)
	}
	soon := flow("hd soon", open, ann, 60)
	later := flow("hd later", open, bob, 600)
	far := flow("hd next week", open, ann, 60*24*5)
	hidden := flow("hd hidden", closed, bob, 30)
	plain := flow("hd not scheduled", open, ann, nil)
	report := one(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ('hd report', '', $1, $2, 1, now(), now()) RETURNING id`, open, bob)
	ds := one(`INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, columns, sort_keys, column_descriptions, kind, created_at, updated_at,
			refresh_schedule, refresh_scheduled_by, next_refresh_at)
		VALUES ('hd table', '', $1, $2, 'b', 'k', '[]', '[]', '{}', 'database', now(), now(), '*/15 * * * *', $2, now() + interval '10 minutes') RETURNING id`, open, bob)

	reader := recentScope{readable: []int64{open}}
	admin := recentScope{admin: true}
	nobody := recentScope{}

	// ── favourites ──
	pin := func(u int64, s recentScope, kind string, id int64) (bool, bool) {
		t.Helper()
		found, full, err := pinFavourite(ctx, tx, u, s, kind, id)
		if err != nil {
			t.Fatal(err)
		}
		return found, full
	}
	favs := func(u int64, s recentScope) []string {
		t.Helper()
		items, err := favourites(ctx, tx, u, s)
		if err != nil {
			t.Fatal(err)
		}
		out := []string{}
		for _, it := range items {
			out = append(out, it.Kind+" "+it.Name)
		}
		return out
	}
	if found, _ := pin(ann, reader, "report", report); !found {
		t.Error("a readable report could not be pinned")
	}
	pin(ann, reader, "flow", soon)
	pin(ann, reader, "flow", soon) // twice is once
	if found, _ := pin(ann, reader, "flow", hidden); found {
		t.Error("an object of a folder she cannot read was pinned")
	}
	if found, _ := pin(ann, reader, "flow", 99999999); found {
		t.Error("an object that does not exist was pinned")
	}
	if got := favs(ann, reader); !reflect.DeepEqual(got, []string{"report hd report", "flow hd soon"}) {
		t.Errorf("ann's favourites: %v", got)
	}
	if got := favs(bob, reader); len(got) != 0 {
		t.Errorf("bob sees ann's favourites: %v", got)
	}
	// the right to read goes: the pin stays and is not listed; it comes back with the right
	if got := favs(ann, nobody); len(got) != 0 {
		t.Errorf("listed without the right to read: %v", got)
	}
	if got := favs(ann, reader); len(got) != 2 {
		t.Errorf("after the right came back: %v", got)
	}
	// an administrator may pin what is in a closed folder
	if found, _ := pin(bob, admin, "flow", hidden); !found {
		t.Error("an administrator could not pin")
	}
	// the object deleted: gone from the list
	if _, err := tx.Exec(ctx, `DELETE FROM reports WHERE id = $1`, report); err != nil {
		t.Fatal(err)
	}
	if got := favs(ann, reader); !reflect.DeepEqual(got, []string{"flow hd soon"}) {
		t.Errorf("after deleting the report: %v", got)
	}
	// the limit
	for i := 0; i < homeFavouritesMax; i++ {
		id := flow("hd many", open, bob, nil)
		if _, full := pin(ann, reader, "flow", id); full != (i >= homeFavouritesMax-2) { // she holds two already (one of them hidden from the list)
			t.Fatalf("pin %d: full=%v", i, full)
		}
	}

	// ── the runs due next ──
	next := func(u int64, s recentScope, hours int, mine bool) []string {
		t.Helper()
		items, err := nextRuns(ctx, tx, u, s, hours, mine)
		if err != nil {
			t.Fatal(err)
		}
		out := []string{}
		for _, it := range items {
			if it.At == nil || it.Schedule == "" {
				t.Errorf("%s: no time or no cron", it.Name)
			}
			out = append(out, it.Kind+" "+it.Name)
		}
		return out
	}
	if got := next(ann, reader, 24, false); !reflect.DeepEqual(got, []string{"datasource hd table", "flow hd soon", "flow hd later"}) {
		t.Errorf("due in a day, all she sees: %v", got)
	}
	if got := next(ann, reader, 24, true); !reflect.DeepEqual(got, []string{"flow hd soon"}) {
		t.Errorf("due in a day, hers: %v", got)
	}
	if got := next(ann, reader, 168, true); !reflect.DeepEqual(got, []string{"flow hd soon", "flow hd next week"}) {
		t.Errorf("due in a week, hers: %v", got)
	}
	if got := next(bob, admin, 1, false); !reflect.DeepEqual(got, []string{"datasource hd table", "flow hd hidden", "flow hd soon"}) {
		t.Errorf("due in an hour, an administrator: %v", got)
	}
	if got := next(ann, nobody, 168, false); len(got) != 0 {
		t.Errorf("nothing readable: %v", got)
	}

	// ── the last runs of flows ──
	run := func(flowID int64, kind, trigger, status string, from, to any) int64 {
		return one(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at)
			VALUES ($1, $2, '', $3, $4, $5, '', 'b', 'k', '', false, '[]', now() - make_interval(secs => $6::int),
				CASE WHEN $7::int IS NULL THEN NULL ELSE now() - make_interval(secs => $7::int) END) RETURNING id`,
			kind, flowID, status, ann, trigger, from, to)
	}
	run(soon, "flow", "manual", "SUCCESS", 4000, 3990)
	run(soon, "flow", "schedule", "FAILURE", 3000, 2970)
	run(soon, "flow", "trial", "SUCCESS", 2500, 2499)       // a node tried from the editor: not a run of the flow
	run(soon, "container", "manual", "SUCCESS", 2400, 2399) // a step's run: not the flow's
	going := run(soon, "flow", "manual", "STARTED", 20, nil)
	for i := 0; i < 8; i++ {
		run(later, "flow", "schedule", "SUCCESS", 900-i*10, 895-i*10)
	}
	run(hidden, "flow", "manual", "SUCCESS", 5, 1)
	run(plain, "flow", "manual", "SUCCESS", 50000, 49990)

	flows, err := flowRuns(ctx, tx, reader, []int64{soon, hidden, later, soon, 99999999}, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(flows) != 2 || flows[0].ID != soon || flows[1].ID != later {
		t.Fatalf("the flows asked, readable, each once, in the order asked: %+v", flows)
	}
	st := []string{}
	for _, r := range flows[0].Runs {
		st = append(st, r.Status+" "+r.Trigger)
	}
	if !reflect.DeepEqual(st, []string{"SUCCESS manual", "FAILURE schedule", "STARTED manual"}) {
		t.Errorf("the runs of the flow, oldest first: %v", st)
	}
	if s := flows[0].Runs[1].Seconds; s == nil || *s < 29 || *s > 31 {
		t.Errorf("thirty seconds: %v", s)
	}
	if last := flows[0].Runs[2]; last.ID != going || last.Seconds != nil {
		t.Errorf("a run still going has no duration: %+v", last)
	}
	if len(flows[1].Runs) != 5 {
		t.Errorf("the last five of eight: %d", len(flows[1].Runs))
	}
	// none asked: the flows that ran most in the last 30 days, among those readable
	flows, err = flowRuns(ctx, tx, reader, nil, 5)
	if err != nil {
		t.Fatal(err)
	}
	ids := []int64{}
	for _, f := range flows {
		ids = append(ids, f.ID)
	}
	if !reflect.DeepEqual(ids, []int64{later, soon, plain}) {
		t.Errorf("the busiest flows, readable: %v (soon %d later %d plain %d hidden %d)", ids, soon, later, plain, hidden)
	}
	if flows, _ = flowRuns(ctx, tx, admin, nil, 5); len(flows) != 4 || flows[0].ID != later || flows[2].ID != hidden {
		t.Errorf("an administrator, none asked: %+v", flows)
	}
	if flows, _ = flowRuns(ctx, tx, nobody, []int64{soon}, 5); len(flows) != 0 {
		t.Errorf("nothing readable: %+v", flows)
	}
	_ = far
	_ = ds
}
