package xmlamodel

import (
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

func TestKindOfDuckDB(t *testing.T) {
	for typ, want := range map[string]string{
		"TINYINT": dataapiwire.KindInt, "SMALLINT": dataapiwire.KindInt, "INTEGER": dataapiwire.KindInt, "BIGINT": dataapiwire.KindInt,
		"HUGEINT": dataapiwire.KindInt, "UTINYINT": dataapiwire.KindInt, "USMALLINT": dataapiwire.KindInt, "UINTEGER": dataapiwire.KindInt,
		"UBIGINT": dataapiwire.KindInt, "UHUGEINT": dataapiwire.KindInt,
		"FLOAT": dataapiwire.KindFloat, "DOUBLE": dataapiwire.KindFloat,
		"DECIMAL(12,2)": dataapiwire.KindDecimal, "DECIMAL(38,10)": dataapiwire.KindDecimal, "DECIMAL(4,0)": dataapiwire.KindDecimal,
		"BOOLEAN": dataapiwire.KindBool, "VARCHAR": dataapiwire.KindString, "JSON": dataapiwire.KindString, "ENUM('a', 'b')": dataapiwire.KindString,
		"DATE": dataapiwire.KindDate, "TIMESTAMP": dataapiwire.KindTimestamp, "TIMESTAMP_S": dataapiwire.KindTimestamp,
		"TIMESTAMP_MS": dataapiwire.KindTimestamp, "TIMESTAMP_NS": dataapiwire.KindTimestamp,
		"TIMESTAMP WITH TIME ZONE": dataapiwire.KindTimestampTZ, "TIME": dataapiwire.KindTime, "UUID": dataapiwire.KindUUID,
		// left out of the cube
		"INTEGER[]": dataapiwire.KindOther, "STRUCT(a INTEGER)": dataapiwire.KindOther, "MAP(VARCHAR, INTEGER)": dataapiwire.KindOther,
		"BLOB": dataapiwire.KindOther, "INTERVAL": dataapiwire.KindOther, "BIT": dataapiwire.KindOther, "": dataapiwire.KindOther,
	} {
		if got := KindOfDuckDB(typ); got != want {
			t.Errorf("%q: %s, want %s", typ, got, want)
		}
	}
	if w, s := DecimalOf("DECIMAL(12,2)"); w != 12 || s != 2 {
		t.Errorf("decimal %d,%d", w, s)
	}
	if w, s := DecimalOf("VARCHAR"); w != 0 || s != 0 {
		t.Errorf("not a decimal %d,%d", w, s)
	}
}

func cols(kv ...string) []Column {
	var out []Column
	for i := 0; i < len(kv); i += 2 {
		c := Column{Name: kv[i], Kind: kv[i+1]}
		if c.Kind == dataapiwire.KindDecimal {
			c.Width, c.Scale = 12, 2
		}
		out = append(out, c)
	}
	return out
}

func measureNames(c *Cube) []string {
	var out []string
	for _, m := range c.Measures {
		out = append(out, m.UniqueName)
	}
	return out
}

func TestEveryKindOfColumn(t *testing.T) {
	c := Build("Sales", "sales", cols(
		"id", dataapiwire.KindInt, "amount", dataapiwire.KindDecimal, "ratio", dataapiwire.KindFloat, "country", dataapiwire.KindString,
		"paid", dataapiwire.KindBool, "day", dataapiwire.KindDate, "seen", dataapiwire.KindTimestamp, "seen_tz", dataapiwire.KindTimestampTZ,
		"at", dataapiwire.KindTime, "ref", dataapiwire.KindUUID, "tags", dataapiwire.KindOther))
	wantMeasures := []string{"[Measures].[Rows]",
		"[Measures].[id]", "[Measures].[id (Average)]", "[Measures].[id (Min)]", "[Measures].[id (Max)]",
		"[Measures].[amount]", "[Measures].[amount (Average)]", "[Measures].[amount (Min)]", "[Measures].[amount (Max)]",
		"[Measures].[ratio]", "[Measures].[ratio (Average)]", "[Measures].[ratio (Min)]", "[Measures].[ratio (Max)]"}
	if got := measureNames(c); strings.Join(got, "|") != strings.Join(wantMeasures, "|") {
		t.Errorf("measures\n got %v\nwant %v", got, wantMeasures)
	}
	// every number has its four measures in a folder of its name; Rows has none
	if c.Measures[0].Folder != "" || c.Measures[0].Agg != AggRows || c.Measure("[Measures].[amount (Max)]").Folder != "amount" {
		t.Error("folders")
	}
	var dims []string
	for _, d := range c.Dimensions {
		dims = append(dims, d.UniqueName)
	}
	// numbers with decimals are measures only; an integer is a dimension as well
	if want := "[id] [country] [paid] [day] [seen] [seen_tz] [at] [ref]"; strings.Join(dims, " ") != want {
		t.Errorf("dimensions %v, want %s", dims, want)
	}
	if len(c.Skipped) != 1 || c.Skipped[0].Name != "tags" {
		t.Errorf("skipped %v", c.Skipped)
	}
	for _, d := range c.Dimensions {
		time := d.Kind == dataapiwire.KindDate || d.Kind == dataapiwire.KindTimestamp || d.Kind == dataapiwire.KindTimestampTZ
		if d.Time != time {
			t.Errorf("%s: time %v", d.Name, d.Time)
		}
		if time {
			if len(d.Hierarchies) != 2 || d.Hierarchies[0].Name != DateHierarchy || d.Hierarchies[1].Name != d.Name {
				t.Errorf("%s: hierarchies", d.Name)
				continue
			}
			var levels []string
			for _, l := range d.Hierarchies[0].Levels {
				levels = append(levels, l.UniqueName)
			}
			want := []string{"[" + d.Name + "].[Year-Quarter-Month-Day].[(All)]", "[" + d.Name + "].[Year-Quarter-Month-Day].[Year]",
				"[" + d.Name + "].[Year-Quarter-Month-Day].[Quarter]", "[" + d.Name + "].[Year-Quarter-Month-Day].[Month]", "[" + d.Name + "].[Year-Quarter-Month-Day].[Day]"}
			if strings.Join(levels, "|") != strings.Join(want, "|") {
				t.Errorf("%s: levels %v", d.Name, levels)
			}
		} else if len(d.Hierarchies) != 1 || len(d.Hierarchies[0].Levels) != 2 {
			t.Errorf("%s: one hierarchy of (All) and one level expected", d.Name)
		}
		for _, h := range d.Hierarchies {
			if h.Levels[0].Part != PartAll || h.Levels[0].Number != 0 || h.AllName() != h.UniqueName+".[All]" {
				t.Errorf("%s: the All level", h.UniqueName)
			}
		}
	}
	if c.Dimensions[3].Hierarchies[1].UniqueName != "[day].[day]" || c.Dimensions[3].Hierarchies[1].Levels[1].UniqueName != "[day].[day].[day]" {
		t.Error("the plain value of a date is the second hierarchy")
	}
	// result kinds, formats and level types
	if m := c.Measure("[Measures].[amount]"); m.Result() != dataapiwire.KindDecimal || m.FormatString() != "#,0.00" || m.Scale != 2 {
		t.Errorf("amount %s %s", m.Result(), m.FormatString())
	}
	if m := c.Measure("[Measures].[amount (Average)]"); m.Result() != dataapiwire.KindFloat {
		t.Error("an average is a float")
	}
	if m := c.Measure("[Measures].[id (Max)]"); m.Result() != dataapiwire.KindInt || m.FormatString() != "#,0" {
		t.Error("integer max")
	}
	if c.Measures[0].Result() != dataapiwire.KindInt {
		t.Error("rows")
	}
	l := c.Dimension("[day]").Hierarchies[0].Levels
	if l[0].Type() != 1 || l[1].Type() != 0x14 || l[2].Type() != 0x44 || l[3].Type() != 0x84 || l[4].Type() != 0x204 {
		t.Error("level types")
	}
	if c.Dimension("[country]").Ordinal != 2 || c.Dimension("[id]").Ordinal != 1 {
		t.Error("ordinals")
	}
}

func TestNamesWithSpacesAccentsBracketsAndCase(t *testing.T) {
	c := Build("c", "c", cols("Città di nascita", dataapiwire.KindString, "a]b", dataapiwire.KindString, "x]]y", dataapiwire.KindInt,
		"MAIUSCOLE", dataapiwire.KindDecimal, "[brackets]", dataapiwire.KindString, "with.dot", dataapiwire.KindString, "&amp", dataapiwire.KindString))
	if d := c.Dimension("[a]]b]"); d == nil || d.UniqueName != "[a]]b]" || d.Column != "a]b" {
		t.Fatalf("a]b: %+v", d)
	}
	for _, un := range []string{"[Measures].[x]]]]y (Average)]", "[Measures].[MAIUSCOLE (Max)]", "[Città di nascita]", "[[brackets]]]", "[with.dot]", "[&amp]"} {
		if c.Measure(un) == nil && c.Dimension(un) == nil {
			t.Errorf("%s not found", un)
		}
	}
	// case does not matter reading a name
	if c.Dimension("[CITTÀ DI NASCITA]") == nil || c.Measure("[measures].[maiuscole]") == nil {
		t.Error("case")
	}
	// every name of the cube is read back as what it is
	var check func(un string, want RefKind)
	check = func(un string, want RefKind) {
		t.Helper()
		ref, err := c.ResolveName(un)
		if err != nil || ref.Kind != want {
			t.Errorf("%s: %v %v, want kind %d", un, ref.Kind, err, want)
		}
	}
	for _, m := range c.Measures {
		check(m.UniqueName, RefMeasure)
	}
	for _, d := range c.Dimensions {
		check(d.UniqueName, RefDimension)
		for _, h := range d.Hierarchies {
			check(h.UniqueName, RefHierarchy)
			check(h.AllName(), RefMember)
			check(Member{Level: h.Levels[1], Kind: MemberNull}.UniqueName(), RefMember)
			for _, l := range h.Levels {
				check(l.UniqueName, RefLevel)
			}
		}
	}
}

func TestTwoColumnsOneName(t *testing.T) {
	// a column called Rows, one called Measures, one that would be another one's (Average), and names that read as a member
	c := Build("c", "c", cols("Rows", dataapiwire.KindInt, "Measures", dataapiwire.KindString, "x", dataapiwire.KindDecimal, "x (Average)", dataapiwire.KindDecimal,
		"All", dataapiwire.KindString, "(null)", dataapiwire.KindString, "(All)", dataapiwire.KindInt))
	seen := map[string]bool{}
	for _, m := range c.Measures {
		k := strings.ToLower(m.UniqueName)
		if seen[k] {
			t.Errorf("two measures called %s", m.UniqueName)
		}
		seen[k] = true
	}
	if c.Measures[0].UniqueName != "[Measures].[Rows]" {
		t.Error("the row count keeps its name")
	}
	// the column that is called as the row count loses; plain column names win over the variants
	if c.Measure("[Measures].[Rows (2)]") == nil || c.Measure("[Measures].[x (Average)]").Column != "x (Average)" ||
		c.Measure("[Measures].[x (Average) (2)]").Column != "x" || c.Measure("[Measures].[x (Average) (2)]").Agg != AggAverage {
		t.Errorf("collisions: %v", measureNames(c))
	}
	if c.Dimension("[Measures]") != nil || c.Dimension("[Measures (2)]") == nil {
		t.Error("[Measures] belongs to the measures")
	}
	// a level may not be called as the (All) level, the All member or the null member
	for _, name := range []string{"All", "(null)", "(All)"} {
		d := c.Dimension(Bracket(name))
		l := d.Hierarchies[0].Levels[1]
		if strings.EqualFold(l.Name, name) {
			t.Errorf("%s: the level is called %s", name, l.Name)
		}
		for _, un := range []string{l.UniqueName, d.Hierarchies[0].AllName(), Member{Level: l, Kind: MemberNull}.UniqueName()} {
			if _, err := c.ResolveName(un); err != nil {
				t.Errorf("%s: %v", un, err)
			}
		}
		if ref, _ := c.ResolveName(d.Hierarchies[0].AllName()); ref.Kind != RefMember || ref.Member.Kind != MemberAll {
			t.Errorf("%s: the All member is read as %+v", name, ref)
		}
	}
	// the same names every time
	again := Build("c", "c", cols("Rows", dataapiwire.KindInt, "Measures", dataapiwire.KindString, "x", dataapiwire.KindDecimal, "x (Average)", dataapiwire.KindDecimal,
		"All", dataapiwire.KindString, "(null)", dataapiwire.KindString, "(All)", dataapiwire.KindInt))
	if strings.Join(measureNames(c), "|") != strings.Join(measureNames(again), "|") {
		t.Error("not deterministic")
	}
	// two columns that differ only by case (a client may send a list that DuckDB would refuse)
	d := Build("c", "c", cols("Name", dataapiwire.KindString, "name", dataapiwire.KindString))
	if d.Dimensions[0].UniqueName != "[Name]" || d.Dimensions[1].UniqueName != "[name (2)]" || d.Dimensions[1].Column != "name" {
		t.Errorf("case collisions: %s %s", d.Dimensions[0].UniqueName, d.Dimensions[1].UniqueName)
	}
}

func TestKeysRoundTrip(t *testing.T) {
	for _, v := range []string{"IT", "", `\`, `\\`, `\n`, `a\b`, "a]b", "]]", "]", "[x]", "&[x]", ".", "a.b", " ", " padded ", "Città", "日本", "true", "null", "(null)", "All", "-1", "a\"b", "a'b"} {
		key := EncodeKey(v)
		if key == "" {
			t.Errorf("%q: an empty key cannot be written between brackets", v)
		}
		if got, ok := DecodeKey(key); !ok || got != v {
			t.Errorf("%q: encoded %q decoded %q %v", v, key, got, ok)
		}
		// written as the segment of a name, and read back through the splitter
		seg := KeySegment(v)
		segs, ok := SplitName("[d].[h].[l]." + seg)
		if !ok || len(segs) != 4 || len(segs[3].Keys) != 1 || segs[3].Keys[0] != key {
			t.Errorf("%q: %s split as %+v", v, seg, segs)
		}
	}
	if _, ok := DecodeKey(`\x`); ok {
		t.Error(`a backslash and a letter is not a key`)
	}
}

func TestMembersOfTextAndNumbers(t *testing.T) {
	c := Build("c", "c", cols("country", dataapiwire.KindString, "code", dataapiwire.KindInt, "paid", dataapiwire.KindBool))
	segs := func(un string) []Segment {
		s, ok := SplitName(un)
		if !ok {
			t.Fatalf("split %s", un)
		}
		return s
	}
	for _, key := range []string{"IT", "", `\`, `back\slash`, "a]b", "Città", "(null)", "All"} {
		un := "[country].[country].[country]." + KeySegment(key)
		ref, err := c.Resolve(segs(un))
		if err != nil || ref.Kind != RefMember || ref.Member.Kind != MemberKey || ref.Member.Key != key {
			t.Fatalf("%q: %+v %v", key, ref, err)
		}
		if ref.Member.UniqueName() != un {
			t.Errorf("%q: written %s, read from %s", key, ref.Member.UniqueName(), un)
		}
	}
	// a text value «All» or «(null)» is a member of its own, not the All or the null member
	if ref, _ := c.ResolveName("[country].[country].[country].&[All]"); ref.Member.Kind != MemberKey || ref.Member.Key != "All" {
		t.Error("a value called All")
	}
	null, err := c.ResolveName("[country].[country].[(null)]")
	if err != nil || null.Member.Kind != MemberNull || null.Member.Level.Name != "country" || null.Member.UniqueName() != "[country].[country].[(null)]" {
		t.Errorf("null: %+v %v", null, err)
	}
	if null.Member.Caption() != NullCaption {
		t.Error("caption")
	}
	// integers and booleans have a canonical key
	ref, err := c.ResolveName("[code].[code].[code].&[+0042]")
	if err != nil || ref.Member.Key != "42" {
		t.Errorf("int: %+v %v", ref, err)
	}
	if _, err := c.ResolveName("[code].[code].[code].&[4x2]"); err == nil {
		t.Error("an integer level takes integers")
	}
	if ref, err := c.ResolveName("[code].[code].[code].&[-170141183460469231731687303715884105728]"); err != nil || ref.Member.Key == "" {
		t.Errorf("a hugeint: %v", err)
	}
	if ref, err := c.ResolveName("[paid].[paid].[paid].&[TRUE]"); err != nil || ref.Member.Key != "true" {
		t.Errorf("bool: %+v %v", ref, err)
	}
	if _, err := c.ResolveName("[paid].[paid].[paid].&[yes]"); err == nil {
		t.Error("a boolean level takes true or false")
	}
	// the parent of a value is All; All has none; a null's parent is All
	m := Member{Level: c.Dimension("[country]").Hierarchies[0].Levels[1], Kind: MemberKey, Key: "IT"}
	if p, ok := m.Parent(); !ok || p.Kind != MemberAll || p.UniqueName() != "[country].[country].[All]" {
		t.Error("parent")
	}
	if _, ok := m.Hierarchy().All().Parent(); ok {
		t.Error("All has no parent")
	}
	if a := m.Ancestors(); len(a) != 1 || a[0].Kind != MemberAll {
		t.Error("ancestors")
	}
}

func TestDateMembers(t *testing.T) {
	c := Build("c", "c", cols("day", dataapiwire.KindDate, "seen", dataapiwire.KindTimestamp, "stz", dataapiwire.KindTimestampTZ))
	cal := c.Dimension("[day]").Hierarchies[0]
	for _, tc := range []struct {
		level, key, parent string
	}{
		{"Year", "2026", "[day].[Year-Quarter-Month-Day].[All]"},
		{"Quarter", "2026-Q1", "[day].[Year-Quarter-Month-Day].[Year].&[2026]"},
		{"Month", "2026-05", "[day].[Year-Quarter-Month-Day].[Quarter].&[2026-Q2]"},
		{"Month", "2026-12", "[day].[Year-Quarter-Month-Day].[Quarter].&[2026-Q4]"},
		{"Day", "2026-01-15", "[day].[Year-Quarter-Month-Day].[Month].&[2026-01]"},
		{"Day", "2024-02-29", "[day].[Year-Quarter-Month-Day].[Month].&[2024-02]"},
	} {
		un := "[day].[Year-Quarter-Month-Day]." + "[" + tc.level + "].&[" + tc.key + "]"
		ref, err := c.ResolveName(un)
		if err != nil || ref.Member.Key != tc.key || ref.Member.UniqueName() != un {
			t.Errorf("%s: %+v %v", un, ref, err)
			continue
		}
		p, ok := ref.Member.Parent()
		if !ok || p.UniqueName() != tc.parent {
			t.Errorf("%s: parent %s, want %s", un, p.UniqueName(), tc.parent)
		}
	}
	day, _ := c.ResolveName("[day].[Year-Quarter-Month-Day].[Day].&[2026-05-20]")
	var chain []string
	for _, a := range day.Member.Ancestors() {
		chain = append(chain, a.UniqueName())
	}
	if want := "[day].[Year-Quarter-Month-Day].[All] [day].[Year-Quarter-Month-Day].[Year].&[2026] [day].[Year-Quarter-Month-Day].[Quarter].&[2026-Q2] [day].[Year-Quarter-Month-Day].[Month].&[2026-05]"; strings.Join(chain, " ") != want {
		t.Errorf("ancestors %v", chain)
	}
	p, _ := DateParts(cal.Levels[4], "2026-05-20")
	if p != (Parts{2026, 2, 5, 20}) {
		t.Errorf("parts %+v", p)
	}
	// keys that are not those of a level
	for _, un := range []string{"[day].[Year-Quarter-Month-Day].[Year].&[26]", "[day].[Year-Quarter-Month-Day].[Year].&[abcd]",
		"[day].[Year-Quarter-Month-Day].[Quarter].&[2026-Q5]", "[day].[Year-Quarter-Month-Day].[Quarter].&[2026-1]", "[day].[Year-Quarter-Month-Day].[Month].&[2026-13]",
		"[day].[Year-Quarter-Month-Day].[Month].&[2026-00]", "[day].[Year-Quarter-Month-Day].[Day].&[2026-02-30]", "[day].[Year-Quarter-Month-Day].[Day].&[2025-02-29]",
		"[day].[Year-Quarter-Month-Day].[Day].&[2026-1-5]", "[day].[day].[day].&[2026-01-15 08:00:00]", "[day].[day].[day].&[x]", `[day].[Year-Quarter-Month-Day].[Year].&[\]`} {
		if _, err := c.ResolveName(un); err == nil {
			t.Errorf("%s was read", un)
		}
	}
	// the plain value: a date is a day, a timestamp a day and a time, and a «T» is the space
	if ref, err := c.ResolveName("[day].[day].[day].&[2026-01-15]"); err != nil || ref.Member.Key != "2026-01-15" || ref.Member.Level.Part != PartValue {
		t.Errorf("plain date %+v %v", ref, err)
	}
	if p, _ := (Member{Level: c.Dimension("[day]").Hierarchies[1].Levels[1], Kind: MemberKey, Key: "2026-01-15"}).Parent(); p.Kind != MemberAll {
		t.Error("the plain value's parent is All")
	}
	for _, name := range []string{"seen", "stz"} {
		for in, want := range map[string]string{"2026-01-15 08:30:00": "2026-01-15 08:30:00", "2026-01-15T08:30:00": "2026-01-15 08:30:00",
			"2026-01-15 08:30:00.25": "2026-01-15 08:30:00.25", "2026-01-15 08:30:00.123456": "2026-01-15 08:30:00.123456"} {
			ref, err := c.ResolveName("[" + name + "].[" + name + "].[" + name + "].&[" + in + "]")
			if err != nil || ref.Member.Key != want {
				t.Errorf("%s %s: %+v %v", name, in, ref, err)
			}
		}
		for _, bad := range []string{"2026-01-15", "2026-01-15 25:00:00", "2026-01-15 08:61:00", "2026-13-15 08:00:00", "nope"} {
			if _, err := c.ResolveName("[" + name + "].[" + name + "].[" + name + "].&[" + bad + "]"); err == nil {
				t.Errorf("%s %s was read", name, bad)
			}
		}
		// a timestamp has the calendar hierarchy too
		if _, err := c.ResolveName("[" + name + "].[Year-Quarter-Month-Day].[Month].&[2026-01]"); err != nil {
			t.Error(err)
		}
	}
	// the null date is a member of the first level of each hierarchy
	n, err := c.ResolveName("[day].[Year-Quarter-Month-Day].[(null)]")
	if err != nil || n.Member.Level.Name != "Year" {
		t.Errorf("null date: %+v %v", n, err)
	}
	if p, ok := n.Member.Parent(); !ok || p.Kind != MemberAll {
		t.Error("a null date is under All")
	}
}

func TestResolveErrorsAndShapes(t *testing.T) {
	c := Build("c", "c", cols("country", dataapiwire.KindString, "amount", dataapiwire.KindDecimal))
	for un, kind := range map[string]RefKind{
		"[Measures]": RefMeasures, "[measures].[Measures]": RefMeasures, "[Measures].[Rows]": RefMeasure, "[MEASURES].[AMOUNT (min)]": RefMeasure,
		"[Measures].[MeasuresLevel]": RefMeasuresLevel, "[Measures].[Measures].[MeasuresLevel]": RefMeasuresLevel,
		"[country]": RefDimension, "[country].[country]": RefHierarchy, "[country].[country].[(All)]": RefLevel, "[country].[country].[All]": RefMember,
		"[Country].[COUNTRY].[Country].&[IT]": RefMember,
	} {
		ref, err := c.ResolveName(un)
		if err != nil || ref.Kind != kind {
			t.Errorf("%s: %+v %v", un, ref, err)
		}
	}
	for _, un := range []string{"", "country", "[nope]", "[Measures].[nope]", "[country].[nope]", "[country].[country].[nope]", "[country].[country].[country]x",
		"[country].[country].[country].[IT]", "[country].[country].[country].&[a]&[b]", "[country].[country].[(All)].&[IT]", "[country].[country].&[IT]",
		"[country].[country].[All].&[x]", "[a]]", "[country]..[country]", "[country].", "[country].&[x]", "&[x]", "[country].[country].[country].&[]", "[].[x]",
		"[Measures].[Rows].[x]", "[Measures].[Measures].[nope]"} {
		if ref, err := c.ResolveName(un); err == nil {
			t.Errorf("%q was read as %+v", un, ref)
		} else if _, ok := err.(*NameError); !ok {
			t.Errorf("%q: %T", un, err)
		}
	}
	// a key may only end a name
	if _, err := c.Resolve([]Segment{{Name: "country"}, {Keys: []string{"x"}}, {Name: "country"}, {Name: "country"}}); err == nil {
		t.Error("a key in the middle")
	}
	if _, err := c.Resolve(nil); err == nil {
		t.Error("nothing")
	}
}

func TestSplitName(t *testing.T) {
	segs, ok := SplitName("[a]]b].[c d].&[1]&[2]")
	if !ok || len(segs) != 3 || segs[0].Name != "a]b" || segs[1].Name != "c d" || len(segs[2].Keys) != 2 || segs[2].Keys[1] != "2" {
		t.Errorf("%+v %v", segs, ok)
	}
	for _, bad := range []string{"", "[", "[a", "[a]x", "[a].", ".[a]", "[a]]", "&", "&[", "[a]&[b]", "[]"} {
		if _, ok := SplitName(bad); ok {
			t.Errorf("%q was split", bad)
		}
	}
}

func TestMeasureFormats(t *testing.T) {
	for _, tc := range []struct {
		kind   string
		scale  int
		agg    Agg
		format string
	}{
		{dataapiwire.KindInt, 0, AggSum, "#,0"}, {dataapiwire.KindDecimal, 0, AggSum, "#,0"}, {dataapiwire.KindDecimal, 3, AggMin, "#,0.000"},
		{dataapiwire.KindFloat, 0, AggSum, "#,0.00"}, {dataapiwire.KindInt, 0, AggAverage, "#,0.00"}, {"", 0, AggRows, "#,0"},
	} {
		m := &Measure{Kind: tc.kind, Scale: tc.scale, Agg: tc.agg}
		if got := m.FormatString(); got != tc.format {
			t.Errorf("%+v: %q, want %q", tc, got, tc.format)
		}
	}
}

func TestBracket(t *testing.T) {
	for in, want := range map[string]string{"a": "[a]", "a]b": "[a]]b]", "]": "[]]]", "": "[]", "[": "[[]", "a b": "[a b]"} {
		if got := Bracket(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
}
