// Package savedscripts is services/saved_scripts.py: who sees a saved script (VIEW on its
// folder and the use of its environment), what its versions may hold, and what uses it.
package savedscripts

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

const (
	MaxInputs = 20
	NameMax   = 200
)

// alias is ALIAS; Python's `$` also matches before a final newline.
var alias = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,62}\n?$`)

// Script is a row of saved_scripts.
type Script struct {
	ID             int64
	Name           string
	Description    string
	ProjectID      int64
	EnvironmentID  int64
	CurrentVersion int64
	RunSchedule    *string
	RunScheduledBy *int64
	NextRunAt      *time.Time
	CreatedAt      time.Time
	UpdatedAt      time.Time
}

// Columns of saved_scripts read into a Script.
const Columns = `id, name, description, project_id, environment_id, current_version, run_schedule, run_scheduled_by,
	next_run_at, created_at, updated_at`

// Scan reads Columns.
func Scan(row pgx.Row) (*Script, error) {
	var s Script
	if err := row.Scan(&s.ID, &s.Name, &s.Description, &s.ProjectID, &s.EnvironmentID, &s.CurrentVersion, &s.RunSchedule,
		&s.RunScheduledBy, &s.NextRunAt, &s.CreatedAt, &s.UpdatedAt); err != nil {
		return nil, err
	}
	return &s, nil
}

// Get is session.get(SavedScript, id).
func Get(ctx context.Context, q perm.Querier, id int64) (*Script, error) {
	s, err := Scan(q.QueryRow(ctx, `SELECT `+Columns+` FROM saved_scripts WHERE id = $1`, id))
	if err == pgx.ErrNoRows {
		return nil, nil
	}
	return s, err
}

// UsableEnvironments is usable_environment_ids: nil for an administrator (all of them).
func UsableEnvironments(ctx context.Context, c *perm.Checker, q perm.Querier, userID int64) (map[int64]bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return nil, err
	}
	rows, err := q.Query(ctx, `SELECT eg.environment_id FROM script_environment_groups eg
		JOIN user_groups ug ON ug.group_id = eg.group_id WHERE ug.user_id = $1`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]bool{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// CanSee is can_see (notebooks.can_see alike): VIEW on the folder and the environment's use.
func CanSee(ctx context.Context, c *perm.Checker, q perm.Querier, userID, projectID, envID int64) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	envs, err := UsableEnvironments(ctx, c, q, userID)
	if err != nil || !envs[envID] {
		return false, err
	}
	return c.Has(ctx, projectID, perm.View)
}

// GetVisible is get_visible.
func GetVisible(ctx context.Context, c *perm.Checker, q perm.Querier, userID, id int64) (*Script, *auth.Refusal, error) {
	s, err := Get(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	if s != nil {
		ok, err := CanSee(ctx, c, q, userID, s.ProjectID, s.EnvironmentID)
		if err != nil {
			return nil, nil, err
		}
		if ok {
			return s, nil, nil
		}
	}
	return nil, &auth.Refusal{Status: http.StatusNotFound, Detail: "Script not found"}, nil
}

// VisibleFilter is visible_query's WHERE (notebooks' alike): ok false when there are none;
// the clause and its arguments otherwise ("" for an administrator without folders).
func VisibleFilter(ctx context.Context, c *perm.Checker, q perm.Querier, userID int64, projects []int64) (string, []any, bool, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return "", nil, false, err
	}
	if admin {
		if projects != nil {
			return ` WHERE project_id = ANY($1)`, []any{projects}, true, nil
		}
		return "", nil, true, nil
	}
	envs, err := UsableEnvironments(ctx, c, q, userID)
	if err != nil {
		return "", nil, false, err
	}
	readable, err := c.Readable(ctx)
	if err != nil {
		return "", nil, false, err
	}
	if projects != nil {
		only := map[int64]bool{}
		for _, p := range projects {
			if readable[p] {
				only[p] = true
			}
		}
		readable = only
	}
	if len(envs) == 0 || len(readable) == 0 {
		return "", nil, false, nil
	}
	return ` WHERE environment_id = ANY($1) AND project_id = ANY($2)`, []any{perm.Sorted(envs), perm.Sorted(readable)}, true, nil
}

// Visible is visible_query run, by name.
func Visible(ctx context.Context, c *perm.Checker, q perm.Querier, userID int64, projects []int64) ([]Script, error) {
	where, args, ok, err := VisibleFilter(ctx, c, q, userID, projects)
	if err != nil || !ok {
		return nil, err
	}
	rows, err := q.Query(ctx, `SELECT `+Columns+` FROM saved_scripts`+where+` ORDER BY name`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Script
	for rows.Next() {
		s, err := Scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *s)
	}
	return out, rows.Err()
}

// EmptyContent is empty_content.
func EmptyContent() pyjson.Object {
	return pyjson.Object{{Key: "files", Value: []any{}}, {Key: "entrypoint", Value: ""}, {Key: "inputs", Value: []any{}},
		{Key: "output", Value: pyjson.Object{{Key: "publish", Value: false}, {Key: "name", Value: ""}, {Key: "project_id", Value: nil},
			{Key: "overwrite", Value: true}}},
		{Key: "timeout_seconds", Value: int64(900)}, {Key: "retries", Value: int64(0)}, {Key: "retry_delay_seconds", Value: int64(60)}}
}

// VersionRow is version_row of a versions table: `version` 0 is the current one (Python's
// `version or current`). Whether it exists, and its content.
func VersionRow(ctx context.Context, q perm.Querier, table, fk string, id, current, version int64) (bool, *string, error) {
	if version == 0 {
		version = current
	}
	var content string
	err := q.QueryRow(ctx, fmt.Sprintf(`SELECT content FROM %s WHERE %s = $1 AND version = $2`, table, fk), id, version).Scan(&content)
	if err == pgx.ErrNoRows {
		return false, nil, nil
	}
	if err != nil {
		return false, nil, err
	}
	return true, &content, nil
}

// errNotAnObject: `{**empty, **json.loads(…)}` of a JSON that is no object (a TypeError, a 500).
var errNotAnObject = fmt.Errorf("a version's content is not a JSON object")

// Over is `{**empty, **json.loads(content)}`, the empty one when there is no row or the
// JSON does not read.
func Over(empty pyjson.Object, content *string) (pyjson.Object, error) {
	if content == nil {
		return empty, nil
	}
	v, err := pyjson.Loads(*content)
	if err != nil {
		return empty, nil
	}
	obj, ok := v.(pyjson.Object)
	if !ok {
		return nil, errNotAnObject
	}
	out := empty
	for _, kv := range obj {
		out = out.Set(kv.Key, kv.Value)
	}
	return out, nil
}

// ContentOf is content_of (`version` 0: the current one).
func ContentOf(ctx context.Context, q perm.Querier, s *Script, version int64) (pyjson.Object, error) {
	_, raw, err := VersionRow(ctx, q, "saved_script_versions", "script_id", s.ID, s.CurrentVersion, version)
	if err != nil {
		return nil, err
	}
	return Over(EmptyContent(), raw)
}

// AddVersion is add_version of a versioned object (saved scripts, notebooks): the row
// locked, the next number, the content as json.dumps writes it. Returns the number; the
// caller sets the object's current_version.
func AddVersion(ctx context.Context, tx pgx.Tx, table, versions, fk string, id int64, content pyjson.Object, note string, userID int64) (int64, error) {
	if _, err := tx.Exec(ctx, fmt.Sprintf(`SELECT id FROM %s WHERE id = $1 FOR UPDATE`, table), id); err != nil {
		return 0, err
	}
	var last int64
	err := tx.QueryRow(ctx, fmt.Sprintf(`SELECT version FROM %s WHERE %s = $1 ORDER BY version DESC`, versions, fk), id).Scan(&last)
	if err != nil && err != pgx.ErrNoRows {
		return 0, err
	}
	text, err := pyjson.Dumps(content, pyjson.Options{EnsureASCII: true})
	if err != nil {
		return 0, err
	}
	if _, err := tx.Exec(ctx, fmt.Sprintf(`INSERT INTO %s (%s, version, content, note, created_at, created_by)
		VALUES ($1, $2, $3, $4, $5, $6)`, versions, fk), id, last+1, text, note, time.Now().UTC(), userID); err != nil {
		return 0, err
	}
	return last + 1, nil
}

func get(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int64:
		return x != 0
	case float64:
		return x != 0
	case pyjson.Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}

func pyInt(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

// Environment is the part of a script_environments row the checks read.
type Environment struct {
	ID         int64
	Name       string
	Language   string
	MaxTimeout int64
}

func refuse(name, message string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("Script «%s»: %s", name, message)}
}

// CheckContent is check_content: the content as it will be saved, or the 422/403 saying
// what is wrong. The inputs and the publish folder new in this save are checked for the
// person saving.
func CheckContent(ctx context.Context, c *perm.Checker, q perm.Querier, name string, content any, env Environment,
	previous pyjson.Object) (pyjson.Object, *auth.Refusal, error) {
	in, ok := content.(pyjson.Object)
	if !ok {
		return nil, refuse(name, "its content is not an object"), nil
	}
	out := EmptyContent()
	for _, k := range []string{"files", "entrypoint", "timeout_seconds", "retries", "retry_delay_seconds"} {
		if v, has := in.Get(k); has {
			out = out.Set(k, v)
		}
	}
	files, filesOK := get(out, "files").([]any)
	_, entryOK := get(out, "entrypoint").(string)
	if !filesOK || !entryOK {
		return nil, refuse(name, "its files are a list and its entrypoint a file name"), nil
	}
	kept := []any{}
	for _, f := range files {
		o, isObj := f.(pyjson.Object)
		if !isObj {
			continue
		}
		path := ""
		if p := get(o, "path"); truthy(p) {
			path = pyjson.Str(p)
		}
		kept = append(kept, pyjson.Object{{Key: "path", Value: path}, {Key: "content", Value: get(o, "content")}})
	}
	out = out.Set("files", kept)
	var bucket string
	err := q.QueryRow(ctx, `SELECT bucket FROM script_storage WHERE id = 1`).Scan(&bucket)
	if err != nil && err != pgx.ErrNoRows {
		return nil, nil, err
	}
	node := scriptnodes.Node{{Key: "id", Value: name}, {Key: "data", Value: pyjson.Object{{Key: "label", Value: name},
		{Key: "files", Value: kept}, {Key: "entrypoint", Value: get(out, "entrypoint")},
		{Key: "timeoutSeconds", Value: get(out, "timeout_seconds")}, {Key: "retries", Value: get(out, "retries")},
		{Key: "retryDelaySeconds", Value: get(out, "retry_delay_seconds")}}}}
	if r := scriptnodes.CheckFiles(node, env.Language, pyd.Strip(bucket), false); r != nil {
		return nil, r, nil
	}
	if r := scriptnodes.CheckSettings(node, env.MaxTimeout); r != nil {
		return nil, r, nil
	}

	var before []any
	if prevInputs, _ := get(previous, "inputs").([]any); prevInputs != nil {
		for _, i := range prevInputs {
			if o, ok := i.(pyjson.Object); ok {
				before = append(before, get(o, "datasource_id"))
			}
		}
	}
	wasBefore := func(id int64) bool {
		for _, b := range before {
			if pyjson.Equal(b, id) {
				return true
			}
		}
		return false
	}
	inputsV := get(in, "inputs")
	if !truthy(inputsV) {
		inputsV = []any{}
	}
	inputs, isList := inputsV.([]any)
	if !isList || len(inputs) > MaxInputs {
		return nil, refuse(name, fmt.Sprintf("at most %d inputs", MaxInputs)), nil
	}
	seenAlias, seenDS := map[string]bool{}, map[int64]bool{}
	outInputs := []any{}
	for _, item := range inputs {
		o, isObj := item.(pyjson.Object)
		var dsID int64
		var idOK bool
		if isObj {
			dsID, idOK = pyInt(get(o, "datasource_id"))
		}
		if !isObj || !idOK {
			return nil, refuse(name, "an input names a datasource by its id"), nil
		}
		a := ""
		if v := get(o, "alias"); truthy(v) {
			a = pyjson.Str(v)
		}
		if !alias.MatchString(a) {
			return nil, refuse(name, fmt.Sprintf("«%s» is not an alias: capitals, digits and _, starting with a letter", a)), nil
		}
		if seenAlias[a] {
			return nil, refuse(name, fmt.Sprintf("two inputs are called %s", a)), nil
		}
		if seenDS[dsID] {
			return nil, refuse(name, "the same datasource is an input twice"), nil
		}
		seenAlias[a], seenDS[dsID] = true, true
		var project int64
		err := q.QueryRow(ctx, `SELECT project_id FROM datasources WHERE id = $1`, dsID).Scan(&project)
		if err == pgx.ErrNoRows {
			if wasBefore(dsID) {
				outInputs = append(outInputs, pyjson.Object{{Key: "datasource_id", Value: get(o, "datasource_id")}, {Key: "alias", Value: a}})
				continue
			}
			return nil, refuse(name, fmt.Sprintf("the datasource of %s does not exist", a)), nil
		}
		if err != nil {
			return nil, nil, err
		}
		if !wasBefore(dsID) {
			ok, err := c.Has(ctx, project, perm.View)
			if err != nil {
				return nil, nil, err
			}
			if !ok {
				return nil, &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("Script «%s»: you cannot read the datasource of %s", name, a)}, nil
			}
		}
		outInputs = append(outInputs, pyjson.Object{{Key: "datasource_id", Value: dsID}, {Key: "alias", Value: a}})
	}
	out = out.Set("inputs", outInputs)

	outputV := get(in, "output")
	if !truthy(outputV) {
		outputV = pyjson.Object{}
	}
	output, isObj := outputV.(pyjson.Object)
	if !isObj {
		return nil, refuse(name, "its output settings are an object"), nil
	}
	publish := truthy(get(output, "publish"))
	outName := ""
	if v := get(output, "name"); truthy(v) {
		outName = pyd.Strip(pyjson.Str(v))
	}
	target := get(output, "project_id")
	var targetOut any
	if _, isInt := pyInt(target); isInt {
		targetOut = target
	}
	short := []rune(outName)
	if len(short) > NameMax {
		short = short[:NameMax]
	}
	ow, isBool := get(output, "overwrite").(bool)
	out = out.Set("output", pyjson.Object{{Key: "publish", Value: publish}, {Key: "name", Value: string(short)},
		{Key: "project_id", Value: targetOut}, {Key: "overwrite", Value: !(isBool && !ow)}})
	if publish {
		if outName == "" {
			return nil, refuse(name, "the datasource it publishes needs a name"), nil
		}
		tid, isInt := pyInt(target)
		exists := false
		if isInt {
			var one int
			err := q.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, tid).Scan(&one)
			if err != nil && err != pgx.ErrNoRows {
				return nil, nil, err
			}
			exists = err == nil
		}
		if !exists {
			return nil, refuse(name, "choose the folder of the datasource it publishes"), nil
		}
		old, _ := get(previous, "output").(pyjson.Object)
		if !pyjson.Equal(target, get(old, "project_id")) || !truthy(get(old, "publish")) {
			ok, err := c.Has(ctx, tid, perm.Edit)
			if err != nil {
				return nil, nil, err
			}
			if !ok {
				return nil, &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("Script «%s»: you cannot publish into that folder", name)}, nil
			}
		}
	}
	return out, nil, nil
}

// Widens is widens: what a change makes the script reach that it did not.
func Widens(before, after pyjson.Object) []string {
	var reasons []string
	if !pyjson.Equal(get(before, "files"), get(after, "files")) || !pyjson.Equal(get(before, "entrypoint"), get(after, "entrypoint")) {
		reasons = append(reasons, "its code changed")
	}
	ids := func(c pyjson.Object) []any {
		var out []any
		list, _ := get(c, "inputs").([]any)
		for _, i := range list {
			if o, ok := i.(pyjson.Object); ok {
				out = append(out, get(o, "datasource_id"))
			}
		}
		return out
	}
	old := ids(before)
	for _, n := range ids(after) {
		found := false
		for _, o := range old {
			if pyjson.Equal(n, o) {
				found = true
			}
		}
		if !found {
			reasons = append(reasons, "it reads other datasources")
			break
		}
	}
	b, _ := get(before, "output").(pyjson.Object)
	a, _ := get(after, "output").(pyjson.Object)
	if truthy(get(a, "publish")) && (!truthy(get(b, "publish")) || !pyjson.Equal(get(a, "project_id"), get(b, "project_id"))) {
		reasons = append(reasons, "it publishes its output elsewhere")
	}
	return reasons
}

// Scheduled is a flow or a flow group that runs the script, with its schedule.
type Scheduled struct {
	ID             int64
	Name           string
	RunSchedule    *string
	RunScheduledBy *int64
}

// Using is using: (the flows, the flow groups) that run the script, in the database's order.
func Using(ctx context.Context, q perm.Querier, scriptID int64) ([]Scheduled, []Scheduled, error) {
	rows, err := q.Query(ctx, `SELECT id, name, definition, run_schedule, run_scheduled_by FROM flows
		WHERE definition LIKE '%' || '"scriptId"' || '%'`)
	if err != nil {
		return nil, nil, err
	}
	var flows []Scheduled
	for rows.Next() {
		var f Scheduled
		var def string
		if err := rows.Scan(&f.ID, &f.Name, &def, &f.RunSchedule, &f.RunScheduledBy); err != nil {
			rows.Close()
			return nil, nil, err
		}
		for _, n := range scriptnodes.ScriptNodes(def) {
			if id, ok := scriptnodes.SavedScriptID(n); ok && id == scriptID {
				flows = append(flows, f)
				break
			}
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	all, err := flowgroups.Containing(ctx, q, `"script"`)
	if err != nil {
		return nil, nil, err
	}
	var groups []Scheduled
	for _, g := range all {
	cells:
		for _, row := range flowgroups.DefinitionOf(g.Definition).Steps {
			for _, c := range row {
				if c != nil && c.Kind == "script" && c.ID == scriptID {
					groups = append(groups, Scheduled{g.ID, g.Name, g.RunSchedule, g.RunScheduledBy})
					break cells
				}
			}
		}
	}
	return flows, groups, nil
}

// References is references: the names of the flows and groups that use the script, sorted.
func References(ctx context.Context, q perm.Querier, scriptID int64) ([]string, []string, error) {
	flows, groups, err := Using(ctx, q, scriptID)
	if err != nil {
		return nil, nil, err
	}
	names := func(list []Scheduled) []string {
		out := []string{}
		for _, x := range list {
			out = append(out, x.Name)
		}
		sort.Strings(out)
		return out
	}
	return names(flows), names(groups), nil
}

// UsingEnvironment is scripts_using_environment (notebooks_using_environment with its
// table): the first ten names.
func UsingEnvironment(ctx context.Context, q perm.Querier, table string, envID int64) ([]string, error) {
	rows, err := q.Query(ctx, fmt.Sprintf(`SELECT name FROM %s WHERE environment_id = $1 ORDER BY name LIMIT 10`, table), envID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

var _ = strings.TrimSpace
