// Package xmlamodel is the cube a resource of a data API becomes for the XMLA interface
// (docs/specs/xmla.md, «The model»): measures, dimensions, hierarchies and levels worked out from
// the resource's columns and their types, and the unique names of all of them with the functions
// that write and read those names back. Nobody writes the model: the same columns always give the
// same cube. The standard library and dataapiwire only: the pods (Discover, Execute) and the
// gateway (the documentation of a data API) share it.
//
// Unique names (every name is bracketed, and a «]» inside a name is doubled):
//
//	measure          [Measures].[amount]             Sum, named as the column
//	                 [Measures].[amount (Average)]   also (Min), (Max); Rows is the row count
//	dimension        [country]                       the dimension of Measures is [Measures]
//	hierarchy        [country].[country]             a date also has [signup].[Year-Quarter-Month-Day]
//	level            [country].[country].[country]   level 0 is [country].[country].[(All)]
//	all member       [country].[country].[All]
//	null member      [country].[country].[(null)]
//	member           [country].[country].[country].&[IT]
//	date members     [signup].[Year-Quarter-Month-Day].[Year].&[2026]    …[Quarter].&[2026-Q1]
//	                 …[Month].&[2026-01]    …[Day].&[2026-01-15]
//	plain date       [signup].[signup].[signup].&[2026-01-15]   (a timestamp: &[2026-01-15 08:30:00])
//
// MDX reads these as lists of segments: a name is a segment [x], a member key a segment &[key].
package xmlamodel

import (
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// Names the model reserves.
const (
	MeasuresName   = "Measures"      // the dimension and hierarchy of the measures
	MeasuresLevel  = "MeasuresLevel" // their only level
	RowsName       = "Rows"          // the row count
	AllLevelName   = "(All)"         // level 0 of every hierarchy
	AllMemberName  = "All"           // the member of level 0
	NullMemberName = "(null)"        // the member that stands for a null
	NullCaption    = "null"          // as the app shows a null
	AllCaption     = "All"           // the caption of the All member
	DateHierarchy  = "Year-Quarter-Month-Day"
)

// Column is a column of a resource.
type Column struct {
	Name        string
	Kind        string // dataapiwire.Kind…, see KindOfDuckDB and KindOfPolars
	Description string
	// DECIMAL(Width, Scale), when Kind is decimal
	Width, Scale int
}

// KindOfDuckDB is the kind of a column from its DuckDB type: dataapiwire's table, the one the pods'
// REST interface reads its columns with, so that a column has the same kind in every interface.
// What is not a number, text, boolean, date, timestamp or time is KindOther and stays out of the
// cube: a list, a struct, a map, a blob or an interval has no sensible way to be grouped on or
// summed.
func KindOfDuckDB(typ string) string { return dataapiwire.KindOfDuckDB(typ) }

// DecimalOf reads the width and scale of a DECIMAL(w,s) type; 0, 0 for anything else.
func DecimalOf(typ string) (width, scale int) {
	t := strings.ToUpper(strings.TrimSpace(typ))
	if _, err := fmt.Sscanf(t, "DECIMAL(%d,%d)", &width, &scale); err != nil {
		if _, err := fmt.Sscanf(t, "NUMERIC(%d,%d)", &width, &scale); err != nil {
			return 0, 0
		}
	}
	return width, scale
}

// Agg is how a measure aggregates.
type Agg int

const (
	AggRows Agg = iota // the row count
	AggSum
	AggAverage
	AggMin
	AggMax
)

// Measure is a number a pivot shows in its cells.
type Measure struct {
	Name       string // the name inside the brackets: amount, amount (Average), Rows
	UniqueName string // [Measures].[amount]
	Agg        Agg
	Column     string // the column it aggregates; "" for Rows
	Kind       string // the column's kind (dataapiwire.Kind…); "" for Rows
	Folder     string // the display folder: the column's name; "" for Rows
	// DECIMAL(Width, Scale), for a decimal column
	Width, Scale int
	Description  string
}

// Result is the kind of value a measure yields: KindInt for a row count and the sum, minimum and
// maximum of an integer column, KindDecimal for those of a decimal, KindFloat for the rest
// (an average is always a float).
func (m *Measure) Result() string {
	switch {
	case m.Agg == AggRows:
		return dataapiwire.KindInt
	case m.Agg == AggAverage:
		return dataapiwire.KindFloat
	}
	return m.Kind
}

// FormatString is the format a cell of the measure is shown with (the FORMAT_STRING cell property
// and DEFAULT_FORMAT_STRING): thousands separators, and the decimals a decimal column has.
func (m *Measure) FormatString() string {
	switch m.Result() {
	case dataapiwire.KindDecimal:
		if m.Scale > 0 {
			return "#,0." + strings.Repeat("0", min(m.Scale, 10))
		}
		return "#,0"
	case dataapiwire.KindFloat:
		return "#,0.00"
	}
	return "#,0"
}

// Part says what of a value a level is.
type Part int

const (
	PartAll Part = iota
	PartValue
	PartYear
	PartQuarter
	PartMonth
	PartDay
)

// Level is a level of a hierarchy; level 0 is (All).
type Level struct {
	Name       string
	UniqueName string
	Number     int
	Part       Part
	Hierarchy  *Hierarchy `json:"-"`
}

// Type is LEVEL_TYPE: 1 for (All), the time levels' codes for a date, 0 for the rest.
func (l *Level) Type() int {
	switch l.Part {
	case PartAll:
		return 1
	case PartYear:
		return 0x14
	case PartQuarter:
		return 0x44
	case PartMonth:
		return 0x84
	case PartDay:
		return 0x204
	}
	return 0
}

// Hierarchy is a way to walk a dimension: (All) and one or more levels.
type Hierarchy struct {
	Name       string
	UniqueName string
	Ordinal    int
	Levels     []*Level   // Levels[0] is (All)
	Dimension  *Dimension `json:"-"`
}

// All is the hierarchy's All member.
func (h *Hierarchy) All() Member { return Member{Level: h.Levels[0], Kind: MemberAll} }

// AllName is the unique name of the All member.
func (h *Hierarchy) AllName() string { return h.UniqueName + "." + Bracket(AllMemberName) }

// Leaf is the lowest level.
func (h *Hierarchy) Leaf() *Level { return h.Levels[len(h.Levels)-1] }

// Dimension is a column as a pivot groups by it.
type Dimension struct {
	Name        string
	UniqueName  string
	Column      string
	Kind        string
	Ordinal     int // 0 is [Measures]
	Time        bool
	Hierarchies []*Hierarchy
	Description string
}

// Default is the hierarchy a dimension is read through when none is named: the first.
func (d *Dimension) Default() *Hierarchy { return d.Hierarchies[0] }

// Cube is a resource as a pivot table sees it.
type Cube struct {
	Name       string // the cube's name: the resource's caption, or its name
	Resource   string
	Measures   []*Measure // Rows first
	Dimensions []*Dimension
	// the columns left out, with their kind: a list, a struct, a blob… (see KindOfDuckDB)
	Skipped []Column
	// when the data was written (RFC 3339), "" when not known
	Updated string
}

// the allocator of names: case-insensitive, as MDX is, and deterministic
type names map[string]bool

func (n names) take(name string) string {
	candidate := name
	for i := 2; n[strings.ToLower(candidate)]; i++ {
		candidate = fmt.Sprintf("%s (%d)", name, i)
	}
	n[strings.ToLower(candidate)] = true
	return candidate
}

// Build works out the cube of a resource from its columns. Two columns that would give the same
// name get the first the plain name and the other one «name (2)»; the plain column names of the
// measures are all taken before the (Average), (Min) and (Max) ones, so that those are the ones
// to move aside, and a column always keeps its own name unless it shares it with another column
// or with a reserved name (Rows, Measures, All, (All), (null)).
func Build(cube, resource string, cols []Column) *Cube {
	c := &Cube{Name: cube, Resource: resource}
	measureNames := names{}
	rows := &Measure{Name: measureNames.take(RowsName), Agg: AggRows}
	rows.UniqueName = measureUN(rows.Name)
	c.Measures = []*Measure{rows}

	numeric := func(k string) bool {
		return k == dataapiwire.KindInt || k == dataapiwire.KindFloat || k == dataapiwire.KindDecimal
	}
	sums := map[int]*Measure{} // by column index
	for i, col := range cols {
		if numeric(col.Kind) {
			m := &Measure{Name: measureNames.take(col.Name), Agg: AggSum, Column: col.Name, Kind: col.Kind, Folder: col.Name,
				Width: col.Width, Scale: col.Scale, Description: col.Description}
			m.UniqueName = measureUN(m.Name)
			sums[i] = m
		}
	}
	for i, col := range cols {
		s := sums[i]
		if s == nil {
			continue
		}
		c.Measures = append(c.Measures, s)
		for _, v := range []struct {
			agg  Agg
			word string
		}{{AggAverage, "Average"}, {AggMin, "Min"}, {AggMax, "Max"}} {
			m := *s
			m.Agg = v.agg
			m.Name = measureNames.take(col.Name + " (" + v.word + ")")
			m.UniqueName = measureUN(m.Name)
			c.Measures = append(c.Measures, &m)
		}
	}

	dimNames := names{}
	dimNames.take(MeasuresName)
	for _, col := range cols {
		var d *Dimension
		switch col.Kind {
		case dataapiwire.KindInt, dataapiwire.KindString, dataapiwire.KindBool, dataapiwire.KindUUID, dataapiwire.KindTime:
			d = newDimension(dimNames.take(col.Name), col, false)
		case dataapiwire.KindDate, dataapiwire.KindTimestamp, dataapiwire.KindTimestampTZ:
			d = newDimension(dimNames.take(col.Name), col, true)
		case dataapiwire.KindFloat, dataapiwire.KindDecimal:
			continue // measures only
		default:
			c.Skipped = append(c.Skipped, col)
			continue
		}
		d.Ordinal = len(c.Dimensions) + 1
		c.Dimensions = append(c.Dimensions, d)
	}
	return c
}

func measureUN(name string) string { return Bracket(MeasuresName) + "." + Bracket(name) }

func newDimension(name string, col Column, date bool) *Dimension {
	d := &Dimension{Name: name, UniqueName: Bracket(name), Column: col.Name, Kind: col.Kind, Time: date, Description: col.Description}
	type lv struct {
		name string
		part Part
	}
	hierNames := names{}
	var hiers []struct {
		name   string
		levels []lv
	}
	if date {
		hiers = append(hiers, struct {
			name   string
			levels []lv
		}{hierNames.take(DateHierarchy), []lv{{"Year", PartYear}, {"Quarter", PartQuarter}, {"Month", PartMonth}, {"Day", PartDay}}})
	}
	hiers = append(hiers, struct {
		name   string
		levels []lv
	}{hierNames.take(name), []lv{{name, PartValue}}})
	for i, hs := range hiers {
		h := &Hierarchy{Name: hs.name, UniqueName: d.UniqueName + "." + Bracket(hs.name), Ordinal: i, Dimension: d}
		// the names a level may not have: they would read as the (All) level or as a member
		levelNames := names{}
		levelNames.take(AllLevelName)
		levelNames.take(AllMemberName)
		levelNames.take(NullMemberName)
		h.Levels = append(h.Levels, &Level{Name: AllLevelName, UniqueName: h.UniqueName + "." + Bracket(AllLevelName), Part: PartAll, Hierarchy: h})
		for n, l := range hs.levels {
			ln := l.name
			if l.part == PartValue {
				ln = levelNames.take(ln)
			}
			h.Levels = append(h.Levels, &Level{Name: ln, UniqueName: h.UniqueName + "." + Bracket(ln), Number: n + 1, Part: l.part, Hierarchy: h})
		}
		d.Hierarchies = append(d.Hierarchies, h)
	}
	return d
}

// KeyComparedAsText says whether a member's key is compared with the text of the column (what
// Discover lists as the key) and not as a typed value. Only an integer, a boolean and a date are
// bound as what they are: the others, a timestamp above all, are compared by text, because a bound
// timestamp is cut to the microsecond and a column that holds nanoseconds would never equal the key
// Discover gave. Discover and Execute both ask this, so that they cannot diverge.
func KeyComparedAsText(kind string) bool {
	switch kind {
	case dataapiwire.KindInt, dataapiwire.KindBool, dataapiwire.KindDate:
		return false
	}
	return true
}

// Bracket writes a name as MDX does: between brackets, a «]» doubled.
func Bracket(name string) string { return "[" + strings.ReplaceAll(name, "]", "]]") + "]" }

// XMLSafe says whether XML 1.0 can hold the character: the rest (control characters, U+FFFE and
// U+FFFF) is not in any document, escaped or not.
func XMLSafe(r rune) bool {
	return r == 0x9 || r == 0xA || r == 0xD || (r >= 0x20 && r <= 0xD7FF) || (r >= 0xE000 && r <= 0xFFFD) || r >= 0x10000
}

// EncodeKey is the text of a member's key as it is written after «&»: the value itself, except
// that
//   - the empty value (a bracket cannot be empty in MDX) is a single backslash;
//   - a value that starts with a backslash gets another one in front;
//   - a value with a character XML cannot hold (or a byte that is not UTF-8) is written as «\#»
//     and then the value with each backslash doubled, each such character as \u{hex} and each
//     such byte as \x{hex}: two values that differ in nothing but those characters stay two names.
//
// DecodeKey is its inverse.
func EncodeKey(value string) string {
	if value == "" {
		return `\`
	}
	if needsEscape(value) {
		var b strings.Builder
		b.WriteString(`\#`)
		for i := 0; i < len(value); {
			r, n := utf8.DecodeRuneInString(value[i:])
			switch {
			case r == utf8.RuneError && n == 1:
				fmt.Fprintf(&b, `\x{%02x}`, value[i])
			case r == '\\':
				b.WriteString(`\\`)
			case !XMLSafe(r):
				fmt.Fprintf(&b, `\u{%x}`, r)
			default:
				b.WriteString(value[i : i+n])
			}
			i += n
		}
		return b.String()
	}
	if strings.HasPrefix(value, `\`) {
		return `\` + value
	}
	return value
}

func needsEscape(value string) bool {
	if !utf8.ValidString(value) {
		return true
	}
	for _, r := range value {
		if !XMLSafe(r) {
			return true
		}
	}
	return false
}

// DecodeKey reads a key written by EncodeKey; false for text that is not one.
func DecodeKey(key string) (string, bool) {
	switch {
	case key == `\`:
		return "", true
	case strings.HasPrefix(key, `\\`):
		return key[1:], true
	case strings.HasPrefix(key, `\#`):
		return decodeEscaped(key[2:])
	case strings.HasPrefix(key, `\`):
		return "", false
	}
	return key, true
}

func decodeEscaped(body string) (string, bool) {
	var out []byte
	for i := 0; i < len(body); i++ {
		if body[i] != '\\' {
			out = append(out, body[i])
			continue
		}
		if i+1 >= len(body) {
			return "", false
		}
		switch body[i+1] {
		case '\\':
			out = append(out, '\\')
			i++
		case 'u', 'x':
			end := strings.IndexByte(body[i:], '}')
			if i+2 >= len(body) || body[i+2] != '{' || end < 0 {
				return "", false
			}
			n, err := strconv.ParseUint(body[i+3:i+end], 16, 32)
			if err != nil {
				return "", false
			}
			if body[i+1] == 'x' {
				if n > 0xff {
					return "", false
				}
				out = append(out, byte(n))
			} else {
				out = utf8.AppendRune(out, rune(n))
			}
			i += end
		default:
			return "", false
		}
	}
	return string(out), true
}

// KeySegment writes a key as MDX does: &[key].
func KeySegment(value string) string { return "&" + Bracket(EncodeKey(value)) }

// Find helpers: names are matched without regard to case, as MDX does; keys are not.

func eq(a, b string) bool { return strings.EqualFold(a, b) }

// Dimension is the dimension with this name, or unique name.
func (c *Cube) Dimension(name string) *Dimension {
	for _, d := range c.Dimensions {
		if eq(d.Name, name) || eq(d.UniqueName, name) {
			return d
		}
	}
	return nil
}

// Measure is the measure with this name, or unique name.
func (c *Cube) Measure(name string) *Measure {
	for _, m := range c.Measures {
		if eq(m.Name, name) || eq(m.UniqueName, name) {
			return m
		}
	}
	return nil
}

// Hierarchy is the hierarchy with this unique name.
func (c *Cube) Hierarchy(unique string) *Hierarchy {
	for _, d := range c.Dimensions {
		for _, h := range d.Hierarchies {
			if eq(h.UniqueName, unique) {
				return h
			}
		}
	}
	return nil
}

// Level is the level with this unique name.
func (c *Cube) Level(unique string) *Level {
	for _, d := range c.Dimensions {
		for _, h := range d.Hierarchies {
			for _, l := range h.Levels {
				if eq(l.UniqueName, unique) {
					return l
				}
			}
		}
	}
	return nil
}
