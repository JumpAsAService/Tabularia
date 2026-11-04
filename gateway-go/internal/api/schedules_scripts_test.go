package api

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// validate_schedule's refusals are the 422 with its text; a valid cron comes back stripped.
func TestTheScheduleReadsTheCronAsValidateScheduleDoes(t *testing.T) {
	for _, c := range []struct {
		in, want string
		status   int
	}{
		{" 0 3 * * * ", "0 3 * * *", 0},
		{"@daily", "@daily", 0},
		{"x", `{"detail":"Invalid cron expression: 'x' (format: 'minute hour day month day-of-week', e.g. '0 3 * * *')"}`, 422},
		{"61 * * * *", `{"detail":"Invalid cron expression: '61 * * * *'`, 422},
	} {
		rec := httptest.NewRecorder()
		got, ok := scValidCron(&Deps{}, rec, c.in)
		if c.status == 0 {
			if !ok || got != c.want {
				t.Errorf("%q → %q %v, want %q", c.in, got, ok, c.want)
			}
			continue
		}
		if ok || rec.Code != c.status || !strings.HasPrefix(rec.Body.String(), c.want) {
			t.Errorf("%q → %d %s, want %d %s", c.in, rec.Code, rec.Body.String(), c.status, c.want)
		}
	}
}
