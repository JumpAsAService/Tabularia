package bayescontract

// This file is the program's manifests (docs/specs/bayes-contract.md, «The data manifest», «The quantity manifest»): what
// stan-codegen writes beside a program and what the runner reads instead of the graph. Their JSON is stored with each version
// of a saved model (bayes_model_versions.data_manifest, quantity_manifest, mapping) and travels in task.json's `bayes` object,
// so a change of a tag or of an omitempty changes stored bytes and every golden manifest of stan-codegen.

// DataEntry is one variable of the program's data block, in the block's order, as the runner writes it into data.json. Column is
// the input column the flow node binds (answer 6): stan-codegen does not know it and leaves it empty; flow-node fills it from the
// binding of Node when it writes the task, for the kinds that read a column (DataReadsColumn).
type DataEntry struct {
	Stan        string `json:"stan"`
	Kind        string `json:"kind"`
	Of          string `json:"of,omitempty"`
	Plate       string `json:"plate,omitempty"`
	Column      string `json:"column,omitempty"`
	Node        string `json:"node,omitempty"`
	Standardize bool   `json:"standardize,omitempty"`
	Target      string `json:"target,omitempty"`
	Support     string `json:"support,omitempty"`
	// Lag is k of Lag<k>: the row k steps before, within the series' unit.
	Lag int `json:"lag,omitempty"`
}

// Kinds of a data entry.
const (
	// DataSize is an integer size: Of names what it counts, the rows plate (N), a group plate (J_<plate>), a category's
	// levels plate (K_<category>, the number of levels), or the array of a series it sizes (N_lag of Rows_lag, N_first of
	// Rows_first, N_next of Rows_next).
	DataSize = "size"
	// DataReal and DataInt are a column's values on the rows plate (or on a group plate, one value a unit); an integer has a
	// Support (SupportAny, SupportNonnegative, SupportBinary). Standardize: the runner centres and scales it (answer 6).
	DataReal = "real"
	DataInt  = "int"
	// DataGroupIndex is a text column read as the units of the group plate Target, 1…J in the units' text order.
	DataGroupIndex = "group_index"
	// DataCategory is a text column read as a category's levels, 1…K in the levels' text order, the first the reference.
	DataCategory = "category"
	// DataLift is an array over the group plate Plate: for each of its units, the unit of the enclosing plate Target, read from
	// the group index Node (Up_<index>_<plate>, model-graph's Model.Lifts).
	DataLift = "lift"
	// DataMode is the flag Mode: 0 the posterior (sample), 1 the prior predictive.
	DataMode = "mode"
	// A series: the rows that have every lag (DataRowsSubset, Rows_lag) and, for each of them, the row Lag steps before within
	// the series' unit (DataLag, Lag<k>); a latent state's first row of each unit (DataFirst, Rows_first), its other rows
	// (DataNext, Rows_next) and the row before each of these (DataPrevious, Prev). All on the rows plate, as row numbers
	// 1…N of the kept rows.
	DataRowsSubset = "rows_subset"
	DataLag        = "lag"
	DataFirst      = "first"
	DataNext       = "next"
	DataPrevious   = "previous"
)

// Supports of an integer data entry.
const (
	SupportAny         = "any"
	SupportNonnegative = "nonnegative"
	SupportBinary      = "binary"
)

// Quantity is one variable the program writes, in the order of the CSV's columns. Dims is never null: [] for a single value.
type Quantity struct {
	Stan     string `json:"stan"`
	Role     string `json:"role"`
	Node     string `json:"node,omitempty"`
	Dims     []Dim  `json:"dims"`
	Triangle string `json:"triangle,omitempty"`
	Check    string `json:"check,omitempty"`
	Of       string `json:"of,omitempty"`
}

// Dim is one dimension of a written variable, exactly one of its fields set: over a plate's units (the rows plate, a group
// plate, or a category's levels plate `levels:<category>`, which has K - 1 elements, the levels after the reference), over a
// size (a data entry of kind DataSize or an integer of the extension's data), or over the components of correlated effects
// (the node's id: its components in the mapping's order).
type Dim struct {
	Plate     string `json:"plate,omitempty"`
	Size      string `json:"size,omitempty"`
	Component string `json:"component,omitempty"`
}

// Roles of a written variable.
const (
	// RoleQuantity is a node of the graph (a parameter, a transformed node, a linear predictor, a state, a component, a
	// correlation): in the draws, the summary and the report.
	RoleQuantity = "quantity"
	// RoleInternal is the program's own (Z_, L_, V_ of a non-centred or centred form): read from the CSV and dropped.
	RoleInternal = "internal"
	// RoleCheck is a statistic of predictive-checks (Check names it): to the checks file, never to the draws.
	RoleCheck = "check"
	// RolePriorDraw is the prior predictive's ancestral draw of the quantity Of: it replaces Of's own columns in
	// prior_predictive; in sample it is read and dropped (with a dimension it has no element there: its size is by the mode).
	RolePriorDraw = "prior_draw"
)

// TriangleStrictLower is a correlation matrix's triangle: only the elements below the diagonal are quantities.
const TriangleStrictLower = "strict_lower"

// MappingEntry is one identifier the program declares (the mapping table): what it stands for and why it is not the person's
// name when it is not. Labels live here, never in the program.
type MappingEntry struct {
	Stan      string  `json:"stan"`
	Block     string  `json:"block"`
	NodeID    *string `json:"node_id"`
	Component *string `json:"component"`
	Name      *string `json:"name"`
	Label     *string `json:"label"`
	Kind      string  `json:"kind"`
	Plate     *string `json:"plate"`
	Role      *string `json:"role"`
	Renamed   *string `json:"renamed"`
	// Word is the word that made a reserved name renamed (the name itself, or the suffix).
	Word string `json:"word,omitempty"`
}

// Kinds of a mapping entry beyond the node kinds (parameter, transformed, linear_predictor, data, state): a size (N, J_<plate>,
// K_<category>, N_lag, N_first, N_next), the mode flag, a lifted index (Up_<index>_<plate>), the derived data of a series
// (Rows_lag, Lag<k>, Rows_first, Rows_next, Prev), a component of correlated effects and their correlation matrix, an identifier
// the program declares for a node of its own (Z_<id> standard normals, L_<id> a Cholesky factor, W_<id> the scaled effects,
// V_<id> the centred effects, Full_<id> a category's coefficients with the reference's 0, Arg<k>_<id> an argument of a truncated
// prior over a plate, E_<id>_<k> a part of a long expression), and what an extension declares.
const (
	KindSize        = "size"
	KindMode        = "mode"
	KindLift        = "lift"
	KindSeries      = "series"
	KindComponent   = "component"
	KindCorrelation = "correlation"
	KindInternal    = "internal"
	KindExtension   = "extension"
)

// Node kinds of a mapping entry (model-graph's node kinds as stan-codegen writes them).
const (
	KindParameter       = "parameter"
	KindTransformed     = "transformed"
	KindLinearPredictor = "linear_predictor"
	KindData            = "data"
	KindState           = "state"
)
