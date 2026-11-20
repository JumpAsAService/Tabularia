package api

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelcatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// A Predict node (`model_predict`) as a step of a run or of a trial
// (docs/specs/statistical-models.md, «Predict»): a model version Tabularia registered in MLflow,
// applied to the rows that reach the node by the Job of the model's group, which adds the
// predicted columns. The gateway does what needs MLflow; the Job never sees it.
//
//  1. Before anything (planPredict): the connection is an MLflow one the person may CONNECT; the
//     version is read from MLflow — the one named, or «latest», the highest version of that
//     registered model that carries the tag tabularia.model (Tabularia's versions only); it must
//     carry Tabularia's tags; its MLmodel must have the `tabularia` flavor; the sha256 of its
//     model.json must equal the version's tag tabularia.card_sha256; the card must be one this
//     Tabularia applies (below); the card's group must be enabled, ready and usable by the
//     person; the options must be the model's; no added column may be one the card reads.
//     Otherwise the step fails with the reason, before any row, input or Job exists.
//  2. The input: the whole chain before the node (every column: the output gives them back)
//     written into the scripts bucket; past the group's max_rows the step fails there.
//  3. The card, as the gateway checked it, written by the engine beside the input
//     (<step>/model.json): the Job reads TABULARIA_MODEL_IN, never MLflow, and holds no MLflow
//     variable or secret. No serialized object (native.rds) is ever read.
//     A boosting model also keeps a model file (xgboost.json or catboost.cbm) beside its card:
//     the card must declare it (boosting.model_file: the name of its library's file, its size at
//     most max_model_file_bytes, its sha256), and MLflow must have it. At the run the gateway
//     streams it from the artifact proxy into the engine's POST /scripts/objects/upload, which
//     hashes it while it is received and writes it to <step>/model_file only when its size
//     and sha256 are the card's; the Job gets TABULARIA_MODEL_FILE_IN, and never a pickle or an
//     MLflow credential. The file leaves the bucket with the step's other files.
//  4. The Job: the group's image by digest, its runner in mode predict, internet access off —
//     the machine of a model step (modelAttempts: attempts, timeout, the trials' quota, the
//     runner's refusal with its sentence).
//  5. The import: TABULARIA_OUTPUT checked as parquet and copied into Tabularia's storage, which
//     the nodes after it read; the runner's warnings (rows with a level the model did not see)
//     kept for the run page.
//  6. The step's files leave the scripts bucket, by exact key.
//
// Which card is applied (the compatibility rule): a card is applied by the current ready
// version of its group when its `format` is "tabularia-model" and its `format_version` is one
// this Tabularia's runners read (predictCardFormats), its `model` is the version's tag
// tabularia.model and a model of this Tabularia, its `group` that model's group (and the tag
// tabularia.group, when given), its `variant` one the catalogue knows. The group's version it
// was fitted with (`group_version`, the tag tabularia.group_version) need not be the version that
// applies it: the card holds numbers, which every version's runner reads the same way, and each
// version's self-test checks that predicting from a card gives the fitted values. A different
// version is recorded and said on the run page. A change the runners cannot read in the old way
// is a new format_version, which this rule refuses with its sentence.
//
// A Predict step logs nothing to MLflow; its run records the registered model, the version (the
// one «latest» resolved to), the card's hash and the group's versions.

// predictCardFormats are the card formats this Tabularia's runners read.
var predictCardFormats = map[int]bool{1: true}

// predictMLmodelMax is the most bytes of an MLmodel read.
const predictMLmodelMax = 256 << 10

// predictStep is what the checks made of a Predict node.
type predictStep struct {
	modelStep
	conn         *mlflowConn
	experiment   string
	registered   string
	asked        string // the node's version: a number, or «latest»
	version      *mlflowconn.ModelVersion
	card         []byte
	cardSHA      string
	variant      string
	file         *cardModelFile // the model file the card declares (a boosting model), nil for the others
	groupVersion any            // the card's group_version: the group's version it was fitted with
	known        int64
	notes        []string
}

// predictLatest is the version that means «the newest one Tabularia registered».
const predictLatest = "latest"

// predictTrialReadTimeout bounds the reading of MLflow (the version, its files, its card of at most
// 1 MB) that a trial does inside the editor's HTTP request. A variable: the tests shorten it.
var predictTrialReadTimeout = 45 * time.Second

var versionNumber = regexp.MustCompile(`^[1-9][0-9]{0,17}$`)

// planPredict is «Before anything» for a Predict node: the step, or why it cannot run (a
// *scriptFailed), before any row, input or Job exists. It reads MLflow.
func (q *queue) planPredict(ctx context.Context, user *auth.User, node pyjson.Object) (*predictStep, error) {
	s := &predictStep{modelStep: modelStep{label: modelLabel(node), predict: true, output: "rows"}}
	refuse := func(format string, args ...any) (*predictStep, error) {
		return nil, &scriptFailed{s.what() + ": " + fmt.Sprintf(format, args...)}
	}
	if why := modelsOff(q.sc); why != "" {
		return nil, &scriptFailed{why}
	}
	data := nodeData(node)
	id, isID := pyjson.IsInt(data.Value("connectionId"))
	if !isID {
		return refuse("choose the MLflow connection the model is read from")
	}
	s.experiment = strings.TrimSpace(pyjson.Str(pyjson.Or(data.Value("experiment"), "")))
	s.registered = strings.TrimSpace(pyjson.Str(pyjson.Or(data.Value("registeredModel"), "")))
	s.asked = strings.TrimSpace(pyjson.Str(pyjson.Or(data.Value("version"), "")))
	switch {
	case s.registered == "" || s.asked == "":
		return refuse("choose the model's version (or its latest version)")
	case s.asked != predictLatest && !versionNumber.MatchString(s.asked):
		return refuse("«%s» is not a version: a number, or «latest»", s.asked)
	}
	params, err := modelParams(data)
	var sf *scriptFailed
	if errors.As(err, &sf) {
		return refuse("%s", sf.msg)
	}
	if err != nil {
		return nil, err
	}
	conn, err := q.d.mlflowConnection(ctx, user, id)
	var refused *mlflowConnRefused
	if errors.As(err, &refused) {
		return refuse("%s", refused.msg)
	}
	if err != nil {
		return nil, err
	}
	s.conn = conn
	tr := mlflowconn.NewTracker(conn.cfg)
	mlflowFailed := func(err error) (*predictStep, error) {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var nf *mlflowconn.NotFound
		if errors.As(err, &nf) {
			return refuse("%s", nf.Error())
		}
		return refuse("MLflow could not be read: %s", mlflowWhy(err))
	}
	// the version: the one named, or the newest one Tabularia registered
	if s.asked == predictLatest {
		versions, err := tr.VersionsOf(ctx, s.registered)
		if err != nil {
			return mlflowFailed(err)
		}
		for i := range versions {
			v := versions[i]
			if v.Tags["tabularia.model"] != "" && (s.version == nil || v.Number() > s.version.Number()) {
				s.version = &v
			}
		}
		if s.version == nil {
			return refuse("the registered model «%s» has no version Tabularia registered: run the flow that registers it first", s.registered)
		}
		if s.known, err = predictKnownLatest(ctx, q.d.DB, conn.id, s.registered); err != nil {
			return nil, err
		}
	} else if s.version, err = tr.Version(ctx, s.registered, s.asked); err != nil {
		return mlflowFailed(err)
	}
	v := s.version
	name := fmt.Sprintf("the version %s of «%s»", v.Version, v.Name)
	s.model = v.Tags["tabularia.model"]
	s.cardSHA = strings.ToLower(strings.TrimSpace(v.Tags["tabularia.card_sha256"]))
	switch {
	case s.model == "":
		return refuse("%s was not registered by Tabularia (it has no tag tabularia.model): Predict applies only Tabularia's models", name)
	case s.cardSHA == "":
		return refuse("%s has no fingerprint of its model card (the tag tabularia.card_sha256): it is not applied", name)
	}
	s.group = modelcatalog.GroupOf(s.model)
	if s.group == "" {
		return refuse("%s holds the model «%s», which this version of Tabularia does not know", name, s.model)
	}
	if g := v.Tags["tabularia.group"]; g != "" && g != s.group {
		return refuse("%s says it belongs to the group «%s», but the model «%s» is in «%s»", name, g, s.model, s.group)
	}
	// the card, from the artifact proxy, checked against the version's tag
	where := mlflowconn.ArtifactPath(v.Source)
	if where == "" {
		return refuse("the files of %s are not served by the MLflow server (no artifact proxy): Tabularia cannot read its card", name)
	}
	var nf *mlflowconn.NotFound
	mlmodel, err := tr.Download(ctx, where, "MLmodel", predictMLmodelMax)
	if errors.As(err, &nf) {
		return refuse("%s has no MLmodel among its files: it is not a model Tabularia applies", name)
	}
	if err != nil {
		return mlflowFailed(err)
	}
	if !hasTabulariaFlavor(mlmodel) {
		return refuse("the MLmodel of %s has no `tabularia` flavor: it is not a model Tabularia applies", name)
	}
	maxCard := int64(modelcatalog.Limit("max_card_bytes"))
	if maxCard <= 0 {
		maxCard = 1 << 20
	}
	if s.card, err = tr.Download(ctx, where, "model.json", maxCard); err != nil {
		var tl *mlflowconn.TooLarge
		if errors.As(err, &tl) {
			return refuse("the model card of %s is larger than a card can be (%d bytes)", name, maxCard)
		}
		if errors.As(err, &nf) {
			return refuse("%s has no model card (model.json) among its files: it is not a model Tabularia applies", name)
		}
		return mlflowFailed(err)
	}
	sum := sha256.Sum256(s.card)
	if hex.EncodeToString(sum[:]) != s.cardSHA {
		return refuse("the model card of %s does not match the fingerprint MLflow keeps for it (tabularia.card_sha256): "+
			"it was changed after it was registered, and is not applied", name)
	}
	var card predictCard
	if err := json.Unmarshal(s.card, &card); err != nil {
		return refuse("the model card of %s is not JSON: it is not applied", name)
	}
	if why := card.incompatible(s.model, s.group); why != "" {
		return refuse("the model card of %s %s", name, why)
	}
	s.variant, s.groupVersion = card.Variant, card.GroupVersion
	if refusal, err := checkModelFile(ctx, tr, where, name, s, &card); err != nil {
		return mlflowFailed(err)
	} else if refusal != "" {
		return refuse("%s", refusal)
	}
	// the group: enabled, ready, usable by the person
	if s.img, err = checkModelGroup(ctx, q.d, user, s.group); err != nil {
		if errors.As(err, &sf) {
			return refuse("%s", sf.msg)
		}
		return nil, err
	}
	if gv, isNum := card.GroupVersion.(float64); isNum && int64(gv) != s.img.Version {
		s.notes = append(s.notes, fmt.Sprintf("The model was fitted with version %d of the group «%s» and applied with version %d: the card's numbers are read the same way.",
			int64(gv), s.group, s.img.Version))
	}
	// the options, and the columns they add
	if s.checked, err = modelcatalog.CheckPredict(s.model, s.variant, params); err != nil {
		var ce *modelcatalog.Error
		if errors.As(err, &ce) {
			return refuse("%s", ce.Message)
		}
		return nil, err
	}
	reads := card.columns()
	for _, c := range modelcatalog.PredictColumns(s.model, s.variant, s.checked) {
		if reads[c] {
			return refuse("the model reads a column «%s», which the prediction would overwrite: rename it before the node", c)
		}
	}
	if s.model == "arima" {
		s.output = "forecast"
	}
	// the Job's limits, as a model node's
	timeout, retries, delay, err := retrySettings(data)
	if err != nil {
		return refuse("its timeout and retries are whole numbers")
	}
	switch {
	case timeout < 1:
		return refuse("its timeout must be at least one second")
	case s.img.MaxTimeoutSeconds > 0 && timeout > s.img.MaxTimeoutSeconds:
		return refuse("its timeout (%d s) is longer than the group «%s» allows (%d s)", timeout, s.group, s.img.MaxTimeoutSeconds)
	case retries < 0 || retries > 5:
		return refuse("from 0 to 5 retries")
	case delay < 0:
		delay = 0
	}
	if s.budget, err = budgetSeconds(data); err != nil {
		return nil, err
	}
	s.timeout, s.retries, s.delay = timeout, retries, delay
	return s, nil
}

// hasTabulariaFlavor says whether an MLmodel (YAML, as the runner writes it) has the flavor
// `tabularia` under `flavors:`.
func hasTabulariaFlavor(mlmodel []byte) bool {
	in := false
	for _, line := range strings.Split(strings.ReplaceAll(string(mlmodel), "\r\n", "\n"), "\n") {
		trimmed := strings.TrimSpace(line)
		switch {
		case trimmed == "" || strings.HasPrefix(trimmed, "#"):
		case !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t"):
			in = strings.TrimRight(trimmed, " ") == "flavors:"
		case in && strings.TrimRight(trimmed, " ") == "tabularia:" && strings.HasPrefix(line, "  ") && !strings.HasPrefix(line, "   "):
			return true
		}
	}
	return false
}

// predictCard is what the gateway reads of a model card (card.schema.json): what decides
// whether it is applied, and the columns it reads.
type predictCard struct {
	Format        string `json:"format"`
	FormatVersion int    `json:"format_version"`
	Model         string `json:"model"`
	Variant       string `json:"variant"`
	Group         string `json:"group"`
	GroupVersion  any    `json:"group_version"`
	Inputs        []struct {
		Name string `json:"name"`
	} `json:"inputs"`
	Params   map[string]any `json:"params"`
	Boosting *struct {
		Library   string         `json:"library"`
		ModelFile *cardModelFile `json:"model_file"`
	} `json:"boosting"`
	KM struct {
		Time   string `json:"time_column"`
		Strata string `json:"strata_column"`
	} `json:"kaplan_meier"`
	Cox struct {
		Time  string `json:"time_column"`
		Start string `json:"start_column"`
	} `json:"cox"`
}

// incompatible is why this Tabularia does not apply the card ("" when it does): the rule of
// the file's head.
func (c *predictCard) incompatible(model, group string) string {
	switch {
	case c.Format != "tabularia-model":
		return "is not a Tabularia model card"
	case !predictCardFormats[c.FormatVersion]:
		return fmt.Sprintf("has the format version %d, which this version of Tabularia does not read (it reads 1): fit the model again", c.FormatVersion)
	case c.Model != model:
		return fmt.Sprintf("holds the model «%s», but the version says «%s»", c.Model, model)
	case c.Group != group:
		return fmt.Sprintf("says the group «%s», but the model «%s» is in «%s»", c.Group, model, group)
	case !modelcatalog.HasVariant(model, c.Variant):
		return fmt.Sprintf("was fitted with the variant «%s», which this version of Tabularia does not know", c.Variant)
	}
	return ""
}

// columns are the columns the card's model reads of the rows.
func (c *predictCard) columns() map[string]bool {
	out := map[string]bool{}
	add := func(s string) {
		if s != "" {
			out[s] = true
		}
	}
	for _, in := range c.Inputs {
		add(in.Name)
	}
	add(c.KM.Time)
	add(c.KM.Strata)
	add(c.Cox.Time)
	add(c.Cox.Start)
	for _, k := range []string{"time", "value"} { // ARIMA's series
		if s, ok := c.Params[k].(string); ok {
			add(s)
		}
	}
	return out
}

// ── «not up to date» ─────────────────────────────────────────────────────────

// predictFingerprint is what a Predict node's output is up to date with: the model it applies
// (connection, registered model, version) and its options, the chain before it (as a model
// node's), and, for «latest», the newest version of that registered model a run of this
// Tabularia registered on the same tracking server (known: predictKnownLatest). A new version
// registered by a flow run of this Tabularia thus makes the output «not up to date» without
// asking MLflow at each look; one registered elsewhere (another installation pointing at the
// same server) is not seen until the node runs again.
func predictFingerprint(definition, node pyjson.Object, known int64) string {
	data := nodeData(node)
	own := pyjson.Object{{Key: "connectionId", Value: data.Value("connectionId")}, {Key: "registeredModel", Value: data.Value("registeredModel")},
		{Key: "version", Value: data.Value("version")}, {Key: "params", Value: pyjson.Or(data.Value("params"), pyjson.Object{})}}
	if data.Value("version") == predictLatest {
		own = append(own, pyjson.KV{Key: "known", Value: known})
	}
	return chainFingerprint(definition, node, "predict:"+pyDumps(own))
}

// predictKnownLatest is the newest version of the registered model that a model step of this
// Tabularia registered on the tracking server of the connection (any connection to the same
// tracking URI), 0 for none: what «latest» is known to be without asking MLflow.
func predictKnownLatest(ctx context.Context, q perm.Querier, connectionID int64, model string) (int64, error) {
	var n int64
	err := q.QueryRow(ctx, `SELECT coalesce(max(CASE WHEN r.mlflow_model_version ~ '^[0-9]{1,18}$' THEN r.mlflow_model_version::bigint END), 0)
		FROM runs r JOIN connections c ON c.id = r.mlflow_connection_id
		WHERE r.kind = 'model' AND r.mlflow_model_name = $2
		AND rtrim(c.host, '/') = (SELECT rtrim(host, '/') FROM connections WHERE id = $1)`, connectionID, model).Scan(&n)
	return n, err
}

// predictNodeFingerprint is predictFingerprint with what is known of «latest».
func predictNodeFingerprint(ctx context.Context, q perm.Querier, definition, node pyjson.Object) (string, error) {
	data := nodeData(node)
	var known int64
	if data.Value("version") == predictLatest {
		id, isID := pyjson.IsInt(data.Value("connectionId"))
		model, _ := data.Value("registeredModel").(string)
		if isID && model != "" {
			var err error
			if known, err = predictKnownLatest(ctx, q, id, strings.TrimSpace(model)); err != nil {
				return "", err
			}
		}
	}
	return predictFingerprint(definition, node, known), nil
}

// ── the step ─────────────────────────────────────────────────────────────────

// runPredict is a flow's Predict node as a step: where the nodes after it read, nil when it
// failed.
func (q *queue) runPredict(ctx context.Context, r *scriptNodeRun) (*flowresolver.Source, error) {
	d := q.d
	s, err := q.planPredict(ctx, r.user, r.node)
	if err != nil {
		return nil, err
	}
	storage, err := loadScriptStorage(ctx, d.DB)
	if err != nil {
		return nil, err
	}
	if !storage.ready() {
		return nil, &scriptFailed{"the scripts bucket is not configured (Administration → Scripting environments)"}
	}
	task, err := modelgroups.PredictTask(s.model, s.variant, s.checked)
	if err != nil {
		var ce *modelcatalog.Error
		if errors.As(err, &ce) {
			return nil, &scriptFailed{s.what() + ": " + ce.Message}
		}
		return nil, err
	}
	nodeID := r.node.Value("id")
	trial := r.trial != 0
	ns := q.sc.Scripts.Namespace
	if trial && q.sc.Scripts.TrialNamespace != "" {
		ns = q.sc.Scripts.TrialNamespace
	}
	stepID := newUUIDText()
	prefix := fmt.Sprintf("env-%d/%s", s.img.EnvironmentID, stepID)
	inKey, outKey, artifacts, cardKey := prefix+"/input.parquet", prefix+"/output.parquet", prefix+"/artifacts/", prefix+"/model.json"
	// fileKey: a boosting model's xgboost.json or catboost.cbm, beside its card; deleted with the rest
	fileKey := prefix + "/model_file"
	written := []string{inKey, outKey, cardKey, fileKey, artifacts + "run.json", artifacts + "error.json"}
	bucket := d.Config.EngineBucket
	engineOutKey := "out/" + newHex() + ".parquet"
	modelURL := fmt.Sprintf("%s/#/models/%s/versions/%s", s.conn.ui, url.PathEscape(s.version.Name), url.PathEscape(s.version.Version))
	ml := pyjson.Object{{Key: "connection_id", Value: s.conn.id}, {Key: "connection", Value: s.conn.name},
		{Key: "experiment", Value: s.experiment}, {Key: "run_id", Value: nilIfEmpty(s.version.RunID)},
		{Key: "registered_model", Value: s.version.Name}, {Key: "model_version", Value: s.version.Version}, {Key: "model_url", Value: modelURL},
		{Key: "warnings", Value: stringsAny(s.notes)}}
	info := pyjson.Object{{Key: "node_id", Value: nodeID}, {Key: "label", Value: s.label}, {Key: "model", Value: s.model},
		{Key: "variant", Value: s.variant}, {Key: "params", Value: pyjsonOf(s.checked)}, {Key: "output_kind", Value: s.output},
		{Key: "group", Value: s.group}, {Key: "environment", Value: s.group}, {Key: "language", Value: modelgroupsLanguage(s.group)},
		{Key: "version", Value: s.img.Version}, {Key: "image", Value: s.img.Image}, {Key: "step_id", Value: stepID},
		{Key: "registered_model", Value: s.version.Name}, {Key: "asked_version", Value: s.asked}, {Key: "model_version", Value: s.version.Version},
		{Key: "card_sha256", Value: s.cardSHA}, {Key: "card_group_version", Value: pyjsonOf(s.groupVersion)},
		{Key: "input", Value: true}, {Key: "output", Value: true}, {Key: "sample", Value: []any{}},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "log", Value: ""}, {Key: "trial", Value: trial},
		{Key: "fingerprint", Value: predictFingerprint(r.definition, r.node, s.known)}, {Key: "budget_seconds", Value: s.budget},
		{Key: "namespace", Value: ns}, {Key: "internet_access", Value: false}, {Key: "input_rows", Value: nil}, {Key: "mlflow", Value: ml}}
	if s.file != nil {
		info = append(info, pyjson.KV{Key: "model_file_sha256", Value: s.file.SHA256})
	}
	childID, err := r.startChild(ctx, d, "model_predict", r.flow.ID, fmt.Sprintf("%s v%d", s.group, s.img.Version), bucket, engineOutKey, info)
	if err != nil {
		return nil, err
	}
	child := &childRun{d: d, id: childID, info: info}
	if err := child.save(ctx, "mlflow_connection_id", s.conn.id, "mlflow_run_id", nilIfEmpty(s.version.RunID),
		"mlflow_model_name", s.version.Name, "mlflow_model_version", s.version.Version); err != nil {
		return nil, err
	}
	r.io()
	defer func() { d.deleteObjects(context.Background(), storage, written) }()
	out, err := func() (*flowresolver.Source, error) {
		in, err := modelPrepareInput(q, ctx, r, child, storage, inKey, nil, s.what())
		if err != nil {
			return nil, err
		}
		child.info = child.info.Set("sample", in.sample)
		if in.rows != nil {
			child.info = child.info.Set("input_rows", *in.rows)
		}
		if err := child.save(ctx); err != nil {
			return nil, err
		}
		if in.rows != nil && s.img.MaxRows > 0 && *in.rows > s.img.MaxRows {
			return nil, &scriptFailed{fmt.Sprintf("%s: the prediction would read %s rows; the group «%s» allows %s: filter or sample "+
				"before the node, or ask an administrator", s.what(), thousands(*in.rows), s.group, thousands(s.img.MaxRows))}
		}
		if err := d.writeScriptText(ctx, storage, cardKey, s.card); err != nil {
			return nil, err
		}
		if s.file != nil {
			if err := q.stageModelFile(ctx, s, storage, fileKey); err != nil {
				return nil, err
			}
		}
		runID := r.orchestrationOf(childID)
		task.GroupVersion = s.img.Version
		task.IDs = modelgroups.IDs{FlowID: r.flow.ID, NodeID: pyjson.Str(nodeID), RunID: runID, StepRunID: childID}
		if in.rows != nil {
			task.InputRows = *in.rows
		}
		if trial {
			task.RowsFrom = "trial"
		}
		code, err := modelgroups.CodeFiles(s.group, task.JSON(), false)
		if err != nil {
			return nil, err
		}
		cm, items := modelgroups.ConfigMap(code)
		codeData := pyjson.Object{}
		for i := range code {
			k := fmt.Sprintf("f%d", i)
			codeData = append(codeData, pyjson.KV{Key: k, Value: cm[k]})
		}
		itemList := make([]any, len(items))
		for i, it := range items {
			itemList[i] = pyjson.Object{{Key: "key", Value: it["key"]}, {Key: "path", Value: it["path"]}}
		}
		s3 := func(key string) string { return "s3://" + storage.Bucket + "/" + key }
		threads := fmt.Sprint(threadsOf(s.img.CPULimit))
		base := merge(pyjson.Object{{Key: modelgroups.EnvInput, Value: s3(inKey)}, {Key: modelgroups.EnvOutput, Value: s3(outKey)},
			{Key: modelgroups.EnvResults, Value: ""}, {Key: modelgroups.EnvArtifacts, Value: s3(artifacts)},
			{Key: modelgroups.EnvModelIn, Value: s3(cardKey)},
			{Key: "TABULARIA_STEP_ID", Value: stepID}, {Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(runID)},
			{Key: "TABULARIA_FLOW_ID", Value: fmt.Sprint(r.flow.ID)}, {Key: "TABULARIA_NODE_ID", Value: pyjson.Str(nodeID)},
			{Key: "OMP_NUM_THREADS", Value: threads}, {Key: "OPENBLAS_NUM_THREADS", Value: threads},
			{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}}, storage.env())
		if s.file != nil { // the model file, beside the card: the Job reads it from the bucket
			base = base.Set(modelgroups.EnvModelFileIn, s3(fileKey))
		}
		key, err := d.Fernet.Decrypt(storage.SecretEnc)
		if err != nil {
			return nil, err
		}
		secret := pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "models"},
			{Key: labelEnv, Value: fmt.Sprint(s.img.EnvironmentID)}, {Key: labelModelGroup, Value: s.group}, {Key: labelModel, Value: s.model},
			{Key: labelScriptRun, Value: fmt.Sprint(childID)}, {Key: labelOrchestration, Value: fmt.Sprint(runID)},
			{Key: "tabularia.io/stage", Value: "run"}}
		offline := offlineNetwork(ctx, storage, d.Log)
		js := d.jobSettings()
		if js.logMaxBytes <= 0 {
			js.logMaxBytes = 256 * 1024
		}
		manifest := func(name string, attempt int64) pyjson.Object {
			return modelJobManifest(js, name, s.img, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, s.timeout, itemList, name+"-code", name+"-env", offline)
		}
		ok, refused, reason, err := q.modelAttempts(ctx, child, &s.modelStep, runID, storage, outKey, true, manifest, codeData, secret, ns, js)
		if err != nil {
			return nil, err
		}
		if refused != "" {
			return nil, &scriptFailed{s.what() + ": " + refused}
		}
		if !ok {
			return nil, &scriptFailed{fmt.Sprintf("%s failed: %s", s.what(), reason)}
		}
		return q.importPredict(ctx, s, child, storage, outKey, artifacts, engineOutKey)
	}()
	return q.closeChild(ctx, child, out, err, s.what())
}

// importPredict is «The import»: the rows copied into Tabularia's storage, the runner's warnings
// kept; the run closed SUCCESS with its key.
func (q *queue) importPredict(ctx context.Context, s *predictStep, child *childRun, storage *scriptStorage, outKey, artifacts,
	engineOutKey string) (*flowresolver.Source, error) {
	d := q.d
	bucket := d.Config.EngineBucket
	cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/output/import", pyjson.Object{{Key: "connection", Value: storage.payload()},
		{Key: "key", Value: outKey}, {Key: "bucket", Value: bucket}, {Key: "output_key", Value: engineOutKey}})
	cancel()
	if err != nil {
		return nil, err
	}
	if res.StatusCode >= 400 {
		return nil, &scriptFailed{fmt.Sprintf("%s: its rows could not be read: %s", s.what(), engineDetail(raw))}
	}
	v, err := pyjson.Loads(string(raw))
	if err != nil {
		return nil, &pyJSONError{err}
	}
	got := pyjson.Obj(v)
	if !pyjson.Truthy(got.Value("found")) {
		return nil, &scriptFailed{fmt.Sprintf("%s did not write its rows: a fault of the runner (its log may say more)", s.what())}
	}
	rows, err := intColumn(got.Value("rows"))
	if err != nil {
		return nil, err
	}
	if warnings := d.runnerWarnings(ctx, storage, artifacts+"run.json"); len(warnings) > 0 {
		ml := pyjson.Obj(child.info.Value("mlflow"))
		all, _ := ml.Value("warnings").([]any)
		for _, w := range warnings {
			all = append(all, w)
		}
		child.info = child.info.Set("mlflow", ml.Set("warnings", all))
	}
	if err := child.save(ctx, "output_bucket", bucket, "output_key", engineOutKey, "rows_written", rows); err != nil {
		return nil, err
	}
	if err := child.save(ctx, "status", "SUCCESS", "finished_at", jobs.Now()); err != nil {
		return nil, err
	}
	return &flowresolver.Source{Bucket: bucket, Key: engineOutKey}, nil
}

// runnerWarnings are the warnings a runner kept in its run.json (the rows a model could not
// predict, a penalized model asked for an interval…), as people read them; none when the file
// cannot be read: they inform, the step has succeeded.
func (d *Deps) runnerWarnings(ctx context.Context, storage *scriptStorage, key string) []string {
	in, _, err := d.openScriptObject(ctx, storage, key)
	if err != nil || in == nil {
		return nil
	}
	defer in.Close()
	b, err := io.ReadAll(io.LimitReader(in, 1<<20))
	if err != nil {
		return nil
	}
	var run struct {
		Warnings []string `json:"warnings"`
	}
	if json.Unmarshal(b, &run) != nil {
		return nil
	}
	var out []string
	for _, w := range run.Warnings {
		if w = strings.TrimSpace(w); w != "" && len(out) < 10 {
			out = append(out, runes(upperFirst(w), 500))
		}
	}
	return out
}

// writeScriptText writes a small text into the scripts bucket, by the engine (which holds the
// bucket's key).
func (d *Deps) writeScriptText(ctx context.Context, storage *scriptStorage, key string, text []byte) error {
	cctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/objects/write", pyjson.Object{{Key: "connection", Value: storage.payload()},
		{Key: "key", Value: key}, {Key: "text", Value: string(text)}})
	if err != nil {
		return err
	}
	if res.StatusCode >= 400 {
		return &scriptFailed{"the model card could not be written to the scripts bucket: " + engineDetail(raw)}
	}
	return nil
}

// predictTrialStatus is the status a trial's refusal answers with.
func predictTrialStatus(qq *queue, msg string) int {
	switch {
	case modelsOff(qq.sc) != "":
		return http.StatusConflict
	case strings.Contains(msg, " may not use the model group "), strings.Contains(msg, " may not use the MLflow connection "):
		return http.StatusForbidden
	case strings.Contains(msg, "MLflow could not be read"):
		return http.StatusBadGateway
	}
	return http.StatusUnprocessableEntity
}

var sha256Text = regexp.MustCompile(`^[0-9a-f]{64}$`)

// checkModelFile is the checks of a boosting model's file before anything runs, in the editor's
// trial as in a flow run: the refusal's sentence ("" when the version can be applied), or the
// error of MLflow's. The card must declare the file of its library, by that name and no other
// (a path is refused), of a size within the limit and with a sha256; MLflow must have it, with
// the size the card says. Its bytes are not read here: the run streams them, and the engine
// checks the sha256 as they arrive. A model of another group declares none, and none is read.
func checkModelFile(ctx context.Context, tr *mlflowconn.Tracker, where, what string, s *predictStep, card *predictCard) (string, error) {
	want, needs := modelFileNames[s.model]
	if !needs {
		return "", nil
	}
	limit := maxModelFileBytes()
	var mf *cardModelFile
	library := ""
	if card.Boosting != nil {
		mf, library = card.Boosting.ModelFile, card.Boosting.Library
	}
	switch {
	case mf == nil:
		return fmt.Sprintf("the model card of %s declares no model file: it is not applied", what), nil
	case mf.Name != want || library != s.model:
		return fmt.Sprintf("the model card of %s names a model file (%s) that is not the one of its library (%s): it is not applied",
			what, runes(mf.Name, 60), want), nil
	case !sha256Text.MatchString(mf.SHA256):
		return fmt.Sprintf("the model card of %s keeps no valid fingerprint of its model file: it is not applied", what), nil
	case mf.Bytes < 1:
		return fmt.Sprintf("the model card of %s declares an empty model file: it is not applied", what), nil
	case mf.Bytes > limit:
		return fmt.Sprintf("the model file of %s is larger than a model file may be (%d bytes)", what, limit), nil
	}
	// the version's tag is informative, and free to check: when it is there it says the same as the card
	if tag := strings.ToLower(strings.TrimSpace(s.version.Tags["tabularia.model_file_sha256"])); tag != "" && tag != strings.ToLower(mf.SHA256) {
		return fmt.Sprintf("the model file of %s has a fingerprint (the tag tabularia.model_file_sha256) that is not the one its card keeps: "+
			"it was changed after it was registered, and is not applied", what), nil
	}
	rc, length, err := tr.Open(ctx, where, mf.Name, limit)
	var nf *mlflowconn.NotFound
	var tl *mlflowconn.TooLarge
	switch {
	case errors.As(err, &nf):
		return fmt.Sprintf("%s has no model file among its files (%s): it is not applied", what, mf.Name), nil
	case errors.As(err, &tl):
		return fmt.Sprintf("the model file of %s is larger than a model file may be (%d bytes)", what, limit), nil
	case err != nil:
		return "", err
	}
	rc.Close() // only its existence and its length are asked here
	if length >= 0 && length != mf.Bytes {
		return fmt.Sprintf("the model file of %s does not match the fingerprint its card keeps: it is not applied", what), nil
	}
	s.file = mf
	return "", nil
}

// countingReader counts the bytes read and keeps the first error: what was read from MLflow
// is told apart from what the engine answered.
type countingReader struct {
	r   io.Reader
	n   int64
	err error
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	if err != nil && err != io.EOF && c.err == nil {
		c.err = err
	}
	return n, err
}

// stageModelFile streams the model file from MLflow's artifact proxy into the scripts bucket by
// the engine's POST /scripts/objects/upload, never holding it: the engine hashes it while it
// receives it and writes it only when its size and sha256 are the card's. The gateway holds
// the MLflow credentials, the engine the bucket's; the Job gets neither's other.
func (q *queue) stageModelFile(ctx context.Context, s *predictStep, storage *scriptStorage, key string) error {
	d := q.d
	mf := s.file
	name := fmt.Sprintf("the version %s of «%s»", s.version.Version, s.version.Name)
	mismatch := &scriptFailed{fmt.Sprintf("%s: the model file of %s does not match the fingerprint its card keeps: it is not applied", s.what(), name)}
	tooLarge := &scriptFailed{fmt.Sprintf("%s: the model file of %s is larger than a model file may be (%d bytes)", s.what(), name, maxModelFileBytes())}
	tr := mlflowconn.NewTracker(s.conn.cfg)
	rc, _, err := tr.Open(ctx, mlflowconn.ArtifactPath(s.version.Source), mf.Name, maxModelFileBytes())
	var nf *mlflowconn.NotFound
	var tl *mlflowconn.TooLarge
	switch {
	case errors.As(err, &nf):
		return &scriptFailed{fmt.Sprintf("%s: %s has no model file among its files (%s): it is not applied", s.what(), name, mf.Name)}
	case errors.As(err, &tl):
		return tooLarge
	case err != nil:
		return &scriptFailed{fmt.Sprintf("%s: MLflow could not be read: %s", s.what(), mlflowWhy(err))}
	}
	defer rc.Close()
	body := &countingReader{r: rc}
	conn := base64.StdEncoding.EncodeToString([]byte(pyDumps(storage.payload())))
	query := url.Values{"key": {key}, "sha256": {mf.SHA256}, "bytes": {fmt.Sprint(mf.Bytes)}}
	cctx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	req, err := d.Engine.Request(cctx, http.MethodPost, "/scripts/objects/upload?"+query.Encode(), body)
	if err != nil {
		return err
	}
	req.ContentLength = mf.Bytes
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("X-Tabularia-Scripts-Connection", conn)
	res, err := d.Engine.Do(req)
	if err != nil {
		switch {
		case errors.As(body.err, &tl):
			return tooLarge
		case body.err != nil:
			return &scriptFailed{fmt.Sprintf("%s: MLflow could not be read: %s", s.what(), mlflowWhy(body.err))}
		case body.n != mf.Bytes:
			return mismatch // the file is shorter than its card says
		}
		return err
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 64<<10))
	switch {
	case res.StatusCode == http.StatusConflict:
		return mismatch
	case res.StatusCode == http.StatusRequestEntityTooLarge:
		return tooLarge
	case res.StatusCode >= 400:
		return &scriptFailed{fmt.Sprintf("%s: the model file could not be written to the scripts bucket: %s", s.what(), engineDetail(raw))}
	}
	return nil
}
