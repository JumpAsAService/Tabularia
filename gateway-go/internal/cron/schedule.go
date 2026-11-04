package cron

import (
	"fmt"
	"strings"
	"time"
)

// ScheduleError is services/schedule.py ScheduleError: a message for the person.
type ScheduleError struct{ Msg string }

func (e *ScheduleError) Error() string { return e.Msg }

// pyStrip is str.strip().
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return strings.ContainsRune(" \t\n\r\v\f\x1c\x1d\x1e\x1f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000", r)
	})
}

// ValidateSchedule is schedule.validate_schedule: the expression stripped, or the error.
func ValidateSchedule(spec string) (string, error) {
	if pyStrip(spec) == "" {
		return "", &ScheduleError{"Empty cron expression"}
	}
	spec = pyStrip(spec)
	if !IsValid(spec) {
		return "", &ScheduleError{fmt.Sprintf("Invalid cron expression: '%s' (format: 'minute hour day month day-of-week', e.g. '0 3 * * *')", spec)}
	}
	return spec, nil
}

// NextFire is schedule.next_fire: the next instant strictly after `after`, the cron read
// in `zone`'s wall time, in UTC. An expression that never fires is ErrBadDate (a 500 in
// Python).
func NextFire(spec string, after time.Time, zone string) (time.Time, error) {
	spec, err := ValidateSchedule(spec)
	if err != nil {
		return time.Time{}, err
	}
	loc, err := time.LoadLocation(zone)
	if err != nil {
		return time.Time{}, err
	}
	s, err := Parse(spec)
	if err != nil {
		return time.Time{}, err
	}
	n, err := s.Next(FromUTC(after, loc))
	if err != nil {
		return time.Time{}, err
	}
	return n.UTC(), nil
}
