package bayesexpr

import (
	"math"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the static interval of a value (docs/specs/bayes-expression-language.md,
// «Intervals»), for model-graph's warnings, never for a refusal. The arithmetic is IEEE double,
// rounded to nearest, every step in the order written here, so that the TypeScript mirror, doing
// the same steps, gives the same endpoints:
//
//   - a literal v is [v, v]; a node its declared interval (unbounded when it has none); a function
//     the range of language.functions; an indexing and a lag the interval of their node;
//   - -a = [-a.hi, -a.lo], the ends' openness swapped;
//   - a + b = [a.lo + b.lo, a.hi + b.hi], an end open when either is; a - b = a + (-b);
//   - a * b: the four corners in the order lo·lo, lo·hi, hi·lo, hi·hi, a corner with a zero factor
//     being 0 (so 0 · ∞ is 0); lo the first smallest by <, hi the first largest by >; an end closed
//     when some corner giving its value is attained (both factors closed, or a closed zero factor);
//   - a / b: unbounded when b contains 0, otherwise a * (1 / b), with 1 / b = [1 / b.hi, 1 / b.lo]
//     (openness swapped), 1 / 0 being -∞ at the upper end and +∞ at the lower end;
//   - a ^ b: (0, ∞) when a is inside (0, ∞), unbounded otherwise;
//   - after every step: a NaN end, a lower end at +∞ or an upper end at -∞ (an overflow) makes the
//     interval unbounded; an infinite end is open; -0 is 0.

// Interval is a set of reals: Lo may be -∞, Hi +∞ (an infinite end is open).
type Interval struct {
	Lo, Hi         float64
	LoOpen, HiOpen bool
}

// Unbounded is the real line.
func Unbounded() Interval {
	return Interval{Lo: math.Inf(-1), Hi: math.Inf(1), LoOpen: true, HiOpen: true}
}

// Point is [v, v].
func Point(v float64) Interval { return tidy(Interval{Lo: v, Hi: v}) }

// FromCatalog is a catalogue interval (a missing bound is infinite).
func FromCatalog(c bayescatalog.Interval) Interval {
	i := Unbounded()
	if c.Lower != nil {
		i.Lo, i.LoOpen = *c.Lower, c.LowerOpen
	}
	if c.Upper != nil {
		i.Hi, i.HiOpen = *c.Upper, c.UpperOpen
	}
	return tidy(i)
}

// Catalog is the interval in the catalogue's form, the corpus's and the JSON's: an infinite end
// is left out.
func (i Interval) Catalog() bayescatalog.Interval {
	var c bayescatalog.Interval
	if !math.IsInf(i.Lo, 0) {
		lo := i.Lo
		c.Lower, c.LowerOpen = &lo, i.LoOpen
	}
	if !math.IsInf(i.Hi, 0) {
		hi := i.Hi
		c.Upper, c.UpperOpen = &hi, i.HiOpen
	}
	return c
}

// tidy is the step after every operation.
func tidy(i Interval) Interval {
	if math.IsNaN(i.Lo) || math.IsNaN(i.Hi) || math.IsInf(i.Lo, 1) || math.IsInf(i.Hi, -1) {
		return Unbounded()
	}
	if math.IsInf(i.Lo, -1) {
		i.LoOpen = true
	}
	if math.IsInf(i.Hi, 1) {
		i.HiOpen = true
	}
	if i.Lo == 0 {
		i.Lo = 0
	}
	if i.Hi == 0 {
		i.Hi = 0
	}
	return i
}

func negInterval(a Interval) Interval {
	return tidy(Interval{Lo: -a.Hi, Hi: -a.Lo, LoOpen: a.HiOpen, HiOpen: a.LoOpen})
}

func addInterval(a, b Interval) Interval {
	return tidy(Interval{Lo: a.Lo + b.Lo, Hi: a.Hi + b.Hi, LoOpen: a.LoOpen || b.LoOpen, HiOpen: a.HiOpen || b.HiOpen})
}

func subInterval(a, b Interval) Interval { return addInterval(a, negInterval(b)) }

func mulInterval(a, b Interval) Interval {
	type end struct {
		v      float64
		closed bool
	}
	corner := func(x, y end) end {
		if (x.v == 0 && x.closed) || (y.v == 0 && y.closed) {
			return end{0, true}
		}
		if x.v == 0 || y.v == 0 {
			return end{0, false}
		}
		return end{x.v * y.v, x.closed && y.closed}
	}
	alo, ahi := end{a.Lo, !a.LoOpen}, end{a.Hi, !a.HiOpen}
	blo, bhi := end{b.Lo, !b.LoOpen}, end{b.Hi, !b.HiOpen}
	cs := [4]end{corner(alo, blo), corner(alo, bhi), corner(ahi, blo), corner(ahi, bhi)}
	lo, hi := cs[0].v, cs[0].v
	for _, c := range cs[1:] {
		if c.v < lo {
			lo = c.v
		}
		if c.v > hi {
			hi = c.v
		}
	}
	loClosed, hiClosed := false, false
	for _, c := range cs {
		if c.v == lo && c.closed {
			loClosed = true
		}
		if c.v == hi && c.closed {
			hiClosed = true
		}
	}
	return tidy(Interval{Lo: lo, Hi: hi, LoOpen: !loClosed, HiOpen: !hiClosed})
}

// containsZero reports whether 0 is in the interval.
func (i Interval) containsZero() bool {
	return (i.Lo < 0 || (i.Lo == 0 && !i.LoOpen)) && (i.Hi > 0 || (i.Hi == 0 && !i.HiOpen))
}

func divInterval(a, b Interval) Interval {
	if b.containsZero() {
		return Unbounded()
	}
	r := Interval{LoOpen: b.HiOpen, HiOpen: b.LoOpen}
	if b.Hi == 0 {
		r.Lo = math.Inf(-1)
	} else {
		r.Lo = 1 / b.Hi
	}
	if b.Lo == 0 {
		r.Hi = math.Inf(1)
	} else {
		r.Hi = 1 / b.Lo
	}
	return mulInterval(a, tidy(r))
}

var positiveRay = Interval{Lo: 0, Hi: math.Inf(1), LoOpen: true, HiOpen: true}

func powInterval(a Interval) Interval {
	if a.Within(positiveRay) == Inside {
		return positiveRay
	}
	return Unbounded()
}

// Relation is how a value's interval stands to the interval a slot needs.
type Relation uint8

const (
	Inside   Relation = iota + 1 // every value is allowed
	Overlap                      // some values are, some are not: a warning
	Disjoint                     // none is: an error
)

// Within is how i stands to need.
func (i Interval) Within(need Interval) Relation {
	loIn := i.Lo > need.Lo || (i.Lo == need.Lo && (i.LoOpen || !need.LoOpen))
	hiIn := i.Hi < need.Hi || (i.Hi == need.Hi && (i.HiOpen || !need.HiOpen))
	if loIn && hiIn {
		return Inside
	}
	below := i.Hi < need.Lo || (i.Hi == need.Lo && (i.HiOpen || need.LoOpen))
	above := i.Lo > need.Hi || (i.Lo == need.Hi && (i.LoOpen || need.HiOpen))
	if below || above {
		return Disjoint
	}
	return Overlap
}

// catalogIntervals are the catalogue's named intervals.
var catalogIntervals = func() map[string]Interval {
	m := map[string]Interval{}
	for _, i := range bayescatalog.Declared().Intervals {
		m[i.ID] = FromCatalog(i.Interval)
	}
	return m
}()

// NamedInterval is the catalogue's interval with the id (real, positive, unit…), for the slots
// of model-graph; unknown ids are unbounded.
func NamedInterval(id string) Interval {
	if i, ok := catalogIntervals[id]; ok {
		return i
	}
	return Unbounded()
}

// Interval is the static interval of the value.
func (t *Typed) Interval() Interval {
	n := t.Node
	switch n.Kind {
	case KindNum:
		return Point(n.Num)
	case KindName, KindIndex, KindLag:
		if t.Sym != nil && t.Sym.Interval != nil {
			return tidy(*t.Sym.Interval)
		}
		return Unbounded()
	case KindCall:
		return NamedInterval(functions[n.Name].Range)
	case KindNeg:
		return negInterval(t.Args[0].Interval())
	case KindBinary:
		a, b := t.Args[0].Interval(), t.Args[1].Interval()
		switch n.Op {
		case "+":
			return addInterval(a, b)
		case "-":
			return subInterval(a, b)
		case "*":
			return mulInterval(a, b)
		case "/":
			return divInterval(a, b)
		}
		return powInterval(a)
	}
	return Unbounded()
}
