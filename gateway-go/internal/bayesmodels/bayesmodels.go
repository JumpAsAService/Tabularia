// Package bayesmodels is the store of the Bayesian editor's saved models
// (docs/specs/bayes-model-store.md): a model is an object of a folder, as a saved script is,
// with versions written once and never again. Every version holds the graph as saved and the
// Stan program generated from it, with the program's sha256, the mapping table and the
// contract's two manifests: the program stored is the one that runs, and it is never generated
// again.
//
// Who sees a model: an administrator, or whoever has VIEW on its folder and the use of the
// group `bayes` (whoever lacks either gets the 404 of a model that does not exist). The group's
// use is the API's (ModelGroupUsable), read once per request and passed here as a bool.
//
// This file holds the rows, their reading and the visibility; save.go the saves, restore and
// duplicate; uses.go the flows that pin a model; compile.go the compiler's interface.
package bayesmodels

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
)

// Group is the model group whose use, with VIEW on the folder, lets a person see the models.
const Group = "bayes"

// NotFoundDetail is the 404's, the same for a model that does not exist and for one not seen.
const NotFoundDetail = "Bayesian model not found"

// Model is a row of bayes_models.
type Model struct {
	ID              int64
	Name            string
	Description     string
	ProjectID       int64
	OwnerID         *int64
	CurrentVersion  int64
	TemplateID      *string
	TemplateVersion *int64
	CreatedAt       time.Time
	UpdatedAt       time.Time
}

// Columns of bayes_models read into a Model.
const Columns = `id, name, description, project_id, owner_id, current_version, template_id, template_version,
	created_at, updated_at`

// Scan reads Columns.
func Scan(row pgx.Row) (*Model, error) {
	var m Model
	if err := row.Scan(&m.ID, &m.Name, &m.Description, &m.ProjectID, &m.OwnerID, &m.CurrentVersion, &m.TemplateID,
		&m.TemplateVersion, &m.CreatedAt, &m.UpdatedAt); err != nil {
		return nil, err
	}
	return &m, nil
}

// Get reads a model; nil when there is none.
func Get(ctx context.Context, q perm.Querier, id int64) (*Model, error) {
	m, err := Scan(q.QueryRow(ctx, `SELECT `+Columns+` FROM bayes_models WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return m, err
}

// Content is what a version stores beside its number: the graph and everything generated from
// it, written once. Graph is bayesgraph.Canonical with the layout; the JSON texts are compact.
type Content struct {
	Graph            string
	FormatVersion    int
	SemanticHash     string
	Findings         string // JSON: the validator's findings at save (warnings only)
	Program          string
	ProgramSHA256    string
	Mapping          string // JSON: the mapping table
	DataManifest     string // JSON: bayes-contract's data manifest
	QuantityManifest string // JSON: bayes-contract's quantity manifest
	CodegenVersion   int
	Family           string // FamilyHierarchical, FamilySeries, FamilyGLM, FamilyFree
	CatalogSHA256    string
}

// Version is a row of bayes_model_versions.
type Version struct {
	ModelID   int64
	Version   int64
	Note      string
	CreatedAt time.Time
	CreatedBy *int64
	Content
}

const versionColumns = `model_id, version, note, created_at, created_by, graph, format_version, semantic_hash, findings,
	program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version, family, catalog_sha256`

func scanVersion(row pgx.Row) (*Version, error) {
	var v Version
	if err := row.Scan(&v.ModelID, &v.Version, &v.Note, &v.CreatedAt, &v.CreatedBy, &v.Graph, &v.FormatVersion,
		&v.SemanticHash, &v.Findings, &v.Program, &v.ProgramSHA256, &v.Mapping, &v.DataManifest, &v.QuantityManifest,
		&v.CodegenVersion, &v.Family, &v.CatalogSHA256); err != nil {
		return nil, err
	}
	return &v, nil
}

// GetVersion reads one version in full (`version` 0: the current one); nil when there is none.
func GetVersion(ctx context.Context, q perm.Querier, m *Model, version int64) (*Version, error) {
	if version == 0 {
		version = m.CurrentVersion
	}
	v, err := scanVersion(q.QueryRow(ctx, `SELECT `+versionColumns+` FROM bayes_model_versions
		WHERE model_id = $1 AND version = $2`, m.ID, version))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return v, err
}

// VersionInfo is a version as the list of versions shows it: never the graph nor the program.
type VersionInfo struct {
	Version        int64
	Note           string
	CreatedAt      time.Time
	CreatedBy      *int64
	SemanticHash   string
	ProgramSHA256  string
	CodegenVersion int
	Family         string
}

// Versions lists a model's versions, newest first.
func Versions(ctx context.Context, q perm.Querier, modelID int64) ([]VersionInfo, error) {
	rows, err := q.Query(ctx, `SELECT version, note, created_at, created_by, semantic_hash, program_sha256, codegen_version,
		family FROM bayes_model_versions WHERE model_id = $1 ORDER BY version DESC`, modelID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []VersionInfo{}
	for rows.Next() {
		var v VersionInfo
		if err := rows.Scan(&v.Version, &v.Note, &v.CreatedAt, &v.CreatedBy, &v.SemanticHash, &v.ProgramSHA256,
			&v.CodegenVersion, &v.Family); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// CanSee: an administrator sees every model (the group disabled or not); anyone else needs the
// group's use (`groupUsable`, the API's ModelGroupUsable for Group) and VIEW on the folder.
func CanSee(ctx context.Context, c *perm.Checker, m *Model, groupUsable bool) (bool, error) {
	if admin, err := c.IsAdmin(ctx); err != nil || admin {
		return admin, err
	}
	if !groupUsable {
		return false, nil
	}
	return c.Has(ctx, m.ProjectID, perm.View)
}

// CanRun: seen, and RUN on the folder. flow-node asks it at every run with the run's authority
// (the person who ran the flow, or the schedule's author) and that person's use of the group.
func CanRun(ctx context.Context, c *perm.Checker, m *Model, groupUsable bool) (bool, error) {
	ok, err := CanSee(ctx, c, m, groupUsable)
	if err != nil || !ok {
		return false, err
	}
	return c.Has(ctx, m.ProjectID, perm.Run)
}

// CanEdit: seen, and EDIT on the folder (save, rename, describe, restore, duplicate, delete).
func CanEdit(ctx context.Context, c *perm.Checker, m *Model, groupUsable bool) (bool, error) {
	ok, err := CanSee(ctx, c, m, groupUsable)
	if err != nil || !ok {
		return false, err
	}
	return c.Has(ctx, m.ProjectID, perm.Edit)
}

// NotFound is the 404 of a model that does not exist or is not seen: one body for both.
func NotFound() *auth.Refusal {
	return &auth.Refusal{Status: http.StatusNotFound, Detail: NotFoundDetail}
}

// GetVisible reads a model this person sees, or the 404.
func GetVisible(ctx context.Context, c *perm.Checker, q perm.Querier, id int64, groupUsable bool) (*Model, *auth.Refusal, error) {
	m, err := Get(ctx, q, id)
	if err != nil {
		return nil, nil, err
	}
	if m == nil {
		return nil, NotFound(), nil
	}
	ok, err := CanSee(ctx, c, m, groupUsable)
	if err != nil {
		return nil, nil, err
	}
	if !ok {
		return nil, NotFound(), nil
	}
	return m, nil, nil
}

// Listed is a model in a list, with its current version's family.
type Listed struct {
	Model
	Family string
}

// Visible lists the models this person sees (in `projects` when not nil), by name; never a
// graph or a program.
func Visible(ctx context.Context, c *perm.Checker, q perm.Querier, projects []int64, groupUsable bool) ([]Listed, error) {
	admin, err := c.IsAdmin(ctx)
	if err != nil {
		return nil, err
	}
	where, args := "", []any{}
	switch {
	case admin && projects != nil:
		where, args = ` WHERE m.project_id = ANY($1)`, []any{projects}
	case !admin:
		if !groupUsable {
			return nil, nil
		}
		readable, err := c.Readable(ctx)
		if err != nil {
			return nil, err
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
		if len(readable) == 0 {
			return nil, nil
		}
		where, args = ` WHERE m.project_id = ANY($1)`, []any{perm.Sorted(readable)}
	}
	rows, err := q.Query(ctx, `SELECT m.id, m.name, m.description, m.project_id, m.owner_id, m.current_version, m.template_id,
		m.template_version, m.created_at, m.updated_at, coalesce(v.family, '')
		FROM bayes_models m LEFT JOIN bayes_model_versions v ON v.model_id = m.id AND v.version = m.current_version`+
		where+` ORDER BY m.name, m.id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Listed
	for rows.Next() {
		var l Listed
		if err := rows.Scan(&l.ID, &l.Name, &l.Description, &l.ProjectID, &l.OwnerID, &l.CurrentVersion, &l.TemplateID,
			&l.TemplateVersion, &l.CreatedAt, &l.UpdatedAt, &l.Family); err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, rows.Err()
}
