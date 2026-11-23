package bayescodegen

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// Properties of the generator (docs/specs/bayes-stan-codegen.md, «Testing strategy»), on graphs
// written by gen_test.go's generator (the four families with random sizes, names, ids and orders,
// large expressions in every place the splitting of lot S5 writes locals) and on seeded mutations
// of them. Nothing here is a fixed answer: each property compares the generator with itself under a
// change that must not matter, or checks what every program must satisfy.

// validModel decodes and validates a graph; ok is false for anything that is not a valid model.
func validModel(data []byte) (*bayesgraph.Model, bool) {
	g, _ := bayesgraph.Decode(data)
	if g == nil {
		return nil, false
	}
	m := bayesgraph.BuildModel(g)
	return m, m.Valid()
}

func seededGraph(seed int) []byte {
	return genGraph(randChooser{rand.New(rand.NewPCG(uint64(seed), 0x9e3779b9))})
}

// ppcExtension is a replicated outcome for a normal likelihood, the way predictive-checks will write
// it: the likelihood's arguments read as slots, so that a split mean is rebuilt in the nested block
// and the value the extension asks for gets locals of its own. For another outcome it adds nothing.
type ppcExtension struct{}

func (ppcExtension) Data(*bayesgraph.Model) []Decl { return nil }

func (ppcExtension) Generated(m *bayesgraph.Model, s Scope) ([]Decl, []Stmt, error) {
	for _, n := range m.Graph.Nodes {
		if n.Kind != "likelihood" || n.Dist != "normal" {
			continue
		}
		// with single-value arguments normal_rng returns one number, not one a row
		muKey, sigmaKey := bayesgraph.SlotKey{Node: n.ID, Slot: "args.mu"}, bayesgraph.SlotKey{Node: n.ID, Slot: "args.sigma"}
		if m.Slots[muKey].Type.Plate == "" && m.Slots[sigmaKey].Type.Plate == "" {
			return nil, nil, nil
		}
		rows, err := s.Rows(n.ID)
		if err != nil {
			return nil, nil, err
		}
		mu, err := s.Slot(bayesgraph.SlotKey{Node: n.ID, Slot: "args.mu"}, nil)
		if err != nil {
			return nil, nil, err
		}
		sigma, err := s.Slot(bayesgraph.SlotKey{Node: n.ID, Slot: "args.sigma"}, nil)
		if err != nil {
			return nil, nil, err
		}
		call, err := s.Call("normal_rng", mu, sigma)
		if err != nil {
			return nil, nil, err
		}
		yrep := Decl{Name: "Ppc_yrep", Elem: Real, Shape: Array, Size: rows}
		return nil, []Stmt{Declare(yrep, call)}, nil
	}
	return nil, nil, nil
}

// tamperSlots writes hostile text into every expression of a validated model's graph: the program
// must not change (the text is never read after validation).
func tamperSlots(g *bayesgraph.Graph) {
	evil := `a); print("x"); target += 1e300; //`
	set := func(s *bayesgraph.Slot) {
		if s != nil && s.Expr != nil {
			e := evil
			s.Expr = &e
		}
	}
	setMap := func(m map[string]bayesgraph.Slot) {
		for k, s := range m {
			set(&s)
			m[k] = s
		}
	}
	for i := range g.Nodes {
		n := &g.Nodes[i]
		if n.Prior != nil {
			setMap(n.Prior.Args)
		}
		if n.Init != nil {
			setMap(n.Init.Args)
		}
		setMap(n.Args)
		set(n.Value)
		set(n.Intercept)
		set(n.Scale)
		for j := range n.Terms {
			set(&n.Terms[j].Coef)
		}
		for j := range n.Components {
			set(&n.Components[j].Mean)
			set(&n.Components[j].Scale)
		}
		if n.Correlation != nil {
			set(&n.Correlation.Eta)
		}
		n.Label = `*/ reject("x"); /*`
		n.Note = "target += 1;"
	}
	for i := range g.Plates {
		g.Plates[i].Label = `#include "x"`
	}
}

// splitPlaces classifies where a program writes the locals of a split value.
func splitPlaces(text string) map[string]bool {
	out := map[string]bool{}
	block, depth := "", 0
	for _, l := range strings.Split(strings.TrimSuffix(text, "\n"), "\n") {
		ind := (len(l) - len(strings.TrimLeft(l, " "))) / 2
		t := strings.TrimSpace(l)
		if ind == 0 && strings.HasSuffix(t, "{") {
			block = strings.TrimSuffix(t, " {")
			depth = 0
			continue
		}
		depth = ind
		if !strings.Contains(t, "E_") || !strings.Contains(t, "] E_") && !strings.Contains(t, "E_") {
			continue
		}
		decl := strings.HasPrefix(t, "vector[") && strings.Contains(t, "] E_")
		sub := strings.Contains(t, "[Rows_lag] =") && strings.HasPrefix(t, "E_")
		sub2 := strings.Contains(t, "[Rows_first] =") && strings.HasPrefix(t, "E_")
		switch {
		case sub:
			out["lag subset"] = true
		case sub2:
			out["first subset"] = true
		case decl && block == "model" && ind >= 2:
			out["truncated loop"] = true
		case decl && block == "model":
			out["model"] = true
		case decl && block == "transformed data":
			out["transformed data"] = true
		case decl && block == "transformed parameters":
			out["transformed parameters"] = true
		case decl && block == "generated quantities":
			out["generated quantities"] = true
		}
		_ = depth
	}
	return out
}

// reached counts, over a set of programs, the places the splitting wrote locals in.
type reached map[string]int

func (r reached) add(text string) {
	for k := range splitPlaces(text) {
		r[k]++
	}
	if regexp.MustCompile(`E_[a-z0-9]+_[0-9]+\[Rows_lag\]`).MatchString(text) {
		r["lagged read"]++
	}
	if strings.Contains(text, "Arg1_") && strings.Contains(text, " E_") {
		r["Arg with locals"]++
	}
	if regexp.MustCompile(`E_q[a-z0-9]*_[0-9]+`).MatchString(text) {
		r["linear predictor"]++
	}
	// a value per row of the model block rebuilt in the transformed parameters (a non-centred
	// state's init reading it)
	if _, tp, ok := strings.Cut(text, "transformed parameters {\n"); ok {
		tp, _, _ = strings.Cut(tp, "\nmodel {")
		for _, m := range perRowLocal.FindAllStringSubmatch(tp, -1) {
			if !strings.HasPrefix(m[1], "E_") {
				r["per-row value in transformed parameters"]++
				break
			}
		}
	}
}

var perRowLocal = regexp.MustCompile(`(?m)^    vector\[N\] ([A-Za-z][A-Za-z0-9_]*) = `)

// TestPropertyGeneratedPrograms: every valid graph of the generator gives a program that passes the
// alphabet, the scan and the identifier rule (never ErrUnsupported, never ErrBug), the same twice,
// the same when the graph's JSON is shuffled and its boxes, labels and notes changed, the same when
// the text of its expressions is replaced after validation, and the same through the extension; and
// the sample reaches the splitting in every place the table of lot S5 lists.
func TestPropertyGeneratedPrograms(t *testing.T) {
	n := 500
	if testing.Short() {
		n = 120
	}
	valid := 0
	places := reached{}
	extPlaces := reached{}
	checkPlaces := checksReached{}
	for seed := 0; seed < n; seed++ {
		data := seededGraph(seed)
		m, ok := validModel(data)
		if !ok {
			continue
		}
		valid++
		p, err := Generate(m, nil)
		if err != nil {
			t.Fatalf("seed %d: a valid model gives %v (ErrUnsupported: %v)\n%s", seed, err, errors.Is(err, ErrUnsupported), data)
		}
		if err := CheckProgram(string(p.Text)); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if err := CheckIdentifiers(p.Mapping); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		places.add(string(p.Text))

		// twice: the same bytes
		m2, _ := validModel(data)
		again, err := Generate(m2, nil)
		if err != nil || !bytes.Equal(p.Text, again.Text) || p.SHA256 != again.SHA256 {
			t.Fatalf("seed %d: two generations differ (%v)", seed, err)
		}
		// the JSON shuffled, boxes moved, labels and notes hostile
		if seed%4 == 0 {
			m3, ok := validModel(shuffled(t, data, uint64(seed)+1))
			if !ok {
				t.Fatalf("seed %d: the shuffled graph is not valid", seed)
			}
			q, err := Generate(m3, nil)
			if err != nil || !bytes.Equal(p.Text, q.Text) || !jsonEqual(t, p.Data, q.Data) || !jsonEqual(t, p.Quantities, q.Quantities) ||
				!jsonEqual(t, withoutLabels(p.Mapping), withoutLabels(q.Mapping)) {
				t.Fatalf("seed %d: the shuffled graph gives another program (%v)", seed, err)
			}
		}
		// the text of the expressions never read after validation
		tamperSlots(m2.Graph)
		if q, err := Generate(m2, nil); err != nil || !bytes.Equal(p.Text, q.Text) {
			t.Fatalf("seed %d: the program read the graph's text (%v)", seed, err)
		}
		// the extension
		m4, _ := validModel(data)
		e, err := Generate(m4, ppcExtension{})
		if err != nil {
			t.Fatalf("seed %d: with the extension: %v\n%s", seed, err, data)
		}
		if err := CheckProgram(string(e.Text)); err != nil {
			t.Fatalf("seed %d: with the extension: %v", seed, err)
		}
		if err := CheckIdentifiers(e.Mapping); err != nil {
			t.Fatalf("seed %d: with the extension: %v", seed, err)
		}
		extPlaces.add(string(e.Text))
		// the test extension of the checks (lot P1): the same checks, the same bytes twice and
		// when the JSON is shuffled
		c, err := Generate(m4, checksExtension{})
		if err != nil {
			t.Fatalf("seed %d: with the checks: %v\n%s", seed, err, data)
		}
		if err := CheckProgram(string(c.Text)); err != nil {
			t.Fatalf("seed %d: with the checks: %v", seed, err)
		}
		if err := CheckIdentifiers(c.Mapping); err != nil {
			t.Fatalf("seed %d: with the checks: %v", seed, err)
		}
		m5, _ := validModel(data)
		if seed%4 == 1 {
			m5, _ = validModel(shuffled(t, data, uint64(seed)+7))
		}
		if again, err := Generate(m5, checksExtension{}); err != nil || !bytes.Equal(c.Text, again.Text) {
			t.Fatalf("seed %d: with the checks, two generations differ (%v)", seed, err)
		}
		checkPlaces.add(string(c.Text))
	}
	if valid < n*9/10 {
		t.Errorf("only %d of the %d generated graphs are valid", valid, n)
	}
	for _, place := range []string{"model", "transformed data", "transformed parameters", "truncated loop", "lag subset", "first subset", "linear predictor", "lagged read",
		"per-row value in transformed parameters"} {
		if places[place] == 0 {
			t.Errorf("no generated program writes locals in %q: %v", place, places)
		}
	}
	if extPlaces["generated quantities"] == 0 {
		t.Errorf("no generated program writes locals in the generated quantities: %v", extPlaces)
	}
	for _, place := range []string{"prior path", "rebuilt on the rows", "lagged positions", "lag at positions", "split on the rows"} {
		if checkPlaces[place] == 0 {
			t.Errorf("no generated program with the checks reaches %q: %v", place, checkPlaces)
		}
	}
	t.Logf("%d of %d graphs valid; places reached: %v; with the extension: %v; with the checks: %v", valid, n, places, extPlaces, checkPlaces)
}

// checksReached counts what the programs with the test extension of the checks reach: a prior's
// path, a value per row rebuilt with the draws on the rows named, a lagged statement's positions,
// a lag at the positions, a split value on the rows named.
type checksReached map[string]int

func (r checksReached) add(text string) {
	for place, re := range map[string]*regexp.Regexp{
		"prior path":          regexp.MustCompile(`Prior_o1_draw = `),
		"rebuilt on the rows": regexp.MustCompile(`\[Prior_o[0-9]_rows\] = `),
		"lagged positions":    regexp.MustCompile(`Rows_lag\[(Ppc|Prior)_o[0-9]_rows\]`),
		"lag at positions":    regexp.MustCompile(`Lag[0-9]+\[(Ppc|Prior)_o[0-9]_rows\]`),
		"split on the rows":   regexp.MustCompile(`E_[a-z0-9]+_[0-9]+\[(Rows_lag\[)?(Ppc|Prior)_o[0-9]_rows`),
	} {
		if re.MatchString(text) {
			r[place]++
		}
	}
}

// jsonWalk applies f to every JSON object of a document.
func jsonWalk(v any, f func(map[string]any)) {
	switch x := v.(type) {
	case map[string]any:
		f(x)
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			jsonWalk(x[k], f)
		}
	case []any:
		for _, e := range x {
			jsonWalk(e, f)
		}
	}
}

// TestPropertyRenameOneNode: renaming a node (whose name is its identifier) changes only the
// occurrences of that identifier in the program, and in the mapping table the node's name.
func TestPropertyRenameOneNode(t *testing.T) {
	n := 300
	if testing.Short() {
		n = 80
	}
	renamed := 0
	for seed := 0; seed < n; seed++ {
		data := seededGraph(seed)
		m, ok := validModel(data)
		if !ok {
			continue
		}
		before, err := Generate(m, nil)
		if err != nil {
			t.Fatal(err)
		}
		// the nodes that carry their own name into the program
		var cands []bayescontract.MappingEntry
		for _, e := range before.Mapping {
			if e.NodeID != nil && e.Component == nil && e.Name != nil && e.Renamed == nil && *e.Name == e.Stan &&
				(e.Kind == "data" || e.Kind == "parameter" || e.Kind == "transformed" || e.Kind == "linear_predictor" || e.Kind == "state") {
				cands = append(cands, e)
			}
		}
		if len(cands) == 0 {
			continue
		}
		pick := cands[seed%len(cands)]
		old, fresh := *pick.Name, fmt.Sprintf("renamed_%d", seed)
		word := regexp.MustCompile(`\b` + regexp.QuoteMeta(old) + `\b`)
		var doc any
		if err := json.Unmarshal(data, &doc); err != nil {
			t.Fatal(err)
		}
		jsonWalk(doc, func(o map[string]any) {
			if s, ok := o["expr"].(string); ok {
				o["expr"] = word.ReplaceAllString(s, fresh)
			}
			if id, _ := o["id"].(string); id == *pick.NodeID && o["name"] == old {
				o["name"] = fresh
			}
		})
		out, _ := json.Marshal(doc)
		m2, ok := validModel(out)
		if !ok {
			t.Fatalf("seed %d: the renamed graph is not valid: %+v", seed, m2.Findings)
		}
		after, err := Generate(m2, nil)
		if err != nil {
			t.Fatal(err)
		}
		want := word.ReplaceAllString(string(before.Text), fresh)
		if string(after.Text) != want {
			t.Fatalf("seed %d: renaming %s changed more than its occurrences\n--- want\n%s--- got\n%s", seed, old, want, after.Text)
		}
		if bytes.Equal(before.Text, after.Text) {
			t.Fatalf("seed %d: the rename of %s changed nothing", seed, old)
		}
		renamed++
	}
	if renamed < n/2 {
		t.Errorf("only %d renames tried out of %d graphs", renamed, n)
	}
}

// mutateGraph is a seeded walk into the graph's JSON that breaks, removes, duplicates or retypes
// values: most results are not valid models, some are, and those are what the generator must still
// write.
func mutateGraph(c chooser, data []byte) []byte {
	var doc map[string]any
	if json.Unmarshal(data, &doc) != nil {
		return data
	}
	snippets := []string{"normal", "x", "y", "1", "0", "2.5 * a", "lag(y, 2)", "exp(1)", "-1", "student_t", "poisson_log",
		"non_centred", "centred", "real", "int", "category", "rows", "lower", "square(", "a b", "1e999", "model", "N", "J_x"}
	// every string leaf, to swap values between them
	type leaf struct {
		m map[string]any
		k string
	}
	for rounds := 1 + c.pick(3); rounds > 0; rounds-- {
		var leaves []leaf
		var objects []map[string]any
		jsonWalk(doc, func(o map[string]any) {
			objects = append(objects, o)
			keys := make([]string, 0, len(o))
			for k := range o {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				if _, ok := o[k].(string); ok && k != "id" {
					leaves = append(leaves, leaf{o, k})
				}
			}
		})
		if len(leaves) == 0 || len(objects) == 0 {
			break
		}
		l := leaves[c.pick(len(leaves))]
		switch c.pick(7) {
		case 0, 1:
			other := leaves[c.pick(len(leaves))]
			l.m[l.k] = other.m[other.k]
		case 2, 3:
			l.m[l.k] = snippets[c.pick(len(snippets))]
		case 4:
			if s, _ := l.m[l.k].(string); s != "" {
				l.m[l.k] = s + " + " + snippets[c.pick(len(snippets))]
			}
		case 5:
			o := objects[c.pick(len(objects))]
			keys := make([]string, 0, len(o))
			for k := range o {
				if k != "id" && k != "kind" {
					keys = append(keys, k)
				}
			}
			sort.Strings(keys)
			if len(keys) > 0 {
				delete(o, keys[c.pick(len(keys))])
			}
		default:
			if nodes, _ := doc["nodes"].([]any); len(nodes) > 1 {
				i := c.pick(len(nodes))
				if c.pick(2) == 0 {
					doc["nodes"] = append(nodes[:i:i], nodes[i+1:]...)
				} else if o, ok := nodes[i].(map[string]any); ok {
					cp := map[string]any{}
					for k, v := range o {
						cp[k] = v
					}
					cp["id"] = fmt.Sprintf("dup%d", rounds)
					doc["nodes"] = append(nodes, cp)
				}
			}
		}
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return data
	}
	return out
}

// TestPropertyMutatedGraphs: whatever a mutation leaves valid still gives a program (never a
// panic, never ErrUnsupported or ErrBug) that passes the checks; whatever it leaves invalid is
// refused with ErrInvalidModel.
func TestPropertyMutatedGraphs(t *testing.T) {
	n := 600
	if testing.Short() {
		n = 150
	}
	valid, invalid := 0, 0
	for seed := 0; seed < n; seed++ {
		c := randChooser{rand.New(rand.NewPCG(uint64(seed), 0xabcdef))}
		data := mutateGraph(c, seededGraph(seed))
		g, _ := bayesgraph.Decode(data)
		if g == nil {
			continue
		}
		m := bayesgraph.BuildModel(g)
		p, err := Generate(m, nil)
		if !m.Valid() {
			invalid++
			if !errors.Is(err, ErrInvalidModel) {
				t.Fatalf("seed %d: an invalid model gives %v", seed, err)
			}
			continue
		}
		valid++
		if err != nil {
			t.Fatalf("seed %d: a valid mutated model gives %v\n%s", seed, err, data)
		}
		if err := CheckProgram(string(p.Text)); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if err := CheckIdentifiers(p.Mapping); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
	}
	if valid < n/20 || invalid < n/10 {
		t.Errorf("mutations: %d valid, %d invalid", valid, invalid)
	}
	t.Logf("%d valid and %d invalid mutations", valid, invalid)
}
