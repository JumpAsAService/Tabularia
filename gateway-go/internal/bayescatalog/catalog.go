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
}

// LocScale names the location and the scale parameters (the non-centred form).
type LocScale struct {
	Location string `json:"location"`
	Scale    string `json:"scale"`
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
	MaxGraphBytes          int     `json:"max_graph_bytes"`
}

// positive reports whether every limit is a finite number above zero.
func (l Limits) positive() bool {
	for _, v := range []float64{
		float64(l.MaxNameBytes), float64(l.MaxLabelChars), float64(l.MaxNoteChars),
		float64(l.MaxExpressionBytes), float64(l.MaxExpressionNesting), float64(l.MaxExpressionNodes),
		float64(l.MaxNumberChars), l.MaxAbsLiteral, float64(l.MaxLag), float64(l.MaxNodes),
		float64(l.MaxPlates), float64(l.MaxPlateDepth), float64(l.MaxEffectComponents),
		float64(l.MaxTotalExpressionNode), float64(l.MaxGraphBytes),
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
