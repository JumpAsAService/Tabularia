package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// services/script_runs.py run_script (a flow's Script node) and services/saved_script_runs.py
// (a saved script's queued run): the code run as a Kubernetes Job in its environment's image,
// its input and output through the scripts bucket.

// scriptFailed is ScriptFailed (ContainerFailed too): the node failed, with the run page's text.
type scriptFailed struct{ msg string }

func (e *scriptFailed) Error() string { return e.msg }

const (
	codeDir   = "/tabularia/code" // CODE_DIR
	scriptUID = 10001             // script_builds.SCRIPT_UID
	labelEnv  = "tabularia.io/environment"
)

// jobSettings are ScriptsSettings' and ContainersSettings' values a run reads that
// internal/scriptcfg has not.
type jobSettings struct {
	pollSeconds        float64
	logMaxBytes        int
	jobTTL             int64
	registryPullSecret string
	cPollSeconds       float64
	cLogMaxBytes       int
	cJobTTL            int64
}

// jobSettings are read at every use, as get_settings(): the panel may change them.
func (d *Deps) jobSettings() jobSettings {
	p := d.Panel
	return jobSettings{pollSeconds: p.ScriptsPollSeconds(), logMaxBytes: int(p.ScriptsLogMaxBytes()), jobTTL: p.ScriptsJobTTL(),
		registryPullSecret: d.Config.Jobs.Kube.RegistryPullSecret, cPollSeconds: p.ContainersPoll(),
		cLogMaxBytes: int(p.ContainersLogMaxBytes()), cJobTTL: p.ContainersJobTTL()}
}

type scriptEnv struct {
	ID                                               int64
	Name, Language                                   string
	Enabled                                          bool
	CurrentVersionID                                 *int64
	CPURequest, CPULimit, MemoryRequest, MemoryLimit string
	MaxTimeout                                       int64
}

func loadScriptEnv(ctx context.Context, q perm.Querier, id int64) (*scriptEnv, error) {
	var e scriptEnv
	err := q.QueryRow(ctx, `SELECT id, name, language, enabled, current_version_id, cpu_request, cpu_limit, memory_request, memory_limit,
		max_timeout_seconds FROM script_environments WHERE id = $1`, id).Scan(&e.ID, &e.Name, &e.Language, &e.Enabled, &e.CurrentVersionID,
		&e.CPURequest, &e.CPULimit, &e.MemoryRequest, &e.MemoryLimit, &e.MaxTimeout)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &e, err
}

type scriptVersion struct {
	Number         int64
	Status         string
	ImageRef, Arch *string
}

type scriptStorage struct{ Endpoint, Region, Bucket, AccessKey, SecretEnc string }

func loadScriptStorage(ctx context.Context, q perm.Querier) (*scriptStorage, error) {
	var s scriptStorage
	var enc *string
	err := q.QueryRow(ctx, `SELECT endpoint, region, bucket, access_key, secret_key_enc FROM script_storage WHERE id = 1`).
		Scan(&s.Endpoint, &s.Region, &s.Bucket, &s.AccessKey, &enc)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	s.SecretEnc = deref(enc)
	return &s, err
}

func (s *scriptStorage) ready() bool {
	return s != nil && s.Bucket != "" && s.AccessKey != "" && s.SecretEnc != ""
}

// payload is storage_payload.
func (s *scriptStorage) payload() pyjson.Object {
	return pyjson.Object{{Key: "db_type", Value: "s3"}, {Key: "endpoint_url", Value: s.Endpoint}, {Key: "access_key", Value: s.AccessKey},
		{Key: "secret_key_encrypted", Value: s.SecretEnc}, {Key: "bucket", Value: s.Bucket}, {Key: "region", Value: s.Region}}
}

// env is _storage_env.
func (s *scriptStorage) env() pyjson.Object {
	region := s.Region
	if region == "" {
		region = "us-east-1"
	}
	out := pyjson.Object{{Key: "AWS_REGION", Value: region}}
	if s.Endpoint != "" {
		out = append(out, pyjson.KV{Key: "AWS_ENDPOINT_URL", Value: s.Endpoint})
		if strings.HasPrefix(s.Endpoint, "http://") {
			out = append(out, pyjson.KV{Key: "AWS_ALLOW_HTTP", Value: "true"})
		}
	}
	return out
}

// pyNaiveISO is datetime.isoformat() of a naive datetime.
func pyNaiveISO(t time.Time) string {
	if t.Nanosecond()/1000 != 0 {
		return t.Format("2006-01-02T15:04:05.000000")
	}
	return t.Format("2006-01-02T15:04:05")
}

// envVariables is script_runs._variables: (plain, secret, snapshot).
func (d *Deps) envVariables(ctx context.Context, envID int64) (pyjson.Object, pyjson.Object, pyjson.Object, error) {
	rows, err := d.DB.Query(ctx, `SELECT name, secret, value, value_enc, from_tabularia, updated_at FROM script_environment_variables
		WHERE environment_id = $1`, envID)
	if err != nil {
		return nil, nil, nil, err
	}
	type row struct {
		name, value     string
		secret, fromTab bool
		enc             *string
		updated         *time.Time
	}
	var list []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.name, &r.secret, &r.value, &r.enc, &r.fromTab, &r.updated); err != nil {
			rows.Close()
			return nil, nil, nil, err
		}
		list = append(list, r)
	}
	rows.Close()
	plain, secret, snapshot := pyjson.Object{}, pyjson.Object{}, pyjson.Object{}
	for _, v := range list {
		switch {
		case v.fromTab:
			value, ok := os.LookupEnv(v.value)
			if !ok {
				return nil, nil, nil, &scriptFailed{fmt.Sprintf("the variable %s hands over Tabularia's %s, which is not set here", v.name, v.value)}
			}
			secret = secret.Set(v.name, value)
			snapshot = snapshot.Set(v.name, pyjson.Object{{Key: "from_tabularia", Value: v.value}})
		case v.secret:
			plainText, err := d.Fernet.Decrypt(deref(v.enc))
			if err != nil {
				return nil, nil, nil, err
			}
			secret = secret.Set(v.name, plainText)
			var at any
			if v.updated != nil {
				at = pyNaiveISO(*v.updated)
			}
			snapshot = snapshot.Set(v.name, pyjson.Object{{Key: "secret", Value: true}, {Key: "changed_at", Value: at}})
		default:
			plain = plain.Set(v.name, v.value)
			snapshot = snapshot.Set(v.name, pyjson.Object{{Key: "value", Value: v.value}})
		}
	}
	return plain, secret, snapshot, nil
}

// codeItems is _code_items.
func codeItems(node pyjson.Object) (pyjson.Object, []any) {
	data, items := pyjson.Object{}, []any{}
	for i, f := range scriptnodes.FilesOf(node) {
		k := fmt.Sprintf("f%d", i)
		data = append(data, pyjson.KV{Key: k, Value: f.Value("content")})
		items = append(items, pyjson.Object{{Key: "key", Value: k}, {Key: "path", Value: f.Value("path")}})
	}
	return data, items
}

func envList(environment pyjson.Object) []any {
	out := make([]any, len(environment))
	for i, kv := range environment {
		out[i] = pyjson.Object{{Key: "name", Value: kv.Key}, {Key: "value", Value: kv.Value}}
	}
	return out
}

// scriptJobManifest is script_runs.job_manifest.
func scriptJobManifest(js jobSettings, name string, env *scriptEnv, version *scriptVersion, node pyjson.Object, environment pyjson.Object,
	labels pyjson.Object, timeout int64, items []any, codeName, secretName string) pyjson.Object {
	entry := nodeData(node).Value("entrypoint")
	var command []any
	if env.Language == "python" {
		command = []any{"python", "-P", entry}
		environment = merge(pyjson.Object{{Key: "PYTHONPATH", Value: codeDir}}, environment)
	} else {
		command = []any{"Rscript", entry}
	}
	container := pyjson.Object{{Key: "name", Value: "script"}, {Key: "image", Value: deref(version.ImageRef)}, {Key: "command", Value: command},
		{Key: "workingDir", Value: codeDir}, {Key: "env", Value: envList(environment)},
		{Key: "envFrom", Value: []any{pyjson.Object{{Key: "secretRef", Value: pyjson.Object{{Key: "name", Value: secretName}}}}}},
		{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: env.CPURequest}, {Key: "memory", Value: env.MemoryRequest}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: env.CPULimit}, {Key: "memory", Value: env.MemoryLimit}}}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
			{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}},
		{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "mountPath", Value: codeDir}, {Key: "readOnly", Value: true}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}}}}}
	pod := pyjson.Object{{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false},
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(scriptUID)},
			{Key: "runAsGroup", Value: int64(scriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{container}}}
	if version.Arch != nil && *version.Arch != "" {
		pod = append(pod, pyjson.KV{Key: "nodeSelector", Value: pyjson.Object{{Key: "kubernetes.io/arch", Value: *version.Arch}}})
	}
	pod = append(pod, pyjson.KV{Key: "volumes", Value: []any{pyjson.Object{{Key: "name", Value: "code"}, {Key: "configMap",
		Value: pyjson.Object{{Key: "name", Value: codeName}, {Key: "items", Value: items}}}},
		pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}}}})
	if js.registryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: js.registryPullSecret}}}})
	}
	return jobManifestOf(name, labels, timeout, js.jobTTL, pod)
}

func jobManifestOf(name string, labels pyjson.Object, timeout, ttl int64, pod pyjson.Object) pyjson.Object {
	return pyjson.Object{{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: labels}}},
		{Key: "spec", Value: pyjson.Object{{Key: "backoffLimit", Value: int64(0)}, {Key: "activeDeadlineSeconds", Value: timeout},
			{Key: "ttlSecondsAfterFinished", Value: ttl}, {Key: "template", Value: pyjson.Object{{Key: "metadata", Value: pyjson.Object{{Key: "labels", Value: labels}}},
				{Key: "spec", Value: pod}}}}}}
}

// budgetSeconds is budget_seconds.
func budgetSeconds(d pyjson.Object) (int64, error) {
	get := func(k string, def int64) (int64, error) {
		v, ok := d.Get(k)
		if !ok {
			return def, nil
		}
		n, ok := pyInt(v)
		if !ok || v == nil {
			return 0, fmt.Errorf("int() of %s", k)
		}
		return n, nil
	}
	timeout, err := get("timeoutSeconds", 900)
	if err != nil {
		return 0, err
	}
	retries, err := get("retries", 0)
	if err != nil {
		return 0, err
	}
	delay, err := get("retryDelaySeconds", 60)
	if err != nil {
		return 0, err
	}
	return (timeout+jobGraceSeconds)*(retries+1) + delay*retries + 900, nil
}

// runStillOpen is run_still_open.
func (d *Deps) runStillOpen(ctx context.Context, id int64) bool {
	var status string
	err := d.DB.QueryRow(ctx, `SELECT status FROM runs WHERE id = $1`, id).Scan(&status)
	return err == nil && !terminal[status]
}

// childRun is a Script/Container run's row, saved as script_runs' `save` does.
type childRun struct {
	d    *Deps
	id   int64
	info pyjson.Object
	// unchecked: saved_script_runs.execute's save, which writes a terminal outcome without
	// asking whether the run is still open
	unchecked bool
}

// save is save(**values): the values and the info written; a terminal outcome only on a run
// still open.
func (c *childRun) save(ctx context.Context, values ...any) error {
	for i := 0; i < len(values); i += 2 {
		if !c.unchecked && values[i] == "status" && terminal[values[i+1].(string)] && !c.d.runStillOpen(ctx, c.id) {
			return nil
		}
	}
	set := []string{"script = $2"}
	args := []any{c.id, pyDumps(c.info)}
	for i := 0; i < len(values); i += 2 {
		args = append(args, values[i+1])
		set = append(set, fmt.Sprintf("%s = $%d", values[i], len(args)))
	}
	_, err := c.d.DB.Exec(ctx, `UPDATE runs SET `+strings.Join(set, ", ")+` WHERE id = $1`, args...)
	return err
}

// deleteObjects is _delete_objects / the `finally` of a run: the step's keys removed from the
// scripts bucket, as best it can.
func (d *Deps) deleteObjects(ctx context.Context, storage *scriptStorage, keys []string) {
	cctx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	if _, _, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/objects/delete", pyjson.Object{{Key: "connection", Value: storage.payload()},
		{Key: "keys", Value: stringsAny(keys)}}); err != nil {
		d.Log.Warn("script: step not cleaned up", "keys", keys, "err", err)
	}
}

// engineDetail is _detail.
func engineDetail(raw []byte) string {
	if v, err := pyjson.Loads(string(raw)); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			return runes(pyjson.Str(pyjson.Or(o.Value("detail"), string(raw))), 500)
		}
		return runes(string(raw), 500)
	}
	return runes(string(raw), 500)
}

// newUUIDText is str(uuid.uuid4()).
func newUUIDText() string { return newUUID() }

// errorMessage is `getattr(e, "detail", None) or str(e) or type(e).__name__`.
func errorMessage(err error) string {
	if m := errorDetail(err); m != "" {
		return m
	}
	return "Exception"
}

// scriptNodeRun is what run_script needs of the orchestration.
type scriptNodeRun struct {
	q          *queue
	user       *auth.User
	flow       *jobFlow
	definition pyjson.Object
	node       pyjson.Object
	parent     int64
	trigger    string
	mode       string
	outputs    map[string]*[2]any
	resolveDS  dsResolver
}

// fingerprintOf is script_nodes.fingerprint.
func (d *Deps) fingerprintOf(ctx context.Context, node pyjson.Object) (string, error) {
	sid, ok := scriptnodes.SavedScriptID(node)
	code := flowaccess.CodeFingerprint(node)
	if !ok {
		return code, nil
	}
	s, err := savedscripts.Get(ctx, d.DB, sid)
	if err != nil {
		return "", err
	}
	var version int64
	if s != nil {
		version = s.CurrentVersion
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s:v%d", code, version)))
	return hex.EncodeToString(sum[:]), nil
}

// resolvedNode is script_nodes.resolved: a node by reference takes the script's current files.
func (d *Deps) resolvedNode(ctx context.Context, node pyjson.Object) (pyjson.Object, *savedscripts.Script, pyjson.Object, error) {
	sid, ok := scriptnodes.SavedScriptID(node)
	if !ok {
		return node, nil, nil, nil
	}
	s, err := savedscripts.Get(ctx, d.DB, sid)
	if err != nil || s == nil {
		return node, nil, nil, err
	}
	content, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		return nil, nil, nil, err
	}
	data := merge(nodeData(node), pyjson.Object{{Key: "environmentId", Value: s.EnvironmentID}, {Key: "files", Value: content.Value("files")},
		{Key: "entrypoint", Value: content.Value("entrypoint")}, {Key: "timeoutSeconds", Value: content.Value("timeout_seconds")},
		{Key: "retries", Value: content.Value("retries")}, {Key: "retryDelaySeconds", Value: content.Value("retry_delay_seconds")}})
	return append(pyjson.Object(nil), node...).Set("data", data), s, content, nil
}

// environmentOf is the environment checks run_script and saved_script_runs._environment share.
func (d *Deps) environmentOf(ctx context.Context, sc *scriptcfg.Settings, user *auth.User, envID any, missing string) (*scriptEnv,
	*scriptVersion, error) {
	if reason := sc.DisabledReason(); reason != "" {
		if reason == "kubernetes" {
			return nil, nil, &scriptFailed{"scripting environments are off on this installation"}
		}
		return nil, nil, &scriptFailed{"scripting environments are not configured"}
	}
	var env *scriptEnv
	if id, ok := envID.(int64); ok {
		var err error
		if env, err = loadScriptEnv(ctx, d.DB, id); err != nil {
			return nil, nil, err
		}
	}
	if env == nil {
		return nil, nil, &scriptFailed{missing}
	}
	if !env.Enabled {
		return nil, nil, &scriptFailed{fmt.Sprintf("the scripting environment «%s» is disabled", env.Name)}
	}
	var version *scriptVersion
	if env.CurrentVersionID != nil && *env.CurrentVersionID != 0 {
		var v scriptVersion
		err := d.DB.QueryRow(ctx, `SELECT number, status, image_ref, arch FROM script_environment_versions WHERE id = $1`, *env.CurrentVersionID).
			Scan(&v.Number, &v.Status, &v.ImageRef, &v.Arch)
		if err == nil {
			version = &v
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return nil, nil, err
		}
	}
	if version == nil || version.Status != "ready" || deref(version.ImageRef) == "" {
		return nil, nil, &scriptFailed{fmt.Sprintf("the scripting environment «%s» has no version built yet", env.Name)}
	}
	ok, err := scriptnodes.CanUse(ctx, perm.For(d.DB, user), d.DB, user.ID, env.ID)
	if err != nil {
		return nil, nil, err
	}
	if !ok {
		return nil, nil, &scriptFailed{fmt.Sprintf("%s cannot run scripts in the environment «%s»", user.Email, env.Name)}
	}
	return env, version, nil
}

// runScript is script_runs.run_script for a flow's Script node: where its output is, nil
// when it has none.
func (q *queue) runScript(ctx context.Context, r *scriptNodeRun) (*[2]any, error) {
	d := q.d
	if reason := q.sc.DisabledReason(); reason != "" {
		if reason == "kubernetes" {
			return nil, &scriptFailed{"scripting environments are off on this installation"}
		}
		return nil, &scriptFailed{"scripting environments are not configured"}
	}
	label := scriptnodes.Label(r.node)
	_, byReference := scriptnodes.SavedScriptID(r.node)
	fingerprint, err := d.fingerprintOf(ctx, r.node)
	if err != nil {
		return nil, err
	}
	node, saved, content, err := d.resolvedNode(ctx, r.node)
	if err != nil {
		return nil, err
	}
	c := perm.For(d.DB, r.user)
	if byReference {
		if saved == nil {
			return nil, &scriptFailed{fmt.Sprintf("script «%s» runs a saved script that no longer exists", label)}
		}
		see, err := savedscripts.CanSee(ctx, c, d.DB, r.user.ID, saved.ProjectID, saved.EnvironmentID)
		if err != nil {
			return nil, err
		}
		if !see {
			return nil, &scriptFailed{fmt.Sprintf("%s cannot see the saved script that «%s» runs", r.user.Email, label)}
		}
	}
	data := nodeData(node)
	env, version, err := d.environmentOf(ctx, q.sc, r.user, data.Value("environmentId"), fmt.Sprintf("script «%s» has no scripting environment", label))
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
	if ref := scriptnodes.CheckFiles(node, env.Language, storage.Bucket, true); ref != nil {
		return nil, &scriptFailed{ref.Detail}
	}
	if ref := scriptnodes.CheckSettings(node, env.MaxTimeout); ref != nil {
		return nil, &scriptFailed{ref.Detail}
	}
	var declared []declaredInput
	if saved != nil {
		if declared, err = d.checkInputs(ctx, r.user, content); err != nil {
			return nil, err
		}
	}
	ns := q.sc.Scripts.Namespace
	nodeID := node.Value("id")
	upstream, hasUpstream := incoming(objectList(r.definition.Value("edges")))[nodeKey(nodeID)]["left"]
	hasInput := hasUpstream && upstream != nil
	wantsOutput := scriptHasOutput(r.definition, nodeID)
	hasOutput := wantsOutput
	stepID := newUUIDText()
	prefix := fmt.Sprintf("env-%d/%s", env.ID, stepID)
	inKey, outKey := prefix+"/input.parquet", prefix+"/output.parquet"
	engineOutKey := ""
	if hasOutput {
		engineOutKey = "out/" + newHex() + ".parquet"
	}
	plain, secretVars, snapshot, err := d.envVariables(ctx, env.ID)
	if err != nil {
		return nil, err
	}
	var files []any
	for _, f := range scriptnodes.FilesOf(node) {
		files = append(files, f.Value("path"))
	}
	if files == nil {
		files = []any{}
	}
	budget, err := budgetSeconds(data)
	if err != nil {
		return nil, err
	}
	info := pyjson.Object{{Key: "node_id", Value: nodeID}, {Key: "label", Value: label}, {Key: "environment_id", Value: env.ID},
		{Key: "environment", Value: env.Name}, {Key: "language", Value: env.Language}, {Key: "version", Value: version.Number},
		{Key: "image", Value: deref(version.ImageRef)}, {Key: "step_id", Value: stepID}, {Key: "input", Value: hasInput},
		{Key: "output", Value: hasOutput}, {Key: "entrypoint", Value: data.Value("entrypoint")}, {Key: "files", Value: files},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "variables", Value: snapshot}, {Key: "log", Value: ""},
		{Key: "trial", Value: false}, {Key: "fingerprint", Value: fingerprint}, {Key: "budget_seconds", Value: budget},
		{Key: "namespace", Value: ns}}
	if saved != nil {
		inputs := []any{}
		for _, in := range declared {
			inputs = append(inputs, pyjson.Object{{Key: "alias", Value: in.alias}, {Key: "datasource_id", Value: in.ds.ID}, {Key: "name", Value: in.ds.Name}})
		}
		info = append(info, pyjson.KV{Key: "saved_script_id", Value: saved.ID}, pyjson.KV{Key: "script", Value: saved.Name},
			pyjson.KV{Key: "script_version", Value: saved.CurrentVersion}, pyjson.KV{Key: "inputs", Value: inputs})
	}
	bucket := d.Config.EngineBucket
	outBucket := ""
	if hasOutput {
		outBucket = bucket
	}
	var childID int64
	if err := d.DB.QueryRow(ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, parent_run_id, engine,
		input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at)
		VALUES ('script', $1, '', 'STARTED', $2, $3, $4, $5, '', $6, $7, '', false, '[]', $8, $9) RETURNING id`, r.flow.ID, r.user.ID,
		r.trigger, r.parent, fmt.Sprintf("%s v%d", env.Name, version.Number), outBucket, engineOutKey, pyDumps(info), jobs.Now()).
		Scan(&childID); err != nil {
		return nil, err
	}
	child := &childRun{d: d, id: childID, info: info}
	written := []string{inKey, outKey}
	defer func() { d.deleteObjects(context.Background(), storage, written) }()
	out, err := func() (*[2]any, error) {
		inputEnv := pyjson.Object{}
		if saved != nil {
			env2, described, keys, err := d.prepareInputs(ctx, declared, storage, prefix)
			if err != nil {
				return nil, err
			}
			written = append(written, keys...)
			inputEnv = append(env2, pyjson.Object{{Key: "TABULARIA_INPUTS", Value: pyDumpsUnicode(described)},
				{Key: "TABULARIA_SCRIPT_ID", Value: fmt.Sprint(saved.ID)}, {Key: "TABULARIA_SCRIPT_NAME", Value: saved.Name},
				{Key: "TABULARIA_SCRIPT_VERSION", Value: fmt.Sprint(saved.CurrentVersion)}}...)
		}
		if hasInput {
			if err := q.prepareNodeInput(ctx, r, node, child, storage, inKey, "script_input", "script_run_id", "script", label); err != nil {
				return nil, err
			}
		}
		timeout, retries, delay, err := retrySettings(data)
		if err != nil {
			return nil, err
		}
		code, items := codeItems(node)
		bucketURL := "s3://" + storage.Bucket
		inURL, outURL := "", ""
		if hasInput {
			inURL = bucketURL + "/" + inKey
		}
		if hasOutput {
			outURL = bucketURL + "/" + outKey
		}
		runID := r.parent
		base := merge(pyjson.Object{{Key: "TABULARIA_INPUT", Value: inURL}, {Key: "TABULARIA_OUTPUT", Value: outURL},
			{Key: "TABULARIA_STEP_ID", Value: stepID}, {Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(runID)},
			{Key: "TABULARIA_FLOW_ID", Value: fmt.Sprint(r.flow.ID)}, {Key: "TABULARIA_FLOW_NAME", Value: r.flow.Name},
			{Key: "TABULARIA_NODE_ID", Value: pyjson.Str(nodeID)}}, inputEnv, pyjson.Object{{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}},
			storage.env(), plain)
		key, err := d.Fernet.Decrypt(storage.SecretEnc)
		if err != nil {
			return nil, err
		}
		secret := merge(pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}, secretVars)
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "scripting"},
			{Key: labelEnv, Value: fmt.Sprint(env.ID)}, {Key: labelScriptRun, Value: fmt.Sprint(childID)},
			{Key: labelOrchestration, Value: fmt.Sprint(runID)}, {Key: "tabularia.io/stage", Value: "run"}}
		manifest := func(name string, attempt int64) pyjson.Object {
			return scriptJobManifest(q.d.jobSettings(), name, env, version, node, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, timeout, items, name+"-code", name+"-env")
		}
		ok, reason, err := q.attempts(ctx, child, "script", "script «"+label+"»", retries, delay, runID, hasOutput, storage, outKey, manifest, code, secret,
			timeout, ns, q.d.jobSettings().pollSeconds, q.d.jobSettings().logMaxBytes)
		if err != nil {
			return nil, err
		}
		if !ok {
			return nil, &scriptFailed{fmt.Sprintf("script «%s» failed: %s", label, reason)}
		}
		return q.importOutput(ctx, child, hasOutput, wantsOutput, storage, outKey, engineOutKey, "script «"+label+"»",
			fmt.Sprintf("script «%s» did not write its output: the nodes after it read the parquet file the script writes at TABULARIA_OUTPUT", label))
	}()
	return q.closeChild(ctx, child, out, err, "script «"+label+"»")
}

// closeChild is the `except` of run_script / run_container: the failure written on the run.
func (q *queue) closeChild(ctx context.Context, child *childRun, out *[2]any, err error, what string) (*[2]any, error) {
	if err == nil {
		return out, nil
	}
	var sf *scriptFailed
	if errors.As(err, &sf) {
		_ = child.save(ctx, "status", "FAILURE", "error", runes(sf.msg, 2000), "finished_at", jobs.Now())
		return nil, err
	}
	message := errorMessage(err)
	_ = child.save(ctx, "status", "FAILURE", "error", runes(what+": "+message, 2000), "finished_at", jobs.Now())
	return nil, &scriptFailed{what + ": " + message}
}

// retrySettings is int(d.get("timeoutSeconds", 900)), retries, retryDelaySeconds.
func retrySettings(d pyjson.Object) (int64, int64, int64, error) {
	vals := [3]int64{}
	for i, k := range []struct {
		key string
		def int64
	}{{"timeoutSeconds", 900}, {"retries", 0}, {"retryDelaySeconds", 60}} {
		v, has := d.Get(k.key)
		if !has {
			vals[i] = k.def
			continue
		}
		n, ok := pyInt(v)
		if !ok || v == nil {
			return 0, 0, 0, fmt.Errorf("int() of %s", k.key)
		}
		vals[i] = n
	}
	return vals[0], vals[1], vals[2], nil
}

// prepareNodeInput is the input of a Script or Container node: the chain before it written by
// the engine into the scripts bucket, awaited.
func (q *queue) prepareNodeInput(ctx context.Context, r *scriptNodeRun, node pyjson.Object, child *childRun, storage *scriptStorage,
	inKey, kind, idKey, what, label string) error {
	req, err := resolveScriptInput(r.definition, node, r.resolveDS, q.d.Config.EngineBucket, r.mode, int64(q.d.Panel.PreviewSampleRows()), r.outputs)
	if err != nil {
		return err
	}
	target := pyjson.Object{{Key: "type", Value: "s3"}, {Key: "connection", Value: storage.payload()}, {Key: "target", Value: pyjson.Object{
		{Key: "bucket", Value: storage.Bucket}, {Key: "key", Value: inKey}, {Key: "format", Value: "parquet"}, {Key: "partition_by", Value: []any{}}}}}
	summary := pyDumps(pyjson.Object{{Key: "type", Value: kind}, {Key: idKey, Value: child.id}, {Key: "bucket", Value: storage.Bucket},
		{Key: "key", Value: inKey}})
	parent := r.parent
	runID, err := q.d.launchFlowRunTo(ctx, r.user, r.flow, req, r.trigger, &parent, r.mode, target, &summary)
	if err != nil {
		return err
	}
	run, err := q.waitRun(ctx, runID)
	if err != nil {
		return err
	}
	if run.Status != "SUCCESS" {
		why := run.Status
		if run.Error != nil && *run.Error != "" {
			why = *run.Error
		}
		return &scriptFailed{fmt.Sprintf("the input of %s «%s» could not be prepared (run %d): %s", what, label, run.ID, why)}
	}
	return child.save(ctx, "input_key", run.OutputKey, "rows_written", nil)
}

// attempts is the attempts' loop of run_script / run_container / execute: (succeeded, the
// last reason).
func (q *queue) attempts(ctx context.Context, child *childRun, kind, what string, retries, delay, runID int64, hasOutput bool,
	storage *scriptStorage, outKey string, manifest func(string, int64) pyjson.Object, code, secret pyjson.Object, timeout int64,
	ns string, poll float64, logMax int) (bool, string, error) {
	var logs []string
	reason := ""
	for attempt := int64(1); attempt <= retries+1; attempt++ {
		name := fmt.Sprintf("tabularia-%s-%d-a%d", kind, child.id, attempt)
		m := manifest(name, attempt)
		child.info = child.info.Set("attempts", attempt)
		if err := child.save(ctx); err != nil {
			return false, "", err
		}
		ok, log, exitCode, why, err := kjAttempt(ctx, kubeAPI(), name, m, code, secret, timeout, ns, poll, logMax)
		if err != nil {
			return false, "", err
		}
		reason = why
		if retries > 0 {
			logs = append(logs, fmt.Sprintf("── attempt %d ──\n%s", attempt, log))
		} else {
			logs = append(logs, log)
		}
		child.info = child.info.Set("log", kube.Tail(strings.Join(logs, "\n"), logMax)).Set("exit_code", exitCode)
		if err := child.save(ctx); err != nil {
			return false, "", err
		}
		if ok {
			return true, "", nil
		}
		if attempt <= retries {
			if !jobs.Sleep(ctx, time.Duration(delay)*time.Second) {
				return false, "", ctx.Err()
			}
			if !q.d.runStillOpen(ctx, runID) {
				return false, "", &scriptFailed{fmt.Sprintf("%s failed: %s (not run again: its run was closed)", what, reason)}
			}
			if hasOutput {
				q.d.deleteObjects(ctx, storage, []string{outKey})
			}
		}
	}
	return false, reason, nil
}

// importOutput reads what the node wrote into Tabularia's storage (/scripts/output/import).
func (q *queue) importOutput(ctx context.Context, child *childRun, hasOutput, wantsOutput bool, storage *scriptStorage, outKey,
	engineOutKey, what, missing string) (*[2]any, error) {
	bucket := q.d.Config.EngineBucket
	if hasOutput {
		cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		res, raw, err := q.d.Engine.JSON(cctx, http.MethodPost, "/scripts/output/import", pyjson.Object{{Key: "connection", Value: storage.payload()},
			{Key: "key", Value: outKey}, {Key: "bucket", Value: bucket}, {Key: "output_key", Value: engineOutKey}})
		cancel()
		if err != nil {
			return nil, err
		}
		if res.StatusCode >= 400 {
			return nil, &scriptFailed{fmt.Sprintf("%s: its output could not be read: %s", what, engineDetail(raw))}
		}
		v, err := pyjson.Loads(string(raw))
		if err != nil {
			return nil, &pyJSONError{err}
		}
		out := pyjson.Obj(v)
		if !pyjson.Truthy(out.Value("found")) && !wantsOutput {
			child.info = child.info.Set("output", false)
			return nil, child.save(ctx, "output_key", "", "output_bucket", "", "status", "SUCCESS", "finished_at", jobs.Now())
		}
		if !pyjson.Truthy(out.Value("found")) {
			return nil, &scriptFailed{missing}
		}
		rows, err := intColumn(out.Value("rows"))
		if err != nil {
			return nil, err
		}
		if err := child.save(ctx, "rows_written", rows); err != nil {
			return nil, err
		}
	}
	if err := child.save(ctx, "status", "SUCCESS", "finished_at", jobs.Now()); err != nil {
		return nil, err
	}
	if hasOutput {
		return &[2]any{bucket, engineOutKey}, nil
	}
	return nil, nil
}

// scriptHasOutput is script_has_output.
func scriptHasOutput(definition pyjson.Object, nodeID any) bool {
	for _, e := range objectList(definition.Value("edges")) {
		if pyjson.Equal(e.Value("source"), nodeID) && !isSeqEdge(e) {
			return true
		}
	}
	return false
}

// resolveScriptInput is resolve_script_input.
func resolveScriptInput(definition, node pyjson.Object, resolveDS dsResolver, defaultBucket, mode string, defaultRows int64,
	outputs map[string]*[2]any) (pyjson.Object, error) {
	upstream := incoming(objectList(definition.Value("edges")))[nodeKey(node.Value("id"))]["left"]
	r := newFlowResolver(objectList(definition.Value("nodes")), objectList(definition.Value("edges")), resolveDS, mode, defaultRows, outputs)
	source, ops, err := r.chain(upstream)
	if err != nil {
		return nil, err
	}
	if source == nil {
		label := pyjson.Str(pyjson.Or(nodeData(node).Value("label"), node.Value("id")))
		return nil, &flowResolveError{fmt.Sprintf("script %s: no source with data upstream (or the source datasource has no snapshot)", label)}
	}
	if ops == nil {
		ops = []any{}
	}
	return pyjson.Object{{Key: "bucket", Value: pyjson.Or(source[0], defaultBucket)}, {Key: "input_key", Value: source[1]},
		{Key: "operations", Value: ops}}, nil
}

// pyDumpsUnicode is json.dumps(v, ensure_ascii=False).
func pyDumpsUnicode(v any) string {
	s, _ := pyjson.Dumps(v, pyjson.Options{})
	return s
}

// ── saved scripts' inputs (services/saved_script_runs.py) ─────────────────────

type declaredInput struct {
	alias string
	ds    *jobDatasource
	cols  string
}

// checkInputs is check_inputs.
func (d *Deps) checkInputs(ctx context.Context, user *auth.User, content pyjson.Object) ([]declaredInput, error) {
	var out []declaredInput
	c := perm.For(d.DB, user)
	for _, item := range objectList(content.Value("inputs")) {
		alias := textOrEmpty(item.Value("alias"))
		var ds *jobDatasource
		if id, ok := item.Value("datasource_id").(int64); ok {
			var err error
			if ds, err = loadJobDatasource(ctx, d.DB, id); err != nil {
				return nil, err
			}
		}
		if ds == nil {
			return nil, &scriptFailed{fmt.Sprintf("the input %s is a datasource that no longer exists: choose another one", alias)}
		}
		if ok, err := c.Has(ctx, ds.ProjectID, perm.View); err != nil {
			return nil, err
		} else if !ok {
			return nil, &scriptFailed{fmt.Sprintf("%s can no longer read the datasource «%s» (input %s)", user.Email, ds.Name, alias)}
		}
		if ds.Key == "" {
			return nil, &scriptFailed{fmt.Sprintf("the datasource «%s» (input %s) has no data yet", ds.Name, alias)}
		}
		var cols string
		_ = d.DB.QueryRow(ctx, `SELECT columns FROM datasources WHERE id = $1`, ds.ID).Scan(&cols)
		out = append(out, declaredInput{alias, ds, cols})
	}
	return out, nil
}

// folderPath is folder_path.
func (d *Deps) folderPath(ctx context.Context, projectID int64) (string, error) {
	rows, err := d.DB.Query(ctx, `SELECT id, name, parent_id FROM projects`)
	if err != nil {
		return "", err
	}
	names := map[int64]string{}
	parents := map[int64]*int64{}
	for rows.Next() {
		var id int64
		var name string
		var parent *int64
		if err := rows.Scan(&id, &name, &parent); err != nil {
			rows.Close()
			return "", err
		}
		names[id], parents[id] = name, parent
	}
	rows.Close()
	ids := perm.Ancestors(parents, projectID)
	var parts []string
	for i := len(ids) - 1; i >= 0; i-- {
		if n, ok := names[ids[i]]; ok {
			parts = append(parts, n)
		}
	}
	return strings.Join(parts, " / "), nil
}

// prepareInputs is prepare_inputs: each input copied to the scripts bucket.
func (d *Deps) prepareInputs(ctx context.Context, inputs []declaredInput, storage *scriptStorage, prefix string) (pyjson.Object, pyjson.Object,
	[]string, error) {
	env, described, keys := pyjson.Object{}, pyjson.Object{}, []string{}
	bucketURL := "s3://" + storage.Bucket
	for _, in := range inputs {
		key := fmt.Sprintf("%s/in-%s.parquet", prefix, in.alias)
		cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/input/export", pyjson.Object{{Key: "connection", Value: storage.payload()},
			{Key: "bucket", Value: in.ds.Bucket}, {Key: "key", Value: in.ds.Key}, {Key: "target_key", Value: key}})
		cancel()
		if err != nil {
			return nil, nil, nil, err
		}
		if res.StatusCode >= 400 {
			return nil, nil, nil, &scriptFailed{fmt.Sprintf("the datasource «%s» (input %s) could not be copied for the script: %s",
				in.ds.Name, in.alias, engineDetail(raw))}
		}
		keys = append(keys, key)
		env = env.Set("TABULARIA_INPUT_"+in.alias, bucketURL+"/"+key)
		folder, err := d.folderPath(ctx, in.ds.ProjectID)
		if err != nil {
			return nil, nil, nil, err
		}
		columns := []any{}
		if v, err := pyjson.Loads(pyjson.Str(pyjson.Or(in.cols, "[]"))); err == nil {
			if list, ok := v.([]any); ok {
				for _, c := range list {
					if o, ok := c.(pyjson.Object); ok {
						columns = append(columns, o.Value("name"))
					}
				}
			}
		}
		described = described.Set(in.alias, pyjson.Object{{Key: "datasource_id", Value: in.ds.ID}, {Key: "name", Value: in.ds.Name},
			{Key: "folder", Value: folder}, {Key: "uri", Value: bucketURL + "/" + key}, {Key: "columns", Value: columns}})
	}
	return env, described, keys, nil
}

// gapsOfScript is schedule_authority.gaps_of_script.
func (d *Deps) gapsOfScript(ctx context.Context, user *auth.User, s *savedscripts.Script, inFlow bool) ([]string, error) {
	c := perm.For(d.DB, user)
	see, err := savedscripts.CanSee(ctx, c, d.DB, user.ID, s.ProjectID, s.EnvironmentID)
	if err != nil {
		return nil, err
	}
	if !see {
		return []string{fmt.Sprintf("access to the saved script '%s' (its folder and its environment)", s.Name)}, nil
	}
	var gaps []string
	if !inFlow {
		if ok, err := c.Has(ctx, s.ProjectID, perm.Run); err != nil {
			return nil, err
		} else if !ok {
			gaps = append(gaps, fmt.Sprintf("RUN on the folder of the saved script '%s'", s.Name))
		}
	}
	content, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		return nil, err
	}
	for _, item := range objectList(content.Value("inputs")) {
		id, ok := item.Value("datasource_id").(int64)
		if !ok {
			continue
		}
		ds, err := loadJobDatasource(ctx, d.DB, id)
		if err != nil {
			return nil, err
		}
		if ds == nil {
			continue
		}
		if ok, err := c.Has(ctx, ds.ProjectID, perm.View); err != nil {
			return nil, err
		} else if !ok {
			gaps = append(gaps, fmt.Sprintf("read access to '%s', input %s of '%s'", ds.Name, pyjson.Str(item.Value("alias")), s.Name))
		}
	}
	output := pyjson.Obj(pyjson.Or(content.Value("output"), pyjson.Object{}))
	if project, ok := output.Value("project_id").(int64); !inFlow && pyjson.Truthy(output.Value("publish")) && ok {
		if ok, err := c.Has(ctx, project, perm.Edit); err != nil {
			return nil, err
		} else if !ok {
			gaps = append(gaps, fmt.Sprintf("EDIT on the folder where '%s' publishes its output", s.Name))
		}
	}
	return gaps, nil
}
