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
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sharelinks"
)

// A model step's MLflow tracking (docs/specs/statistical-models.md, «MLflow» → «What is
// logged», lot G). Only flow runs log, never a trial. The gateway does it, after the step's
// results are imported and before its files leave the scripts bucket: it holds the
// connection's secret, which never reaches the Job (whose task.json only says `mlflow: true`,
// so that the runner writes MLmodel and native.rds).
//
//  1. The experiment, by name; created when missing (tag tabularia.created = true).
//  2. The run «flow · node · date»; parameters (the node's, flattened, and the formula), metrics
//     (every number of the `model` section; per assumption its statistic and p-value) and tags
//     in batches (mlflowconn.Batches).
//  3. The artifacts through the server's artifact proxy: tabularia/results.parquet,
//     tabularia/coefficients.csv, tabularia/report.pdf (the Report tab's layout, in the flow's
//     owner's language), model/MLmodel, model/model.json, model/native.rds and, for a boosting
//     model, its model file model/xgboost.json or model/catboost.cbm (hashed while it goes up,
//     which must be what the card declares; never a pickle). A server whose runs
//     keep their artifacts elsewhere gets none, and the run page says so.
//  4. With registerAs, a version of that registered model from the run's `model` folder, with
//     the tags tabularia.model, tabularia.group, tabularia.variant and tabularia.card_sha256
//     (what Predict checks), and tabularia.model_file_sha256 when there is a model file.
//  5. The run FINISHED; its ids and link kept on Tabularia's run (`mlflow_*` columns and the
//     `mlflow` object of its script, which the run page reads).
//
// When MLflow fails the step fails, «the model was fitted but MLflow could not record it: …»,
// unless the node's «Continue without MLflow» is on: then the step succeeds with a warning.
// A step that fails after its Job ran is logged as a FAILED run, as far as MLflow answers.

// mlflowCallTimeout bounds each call to the tracking server (a variable: the tests shorten it).
var mlflowCallTimeout = 30 * time.Second

// mlflowReportPoll is how often the report's render is asked after (a variable for the tests).
var mlflowReportPoll = time.Second

// mlflowReportTimeout is how long the report's render is waited for.
var mlflowReportTimeout = 10 * time.Minute

// mlflowPlan is the node's MLflow block, checked: where its flow runs log.
type mlflowPlan struct {
	connID          int64
	connName        string
	cfg             mlflowconn.Config
	ui              string // the base of the links: the connection's UI address, else its tracking URI
	experiment      string
	registerAs      string
	continueWithout bool
}

// planMLflow reads and checks a model node's `mlflow` block for a flow run: nil when the node
// has none. The connection must be an `mlflow` one the person may CONNECT
// (docs/specs/statistical-models.md, «A run», 1); otherwise the step fails before anything.
func (q *queue) planMLflow(ctx context.Context, user *auth.User, node pyjson.Object, label string) (*mlflowPlan, error) {
	block, _ := nodeData(node).Value("mlflow").(pyjson.Object)
	if block == nil || block.Value("connectionId") == nil {
		return nil, nil
	}
	refuse := func(why string) (*mlflowPlan, error) {
		return nil, &scriptFailed{fmt.Sprintf("model «%s»: %s", label, why)}
	}
	id, ok := pyjson.IsInt(block.Value("connectionId"))
	if !ok {
		return refuse("its MLflow connection is not a connection's id")
	}
	experiment, _ := block.Value("experiment").(string)
	experiment = strings.TrimSpace(experiment)
	if experiment == "" {
		return refuse("name the MLflow experiment its runs are logged to, or remove the MLflow connection")
	}
	conn, err := q.d.mlflowConnection(ctx, user, id)
	var refused *mlflowConnRefused
	if errors.As(err, &refused) {
		return refuse(refused.msg)
	}
	if err != nil {
		return nil, err
	}
	register, _ := block.Value("registerAs").(string)
	return &mlflowPlan{connID: conn.id, connName: conn.name, cfg: conn.cfg, ui: conn.ui, experiment: experiment,
		registerAs: strings.TrimSpace(register), continueWithout: block.Value("continueWithout") == true}, nil
}

// mlflowRecord is what Tabularia's run keeps of its MLflow run.
type mlflowRecord struct {
	plan              *mlflowPlan
	experimentID      string
	experimentCreated bool
	runID             string
	artifacts         bool
	model             string // the registered model
	version           string
	warnings          []string
	err               string // why MLflow could not record the step
	continued         bool   // the step went on without it («Continue without MLflow»)
	failed            bool   // the MLflow run of a failed step
}

func (m *mlflowRecord) runURL() string {
	if m.runID == "" || m.experimentID == "" {
		return ""
	}
	return fmt.Sprintf("%s/#/experiments/%s/runs/%s", m.plan.ui, url.PathEscape(m.experimentID), url.PathEscape(m.runID))
}

func (m *mlflowRecord) object() pyjson.Object {
	o := pyjson.Object{{Key: "connection_id", Value: m.plan.connID}, {Key: "connection", Value: m.plan.connName},
		{Key: "experiment", Value: m.plan.experiment}, {Key: "experiment_id", Value: nilIfEmpty(m.experimentID)},
		{Key: "experiment_created", Value: m.experimentCreated}, {Key: "run_id", Value: nilIfEmpty(m.runID)},
		{Key: "run_url", Value: nilIfEmpty(m.runURL())}, {Key: "artifacts", Value: m.artifacts}}
	if m.experimentID != "" {
		o = append(o, pyjson.KV{Key: "experiment_url", Value: fmt.Sprintf("%s/#/experiments/%s", m.plan.ui, url.PathEscape(m.experimentID))})
	}
	if m.plan.registerAs != "" {
		o = append(o, pyjson.KV{Key: "register_as", Value: m.plan.registerAs})
	}
	if m.version != "" {
		o = append(o, pyjson.KV{Key: "registered_model", Value: m.model}, pyjson.KV{Key: "model_version", Value: m.version},
			pyjson.KV{Key: "model_url", Value: fmt.Sprintf("%s/#/models/%s/versions/%s", m.plan.ui, url.PathEscape(m.model), url.PathEscape(m.version))})
	}
	o = append(o, pyjson.KV{Key: "warnings", Value: stringsAny(m.warnings)})
	if m.err != "" {
		o = append(o, pyjson.KV{Key: "error", Value: m.err}, pyjson.KV{Key: "continued", Value: m.continued})
	}
	if m.failed {
		o = append(o, pyjson.KV{Key: "failed_step", Value: true})
	}
	return o
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// save writes the record on Tabularia's run: the `mlflow` object of its script and the columns.
func (m *mlflowRecord) save(ctx context.Context, child *childRun) error {
	child.info = child.info.Set("mlflow", m.object())
	errText := m.err
	if errText == "" && len(m.warnings) > 0 {
		errText = strings.Join(m.warnings, " ")
	}
	return child.save(ctx, "mlflow_connection_id", m.plan.connID, "mlflow_experiment_id", nilIfEmpty(m.experimentID),
		"mlflow_run_id", nilIfEmpty(m.runID), "mlflow_model_name", nilIfEmpty(m.model), "mlflow_model_version", nilIfEmpty(m.version),
		"mlflow_error", nilIfEmpty(runes(errText, 2000)))
}

// modelFiles are where a step's files are when it is logged.
type modelFiles struct {
	storage          *scriptStorage
	results          string // results.parquet in the scripts bucket
	artifacts        string // the artifacts' prefix in the scripts bucket
	engineBucket     string // Tabularia's copy of the results, which the report reads
	engineResultsKey string
}

// logModelMLflow is step 5 of a model step's run: nil when MLflow recorded it, or when it could
// not and the node goes on without it.
func (q *queue) logModelMLflow(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun, f modelFiles) error {
	rec := &mlflowRecord{plan: s.mlflow}
	err := q.mlflowLog(ctx, r, s, child, f, rec)
	if err == nil {
		return rec.save(ctx, child)
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	rec.err = "the model was fitted but MLflow could not record it: " + mlflowWhy(err)
	if s.mlflow.continueWithout {
		rec.continued = true
		return rec.save(ctx, child)
	}
	if serr := rec.save(ctx, child); serr != nil {
		return serr
	}
	return &scriptFailed{fmt.Sprintf("model «%s»: %s", s.label, rec.err)}
}

// mlflowWhy is a failure as the run page says it: the tracking client's sentence, never a
// server's text.
func mlflowWhy(err error) string {
	var me *mlflowconn.Error
	var del *mlflowconn.Deleted
	var sf *scriptFailed
	switch {
	case errors.As(err, &me), errors.As(err, &del):
		return err.Error()
	case errors.As(err, &sf):
		return sf.msg
	}
	return "Tabularia could not read the step's files for it (" + runes(errorMessage(err), 300) + ")"
}

// mlflowLog does the logging; rec holds what was done, even when it fails half-way.
func (q *queue) mlflowLog(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun, f modelFiles, rec *mlflowRecord) error {
	rows, err := q.d.modelResultRows(ctx, f.storage, f.results)
	if err != nil {
		return err
	}
	tr := mlflowconn.NewTracker(s.mlflow.cfg)
	if rec.experimentID, rec.experimentCreated, err = tr.Experiment(ctx, s.mlflow.experiment); err != nil {
		return err
	}
	started := time.Now()
	run, err := tr.CreateRun(ctx, rec.experimentID, mlflowRunName(r.flow.Name, s.label, started), started)
	if err != nil {
		return err
	}
	rec.runID = run.ID
	failed := true
	defer func() {
		if failed { // as far as the server answers: the run is not left RUNNING
			cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), mlflowCallTimeout)
			_ = tr.EndRun(cctx, run.ID, true, time.Now())
			cancel()
		}
	}()
	tags, err := q.mlflowTags(ctx, r, s, child)
	if err != nil {
		return err
	}
	params := append(append(append(flattenParams(s.checked), modelFormula(rows)...), modelDistribution(rows)...),
		customerLifetimeParams(rows, s.checked)...)
	metrics, metricWarnings := modelMetricsChecked(rows, started)
	rec.warnings = append(rec.warnings, metricWarnings...)
	if err := tr.LogBatch(ctx, run.ID, params, metrics, tags); err != nil {
		return err
	}
	up, err := q.mlflowArtifacts(ctx, tr, r, s, child, f, rows, run, rec)
	if err != nil {
		return err
	}
	card := up.card
	if s.mlflow.registerAs != "" {
		switch {
		case !rec.artifacts:
			rec.warnings = append(rec.warnings, fmt.Sprintf("The model was not registered as «%s»: its files could not be uploaded to MLflow.",
				s.mlflow.registerAs))
		case card == nil:
			rec.warnings = append(rec.warnings, fmt.Sprintf("The model was not registered as «%s»: the runner wrote no model card.",
				s.mlflow.registerAs))
		case up.notRegistered != "":
			rec.warnings = append(rec.warnings, fmt.Sprintf("The model was not registered as «%s»: %s", s.mlflow.registerAs, up.notRegistered))
		default:
			sum := sha256.Sum256(card)
			tags := []mlflowconn.Tag{
				{Key: "tabularia.model", Value: s.model}, {Key: "tabularia.group", Value: s.group},
				{Key: "tabularia.group_version", Value: strconv.FormatInt(s.img.Version, 10)},
				{Key: "tabularia.card_sha256", Value: hex.EncodeToString(sum[:])}}
			if c := parseCardFile(card); c.variant != "" {
				tags = append(tags, mlflowconn.Tag{Key: "tabularia.variant", Value: c.variant})
			}
			if c := parseCardFile(card); c.purchaseModel != "" {
				tags = append(tags, mlflowconn.Tag{Key: "tabularia.purchase_model", Value: c.purchaseModel})
			}
			if up.fileSHA != "" {
				tags = append(tags, mlflowconn.Tag{Key: "tabularia.model_file_sha256", Value: up.fileSHA})
			}
			version, err := tr.Register(ctx, s.mlflow.registerAs, strings.TrimRight(run.ArtifactURI, "/")+"/model", run.ID, tags)
			switch {
			case err == nil:
				rec.model, rec.version = s.mlflow.registerAs, version
			case mlflowconn.Unsupported(err):
				rec.warnings = append(rec.warnings, fmt.Sprintf("The model was not registered as «%s»: this MLflow server has no model registry.",
					s.mlflow.registerAs))
			default:
				return err
			}
		}
	}
	if err := tr.EndRun(ctx, run.ID, false, time.Now()); err != nil {
		return err
	}
	failed = false
	return nil
}

// mlflowRunName is «flow · node · date».
func mlflowRunName(flow, node string, at time.Time) string {
	return fmt.Sprintf("%s · %s · %s", flow, node, at.UTC().Format("2006-01-02 15:04"))
}

// uploadedFiles is what the upload of a step's files leaves for the registration.
type uploadedFiles struct {
	card          []byte // model.json's bytes when it was uploaded (what a registration names)
	fileSHA       string // the model file's sha256, computed while it was uploaded ("" when there is none)
	notRegistered string // why the version is not registered, when the files are logged all the same
}

// modelFileNames are the model files a boosting model keeps beside its card (docs/specs/
// statistical-models.md, «The saved model, MLflow and Predict»): data a library parses, never
// code (no pickle).
var modelFileNames = map[string]string{"xgboost": "xgboost.json", "catboost": "catboost.cbm"}

func isModelFile(name string) bool { return name == "xgboost.json" || name == "catboost.cbm" }

// cardModelFile is the card's `boosting.model_file`.
type cardModelFile struct {
	Name   string `json:"name"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

// cardFile is what the gateway reads of a card besides what decides whether it is applied: its
// variant and the model file it declares.
type cardFile struct {
	variant       string
	library       string
	file          *cardModelFile
	purchaseModel string // a customer lifetime card's purchase model (the one used)
}

func parseCardFile(card []byte) cardFile {
	var c struct {
		Variant  string `json:"variant"`
		Boosting *struct {
			Library   string         `json:"library"`
			ModelFile *cardModelFile `json:"model_file"`
		} `json:"boosting"`
		CustomerLifetime *struct {
			PurchaseModel string `json:"purchase_model"`
		} `json:"customer_lifetime"`
	}
	if json.Unmarshal(card, &c) != nil {
		return cardFile{}
	}
	out := cardFile{variant: c.Variant}
	if c.CustomerLifetime != nil {
		out.purchaseModel = c.CustomerLifetime.PurchaseModel
	}
	if c.Boosting != nil {
		out.library, out.file = c.Boosting.Library, c.Boosting.ModelFile
	}
	return out
}

// maxModelFileBytes is the catalogue's limit of a model file (64 MiB); a variable for the tests.
var maxModelFileBytes = func() int64 {
	if n := int64(modelcatalog.Limit("max_model_file_bytes")); n > 0 {
		return n
	}
	return 64 << 20
}

// mlflowArtifacts uploads the step's files through the artifact proxy.
func (q *queue) mlflowArtifacts(ctx context.Context, tr *mlflowconn.Tracker, r *scriptNodeRun, s *modelStep, child *childRun, f modelFiles,
	rows []resultRow, run *mlflowconn.Run, rec *mlflowRecord) (uploadedFiles, error) {
	var up uploadedFiles
	where := mlflowconn.ArtifactPath(run.ArtifactURI)
	noProxy := "This MLflow server keeps the runs' files where Tabularia does not write (no artifact proxy): parameters, metrics and tags were logged, no file."
	if where == "" {
		rec.warnings = append(rec.warnings, noProxy)
		return up, nil
	}
	upload := func(name string, body io.Reader, size int64) error { return tr.Upload(ctx, where, name, body, size) }
	// the results first: always there, and the call that tells a server without the proxy
	in, size, err := q.d.openScriptObject(ctx, f.storage, f.results)
	if err != nil {
		return up, err
	}
	if in == nil {
		return up, &scriptFailed{"the step's results are no longer in the scripts bucket"}
	}
	err = upload("tabularia/results.parquet", in, size)
	in.Close()
	if mlflowconn.Unsupported(err) {
		rec.warnings = append(rec.warnings, noProxy)
		return up, nil
	}
	if err != nil {
		return up, err
	}
	rec.artifacts = true
	if text := coefficientsCSV(rows); text != nil {
		if err := upload("tabularia/coefficients.csv", bytes.NewReader(text), int64(len(text))); err != nil {
			return up, err
		}
	}
	for _, name := range []string{"MLmodel", "model.json", "native.rds", "xgboost.json", "catboost.cbm"} {
		in, size, err := q.d.openScriptObject(ctx, f.storage, f.artifacts+name)
		if err != nil {
			return up, err
		}
		if in == nil {
			continue
		}
		if isModelFile(name) {
			err := q.uploadModelFile(name, in, size, upload, &up)
			in.Close()
			if err != nil {
				return up, err
			}
			continue
		}
		var body io.Reader = in
		switch name {
		case "MLmodel", "model.json": // small: read whole (the card's hash; the run's id in MLmodel)
			b, err := io.ReadAll(io.LimitReader(in, 4<<20))
			in.Close()
			if err != nil {
				return up, err
			}
			if name == "model.json" {
				up.card = b
			} else {
				b = withRunID(b, run.ID)
			}
			body, size = bytes.NewReader(b), int64(len(b))
		}
		err = upload("model/"+name, body, size)
		if c, isCloser := body.(io.Closer); isCloser {
			c.Close()
		}
		if err != nil {
			return up, err
		}
	}
	if up.card == nil {
		rec.warnings = append(rec.warnings, "The runner wrote no model card: the model's files are not in the MLflow run.")
	} else if c := parseCardFile(up.card); c.file != nil && up.fileSHA == "" && up.notRegistered == "" {
		return up, &scriptFailed{"the model card declares a model file (" + c.file.Name + ") that the step did not leave"}
	}
	pdf, err := q.modelReportPDF(ctx, r, s, child, f)
	if err != nil {
		if ctx.Err() != nil {
			return up, ctx.Err()
		}
		rec.warnings = append(rec.warnings, "The report could not be rendered for MLflow: "+runes(mlflowWhy(err), 300))
	} else if err := upload("tabularia/report.pdf", bytes.NewReader(pdf), int64(len(pdf))); err != nil {
		return up, err
	}
	return up, nil
}

// uploadModelFile streams a boosting model's file from the scripts bucket to the run's
// artifacts (model/<name>), hashing it on the way: never held whole. The card (uploaded just
// before) must declare it, and its name, size and sha256 must be the ones the card keeps: the
// card fixes the file, as the MLflow tag fixes the card. A file above the limit is not
// uploaded and the version is not registered (the runner writes none, and says so in a note;
// this is the gateway's own check).
func (q *queue) uploadModelFile(name string, in io.Reader, size int64, upload func(string, io.Reader, int64) error, up *uploadedFiles) error {
	if size > maxModelFileBytes() {
		up.notRegistered = fmt.Sprintf("its model file is larger than a model file may be (%d bytes).", maxModelFileBytes())
		return nil
	}
	c := parseCardFile(up.card)
	if c.file == nil || c.file.Name != name {
		return &scriptFailed{"the step left a model file (" + name + ") that its model card does not declare"}
	}
	h := sha256.New()
	if err := upload("model/"+name, io.TeeReader(in, h), size); err != nil {
		return err
	}
	sum := hex.EncodeToString(h.Sum(nil))
	if sum != strings.ToLower(c.file.SHA256) || size != c.file.Bytes {
		return &scriptFailed{"the model file (" + name + ") is not the one its model card describes (size or sha256): it was not registered"}
	}
	up.fileSHA = sum
	return nil
}

// withRunID adds the MLflow run's id to an MLmodel the runner wrote (it cannot know it).
func withRunID(mlmodel []byte, runID string) []byte {
	for _, line := range strings.Split(string(mlmodel), "\n") {
		if strings.HasPrefix(line, "run_id:") {
			return mlmodel
		}
	}
	text := strings.TrimRight(string(mlmodel), "\n")
	return []byte(text + "\nrun_id: " + runID + "\n")
}

// mlflowTags are the run's tags (docs/specs/statistical-models.md, «What is logged»).
func (q *queue) mlflowTags(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun) ([]mlflowconn.Tag, error) {
	base := sharelinks.PublicBaseURL() // the interface's address when the installation says it; else the links are relative
	var version int64
	if err := q.d.DB.QueryRow(ctx, `SELECT coalesce(max(version), 0) FROM flow_versions WHERE flow_id = $1`, r.flow.ID).Scan(&version); err != nil {
		return nil, err
	}
	digest := s.img.Image
	if _, after, found := strings.Cut(digest, "@"); found {
		digest = after
	}
	sample, _ := child.info.Value("sample").([]any)
	tags := []mlflowconn.Tag{
		{Key: "tabularia.url", Value: fmt.Sprintf("%s/runs?run=%d", base, child.id)},
		{Key: "tabularia.flow_id", Value: strconv.FormatInt(r.flow.ID, 10)},
		{Key: "tabularia.flow_name", Value: r.flow.Name},
		{Key: "tabularia.node_id", Value: pyjson.Str(r.node.Value("id"))},
		{Key: "tabularia.node_name", Value: s.label},
		{Key: "tabularia.run_id", Value: strconv.FormatInt(r.orchestrationOf(child.id), 10)},
		{Key: "tabularia.step_run_id", Value: strconv.FormatInt(child.id, 10)},
		{Key: "tabularia.model", Value: s.model},
		{Key: "tabularia.group", Value: s.group},
		{Key: "tabularia.group_version", Value: strconv.FormatInt(s.img.Version, 10)},
		{Key: "tabularia.image_digest", Value: digest},
		{Key: "tabularia.dev_sample", Value: strconv.FormatBool(len(sample) > 0)},
		{Key: "tabularia.launched_by", Value: r.user.Email},
		{Key: "mlflow.source.name", Value: fmt.Sprintf("%s/editor?flow=%d", base, r.flow.ID)},
		{Key: "mlflow.source.type", Value: "JOB"},
	}
	if version > 0 {
		tags = append(tags, mlflowconn.Tag{Key: "tabularia.flow_version", Value: strconv.FormatInt(version, 10)})
	}
	if n, ok := pyInt(child.info.Value("input_rows")); ok && child.info.Value("input_rows") != nil {
		tags = append(tags, mlflowconn.Tag{Key: "tabularia.input_rows", Value: strconv.FormatInt(n, 10)})
	}
	return tags, nil
}

// ── the content ──────────────────────────────────────────────────────────────

// resultRow is a row of a model's results, as the engine reads them back.
type resultRow struct {
	Section string   `json:"section"`
	Level   *string  `json:"level"`
	Level2  *string  `json:"level_2"`
	Stat    string   `json:"stat"`
	Value   *float64 `json:"value"`
	Text    *string  `json:"text"`
	X0      *float64 `json:"x0"` // a chart point's x (a learning curve's round)
}

// flattenParams are the node's checked parameters as MLflow's: one per key, a nested object's
// keys joined with a dot (`reference.channel`), a list as comma-separated text, sorted.
func flattenParams(params map[string]any) []mlflowconn.Tag {
	var out []mlflowconn.Tag
	var walk func(prefix string, v any)
	walk = func(prefix string, v any) {
		switch x := v.(type) {
		case nil:
		case map[string]any:
			for k, sub := range x {
				walk(prefix+"."+k, sub)
			}
		default:
			out = append(out, mlflowconn.Tag{Key: strings.TrimPrefix(prefix, "."), Value: paramText(x)})
		}
	}
	for k, v := range params {
		walk("."+k, v)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

func paramText(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case bool:
		return strconv.FormatBool(x)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	case json.Number:
		return x.String()
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = paramText(e)
		}
		return strings.Join(parts, ",")
	case map[string]any:
		b, _ := json.Marshal(x)
		return string(b)
	}
	return fmt.Sprint(v)
}

// modelFormula is the `formula` text of the results' model section, as a parameter.
func modelFormula(rows []resultRow) []mlflowconn.Tag {
	for _, r := range rows {
		if r.Section == "model" && r.Stat == "formula" && r.Text != nil && *r.Text != "" {
			return []mlflowconn.Tag{{Key: "formula", Value: *r.Text}}
		}
	}
	return nil
}

// modelDistribution is the distribution the fit used, the `distribution` text of the results'
// model section, as the parameter `distribution_chosen`. The node's own parameter `distribution`
// may say only `auto`; this is the one the automatic choice picked (the same text otherwise).
func modelDistribution(rows []resultRow) []mlflowconn.Tag {
	for _, r := range rows {
		if r.Section == "model" && r.Stat == "distribution" && r.Text != nil && *r.Text != "" {
			return []mlflowconn.Tag{{Key: "distribution_chosen", Value: *r.Text}}
		}
	}
	return nil
}

// isCustomerLifetime says whether results are a customer lifetime model's: its model section says
// the purchase model it used (`purchase_model`, a text no other model's results have).
func isCustomerLifetime(rows []resultRow) bool {
	for _, r := range rows {
		if r.Section == "model" && r.Stat == "purchase_model" && r.Text != nil && *r.Text != "" {
			return true
		}
	}
	return false
}

// customerLifetimeParams are what a customer lifetime fit settled that the node may only have said
// as `auto` or left empty: the purchase model it used (`purchase_model_chosen`; the node's own
// `purchase_model` stays as the node says it) and, when the node gave no horizon, the one it used
// (the default of one year, in the node's unit). The unit, the discount rate, the margin and alpha
// are the node's checked parameters, defaults included, logged with the others.
func customerLifetimeParams(rows []resultRow, checked map[string]any) []mlflowconn.Tag {
	var out []mlflowconn.Tag
	horizon := ""
	for _, r := range rows {
		if r.Section != "model" {
			continue
		}
		switch {
		case r.Stat == "purchase_model" && r.Text != nil && *r.Text != "":
			out = append(out, mlflowconn.Tag{Key: "purchase_model_chosen", Value: *r.Text})
		case r.Stat == "horizon" && r.Value != nil:
			horizon = paramText(*r.Value)
		}
	}
	if len(out) > 0 && horizon != "" && checked["horizon"] == nil {
		out = append(out, mlflowconn.Tag{Key: "horizon", Value: horizon})
	}
	return out
}

// metricKey keeps the characters MLflow allows in a key (letters, digits, _ - . space /).
func metricKey(s string) string {
	var b strings.Builder
	for _, c := range s {
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '_', c == '-', c == '.', c == ' ', c == '/':
			b.WriteRune(c)
		default:
			b.WriteByte('_')
		}
	}
	return b.String()
}

// maxMetricKey is MLflow's longest key (MAX_ENTITY_KEY_LENGTH = 250 characters in its
// validation.py, which the REST reference of log-batch repeats): longer ones fail the batch.
const maxMetricKey = mlflowconn.MaxKeyLength

// shortKey keeps a cleaned key within MLflow's limit: past it, the head and «_» and 8 hexadecimal
// digits of the hash of the whole raw name, so that two long names that start alike stay apart.
func shortKey(cleaned, raw string) string {
	if len(cleaned) <= maxMetricKey {
		return cleaned
	}
	sum := sha256.Sum256([]byte(raw))
	return cleaned[:maxMetricKey-9] + "_" + hex.EncodeToString(sum[:4])
}

// suffixed is key with «_n» at its end, cut so that the whole stays within the limit.
func suffixed(key string, n int) string {
	suffix := "_" + strconv.Itoa(n)
	if len(key)+len(suffix) > maxMetricKey {
		key = key[:maxMetricKey-len(suffix)]
	}
	return key + suffix
}

// metricNames gives each raw name of the run's scalar metrics its key in MLflow: cleaned, within
// the limit, and unique. A second raw name that is the same key once cleaned (`a b` and `a_b`)
// does not replace the first: it takes `_2`, `_3`… and is said.
type metricNames struct {
	rawOf   map[string]string // key -> the raw name that has it
	keyOf   map[string]string // raw name -> its key
	said    []string
	clashes int
}

// of is the key of a raw name; again false when this very raw name was given a key before.
func (m *metricNames) of(raw string) (string, bool) {
	if key, known := m.keyOf[raw]; known {
		return key, false
	}
	base := shortKey(metricKey(raw), raw)
	key := base
	for n := 2; ; n++ {
		holder, taken := m.rawOf[key]
		if !taken {
			m.rawOf[key], m.keyOf[raw] = raw, key
			return key, true
		}
		clashed := suffixed(base, n)
		if m.clashes < 3 {
			m.said = append(m.said, fmt.Sprintf("«%s» and «%s» are the same name once MLflow's characters are kept: the second is logged as «%s».",
				runes(holder, 60), runes(raw, 60), runes(clashed, 60)))
		}
		m.clashes++
		key = clashed
	}
}

func (m *metricNames) warnings() []string {
	if m.clashes == 0 {
		return nil
	}
	text := "Two metrics of the model had the same name in MLflow: " + strings.Join(m.said, " ")
	if m.clashes > len(m.said) {
		text += fmt.Sprintf(" And %d more like them.", m.clashes-len(m.said))
	}
	return []string{text}
}

// maxImportanceMetrics is how many predictors' importance a run logs (the first by rank).
const maxImportanceMetrics = 20

// learningMetricName is the name a learning curve's metric history carries: the library's own
// name of the loss (`learning_loss`) or the text of the stop metric (`learning_stop`).
func learningMetricName(level string, rows []resultRow) string {
	text := func(stat string) string {
		for _, r := range rows {
			if r.Section == "model" && r.Stat == stat && r.Text != nil {
				return *r.Text
			}
		}
		return ""
	}
	if level == "learning_stop" {
		if m := text("stop_metric"); m != "" {
			return m
		}
		return "stop"
	}
	losses := map[string]map[string]string{
		"xgboost":  {"regression": "rmse", "binary": "logloss", "multiclass": "mlogloss"},
		"catboost": {"regression": "RMSE", "binary": "Logloss", "multiclass": "MultiClass"},
	}
	if m := losses[text("library")][text("task")]; m != "" {
		return m
	}
	return "loss"
}

// modelMetrics are every number of the `model` section and, per assumption, its statistic
// and p-value (`assumption.<check>.statistic`, with the term or stratum between when the
// check has one: `assumption.<check>.<term>.p_value`), every number of an automatic choice's
// candidates (`candidate.<level>.<stat>`: aic, bic, log_lik, akaike_weight, chosen, parsimony…
// for the parametric survival distributions, aicc, aic, bic, converged, chosen for the ARIMA
// orders; a candidate's text `status` is not a metric) and each parameter's estimate
// (`parameter.<level>[.<level_2>].estimate`: the shape and the scale of a distribution). Each key
// once. A boosting model's
// results (they carry a `library` text) add what the spec lists for it: the diagnostics' value
// (`assumption.<check>[.<level_2>].value`), `importance.<predictor>.gain_share` and
// `.mean_abs_shap_share` for the first 20 predictors, and each learning curve as a metric
// history (`learning.train.<metric>`, `learning.validation.<metric>`, step = the round).
func modelMetrics(rows []resultRow, at time.Time) []mlflowconn.Metric {
	out, _ := modelMetricsChecked(rows, at)
	return out
}

// modelMetricsChecked is modelMetrics and the warnings to write in the run: the keys are within
// MLflow's length (a long one is cut and ends with a hash), and two names that are the same key
// once cleaned are both kept (the second with `_2`) and said.
func modelMetricsChecked(rows []resultRow, at time.Time) ([]mlflowconn.Metric, []string) {
	names := &metricNames{rawOf: map[string]string{}, keyOf: map[string]string{}}
	var out []mlflowconn.Metric
	add := func(raw string, v float64) {
		if key, isNew := names.of(raw); isNew {
			out = append(out, mlflowconn.Metric{Key: key, Value: v, Timestamp: at.UnixMilli()})
		}
	}
	boosting := false
	for _, r := range rows {
		if r.Section == "model" && r.Stat == "library" {
			boosting = true
		}
	}
	// a customer lifetime model's checks are also measures with a value and no verdict
	withValue := boosting || isCustomerLifetime(rows)
	predictors := map[string]bool{}
	for _, r := range rows {
		if r.Value == nil {
			continue
		}
		switch {
		case r.Section == "model":
			add(r.Stat, *r.Value)
		case r.Section == "assumption" && (r.Stat == "statistic" || r.Stat == "p_value" || (withValue && r.Stat == "value")) && r.Level != nil:
			key := "assumption." + *r.Level
			if r.Level2 != nil && *r.Level2 != "" {
				key += "." + *r.Level2
			}
			add(key+"."+r.Stat, *r.Value)
		case r.Section == "candidate" && r.Level != nil && r.Stat != "":
			add("candidate."+*r.Level+"."+r.Stat, *r.Value)
		case r.Section == "parameter" && r.Stat == "estimate" && r.Level != nil:
			key := "parameter." + *r.Level
			if r.Level2 != nil && *r.Level2 != "" {
				key += "." + *r.Level2
			}
			add(key+".estimate", *r.Value)
		case boosting && r.Section == "importance" && (r.Stat == "gain_share" || r.Stat == "mean_abs_shap_share") && r.Level != nil:
			if !predictors[*r.Level] && len(predictors) >= maxImportanceMetrics {
				continue
			}
			predictors[*r.Level] = true
			add("importance."+*r.Level+"."+r.Stat, *r.Value)
		case boosting && r.Section == "curve" && r.Stat == "y" && r.Level != nil && r.Level2 != nil && r.X0 != nil &&
			strings.HasPrefix(*r.Level, "learning_") && (*r.Level2 == "train" || *r.Level2 == "validation"):
			raw := "learning." + *r.Level2 + "." + learningMetricName(*r.Level, rows)
			out = append(out, mlflowconn.Metric{Key: shortKey(metricKey(raw), raw),
				Value: *r.Value, Timestamp: at.UnixMilli(), Step: int64(*r.X0)})
		}
	}
	return out, names.warnings()
}

// coefficientsCSV is the `coef` section as a table: term, level, then one column per
// statistic in the order met; nil when the model has no coefficients.
func coefficientsCSV(rows []resultRow) []byte {
	type term struct{ term, level string }
	var terms []term
	var stats []string
	cells := map[term]map[string]string{}
	for _, r := range rows {
		if r.Section != "coef" {
			continue
		}
		t := term{deref(r.Level), deref(r.Level2)}
		if cells[t] == nil {
			cells[t] = map[string]string{}
			terms = append(terms, t)
		}
		if !containsString(stats, r.Stat) {
			stats = append(stats, r.Stat)
		}
		switch {
		case r.Value != nil:
			cells[t][r.Stat] = strconv.FormatFloat(*r.Value, 'g', -1, 64)
		case r.Text != nil:
			cells[t][r.Stat] = *r.Text
		}
	}
	if len(terms) == 0 {
		return nil
	}
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	_ = w.Write(append([]string{"term", "level"}, stats...))
	for _, t := range terms {
		line := []string{t.term, t.level}
		for _, s := range stats {
			line = append(line, cells[t][s])
		}
		_ = w.Write(line)
	}
	w.Flush()
	return buf.Bytes()
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// ── the engine's part: the step's files ──────────────────────────────────────

// modelResultRows are the model, assumption and coef sections of the step's results, read back
// by the engine from the scripts bucket (small by construction).
func (d *Deps) modelResultRows(ctx context.Context, storage *scriptStorage, key string) ([]resultRow, error) {
	cctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/results/rows", pyjson.Object{{Key: "connection", Value: storage.payload()},
		{Key: "key", Value: key}, {Key: "sections", Value: []any{"model", "assumption", "coef", "importance", "curve", "candidate", "parameter"}}})
	if err != nil {
		return nil, err
	}
	if res.StatusCode >= 400 {
		return nil, &scriptFailed{"Tabularia could not read the step's results back: " + engineDetail(raw)}
	}
	var got struct {
		Rows []resultRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		return nil, err
	}
	return got.Rows, nil
}

// openScriptObject is one file of the scripts bucket, streamed by the engine: nil when it is not
// there. The caller closes it.
func (d *Deps) openScriptObject(ctx context.Context, storage *scriptStorage, key string) (io.ReadCloser, int64, error) {
	body, err := json.Marshal(map[string]any{"connection": storage.payload(), "key": key})
	if err != nil {
		return nil, 0, err
	}
	req, err := d.Engine.Request(ctx, http.MethodPost, "/scripts/objects/read", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := d.Engine.Do(req)
	if err != nil {
		return nil, 0, err
	}
	switch {
	case res.StatusCode == http.StatusNotFound:
		res.Body.Close()
		return nil, 0, nil
	case res.StatusCode >= 400:
		raw, _ := io.ReadAll(io.LimitReader(res.Body, 64<<10))
		res.Body.Close()
		return nil, 0, &scriptFailed{"Tabularia could not read " + key[strings.LastIndex(key, "/")+1:] + " from the scripts bucket: " + engineDetail(raw)}
	}
	if res.ContentLength < 0 { // a length is needed to upload it: read whole
		b, err := io.ReadAll(res.Body)
		res.Body.Close()
		if err != nil {
			return nil, 0, err
		}
		return io.NopCloser(bytes.NewReader(b)), int64(len(b)), nil
	}
	return res.Body, res.ContentLength, nil
}

// modelReportPDF is the step's report as the Report tab draws it, in the flow's owner's
// language, rendered by the engine as an export of its own.
func (q *queue) modelReportPDF(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun, f modelFiles) ([]byte, error) {
	d := q.d
	locale := "en"
	var l *string
	err := d.DB.QueryRow(ctx, `SELECT u.locale FROM flows f JOIN users u ON u.id = f.owner_id WHERE f.id = $1`, r.flow.ID).Scan(&l)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if l != nil && statReportLocales[*l] {
		locale = *l
	}
	owner := fmt.Sprintf("mlflow:%d", child.id)
	o := pyjson.Object{{Key: "bucket", Value: f.engineBucket}, {Key: "results_key", Value: f.engineResultsKey}, {Key: "model", Value: s.model},
		{Key: "params", Value: pyjsonOf(s.checked)}, {Key: "format", Value: "pdf"}, {Key: "locale", Value: locale},
		{Key: "step", Value: truncateRunes(s.label, statReportStepMax)}, {Key: "owner", Value: owner}, {Key: "wait_seconds", Value: int64(600)}}
	if sample, isList := child.info.Value("sample").([]any); isList && len(sample) > 0 {
		o = o.Set("sample", sample)
	}
	cctx, cancel := context.WithTimeout(ctx, mlflowReportTimeout)
	defer cancel()
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/reports/model", o)
	if err != nil {
		return nil, err
	}
	if res.StatusCode >= 400 {
		return nil, &scriptFailed{engineDetail(raw)}
	}
	var started struct {
		TaskID string `json:"task_id"`
	}
	if json.Unmarshal(raw, &started) != nil || started.TaskID == "" {
		return nil, &scriptFailed{"the engine did not start the report"}
	}
	who := "?owner=" + url.QueryEscape(owner)
	for {
		res, raw, err := d.Engine.JSON(cctx, http.MethodGet, "/tasks/exports/"+url.PathEscape(started.TaskID)+who, nil)
		if err != nil {
			return nil, err
		}
		var st struct {
			Status string `json:"status"`
			Detail string `json:"detail"`
		}
		if res.StatusCode >= 400 || json.Unmarshal(raw, &st) != nil {
			return nil, &scriptFailed{engineDetail(raw)}
		}
		if st.Status == "failed" {
			return nil, &scriptFailed{runes(st.Detail, 300)}
		}
		if st.Status == "ready" {
			break
		}
		select {
		case <-cctx.Done():
			return nil, &scriptFailed{"it took longer than " + mlflowReportTimeout.String()}
		case <-time.After(mlflowReportPoll):
		}
	}
	req, err := d.Engine.Request(cctx, http.MethodGet, "/tasks/exports/"+url.PathEscape(started.TaskID)+"/file"+who, nil)
	if err != nil {
		return nil, err
	}
	fres, err := d.Engine.Do(req)
	if err != nil {
		return nil, err
	}
	defer fres.Body.Close()
	pdf, err := io.ReadAll(io.LimitReader(fres.Body, 256<<20))
	if err != nil {
		return nil, err
	}
	if fres.StatusCode >= 400 {
		return nil, &scriptFailed{engineDetail(pdf)}
	}
	return pdf, nil
}

// ── a failed step ────────────────────────────────────────────────────────────

// logFailedModel logs a flow run's model step that failed after its Job ran as a FAILED MLflow
// run, with its tags, parameters and error: as far as MLflow answers, and never changing why
// the step failed (a failure to log it is a warning on the run page).
func (q *queue) logFailedModel(ctx context.Context, r *scriptNodeRun, s *modelStep, child *childRun, stepErr error) {
	if s.mlflow == nil || child.info.Value("mlflow") != nil || ctx.Err() != nil {
		return
	}
	if n, ok := pyInt(child.info.Value("attempts")); !ok || n < 1 {
		return // it did not get as far as the Job: nothing was fitted
	}
	rec := &mlflowRecord{plan: s.mlflow, failed: true}
	err := func() error {
		tr := mlflowconn.NewTracker(s.mlflow.cfg)
		var err error
		if rec.experimentID, rec.experimentCreated, err = tr.Experiment(ctx, s.mlflow.experiment); err != nil {
			return err
		}
		started := time.Now()
		run, err := tr.CreateRun(ctx, rec.experimentID, mlflowRunName(r.flow.Name, s.label, started), started)
		if err != nil {
			return err
		}
		rec.runID = run.ID
		tags, err := q.mlflowTags(ctx, r, s, child)
		if err != nil {
			return err
		}
		why := errorMessage(stepErr)
		var sf *scriptFailed
		if errors.As(stepErr, &sf) {
			why = sf.msg
		}
		tags = append(tags, mlflowconn.Tag{Key: "tabularia.error", Value: why})
		logErr := tr.LogBatch(ctx, run.ID, flattenParams(s.checked), nil, tags)
		if err := tr.EndRun(ctx, run.ID, true, time.Now()); err != nil {
			return err
		}
		return logErr
	}()
	if err != nil {
		rec.warnings = append(rec.warnings, "The failure could not be logged to MLflow: "+mlflowWhy(err))
	}
	if err := rec.save(ctx, child); err != nil {
		q.d.Log.Warn("model: the MLflow record of a failed step was not saved", "run", child.id, "err", err)
	}
}
