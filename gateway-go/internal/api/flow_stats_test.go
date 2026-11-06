package api

import (
	"context"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// A flow's stats count its EXECUTIONS: a launch writes the orchestration run and, under it,
// one child per step with the same flow_id; those children are not further runs. A child
// whose parent is not the flow's (a flow group's run) is the flow's only row, so it counts.
func TestFlowRunStatsCountsExecutions(t *testing.T) {
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
	user := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
		VALUES ('fs-ann@x.test', '', 'x', true, false, false, false, now()) RETURNING id`)
	folder := one(`INSERT INTO projects (name, description, created_at) VALUES ('fs-folder', '', now()) RETURNING id`)
	flow := one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('fs flow', '', $1, $2, '{}', 'duckdb', now(), now()) RETURNING id`, folder, user)
	// started/finished seconds ago; parent may be nil; flowID may be nil
	run := func(kind string, flowID any, parent any, status string, from, to int) int64 {
		return one(`INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, parent_run_id, input_key,
			output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, started_at, finished_at)
			VALUES ($1, $2, '', $3, $4, 'manual', $5, '', 'b', 'k', '', false, '[]',
				now() - make_interval(secs => $6), now() - make_interval(secs => $7)) RETURNING id`,
			kind, flowID, status, user, parent, from, to)
	}
	// two executions of the flow, each an orchestration (10 s) with two step children
	for _, st := range []string{"SUCCESS", "FAILURE"} {
		o := run("orchestration", flow, nil, st, 100, 90)
		run("flow", flow, o, st, 99, 97)
		run("flow", flow, o, st, 96, 92)
	}
	// a third: the flow as a cell of a run that is not the flow's (4 s)
	g := run("orchestration", nil, nil, "SUCCESS", 50, 40)
	run("flow", flow, g, "SUCCESS", 49, 45)

	got, err := flowRunStats(ctx, tx, flow)
	if err != nil {
		t.Fatal(err)
	}
	if got.RunCount != 3 || got.SuccessCount != 2 || got.FailureCount != 1 {
		t.Errorf("counts %d/%d/%d, want 3/2/1", got.RunCount, got.SuccessCount, got.FailureCount)
	}
	if got.AvgDurationSeconds == nil || *got.AvgDurationSeconds != 8 {
		t.Errorf("average %v, want 8 (10, 10 and 4 seconds)", got.AvgDurationSeconds)
	}
}
