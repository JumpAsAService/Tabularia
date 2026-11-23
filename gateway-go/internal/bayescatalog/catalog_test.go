package bayescatalog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"
)

var stanName = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

func TestSnapshotIsWhatTheDeclarationsWrite(t *testing.T) {
	if !bytes.Equal(Raw(), Build()) {
		t.Fatal("catalog.json is not what the declarations write: run `go run ./cmd/bayescatalog` from gateway-go/")
	}
	if !bytes.HasSuffix(Raw(), []byte("}\n")) {
		t.Fatal("the snapshot ends with a newline")
	}
}

func TestRawIsACopyAndCompactIsTheSameJSON(t *testing.T) {
	r := Raw()
	r[0] = 'x'
	if Raw()[0] != '{' {
		t.Fatal("Raw hands out the embedded bytes themselves")
	}
	var a, b any
	if err := json.Unmarshal(Raw(), &a); err != nil {
		t.Fatal(err)
	}
	c := Compact()
	if err := json.Unmarshal(c, &b); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) || bytes.Contains(c, []byte("\n")) {
		t.Fatal("Compact is not the snapshot without its indentation")
	}
}

func TestSnapshotKeepsTheDocumentedOrder(t *testing.T) {
	dec := json.NewDecoder(bytes.NewReader(Raw()))
	if _, err := dec.Token(); err != nil {
		t.Fatal(err)
	}
	var keys []string
	for dec.More() {
		k, _ := dec.Token()
		keys = append(keys, k.(string))
		var skip json.RawMessage
		if err := dec.Decode(&skip); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{"format_version", "stan", "intervals", "distributions", "language", "internal_functions", "limits"}
	if !slices.Equal(keys, want) {
		t.Fatalf("keys %v, want %v", keys, want)
	}
}

func TestDistributionsInvariants(t *testing.T) {
	c := Declared()
	if len(c.Distributions) == 0 {
		t.Fatal("no distribution")
	}
	constraints := []Constraint{Real, Positive, NonNegInt, Vector, CholeskyCov}
	supports := []Support{RealLine, PositiveRay, UnitRange, Bounded, CholeskyCorrelation, VectorSupport, Binary, ZeroToN, Counts}
	seen := map[string]bool{}
	for _, d := range c.Distributions {
		if seen[d.ID] {
			t.Errorf("%s: id twice", d.ID)
		}
		seen[d.ID] = true
		if !stanName.MatchString(d.ID) {
			t.Errorf("%s: id is not a Stan-style name", d.ID)
		}
		if len(d.Uses) == 0 {
			t.Errorf("%s: no use", d.ID)
		}
		for _, u := range d.Uses {
			if !slices.Contains([]Use{Prior, Likelihood, Internal}, u) {
				t.Errorf("%s: unknown use %q", d.ID, u)
			}
		}
		if d.Params == nil {
			t.Errorf("%s: params is null in JSON, want []", d.ID)
		}
		names := map[string]bool{}
		for _, p := range d.Params {
			if !slices.Contains(constraints, p.Constraint) {
				t.Errorf("%s.%s: unknown constraint %q", d.ID, p.Name, p.Constraint)
			}
			if names[p.Name] || p.Name == "" {
				t.Errorf("%s: parameter %q empty or twice", d.ID, p.Name)
			}
			names[p.Name] = true
		}
		if !slices.Contains(supports, d.Support) {
			t.Errorf("%s: unknown support %q", d.ID, d.Support)
		}
		if (d.Support == RealLine || d.Support == PositiveRay || d.Support == UnitRange) && !c.Intervals.Has(string(d.Support)) {
			t.Errorf("%s: support %q is not a declared interval", d.ID, d.Support)
		}
		for _, n := range []string{d.Stan.Statement, d.Stan.Density, d.Stan.RNG, d.Stan.RNGInverseLink, d.Stan.LCDF, d.Stan.LCCDF} {
			if n != "" && !stanName.MatchString(n) {
				t.Errorf("%s: Stan name %q does not match %s", d.ID, n, stanName)
			}
		}
		if d.Stan.Statement == "" || d.Stan.Density == "" {
			t.Errorf("%s: no Stan statement or density", d.ID)
		}
		if (d.Stan.LCDF == "") != (d.Stan.LCCDF == "") {
			t.Errorf("%s: lcdf and lccdf come together", d.ID)
		}
		if !slices.Contains([]Browser{Exact, Marginal, None}, d.Browser) {
			t.Errorf("%s: unknown browser %q", d.ID, d.Browser)
		}
		if d.HasUse(Prior) && d.Browser == None {
			t.Errorf("%s: a prior the browser cannot draw", d.ID)
		}
		if d.HasUse(Likelihood) {
			if d.Outcome == "" {
				t.Errorf("%s: a likelihood without an outcome", d.ID)
			}
			if d.Stan.RNG == "" {
				t.Errorf("%s: a likelihood without a generator", d.ID)
			}
		}
		if d.Stan.RNGInverseLink != "" && d.Link == "" {
			t.Errorf("%s: a generator's inverse link without a link", d.ID)
		}
		if ls := d.LocationScale; ls != nil && (!names[ls.Location] || !names[ls.Scale]) {
			t.Errorf("%s: location_scale names %q and %q, not parameters", d.ID, ls.Location, ls.Scale)
		}
	}
	for _, id := range []string{"normal", "student_t", "cauchy", "exponential", "gamma", "inv_gamma", "lognormal",
		"beta", "uniform", "lkj_corr_cholesky", "multi_normal_cholesky", "std_normal", "bernoulli_logit",
		"binomial_logit", "poisson_log", "neg_binomial_2_log"} {
		if !seen[id] {
			t.Errorf("the distribution %s of the owner's list is missing", id)
		}
	}
}

func TestPriorsOfTheBrowserAreTruncatableExceptTheMultivariate(t *testing.T) {
	for _, id := range []string{"normal", "student_t", "cauchy", "exponential", "gamma", "inv_gamma", "lognormal", "beta", "uniform"} {
		d, ok := LookupDistribution(id)
		if !ok || !d.Truncatable() || d.Browser != Exact {
			t.Errorf("%s: want a truncatable distribution the browser draws exactly", id)
		}
	}
	if d, _ := LookupDistribution("neg_binomial_2_log"); d.Truncatable() {
		t.Error("neg_binomial_2_log has no CDF in Stan: it cannot be truncated")
	}
	if d, _ := LookupDistribution("binomial_logit"); d.Stan.RNG != "binomial_rng" || d.Stan.RNGInverseLink != "inv_logit" {
		t.Error("binomial_logit's generator is binomial_rng(N, inv_logit(alpha))")
	}
	if _, ok := LookupDistribution("nope"); ok {
		t.Error("an unknown id is found")
	}
}

func TestLanguage(t *testing.T) {
	c := Declared()
	names := map[string]bool{}
	for _, fn := range c.Language.Functions {
		if names[fn.Name] {
			t.Errorf("function %s twice", fn.Name)
		}
		names[fn.Name] = true
		if fn.Apart {
			continue
		}
		if fn.Arguments != 1 || fn.Stan != fn.Name {
			t.Errorf("%s: one argument and a Stan name equal to its own", fn.Name)
		}
		if !c.Intervals.Has(fn.Domain) || !c.Intervals.Has(fn.Range) {
			t.Errorf("%s: unknown interval %q -> %q", fn.Name, fn.Domain, fn.Range)
		}
	}
	for _, want := range []string{"exp", "log", "log1p", "expm1", "sqrt", "square", "inv_logit", "logit", "log1p_exp", "log_inv_logit", "lag"} {
		if _, ok := LookupFunction(want); !ok {
			t.Errorf("function %s missing", want)
		}
	}
	for _, banned := range []string{"softplus", "abs", "fmin", "fmax", "step"} {
		if _, ok := LookupFunction(banned); ok {
			t.Errorf("function %s is not in the first release", banned)
		}
	}
	for _, o := range c.Language.Operators {
		if o.Precedence < 1 || (o.Assoc != "left" && o.Assoc != "right") || (o.Kind != "binary" && o.Kind != "unary") || o.Elementwise == "" {
			t.Errorf("operator %+v is incomplete", o)
		}
	}
	codepoints := map[string]bool{}
	for _, x := range c.Language.Confusables {
		if codepoints[x.CodePoint] {
			t.Errorf("confusable %s twice", x.CodePoint)
		}
		codepoints[x.CodePoint] = true
		r, err := strconv.ParseUint(strings.TrimPrefix(x.CodePoint, "U+"), 16, 32)
		if !strings.HasPrefix(x.CodePoint, "U+") || len(x.CodePoint) < 6 || err != nil || r < 0x80 ||
			x.CodePoint != fmt.Sprintf("U+%04X", r) {
			t.Errorf("confusable %+v: not a non-ASCII code point written U+XXXX", x)
			continue
		}
		if x.Direction {
			// Named by its code point alone: no such character in a file or a message.
			if x.Char != "" || x.ASCII != "" {
				t.Errorf("confusable %+v: a direction control has neither char nor ascii", x)
			}
			continue
		}
		if string(rune(r)) != x.Char || len(x.ASCII) > 1 || (x.ASCII != "" && x.ASCII[0] > 127) {
			t.Errorf("confusable %+v: one non-ASCII character for one ASCII one (or none, when invisible)", x)
		}
	}
	tokens := map[string]bool{}
	for _, h := range c.Language.Hints {
		ascii := h.Token != ""
		for i := 0; i < len(h.Token); i++ {
			ascii = ascii && h.Token[i] > ' ' && h.Token[i] < 0x7f
		}
		if !ascii || tokens[h.Token] || !strings.HasPrefix(h.Message, "bayes.expr.hint.") {
			t.Errorf("hint %+v: an ASCII token once, and a message of bayes.expr.hint", h)
		}
		tokens[h.Token] = true
	}
	if len(c.Language.Hints) == 0 || len(c.InternalFunctions) == 0 {
		t.Error("hints and internal functions are not empty")
	}
}

func TestLimitsArePositiveAndAsDocumented(t *testing.T) {
	l := GetLimits()
	if !l.positive() {
		t.Fatalf("a limit is not positive: %+v", l)
	}
	if l.MaxNameBytes != 40 || l.MaxNodes != 200 || l.MaxGraphBytes != 262144 || l.MaxAbsLiteral != 1e15 || l.MaxLag != 60 {
		t.Errorf("a default differs from the spec's table: %+v", l)
	}
}

// The names of the model's C++ class (E4, finding 2): lower-case words of the graph's name form
// without "__", each once; every pattern has one "*"; none is matched by a generated identifier
// (an upper-case letter first).
func TestBackendMemberNames(t *testing.T) {
	s := StanLists()
	if len(s.BackendMemberWords) == 0 || len(s.BackendMemberPatterns) == 0 {
		t.Fatal("no backend member names")
	}
	seen := map[string]bool{}
	for _, w := range s.BackendMemberWords {
		if seen[w] {
			t.Errorf("%q twice", w)
		}
		seen[w] = true
		if w == "" || w[0] < 'a' || w[0] > 'z' || strings.Contains(w, "__") || len(w) > GetLimits().MaxNameBytes ||
			strings.Trim(w, "abcdefghijklmnopqrstuvwxyz0123456789_") != "" {
			t.Errorf("%q is not of the graph's name form", w)
		}
		if s.BackendMember(w) != w {
			t.Errorf("BackendMember(%q) = %q", w, s.BackendMember(w))
		}
	}
	for _, p := range s.BackendMemberPatterns {
		if strings.Count(p, "*") != 1 {
			t.Errorf("%q has not one *", p)
		}
	}
	for name, want := range map[string]string{
		"log_prob": "log_prob", "errno": "errno", "model_model": "model_model",
		"get_constrained_sizedtypes": "get_*_sizedtypes", "size_t": "size_t", "get_any_sizedtypes": "get_*_sizedtypes",
		"log_prob_impl": "*_impl", "write_array_impl": "*_impl", "my_impl": "*_impl", "a_b_impl": "*_impl",
		"_impl": "", "impl": "", "get__sizedtypes": "", "get_sizedtypes": "", "get_a_sizedtypes": "get_*_sizedtypes",
		"V_impl": "", "Z_n1_impl": "", "get_X_sizedtypes": "", "implement": "", "log_prob2": "", "mu": "",
	} {
		if got := s.BackendMember(name); got != want {
			t.Errorf("BackendMember(%q) = %q, want %q", name, got, want)
		}
	}
}

type frozen struct {
	Version, Source, Retrieved string
	ReservedWords              []string `json:"reserved_words"`
	TypeNames                  []string `json:"type_names"`
	ReservedFunctions          []string `json:"reserved_functions"`
	BlockNames                 []string `json:"block_names"`
	ReservedSuffixes           []string `json:"reserved_suffixes"`
	RenamedBackendWords        []string `json:"renamed_backend_words"`
}

// The lists are the manual's, in the manual's order (a sorted copy would not be verbatim), each
// without a duplicate.
func TestStanListsAreTheFrozenCopyOfTheManual(t *testing.T) {
	b, err := os.ReadFile("testdata/stan_reserved_2.40.json")
	if err != nil {
		t.Fatal(err)
	}
	var f frozen
	if err := json.Unmarshal(b, &f); err != nil {
		t.Fatal(err)
	}
	if f.Source == "" || f.Retrieved == "" {
		t.Fatal("the frozen copy names its URL and its date")
	}
	s := StanLists()
	if s.Version != f.Version || s.Source != f.Source {
		t.Errorf("version/source %q %q, frozen %q %q", s.Version, s.Source, f.Version, f.Source)
	}
	for name, pair := range map[string][2][]string{
		"reserved_words":        {s.ReservedWords, f.ReservedWords},
		"type_names":            {s.TypeNames, f.TypeNames},
		"reserved_functions":    {s.ReservedFunctions, f.ReservedFunctions},
		"block_names":           {s.BlockNames, f.BlockNames},
		"reserved_suffixes":     {s.ReservedSuffixes, f.ReservedSuffixes},
		"renamed_backend_words": {s.RenamedBackendWords, f.RenamedBackendWords},
	} {
		if len(pair[1]) == 0 || !slices.Equal(pair[0], pair[1]) {
			t.Errorf("%s differs from testdata/stan_reserved_2.40.json", name)
		}
		seen := map[string]bool{}
		for _, w := range pair[0] {
			if seen[w] {
				t.Errorf("%s: %q twice", name, w)
			}
			seen[w] = true
		}
	}
}
