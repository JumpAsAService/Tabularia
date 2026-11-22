package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/masking"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// The licence of a group's weights (models_licence.go): the acceptance in the audit, the version's two
// lines, licence_pending. docs/specs/foundation-forecast.md, «The licence: enabling the group».

const mgWeightsGroup = "foundation_forecast"

func weightsGroup(t *testing.T) modelgroups.Group {
	t.Helper()
	g, ok := modelgroups.Get(mgWeightsGroup)
	if !ok || g.Recipe.Weights == nil {
		t.Fatalf("the group %s with weights is not shipped", mgWeightsGroup)
	}
	return g
}

func acceptBody(g modelgroups.Group, accept bool, sizes ...string) string {
	b, _ := json.Marshal(mgBuildIn{LicenceSHA256: g.Recipe.Weights.Licence.SHA256, Accept: accept, Sizes: sizes})
	return string(b)
}

// ── without a database ───────────────────────────────────────────────────────

func TestAVersionsPackagesCarryTheSizesAndTheAcceptanceAndAreReadBack(t *testing.T) {
	g := weightsGroup(t)
	at := time.Date(2026, 11, 21, 10, 14, 3, 0, time.UTC)
	a := &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, By: "ada@example.com", At: at, Audit: 81234, Sizes: []string{"28m", "5m"}}
	text := mgPackagesText(g, a)
	if !strings.HasPrefix(text, strings.Join(g.Recipe.Packages, "\n")+"\n# tabularia:weights 28m,5m\n# tabularia:licence "+a.SHA256+
		" accepted-by ada@example.com accepted-at 2026-11-21T10:14:03Z audit 81234") {
		t.Errorf("the packages text:\n%s", text)
	}
	back := mgAcceptanceOfPackages(text)
	if back == nil || back.By != a.By || !back.At.Equal(at) || back.Audit != 81234 || back.SHA256 != a.SHA256 || strings.Join(back.Sizes, ",") != "28m,5m" {
		t.Errorf("read back: %+v", back)
	}
	if sizes, ok := scriptimage.WeightsSizes(text); !ok || strings.Join(sizes, ",") != "28m,5m" {
		t.Errorf("the build's line: %v %v", sizes, ok)
	}
	// the build's parser drops the lines: the packages are the recipe's
	parsed, err := sbParsePackages(g.Recipe.Language, text)
	if err != nil {
		t.Fatal(err)
	}
	plain, _ := sbParsePackages(g.Recipe.Language, strings.Join(g.Recipe.Packages, "\n"))
	if strings.Join(parsed, "|") != strings.Join(plain, "|") {
		t.Error("the lines changed the packages the build installs")
	}
	// every other group's text is the recipe's, whatever acceptance is given
	for _, o := range modelgroups.Groups() {
		if o.Recipe.Weights == nil && mgPackagesText(o, a) != strings.Join(o.Recipe.Packages, "\n") {
			t.Errorf("%s: the packages changed", o.ID)
		}
	}
}

func TestAGroupWithWeightsWithoutAnAcceptanceIsLicencePending(t *testing.T) {
	g := weightsGroup(t)
	cur := int64(7)
	old := "old"
	stale := []mgVersion{{ID: 7, Number: 7, Status: "ready", RecipeHash: &old}}
	pending := mgState{Group: g, Enabled: true, CurrentID: &cur, Versions: stale}
	if got := pending.state(); got != "licence_pending" {
		t.Errorf("a new recipe and no acceptance: %s", got)
	}
	if lic := mgLicenceView(&pending, 0); lic == nil || !lic.Pending || lic.Line != nil {
		t.Errorf("the view: %+v", lic)
	}
	at := time.Now()
	accepted := pending
	accepted.Accepted = &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, At: at}
	if got := accepted.state(); got != "rebuild_needed" {
		t.Errorf("with the acceptance: %s", got)
	}
	// the version in use, ready on the shipped recipe, is ready whatever the audit holds
	ready := []mgVersion{{ID: 7, Number: 7, Status: "ready", RecipeHash: &g.RecipeHash,
		PackagesText: mgPackagesText(g, &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, By: "ada@example.com", At: at, Audit: 5, Sizes: []string{"5m"}})}}
	still := mgState{Group: g, Enabled: true, CurrentID: &cur, Versions: ready}
	if got := still.state(); got != "ready" || still.current() == nil {
		t.Errorf("the audit row is gone and the version runs: %s", got)
	}
	if lic := mgLicenceView(&still, 0); lic == nil || !lic.Pending || lic.Line == nil || lic.Line.By != "ada@example.com" || lic.Line.Version != 7 || !lic.Line.Current {
		t.Errorf("the line stays: %+v", lic)
	}
	// the retention's warning: the acceptance leaves the audit at its time plus the retention
	still.Accepted = &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, At: time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC)}
	if lic := mgLicenceView(&still, 90); lic.AuditExpiresAt == nil || !strings.HasPrefix(*lic.AuditExpiresAt, "2026-11-21T11:30:00") {
		t.Errorf("the retention's warning: %v", lic.AuditExpiresAt)
	}
	if lic := mgLicenceView(&still, 0); lic.AuditExpiresAt != nil {
		t.Errorf("a warning with the audit kept forever: %v", *lic.AuditExpiresAt)
	}
	// a group without weights has no licence and is never licence_pending
	for _, o := range modelgroups.Groups() {
		if o.Recipe.Weights != nil {
			continue
		}
		s := mgState{Group: o, Enabled: true, CurrentID: &cur, Versions: stale}
		if s.state() != "rebuild_needed" || mgLicenceView(&s, 0) != nil {
			t.Errorf("%s: %s", o.ID, s.state())
		}
	}
}

// ── on a database ────────────────────────────────────────────────────────────

func (f *modelsFixture) acceptances() []struct {
	ID     int64
	Detail string
	Actor  string
} {
	f.t.Helper()
	rows, err := f.db.Query(f.ctx, `SELECT id, coalesce(detail, ''), actor_label FROM audit_logs WHERE action = 'models.licence_accept' AND actor_id = $1 ORDER BY id`, f.admin.ID)
	if err != nil {
		f.t.Fatal(err)
	}
	defer rows.Close()
	var out []struct {
		ID     int64
		Detail string
		Actor  string
	}
	for rows.Next() {
		var r struct {
			ID     int64
			Detail string
			Actor  string
		}
		if err := rows.Scan(&r.ID, &r.Detail, &r.Actor); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, r)
	}
	return out
}

func (f *modelsFixture) enableWith(group, body string) *httptest.ResponseRecorder {
	return f.call(f.admin, "POST", group, "enable", body, true, mgEnable)
}

func TestEnablingAGroupWithWeightsNeedsTheAcceptanceOfItsLicence(t *testing.T) {
	f := newModelsFixture(t)
	g := weightsGroup(t)
	sha := g.Recipe.Weights.Licence.SHA256
	for _, c := range []struct {
		name, body string
		code       int
		says       string
	}{
		{"no body", "", 409, "requires accepting the licence"},
		{"not accepted", acceptBody(g, false, "28m"), 409, "requires accepting the licence"},
		{"a stale licence", `{"licence_sha256":"` + strings.Repeat("0", 64) + `","accept":true,"sizes":["28m"]}`, 409, "reload the page"},
		{"no sha", `{"accept":true,"sizes":["28m"]}`, 409, "reload the page"},
		{"no sizes", acceptBody(g, true), 422, "at least one size"},
		{"an unknown size", acceptBody(g, true, "7b"), 422, "«7b»"},
		{"a size twice", acceptBody(g, true, "28m", "28m"), 422, "twice"},
		{"not JSON", "{", 422, "not valid JSON"},
	} {
		w := f.enableWith(mgWeightsGroup, c.body)
		if w.Code != c.code || !strings.Contains(w.Body.String(), c.says) {
			t.Errorf("%s: %d %s", c.name, w.Code, w.Body)
		}
	}
	// nothing was made: no environment, no version, no row of the acceptance, no group row
	var n int
	if err := f.db.QueryRow(f.ctx, `SELECT (SELECT count(*) FROM script_environments WHERE managed_by = $1) + (SELECT count(*) FROM model_groups WHERE id = $2)`,
		mgManagedBy(mgWeightsGroup), mgWeightsGroup).Scan(&n); err != nil || n != 0 {
		t.Errorf("a refused enabling left %d rows (%v)", n, err)
	}
	if len(f.acceptances()) != 0 || len(f.audits()) != 0 {
		t.Errorf("a refused enabling was audited: %v %v", f.acceptances(), f.audits())
	}

	// accepted, two sizes given out of order
	got := answerOf(t, f.enableWith(mgWeightsGroup, acceptBody(g, true, "123m", "28m"))).Group
	if !got.Enabled || got.State != "building" || len(got.Versions) != 1 || got.Versions[0].Status != "queued" {
		t.Fatalf("enabled: %+v", got)
	}
	acc := f.acceptances()
	if len(acc) != 1 || acc[0].Actor != f.admin.Email || !strings.Contains(acc[0].Detail, `"licence_sha256": "`+sha+`"`) ||
		!strings.Contains(acc[0].Detail, `"sizes": ["28m", "123m"]`) || !strings.Contains(acc[0].Detail, `"recipe_hash": "`+g.RecipeHash+`"`) {
		t.Fatalf("the audit: %+v", acc)
	}
	vs := f.versions(mgWeightsGroup)
	line := mgAcceptanceOfPackages(vs[0].PackagesText)
	if line == nil || line.Audit != acc[0].ID || line.By != f.admin.Email || strings.Join(line.Sizes, ",") != "28m,123m" || line.SHA256 != sha {
		t.Fatalf("the version's lines:\n%s\n%+v", vs[0].PackagesText, line)
	}
	if got.Licence == nil || got.Licence.Pending || got.Licence.Line == nil || got.Licence.Line.Audit != acc[0].ID || got.Licence.SHA256 != sha ||
		!strings.Contains(got.Licence.Text, "$20 million") || !strings.Contains(got.Licence.URL, "huggingface.co/mistralai/") || len(got.Licence.Sizes) != 4 {
		t.Errorf("the answer's licence: %+v", got.Licence)
	}
	for _, name := range []string{"models.group_enable"} {
		found := false
		for _, a := range f.audits() {
			found = found || strings.HasPrefix(a, name+" "+mgWeightsGroup)
		}
		if !found {
			t.Errorf("%s is not audited: %v", name, f.audits())
		}
	}

	// enabled again, with nothing to build (a version in use ready on this recipe) needs no acceptance
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'ready', image_ref = 'registry/x@sha256:abc', recipe_hash = $1`, g.RecipeHash); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = (SELECT id FROM script_environment_versions LIMIT 1) WHERE managed_by = $1`, mgManagedBy(mgWeightsGroup)); err != nil {
		t.Fatal(err)
	}
	f.call(f.admin, "POST", mgWeightsGroup, "", "", true, mgDisable)
	if again := answerOf(t, f.enableWith(mgWeightsGroup, "")); !again.Group.Enabled || again.Group.State != "ready" || len(again.Group.Versions) != 1 || len(f.acceptances()) != 1 {
		t.Errorf("enabling a ready group again: %+v, %d acceptances", again.Group, len(f.acceptances()))
	}
}

func TestBuildingAGroupWithWeightsAgainAsksForTheAcceptanceAndTheSizes(t *testing.T) {
	f := newModelsFixture(t)
	g := weightsGroup(t)
	f.enableWith(mgWeightsGroup, acceptBody(g, true, "5m", "28m", "123m", "209m"))
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'failed'`); err != nil {
		t.Fatal(err)
	}
	rebuild := func(body string) *httptest.ResponseRecorder {
		return f.call(f.admin, "POST", mgWeightsGroup, "", body, true, mgRebuild)
	}
	for body, code := range map[string]int{"": 409, acceptBody(g, false, "5m"): 409, acceptBody(g, true): 422, `{"licence_sha256":"x","accept":true,"sizes":["5m"]}`: 409} {
		if w := rebuild(body); w.Code != code {
			t.Errorf("%q: %d %s", body, w.Code, w.Body)
		}
	}
	if len(f.versions(mgWeightsGroup)) != 1 || len(f.acceptances()) != 1 {
		t.Fatalf("a refused rebuild queued or audited: %d versions, %d acceptances", len(f.versions(mgWeightsGroup)), len(f.acceptances()))
	}
	// a change of sizes is a rebuild with the new ones
	out := answerOf(t, rebuild(acceptBody(g, true, "5m"))).Group
	acc := f.acceptances()
	vs := f.versions(mgWeightsGroup)
	if len(out.Versions) != 2 || len(acc) != 2 || len(vs) != 2 {
		t.Fatalf("rebuilt: %d versions, %d acceptances", len(out.Versions), len(acc))
	}
	if line := mgAcceptanceOfPackages(vs[0].PackagesText); line == nil || line.Audit != acc[1].ID || strings.Join(line.Sizes, ",") != "5m" {
		t.Errorf("the new version's lines:\n%s", vs[0].PackagesText)
	}
	if sizes, _ := scriptimage.WeightsSizes(vs[0].PackagesText); len(sizes) != 1 || sizes[0] != "5m" {
		t.Errorf("the build reads %v", sizes)
	}
	if line := mgAcceptanceOfPackages(vs[1].PackagesText); line == nil || line.Audit != acc[0].ID || len(line.Sizes) != 4 {
		t.Errorf("the first version's lines changed:\n%s", vs[1].PackagesText)
	}
	// a group without weights: the click's body is ignored and its packages are the recipe's
	f.enable(f.admin, "regression")
	r, _ := modelgroups.Get("regression")
	for _, v := range f.versions("regression") {
		if v.PackagesText != strings.Join(r.Recipe.Packages, "\n") {
			t.Errorf("the packages of another group: %q", v.PackagesText)
		}
	}
}

func TestWhenTheAuditRowIsGoneTheVersionRunsAndNothingIsBuiltBySelf(t *testing.T) {
	f := newModelsFixture(t)
	g := weightsGroup(t)
	f.enableWith(mgWeightsGroup, acceptBody(g, true, "28m"))
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'ready', image_ref = 'registry/x@sha256:abc', arch = 'amd64'`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = (SELECT id FROM script_environment_versions LIMIT 1) WHERE managed_by = $1`, mgManagedBy(mgWeightsGroup)); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec(f.ctx, `DELETE FROM audit_logs WHERE action = 'models.licence_accept' AND actor_id = $1`, f.admin.ID); err != nil {
		t.Fatal(err)
	}
	// as with a short retention: the row is gone, the version in use serves and its line shows
	got := answerOf(t, f.call(f.admin, "GET", mgWeightsGroup, "", "", false, mgDetail)).Group
	if got.State != "ready" || !got.Ready || got.Licence == nil || !got.Licence.Pending || got.Licence.Line == nil || got.Licence.Line.By != f.admin.Email ||
		got.Licence.Line.Version != 1 || strings.Join(got.Licence.Line.Sizes, ",") != "28m" {
		t.Fatalf("the group without its audit row: %+v %+v", got, got.Licence)
	}
	if img, err := ReadyModelGroupImage(f.ctx, f.db, mgWeightsGroup); err != nil || img.Version != 1 {
		t.Errorf("the ready version does not run: %v", err)
	}

	// a Tabularia with a new recipe: nothing is queued by itself, and the steps say why
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recipe_hash = 'of-another-recipe', created_at = created_at - interval '1 day'`); err != nil {
		t.Fatal(err)
	}
	b := f.builds()
	for i := 0; i < 2; i++ {
		if _, err := b.oneRound(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	if vs := f.versions(mgWeightsGroup); len(vs) != 1 {
		t.Fatalf("a version was queued without an acceptance: %d", len(vs))
	}
	got = answerOf(t, f.call(f.admin, "GET", mgWeightsGroup, "", "", false, mgDetail)).Group
	if got.State != "licence_pending" || got.Ready {
		t.Errorf("the state: %s ready %v", got.State, got.Ready)
	}
	_, err := ReadyModelGroupImage(f.ctx, f.db, mgWeightsGroup)
	if un, ok := err.(*ModelGroupUnavailable); !ok || un.Reason != "licence_pending" ||
		un.Message != `the model group "foundation_forecast" waits for an administrator to accept the licence of its weights` {
		t.Errorf("the step's sentence: %v", err)
	}
	pal := f.call(f.admin, "GET", "", "", "", false, func(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) { mgPalette(d, w, r, p) })
	if !strings.Contains(pal.Body.String(), `"licence_pending":true`) {
		t.Errorf("the palette: %s", pal.Body)
	}

	// an administrator accepts again (a click): a version is queued, the audit has the row, and the next
	// recipe change is queued by itself on that row, with the sizes of the version in use
	out := answerOf(t, f.call(f.admin, "POST", mgWeightsGroup, "", acceptBody(g, true, "5m", "28m"), true, mgRebuild)).Group
	if len(out.Versions) != 2 || out.State != "building" {
		t.Fatalf("rebuilt: %+v", out)
	}
	acc := f.acceptances()
	if len(acc) != 1 {
		t.Fatalf("acceptances: %v", acc)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'ready', image_ref = 'registry/y@sha256:def', arch = 'amd64', recipe_hash = 'of-another-recipe',
		created_at = created_at - interval '1 day' WHERE number = 2`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = (SELECT id FROM script_environment_versions WHERE number = 2) WHERE managed_by = $1`, mgManagedBy(mgWeightsGroup)); err != nil {
		t.Fatal(err)
	}
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	vs := f.versions(mgWeightsGroup)
	if len(vs) != 3 || vs[0].Number != 3 || vs[0].CreatedBy == nil || *vs[0].CreatedBy != "tabularia" {
		t.Fatalf("the automatic build: %d versions", len(vs))
	}
	if line := mgAcceptanceOfPackages(vs[0].PackagesText); line == nil || line.Audit != acc[0].ID || line.By != f.admin.Email || strings.Join(line.Sizes, ",") != "5m,28m" {
		t.Errorf("the automatic version's lines:\n%s", vs[0].PackagesText)
	}
	// the acceptance is read for the shipped licence only: a row of another licence is no acceptance
	if _, err := f.db.Exec(f.ctx, `UPDATE audit_logs SET detail = replace(detail, $1, $2) WHERE action = 'models.licence_accept' AND actor_id = $3`,
		g.Recipe.Weights.Licence.SHA256, strings.Repeat("a", 64), f.admin.ID); err != nil {
		t.Fatal(err)
	}
	s, err := mgLoad(f.ctx, f.db, g, false, 5)
	if err != nil || s.Accepted != nil {
		t.Errorf("an acceptance of another licence is valid: %v %v", s.Accepted, err)
	}
}

// Build again ticks the sizes of the version in use, never sizes nobody chose (lot F9: on kind the box opened with the four
// sizes ticked on a version built with two, and one click would have downloaded 123M and 209M too); a group never built
// ticks them all (the panel's rule for an empty list).
func TestBuildAgainTicksTheSizesOfTheVersionInUse(t *testing.T) {
	g := weightsGroup(t)
	cur := int64(7)
	built := mgPackagesText(g, &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, By: "ada@example.com", At: time.Now(), Audit: 5,
		Sizes: []string{"5m", "28m"}})
	newer := mgPackagesText(g, &mgAcceptance{SHA256: g.Recipe.Weights.Licence.SHA256, By: "ada@example.com", At: time.Now(), Audit: 6,
		Sizes: []string{"209m"}})
	s := mgState{Group: g, Enabled: true, CurrentID: &cur, Versions: []mgVersion{
		{ID: 8, Number: 8, Status: "failed", PackagesText: newer}, {ID: 7, Number: 7, Status: "ready", RecipeHash: &g.RecipeHash, PackagesText: built}}}
	if got := fmt.Sprint(mgLicenceView(&s, 0).SizesInUse); got != "[5m 28m]" {
		t.Errorf("the version in use's sizes: %s", got)
	}
	// a version in use on an older recipe is still the one serving: its sizes
	old := "old"
	s.Versions[1].RecipeHash = &old
	if got := fmt.Sprint(mgLicenceView(&s, 0).SizesInUse); got != "[5m 28m]" {
		t.Errorf("an older recipe: %s", got)
	}
	// nothing in use (a group never built, or whose first build failed): none, and the panel ticks them all
	s.CurrentID = nil
	if lic := mgLicenceView(&s, 0); lic.SizesInUse == nil || len(lic.SizesInUse) != 0 {
		t.Errorf("no version in use: %v", lic.SizesInUse)
	}
}

// The node's form reads the licence's name and its text at the pinned revision of each size from the gateway (lot F9: the
// editor kept them in a constant whose link pointed at the Hub's moving `main`).
func TestThePaletteSaysTheLicenceAtThePinnedRevisions(t *testing.T) {
	g := weightsGroup(t)
	l := mgPaletteLicenceOf(g, []string{"5m", "28m"})
	w := g.Recipe.Weights
	if l == nil || l.Name != w.Licence.Name || len(l.URLs) != 2 {
		t.Fatalf("%+v", l)
	}
	for _, z := range []string{"5m", "28m"} {
		want := "https://huggingface.co/" + w.Sizes[z].Repo + "/blob/" + w.Sizes[z].Revision + "/LICENSE"
		if l.URLs[z] != want || len(w.Sizes[z].Revision) != 40 {
			t.Errorf("%s: %s", z, l.URLs[z])
		}
	}
	if l.URL != l.URLs["5m"] || strings.Contains(l.URL, "/main/") {
		t.Errorf("url: %s", l.URL)
	}
	if all := mgPaletteLicenceOf(g, nil); len(all.URLs) != len(w.Sizes) {
		t.Errorf("no version in use: every size, %d", len(all.URLs))
	}
	for _, o := range modelgroups.Groups() {
		if o.Recipe.Weights == nil && mgPaletteLicenceOf(o, nil) != nil {
			t.Errorf("%s has a licence", o.ID)
		}
	}
}

// An observer and nothing else (a public demo's guest) reads the people of the section masked, as everywhere else in
// Administration: who enabled a group, who queued each version and who accepted the licence of its weights. An
// administrator reads them whole.
func TestAnObserverReadsThePeopleOfTheGroupsMasked(t *testing.T) {
	f := newModelsFixture(t)
	g := weightsGroup(t)
	if w := f.enableWith(mgWeightsGroup, acceptBody(g, true, "28m")); w.Code != http.StatusOK {
		t.Fatalf("enabling: %d %s", w.Code, w.Body)
	}
	masked := masking.Email(f.admin.Email)
	for _, c := range []struct {
		name string
		h    func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)
	}{{"list", mgList}, {"detail", mgDetail}} {
		w := f.call(f.observer, "GET", mgWeightsGroup, "", "", false, c.h)
		if w.Code != http.StatusOK {
			t.Fatalf("%s: %d %s", c.name, w.Code, w.Body)
		}
		if strings.Contains(w.Body.String(), f.admin.Email) || !strings.Contains(w.Body.String(), masked) {
			t.Errorf("%s as an observer: the administrator's email is not masked", c.name)
		}
		if a := f.call(f.admin, "GET", mgWeightsGroup, "", "", false, c.h); !strings.Contains(a.Body.String(), f.admin.Email) {
			t.Errorf("%s as an administrator: the email is masked", c.name)
		}
	}
	got := answerOf(t, f.call(f.observer, "GET", mgWeightsGroup, "", "", false, mgDetail)).Group
	if got.EnabledBy == nil || *got.EnabledBy != masked || got.Versions[0].CreatedBy == nil || *got.Versions[0].CreatedBy != masked ||
		got.Licence == nil || got.Licence.Line == nil || got.Licence.Line.By != masked {
		t.Errorf("the observer's group: %+v", got)
	}
}
