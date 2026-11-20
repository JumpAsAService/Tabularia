package api

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// A Predict run (kind model_predict) is a Job the orchestration follows, like a Script, a Container
// or a model run: reading it never asks the engine for a task it does not have (an empty task_id),
// an orphan is closed saying so and its Job deleted, and a long one that is alive is left running.

func (f *modelsFixture) predictRun(t *testing.T, kind string, started time.Time, heartbeat *time.Time, status string) int64 {
	t.Helper()
	var id int64
	err := f.db.QueryRow(f.ctx, `INSERT INTO runs (kind, task_id, status, launched_by, trigger_type, input_key, output_bucket, output_key,
		publish_description, publish_overwrite, publish_sort_keys, script, started_at, heartbeat_at)
		VALUES ($1, '', $2, $3, 'manual', '', '', '', '', false, '[]', '{"namespace":"script-trials"}', $4, $5) RETURNING id`,
		kind, status, f.ann.ID, started, heartbeat).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = f.db.Exec(f.ctx, `DELETE FROM runs WHERE id = $1`, id) })
	return id
}

func TestAnOrphanedPredictRunIsClosedAndItsJobDeleted(t *testing.T) {
	f := newModelsFixture(t)
	f.d.Panel = settings.New(f.d.Config, false, f.d.Log)
	for _, kind := range []string{"model", "model_predict"} {
		old := time.Now().UTC().Add(-10 * time.Minute)
		id := f.predictRun(t, kind, time.Now().UTC().Add(-20*time.Minute), &old, "STARTED")
		req := f.d.jobRequest(f.ctx, f.ann)
		run, err := req.readRun(id)
		if err != nil || run == nil {
			t.Fatal(err)
		}
		run, err = req.reconcile(run, progressOf{})
		if err != nil {
			t.Fatal(err)
		}
		if run.Status != "FAILURE" || run.Error == nil || !strings.Contains(*run.Error, "the process running it is gone") {
			t.Errorf("%s: %s %v", kind, run.Status, run.Error)
		}
		if strings.Contains(deref(run.Error), "Timeout") {
			t.Errorf("%s: closed as a timeout: %s", kind, *run.Error)
		}
		f.kube.mu.Lock()
		got := strings.Join(f.kube.selectors, " ")
		f.kube.mu.Unlock()
		if !strings.Contains(got, labelScriptRun+"="+fmt.Sprint(id)) {
			t.Errorf("%s: the Job was not deleted: %q", kind, got)
		}
	}
}

func TestALongPredictRunThatIsAliveIsNotTimedOut(t *testing.T) {
	f := newModelsFixture(t)
	f.d.Panel = settings.New(f.d.Config, false, f.d.Log)
	beat := time.Now().UTC().Add(-5 * time.Second)
	// started far beyond any RUN_STALE_TIMEOUT, its process still beats
	id := f.predictRun(t, "model_predict", time.Now().UTC().Add(-72*time.Hour), &beat, "STARTED")
	req := f.d.jobRequest(f.ctx, f.ann)
	run, err := req.readRun(id)
	if err != nil || run == nil {
		t.Fatal(err)
	}
	run, err = req.reconcile(run, progressOf{})
	if err != nil {
		t.Fatal(err)
	}
	if run.Status != "STARTED" || run.Error != nil {
		t.Errorf("a live Predict: %s %v", run.Status, deref(run.Error))
	}
}
