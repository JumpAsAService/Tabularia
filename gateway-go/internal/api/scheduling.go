package api

import (
	"net/http"
	"sort"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/cron"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/scheduling.py: the schedules' forecast load (services/schedule_load.py).

func init() {
	register("scheduling", func(d *Deps) []server.Route {
		return []server.Route{{Method: http.MethodGet, Pattern: "/schedule/load",
			Handler: server.Authed(d.Auth, "/schedule/load", d.scheduleLoad)}}
	})
}

// schedule_load.py's guards
const (
	maxFiringsPerSchedule = 5000
	maxCollisions         = 30
)

type loadCell struct {
	Weekday        int  `json:"weekday"`
	Hour           int  `json:"hour"`
	Count          int  `json:"count"`
	PeakConcurrent int  `json:"peak_concurrent"`
	Critical       bool `json:"critical"`
}

type collision struct {
	Weekday   int      `json:"weekday"`
	Hour      int      `json:"hour"`
	Minute    int      `json:"minute"`
	Count     int      `json:"count"`
	Queued    int      `json:"queued"`
	Schedules []string `json:"schedules"`
}

type scheduleLoadOut struct {
	Days           int64       `json:"days"`
	Timezone       string      `json:"timezone"`
	WorkerCapacity int64       `json:"worker_capacity"`
	TotalSchedules int         `json:"total_schedules"`
	TotalFirings   int         `json:"total_firings"`
	Cells          []loadCell  `json:"cells"`
	Collisions     []collision `json:"collisions"`
}

// pyWeekday is datetime.weekday(): Monday 0.
func pyWeekday(t time.Time) int { return (int(t.Weekday()) + 6) % 7 }

// minuteKey is the minute's isoformat with its offset: the key of minute_map.
func minuteKey(l cron.Local) string {
	off := l.Offset()
	sign := "+"
	if off < 0 {
		sign, off = "-", -off
	}
	h, m := int(off/time.Hour), int(off%time.Hour/time.Minute)
	return l.Wall.Truncate(time.Minute).Format("2006-01-02T15:04") + ":00" + sign + twoDigit(h) + ":" + twoDigit(m)
}

func twoDigit(n int) string { return string(rune('0'+n/10)) + string(rune('0'+n%10)) }

// scheduleLoad is GET /schedule/load (compute_schedule_load): the active schedules of the
// readable folders projected over the next `days` days.
func (d *Deps) scheduleLoad(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	var ps pyd.Params
	daysV := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "days", pyd.Int().Ge(1).Le(31), int64(7))
	if resp := ps.Response(); resp != nil {
		resp.Write(w)
		return
	}
	days := daysV.(int64)
	q := d.req(w, r, p)
	zone := d.Panel.AppTimezone()
	loc, err := time.LoadLocation(zone)
	if err != nil {
		q.fail(err)
		return
	}
	capacity := max(1, d.Panel.WorkerCapacity())
	now := cron.FromUTC(time.Now().Truncate(time.Microsecond), loc)
	horizon := now.Wall.AddDate(0, 0, int(days)) // same zone: compared by the wall clock
	out := scheduleLoadOut{Days: days, Timezone: zone, WorkerCapacity: capacity, Cells: []loadCell{}, Collisions: []collision{}}

	readable, err := q.c.Readable(q.ctx)
	if err != nil {
		q.fail(err)
		return
	}
	if len(readable) == 0 {
		server.JSON(w, http.StatusOK, out)
		return
	}
	type sched struct{ name, cron string }
	var schedules []sched
	ids := perm.Sorted(readable)
	for _, query := range []string{
		`SELECT name, run_schedule FROM flows WHERE project_id = ANY($1) AND run_schedule IS NOT NULL`,
		`SELECT name, refresh_schedule FROM datasources WHERE project_id = ANY($1) AND refresh_schedule IS NOT NULL`,
	} {
		rows, err := d.DB.Query(q.ctx, query, ids)
		if err != nil {
			q.fail(err)
			return
		}
		for rows.Next() {
			var s sched
			if err := rows.Scan(&s.name, &s.cron); err != nil {
				rows.Close()
				q.fail(err)
				return
			}
			schedules = append(schedules, s)
		}
		rows.Close()
	}
	if len(schedules) == 0 {
		server.JSON(w, http.StatusOK, out)
		return
	}

	type cellKey struct{ wd, h int }
	grid := map[cellKey]int{}
	var gridOrder []cellKey
	minuteMap := map[string][]string{}
	var minuteOrder []string
	firings := 0
	for _, s := range schedules {
		c, err := cron.Parse(s.cron)
		if err != nil {
			d.Log.Warn("schedule_load: invalid cron", "schedule", s.name, "cron", s.cron)
			continue
		}
		cur := now
		for n := 0; n < maxFiringsPerSchedule; n++ {
			t, err := c.Next(cur)
			if err != nil { // a cron that never fires, saved before it was refused: skipped, as Python
				d.Log.Warn("schedule_load: cron that never fires", "schedule", s.name, "cron", s.cron)
				break
			}
			if t.Wall.After(horizon) {
				break
			}
			cur = t
			k := cellKey{pyWeekday(t.Wall), t.Wall.Hour()}
			if _, ok := grid[k]; !ok {
				gridOrder = append(gridOrder, k)
			}
			grid[k]++
			mk := minuteKey(t)
			if _, ok := minuteMap[mk]; !ok {
				minuteOrder = append(minuteOrder, mk)
			}
			minuteMap[mk] = append(minuteMap[mk], s.name)
			firings++
		}
	}
	peak := map[cellKey]int{}
	for _, mk := range minuteOrder {
		jobs := minuteMap[mk]
		if len(jobs) <= 1 {
			continue
		}
		t, _ := time.Parse("2006-01-02T15:04:05", mk[:19])
		k := cellKey{pyWeekday(t), t.Hour()}
		peak[k] = max(peak[k], len(jobs))
		if int64(len(jobs)) > capacity {
			set := map[string]bool{}
			var names []string
			for _, j := range jobs {
				if !set[j] {
					set[j] = true
					names = append(names, j)
				}
			}
			sort.Strings(names)
			out.Collisions = append(out.Collisions, collision{Weekday: k.wd, Hour: k.h, Minute: t.Minute(), Count: len(jobs),
				Queued: len(jobs) - int(capacity), Schedules: names})
		}
	}
	// sorted by (count, weekday, hour), the largest first; equal ones keep their order
	sort.SliceStable(out.Collisions, func(i, j int) bool {
		a, b := out.Collisions[i], out.Collisions[j]
		if a.Count != b.Count {
			return a.Count > b.Count
		}
		if a.Weekday != b.Weekday {
			return a.Weekday > b.Weekday
		}
		return a.Hour > b.Hour
	})
	if len(out.Collisions) > maxCollisions {
		out.Collisions = out.Collisions[:maxCollisions]
	}
	for _, k := range gridOrder {
		out.Cells = append(out.Cells, loadCell{Weekday: k.wd, Hour: k.h, Count: grid[k], PeakConcurrent: max(peak[k], 1),
			Critical: int64(peak[k]) > capacity})
	}
	out.TotalSchedules, out.TotalFirings = len(schedules), firings
	server.JSON(w, http.StatusOK, out)
}
