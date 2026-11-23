package bayescatalog

func uses(u ...Use) []Use { return u }

func f(x float64) *float64 { return &x }

// params builds a parameter list, so that a distribution without arguments is [] and not null.
func params(p ...Param) []Param {
	if p == nil {
		return []Param{}
	}
	return p
}

var declared = Catalog{
	FormatVersion: FormatVersion,

	// Stan 2.40.0's lists, in the manual's order, copied from the reference manual's «Reserved
	// names» (read 2026-11-23). bayes-image checks them against the version it pins.
	Stan: Stan{
		Version: "2.40.0",
		Source:  "https://mc-stan.org/docs/reference-manual/expressions.html",
		ReservedWords: []string{
			"for", "in", "while", "repeat", "until", "if", "then", "else",
			"true", "false", "target", "struct", "typedef", "export",
			"auto", "extern", "var", "static", "lower", "upper", "offset",
			"multiplier",
			// Not in the manual's list, refused as names by stanc 2.40.0 (lot S0, 2026-11-24).
			"return", "void", "tuple", "break", "continue",
		},
		TypeNames: []string{
			"int", "real", "complex", "vector", "simplex", "unit_vector",
			"sum_to_zero_vector", "sum_to_zero_matrix", "ordered", "positive_ordered",
			"row_vector", "matrix", "cholesky_factor_corr",
			"column_stochastic_matrix", "row_stochastic_matrix",
			"cholesky_factor_cov", "corr_matrix", "cov_matrix", "array",
			// Not in the manual's list, refused as names by stanc 2.40.0 (lot S0, 2026-11-24).
			"complex_vector", "complex_row_vector", "complex_matrix",
		},
		ReservedFunctions: []string{"print", "reject", "profile", "fatal_error", "target", "jacobian"},
		BlockNames: []string{
			"functions", "model", "data", "parameters", "quantities",
			"transformed", "generated",
		},
		ReservedSuffixes: []string{"_lpdf", "_lpmf", "_lcdf", "_lccdf", "_cdf", "_ccdf", "_lupdf", "_lupmf"},
		RenamedBackendWords: []string{
			"alignas", "alignof", "and", "and_eq", "asm", "bitand", "bitor", "bool",
			"case", "catch", "char", "char16_t", "char32_t", "class", "compl", "const",
			"constexpr", "const_cast", "decltype", "default", "delete", "do",
			"double", "dynamic_cast", "enum", "explicit", "float", "friend", "goto",
			"inline", "long", "mutable", "namespace", "new", "noexcept", "not", "not_eq",
			"nullptr", "operator", "or", "or_eq", "private", "protected", "public",
			"register", "reinterpret_cast", "short", "signed", "sizeof",
			"static_assert", "static_cast", "switch", "template", "this", "thread_local",
			"throw", "try", "typeid", "typename", "union", "unsigned", "using", "virtual",
			"volatile", "wchar_t", "xor", "xor_eq", "fvar", "STAN_MAJOR", "STAN_MINOR",
			"STAN_PATCH", "STAN_MATH_MAJOR", "STAN_MATH_MINOR", "STAN_MATH_PATCH",
		},
		// The names of the C++ class stanc 2.40.0 writes for a model that g++ refuses for a data
		// variable (a member of that class), found by compiling in the image of the group bayes
		// every lower-case word of the headers stanc writes for the golden programs and a program
		// with every block, of model_base.hpp and model_base_crtp.hpp, and of the macros the
		// compile defines (E4, finding 2, 2026-11-24): the class's member functions, the base
		// class's name (its constructor's initializer), size_t, the object-like macros of the C
		// library, SUNDIALS and Eigen; model_model, which stanc refuses (the compile Job's file is
		// model.stan); emit_transformed_parameters and emit_generated_quantities, the parameters
		// of write_array (seen failing by the review, conservative).
		BackendMemberWords: []string{
			"log_prob", "write_array", "transform_inits", "unconstrain_array", "model_name",
			"model_compile_info", "get_dims", "get_param_names", "constrained_param_names",
			"unconstrained_param_names", "model_base_crtp", "size_t", "model_model",
			"emit_transformed_parameters", "emit_generated_quantities",
			"errno", "math_errhandling", "booleantype", "sunbooleantype", "chkder_factor", "chkder_log10e",
		},
		BackendMemberPatterns: []string{"get_*_sizedtypes", "*_impl"},
	},

	// The intervals the supports, the functions' domains and their ranges are written in. A
	// missing bound is infinite.
	Intervals: Intervals{
		{ID: "real"},
		{ID: "positive", Interval: Interval{Lower: f(0), LowerOpen: true}},
		{ID: "unit", Interval: Interval{Lower: f(0), LowerOpen: true, Upper: f(1), UpperOpen: true}},
		{ID: "non_negative", Interval: Interval{Lower: f(0)}},
		{ID: "above_minus_one", Interval: Interval{Lower: f(-1), LowerOpen: true}},
		{ID: "negative", Interval: Interval{Upper: f(0), UpperOpen: true}},
	},

	Distributions: []Distribution{
		{ID: "normal", Uses: uses(Prior, Likelihood),
			Params:  params(Param{Name: "mu", Constraint: Real}, Param{Name: "sigma", Constraint: Positive}),
			Support: RealLine, Outcome: RealLine,
			Stan: StanNames{Statement: "normal", Density: "normal_lpdf", RNG: "normal_rng",
				LCDF: "normal_lcdf", LCCDF: "normal_lccdf"},
			LocationScale: &LocScale{Location: "mu", Scale: "sigma"},
			Browser:       Exact},

		// Student's t as Stan writes it: degrees of freedom, location, scale. The browser's library
		// has the standard t only; bayesDistributions.ts moves and stretches it.
		{ID: "student_t", Uses: uses(Prior),
			Params: params(Param{Name: "nu", Constraint: Positive}, Param{Name: "mu", Constraint: Real},
				Param{Name: "sigma", Constraint: Positive}),
			Support: RealLine,
			Stan: StanNames{Statement: "student_t", Density: "student_t_lpdf", RNG: "student_t_rng",
				LCDF: "student_t_lcdf", LCCDF: "student_t_lccdf"},
			LocationScale: &LocScale{Location: "mu", Scale: "sigma"},
			Browser:       Exact},

		{ID: "cauchy", Uses: uses(Prior),
			Params:  params(Param{Name: "mu", Constraint: Real}, Param{Name: "sigma", Constraint: Positive}),
			Support: RealLine,
			Stan: StanNames{Statement: "cauchy", Density: "cauchy_lpdf", RNG: "cauchy_rng",
				LCDF: "cauchy_lcdf", LCCDF: "cauchy_lccdf"},
			LocationScale: &LocScale{Location: "mu", Scale: "sigma"},
			Browser:       Exact},

		// Stan's beta is the rate (the inverse scale).
		{ID: "exponential", Uses: uses(Prior),
			Params:  params(Param{Name: "beta", Constraint: Positive}),
			Support: PositiveRay,
			Stan: StanNames{Statement: "exponential", Density: "exponential_lpdf", RNG: "exponential_rng",
				LCDF: "exponential_lcdf", LCCDF: "exponential_lccdf"},
			Browser: Exact},

		// Shape and rate.
		{ID: "gamma", Uses: uses(Prior),
			Params:  params(Param{Name: "alpha", Constraint: Positive}, Param{Name: "beta", Constraint: Positive}),
			Support: PositiveRay,
			Stan: StanNames{Statement: "gamma", Density: "gamma_lpdf", RNG: "gamma_rng",
				LCDF: "gamma_lcdf", LCCDF: "gamma_lccdf"},
			Browser: Exact},

		// Shape and scale.
		{ID: "inv_gamma", Uses: uses(Prior),
			Params:  params(Param{Name: "alpha", Constraint: Positive}, Param{Name: "beta", Constraint: Positive}),
			Support: PositiveRay,
			Stan: StanNames{Statement: "inv_gamma", Density: "inv_gamma_lpdf", RNG: "inv_gamma_rng",
				LCDF: "inv_gamma_lcdf", LCCDF: "inv_gamma_lccdf"},
			Browser: Exact},

		{ID: "lognormal", Uses: uses(Prior),
			Params:  params(Param{Name: "mu", Constraint: Real}, Param{Name: "sigma", Constraint: Positive}),
			Support: PositiveRay,
			Stan: StanNames{Statement: "lognormal", Density: "lognormal_lpdf", RNG: "lognormal_rng",
				LCDF: "lognormal_lcdf", LCCDF: "lognormal_lccdf"},
			Browser: Exact},

		{ID: "beta", Uses: uses(Prior),
			Params:  params(Param{Name: "alpha", Constraint: Positive}, Param{Name: "beta", Constraint: Positive}),
			Support: UnitRange,
			Stan: StanNames{Statement: "beta", Density: "beta_lpdf", RNG: "beta_rng",
				LCDF: "beta_lcdf", LCCDF: "beta_lccdf"},
			Browser: Exact},

		// Both bounds are constants in the first release; the parameter is declared with them.
		{ID: "uniform", Uses: uses(Prior),
			Params: params(Param{Name: "alpha", Constraint: Real, Constant: true},
				Param{Name: "beta", Constraint: Real, Constant: true}),
			Support: Bounded,
			Stan: StanNames{Statement: "uniform", Density: "uniform_lpdf", RNG: "uniform_rng",
				LCDF: "uniform_lcdf", LCCDF: "uniform_lccdf"},
			Browser: Exact},

		// The browser draws one correlation: (r + 1) / 2 ~ Beta(eta - 1 + K/2, eta - 1 + K/2).
		{ID: "lkj_corr_cholesky", Uses: uses(Prior),
			Params:  params(Param{Name: "eta", Constraint: Positive, Constant: true}),
			Support: CholeskyCorrelation,
			Stan: StanNames{Statement: "lkj_corr_cholesky", Density: "lkj_corr_cholesky_lpdf",
				RNG: "lkj_corr_cholesky_rng"},
			Browser: Marginal},

		// The centred form of correlated effects; with L = diag(tau) L_omega, component k is
		// normal(mu_k, tau_k) whatever the correlation.
		{ID: "multi_normal_cholesky", Uses: uses(Internal),
			Params:  params(Param{Name: "mu", Constraint: Vector}, Param{Name: "L", Constraint: CholeskyCov}),
			Support: VectorSupport,
			Stan: StanNames{Statement: "multi_normal_cholesky", Density: "multi_normal_cholesky_lpdf",
				RNG: "multi_normal_cholesky_rng"},
			LocationScale: &LocScale{Location: "mu", Scale: "L"},
			Browser:       Marginal},

		// The non-centred form.
		{ID: "std_normal", Uses: uses(Internal),
			Params:  params(),
			Support: RealLine,
			Stan: StanNames{Statement: "std_normal", Density: "std_normal_lpdf", RNG: "std_normal_rng",
				LCDF: "std_normal_lcdf", LCCDF: "std_normal_lccdf"},
			Browser: Exact},

		{ID: "bernoulli_logit", Uses: uses(Likelihood),
			Params:  params(Param{Name: "alpha", Constraint: Real}),
			Support: Binary, Outcome: Binary, Link: "logit",
			Stan: StanNames{Statement: "bernoulli_logit", Density: "bernoulli_logit_lpmf",
				RNG: "bernoulli_logit_rng"},
			Browser: None},

		// Stan 2.40 has no binomial_logit_rng: the generator is binomial_rng(N, inv_logit(alpha)).
		{ID: "binomial_logit", Uses: uses(Likelihood),
			Params: params(Param{Name: "N", Constraint: NonNegInt, Data: true},
				Param{Name: "alpha", Constraint: Real}),
			Support: ZeroToN, Outcome: ZeroToN, Link: "logit",
			Stan: StanNames{Statement: "binomial_logit", Density: "binomial_logit_lpmf",
				RNG: "binomial_rng", RNGInverseLink: "inv_logit"},
			Browser: None},

		// Stan's Poisson generator refuses very large rates; predictive-checks reads the bound.
		{ID: "poisson_log", Uses: uses(Likelihood),
			Params:  params(Param{Name: "alpha", Constraint: Real}),
			Support: Counts, Outcome: Counts, Link: "log",
			Stan:    StanNames{Statement: "poisson_log", Density: "poisson_log_lpmf", RNG: "poisson_log_rng"},
			Browser: None},

		// Stan has no CDF for it: it cannot be truncated.
		{ID: "neg_binomial_2_log", Uses: uses(Likelihood),
			Params:  params(Param{Name: "eta", Constraint: Real}, Param{Name: "phi", Constraint: Positive}),
			Support: Counts, Outcome: Counts, Link: "log",
			Stan: StanNames{Statement: "neg_binomial_2_log", Density: "neg_binomial_2_log_lpmf",
				RNG: "neg_binomial_2_log_rng"},
			Browser: None},
	},

	Language: Language{
		// Stan has no softplus: its name is log1p_exp. lag is treated apart by the language.
		Functions: []Function{
			{Name: "exp", Stan: "exp", Arguments: 1, Domain: "real", Range: "positive"},
			{Name: "log", Stan: "log", Arguments: 1, Domain: "positive", Range: "real"},
			{Name: "log1p", Stan: "log1p", Arguments: 1, Domain: "above_minus_one", Range: "real"},
			{Name: "expm1", Stan: "expm1", Arguments: 1, Domain: "real", Range: "above_minus_one"},
			{Name: "sqrt", Stan: "sqrt", Arguments: 1, Domain: "non_negative", Range: "non_negative"},
			{Name: "square", Stan: "square", Arguments: 1, Domain: "real", Range: "non_negative"},
			{Name: "inv_logit", Stan: "inv_logit", Arguments: 1, Domain: "real", Range: "unit"},
			{Name: "logit", Stan: "logit", Arguments: 1, Domain: "unit", Range: "real"},
			{Name: "log1p_exp", Stan: "log1p_exp", Arguments: 1, Domain: "real", Range: "positive"},
			{Name: "log_inv_logit", Stan: "log_inv_logit", Arguments: 1, Domain: "real", Range: "negative"},
			{Name: "lag", Arguments: 2, Apart: true},
		},
		// Stan's precedence: + - 5 left; * / 4 left; unary - 2; ^ 1 right.
		Operators: []Operator{
			{Symbol: "+", Kind: "binary", Stan: "+", Elementwise: "+", Precedence: 5, Assoc: "left"},
			{Symbol: "-", Kind: "binary", Stan: "-", Elementwise: "-", Precedence: 5, Assoc: "left"},
			{Symbol: "*", Kind: "binary", Stan: "*", Elementwise: ".*", Precedence: 4, Assoc: "left"},
			{Symbol: "/", Kind: "binary", Stan: "/", Elementwise: "./", Precedence: 4, Assoc: "left"},
			{Symbol: "-", Kind: "unary", Stan: "-", Elementwise: "-", Precedence: 2, Assoc: "right"},
			{Symbol: "^", Kind: "binary", Stan: "^", Elementwise: ".^", Precedence: 1, Assoc: "right"},
		},
		// The operators of other languages, as bayes-expression-language.md lists them; one message per
		// group of the list. The messages' ids are the words of the namespace `bayes` in the five
		// languages. The language reads the longest token first ("%/%" before "%", "<-" before "<").
		Hints: []Hint{
			{Token: "**", Message: "bayes.expr.hint.double_star"},
			{Token: "%", Message: "bayes.expr.hint.percent"},
			{Token: "%/%", Message: "bayes.expr.hint.percent"},
			{Token: "=", Message: "bayes.expr.hint.equals"},
			{Token: "==", Message: "bayes.expr.hint.equals"},
			{Token: "<-", Message: "bayes.expr.hint.equals"},
			{Token: ";", Message: "bayes.expr.hint.semicolon"},
			{Token: "~", Message: "bayes.expr.hint.tilde"},
			{Token: "<", Message: "bayes.expr.hint.comparison"},
			{Token: ">", Message: "bayes.expr.hint.comparison"},
			{Token: "!", Message: "bayes.expr.hint.comparison"},
			{Token: "&", Message: "bayes.expr.hint.comparison"},
			{Token: "|", Message: "bayes.expr.hint.comparison"},
			{Token: "?", Message: "bayes.expr.hint.comparison"},
			{Token: ":", Message: "bayes.expr.hint.comparison"},
			{Token: ".*", Message: "bayes.expr.hint.elementwise"},
			{Token: "./", Message: "bayes.expr.hint.elementwise"},
			{Token: ".^", Message: "bayes.expr.hint.elementwise"},
			{Token: "\\", Message: "bayes.expr.hint.backslash"},
			{Token: "{", Message: "bayes.expr.hint.brace"},
			{Token: "}", Message: "bayes.expr.hint.brace"},
		},
		// The look-alikes of bayes-expression-language.md, a short list, never a Unicode-wide table.
		// ASCII "" is an invisible character (to be removed); Direction is a control that changes
		// the direction of the text: its Char is left empty, so that no such character is written in
		// a file of the repository or shown in a message.
		Confusables: []Confusable{
			{CodePoint: "U+2212", Char: "−", ASCII: "-"},
			{CodePoint: "U+2013", Char: "–", ASCII: "-"},
			{CodePoint: "U+2014", Char: "—", ASCII: "-"},
			{CodePoint: "U+00D7", Char: "×", ASCII: "*"},
			{CodePoint: "U+00B7", Char: "·", ASCII: "*"},
			{CodePoint: "U+2219", Char: "∙", ASCII: "*"},
			{CodePoint: "U+22C5", Char: "⋅", ASCII: "*"},
			{CodePoint: "U+00F7", Char: "÷", ASCII: "/"},
			{CodePoint: "U+2215", Char: "∕", ASCII: "/"},
			{CodePoint: "U+00A0", Char: "\u00a0", ASCII: " "},
			{CodePoint: "U+2009", Char: "\u2009", ASCII: " "},
			{CodePoint: "U+202F", Char: "\u202f", ASCII: " "},
			{CodePoint: "U+200B", Char: "\u200b", ASCII: ""},
			{CodePoint: "U+200C", Char: "\u200c", ASCII: ""},
			{CodePoint: "U+200D", Char: "\u200d", ASCII: ""},
			{CodePoint: "U+FEFF", Char: "\ufeff", ASCII: ""},
			{CodePoint: "U+FF10", Char: "０", ASCII: "0"},
			{CodePoint: "U+FF11", Char: "１", ASCII: "1"},
			{CodePoint: "U+FF12", Char: "２", ASCII: "2"},
			{CodePoint: "U+FF13", Char: "３", ASCII: "3"},
			{CodePoint: "U+FF14", Char: "４", ASCII: "4"},
			{CodePoint: "U+FF15", Char: "５", ASCII: "5"},
			{CodePoint: "U+FF16", Char: "６", ASCII: "6"},
			{CodePoint: "U+FF17", Char: "７", ASCII: "7"},
			{CodePoint: "U+FF18", Char: "８", ASCII: "8"},
			{CodePoint: "U+FF19", Char: "９", ASCII: "9"},
			{CodePoint: "U+0430", Char: "а", ASCII: "a"},
			{CodePoint: "U+0435", Char: "е", ASCII: "e"},
			{CodePoint: "U+043E", Char: "о", ASCII: "o"},
			{CodePoint: "U+0440", Char: "р", ASCII: "p"},
			{CodePoint: "U+0441", Char: "с", ASCII: "c"},
			{CodePoint: "U+0445", Char: "х", ASCII: "x"},
			{CodePoint: "U+0443", Char: "у", ASCII: "y"},
			{CodePoint: "U+03BF", Char: "ο", ASCII: "o"},
			{CodePoint: "U+03B1", Char: "α", ASCII: "a"},
			{CodePoint: "U+202A", Direction: true},
			{CodePoint: "U+202B", Direction: true},
			{CodePoint: "U+202C", Direction: true},
			{CodePoint: "U+202D", Direction: true},
			{CodePoint: "U+202E", Direction: true},
			{CodePoint: "U+2066", Direction: true},
			{CodePoint: "U+2067", Direction: true},
			{CodePoint: "U+2068", Direction: true},
			{CodePoint: "U+2069", Direction: true},
		},
	},

	// What stan-codegen may emit beyond the language; it extends this list.
	InternalFunctions: []string{
		"to_vector", "rep_vector", "append_row", "diag_pre_multiply",
		"multiply_lower_tri_self_transpose", "inv_logit", "std_normal",
	},

	Limits: Limits{
		MaxNameBytes: 40, MaxLabelChars: 200, MaxNoteChars: 2000,
		MaxExpressionBytes: 1000, MaxExpressionNesting: 32, MaxExpressionNodes: 256,
		MaxNumberChars: 32, MaxAbsLiteral: 1e15, MaxLag: 60,
		MaxNodes: 200, MaxPlates: 8, MaxPlateDepth: 4, MaxEffectComponents: 10,
		MaxTotalExpressionNode: 4096, MaxGraphBytes: 262144,
	},
}
