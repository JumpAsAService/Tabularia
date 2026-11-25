package xmla

import (
	"context"
	"errors"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// MDSCHEMA_MEMBERS. The members of a level are read from the data, in order, and written as they
// are read; one that would be past the cap ends the answer with a fault (a client has no way to
// page a Discover, and a list cut short would be taken for the whole one).

// the TREE_OP bits of the standard
const (
	treeChildren    = 1
	treeSiblings    = 2
	treeParent      = 4
	treeSelf        = 8
	treeDescendants = 16
	treeAncestors   = 32
)

// the MEMBER_TYPE of the standard
const (
	memberRegular = 1
	memberAll     = 2
	memberMeasure = 3
)

type memberWriter struct {
	rw   *RowsetWriter
	r    restr
	cat  *Catalog
	cube *xmlamodel.Cube
	max  int
	seen map[string]bool // by cube and unique name: a member is written once
	n    int
}

func members(ctx context.Context, rw *RowsetWriter, r restr, cat *Catalog, data Data, lim Limits) error {
	if !r.catalog(cat) || r.given("SCHEMA_NAME") || !r.flag("CUBE_SOURCE", 1) {
		return nil
	}
	op := treeSelf
	if r.given("TREE_OP") {
		op = 0
		for _, v := range r["TREE_OP"] {
			n, err := strconv.Atoi(strings.TrimSpace(v))
			if err != nil || n < 0 || n > 63 {
				return NewFault(CodeBadRequest, "TREE_OP is a number made of the bits 1 (children), 2 (siblings), 4 (parent), 8 (self), 16 (descendants) and 32 (ancestors)")
			}
			op |= n
		}
	}
	mw := &memberWriter{rw: rw, r: r, cat: cat, max: lim.MaxMembers, seen: map[string]bool{}}
	for _, c := range cat.Cubes {
		if !r.is("CUBE_NAME", c.Name) {
			continue
		}
		mw.cube = c
		var err error
		if r.given("MEMBER_UNIQUE_NAME") {
			err = mw.related(ctx, data, op)
		} else {
			err = mw.levels(ctx, data)
		}
		if errors.Is(err, errMembersCap) {
			return limitFault(lim.MaxMembers)
		}
		if err != nil {
			return asDataFault(ctx, err)
		}
	}
	return nil
}

// asDataFault is the fault of a failed read of the data: the caller's own faults pass.
func asDataFault(ctx context.Context, err error) error {
	var f *Fault
	if errors.As(err, &f) {
		return err
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return NewFault(CodeLimit, "The query took longer than the limit of seconds for a statement")
	}
	if ctx.Err() != nil {
		return NewFault(CodeInternal, "The request was cancelled")
	}
	return NewFault(CodeInternal, "The members could not be read: try again")
}

// levels writes the members of the levels the restrictions name, no member being asked for.
func (mw *memberWriter) levels(ctx context.Context, data Data) error {
	r := mw.r
	if r.is("DIMENSION_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) && r.is("HIERARCHY_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) &&
		(!r.given("LEVEL_UNIQUE_NAME") || r.is("LEVEL_UNIQUE_NAME", measuresLevelUnique) || r.is("LEVEL_UNIQUE_NAME", measuresLevelUnique3())) &&
		r.number("LEVEL_NUMBER", 0) {
		for _, m := range mw.cube.Measures {
			if err := mw.measure(m); err != nil {
				return err
			}
		}
	}
	for _, d := range mw.cube.Dimensions {
		if !r.is("DIMENSION_UNIQUE_NAME", d.UniqueName) {
			continue
		}
		for _, h := range d.Hierarchies {
			if !r.is("HIERARCHY_UNIQUE_NAME", h.UniqueName) {
				continue
			}
			for _, l := range h.Levels {
				if !r.is("LEVEL_UNIQUE_NAME", l.UniqueName) || !r.number("LEVEL_NUMBER", l.Number) {
					continue
				}
				if l.Part == xmlamodel.PartAll {
					if err := mw.one(ctx, data, h.All()); err != nil {
						return err
					}
					continue
				}
				if err := mw.level(ctx, data, l, nil); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// related writes the members that the restricted unique names and the TREE_OP ask for.
func (mw *memberWriter) related(ctx context.Context, data Data, op int) error {
	for _, name := range mw.r["MEMBER_UNIQUE_NAME"] {
		ref, err := mw.cube.ResolveName(name)
		if err != nil {
			continue // a name of another cube, or of nothing: no member
		}
		switch ref.Kind {
		case xmlamodel.RefMeasure:
			if op&treeSelf != 0 {
				if err := mw.measure(ref.Measure); err != nil {
					return err
				}
			}
			if op&treeSiblings != 0 {
				for _, m := range mw.cube.Measures {
					if m != ref.Measure {
						if err := mw.measure(m); err != nil {
							return err
						}
					}
				}
			}
		case xmlamodel.RefMember:
			if err := mw.tree(ctx, data, ref.Member, op); err != nil {
				return err
			}
		}
	}
	return nil
}

// tree writes the members of the relations of op to m, if the data has m.
func (mw *memberWriter) tree(ctx context.Context, data Data, m xmlamodel.Member, op int) error {
	if _, found, err := data.Member(ctx, mw.cube, m); err != nil || !found {
		return err
	}
	if op&treeAncestors != 0 {
		for _, a := range m.Ancestors() {
			if err := mw.one(ctx, data, a); err != nil {
				return err
			}
		}
	} else if op&treeParent != 0 {
		if p, ok := m.Parent(); ok {
			if err := mw.one(ctx, data, p); err != nil {
				return err
			}
		}
	}
	if op&treeSelf != 0 {
		if err := mw.one(ctx, data, m); err != nil {
			return err
		}
	}
	if op&treeSiblings != 0 && m.Kind != xmlamodel.MemberAll {
		p, _ := m.Parent()
		var under *xmlamodel.Member
		if p.Kind != xmlamodel.MemberAll {
			under = &p
		}
		err := mw.level(ctx, data, m.Level, under, m.UniqueName())
		if err != nil {
			return err
		}
	}
	h := m.Hierarchy()
	if m.Kind == xmlamodel.MemberNull {
		return nil // a null has no children
	}
	var under *xmlamodel.Member
	if m.Kind != xmlamodel.MemberAll {
		under = &m
	}
	first := m.Level.Number + 1
	last := first
	if op&treeDescendants != 0 {
		last = len(h.Levels) - 1
	} else if op&treeChildren == 0 {
		return nil
	}
	for n := first; n <= last && n < len(h.Levels); n++ {
		if err := mw.level(ctx, data, h.Levels[n], under); err != nil {
			return err
		}
	}
	return nil
}

// level writes the members of a level (below a member if given), but for those named in `except`.
func (mw *memberWriter) level(ctx context.Context, data Data, l *xmlamodel.Level, under *xmlamodel.Member, except ...string) error {
	return data.Members(ctx, mw.cube, l, under, func(m xmlamodel.Member, children int) error {
		for _, e := range except {
			if m.UniqueName() == e {
				return nil
			}
		}
		return mw.emit(m, children)
	})
}

// one writes a member the data has, looking its children up.
func (mw *memberWriter) one(ctx context.Context, data Data, m xmlamodel.Member) error {
	children, found, err := data.Member(ctx, mw.cube, m)
	if err != nil || !found {
		return err
	}
	return mw.emit(m, children)
}

// emit writes a member that meets the restrictions, once.
func (mw *memberWriter) emit(m xmlamodel.Member, children int) error {
	r, h := mw.r, m.Hierarchy()
	typ := memberRegular
	if m.Kind == xmlamodel.MemberAll {
		typ = memberAll
	}
	un := m.UniqueName()
	if !r.is("DIMENSION_UNIQUE_NAME", h.Dimension.UniqueName) || !r.is("HIERARCHY_UNIQUE_NAME", h.UniqueName) ||
		!r.is("LEVEL_UNIQUE_NAME", m.Level.UniqueName) || !r.number("LEVEL_NUMBER", m.Level.Number) ||
		!r.is("MEMBER_CAPTION", m.Caption()) || !r.is("MEMBER_NAME", m.Caption()) || !r.number("MEMBER_TYPE", typ) {
		return nil
	}
	if !mw.take(un) {
		return nil
	}
	if mw.n > mw.max {
		return errMembersCap
	}
	v := map[string]string{"CATALOG_NAME": mw.cat.Name, "CUBE_NAME": mw.cube.Name, "DIMENSION_UNIQUE_NAME": h.Dimension.UniqueName,
		"HIERARCHY_UNIQUE_NAME": h.UniqueName, "LEVEL_UNIQUE_NAME": m.Level.UniqueName, "LEVEL_NUMBER": strconv.Itoa(m.Level.Number),
		"MEMBER_ORDINAL": strconv.Itoa(mw.n - 1), "MEMBER_NAME": m.Caption(), "MEMBER_UNIQUE_NAME": un, "MEMBER_TYPE": strconv.Itoa(typ),
		"MEMBER_CAPTION": m.Caption(), "CHILDREN_CARDINALITY": strconv.Itoa(children), "PARENT_COUNT": "0", "MEMBER_KEY": m.Key}
	if m.Kind == xmlamodel.MemberAll {
		v["MEMBER_KEY"] = xmlamodel.AllMemberName
	}
	if p, ok := m.Parent(); ok {
		v["PARENT_UNIQUE_NAME"], v["PARENT_LEVEL"], v["PARENT_COUNT"] = p.UniqueName(), strconv.Itoa(p.Level.Number), "1"
	}
	put(mw.rw, v)
	return nil
}

// take counts a member, false when it was written already.
func (mw *memberWriter) take(unique string) bool {
	k := mw.cube.Name + "\x00" + unique
	if mw.seen[k] {
		return false
	}
	mw.seen[k] = true
	mw.n++
	return true
}

// measure writes a measure as a member of the measures' hierarchy.
func (mw *memberWriter) measure(m *xmlamodel.Measure) error {
	r := mw.r
	if !r.is("MEMBER_CAPTION", m.Name) || !r.is("MEMBER_NAME", m.Name) || !r.number("MEMBER_TYPE", memberMeasure) {
		return nil
	}
	if !mw.take(m.UniqueName) {
		return nil
	}
	if mw.n > mw.max {
		return errMembersCap
	}
	put(mw.rw, map[string]string{"CATALOG_NAME": mw.cat.Name, "CUBE_NAME": mw.cube.Name, "DIMENSION_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName),
		"HIERARCHY_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName), "LEVEL_UNIQUE_NAME": measuresLevelUnique, "LEVEL_NUMBER": "0",
		"MEMBER_ORDINAL": strconv.Itoa(mw.n - 1), "MEMBER_NAME": m.Name, "MEMBER_UNIQUE_NAME": m.UniqueName, "MEMBER_TYPE": strconv.Itoa(memberMeasure),
		"MEMBER_CAPTION": m.Name, "CHILDREN_CARDINALITY": "0", "PARENT_COUNT": "0", "DESCRIPTION": m.Description, "MEMBER_KEY": m.Name})
	return nil
}
