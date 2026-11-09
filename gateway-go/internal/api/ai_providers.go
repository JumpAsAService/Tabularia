package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aimodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/ai_providers.py: the AI model providers from the panel. The key is written and
// never read back. Testing an Anthropic provider goes through Anthropic's SDK: Python's.
func init() {
	register("ai_providers", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/ai/providers", Handler: adminRole(d, "/ai/providers", false, listAIProviders(d))},
			{Method: http.MethodPost, Pattern: "/ai/providers", Handler: adminBody(d, "/ai/providers", createAIProvider(d))},
			{Method: http.MethodPatch, Pattern: "/ai/providers/{provider_id}", Handler: adminBody(d, "/ai/providers/{provider_id}", updateAIProvider(d))},
			{Method: http.MethodDelete, Pattern: "/ai/providers/{provider_id}", Handler: adminRole(d, "/ai/providers/{provider_id}", true, deleteAIProvider(d))},
			{Method: http.MethodPost, Pattern: "/ai/providers/test", Handler: adminBody(d, "/ai/providers/test", testAIProvider(d))},
		}
	})
}

const newAddressNeedsKey = "Enter the API key again when you change the address: the saved key would be sent to the new one"

// AiProviderOut is routes/ai_providers.py AiProviderOut.
type AiProviderOut struct {
	Key           string `json:"key"`
	ID            *int64 `json:"id"`
	Name          string `json:"name"`
	Kind          string `json:"kind"`
	BaseURL       string `json:"base_url"`
	Active        bool   `json:"active"`
	HasKey        bool   `json:"has_key"`
	ReadOnly      bool   `json:"read_only"`
	Chats         int64  `json:"chats"`
	EnabledModels int64  `json:"enabled_models"`
	// ModelsProblem: on a provider just added, why its models could not be enabled by
	// themselves (its catalog did not answer); absent otherwise
	ModelsProblem *string `json:"models_problem,omitempty"`
}

func (d *Deps) aiEnv() *aimodels.Endpoint {
	return aimodels.Env(d.Config.Admin.AIBaseURL, d.Config.Admin.AISecretKey)
}

// providerOut is _provider_out: the address only to an administrator.
func providerOut(ctx context.Context, d *Deps, e aimodels.Endpoint, full bool) (AiProviderOut, error) {
	out := AiProviderOut{Key: e.Key(), ID: e.ProviderID, Name: e.Name, Kind: e.Kind, Active: e.Active, HasKey: true,
		ReadOnly: e.ProviderID == nil}
	if full {
		out.BaseURL = e.BaseURL
	}
	if e.ProviderID != nil {
		var err error
		if out.Chats, out.EnabledModels, err = aimodels.Usage(ctx, d.DB, *e.ProviderID); err != nil {
			return out, err
		}
	}
	return out, nil
}

func listAIProviders(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		ctx := r.Context()
		eps, err := aimodels.Endpoints(ctx, d.DB, d.aiEnv(), false)
		if err != nil {
			adminFail(d, w, "reading the providers", err)
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		out := []AiProviderOut{}
		for _, e := range eps {
			o, err := providerOut(ctx, d, e, full)
			if err != nil {
				adminFail(d, w, "reading the providers' usage", err)
				return
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// validProviderURL is _valid_url; false when the answer was written (a 422).
func validProviderURL(d *Deps, w http.ResponseWriter, r *http.Request, raw []byte, kind, url string) (string, bool) {
	u, err := aimodels.NormalizeBaseURL(kind, url)
	if err != nil {
		adminDetail(w, http.StatusUnprocessableEntity, err.Error())
		return "", false
	}
	return u, true
}

// nameFree is _name_free: no other provider with the name, whatever its case.
func nameFree(ctx context.Context, d *Deps, w http.ResponseWriter, name string, notID *int64) bool {
	var id int64
	var same string
	err := d.DB.QueryRow(ctx, `SELECT id, name FROM ai_providers WHERE lower(name) = $1 LIMIT 1`, strings.ToLower(name)).Scan(&id, &same)
	if errors.Is(err, pgx.ErrNoRows) {
		return true
	}
	if err != nil {
		adminFail(d, w, "reading the providers", err)
		return false
	}
	if notID != nil && id == *notID {
		return true
	}
	adminDetail(w, http.StatusConflict, fmt.Sprintf("There is already a provider called «%s»", same))
	return false
}

func createAIProvider(d *Deps) adminBodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.AiProviderIn)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		name := pyd.Strip(b.Get("name").(string))
		if name == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "Give the provider a name")
			return
		}
		key := pyd.Strip(b.Get("api_key").(string))
		if key == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "Enter the provider's API key")
			return
		}
		kind := b.Get("kind").(string)
		url, ok := validProviderURL(d, w, r, raw, kind, b.Get("base_url").(string))
		if !ok || !nameFree(ctx, d, w, name, nil) {
			return
		}
		enc, err := d.Fernet.Encrypt(key)
		if err != nil {
			adminFail(d, w, "encrypting the key", err)
			return
		}
		now := nowNaive()
		var id int64
		if err := d.DB.QueryRow(ctx, `INSERT INTO ai_providers (name, kind, base_url, secret_encrypted, active, created_at,
			updated_at, updated_by) VALUES ($1, $2, $3, $4, true, $5, $5, $6) RETURNING id`,
			name, kind, url, enc, now, p.User.ID).Scan(&id); err != nil {
			adminFail(d, w, "saving the provider", err)
			return
		}
		tt := "ai_provider"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.provider_create", TargetType: &tt,
			TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "kind", Value: kind}, {Key: "base_url", Value: url}}})
		problem := enableOfferedModels(ctx, d, p, id, name)
		e := aimodels.Endpoint{ProviderID: &id, Name: name, Kind: kind, BaseURL: url, Active: true}
		out, err := providerOut(ctx, d, e, true)
		if err != nil {
			adminFail(d, w, "reading the provider", err)
			return
		}
		out.ModelsProblem = problem
		server.JSON(w, http.StatusCreated, out)
	}
}

// enableOfferedModels turns on every chat model a provider just added offers, so that people
// can try the assistant at once and the administrator turns off what they do not want. The
// catalog is the AI service's, the one the panel shows and PUT /ai/models checks against
// (Anthropic's read through its SDK), and the same is_chat_model check applies: no embedding,
// audio, image or reranker. One audit entry lists them. A catalog that cannot be read leaves the
// provider saved with no model on, and the words say so (nil: nothing to say).
func enableOfferedModels(ctx context.Context, d *Deps, p *auth.Principal, id int64, name string) *string {
	byHand := func(why string) *string {
		s := "Its models were not enabled: " + why + " Enable them by hand in Administration → AI models."
		return &s
	}
	cat, err := d.SQL.Catalog(ctx, id)
	if err != nil {
		d.Log.Warn("reading the catalog of a new AI provider", "provider", id, "err", err)
		return byHand("the AI service did not answer.")
	}
	if cat.Problem != nil {
		return byHand(*cat.Problem)
	}
	refs, names := []string{}, []string{}
	for _, m := range cat.Models {
		if aimodels.IsChatModel(m) {
			refs, names = append(refs, aimodels.RefOf(&id, m)), append(names, m)
		}
	}
	if len(refs) == 0 {
		return nil
	}
	if _, err := d.DB.Exec(ctx, `INSERT INTO ai_models (model_id, enabled, updated_at, updated_by)
		SELECT unnest($1::text[]), true, $2, $3
		ON CONFLICT (model_id) DO UPDATE SET enabled = true, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by`,
		refs, nowNaive(), p.User.ID); err != nil {
		d.Log.Error("enabling the models of a new AI provider", "provider", id, "err", err)
		return byHand("they could not be saved.")
	}
	tt := "ai_provider"
	listed := make([]any, len(names))
	for i, n := range names {
		listed[i] = n
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.model_enable", TargetType: &tt,
		TargetID: &id, TargetLabel: &name, Detail: pyjson.Object{{Key: "models", Value: listed}, {Key: "automatic", Value: true}}})
	return nil
}

// providerRow is _provider_row: the path's id validated, then the row or a 404.
func providerRow(ctx context.Context, d *Deps, w http.ResponseWriter, idV any) (*aimodels.Endpoint, bool) {
	e, err := aimodels.Row(ctx, d.DB, pathID(idV))
	if err != nil {
		adminFail(d, w, "reading the provider", err)
		return nil, false
	}
	if e == nil {
		adminDetail(w, http.StatusNotFound, "Provider not found")
		return nil, false
	}
	return e, true
}

func updateAIProvider(d *Deps) adminBodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		var ps pyd.Params
		idV := ps.Path("provider_id", r.PathValue("provider_id"), pyd.Int())
		b := ps.Body(body, gatewaymodels.AiProviderUpdate)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		e, ok := providerRow(ctx, d, w, idV)
		if !ok {
			return
		}
		changed := pyjson.Object{}
		key := pyd.Strip(b.Get("api_key").(string))
		if v, ok := b.Get("name").(string); ok && pyd.Strip(v) != e.Name {
			name := pyd.Strip(v)
			if name == "" {
				adminDetail(w, http.StatusUnprocessableEntity, "Give the provider a name")
				return
			}
			if !nameFree(ctx, d, w, name, e.ProviderID) {
				return
			}
			e.Name, changed = name, changed.Set("name", name)
		}
		if v, ok := b.Get("base_url").(string); ok {
			url, ok := validProviderURL(d, w, r, raw, e.Kind, v)
			if !ok {
				return
			}
			if url != e.BaseURL {
				if key == "" {
					adminDetail(w, http.StatusUnprocessableEntity, newAddressNeedsKey)
					return
				}
				e.BaseURL, changed = url, changed.Set("base_url", url)
			}
		}
		if key != "" {
			enc, err := d.Fernet.Encrypt(key)
			if err != nil {
				adminFail(d, w, "encrypting the key", err)
				return
			}
			e.SecretEncrypted, changed = enc, changed.Set("key_changed", true)
		}
		if v, ok := b.Get("active").(bool); ok && v != e.Active {
			e.Active, changed = v, changed.Set("active", v)
		}
		if len(changed) > 0 {
			if _, err := d.DB.Exec(ctx, `UPDATE ai_providers SET name = $1, base_url = $2, secret_encrypted = $3, active = $4,
				updated_at = $5, updated_by = $6 WHERE id = $7`, e.Name, e.BaseURL, e.SecretEncrypted, e.Active, nowNaive(),
				p.User.ID, *e.ProviderID); err != nil {
				adminFail(d, w, "saving the provider", err)
				return
			}
			tt := "ai_provider"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.provider_update", TargetType: &tt,
				TargetID: e.ProviderID, TargetLabel: &e.Name, Detail: changed})
		}
		out, err := providerOut(ctx, d, *e, true)
		if err != nil {
			adminFail(d, w, "reading the provider", err)
			return
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// deleteAIProvider is DELETE /ai/providers/{id}: asked without `confirm` while something
// depends on it, it says what; its enabled models go with it.
func deleteAIProvider(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		idV := ps.Path("provider_id", r.PathValue("provider_id"), pyd.Int())
		confirm := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "confirm", pyd.Bool(), false)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		e, ok := providerRow(ctx, d, w, idV)
		if !ok {
			return
		}
		id := *e.ProviderID
		chats, enabled, err := aimodels.Usage(ctx, d.DB, id)
		if err != nil {
			adminFail(d, w, "reading the provider's usage", err)
			return
		}
		if (chats != 0 || enabled != 0) && !confirm.(bool) {
			adminDetail(w, http.StatusConflict, fmt.Sprintf("«%s» has %d enabled models and %d conversations using them. "+
				"Its models go with it; the conversations stay readable and can go on with another model.", e.Name, enabled, chats))
			return
		}
		tx, err := d.DB.Begin(ctx)
		if err != nil {
			adminFail(d, w, "deleting the provider", err)
			return
		}
		defer tx.Rollback(ctx) //nolint:errcheck
		if _, err = tx.Exec(ctx, `DELETE FROM ai_models WHERE model_id LIKE $1 || '%'`, fmt.Sprintf("provider:%d/", id)); err == nil {
			if _, err = tx.Exec(ctx, `DELETE FROM ai_providers WHERE id = $1`, id); err == nil {
				err = tx.Commit(ctx)
			}
		}
		if err != nil {
			adminFail(d, w, "deleting the provider", err)
			return
		}
		tt := "ai_provider"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.provider_delete", TargetType: &tt,
			TargetID: &id, TargetLabel: &e.Name, Detail: aimodels.UsageObject(chats, enabled)})
		adminNone(w)
	}
}

// AiProviderTestOut is routes/ai_providers.py AiProviderTestOut.
type AiProviderTestOut struct {
	OK      bool    `json:"ok"`
	Models  int     `json:"models"`
	Code    *string `json:"code"`
	Message *string `json:"message"`
}

// testAIProvider is POST /ai/providers/test: the provider's own catalog, read with the
// address and the key given or saved.
func testAIProvider(d *Deps) adminBodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, raw []byte) {
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.AiProviderTest)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		ctx := r.Context()
		var saved *aimodels.Endpoint
		keyRef, _ := b.Get("key").(string)
		if keyRef != "" {
			var err error
			if saved, err = aimodels.OfKey(ctx, d.DB, d.aiEnv(), keyRef); err != nil {
				adminFail(d, w, "reading the provider", err)
				return
			}
			if saved == nil {
				adminDetail(w, http.StatusNotFound, "Provider not found")
				return
			}
		}
		kind, _ := b.Get("kind").(string)
		if saved != nil {
			kind = saved.Kind
		}
		if kind == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "Say which kind of provider to test")
			return
		}
		key := pyd.Strip(b.Get("api_key").(string))
		baseURL, given := b.Get("base_url").(string)
		var url string
		if saved != nil && !given {
			url = saved.BaseURL
		} else {
			var ok bool
			if url, ok = validProviderURL(d, w, r, raw, kind, baseURL); !ok {
				return
			}
		}
		if saved == nil && key == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "Enter the provider's API key")
			return
		}
		if saved != nil && key == "" && url != saved.BaseURL {
			adminDetail(w, http.StatusUnprocessableEntity, newAddressNeedsKey)
			return
		}
		var target aimodels.Endpoint
		secret := key
		if saved != nil && key == "" {
			target = *saved
			if target.SecretEncrypted != "" {
				s, err := d.Fernet.Decrypt(target.SecretEncrypted)
				if err != nil {
					adminDetail(w, http.StatusConflict, fmt.Sprintf("The key of «%s» can no longer be read: enter it again "+
						"in Administration → AI models.", target.Name))
					return
				}
				secret = s
			} else {
				secret = target.SecretPlain
			}
		} else {
			name, _ := b.Get("name").(string)
			name = pyd.Strip(name)
			if name == "" {
				name = url
				if saved != nil {
					name = saved.Name
				}
			}
			target = aimodels.Endpoint{Name: name, Kind: kind, BaseURL: url, Active: true}
		}
		if target.Kind == aimodels.Anthropic {
			// services/ai_anthropic.py catalog reads it with Anthropic's SDK (its requests, its
			// retry, its errors): the assistant's provider code, in the AI service. The request
			// goes there as it came; the checks above read only the database, and it makes
			// them again identically before reading the catalog
			toAIService(d, w, r, raw)
			return
		}
		ids, err := aimodels.OpenAICatalog(ctx, target, secret)
		var ce *aimodels.CatalogError
		if errors.As(err, &ce) {
			server.JSON(w, http.StatusOK, AiProviderTestOut{Code: &ce.Code, Message: &ce.Message})
			return
		}
		if err != nil {
			adminFail(d, w, "reading the catalog", err)
			return
		}
		server.JSON(w, http.StatusOK, AiProviderTestOut{OK: true, Models: len(ids)})
	}
}
