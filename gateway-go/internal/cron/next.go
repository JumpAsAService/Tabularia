package cron

import (
	"sort"
	"time"
)

// Local is an aware datetime as Python holds one: the wall clock in a zone, and `fold`
// for the second of two equal wall times (the hour the clocks go back).
type Local struct {
	Wall time.Time // the wall clock, in a UTC container
	Fold int
	Loc  *time.Location
}

// candidates are the instants a wall time names in the zone, in order (none: it does not
// exist; two: it is ambiguous).
func candidates(wall time.Time, loc *time.Location) []time.Time {
	seen := map[int]bool{}
	var out []time.Time
	for _, probe := range []time.Duration{-36 * time.Hour, 0, 36 * time.Hour} {
		_, off := wall.Add(probe).In(loc).Zone()
		if seen[off] {
			continue
		}
		seen[off] = true
		c := wall.Add(-time.Duration(off) * time.Second)
		if _, o := c.In(loc).Zone(); o == off {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Before(out[j]) })
	return out
}

// UTC is the instant (zoneinfo's rules: fold picks among two; a wall time that does not
// exist reads with the offset before the change for fold 0, after it for fold 1).
func (l Local) UTC() time.Time {
	c := candidates(l.Wall, l.Loc)
	switch len(c) {
	case 0:
		_, before := l.Wall.Add(-36 * time.Hour).In(l.Loc).Zone()
		_, after := l.Wall.Add(36 * time.Hour).In(l.Loc).Zone()
		off := before
		if l.Fold == 1 {
			off = after
		}
		return l.Wall.Add(-time.Duration(off) * time.Second)
	case 1:
		return c[0]
	}
	if l.Fold == 1 {
		return c[len(c)-1]
	}
	return c[0]
}

// Offset is utcoffset().
func (l Local) Offset() time.Duration { return l.Wall.Sub(l.UTC()) }

func (l Local) exists() bool { return len(candidates(l.Wall, l.Loc)) > 0 }

// FromUTC is datetime.astimezone(loc).
func FromUTC(t time.Time, loc *time.Location) Local {
	t = t.UTC()
	_, off := t.In(loc).Zone()
	wall := t.Add(time.Duration(off) * time.Second)
	l := Local{Wall: wall, Loc: loc}
	if c := candidates(wall, loc); len(c) == 2 && t.Equal(c[1]) {
		l.Fold = 1
	}
	return l
}

func successor(date, previous Local) bool { return date.UTC().After(previous.UTC()) }

// addTzinfo is croniter._add_tzinfo for a zoneinfo zone, going forward.
func addTzinfo(date time.Time, previous Local) (Local, bool) {
	result := Local{Wall: date, Fold: 0, Loc: previous.Loc}
	if !result.exists() {
		for !result.exists() {
			result.Wall = result.Wall.Add(time.Minute)
		}
		return result, false
	}
	farther := Local{Wall: date, Fold: 1, Loc: previous.Loc}
	if result.Offset() != farther.Offset() && !successor(result, previous) {
		result = farther
	}
	return result, true
}

func lastDay(year int, month time.Month) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// replace is relativedelta's absolute part: the fields given (-1: kept), the day
// clipped to the month.
func replace(d time.Time, day, hour, minute, second int) time.Time {
	y, m, dd := d.Date()
	h, mi, s := d.Clock()
	if day >= 0 {
		dd = day
	}
	if dd > lastDay(y, m) {
		dd = lastDay(y, m)
	}
	if hour >= 0 {
		h = hour
	}
	if minute >= 0 {
		mi = minute
	}
	if second >= 0 {
		s = second
	}
	return time.Date(y, m, dd, h, mi, s, d.Nanosecond(), time.UTC)
}

// addMonths is relativedelta(months=n) with day=1 (the only use going forward).
func addMonths(d time.Time, n int) time.Time {
	y, m, _ := d.Date()
	total := int(m) - 1 + n
	y += total / 12
	if total%12 < 0 {
		total += 12
		y--
	}
	return time.Date(y, time.Month(total%12+1), 1, 0, 0, 0, d.Nanosecond(), time.UTC)
}

// nextNearestDiff is croniter._get_next_nearest_diff; ok false is None.
func nextNearestDiff(x int, check []value, rangeVal int, loop bool) (int, bool) {
	for _, v := range check {
		d := v.n
		if loop {
			if v.last {
				d = rangeVal
			} else if d > rangeVal {
				continue
			}
		}
		if d >= x {
			return d - x, true
		}
	}
	if !loop {
		return 0, false
	}
	first := check[0].n
	if check[0].last {
		first = rangeVal // croniter would compare «l» with an int here: it never comes first
	}
	return first - x + rangeVal, true
}

func hasStar(list []value) bool {
	for _, v := range list {
		if v.star {
			return true
		}
	}
	return false
}

func hasLast(list []value) bool {
	for _, v := range list {
		if v.last {
			return true
		}
	}
	return false
}

// nthWeekdays is _get_nth_weekday_of_month: the days of the month that are that weekday.
func nthWeekdays(year int, month time.Month, dow int) []int {
	var out []int
	for d := 1; d <= lastDay(year, month); d++ {
		if int(time.Date(year, month, d, 0, 0, 0, 0, time.UTC).Weekday()) == dow {
			out = append(out, d)
		}
	}
	return out
}

// nearestWeekdayOf is _get_nearest_weekday.
func nearestWeekdayOf(year int, month time.Month, day int) int {
	last := lastDay(year, month)
	day = min(day, last)
	wd := time.Date(year, month, day, 0, 0, 0, 0, time.UTC).Weekday()
	switch wd {
	case time.Saturday:
		if day > 1 {
			return day - 1
		}
		return day + 2
	case time.Sunday:
		if day < last {
			return day + 1
		}
		return day - 2
	}
	return day
}

const maxYears = 50

// calc is croniter._calc, going forward.
func (e *expression) calc(now Local, expanded [][]value, aware bool) (Local, error) {
	long := len(expanded) > unixLen
	unaware := now.Wall
	if long {
		unaware = unaware.Add(time.Second).Truncate(time.Second)
	} else {
		unaware = unaware.Add(time.Minute).Truncate(time.Minute)
	}
	month, year := unaware.Month(), unaware.Year()
	currentYear := year

	procYear := func(d time.Time) (int, time.Time) {
		if len(expanded) == yearLen && !hasStar(expanded[yearField]) {
			diff, ok := nextNearestDiff(d.Year(), expanded[yearField], 0, false)
			if !ok {
				return -1, d
			}
			if diff != 0 {
				return 1, time.Date(d.Year()+diff, 1, 1, 0, 0, 0, d.Nanosecond(), time.UTC)
			}
		}
		return 0, d
	}
	procMonth := func(d time.Time) (int, time.Time) {
		if !hasStar(expanded[monthField]) {
			diff, _ := nextNearestDiff(int(d.Month()), expanded[monthField], 12, true)
			if diff != 0 {
				return 1, addMonths(d, diff)
			}
		}
		return 0, d
	}
	procDay := func(d time.Time) (int, time.Time) {
		if !hasStar(expanded[dayField]) {
			days := lastDay(year, month)
			if hasLast(expanded[dayField]) && days == d.Day() {
				return 0, d
			}
			diff, _ := nextNearestDiff(d.Day(), expanded[dayField], days, true)
			if diff != 0 {
				return 1, replace(d, -1, 0, 0, 0).AddDate(0, 0, diff)
			}
		}
		return 0, d
	}
	procDow := func(d time.Time) (int, time.Time) {
		if !hasStar(expanded[dowField]) {
			diff, _ := nextNearestDiff(int(d.Weekday()), expanded[dowField], 7, true)
			if diff != 0 {
				return 1, replace(d, -1, 0, 0, 0).AddDate(0, 0, diff)
			}
		}
		return 0, d
	}
	toNextMonth := func(d time.Time) time.Time {
		return replace(d, -1, 0, 0, 0).AddDate(0, 0, lastDay(year, month)-d.Day()+1)
	}
	procDowNth := func(d time.Time) (int, time.Time) {
		var cands []int
		for wday, nths := range e.nth {
			c := nthWeekdays(d.Year(), d.Month(), wday)
			for _, n := range nths {
				var candidate int
				if n == "l" {
					candidate = c[len(c)-1]
				} else if k := n.(int); len(c) < k {
					continue
				} else {
					candidate = c[k-1]
				}
				if d.Day() <= candidate {
					cands = append(cands, candidate)
				}
			}
		}
		if len(cands) == 0 {
			return 1, toNextMonth(d)
		}
		sort.Ints(cands)
		if diff := cands[0] - d.Day(); diff != 0 {
			return 1, replace(d, -1, 0, 0, 0).AddDate(0, 0, diff)
		}
		return 0, d
	}
	procNearest := func(d time.Time) (int, time.Time) {
		var cands []int
		for _, w := range e.nearest {
			if c := nearestWeekdayOf(d.Year(), d.Month(), w); d.Day() <= c {
				cands = append(cands, c)
			}
		}
		if len(cands) == 0 {
			return 1, toNextMonth(d)
		}
		sort.Ints(cands)
		if diff := cands[0] - d.Day(); diff != 0 {
			return 1, replace(d, -1, 0, 0, 0).AddDate(0, 0, diff)
		}
		return 0, d
	}
	procHour := func(d time.Time) (int, time.Time) {
		if !hasStar(expanded[hourField]) {
			diff, _ := nextNearestDiff(d.Hour(), expanded[hourField], 24, true)
			if diff != 0 {
				return 1, replace(d, -1, -1, 0, 0).Add(time.Duration(diff) * time.Hour)
			}
		}
		return 0, d
	}
	procMinute := func(d time.Time) (int, time.Time) {
		if !hasStar(expanded[minuteField]) {
			diff, _ := nextNearestDiff(d.Minute(), expanded[minuteField], 60, true)
			if diff != 0 {
				return 1, replace(d, -1, -1, -1, 0).Add(time.Duration(diff) * time.Minute)
			}
		}
		return 0, d
	}
	procSecond := func(d time.Time) (int, time.Time) {
		if long {
			if !hasStar(expanded[secondField]) {
				diff, _ := nextNearestDiff(d.Second(), expanded[secondField], 60, true)
				if diff != 0 {
					return 1, d.Add(time.Duration(diff) * time.Second)
				}
			}
		} else {
			d = replace(d, -1, -1, -1, 0)
		}
		return 0, d
	}
	day := procDay
	if len(e.nearest) > 0 {
		day = procNearest
	}
	dow := procDow
	if len(e.nth) > 0 {
		dow = procDowNth
	}
	procs := []func(time.Time) (int, time.Time){procYear, procMonth, day, dow, procHour, procMinute, procSecond}

	for abs(year-currentYear) <= maxYears {
		next, stop := false, false
		for _, proc := range procs {
			changed, d := proc(unaware)
			unaware = d
			if changed < 0 {
				stop = true
				break
			}
			if changed > 0 {
				month, year = unaware.Month(), unaware.Year()
				next = true
				break
			}
		}
		if stop {
			break
		}
		if next {
			continue
		}
		unaware = unaware.Truncate(time.Second)
		if !aware {
			return Local{Wall: unaware, Loc: now.Loc}, nil
		}
		awareTime, exists := addTzinfo(unaware, now)
		if !exists && (!successor(awareTime, now) || hasStar(expanded[hourField])) {
			for !exists {
				l, err := e.calc(Local{Wall: unaware, Loc: now.Loc}, expanded, false)
				if err != nil {
					return Local{}, err
				}
				unaware = l.Wall
				awareTime, exists = addTzinfo(unaware, now)
			}
		}
		delta := awareTime.Offset() - now.Offset()
		if delta == 0 {
			return awareTime, nil
		}
		alt, err := e.calc(Local{Wall: now.Wall.Add(delta), Loc: now.Loc}, expanded, false)
		if err != nil {
			return Local{}, err
		}
		altAware, _ := addTzinfo(alt.Wall, now)
		if !successor(altAware, now) {
			return awareTime, nil
		}
		if successor(awareTime, altAware) {
			return altAware, nil
		}
		return awareTime, nil
	}
	return Local{}, ErrBadDate
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

// Schedule is a parsed expression.
type Schedule struct{ e *expression }

// Parse is croniter(expr): an error when croniter refuses it.
func Parse(expr string) (*Schedule, error) {
	e, err := expand(expr)
	if err != nil {
		return nil, err
	}
	return &Schedule{e}, nil
}

// Next is get_next(datetime) from `current` (an aware local datetime): croniter's
// _calc_next, with the day-of-month OR day-of-week union.
func (s *Schedule) Next(current Local) (Local, error) {
	// croniter goes through a float timestamp and back: microseconds kept
	current = FromUTC(current.UTC().Truncate(time.Microsecond), current.Loc)
	exp := append([][]value(nil), s.e.expanded...)
	if !exp[dayField][0].star && !exp[dowField][0].star {
		bak := exp[dowField]
		exp[dowField] = []value{{star: true}}
		t1, err1 := s.e.calc(current, exp, true)
		exp[dowField] = bak
		exp[dayField] = []value{{star: true}}
		t2, err2 := s.e.calc(current, exp, true)
		if err1 != nil {
			return Local{}, err1
		}
		if err2 != nil {
			return Local{}, err2
		}
		// two aware datetimes with the same tzinfo compare by their wall clocks
		if t1.Wall.Before(t2.Wall) {
			return t1, nil
		}
		return t2, nil
	}
	return s.e.calc(current, exp, true)
}
