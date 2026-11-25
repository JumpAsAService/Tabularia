package mdx

import (
	"reflect"
	"strings"
	"testing"
)

func mustParse(t *testing.T, src string) *Statement {
	t.Helper()
	st, err := Parse(src)
	if err != nil {
		t.Fatalf("Parse(%q): %v", src, err)
	}
	return st
}

// roundTrip checks parse -> print -> parse gives an equal tree and that the
// printed text is a fixed point.
func roundTrip(t *testing.T, src string) string {
	t.Helper()
	a := mustParse(t, src)
	text := a.String()
	b, err := Parse(text)
	if err != nil {
		t.Fatalf("printed form does not parse: %v\nsource:  %s\nprinted: %s", err, src, text)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("round trip changed the tree\nsource:  %s\nprinted: %s", src, text)
	}
	if again := b.String(); again != text {
		t.Fatalf("print is not stable\nfirst:  %s\nsecond: %s", text, again)
	}
	return text
}

// Statements that must parse; canonical is the expected print, "" to skip.
var validCases = []struct{ name, src, canonical string }{
	{"columns only", `SELECT {[a].[b].Members} ON COLUMNS FROM [c]`,
		`SELECT {[a].[b].Members} ON COLUMNS FROM [c]`},
	{"rows then columns", `SELECT {[a].[b].Members} ON ROWS, {[Measures].[m]} ON COLUMNS FROM [c]`,
		`SELECT {[a].[b].Members} ON ROWS, {[Measures].[m]} ON COLUMNS FROM [c]`},
	{"three axes", `SELECT {[Measures].[m]} ON COLUMNS, {[a].[b].Members} ON ROWS, {[d].[e].Members} ON PAGES FROM [c]`, ""},
	{"five named axes", `SELECT {[a].[a].Members} ON 0, {[b].[b].Members} ON 1, {[c].[c].Members} ON 2, {[d].[d].Members} ON CHAPTERS, {[e].[e].Members} ON SECTIONS FROM [x]`, ""},
	{"axis by number", `SELECT {[a].[b]} ON 0, {[c].[d]} ON 1 FROM [x]`,
		`SELECT {[a].[b]} ON COLUMNS, {[c].[d]} ON ROWS FROM [x]`},
	{"axis function", `SELECT {[a].[b]} ON AXIS(0), {[c].[d]} ON axis( 1 ) FROM [x]`,
		`SELECT {[a].[b]} ON COLUMNS, {[c].[d]} ON ROWS FROM [x]`},
	{"axis number beyond named", `SELECT {[a]} ON 0, {[b]} ON 1, {[c]} ON 2, {[d]} ON 3, {[e]} ON 4, {[f]} ON AXIS(5) FROM [x]`,
		`SELECT {[a]} ON COLUMNS, {[b]} ON ROWS, {[c]} ON PAGES, {[d]} ON CHAPTERS, {[e]} ON SECTIONS, {[f]} ON AXIS(5) FROM [x]`},
	{"axis hole is left to the planner", `SELECT {[a].[b]} ON ROWS FROM [c]`, ""},
	{"no axes", `SELECT FROM [c] WHERE ([Measures].[m])`, `SELECT FROM [c] WHERE ([Measures].[m])`},
	{"non empty", `SELECT NON EMPTY {[a].[b].Members} ON ROWS FROM [c]`, `SELECT NON EMPTY {[a].[b].Members} ON ROWS FROM [c]`},
	{"non empty lower", `select non empty {[a].[b].Members} on rows from [c]`, `SELECT NON EMPTY {[a].[b].Members} ON ROWS FROM [c]`},
	{"dimension properties", `SELECT {[a].[b].Members} DIMENSION PROPERTIES PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME ON COLUMNS FROM [c]`,
		`SELECT {[a].[b].Members} DIMENSION PROPERTIES PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME ON COLUMNS FROM [c]`},
	{"properties shorthand", `SELECT {[a].[b].Members} PROPERTIES MEMBER_CAPTION ON COLUMNS FROM [c]`,
		`SELECT {[a].[b].Members} DIMENSION PROPERTIES MEMBER_CAPTION ON COLUMNS FROM [c]`},
	{"dotted property", `SELECT {[a].[b].Members} DIMENSION PROPERTIES [a].[b].[Caption] ON COLUMNS FROM [c]`,
		`SELECT {[a].[b].Members} DIMENSION PROPERTIES [a].[b].[Caption] ON COLUMNS FROM [c]`},
	{"cell properties", `SELECT {[a].[b]} ON COLUMNS FROM [c] CELL PROPERTIES VALUE, FORMATTED_VALUE`,
		`SELECT {[a].[b]} ON COLUMNS FROM [c] CELL PROPERTIES VALUE, FORMATTED_VALUE`},
	{"where tuple", `SELECT {[a].[b]} ON COLUMNS FROM [c] WHERE ([Measures].[m], [d].[e].&[x])`, ""},
	{"where bare member", `SELECT {[a].[b]} ON COLUMNS FROM [c] WHERE [Measures].[m]`, ""},
	{"where set", `SELECT {[a].[b]} ON COLUMNS FROM [c] WHERE {[d].[e].&[x]}`, ""},
	{"where and cell properties", `SELECT {[a].[b]} ON COLUMNS FROM [c] WHERE ([Measures].[m]) CELL PROPERTIES VALUE`, ""},
	{"qualified cube", `SELECT {[a].[b]} ON COLUMNS FROM [catalog].[cube]`, `SELECT {[a].[b]} ON COLUMNS FROM [catalog].[cube]`},
	{"bare cube", `SELECT {[a].[b]} ON COLUMNS FROM orders`, `SELECT {[a].[b]} ON COLUMNS FROM orders`},
	{"subselect", `SELECT {[a].[b]} ON COLUMNS FROM (SELECT ({[x].[y].&[1]}) ON COLUMNS FROM [c])`,
		`SELECT {[a].[b]} ON COLUMNS FROM (SELECT ({[x].[y].&[1]}) ON COLUMNS FROM [c])`},
	{"nested subselect", `SELECT {[a].[b]} ON COLUMNS FROM (SELECT ({[x].[y].&[1]}) ON COLUMNS FROM (SELECT ({[z].[w].&[2]}) ON COLUMNS FROM [c]))`, ""},
	{"subselect with where and two axes", `SELECT {[a].[b]} ON COLUMNS FROM (SELECT ({[x].[y]}) ON COLUMNS, {[z].[w]} ON ROWS FROM [c] WHERE ([Measures].[m]))`, ""},
	{"subselect with filter in a set", `SELECT {[a].[b]} ON COLUMNS FROM (SELECT ({[x].[y].&[1], [x].[y].&[2]}) ON COLUMNS FROM [c])`, ""},
	{"set literal", `SELECT {[a].[b].&[1], [a].[b].&[2]} ON COLUMNS FROM [c]`, ""},
	{"empty set", `SELECT {} ON COLUMNS FROM [c]`, `SELECT {} ON COLUMNS FROM [c]`},
	{"nested braces", `SELECT {{[a].[b]}, {[a].[c]}} ON COLUMNS FROM [c]`, ""},
	{"tuple in set", `SELECT {([a].[b].&[1], [d].[e].&[2])} ON COLUMNS FROM [c]`, ""},
	{"star operator", `SELECT [a].[b].Members * [d].[e].Members ON ROWS FROM [c]`,
		`SELECT CrossJoin([a].[b].Members, [d].[e].Members) ON ROWS FROM [c]`},
	{"star chain", `SELECT {[a].[b]} * {[d].[e]} * {[f].[g]} ON ROWS FROM [c]`,
		`SELECT CrossJoin({[a].[b]}, {[d].[e]}, {[f].[g]}) ON ROWS FROM [c]`},
	{"crossjoin two", `SELECT CrossJoin({[a].[b]}, {[d].[e]}) ON ROWS FROM [c]`, ""},
	{"crossjoin three", `SELECT CrossJoin({[a].[b]}, {[d].[e]}, {[f].[g]}) ON ROWS FROM [c]`, ""},
	{"crossjoin nested", `SELECT CrossJoin(CrossJoin({[a].[b]}, {[d].[e]}), {[f].[g]}) ON ROWS FROM [c]`, ""},
	{"crossjoin lower", `SELECT crossjoin({[a].[b]}, {[d].[e]}) ON ROWS FROM [c]`,
		`SELECT CrossJoin({[a].[b]}, {[d].[e]}) ON ROWS FROM [c]`},
	{"hierarchize", `SELECT Hierarchize({[a].[b].Members}) ON ROWS FROM [c]`, ""},
	{"hierarchize post", `SELECT Hierarchize({[a].[b].Members}, POST) ON ROWS FROM [c]`, ""},
	{"hierarchize pre lower", `SELECT hierarchize({[a].[b].Members}, pre) ON ROWS FROM [c]`,
		`SELECT Hierarchize({[a].[b].Members}, PRE) ON ROWS FROM [c]`},
	{"drilldownlevel set only", `SELECT DrilldownLevel({[a].[b].[All]}) ON ROWS FROM [c]`, ""},
	{"drilldownlevel level", `SELECT DrilldownLevel({[a].[b].[All]}, [a].[b].[Country]) ON ROWS FROM [c]`, ""},
	{"drilldownlevel level and index", `SELECT DrilldownLevel({[a].[b].[All]}, [a].[b].[Country], 2) ON ROWS FROM [c]`, ""},
	{"drilldownlevel empties", `SELECT DrilldownLevel({[a].[b].[All]},,,INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`,
		`SELECT DrilldownLevel({[a].[b].[All]}, , , INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`},
	{"drilldownlevel all four", `SELECT DrilldownLevel({[a].[b].[All]}, [a].[b].[Country], 1, INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`, ""},
	{"drilldownlevel flag second", `SELECT DrilldownLevel({[a].[b].[All]}, INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`, ""},
	{"drilldownlevel spaced empties", `SELECT DrilldownLevel( {[a].[b].[All]} , , , include_calc_members ) ON ROWS FROM [c]`,
		`SELECT DrilldownLevel({[a].[b].[All]}, , , INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`},
	{"drilldownmember", `SELECT DrilldownMember({[a].[b].[All]}, {[a].[b].[Year].&[2026]}) ON ROWS FROM [c]`, ""},
	{"drilldownmember recursive", `SELECT DrilldownMember({[a].[b].[All]}, {[a].[b].[Year].&[2026]}, RECURSIVE) ON ROWS FROM [c]`, ""},
	{"drilldownmember flags", `SELECT DrilldownMember({[a].[b].[All]}, {[a].[b].[Year].&[2026]},,,INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`, ""},
	{"drilldownmember both flags", `SELECT DrilldownMember({[a].[b].[All]}, {[a].[b].[Year].&[2026]}, RECURSIVE, INCLUDE_CALC_MEMBERS) ON ROWS FROM [c]`, ""},
	{"addcalculatedmembers", `SELECT AddCalculatedMembers({[a].[b].Members}) ON ROWS FROM [c]`, ""},
	{"except", `SELECT Except({[a].[b].Members}, {[a].[b].&[x]}) ON ROWS FROM [c]`, ""},
	{"except all", `SELECT Except({[a].[b].Members}, {[a].[b].&[x]}, ALL) ON ROWS FROM [c]`, ""},
	{"distinct", `SELECT Distinct({[a].[b].Members}) ON ROWS FROM [c]`, ""},
	{"head", `SELECT Head({[a].[b].Members}) ON ROWS FROM [c]`, ""},
	{"head n", `SELECT Head({[a].[b].Members}, 10) ON ROWS FROM [c]`, ""},
	{"topcount", `SELECT TopCount({[a].[b].Members}, 5) ON ROWS FROM [c]`, ""},
	{"topcount measure", `SELECT TopCount({[a].[b].Members}, 5, [Measures].[m]) ON ROWS FROM [c]`, ""},
	{"bottomcount measure", `SELECT BottomCount({[a].[b].Members}, 5, [Measures].[m]) ON ROWS FROM [c]`, ""},
	{"order", `SELECT Order({[a].[b].Members}, [Measures].[m]) ON ROWS FROM [c]`, ""},
	{"order asc", `SELECT Order({[a].[b].Members}, [Measures].[m], ASC) ON ROWS FROM [c]`, ""},
	{"order desc", `SELECT Order({[a].[b].Members}, [Measures].[m], DESC) ON ROWS FROM [c]`, ""},
	{"order basc", `SELECT Order({[a].[b].Members}, [Measures].[m], BASC) ON ROWS FROM [c]`, ""},
	{"order bdesc lower", `SELECT order({[a].[b].Members}, [Measures].[m], bdesc) ON ROWS FROM [c]`,
		`SELECT Order({[a].[b].Members}, [Measures].[m], BDESC) ON ROWS FROM [c]`},
	{"filter gt", `SELECT Filter({[a].[b].Members}, [Measures].[m] > 100) ON ROWS FROM [c]`, ""},
	{"filter ops", `SELECT Filter({[a].[b].Members}, [Measures].[m] >= 1) ON ROWS FROM [c]`, ""},
	{"filter lt", `SELECT Filter({[a].[b].Members}, [Measures].[m] < 1.5) ON ROWS FROM [c]`, ""},
	{"filter le", `SELECT Filter({[a].[b].Members}, [Measures].[m] <= 1e3) ON ROWS FROM [c]`, ""},
	{"filter eq", `SELECT Filter({[a].[b].Members}, [Measures].[m] = 0) ON ROWS FROM [c]`, ""},
	{"filter ne", `SELECT Filter({[a].[b].Members}, [Measures].[m] <> 0) ON ROWS FROM [c]`, ""},
	{"filter negative", `SELECT Filter({[a].[b].Members}, [Measures].[m] > -5) ON ROWS FROM [c]`, ""},
	{"filter and", `SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 AND [Measures].[n] < 9) ON ROWS FROM [c]`, ""},
	{"filter or lower", `SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 or [Measures].[n] < 9) ON ROWS FROM [c]`,
		`SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 OR [Measures].[n] < 9) ON ROWS FROM [c]`},
	{"filter precedence", `SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 OR [Measures].[n] < 9 AND [Measures].[o] = 3) ON ROWS FROM [c]`,
		`SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 OR [Measures].[n] < 9 AND [Measures].[o] = 3) ON ROWS FROM [c]`},
	{"filter groups", `SELECT Filter({[a].[b].Members}, ([Measures].[m] > 1 OR [Measures].[n] < 9) AND [Measures].[o] = 3) ON ROWS FROM [c]`,
		`SELECT Filter({[a].[b].Members}, ([Measures].[m] > 1 OR [Measures].[n] < 9) AND [Measures].[o] = 3) ON ROWS FROM [c]`},
	{"filter flattens", `SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 AND ([Measures].[n] < 9 AND [Measures].[o] = 3)) ON ROWS FROM [c]`,
		`SELECT Filter({[a].[b].Members}, [Measures].[m] > 1 AND [Measures].[n] < 9 AND [Measures].[o] = 3) ON ROWS FROM [c]`},
	{"filter bare measures", `SELECT Filter({[a].[b].Members}, Measures.m > 1) ON ROWS FROM [c]`, ""},
	{"filter inside crossjoin", `SELECT CrossJoin(Filter({[a].[b].Members}, [Measures].[m] > 1), {[d].[e]}) ON ROWS FROM [c]`, ""},
	{"members", `SELECT {[a].[b].Members} ON ROWS FROM [c]`, ""},
	{"allmembers", `SELECT {[a].[b].AllMembers} ON ROWS FROM [c]`, ""},
	{"children", `SELECT {[a].[b].[All].Children} ON ROWS FROM [c]`, ""},
	{"defaultmember", `SELECT {[a].[b].DefaultMember} ON ROWS FROM [c]`, ""},
	{"suffix case", `SELECT {[a].[b].MEMBERS, [a].[c].children, [a].[d].allmembers, [a].[e].defaultMember} ON ROWS FROM [c]`,
		`SELECT {[a].[b].Members, [a].[c].Children, [a].[d].AllMembers, [a].[e].DefaultMember} ON ROWS FROM [c]`},
	{"all member", `SELECT {[a].[b].[All]} ON ROWS FROM [c]`, ""},
	{"member by key", `SELECT {[a].[b].[Year].&[2026]} ON ROWS FROM [c]`, ""},
	{"two keys", `SELECT {[a].[b].[Month].&[2026]&[4]} ON ROWS FROM [c]`, ""},
	{"three keys", `SELECT {[a].[b].[Day].&[2026]&[4]&[15]} ON ROWS FROM [c]`, ""},
	{"key with brackets", `SELECT {[a].[b].&[x]]y]} ON ROWS FROM [c]`, ""},
	{"escaped bracket name", `SELECT {[a]]b].[c]]]} ON ROWS FROM [c]`, ""},
	{"name with spaces and unicode", `SELECT {[Prodotti più venduti].[Città - Nome]} ON ROWS FROM [c]`, ""},
	{"measures", `SELECT {[Measures].[amount Sum], [Measures].[Rows]} ON COLUMNS FROM [c]`, ""},
	{"measures bare", `SELECT {Measures.amount} ON COLUMNS FROM [c]`, `SELECT {Measures.amount} ON COLUMNS FROM [c]`},
	{"bare identifiers", `SELECT {Geo.Country.Italy} ON COLUMNS FROM c`, `SELECT {Geo.Country.Italy} ON COLUMNS FROM c`},
	{"line comment dashes", "SELECT {[a].[b]} -- ON COLUMNS\nON COLUMNS FROM [c]", `SELECT {[a].[b]} ON COLUMNS FROM [c]`},
	{"line comment slashes", "SELECT {[a].[b]} // note\nON COLUMNS FROM [c]", `SELECT {[a].[b]} ON COLUMNS FROM [c]`},
	{"block comment", "SELECT /* c1 */ {[a].[b]} /* c2\nspans */ ON COLUMNS FROM [c]", `SELECT {[a].[b]} ON COLUMNS FROM [c]`},
	{"comment last line", "SELECT {[a].[b]} ON COLUMNS FROM [c] -- end", `SELECT {[a].[b]} ON COLUMNS FROM [c]`},
	{"comment markers inside brackets", `SELECT {[a--b].[c//d], [e/*f*/]} ON COLUMNS FROM [c]`, ""},
	{"crlf and tabs", "SELECT\r\n\t{[a].[b]}\r\nON COLUMNS\r\nFROM [c]\r\n", `SELECT {[a].[b]} ON COLUMNS FROM [c]`},
	{"leading and trailing space", "  \n SELECT {[a].[b]} ON COLUMNS FROM [c] \n ", ""},
	{"numbers", `SELECT Head({[a].[b].Members}, 3) ON ROWS FROM [c] WHERE ([Measures].[m])`, ""},
	{"exponent number", `SELECT Filter({[a].[b]}, [Measures].[m] > 2.5E-3) ON ROWS FROM [c]`, ""},
	{"crossjoin with empties inside", `SELECT CrossJoin(DrilldownLevel({[a].[b].[All]},,,INCLUDE_CALC_MEMBERS), {[d].[e]}) ON ROWS FROM [c]`, ""},
	{"parenthesised set", `SELECT ({[a].[b].Members}) ON ROWS FROM [c]`, ""},
	{"a call as a set item", `SELECT {Hierarchize({[a].[b].Members}), [d].[e]} ON ROWS FROM [c]`, ""},
}

func TestParseValid(t *testing.T) {
	for _, tc := range validCases {
		t.Run(tc.name, func(t *testing.T) {
			text := roundTrip(t, tc.src)
			if tc.canonical != "" && text != tc.canonical {
				t.Errorf("canonical form\n got: %s\nwant: %s", text, tc.canonical)
			}
		})
	}
}

// The three statements a pivot sends, checked down to the tree.
func TestParsePivotShapes(t *testing.T) {
	t.Run("one column field", func(t *testing.T) {
		st := mustParse(t, `SELECT NON EMPTY Hierarchize({DrilldownLevel({[country].[country].[All]},,,INCLUDE_CALC_MEMBERS)}) DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME ON COLUMNS FROM [orders] WHERE ([Measures].[amount Sum]) CELL PROPERTIES VALUE, FORMAT_STRING`)
		if len(st.Axes) != 1 || st.Axes[0].Index != 0 || !st.Axes[0].NonEmpty {
			t.Fatalf("axes: %+v", st.Axes)
		}
		h := st.Axes[0].Set.(*Call)
		if h.Name != "Hierarchize" || len(h.Args) != 1 {
			t.Fatalf("hierarchize: %+v", h)
		}
		dl := h.Args[0].(*SetLit).Items[0].(*Call)
		if dl.Name != "DrilldownLevel" || len(dl.Args) != 4 {
			t.Fatalf("drilldownlevel: %+v", dl)
		}
		if _, ok := dl.Args[1].(*Empty); !ok {
			t.Errorf("arg 2 should be Empty: %T", dl.Args[1])
		}
		if f := dl.Args[3].(*Flag); f.Name != "INCLUDE_CALC_MEMBERS" {
			t.Errorf("flag: %+v", f)
		}
		ref := dl.Args[0].(*SetLit).Items[0].(*Ref)
		want := []Segment{{Name: "country", Bracketed: true}, {Name: "country", Bracketed: true}, {Name: "All", Bracketed: true}}
		if !reflect.DeepEqual(ref.Segments, want) {
			t.Errorf("segments: %+v", ref.Segments)
		}
		if got := st.Axes[0].DimProps; len(got) != 2 || got[0].Name() != "PARENT_UNIQUE_NAME" || got[1].Name() != "HIERARCHY_UNIQUE_NAME" {
			t.Errorf("dim props: %+v", got)
		}
		if st.From.Cube[0].Name != "orders" || st.From.Sub != nil {
			t.Errorf("from: %+v", st.From)
		}
		slicer := st.Slicer.(*Paren)
		if m := slicer.Items[0].(*Ref); !isMeasure(m) || m.Segments[1].Name != "amount Sum" {
			t.Errorf("slicer: %+v", m)
		}
		if len(st.Cells) != 2 || st.Cells[1].Name() != "FORMAT_STRING" {
			t.Errorf("cells: %+v", st.Cells)
		}
	})
	t.Run("subselect and swapped axes", func(t *testing.T) {
		st := mustParse(t, `SELECT NON EMPTY CrossJoin(Hierarchize({DrilldownLevel({[date].[Year-Quarter-Month-Day].[All]},,,INCLUDE_CALC_MEMBERS)}), Hierarchize({DrilldownLevel({[country].[country].[All]},,,INCLUDE_CALC_MEMBERS)})) DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME ON ROWS, {[Measures].[amount Sum],[Measures].[Rows]} DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME ON COLUMNS FROM (SELECT ({[channel].[channel].&[web]}) ON COLUMNS FROM [orders]) CELL PROPERTIES VALUE`)
		if st.Axes[0].Index != 1 || st.Axes[1].Index != 0 {
			t.Fatalf("axis order as written: %d %d", st.Axes[0].Index, st.Axes[1].Index)
		}
		if st.Axis(0) != st.Axes[1] || st.Axis(1) != st.Axes[0] || st.Axis(2) != nil {
			t.Error("Axis lookup")
		}
		cj := st.Axis(1).Set.(*Call)
		if cj.Name != "CrossJoin" || len(cj.Args) != 2 {
			t.Fatalf("crossjoin: %+v", cj)
		}
		if st.From.Sub == nil || st.From.Cube != nil {
			t.Fatalf("from: %+v", st.From)
		}
		sub := st.From.Sub
		if len(sub.Axes) != 1 || sub.From.Cube[0].Name != "orders" {
			t.Fatalf("sub: %+v", sub)
		}
		key := sub.Axes[0].Set.(*Paren).Items[0].(*SetLit).Items[0].(*Ref)
		if last := key.Segments[len(key.Segments)-1]; !reflect.DeepEqual(last.Keys, []string{"web"}) || last.Name != "" {
			t.Errorf("key segment: %+v", last)
		}
	})
	t.Run("drilldown member with slicer tuple", func(t *testing.T) {
		st := mustParse(t, `SELECT NON EMPTY Hierarchize(DrilldownMember({{DrilldownLevel({[date].[Year-Quarter-Month-Day].[All]},,,INCLUDE_CALC_MEMBERS)}}, {[date].[Year-Quarter-Month-Day].[Year].&[2026]},,,INCLUDE_CALC_MEMBERS)) ON COLUMNS FROM [orders] WHERE ([Measures].[Rows],[country].[country].&[IT])`)
		dm := st.Axes[0].Set.(*Call).Args[0].(*Call)
		if dm.Name != "DrilldownMember" || len(dm.Args) != 5 {
			t.Fatalf("drilldownmember: %+v", dm)
		}
		if len(st.Slicer.(*Paren).Items) != 2 {
			t.Errorf("slicer tuple")
		}
	})
}

func TestParseDetails(t *testing.T) {
	t.Run("escapes", func(t *testing.T) {
		st := mustParse(t, `SELECT {[a]]b].[c]]]]].&[k]]1]} ON COLUMNS FROM [x]]y]`)
		segs := st.Axes[0].Set.(*SetLit).Items[0].(*Ref).Segments
		if segs[0].Name != "a]b" || segs[1].Name != "c]]" || segs[2].Keys[0] != "k]1" {
			t.Errorf("segments: %+v", segs)
		}
		if st.From.Cube[0].Name != "x]y" {
			t.Errorf("cube: %+v", st.From.Cube)
		}
	})
	t.Run("keys keep order", func(t *testing.T) {
		st := mustParse(t, `SELECT {[a].[b].&[2026]&[4]&[x y]} ON COLUMNS FROM [c]`)
		segs := st.Axes[0].Set.(*SetLit).Items[0].(*Ref).Segments
		if !reflect.DeepEqual(segs[2].Keys, []string{"2026", "4", "x y"}) {
			t.Errorf("keys: %v", segs[2].Keys)
		}
	})
	t.Run("suffix and all", func(t *testing.T) {
		st := mustParse(t, `SELECT {[a].[b].[All].Children, [a].[b].Members} ON COLUMNS FROM [c]`)
		items := st.Axes[0].Set.(*SetLit).Items
		r0, r1 := items[0].(*Ref), items[1].(*Ref)
		if r0.Suffix != "Children" || r0.Segments[2].Name != "All" || r1.Suffix != "Members" || len(r1.Segments) != 2 {
			t.Errorf("%+v %+v", r0, r1)
		}
	})
	t.Run("numbers", func(t *testing.T) {
		for text, v := range map[string]float64{"7": 7, "2.5": 2.5, "1e3": 1000, "1.5E-2": 0.015, "-4": -4, "0": 0} {
			st := mustParse(t, `SELECT Filter({[a].[b]}, [Measures].[m] > `+text+`) ON COLUMNS FROM [c]`)
			n := st.Axes[0].Set.(*Call).Args[1].(*Compare).Right
			if n.Value != v || n.Text != text {
				t.Errorf("%s: %+v", text, n)
			}
		}
	})
	t.Run("star equals crossjoin", func(t *testing.T) {
		a := mustParse(t, `SELECT {[a].[b]} * {[c].[d]} ON ROWS FROM [x]`)
		b := mustParse(t, `SELECT CrossJoin({[a].[b]}, {[c].[d]}) ON ROWS FROM [x]`)
		if !reflect.DeepEqual(a, b) {
			t.Error("a * b should equal CrossJoin(a, b)")
		}
	})
	t.Run("keyword case is irrelevant but names keep theirs", func(t *testing.T) {
		a := mustParse(t, `sElEcT NoN eMpTy {[Ab].[Cd]} oN rOwS fRoM [Orders] wHeRe ([Measures].[M])`)
		if a.Axes[0].Set.(*SetLit).Items[0].(*Ref).Segments[0].Name != "Ab" || a.From.Cube[0].Name != "Orders" {
			t.Error("names must keep their case")
		}
	})
	t.Run("filter tree", func(t *testing.T) {
		st := mustParse(t, `SELECT Filter({[a].[b]}, [Measures].[m] > 1 OR [Measures].[n] <= 2 AND [Measures].[o] <> 3) ON COLUMNS FROM [c]`)
		or := st.Axes[0].Set.(*Call).Args[1].(*Logic)
		if or.Op != "OR" || len(or.Terms) != 2 {
			t.Fatalf("or: %+v", or)
		}
		and := or.Terms[1].(*Logic)
		if and.Op != "AND" || len(and.Terms) != 2 || and.Terms[1].(*Compare).Op != "<>" {
			t.Errorf("and: %+v", and)
		}
	})
}

// Statements that must be refused. at is a snippet of the statement where the
// error must point (its first occurrence); "" means offset 0.
var refusalCases = []struct {
	name, src, code, errName, at string
}{
	{"with member", `WITH MEMBER [Measures].[x] AS 1 SELECT {[Measures].[x]} ON COLUMNS FROM [c]`, CodeWithMember, "WITH MEMBER", "WITH"},
	{"with member lower and spaced", "  with\n member [Measures].[x] as 1 select {} on 0 from [c]", CodeWithMember, "WITH MEMBER", "with"},
	{"with set", `WITH SET [s] AS '{[a].[b]}' SELECT [s] ON COLUMNS FROM [c]`, CodeWithSet, "WITH SET", "WITH"},
	{"with other", `WITH CELL CALCULATION x FOR '(*)' AS '1' SELECT {} ON 0 FROM [c]`, CodeWithOther, "WITH CELL", "WITH"},
	{"with alone", `WITH`, CodeWithOther, "WITH", "WITH"},
	{"create member", `CREATE MEMBER [c].[Measures].[x] AS 1`, CodeCreate, "CREATE MEMBER", "CREATE"},
	{"create alone", `create`, CodeCreate, "CREATE", "create"},
	{"drillthrough", `DRILLTHROUGH SELECT {[a].[b]} ON COLUMNS FROM [c]`, CodeDrillthrough, "DRILLTHROUGH", "DRILLTHROUGH"},
	{"call", `CALL MyProc(1)`, CodeCall, "CALL", "CALL"},
	{"update", `UPDATE CUBE [c] SET x = 1`, CodeUnsupportedStatement, "UPDATE", "UPDATE"},
	{"empty", ``, CodeEmpty, "", ""},
	{"only whitespace and comments", " -- nothing\n /* x */ ", CodeEmpty, "", "$END"},
	{"punctuation first", `{[a].[b]}`, CodeSyntax, "", "{"},
	{"select without from", `SELECT {[a].[b]} ON COLUMNS`, CodeNoFrom, "", "$END"},
	{"select alone", `SELECT`, CodeNoFrom, "", "$END"},
	{"select where without from", `SELECT {[a].[b]} ON COLUMNS WHERE ([Measures].[m])`, CodeNoFrom, "", "WHERE"},
	{"unknown function", `SELECT Generate({[a].[b].Members}, {[c].[d]}) ON COLUMNS FROM [x]`, CodeUnknownFunction, "Generate", "Generate"},
	{"unknown function lower", `SELECT {[a].[b]} ON COLUMNS FROM [x] WHERE iif(1, 2, 3)`, CodeUnknownFunction, "iif", "iif"},
	{"unknown function nested", `SELECT Hierarchize(Union({[a].[b]}, {[c].[d]})) ON COLUMNS FROM [x]`, CodeUnknownFunction, "Union", "Union"},
	{"unknown function in filter", `SELECT Filter({[a].[b]}, IsEmpty([Measures].[m])) ON COLUMNS FROM [x]`, CodeUnknownFunction, "IsEmpty", "IsEmpty"},
	{"unknown function in axis", `SELECT {[a].[b]} DIMENSION PROPERTIES X ON COLUMNS, NonEmptyCrossJoin({[a].[b]}, {[c].[d]}) ON ROWS FROM [x]`, CodeUnknownFunction, "NonEmptyCrossJoin", "NonEmptyCrossJoin"},
	{"method call", `SELECT {[a].[b].Lead(1)} ON COLUMNS FROM [x]`, CodeUnknownFunction, "Lead", "Lead"},
	{"member property", `SELECT {[a].[b].CurrentMember} ON COLUMNS FROM [x]`, CodeUnsupportedMember, "CurrentMember", "CurrentMember"},
	{"member property parent", `SELECT {[a].[b].[c].Parent} ON COLUMNS FROM [x]`, CodeUnsupportedMember, "Parent", "Parent"},
	{"after suffix", `SELECT {[a].[b].Members.Item} ON COLUMNS FROM [x]`, CodeUnsupportedMember, "Item", ".Item"},
	{"arithmetic plus", `SELECT {[Measures].[a] + [Measures].[b]} ON COLUMNS FROM [x]`, CodeArithmetic, "+", "+"},
	{"arithmetic minus", `SELECT {[Measures].[a] - [Measures].[b]} ON COLUMNS FROM [x]`, CodeArithmetic, "-", "-"},
	{"arithmetic divide", `SELECT {[Measures].[a] / [Measures].[b]} ON COLUMNS FROM [x]`, CodeArithmetic, "/", "/ "},
	{"arithmetic times measures", `SELECT {[Measures].[a] * [Measures].[b]} ON COLUMNS FROM [x]`, CodeArithmetic, "*", "*"},
	{"arithmetic times number", `SELECT {[Measures].[a] * 2} ON COLUMNS FROM [x]`, CodeArithmetic, "*", "*"},
	{"arithmetic in where", `SELECT {[a].[b]} ON COLUMNS FROM [x] WHERE [Measures].[a] + 1`, CodeArithmetic, "+", "+"},
	{"arithmetic in argument", `SELECT Head({[a].[b]}, 1 + 2) ON COLUMNS FROM [x]`, CodeArithmetic, "+", "+"},
	{"arithmetic in filter", `SELECT Filter({[a].[b]}, [Measures].[m] + 1 > 5) ON COLUMNS FROM [x]`, CodeArithmetic, "+", "+"},
	{"arithmetic right of filter", `SELECT Filter({[a].[b]}, [Measures].[m] > 5 * 2) ON COLUMNS FROM [x]`, CodeArithmetic, "*", "*"},
	{"range", `SELECT {[a].[b].&[1]:[a].[b].&[5]} ON COLUMNS FROM [x]`, CodeRange, ":", ":"},
	{"string literal in set", `SELECT {"abc"} ON COLUMNS FROM [x]`, CodeUnexpectedLiteral, "", `"abc"`},
	{"number in set", `SELECT {5} ON COLUMNS FROM [x]`, CodeUnexpectedLiteral, "5", "5"},
	{"number as axis", `SELECT 5 ON COLUMNS FROM [x]`, CodeUnexpectedLiteral, "5", "5"},
	{"filter not", `SELECT Filter({[a].[b]}, NOT [Measures].[m] > 1) ON COLUMNS FROM [x]`, CodeFilterCondition, "NOT", "NOT"},
	{"filter non measure", `SELECT Filter({[a].[b]}, [c].[d] > 1) ON COLUMNS FROM [x]`, CodeFilterCondition, "", "[c].[d]"},
	{"filter number first", `SELECT Filter({[a].[b]}, 1 < [Measures].[m]) ON COLUMNS FROM [x]`, CodeFilterCondition, "", "1 <"},
	{"filter measure suffix", `SELECT Filter({[a].[b]}, [Measures].[m].Members > 1) ON COLUMNS FROM [x]`, CodeFilterCondition, "", "[Measures]"},
	{"filter no operator", `SELECT Filter({[a].[b]}, [Measures].[m]) ON COLUMNS FROM [x]`, CodeFilterCondition, "", ")"},
	{"filter measure on right", `SELECT Filter({[a].[b]}, [Measures].[m] > [Measures].[n]) ON COLUMNS FROM [x]`, CodeFilterCondition, "", "[Measures].[n]"},
	{"filter string on right", `SELECT Filter({[a].[b]}, [Measures].[m] = "x") ON COLUMNS FROM [x]`, CodeFilterCondition, "", `"x"`},
	{"filter dangling and", `SELECT Filter({[a].[b]}, [Measures].[m] > 1 AND) ON COLUMNS FROM [x]`, CodeFilterCondition, "", ")"},
	{"arity too few", `SELECT Head() ON COLUMNS FROM [x]`, CodeBadArguments, "Head", "Head"},
	{"arity too many", `SELECT Distinct({[a].[b]}, {[c].[d]}) ON COLUMNS FROM [x]`, CodeBadArguments, "Distinct", "Distinct"},
	{"crossjoin one arg", `SELECT CrossJoin({[a].[b]}) ON COLUMNS FROM [x]`, CodeBadArguments, "CrossJoin", "CrossJoin"},
	{"head n not number", `SELECT Head({[a].[b]}, [Measures].[m]) ON COLUMNS FROM [x]`, CodeBadArguments, "Head", "[Measures]"},
	{"head flag as n", `SELECT Head({[a].[b]}, ASC) ON COLUMNS FROM [x]`, CodeBadArguments, "Head", "ASC"},
	{"set argument is a number", `SELECT Distinct(5) ON COLUMNS FROM [x]`, CodeBadArguments, "Distinct", "5"},
	{"hierarchize bad flag", `SELECT Hierarchize({[a].[b]}, ASC) ON COLUMNS FROM [x]`, CodeBadArguments, "Hierarchize", "ASC"},
	{"hierarchize flag is a member", `SELECT Hierarchize({[a].[b]}, [c].[d]) ON COLUMNS FROM [x]`, CodeBadArguments, "Hierarchize", "[c].[d]"},
	{"order bad flag", `SELECT Order({[a].[b]}, [Measures].[m], POST) ON COLUMNS FROM [x]`, CodeBadArguments, "Order", "POST"},
	{"order expression not a ref", `SELECT Order({[a].[b]}, 5) ON COLUMNS FROM [x]`, CodeBadArguments, "Order", "5"},
	{"topcount n missing", `SELECT TopCount({[a].[b]}) ON COLUMNS FROM [x]`, CodeBadArguments, "TopCount", "TopCount"},
	{"topcount third not measure", `SELECT TopCount({[a].[b]}, 3, 4) ON COLUMNS FROM [x]`, CodeBadArguments, "TopCount", "4"},
	{"drilldownlevel five args", `SELECT DrilldownLevel({[a].[b]},,,,) ON COLUMNS FROM [x]`, CodeSyntax, "", ")"},
	{"drilldownlevel bad third", `SELECT DrilldownLevel({[a].[b]}, [c].[d], [e].[f]) ON COLUMNS FROM [x]`, CodeBadArguments, "DrilldownLevel", "[e].[f]"},
	{"drilldownlevel flag twice", `SELECT DrilldownLevel({[a].[b]}, INCLUDE_CALC_MEMBERS, , INCLUDE_CALC_MEMBERS) ON COLUMNS FROM [x]`, CodeBadArguments, "DrilldownLevel", "INCLUDE_CALC_MEMBERS)"},
	{"drilldownmember one arg", `SELECT DrilldownMember({[a].[b]}) ON COLUMNS FROM [x]`, CodeBadArguments, "DrilldownMember", "DrilldownMember"},
	{"drilldownmember bad flag", `SELECT DrilldownMember({[a].[b]}, {[c].[d]}, ASC) ON COLUMNS FROM [x]`, CodeBadArguments, "DrilldownMember", "ASC"},
	{"drilldownmember repeated flag", `SELECT DrilldownMember({[a].[b]}, {[c].[d]}, RECURSIVE, RECURSIVE) ON COLUMNS FROM [x]`, CodeBadArguments, "DrilldownMember", "RECURSIVE)"},
	{"except bad flag", `SELECT Except({[a].[b]}, {[c].[d]}, POST) ON COLUMNS FROM [x]`, CodeBadArguments, "Except", "POST"},
	{"filter one arg", `SELECT Filter({[a].[b]}) ON COLUMNS FROM [x]`, CodeBadArguments, "Filter", "Filter"},
	{"first argument empty", `SELECT DrilldownLevel(,{[a].[b]}) ON COLUMNS FROM [x]`, CodeSyntax, "", ","},
	{"trailing empty argument", `SELECT Hierarchize({[a].[b]},) ON COLUMNS FROM [x]`, CodeSyntax, "", ")"},
	{"extra comma in set", `SELECT {[a].[b],} ON COLUMNS FROM [x]`, CodeSyntax, "", "}"},
	{"double comma in set", `SELECT {[a].[b],,[c].[d]} ON COLUMNS FROM [x]`, CodeSyntax, "", ",[c]"},
	{"extra comma between axes", `SELECT {[a].[b]} ON COLUMNS,, {[c].[d]} ON ROWS FROM [x]`, CodeSyntax, "", ", {[c]"},
	{"trailing comma after axes", `SELECT {[a].[b]} ON COLUMNS, FROM [x]`, CodeSyntax, "", "FROM"},
	{"unclosed brace", `SELECT {[a].[b] ON COLUMNS FROM [x]`, CodeSyntax, "", "ON COLUMNS"},
	{"unclosed paren", `SELECT Hierarchize({[a].[b]} ON COLUMNS FROM [x]`, CodeSyntax, "", "ON COLUMNS"},
	{"unclosed paren at end", `SELECT {[a].[b]} ON COLUMNS FROM [x] WHERE ([Measures].[m]`, CodeSyntax, "", "$END"},
	{"unclosed subselect", `SELECT {[a].[b]} ON COLUMNS FROM (SELECT {[c].[d]} ON COLUMNS FROM [x]`, CodeSyntax, "", "$END"},
	{"unclosed bracket", `SELECT {[a].[b} ON COLUMNS FROM [x]`, CodeSyntax, "", "$END"},
	{"empty bracket", `SELECT {[]} ON COLUMNS FROM [x]`, CodeSyntax, "", "[]"},
	{"unterminated comment", `SELECT {[a].[b]} /* never closed ON COLUMNS FROM [x]`, CodeSyntax, "", "/*"},
	{"unterminated string", `SELECT {[a].[b]} ON COLUMNS FROM [x] WHERE "abc`, CodeSyntax, "", `"abc`},
	{"trailing text", `SELECT {[a].[b]} ON COLUMNS FROM [x] garbage`, CodeSyntax, "", "garbage"},
	{"second statement", `SELECT {[a].[b]} ON COLUMNS FROM [x]; SELECT {[a].[b]} ON COLUMNS FROM [x]`, CodeSyntax, "", ";"},
	{"stray close", `SELECT {[a].[b]} ON COLUMNS FROM [x])`, CodeSyntax, "", ")"},
	{"cell properties in subselect", `SELECT {[a]} ON COLUMNS FROM (SELECT {[b]} ON COLUMNS FROM [x] CELL PROPERTIES VALUE)`, CodeSyntax, "", "CELL"},
	{"missing ON", `SELECT {[a].[b]} COLUMNS FROM [x]`, CodeSyntax, "", "COLUMNS"},
	{"missing axis name", `SELECT {[a].[b]} ON FROM [x]`, CodeSyntax, "", "FROM"},
	{"bad axis name", `SELECT {[a].[b]} ON SIDEWAYS FROM [x]`, CodeSyntax, "", "SIDEWAYS"},
	{"axis number too big", `SELECT {[a].[b]} ON 99999 FROM [x]`, CodeSyntax, "", "99999"},
	{"axis number decimal", `SELECT {[a].[b]} ON 1.5 FROM [x]`, CodeSyntax, "", "1.5"},
	{"missing cube", `SELECT {[a].[b]} ON COLUMNS FROM`, CodeSyntax, "", "$END"},
	{"from a number", `SELECT {[a].[b]} ON COLUMNS FROM 5`, CodeSyntax, "", "5"},
	{"duplicate axis", `SELECT {[a].[b]} ON COLUMNS, {[c].[d]} ON 0 FROM [x]`, CodeAxisDuplicate, "COLUMNS", "0 FROM"},
	{"bad key", `SELECT {[a].&abc} ON COLUMNS FROM [x]`, CodeSyntax, "", "abc"},
	{"key first", `SELECT {&[a]} ON COLUMNS FROM [x]`, CodeSyntax, "", "&"},
	{"dot then nothing", `SELECT {[a].} ON COLUMNS FROM [x]`, CodeSyntax, "", "}"},
	{"dot then number", `SELECT {[a].5} ON COLUMNS FROM [x]`, CodeSyntax, "", "5"},
	{"bad character", `SELECT {[a].[b]} ON COLUMNS FROM [x] WHERE @x`, CodeSyntax, "", "@"},
	{"bad number", `SELECT Head({[a].[b]}, 3x) ON COLUMNS FROM [x]`, CodeSyntax, "", "3x"},
	{"number out of range", `SELECT Head({[a].[b]}, 1e999) ON COLUMNS FROM [x]`, CodeSyntax, "", "1e999"},
	{"invalid utf8", "SELECT {[a].[b]} ON COLUMNS FROM [x] \xff", CodeSyntax, "", "\xff"},
	{"properties without names", `SELECT {[a].[b]} DIMENSION PROPERTIES ON COLUMNS FROM [x]`, CodeSyntax, "", "COLUMNS"},
	{"cell properties without names", `SELECT {[a].[b]} ON COLUMNS FROM [x] CELL PROPERTIES`, CodeSyntax, "", "$END"},
	{"cell without properties", `SELECT {[a].[b]} ON COLUMNS FROM [x] CELL`, CodeSyntax, "", "$END"},
	{"empty parens", `SELECT () ON COLUMNS FROM [x]`, CodeSyntax, "", ")"},
	{"line and column", "SELECT {[a].[b]} ON COLUMNS\nFROM [x]\nWHERE Nope(1)", CodeUnknownFunction, "Nope", "Nope"},
}

func TestParseRefusals(t *testing.T) {
	known := map[string]bool{}
	for _, c := range AllCodes {
		known[c] = true
	}
	for _, tc := range refusalCases {
		t.Run(tc.name, func(t *testing.T) {
			st, err := Parse(tc.src)
			if err == nil {
				t.Fatalf("parsed, want %s; printed: %s", tc.code, st)
			}
			e, ok := err.(*Error)
			if !ok {
				t.Fatalf("error is %T, want *Error", err)
			}
			if !known[e.Code] {
				t.Errorf("code %q is not in AllCodes", e.Code)
			}
			if e.Code != tc.code {
				t.Errorf("code = %q, want %q (%s)", e.Code, tc.code, e.Msg)
			}
			if e.Name != tc.errName {
				t.Errorf("name = %q, want %q", e.Name, tc.errName)
			}
			want := 0
			if tc.at == "$END" {
				want = len(tc.src)
			} else if tc.at != "" {
				want = strings.Index(tc.src, tc.at)
				if want < 0 {
					t.Fatalf("test bug: %q not in the statement", tc.at)
				}
			}
			if e.Offset != want {
				t.Errorf("offset = %d, want %d (%s)", e.Offset, want, e.Msg)
			}
			if e.Msg == "" || e.Error() == "" {
				t.Error("empty message")
			}
			if e.Line < 1 || e.Column < 1 {
				t.Errorf("line/column %d/%d", e.Line, e.Column)
			}
		})
	}
}

func TestErrorPosition(t *testing.T) {
	_, err := Parse("SELECT {[a].[b]} ON COLUMNS\nFROM [x]\nWHERE   Nope(1)")
	e := err.(*Error)
	if e.Line != 3 || e.Column != 9 {
		t.Errorf("line/column = %d/%d, want 3/9", e.Line, e.Column)
	}
	if !strings.Contains(e.Error(), "Nope") || !strings.Contains(e.Error(), "line 3, column 9") {
		t.Errorf("message: %s", e.Error())
	}
	// Columns count characters, not bytes.
	_, err = Parse("SELECT {[città].[à]} ON COLUMNS FROM [x] Nope")
	e = err.(*Error)
	if e.Column != strings.Index("SELECT {[città].[à]} ON COLUMNS FROM [x] Nope", "Nope")-2+1 {
		t.Errorf("column %d", e.Column)
	}
}

func TestLimits(t *testing.T) {
	good := func(depth int) string {
		return "SELECT " + strings.Repeat("{", depth) + "[a]" + strings.Repeat("}", depth) + " ON COLUMNS FROM [x]"
	}
	if _, err := Parse(good(MaxDepth)); err != nil {
		t.Errorf("nesting at the limit: %v", err)
	}
	_, err := Parse(good(MaxDepth + 1))
	if e, ok := err.(*Error); !ok || e.Code != CodeTooDeep {
		t.Errorf("nesting past the limit: %v", err)
	}
	// Function calls and subselects count too.
	deepCall := "SELECT " + strings.Repeat("Distinct(", 200) + "{[a]}" + strings.Repeat(")", 200) + " ON COLUMNS FROM [x]"
	if _, err := Parse(deepCall); err == nil || err.(*Error).Code != CodeTooDeep {
		t.Errorf("deep calls: %v", err)
	}
	deepSub := "SELECT {[a]} ON COLUMNS FROM " + strings.Repeat("(SELECT {[a]} ON COLUMNS FROM ", 200) + "[x]" + strings.Repeat(")", 200)
	if _, err := Parse(deepSub); err == nil || err.(*Error).Code != CodeTooDeep {
		t.Errorf("deep subselects: %v", err)
	}
	deepCond := "SELECT Filter({[a]}, " + strings.Repeat("(", 200) + "[Measures].[m] > 1" + strings.Repeat(")", 200) + ") ON COLUMNS FROM [x]"
	if _, err := Parse(deepCond); err == nil || err.(*Error).Code != CodeTooDeep {
		t.Errorf("deep conditions: %v", err)
	}
	// A long flat chain is not nesting.
	chain := "SELECT " + strings.Repeat("{[a]} * ", 5000) + "{[a]} ON COLUMNS FROM [x]"
	if st, err := Parse(chain); err != nil {
		t.Errorf("long star chain: %v", err)
	} else if n := len(st.Axes[0].Set.(*Call).Args); n != 5001 {
		t.Errorf("chain args = %d", n)
	}

	big := "SELECT {[a]} ON COLUMNS FROM [x] -- " + strings.Repeat("x", MaxStatementBytes)
	_, err = Parse(big)
	if e, ok := err.(*Error); !ok || e.Code != CodeTooLong {
		t.Errorf("long text: %v", err)
	}
	atLimit := "SELECT {[a]} ON COLUMNS FROM [x] --"
	atLimit += strings.Repeat("x", MaxStatementBytes-len(atLimit))
	if _, err := Parse(atLimit); err != nil {
		t.Errorf("text at the limit: %v", err)
	}
}

func TestLexicalDetails(t *testing.T) {
	t.Run("quoted strings are lexed with doubling", func(t *testing.T) {
		for src, want := range map[string]string{`"a""b"`: `a"b`, `'a''b'`: `a'b`, `"it's"`: `it's`, `''`: ``} {
			l := &lexer{src: src}
			tok := l.next()
			if tok.kind != tString || tok.text != want || tok.end != len(src) {
				t.Errorf("%s: %+v", src, tok)
			}
		}
	})
	t.Run("brackets", func(t *testing.T) {
		for src, want := range map[string]string{`[a]]b]`: "a]b", `[a]]]`: "a]", "[a\nb]": "a\nb", `[ x ]`: " x "} {
			l := &lexer{src: src}
			tok := l.next()
			if tok.kind != tBracket || tok.text != want || tok.end != len(src) {
				t.Errorf("%q: %+v", src, tok)
			}
		}
	})
	t.Run("operators", func(t *testing.T) {
		l := &lexer{src: `<= >= <> < > = & ;`}
		var got []string
		for tok := l.next(); tok.kind != tEOF; tok = l.next() {
			got = append(got, tok.text)
		}
		if strings.Join(got, "|") != `<=|>=|<>|<|>|=|&|;` {
			t.Errorf("%v", got)
		}
	})
	t.Run("a refused statement is named before a later lexical error", func(t *testing.T) {
		_, err := Parse(`WITH MEMBER [m] AS "unterminated`)
		if e := err.(*Error); e.Code != CodeWithMember {
			t.Errorf("code %s", e.Code)
		}
	})
}

func TestPrintEscapesAndKeys(t *testing.T) {
	src := `SELECT {[a]]b].[c d].&[k]]1]&[2], Measures.x} ON COLUMNS FROM [x]]y]`
	got := roundTrip(t, src)
	if got != src {
		t.Errorf("got %s", got)
	}
	if s := ExprString(mustParse(t, src).Axes[0].Set); !strings.HasPrefix(s, "{[a]]b]") {
		t.Errorf("ExprString: %s", s)
	}
}
