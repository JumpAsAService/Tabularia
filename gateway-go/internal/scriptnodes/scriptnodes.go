// Package scriptnodes is services/script_nodes.py: the Script nodes of a flow definition,
// who may use a scripting environment, and the checks of a script's files and settings.
package scriptnodes

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const (
	MaxFiles     = 50
	MaxFileBytes = 256 * 1024
	MaxCodeBytes = 768 * 1024
	MaxRetries   = 5
)

// pathRe is _PATH; Python's `$` also matches before a final newline.
var pathRe = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}){0,7}\n?$`)

// EntrypointSuffix is ENTRYPOINT_SUFFIX.
var EntrypointSuffix = map[string][]string{"python": {".py"}, "r": {".R", ".r"}}

// CanUse is can_use: an administrator, or someone in one of the environment's groups.
func CanUse(ctx context.Context, c *perm.Checker, q perm.Querier, userID, envID int64) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	var g int64
	err := q.QueryRow(ctx, `SELECT eg.group_id FROM script_environment_groups eg
		JOIN user_groups ug ON ug.group_id = eg.group_id WHERE eg.environment_id = $1 AND ug.user_id = $2`, envID, userID).Scan(&g)
	if err == pgx.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

// Node is a Script node: an Object with "id" and "data".
type Node = pyjson.Object

func data(node Node) pyjson.Object {
	d, _ := node.Get("data")
	o, _ := d.(pyjson.Object)
	return o
}

func get(o pyjson.Object, k string) any {
	v, _ := o.Get(k)
	return v
}

// Label is label.
func Label(node Node) string {
	if v := get(data(node), "label"); truthy(v) {
		return pyjson.Str(v)
	}
	if v := get(node, "id"); truthy(v) {
		return pyjson.Str(v)
	}
	return "Script"
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

func refuse(node Node, message string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: fmt.Sprintf("Script «%s»: %s", Label(node), message)}
}

// FilesOf is files_of.
func FilesOf(node Node) []pyjson.Object {
	var out []pyjson.Object
	files, _ := get(data(node), "files").([]any)
	for _, f := range files {
		if o, ok := f.(pyjson.Object); ok {
			out = append(out, o)
		}
	}
	return out
}

// CheckFiles is check_files: names that make a tree, sizes a ConfigMap takes, code that
// does not name the scripts bucket and, when `complete`, an entrypoint among them.
func CheckFiles(node Node, language, bucket string, complete bool) *auth.Refusal {
	files := FilesOf(node)
	if len(files) == 0 && !complete {
		return nil
	}
	if len(files) == 0 {
		return refuse(node, "it has no code")
	}
	if len(files) > MaxFiles {
		return refuse(node, fmt.Sprintf("at most %d files", MaxFiles))
	}
	seen := map[string]bool{}
	total := 0
	for _, f := range files {
		path := ""
		if p := get(f, "path"); truthy(p) {
			path = pyjson.Str(p)
		}
		content, ok := get(f, "content").(string)
		if !ok {
			return refuse(node, fmt.Sprintf("the file «%s» has no text", path))
		}
		dots := false
		for _, part := range strings.Split(path, "/") {
			if part == ".." {
				dots = true
			}
		}
		if !pathRe.MatchString(path) || dots {
			return refuse(node, fmt.Sprintf("«%s» is not a file name: letters, digits, . _ - and / between folders", path))
		}
		if seen[strings.ToLower(path)] {
			return refuse(node, fmt.Sprintf("two files are called «%s»", path))
		}
		seen[strings.ToLower(path)] = true
		size := len(content)
		if size > MaxFileBytes {
			return refuse(node, fmt.Sprintf("«%s» is larger than %d KB", path, MaxFileBytes/1024))
		}
		total += size
		if bucket != "" {
			if r := checkPaths(node, path, content, bucket); r != nil {
				return r
			}
		}
	}
	if total > MaxCodeBytes {
		return refuse(node, fmt.Sprintf("the code is larger than %d KB", MaxCodeBytes/1024))
	}
	entry := ""
	if e := get(data(node), "entrypoint"); truthy(e) {
		entry = pyjson.Str(e)
	}
	if !complete && entry == "" {
		return nil
	}
	found := false
	for _, f := range files {
		if pyjson.Str(get(f, "path")) == entry {
			found = true
		}
	}
	if !found {
		shown := entry
		if shown == "" {
			shown = "—"
		}
		return refuse(node, fmt.Sprintf("the file to run («%s») is not among its files", shown))
	}
	if suffixes, ok := EntrypointSuffix[language]; ok && language != "" {
		okSuffix := false
		for _, s := range suffixes {
			if strings.HasSuffix(entry, s) {
				okSuffix = true
			}
		}
		if !okSuffix {
			kind := "R"
			if language == "python" {
				kind = "Python"
			}
			return refuse(node, fmt.Sprintf("the file to run is a %s file (%s)", kind, strings.Join(suffixes, " or ")))
		}
	}
	return nil
}

// checkPaths is check_paths: the code may not name the scripts bucket.
func checkPaths(node Node, path, content, bucket string) *auth.Refusal {
	b := regexp.QuoteMeta(bucket)
	pattern := regexp.MustCompile(`(?i)(s3a?://|gs://|//)` + b + `\b|\b` + b + `/env-\d`)
	for n, line := range SplitLines(content) {
		if pattern.MatchString(line) {
			return refuse(node, fmt.Sprintf("%s, line %d names the scripts bucket: read the input from "+
				"TABULARIA_INPUT and write the output to TABULARIA_OUTPUT", path, n+1))
		}
	}
	return nil
}

// SplitLines is Python's str.splitlines().
func SplitLines(s string) []string {
	var out []string
	start := 0
	rs := []rune(s)
	for i := 0; i < len(rs); i++ {
		switch rs[i] {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', ' ', ' ':
			out = append(out, string(rs[start:i]))
			start = i + 1
		case '\r':
			out = append(out, string(rs[start:i]))
			if i+1 < len(rs) && rs[i+1] == '\n' {
				i++
			}
			start = i + 1
		}
	}
	if start < len(rs) {
		out = append(out, string(rs[start:]))
	}
	return out
}

// pyIntOf is isinstance(v, int): a bool is one.
func pyIntOf(v any) (int64, bool) {
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

// CheckSettings is check_settings; maxTimeout < 0: no environment.
func CheckSettings(node Node, maxTimeout int64) *auth.Refusal {
	d := data(node)
	val := func(k string, def int64) any {
		if v, ok := d.Get(k); ok {
			return v
		}
		return def
	}
	timeout, ok := pyIntOf(val("timeoutSeconds", 900))
	if !ok || timeout < 10 {
		return refuse(node, "the timeout is a number of seconds, at least 10")
	}
	if maxTimeout >= 0 && timeout > maxTimeout {
		return refuse(node, fmt.Sprintf("the timeout is longer than its environment allows (%d s)", maxTimeout))
	}
	retries, ok := pyIntOf(val("retries", 0))
	if !ok || retries < 0 || retries > MaxRetries {
		return refuse(node, fmt.Sprintf("the retries are between 0 and %d", MaxRetries))
	}
	delay, ok := pyIntOf(val("retryDelaySeconds", 60))
	if !ok || delay < 0 || delay > 3600 {
		return refuse(node, "the delay between retries is between 0 and 3600 seconds")
	}
	return nil
}

// ScriptNodes is script_nodes: the nodes of type "script" of a definition.
func ScriptNodes(definition string) []Node {
	if definition == "" {
		definition = "{}"
	}
	v, err := pyjson.Loads(definition)
	if err != nil {
		return nil
	}
	doc, ok := v.(pyjson.Object)
	if !ok {
		return nil
	}
	nodes, _ := get(doc, "nodes").([]any)
	var out []Node
	for _, n := range nodes {
		if o, ok := n.(pyjson.Object); ok && get(o, "type") == "script" {
			out = append(out, o)
		}
	}
	return out
}

// SavedScriptID is saved_script_id: the saved script a node runs by reference.
func SavedScriptID(node Node) (int64, bool) {
	id, ok := get(data(node), "scriptId").(int64)
	return id, ok
}
