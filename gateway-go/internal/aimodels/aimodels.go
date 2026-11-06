// Package aimodels is services/ai_models.py without the calls a model answers: the
// providers (the environment's and the panel's), how a model is named, what depends on a
// provider, the address's check, and the OpenAI-compatible catalog. An Anthropic catalog
// goes through Anthropic's SDK in Python (services/ai_anthropic.py): not here.
package aimodels

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
)

// the kinds, and what the API calls the environment's provider
const (
	OpenAICompatible = "openai_compatible"
	Anthropic        = "anthropic"
	AnthropicBaseURL = "https://api.anthropic.com"
	Environment      = "env"
)

var refRe = regexp.MustCompile(`(?s)^provider:(\d+)/(.+)$`)

// SplitRef is split_ref: (provider id, name at the provider); nil for the environment's.
func SplitRef(ref string) (*int64, string) {
	if m := refRe.FindStringSubmatch(ref); m != nil {
		var id int64
		if _, err := fmt.Sscan(m[1], &id); err == nil {
			return &id, m[2]
		}
	}
	return nil, ref
}

// RefOf is ref_of.
func RefOf(providerID *int64, model string) string {
	if providerID == nil {
		return model
	}
	return fmt.Sprintf("provider:%d/%s", *providerID, model)
}

// ModelName is model_name.
func ModelName(ref string) string { _, n := SplitRef(ref); return n }

var notChat = []string{"embedding", "embed-", "bge-", "whisper", "rerank", "tts", "speech", "transcri"}

// IsChatModel is is_chat_model.
func IsChatModel(ref string) bool {
	low := strings.ToLower(ModelName(ref))
	for _, m := range notChat {
		if strings.Contains(low, m) {
			return false
		}
	}
	return true
}

// Endpoint is a provider that can be called.
type Endpoint struct {
	ProviderID      *int64 // nil = the environment's
	Name, Kind      string
	BaseURL         string
	Active          bool
	SecretEncrypted string
	SecretPlain     string
}

// Key is Endpoint.key.
func (e Endpoint) Key() string {
	if e.ProviderID == nil {
		return Environment
	}
	return fmt.Sprintf("provider:%d", *e.ProviderID)
}

// Querier is a pool, a connection or a transaction.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Env is environment_endpoint: on when AI__BASE_URL and AI__SECRET_KEY are both set; named
// after its host.
func Env(baseURL, secretKey string) *Endpoint {
	base := strings.TrimFunc(baseURL, unicode.IsSpace)
	if base == "" || strings.TrimFunc(secretKey, unicode.IsSpace) == "" {
		return nil
	}
	name := base
	if parts, err := pyurl.URLSplit(base); err == nil {
		if h, ok := parts.Hostname(); ok {
			name = h
		}
	}
	return &Endpoint{Name: name, Kind: OpenAICompatible, BaseURL: base, Active: true, SecretPlain: secretKey}
}

const providerColumns = `id, name, kind, base_url, active, secret_encrypted`

func scan(row pgx.Row) (Endpoint, error) {
	var e Endpoint
	var id int64
	err := row.Scan(&id, &e.Name, &e.Kind, &e.BaseURL, &e.Active, &e.SecretEncrypted)
	e.ProviderID = &id
	return e, err
}

// Endpoints is endpoints: the environment's first, then the panel's by name.
func Endpoints(ctx context.Context, q Querier, env *Endpoint, onlyActive bool) ([]Endpoint, error) {
	sql := `SELECT ` + providerColumns + ` FROM ai_providers`
	if onlyActive {
		sql += ` WHERE active = true`
	}
	rows, err := q.Query(ctx, sql+` ORDER BY name`)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (Endpoint, error) { return scan(r) })
	if err != nil {
		return nil, err
	}
	if env != nil {
		return append([]Endpoint{*env}, found...), nil
	}
	return found, nil
}

var keyRe = regexp.MustCompile(`^provider:(\d+)$`)

// OfKey is endpoint_of_key: a provider by its key (`env`, `provider:3`), on or off.
func OfKey(ctx context.Context, q Querier, env *Endpoint, key string) (*Endpoint, error) {
	if key == Environment {
		return env, nil
	}
	m := keyRe.FindStringSubmatch(key)
	if m == nil {
		return nil, nil
	}
	var id int64
	if _, err := fmt.Sscan(m[1], &id); err != nil || id > 1<<31-1 {
		return nil, nil // past the column: no row has it
	}
	return Row(ctx, q, id)
}

// Row is the ai_providers row as an Endpoint, or nil.
func Row(ctx context.Context, q Querier, id int64) (*Endpoint, error) {
	e, err := scan(q.QueryRow(ctx, `SELECT `+providerColumns+` FROM ai_providers WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &e, nil
}

// Usage is provider_usage: the conversations and the enabled models of a provider.
func Usage(ctx context.Context, q Querier, id int64) (chats, enabled int64, err error) {
	prefix := fmt.Sprintf("provider:%d/", id)
	if err = q.QueryRow(ctx, `SELECT count(*) FROM ai_chats WHERE model_id LIKE $1 || '%'`, prefix).Scan(&chats); err != nil {
		return
	}
	err = q.QueryRow(ctx, `SELECT count(*) FROM ai_models WHERE model_id LIKE $1 || '%' AND enabled = true`, prefix).Scan(&enabled)
	return
}

// UsageObject is provider_usage's dict.
func UsageObject(chats, enabled int64) pyjson.Object {
	return pyjson.Object{{Key: "chats", Value: chats}, {Key: "enabled_models", Value: enabled}}
}

// NormalizeBaseURL is normalize_base_url: the address as it is stored, or the ValueError's
// text.
func NormalizeBaseURL(kind, url string) (string, error) {
	url = strings.TrimRight(strings.TrimFunc(url, unicode.IsSpace), "/")
	if url == "" {
		if kind == Anthropic {
			return AnthropicBaseURL, nil
		}
		return "", errors.New("Enter the provider's address, ending in /v1")
	}
	parts, err := pyurl.URLSplit(url)
	if err != nil {
		return "", err
	}
	if _, ok := parts.Hostname(); (parts.Scheme != "http" && parts.Scheme != "https") || !ok {
		return "", errors.New("The address must start with https:// (or http:// inside your network)")
	}
	user, password := parts.UserInfo()
	if user != "" || password != "" || parts.Query != "" || parts.Fragment != "" {
		return "", errors.New("The address cannot hold a user, a password or a query: the key goes in its own field")
	}
	if kind == Anthropic {
		return strings.TrimSuffix(url, "/v1"), nil
	}
	return url, nil
}

// Failure is failure(status, provider): (code, message) of a failed call; status 0 = not
// reached.
func Failure(status int, provider string) (string, string) {
	switch status {
	case 0:
		return "provider_unreachable", fmt.Sprintf("«%s» could not be reached: check its address and the network.", provider)
	case 400:
		return "provider_rejected", fmt.Sprintf("«%s» refused the request. The conversation may be too long for this model: try a new one, or another model.", provider)
	case 401:
		return "provider_key", fmt.Sprintf("«%s» refused the API key. An administrator can change it in Administration → AI models.", provider)
	case 402:
		return "provider_billing", fmt.Sprintf("«%s» reports a billing problem on this account. An administrator can check it on the provider's site.", provider)
	case 403:
		return "provider_forbidden", fmt.Sprintf("«%s» does not allow this API key to do this. An administrator can check the key's permissions on the provider's site.", provider)
	case 404:
		return "provider_model", fmt.Sprintf("«%s» does not offer this model, or its address is wrong. Try another model, or ask an administrator to check the provider.", provider)
	case 413:
		return "provider_too_large", fmt.Sprintf("The request is too large for «%s»: start a new conversation.", provider)
	case 429:
		return "provider_busy", fmt.Sprintf("«%s» is limiting requests on this account. Try again in a moment.", provider)
	case 529:
		return "provider_overloaded", fmt.Sprintf("«%s» is overloaded right now. Try again in a moment, or choose another model.", provider)
	}
	if status >= 500 {
		return "provider_error", fmt.Sprintf("«%s» had a problem of its own (%d). Try again in a moment.", provider, status)
	}
	return "provider_rejected", fmt.Sprintf("«%s» refused the request (%d).", provider, status)
}

// CatalogError is a catalog that could not be read: its code and words.
type CatalogError struct{ Code, Message string }

func (e *CatalogError) Error() string { return e.Message }

// OpenAICatalog is _openai_compatible_catalog: GET <base>/models with the key, the ids
// sorted without repeats. Not for Anthropic's kind.
func OpenAICatalog(ctx context.Context, e Endpoint, secret string) ([]string, error) {
	unreachable := func() error { c, m := Failure(0, e.Name); return &CatalogError{c, m} }
	client := &http.Client{Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(e.BaseURL, "/")+"/models", nil)
	if err != nil {
		return nil, unreachable()
	}
	req.Header.Set("Authorization", "Bearer "+secret)
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", "python-httpx/0.28.1")
	res, err := client.Do(req)
	if err != nil {
		return nil, unreachable()
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 { // raise_for_status
		c, m := Failure(res.StatusCode, e.Name)
		return nil, &CatalogError{c, m}
	}
	var raw []byte
	buf := make([]byte, 32*1024)
	for {
		n, err := res.Body.Read(buf)
		raw = append(raw, buf[:n]...)
		if err != nil {
			break
		}
	}
	v, err := pyjson.Loads(string(raw))
	obj, ok := v.(pyjson.Object)
	if err != nil || !ok {
		return nil, unreachable() // not JSON, or .get on what is not a dict
	}
	data, has := obj.Get("data")
	if !has {
		return []string{}, nil
	}
	list, ok := data.([]any)
	if !ok {
		return nil, unreachable()
	}
	set := map[string]bool{}
	for _, m := range list {
		item, ok := m.(pyjson.Object)
		if !ok {
			return nil, unreachable()
		}
		if id, _ := item.Get("id"); truthy(id) {
			set[pystr.Str(id)] = true
		}
	}
	out := make([]string, 0, len(set))
	for id := range set {
		out = append(out, id)
	}
	sort.Strings(out)
	return out, nil
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case int64:
		return x != 0
	case float64:
		return x != 0
	case string:
		return x != ""
	case pyjson.Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return true
}

// ── the administrator's choice ───────────────────────────────────────────────

func samePID(a, b *int64) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// EnabledModelIDs is enabled_model_ids: enabled, fit for the chat, of a provider that is
// on; sorted.
func EnabledModelIDs(ctx context.Context, q Querier, env *Endpoint) ([]string, error) {
	rows, err := q.Query(ctx, `SELECT model_id FROM ai_models WHERE enabled = true`)
	if err != nil {
		return nil, err
	}
	refs, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	alive, err := Endpoints(ctx, q, env, true)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, ref := range refs {
		pid, _ := SplitRef(ref)
		ok := false
		for _, e := range alive {
			if samePID(e.ProviderID, pid) {
				ok = true
				break
			}
		}
		if ok && IsChatModel(ref) {
			out = append(out, ref)
		}
	}
	sort.Strings(out)
	return out, nil
}

// Preferred is preferred(configured, enabled): the model offered first.
func Preferred(configured string, enabled []string) *string {
	for _, r := range enabled {
		if r == configured {
			return &r
		}
	}
	var best *string
	var bestKey int64
	for i, r := range enabled { // the same name: the environment's, then the provider added first
		if ModelName(r) != configured {
			continue
		}
		pid, _ := SplitRef(r)
		key := int64(0)
		if pid != nil {
			key = *pid
		}
		if best == nil || key < bestKey {
			best, bestKey = &enabled[i], key
		}
	}
	if best != nil {
		return best
	}
	for i, r := range enabled { // min by (has a provider, ref)
		if best == nil || lessRef(r, *best) {
			best = &enabled[i]
		}
	}
	return best
}

func lessRef(a, b string) bool {
	pa, _ := SplitRef(a)
	pb, _ := SplitRef(b)
	if (pa != nil) != (pb != nil) {
		return pa == nil
	}
	return a < b
}

// Option is one of options(): a model with who answers.
type Option struct {
	ID       string `json:"id"`
	Model    string `json:"model"`
	Provider string `json:"provider"`
}

// Options is options(session, enabled): the environment's first, then by provider and name.
func Options(ctx context.Context, q Querier, env *Endpoint, enabled []string) ([]Option, error) {
	eps, err := Endpoints(ctx, q, env, true)
	if err != nil {
		return nil, err
	}
	name := func(pid *int64) string {
		for _, e := range eps {
			if samePID(e.ProviderID, pid) {
				return e.Name
			}
		}
		return ""
	}
	refs := append([]string(nil), enabled...)
	sort.SliceStable(refs, func(i, j int) bool {
		pi, _ := SplitRef(refs[i])
		pj, _ := SplitRef(refs[j])
		if (pi != nil) != (pj != nil) {
			return pi == nil
		}
		if ni, nj := name(pi), name(pj); ni != nj {
			return ni < nj
		}
		return ModelName(refs[i]) < ModelName(refs[j])
	})
	out := make([]Option, len(refs))
	for i, r := range refs {
		pid, n := SplitRef(r)
		out[i] = Option{ID: r, Model: n, Provider: name(pid)}
	}
	return out, nil
}

// Described is described(ref): «name (provider)», bare for the environment's provider or a
// provider gone.
func Described(ctx context.Context, q Querier, ref string) (string, error) {
	pid, n := SplitRef(ref)
	if pid == nil {
		return n, nil
	}
	row, err := Row(ctx, q, *pid)
	if err != nil || row == nil || row.Name == "" {
		return n, err
	}
	return n + " (" + row.Name + ")", nil
}

// CommentModel is ai_comment.model_of: AI__COMMENT_MODEL, and if it cannot be used no other;
// empty, the default model.
func CommentModel(commentModel, defaultModel string, enabled []string) *string {
	named := strings.TrimFunc(commentModel, unicode.IsSpace)
	if named != "" {
		usable := false
		for _, r := range enabled {
			if r == named || ModelName(r) == named {
				usable = true
				break
			}
		}
		if !usable {
			return nil
		}
		return Preferred(named, enabled)
	}
	return Preferred(defaultModel, enabled)
}
