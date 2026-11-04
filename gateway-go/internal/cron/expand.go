// Package cron is croniter 6.2.4 (the Python gateway's cron library), the parts the
// gateway uses: `croniter.is_valid` (services/schedule.py validate_schedule) and
// `get_next` from an aware datetime, in a time zone, with croniter's handling of the
// daylight saving changes. It is a port of croniter.py, function by function, so that a
// schedule fires at the very instants the Python gateway computed.
package cron

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	minuteField = iota
	hourField
	dayField
	monthField
	dowField
	secondField
	yearField
)

const (
	unixLen   = 5
	secondLen = 6
	yearLen   = 7
)

var (
	ranges      = [7][2]int{{0, 59}, {0, 23}, {1, 31}, {1, 12}, {0, 6}, {0, 59}, {1970, 2099}}
	lenMeansAll = [7]int{60, 24, 31, 12, 7, 60, 130}
	monthAlphas = map[string]int{"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
		"sep": 9, "oct": 10, "nov": 11, "dec": 12}
	dowAlphas = map[string]int{"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

	// Python's `$` also matches before a final line feed: `\n?\z`; the fields come from
	// str.split(), so they hold no blanks.
	stepSearch     = regexp.MustCompile(`^([^-]+)-([^-/]+)(/(\d+))?\n?\z`)
	onlyInt        = regexp.MustCompile(`^\d+\n?\z`)
	starOrInt      = regexp.MustCompile(`^(\d+|\*)\n?\z`)
	weekdays       = "sun|mon|tue|wed|thu|fri|sat"
	months         = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
	specialDow     = regexp.MustCompile(`^(?P<pre>((?P<he>((` + weekdays + `)(-(` + weekdays + `))?)|((` + months + `)(-(` + months + `))?)|\w+)#)|l)(?P<last>\d+)\n?\z`)
	nearestWeekday = regexp.MustCompile(`^(?:(\d+)w|w(\d+))\n?\z`)
	hashExpression = regexp.MustCompile(`^(?P<hash_type>h|r)(\((?P<range_begin>\d+)-(?P<range_end>\d+)\))?(\/(?P<divisor>\d+))?\n?\z`)
	starStep       = regexp.MustCompile(`^\*(/.+)\n?\z`)
	startStep      = regexp.MustCompile(`^(.+)/(.+)\n?\z`)
)

var aliases = map[string]string{
	"@midnight": "0 0 * * *", "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0",
	"@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
}

// value is one item of an expanded field: an int, «*» or «l».
type value struct {
	star, last bool
	n          int
}

func (v value) key() string {
	switch {
	case v.star:
		return "*"
	case v.last:
		return "l"
	}
	return fmt.Sprintf("%02d", v.n)
}

// ErrBad is CroniterBadCronError: an expression croniter refuses.
var ErrBad = errors.New("bad cron expression")

// ErrBadDate is CroniterBadDateError: no fire within croniter's 50 years.
var ErrBadDate = errors.New("failed to find next date")

func bad(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrBad, fmt.Sprintf(format, a...))
}

// expression is croniter's expanded form.
type expression struct {
	expanded    [][]value
	nth         map[int][]any // weekday → the nths (an int, or "l")
	expressions []string
	nearest     []int
}

// queue item of the field's expansion: a text, as croniter's e_list holds texts and ints
type item struct {
	text  string
	isInt bool
	n     int
}

func (it item) str() string {
	if it.isInt {
		return strconv.Itoa(it.n)
	}
	return it.text
}

func alphaconv(index int, key string, expressions []string) (int, bool, error) {
	switch index {
	case dayField:
		if key == "l" {
			return 0, true, nil
		}
	case monthField:
		if n, ok := monthAlphas[key]; ok {
			return n, false, nil
		}
	case dowField:
		if n, ok := dowAlphas[key]; ok {
			return n, false, nil
		}
	}
	return 0, false, bad("[%s] is not acceptable", strings.Join(expressions, " "))
}

// valueAlias is croniter.value_alias.
func valueAlias(val, index, length int) int {
	low := map[int]map[int]int{dayField: {0: 1}, monthField: {0: 1}, dowField: {7: 0}}[index]
	to, ok := low[val]
	if !ok {
		return val
	}
	if ((index == dayField || index == monthField) && length == unixLen) ||
		((index == monthField || index == dowField) && length == secondLen) ||
		((index == dayField || index == monthField || index == dowField) && length == yearLen) {
		return val
	}
	return to
}

func pyRange(start, stop, step int) []int {
	var out []int
	for i := start; i < stop; i += step {
		out = append(out, i)
	}
	return out
}

// hashExpand is HashExpander.expand: «r» a random value (croniter's `R`); «h» needs a
// hash id, which the gateway never gives.
func hashExpand(idx int, expr string) (string, error) {
	m := hashExpression.FindStringSubmatch(expr)
	if m == nil {
		return expr, nil
	}
	kind, begin, end, divisor := m[1], m[3], m[4], m[6]
	if kind == "h" {
		return "", bad("Hashed definitions must include hash_id")
	}
	do := func(lo, hi int) int {
		crc := rand.Uint32()
		return int((uint64(crc)>>idx)%uint64(hi-lo+1)) + lo
	}
	atoi := func(s string) int { n, _ := strconv.Atoi(s); return n }
	if begin != "" && end != "" && atoi(begin) >= atoi(end) {
		return "", bad("Range end must be greater than range begin")
	}
	if begin != "" && end != "" && divisor != "" {
		if atoi(divisor) == 0 {
			return "", bad("Bad expression: %s", expr)
		}
		x := do(atoi(begin), atoi(divisor)-1+atoi(begin))
		return fmt.Sprintf("%d-%d/%d", x, atoi(end), atoi(divisor)), nil
	}
	if begin != "" && end != "" {
		return strconv.Itoa(do(atoi(begin), atoi(end))), nil
	}
	if divisor != "" {
		if atoi(divisor) == 0 {
			return "", bad("Bad expression: %s", expr)
		}
		x := do(ranges[idx][0], atoi(divisor)-1+ranges[idx][0])
		return fmt.Sprintf("%d-%d/%d", x, ranges[idx][1], atoi(divisor)), nil
	}
	return strconv.Itoa(do(ranges[idx][0], ranges[idx][1])), nil
}

// expand is croniter._expand (no hash id, seconds last, not strict).
func expand(format string) (*expression, error) {
	efl := strings.ToLower(format)
	if a, ok := aliases[efl]; ok {
		efl = a
	}
	expressions := strings.Fields(efl)
	n := len(expressions)
	if n != unixLen && n != secondLen && n != yearLen {
		return nil, bad("Exactly 5, 6 or 7 columns has to be specified for iterator expression.")
	}
	out := &expression{nth: map[int][]any{}, expressions: expressions}
	nearestSet := map[int]bool{}
	for index, expr := range expressions {
		expr, err := hashExpand(index, expr)
		if err != nil {
			return nil, err
		}
		if strings.Contains(expr, "?") {
			if expr != "?" {
				return nil, bad("[%s] is not acceptable. Question mark can not used with other characters", format)
			}
			if index != dayField && index != dowField {
				return nil, bad("[%s] is not acceptable. Question mark can only used in day_of_month or day_of_week", format)
			}
			expr = "*"
		}
		var eList []item
		for _, p := range strings.Split(expr, ",") {
			eList = append(eList, item{text: p})
		}
		var res []value
		seen := map[string]bool{}
		for len(eList) > 0 {
			e := eList[len(eList)-1]
			eList = eList[:len(eList)-1]
			es := e.str()
			var nth any
			if index == dowField {
				if m := specialDow.FindStringSubmatch(es); m != nil {
					he, last, pre := m[specialDow.SubexpIndex("he")], m[specialDow.SubexpIndex("last")], m[specialDow.SubexpIndex("pre")]
					if he != "" {
						es = he
						k, err := strconv.Atoi(last)
						if err != nil || k < 1 || k > 5 {
							return nil, bad("[%s] is not acceptable. Invalid day_of_week value", format)
						}
						nth = k
					} else if last != "" {
						es = last
						nth = pre // «l»
					}
				}
			}
			if index == dayField {
				if m := nearestWeekday.FindStringSubmatch(es); m != nil {
					d := m[1]
					if d == "" {
						d = m[2]
					}
					w, _ := strconv.Atoi(d)
					if w < 1 || w > 31 {
						return nil, bad("[%s] is not acceptable, nearest weekday day value '%d' out of range", format, w)
					}
					if len(eList) > 0 || len(res) > 0 {
						return nil, bad("[%s] is not acceptable. 'W' can only be used with a single day value", format)
					}
					nearestSet[w] = true
					res = append(res, value{n: w})
					continue
				}
			}
			t := es
			if m := starStep.FindStringSubmatch(es); m != nil {
				t = fmt.Sprintf("%d-%d%s", ranges[index][0], ranges[index][1], m[1])
			}
			m := stepSearch.FindStringSubmatch(t)
			if m == nil {
				if mm := startStep.FindStringSubmatch(es); mm != nil {
					t = fmt.Sprintf("%s-%d/%s", mm[1], ranges[index][1], mm[2])
				} else {
					t = es
				}
				m = stepSearch.FindStringSubmatch(t)
			}
			if m != nil {
				lowS, highS, stepS := m[1], m[2], m[4]
				if stepS == "" {
					stepS = "1"
				}
				if index == dayField && highS == "l" {
					highS = "31"
				}
				if !onlyInt.MatchString(lowS) {
					v, isL, err := alphaconv(index, lowS, expressions)
					if err != nil {
						return nil, err
					}
					lowS = strconv.Itoa(v)
					if isL {
						lowS = "l"
					}
				}
				if !onlyInt.MatchString(highS) {
					v, isL, err := alphaconv(index, highS, expressions)
					if err != nil {
						return nil, err
					}
					highS = strconv.Itoa(v)
					if isL {
						highS = "l"
					}
				}
				step, _ := strconv.Atoi(strings.TrimSuffix(stepS, "\n"))
				if step == 0 {
					return nil, bad("[%s] step '%d' in field %d is not acceptable", format, step, index)
				}
				if !onlyInt.MatchString(lowS) || !onlyInt.MatchString(highS) {
					return nil, bad("[%s] bands '%s-%s' in field %d are not acceptable", format, lowS, highS, index)
				}
				lo, _ := strconv.Atoi(strings.TrimSuffix(lowS, "\n"))
				hi, _ := strconv.Atoi(strings.TrimSuffix(highS, "\n"))
				lo, hi = valueAlias(lo, index, n), valueAlias(hi, index, n)
				if max(lo, hi) > max(ranges[index][0], ranges[index][1]) {
					return nil, bad("%s is out of bands", format)
				}
				var rng []int
				switch {
				case lo > hi:
					var whole []int
					for i := ranges[index][0]; i <= ranges[index][1]; i++ {
						whole = append(whole, i)
					}
					rng = pyRange(lo, ranges[index][1]+1, step)
					toSkip := 0
					if len(rng) > 0 {
						last := rng[len(rng)-1]
						curpos := last - ranges[index][0]
						alreadySkipped := len(whole) - 1 - curpos
						if curpos+step > len(whole) && alreadySkipped < step {
							toSkip = step - alreadySkipped
						}
					}
					rng = append(rng, pyRange(ranges[index][0]+toSkip, hi+1, step)...)
				case lo == hi:
					rng = pyRange(ranges[index][0], ranges[index][1]+1, step)
				default:
					rng = pyRange(lo, hi+1, step)
				}
				for _, a := range rng {
					it := item{isInt: true, n: a}
					if index == dowField && nth != nil && nth != "l" {
						it = item{text: fmt.Sprintf("%d#%v", a, nth)}
					}
					if !seen[it.str()] {
						eList = append(eList, it)
					}
				}
				for _, a := range rng {
					if index == dowField && nth != nil && nth != "l" {
						seen[fmt.Sprintf("%d#%v", a, nth)] = true
					} else {
						seen[strconv.Itoa(a)] = true
					}
				}
				continue
			}
			if strings.HasPrefix(t, "-") {
				return nil, bad("[%s] is not acceptable, negative numbers not allowed", format)
			}
			var v value
			if t == "*" {
				v = value{star: true}
			} else if onlyInt.MatchString(t) {
				k, err := strconv.Atoi(strings.TrimSuffix(t, "\n"))
				if err != nil {
					return nil, bad("[%s] is not acceptable, out of range", format)
				}
				v = value{n: valueAlias(k, index, n)}
			} else {
				k, isL, err := alphaconv(index, t, expressions)
				if err != nil {
					return nil, err
				}
				if isL {
					v = value{last: true}
				} else {
					v = value{n: valueAlias(k, index, n)}
				}
			}
			if !v.star && !v.last && (v.n < ranges[index][0] || v.n > ranges[index][1]) {
				return nil, bad("[%s] is not acceptable, out of range", format)
			}
			res = append(res, v)
			if index == dowField && nth != nil && !v.star && !v.last {
				out.nth[v.n] = appendUnique(out.nth[v.n], nth)
			}
		}
		// set, then sorted by croniter's key
		uniq := map[string]value{}
		for _, v := range res {
			uniq[v.key()] = v
		}
		keys := make([]string, 0, len(uniq))
		for k := range uniq {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		res = res[:0]
		for _, k := range keys {
			res = append(res, uniq[k])
		}
		if len(res) == lenMeansAll[index] {
			if (index == dayField && !strings.Contains(expressions[dowField], "*")) ||
				(index == dowField && !strings.Contains(expressions[dayField], "*")) {
				// kept as listed
			} else {
				res = []value{{star: true}}
			}
		}
		out.expanded = append(out.expanded, res)
	}
	for w := range nearestSet {
		out.nearest = append(out.nearest, w)
	}
	sort.Ints(out.nearest)
	if len(out.nth) > 0 {
		rest := map[string]bool{}
		all := map[string]bool{}
		for _, v := range out.expanded[dowField] {
			all[v.key()] = true
			if v.star {
				continue
			}
			if _, isNth := out.nth[v.n]; !isNth || v.last {
				rest[v.key()] = true
			}
		}
		if len(rest) > 0 && len(all) != lenMeansAll[dowField] {
			return nil, bad("day-of-week field does not support mixing literal values and nth day of week syntax.  Cron: '%s'", format)
		}
	}
	return out, nil
}

func appendUnique(list []any, v any) []any {
	for _, x := range list {
		if x == v {
			return list
		}
	}
	return append(list, v)
}

// IsValid is croniter.is_valid.
func IsValid(expr string) bool {
	_, err := expand(expr)
	return err == nil
}
