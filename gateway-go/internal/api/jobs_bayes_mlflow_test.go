package api

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
)

// The pure functions of a Bayesian run's MLflow record (docs/specs/bayes-mlflow.md, lot M0): the parameters, the metrics, the
// tags, the summary table and the program's check, over the report spec's fixtures and over made-up rows for the caps and the
// hostile names. Nothing here needs a database, a cluster or an MLflow server.

// bayesFixtureRows reads the rows of one of the report spec's fixtures (backend/tests/fixtures/models), as the gateway reads
// them back from the engine.
func bayesFixtureRows(t *testing.T, variant string) []resultRow {
	t.Helper()
	b, err := os.ReadFile("../../../backend/tests/fixtures/models/bayes_model-" + variant + ".json")
	if err != nil {
		t.Fatal(err)
	}
	var f struct {
		Columns []string `json:"columns"`
		Rows    [][]any  `json:"rows"`
	}
	if err := json.Unmarshal(b, &f); err != nil {
		t.Fatal(err)
	}
	at := map[string]int{}
	for i, c := range f.Columns {
		at[c] = i
	}
	text := func(r []any, c string) *string {
		if s, ok := r[at[c]].(string); ok {
			return &s
		}
		return nil
	}
	number := func(r []any, c string) *float64 {
		if v, ok := r[at[c]].(float64); ok {
			return &v
		}
		return nil
	}
	var out []resultRow
	for _, r := range f.Rows {
		out = append(out, resultRow{Section: *text(r, "section"), Level: text(r, "level"), Level2: text(r, "level_2"),
			Stat: *text(r, "stat"), Value: number(r, "value"), Text: text(r, "text"), X0: number(r, "x0")})
	}
	return out
}

func brow(section, level, level2, stat string, v float64) resultRow {
	r := resultRow{Section: section, Stat: stat, Value: &v}
	if level != "" {
		r.Level = &level
	}
	if level2 != "" {
		r.Level2 = &level2
	}
	return r
}

func btext(section, level, stat, text string) resultRow {
	r := resultRow{Section: section, Stat: stat, Text: &text}
	if level != "" {
		r.Level = &level
	}
	return r
}

func metricsByKey(ms []mlflowconn.Metric) map[string]float64 {
	out := map[string]float64{}
	for _, m := range ms {
		out[m.Key] = m.Value
	}
	return out
}

func paramsByKey(ps []mlflowconn.Tag) map[string]string {
	out := map[string]string{}
	for _, p := range ps {
		out[p.Key] = p.Value
	}
	return out
}

var bayesMetricTime = time.Date(2026, 11, 24, 10, 0, 0, 0, time.UTC)

func TestTheMetricsOfABayesianRunAreTheSummaryTheFactsAndTheCounts(t *testing.T) {
	rows := bayesFixtureRows(t, "sample")
	ms, warnings := bayesMetrics(rows, bayesMetricTime)
	if len(warnings) != 0 {
		t.Errorf("warnings: %v", warnings)
	}
	got := metricsByKey(ms)
	for _, c := range []struct {
		key  string
		want float64
	}{
		{"sampler.divergences", 2}, {"sampler.treedepth_hits", 0}, {"sampler.e_bfmi_min", 0.88}, {"sampler.stepsize", 0.34},
		{"run.n", 588}, {"run.n_missing", 12}, {"run.n_not_finite", 0}, {"run.seconds_warmup", 15}, {"run.seconds_sampling", 18},
	} {
		if v, ok := got[c.key]; !ok || v != c.want {
			t.Errorf("%s = %v (%v), want %v", c.key, v, ok, c.want)
		}
	}
	for _, key := range []string{"summary.mu_a.mean", "summary.mu_a.sd", "summary.mu_a.q5", "summary.mu_a.q50", "summary.mu_a.q95",
		"summary.mu_a.rhat", "summary.mu_a.ess_bulk", "summary.mu_a.ess_tail", "summary.mu_a.mcse_mean",
		"quantity.mu_a.rhat_max", "quantity.alpha_school.ess_bulk_min", "quantity.rho_g.ess_tail_min", "sampler.accept_stat_mean"} {
		if _, ok := got[key]; !ok {
			t.Errorf("no metric %s", key)
		}
	}
	// a quantity of a plate (a unit), a correlation, the chart rows and the settings are never metrics
	for key := range got {
		for _, never := range []string{"summary.alpha_school", "summary.rho_g", "curve", "hist", "point", "ppc", "prior_check", "trace", "rank"} {
			if strings.HasPrefix(key, never) {
				t.Errorf("metric %q", key)
			}
		}
		switch key {
		case "chains", "warmup", "draws", "seed", "adapt_delta", "max_treedepth", "threads", "run.chains", "run.seed":
			t.Errorf("metric %q is a parameter", key)
		}
		if len(key) > mlflowconn.MaxKeyLength || metricKey(key) != key {
			t.Errorf("key %q is not one MLflow takes", key)
		}
	}
	for _, m := range ms {
		if m.Timestamp != bayesMetricTime.UnixMilli() || m.Step != 0 {
			t.Errorf("%s at %d step %d", m.Key, m.Timestamp, m.Step)
		}
	}
	// no verdict in any word of a key
	for key := range got {
		for _, verdict := range []string{"converged", "passed", "threshold", "ok", "fail"} {
			if strings.Contains(key, verdict) {
				t.Errorf("metric %q holds a verdict word", key)
			}
		}
	}
}

func TestThePriorPredictiveSummaryLogsNoSamplerFact(t *testing.T) {
	ms, _ := bayesMetrics(bayesFixtureRows(t, "prior_predictive"), bayesMetricTime)
	got := metricsByKey(ms)
	for key := range got {
		if strings.HasPrefix(key, "sampler.") || strings.HasSuffix(key, ".rhat") || strings.HasPrefix(key, "quantity.") {
			t.Errorf("metric %q in a run with no diagnostics", key)
		}
	}
	if got["summary.mu_a.mean"] == 0 || got["run.n"] != 588 {
		t.Errorf("metrics: %v", got)
	}
}

// Only the five sections are read: rows of the charts and of the draws give nothing.
func TestNoDrawIsEverAMetricOrACell(t *testing.T) {
	rows := bayesFixtureRows(t, "sample")
	var draws []resultRow
	seen := map[string]bool{}
	var traceValues []string
	for _, r := range rows {
		switch r.Section {
		case "curve", "hist", "point", "ppc", "prior_check":
			draws = append(draws, r)
			// a `point` is a unit's median, a summary of the draws and also in the table: only the draws' own rows are scanned
			if r.Value != nil && r.Section != "point" {
				if s := strconv.FormatFloat(*r.Value, 'g', -1, 64); len(s) >= 8 && !seen[s] {
					seen[s] = true
					traceValues = append(traceValues, s)
				}
			}
		}
	}
	if len(draws) < 1000 || len(traceValues) < 500 {
		t.Fatalf("the fixture holds %d chart rows and %d long values", len(draws), len(traceValues))
	}
	if ms, w := bayesMetrics(draws, bayesMetricTime); len(ms) != 0 || len(w) != 0 {
		t.Errorf("the chart rows alone gave %d metrics", len(ms))
	}
	if csvOf := bayesSummaryCSV(draws); csvOf != nil {
		t.Errorf("the chart rows alone gave a table: %s", csvOf)
	}
	step := &bayesStep{modelStep: &modelStep{output: "draws"}, version: &bayesmodels.Version{}}
	if ps, _ := bayesParams(step, draws); len(paramsByKey(ps)) > 10 {
		t.Errorf("the chart rows alone gave parameters: %v", ps)
	}
	table := string(bayesSummaryCSV(rows))
	for _, v := range traceValues {
		if strings.Contains(table, v) {
			t.Errorf("the table holds the draw value %s", v)
		}
	}
	// the card has no field for a draw: its JSON keys are all fixed ones
	b, _ := json.Marshal(bayescontract.Card{})
	for _, never := range []string{"trace", "replicated", "chains\":["} {
		if strings.Contains(string(b), never) {
			t.Errorf("the card has %s", never)
		}
	}
}

func TestTheSummaryTableHoldsEachQuantityAndItsUnits(t *testing.T) {
	table := bayesSummaryCSV(bayesFixtureRows(t, "sample"))
	recs, err := csv.NewReader(strings.NewReader(string(table))).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	header := strings.Join(recs[0], ",")
	if header != "quantity,unit,mean,sd,q5,q50,q95,rhat,ess_bulk,ess_tail,mcse_mean,orig_mean,orig_sd,orig_q5,orig_q50,orig_q95" {
		t.Errorf("header: %s", header)
	}
	// 4 scalars, 1 correlation, 6 units of the plate
	if len(recs) != 1+4+1+6 {
		t.Errorf("%d rows", len(recs)-1)
	}
	var withOriginal int
	units := map[string]bool{}
	for _, r := range recs[1:] {
		if r[0] == "alpha_school" {
			units[r[1]] = true
		}
		if r[11] != "" {
			withOriginal++
		}
		if r[2] == "" || r[7] == "" {
			t.Errorf("a row without a mean or R-hat: %v", r)
		}
	}
	if len(units) != 6 || withOriginal != 1 {
		t.Errorf("%d units, %d rows on the original scale", len(units), withOriginal)
	}
	// a prior predictive summary has no diagnostics and no original scale: empty cells, and the columns it has no use for are gone
	prior, _ := csv.NewReader(strings.NewReader(string(bayesSummaryCSV(bayesFixtureRows(t, "prior_predictive"))))).ReadAll()
	for _, r := range prior[1:] {
		if r[7] != "" || r[8] != "" {
			t.Errorf("a diagnostic in a prior predictive row: %v", r)
		}
	}
	if len(prior[0]) != 11 {
		t.Errorf("prior header: %v", prior[0])
	}
	if bayesSummaryCSV(nil) != nil {
		t.Error("a table with no summary")
	}
}

func TestTheCapsOfTheMetricsAndOfTheTable(t *testing.T) {
	var rows []resultRow
	for i := 0; i < 2100; i++ {
		q := fmt.Sprintf("q%04d", i)
		for _, s := range bayesSummaryStats {
			rows = append(rows, brow("summary", q, "", s, float64(i)+0.5))
		}
	}
	for i := 0; i < 150; i++ {
		q := fmt.Sprintf("node%03d", i)
		for _, s := range bayesNodeExtremes {
			rows = append(rows, brow("quantity", q, "", s, 1.5))
		}
	}
	rows = append(rows, brow("sampler", "all", "", "divergences", 0), brow("model", "", "", "n", 10))
	ms, w := bayesMetrics(rows, bayesMetricTime)
	if len(w) != 0 {
		t.Errorf("warnings: %v", w)
	}
	counts := map[string]int{}
	for _, m := range ms {
		counts[strings.SplitN(m.Key, ".", 2)[0]]++
	}
	if counts["summary"] != 100*len(bayesSummaryStats) || counts["quantity"] != 100*3 || counts["sampler"] != 1 || counts["run"] != 1 {
		t.Errorf("counts: %v", counts)
	}
	// the first 100 in the report's order, the others only in the table
	got := metricsByKey(ms)
	if _, ok := got["summary.q0099.mean"]; !ok {
		t.Error("the hundredth quantity is missing")
	}
	if _, ok := got["summary.q0100.mean"]; ok {
		t.Error("the 101st quantity is a metric")
	}
	recs, err := csv.NewReader(strings.NewReader(string(bayesSummaryCSV(rows)))).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1+bayesMLflowCSVQuantities {
		t.Errorf("%d table rows", len(recs)-1)
	}
	// every entry fits the batches: the call count follows MLflow's limits
	for _, b := range mlflowconn.Batches(nil, ms, nil) {
		if len(b.Metrics) > mlflowconn.MaxBatchEntries {
			t.Errorf("a batch of %d metrics", len(b.Metrics))
		}
	}
}

func TestHostileNamesAreValidKeysApartAndText(t *testing.T) {
	hostile := `=HYPERLINK("http://x","y") #set ] 1% <b>`
	long := strings.Repeat("long name ", 30)
	names := []string{hostile, "a?b", "a_b", long, long + "x", "-minus", "@at", "+plus", "alpha[Liceo Galilei]"}
	var rows []resultRow
	for _, n := range names {
		rows = append(rows, brow("summary", n, "", "mean", 1), brow("summary", n, "", "rhat", 1))
	}
	rows = append(rows, brow("summary", "plate", "=cmd|' /C calc'!A0", "mean", 3))
	ms, w := bayesMetrics(rows, bayesMetricTime)
	seen := map[string]bool{}
	for _, m := range ms {
		if len(m.Key) > mlflowconn.MaxKeyLength || metricKey(m.Key) != m.Key {
			t.Errorf("key %q", m.Key)
		}
		if seen[m.Key] {
			t.Errorf("key %q twice", m.Key)
		}
		seen[m.Key] = true
	}
	if len(ms) != 2*len(names) {
		t.Errorf("%d metrics", len(ms))
	}
	if len(w) != 1 || !strings.Contains(w[0], "had the same name in MLflow") {
		t.Errorf("warnings: %v", w)
	}
	recs, err := csv.NewReader(strings.NewReader(string(bayesSummaryCSV(rows)))).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	cells := map[string]bool{}
	for _, r := range recs[1:] {
		cells[r[0]+"|"+r[1]] = true
		for _, c := range r[:2] {
			if c != "" && strings.ContainsRune("=+-@\t\r", rune(c[0])) {
				t.Errorf("cell %q is a formula", c)
			}
		}
	}
	for _, want := range []string{"'" + hostile + "|", "'-minus|", "'@at|", "'+plus|", "a?b|", "plate|'=cmd|' /C calc'!A0"} {
		if !cells[want] {
			t.Errorf("no cell %q in %v", want, cells)
		}
	}
	// the numbers of a table are numbers, even the negative ones
	neg := bayesSummaryCSV([]resultRow{brow("summary", "x", "", "mean", -0.25)})
	if !strings.Contains(string(neg), ",-0.25,") {
		t.Errorf("table: %s", neg)
	}
}

func bayesTestStep(output string) *bayesStep {
	radius := 0.5
	return &bayesStep{modelStep: &modelStep{output: output}, version: &bayesmodels.Version{ModelID: 17, Version: 4},
		sampler:    bayescontract.Sampler{Chains: 4, Warmup: 500, Draws: 250, Seed: 20261123, AdaptDelta: 0.8, MaxTreedepth: 10, Init: &bayescontract.Init{Radius: &radius}},
		replicated: &bayescontract.Replicated{DrawsPerChain: 50}}
}

func TestTheParametersOfABayesianRun(t *testing.T) {
	ps, w := bayesParams(bayesTestStep("summary"), bayesFixtureRows(t, "sample"))
	if len(w) != 0 {
		t.Errorf("warnings: %v", w)
	}
	got := paramsByKey(ps)
	for k, want := range map[string]string{
		"model.id": "17", "model.version": "4", "sampler.chains": "4", "sampler.warmup": "500", "sampler.draws": "250",
		"sampler.seed": "20261123", "sampler.adapt_delta": "0.8", "sampler.max_treedepth": "10", "sampler.init": "radius 0.5",
		"output": "summary", "data.score.column": "exam_score", "data.score.standardized": "false",
		"data.hours.column": "hours_studied", "data.hours.standardized": "true", "data.hours.mean": "6.21", "data.hours.sd": "2.4",
	} {
		if got[k] != want {
			t.Errorf("%s = %q, want %q", k, got[k], want)
		}
	}
	if _, ok := got["replicated_draws"]; ok {
		t.Error("replicated_draws with the output summary")
	}
	if _, ok := got["data.score.mean"]; ok {
		t.Error("a mean for a variable that is not standardized")
	}
	for i := 1; i < len(ps); i++ {
		if ps[i-1].Key >= ps[i].Key {
			t.Errorf("not sorted: %s, %s", ps[i-1].Key, ps[i].Key)
		}
	}
	// the size data (no column) are no binding
	for k := range got {
		if strings.HasPrefix(k, "data.") && strings.HasSuffix(k, ".type") {
			t.Errorf("parameter %s", k)
		}
	}
	rep, _ := bayesParams(bayesTestStep("replicated"), nil)
	if v := paramsByKey(rep)["replicated_draws"]; v != "50" {
		t.Errorf("replicated_draws = %q", v)
	}
	none := bayesTestStep("draws")
	none.sampler.Init = nil
	if v := paramsByKey(first(bayesParams(none, nil)))["sampler.init"]; v != "default" {
		t.Errorf("init = %q", v)
	}
	if bayesInitText(&bayescontract.Init{Values: map[string]bayescontract.InitValue{}}) != "values" {
		t.Error("init values")
	}
}

func first(ps []mlflowconn.Tag, _ []string) []mlflowconn.Tag { return ps }

func TestTheParametersOfManyDataVariablesStayInBatchesAndApart(t *testing.T) {
	var rows []resultRow
	for i := 0; i < 200; i++ {
		v := fmt.Sprintf("v%03d", i)
		rows = append(rows, btext("data", v, "column", "col"+v), btext("data", v, "standardized", "yes"),
			brow("data", v, "", "mean", 1), brow("data", v, "", "sd", 2))
	}
	hostile := `x]"y z`
	rows = append(rows, btext("data", hostile, "column", "c1"), btext("data", "x?y", "column", "c2"), btext("data", "x_y", "column", "c3"))
	long := strings.Repeat("n", 300)
	rows = append(rows, btext("data", long, "column", "c4"))
	ps, w := bayesParams(bayesTestStep("summary"), rows)
	seen := map[string]bool{}
	for _, p := range ps {
		if len(p.Key) > mlflowconn.MaxKeyLength || metricKey(p.Key) != p.Key || seen[p.Key] {
			t.Errorf("key %q", p.Key)
		}
		seen[p.Key] = true
	}
	if len(w) != 1 {
		t.Errorf("warnings: %v", w)
	}
	batches := mlflowconn.Batches(ps, nil, nil)
	total := 0
	for _, b := range batches {
		if len(b.Params) > mlflowconn.MaxBatchParams {
			t.Errorf("a batch of %d parameters", len(b.Params))
		}
		total += len(b.Params)
	}
	if total != len(ps) || len(ps) < 800 {
		t.Errorf("%d parameters in batches of %d", len(ps), total)
	}
}

func TestTheTagsOfABayesianRun(t *testing.T) {
	card := &bayescontract.Card{
		Model:   bayescontract.CardModel{ID: 17, Version: 4, SemanticHash: "abc"},
		Program: bayescontract.CardProgram{SHA256: "p1"}, Binary: bayescontract.CardBinary{SHA256: "b1", CmdStan: "2.40.0"},
		Sampler: bayescontract.CardSampler{Algorithm: "nuts"},
	}
	got := paramsByKey(bayesTags(card, "School scores"))
	for k, want := range map[string]string{"tabularia.bayes_model_id": "17", "tabularia.bayes_model_version": "4",
		"tabularia.bayes_model_name": "School scores", "tabularia.bayes_semantic_hash": "abc", "tabularia.program_sha256": "p1",
		"tabularia.binary_sha256": "b1", "tabularia.cmdstan": "2.40.0", "tabularia.sampler": "nuts"} {
		if got[k] != want {
			t.Errorf("%s = %q, want %q", k, got[k], want)
		}
	}
	if _, ok := got["tabularia.model_file_sha256"]; ok {
		t.Error("a model file's hash")
	}
	if _, ok := got["tabularia.card_sha256"]; ok {
		t.Error("the card's hash belongs to the registered version")
	}
	cut := paramsByKey(bayesTags(card, strings.Repeat("é", 400)))["tabularia.bayes_model_name"]
	if len([]rune(cut)) != mlflowconn.MaxValueLength {
		t.Errorf("the name has %d characters", len([]rune(cut)))
	}
	// no card (too large): the name only
	if only := bayesTags(nil, "x"); len(only) != 1 || only[0].Key != "tabularia.bayes_model_name" {
		t.Errorf("tags with no card: %v", only)
	}
	if len(bayesTags(nil, "")) != 0 {
		t.Error("tags with nothing")
	}
}

func TestTheProgramIsUploadedOnlyWhenItIsTheOneTheCardNames(t *testing.T) {
	program := "parameters { real mu; }\nmodel { mu ~ normal(0, 1); }\n"
	sum := shaOf([]byte(program))
	card := &bayescontract.Card{Program: bayescontract.CardProgram{SHA256: sum}}
	b, err := bayesProgramFile(program, sum, card)
	if err != nil || string(b) != program {
		t.Errorf("the program: %q, %v", b, err)
	}
	if _, err := bayesProgramFile(program, "", nil); err != nil {
		t.Errorf("with nothing to check against: %v", err)
	}
	if _, err := bayesProgramFile(program, strings.ToUpper(sum), card); err != nil {
		t.Errorf("an upper-case hash: %v", err)
	}
	if _, err := bayesProgramFile(program+" ", sum, card); err == nil || !strings.Contains(err.Error(), "not uploaded") {
		t.Errorf("an altered program: %v", err)
	}
	other := &bayescontract.Card{Program: bayescontract.CardProgram{SHA256: shaOf([]byte("another"))}}
	if _, err := bayesProgramFile(program, sum, other); err == nil || !strings.Contains(err.Error(), "card names") {
		t.Errorf("a card of another program: %v", err)
	}
	if _, err := bayesProgramFile(program, "", other); err == nil {
		t.Error("a card of another program, no version hash")
	}
}

func TestOnlyTheFiveSectionsAreReadBack(t *testing.T) {
	var got []string
	for _, s := range bayesResultSections {
		got = append(got, s.(string))
	}
	if strings.Join(got, ",") != "summary,sampler,quantity,model,data" {
		t.Errorf("sections: %v", got)
	}
}
