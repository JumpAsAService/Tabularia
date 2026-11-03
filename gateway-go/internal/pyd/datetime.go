package pyd

import (
	"math"
	"math/big"
	"time"
)

// DateTime is a validated `datetime`: Aware says whether the input gave an offset
// (Python's tzinfo); a naive one is kept in UTC with Aware false.
type DateTime struct {
	time.Time
	Aware bool
}

// speedate's errors, as pydantic writes them in ctx.error.
const (
	dtTooShort       = "input is too short"
	dtExtraChars     = "unexpected extra characters at the end of the input"
	dtCharYear       = "invalid character in year"
	dtDateSep        = "invalid date separator, expected `-`"
	dtCharMonth      = "invalid character in month"
	dtCharDay        = "invalid character in day"
	dtRangeMonth     = "month value is outside expected range of 1-12"
	dtRangeDay       = "day value is outside expected range"
	dtTooLarge       = "dates after 9999 are not supported as unix timestamps"
	dtTooSmall       = "dates before 0000 are not supported as unix timestamps"
	unix0000         = -62_167_219_200
	unix10000        = 253_402_300_800
	msWatershed      = 20_000_000_000
	errDatetimeType  = "Input should be a valid datetime"
	errDatetimeDate  = "Input should be a valid datetime or date, "
	errDatetimeParse = "Input should be a valid datetime, "
)

// toDatetime is pydantic's datetime validator in lax mode. A string is first a
// datetime (RFC 3339-like, or a unix timestamp); failing that, a date (then at
// midnight); failing that too, the DATE's error is the one reported
// (datetime_from_date_parsing). Numbers are unix timestamps (milliseconds beyond
// 2e10).
func toDatetime(v any, strict bool) (any, *Error) {
	var d DateTime
	var perr string
	switch x := v.(type) {
	case string:
		if strict {
			return nil, newErr("datetime_type", errDatetimeType, v)
		}
		if hasSurrogate(x) {
			return nil, errStringUnicode(v)
		}
		var ok bool
		if d, ok = parseDateTime([]byte(x)); !ok {
			var derr string
			if d, derr = parseDateAsDateTime([]byte(x)); derr != "" {
				return nil, newErr("datetime_from_date_parsing", errDatetimeDate+derr, v, KV{"error", derr})
			}
		}
	case int64:
		d, perr = datetimeFromTimestamp(x, 0)
	case *big.Int:
		// beyond i64 the int is read as a float: too big for one, it is no datetime
		if _, err := intToFloat(v, x); err != nil {
			return nil, newErr("datetime_type", errDatetimeType, v)
		}
		perr = dtTooLarge
		if x.Sign() < 0 {
			perr = dtTooSmall
		}
	case float64:
		switch {
		case math.IsNaN(x):
			perr = "NaN values not permitted"
		case math.IsInf(x, 1):
			perr = dtTooLarge
		case math.IsInf(x, -1):
			perr = dtTooSmall
		default:
			d, perr = datetimeFromFloat(x)
		}
	default:
		return nil, newErr("datetime_type", errDatetimeType, v)
	}
	if perr != "" {
		return nil, newErr("datetime_parsing", errDatetimeParse+perr, v, KV{"error", perr})
	}
	if d.Year() == 0 {
		// speedate allows year 0, Python's datetime does not
		return nil, newErr("datetime_parsing", errDatetimeParse+"year 0 is out of range", v, KV{"error", "year 0 is out of range"})
	}
	return d, nil
}

func digits(b []byte, i, n int) (int, bool) {
	if i+n > len(b) {
		return 0, false
	}
	v := 0
	for k := 0; k < n; k++ {
		c := b[i+k]
		if c < '0' || c > '9' {
			return 0, false
		}
		v = v*10 + int(c-'0')
	}
	return v, true
}

// parseDatePartial is speedate's Date::parse_bytes_partial: YYYY-MM-DD at the start.
func parseDatePartial(b []byte) (y, m, d int, err string) {
	if len(b) < 10 {
		return 0, 0, 0, dtTooShort
	}
	var ok bool
	if y, ok = digits(b, 0, 4); !ok {
		return 0, 0, 0, dtCharYear
	}
	if b[4] != '-' {
		return 0, 0, 0, dtDateSep
	}
	if m, ok = digits(b, 5, 2); !ok {
		return 0, 0, 0, dtCharMonth
	}
	if b[7] != '-' {
		return 0, 0, 0, dtDateSep
	}
	if d, ok = digits(b, 8, 2); !ok {
		return 0, 0, 0, dtCharDay
	}
	var maxDays int
	switch m {
	case 1, 3, 5, 7, 8, 10, 12:
		maxDays = 31
	case 4, 6, 9, 11:
		maxDays = 30
	case 2:
		maxDays = 28
		if y%4 == 0 && (y%100 != 0 || y%400 == 0) {
			maxDays = 29
		}
	default:
		return 0, 0, 0, dtRangeMonth
	}
	if d < 1 || d > maxDays {
		return 0, 0, 0, dtRangeDay
	}
	return y, m, d, ""
}

// parseDateAsDateTime is Date::parse_bytes (YYYY-MM-DD, else an integer unix
// timestamp) turned into a naive midnight, with the date's error when it fails.
func parseDateAsDateTime(b []byte) (DateTime, string) {
	y, m, d, err := parseDatePartial(b)
	if err == "" && len(b) > 10 {
		err = dtExtraChars
	}
	if err == "" {
		return DateTime{Time: time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC)}, ""
	}
	if ts, ok := parseTimestampInt(b); ok {
		dt, terr := datetimeFromTimestamp(ts, 0)
		if terr != "" {
			return DateTime{}, terr
		}
		return dt, ""
	}
	return DateTime{}, err
}

// parseDateTime is DateTime::parse_bytes: RFC 3339 with T, t, _ or a space
// between date and time, or else a unix timestamp (int or decimal).
func parseDateTime(b []byte) (DateTime, bool) {
	if dt, ok := parseRFC3339(b); ok {
		return dt, true
	}
	if ts, ok := parseTimestampInt(b); ok {
		dt, err := datetimeFromTimestamp(ts, 0)
		return dt, err == ""
	}
	if f, ok := parseTimestampFloat(b); ok {
		dt, err := datetimeFromFloat(f)
		return dt, err == ""
	}
	return DateTime{}, false
}

func parseRFC3339(b []byte) (DateTime, bool) {
	y, mo, d, err := parseDatePartial(b)
	if err != "" || len(b) < 11 {
		return DateTime{}, false
	}
	switch b[10] {
	case 'T', 't', ' ', '_':
	default:
		return DateTime{}, false
	}
	t := b[11:]
	h, ok := digits(t, 0, 2)
	if !ok || h > 23 || len(t) < 3 || t[2] != ':' {
		return DateTime{}, false
	}
	mi, ok := digits(t, 3, 2)
	if !ok || mi > 59 {
		return DateTime{}, false
	}
	n := 5
	sec, micro := 0, 0
	if len(t) > 5 && t[5] == ':' {
		if sec, ok = digits(t, 6, 2); !ok || sec > 59 {
			return DateTime{}, false
		}
		n = 8
		if len(t) > 8 && (t[8] == '.' || t[8] == ',') {
			n = 9
			k := 0
			for n+k < len(t) && t[n+k] >= '0' && t[n+k] <= '9' {
				if k < 6 {
					micro = micro*10 + int(t[n+k]-'0')
				}
				k++
			}
			if k == 0 {
				return DateTime{}, false
			}
			for p := k; p < 6; p++ {
				micro *= 10
			}
			n += k
		}
	}
	loc := time.UTC
	aware := false
	if n < len(t) {
		switch t[n] {
		case 'Z', 'z':
			aware = true
			n++
		case '+', '-':
			sign := 1
			if t[n] == '-' {
				sign = -1
			}
			oh, ok := digits(t, n+1, 2)
			if !ok {
				return DateTime{}, false
			}
			p := n + 3
			if p < len(t) && t[p] == ':' {
				p++
			}
			om, ok := digits(t, p, 2)
			if !ok || oh > 23 || om > 59 {
				return DateTime{}, false
			}
			loc = time.FixedZone("", sign*(oh*3600+om*60))
			aware = true
			n = p + 2
		}
	}
	if n != len(t) {
		return DateTime{}, false
	}
	return DateTime{Time: time.Date(y, time.Month(mo), d, h, mi, sec, micro*1000, loc), Aware: aware}, true
}

// parseTimestampInt is speedate's int_parse_bytes: an optional sign and digits
// that fit an i64.
func parseTimestampInt(b []byte) (int64, bool) {
	i := 0
	neg := false
	if len(b) > 0 && (b[0] == '+' || b[0] == '-') {
		neg = b[0] == '-'
		i = 1
	}
	if i == len(b) {
		return 0, false
	}
	var v int64
	for ; i < len(b); i++ {
		c := b[i]
		if c < '0' || c > '9' {
			return 0, false
		}
		if v > (math.MaxInt64-int64(c-'0'))/10 {
			return 0, false
		}
		v = v*10 + int64(c-'0')
	}
	if neg {
		v = -v
	}
	return v, true
}

// parseTimestampFloat is speedate's float_parse_bytes for a decimal timestamp: a
// sign, digits, a '.' (required: "1e10" is not a timestamp), digits, an optional
// exponent; an integer part beyond i64 or an infinite result refused.
func parseTimestampFloat(b []byte) (float64, bool) {
	i := 0
	if len(b) > 0 && (b[0] == '+' || b[0] == '-') {
		i = 1
	}
	start := i
	for i < len(b) && b[i] >= '0' && b[i] <= '9' {
		i++
	}
	intDigits := i - start
	if i == len(b) || b[i] != '.' {
		return 0, false
	}
	i++
	fracStart := i
	for i < len(b) && b[i] >= '0' && b[i] <= '9' {
		i++
	}
	if intDigits == 0 && i == fracStart {
		return 0, false
	}
	if i < len(b) && (b[i] == 'e' || b[i] == 'E') {
		i++
		if i < len(b) && (b[i] == '+' || b[i] == '-') {
			i++
		}
		expStart := i
		for i < len(b) && b[i] >= '0' && b[i] <= '9' {
			i++
		}
		if i == expStart {
			return 0, false
		}
	}
	if i != len(b) {
		return 0, false
	}
	if _, ok := parseTimestampInt(b[:fracStart-1]); !ok && intDigits > 0 {
		return 0, false
	}
	f, ok := parseRustFloat(string(b))
	if !ok || math.IsInf(f, 0) {
		return 0, false
	}
	return f, true
}

// datetimeFromTimestamp is DateTime::from_timestamp: seconds, or milliseconds
// when beyond 2e10, between the years 0 and 9999 (UTC, aware).
func datetimeFromTimestamp(ts int64, micro int64) (DateTime, string) {
	sec := ts
	if ts > msWatershed || ts < -msWatershed {
		sec = ts / 1000
		extra := (ts % 1000) * 1000
		if extra < 0 {
			sec--
			extra += 1_000_000
		}
		micro += extra
	}
	sec += micro / 1_000_000
	micro %= 1_000_000
	if sec < unix0000 {
		return DateTime{}, dtTooSmall
	}
	if sec >= unix10000 {
		return DateTime{}, dtTooLarge
	}
	return DateTime{Time: time.Unix(sec, micro*1000).UTC(), Aware: true}, ""
}

func datetimeFromFloat(f float64) (DateTime, string) {
	if math.Abs(f) > msWatershed {
		f /= 1000
	}
	sec := math.Floor(f)
	if sec < unix0000 {
		return DateTime{}, dtTooSmall
	}
	if sec >= unix10000 {
		return DateTime{}, dtTooLarge
	}
	micro := int64(math.Round((f - sec) * 1_000_000))
	return datetimeFromTimestamp(int64(sec), micro)
}
