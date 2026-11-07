package pod

import (
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/duckdb/duckdb-go/v2"
)

// The kinds of column the pods know: how a filter's value is read and bound, and how a value
// is written in an answer. Anything else (lists, structs, maps, blobs, intervals) goes out as
// JSON but cannot be filtered on or sorted by.
const (
	KindInt         = "integer"
	KindFloat       = "float"
	KindDecimal     = "decimal"
	KindBool        = "boolean"
	KindString      = "string"
	KindDate        = "date"
	KindTimestamp   = "timestamp"
	KindTimestampTZ = "timestamptz"
	KindTime        = "time"
	KindUUID        = "uuid"
	KindOther       = "other"
)

// Column is a column of a prepared resource: its name and DuckDB's type.
type Column struct {
	Name string
	Type string // as DuckDB writes it: BIGINT, DECIMAL(12,2), TIMESTAMP WITH TIME ZONE…
	Kind string
	// DECIMAL(Width, Scale)
	Width, Scale int
}

// a decimal's type as DuckDB writes it; the only types written in a CAST are the fixed names
// below and DECIMAL(38,n) made here, never one read from anywhere else
var decimalType = regexp.MustCompile(`^DECIMAL\((\d+),(\d+)\)$`)

var intRanges = map[string][2]*big.Int{}

func init() {
	r := func(name string, lo, hi string) {
		a, _ := new(big.Int).SetString(lo, 10)
		b, _ := new(big.Int).SetString(hi, 10)
		intRanges[name] = [2]*big.Int{a, b}
	}
	r("TINYINT", "-128", "127")
	r("SMALLINT", "-32768", "32767")
	r("INTEGER", "-2147483648", "2147483647")
	r("BIGINT", "-9223372036854775808", "9223372036854775807")
	r("HUGEINT", "-170141183460469231731687303715884105728", "170141183460469231731687303715884105727")
	r("UTINYINT", "0", "255")
	r("USMALLINT", "0", "65535")
	r("UINTEGER", "0", "4294967295")
	r("UBIGINT", "0", "18446744073709551615")
	r("UHUGEINT", "0", "340282366920938463463374607431768211455")
}

// NewColumn reads DuckDB's type of a column.
func NewColumn(name, typ string) Column {
	c := Column{Name: name, Type: typ, Kind: KindOther}
	switch {
	case intRanges[typ] != [2]*big.Int{}:
		c.Kind = KindInt
	case typ == "FLOAT" || typ == "DOUBLE":
		c.Kind = KindFloat
	case decimalType.MatchString(typ):
		m := decimalType.FindStringSubmatch(typ)
		c.Kind = KindDecimal
		c.Width, _ = strconv.Atoi(m[1])
		c.Scale, _ = strconv.Atoi(m[2])
	case typ == "BOOLEAN":
		c.Kind = KindBool
	case typ == "VARCHAR":
		c.Kind = KindString
	case typ == "DATE":
		c.Kind = KindDate
	case typ == "TIMESTAMP" || typ == "TIMESTAMP_S" || typ == "TIMESTAMP_MS" || typ == "TIMESTAMP_NS":
		c.Kind = KindTimestamp
	case typ == "TIMESTAMP WITH TIME ZONE":
		c.Kind = KindTimestampTZ
	case typ == "TIME":
		c.Kind = KindTime
	case typ == "UUID":
		c.Kind = KindUUID
	}
	return c
}

// Comparable says whether the column can be filtered on and sorted by.
func (c Column) Comparable() bool { return c.Kind != KindOther }

// Ordered says whether the range operators apply.
func (c Column) Ordered() bool {
	switch c.Kind {
	case KindInt, KindFloat, KindDecimal, KindDate, KindTimestamp, KindTimestampTZ, KindTime:
		return true
	}
	return false
}

// Param is a value of a request, read by its column's type: the SQL placeholder that takes it
// (a CAST to the column's type, never the value itself) and what is bound to it.
type Param struct {
	Placeholder string
	Value       any
}

var (
	errNotInteger = errors.New("a whole number")
	uuidText      = regexp.MustCompile(`^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$`)
	decimalText   = regexp.MustCompile(`^[+-]?(\d+)(?:\.(\d+))?$`)
)

// what a value of each kind looks like, for the errors
var kindExample = map[string]string{
	KindInt: "a whole number", KindFloat: "a number", KindDecimal: "a decimal number such as 12.50", KindBool: "true or false",
	KindDate: "a date such as 2026-01-31", KindTimestamp: "a date and time such as 2026-01-31T08:30:00",
	KindTimestampTZ: "a date and time such as 2026-01-31T08:30:00Z", KindTime: "a time such as 08:30:00", KindUUID: "a UUID",
}

// ParseValue reads a request's text as a value of the column; the error says what it should be.
func (c Column) ParseValue(text string) (Param, error) {
	bad := func() (Param, error) {
		return Param{}, fmt.Errorf("«%s» is %s", c.Name, kindExample[c.Kind])
	}
	switch c.Kind {
	case KindString:
		return Param{"?", text}, nil
	case KindBool:
		switch strings.ToLower(text) {
		case "true":
			return Param{"?", true}, nil
		case "false":
			return Param{"?", false}, nil
		}
		return bad()
	case KindInt:
		n, ok := new(big.Int).SetString(strings.TrimPrefix(text, "+"), 10)
		if !ok {
			return bad()
		}
		r := intRanges[c.Type]
		if n.Cmp(r[0]) < 0 || n.Cmp(r[1]) > 0 {
			return Param{}, fmt.Errorf("«%s» is between %s and %s", c.Name, r[0], r[1])
		}
		if n.IsInt64() {
			// bound as it is: DuckDB gives the placeholder the column's type (within its range,
			// checked above), and a lookup by key keeps to the block of its value
			return Param{"?", n.Int64()}, nil
		}
		return Param{"CAST(? AS " + c.Type + ")", n.String()}, nil
	case KindFloat:
		f, err := strconv.ParseFloat(text, 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return bad()
		}
		return Param{"?", f}, nil
	case KindDecimal:
		m := decimalText.FindStringSubmatch(text)
		if m == nil {
			return bad()
		}
		// the value's own scale when it has more decimals than the column: 12.505 is not 12.51
		scale := max(c.Scale, len(m[2]))
		digits := len(strings.TrimLeft(m[1], "0"))
		if digits+scale > 38 {
			return Param{}, fmt.Errorf("«%s»: too many digits", c.Name)
		}
		return Param{fmt.Sprintf("CAST(? AS DECIMAL(38,%d))", scale), text}, nil
	case KindDate:
		t, err := time.Parse("2006-01-02", text)
		if err != nil {
			return bad()
		}
		return Param{"CAST(? AS DATE)", t.Format("2006-01-02")}, nil
	case KindTimestamp, KindTimestampTZ:
		t, ok := parseTimestamp(text)
		if !ok {
			return bad()
		}
		if c.Kind == KindTimestampTZ {
			return Param{"CAST(? AS TIMESTAMP WITH TIME ZONE)", t.UTC().Format("2006-01-02 15:04:05.999999") + "+00"}, nil
		}
		return Param{"CAST(? AS " + c.Type + ")", t.Format("2006-01-02 15:04:05.999999999")}, nil
	case KindTime:
		for _, layout := range []string{"15:04:05.999999999", "15:04"} {
			if t, err := time.Parse(layout, text); err == nil {
				return Param{"CAST(? AS TIME)", t.Format("15:04:05.999999")}, nil
			}
		}
		return bad()
	case KindUUID:
		if !uuidText.MatchString(text) {
			return bad()
		}
		return Param{"CAST(? AS UUID)", strings.ToLower(text)}, nil
	}
	return Param{}, fmt.Errorf("«%s» cannot be filtered on", c.Name)
}

// parseTimestamp reads a date and time, with or without a zone (none is UTC), or a date alone
// (its midnight)
func parseTimestamp(text string) (time.Time, bool) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999",
		"2006-01-02T15:04", "2006-01-02"} {
		if t, err := time.Parse(layout, text); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// ── writing the values ────────────────────────────────────────────────────────

// Plain is a value of the column as JSON writes it: dates in ISO 8601, a decimal as a number
// when a float holds it exactly and as a string when it does not, a float that is not a number
// as "NaN" or "Infinity", nested values made of lists and objects.
func (c Column) Plain(v any) any {
	if v == nil {
		return nil
	}
	switch c.Kind {
	case KindDate:
		if t, ok := v.(time.Time); ok {
			return t.Format("2006-01-02")
		}
	case KindTimestamp:
		if t, ok := v.(time.Time); ok {
			return t.Format("2006-01-02T15:04:05.999999999")
		}
	case KindTimestampTZ:
		if t, ok := v.(time.Time); ok {
			return t.UTC().Format(time.RFC3339Nano)
		}
	case KindTime:
		if t, ok := v.(time.Time); ok {
			return t.Format("15:04:05.999999999")
		}
	}
	return plain(v)
}

// plain turns what the driver gives into what encoding/json writes as intended
func plain(v any) any {
	switch x := v.(type) {
	case nil, bool, string, int8, int16, int32, int64, uint8, uint16, uint32, uint64, int, uint:
		return x
	case float32:
		return plainFloat(float64(x))
	case float64:
		return plainFloat(x)
	case *big.Int:
		return jsonNumber(x.String())
	case duckdb.Decimal:
		s := x.String()
		if f := x.Float64(); !math.IsInf(f, 0) && strconv.FormatFloat(f, 'f', -1, 64) == s {
			return jsonNumber(s)
		}
		return s
	case duckdb.UUID:
		return x.String()
	case *duckdb.UUID:
		return x.String()
	case []byte:
		return base64.StdEncoding.EncodeToString(x)
	case time.Time:
		return x.UTC().Format(time.RFC3339Nano)
	case duckdb.Interval:
		return map[string]any{"months": x.Months, "days": x.Days, "micros": x.Micros}
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = plain(e)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = plain(e)
		}
		return out
	case duckdb.Map:
		out := make([]any, 0, len(x))
		for k, e := range x {
			out = append(out, map[string]any{"key": plain(k), "value": plain(e)})
		}
		return out
	case duckdb.Union:
		return map[string]any{"tag": x.Tag, "value": plain(x.Value)}
	case [16]byte:
		return hex.EncodeToString(x[:])
	}
	return fmt.Sprint(v)
}

func plainFloat(f float64) any {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	return f
}

// jsonNumber is a number written as it is (a decimal, a huge integer)
type jsonNumber string

func (n jsonNumber) MarshalJSON() ([]byte, error) { return []byte(n), nil }

// CSVText is a value as a CSV cell: empty for null, nested values as JSON.
func (c Column) CSVText(v any) string {
	switch x := c.Plain(v).(type) {
	case nil:
		return ""
	case string:
		return x
	case bool:
		return strconv.FormatBool(x)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	case jsonNumber:
		return string(x)
	case int8, int16, int32, int64, uint8, uint16, uint32, uint64, int, uint:
		return fmt.Sprint(x)
	default:
		b, err := marshal(x)
		if err != nil {
			return fmt.Sprint(x)
		}
		return string(b)
	}
}

// CursorText is a value of a sort column as a request would write it, for a cursor; nil for
// null.
func (c Column) CursorText(v any) *string {
	if v == nil {
		return nil
	}
	var s string
	switch x := v.(type) {
	case time.Time:
		switch c.Kind {
		case KindDate:
			s = x.Format("2006-01-02")
		case KindTimestampTZ:
			s = x.UTC().Format(time.RFC3339Nano)
		case KindTime:
			s = x.Format("15:04:05.999999999")
		default:
			s = x.Format("2006-01-02T15:04:05.999999999")
		}
	case float64:
		s = strconv.FormatFloat(x, 'g', -1, 64)
	case float32:
		s = strconv.FormatFloat(float64(x), 'g', -1, 32)
	case duckdb.Decimal:
		s = x.String()
	case *big.Int:
		s = x.String()
	case duckdb.UUID:
		s = x.String()
	case *duckdb.UUID:
		s = x.String()
	default:
		s = fmt.Sprint(x)
	}
	return &s
}
