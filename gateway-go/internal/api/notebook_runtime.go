package api

// services/notebook_runtime.py for the routes: a notebook's pod started (its Job, ConfigMap and
// Secret), followed until marimo answers, stopped, the editor's file read back, and the app's
// visitors. The reaper (the orchestrator's round) is the background work's.

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/notebookpod"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/savedscripts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sessions"
)

const (
	nbAppPort      = 2718
	nbFilesPort    = 2719
	nbTokenEnv     = "TABULARIA_NOTEBOOK_TOKEN"
	nbLibDir       = "/tabularia/lib"
	nbSourceDir    = "/tabularia/source"
	nbWorkDir      = "/work"
	nbStartLock    = 7_402_141
	nbVisitorsLock = 7_402_142
	nbCheckEvery   = time.Second
	nbNoJobSeconds = 60
	nbFileTimeout  = 5 * time.Second
	nbMaxFileBytes = 4 * 1024 * 1024
	nbScriptUID    = 10001 // script_builds.SCRIPT_UID
	nbVisitorStale = 3 * time.Minute
	nbTouchEvery   = 60 * time.Second
)

var (
	nbOwnVariables = map[string]bool{"HOME": true, "PYTHONPATH": true, "TABULARIA_URL": true, nbTokenEnv: true}
	nbNoMarimo     = regexp.MustCompile(`No module named '?marimo'?`)
	nbMarimoLocked = regexp.MustCompile(`(?mi)^marimo\s*(==|@)`)
	nbCopyAndRun   = "import os, shutil, sys; " + "shutil.copyfile('" + nbSourceDir + "/notebook.py', '" + nbWorkDir + "/notebook.py'); " +
		"os.execv(sys.executable, [sys.executable, '-m', 'marimo', *sys.argv[1:]])"
	nbWaitForMarimo = "import sys, time, urllib.request\n" + "end = time.monotonic() + 25\n" + "while time.monotonic() < end:\n" +
		"    try:\n" + "        urllib.request.urlopen(sys.argv[1], timeout=2)\n" + "        sys.exit(0)\n" + "    except Exception:\n" +
		"        time.sleep(0.25)\n" + "sys.exit(1)\n"
	// nbChecked is _checked: when this process last asked the cluster about a starting session
	nbChecked = struct {
		sync.Mutex
		at map[int64]time.Time
	}{at: map[int64]time.Time{}}
)

// nbSession is a row of notebook_sessions.
type nbSession struct {
	ID, NotebookID, UserID, Version int64
	Mode, Namespace, JobName        string
	Status, TokenFP                 string
	PodIP, Error                    *string
	StartedAt, LastSeenAt           time.Time
	ReadyAt                         *time.Time
}

const nbSessionColumns = `id, notebook_id, user_id, version, mode, namespace, job_name, status, token_fp, pod_ip, error, started_at,
	last_seen_at, ready_at`

func nbScanSession(row pgx.Row) (*nbSession, error) {
	var s nbSession
	err := row.Scan(&s.ID, &s.NotebookID, &s.UserID, &s.Version, &s.Mode, &s.Namespace, &s.JobName, &s.Status, &s.TokenFP, &s.PodIP,
		&s.Error, &s.StartedAt, &s.LastSeenAt, &s.ReadyAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &s, err
}

// nbRowOf is row_of.
func nbRowOf(ctx context.Context, q perm.Querier, notebookID int64, mode string) (*nbSession, error) {
	return nbScanSession(q.QueryRow(ctx, `SELECT `+nbSessionColumns+` FROM notebook_sessions WHERE notebook_id = $1 AND mode = $2
		LIMIT 1`, notebookID, mode))
}

// nbNaiveNow is _now(): naive UTC, to the microsecond, at the round's moment in cmd/jobsonce.
func nbNaiveNow() time.Time { return jobs.Now().Truncate(time.Microsecond) }

func nbAge(t time.Time) float64 { return nbNaiveNow().Sub(t).Seconds() }

// nbDisabledReason is notebook_runtime.disabled_reason.
func nbDisabledReason() string {
	switch scriptcfg.Get().DisabledReason() {
	case "kubernetes":
		return "Notebooks run on Kubernetes, with the scripting environments: they are off on this installation"
	case "":
		return ""
	}
	return "Notebooks need the scripting environments, which are not configured on this installation"
}

// nbBaseURL is base_url: where marimo answers, as the browser sees it.
func nbBaseURL(notebookID int64, mode string) string {
	return fmt.Sprintf("%s/notebooks/%d/%s", scriptcfg.Get().App.RootPath, notebookID, mode)
}

// nbTouch is touch: the pod's idle time starts again, at most once a minute.
func nbTouch(ctx context.Context, d *Deps, row *nbSession) error {
	now := nbNaiveNow()
	if now.Sub(row.LastSeenAt) < nbTouchEvery {
		return nil
	}
	row.LastSeenAt = now
	_, err := d.DB.Exec(ctx, `UPDATE notebook_sessions SET last_seen_at = $1 WHERE id = $2`, now, row.ID)
	return err
}

// nbRefusal is an HTTPException the runtime raises.
func nbRefusal(status int, format string, a ...any) *auth.Refusal {
	return &auth.Refusal{Status: status, Detail: fmt.Sprintf(format, a...)}
}

// nbBeingEdited is BeingEdited: somebody else has the notebook's editor open.
type nbBeingEdited struct {
	message string
	editor  *nbPerson
}

func (e *nbBeingEdited) Error() string { return e.message }

func nbEditedBy(ctx context.Context, q perm.Querier, n *nbRow, userID int64) error {
	who := "someone else"
	p, err := nbPersonOf(ctx, q, &userID)
	if err != nil {
		return err
	} else if p != nil {
		who = p.Email
	}
	return &nbBeingEdited{fmt.Sprintf("«%s» is being edited by %s", n.Name, who), p}
}

type nbHolder struct {
	ID    int64
	Email string
}

// nbAuthor is _author: whose rights the app reads with.
func nbAuthor(ctx context.Context, d *Deps, n *nbRow) (*nbHolder, error) {
	var createdBy *int64
	err := d.DB.QueryRow(ctx, `SELECT created_by FROM notebook_versions WHERE notebook_id = $1 AND version = $2 LIMIT 1`,
		n.ID, n.CurrentVersion).Scan(&createdBy)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	fix := "someone who may edit the notebook must save or restore a version, which then runs with their rights"
	var u *userRow
	if createdBy != nil && *createdBy != 0 {
		if u, err = scanUserRow(d.DB.QueryRow(ctx, `SELECT `+userRowColumns+` FROM users WHERE id = $1`, *createdBy)); errors.Is(err, pgx.ErrNoRows) {
			u, err = nil, nil
		} else if err != nil {
			return nil, err
		}
	}
	if u == nil {
		return nil, nbRefusal(422, "The author of the version the app runs no longer exists: %s", fix)
	}
	if !u.IsActive {
		return nil, nbRefusal(422, "The author of the version the app runs, %s, is deactivated: %s", u.Email, fix)
	}
	ok, err := savedscripts.CanSee(ctx, perm.For(d.DB, &u.User), d.DB, u.ID, n.ProjectID, n.EnvironmentID)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, nbRefusal(422, "The author of the version the app runs, %s, can no longer open this notebook (its folder or its environment): %s",
			u.Email, fix)
	}
	return &nbHolder{u.ID, u.Email}, nil
}

type nbImage struct {
	env            *scEnvironment
	versionID      int64
	imageRef, arch string
}

// nbImageOf is _image: the environment's image, ready and with marimo.
func nbImageOf(ctx context.Context, d *Deps, n *nbRow) (*nbImage, error) {
	env, err := scGetEnv(ctx, d.DB, n.EnvironmentID)
	if err != nil {
		return nil, err
	}
	if env == nil {
		return nil, nbRefusal(422, "The notebook's scripting environment no longer exists: choose another")
	}
	if !env.Enabled {
		return nil, nbRefusal(422, "The scripting environment «%s» is disabled", env.Name)
	}
	var status string
	var imageRef, lock, arch *string
	found := false
	if env.CurrentVersionID != nil && *env.CurrentVersionID != 0 {
		err := d.DB.QueryRow(ctx, `SELECT status, image_ref, lock, arch FROM script_environment_versions WHERE id = $1`, *env.CurrentVersionID).
			Scan(&status, &imageRef, &lock, &arch)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
		found = err == nil
	}
	if !found || status != "ready" || imageRef == nil || *imageRef == "" {
		return nil, nbRefusal(422, "The scripting environment «%s» has no image ready yet: an administrator builds it in Administration → Scripting environments",
			env.Name)
	}
	if lock != nil && *lock != "" && !nbMarimoLocked.MatchString(*lock) {
		return nil, nbRefusal(422, "%s", nbNoMarimoText(env.Name))
	}
	img := &nbImage{env: env, versionID: *env.CurrentVersionID, imageRef: *imageRef}
	if arch != nil {
		img.arch = *arch
	}
	return img, nil
}

// nbNoMarimoText is no_marimo.
func nbNoMarimoText(envName string) string {
	return "marimo is not in the image of the scripting environment «" + envName + "»: an administrator rebuilds it " +
		"(Administration → Scripting environments → Build again). Every Python environment has marimo now, " +
		"but the images built before it do not"
}

type kv struct{ k, v string }

// nbVariables is _variables (script_runs._variables without the notebook's own): (plain,
// secret), in the table's order.
func nbVariables(ctx context.Context, d *Deps, env *scEnvironment) ([]kv, []kv, error) {
	rows, err := d.DB.Query(ctx, `SELECT name, secret, value, value_enc, from_tabularia FROM script_environment_variables
		WHERE environment_id = $1`, env.ID)
	if err != nil {
		return nil, nil, err
	}
	type v struct {
		name, value  string
		secret, from bool
		enc          *string
	}
	var list []v
	for rows.Next() {
		var x v
		if err := rows.Scan(&x.name, &x.secret, &x.value, &x.enc, &x.from); err != nil {
			rows.Close()
			return nil, nil, err
		}
		list = append(list, x)
	}
	rows.Close()
	var plain, secret []kv
	put := func(list []kv, k, value string) []kv {
		for i := range list {
			if list[i].k == k {
				list[i].v = value
				return list
			}
		}
		return append(list, kv{k, value})
	}
	for _, x := range list {
		switch {
		case x.from:
			value, ok := os.LookupEnv(x.value)
			if !ok {
				return nil, nil, nbRefusal(422, "The scripting environment «%s» cannot start the notebook: the variable %s hands over Tabularia's %s, which is not set here",
					env.Name, x.name, x.value)
			}
			if !nbOwnVariables[x.name] {
				secret = put(secret, x.name, value)
			}
		case x.secret:
			enc := ""
			if x.enc != nil {
				enc = *x.enc
			}
			value, err := d.Fernet.Decrypt(enc)
			if err != nil {
				return nil, nil, err
			}
			if !nbOwnVariables[x.name] {
				secret = put(secret, x.name, value)
			}
		default:
			if !nbOwnVariables[x.name] {
				plain = put(plain, x.name, x.value)
			}
		}
	}
	return plain, secret, nil
}

func nbSecurity() pyjson.Object {
	return pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
		{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}
}

func nbLabels(notebookID int64, mode string) pyjson.Object {
	return pyjson.Object{{Key: "app.kubernetes.io/component", Value: "notebook"}, {Key: "tabularia.io/notebook", Value: fmt.Sprint(notebookID)},
		{Key: "tabularia.io/notebook-mode", Value: mode}}
}

// nbJobManifest is job_manifest: marimo, and in edit mode the container serving its file.
func nbJobManifest(name string, notebookID int64, mode string, img *nbImage, configName, secretName string, plain []kv) pyjson.Object {
	rt := scriptcfg.GetRuntime()
	base := nbBaseURL(notebookID, mode)
	args := []any{"run", nbWorkDir + "/notebook.py", "--host", "0.0.0.0", "--port", fmt.Sprint(nbAppPort), "--headless", "--base-url", base}
	if mode == "edit" {
		args[0] = "edit"
		args = append(args, "--no-token", "--skip-update-check")
	}
	var env []any
	for _, x := range plain {
		env = append(env, pyjson.Object{{Key: "name", Value: x.k}, {Key: "value", Value: x.v}})
	}
	env = append(env, pyjson.Object{{Key: "name", Value: "HOME"}, {Key: "value", Value: "/tmp"}},
		pyjson.Object{{Key: "name", Value: "PYTHONPATH"}, {Key: "value", Value: nbLibDir}})
	if g := strings.TrimSpace(rt.NotebooksGatewayURL); g != "" {
		env = append(env, pyjson.Object{{Key: "name", Value: "TABULARIA_URL"}, {Key: "value", Value: strings.TrimRight(g, "/")}})
	}
	command := append([]any{"python", "-c", nbCopyAndRun}, args...)
	threshold := (rt.NotebooksStartTimeout + 27) / 28
	if threshold < 1 {
		threshold = 1
	}
	e := img.env
	marimo := pyjson.Object{
		{Key: "name", Value: "marimo"}, {Key: "image", Value: img.imageRef}, {Key: "command", Value: command},
		{Key: "workingDir", Value: nbWorkDir}, {Key: "env", Value: env},
		{Key: "envFrom", Value: []any{pyjson.Object{{Key: "secretRef", Value: pyjson.Object{{Key: "name", Value: secretName}}}}}},
		{Key: "ports", Value: []any{pyjson.Object{{Key: "name", Value: "marimo"}, {Key: "containerPort", Value: int64(nbAppPort)}}}},
		{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: e.CPURequest}, {Key: "memory", Value: e.MemoryRequest}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: e.CPULimit}, {Key: "memory", Value: e.MemoryLimit}}}}},
		{Key: "securityContext", Value: nbSecurity()},
		{Key: "startupProbe", Value: pyjson.Object{{Key: "exec", Value: pyjson.Object{{Key: "command", Value: []any{"python", "-c", nbWaitForMarimo,
			fmt.Sprintf("http://127.0.0.1:%d%s/health", nbAppPort, base)}}}},
			{Key: "periodSeconds", Value: int64(3)}, {Key: "timeoutSeconds", Value: int64(28)}, {Key: "failureThreshold", Value: threshold}}},
		{Key: "readinessProbe", Value: pyjson.Object{{Key: "httpGet", Value: pyjson.Object{{Key: "path", Value: base + "/health"},
			{Key: "port", Value: int64(nbAppPort)}}}, {Key: "periodSeconds", Value: int64(2)}, {Key: "timeoutSeconds", Value: int64(2)},
			{Key: "failureThreshold", Value: int64(3)}}},
		{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "work"}, {Key: "mountPath", Value: nbWorkDir}},
			pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}},
			pyjson.Object{{Key: "name", Value: "lib"}, {Key: "mountPath", Value: nbLibDir}, {Key: "readOnly", Value: true}},
			pyjson.Object{{Key: "name", Value: "source"}, {Key: "mountPath", Value: nbSourceDir}, {Key: "readOnly", Value: true}}}},
	}
	containers := []any{marimo}
	if mode == "edit" {
		containers = append(containers, pyjson.Object{
			{Key: "name", Value: "files"}, {Key: "image", Value: img.imageRef},
			{Key: "command", Value: []any{"python", "-m", "http.server", fmt.Sprint(nbFilesPort), "--bind", "0.0.0.0", "--directory", nbWorkDir}},
			{Key: "ports", Value: []any{pyjson.Object{{Key: "name", Value: "files"}, {Key: "containerPort", Value: int64(nbFilesPort)}}}},
			{Key: "resources", Value: pyjson.Object{{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: "10m"}, {Key: "memory", Value: "32Mi"}}},
				{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: "100m"}, {Key: "memory", Value: "64Mi"}}}}},
			{Key: "securityContext", Value: nbSecurity()},
			{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "work"}, {Key: "mountPath", Value: nbWorkDir}, {Key: "readOnly", Value: true}}}},
		})
	}
	pod := pyjson.Object{
		{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false}, {Key: "enableServiceLinks", Value: false},
		{Key: "terminationGracePeriodSeconds", Value: int64(5)},
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(nbScriptUID)},
			{Key: "runAsGroup", Value: int64(nbScriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: containers},
	}
	if img.arch != "" {
		pod = append(pod, pyjson.KV{Key: "nodeSelector", Value: pyjson.Object{{Key: "kubernetes.io/arch", Value: img.arch}}})
	}
	pod = append(pod, pyjson.KV{Key: "volumes", Value: []any{
		pyjson.Object{{Key: "name", Value: "work"}, {Key: "emptyDir", Value: pyjson.Object{}}},
		pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}},
		pyjson.Object{{Key: "name", Value: "lib"}, {Key: "configMap", Value: pyjson.Object{{Key: "name", Value: configName},
			{Key: "items", Value: []any{pyjson.Object{{Key: "key", Value: "tabularia.py"}, {Key: "path", Value: "tabularia.py"}}}}}}},
		pyjson.Object{{Key: "name", Value: "source"}, {Key: "configMap", Value: pyjson.Object{{Key: "name", Value: configName},
			{Key: "items", Value: []any{pyjson.Object{{Key: "key", Value: "notebook.py"}, {Key: "path", Value: "notebook.py"}}}}}}},
	}})
	if rt.ScriptsPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: rt.ScriptsPullSecret}}}})
	}
	tags := nbLabels(notebookID, mode)
	return pyjson.Object{
		{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: tags}}},
		{Key: "spec", Value: pyjson.Object{{Key: "backoffLimit", Value: int64(0)}, {Key: "activeDeadlineSeconds", Value: rt.NotebooksMaxLifetimeHours * 3600},
			{Key: "ttlSecondsAfterFinished", Value: scriptsJobTTL()},
			{Key: "template", Value: pyjson.Object{{Key: "metadata", Value: pyjson.Object{{Key: "labels", Value: tags}}}, {Key: "spec", Value: pod}}}}},
	}
}

// nbCreatePod is _create_pod: the Job, then its ConfigMap and Secret, owned by it.
func nbCreatePod(ctx context.Context, row *nbSession, img *nbImage, source, token string, plain, secret []kv) error {
	configName, secretName := row.JobName+"-files", row.JobName+"-token"
	manifest := nbJobManifest(row.JobName, row.NotebookID, row.Mode, img, configName, secretName, plain)
	tags := nbLabels(row.NotebookID, row.Mode)
	job, err := kube.CreateJob(ctx, kubeAPI(), row.Namespace, manifest)
	if err != nil {
		return err
	}
	owners := kube.OwnedBy(job)
	if err := kube.CreateObject(ctx, kubeAPI(), row.Namespace, "configmaps", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "ConfigMap"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: configName}, {Key: "labels", Value: tags}, {Key: "ownerReferences", Value: owners}}},
		{Key: "data", Value: pyjson.Object{{Key: "notebook.py", Value: source}, {Key: "tabularia.py", Value: notebookpod.Module}}}}); err != nil {
		return err
	}
	data := pyjson.Object{}
	for _, x := range secret {
		data = append(data, pyjson.KV{Key: x.k, Value: x.v})
	}
	data = data.Set(nbTokenEnv, token)
	return kube.CreateObject(ctx, kubeAPI(), row.Namespace, "secrets", pyjson.Object{{Key: "apiVersion", Value: "v1"}, {Key: "kind", Value: "Secret"},
		{Key: "type", Value: "Opaque"}, {Key: "metadata", Value: pyjson.Object{{Key: "name", Value: secretName}, {Key: "labels", Value: tags},
			{Key: "ownerReferences", Value: owners}}}, {Key: "stringData", Value: data}})
}

// nbEnsureStarted is ensure_started: the live session in `mode`, started when there is none.
// Whoever asks has been checked already. An *nbBeingEdited or an *auth.Refusal is an answer.
func nbEnsureStarted(ctx context.Context, d *Deps, r *http.Request, n *nbRow, mode string, user *auth.User) (*nbSession, error) {
	if why := nbDisabledReason(); why != "" {
		return nil, nbRefusal(400, "%s", why)
	}
	row, err := nbRowOf(ctx, d.DB, n.ID, mode)
	if err != nil {
		return nil, err
	}
	if row != nil && row.Status != "failed" {
		if mode == "edit" && row.UserID != user.ID {
			return nil, nbEditedBy(ctx, d.DB, n, row.UserID)
		}
		if mode == "app" && row.Version != n.CurrentVersion {
			if err := nbStop(ctx, d, row, nil, "new version", false); err != nil {
				return nil, err
			}
		} else {
			if err := nbTouch(ctx, d, row); err != nil {
				return nil, err
			}
			return row, nbRefresh(ctx, d, row, false)
		}
	} else if row != nil {
		if err := nbStop(ctx, d, row, nil, "tried again", true); err != nil {
			return nil, err
		}
	}
	return nbStart(ctx, d, n, mode, user)
}

func nbTokenHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// nbStart is _start.
func nbStart(ctx context.Context, d *Deps, n *nbRow, mode string, user *auth.User) (*nbSession, error) {
	holder := &nbHolder{user.ID, user.Email}
	if mode != "edit" {
		h, err := nbAuthor(ctx, d, n)
		if err != nil {
			return nil, err
		}
		holder = h
	}
	img, err := nbImageOf(ctx, d, n)
	if err != nil {
		return nil, err
	}
	plain, secret, err := nbVariables(ctx, d, img.env)
	if err != nil {
		return nil, err
	}
	var source string
	if mode == "edit" && n.Draft != nil {
		source = *n.Draft
	} else {
		content, err := nbContentOf(ctx, d.DB, n, 0)
		if err != nil {
			return nil, err
		}
		source, _ = content.Value("source").(string)
	}
	token := "nbk_" + sessions.TokenURLSafe(32)
	sum := sha256.Sum256([]byte(token))
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	end := time.Now().Add(5 * time.Second)
	for {
		var got bool
		if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(nbStartLock)).Scan(&got); err != nil {
			return nil, err
		}
		if got {
			break
		}
		if time.Now().After(end) {
			return nil, nbRefusal(503, "Tabularia is busy starting other notebooks: try again in a moment")
		}
		time.Sleep(100 * time.Millisecond)
	}
	var live int64
	if err := tx.QueryRow(ctx, `SELECT count(*) FROM notebook_sessions WHERE status IN ('starting', 'ready')`).Scan(&live); err != nil {
		return nil, err
	}
	if capLive := scriptcfg.GetRuntime().NotebooksMaxLive; live >= capLive {
		return nil, nbRefusal(503, "%d notebooks are running already, the most this installation allows: try again later, or ask an administrator (NOTEBOOKS__MAX_LIVE)",
			live)
	}
	now := nbNaiveNow()
	row := &nbSession{NotebookID: n.ID, Mode: mode, UserID: holder.ID, Version: n.CurrentVersion, Namespace: scriptcfg.Get().Scripts.Namespace,
		JobName: fmt.Sprintf("nb-%d-%s-%s", n.ID, mode, nbTokenHex(3)), Status: "starting", TokenFP: hex.EncodeToString(sum[:]),
		StartedAt: now, LastSeenAt: now}
	err = tx.QueryRow(ctx, `INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, pod_ip, status, error,
		token_fp, started_at, ready_at, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6, NULL, 'starting', NULL, $7, $8, NULL, $9) RETURNING id`,
		row.NotebookID, row.Mode, row.UserID, row.Version, row.Namespace, row.JobName, row.TokenFP, row.StartedAt, row.LastSeenAt).Scan(&row.ID)
	if err == nil {
		err = tx.Commit(ctx)
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" { // another replica started it a moment ago
		existing, err := nbRowOf(ctx, d.DB, n.ID, mode)
		if err != nil {
			return nil, err
		}
		if existing == nil {
			return nil, nbRefusal(409, "The notebook was being started and stopped at once: try again")
		}
		if mode == "edit" && existing.UserID != user.ID {
			return nil, nbEditedBy(ctx, d.DB, n, existing.UserID)
		}
		return existing, nil
	}
	if err != nil {
		return nil, err
	}
	nbTellLineage(ctx, d, d.DB, row, "START", "", nil) // the session is a run of the notebook's job
	if err := nbCreatePod(ctx, row, img, source, token, plain, secret); err != nil {
		var ke *kube.Error
		if errors.As(err, &ke) {
			if err := nbFail(ctx, d, row, "the cluster refused its pod: "+ke.Message); err != nil {
				return nil, err
			}
		} else {
			_ = nbFail(ctx, d, row, "its pod could not be created (Exception)")
			return nil, err
		}
	}
	detail := pyjson.Object{{Key: "mode", Value: mode}, {Key: "version", Value: row.Version}, {Key: "rights_of", Value: holder.Email},
		{Key: "environment", Value: img.env.Name}}
	if row.Status == "failed" {
		detail = append(detail, pyjson.KV{Key: "outcome", Value: row.Error})
	}
	kind := "notebook"
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: "notebook.start", TargetType: &kind, TargetID: &n.ID, TargetLabel: &n.Name,
		Detail: detail})
	return row, nil
}

func nbContainer(pod any, name string) pyjson.Object {
	for _, st := range kube.List(kube.Dict(kube.Dict(pod).Value("status")).Value("containerStatuses")) {
		if kube.Dict(st).Value("name") == name {
			return kube.Dict(st)
		}
	}
	return nil
}

func nbPodReady(pod any) bool {
	status := kube.Dict(kube.Dict(pod).Value("status"))
	if !pyjson.Truthy(status.Value("podIP")) {
		return false
	}
	for _, c := range kube.List(status.Value("conditions")) {
		o := kube.Dict(c)
		if o.Value("type") == "Ready" && o.Value("status") == "True" {
			return true
		}
	}
	return false
}

func nbLatest(pods []any) any {
	sorted := kube.SortByCreation(pods)
	if len(sorted) == 0 {
		return nil
	}
	return sorted[len(sorted)-1]
}

// nbEnded is _ended: why marimo is no longer running in the pod, "" while it runs.
func nbEnded(ctx context.Context, d *Deps, row *nbSession, pod any) (string, error) {
	terminated := kube.Dict(kube.Dict(nbContainer(pod, "marimo").Value("state")).Value("terminated"))
	stopped := kube.StoppedByCluster(pod)
	if len(terminated) == 0 && stopped == "" {
		return "", nil
	}
	if stopped != "" {
		return kube.StoppedByClusterText, nil
	}
	log, err := kube.PodLog(ctx, kubeAPI(), row.Namespace, kube.Text(kube.Dict(kube.Dict(pod).Value("metadata")).Value("name")), "marimo", 40)
	var ke *kube.Error
	if errors.As(err, &ke) {
		log = ""
	} else if err != nil {
		return "", err
	}
	if nbNoMarimo.MatchString(log) {
		name := "?"
		var envName string
		err := d.DB.QueryRow(ctx, `SELECT e.name FROM notebooks n JOIN script_environments e ON e.id = n.environment_id WHERE n.id = $1`,
			row.NotebookID).Scan(&envName)
		if err == nil {
			name = envName
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return "", err
		}
		return nbNoMarimoText(name), nil
	}
	limit := ""
	for _, c := range kube.List(kube.Dict(kube.Dict(pod).Value("spec")).Value("containers")) {
		if kube.Dict(c).Value("name") == "marimo" {
			limit = kube.Text(kube.Dict(kube.Dict(kube.Dict(c).Value("resources")).Value("limits")).Value("memory"))
		}
	}
	why := kube.ExplainExit(terminated.Value("exitCode"), kube.Text(terminated.Value("reason")), limit)
	lines := scriptnodes.SplitLines(pystr.Strip(log))
	if len(lines) > 8 {
		lines = lines[len(lines)-8:]
	}
	out := "marimo stopped: " + why
	if last := strings.Join(lines, "\n"); last != "" {
		out += "\n" + last
	}
	return out, nil
}

// nbRefresh is refresh: a starting session moved forward, asked at most once a second per
// process unless `force`.
func nbRefresh(ctx context.Context, d *Deps, row *nbSession, force bool) error {
	if row.Status != "starting" {
		return nil
	}
	nbChecked.Lock()
	last, seen := nbChecked.at[row.ID]
	if !force && seen && time.Since(last) < nbCheckEvery {
		nbChecked.Unlock()
		return nil
	}
	nbChecked.at[row.ID] = time.Now()
	nbChecked.Unlock()
	err := nbRefreshStep(ctx, d, row)
	var ke *kube.Error
	if errors.As(err, &ke) {
		if ke.Status == 0 || ke.Status == 429 || ke.Status >= 500 {
			d.Log.Warn(fmt.Sprintf("notebooks: session %d not checked (%s)", row.ID, ke.Message))
			return nil
		}
		return nbFail(ctx, d, row, ke.Message)
	}
	return err
}

func nbRefreshStep(ctx context.Context, d *Deps, row *nbSession) error {
	k := kubeAPI()
	job, err := kube.GetJob(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	if job == nil {
		if nbAge(row.StartedAt) > nbNoJobSeconds {
			return nbFail(ctx, d, row, "its Job is not in the cluster (the gateway that started it stopped)")
		}
		return nil
	}
	if kube.JobOutcome(job) != "" {
		why := kube.JobFailure(job)
		if why == "" {
			why = "its Job ended before the notebook answered"
		}
		return nbFail(ctx, d, row, why)
	}
	pods, err := kube.JobPods(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	pod := nbLatest(pods)
	if pod != nil && nbPodReady(pod) {
		ip := pyjson.Str(kube.Dict(kube.Dict(pod).Value("status")).Value("podIP"))
		now := nbNaiveNow()
		if _, err := d.DB.Exec(ctx, `UPDATE notebook_sessions SET status = 'ready', pod_ip = $2, ready_at = $3, error = NULL WHERE id = $1`,
			row.ID, ip, now); err != nil {
			return err
		}
		row.Status, row.PodIP, row.ReadyAt, row.Error = "ready", &ip, &now, nil
		nbChecked.Lock()
		delete(nbChecked.at, row.ID)
		nbChecked.Unlock()
		return nil
	}
	if pod != nil {
		ended, err := nbEnded(ctx, d, row, pod)
		if err != nil {
			return err
		}
		if ended != "" {
			return nbFail(ctx, d, row, ended)
		}
	}
	cannot, unscheduled, _, err := kube.StartState(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	if cannot == "" && !kube.HasPod(job) && nbAge(row.StartedAt) > 10 {
		if cannot, err = kube.Refused(ctx, k, row.Namespace, row.JobName); err != nil {
			return err
		}
	}
	if cannot != "" {
		return nbFail(ctx, d, row, cannot)
	}
	timeout := scriptcfg.GetRuntime().NotebooksStartTimeout
	if nbAge(row.StartedAt) > float64(timeout) {
		why := ""
		if unscheduled != "" {
			why = ": no node could take it (" + unscheduled + ")"
		}
		return nbFail(ctx, d, row, fmt.Sprintf("it did not start within %d s%s", timeout, why))
	}
	return nil
}

// nbFail is _fail: the row stays, saying why; its Job goes now.
func nbFail(ctx context.Context, d *Deps, row *nbSession, why string) error {
	// whoever fails it first tells the lineage: a replica following the start and the reaper
	// can get here together, and the run would fail twice in the catalog
	tag, err := d.DB.Exec(ctx, `UPDATE notebook_sessions SET status = 'failed' WHERE id = $1 AND status <> 'failed'`, row.ID)
	if err != nil {
		return err
	}
	first := tag.RowsAffected() == 1
	message := pystr.Cut("The notebook could not start: "+why, 4000)
	if _, err := d.DB.Exec(ctx, `UPDATE notebook_sessions SET status = 'failed', error = $2, pod_ip = NULL WHERE id = $1`, row.ID, message); err != nil {
		return err
	}
	row.Status, row.Error, row.PodIP = "failed", &message, nil
	nbChecked.Lock()
	delete(nbChecked.at, row.ID)
	nbChecked.Unlock()
	d.Log.Info(fmt.Sprintf("notebooks: session %d of notebook %d failed: %s", row.ID, row.NotebookID, pystr.Cut(why, 300)))
	if first {
		nbTellLineage(ctx, d, d.DB, row, "FAIL", message, nil)
	}
	err = kube.DeleteJob(ctx, kubeAPI(), row.Namespace, row.JobName)
	var ke *kube.Error
	if errors.As(err, &ke) {
		return nil
	}
	return err
}

// nbAlive is alive: a ready session's marimo still runs; when it does not, it is stopped.
func nbAlive(ctx context.Context, d *Deps, row *nbSession) (bool, error) {
	k := kubeAPI()
	var ke *kube.Error
	job, err := kube.GetJob(ctx, k, row.Namespace, row.JobName)
	if errors.As(err, &ke) {
		return true, nil
	} else if err != nil {
		return false, err
	}
	var pod any
	if pyjson.Truthy(job) {
		pods, err := kube.JobPods(ctx, k, row.Namespace, row.JobName)
		if errors.As(err, &ke) {
			return true, nil
		} else if err != nil {
			return false, err
		}
		pod = nbLatest(pods)
	}
	if job != nil && kube.JobOutcome(job) == "" && pod != nil {
		ended, err := nbEnded(ctx, d, row, pod)
		if err != nil {
			return false, err
		}
		if ended == "" {
			return true, nil
		}
	}
	return false, nbStop(ctx, d, row, nil, "stopped", false)
}

// nbReadFile is read_file: what marimo last saved in the editor's pod.
func nbReadFile(ctx context.Context, d *Deps, row *nbSession) (string, bool) {
	if row.Mode != "edit" || row.PodIP == nil || *row.PodIP == "" {
		return "", false
	}
	client := &http.Client{Timeout: nbFileTimeout, Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{}).DialContext}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("http://%s:%d/notebook.py", *row.PodIP, nbFilesPort), nil)
	if err != nil {
		return "", false
	}
	res, err := client.Do(req)
	if err != nil {
		d.Log.Info(fmt.Sprintf("notebooks: the editor's file of session %d not read (%v)", row.ID, err))
		return "", false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return "", false
	}
	data, err := io.ReadAll(io.LimitReader(res.Body, nbMaxFileBytes+1))
	if err != nil {
		d.Log.Info(fmt.Sprintf("notebooks: the editor's file of session %d not read (%v)", row.ID, err))
		return "", false
	}
	if len(data) > nbMaxFileBytes {
		d.Log.Warn(fmt.Sprintf("notebooks: the editor's file of session %d is too large", row.ID))
		return "", false
	}
	return nbDecode(data), true
}

// nbDecode is bytes.decode("utf-8", errors="replace"): a replacement character for each byte
// that is not part of a valid sequence.
func nbDecode(data []byte) string {
	if utf8.Valid(data) {
		return string(data)
	}
	var b strings.Builder
	for len(data) > 0 {
		r, size := utf8.DecodeRune(data)
		if r == utf8.RuneError && size == 1 {
			b.WriteRune('\uFFFD')
		} else {
			b.Write(data[:size])
		}
		data = data[size:]
	}
	return b.String()
}

// nbSyncDraft is sync_draft: the editor's file becomes the notebook's draft when it differs
// from the current version.
func nbSyncDraft(ctx context.Context, d *Deps, row *nbSession) error {
	source, read := nbReadFile(ctx, d, row)
	n, err := nbGet(ctx, d.DB, row.NotebookID, false)
	if err != nil || !read || n == nil {
		return err
	}
	content, err := nbContentOf(ctx, d.DB, n, 0)
	if err != nil {
		return err
	}
	var draft *string
	if cur, isText := content.Value("source").(string); !isText || cur != source {
		draft = &source
	}
	if draft != nil && len(*draft) > nbMaxSourceBytes {
		d.Log.Warn(fmt.Sprintf("notebooks: the editor's file of notebook %d is over the size of a version: not kept", n.ID))
		return nil
	}
	same := (draft == nil && n.Draft == nil) || (draft != nil && n.Draft != nil && *draft == *n.Draft)
	if same {
		return nil
	}
	var by *int64
	var at *time.Time
	if draft != nil {
		now := jobs.Now()
		by, at = &row.UserID, &now
	}
	_, err = d.DB.Exec(ctx, `UPDATE notebooks SET draft = $2, draft_by = $3, draft_at = $4 WHERE id = $1`, n.ID, draft, by, at)
	return err
}

// nbStop is stop: the pod stopped and the session forgotten; an editor's file becomes the
// draft first. `actor` nil: the orchestrator's doing.
func nbStop(ctx context.Context, d *Deps, row *nbSession, actor *auth.User, reason string, quiet bool) error {
	if row.Mode == "edit" && row.Status == "ready" {
		if err := nbSyncDraft(ctx, d, row); err != nil {
			return err
		}
	}
	var ke *kube.Error
	if err := kube.DeleteJob(ctx, kubeAPI(), row.Namespace, row.JobName); errors.As(err, &ke) {
		d.Log.Warn(fmt.Sprintf("notebooks: the Job %s not deleted now (%s)", row.JobName, ke.Message))
	} else if err != nil {
		return err
	}
	n, err := nbGet(ctx, d.DB, row.NotebookID, false)
	if err != nil {
		return err
	}
	was := row.Status
	tag, err := d.DB.Exec(ctx, `DELETE FROM notebook_sessions WHERE id = $1`, row.ID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() > 0 && was != "failed" {
		// its run ends with it, told by whoever removed the row: a person and the reaper
		// stopping it together tell it once (a failed one was told already)
		nbTellLineage(ctx, d, d.DB, row, nbEndState(was), "", nil)
	}
	nbChecked.Lock()
	delete(nbChecked.at, row.ID)
	nbChecked.Unlock()
	if quiet || was == "failed" || n == nil {
		return nil
	}
	kind := "notebook"
	e := audit.Event{Actor: actor, Action: "notebook.stop", TargetType: &kind, TargetID: &n.ID, TargetLabel: &n.Name,
		Detail: pyjson.Object{{Key: "mode", Value: row.Mode}, {Key: "reason", Value: reason}}}
	if actor == nil {
		e.ActorLabel = "orchestrator"
	}
	audit.Record(ctx, d.DB, d.Log, e)
	return nil
}

// ── visitors ────────────────────────────────────────────────────────────────

// nbVisitorCount is visitors: the people with the app open now, on every replica.
func nbVisitorCount(ctx context.Context, q perm.Querier, notebookID int64) (int64, error) {
	var n int64
	err := q.QueryRow(ctx, `SELECT count(*) FROM notebook_visitors WHERE notebook_id = $1 AND last_seen_at >= $2`, notebookID,
		nbNaiveNow().Add(-nbVisitorStale)).Scan(&n)
	return n, err
}

// nbIsVisiting is is_visiting.
func nbIsVisiting(ctx context.Context, q perm.Querier, notebookID, userID int64) (bool, error) {
	var id int64
	err := q.QueryRow(ctx, `SELECT id FROM notebook_visitors WHERE notebook_id = $1 AND user_id = $2 AND last_seen_at >= $3 LIMIT 1`,
		notebookID, userID, nbNaiveNow().Add(-nbVisitorStale)).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// nbTakeVisitor is take_visitor: (the visitor's id or 0 when full, the visitors counted).
func nbTakeVisitor(ctx context.Context, d *Deps, n *nbRow, userID int64) (int64, int64, error) {
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1::integer, $2::integer)`, int64(nbVisitorsLock), n.ID); err != nil {
		return 0, 0, err
	}
	count, err := nbVisitorCount(ctx, tx, n.ID)
	if err != nil {
		return 0, 0, err
	}
	if count >= nbVisitorsCap(n.MaxVisitors) {
		return 0, count, nil
	}
	now := nbNaiveNow()
	var id int64
	if err := tx.QueryRow(ctx, `INSERT INTO notebook_visitors (notebook_id, user_id, opened_at, last_seen_at) VALUES ($1, $2, $3, $3) RETURNING id`,
		n.ID, userID, now).Scan(&id); err != nil {
		return 0, 0, err
	}
	return id, count + 1, tx.Commit(ctx)
}

// nbKeepVisitor is keep_visitor.
func nbKeepVisitor(ctx context.Context, d *Deps, id int64) error {
	_, err := d.DB.Exec(ctx, `UPDATE notebook_visitors SET last_seen_at = $1 WHERE id = $2`, nbNaiveNow(), id)
	return err
}

// nbDropVisitor is drop_visitor.
func nbDropVisitor(ctx context.Context, d *Deps, id int64) error {
	_, err := d.DB.Exec(ctx, `DELETE FROM notebook_visitors WHERE id = $1`, id)
	return err
}
