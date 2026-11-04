package reportparams

import (
	"fmt"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// Dialect is report_params.Dialect: how a database reads its SQL's quotes and comments.
type Dialect struct {
	SQLGlot      string // how sqlglot reads and writes it
	HashComments bool   // `#` starts a comment
	DollarQuotes bool   // PostgreSQL's $tag$…$tag$
	EStrings     bool   // PostgreSQL's E'…', where a backslash always escapes
	Brackets     bool   // SQL Server's [quoted name]
	Server       bool   // a database whose settings change how it reads a string
	QQuotes      bool   // Oracle's q'[…]'
	TripleQuotes bool   // BigQuery's '''…'''
}

// Dialects are DIALECTS: by engine (the catalog) and by connection type (a database).
var Dialects = map[string]Dialect{
	"polars":     {SQLGlot: "postgres", DollarQuotes: true},
	"duckdb":     {SQLGlot: "duckdb", DollarQuotes: true, EStrings: true},
	"chdb":       {SQLGlot: "clickhouse", HashComments: true, DollarQuotes: true},
	"clickhouse": {SQLGlot: "clickhouse", HashComments: true, DollarQuotes: true},
	"bigquery":   {SQLGlot: "bigquery", HashComments: true, TripleQuotes: true},
	"postgresql": {SQLGlot: "postgres", DollarQuotes: true, EStrings: true, Server: true},
	"mysql":      {SQLGlot: "mysql", HashComments: true, Server: true},
	"mariadb":    {SQLGlot: "mysql", HashComments: true, Server: true},
	"trino":      {SQLGlot: "trino", Server: true},
	"oracle":     {SQLGlot: "oracle", Server: true, QQuotes: true},
	"sqlserver":  {SQLGlot: "tsql", Brackets: true, Server: true},
}

// DialectOf is dialect_of.
func DialectOf(key string) Dialect {
	if d, ok := Dialects[key]; ok && key != "" {
		return d
	}
	return Dialects["polars"]
}

// Brace is one brace of the SQL that means something.
type Brace struct {
	Start, End int // in characters; End after the last one
	Quoted     bool
	Kind       string // open, close, lone, unclosed, name, escaped
	Inner      string
}

// Refusal is a 422 of the SQL (_refuse).
type Refusal struct{ Message string }

func (r *Refusal) Error() string { return r.Message }

func refuse(format string, args ...any) *Refusal { return &Refusal{fmt.Sprintf(format, args...)} }

var dollar = regexp.MustCompile(`^\$([A-Za-z_][A-Za-z0-9_]*)?\$`)

func ident(ch rune) bool {
	return unicode.IsLetter(ch) || unicode.IsDigit(ch) || isPyAlnum(ch) || ch == '_' || ch == '$'
}

// isPyAlnum covers what str.isalnum() adds to letters and decimal digits (other numerics).
func isPyAlnum(ch rune) bool { return unicode.IsNumber(ch) }

type text []rune

func (s text) startsWith(p string, i int) bool {
	r := []rune(p)
	if i < 0 || i+len(r) > len(s) {
		return false
	}
	for k := range r {
		if s[i+k] != r[k] {
			return false
		}
	}
	return true
}

func (s text) find(p string, from, stop int) int {
	r := []rune(p)
	if stop > len(s) {
		stop = len(s)
	}
	for i := max(from, 0); i+len(r) <= stop; i++ {
		if s.startsWith(p, i) {
			return i
		}
	}
	return -1
}

func quotedEnd(s text, i int, close rune, backslash bool) int {
	j, n := i+1, len(s)
	for j < n {
		ch := s[j]
		if backslash && ch == '\\' {
			j += 2
			continue
		}
		if ch == close {
			if j+1 < n && s[j+1] == close {
				j += 2
				continue
			}
			return j + 1
		}
		j++
	}
	return n
}

var qClose = map[rune]rune{'[': ']', '{': '}', '(': ')', '<': '>'}

func qQuote(s text, i int) (int, int, int, bool) {
	j := i
	if s[i] == 'n' || s[i] == 'N' {
		j = i + 1
	}
	if (i > 0 && ident(s[i-1])) || j >= len(s) || (s[j] != 'q' && s[j] != 'Q') || j+1 >= len(s) || s[j+1] != '\'' ||
		j+2 >= len(s) || pystr.IsSpace(s[j+2]) {
		return 0, 0, 0, false
	}
	c, ok := qClose[s[j+2]]
	if !ok {
		c = s[j+2]
	}
	end := s.find(string(c)+"'", j+3, len(s))
	if end >= 0 {
		return j + 3, end, end + 2, true
	}
	return j + 3, len(s), len(s), true
}

func tripleEnd(s text, i int, backslash bool) int {
	quote := strings.Repeat(string(s[i]), 3)
	j, n := i+3, len(s)
	for j < n {
		if backslash && s[j] == '\\' {
			j += 2
			continue
		}
		if s.startsWith(quote, j) {
			return j
		}
		j++
	}
	return n
}

func commentEnd(s text, i int, nested bool) int {
	depth, j, n := 1, i+2, len(s)
	for j < n {
		switch {
		case s.startsWith("*/", j):
			depth--
			j += 2
			if depth == 0 || !nested {
				return j
			}
		case nested && s.startsWith("/*", j):
			depth++
			j += 2
		default:
			j++
		}
	}
	return n
}

func bodyMatch(s string) bool { return body.MatchString(s) }

func inQuotes(s text, start, stop int) []Brace {
	var out []Brace
	k := start
	for {
		k = s.find("{", k, stop)
		if k < 0 {
			return out
		}
		if s.startsWith("{{", k) {
			end := s.find("}}", k+2, stop)
			if end >= 0 && bodyMatch(pystr.Strip(string(s[k+2:end]))) {
				out = append(out, Brace{k, end + 2, true, "escaped", string(s[k+2 : end])})
				k = end + 2
			} else {
				k += 2
			}
			continue
		}
		end := s.find("}", k+1, stop)
		inner := ""
		if end >= 0 {
			inner = string(s[k+1 : end])
		}
		if end >= 0 && !strings.Contains(inner, "{") && bodyMatch(pystr.Strip(inner)) {
			out = append(out, Brace{k, end + 1, true, "name", inner})
			k = end + 1
		} else {
			k++
		}
	}
}

func read(s text, d Dialect, backslash, nested bool) []Brace {
	var out []Brace
	i, n := 0, len(s)
	for i < n {
		c := s[i]
		switch {
		case s.startsWith("--", i) || (c == '#' && d.HashComments):
			j := s.find("\n", i, n)
			if j < 0 {
				i = n
			} else {
				i = j
			}
		case s.startsWith("/*", i):
			i = commentEnd(s, i, nested)
		case d.QQuotes && strings.ContainsRune("nNqQ", c) && func() bool { _, _, _, ok := qQuote(s, i); return ok }():
			a, b, e, _ := qQuote(s, i)
			out = append(out, inQuotes(s, a, b)...)
			i = e
		case d.TripleQuotes && (c == '\'' || c == '"') && s.startsWith(strings.Repeat(string(c), 3), i):
			j := tripleEnd(s, i, backslash)
			out = append(out, inQuotes(s, i+3, j)...)
			i = j + 3
		case c == '\'' || c == '"' || c == '`' || (c == '[' && d.Brackets):
			eString := d.EStrings && c == '\'' && i > 0 && (s[i-1] == 'e' || s[i-1] == 'E') && (i < 2 || !ident(s[i-2]))
			close := c
			if c == '[' {
				close = ']'
			}
			j := quotedEnd(s, i, close, backslash || eString)
			out = append(out, inQuotes(s, i+1, j)...)
			i = j
		case c == '$' && d.DollarQuotes && dollar.MatchString(string(s[i:])) && !(i > 0 && ident(s[i-1])):
			tag := []rune(dollar.FindString(string(s[i:])))
			mEnd := i + len(tag)
			end := s.find(string(tag), mEnd, n)
			if end < 0 {
				out = append(out, inQuotes(s, mEnd, n)...)
				i = n
			} else {
				out = append(out, inQuotes(s, mEnd, end)...)
				i = end + len(tag)
			}
		case s.startsWith("{{", i):
			out = append(out, Brace{i, i + 2, false, "open", ""})
			i += 2
		case s.startsWith("}}", i):
			out = append(out, Brace{i, i + 2, false, "close", ""})
			i += 2
		case c == '}':
			out = append(out, Brace{i, i + 1, false, "lone", ""})
			i++
		case c == '{':
			end := s.find("}", i+1, n)
			if end < 0 || strings.Contains(string(s[i+1:end]), "{") {
				out = append(out, Brace{i, i + 1, false, "unclosed", ""})
				i++
			} else {
				out = append(out, Brace{i, end + 1, false, "name", string(s[i+1 : end])})
				i = end + 1
			}
		default:
			i++
		}
	}
	return out
}

func same(a, b []Brace) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// Braces is braces: the SQL's braces, read every way its database might read it; a 422 when the
// readings disagree on one.
func Braces(sql string, d Dialect) ([]Brace, *Refusal) {
	if !strings.ContainsAny(sql, "{}") {
		return nil, nil
	}
	s := text([]rune(sql))
	type way struct{ backslash, nested bool }
	var ways []way
	for _, b := range []bool{false, true} {
		if b && !strings.Contains(sql, `\`) {
			continue
		}
		for _, nested := range []bool{false, true} {
			if nested && !strings.Contains(sql, "/*") {
				continue
			}
			ways = append(ways, way{b, nested})
		}
	}
	first := read(s, d, ways[0].backslash, ways[0].nested)
	for _, w := range ways[1:] {
		other := read(s, d, w.backslash, w.nested)
		if same(other, first) {
			continue
		}
		var differ Brace
		for k := 0; k < max(len(first), len(other)); k++ {
			var a, b *Brace
			if k < len(first) {
				a = &first[k]
			}
			if k < len(other) {
				b = &other[k]
			}
			if a == nil || b == nil || *a != *b {
				if a != nil {
					differ = *a
				} else {
					differ = *b
				}
				break
			}
		}
		what := "a comment inside another"
		if w.backslash {
			what = "a backslash"
		}
		return nil, refuse("The SQL is unclear near «%s»: %s makes it uncertain whether it is inside quoted text. "+
			"Write the text before it without that", pystr.Cut(string(s[differ.Start:differ.End]), 60), what)
	}
	return first, nil
}

// Parts is _parts: the name, the offset and the format of a brace.
func Parts(b Brace) (string, string, string) {
	m := body.FindStringSubmatch(pystr.Strip(b.Inner))
	if m == nil {
		return "", "", ""
	}
	return m[1], m[2], m[3]
}

// CheckSQL is check_sql: the parameters and the placeholders (their insides) the SQL uses; a 422
// for a brace that is neither, a parameter inside quotes, an unclear reading.
func CheckSQL(sql string, d Dialect, parameters map[string]bool) (map[string]bool, map[string]bool, *Refusal) {
	used, placeholders := map[string]bool{}, map[string]bool{}
	braces, refusal := Braces(sql, d)
	if refusal != nil {
		return nil, nil, refusal
	}
	for _, b := range braces {
		switch b.Kind {
		case "lone":
			return nil, nil, refuse("A single } in the SQL: write }} for a literal brace")
		case "unclosed":
			return nil, nil, refuse("A { in the SQL is not closed: add }, or write {{ for a literal brace")
		}
		if b.Kind != "name" {
			continue
		}
		name, offset, format := Parts(b)
		if b.Quoted {
			if parameters[name] {
				return nil, nil, refuse("Write {%s} without quotes: Tabularia quotes it", name)
			}
			if Builtins[name] {
				placeholders[pystr.Strip(b.Inner)] = true
			}
			continue
		}
		switch {
		case parameters[name]:
			if offset != "" || format != "" {
				return nil, nil, refuse("{%s}: a parameter takes no offset nor format", b.Inner)
			}
			used[name] = true
		case Builtins[name]:
			placeholders[pystr.Strip(b.Inner)] = true
		default:
			return nil, nil, refuse("{%s} is neither a parameter of the report nor a placeholder: write {{ and }} for literal braces",
				pystr.Cut(b.Inner, 60))
		}
	}
	return used, placeholders, nil
}

// ── the literals ──────────────────────────────────────────────────────────────

var backslashEscapes = map[rune]string{'\a': `\a`, '\b': `\b`, '\f': `\f`, '\n': `\n`, '\r': `\r`, '\t': `\t`, '\v': `\v`, '\\': `\\`}

// sqlglotString is exp.Literal.string(value).sql(dialect): how sqlglot writes a string of the
// dialect (probed on sqlglot 30.21: ClickHouse, MySQL and BigQuery escape with a backslash,
// BigQuery its quote too; the others double the quote and leave the rest).
func sqlglotString(value, dialect string) string {
	var b strings.Builder
	b.WriteByte('\'')
	escapes := dialect == "clickhouse" || dialect == "mysql" || dialect == "bigquery"
	for _, r := range value {
		switch {
		case r == '\'' && dialect == "bigquery":
			b.WriteString(`\'`)
		case r == '\'':
			b.WriteString("''")
		case escapes && backslashEscapes[r] != "":
			b.WriteString(backslashEscapes[r])
		case r == 0 && dialect == "clickhouse":
			b.WriteString(`\0`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('\'')
	return b.String()
}

// Text is _text: a text as a literal of the dialect.
func Text(value string, d Dialect) string {
	if d.SQLGlot == "tsql" {
		return "N'" + strings.ReplaceAll(value, "'", "''") + "'" // a national string: no character lost to a code page
	}
	if d.Server && d.EStrings && strings.Contains(value, `\`) {
		// E'…' reads a backslash as an escape whatever standard_conforming_strings says
		return "E'" + strings.ReplaceAll(strings.ReplaceAll(value, `\`, `\\`), "'", "''") + "'"
	}
	return sqlglotString(value, d.SQLGlot)
}

// number is _number: a number as it is written, a negative one in brackets.
func number(v any) string {
	var text string
	switch x := v.(type) {
	case int64:
		text = strconv.FormatInt(x, 10)
	case float64:
		text = decimalF(x)
	}
	if strings.HasPrefix(text, "-") {
		return "(" + text + ")"
	}
	return text
}

// decimalF is format(Decimal(repr(value)), "f"): the float's shortest digits, without exponent.
func decimalF(f float64) string {
	r := pyjson.FloatRepr(f)
	d, ok := new(big.Float).SetPrec(2000).SetString(r)
	if !ok {
		return r
	}
	// Decimal keeps the digits of the repr: as many decimals as it has after the point
	mant, exp, _ := strings.Cut(r, "e")
	decimals := 0
	if i := strings.Index(mant, "."); i >= 0 {
		decimals = len(mant) - i - 1
	}
	if exp != "" {
		e, _ := strconv.Atoi(exp)
		decimals -= e
		if decimals < 0 {
			decimals = 0
		}
	}
	return d.Text('f', decimals)
}

func date(value string, d Dialect) string {
	switch d.SQLGlot {
	case "clickhouse":
		return "toDate('" + value + "')"
	case "tsql":
		return "CAST('" + value + "' AS DATE)"
	}
	return "DATE '" + value + "'"
}

func timestamp(value string, d Dialect) string {
	switch d.SQLGlot {
	case "clickhouse":
		return "toDateTime('" + value + "')"
	case "tsql":
		return "CAST('" + strings.ReplaceAll(value, " ", "T") + "' AS DATETIME2)"
	case "bigquery":
		return "DATETIME '" + value + "'"
	}
	return "TIMESTAMP '" + value + "'"
}

// Literal is literal: a parameter's value as a literal of the dialect; a list as (a, b, c).
func Literal(value any, d Dialect, name string) (string, *Refusal) {
	switch x := value.(type) {
	case []any:
		if len(x) == 0 {
			return "", refuse("«%s» has no value, and the SQL needs at least one: choose one", name)
		}
		parts := make([]string, len(x))
		for i, v := range x {
			s, r := Literal(v, d, name)
			if r != nil {
				return "", r
			}
			parts[i] = s
		}
		return "(" + strings.Join(parts, ", ") + ")", nil
	case nil:
		return "NULL", nil
	case bool:
		if d.SQLGlot == "tsql" || d.SQLGlot == "oracle" {
			if x {
				return "1", nil
			}
			return "0", nil
		}
		if x {
			return "TRUE", nil
		}
		return "FALSE", nil
	case int64, float64:
		return number(x), nil
	case Date:
		return date(x.ISO(), d), nil
	}
	return Text(pystr.Str(value), d), nil
}

var (
	isoDateT    = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}\n?$`)
	isoDatetime = regexp.MustCompile(`^([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2}(?::[0-9]{2})?)(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?\n?$`)
	epochDigits = regexp.MustCompile(`^-?[0-9]{1,18}$`)
)

// placeholderLiteral is _placeholder_literal: a placeholder's value outside quotes.
func placeholderLiteral(text string, d Dialect) string {
	if isoDateT.MatchString(text) {
		return date(text, d)
	}
	if m := isoDatetime.FindStringSubmatch(text); m != nil {
		clock := m[2]
		if len(clock) != 8 {
			clock += ":00"
		}
		return timestamp(m[1]+" "+clock, d)
	}
	if epochDigits.MatchString(text) {
		n, _ := strconv.ParseInt(text, 10, 64)
		return number(n)
	}
	return Text(text, d)
}

// Fill is fill: the SQL the database reads, every parameter a literal of the dialect, every
// placeholder resolved (`placeholders`: inside → text). CheckSQL first.
func Fill(sql string, d Dialect, types map[string]string, values map[string]any, placeholders map[string]string) (string, *Refusal) {
	braces, refusal := Braces(sql, d)
	if refusal != nil {
		return "", refusal
	}
	s := []rune(sql)
	var out strings.Builder
	at := 0
	for _, b := range braces {
		out.WriteString(string(s[at:b.Start]))
		at = b.End
		name, _, _ := Parts(b)
		_, isParam := types[name]
		switch {
		case b.Kind == "open":
			out.WriteString("{")
		case b.Kind == "close":
			out.WriteString("}")
		case b.Kind == "escaped":
			out.WriteString("{" + b.Inner + "}")
		case b.Kind == "name" && b.Quoted:
			if Builtins[name] {
				text := placeholders[pystr.Strip(b.Inner)]
				if strings.ContainsAny(text, "'\"`\\]$\n") {
					return "", refuse("{%s} gives «%s», which cannot go inside quoted text", b.Inner, pystr.Cut(text, 40))
				}
				out.WriteString(text)
			} else {
				out.WriteString(string(s[b.Start:b.End]))
			}
		case b.Kind == "name" && isParam:
			lit, r := Literal(values[name], d, name)
			if r != nil {
				return "", r
			}
			out.WriteString(lit)
		case b.Kind == "name" && Builtins[name]:
			out.WriteString(placeholderLiteral(placeholders[pystr.Strip(b.Inner)], d))
		default:
			out.WriteString(string(s[b.Start:b.End]))
		}
	}
	out.WriteString(string(s[at:]))
	return out.String(), nil
}
