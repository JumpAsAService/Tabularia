package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
)

// What a Bayesian run puts in MLflow (docs/specs/bayes-mlflow.md): pure functions of the step and of the results' rows (lot
// M0), and the step's files as MLflow gets them (lot M1: bayesLogOf, bayesArtifacts), which mlflowLog calls for a step whose
// modelStep.bayes is set; the machinery (the experiment, the run, the batches, the registration, the run page's record) is
// the model nodes' own. The rule of the whole file: **never a draw**. Every function reads the sections `summary`, `sampler`,
// `quantity`, `model` and `data` of the rows and no other (`curve`, `hist`, `point`, `ppc` and `prior_check` hold thinned
// draws and the charts' bins), and none of them writes a threshold, a reference value or a verdict: a metric is a measure or
// a fact.

// bayesMLflowQuantities is how many scalar quantities of the report's scope get their statistics as metrics; the others are in
// the summary table (`tabularia/summary.csv`), as the quantities of a plate always are.
const bayesMLflowQuantities = 100

// bayesMLflowCSVQuantities is the most rows of the summary table: the report's own ceiling of quantities.
const bayesMLflowCSVQuantities = 2000

// bayesResultSections are the sections of a Bayesian step's results that MLflow reads back from the engine (M1's
// `resultRowsOf` call): the posterior summary, the sampler's facts, the nodes' extremes, the run's counts and the data
// bindings. Never `curve`, `hist`, `point`, `ppc` or `prior_check`.
var bayesResultSections = []any{"summary", "sampler", "quantity", "model", "data"}

// bayesSummaryStats are the statistics of a scalar quantity that become metrics, in this order.
var bayesSummaryStats = []string{"mean", "sd", "q5", "q50", "q95", "rhat", "ess_bulk", "ess_tail", "mcse_mean"}

// bayesSamplerFacts maps the sampler's row `all` to its metric.
var bayesSamplerFacts = []struct{ stat, key string }{
	{"divergences", "sampler.divergences"},
	{"treedepth_hits", "sampler.treedepth_hits"},
	{"e_bfmi", "sampler.e_bfmi_min"}, // the row `all` holds the minimum of the chains' E-BFMI
	{"accept_stat_mean", "sampler.accept_stat_mean"},
	{"stepsize", "sampler.stepsize"},
}

// bayesRunCounts maps the `model` section's counts and times to their metric.
var bayesRunCounts = []string{"n", "n_missing", "n_not_finite", "seconds_warmup", "seconds_sampling"}

// bayesNodeExtremes are the `quantity` section's extremes of a node (a plate's quantities together).
var bayesNodeExtremes = []string{"rhat_max", "ess_bulk_min", "ess_tail_min"}

// bayesParams are the run's parameters: the saved model and its pinned version, the sampler's settings, the output and, for
// the output `replicated`, the draws a chain; and for each bound data variable its column, whether it was standardized and,
// when it was, its mean and sd (what a reader needs to put a coefficient back on a column's scale). The data variables come
// from the `data` rows by their name (the variable's name, else its Stan identifier, which is what the rows carry). The keys
// are cleaned as flattenParams' are, a collision kept apart and said.
func bayesParams(s *bayesStep, rows []resultRow) ([]mlflowconn.Tag, []string) {
	var raw []mlflowconn.Tag
	put := func(key, value string) { raw = append(raw, mlflowconn.Tag{Key: key, Value: value}) }
	if s.version != nil {
		put("model.id", strconv.FormatInt(s.version.ModelID, 10))
		put("model.version", strconv.FormatInt(s.version.Version, 10))
	}
	sp := s.sampler
	put("sampler.chains", strconv.Itoa(sp.Chains))
	put("sampler.warmup", strconv.Itoa(sp.Warmup))
	put("sampler.draws", strconv.Itoa(sp.Draws))
	put("sampler.seed", strconv.FormatInt(sp.Seed, 10))
	put("sampler.adapt_delta", strconv.FormatFloat(sp.AdaptDelta, 'g', -1, 64))
	put("sampler.max_treedepth", strconv.Itoa(sp.MaxTreedepth))
	put("sampler.init", bayesInitText(sp.Init))
	if s.modelStep != nil && s.output != "" {
		put("output", s.output)
		if s.output == bayescontract.OutputReplicated && s.replicated != nil {
			put("replicated_draws", strconv.Itoa(s.replicated.DrawsPerChain))
		}
	}
	// the data bindings, in the order the rows name the variables
	type binding struct{ column, standardized, mean, sd string }
	var order []string
	bound := map[string]*binding{}
	for _, r := range rows {
		if r.Section != "data" || r.Level == nil {
			continue
		}
		b := bound[*r.Level]
		if b == nil {
			b = &binding{}
			bound[*r.Level] = b
			order = append(order, *r.Level)
		}
		switch r.Stat {
		case "column":
			if r.Text != nil {
				b.column = *r.Text
			}
		case "standardized":
			if r.Text != nil {
				b.standardized = strconv.FormatBool(*r.Text == "yes")
			}
		case "mean":
			if r.Value != nil {
				b.mean = strconv.FormatFloat(*r.Value, 'g', -1, 64)
			}
		case "sd":
			if r.Value != nil {
				b.sd = strconv.FormatFloat(*r.Value, 'g', -1, 64)
			}
		}
	}
	for _, name := range order {
		b := bound[name]
		if b.column == "" {
			continue // a datum that reads no column (a size, a lag) is not a binding
		}
		put("data."+name+".column", b.column)
		if b.standardized != "" {
			put("data."+name+".standardized", b.standardized)
		}
		if b.standardized == "true" {
			if b.mean != "" {
				put("data."+name+".mean", b.mean)
			}
			if b.sd != "" {
				put("data."+name+".sd", b.sd)
			}
		}
	}
	names := newKeyNames("parameters of the node")
	for _, p := range raw {
		names.add(p.Key)
	}
	names.assign()
	out := make([]mlflowconn.Tag, 0, len(raw))
	seen := map[string]bool{}
	for _, p := range raw {
		if seen[p.Key] {
			continue
		}
		seen[p.Key] = true
		out = append(out, mlflowconn.Tag{Key: names.key(p.Key), Value: p.Value})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out, names.warnings()
}

// bayesInitText says the sampler's initial values: `default`, `radius <r>` or `values`.
func bayesInitText(i *bayescontract.Init) string {
	switch {
	case i == nil:
		return "default"
	case i.Radius != nil:
		return "radius " + strconv.FormatFloat(*i.Radius, 'g', -1, 64)
	}
	return "values"
}

// bayesMetrics are the run's metrics: the posterior summary of the scalar quantities (the summary rows with no unit, in their
// order, at most bayesMLflowQuantities of them, each with the nine statistics of bayesSummaryStats), the sampler's facts
// (the row `all`), the run's counts and times, and each node's extremes (at most bayesMLflowQuantities nodes). The keys are
// cleaned and cut (metricKey, shortKey) and two names that clean to one are kept apart and said. At the run's start, step 0.
func bayesMetrics(rows []resultRow, at time.Time) ([]mlflowconn.Metric, []string) {
	names := newKeyNames("metrics of the model")
	var out []mlflowconn.Metric
	var named []int // the metrics of out whose Key is a raw name, given its key at the end
	add := func(raw string, v float64) {
		if names.add(raw) {
			named = append(named, len(out))
			out = append(out, mlflowconn.Metric{Key: raw, Value: v, Timestamp: at.UnixMilli()})
		}
	}
	wanted := func(list []string, stat string) bool {
		for _, x := range list {
			if x == stat {
				return true
			}
		}
		return false
	}
	scalars := map[string]bool{}
	nodes := map[string]bool{}
	for _, r := range rows {
		if r.Value == nil {
			continue
		}
		switch {
		case r.Section == "sampler" && r.Level != nil && *r.Level == "all":
			for _, f := range bayesSamplerFacts {
				if r.Stat == f.stat {
					add(f.key, *r.Value)
				}
			}
		case r.Section == "model" && r.Level == nil && wanted(bayesRunCounts, r.Stat):
			add("run."+r.Stat, *r.Value)
		case r.Section == "summary" && r.Level != nil && r.Level2 == nil && wanted(bayesSummaryStats, r.Stat):
			q := *r.Level
			if !scalars[q] {
				if len(scalars) >= bayesMLflowQuantities {
					continue
				}
				scalars[q] = true
			}
			add("summary."+q+"."+r.Stat, *r.Value)
		case r.Section == "quantity" && r.Level != nil && wanted(bayesNodeExtremes, r.Stat):
			q := *r.Level
			if !nodes[q] {
				if len(nodes) >= bayesMLflowQuantities {
					continue
				}
				nodes[q] = true
			}
			add("quantity."+q+"."+r.Stat, *r.Value)
		}
	}
	names.assign()
	for _, i := range named {
		out[i].Key = names.key(out[i].Key)
	}
	return out, names.warnings()
}

// bayesTags are the Bayesian run's tags beside the common ones: the saved model's id, version, name and semantic hash, the
// program's and the binary's hashes, CmdStan's version and the sampler. They come from the card the runner wrote (no
// verdict, no draw in it); a run with no card (it was too large) keeps only the name, the one fact the gateway holds.
// The name is data: a tag's value as text, cut to MLflow's length.
func bayesTags(card *bayescontract.Card, name string) []mlflowconn.Tag {
	var out []mlflowconn.Tag
	put := func(key, value string) {
		if value != "" {
			out = append(out, mlflowconn.Tag{Key: key, Value: runes(value, mlflowconn.MaxValueLength)})
		}
	}
	if card != nil {
		put("tabularia.bayes_model_id", strconv.FormatInt(card.Model.ID, 10))
		put("tabularia.bayes_model_version", strconv.Itoa(card.Model.Version))
	}
	put("tabularia.bayes_model_name", name)
	if card != nil {
		put("tabularia.bayes_semantic_hash", card.Model.SemanticHash)
		put("tabularia.program_sha256", card.Program.SHA256)
		put("tabularia.binary_sha256", card.Binary.SHA256)
		put("tabularia.cmdstan", card.Binary.CmdStan)
		sampler := card.Sampler.Algorithm
		if sampler == "" || sampler == "fixed_param" {
			sampler = "nuts" // a flow run samples; fixed_param is the prior predictive check's, a trial that logs nothing
		}
		put("tabularia.sampler", sampler)
	}
	return out
}

// bayesSummaryColumns are the summary table's statistics after `quantity` and `unit`.
var bayesSummaryColumns = []string{"mean", "sd", "q5", "q50", "q95", "rhat", "ess_bulk", "ess_tail", "mcse_mean"}

// bayesOriginalColumns are the extra columns of a linear predictor's coefficients on the column's own scale.
var bayesOriginalColumns = []string{"orig_mean", "orig_sd", "orig_q5", "orig_q50", "orig_q95"}

// bayesSummaryCSV is `tabularia/summary.csv`: one row a quantity of the report's scope (a scalar, a correlation, each unit of
// a plate's quantity), in the order of the summary rows, at most bayesMLflowCSVQuantities, with `quantity`, `unit`, the
// statistics and, when some quantity has them, the `orig_*` columns. The names are the person's: text, with a leading `=`,
// `+`, `-`, `@` (or a tab or a carriage return) neutralised by a quote so a spreadsheet never reads them as a formula.
// Nil when there is no summary.
func bayesSummaryCSV(rows []resultRow) []byte {
	type key struct{ q, unit string }
	var order []key
	cells := map[key]map[string]string{}
	withOriginal := false
	for _, r := range rows {
		if r.Section != "summary" || r.Level == nil || r.Value == nil {
			continue
		}
		k := key{q: *r.Level}
		if r.Level2 != nil {
			k.unit = *r.Level2
		}
		if cells[k] == nil {
			if len(order) >= bayesMLflowCSVQuantities {
				continue
			}
			cells[k] = map[string]string{}
			order = append(order, k)
		}
		cells[k][r.Stat] = strconv.FormatFloat(*r.Value, 'g', -1, 64)
		if strings.HasPrefix(r.Stat, "orig_") {
			withOriginal = true
		}
	}
	if len(order) == 0 {
		return nil
	}
	columns := append([]string{}, bayesSummaryColumns...)
	if withOriginal {
		columns = append(columns, bayesOriginalColumns...)
	}
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	_ = w.Write(append([]string{"quantity", "unit"}, columns...))
	for _, k := range order {
		line := []string{csvText(k.q), csvText(k.unit)}
		for _, c := range columns {
			line = append(line, cells[k][c])
		}
		_ = w.Write(line)
	}
	w.Flush()
	return buf.Bytes()
}

// csvText is a person's text in a table a spreadsheet may open: never a formula.
func csvText(s string) string {
	if s != "" && strings.ContainsRune("=+-@\t\r", rune(s[0])) {
		return "'" + s
	}
	return s
}

// bayesProgramFile is `model/program.stan`: the program the pinned version stores, as bytes, once it is the one the card
// says it is. The card's program hash must equal the sha256 of the program the gateway holds (and the version's own hash
// when it is given): a difference is a fault of Tabularia, said, and the program is not uploaded. A step with no card
// (too large) is checked against the version's hash alone.
func bayesProgramFile(program string, versionSHA256 string, card *bayescontract.Card) ([]byte, error) {
	sum := sha256.Sum256([]byte(program))
	got := hex.EncodeToString(sum[:])
	if want := strings.ToLower(strings.TrimSpace(versionSHA256)); want != "" && want != got {
		return nil, fmt.Errorf("the program stored for this version of the model does not match its fingerprint (%s, not %s): it is not uploaded to MLflow", got[:12], want[:min(12, len(want))])
	}
	if card != nil {
		if want := strings.ToLower(strings.TrimSpace(card.Program.SHA256)); want != "" && want != got {
			return nil, fmt.Errorf("the program stored for this version of the model is not the one the run's card names (%s, not %s): it is not uploaded to MLflow", got[:12], want[:min(12, len(want))])
		}
	}
	return []byte(program), nil
}

// ── the step's files (lot M1) ────────────────────────────────────────────────

// bayesLog is what a Bayesian step's MLflow record needs besides its rows: the card the runner wrote (nil when it was too
// large and the runner wrote none), its bytes as they are uploaded, and the program the pinned version stores, checked
// against both.
type bayesLog struct {
	step    *bayesStep
	card    *bayescontract.Card
	cardRaw []byte
	program []byte
}

// bayesFault is a fault of Tabularia found before anything is logged (a program that is not the one the card and the version
// name, a card that does not read): the step fails, «Continue without MLflow» or not, and nothing reaches MLflow. It reads as
// a *scriptFailed (mlflowWhy says its sentence).
type bayesFault struct{ msg string }

func (e *bayesFault) Error() string { return e.msg }
func (e *bayesFault) Unwrap() error { return &scriptFailed{e.msg} }

// bayesCardOf reads the `bayes` object of a run's model.json.
func bayesCardOf(raw []byte) (*bayescontract.Card, error) {
	var c struct {
		Bayes *bayescontract.Card `json:"bayes"`
	}
	if err := json.Unmarshal(raw, &c); err != nil {
		return nil, err
	}
	if c.Bayes == nil {
		return nil, errors.New("it has no `bayes` object")
	}
	return c.Bayes, nil
}

// bayesModelName is the saved model's name, "" when the step holds none.
func (s *bayesStep) bayesModelName() string {
	if s.model == nil {
		return ""
	}
	return s.model.Name
}

// bayesLogOf reads the card the runner left in the step's artifacts (none when it was too large) and checks the program the
// gateway holds against the version's hash and the card's, before anything is logged.
func (q *queue) bayesLogOf(ctx context.Context, s *modelStep, f modelFiles) (*bayesLog, error) {
	b := s.bayesRun
	if b == nil || b.version == nil {
		return nil, &bayesFault{"the Bayesian step holds no version of its model: a fault of Tabularia"}
	}
	out := &bayesLog{step: b}
	in, _, err := q.d.openScriptObject(ctx, f.storage, f.artifacts+"model.json")
	if err != nil {
		return nil, err
	}
	if in != nil {
		raw, err := io.ReadAll(io.LimitReader(in, 4<<20))
		in.Close()
		if err != nil {
			return nil, err
		}
		card, err := bayesCardOf(raw)
		if err != nil {
			return nil, &bayesFault{"the run's model card does not read (" + runes(err.Error(), 200) + "): a fault of Tabularia, nothing was logged"}
		}
		out.card, out.cardRaw = card, raw
	}
	program, err := bayesProgramFile(b.version.Program, b.version.ProgramSHA256, out.card)
	if err != nil {
		return nil, &bayesFault{err.Error() + "; a fault of Tabularia, nothing was logged"}
	}
	out.program = program
	return out, nil
}

// bayesArtifacts uploads a Bayesian step's files through the artifact proxy, in place of mlflowArtifacts: the summary table
// first (the call that tells a server without the proxy), then, when the runner wrote a card, model/MLmodel (with the run's
// id), model/model.json and model/program.stan (the version's, checked by bayesLogOf), then the report. Never
// results.parquet: it holds thinned draws and the charts' bins (owner, 2026-11-24); never a file of the Job but these two.
func (q *queue) bayesArtifacts(ctx context.Context, tr *mlflowconn.Tracker, r *scriptNodeRun, s *modelStep, child *childRun, f modelFiles,
	rows []resultRow, run *mlflowconn.Run, rec *mlflowRecord, bl *bayesLog) (uploadedFiles, error) {
	var up uploadedFiles
	where := mlflowconn.ArtifactPath(run.ArtifactURI)
	noProxy := "This MLflow server keeps the runs' files where Tabularia does not write (no artifact proxy): parameters, metrics and tags were logged, no file."
	if where == "" {
		rec.warnings = append(rec.warnings, noProxy)
		return up, nil
	}
	upload := func(name string, body []byte) error {
		return tr.Upload(ctx, where, name, bytes.NewReader(body), int64(len(body)))
	}
	summary := bayesSummaryCSV(rows)
	if summary == nil { // no quantity: the table's header alone
		summary = []byte("quantity,unit," + strings.Join(bayesSummaryColumns, ",") + "\n")
	}
	err := upload("tabularia/summary.csv", summary)
	if mlflowconn.Unsupported(err) {
		rec.warnings = append(rec.warnings, noProxy)
		return up, nil
	}
	if err != nil {
		return up, err
	}
	rec.artifacts = true
	if bl.cardRaw == nil {
		rec.warnings = append(rec.warnings, "The runner wrote no model card: the model's files are not in the MLflow run.")
	} else {
		in, _, err := q.d.openScriptObject(ctx, f.storage, f.artifacts+"MLmodel")
		if err != nil {
			return up, err
		}
		if in != nil {
			b, err := io.ReadAll(io.LimitReader(in, 4<<20))
			in.Close()
			if err != nil {
				return up, err
			}
			if err := upload("model/MLmodel", withRunID(b, run.ID)); err != nil {
				return up, err
			}
		}
		if err := upload("model/model.json", bl.cardRaw); err != nil {
			return up, err
		}
		up.card = bl.cardRaw
		if err := upload("model/program.stan", bl.program); err != nil {
			return up, err
		}
	}
	pdf, err := q.modelReportPDF(ctx, r, s, child, f)
	if err != nil {
		if ctx.Err() != nil {
			return up, ctx.Err()
		}
		rec.warnings = append(rec.warnings, "The report could not be rendered for MLflow: "+runes(mlflowWhy(err), 300))
	} else if err := upload("tabularia/report.pdf", pdf); err != nil {
		return up, err
	}
	return up, nil
}

// bayesVersionTags are the Bayesian tags a registered version carries beside the common ones: the saved model's
// (`tabularia.bayes_*`), from the card.
func bayesVersionTags(bl *bayesLog) []mlflowconn.Tag {
	var out []mlflowconn.Tag
	for _, t := range bayesTags(bl.card, bl.step.bayesModelName()) {
		if strings.HasPrefix(t.Key, "tabularia.bayes_") {
			out = append(out, t)
		}
	}
	return out
}
