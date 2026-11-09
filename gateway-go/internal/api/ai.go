package api

import (
	"context"
	"errors"
	"net/http"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aimodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/aiprompts"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/ai.py, the part that runs no model: the assistant's state and the agents'
// instructions from the panel. The rest stays with the Python AI service (decision 4 of
// docs/specs/gateway-go.md) and is forwarded: the models' catalogs and their enabling
// (GET /ai/models, PUT /ai/models/{id}: their «priced» comes from genai-prices), the chat,
// the flow and script assistants, the saved conversations and their exports.
func init() {
	register("ai", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/ai/status", Handler: server.Authed(d.Auth, "/ai/status", aiStatus(d))},
			{Method: http.MethodGet, Pattern: "/ai/prompts", Handler: adminRole(d, "/ai/prompts", false, listAIPrompts(d))},
			{Method: http.MethodPut, Pattern: "/ai/prompts/{agent}", Handler: adminBody(d, "/ai/prompts/{agent}", setAIPrompt(d))},
			{Method: http.MethodDelete, Pattern: "/ai/prompts/{agent}", Handler: adminRole(d, "/ai/prompts/{agent}", true, resetAIPrompt(d))},
		}
	})
}

// AiStatusOut is routes/ai.py AiStatusOut.
type AiStatusOut struct {
	Enabled         bool              `json:"enabled"`
	Models          []string          `json:"models"`
	DefaultModel    *string           `json:"default_model"`
	Options         []aimodels.Option `json:"options"`
	MetadataOnly    bool              `json:"metadata_only"`
	MCPMetadataOnly bool              `json:"mcp_metadata_only"`
	CommentModel    *string           `json:"comment_model"`
}

// aiStatus is GET /ai/status: for everyone, is the assistant on, with which models.
func aiStatus(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, _ *auth.Principal) {
		ctx := r.Context()
		out := AiStatusOut{Models: []string{}, Options: []aimodels.Option{}, MetadataOnly: d.Panel.MetadataOnly("chat"),
			MCPMetadataOnly: d.Panel.MetadataOnly("mcp")}
		env := d.aiEnv()
		eps, err := aimodels.Endpoints(ctx, d.DB, env, true)
		if err != nil {
			adminFail(d, w, "reading the providers", err)
			return
		}
		if len(eps) == 0 { // ai_models.configured
			server.JSON(w, http.StatusOK, out)
			return
		}
		models, err := aimodels.EnabledModelIDs(ctx, d.DB, env)
		if err == nil {
			out.Options, err = aimodels.Options(ctx, d.DB, env, models)
		}
		if err != nil {
			adminFail(d, w, "reading the models", err)
			return
		}
		out.Enabled, out.Models = true, models
		out.DefaultModel = aimodels.Preferred(d.Panel.AIDefaultModel(), models)
		if writer := aimodels.CommentModel(d.Panel.AICommentModel(), d.Panel.AIDefaultModel(), models); writer != nil {
			described, err := aimodels.Described(ctx, d.DB, *writer)
			if err != nil {
				adminFail(d, w, "reading the comment's model", err)
				return
			}
			out.CommentModel = &described
		}
		server.JSON(w, http.StatusOK, out)
	}
}

// AiPromptOut is routes/ai.py AiPromptOut.
type AiPromptOut struct {
	Agent     string  `json:"agent"`
	Text      string  `json:"text"`
	Default   string  `json:"default"`
	Custom    bool    `json:"custom"`
	Context   string  `json:"context"`
	UpdatedAt *string `json:"updated_at"`
	UpdatedBy *string `json:"updated_by"`
}

// promptOut is _prompt_out: the administrators' text when they changed it, who did (masked
// for an observer), and the context with example values.
func promptOut(ctx context.Context, d *Deps, full bool, agent, locale string) (AiPromptOut, error) {
	def, _ := aiprompts.Default(agent)
	out := AiPromptOut{Agent: agent, Text: def, Default: def,
		Context: aiprompts.ExampleContext(agent, locale, d.Panel.MetadataOnly("chat"), min(d.Panel.AIMaxResultRows(), aiprompts.EnginePreviewMaxRows), time.Now())}
	var text string
	var at time.Time
	var by *string
	err := d.DB.QueryRow(ctx, `SELECT text, updated_at, updated_by FROM ai_prompts WHERE agent = $1`, agent).Scan(&text, &at, &by)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return out, err
	}
	if pyd.Strip(text) == "" {
		return out, nil
	}
	out.Custom, out.Text = true, text
	iso := pyd.DateTime{Time: at}.ISO() // a plain datetime: naive, no offset
	out.UpdatedAt = &iso
	if by != nil && *by != "" {
		b := *by
		if !full {
			b = masking.Email(b)
		}
		out.UpdatedBy = &b
	}
	return out, nil
}

// promptLocale is the `locale` query parameter, [:10].
func promptLocale(ps *pyd.Params, r *http.Request) func() string {
	v := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "locale", pyd.Str(), "en")
	return func() string {
		s, _ := v.(string)
		if rs := []rune(s); len(rs) > 10 {
			return string(rs[:10])
		}
		return s
	}
}

func listAIPrompts(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		locale := promptLocale(&ps, r)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		full, err := isAdmin(d, r, p)
		if err != nil {
			adminFail(d, w, "reading the roles", err)
			return
		}
		out := []AiPromptOut{}
		for _, a := range aiprompts.Agents() {
			o, err := promptOut(r.Context(), d, full, a, locale())
			if err != nil {
				adminFail(d, w, "reading the prompts", err)
				return
			}
			out = append(out, o)
		}
		server.JSON(w, http.StatusOK, out)
	}
}

func knownAgent(w http.ResponseWriter, agent string) bool {
	if _, ok := aiprompts.Default(agent); !ok {
		adminDetail(w, http.StatusNotFound, "Unknown agent: "+agent)
		return false
	}
	return true
}

func writePrompt(ctx context.Context, d *Deps, w http.ResponseWriter, agent, locale string) {
	o, err := promptOut(ctx, d, true, agent, locale)
	if err != nil {
		adminFail(d, w, "reading the prompt", err)
		return
	}
	server.JSON(w, http.StatusOK, o)
}

// setAIPrompt is PUT /ai/prompts/{agent} (ai_prompts.save): the default's own text is no
// change, the row goes.
func setAIPrompt(d *Deps) adminBodyHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal, body pyd.Body, _ []byte) {
		var ps pyd.Params
		b := ps.Body(body, gatewaymodels.AiPromptUpdate)
		locale := promptLocale(&ps, r)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		agent := r.PathValue("agent")
		if !knownAgent(w, agent) {
			return
		}
		ctx := r.Context()
		text := pyd.Strip(b.Get("text").(string))
		if text == "" {
			adminDetail(w, http.StatusUnprocessableEntity, "The instructions cannot be empty: restore the default instead")
			return
		}
		def, _ := aiprompts.Default(agent)
		var err error
		if text == pyd.Strip(def) {
			_, err = d.DB.Exec(ctx, `DELETE FROM ai_prompts WHERE agent = $1`, agent)
		} else {
			_, err = d.DB.Exec(ctx, `INSERT INTO ai_prompts (agent, text, updated_at, updated_by) VALUES ($1, $2, $3, $4)
				ON CONFLICT (agent) DO UPDATE SET text = EXCLUDED.text, updated_at = EXCLUDED.updated_at,
				updated_by = EXCLUDED.updated_by`, agent, text, nowNaive(), p.User.Email)
		}
		if err != nil {
			adminFail(d, w, "saving the prompt", err)
			return
		}
		tt := "ai_prompt"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.prompt_update", TargetType: &tt, TargetLabel: &agent,
			Detail: pyjson.Object{{Key: "chars", Value: int64(utf8.RuneCountInString(text))}}})
		writePrompt(ctx, d, w, agent, locale())
	}
}

// resetAIPrompt is DELETE /ai/prompts/{agent}: back to the default.
func resetAIPrompt(d *Deps) adminHandler {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		locale := promptLocale(&ps, r)
		if resp := ps.Response(); resp != nil {
			resp.Write(w)
			return
		}
		agent := r.PathValue("agent")
		if !knownAgent(w, agent) {
			return
		}
		ctx := r.Context()
		tag, err := d.DB.Exec(ctx, `DELETE FROM ai_prompts WHERE agent = $1`, agent)
		if err != nil {
			adminFail(d, w, "resetting the prompt", err)
			return
		}
		if tag.RowsAffected() > 0 {
			tt := "ai_prompt"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "ai.prompt_reset", TargetType: &tt, TargetLabel: &agent})
		}
		writePrompt(ctx, d, w, agent, locale())
	}
}
