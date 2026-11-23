package bayescontract

// Card is the `bayes` object of a run's model.json («The card»), beside tab_card's common keys: what a run of a saved model
// is, as data, for MLflow (bayes-mlflow reads it). It never holds a draw (answer 14); Summary holds the scalar quantities only
// and Units the labels of each plate, both cut first when the card would pass max_card_bytes.
type Card struct {
	Model   CardModel             `json:"model"`
	Program CardProgram           `json:"program"`
	Binary  CardBinary            `json:"binary"`
	Mapping []CardMapping         `json:"mapping"`
	Data    []CardData            `json:"data"`
	Units   map[string]CardUnits  `json:"units"`
	Sampler CardSampler           `json:"sampler"`
	Facts   CardFacts             `json:"facts"`
	Summary []CardSummaryQuantity `json:"summary"`
}

// CardModel is the saved model's version.
type CardModel struct {
	ID             int64  `json:"id"`
	Version        int    `json:"version"`
	SemanticHash   string `json:"semantic_hash"`
	CodegenVersion int    `json:"codegen_version"`
}

// CardProgram is the program's hash and size (its text is an artifact of bayes-mlflow, never in the card).
type CardProgram struct {
	SHA256 string `json:"sha256"`
	Bytes  int64  `json:"bytes"`
}

// CardBinary is the compiled model: its hash, CmdStan's version, the image's digest.
type CardBinary struct {
	SHA256  string `json:"sha256"`
	CmdStan string `json:"cmdstan"`
	Image   string `json:"image"`
}

// CardMapping is a quantity's identifier and what it stands for.
type CardMapping struct {
	Stan   string  `json:"stan"`
	NodeID string  `json:"node_id"`
	Name   *string `json:"name"`
	Label  *string `json:"label"`
	Kind   string  `json:"kind"`
}

// CardData is a bound data variable: its column and its standardization.
type CardData struct {
	Stan         string   `json:"stan"`
	Column       string   `json:"column"`
	Standardized bool     `json:"standardized"`
	Mean         *float64 `json:"mean,omitempty"`
	SD           *float64 `json:"sd,omitempty"`
}

// CardUnits are a plate's units: their count and their labels (data).
type CardUnits struct {
	Count  int      `json:"count"`
	Labels []string `json:"labels"`
}

// CardSampler is the sampler's settings as run.
type CardSampler struct {
	Algorithm    string  `json:"algorithm"` // nuts, or fixed_param
	Chains       int     `json:"chains"`
	Warmup       int     `json:"warmup"`
	Draws        int     `json:"draws"`
	Seed         int64   `json:"seed"`
	AdaptDelta   float64 `json:"adapt_delta"`
	MaxTreedepth int     `json:"max_treedepth"`
}

// CardFacts are the sampler's facts over the chains (facts, never verdicts).
type CardFacts struct {
	Divergences   int      `json:"divergences"`
	TreedepthHits int      `json:"treedepth_hits"`
	EBFMIMin      *float64 `json:"e_bfmi_min"`
}

// CardSummaryQuantity is a scalar quantity's summary.
type CardSummaryQuantity struct {
	Quantity string   `json:"quantity"`
	Mean     *float64 `json:"mean"`
	SD       *float64 `json:"sd"`
	Q5       *float64 `json:"q5"`
	Q50      *float64 `json:"q50"`
	Q95      *float64 `json:"q95"`
	Rhat     *float64 `json:"rhat"`
	ESSBulk  *float64 `json:"ess_bulk"`
	ESSTail  *float64 `json:"ess_tail"`
}
