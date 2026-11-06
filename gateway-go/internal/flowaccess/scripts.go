package flowaccess

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// services/script_nodes.py
const (
	maxFiles     = 50
	maxFileBytes = 256 * 1024
	maxCodeBytes = 768 * 1024
	maxRetries   = 5
)

// Python's `$` also matches before a final line feed: `\n?\z` here.
var scriptPath = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}){0,7}\n?\z`)

var entrypointSuffix = map[string][]string{"python": {".py"}, "r": {".R", ".r"}}

// ScriptNodes is script_nodes.script_nodes: the Script nodes of a definition (text or
// parsed).
func ScriptNodes(definition any) []pyjson.Object {
	var parsed pyjson.Object
	switch d := definition.(type) {
	case pyjson.Object:
		parsed = d
	case string:
		return scriptnodes.ScriptNodes(d)
	}
	nodes, _ := pyjson.Or(parsed.Value("nodes"), []any{}).([]any)
	var out []pyjson.Object
	for _, n := range nodes {
		if node := pyjson.Obj(n); node != nil && node.Value("type") == "script" {
			out = append(out, node)
		}
	}
	return out
}

func data(node pyjson.Object) pyjson.Object {
	return pyjson.Obj(pyjson.Or(node.Value("data"), pyjson.Object{}))
}

func filesOf(node pyjson.Object) []pyjson.Object {
	files, _ := pyjson.Or(data(node).Value("files"), []any{}).([]any)
	var out []pyjson.Object
	for _, f := range files {
		if o := pyjson.Obj(f); o != nil {
			out = append(out, o)
		}
	}
	return out
}

// SavedScriptID is script_nodes.saved_script_id (internal/scriptnodes).
func SavedScriptID(node pyjson.Object) (int64, bool) { return scriptnodes.SavedScriptID(node) }

func sha(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

// CodeFingerprint is script_nodes.code_fingerprint.
func CodeFingerprint(node pyjson.Object) string {
	d := data(node)
	if sid, ok := SavedScriptID(node); ok {
		s, _ := pyjson.Dumps(pyjson.Object{{Key: "script", Value: sid}}, pyjson.Options{EnsureASCII: true})
		return sha(s)
	}
	var files [][2]string
	for _, f := range filesOf(node) {
		files = append(files, [2]string{pyjson.Str(f.Value("path")), pyjson.Str(f.Value("content"))})
	}
	sort.Slice(files, func(i, j int) bool {
		if files[i][0] != files[j][0] {
			return files[i][0] < files[j][0]
		}
		return files[i][1] < files[j][1]
	})
	list := make([]any, len(files))
	for i, f := range files {
		list[i] = []any{f[0], f[1]}
	}
	s, _ := pyjson.Dumps(pyjson.Object{{Key: "environment", Value: d.Value("environmentId")},
		{Key: "entrypoint", Value: d.Value("entrypoint")}, {Key: "files", Value: list}},
		pyjson.Options{EnsureASCII: true, SortKeys: true})
	return sha(s)
}

func scriptLabel(node pyjson.Object) string { return scriptnodes.Label(node) }

func scriptRefuse(node pyjson.Object, message string) *auth.Refusal {
	return refuse(422, fmt.Sprintf("Script «%s»: %s", scriptLabel(node), message))
}

// scriptsDisabled is script_builds.disabled_reason() == "kubernetes".
func (s Settings) scriptsDisabled() bool { return !s.ScriptsEnabled }

// ScriptCheckDefinition is script_nodes.check_definition, at every save of a flow.
func (a *Access) ScriptCheckDefinition(ctx context.Context, before, after string) (*auth.Refusal, error) {
	nodes := ScriptNodes(after)
	if len(nodes) == 0 || a.Set.scriptsDisabled() {
		return nil, nil
	}
	if before == "" {
		before = "{}"
	}
	old := map[string]string{}
	for _, n := range ScriptNodes(before) {
		old[pyjson.Repr(n.Value("id"))] = CodeFingerprint(n)
	}
	unchanged := func(n pyjson.Object) bool {
		fp, ok := old[pyjson.Repr(n.Value("id"))]
		return ok && fp == CodeFingerprint(n)
	}
	var bucket string
	err := a.Q.QueryRow(ctx, `SELECT bucket FROM script_storage WHERE id = 1`).Scan(&bucket)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	bucket = pyStrip(bucket)
	for _, node := range nodes {
		if sid, ok := SavedScriptID(node); ok {
			if unchanged(node) {
				continue
			}
			s, err := a.SavedScriptByID(ctx, sid)
			if err != nil {
				return nil, err
			}
			see := false
			if s != nil {
				if see, err = a.CanSeeScript(ctx, s); err != nil {
					return nil, err
				}
			}
			if !see {
				return scriptRefuse(node, "choose a saved script you may see"), nil
			}
			continue
		}
		raw := data(node).Value("environmentId")
		var env *Environment
		if id, ok := pyjson.IsInt(raw); ok {
			if env, err = a.environment(ctx, id); err != nil {
				return nil, err
			}
		}
		if raw != nil && env == nil {
			return scriptRefuse(node, "its scripting environment no longer exists: choose another"), nil
		}
		if !unchanged(node) && (env != nil || len(filesOf(node)) > 0) {
			admin, err := a.C.IsAdmin(ctx)
			if err != nil {
				return nil, err
			}
			if !admin {
				can := false
				if env != nil {
					if can, err = a.CanUseEnvironment(ctx, env.ID); err != nil {
						return nil, err
					}
				}
				if !can {
					what := "this environment"
					if env != nil {
						what = fmt.Sprintf("the environment «%s»", env.Name)
					}
					return refuse(http.StatusForbidden, fmt.Sprintf("Script «%s»: you cannot write scripts for %s", scriptLabel(node), what)), nil
				}
			}
		}
		language := ""
		if env != nil {
			language = env.Language
		}
		if r := scriptnodes.CheckFiles(node, language, bucket, false); r != nil {
			return r, nil
		}
		maxTimeout := int64(-1)
		if env != nil {
			maxTimeout = env.MaxTimeoutSeconds
		}
		if r := scriptnodes.CheckSettings(node, maxTimeout); r != nil {
			return r, nil
		}
	}
	return nil, nil
}

// pyIntValue is isinstance(v, int), where a boolean counts (True is 1).
func pyIntValue(v any) (int64, bool) {
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

// pyStrip is str.strip(): Python's whitespace.
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	})
}
