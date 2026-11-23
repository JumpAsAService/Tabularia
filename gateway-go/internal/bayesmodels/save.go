package bayesmodels

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayestemplates"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/copynames"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
)

// Notes of a version: stable English codes the interface translates.
const (
	NoteCreated = "created"
	NoteEdited  = "edited"
	NoteLayout  = "layout"
)

// NoteRestored is the note of a version restored from `from`.
func NoteRestored(from int64) string { return fmt.Sprintf("restored from v%d", from) }

// NoteDuplicated is the note of a copy's first version.
func NoteDuplicated(name string, from int64) string {
	return fmt.Sprintf("duplicated from «%s» v%d", name, from)
}

// Lock is the first key of the advisory lock a model is deleted under (the second is its id): a
// flow that pins a model takes it too, so that a model is never deleted under a flow being saved.
const Lock = 74610046

var (
	// ErrGenerate: the generator refused a model the validator accepted, a bug of the generator
	// (the save answers 500 and the log says which rule; nothing is stored).
	ErrGenerate = errors.New("bayes models: the program could not be generated")
	// ErrNotFound: the model, or the version asked for, is not there (deleted meanwhile).
	ErrNotFound = errors.New("bayes models: not found")
	// ErrTemplate: a template the gateway does not know.
	ErrTemplate = errors.New("bayes models: unknown template")
)

// Refused is a graph that cannot be saved: it does not decode, or the validator found an error.
// Findings are the gateway's, sorted; the save answers 422 with them and writes no version.
type Refused struct {
	Findings []bayesgraph.Finding
}

func (r *Refused) Error() string {
	return fmt.Sprintf("bayes models: the graph has %d findings with an error", r.errors())
}

func (r *Refused) errors() int {
	n := 0
	for _, f := range r.Findings {
		if f.Severity == bayesgraph.Error {
			n++
		}
	}
	return n
}

// Stale is a save made on a version that is no longer the current one (409).
type Stale struct {
	Current int64
}

func (s *Stale) Error() string {
	return fmt.Sprintf("version %d was saved by someone else meanwhile: reload to see it", s.Current)
}

// InUse is a delete refused because flows pin the model (409).
type InUse struct {
	Flows []string // their names, sorted, the first ten
	More  int      // how many more
}

func (u *InUse) Error() string {
	return fmt.Sprintf("bayes models: used by %d flows", len(u.Flows)+u.More)
}

// Prepare reads a graph as the browser sent it and writes what a version stores: the graph decoded
// strictly (an older format upgraded), validated, its program generated, its canonical form and
// hashes. A graph that does not decode, or with an error among its findings, gives *Refused with the
// findings; a generator's failure on a valid model gives ErrGenerate. The findings returned with a
// Content are its warnings (possibly none). It is also POST /bayes-models/check: nothing is stored.
func Prepare(graph []byte) (*Content, []bayesgraph.Finding, error) {
	g, fs := bayesgraph.Decode(graph)
	if g == nil {
		return nil, fs, &Refused{Findings: fs}
	}
	m := bayesgraph.BuildModel(g)
	fs = m.Findings
	if fs == nil {
		fs = []bayesgraph.Finding{}
	}
	if !m.Valid() {
		return nil, fs, &Refused{Findings: fs}
	}
	p, err := bayescodegen.Generate(m, nil)
	if err != nil {
		return nil, fs, fmt.Errorf("%w: %w", ErrGenerate, err)
	}
	canonical, err := bayesgraph.Canonical(g)
	if err != nil {
		return nil, fs, err
	}
	hash, err := bayesgraph.SemanticHash(g)
	if err != nil {
		return nil, fs, err
	}
	sum := sha256.Sum256(p.Text)
	if hex.EncodeToString(sum[:]) != p.SHA256 {
		return nil, fs, fmt.Errorf("%w: the program's sha256 is not its text's", ErrGenerate)
	}
	c := &Content{
		Graph:          string(canonical),
		FormatVersion:  g.FormatVersion,
		SemanticHash:   hash,
		Program:        string(p.Text),
		ProgramSHA256:  p.SHA256,
		CodegenVersion: p.CodegenVersion,
		Family:         Family(m),
		CatalogSHA256:  catalogSHA256(),
	}
	for _, x := range []struct {
		dst *string
		v   any
	}{{&c.Findings, fs}, {&c.Mapping, orEmpty(p.Mapping)}, {&c.DataManifest, orEmpty(p.Data)},
		{&c.QuantityManifest, orEmpty(p.Quantities)}} {
		b, err := json.Marshal(x.v)
		if err != nil {
			return nil, fs, err
		}
		*x.dst = string(b)
	}
	return c, fs, nil
}

func orEmpty[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

func catalogSHA256() string {
	sum := sha256.Sum256(bayescatalog.Raw())
	return hex.EncodeToString(sum[:])
}

// Unchanged reports that a prepared graph is what the current version already stores: the same
// canonical bytes, layout included, written by the same generator. Saving it writes nothing. A graph
// whose program the current generator writes differently (a version saved by an older generator,
// or restored from one) is not unchanged: saving it again writes it with the current generator.
func Unchanged(current *Version, c *Content) bool {
	return current != nil && current.Graph == c.Graph && current.CodegenVersion == c.CodegenVersion
}

// noteOf is the note of a version that follows `current`: layout when only the layout moved and
// the program is the same bytes, edited otherwise.
func noteOf(current *Version, c *Content) string {
	if current != nil && current.SemanticHash == c.SemanticHash && current.ProgramSHA256 == c.ProgramSHA256 {
		return NoteLayout
	}
	return NoteEdited
}

// DB is the pool the store writes through.
type DB interface {
	perm.Querier
	Begin(ctx context.Context) (pgx.Tx, error)
}

// Store writes models and their versions. Compiler is asked for a version's compile after its
// transaction has committed (nil: never asked).
type Store struct {
	DB       DB
	Compiler Compiler
	Log      *slog.Logger
}

// Saved is what a write did.
type Saved struct {
	Model    *Model
	Version  int64 // the version written, or the current one when nothing was written
	Written  bool  // false: the graph was the current version's, nothing was stored
	Note     string
	Content  *Content
	Warnings []bayesgraph.Finding // the gateway's findings: warnings only
	Enqueued bool                 // the compile was asked for
	// SemanticChanged: the new version's semantic hash differs from the one it follows (the audit's).
	SemanticChanged bool
}

// NewModel is a model to create in a folder; its graph becomes version 1.
type NewModel struct {
	ProjectID       int64
	Name            string
	Description     string
	TemplateID      *string
	TemplateVersion *int64 // nil with a template: the template's own version
	Graph           []byte
}

// Create writes a model and its version 1 (note created) in one transaction. The name is the
// caller's to check (free in its folder: a clash is the database's unique error, copynames.Clash).
func (s *Store) Create(ctx context.Context, in NewModel, userID int64) (*Saved, error) {
	if in.TemplateID != nil {
		t, ok := bayestemplates.Get(*in.TemplateID)
		if !ok {
			return nil, ErrTemplate
		}
		if in.TemplateVersion == nil {
			v := int64(t.Version)
			in.TemplateVersion = &v
		}
	} else {
		in.TemplateVersion = nil
	}
	c, fs, err := Prepare(in.Graph)
	if err != nil {
		return nil, err
	}
	out := &Saved{Version: 1, Written: true, Note: NoteCreated, Content: c, Warnings: fs, SemanticChanged: true}
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		now := time.Now().UTC()
		var id int64
		if err := tx.QueryRow(ctx, `INSERT INTO bayes_models (name, description, project_id, owner_id, current_version,
			template_id, template_version, created_at, updated_at) VALUES ($1, $2, $3, $4, 1, $5, $6, $7, $7) RETURNING id`,
			in.Name, in.Description, in.ProjectID, userID, in.TemplateID, in.TemplateVersion, now).Scan(&id); err != nil {
			return err
		}
		if err := insertVersion(ctx, tx, id, 1, c, NoteCreated, userID, now); err != nil {
			return err
		}
		m, err := Get(ctx, tx, id)
		out.Model = m
		return err
	})
	if err != nil {
		return nil, err
	}
	out.Enqueued = s.compile(ctx, out, userID)
	return out, nil
}

// Save writes a new version of a model from the graph the browser sent, made on version `base`
// (the version the editor opened). The graph is decoded, validated and generated again here, never
// taken from elsewhere; a graph with an error is *Refused; one equal to the current version's
// (Unchanged) writes nothing. Otherwise, under the model's row lock, a `base` that is not the
// current version is *Stale, and the next version is written and made the current one. `also`, when
// not nil, runs in the same transaction (a rename or a move made with the save), whether a version
// is written or not. The compile is asked for after the commit.
func (s *Store) Save(ctx context.Context, modelID, base int64, graph []byte, userID int64, also func(pgx.Tx) error) (*Saved, error) {
	c, fs, err := Prepare(graph)
	if err != nil {
		return nil, err
	}
	out := &Saved{Content: c, Warnings: fs}
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var current int64
		err := tx.QueryRow(ctx, `SELECT current_version FROM bayes_models WHERE id = $1 FOR UPDATE`, modelID).Scan(&current)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		prev, err := scanVersion(tx.QueryRow(ctx, `SELECT `+versionColumns+` FROM bayes_model_versions
			WHERE model_id = $1 AND version = $2`, modelID, current))
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if errors.Is(err, pgx.ErrNoRows) {
			prev = nil
		}
		if Unchanged(prev, c) {
			out.Version, out.Content = current, &prev.Content
		} else {
			if base != current {
				return &Stale{Current: current}
			}
			out.Version, out.Written, out.Note = current+1, true, noteOf(prev, c)
			out.SemanticChanged = prev == nil || prev.SemanticHash != c.SemanticHash
			now := time.Now().UTC()
			if err := insertVersion(ctx, tx, modelID, out.Version, c, out.Note, userID, now); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `UPDATE bayes_models SET current_version = $2, updated_at = $3 WHERE id = $1`,
				modelID, out.Version, now); err != nil {
				return err
			}
		}
		if also != nil {
			if err := also(tx); err != nil {
				return err
			}
		}
		m, err := Get(ctx, tx, modelID)
		out.Model = m
		return err
	})
	if err != nil {
		return nil, err
	}
	if out.Written {
		out.Enqueued = s.compile(ctx, out, userID)
	}
	return out, nil
}

// Restore writes a new version holding version `from`'s graph, program and everything generated
// with it, copied byte for byte by the database (owner, 2026-11-23): nothing is validated or
// generated again, so the restored version runs what the old one ran.
func (s *Store) Restore(ctx context.Context, modelID, from, userID int64) (*Saved, error) {
	out := &Saved{Written: true, Note: NoteRestored(from)}
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		var current int64
		err := tx.QueryRow(ctx, `SELECT current_version FROM bayes_models WHERE id = $1 FOR UPDATE`, modelID).Scan(&current)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		var prevHash string
		if err := tx.QueryRow(ctx, `SELECT semantic_hash FROM bayes_model_versions WHERE model_id = $1 AND version = $2`,
			modelID, current).Scan(&prevHash); err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		out.Version = current + 1
		now := time.Now().UTC()
		tag, err := tx.Exec(ctx, `INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash,
			findings, program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version, family,
			catalog_sha256, note, created_at, created_by)
			SELECT model_id, $3, graph, format_version, semantic_hash, findings, program, program_sha256, mapping,
				data_manifest, quantity_manifest, codegen_version, family, catalog_sha256, $4, $5, $6
			FROM bayes_model_versions WHERE model_id = $1 AND version = $2`,
			modelID, from, out.Version, out.Note, now, userID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return ErrNotFound
		}
		if _, err := tx.Exec(ctx, `UPDATE bayes_models SET current_version = $2, updated_at = $3 WHERE id = $1`,
			modelID, out.Version, now); err != nil {
			return err
		}
		m, err := Get(ctx, tx, modelID)
		if err != nil {
			return err
		}
		out.Model = m
		v, err := GetVersion(ctx, tx, m, out.Version)
		if err != nil {
			return err
		}
		out.Content = &v.Content
		out.SemanticChanged = prevHash != v.SemanticHash
		return nil
	})
	if err != nil {
		return nil, err
	}
	out.Enqueued = s.compile(ctx, out, userID)
	return out, nil
}

// Duplicate copies a model into its own folder under the first free copy name (copynames.Free):
// its version 1 is the source's current version copied byte for byte, program included (the
// compile cache answers), noted «duplicated from «name» vN». A name taken at the same moment is
// the database's unique error (copynames.Clash).
func (s *Store) Duplicate(ctx context.Context, src *Model, userID int64) (*Saved, error) {
	out := &Saved{Version: 1, Written: true, SemanticChanged: true}
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		m, err := Get(ctx, tx, src.ID)
		if err != nil {
			return err
		}
		if m == nil {
			return ErrNotFound
		}
		name, err := copynames.Free(ctx, tx, "bayes_models", m.ProjectID, m.Name)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		var id int64
		if err := tx.QueryRow(ctx, `INSERT INTO bayes_models (name, description, project_id, owner_id, current_version,
			template_id, template_version, created_at, updated_at) VALUES ($1, $2, $3, $4, 1, $5, $6, $7, $7) RETURNING id`,
			name, m.Description, m.ProjectID, userID, m.TemplateID, m.TemplateVersion, now).Scan(&id); err != nil {
			return err
		}
		out.Note = NoteDuplicated(m.Name, m.CurrentVersion)
		tag, err := tx.Exec(ctx, `INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash,
			findings, program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version, family,
			catalog_sha256, note, created_at, created_by)
			SELECT $3, 1, graph, format_version, semantic_hash, findings, program, program_sha256, mapping,
				data_manifest, quantity_manifest, codegen_version, family, catalog_sha256, $4, $5, $6
			FROM bayes_model_versions WHERE model_id = $1 AND version = $2`,
			m.ID, m.CurrentVersion, id, out.Note, now, userID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return ErrNotFound
		}
		if out.Model, err = Get(ctx, tx, id); err != nil {
			return err
		}
		v, err := GetVersion(ctx, tx, out.Model, 1)
		if err != nil {
			return err
		}
		out.Content = &v.Content
		return nil
	})
	if err != nil {
		return nil, err
	}
	out.Enqueued = s.compile(ctx, out, userID)
	return out, nil
}

// Delete removes a model and its versions in one transaction under its advisory lock (Lock), or
// refuses with *InUse while a flow pins it.
func (s *Store) Delete(ctx context.Context, modelID int64) error {
	return s.inTx(ctx, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1::integer, $2::integer)`, Lock, modelID); err != nil {
			return err
		}
		names, err := References(ctx, tx, modelID)
		if err != nil {
			return err
		}
		if len(names) > 0 {
			u := &InUse{Flows: names}
			if len(names) > 10 {
				u.Flows, u.More = names[:10], len(names)-10
			}
			return u
		}
		if _, err := tx.Exec(ctx, `DELETE FROM bayes_model_versions WHERE model_id = $1`, modelID); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `DELETE FROM bayes_models WHERE id = $1`, modelID)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return ErrNotFound
		}
		return nil
	})
}

func insertVersion(ctx context.Context, tx pgx.Tx, modelID, version int64, c *Content, note string, userID int64, now time.Time) error {
	_, err := tx.Exec(ctx, `INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash,
		findings, program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version, family,
		catalog_sha256, note, created_at, created_by)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)`,
		modelID, version, c.Graph, c.FormatVersion, c.SemanticHash, c.Findings, c.Program, c.ProgramSHA256, c.Mapping,
		c.DataManifest, c.QuantityManifest, c.CodegenVersion, c.Family, c.CatalogSHA256, note, now, userID)
	return err
}

func (s *Store) inTx(ctx context.Context, f func(pgx.Tx) error) error {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// compile asks for a committed version's compile (afterCommit).
func (s *Store) compile(ctx context.Context, out *Saved, userID int64) bool {
	return afterCommit(ctx, s.Compiler, s.Log, CompileRequest{ModelID: out.Model.ID, Version: out.Version,
		ProgramSHA256: out.Content.ProgramSHA256, By: userID})
}
