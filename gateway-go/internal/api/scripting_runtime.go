package api

// routes/scripting.py: the package search (services/script_packages.py, internal/pypkg), the
// editor's check of a Python file (Ruff and Python's ast, asked of the AI service: the Python
// part of script_lint), the outputs of a flow's Script and Container nodes
// (script_runs.latest_outputs) and the editor's trial of one of them (scripting_trial.go).

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aisvc"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowaccess"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pypkg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

func init() {
	register("scripting_runtime", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/admin/scripting/packages", Handler: superuser(d, "/admin/scripting/packages", searchPackages(d))},
			{Method: http.MethodGet, Pattern: "/admin/scripting/packages/{language}/{name}",
				Handler: superuser(d, "/admin/scripting/packages/{language}/{name}", packageVersions(d))},
			{Method: http.MethodPost, Pattern: "/scripting/lint", Handler: withBody(server.Authed(d.Auth, "/scripting/lint", editorLint(d)))},
			{Method: http.MethodGet, Pattern: "/scripting/flows/{flow_id}/outputs",
				Handler: server.Authed(d.Auth, "/scripting/flows/{flow_id}/outputs", nodeOutputs(d))},
		}
	})
}

func pkgSettings() pypkg.Settings {
	s := scriptcfg.Get().Scripts
	return pypkg.Settings{PyPIIndexURL: s.PyPIIndexURL, CRANURL: s.CRANURL}
}

// pkgLanguage is _language: python or r, the 422 otherwise.
func pkgLanguage(w http.ResponseWriter, language string) bool {
	if language != "python" && language != "r" {
		detail(w, http.StatusUnprocessableEntity, "The language is python or r")
		return false
	}
	return true
}

// pkgIndexFailed answers a PackageIndexError (a 502), or an unexpected error.
func pkgIndexFailed(d *Deps, w http.ResponseWriter, err error) {
	var ie *pypkg.IndexError
	if errors.As(err, &ie) {
		detail(w, http.StatusBadGateway, "The package index could not be read: "+ie.Message)
		return
	}
	fail(d, w, "scripting: the package index", err)
}

// searchPackages is GET /admin/scripting/packages (search_packages).
func searchPackages(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		q := pyd.ParseQuery(r.URL.RawQuery)
		language, _ := ps.Query(q, "language", pyd.Str()).(string)
		text, _ := ps.Query(q, "q", pyd.Str(), "").(string)
		if !validated(w, &ps) || !pkgLanguage(w, language) {
			return
		}
		hits, err := pypkg.Search(r.Context(), pkgSettings(), language, runeClip(text, 200))
		if err != nil {
			pkgIndexFailed(d, w, err)
			return
		}
		out := []any{}
		for _, h := range hits {
			var v any
			if h.Version != nil {
				v = *h.Version
			}
			out = append(out, pyjson.Object{{Key: "name", Value: h.Name}, {Key: "version", Value: v}})
		}
		writePy(w, out)
	}
}

// packageName is _PACKAGE_NAME (re.match: `$` also before a final line feed).
var packageName = map[string]*regexp.Regexp{
	"python": regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\n?$`),
	"r":      regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]{0,199}\n?$`),
}

// packageVersions is GET /admin/scripting/packages/{language}/{name} (package_versions).
func packageVersions(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		language, _ := ps.Path("language", r.PathValue("language"), pyd.Str()).(string)
		name, _ := ps.Path("name", r.PathValue("name"), pyd.Str()).(string)
		python, _ := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "python", pyd.Optional(pyd.Str()), nil).(string)
		if !validated(w, &ps) || !pkgLanguage(w, language) {
			return
		}
		if !packageName[language].MatchString(name) {
			detail(w, http.StatusUnprocessableEntity, "Not a package name")
			return
		}
		var versions []pypkg.VersionOut
		var err error
		if language == "python" {
			versions, err = pypkg.PythonVersions(r.Context(), pkgSettings(), name, python)
		} else {
			versions, err = pypkg.RVersions(r.Context(), pkgSettings(), name)
		}
		if err != nil {
			pkgIndexFailed(d, w, err)
			return
		}
		if len(versions) == 0 {
			detail(w, http.StatusNotFound, fmt.Sprintf("No package «%s» in the index", name))
			return
		}
		list := []any{}
		for _, v := range versions {
			var wheel any
			if v.Wheel != nil {
				wheel = *v.Wheel
			}
			list = append(list, pyjson.Object{{Key: "version", Value: v.Version}, {Key: "pre", Value: v.Pre}, {Key: "wheel", Value: wheel}})
		}
		writePy(w, pyjson.Object{{Key: "name", Value: name}, {Key: "versions", Value: list}})
	}
}

// ── the editor's check of a Python file ─────────────────────────────────────

// lintRate is script_lint.allowed: at most RATE checks per person per second, in this process.
var lintRate = struct {
	sync.Mutex
	recent map[int64][]time.Time
}{recent: map[int64][]time.Time{}}

func lintAllowed(userID int64) bool {
	now := time.Now()
	lintRate.Lock()
	defer lintRate.Unlock()
	seen := lintRate.recent[userID]
	for len(seen) > 0 && now.Sub(seen[0]) > time.Second {
		seen = seen[1:]
	}
	if len(seen) >= 2 {
		lintRate.recent[userID] = seen
		return false
	}
	lintRate.recent[userID] = append(seen, now)
	return true
}

// lintNodeModules is script_lint.node_modules.
func lintNodeModules(paths []string) []string {
	set := map[string]bool{}
	var out []string
	for _, p := range paths {
		first, _, _ := strings.Cut(p, "/")
		first = strings.TrimSuffix(first, ".py")
		if !set[first] {
			set[first] = true
			out = append(out, first)
		}
	}
	return out
}

// lintInstalled is `set(json.loads(current.completions))`: the names (texts) and whether the set
// is empty; an error where Python's set() raises (a TypeError: a 500).
func lintInstalled(completions *string) ([]string, bool, error) {
	if completions == nil || *completions == "" {
		return nil, false, nil
	}
	v, err := pyjson.Loads(*completions)
	if err != nil {
		return nil, false, nil // ValueError: nothing installed known
	}
	var items []any
	switch x := v.(type) {
	case []any:
		items = x
	case pyjson.Object:
		for _, kv := range x {
			items = append(items, kv.Key)
		}
	case string:
		for _, c := range x {
			items = append(items, string(c))
		}
	default:
		return nil, false, errors.New("scripting: completions are not iterable")
	}
	var names []string
	for _, it := range items {
		switch s := it.(type) {
		case string:
			names = append(names, s)
		case []any, pyjson.Object:
			return nil, false, errors.New("scripting: unhashable completions")
		}
	}
	return names, len(items) > 0, nil
}

// editorLint is POST /scripting/lint (editor_check): what Ruff finds in one Python file of a
// Script node, and the imports the environment does not have.
func editorLint(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.LintIn)
		if !validated(w, &ps) {
			return
		}
		ctx := r.Context()
		envID := *optInt(body, "environment_id")
		var name, language string
		var current *int64
		err := d.DB.QueryRow(ctx, `SELECT name, language, current_version_id FROM script_environments WHERE id = $1`, envID).
			Scan(&name, &language, &current)
		if errors.Is(err, pgx.ErrNoRows) {
			detail(w, http.StatusNotFound, "Scripting environment not found")
			return
		}
		if err != nil {
			fail(d, w, "scripting: the environment", err)
			return
		}
		ok, err := scriptnodes.CanUse(ctx, perm.For(d.DB, &p.User), d.DB, p.User.ID, envID)
		if err != nil {
			fail(d, w, "scripting: who may use it", err)
			return
		}
		if !ok {
			detail(w, http.StatusForbidden, "You cannot write scripts for this environment")
			return
		}
		path := body.Get("path").(string)
		content := body.Get("content").(string)
		if len(content) > scriptnodes.MaxFileBytes {
			detail(w, http.StatusRequestEntityTooLarge, fmt.Sprintf("A file holds at most %d KB", scriptnodes.MaxFileBytes/1024))
			return
		}
		empty := pyjson.Object{{Key: "diagnostics", Value: []any{}}, {Key: "skipped", Value: false}}
		if language != "python" || !scriptcfg.Get().Scripts.Lint || !strings.HasSuffix(path, ".py") {
			writePy(w, empty)
			return
		}
		if !lintAllowed(p.User.ID) {
			writePy(w, pyjson.Object{{Key: "diagnostics", Value: []any{}}, {Key: "skipped", Value: true}})
			return
		}
		in := aisvc.LintIn{Path: path, Content: content, Environment: name}
		if current != nil {
			var lv string
			var completions *string
			err := d.DB.QueryRow(ctx, `SELECT language_version, completions FROM script_environment_versions WHERE id = $1`, *current).
				Scan(&lv, &completions)
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				fail(d, w, "scripting: the environment's version", err)
				return
			}
			if err == nil {
				in.LanguageVersion = &lv
				names, any, err := lintInstalled(completions)
				if err != nil {
					fail(d, w, "scripting: the installed modules", err)
					return
				}
				in.Installed, in.CheckImports = names, any
			}
		}
		paths := []string{path}
		for _, f := range body.Get("files").([]any) {
			paths = append(paths, f.(string))
		}
		in.Local = lintNodeModules(paths)
		found, err := d.SQL.Lint(ctx, in)
		if err != nil {
			// Ruff not reachable: nothing shown, as when the binary cannot run (lint_python)
			d.Log.Warn("scripting: the AI service did not check the file", "path", path, "err", err)
			writePy(w, empty)
			return
		}
		list := []any{}
		for _, x := range found {
			list = append(list, pyjson.Object{{Key: "line", Value: x.Line}, {Key: "column", Value: x.Column},
				{Key: "end_line", Value: x.EndLine}, {Key: "end_column", Value: x.EndColumn}, {Key: "code", Value: x.Code},
				{Key: "message", Value: x.Message}, {Key: "severity", Value: x.Severity}})
		}
		writePy(w, pyjson.Object{{Key: "diagnostics", Value: list}, {Key: "skipped", Value: false}})
	}
}

// ── the outputs of a flow's Script and Container nodes ──────────────────────

// naiveIso is jsonable_encoder of a naive datetime: isoformat(), no offset.
func naiveIso(t *time.Time) any {
	if t == nil {
		return nil
	}
	if t.Nanosecond()/1000 != 0 {
		return t.Format("2006-01-02T15:04:05.000000")
	}
	return t.Format("2006-01-02T15:04:05")
}

// jsonKey is the text pydantic's JSON mode writes for a dict's key (the route returns a dict):
// None is «None», the others as json.dumps writes them.
func jsonKey(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return "None"
	}
	out, _ := pyjson.Dumps(v, pyjson.Options{})
	return out
}

type stepRun struct {
	id                   int64
	status               string
	script, outputBucket *string
	outputKey            *string
	errText              *string
	started              time.Time
	finished             *time.Time
	rows                 *int64
}

// runsOfNode is script_runs._runs_of_node: the runs of one node of a flow, newest first (the
// `script` column starts with the node's id).
func runsOfNode(ctx context.Context, q perm.Querier, flowID int64, nodeID any, succeeded bool) (*stepRun, error) {
	key, _ := pyjson.Dumps(pyjson.Object{{Key: "node_id", Value: nodeID}}, pyjson.Options{EnsureASCII: true})
	prefix := key[:len(key)-1] + ","
	extra := ""
	if succeeded {
		extra = ` AND status = 'SUCCESS' AND output_key != ''`
	}
	var r stepRun
	err := q.QueryRow(ctx, `SELECT id, status, script, output_bucket, output_key, error, started_at, finished_at, rows_written
		FROM runs WHERE flow_id = $1 AND kind IN ('script', 'container') AND starts_with(script, $2)`+extra+`
		ORDER BY started_at DESC, id DESC LIMIT 1`, flowID, prefix).
		Scan(&r.id, &r.status, &r.script, &r.outputBucket, &r.outputKey, &r.errText, &r.started, &r.finished, &r.rows)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

func str0(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

type latest struct {
	last, output pyjson.Object // nil = None
	has          bool
}

// addToLatest is script_runs._add_to_latest.
func addToLatest(e *latest, run *stepRun) error {
	text := str0(run.script)
	if text == "" {
		text = "{}"
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return nil
	}
	info, ok := v.(pyjson.Object)
	if !ok {
		return errors.New("scripting: a run's script is not a dict")
	}
	e.has = true
	if e.last == nil {
		e.last = pyjson.Object{{Key: "run_id", Value: run.id}, {Key: "status", Value: run.status},
			{Key: "trial", Value: pyTruthy(info.Value("trial"))}, {Key: "error", Value: run.errText},
			{Key: "started_at", Value: naiveIso(&run.started)}, {Key: "finished_at", Value: naiveIso(run.finished)},
			{Key: "attempts", Value: info.Value("attempts")}, {Key: "exit_code", Value: info.Value("exit_code")},
			{Key: "log", Value: pyjson.Or(info.Value("log"), "")}, {Key: "log_expired", Value: pyTruthy(info.Value("log_expired"))},
			{Key: "fingerprint", Value: info.Value("fingerprint")}}
	}
	if e.output == nil && run.status == "SUCCESS" && str0(run.outputKey) != "" {
		e.output = pyjson.Object{{Key: "run_id", Value: run.id}, {Key: "bucket", Value: run.outputBucket},
			{Key: "key", Value: run.outputKey}, {Key: "rows", Value: run.rows}, {Key: "trial", Value: pyTruthy(info.Value("trial"))},
			{Key: "finished_at", Value: naiveIso(run.finished)}, {Key: "fingerprint", Value: info.Value("fingerprint")}}
	}
	return nil
}

// savedNode is one entry of the route's `saved`: the node's id (any JSON value) and its
// fingerprint, in the dict's order.
type savedNode struct {
	id          any
	fingerprint string
}

// latestOutputs is script_runs.latest_outputs for the nodes given, in their order.
func latestOutputs(ctx context.Context, q perm.Querier, flowID int64, nodes []savedNode) (map[string]*latest, error) {
	out := map[string]*latest{}
	for _, n := range nodes {
		last, err := runsOfNode(ctx, q, flowID, n.id, false)
		if err != nil {
			return nil, err
		}
		if last == nil {
			continue
		}
		ok := last
		if !(last.status == "SUCCESS" && str0(last.outputKey) != "") {
			if ok, err = runsOfNode(ctx, q, flowID, n.id, true); err != nil {
				return nil, err
			}
		}
		e := &latest{}
		if err := addToLatest(e, last); err != nil {
			return nil, err
		}
		if ok != nil && ok.id != last.id {
			if err := addToLatest(e, ok); err != nil {
				return nil, err
			}
		}
		if e.has {
			out[jsonKey(n.id)] = e
		}
	}
	return out, nil
}

// nodeFingerprint is script_nodes.fingerprint: code_fingerprint, with the version a saved
// script is at.
func nodeFingerprint(ctx context.Context, q perm.Querier, node pyjson.Object) (string, error) {
	sid, ok := flowaccess.SavedScriptID(node)
	if !ok {
		return flowaccess.CodeFingerprint(node), nil
	}
	var version int64
	err := q.QueryRow(ctx, `SELECT current_version FROM saved_scripts WHERE id = $1`, sid).Scan(&version)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return "", err
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s:v%d", flowaccess.CodeFingerprint(node), version)))
	return hex.EncodeToString(sum[:]), nil
}

// nodeOutputs is GET /scripting/flows/{flow_id}/outputs (node_outputs).
func nodeOutputs(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ids, ok := pathInts(w, r, "flow_id")
		if !ok {
			return
		}
		q := d.req(w, r, p)
		f := q.flow(ids[0])
		if f == nil || !q.ensure(f.ProjectID, perm.View) {
			return
		}
		if q.refused(q.acc.EnsureCanOpen(q.ctx, f.Definition)) {
			return
		}
		var saved []savedNode
		index := map[string]int{}
		put := func(id any, fp string) {
			k := jsonKey(id)
			if i, ok := index[k]; ok {
				saved[i].fingerprint = fp
				return
			}
			index[k] = len(saved)
			saved = append(saved, savedNode{id, fp})
		}
		for _, n := range flowaccess.ScriptNodes(f.Definition) {
			fp, err := nodeFingerprint(q.ctx, d.DB, n)
			if err != nil {
				q.fail(err)
				return
			}
			put(n.Value("id"), fp)
		}
		for _, n := range flowaccess.ContainerNodes(f.Definition) {
			put(n.Value("id"), flowaccess.ContainerFingerprint(n))
		}
		found, err := latestOutputs(q.ctx, d.DB, f.ID, saved)
		if err != nil {
			q.fail(err)
			return
		}
		out := pyjson.Object{}
		for _, n := range saved {
			e := found[jsonKey(n.id)]
			if e == nil {
				continue
			}
			stale := func(part pyjson.Object) any {
				if part == nil {
					return nil
				}
				var fp any
				rest := pyjson.Object{}
				for _, kv := range part {
					if kv.Key == "fingerprint" {
						fp = kv.Value
						continue
					}
					rest = append(rest, kv)
				}
				s, isStr := fp.(string)
				return append(rest, pyjson.KV{Key: "stale", Value: !(isStr && s == n.fingerprint)})
			}
			out = append(out, pyjson.KV{Key: jsonKey(n.id), Value: pyjson.Object{{Key: "last", Value: stale(e.last)},
				{Key: "output", Value: stale(e.output)}}})
		}
		writePy(w, out)
	}
}
