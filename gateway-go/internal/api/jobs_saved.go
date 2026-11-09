package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/runlaunch"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// services/saved_script_runs.py run_queued and execute: a saved script's queued run, its
// current version run as a Job with its declared inputs, its output published.

// runSavedQueued is run_queued.
func (q *queue) runSavedQueued(ctx context.Context, id int64, run *runRow) error {
	s, err := savedscripts.Get(ctx, q.d.DB, *run.SavedScriptID)
	if err != nil {
		return err
	}
	user, err := loadUserOpt(ctx, q.d.DB, run.LaunchedBy)
	if err != nil {
		return err
	}
	if s == nil || user == nil || !user.IsActive {
		q.finalize(ctx, id, "FAILURE", strp("saved script or user no longer valid"), false)
		return nil
	}
	if run.TriggerType == "schedule" {
		gaps, err := q.d.gapsOfScript(ctx, user, s, false)
		if err != nil {
			return err
		}
		if len(gaps) > 0 {
			q.finalize(ctx, id, "FAILURE", strp(notStarted(user, gaps)), false)
			return nil
		}
	}
	err = q.executeSaved(ctx, user, s, id, run.TriggerType)
	var sf *scriptFailed
	if errors.As(err, &sf) {
		q.finalize(ctx, id, "FAILURE", strp(sf.msg), false)
		return nil
	}
	if err != nil {
		return err
	}
	q.finalize(ctx, id, "SUCCESS", nil, false)
	return nil
}

// asNode is saved_scripts._as_node.
func asNode(name string, content pyjson.Object) pyjson.Object {
	return pyjson.Object{{Key: "id", Value: name}, {Key: "data", Value: pyjson.Object{{Key: "label", Value: name},
		{Key: "files", Value: content.Value("files")}, {Key: "entrypoint", Value: content.Value("entrypoint")},
		{Key: "timeoutSeconds", Value: content.Value("timeout_seconds")}, {Key: "retries", Value: content.Value("retries")},
		{Key: "retryDelaySeconds", Value: content.Value("retry_delay_seconds")}}}}
}

// executeSaved is execute.
func (q *queue) executeSaved(ctx context.Context, user *auth.User, s *savedscripts.Script, parent int64, trigger string) error {
	d := q.d
	c := perm.For(d.DB, user)
	see, err := savedscripts.CanSee(ctx, c, d.DB, user.ID, s.ProjectID, s.EnvironmentID)
	if err != nil {
		return err
	}
	if !see {
		return &scriptFailed{fmt.Sprintf("%s can no longer see the saved script «%s»", user.Email, s.Name)}
	}
	if ok, err := c.Has(ctx, s.ProjectID, perm.Run); err != nil {
		return err
	} else if !ok {
		return &scriptFailed{fmt.Sprintf("RUN missing on the folder of the saved script «%s»", s.Name)}
	}
	env, version, err := d.environmentOf(ctx, q.sc, user, s.EnvironmentID, fmt.Sprintf("the saved script «%s» has no scripting environment", s.Name))
	if err != nil {
		return err
	}
	storage, err := loadScriptStorage(ctx, d.DB)
	if err != nil {
		return err
	}
	if !storage.ready() {
		return &scriptFailed{"the scripts bucket is not configured (Administration → Scripting environments)"}
	}
	content, err := savedscripts.ContentOf(ctx, d.DB, s, 0)
	if err != nil {
		return err
	}
	node := asNode(s.Name, content)
	if ref := scriptnodes.CheckFiles(node, env.Language, storage.Bucket, true); ref != nil {
		return &scriptFailed{ref.Detail}
	}
	if ref := scriptnodes.CheckSettings(node, env.MaxTimeout); ref != nil {
		return &scriptFailed{ref.Detail}
	}
	inputs, err := d.checkInputs(ctx, user, content)
	if err != nil {
		return err
	}
	publish := pyjson.Obj(pyjson.Or(content.Value("output"), pyjson.Object{}))
	publishes := pyjson.Truthy(publish.Value("publish"))
	if publishes {
		project, isInt := publish.Value("project_id").(int64)
		if !pyjson.Truthy(publish.Value("name")) || !isInt {
			return &scriptFailed{fmt.Sprintf("the saved script «%s» publishes its output, but without a name or a folder", s.Name)}
		}
		if ok, err := c.Has(ctx, project, perm.Edit); err != nil {
			return err
		} else if !ok {
			return &scriptFailed{fmt.Sprintf("%s cannot write in the folder where «%s» publishes its output", user.Email, s.Name)}
		}
	}
	ns := q.sc.Scripts.Namespace
	bucket := d.Config.EngineBucket
	stepID := newUUIDText()
	prefix := fmt.Sprintf("script-%d/%s", s.ID, stepID)
	outKey := prefix + "/output.parquet"
	engineOutKey := "out/" + newHex() + ".parquet"
	plain, secretVars, snapshot, err := d.envVariables(ctx, env.ID)
	if err != nil {
		return err
	}
	files := []any{}
	for _, f := range scriptnodes.FilesOf(node) {
		files = append(files, f.Value("path"))
	}
	declared := []any{}
	for _, in := range inputs {
		declared = append(declared, pyjson.Object{{Key: "alias", Value: in.alias}, {Key: "datasource_id", Value: in.ds.ID}, {Key: "name", Value: in.ds.Name}})
	}
	info := pyjson.Object{{Key: "saved_script_id", Value: s.ID}, {Key: "label", Value: s.Name}, {Key: "script_version", Value: s.CurrentVersion},
		{Key: "environment_id", Value: env.ID}, {Key: "environment", Value: env.Name}, {Key: "language", Value: env.Language},
		{Key: "version", Value: version.Number}, {Key: "image", Value: deref(version.ImageRef)}, {Key: "step_id", Value: stepID},
		{Key: "output", Value: true}, {Key: "entrypoint", Value: content.Value("entrypoint")}, {Key: "files", Value: files},
		{Key: "attempts", Value: int64(0)}, {Key: "exit_code", Value: nil}, {Key: "inputs", Value: declared},
		{Key: "publish", Value: publishes}, {Key: "variables", Value: snapshot}, {Key: "log", Value: ""}, {Key: "namespace", Value: ns},
		{Key: "internet_access", Value: version.InternetAccess}}
	var childID int64
	if err := d.DB.QueryRow(ctx, `INSERT INTO runs (kind, saved_script_id, task_id, status, launched_by, trigger_type, parent_run_id, engine,
		input_key, output_bucket, output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at)
		VALUES ('script', $1, $2, 'STARTED', $3, $4, $5, $6, '', $7, $8, '', false, '[]', $9, $10) RETURNING id`, s.ID, newHex(), user.ID,
		trigger, parent, fmt.Sprintf("%s v%d", env.Name, version.Number), bucket, engineOutKey, pyDumps(info), jobs.Now()).Scan(&childID); err != nil {
		return err
	}
	// execute's own save: no check of the run still open
	child := &childRun{d: d, id: childID, info: info, unchecked: true}
	if !version.InternetAccess {
		child.preface = offlineNotice(env.Name, version.Number)
	}
	what := fmt.Sprintf("the saved script «%s»", s.Name)
	written := []string{outKey}
	defer func() { d.deleteObjects(context.Background(), storage, written) }()
	err = func() error {
		inputEnv, described, keys, err := d.prepareInputs(ctx, inputs, storage, prefix)
		if err != nil {
			return err
		}
		written = append(written, keys...)
		timeout, retries, delay, err := retrySettings(pyjson.Object{{Key: "timeoutSeconds", Value: valueOr(content, "timeout_seconds", int64(900))},
			{Key: "retries", Value: valueOr(content, "retries", int64(0))}, {Key: "retryDelaySeconds", Value: valueOr(content, "retry_delay_seconds", int64(60))}})
		if err != nil {
			return err
		}
		code, items := codeItems(node)
		base := merge(inputEnv, pyjson.Object{{Key: "TABULARIA_INPUTS", Value: pyDumpsUnicode(described)},
			{Key: "TABULARIA_OUTPUT", Value: "s3://" + storage.Bucket + "/" + outKey}, {Key: "TABULARIA_STEP_ID", Value: stepID},
			{Key: "TABULARIA_RUN_ID", Value: fmt.Sprint(parent)}, {Key: "TABULARIA_SCRIPT_ID", Value: fmt.Sprint(s.ID)},
			{Key: "TABULARIA_SCRIPT_NAME", Value: s.Name}, {Key: "TABULARIA_SCRIPT_VERSION", Value: fmt.Sprint(s.CurrentVersion)},
			{Key: "HOME", Value: "/tmp"}, {Key: "TMPDIR", Value: "/tmp"}}, storage.env(), plain)
		key, err := d.Fernet.Decrypt(storage.SecretEnc)
		if err != nil {
			return err
		}
		secret := merge(pyjson.Object{{Key: "AWS_ACCESS_KEY_ID", Value: storage.AccessKey}, {Key: "AWS_SECRET_ACCESS_KEY", Value: key}}, secretVars)
		labels := pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "scripting"},
			{Key: labelEnv, Value: fmt.Sprint(env.ID)}, {Key: labelScriptRun, Value: fmt.Sprint(childID)},
			{Key: labelOrchestration, Value: fmt.Sprint(parent)}, {Key: "tabularia.io/stage", Value: "run"}}
		var offline *offlineNet
		if !version.InternetAccess {
			offline = offlineNetwork(ctx, storage, d.Log)
		}
		manifest := func(name string, attempt int64) pyjson.Object {
			return scriptJobManifest(q.d.jobSettings(), name, env, version, node, merge(base, pyjson.Object{{Key: "TABULARIA_ATTEMPT", Value: fmt.Sprint(attempt)}}),
				labels, timeout, items, name+"-code", name+"-env", offline)
		}
		ok, reason, err := q.attempts(ctx, child, "script", what, retries, delay, parent, true, storage, outKey, manifest, code, secret,
			timeout, ns, q.d.jobSettings().pollSeconds, q.d.jobSettings().logMaxBytes)
		if err != nil {
			return err
		}
		if !ok {
			return &scriptFailed{fmt.Sprintf("%s failed: %s%s", what, reason, offlineFailure(version.InternetAccess))}
		}
		// the datasource the output will replace, read now; its contract checked on the copy
		var target *jobDatasource
		var targetDesc string
		if publishes && publish.Value("overwrite") != false {
			var id int64
			var kind string
			err := d.DB.QueryRow(ctx, `SELECT id, kind, description FROM datasources WHERE project_id = $1 AND name = $2 LIMIT 1`,
				publish.Value("project_id"), pyjson.Str(publish.Value("name"))).Scan(&id, &kind, &targetDesc)
			if err == nil && kind == "flow" {
				if target, err = loadJobDatasource(ctx, d.DB, id); err != nil {
					return err
				}
			} else if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}
		req := d.jobRequest(ctx, user)
		var contract pyjson.Object
		if target != nil {
			if contract, err = runlaunch.ContractPayload(req.ctx, req.d.DB, &target.ID); err != nil {
				return err
			}
		}
		body := pyjson.Object{{Key: "connection", Value: storage.payload()}, {Key: "key", Value: outKey}, {Key: "bucket", Value: bucket},
			{Key: "output_key", Value: engineOutKey}}
		if contract != nil {
			body = append(body, pyjson.KV{Key: "contract", Value: contract})
		}
		cctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		res, raw, err := d.Engine.JSON(cctx, http.MethodPost, "/scripts/output/import", body)
		cancel()
		if err != nil {
			return err
		}
		if res.StatusCode >= 400 {
			return &scriptFailed{fmt.Sprintf("%s: its output could not be read: %s", what, engineDetail(raw))}
		}
		v, err := pyjson.Loads(string(raw))
		if err != nil {
			return &pyJSONError{err}
		}
		out := pyjson.Obj(v)
		if !pyjson.Truthy(out.Value("found")) {
			child.info = child.info.Set("output", false)
			return child.save(ctx, "output_key", "", "output_bucket", "", "status", "SUCCESS", "finished_at", jobs.Now())
		}
		child.info = child.info.Set("columns", pyjson.Or(out.Value("columns"), []any{}))
		rows, err := intColumn(out.Value("rows"))
		if err != nil {
			return err
		}
		if err := child.save(ctx, "rows_written", rows); err != nil {
			return err
		}
		if publishes {
			if err := q.publishSaved(ctx, req, childID, s, publish, target, targetDesc, out); err != nil {
				return err
			}
		}
		return child.save(ctx, "status", "SUCCESS", "finished_at", jobs.Now())
	}()
	if err == nil {
		return nil
	}
	var sf *scriptFailed
	if errors.As(err, &sf) {
		_ = child.save(ctx, "status", "FAILURE", "error", runes(sf.msg, 2000), "finished_at", jobs.Now())
		return err
	}
	message := errorMessage(err)
	_ = child.save(ctx, "status", "FAILURE", "error", runes(what+": "+message, 2000), "finished_at", jobs.Now())
	return &scriptFailed{what + ": " + message}
}

// publishSaved is saved_script_runs._publish: the output becomes the datasource, unless a
// blocking rule of the replaced datasource's contract is broken.
func (q *queue) publishSaved(ctx context.Context, req *request, childID int64, s *savedscripts.Script, publish pyjson.Object,
	target *jobDatasource, targetDesc string, out pyjson.Object) error {
	report, _ := out.Value("contract").(pyjson.Object)
	if target == nil {
		report = nil
	}
	if report != nil {
		doc, err := runlaunch.ContractPayload(req.ctx, req.d.DB, &target.ID)
		if err != nil {
			return err
		}
		if doc == nil {
			report = nil
		}
	}
	if report != nil && report.Value("outcome") == "failed" {
		err := pgx.BeginFunc(ctx, q.d.DB, func(tx pgx.Tx) error {
			c, err := contractOf(ctx, tx, target.ID)
			if err != nil || c == nil {
				return err
			}
			var key string
			if err := tx.QueryRow(ctx, `SELECT output_key FROM runs WHERE id = $1`, childID).Scan(&key); err != nil {
				return err
			}
			_, err = recordContractResult(ctx, tx, c, report, "publish", &childID, key, true)
			return err
		})
		if err != nil {
			return err
		}
		return &scriptFailed{brokenRulesMessage(report)}
	}
	description := fmt.Sprintf("Written by the saved script «%s»", s.Name)
	if target != nil && targetDesc != "" {
		description = targetDesc
	}
	overwrite := pyjson.Truthy(valueOr(publish, "overwrite", true))
	return pgx.BeginFunc(ctx, q.d.DB, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `UPDATE runs SET publish_name = $2, publish_project_id = $3, publish_overwrite = $4, publish_description = $5
			WHERE id = $1`, childID, pyjson.Str(publish.Value("name")), publish.Value("project_id"), overwrite, description); err != nil {
			return err
		}
		run, err := scanRunTx(ctx, tx, childID)
		if err != nil {
			return err
		}
		result := pyjson.Object{{Key: "columns", Value: pyjson.Or(out.Value("columns"), []any{})}, {Key: "rows_written", Value: out.Value("rows")}}
		if err := req.publishDatasource(tx, run, result); err != nil {
			return err
		}
		return req.recordContract(tx, childID, report, "publish")
	})
}
