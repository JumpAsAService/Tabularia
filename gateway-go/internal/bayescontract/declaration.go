package bayescontract

// This file is what the runner writes, as data: the columns of the draws parquet (always long) and of the outputs, the limits
// with their defaults, the sections of the ten-column table with their shapes and caps, the refusals. testdata/contract.json is
// its JSON (TestTheDeclarationFile), which the runner's tests read; the entry `bayes_model` of the models' catalogue, when it
// comes, declares the same sections, outputs and caps (a test compares them then).

// Column is a column of a parquet the runner writes: its name, its Arrow type, whether it may be null.
type Column struct {
	Name     string `json:"name"`
	Type     string `json:"type"`
	Nullable bool   `json:"nullable"`
	// Dictionary: the strings are dictionary-encoded (the long layout repeats them on every row).
	Dictionary bool `json:"dictionary,omitempty"`
}

// DrawsColumns are the draws parquet's columns, one row a quantity of a draw (owner, 2026-11-23: always long). The sampler's
// fields are quantities named lp__, accept_stat__…; node, unit and the indices are null for them.
var DrawsColumns = []Column{
	{Name: "chain", Type: "Int32"},
	{Name: "draw", Type: "Int32"},
	{Name: "quantity", Type: "String", Dictionary: true},
	{Name: "node", Type: "String", Nullable: true, Dictionary: true},
	{Name: "unit", Type: "String", Nullable: true, Dictionary: true},
	{Name: "index", Type: "Int32", Nullable: true},
	{Name: "index_2", Type: "Int32", Nullable: true},
	{Name: "value", Type: "Float64"},
}

// SummaryColumns are the summary output's columns, one row a quantity (and a second row, scale `original`, for a
// linear-predictor coefficient's pair).
var SummaryColumns = []Column{
	{Name: "quantity", Type: "String"},
	{Name: "node", Type: "String"},
	{Name: "label", Type: "String", Nullable: true},
	{Name: "unit", Type: "String", Nullable: true},
	{Name: "index", Type: "Int32", Nullable: true},
	{Name: "index_2", Type: "Int32", Nullable: true},
	{Name: "kind", Type: "String"},
	{Name: "scale", Type: "String"},
	{Name: "mean", Type: "Float64", Nullable: true},
	{Name: "sd", Type: "Float64", Nullable: true},
	{Name: "q5", Type: "Float64", Nullable: true},
	{Name: "q25", Type: "Float64", Nullable: true},
	{Name: "q50", Type: "Float64", Nullable: true},
	{Name: "q75", Type: "Float64", Nullable: true},
	{Name: "q95", Type: "Float64", Nullable: true},
	{Name: "mcse_mean", Type: "Float64", Nullable: true},
	{Name: "mcse_sd", Type: "Float64", Nullable: true},
	{Name: "rhat", Type: "Float64", Nullable: true},
	{Name: "ess_bulk", Type: "Float64", Nullable: true},
	{Name: "ess_tail", Type: "Float64", Nullable: true},
	// contraction: 1 - Var_post / Var_prior (Betancourt 2018), NULL when the prior's marginal has no finite variance (lot X4).
	{Name: "contraction", Type: "Float64", Nullable: true},
}

// ReplicatedColumns are the replicated output's columns: y_rep for a thinned set of draws, `draw` the thinned draw's number in its
// chain, `row` the kept row (1…N, in the input's order), `outcome` the observed data's Stan identifier (Correction of 2026-11-24 from
// the step-5 specs).
var ReplicatedColumns = []Column{
	{Name: "chain", Type: "Int32"},
	{Name: "draw", Type: "Int32"},
	{Name: "row", Type: "Int32"},
	{Name: "outcome", Type: "String", Dictionary: true},
	{Name: "value", Type: "Float64"},
}

// Kinds of a summary row (its `kind`) and its scales.
var (
	SummaryKinds  = []string{"parameter", "transformed", "state", "effect", "correlation"}
	SummaryScales = []string{"model", "original"}
)

// OutputColumns are the columns of each output, known before any run (the schema does not depend on the model).
var OutputColumns = map[string][]Column{OutputSummary: SummaryColumns, OutputDraws: DrawsColumns, OutputReplicated: ReplicatedColumns}

// Limit is a limit of a run: its key in task.json's `limits`, its default, where it is set.
type Limit struct {
	Key     string `json:"key"`
	Default int64  `json:"default"`
	Where   string `json:"where"` // group (a setting of the group), constant (a test lowers it), catalogue (a cap of the results)
}

// Limits are the group's limits beyond the common ones (time_budget_seconds, memory_bytes, started_unix, max_result_rows,
// max_chart_points), with their defaults (answer 10: every limit a setting with a visible default).
var Limits = []Limit{
	{Key: "tmp_bytes", Default: 2 << 30, Where: "group"},
	{Key: "max_quantities", Default: 20_000, Where: "group"},
	{Key: "max_units", Default: 20_000, Where: "group"},
	{Key: "block_values", Default: 1 << 20, Where: "constant"},
	{Key: "max_check_columns", Default: 200, Where: "constant"},
	{Key: "bayes_max_summary_quantities", Default: 2000, Where: "catalogue"},
	{Key: "bayes_max_quantity_entries", Default: 600, Where: "catalogue"},
	{Key: "bayes_max_chart_quantities", Default: 12, Where: "catalogue"},
	{Key: "bayes_trace_points", Default: 200, Where: "catalogue"},
	{Key: "bayes_rank_bins", Default: 20, Where: "catalogue"},
	{Key: "bayes_density_points", Default: 200, Where: "catalogue"},
	{Key: "bayes_max_unit_charts", Default: 8, Where: "catalogue"},
	{Key: "bayes_unit_points", Default: 1000, Where: "catalogue"},
	{Key: "bayes_max_pair_quantities", Default: 4, Where: "catalogue"},
	{Key: "bayes_pair_points", Default: 300, Where: "catalogue"},
	{Key: "bayes_max_check_rows", Default: 5000, Where: "catalogue"},
}

// LimitDefault is a limit's default, 0 for none.
func LimitDefault(key string) int64 {
	for _, l := range Limits {
		if l.Key == key {
			return l.Default
		}
	}
	return 0
}

// Shape is how a section's rows use level, level_2 and the index (the models' catalogue's `section_shapes`).
type Shape struct {
	Level     string   `json:"level"`   // required | optional | null
	Level2    string   `json:"level_2"` // required | optional | null
	Indexed   bool     `json:"indexed"`
	Unindexed []string `json:"unindexed,omitempty"`
}

// Shapes are the group's new section shapes (the others, model, point, curve, hist, reading, are the catalogue's).
var Shapes = map[string]Shape{
	"summary":     {Level: "required", Level2: "optional"},
	"quantity":    {Level: "required", Level2: "null"},
	"data":        {Level: "required", Level2: "null"},
	"sampler":     {Level: "required", Level2: "null"},
	"ppc":         {Level: "required", Level2: "optional", Indexed: true, Unindexed: []string{"observed", "n"}},
	"prior_check": {Level: "required", Level2: "optional", Indexed: true, Unindexed: []string{"observed", "n"}},
}

// Section is what a variant writes in a section: its levels (nil: any, from the model and the data), its numbers, its texts.
type Section struct {
	Levels  []string `json:"levels"`
	Numbers []string `json:"numbers"`
	Texts   []string `json:"texts"`
}

var (
	modelTexts = []string{"model", "variant", "rows_from", "group_version", "software", "bayes_model", "bayes_version",
		"semantic_hash", "program_sha256", "binary_sha256", "sampler", "metric", "precision", "note_*"}
	modelNumbers = []string{"n", "n_missing", "n_not_finite", "chains", "warmup", "draws", "seed", "adapt_delta",
		"max_treedepth", "threads", "n_quantities", "n_in_summary", "seconds_data", "seconds_warmup", "seconds_sampling",
		"seconds_conversion", "seconds_pass_2", "seconds_checks", "seconds_replicated"}
	quantitySection = Section{Numbers: []string{"n_quantities", "rhat_max", "ess_bulk_min", "ess_tail_min"},
		Texts: []string{"label", "name", "node_id", "kind", "plate", "plate_label"}}
	summaryNumbers = []string{"mean", "sd", "q5", "q25", "q50", "q75", "q95", "mcse_mean", "mcse_sd", "rhat", "ess_bulk",
		"ess_tail", "contraction", "orig_mean", "orig_sd", "orig_q5", "orig_q50", "orig_q95"}
	dataSection = Section{Numbers: []string{"mean", "sd", "n_units", "n_levels", "n_missing"},
		Texts: []string{"column", "type", "reference", "standardized"}}
	samplerSection = Section{Numbers: []string{"divergences", "treedepth_hits", "treedepth_max", "e_bfmi", "stepsize",
		"accept_stat_mean", "n_leapfrog_mean", "seconds_warmup", "seconds_sampling", "draws"}}
	checkSection = Section{Numbers: []string{"observed", "count", "n"}, Texts: []string{}}
	chartSection = Section{Numbers: []string{"y", "value", "count"}, Texts: []string{}}
	pointSection = Section{Numbers: []string{"y"}, Texts: []string{"unit"}}
	// the layout's rows (report-explanations.md): blocks and lines are bayes-report's
	readingSection = Section{Texts: []string{"line_*"}}
)

// Variants are the sections each variant writes; `reading` is written by the layout, never by the runner, and declared so that
// the check accepts its rows.
var Variants = map[string]map[string]Section{
	ModeSample: {
		"model":    {Numbers: modelNumbers, Texts: modelTexts},
		"quantity": quantitySection,
		"summary":  {Numbers: summaryNumbers, Texts: []string{}},
		"data":     dataSection,
		"sampler":  samplerSection,
		"ppc":      checkSection,
		"point":    pointSection,
		"curve":    chartSection,
		"hist":     chartSection,
		"reading":  readingSection,
	},
	ModePriorPredictive: {
		"model":       {Numbers: modelNumbers, Texts: modelTexts},
		"quantity":    quantitySection,
		"summary":     {Numbers: []string{"mean", "sd", "q5", "q25", "q50", "q75", "q95"}, Texts: []string{}},
		"data":        dataSection,
		"prior_check": checkSection,
		"curve":       chartSection,
		"reading":     readingSection,
	},
}

// Rows per item of the ten-column table, for the arithmetic of the caps.
const (
	rowsPerSummary      = 13 // mean, sd, five quantiles, two MCSE, R-hat, two ESS, the contraction
	rowsOfOriginalScale = 5  // a linear-predictor coefficient's pair adds the five numbers of the original scale
	rowsPerQuantityItem = 10 // six texts, four numbers
	rowsPerDataItem     = 8  // four texts, four numbers (n_units or n_levels)
	rowsPerSamplerLevel = 10
	rowsOfModelSection  = 80 // its texts and numbers (31) and the notes, with room
	densitySeries       = 2  // posterior, prior
	unitSeries          = 3  // median, low, high
)

// MaxResultRows is the most rows the ten-column table can hold at the most chains a node allows, whatever the model: the
// arithmetic of «The report's scope and the caps», which must stay under the catalogue's max_result_rows. maxNodes is the
// graph's limit (bayescatalog's max_nodes): the data section has at most one entry a node.
func MaxResultRows(maxNodes int) int64 {
	c := func(k string) int64 { return LimitDefault(k) }
	chains := int64(MaxChains)
	// The original-scale numbers belong to a linear-predictor coefficient, a component of an effect, which is an entry of
	// the `quantity` section: at most bayes_max_quantity_entries of them. (Every quantity counted with them, as the 17 rows
	// of the first count did, would pass 100,000 with the contraction.)
	summary := c("bayes_max_summary_quantities")*rowsPerSummary + c("bayes_max_quantity_entries")*rowsOfOriginalScale
	pairs := int64(0)
	if n := c("bayes_max_pair_quantities"); n > 1 {
		pairs = n * (n - 1) / 2 * c("bayes_pair_points")
	}
	charts := c("bayes_max_chart_quantities") * (chains*c("bayes_trace_points") + chains*c("bayes_rank_bins") +
		densitySeries*c("bayes_density_points"))
	units := c("bayes_max_unit_charts") * unitSeries * c("bayes_unit_points")
	quantities := c("bayes_max_quantity_entries") * rowsPerQuantityItem
	data := int64(maxNodes) * rowsPerDataItem
	sampler := (chains + 1) * rowsPerSamplerLevel
	return summary + charts + pairs + units + quantities + data + sampler + rowsOfModelSection + c("bayes_max_check_rows")
}

// Refusal is a refusal of the runner: its code, the exit code, the modes it happens in. The sentences are the runner's (they
// name the way out and what they name travels in params, as data).
type Refusal struct {
	Code  string   `json:"code"`
	Exit  int      `json:"exit"`
	Modes []string `json:"modes"`
}

var sampling = []string{ModePriorPredictive, ModeSample}

// Refusals are the runner's refusals («Refusals and errors»): exit 3, no partial result. A failed self-test exits 4; anything
// else (exit 1, a program and its manifest that disagree among them) is a fault of the runner.
var Refusals = []Refusal{
	{Code: "protocol", Exit: 3, Modes: Modes},
	{Code: "no_task", Exit: 3, Modes: Modes},
	{Code: "mode", Exit: 3, Modes: Modes},
	{Code: "task_value", Exit: 3, Modes: Modes},
	{Code: "program_hash", Exit: 3, Modes: []string{ModeCompile}},
	{Code: "compile_failed", Exit: 3, Modes: []string{ModeCompile}},
	{Code: "binary_hash", Exit: 3, Modes: sampling},
	{Code: "data_empty", Exit: 3, Modes: sampling},
	{Code: "data_shape", Exit: 3, Modes: sampling},
	{Code: "data_constant", Exit: 3, Modes: sampling},
	{Code: "data_unit_varies", Exit: 3, Modes: sampling},
	{Code: "data_nesting", Exit: 3, Modes: sampling},
	{Code: "data_order", Exit: 3, Modes: sampling},
	{Code: "data_time_twice", Exit: 3, Modes: sampling},
	{Code: "too_many_units", Exit: 3, Modes: sampling},
	{Code: "too_many_quantities", Exit: 3, Modes: sampling},
	{Code: "too_slow", Exit: 3, Modes: sampling},
	{Code: "too_much_disk", Exit: 3, Modes: sampling},
	{Code: "gq_reread", Exit: 3, Modes: []string{ModeSample}},
	{Code: "init_failed", Exit: 3, Modes: sampling},
	{Code: "sampler_failed", Exit: 3, Modes: sampling},
	{Code: "not_finite", Exit: 3, Modes: sampling},
}

// Exit codes of a Job of the group.
const (
	ExitOK            = 0
	ExitBug           = 1
	ExitRefused       = 3
	ExitSelftestFails = 4
)
