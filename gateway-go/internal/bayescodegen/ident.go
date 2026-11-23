package bayescodegen

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the identifier rule (docs/specs/bayes-stan-codegen.md, «Identifiers», answer 5) and
// the mapping table. An identifier of the program is either a person's name that passes the rule
// (it starts with a lower-case letter) or one generated here from ids (it starts with an upper-case
// letter): the two never meet. Labels never enter the program; they live only in the mapping table.

// Reasons a node's name is not its identifier (the mapping table's renamed).
const (
	RenamedNoName   = "no_name"
	RenamedReserved = "reserved"
)

// refusedTokens are the words the output check refuses as tokens («Safety checks on the output»):
// the statements and blocks the generator never writes. jacobian (jacobian += adds to the log
// density as target += does), while (an unbounded loop), and the other statement words of Stan
// 2.40.0 the generator has no use for (if, else, return, break, continue): the extension point's
// statements are declarations, assignments, a for over a size and calls, and the mode sizes the
// written declarations with the conditional operator, not with if (correction of 2026-11-24 from
// E4, finding 3). for and in are written (the loops).
var refusedTokens = []string{"print", "reject", "fatal_error", "target", "include", "functions", "profile",
	"jacobian", "while", "if", "else", "return", "break", "continue"}

// reservedNames are the words a person's name may not be: Stan's reserved words, type names,
// reserved functions, block names, renamed backend words and the names of the model's C++ class
// (the catalogue's lists; its backend_member_patterns are matched by reservedWordFor), the words
// the output refuses as tokens, and the Stan name of every distribution, density, generator,
// function the catalogue lists (a variable never shadows a library function).
var reservedNames = func() map[string]bool {
	m := map[string]bool{}
	add := func(ws ...string) {
		for _, w := range ws {
			if w != "" {
				m[w] = true
			}
		}
	}
	s := bayescatalog.StanLists()
	add(s.ReservedWords...)
	add(s.TypeNames...)
	add(s.ReservedFunctions...)
	add(s.BlockNames...)
	add(s.RenamedBackendWords...)
	add(s.BackendMemberWords...)
	add(refusedTokens...)
	c := bayescatalog.Declared()
	for _, d := range c.Distributions {
		add(d.ID, d.Stan.Statement, d.Stan.Density, d.Stan.RNG, d.Stan.RNGInverseLink, d.Stan.LCDF, d.Stan.LCCDF)
	}
	for _, f := range c.Language.Functions {
		add(f.Name, f.Stan)
	}
	add(c.InternalFunctions...)
	return m
}()

// reservedWordFor reports the word that keeps a person's name out of the program: the name itself
// when it is a reserved name, the pattern of the model's C++ class it is of (get_*_sizedtypes,
// *_impl), the suffix when it ends with one Stan reserves; "" when it passes.
func reservedWordFor(name string) string {
	if reservedNames[name] {
		return name
	}
	if p := bayescatalog.StanLists().BackendMember(name); p != "" {
		return p
	}
	for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
		if strings.HasSuffix(name, suffix) {
			return suffix
		}
	}
	return ""
}

// personName reports whether a name has the graph's lexical form: [a-z][a-z0-9_]*, at most
// max_name_bytes, not ending in "__". A valid model holds no other; a name outside it is a bug of
// the validator, and no program is written.
func personName(s string) bool {
	if s == "" || len(s) > bayescatalog.GetLimits().MaxNameBytes || strings.HasSuffix(s, "__") {
		return false
	}
	if s[0] < 'a' || s[0] > 'z' {
		return false
	}
	for i := 1; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

// graphID reports whether an id has the graph's form ^[a-z][a-z0-9]{0,15}$: no "_", so that a
// generated identifier reads back to one node (and one component).
func graphID(s string) bool {
	if len(s) == 0 || len(s) > 16 || s[0] < 'a' || s[0] > 'z' {
		return false
	}
	for i := 1; i < len(s); i++ {
		if !(s[i] >= 'a' && s[i] <= 'z' || s[i] >= '0' && s[i] <= '9') {
			return false
		}
	}
	return true
}

// generated is the identifier prefix_id1[_id2…]: prefix starts with an upper-case letter and holds
// no "_" of its own beyond its end; every id is checked against the graph's form (a defence: the
// validator refuses another), and the result must not end with a suffix Stan reserves (an id that
// is exactly "lpdf", "cdf"… would make one: refused, see the handoff of lot S0).
func generated(prefix string, ids ...string) (string, error) {
	if prefix == "" || prefix[0] < 'A' || prefix[0] > 'Z' {
		return "", bug("the prefix %q of a generated identifier", prefix)
	}
	var b strings.Builder
	b.WriteString(prefix)
	for _, id := range ids {
		if !graphID(id) {
			return "", bug("the id %s in a generated identifier is not of the graph's form", strconv.Quote(id))
		}
		b.WriteByte('_')
		b.WriteString(id)
	}
	s := b.String()
	for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
		if strings.HasSuffix(s, suffix) {
			return "", fmt.Errorf("%w: the id %s makes the identifier %s end with %s, a suffix Stan reserves",
				ErrUnsupported, strconv.Quote(ids[len(ids)-1]), s, suffix)
		}
	}
	return s, nil
}

// nodeIdentifier is a node's identifier: its name when it passes the rule, V_<id> otherwise, with
// the reason (renamed) and the word that made it (for a reserved name).
func nodeIdentifier(id string, name *string) (ident, renamed, word string, err error) {
	return nameIdentifier(name, "V", id)
}

// nameIdentifier is the identifier of what a person may name (a node, a component of correlated
// effects, their correlation): the name when it passes the rule, prefix_<ids…> otherwise
// (V_<node>, V_<node>_<component>, Omega_<node>), with the reason and the word that made it.
func nameIdentifier(name *string, prefix string, ids ...string) (ident, renamed, word string, err error) {
	if name == nil {
		ident, err = generated(prefix, ids...)
		return ident, RenamedNoName, "", err
	}
	if !personName(*name) {
		return "", "", "", bug("the name %s of %s is not of the graph's form", strconv.Quote(*name), strconv.Quote(strings.Join(ids, ".")))
	}
	if w := reservedWordFor(*name); w != "" {
		ident, err = generated(prefix, ids...)
		return ident, RenamedReserved, w, err
	}
	return *name, "", "", nil
}

func strp(s string) *string { return &s }

func optp(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
