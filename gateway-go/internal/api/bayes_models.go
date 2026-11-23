package api

// The saved Bayesian models (docs/specs/bayes-model-store.md): an object of a folder, as a saved
// script is, with versions written once and never again. A route of the Go gateway only (no Python
// twin); the logic is internal/bayesmodels, this file is the routes, their permissions and their
// answers.
//
// Who sees a model: an administrator, or whoever has VIEW on its folder and the use of the group
// `bayes`; whoever does not gets the 404 of a model that does not exist. The group's use is read
// once per request (ModelGroupUsable) and passed down as a bool.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// bmOtherFields is what a body may hold beside the graph (name, description, ids): the body is
// bounded to max_graph_bytes plus this.
const bmOtherFields = 16 << 10

// bmNotAvailable is the 403 of a person who may not use the group `bayes`.
const bmNotAvailable = "the Bayesian models are not available to you"

func init() {
	register("bayes_models", func(d *Deps) []server.Route {
		route := func(method, pattern string, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) server.Route {
			return server.Route{Method: method, Pattern: pattern, Handler: server.Authed(d.Auth, pattern,
				func(w http.ResponseWriter, r *http.Request, p *auth.Principal) { h(d, w, r, p) })}
		}
		return []server.Route{
			route(http.MethodPost, "/bayes-models/check", checkBayesModel),
			route(http.MethodGet, "/bayes-models", listBayesModels),
			route(http.MethodGet, "/projects/{project_id}/bayes-models", listProjectBayesModels),
			route(http.MethodPost, "/projects/{project_id}/bayes-models", createBayesModel),
			route(http.MethodGet, "/bayes-models/{model_id}", getBayesModel),
			route(http.MethodPatch, "/bayes-models/{model_id}", updateBayesModel),
			route(http.MethodDelete, "/bayes-models/{model_id}", deleteBayesModel),
			route(http.MethodPost, "/bayes-models/{model_id}/duplicate", duplicateBayesModel),
			route(http.MethodGet, "/bayes-models/{model_id}/versions", listBayesModelVersions),
			route(http.MethodGet, "/bayes-models/{model_id}/versions/{version}", getBayesModelVersion),
			route(http.MethodPost, "/bayes-models/{model_id}/versions/{version}/restore", restoreBayesModelVersion),
			route(http.MethodPost, "/bayes-models/{model_id}/versions/{version}/compile", compileBayesModelVersion),
			route(http.MethodGet, "/bayes-models/{model_id}/uses", listBayesModelUses),
		}
	})
}

// bayesCompiler is what stan-compile gives the store; until it is built, one that enqueues nothing.
func (d *Deps) bayesCompiler() bayesmodels.Compiler {
	if d.BayesCompiler != nil {
		return d.BayesCompiler
	}
	return bayesmodels.NoCompiler{}
}

func (d *Deps) bayesStore() *bayesmodels.Store {
	return &bayesmodels.Store{DB: d.DB, Compiler: d.bayesCompiler(), Log: d.Log}
}

// ── shapes ────────────────────────────────────────────────────────────────────

type bayesModelOut struct {
	ID              int64   `json:"id"`
	Name            string  `json:"name"`
	Description     string  `json:"description"`
	ProjectID       int64   `json:"project_id"`
	OwnerID         *int64  `json:"owner_id"`
	CurrentVersion  int64   `json:"current_version"`
	TemplateID      *string `json:"template_id"`
	TemplateVersion *int64  `json:"template_version"`
	Family          string  `json:"family"`
	CreatedAt       string  `json:"created_at"`
	UpdatedAt       string  `json:"updated_at"`
	Editable        bool    `json:"editable"`
	Runnable        bool    `json:"runnable"`
}

// bayesVersionOut is a version in full: the graph and the findings as JSON, the program as text.
type bayesVersionOut struct {
	Version          int64           `json:"version"`
	Note             string          `json:"note"`
	CreatedAt        string          `json:"created_at"`
	CreatedBy        *int64          `json:"created_by"`
	Graph            json.RawMessage `json:"graph"`
	FormatVersion    int             `json:"format_version"`
	SemanticHash     string          `json:"semantic_hash"`
	Findings         json.RawMessage `json:"findings"`
	Program          string          `json:"program"`
	ProgramSHA256    string          `json:"program_sha256"`
	Mapping          json.RawMessage `json:"mapping"`
	DataManifest     json.RawMessage `json:"data_manifest"`
	QuantityManifest json.RawMessage `json:"quantity_manifest"`
	CodegenVersion   int             `json:"codegen_version"`
	Family           string          `json:"family"`
	CatalogSHA256    string          `json:"catalog_sha256"`
	// CodegenCurrent is the generator's version now: a version with an older one runs as stored.
	CodegenCurrent int  `json:"codegen_current"`
	Current        bool `json:"current"`
}

type bayesCompileOut struct {
	State  string  `json:"state"`
	Detail string  `json:"detail"`
	At     *string `json:"at"`
}

type bayesModelDetail struct {
	bayesModelOut
	Version bayesVersionOut `json:"version"`
	Compile bayesCompileOut `json:"compile"`
	Uses    int             `json:"uses"`
}

type bayesVersionListed struct {
	Version        int64   `json:"version"`
	Note           string  `json:"note"`
	CreatedAt      string  `json:"created_at"`
	CreatedBy      *string `json:"created_by"`
	SemanticHash   string  `json:"semantic_hash"`
	ProgramSHA256  string  `json:"program_sha256"`
	CodegenVersion int     `json:"codegen_version"`
	Family         string  `json:"family"`
	Current        bool    `json:"current"`
}

// bayesInput is every body the routes read: a field not in the route's table is refused.
type bayesInput struct {
	Name            *string         `json:"name"`
	Description     *string         `json:"description"`
	ProjectID       *int64          `json:"project_id"`
	Graph           json.RawMessage `json:"graph"`
	BaseVersion     *int64          `json:"base_version"`
	TemplateID      *string         `json:"template_id"`
	TemplateVersion *int64          `json:"template_version"`
}

func (in *bayesInput) hasGraph() bool {
	g := bytes.TrimSpace(in.Graph)
	return len(g) > 0 && !bytes.Equal(g, []byte("null"))
}

// ── helpers ───────────────────────────────────────────────────────────────────

// bayesUsable is ModelGroupUsable for the group `bayes`: read once per request.
func bayesUsable(ctx context.Context, d *Deps, c *perm.Checker, p *auth.Principal) (bool, error) {
	return ModelGroupUsable(ctx, d.DB, c, p.User.ID, bayesmodels.Group)
}

// bayesBody reads a body within max_graph_bytes plus the other fields (413 beyond, before anything
// is decoded) and decodes it strictly (an unknown key is a 422). allowed names the fields the route
// takes.
func bayesBody(w http.ResponseWriter, r *http.Request, allowed ...string) (*bayesInput, bool) {
	limit := int64(bayescatalog.GetLimits().MaxGraphBytes + bmOtherFields)
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, limit))
	if err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			detail(w, http.StatusRequestEntityTooLarge, fmt.Sprintf("The request is larger than %d bytes", limit))
		} else {
			detail(w, http.StatusBadRequest, "The request could not be read")
		}
		return nil, false
	}
	var in bayesInput
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil || dec.More() {
		detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON, or not of the expected shape")
		return nil, false
	}
	ok := map[string]bool{}
	for _, a := range allowed {
		ok[a] = true
	}
	for name, present := range map[string]bool{"name": in.Name != nil, "description": in.Description != nil,
		"project_id": in.ProjectID != nil, "graph": len(in.Graph) > 0, "base_version": in.BaseVersion != nil,
		"template_id": in.TemplateID != nil, "template_version": in.TemplateVersion != nil} {
		if present && !ok[name] {
			detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON, or not of the expected shape")
			return nil, false
		}
	}
	return &in, true
}

// bayesVisible reads a model this person sees (the 404 otherwise) and the group's use.
func bayesVisible(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, p *auth.Principal, id int64) (*bayesmodels.Model, bool, bool) {
	ctx := r.Context()
	usable, err := bayesUsable(ctx, d, c, p)
	if err != nil {
		fail(d, w, "bayes models: the group's use", err)
		return nil, false, false
	}
	m, refusal, err := bayesmodels.GetVisible(ctx, c, d.DB, id, usable)
	if err != nil {
		fail(d, w, "bayes models: reading the model", err)
		return nil, false, false
	}
	if refusal != nil {
		server.Refuse(w, refusal)
		return nil, false, false
	}
	return m, usable, true
}

// bayesFolderEdit is the folder a model is created in, or moved to: 404 when it does not exist,
// 403 without EDIT on it. what names it in the 404.
func bayesFolderEdit(d *Deps, w http.ResponseWriter, ctx context.Context, c *perm.Checker, id int64, what string) bool {
	exists, err := scFolderExists(ctx, d.DB, id)
	if err != nil {
		fail(d, w, "bayes models: the folder", err)
		return false
	}
	if !exists {
		detail(w, http.StatusNotFound, what)
		return false
	}
	return scEnsure(d, w, ctx, c, id, perm.Edit)
}

// bayesOutOf is a model as the lists and the detail show it.
func bayesOutOf(ctx context.Context, c *perm.Checker, m *bayesmodels.Model, family string, rights map[int64][2]bool) (bayesModelOut, error) {
	out := bayesModelOut{ID: m.ID, Name: m.Name, Description: m.Description, ProjectID: m.ProjectID, OwnerID: m.OwnerID,
		CurrentVersion: m.CurrentVersion, TemplateID: m.TemplateID, TemplateVersion: m.TemplateVersion, Family: family,
		CreatedAt: pyjson.Isoformat(m.CreatedAt), UpdatedAt: pyjson.Isoformat(m.UpdatedAt)}
	rt, ok := rights[m.ProjectID]
	if !ok {
		var err error
		if rt[0], err = c.Has(ctx, m.ProjectID, perm.Edit); err != nil {
			return out, err
		}
		if rt[1], err = c.Has(ctx, m.ProjectID, perm.Run); err != nil {
			return out, err
		}
		rights[m.ProjectID] = rt
	}
	out.Editable, out.Runnable = rt[0], rt[1]
	return out, nil
}

func bayesVersionOutOf(v *bayesmodels.Version, current int64) bayesVersionOut {
	raw := func(s string) json.RawMessage {
		if s == "" {
			return json.RawMessage("null")
		}
		return json.RawMessage(s)
	}
	return bayesVersionOut{Version: v.Version, Note: v.Note, CreatedAt: pyjson.Isoformat(v.CreatedAt), CreatedBy: v.CreatedBy,
		Graph: raw(v.Graph), FormatVersion: v.FormatVersion, SemanticHash: v.SemanticHash, Findings: raw(v.Findings),
		Program: v.Program, ProgramSHA256: v.ProgramSHA256, Mapping: raw(v.Mapping), DataManifest: raw(v.DataManifest),
		QuantityManifest: raw(v.QuantityManifest), CodegenVersion: v.CodegenVersion, Family: v.Family,
		CatalogSHA256: v.CatalogSHA256, CodegenCurrent: bayescodegen.CodegenVersion, Current: v.Version == current}
}

func bayesCompileOutOf(s bayesmodels.CompileState) bayesCompileOut {
	out := bayesCompileOut{State: s.State, Detail: s.Detail}
	if !s.At.IsZero() {
		at := pyjson.Isoformat(s.At)
		out.At = &at
	}
	return out
}

// bayesDetailOf is the model and one of its versions (0: the current one) in full, with the
// compile state of that version's program and the number of flows that use the model.
func bayesDetailOf(ctx context.Context, d *Deps, c *perm.Checker, m *bayesmodels.Model, version int64) (*bayesModelDetail, bool, error) {
	v, err := bayesmodels.GetVersion(ctx, d.DB, m, version)
	if err != nil || v == nil {
		return nil, false, err
	}
	out, err := bayesOutOf(ctx, c, m, v.Family, map[int64][2]bool{})
	if err != nil {
		return nil, false, err
	}
	state, err := d.bayesCompiler().State(ctx, v.ProgramSHA256)
	if err != nil {
		return nil, false, err
	}
	uses, err := bayesmodels.Using(ctx, d.DB, m.ID)
	if err != nil {
		return nil, false, err
	}
	flows := map[int64]bool{}
	for _, u := range uses {
		flows[u.FlowID] = true
	}
	return &bayesModelDetail{bayesModelOut: out, Version: bayesVersionOutOf(v, m.CurrentVersion),
		Compile: bayesCompileOutOf(state), Uses: len(flows)}, true, nil
}

// bayesSendDetail answers with the model as it is now (its current version).
func bayesSendDetail(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, id int64, status int) {
	ctx := r.Context()
	m, err := bayesmodels.Get(ctx, d.DB, id)
	if err != nil || m == nil {
		fail(d, w, "bayes models: reading the model again", fmt.Errorf("model %d: %v", id, err))
		return
	}
	det, ok, err := bayesDetailOf(ctx, d, perm.For(d.DB, &p.User), m, 0)
	if err != nil || !ok {
		fail(d, w, "bayes models: the model's detail", fmt.Errorf("model %d: %v", id, err))
		return
	}
	server.JSON(w, status, det)
}

// bayesFailure answers what the store refused; false when it is not a refusal (a failure for the
// caller to log). nameClash is the 409's text for a name taken at the same moment.
func bayesFailure(d *Deps, w http.ResponseWriter, what string, err error, nameClash string) {
	var refused *bayesmodels.Refused
	var stale *bayesmodels.Stale
	var inUse *bayesmodels.InUse
	switch {
	case errors.As(err, &refused):
		fs := refused.Findings
		if fs == nil {
			fs = []bayesgraph.Finding{}
		}
		server.JSON(w, http.StatusUnprocessableEntity, map[string]any{
			"detail": "The graph has errors and cannot be saved", "findings": fs})
	case errors.As(err, &stale):
		server.JSON(w, http.StatusConflict, map[string]any{"detail": stale.Error(), "current_version": stale.Current})
	case errors.As(err, &inUse):
		names := make([]string, len(inUse.Flows))
		for i, f := range inUse.Flows {
			names[i] = "flow «" + f + "»"
		}
		more := ""
		if inUse.More > 0 {
			more = " …"
		}
		server.JSON(w, http.StatusConflict, map[string]any{
			"detail": "The Bayesian model is used by " + strings.Join(names, ", ") + more + ": remove it from there first",
			"flows":  inUse.Flows, "more": inUse.More})
	case errors.Is(err, bayesmodels.ErrNotFound):
		detail(w, http.StatusNotFound, bayesmodels.NotFoundDetail)
	case errors.Is(err, bayesmodels.ErrTemplate):
		detail(w, http.StatusUnprocessableEntity, "Unknown template")
	case copynames.Clash(err):
		detail(w, http.StatusConflict, nameClash)
	default:
		// ErrGenerate is a bug of the generator: logged with its rule, answered as any failure
		fail(d, w, what, err)
	}
}

const bayesNameClash = "There is already a Bayesian model with that name in this folder"

// ── lists ─────────────────────────────────────────────────────────────────────

func bayesMany(d *Deps, w http.ResponseWriter, r *http.Request, c *perm.Checker, usable bool, projects []int64) {
	ctx := r.Context()
	rows, err := bayesmodels.Visible(ctx, c, d.DB, projects, usable)
	if err != nil {
		fail(d, w, "bayes models: the list", err)
		return
	}
	rights := map[int64][2]bool{}
	out := make([]bayesModelOut, 0, len(rows))
	for i := range rows {
		o, err := bayesOutOf(ctx, c, &rows[i].Model, rows[i].Family, rights)
		if err != nil {
			fail(d, w, "bayes models: a model of the list", err)
			return
		}
		out = append(out, o)
	}
	server.JSON(w, http.StatusOK, out)
}

// listBayesModels is GET /bayes-models: the models this person sees, by name.
func listBayesModels(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	c := perm.For(d.DB, &p.User)
	usable, err := bayesUsable(r.Context(), d, c, p)
	if err != nil {
		fail(d, w, "bayes models: the group's use", err)
		return
	}
	bayesMany(d, w, r, c, usable, nil)
}

// listProjectBayesModels is GET /projects/{project_id}/bayes-models.
func listProjectBayesModels(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	exists, err := scFolderExists(ctx, d.DB, ids[0])
	if err != nil {
		fail(d, w, "bayes models: the folder", err)
		return
	}
	if !exists {
		detail(w, http.StatusNotFound, "Folder not found")
		return
	}
	c := perm.For(d.DB, &p.User)
	if !scEnsure(d, w, ctx, c, ids[0], perm.View) {
		return
	}
	usable, err := bayesUsable(ctx, d, c, p)
	if err != nil {
		fail(d, w, "bayes models: the group's use", err)
		return
	}
	bayesMany(d, w, r, c, usable, []int64{ids[0]})
}

// getBayesModel is GET /bayes-models/{model_id}: the model and its current version.
func getBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	det, found, err := bayesDetailOf(r.Context(), d, c, m, 0)
	if err != nil {
		fail(d, w, "bayes models: the model's detail", err)
		return
	}
	if !found {
		detail(w, http.StatusNotFound, bayesmodels.NotFoundDetail)
		return
	}
	server.JSON(w, http.StatusOK, det)
}

// ── check ─────────────────────────────────────────────────────────────────────

// checkBayesModel is POST /bayes-models/check: {graph} → the findings and, with no error, the program
// and what goes with it; nothing is saved.
func checkBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	usable, err := bayesUsable(ctx, d, c, p)
	if err != nil {
		fail(d, w, "bayes models: the group's use", err)
		return
	}
	if !usable {
		detail(w, http.StatusForbidden, bmNotAvailable)
		return
	}
	in, ok := bayesBody(w, r, "graph")
	if !ok {
		return
	}
	if !in.hasGraph() {
		detail(w, http.StatusUnprocessableEntity, "The request needs a graph")
		return
	}
	content, fs, err := bayesmodels.Prepare(in.Graph)
	var refused *bayesmodels.Refused
	if err != nil && !errors.As(err, &refused) {
		fail(d, w, "bayes models: checking a graph", err)
		return
	}
	if fs == nil {
		fs = []bayesgraph.Finding{}
	}
	out := map[string]any{"ok": err == nil, "findings": fs}
	if err == nil {
		out["program"] = content.Program
		out["program_sha256"] = content.ProgramSHA256
		out["mapping"] = json.RawMessage(content.Mapping)
		out["data_manifest"] = json.RawMessage(content.DataManifest)
		out["quantity_manifest"] = json.RawMessage(content.QuantityManifest)
		out["codegen_version"] = content.CodegenVersion
		out["family"] = content.Family
		out["semantic_hash"] = content.SemanticHash
	}
	server.JSON(w, http.StatusOK, out)
}

// ── create, change, copy, delete ──────────────────────────────────────────────

// createBayesModel is POST /projects/{project_id}/bayes-models.
func createBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "project_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	usable, err := bayesUsable(ctx, d, c, p)
	if err != nil {
		fail(d, w, "bayes models: the group's use", err)
		return
	}
	if !usable {
		detail(w, http.StatusForbidden, bmNotAvailable)
		return
	}
	if !bayesFolderEdit(d, w, ctx, c, ids[0], "Folder not found") {
		return
	}
	in, ok := bayesBody(w, r, "name", "description", "graph", "template_id", "template_version")
	if !ok {
		return
	}
	if in.Name == nil || !in.hasGraph() {
		detail(w, http.StatusUnprocessableEntity, "The request needs a name and a graph")
		return
	}
	name, refusal, err := scObjectName(ctx, d.DB, "bayes_models", "Bayesian model", "Bayesian model", ids[0], *in.Name, 0)
	if scAnswer(d, w, "bayes models: the name", refusal, err) {
		return
	}
	description := ""
	if in.Description != nil {
		description = pyd.Strip(*in.Description)
	}
	saved, err := d.bayesStore().Create(ctx, bayesmodels.NewModel{ProjectID: ids[0], Name: name, Description: description,
		TemplateID: in.TemplateID, TemplateVersion: in.TemplateVersion, Graph: in.Graph}, p.User.ID)
	if err != nil {
		bayesFailure(d, w, "bayes models: creating the model", err, bayesNameClash)
		return
	}
	detailOf := pyjson.Object{{Key: "project_id", Value: ids[0]}, {Key: "version", Value: saved.Version},
		{Key: "warnings", Value: int64(len(saved.Warnings))}, {Key: "program_sha256", Value: saved.Content.ProgramSHA256}}
	if in.TemplateID != nil {
		detailOf = append(detailOf, pyjson.KV{Key: "template_id", Value: *in.TemplateID})
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.create", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(saved.Model.ID), TargetLabel: &saved.Model.Name, Detail: detailOf})
	bayesSendDetail(d, w, r, p, saved.Model.ID, http.StatusCreated)
}

// updateBayesModel is PATCH /bayes-models/{model_id}: a new version when the graph differs from the
// current one's; rename, description and move in the same transaction.
func updateBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, m.ProjectID, perm.Edit) {
		return
	}
	in, ok := bayesBody(w, r, "name", "description", "project_id", "graph", "base_version")
	if !ok {
		return
	}
	if in.hasGraph() && in.BaseVersion == nil {
		detail(w, http.StatusUnprocessableEntity, "A graph is saved with the base_version it was opened from")
		return
	}
	target := m.ProjectID
	if in.ProjectID != nil && *in.ProjectID != m.ProjectID {
		if !bayesFolderEdit(d, w, ctx, c, *in.ProjectID, "Destination folder not found") {
			return
		}
		target = *in.ProjectID
	}
	wanted := m.Name
	if in.Name != nil {
		wanted = *in.Name
	}
	name, refusal, err := scObjectName(ctx, d.DB, "bayes_models", "Bayesian model", "Bayesian model", target, wanted, m.ID)
	if scAnswer(d, w, "bayes models: the name", refusal, err) {
		return
	}
	description := m.Description
	if in.Description != nil {
		description = pyd.Strip(*in.Description)
	}
	changes := pyjson.Object{}
	if name != m.Name {
		changes = append(changes, pyjson.KV{Key: "name", Value: pyjson.Object{{Key: "from", Value: m.Name}, {Key: "to", Value: name}}})
	}
	if target != m.ProjectID {
		changes = append(changes, pyjson.KV{Key: "project_id", Value: pyjson.Object{{Key: "from", Value: m.ProjectID}, {Key: "to", Value: target}}})
	}
	if description != m.Description {
		changes = append(changes, pyjson.KV{Key: "description", Value: true})
	}
	meta := func(tx pgx.Tx) error {
		if len(changes) == 0 {
			return nil
		}
		_, err := tx.Exec(ctx, `UPDATE bayes_models SET name = $2, description = $3, project_id = $4, updated_at = $5 WHERE id = $1`,
			m.ID, name, description, target, time.Now().UTC())
		return err
	}
	if !in.hasGraph() {
		if len(changes) == 0 {
			bayesSendDetail(d, w, r, p, m.ID, http.StatusOK)
			return
		}
		if err := scInTx(ctx, d, meta); err != nil {
			bayesFailure(d, w, "bayes models: changing the model", err, bayesNameClash)
			return
		}
	} else {
		saved, err := d.bayesStore().Save(ctx, m.ID, *in.BaseVersion, in.Graph, p.User.ID, meta)
		if err != nil {
			bayesFailure(d, w, "bayes models: saving the model", err, bayesNameClash)
			return
		}
		if saved.Written {
			changes = append(changes, pyjson.KV{Key: "version", Value: saved.Version},
				pyjson.KV{Key: "warnings", Value: int64(len(saved.Warnings))},
				pyjson.KV{Key: "semantic_changed", Value: saved.SemanticChanged},
				pyjson.KV{Key: "program_sha256", Value: saved.Content.ProgramSHA256})
		}
		if !saved.Written && len(changes) == 0 {
			bayesSendDetail(d, w, r, p, m.ID, http.StatusOK)
			return
		}
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.update", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(m.ID), TargetLabel: &name, Detail: changes})
	bayesSendDetail(d, w, r, p, m.ID, http.StatusOK)
}

// duplicateBayesModel is POST /bayes-models/{model_id}/duplicate: a copy in the same folder.
func duplicateBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	src, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, src.ProjectID, perm.Edit) {
		return
	}
	saved, err := d.bayesStore().Duplicate(ctx, src, p.User.ID)
	if err != nil {
		bayesFailure(d, w, "bayes models: the copy", err, copynames.ClashDetail)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.create", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(saved.Model.ID), TargetLabel: &saved.Model.Name,
		Detail: pyjson.Object{{Key: "project_id", Value: src.ProjectID}, {Key: "duplicate_of", Value: src.ID},
			{Key: "program_sha256", Value: saved.Content.ProgramSHA256}}})
	bayesSendDetail(d, w, r, p, saved.Model.ID, http.StatusCreated)
}

// deleteBayesModel is DELETE /bayes-models/{model_id}: refused (409) while a flow uses it.
func deleteBayesModel(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, m.ProjectID, perm.Edit) {
		return
	}
	if err := d.bayesStore().Delete(ctx, m.ID); err != nil {
		bayesFailure(d, w, "bayes models: deleting the model", err, bayesNameClash)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.delete", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(m.ID), TargetLabel: scPtrStr(m.Name), Detail: pyjson.Object{{Key: "project_id", Value: m.ProjectID}}})
	returnsNone(w)
}

// ── versions ──────────────────────────────────────────────────────────────────

// listBayesModelVersions is GET /bayes-models/{model_id}/versions: newest first, no graph.
func listBayesModelVersions(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	m, _, ok := bayesVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	ctx := r.Context()
	rows, err := bayesmodels.Versions(ctx, d.DB, m.ID)
	if err != nil {
		fail(d, w, "bayes models: the versions", err)
		return
	}
	emails := map[int64]string{}
	var authors []int64
	for _, v := range rows {
		if v.CreatedBy != nil {
			authors = append(authors, *v.CreatedBy)
		}
	}
	if len(authors) > 0 {
		us, err := d.DB.Query(ctx, `SELECT id, email FROM users WHERE id = ANY($1)`, authors)
		if err != nil {
			fail(d, w, "bayes models: the authors", err)
			return
		}
		for us.Next() {
			var id int64
			var email string
			if err := us.Scan(&id, &email); err != nil {
				us.Close()
				fail(d, w, "bayes models: the authors", err)
				return
			}
			emails[id] = email
		}
		us.Close()
		if err := us.Err(); err != nil {
			fail(d, w, "bayes models: the authors", err)
			return
		}
	}
	out := make([]bayesVersionListed, 0, len(rows))
	for _, v := range rows {
		l := bayesVersionListed{Version: v.Version, Note: v.Note, CreatedAt: pyjson.Isoformat(v.CreatedAt), SemanticHash: v.SemanticHash,
			ProgramSHA256: v.ProgramSHA256, CodegenVersion: v.CodegenVersion, Family: v.Family, Current: v.Version == m.CurrentVersion}
		if v.CreatedBy != nil {
			if e, ok := emails[*v.CreatedBy]; ok {
				l.CreatedBy = &e
			}
		}
		out = append(out, l)
	}
	server.JSON(w, http.StatusOK, out)
}

// getBayesModelVersion is GET /bayes-models/{model_id}/versions/{version}: one version in full
// (0 is the current one).
func getBayesModelVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id", "version")
	if !ok {
		return
	}
	m, _, ok := bayesVisible(d, w, r, perm.For(d.DB, &p.User), p, ids[0])
	if !ok {
		return
	}
	v, err := bayesmodels.GetVersion(r.Context(), d.DB, m, ids[1])
	if err != nil {
		fail(d, w, "bayes models: the version", err)
		return
	}
	if v == nil {
		detail(w, http.StatusNotFound, "Version not found")
		return
	}
	server.JSON(w, http.StatusOK, bayesVersionOutOf(v, m.CurrentVersion))
}

// restoreBayesModelVersion is POST /bayes-models/{model_id}/versions/{version}/restore: a new version
// holding that one's graph and program as stored.
func restoreBayesModelVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, m.ProjectID, perm.Edit) {
		return
	}
	v, err := bayesmodels.GetVersion(ctx, d.DB, m, ids[1])
	if err != nil {
		fail(d, w, "bayes models: the version", err)
		return
	}
	if v == nil || ids[1] == 0 {
		detail(w, http.StatusNotFound, "Version not found")
		return
	}
	if v.Version == m.CurrentVersion {
		bayesSendDetail(d, w, r, p, m.ID, http.StatusOK)
		return
	}
	saved, err := d.bayesStore().Restore(ctx, m.ID, v.Version, p.User.ID)
	if err != nil {
		bayesFailure(d, w, "bayes models: restoring the version", err, bayesNameClash)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.restore", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(m.ID), TargetLabel: scPtrStr(m.Name),
		Detail: pyjson.Object{{Key: "from", Value: v.Version}, {Key: "version", Value: saved.Version},
			{Key: "semantic_changed", Value: saved.SemanticChanged}, {Key: "program_sha256", Value: saved.Content.ProgramSHA256}}})
	bayesSendDetail(d, w, r, p, m.ID, http.StatusOK)
}

// compileBayesModelVersion is POST /bayes-models/{model_id}/versions/{version}/compile: asks
// stan-compile for that version's program now (RUN); 409 while one is queued or running.
func compileBayesModelVersion(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id", "version")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok || !scEnsure(d, w, ctx, c, m.ProjectID, perm.Run) {
		return
	}
	v, err := bayesmodels.GetVersion(ctx, d.DB, m, ids[1])
	if err != nil {
		fail(d, w, "bayes models: the version", err)
		return
	}
	if v == nil || ids[1] == 0 {
		detail(w, http.StatusNotFound, "Version not found")
		return
	}
	compiler := d.bayesCompiler()
	state, err := compiler.State(ctx, v.ProgramSHA256)
	if err != nil {
		fail(d, w, "bayes models: the compile state", err)
		return
	}
	if state.State == bayesmodels.CompileQueued || state.State == bayesmodels.CompileRunning {
		detail(w, http.StatusConflict, "The program is already being compiled")
		return
	}
	if err := compiler.Enqueue(ctx, bayesmodels.CompileRequest{ModelID: m.ID, Version: v.Version,
		ProgramSHA256: v.ProgramSHA256, By: p.User.ID}); err != nil {
		fail(d, w, "bayes models: asking for the compile", err)
		return
	}
	if state, err = compiler.State(ctx, v.ProgramSHA256); err != nil {
		fail(d, w, "bayes models: the compile state", err)
		return
	}
	audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "bayes_model.compile", TargetType: scPtrStr("bayes_model"),
		TargetID: scPtrInt(m.ID), TargetLabel: scPtrStr(m.Name),
		Detail: pyjson.Object{{Key: "version", Value: v.Version}, {Key: "program_sha256", Value: v.ProgramSHA256}}})
	server.JSON(w, http.StatusAccepted, bayesCompileOutOf(state))
}

// ── uses ──────────────────────────────────────────────────────────────────────

type bayesUseOut struct {
	FlowID   int64  `json:"flow_id"`
	FlowName string `json:"flow_name"`
	NodeID   string `json:"node_id"`
	Version  int64  `json:"version"`
}

// listBayesModelUses is GET /bayes-models/{model_id}/uses: the flows that pin the model, each with
// its node and the version pinned; a flow this person cannot open is counted, not named.
func listBayesModelUses(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	ids, ok := pathInts(w, r, "model_id")
	if !ok {
		return
	}
	ctx := r.Context()
	c := perm.For(d.DB, &p.User)
	m, _, ok := bayesVisible(d, w, r, c, p, ids[0])
	if !ok {
		return
	}
	uses, err := bayesmodels.Using(ctx, d.DB, m.ID)
	if err != nil {
		fail(d, w, "bayes models: the uses", err)
		return
	}
	out := struct {
		Flows  []bayesUseOut `json:"flows"`
		Hidden int           `json:"hidden"`
	}{Flows: []bayesUseOut{}}
	openable := map[int64]bool{}
	hidden := map[int64]bool{}
	for _, u := range uses {
		ok, seen := openable[u.FlowProjectID]
		if !seen {
			if ok, err = c.Has(ctx, u.FlowProjectID, perm.View); err != nil {
				fail(d, w, "bayes models: a flow's folder", err)
				return
			}
			openable[u.FlowProjectID] = ok
		}
		if ok {
			out.Flows = append(out.Flows, bayesUseOut{FlowID: u.FlowID, FlowName: u.FlowName, NodeID: u.NodeID, Version: u.Version})
		} else {
			hidden[u.FlowID] = true
		}
	}
	out.Hidden = len(hidden)
	server.JSON(w, http.StatusOK, out)
}
