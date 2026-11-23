package bayestemplates

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

var wantIDs = []string{"glm_linear", "glm_logistic", "glm_poisson", "glm_negbin", "hier_intercepts",
	"hier_slopes", "hier_nested", "ts_ar", "ts_random_walk", "ts_local_level"}

func decodeGraph(t *testing.T, what string, data []byte) *bayesgraph.Graph {
	t.Helper()
	g, findings := bayesgraph.Decode(data)
	if len(findings) > 0 || g == nil {
		t.Fatalf("%s: does not decode: %v", what, findings)
	}
	return g
}

// requireClean: the graph is a valid model with no finding at all (no error, no warning).
func requireClean(t *testing.T, what string, g *bayesgraph.Graph) *bayesgraph.Model {
	t.Helper()
	m := bayesgraph.BuildModel(g)
	if !m.Valid() || len(m.Findings) > 0 {
		t.Errorf("%s: findings %v", what, m.Findings)
	}
	return m
}

func TestTheFile(t *testing.T) {
	f, err := Decode(Raw())
	if err != nil {
		t.Fatal(err)
	}
	var ids []string
	for _, tpl := range f.Templates {
		ids = append(ids, tpl.ID)
	}
	if strings.Join(ids, ",") != strings.Join(wantIDs, ",") {
		t.Fatalf("templates %v, want %v", ids, wantIDs)
	}
	if got := List(); len(got) != 10 {
		t.Fatalf("List: %d", len(got))
	}
	if _, ok := Get("glm_linear"); !ok {
		t.Fatal("Get glm_linear")
	}
	if _, ok := Get("nope"); ok {
		t.Fatal("Get nope")
	}
	if _, err := Decode([]byte(`{"format_version":1,"templates":[],"sources":{},"extra":1}`)); err == nil {
		t.Error("an unknown key must be refused")
	}
	if _, err := Decode([]byte(`{"format_version":2,"templates":[],"sources":{}}`)); err == nil {
		t.Error("a newer format must be refused")
	}
	for _, tpl := range f.Templates {
		if tpl.Version < 1 {
			t.Errorf("%s: version %d", tpl.ID, tpl.Version)
		}
		if tpl.Scale != nil && *tpl.Scale != "outcome" {
			t.Errorf("%s: scale %q", tpl.ID, *tpl.Scale)
		}
		if (tpl.Scale == nil) != (len(tpl.Scaled) == 0) {
			t.Errorf("%s: scale and scaled disagree", tpl.ID)
		}
		g := decodeGraph(t, tpl.ID, tpl.Graph)
		nodes := map[string]bayesgraph.Node{}
		for _, n := range g.Nodes {
			nodes[n.ID] = n
		}
		for _, s := range tpl.Scaled {
			n, ok := nodes[s.Node]
			if !ok {
				t.Errorf("%s: scaled %v names no node", tpl.ID, s)
				continue
			}
			if _, ok := numberAt(n, s.Slot); !ok {
				t.Errorf("%s: scaled %v: the slot does not hold a number", tpl.ID, s)
			}
			if s.As != "center" && s.As != "spread" && s.As != "rate" {
				t.Errorf("%s: scaled as %q", tpl.ID, s.As)
			}
		}
		for node, key := range tpl.Sources {
			if _, ok := nodes[node]; !ok {
				t.Errorf("%s: source of %q: no such node", tpl.ID, node)
			}
			if _, ok := f.Sources[key]; !ok && key != "spec" {
				t.Errorf("%s: source %q of %q is not in the file", tpl.ID, key, node)
			}
		}
		if (tpl.ID == "ts_ar") != (tpl.Order != nil) {
			t.Errorf("%s: order %v", tpl.ID, tpl.Order)
		}
		if tpl.Order != nil {
			if _, ok := nodes[tpl.Order.Node]; !ok {
				t.Errorf("%s: order node %q", tpl.ID, tpl.Order.Node)
			}
			if tpl.Order.Default < 1 || tpl.Order.Default > bayescatalog.GetLimits().MaxLag {
				t.Errorf("%s: default order %d", tpl.ID, tpl.Order.Default)
			}
		}
	}
	for key, s := range f.Sources {
		if s.Title == "" || !strings.HasPrefix(s.URL, "https://") {
			t.Errorf("source %s: %+v", key, s)
		}
	}
}

// numberAt reads the number a slot path ("prior.mu", "init.sigma") of a node holds.
func numberAt(n bayesgraph.Node, path string) (float64, bool) {
	part, arg, _ := strings.Cut(path, ".")
	var d *bayesgraph.Dist
	switch part {
	case "prior":
		d = n.Prior
	case "init":
		d = n.Init
	}
	if d == nil || d.Args[arg].Expr == nil {
		return 0, false
	}
	v, err := strconv.ParseFloat(*d.Args[arg].Expr, 64)
	return v, err == nil
}

var identifier = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// reservedForNames is every word a program would have to rename a node's name for: the mapping
// table of a template has no `renamed` entry.
func reservedForNames() map[string]bool {
	words := map[string]bool{}
	s := bayescatalog.StanLists()
	for _, l := range [][]string{s.ReservedWords, s.TypeNames, s.ReservedFunctions, s.BlockNames, s.RenamedBackendWords} {
		for _, w := range l {
			words[w] = true
		}
	}
	for _, d := range bayescatalog.Declared().Distributions {
		words[d.ID] = true
	}
	return words
}

func TestEveryTemplateIsAValidCleanModel(t *testing.T) {
	reserved := reservedForNames()
	for _, tpl := range List() {
		g := decodeGraph(t, tpl.ID, tpl.Graph)
		m := requireClean(t, tpl.ID, g)
		if len(m.Bindings) == 0 {
			t.Errorf("%s: nothing to bind", tpl.ID)
		}
		observed := map[string]bool{}
		for _, n := range g.Nodes {
			if n.Kind == "likelihood" {
				observed[n.Observed] = true
			}
		}
		seen := map[string]bool{}
		name := func(what, nm string) {
			switch {
			case nm == "":
				t.Errorf("%s: %s has no name", tpl.ID, what)
			case !identifier.MatchString(nm) || len(nm) > bayescatalog.GetLimits().MaxNameBytes || strings.HasSuffix(nm, "__"):
				t.Errorf("%s: %s: the name %q is not an identifier", tpl.ID, what, nm)
			case reserved[nm] || strings.HasSuffix(nm, "_lpdf") || strings.HasSuffix(nm, "_rng"):
				t.Errorf("%s: %s: the name %q would be renamed in the program", tpl.ID, what, nm)
			case seen[nm]:
				t.Errorf("%s: the name %q is twice", tpl.ID, nm)
			}
			seen[nm] = true
		}
		for _, n := range g.Nodes {
			if n.Kind != "likelihood" && n.Kind != "correlated_effects" { // their components are the names
				nm := ""
				if n.Name != nil {
					nm = *n.Name
				}
				name(n.ID, nm)
			}
			for _, c := range n.Components {
				name(n.ID+"."+c.ID, c.Name)
			}
			if n.Correlation != nil {
				name(n.ID+" correlation", n.Correlation.Name)
			}
			if n.Label == "" {
				t.Errorf("%s: %s has no label", tpl.ID, n.ID)
			}
			if n.Kind == "data" && n.Type == "real" && n.Plate != nil && *n.Plate == "rows" && !observed[n.ID] && !n.Standardize {
				t.Errorf("%s: the predictor %s is not standardized", tpl.ID, n.ID)
			}
		}
		for _, p := range g.Plates {
			if p.Kind == "group" && p.Form != "non_centred" {
				t.Errorf("%s: the plate %s is %q", tpl.ID, p.ID, p.Form)
			}
		}
		checkLayout(t, tpl.ID, g)
	}
}

// checkLayout: every node has a place, the nodes' boxes (180 × 80) do not overlap within 1280 px,
// and a node is inside the box of its group plate, a node of no group plate outside every box.
func checkLayout(t *testing.T, what string, g *bayesgraph.Graph) {
	t.Helper()
	const w, h = 180.0, 80.0
	for i, a := range g.Nodes {
		ba, ok := g.Layout[a.ID]
		if !ok {
			t.Errorf("%s: %s has no place", what, a.ID)
			continue
		}
		if ba.X < -20 || ba.X+w > 1280 {
			t.Errorf("%s: %s is outside 1280 px", what, a.ID)
		}
		for _, b := range g.Nodes[i+1:] {
			bb, ok := g.Layout[b.ID]
			if ok && !(ba.X+w <= bb.X || bb.X+w <= ba.X || ba.Y+h <= bb.Y || bb.Y+h <= ba.Y) {
				t.Errorf("%s: %s and %s overlap", what, a.ID, b.ID)
			}
		}
		for _, p := range g.Plates {
			box, ok := g.Layout[p.ID]
			if p.Kind != "group" || !ok || box.W == nil || box.H == nil {
				continue
			}
			inside := ba.X >= box.X && ba.X+w <= box.X+*box.W && ba.Y >= box.Y && ba.Y+h <= box.Y+*box.H
			outside := ba.X+w <= box.X || box.X+*box.W <= ba.X || ba.Y+h <= box.Y || box.Y+*box.H <= ba.Y
			on := a.Plate != nil && *a.Plate == p.ID
			if on && !inside {
				t.Errorf("%s: %s is not inside its plate %s", what, a.ID, p.ID)
			}
			if !on && !inside && !outside {
				t.Errorf("%s: %s is half inside the plate %s", what, a.ID, p.ID)
			}
			if !on && inside && !isNestedInside(g, a, p.ID) {
				t.Errorf("%s: %s is inside the plate %s it is not on", what, a.ID, p.ID)
			}
		}
	}
}

// isNestedInside: a node of a child plate of plate is drawn inside it.
func isNestedInside(g *bayesgraph.Graph, n bayesgraph.Node, plate string) bool {
	if n.Plate == nil {
		return false
	}
	for p := *n.Plate; p != ""; {
		var next string
		for _, pl := range g.Plates {
			if pl.ID == p && pl.Parent != nil {
				next = *pl.Parent
			}
		}
		if next == plate {
			return true
		}
		p = next
	}
	return false
}

func canonical(t *testing.T, g *bayesgraph.Graph) string {
	t.Helper()
	b, err := bayesgraph.Canonical(g)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// fixtureCase is "<id>.<case>.json" of testdata/opened, written by the browser's test
// (frontend/tests/bayes-templates.test.mjs, BAYES_TEMPLATES_WRITE=1) from bayesOpenTemplate.
func fixtures(t *testing.T) map[string]map[string][]byte {
	t.Helper()
	paths, err := filepath.Glob("testdata/opened/*.json")
	if err != nil || len(paths) == 0 {
		t.Fatalf("no opened fixtures: %v", err)
	}
	out := map[string]map[string][]byte{}
	for _, p := range paths {
		parts := strings.SplitN(strings.TrimSuffix(filepath.Base(p), ".json"), ".", 2)
		data, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		if out[parts[0]] == nil {
			out[parts[0]] = map[string][]byte{}
		}
		out[parts[0]][parts[1]] = data
	}
	return out
}

func TestOpenedTemplatesAreValidCleanModels(t *testing.T) {
	fx := fixtures(t)
	for _, tpl := range List() {
		cases := fx[tpl.ID]
		if len(cases) == 0 {
			t.Errorf("%s: no opened fixture", tpl.ID)
		}
		for name, data := range cases {
			g := decodeGraph(t, tpl.ID+"."+name, data)
			requireClean(t, tpl.ID+"."+name, g)
			checkLayout(t, tpl.ID+"."+name, g)
		}
		want := []string{"default"}
		if tpl.Scale != nil {
			want = append(want, "c0-s1", "c1200-s300", "cm5-s0p02", "s300", "c1200")
		}
		if tpl.Order != nil {
			want = append(want, "p1", "p2", "p5", "p5-c1200-s300")
		}
		for _, w := range want {
			if cases[w] == nil {
				t.Errorf("%s: no fixture %q", tpl.ID, w)
			}
		}
	}
	for id := range fx {
		if _, ok := Get(id); !ok {
			t.Errorf("fixtures of an unknown template %q", id)
		}
	}
}

func TestOpenedWithNothingIsTheTemplate(t *testing.T) {
	fx := fixtures(t)
	for _, tpl := range List() {
		def := canonical(t, decodeGraph(t, tpl.ID, tpl.Graph))
		for _, name := range []string{"default", "c0-s1"} {
			if data := fx[tpl.ID][name]; data != nil {
				if got := canonical(t, decodeGraph(t, tpl.ID+"."+name, data)); got != def {
					t.Errorf("%s.%s is not the template at its default numbers", tpl.ID, name)
				}
			}
		}
		if tpl.Order != nil {
			// the order the template is written for, rebuilt by the browser, is the template
			if got := canonical(t, decodeGraph(t, tpl.ID+".p2", fx[tpl.ID]["p2"])); got != def {
				t.Errorf("%s.p2 is not the template", tpl.ID)
			}
		}
	}
}

// format is a number as the expression language writes the ones the fixtures use.
func format(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }

// TestOpenedScaledSlots: the outcome's numbers are written, in the open, by the same rule in Go
// (an independent reading of the file's `scaled` entries) and in the browser (the fixtures).
func TestOpenedScaledSlots(t *testing.T) {
	fx := fixtures(t)
	cases := map[string][2]float64{"c1200-s300": {1200, 300}, "cm5-s0p02": {-5, 0.02}}
	for _, tpl := range List() {
		if tpl.Scale == nil {
			continue
		}
		def := decodeGraph(t, tpl.ID, tpl.Graph)
		for name, ns := range cases {
			g := decodeGraph(t, tpl.ID+"."+name, fx[tpl.ID][name])
			want := canonicalWithScaled(t, def, tpl.Scaled, ns[0], ns[1])
			if canonical(t, g) != want {
				t.Errorf("%s.%s is not the template with the numbers written in the open", tpl.ID, name)
			}
		}
	}
}

func canonicalWithScaled(t *testing.T, def *bayesgraph.Graph, scaled []Scaled, center, spread float64) string {
	t.Helper()
	data, err := json.Marshal(def)
	if err != nil {
		t.Fatal(err)
	}
	g := decodeGraph(t, "copy", data)
	for _, s := range scaled {
		part, arg, _ := strings.Cut(s.Slot, ".")
		for i := range g.Nodes {
			n := &g.Nodes[i]
			if n.ID != s.Node {
				continue
			}
			d := n.Prior
			if part == "init" {
				d = n.Init
			}
			var text string
			switch {
			case s.As == "center":
				text = format(center)
			case s.As == "rate":
				text = "1 / " + format(spread)
			case s.Times == nil || *s.Times == 1:
				text = format(spread)
			default:
				text = format(*s.Times) + " * " + format(spread)
			}
			d.Args[arg] = bayesgraph.Slot{Expr: &text}
		}
	}
	return canonical(t, g)
}

func TestOpenedARLikelihood(t *testing.T) {
	fx := fixtures(t)
	for _, p := range []int{1, 2, 5} {
		g := decodeGraph(t, "ts_ar", fx["ts_ar"][fmt.Sprintf("p%d", p)])
		want := "mu"
		var phis []string
		for k := 1; k <= p; k++ {
			want += fmt.Sprintf(" + phi%d * (lag(y, %d) - mu)", k, k)
		}
		for _, n := range g.Nodes {
			if n.ID == "lik" && *n.Args["mu"].Expr != want {
				t.Errorf("p=%d: mean %q, want %q", p, *n.Args["mu"].Expr, want)
			}
			if n.Kind == "parameter" && strings.HasPrefix(n.ID, "phi") {
				phis = append(phis, n.ID)
			}
		}
		sort.Strings(phis)
		if len(phis) != p {
			t.Errorf("p=%d: %d coefficients", p, len(phis))
		}
		if m := bayesgraph.BuildModel(g); len(m.Lags) != p {
			t.Errorf("p=%d: lags %v", p, m.Lags)
		}
	}
}

func TestRawIsACopy(t *testing.T) {
	a := Raw()
	a[0] = 'X'
	if bytes.Equal(a, Raw()) {
		t.Error("Raw must return a copy")
	}
}
