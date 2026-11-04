package api

// services/notebook_runtime.py reaper_loop: on Kubernetes, the notebooks' pods looked
// after every 30 seconds — the idle ones and those gone from the cluster stopped (an
// editor's file kept as the notebook's draft first, and at every round), the ones that do
// not start failed saying why, the visitors nobody refreshed cleared, the Jobs nobody holds
// deleted. It returns at once where the scripting environments are off.
//
// One reaper at a time: the session advisory lock 7402140, held for the whole round on a
// connection of its own (the round commits as it goes), as the Python reaper holds it.
//
// Not ported: the OpenLineage events of the sessions that end (openlineage.notebook_session).
// With OpenLineage on, the loop is left to the Python gateway's orchestrator, as the routes
// that tell the catalog are left to it.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	nrLock         = 7_402_140        // LOCK: the reaper's round
	nrReapEvery    = 30 * time.Second // REAP_SECONDS
	nrNoJobSeconds = 60               // NO_JOB_SECONDS
	nrFilesPort    = 2719             // FILES_PORT
	nrFileTimeout  = 5 * time.Second  // FILE_TIMEOUT
	nrMaxFileBytes = 4 * 1024 * 1024  // MAX_FILE_BYTES
	nrOrchestrator = "orchestrator"   // the audit's actor when the reaper stops a pod
	nrVisitorStale = 3 * time.Minute  // VISITOR_STALE
	nrComponent    = "app.kubernetes.io/component=notebook"
)

var nrNoMarimo = regexp.MustCompile(`No module named '?marimo'?`)

func init() {
	RegisterJob("notebooks_loop", func(d *Deps) jobs.Job {
		n := &notebookReaper{db: d.DB, log: d.Log, panel: d.Panel, cfg: d.Config.Jobs.Kube, s: scriptcfg.Load(config.FromOS()),
			lineage: d.Config.OpenLineage}
		return jobs.Job{Order: 11, Loop: n.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_round
			"notebooks": func(ctx context.Context) (any, error) {
				if n.s.DisabledReason() != "" {
					return nil, nil
				}
				return nil, n.oneRound(ctx)
			},
		}}
	})
}

type notebookReaper struct {
	db      *pgxpool.Pool
	log     *slog.Logger
	panel   *settings.Panel
	cfg     config.KubeJobsConfig
	s       *scriptcfg.Settings
	lineage bool
}

// nrSession is a row of notebook_sessions.
type nrSession struct {
	ID, NotebookID, UserID   int64
	Mode, Namespace, JobName string
	Status                   string
	PodIP                    *string
	StartedAt, LastSeenAt    time.Time
}

func (n *notebookReaper) kubeAPI() scriptcfg.Kubernetes {
	k := n.s.Kubernetes
	k.TimeoutSeconds = n.panel.KubernetesTimeout()
	return k
}

// age is _age: seconds since `t` (naive UTC).
func nrAge(t time.Time, now time.Time) float64 { return now.Sub(t).Seconds() }

// nrNoMarimoText is notebook_runtime.no_marimo.
func nrNoMarimoText(envName string) string {
	return "marimo is not in the image of the scripting environment «" + envName + "»: an administrator rebuilds it " +
		"(Administration → Scripting environments → Build again). Every Python environment has marimo now, " +
		"but the images built before it do not"
}

// oneRound is notebook_runtime.one_round.
func (n *notebookReaper) oneRound(ctx context.Context) error {
	conn, err := n.db.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	var mine bool
	if err := conn.QueryRow(ctx, `SELECT pg_try_advisory_lock($1)`, int64(nrLock)).Scan(&mine); err != nil {
		return err
	}
	if !mine {
		return nil
	}
	defer func() {
		// a context of its own: the lock goes even when the round's was cancelled
		_, _ = conn.Exec(context.WithoutCancel(ctx), `SELECT pg_advisory_unlock($1)`, int64(nrLock))
	}()
	k := n.kubeAPI()
	// the Jobs first: one created after this listing has its row already
	listed, err := kube.ListJobs(ctx, k, n.s.Scripts.Namespace, nrComponent)
	var ke *kube.Error
	if errors.As(err, &ke) {
		n.log.Warn(fmt.Sprintf("notebooks: the Jobs not listed (%s)", ke.Message))
		listed = nil
	} else if err != nil {
		return err
	}
	ok := err == nil
	byName := map[any]any{}
	var names []any // in the listing's order, as a dict keeps them
	for _, j := range listed {
		name := kube.Dict(kube.Dict(j).Value("metadata")).Value("name")
		if _, seen := byName[name]; !seen {
			names = append(names, name)
		}
		byName[name] = j
	}
	idle := float64(n.cfg.NotebooksIdleMinutes * 60)
	rows, err := n.db.Query(ctx, `SELECT id, notebook_id, user_id, mode, namespace, job_name, status, pod_ip, started_at, last_seen_at
		FROM notebook_sessions ORDER BY id`)
	if err != nil {
		return err
	}
	var sessions []*nrSession
	for rows.Next() {
		var s nrSession
		if err := rows.Scan(&s.ID, &s.NotebookID, &s.UserID, &s.Mode, &s.Namespace, &s.JobName, &s.Status, &s.PodIP, &s.StartedAt,
			&s.LastSeenAt); err != nil {
			rows.Close()
			return err
		}
		sessions = append(sessions, &s)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, row := range sessions {
		var jobsByName map[any]any
		if ok {
			jobsByName = byName
		}
		if err := n.reap(ctx, row, jobsByName, idle); err != nil {
			if errors.As(err, &ke) {
				n.log.Warn(fmt.Sprintf("notebooks: session %d not checked (%s)", row.ID, ke.Message))
				continue
			}
			return err
		}
	}
	// the visitors nobody refreshed (their replica died): they count no more already
	if _, err := n.db.Exec(ctx, `DELETE FROM notebook_visitors WHERE last_seen_at < $1`, jobs.NaiveNow().Add(-nrVisitorStale)); err != nil {
		return err
	}
	if !ok {
		return nil
	}
	held := map[string]bool{}
	hr, err := n.db.Query(ctx, `SELECT job_name FROM notebook_sessions`)
	if err != nil {
		return err
	}
	for hr.Next() {
		var name string
		if err := hr.Scan(&name); err != nil {
			hr.Close()
			return err
		}
		held[name] = true
	}
	hr.Close()
	if err := hr.Err(); err != nil {
		return err
	}
	for _, key := range names {
		name, _ := key.(string)
		if !pyjson.Truthy(key) || held[name] || kube.JobOutcome(byName[key]) != "" {
			continue
		}
		n.log.Info(fmt.Sprintf("notebooks: the Job %s has no session: deleted", name))
		if err := kube.DeleteJob(ctx, k, n.s.Scripts.Namespace, name); err != nil && !errors.As(err, &ke) {
			return err
		}
	}
	return nil
}

// reap is notebook_runtime._reap. `jobsByName` nil: the Jobs could not be listed.
func (n *notebookReaper) reap(ctx context.Context, row *nrSession, jobsByName map[any]any, idle float64) error {
	now := jobs.NaiveNow()
	if row.Status == "failed" {
		if nrAge(row.StartedAt, now) > idle {
			return n.stop(ctx, row, "failed", true)
		}
		return nil
	}
	if row.Status == "starting" {
		return n.refresh(ctx, row)
	}
	var job any = pyjson.Object{} // not listed: nothing known of it
	gone := false                 // `job is None`: listed, and not there
	if jobsByName != nil {
		var there bool
		job, there = jobsByName[row.JobName]
		gone = !there
	}
	if gone || (pyjson.Truthy(job) && kube.JobOutcome(job) != "") {
		return n.stop(ctx, row, "ended", false)
	}
	if nrAge(row.LastSeenAt, now) > idle {
		return n.stop(ctx, row, "idle", false)
	}
	alive, err := n.alive(ctx, row)
	if err != nil || !alive {
		return err
	}
	if row.Mode == "edit" {
		_, err := n.syncDraft(ctx, row)
		return err
	}
	return nil
}

// container is _container: the status of the pod's container of that name.
func nrContainer(pod any, name string) pyjson.Object {
	for _, st := range kube.List(kube.Dict(kube.Dict(pod).Value("status")).Value("containerStatuses")) {
		if kube.Dict(st).Value("name") == name {
			return kube.Dict(st)
		}
	}
	return nil
}

// ready is _ready: the pod has its address and is Ready.
func nrReady(pod any) bool {
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

// latest is _latest.
func nrLatest(pods []any) any {
	sorted := kube.SortByCreation(pods)
	if len(sorted) == 0 {
		return nil
	}
	return sorted[len(sorted)-1]
}

// ended is _ended: why marimo is no longer running in the pod, "" while it runs.
func (n *notebookReaper) ended(ctx context.Context, row *nrSession, pod any) (string, error) {
	terminated := kube.Dict(kube.Dict(nrContainer(pod, "marimo").Value("state")).Value("terminated"))
	stopped := kube.StoppedByCluster(pod)
	if len(terminated) == 0 && stopped == "" {
		return "", nil
	}
	if stopped != "" {
		return kube.StoppedByClusterText, nil
	}
	log, err := kube.PodLog(ctx, n.kubeAPI(), row.Namespace, kube.Text(kube.Dict(kube.Dict(pod).Value("metadata")).Value("name")), "marimo", 40)
	var ke *kube.Error
	if errors.As(err, &ke) {
		log = ""
	} else if err != nil {
		return "", err
	}
	if nrNoMarimo.MatchString(log) {
		name := "?"
		var envName string
		err := n.db.QueryRow(ctx, `SELECT e.name FROM notebooks n JOIN script_environments e ON e.id = n.environment_id
			WHERE n.id = $1`, row.NotebookID).Scan(&envName)
		if err == nil {
			name = envName
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return "", err
		}
		return nrNoMarimoText(name), nil
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

// refresh is notebook_runtime.refresh (forced, as the reaper asks): a starting session
// ready when its pod answers, failed (saying why) when it cannot start.
func (n *notebookReaper) refresh(ctx context.Context, row *nrSession) error {
	if row.Status != "starting" {
		return nil
	}
	err := n.refreshStep(ctx, row)
	var ke *kube.Error
	if errors.As(err, &ke) {
		if ke.Status == 0 || ke.Status == 429 || ke.Status >= 500 {
			n.log.Warn(fmt.Sprintf("notebooks: session %d not checked (%s)", row.ID, ke.Message))
			return nil
		}
		return n.fail(ctx, row, ke.Message)
	}
	return err
}

func (n *notebookReaper) refreshStep(ctx context.Context, row *nrSession) error {
	k := n.kubeAPI()
	job, err := kube.GetJob(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	if job == nil {
		if nrAge(row.StartedAt, jobs.NaiveNow()) > nrNoJobSeconds {
			return n.fail(ctx, row, "its Job is not in the cluster (the gateway that started it stopped)")
		}
		return nil
	}
	if kube.JobOutcome(job) != "" {
		why := kube.JobFailure(job)
		if why == "" {
			why = "its Job ended before the notebook answered"
		}
		return n.fail(ctx, row, why)
	}
	pods, err := kube.JobPods(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	pod := nrLatest(pods)
	if pod != nil && nrReady(pod) {
		ip := pyjson.Str(kube.Dict(kube.Dict(pod).Value("status")).Value("podIP"))
		_, err := n.db.Exec(ctx, `UPDATE notebook_sessions SET status = 'ready', pod_ip = $2, ready_at = $3, error = NULL WHERE id = $1`,
			row.ID, ip, jobs.NaiveNow())
		if err == nil {
			row.Status, row.PodIP = "ready", &ip
		}
		return err
	}
	if pod != nil {
		ended, err := n.ended(ctx, row, pod)
		if err != nil {
			return err
		}
		if ended != "" {
			return n.fail(ctx, row, ended)
		}
	}
	cannot, unscheduled, _, err := kube.StartState(ctx, k, row.Namespace, row.JobName)
	if err != nil {
		return err
	}
	if cannot == "" && !kube.HasPod(job) && nrAge(row.StartedAt, jobs.NaiveNow()) > 10 {
		if cannot, err = kube.Refused(ctx, k, row.Namespace, row.JobName); err != nil {
			return err
		}
	}
	if cannot != "" {
		return n.fail(ctx, row, cannot)
	}
	timeout := n.cfg.NotebooksStartTimeout
	if nrAge(row.StartedAt, jobs.NaiveNow()) > float64(timeout) {
		why := ""
		if unscheduled != "" {
			why = ": no node could take it (" + unscheduled + ")"
		}
		return n.fail(ctx, row, fmt.Sprintf("it did not start within %d s%s", timeout, why))
	}
	return nil
}

// fail is notebook_runtime._fail: the row stays (the page shows why) until it is tried again
// or the reaper clears it; its Job goes now.
func (n *notebookReaper) fail(ctx context.Context, row *nrSession, why string) error {
	if _, err := n.db.Exec(ctx, `UPDATE notebook_sessions SET status = 'failed' WHERE id = $1 AND status <> 'failed'`, row.ID); err != nil {
		return err
	}
	message := pystr.Cut("The notebook could not start: "+why, 4000)
	if _, err := n.db.Exec(ctx, `UPDATE notebook_sessions SET status = 'failed', error = $2, pod_ip = NULL WHERE id = $1`,
		row.ID, message); err != nil {
		return err
	}
	row.Status, row.PodIP = "failed", nil
	n.log.Info(fmt.Sprintf("notebooks: session %d of notebook %d failed: %s", row.ID, row.NotebookID, pystr.Cut(why, 300)))
	err := kube.DeleteJob(ctx, n.kubeAPI(), row.Namespace, row.JobName)
	var ke *kube.Error
	if errors.As(err, &ke) {
		return nil
	}
	return err
}

// alive is notebook_runtime.alive: a ready session's marimo still runs; when it does not,
// the session is stopped (an editor's file kept as the draft first).
func (n *notebookReaper) alive(ctx context.Context, row *nrSession) (bool, error) {
	k := n.kubeAPI()
	var ke *kube.Error
	job, err := kube.GetJob(ctx, k, row.Namespace, row.JobName)
	if errors.As(err, &ke) {
		return true, nil // the API not answering says nothing about the pod
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
		pod = nrLatest(pods)
	}
	if job != nil && kube.JobOutcome(job) == "" && pod != nil {
		ended, err := n.ended(ctx, row, pod)
		if err != nil {
			return false, err
		}
		if ended == "" {
			return true, nil
		}
	}
	return false, n.stop(ctx, row, "stopped", false)
}

// readFile is notebook_runtime.read_file: what marimo last saved in the editor's pod; false
// when it cannot be read.
func (n *notebookReaper) readFile(ctx context.Context, row *nrSession) (string, bool) {
	if row.Mode != "edit" || row.PodIP == nil || *row.PodIP == "" {
		return "", false
	}
	// plain HTTP to the pod's address, never through the environment's proxy
	client := &http.Client{Timeout: nrFileTimeout, Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{}).DialContext}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("http://%s:%d/notebook.py", *row.PodIP, nrFilesPort), nil)
	if err != nil {
		n.log.Info(fmt.Sprintf("notebooks: the editor's file of session %d not read (%s)", row.ID, "UnsupportedProtocol"))
		return "", false
	}
	res, err := client.Do(req)
	if err != nil {
		n.log.Info(fmt.Sprintf("notebooks: the editor's file of session %d not read (%v)", row.ID, err))
		return "", false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return "", false
	}
	data, err := io.ReadAll(io.LimitReader(res.Body, nrMaxFileBytes+1))
	if err != nil {
		n.log.Info(fmt.Sprintf("notebooks: the editor's file of session %d not read (%v)", row.ID, err))
		return "", false
	}
	if len(data) > nrMaxFileBytes {
		n.log.Warn(fmt.Sprintf("notebooks: the editor's file of session %d is too large", row.ID))
		return "", false
	}
	return nrDecode(data), true
}

// nrDecode is bytes.decode("utf-8", errors="replace").
func nrDecode(data []byte) string {
	if utf8.Valid(data) {
		return string(data)
	}
	var b strings.Builder
	for len(data) > 0 {
		r, size := utf8.DecodeRune(data)
		if r == utf8.RuneError && size == 1 {
			b.WriteRune('�')
		} else {
			b.Write(data[:size])
		}
		data = data[size:]
	}
	return b.String()
}

// syncDraft is notebook_runtime.sync_draft: the editor's file becomes the notebook's draft
// when it differs from the current version (none when it is the same).
func (n *notebookReaper) syncDraft(ctx context.Context, row *nrSession) (string, error) {
	source, read := n.readFile(ctx, row)
	nb, err := nbGet(ctx, n.db, row.NotebookID, false)
	if err != nil || !read || nb == nil {
		return source, err
	}
	content, err := nbContentOf(ctx, n.db, nb, 0)
	if err != nil {
		return source, err
	}
	var draft *string // None when the file is the current version
	if cur, isText := content.Value("source").(string); !isText || cur != source {
		draft = &source
	}
	if draft != nil && len(*draft) > nbMaxSourceBytes {
		n.log.Warn(fmt.Sprintf("notebooks: the editor's file of notebook %d is over the size of a version: not kept", nb.ID))
		return source, nil
	}
	same := (draft == nil && nb.Draft == nil) || (draft != nil && nb.Draft != nil && *draft == *nb.Draft)
	if !same {
		var by *int64
		var at *time.Time
		if draft != nil {
			now := jobs.Now()
			by, at = &row.UserID, &now
		}
		if _, err := n.db.Exec(ctx, `UPDATE notebooks SET draft = $2, draft_by = $3, draft_at = $4 WHERE id = $1`, nb.ID, draft, by, at); err != nil {
			return source, err
		}
	}
	return source, nil
}

// stop is notebook_runtime.stop (by the reaper: no person): the pod stopped and the session
// forgotten; an editor's file becomes the draft first.
func (n *notebookReaper) stop(ctx context.Context, row *nrSession, reason string, quiet bool) error {
	if row.Mode == "edit" && row.Status == "ready" {
		if _, err := n.syncDraft(ctx, row); err != nil {
			return err
		}
	}
	var ke *kube.Error
	if err := kube.DeleteJob(ctx, n.kubeAPI(), row.Namespace, row.JobName); errors.As(err, &ke) {
		// the reaper deletes the Job without a session later
		n.log.Warn(fmt.Sprintf("notebooks: the Job %s not deleted now (%s)", row.JobName, ke.Message))
	} else if err != nil {
		return err
	}
	nb, err := nbGet(ctx, n.db, row.NotebookID, false)
	if err != nil {
		return err
	}
	was := row.Status
	if _, err := n.db.Exec(ctx, `DELETE FROM notebook_sessions WHERE id = $1`, row.ID); err != nil {
		return err
	}
	if quiet || was == "failed" || nb == nil {
		return nil
	}
	audit.Record(ctx, n.db, n.log, audit.Event{ActorLabel: nrOrchestrator, Action: "notebook.stop", TargetType: scPtrStr("notebook"),
		TargetID: &nb.ID, TargetLabel: &nb.Name, Detail: pyjson.Object{{Key: "mode", Value: row.Mode}, {Key: "reason", Value: reason}}})
	return nil
}

// loop is reaper_loop.
func (n *notebookReaper) loop(ctx context.Context) {
	if n.s.DisabledReason() != "" {
		return
	}
	if n.lineage {
		n.log.Info("notebooks: OpenLineage is on, the notebooks' pods are left to the Python gateway's orchestrator")
		return
	}
	for ctx.Err() == nil {
		if err := n.oneRound(ctx); err != nil && !errors.Is(err, context.Canceled) {
			// database or cluster down: at the next round
			n.log.Warn(fmt.Sprintf("notebooks: the pods not checked (%v), again in %ds", err, int(nrReapEvery/time.Second)))
		}
		if !jobs.Sleep(ctx, nrReapEvery) {
			break
		}
	}
}
