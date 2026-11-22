package api

// The licence of a group's weights (docs/specs/foundation-forecast.md, «The licence: enabling the
// group»), with no table of its own: the acceptance is the audit entry `models.licence_accept`, written
// in the transaction that queues the version it authorises, and each version of the group carries two
// comment lines in its `packages` text, the sizes it is built with and the acceptance it relied on:
//
//	# tabularia:weights 5m,28m
//	# tabularia:licence <sha256> accepted-by ada@example.com accepted-at 2026-11-21T10:14:03Z audit 81234
//
// The package parser drops everything after a `#`, so no other group's build changes. An
// administrator's click that queues a build carries the acceptance in its body; a build Tabularia
// queues itself (a new recipe) needs a valid acceptance still in the audit, else the group waits in
// `licence_pending`. A version that is ready runs whatever the audit holds now.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// mgLicenceAccept is the audit action that records an acceptance.
const mgLicenceAccept = "models.licence_accept"

// mgBuildIn is the body of Enable and Build again for a group with weights: the licence the person
// read (by its sha256), that they accept it, and the sizes to download.
type mgBuildIn struct {
	LicenceSHA256 string   `json:"licence_sha256"`
	Accept        bool     `json:"accept"`
	Sizes         []string `json:"sizes"`
}

// mgRefusal is an answer an administrator's request earns: the status and the sentence.
type mgRefusal struct {
	Status  int
	Message string
}

func (e *mgRefusal) Error() string { return e.Message }

// mgAcceptance is an acceptance as the audit or a version's line says it.
type mgAcceptance struct {
	SHA256 string
	By     string
	At     time.Time
	Audit  int64
	Sizes  []string
}

var mgLicenceLine = regexp.MustCompile(`(?m)^#[ \t]*tabularia:licence[ \t]+([0-9a-f]{64})[ \t]+accepted-by[ \t]+(\S+)[ \t]+accepted-at[ \t]+(\S+)[ \t]+audit[ \t]+(\d+)[ \t]*$`)

// mgPackagesText is the version's packages text of a group with weights: the recipe's packages, then
// the two lines.
func mgPackagesText(g modelgroups.Group, a *mgAcceptance) string {
	text := strings.Join(g.Recipe.Packages, "\n")
	if g.Recipe.Weights == nil || a == nil {
		return text
	}
	by := strings.Join(strings.Fields(a.By), "_")
	return text + "\n# tabularia:weights " + strings.Join(a.Sizes, ",") + "\n# tabularia:licence " + a.SHA256 +
		" accepted-by " + by + " accepted-at " + a.At.UTC().Format("2006-01-02T15:04:05Z") + " audit " + strconv.FormatInt(a.Audit, 10) + "\n"
}

// mgAcceptanceOfPackages reads the two lines of a version's packages text; nil when it has no licence line.
func mgAcceptanceOfPackages(text string) *mgAcceptance {
	m := mgLicenceLine.FindStringSubmatch(text)
	if m == nil {
		return nil
	}
	at, err := time.Parse(time.RFC3339, m[3])
	if err != nil {
		return nil
	}
	id, _ := strconv.ParseInt(m[4], 10, 64)
	sizes, _ := scriptimage.WeightsSizes(text)
	return &mgAcceptance{SHA256: m[1], By: m[2], At: at, Audit: id, Sizes: sizes}
}

// mgSizesOrdered keeps the sizes the recipe has, once each, from the smallest.
func mgSizesOrdered(g modelgroups.Group, sizes []string) []string {
	want := map[string]bool{}
	for _, s := range sizes {
		want[s] = true
	}
	var out []string
	for _, s := range modelgroups.WeightSizes {
		if _, has := g.Recipe.Weights.Sizes[s]; has && want[s] {
			out = append(out, s)
		}
	}
	return out
}

// mgAllSizes is every size the recipe has.
func mgAllSizes(g modelgroups.Group) []string {
	return mgSizesOrdered(g, modelgroups.WeightSizes)
}

// mgAuditAcceptance is the newest valid acceptance of the group in the audit: a `models.licence_accept`
// row for it whose licence_sha256 is the one this Tabularia ships and which the audit still holds. nil
// when there is none.
func mgAuditAcceptance(ctx context.Context, q perm.Querier, g modelgroups.Group) (*mgAcceptance, error) {
	if g.Recipe.Weights == nil {
		return nil, nil
	}
	sha := g.Recipe.Weights.Licence.SHA256
	rows, err := q.Query(ctx, `SELECT id, ts, actor_label, coalesce(detail, '') FROM audit_logs WHERE action = $1 AND outcome = 'success'
		AND target_type = 'model_group' AND target_label = $2 AND detail LIKE $3 ORDER BY id DESC`, mgLicenceAccept, g.ID, "%"+sha+"%")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var a mgAcceptance
		var detail string
		if err := rows.Scan(&a.Audit, &a.At, &a.By, &detail); err != nil {
			return nil, err
		}
		var d struct {
			SHA256 string   `json:"licence_sha256"`
			Sizes  []string `json:"sizes"`
		}
		if json.Unmarshal([]byte(detail), &d) != nil || d.SHA256 != sha {
			continue
		}
		a.SHA256, a.Sizes, a.At = d.SHA256, d.Sizes, a.At.UTC()
		return &a, nil
	}
	return nil, rows.Err()
}

// mgSizesInUse are the sizes the group's version in use (else its newest) was built with; every size
// when it has no line.
func mgSizesInUse(ctx context.Context, q perm.Querier, envID int64, g modelgroups.Group) ([]string, error) {
	var text string
	err := q.QueryRow(ctx, `SELECT coalesce(v.packages, '') FROM script_environment_versions v WHERE v.environment_id = $1
		ORDER BY (v.id = (SELECT current_version_id FROM script_environments WHERE id = $1)) DESC, v.number DESC LIMIT 1`, envID).Scan(&text)
	if err != nil && !scNoRows(err) {
		return nil, err
	}
	if sizes, ok := scriptimage.WeightsSizes(text); ok {
		if kept := mgSizesOrdered(g, sizes); len(kept) > 0 {
			return kept, nil
		}
	}
	return mgAllSizes(g), nil
}

// mgAutomaticAcceptance is what a build Tabularia queues itself relies on: the valid acceptance in the
// audit (its actor, its time, its row) with the sizes of the version in use. nil when there is none.
func mgAutomaticAcceptance(ctx context.Context, q perm.Querier, envID int64, g modelgroups.Group) (*mgAcceptance, error) {
	a, err := mgAuditAcceptance(ctx, q, g)
	if err != nil || a == nil {
		return nil, err
	}
	sizes, err := mgSizesInUse(ctx, q, envID, g)
	if err != nil {
		return nil, err
	}
	a.Sizes = sizes
	return a, nil
}

// mgLicenceAct is an administrator's click that queues a build of a group with weights.
type mgLicenceAct struct {
	in    mgBuildIn
	actor *auth.User
	log   *slog.Logger
}

// mgDecodeBuild reads the body of Enable or Build again; an empty one is an empty request.
func mgDecodeBuild(w http.ResponseWriter, r *http.Request) (mgBuildIn, bool) {
	var in mgBuildIn
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<16))
	if err != nil || len(bytes.TrimSpace(body)) == 0 {
		return in, err == nil
	}
	if json.Unmarshal(body, &in) != nil {
		detail(w, http.StatusUnprocessableEntity, "The request is not valid JSON, or not of the expected shape")
		return in, false
	}
	return in, true
}

// accept checks the click's acceptance and writes it in the audit, in the transaction that is about to
// queue the version, and says what the version's lines record. A refusal is a *mgRefusal.
func (a *mgLicenceAct) accept(ctx context.Context, tx pgx.Tx, g modelgroups.Group) (*mgAcceptance, error) {
	l := g.Recipe.Weights.Licence
	if !a.in.Accept {
		return nil, &mgRefusal{http.StatusConflict, "enabling this group requires accepting the licence of its weights"}
	}
	if a.in.LicenceSHA256 != l.SHA256 {
		return nil, &mgRefusal{http.StatusConflict, "the licence shown is not the one this Tabularia ships: reload the page"}
	}
	sizes := mgSizesOrdered(g, a.in.Sizes)
	seen := map[string]bool{}
	for _, s := range a.in.Sizes {
		if _, has := g.Recipe.Weights.Sizes[s]; !has || seen[s] {
			return nil, &mgRefusal{http.StatusUnprocessableEntity, fmt.Sprintf("sizes: «%s» is not a size of the weights, or is given twice (%s)", s, strings.Join(mgAllSizes(g), ", "))}
		}
		seen[s] = true
	}
	if len(sizes) == 0 {
		return nil, &mgRefusal{http.StatusUnprocessableEntity, "sizes: at least one size of the weights is to be downloaded"}
	}
	now := time.Now().UTC().Truncate(time.Second)
	id := audit.RecordID(ctx, tx, a.log, audit.Event{Actor: a.actor, Action: mgLicenceAccept, TargetType: scPtrStr("model_group"), TargetLabel: &g.ID, At: &now,
		Detail: pyjson.Object{{Key: "licence_name", Value: l.Name}, {Key: "licence_sha256", Value: l.SHA256}, {Key: "sizes", Value: sizes},
			{Key: "recipe_hash", Value: g.RecipeHash}}})
	if id == 0 {
		return nil, errors.New("the acceptance of the licence could not be recorded")
	}
	return &mgAcceptance{SHA256: l.SHA256, By: a.actor.Email, At: now, Audit: id, Sizes: sizes}, nil
}

// mgAnswerRefusal sends a *mgRefusal; false for any other error.
func mgAnswerRefusal(w http.ResponseWriter, err error) bool {
	var ref *mgRefusal
	if errors.As(err, &ref) {
		detail(w, ref.Status, ref.Message)
		return true
	}
	return false
}

// ── what the panel reads ─────────────────────────────────────────────────────

// mgLicenceSize is a size of the weights and what it takes on disk.
type mgLicenceSize struct {
	ID    string `json:"id"`
	Bytes int64  `json:"bytes"`
}

// mgLicenceLineOut is the acceptance a version was built under, read from its own lines.
type mgLicenceLineOut struct {
	Version int64    `json:"version"`
	SHA256  string   `json:"licence_sha256"`
	By      string   `json:"accepted_by"`
	At      string   `json:"accepted_at"`
	Audit   int64    `json:"audit"`
	Sizes   []string `json:"sizes"`
	// Current: the licence of the line is the one this Tabularia ships
	Current bool `json:"current"`
}

// mgLicenceOut is what a group with weights says of its licence.
type mgLicenceOut struct {
	Name   string          `json:"name"`
	SHA256 string          `json:"sha256"`
	URL    string          `json:"url"` // the text at the pinned revision, on the repository's host
	Text   string          `json:"text"`
	Sizes  []mgLicenceSize `json:"sizes"`
	// Pending: no valid acceptance in the audit, so no build starts by itself (state licence_pending)
	Pending bool `json:"pending"`
	// Line: the acceptance of the version in use (else the newest one that has one), from the version
	Line *mgLicenceLineOut `json:"line"`
	// AuditExpiresAt: when the audit's retention removes the valid acceptance (null: it keeps it); after it
	// the next build asks again, the version in use keeps running
	AuditExpiresAt *string `json:"audit_expires_at"`
	// SizesInUse: the sizes the version in use was built with (its `# tabularia:weights` line), the ones the box
	// ticks for Build again, so that a click does not download sizes nobody chose; empty without a version in use
	// (a group never built ticks every size)
	SizesInUse []string `json:"sizes_in_use"`
}

// mgLicenceView is the licence part of a group's answer; nil for a group without weights.
func mgLicenceView(s *mgState, retentionMinutes int) *mgLicenceOut {
	g := s.Group
	w := g.Recipe.Weights
	if w == nil {
		return nil
	}
	out := &mgLicenceOut{Name: w.Licence.Name, SHA256: w.Licence.SHA256, Text: g.LicenceText(), Sizes: []mgLicenceSize{}, Pending: s.Accepted == nil,
		SizesInUse: []string{}}
	if v := s.inUse(); v != nil {
		out.SizesInUse = mgSizesOfVersion(g, v)
	}
	for _, id := range mgAllSizes(g) {
		out.Sizes = append(out.Sizes, mgLicenceSize{ID: id, Bytes: w.Sizes[id].Files["model.safetensors"].Bytes})
	}
	if first := mgAllSizes(g); len(first) > 0 {
		out.URL = mgLicenceURL(g, first[0])
	}
	// the version in use first, then the newest: the line survives the audit's retention
	pick := func(v *mgVersion) bool {
		a := mgAcceptanceOfPackages(v.PackagesText)
		if a == nil {
			return false
		}
		sizes := a.Sizes
		if sizes == nil {
			sizes = []string{}
		}
		out.Line = &mgLicenceLineOut{Version: v.Number, SHA256: a.SHA256, By: a.By, At: pyjson.Isoformat(a.At), Audit: a.Audit, Sizes: sizes,
			Current: a.SHA256 == w.Licence.SHA256}
		return true
	}
	done := false
	for i := range s.Versions {
		if v := &s.Versions[i]; s.CurrentID != nil && v.ID == *s.CurrentID {
			done = pick(v)
		}
	}
	for i := 0; i < len(s.Versions) && !done; i++ {
		done = pick(&s.Versions[i])
	}
	if s.Accepted != nil && retentionMinutes > 0 {
		at := pyjson.Isoformat(s.Accepted.At.Add(time.Duration(retentionMinutes) * time.Minute))
		out.AuditExpiresAt = &at
	}
	return out
}

// mgSizesOfVersion are the sizes of the weights a version holds: its `# tabularia:weights` line (every
// size the recipe has when it has none).
func mgSizesOfVersion(g modelgroups.Group, v *mgVersion) []string {
	if sizes, ok := scriptimage.WeightsSizes(v.PackagesText); ok {
		if kept := mgSizesOrdered(g, sizes); len(kept) > 0 {
			return kept
		}
	}
	return mgAllSizes(g)
}

// mgLicenceURL is the licence's text on the repository's host at the revision of a size's weights: the file the weights
// were downloaded with, never a moving branch.
func mgLicenceURL(g modelgroups.Group, size string) string {
	z := g.Recipe.Weights.Sizes[size]
	return modelgroups.OfficialWeightsBaseURL + z.Repo + "/blob/" + z.Revision + "/LICENSE"
}

// inUse is the version the group's environment serves (its current version), whatever its recipe: nil when none.
func (s *mgState) inUse() *mgVersion {
	for i := range s.Versions {
		if v := &s.Versions[i]; s.CurrentID != nil && v.ID == *s.CurrentID {
			return v
		}
	}
	return nil
}

// mgPaletteLicence is what the node's form says of a group's licence under the size: its name and the licence's text
// at the pinned revision of each size the version in use holds (`url`: the first one's).
type mgPaletteLicence struct {
	Name string            `json:"name"`
	URL  string            `json:"url"`
	URLs map[string]string `json:"urls"`
}

func mgPaletteLicenceOf(g modelgroups.Group, sizes []string) *mgPaletteLicence {
	if g.Recipe.Weights == nil {
		return nil
	}
	if len(sizes) == 0 {
		sizes = mgAllSizes(g)
	}
	out := &mgPaletteLicence{Name: g.Recipe.Weights.Licence.Name, URLs: map[string]string{}}
	for _, z := range sizes {
		if _, has := g.Recipe.Weights.Sizes[z]; has {
			out.URLs[z] = mgLicenceURL(g, z)
			if out.URL == "" {
				out.URL = out.URLs[z]
			}
		}
	}
	return out
}
