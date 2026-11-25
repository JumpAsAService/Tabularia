package xmlamodel

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// MemberKind says what a member is.
type MemberKind int

const (
	MemberKey  MemberKind = iota // a value of the column (or a year, a quarter… of a date)
	MemberAll                    // the All member of a hierarchy
	MemberNull                   // the member that stands for a null
)

// Member is a member of a hierarchy: a place in the model, not data (whether it exists in the
// data is for whoever reads the data to say).
type Member struct {
	Level *Level
	Kind  MemberKind
	// the key as the value is, not as it is written after «&»: see EncodeKey. Dates: see DateParts.
	Key string
}

// UniqueName is the member's unique name.
func (m Member) UniqueName() string {
	switch m.Kind {
	case MemberAll:
		return m.Level.Hierarchy.AllName()
	case MemberNull:
		return m.Level.Hierarchy.UniqueName + "." + Bracket(NullMemberName)
	}
	return m.Level.UniqueName + "." + KeySegment(m.Key)
}

// Caption is what a pivot shows for the member.
func (m Member) Caption() string {
	switch m.Kind {
	case MemberAll:
		return AllCaption
	case MemberNull:
		return NullCaption
	}
	return m.Key
}

// Hierarchy is the member's hierarchy.
func (m Member) Hierarchy() *Hierarchy { return m.Level.Hierarchy }

// Parent is the member one level up: for a month the quarter, for a value the All member; false
// for the All member.
func (m Member) Parent() (Member, bool) {
	h := m.Level.Hierarchy
	switch {
	case m.Kind == MemberAll:
		return Member{}, false
	case m.Kind == MemberNull, m.Level.Number == 1:
		return h.All(), true
	}
	up := h.Levels[m.Level.Number-1]
	p, ok := DateParts(m.Level, m.Key)
	if !ok {
		return h.All(), true
	}
	return Member{Level: up, Kind: MemberKey, Key: p.Key(up.Part)}, true
}

// Ancestors are the members above, the All member first and the parent last.
func (m Member) Ancestors() []Member {
	var up []Member
	for p, ok := m.Parent(); ok; p, ok = p.Parent() {
		up = append([]Member{p}, up...)
	}
	return up
}

// ── dates ─────────────────────────────────────────────────────────────────────

// Parts of a date a key says: the year, quarter, month and day it holds, 0 for what it does not.
type Parts struct{ Year, Quarter, Month, Day int }

var (
	yearKey    = regexp.MustCompile(`^(\d{4,7})$`)
	quarterKey = regexp.MustCompile(`^(\d{4,7})-Q([1-4])$`)
	monthKey   = regexp.MustCompile(`^(\d{4,7})-(\d\d)$`)
	dayKey     = regexp.MustCompile(`^(\d{4,7})-(\d\d)-(\d\d)$`)
	// a timestamp: the date, a space (or T) and the time with up to nanoseconds
	hugeKey  = regexp.MustCompile(`^-?[1-9]\d{0,38}$`)
	stampKey = regexp.MustCompile(`^(\d{4,7}-\d\d-\d\d)[ T](\d\d):(\d\d):(\d\d)(\.\d{1,9})?$`)
)

// Key is the key of these parts at this level (a day: 2026-01-15).
func (p Parts) Key(part Part) string {
	switch part {
	case PartYear:
		return fmt.Sprintf("%04d", p.Year)
	case PartQuarter:
		return fmt.Sprintf("%04d-Q%d", p.Year, p.Quarter)
	case PartMonth:
		return fmt.Sprintf("%04d-%02d", p.Year, p.Month)
	}
	return fmt.Sprintf("%04d-%02d-%02d", p.Year, p.Month, p.Day)
}

func quarterOf(month int) int { return (month-1)/3 + 1 }

func validDay(y, m, d int) bool {
	if m < 1 || m > 12 || d < 1 {
		return false
	}
	if y >= 1 && y <= 9999 {
		return time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC).Day() == d
	}
	return d <= 31
}

// DateParts reads the key of a member of a calendar level (Year to Day) into its parts; false when
// the key is not one.
func DateParts(l *Level, key string) (Parts, bool) {
	var p Parts
	var m []string
	switch l.Part {
	case PartYear:
		if m = yearKey.FindStringSubmatch(key); m == nil {
			return p, false
		}
		p.Year, _ = strconv.Atoi(m[1])
	case PartQuarter:
		if m = quarterKey.FindStringSubmatch(key); m == nil {
			return p, false
		}
		p.Year, _ = strconv.Atoi(m[1])
		p.Quarter, _ = strconv.Atoi(m[2])
	case PartMonth:
		if m = monthKey.FindStringSubmatch(key); m == nil {
			return p, false
		}
		p.Year, _ = strconv.Atoi(m[1])
		p.Month, _ = strconv.Atoi(m[2])
		if p.Month < 1 || p.Month > 12 {
			return Parts{}, false
		}
		p.Quarter = quarterOf(p.Month)
	case PartDay:
		if m = dayKey.FindStringSubmatch(key); m == nil {
			return p, false
		}
		p.Year, _ = strconv.Atoi(m[1])
		p.Month, _ = strconv.Atoi(m[2])
		p.Day, _ = strconv.Atoi(m[3])
		if !validDay(p.Year, p.Month, p.Day) {
			return Parts{}, false
		}
		p.Quarter = quarterOf(p.Month)
	default:
		return p, false
	}
	return p, true
}

// CleanKey checks a key read from MDX against the level it is the key of and gives its canonical
// form: a calendar level's keys are those of DateParts; the plain value of a date is a day, that
// of a timestamp a day, a space and the time (a «T» is read as the space); any other level takes
// any text.
func (l *Level) CleanKey(key string) (string, bool) {
	switch l.Part {
	case PartYear, PartQuarter, PartMonth, PartDay:
		if _, ok := DateParts(l, key); !ok {
			return "", false
		}
		return key, true
	case PartValue:
		switch l.Hierarchy.Dimension.Kind {
		case dataapiwire.KindDate:
			if m := dayKey.FindStringSubmatch(key); m != nil && validDay(atoi(m[1]), atoi(m[2]), atoi(m[3])) {
				return key, true
			}
			return "", false
		case dataapiwire.KindTimestamp, dataapiwire.KindTimestampTZ:
			m := stampKey.FindStringSubmatch(key)
			if m == nil {
				return "", false
			}
			day := dayKey.FindStringSubmatch(m[1])
			if day == nil || !validDay(atoi(day[1]), atoi(day[2]), atoi(day[3])) || atoi(m[2]) > 23 || atoi(m[3]) > 59 || atoi(m[4]) > 59 {
				return "", false
			}
			return m[1] + " " + m[2] + ":" + m[3] + ":" + m[4] + m[5], true
		case dataapiwire.KindInt:
			if n, err := strconv.ParseInt(key, 10, 64); err == nil {
				return strconv.FormatInt(n, 10), true
			}
			// wider than 64 bits (HUGEINT): digits and a sign
			if hugeKey.MatchString(key) {
				return key, true
			}
			return "", false
		case dataapiwire.KindBool:
			switch strings.ToLower(key) {
			case "true":
				return "true", true
			case "false":
				return "false", true
			}
			return "", false
		}
	}
	return key, true
}

func atoi(s string) int { n, _ := strconv.Atoi(s); return n }

// ── reading a name back ───────────────────────────────────────────────────────

// Segment is a dotted part of a name as the MDX parser gives it: a bracketed name (Name), or a
// member key (&[a]&[b]: Keys, no Name). The parser's own type has the same two fields and one
// more, and converts by copying them.
type Segment struct {
	Name string
	Keys []string
}

// RefKind says what a name stands for.
type RefKind int

const (
	RefMeasure       RefKind = iota // [Measures].[amount]
	RefMeasures                     // [Measures] and [Measures].[Measures]: the measures' dimension and hierarchy
	RefMeasuresLevel                // [Measures].[Measures].[MeasuresLevel]
	RefDimension                    // [country]
	RefHierarchy                    // [country].[country]
	RefLevel                        // [country].[country].[country], …[(All)]
	RefMember                       // …[All], …[(null)], …&[IT]
)

// Ref is what a name resolved to; only the fields of its kind are set. Where a level or member is
// set its hierarchy and dimension follow from it.
type Ref struct {
	Kind      RefKind
	Measure   *Measure
	Dimension *Dimension
	Hierarchy *Hierarchy
	Level     *Level
	Member    Member
}

// NameError says which part of a name the cube does not know.
type NameError struct {
	Name   string // the name as written
	Reason string
}

func (e *NameError) Error() string { return e.Reason + ": " + e.Name }

// Resolve reads a reference (the segments of a member, level, hierarchy, dimension or measure,
// without a trailing function like .Members) against the cube. Names match without regard to
// case; keys do not. A *NameError says what was not understood.
func (c *Cube) Resolve(segs []Segment) (Ref, error) {
	written := func() string {
		var b strings.Builder
		for i, s := range segs {
			if i > 0 {
				b.WriteByte('.')
			}
			if len(s.Keys) > 0 {
				for _, k := range s.Keys {
					b.WriteString("&" + Bracket(k))
				}
			} else {
				b.WriteString(Bracket(s.Name))
			}
		}
		return b.String()
	}
	bad := func(reason string) (Ref, error) { return Ref{}, &NameError{Name: written(), Reason: reason} }
	if len(segs) == 0 {
		return bad("empty name")
	}
	for i, s := range segs {
		if len(s.Keys) > 0 && (i != len(segs)-1 || i < 3) {
			return bad("a member key can only end a member's name")
		}
	}
	first := segs[0]
	if eq(first.Name, MeasuresName) {
		switch len(segs) {
		case 1:
			return Ref{Kind: RefMeasures}, nil
		case 2:
			if m := c.Measure(Bracket(MeasuresName) + "." + Bracket(segs[1].Name)); m != nil {
				return Ref{Kind: RefMeasure, Measure: m}, nil
			}
			if eq(segs[1].Name, MeasuresName) {
				return Ref{Kind: RefMeasures}, nil
			}
			if eq(segs[1].Name, MeasuresLevel) {
				return Ref{Kind: RefMeasuresLevel}, nil
			}
			return bad("this cube has no such measure")
		case 3:
			if eq(segs[1].Name, MeasuresName) && eq(segs[2].Name, MeasuresLevel) {
				return Ref{Kind: RefMeasuresLevel}, nil
			}
		}
		return bad("not a measure, nor the dimension of the measures")
	}
	d := c.Dimension(Bracket(first.Name))
	if d == nil {
		return bad("this cube has no such dimension")
	}
	if len(segs) == 1 {
		return Ref{Kind: RefDimension, Dimension: d}, nil
	}
	var h *Hierarchy
	for _, x := range d.Hierarchies {
		if eq(x.Name, segs[1].Name) {
			h = x
			break
		}
	}
	if h == nil {
		return bad("this dimension has no such hierarchy")
	}
	if len(segs) == 2 {
		return Ref{Kind: RefHierarchy, Dimension: d, Hierarchy: h}, nil
	}
	third := segs[2]
	var l *Level
	for _, x := range h.Levels {
		if len(third.Keys) == 0 && eq(x.Name, third.Name) {
			l = x
			break
		}
	}
	switch {
	case l != nil && len(segs) == 3:
		return Ref{Kind: RefLevel, Dimension: d, Hierarchy: h, Level: l}, nil
	case l == nil && len(segs) == 3 && len(third.Keys) == 0 && eq(third.Name, AllMemberName):
		return Ref{Kind: RefMember, Dimension: d, Hierarchy: h, Level: h.Levels[0], Member: h.All()}, nil
	case l == nil && len(segs) == 3 && len(third.Keys) == 0 && eq(third.Name, NullMemberName):
		return Ref{Kind: RefMember, Dimension: d, Hierarchy: h, Level: h.Levels[1], Member: Member{Level: h.Levels[1], Kind: MemberNull}}, nil
	case l == nil:
		return bad("this hierarchy has no such level or member")
	case len(segs) != 4 || len(segs[3].Keys) != 1:
		return bad("a member of a level is written with its key: &[key]")
	case l.Part == PartAll:
		return bad("the (All) level has only the All member")
	}
	raw, ok := DecodeKey(segs[3].Keys[0])
	if ok {
		raw, ok = l.CleanKey(raw)
	}
	if !ok {
		return bad("the key is not one of this level")
	}
	return Ref{Kind: RefMember, Dimension: d, Hierarchy: h, Level: l, Member: Member{Level: l, Kind: MemberKey, Key: raw}}, nil
}

// ResolveName reads a unique name written as text (as a rowset restriction gives it), by the same
// rules as Resolve. Only the bracketed form is read.
func (c *Cube) ResolveName(unique string) (Ref, error) {
	segs, ok := SplitName(unique)
	if !ok {
		return Ref{}, &NameError{Name: unique, Reason: "not a name between brackets"}
	}
	return c.Resolve(segs)
}

// SplitName splits a unique name written as text into its segments: [a].[b].&[key].
func SplitName(s string) ([]Segment, bool) {
	var segs []Segment
	i := 0
	for i < len(s) {
		if len(segs) > 0 {
			if s[i] != '.' {
				return nil, false
			}
			i++
		}
		if i >= len(s) {
			return nil, false
		}
		var keys []string
		isKey := false
		for i < len(s) && s[i] == '&' {
			isKey = true
			i++
			text, n, ok := bracketed(s[i:])
			if !ok {
				return nil, false
			}
			keys = append(keys, text)
			i += n
		}
		if isKey {
			segs = append(segs, Segment{Keys: keys})
			continue
		}
		text, n, ok := bracketed(s[i:])
		if !ok {
			return nil, false
		}
		segs = append(segs, Segment{Name: text})
		i += n
	}
	return segs, len(segs) > 0
}

// bracketed reads [text] (a «]]» is a «]») from the start of s and says how much it took.
func bracketed(s string) (string, int, bool) {
	if len(s) == 0 || s[0] != '[' {
		return "", 0, false
	}
	var b strings.Builder
	for i := 1; i < len(s); i++ {
		if s[i] == ']' {
			if i+1 < len(s) && s[i+1] == ']' {
				b.WriteByte(']')
				i++
				continue
			}
			if b.Len() == 0 {
				return "", 0, false
			}
			return b.String(), i + 1, true
		}
		b.WriteByte(s[i])
	}
	return "", 0, false
}
