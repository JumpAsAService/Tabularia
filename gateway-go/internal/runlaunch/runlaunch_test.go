package runlaunch

import (
	"regexp"
	"testing"
)

func TestSnapshotKey(t *testing.T) {
	re := regexp.MustCompile(`^datasets/(new|42)/\d{8}T\d{6}Z-[0-9a-f]{8}\.parquet$`)
	id := int64(42)
	for _, k := range []string{SnapshotKey(nil), SnapshotKey(&id)} {
		if !re.MatchString(k) {
			t.Errorf("snapshot key %q", k)
		}
	}
	if h := Hex32(); len(h) != 32 || h[12] != '4' {
		t.Errorf("uuid4 hex %q", h)
	}
}

func TestCheckSFTPFileName(t *testing.T) {
	for name, refused := range map[string]bool{"": true, "a.csv": false, "a/b.csv": true, `a\b`: true, "{a/b}.csv": false,
		"{{a/b}}.csv": true, "r_{run_id}.csv": false, "{x}/y": true} {
		if got := checkSFTPFileName(name) != nil; got != refused {
			t.Errorf("%q: refused %v", name, got)
		}
	}
}

func TestTaskID(t *testing.T) {
	for raw, want := range map[string]string{`{"task_id": "a"}`: "a", `{"task_id": 7}`: "7", `{"task_id": true}`: "true", `{"task_id": 1.5}`: "1.5"} {
		if got, ref, err := TaskID([]byte(raw)); got != want || ref != nil || err != nil {
			t.Errorf("%s: %q %v %v", raw, got, ref, err)
		}
	}
	for _, raw := range []string{`{}`, `{"task_id": ""}`, `{"task_id": 0}`} {
		if _, ref, err := TaskID([]byte(raw)); ref == nil || ref.Status != 502 || err != nil {
			t.Errorf("%s: %v %v", raw, ref, err)
		}
	}
	for _, raw := range []string{`x`, `[1]`, `{"task_id": [1]}`} {
		if _, _, err := TaskID([]byte(raw)); err == nil {
			t.Errorf("%s: no error", raw)
		}
	}
}
