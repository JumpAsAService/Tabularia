package bayescodegen

import (
	"errors"
	"strings"
	"testing"
)

func TestNodeIdentifier(t *testing.T) {
	name := func(s string) *string { return &s }
	cases := []struct {
		id      string
		name    *string
		ident   string
		renamed string
		word    string
	}{
		{"n8", nil, "V_n8", RenamedNoName, ""},
		{"bx", name("b_x"), "b_x", "", ""},
		{"mu", name("mu"), "mu", "", ""},
		{"e", name("e"), "e", "", ""}, // a library function the catalogue does not list: stanc accepts it
		// Stan's lists
		{"n1", name("lower"), "V_n1", RenamedReserved, "lower"},
		{"n1", name("for"), "V_n1", RenamedReserved, "for"},
		{"n1", name("vector"), "V_n1", RenamedReserved, "vector"},
		{"n1", name("model"), "V_n1", RenamedReserved, "model"},
		{"n1", name("jacobian"), "V_n1", RenamedReserved, "jacobian"},
		{"n1", name("return"), "V_n1", RenamedReserved, "return"},
		{"n1", name("tuple"), "V_n1", RenamedReserved, "tuple"},
		{"n1", name("void"), "V_n1", RenamedReserved, "void"},
		{"n1", name("break"), "V_n1", RenamedReserved, "break"},
		{"n1", name("continue"), "V_n1", RenamedReserved, "continue"},
		{"n1", name("class"), "V_n1", RenamedReserved, "class"},
		// the words the output refuses
		{"n1", name("print"), "V_n1", RenamedReserved, "print"},
		{"n1", name("include"), "V_n1", RenamedReserved, "include"},
		{"n1", name("profile"), "V_n1", RenamedReserved, "profile"},
		// the catalogue's names
		{"n1", name("normal"), "V_n1", RenamedReserved, "normal"},
		{"n1", name("inv_gamma"), "V_n1", RenamedReserved, "inv_gamma"},
		{"n1", name("normal_rng"), "V_n1", RenamedReserved, "normal_rng"},
		{"n1", name("binomial_rng"), "V_n1", RenamedReserved, "binomial_rng"},
		{"n1", name("exp"), "V_n1", RenamedReserved, "exp"},
		{"n1", name("to_vector"), "V_n1", RenamedReserved, "to_vector"},
		{"n1", name("rep_vector"), "V_n1", RenamedReserved, "rep_vector"},
		{"n1", name("std_normal"), "V_n1", RenamedReserved, "std_normal"},
		{"n1", name("multiply_lower_tri_self_transpose"), "V_n1", RenamedReserved, "multiply_lower_tri_self_transpose"},
		// the names of the model's C++ class (E4, finding 2)
		{"n1", name("log_prob"), "V_n1", RenamedReserved, "log_prob"},
		{"n1", name("write_array"), "V_n1", RenamedReserved, "write_array"},
		{"n1", name("errno"), "V_n1", RenamedReserved, "errno"},
		{"n1", name("model_model"), "V_n1", RenamedReserved, "model_model"},
		{"n1", name("emit_generated_quantities"), "V_n1", RenamedReserved, "emit_generated_quantities"},
		{"n1", name("get_any_sizedtypes"), "V_n1", RenamedReserved, "get_*_sizedtypes"},
		{"n1", name("my_impl"), "V_n1", RenamedReserved, "*_impl"},
		{"impl", nil, "V_impl", RenamedNoName, ""}, // C++ tells cases apart: V_impl is no member
		{"n1", name("log_prob2"), "log_prob2", "", ""},
		{"n1", name("implement"), "implement", "", ""},
		// the suffixes
		{"n1", name("x_lpdf"), "V_n1", RenamedReserved, "_lpdf"},
		{"n1", name("y_lupmf"), "V_n1", RenamedReserved, "_lupmf"},
		{"n1", name("z_cdf"), "V_n1", RenamedReserved, "_cdf"},
	}
	for _, c := range cases {
		ident, renamed, word, err := nodeIdentifier(c.id, c.name)
		if err != nil || ident != c.ident || renamed != c.renamed || word != c.word {
			n := "<nil>"
			if c.name != nil {
				n = *c.name
			}
			t.Errorf("%s %s: got (%s, %s, %s, %v), want (%s, %s, %s)", c.id, n, ident, renamed, word, err, c.ident, c.renamed, c.word)
		}
	}
}

func TestNodeIdentifierRefusesWhatAValidModelCannotHold(t *testing.T) {
	name := func(s string) *string { return &s }
	for _, n := range []string{"", "Beta", "1x", "x y", "x__", "x-y", "é", strings.Repeat("a", 41)} {
		if _, _, _, err := nodeIdentifier("n1", name(n)); !errors.Is(err, ErrBug) {
			t.Errorf("name %q: %v", n, err)
		}
	}
	for _, id := range []string{"", "N1", "n_1", "1n", "abcdefghijklmnopq", "n.c"} {
		if _, _, _, err := nodeIdentifier(id, nil); !errors.Is(err, ErrBug) {
			t.Errorf("id %q: %v", id, err)
		}
	}
	// An id that is a reserved suffix's tail makes V_<id> end with it: refused, never written.
	for _, id := range []string{"lpdf", "lpmf", "lcdf", "lccdf", "cdf", "ccdf", "lupdf", "lupmf"} {
		if _, _, _, err := nodeIdentifier(id, nil); !errors.Is(err, ErrUnsupported) {
			t.Errorf("id %q: %v", id, err)
		}
	}
}

func TestGeneratedNeverCollidesWithANameThatPasses(t *testing.T) {
	// a person's name starts with a lower-case letter, a generated identifier with an upper-case one
	for _, prefix := range []string{"V", "J", "K", "Up", "Z", "L", "W", "Full"} {
		s, err := generated(prefix, "n1", "c2")
		if err != nil || s != prefix+"_n1_c2" {
			t.Errorf("%s: %s %v", prefix, s, err)
		}
	}
	if _, err := generated("v", "n1"); !errors.Is(err, ErrBug) {
		t.Error("a lower-case prefix")
	}
}

func TestCheckIdentifiers(t *testing.T) {
	n := func(s string) *string { return &s }
	ok := []MappingEntry{
		{Stan: "N", Kind: KindSize},
		{Stan: "b_x", Name: n("b_x"), Kind: "parameter"},
		{Stan: "V_n1", Kind: "parameter"},
		{Stan: "V_n2", Name: n("lower"), Renamed: n(RenamedReserved), Kind: "parameter"},
		{Stan: "V_impl", Kind: "parameter"},
	}
	if err := CheckIdentifiers(ok); err != nil {
		t.Fatal(err)
	}
	bad := map[string][]MappingEntry{
		"twice":           {{Stan: "N"}, {Stan: "N"}},
		"reserved":        {{Stan: "lower", Name: n("lower")}},
		"suffix":          {{Stan: "x_lpdf", Name: n("x_lpdf")}},
		"double":          {{Stan: "V__"}},
		"not lexical":     {{Stan: "a-b", Name: n("a-b")}},
		"underscore":      {{Stan: "_a"}},
		"not the name":    {{Stan: "b", Name: n("a")}},
		"lower generated": {{Stan: "v_n1"}},
		"renamed passes":  {{Stan: "V_n1", Name: n("mu")}},
		"function":        {{Stan: "normal_lpdf", Name: n("normal_lpdf")}},
		"backend member":  {{Stan: "log_prob", Name: n("log_prob")}},
		"backend pattern": {{Stan: "my_impl", Name: n("my_impl")}},
	}
	for what, m := range bad {
		if err := CheckIdentifiers(m); !errors.Is(err, ErrBug) {
			t.Errorf("%s: %v", what, err)
		}
	}
}
