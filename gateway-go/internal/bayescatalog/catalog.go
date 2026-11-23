// Package bayescatalog is the distribution catalogue of the Bayesian model editor
// (docs/specs/bayes-distribution-catalog.md): the distributions with their parameters in Stan's
// order, the one rule of bounds and truncation's inputs, the functions and operators the
// expression language may use, Stan's reserved words, and the limits of the language and of
// the graph.
//
// The catalogue is declared once, here, in declare.go. catalog.json is its snapshot, written by
// `go run ./cmd/bayescatalog` and embedded, so that the gateway is built from its own folder
// alone; .github/scripts/bayes_catalog.py copies it to the frontend and checks the copy. A
// test fails when the snapshot is not what the declarations write.
package bayescatalog

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"math"
	"strings"
)

//go:embed catalog.json
var raw []byte

// Raw is the snapshot, byte for byte.
func Raw() []byte { return append([]byte(nil), raw...) }

// Compact is the snapshot without its indentation (the same JSON, half the bytes): what a Job
// receives as its catalogue. A Job's ConfigMap holds at most 1 MiB.
func Compact() []byte {
	var out bytes.Buffer
	if err := json.Compact(&out, raw); err != nil {
		panic("bayescatalog: catalog.json: " + err.Error())
	}
	return out.Bytes()
}

// FormatVersion is the snapshot's format, changed when a reader would misread an older one.
const FormatVersion = 1

// Use is the role a distribution may play in a graph.
type Use string

const (
	Prior      Use = "prior"
	Likelihood Use = "likelihood"
	Internal   Use = "internal"
)

// Constraint is what a distribution's argument must satisfy.
type Constraint string

const (
	Real        Constraint = "real"
	Positive    Constraint = "positive"
	NonNegInt   Constraint = "non_negative_integer"
	Vector      Constraint = "vector"
	CholeskyCov Constraint = "cholesky_factor_cov"
)

// Support is where a distribution puts its mass. Real, Positive and Unit are also the names of
// intervals; the others are shapes of values.
type Support string

const (
	RealLine    Support = "real"
	PositiveRay Support = "positive"
	UnitRange   Support = "unit"
	// Bounded is [alpha, beta], the parameters' own bounds (the uniform).
	Bounded Support = "bounded"
	// CholeskyCorrelation is the Cholesky factors of K x K correlation matrices.
	CholeskyCorrelation Support = "cholesky_factor_corr"
	VectorSupport       Support = "vector"
	Binary              Support = "binary"
	ZeroToN             Support = "zero_to_n"
	Counts              Support = "counts"
)

// Browser says what the browser can draw of a distribution: all of it (Exact), the marginal
// of one component (Marginal), or nothing (None).
type Browser string

const (
	Exact    Browser = "exact"
	Marginal Browser = "marginal"
	None     Browser = "none"
)

// Param is one argument of a distribution, in Stan's order.
type Param struct {
	Name       string     `json:"name"`
	Constraint Constraint `json:"constraint"`
	// Constant: the argument is a number written in the graph, never a parameter.
	Constant bool `json:"constant,omitempty"`
	// Data: the argument is data (the binomial's number of trials).
	Data bool `json:"data,omitempty"`
}

// StanNames are the names a distribution has in Stan 2.40. RNGInverseLink is set when Stan has
// no generator for the linked form: the generator of the plain distribution is called with the
// inverse link of the linear parameter (binomial_rng(N, inv_logit(alpha))).
type StanNames struct {
	Statement      string `json:"statement"`
	Density        string `json:"density"`
	RNG            string `json:"rng,omitempty"`
	RNGInverseLink string `json:"rng_inverse_link,omitempty"`
	LCDF           string `json:"lcdf,omitempty"`
	LCCDF          string `json:"lccdf,omitempty"`
	// Quantile is the form of the prior's quantile in Stan, with which predictive-checks draws a
	// truncated prior by inversion; empty when Stan has none (gamma, inv_gamma).
	Quantile Quantile `json:"quantile,omitempty"`
}

// Quantile is the form in which a truncated prior's ancestral draw is written in the program, by
// inversion (docs/specs/bayes-predictive-checks.md, «Truncated priors»): u drawn uniform between
// F(L) and F(U), the cumulative distribution at the bounds, then x = Q(u). Stan 2.40 writes a
// _cdf only with a vertical bar between its first two arguments (measured, lot P1), a byte the
// program's alphabet refuses, so F is written with functions of the catalogue as well: z is
// (x - mu) * inv(sigma), every name below is a function of the language or an internal function.
type Quantile string

const (
	// QuantileInvPhi (normal): F = Phi(z); Q = mu + sigma * inv_Phi(u).
	QuantileInvPhi Quantile = "inv_Phi"
	// QuantileExpInvPhi (lognormal): F = Phi((log(x) - mu) * inv(sigma)); Q = exp(mu + sigma * inv_Phi(u)).
	QuantileExpInvPhi Quantile = "exp_inv_Phi"
	// QuantileTan (Cauchy): F = 0.5 + atan(z) / pi; Q = mu + sigma * tan(pi * (u - 0.5)).
	QuantileTan Quantile = "tan"
	// QuantileIncBetaT (Student-t): with w = square(z) * inv(nu + square(z)),
	// F = 0.5 + (step(z) - 0.5) * inc_beta(0.5, 0.5 * nu, w); Q = mu + sigma * s * sqrt(nu * v * inv(1 - v))
	// with v = inv_inc_beta(0.5, 0.5 * nu, |2u - 1|) and s = 2 * step(u - 0.5) - 1 (|2u - 1| written
	// s * (2u - 1): Stan 2.40 has no fabs).
	QuantileIncBetaT Quantile = "inv_inc_beta_t"
	// QuantileIncBeta (beta): F = inc_beta(alpha, beta, x); Q = inv_inc_beta(alpha, beta, u).
	QuantileIncBeta Quantile = "inv_inc_beta"
	// QuantileLog1m (exponential, rate beta): F = -expm1(-beta * x); Q = -log1m(u) * inv(beta).
	QuantileLog1m Quantile = "log1m"
	// QuantileLinear (uniform, constant bounds): Q = L + (U - L) * u, L and U the bounds within
	// its support.
	QuantileLinear Quantile = "linear"
)

// QuantileFunctions are the Stan functions each form writes, F and Q (a test checks that the
// program may call each).
var QuantileFunctions = map[Quantile][]string{
	QuantileInvPhi:    {"Phi", "inv", "inv_Phi", "uniform_rng"},
	QuantileExpInvPhi: {"Phi", "inv", "log", "exp", "inv_Phi", "uniform_rng"},
	QuantileTan:       {"atan", "inv", "tan", "uniform_rng"},
	QuantileIncBetaT:  {"step", "square", "inv", "inc_beta", "inv_inc_beta", "sqrt", "uniform_rng"},
	QuantileIncBeta:   {"inc_beta", "inv_inc_beta", "uniform_rng"},
	QuantileLog1m:     {"expm1", "log1m", "inv", "uniform_rng"},
	QuantileLinear:    {"uniform_rng"},
}

// LocScale names the location and the scale parameters (the non-centred form).
type LocScale struct {
	Location string `json:"location"`
	Scale    string `json:"scale"`
}

// VarianceRule says when a distribution's marginal has a finite variance, the one fact the
// prior-to-posterior contraction needs (docs/specs/bayes-diagnostics.md, «Prior-to-posterior
// contraction»): Always, Never, or Above, when the constant argument Param is above Bound (the
// Student-t's nu above 2, the inverse-gamma's alpha above 2).
type VarianceRule string

const (
	VarianceAlways VarianceRule = "always"
	VarianceNever  VarianceRule = "never"
	VarianceAbove  VarianceRule = "above"
)

// FiniteVariance is a distribution's rule. A truncation to a bounded range makes the variance
// finite whatever the rule; that is the graph's to say, not the distribution's.
type FiniteVariance struct {
	Rule  VarianceRule `json:"rule"`
	Param string       `json:"param,omitempty"`
	Above *float64     `json:"above,omitempty"`
}

// Holds reports whether the variance is finite for the argument's value (ignored unless the rule
// is Above).
func (v FiniteVariance) Holds(value float64) bool {
	switch v.Rule {
	case VarianceAlways:
		return true
	case VarianceAbove:
		return v.Above != nil && value > *v.Above
	}
	return false
}

// Distribution is one entry of the catalogue.
type Distribution struct {
	ID            string    `json:"id"`
	Uses          []Use     `json:"uses"`
	Params        []Param   `json:"params"`
	Support       Support   `json:"support"`
	Outcome       Support   `json:"outcome,omitempty"`
	Link          string    `json:"link,omitempty"`
	Stan          StanNames `json:"stan"`
	LocationScale *LocScale `json:"location_scale,omitempty"`
	Browser       Browser   `json:"browser"`
	// FiniteVariance: when the marginal has a finite variance (lot X4).
	FiniteVariance FiniteVariance `json:"finite_variance"`
}

// Truncatable reports whether Stan has the CDFs a truncation needs (rule 5 of «Bounds and
// truncation»).
func (d Distribution) Truncatable() bool { return d.Stan.LCDF != "" && d.Stan.LCCDF != "" }

// HasUse reports whether the distribution may play the role.
func (d Distribution) HasUse(u Use) bool {
	for _, x := range d.Uses {
		if x == u {
			return true
		}
	}
	return false
}

// Interval is a set of reals. A missing bound is infinite.
type Interval struct {
	Lower     *float64 `json:"lower,omitempty"`
	LowerOpen bool     `json:"lower_open,omitempty"`
	Upper     *float64 `json:"upper,omitempty"`
	UpperOpen bool     `json:"upper_open,omitempty"`
}

// NamedInterval is an interval and its id.
type NamedInterval struct {
	ID string
	Interval
}

// Intervals are the named intervals, in the order they are declared; they are written as a
// JSON object with that order.
type Intervals []NamedInterval

// Has reports whether the id names an interval.
func (is Intervals) Has(id string) bool {
	for _, i := range is {
		if i.ID == id {
			return true
		}
	}
	return false
}

// MarshalJSON writes the intervals as an object, keys in the declared order.
func (is Intervals) MarshalJSON() ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('{')
	for n, i := range is {
		if n > 0 {
			b.WriteByte(',')
		}
		key, err := json.Marshal(i.ID)
		if err != nil {
			return nil, err
		}
		val, err := json.Marshal(i.Interval)
		if err != nil {
			return nil, err
		}
		b.Write(key)
		b.WriteByte(':')
		b.Write(val)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

// Function is one function of the expression language. A function the language treats apart
// (lag) has no Stan name and no intervals.
type Function struct {
	Name      string `json:"name"`
	Stan      string `json:"stan,omitempty"`
	Arguments int    `json:"arguments"`
	Domain    string `json:"domain,omitempty"`
	Range     string `json:"range,omitempty"`
	Apart     bool   `json:"apart,omitempty"`
}

// Operator is one operator with its Stan forms and Stan's precedence (1 binds tightest) and
// associativity.
type Operator struct {
	Symbol      string `json:"symbol"`
	Kind        string `json:"kind"` // "binary" or "unary"
	Stan        string `json:"stan"`
	Elementwise string `json:"elementwise"`
	Precedence  int    `json:"precedence"`
	Assoc       string `json:"assoc"` // "left" or "right"
}

// Hint is an operator of another language, and the id of the message that says what to write.
type Hint struct {
	Token   string `json:"token"`
	Message string `json:"message"`
}

// Confusable is a non-ASCII character that looks like an ASCII one, and the one meant: ASCII is
// empty for an invisible character. A direction control (Direction) has neither Char nor ASCII:
// it is named by its code point alone.
type Confusable struct {
	CodePoint string `json:"codepoint"`
	Char      string `json:"char"`
	ASCII     string `json:"ascii"`
	Direction bool   `json:"direction,omitempty"`
}

// Language is what the expression language may accept and emit.
type Language struct {
	Functions   []Function   `json:"functions"`
	Operators   []Operator   `json:"operators"`
	Hints       []Hint       `json:"hints"`
	Confusables []Confusable `json:"confusables"`
}

// Limits are the documented constants of the language and of the graph (one number each).
type Limits struct {
	MaxNameBytes           int     `json:"max_name_bytes"`
	MaxLabelChars          int     `json:"max_label_chars"`
	MaxNoteChars           int     `json:"max_note_chars"`
	MaxExpressionBytes     int     `json:"max_expression_bytes"`
	MaxExpressionNesting   int     `json:"max_expression_nesting"`
	MaxExpressionNodes     int     `json:"max_expression_nodes"`
	MaxNumberChars         int     `json:"max_number_chars"`
	MaxAbsLiteral          float64 `json:"max_abs_literal"`
	MaxLag                 int     `json:"max_lag"`
	MaxNodes               int     `json:"max_nodes"`
	MaxPlates              int     `json:"max_plates"`
	MaxPlateDepth          int     `json:"max_plate_depth"`
	MaxEffectComponents    int     `json:"max_effect_components"`
	MaxTotalExpressionNode int     `json:"max_total_expression_nodes"`
	// MaxProgramVectorNodes bounds the compile cost: the nodes of the expressions whose value is
	// over a plate, and of the linear predictors over a plate, summed over the graph (correction
	// of 2026-11-24 from the expression language's review E4, owner).
	MaxProgramVectorNodes int `json:"max_program_vector_nodes"`
	MaxGraphBytes         int `json:"max_graph_bytes"`
}

// positive reports whether every limit is a finite number above zero.
func (l Limits) positive() bool {
	for _, v := range []float64{
		float64(l.MaxNameBytes), float64(l.MaxLabelChars), float64(l.MaxNoteChars),
		float64(l.MaxExpressionBytes), float64(l.MaxExpressionNesting), float64(l.MaxExpressionNodes),
		float64(l.MaxNumberChars), l.MaxAbsLiteral, float64(l.MaxLag), float64(l.MaxNodes),
		float64(l.MaxPlates), float64(l.MaxPlateDepth), float64(l.MaxEffectComponents),
		float64(l.MaxTotalExpressionNode), float64(l.MaxProgramVectorNodes), float64(l.MaxGraphBytes),
	} {
		if !(v > 0) || math.IsInf(v, 0) {
			return false
		}
	}
	return true
}

// Stan holds Stan's lists, copied verbatim from the reference manual's «Reserved names» of the
// version named (testdata/stan_reserved_2.40.json is the frozen copy a test compares with).
type Stan struct {
	Version             string   `json:"version"`
	Source              string   `json:"source"`
	ReservedWords       []string `json:"reserved_words"`
	TypeNames           []string `json:"type_names"`
	ReservedFunctions   []string `json:"reserved_functions"`
	BlockNames          []string `json:"block_names"`
	ReservedSuffixes    []string `json:"reserved_suffixes"`
	RenamedBackendWords []string `json:"renamed_backend_words"`
	// BackendMemberWords are the names the C++ class stanc writes for a model already holds (its
	// member functions, the parameters and locals of its methods, the macros of the C library it
	// expands): a data variable is a member of that class, so stanc accepts such a name and g++
	// refuses the program. Read from the header stanc 2.40.0 writes and compiled with g++ in the
	// image of the group bayes (correction of 2026-11-24 from the expression language's review E4,
	// finding 2). Not the manual's: no frozen copy.
	BackendMemberWords []string `json:"backend_member_words"`
	// BackendMemberPatterns are families of such names: "*" stands for one or more of [a-z0-9_]
	// (get_*_sizedtypes, *_impl). See MatchesBackendMember.
	BackendMemberPatterns []string `json:"backend_member_patterns"`
}

// BackendMember reports the word or the pattern of BackendMemberWords and BackendMemberPatterns a
// name is, "" when it is none.
func (s Stan) BackendMember(name string) string {
	for _, w := range s.BackendMemberWords {
		if name == w {
			return w
		}
	}
	for _, p := range s.BackendMemberPatterns {
		if MatchesBackendPattern(p, name) {
			return p
		}
	}
	return ""
}

// MatchesBackendPattern reports whether a name is of a pattern with one "*", which stands for one
// or more of [a-z0-9_]: *_impl is log_prob_impl, not V_impl (C++ tells cases apart, and a
// generated identifier starts with an upper-case letter).
func MatchesBackendPattern(pattern, name string) bool {
	star := strings.IndexByte(pattern, '*')
	if star < 0 || strings.Count(pattern, "*") != 1 {
		return false
	}
	prefix, suffix := pattern[:star], pattern[star+1:]
	if len(name) <= len(prefix)+len(suffix) || !strings.HasPrefix(name, prefix) || !strings.HasSuffix(name, suffix) {
		return false
	}
	for _, c := range []byte(name[len(prefix) : len(name)-len(suffix)]) {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

// Catalog is the whole snapshot.
type Catalog struct {
	FormatVersion     int            `json:"format_version"`
	Stan              Stan           `json:"stan"`
	Intervals         Intervals      `json:"intervals"`
	Distributions     []Distribution `json:"distributions"`
	Language          Language       `json:"language"`
	InternalFunctions []string       `json:"internal_functions"`
	Limits            Limits         `json:"limits"`
}

// Declared is the catalogue as declare.go writes it.
func Declared() Catalog { return declared }

// Build is the snapshot the declarations write: JSON indented by two spaces, keys in the order
// of the types, one final newline. `go run ./cmd/bayescatalog` writes it to catalog.json.
func Build() []byte {
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(declared); err != nil {
		panic("bayescatalog: " + err.Error())
	}
	return out.Bytes()
}

// LookupDistribution returns the distribution with the id.
func LookupDistribution(id string) (Distribution, bool) {
	for _, d := range declared.Distributions {
		if d.ID == id {
			return d, true
		}
	}
	return Distribution{}, false
}

// LookupFunction returns the language's function with the name.
func LookupFunction(name string) (Function, bool) {
	for _, f := range declared.Language.Functions {
		if f.Name == name {
			return f, true
		}
	}
	return Function{}, false
}

// GetLimits are the limits of the language and of the graph.
func GetLimits() Limits { return declared.Limits }

// StanLists are Stan's reserved lists.
func StanLists() Stan { return declared.Stan }
