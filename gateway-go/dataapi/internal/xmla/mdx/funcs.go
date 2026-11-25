package mdx

import (
	"strconv"
	"strings"
)

// functionNames maps the lower-case name to its canonical spelling: the whole
// list of functions the subset understands.
var functionNames = func() map[string]string {
	m := map[string]string{}
	for _, n := range []string{
		"CrossJoin", "Hierarchize", "DrilldownLevel", "DrilldownMember",
		"AddCalculatedMembers", "Except", "Distinct", "Head", "TopCount",
		"BottomCount", "Filter", "Order",
	} {
		m[strings.ToLower(n)] = n
	}
	return m
}()

// validateCall checks the count and kind of the arguments of one function.
// starts[i] is the offset of argument i, off the offset of the name.
func (p *parser) validateCall(c *Call, off int, starts []int) {
	n := len(c.Args)
	arity := func(min, max int) {
		if n < min || (max >= 0 && n > max) {
			want := ""
			switch {
			case max < 0:
				want = "at least " + itoa(min)
			case min == max:
				want = itoa(min)
			default:
				want = itoa(min) + " to " + itoa(max)
			}
			p.fail(CodeBadArguments, c.Name, off, "%s takes %s arguments, found %d", c.Name, want, n)
		}
	}
	bad := func(i int, what string) {
		p.fail(CodeBadArguments, c.Name, starts[i], "argument %d of %s must be %s", i+1, c.Name, what)
	}
	set := func(i int) {
		switch c.Args[i].(type) {
		case *SetLit, *Paren, *Ref, *Call:
		default:
			bad(i, "a set or member")
		}
	}
	number := func(i int) {
		if _, ok := c.Args[i].(*Number); !ok {
			bad(i, "a number")
		}
	}
	plainRef := func(i int, what string) {
		if r, ok := c.Args[i].(*Ref); !ok || r.Suffix != "" {
			bad(i, what)
		}
	}
	empty := func(i int) bool { _, ok := c.Args[i].(*Empty); return ok }
	flag := func(i int, allowed ...string) bool {
		f, ok := c.Args[i].(*Flag)
		if !ok {
			return false
		}
		for _, a := range allowed {
			if f.Name == a {
				return true
			}
		}
		bad(i, "one of "+strings.Join(allowed, ", "))
		return false
	}

	switch c.Name {
	case "CrossJoin":
		arity(2, -1)
		for i := range c.Args {
			set(i)
		}
	case "Hierarchize":
		arity(1, 2)
		set(0)
		if n == 2 && !flag(1, "POST", "PRE") {
			bad(1, "POST or PRE")
		}
	case "DrilldownLevel":
		arity(1, 4)
		set(0)
		seenFlag := false
		for i := 1; i < n; i++ {
			switch {
			case empty(i):
			case flag(i, "INCLUDE_CALC_MEMBERS") && !seenFlag:
				seenFlag = true
			case i == 1:
				plainRef(i, "a level, INCLUDE_CALC_MEMBERS, or left empty")
			case i == 2:
				number(i)
			default:
				bad(i, "INCLUDE_CALC_MEMBERS or left empty")
			}
		}
	case "DrilldownMember":
		arity(2, 5)
		set(0)
		set(1)
		seen := map[string]bool{}
		for i := 2; i < n; i++ {
			if empty(i) {
				continue
			}
			if !flag(i, "RECURSIVE", "INCLUDE_CALC_MEMBERS") {
				bad(i, "RECURSIVE, INCLUDE_CALC_MEMBERS, or left empty")
			}
			name := c.Args[i].(*Flag).Name
			if seen[name] {
				bad(i, "a flag written once")
			}
			seen[name] = true
		}
	case "AddCalculatedMembers", "Distinct":
		arity(1, 1)
		set(0)
	case "Except":
		arity(2, 3)
		set(0)
		set(1)
		if n == 3 && !flag(2, "ALL") {
			bad(2, "ALL")
		}
	case "Head":
		arity(1, 2)
		set(0)
		if n == 2 {
			number(1)
		}
	case "TopCount", "BottomCount":
		arity(2, 3)
		set(0)
		number(1)
		if n == 3 {
			plainRef(2, "a measure")
		}
	case "Filter":
		arity(2, 2)
		set(0)
	case "Order":
		arity(2, 3)
		set(0)
		plainRef(1, "a measure")
		if n == 3 && !flag(2, "ASC", "DESC", "BASC", "BDESC") {
			bad(2, "ASC, DESC, BASC or BDESC")
		}
	}
}

func itoa(i int) string { return strconv.Itoa(i) }
