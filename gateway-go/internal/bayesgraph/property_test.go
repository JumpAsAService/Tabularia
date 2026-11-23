package bayesgraph

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand"
	"sort"
	"testing"
)

// Properties of the model graph (docs/specs/bayes-model-graph.md, «Testing strategy»), checked on the
// corpus, on a seeded generator of valid graphs (the map's four families with random sizes, names, ids
// and orders) and on seeded mutations of both. Nothing here is a fixed answer: each property compares
// the validator with itself under a change that must not matter.

func propJSON(v any) string { b, _ := json.Marshal(v); return string(b) }

// propFindings is the findings of a graph as text.
func propFindings(g *Graph) string { return propJSON(Validate(g)) }

func propDecode(t testing.TB, data []byte) *Graph {
	t.Helper()
	g, _ := Decode(data)
	return g
}

func propShuffle(r *rand.Rand, g *Graph) {
	r.Shuffle(len(g.Nodes), func(i, j int) { g.Nodes[i], g.Nodes[j] = g.Nodes[j], g.Nodes[i] })
	r.Shuffle(len(g.Plates), func(i, j int) { g.Plates[i], g.Plates[j] = g.Plates[j], g.Plates[i] })
}

// propSample is the documents the properties run on: the corpus and the generated graphs.
func propSample(t testing.TB) map[string][]byte {
	docs := corpusInputs(t)
	r := rand.New(rand.NewSource(7))
	for i := 0; i < 60; i++ {
		docs[fmt.Sprintf("generated-%02d", i)] = propGenerate(r)
	}
	return docs
}

func propNames(docs map[string][]byte) []string {
	var ns []string
	for n := range docs {
		ns = append(ns, n)
	}
	sort.Strings(ns)
	return ns
}

func TestPropertyCanonicalRoundTrip(t *testing.T) {
	docs := propSample(t)
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil {
			continue
		}
		c1, err := Canonical(g)
		if err != nil {
			continue
		}
		g2, fs := Decode(c1)
		if g2 == nil {
			t.Errorf("%s: the canonical form does not decode: %+v", name, fs)
			continue
		}
		c2, err := Canonical(g2)
		if err != nil || !bytes.Equal(c1, c2) {
			t.Errorf("%s: Canonical(Decode(Canonical(g))) differs:\n%s\n%s (%v)", name, c1, c2, err)
		}
		if propFindings(g) != propFindings(g2) {
			t.Errorf("%s: the canonical form has other findings", name)
		}
	}
}

func TestPropertyLayoutIsNotTheModel(t *testing.T) {
	docs := propSample(t)
	r := rand.New(rand.NewSource(11))
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil {
			continue
		}
		h1, err := SemanticHash(g)
		if err != nil {
			continue
		}
		f1 := propFindings(g)
		w := 10.0
		g.Layout = map[string]Box{}
		for _, n := range g.Nodes {
			g.Layout[n.ID] = Box{X: r.Float64() * 1000, Y: r.Float64() * 1000, W: &w}
		}
		for _, p := range g.Plates {
			g.Layout[p.ID] = Box{X: r.Float64(), Y: r.Float64(), W: &w, H: &w}
		}
		g.Layout["nobody"] = Box{X: 1, Y: 2}
		h2, err := SemanticHash(g)
		if err != nil || h1 != h2 {
			t.Errorf("%s: the layout changed the hash (%v)", name, err)
		}
		if f2 := propFindings(g); f1 != f2 {
			t.Errorf("%s: the layout changed the findings:\n%s\n%s", name, f1, f2)
		}
		g.Layout = nil
		if h3, _ := SemanticHash(g); h3 != h1 {
			t.Errorf("%s: dropping the layout changed the hash", name)
		}
	}
}

func TestPropertyOrderOfNodesAndPlatesIsNotTheModel(t *testing.T) {
	docs := propSample(t)
	r := rand.New(rand.NewSource(13))
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil {
			continue
		}
		c1, err1 := Canonical(g)
		h1, _ := SemanticHash(g)
		f1 := propFindings(g)
		// with a duplicate id the canonical order of the two holders is the document's: the graph is refused anyway
		dupID := false
		for _, f := range Validate(g) {
			dupID = dupID || f.Code == "graph.id"
		}
		for k := 0; k < 3; k++ {
			h := propDecode(t, docs[name])
			propShuffle(r, h)
			c2, err2 := Canonical(h)
			if !dupID && ((err1 == nil) != (err2 == nil) || !bytes.Equal(c1, c2)) {
				t.Errorf("%s: the canonical bytes depend on the order of the JSON", name)
			}
			if h2, _ := SemanticHash(h); !dupID && h2 != h1 {
				t.Errorf("%s: the hash depends on the order of the JSON", name)
			}
			if f2 := propFindings(h); f1 != f2 {
				t.Errorf("%s: the findings depend on the order of the JSON:\n%s\n%s", name, f1, f2)
				break
			}
		}
	}
}

// the cycle's names are the nodes' names or, without one, their labels: the one place where a label
// is in a finding.
func propWithoutLabels(fs []Finding) string {
	out := make([]Finding, len(fs))
	for i, f := range fs {
		out[i] = f
		if f.Code == "graph.cycle" {
			a := map[string]any{}
			for k, v := range f.Args {
				if k != "names" {
					a[k] = v
				}
			}
			out[i].Args = a
		}
	}
	return propJSON(out)
}

func TestPropertyRenamingALabelChangesTheHashNotTheFindings(t *testing.T) {
	docs := propSample(t)
	r := rand.New(rand.NewSource(17))
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil || len(g.Nodes) == 0 {
			continue
		}
		fs := Validate(g)
		bad := map[string]bool{}
		for _, f := range fs {
			if f.Code == "graph.label" || f.Code == "graph.id" || f.Code == "graph.rows" {
				bad[f.Node] = true
			}
		}
		if len(bad) > 0 {
			continue
		}
		i := r.Intn(len(g.Nodes))
		h1, err := SemanticHash(g)
		if err != nil {
			continue
		}
		g.Nodes[i].Label = g.Nodes[i].Label + " (renamed)"
		h2, err := SemanticHash(g)
		if err != nil || h1 == h2 {
			t.Errorf("%s: renaming a label did not change the hash (%v)", name, err)
		}
		if a, b := propWithoutLabels(fs), propWithoutLabels(Validate(g)); a != b {
			t.Errorf("%s: renaming a label changed the findings:\n%s\n%s", name, a, b)
		}
	}
}

func TestPropertyGeneratedGraphsAreValid(t *testing.T) {
	r := rand.New(rand.NewSource(3))
	for i := 0; i < 300; i++ {
		doc := propGenerate(r)
		g, fs := Decode(doc)
		if g == nil {
			t.Fatalf("graph %d does not decode: %+v\n%s", i, fs, doc)
		}
		if fs := Validate(g); len(fs) != 0 {
			t.Fatalf("graph %d: %s\n%s", i, codes(fs), doc)
		}
		if m := BuildModel(g); !m.Valid() || len(m.Order) != len(g.Nodes) {
			t.Fatalf("graph %d: model not valid or order incomplete\n%s", i, doc)
		}
	}
}

// the mutations: a seeded walk into the JSON tree that breaks, removes, duplicates or retypes one value.
func propMutate(r *rand.Rand, tree any) any {
	var paths [][]any
	var walk func(v any, path []any)
	walk = func(v any, path []any) {
		paths = append(paths, append([]any(nil), path...))
		switch x := v.(type) {
		case map[string]any:
			for k, c := range x {
				walk(c, append(path, k))
			}
		case []any:
			for i, c := range x {
				walk(c, append(path, i))
			}
		}
	}
	walk(tree, nil)
	var inner [][]any
	for _, p := range paths {
		if len(p) > 0 {
			inner = append(inner, p)
		}
	}
	if len(inner) == 0 {
		return tree
	}
	sort.Slice(inner, func(i, j int) bool { return fmt.Sprint(inner[i]) < fmt.Sprint(inner[j]) })
	path := inner[r.Intn(len(inner))]
	repl := []any{nil, 0, -1, 1e300, "", "x", "n1", "rows", true, []any{}, map[string]any{}, "a‮b", "p1", "d1"}
	var set func(v any, path []any) any
	set = func(v any, path []any) any {
		if len(path) == 1 {
			switch x := v.(type) {
			case map[string]any:
				k := path[0].(string)
				switch r.Intn(3) {
				case 0:
					delete(x, k)
				case 1:
					x[k] = repl[r.Intn(len(repl))]
				default:
					x[k+"x"] = x[k]
				}
			case []any:
				i := path[0].(int)
				if i >= len(x) {
					return v
				}
				switch r.Intn(3) {
				case 0:
					return append(x[:i:i], x[i+1:]...)
				case 1:
					x[i] = repl[r.Intn(len(repl))]
				default:
					return append(x, x[i])
				}
			}
			return v
		}
		switch x := v.(type) {
		case map[string]any:
			k, ok := path[0].(string)
			if _, there := x[k]; ok && there {
				x[k] = set(x[k], path[1:])
			}
		case []any:
			if i, ok := path[0].(int); ok && i < len(x) {
				x[i] = set(x[i], path[1:])
			}
		}
		return v
	}
	return set(tree, path)
}

func propMutated(t testing.TB, seed int64) map[string][]byte {
	docs := propSample(t)
	r := rand.New(rand.NewSource(seed))
	out := map[string][]byte{}
	for _, name := range propNames(docs) {
		var tree any
		if json.Unmarshal(docs[name], &tree) != nil {
			continue
		}
		for k := 0; k < 4; k++ {
			var copyTree any
			_ = json.Unmarshal(docs[name], &copyTree)
			for n := 1 + r.Intn(3); n > 0; n-- {
				copyTree = propMutate(r, copyTree)
			}
			if b, err := json.Marshal(copyTree); err == nil {
				out[fmt.Sprintf("%s~%d", name, k)] = b
			}
		}
	}
	return out
}

func TestPropertyBuildModelNeverPanics(t *testing.T) {
	docs := propMutated(t, 23)
	for name, b := range propSample(t) {
		docs[name] = b
	}
	decoded := 0
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil {
			continue
		}
		decoded++
		func() {
			defer func() {
				if p := recover(); p != nil {
					t.Errorf("%s: panic %v\n%s", name, p, docs[name])
				}
			}()
			m := BuildModel(g)
			if m == nil {
				t.Errorf("%s: nil model", name)
				return
			}
			if f := Validate(g); propJSON(f) != propJSON(m.Findings) {
				t.Errorf("%s: Validate and BuildModel disagree", name)
			}
			_, _ = Canonical(g)
			_, _ = SemanticHash(g)
		}()
	}
	if decoded < 200 {
		t.Errorf("only %d documents decode", decoded)
	}
}

func TestPropertyMutatedGraphsKeepTheirProperties(t *testing.T) {
	docs := propMutated(t, 29)
	r := rand.New(rand.NewSource(31))
	for _, name := range propNames(docs) {
		g := propDecode(t, docs[name])
		if g == nil {
			continue
		}
		f1 := propFindings(g)
		h := propDecode(t, docs[name])
		propShuffle(r, h)
		if f2 := propFindings(h); f1 != f2 {
			t.Errorf("%s: the findings depend on the order of the JSON:\n%s\n%s\n%s", name, f1, f2, docs[name])
		}
		c1, err := Canonical(g)
		if err != nil {
			continue
		}
		if g2, _ := Decode(c1); g2 == nil {
			t.Errorf("%s: the canonical form does not decode\n%s", name, docs[name])
		} else if c2, _ := Canonical(g2); !bytes.Equal(c1, c2) {
			t.Errorf("%s: the canonical form is not stable\n%s", name, docs[name])
		}
	}
}

// propGenerate writes a valid graph of one of the four families, with random sizes, names, ids and
// order.
func propGenerate(r *rand.Rand) []byte {
	ids := map[string]bool{"rows": true}
	id := func(prefix string) string {
		for {
			s := prefix + fmt.Sprint(r.Intn(1000)) + string(rune('a'+r.Intn(26)))
			if !ids[s] {
				ids[s] = true
				return s
			}
		}
	}
	names := map[string]bool{}
	nm := func(base string) string {
		for {
			s := base + "_" + fmt.Sprint(r.Intn(100000))
			if !names[s] {
				names[s] = true
				return s
			}
		}
	}
	norm := func(mu, sigma string) map[string]any { return normalPrior(expr(mu), expr(sigma)) }
	expo := map[string]any{"dist": "exponential", "args": map[string]any{"beta": expr("1")}}
	rowsPlate := map[string]any{"id": "rows", "kind": "rows", "label": "Rows"}
	var plates, nodes []any
	add := func(n map[string]any) map[string]any { nodes = append(nodes, n); return n }
	data := func(base, typ string, extra map[string]any) (string, string) {
		i, n := id("d"), nm(base)
		d := map[string]any{"id": i, "kind": "data", "name": n, "label": "Data " + n, "plate": "rows", "type": typ}
		for k, v := range extra {
			d[k] = v
		}
		add(d)
		return i, n
	}
	par := func(base string, plate any, prior map[string]any) (string, string) {
		i, n := id("m"), nm(base)
		add(map[string]any{"id": i, "kind": "parameter", "name": n, "label": "Parameter " + n, "plate": plate, "prior": prior})
		return i, n
	}
	switch r.Intn(4) {
	case 0: // a regression with a linear predictor
		y, _ := data("y", "real", nil)
		_, a := par("alpha", nil, norm("0", "5"))
		_, s := par("sigma", nil, expo)
		var terms []any
		for k, n := 0, 1+r.Intn(6); k < n; k++ {
			switch r.Intn(3) {
			case 0:
				d, _ := data("x", "real", map[string]any{"standardize": r.Intn(2) == 0})
				b, _ := par("b", nil, norm("0", "1"))
				terms = append(terms, map[string]any{"data": d, "coef": ref(b)})
			case 1:
				d, _ := data("c", "int", map[string]any{"support": []string{"any", "nonnegative", "binary"}[r.Intn(3)]})
				_, b := par("b", nil, norm("0", "1"))
				terms = append(terms, map[string]any{"data": d, "coef": expr(b)})
			default:
				d, _ := data("k", "category", nil)
				b, _ := par("b", "levels:"+d, norm("0", "1"))
				terms = append(terms, map[string]any{"data": d, "coef": ref(b)})
			}
		}
		_, eta := "", nm("eta")
		add(map[string]any{"id": id("q"), "kind": "linear_predictor", "name": eta, "label": "Predictor", "plate": "rows",
			"intercept": expr(a), "terms": terms})
		add(map[string]any{"id": id("l"), "kind": "likelihood", "label": "Outcome", "observed": y, "dist": "normal",
			"args": map[string]any{"mu": expr(eta), "sigma": expr(s)}})
		plates = []any{rowsPlate}
	case 1: // a hierarchy of one or two levels, centred or not
		depth := 1 + r.Intn(2)
		y, _ := data("y", "real", nil)
		_, mu := par("mu", nil, norm("0", "5"))
		_, tau := par("tau", nil, expo)
		_, s := par("sigma", nil, expo)
		plates = []any{rowsPlate}
		form := func() string { return []string{"centred", "non_centred"}[r.Intn(2)] }
		var gi, gn, pid []string
		parent := any(nil)
		for l := 0; l < depth; l++ {
			p := id("p")
			k, n := id("d"), nm("g")
			plates = append(plates, map[string]any{"id": p, "kind": "group", "label": "Level", "parent": parent, "key": k, "form": form()})
			add(map[string]any{"id": k, "kind": "data", "name": n, "label": "Key " + n, "plate": "rows", "type": "group_index", "target": p})
			parent, gi, gn, pid = p, append(gi, k), append(gn, n), append(pid, p)
		}
		prev, prevIdx := mu, ""
		var last, lastIdx string
		for l := 0; l < depth; l++ {
			m := prev
			if prevIdx != "" {
				m = prev + "[" + prevIdx + "]"
			}
			_, an := par("a", pid[l], norm(m, tau))
			prev, prevIdx = an, gn[l]
			last, lastIdx = an, gn[l]
		}
		add(map[string]any{"id": id("l"), "kind": "likelihood", "label": "Outcome", "observed": y, "dist": "normal",
			"args": map[string]any{"mu": expr(last + "[" + lastIdx + "]"), "sigma": expr(s)}})
	case 2: // an autoregression
		t, _ := data("t", "time", nil)
		y, yn := data("y", "real", nil)
		_, c := par("c", nil, norm("0", "5"))
		_, s := par("sigma", nil, expo)
		text := c
		for k, p := 1, 1+r.Intn(4); k <= p; k++ {
			_, phi := par("phi", nil, norm("0", "1"))
			text += fmt.Sprintf(" + %s * lag(%s, %d)", phi, yn, k)
		}
		mn := nm("mean")
		add(map[string]any{"id": id("t"), "kind": "transformed", "name": mn, "label": "Expected", "plate": "rows", "value": expr(text)})
		add(map[string]any{"id": id("l"), "kind": "likelihood", "label": "Outcome", "observed": y, "dist": "normal",
			"args": map[string]any{"mu": expr(mn), "sigma": expr(s)}})
		plates = []any{map[string]any{"id": "rows", "kind": "rows", "label": "Rows", "series": map[string]any{"time": t, "by": nil}}}
	default: // a local level
		t, _ := data("t", "time", nil)
		y, _ := data("y", "real", nil)
		_, s1 := par("sl", nil, expo)
		_, s2 := par("so", nil, expo)
		st := nm("level")
		add(map[string]any{"id": id("s"), "kind": "state", "name": st, "label": "Level", "plate": "rows", "process": "random_walk",
			"form": []string{"centred", "non_centred"}[r.Intn(2)], "init": norm("0", fmt.Sprint(1+r.Intn(20))), "scale": expr(s1)})
		add(map[string]any{"id": id("l"), "kind": "likelihood", "label": "Outcome", "observed": y, "dist": "normal",
			"args": map[string]any{"mu": expr(st), "sigma": expr(s2)}})
		plates = []any{map[string]any{"id": "rows", "kind": "rows", "label": "Rows", "series": map[string]any{"time": t, "by": nil}}}
	}
	r.Shuffle(len(nodes), func(i, j int) { nodes[i], nodes[j] = nodes[j], nodes[i] })
	r.Shuffle(len(plates), func(i, j int) { plates[i], plates[j] = plates[j], plates[i] })
	b, err := json.Marshal(map[string]any{"format_version": 1, "plates": plates, "nodes": nodes})
	if err != nil {
		panic(err)
	}
	return b
}
