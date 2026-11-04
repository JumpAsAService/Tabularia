package main

import (
	"regexp"
	"strconv"
)

var number = regexp.MustCompile(`\d+`)

// reconcileSeconds makes equal, in the two answers, what differs only by a clock's second: the
// Retry-After headers and the texts of the body whose numbers are each within one of the other
// side's (the same text otherwise). A step that counts seconds left asks for it (ClockSeconds).
func reconcileSeconds(a, b *answer) {
	if ra, rb := a.Headers["Retry-After"], b.Headers["Retry-After"]; ra != rb && closeNumbers(ra, rb) {
		b.Headers["Retry-After"] = ra
	}
	a.Body, b.Body = reconcileTexts(a.Body, b.Body)
}

func reconcileTexts(a, b any) (any, any) {
	switch x := a.(type) {
	case map[string]any:
		if y, ok := b.(map[string]any); ok {
			for k, e := range x {
				if f, has := y[k]; has {
					x[k], y[k] = reconcileTexts(e, f)
				}
			}
		}
	case []any:
		if y, ok := b.([]any); ok && len(x) == len(y) {
			for i := range x {
				x[i], y[i] = reconcileTexts(x[i], y[i])
			}
		}
	case string:
		if y, ok := b.(string); ok && x != y && closeNumbers(x, y) {
			return x, x
		}
	}
	return a, b
}

// closeNumbers: the same text but for its numbers, each within one of the other's.
func closeNumbers(x, y string) bool {
	if number.ReplaceAllString(x, "#") != number.ReplaceAllString(y, "#") {
		return false
	}
	nx, ny := number.FindAllString(x, -1), number.FindAllString(y, -1)
	if len(nx) != len(ny) || len(nx) == 0 {
		return false
	}
	for i := range nx {
		p, err1 := strconv.Atoi(nx[i])
		q, err2 := strconv.Atoi(ny[i])
		if err1 != nil || err2 != nil || p-q > 1 || q-p > 1 {
			return false
		}
	}
	return true
}
