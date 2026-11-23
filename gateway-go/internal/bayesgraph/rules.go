package bayesgraph

import (
	"sort"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
)

// This file is the rules about what a model is made of (docs/specs/bayes-model-graph.md,
// «Validation»): graph.likelihood, graph.terms, graph.effects, graph.state, graph.cycle,
// graph.self_predictor, graph.unused_data, graph.unused_parameter, graph.form_centred,
// graph.plate_empty, and graph.too_big for max_plates counting the levels plates.
//
// What the other files already cover is not said twice: graph.node_plate says that a state is on rows
// and a correlated-effects node on a group plate, that a likelihood is on its observed node's
// plate; graph.slot_interval, graph.slot_shape and graph.constant say that a scale is a positive
// scalar and eta a positive constant; graph.slot says that a state's scale is missing.

// modelRules runs the rules of this file, after the slots are typed.
func (v *validation) modelRules() {
	v.platesTotal()
	v.likelihoods()
	v.terms()
	v.effects()
	v.states()
	v.deps = v.dependencies()
	v.cycles(v.deps)
	v.selfPredictors()
	v.unused()
	v.formCentred()
	v.plateEmpty()
}

// specByKey is the specs by their key.
func (v *validation) specByKey() map[SlotKey]*slotSpec {
	m := make(map[SlotKey]*slotSpec, len(v.specs))
	for _, s := range v.specs {
		if _, dup := m[s.key]; !dup {
			m[s.key] = s
		}
	}
	return m
}

// platesTotal: graph.too_big for max_plates, counting the levels plates (one per category data
// node) with the stored ones. (CheckStructure counts the stored ones alone and says so first.)
func (v *validation) platesTotal() {
	stored := len(v.g.Plates)
	if stored > v.lim.MaxPlates {
		return
	}
	total := stored
	for _, n := range v.env.nodes {
		if n.Kind == "data" && n.Type == "category" {
			total++
		}
	}
	if total > v.lim.MaxPlates {
		v.out = append(v.out, tooBig("plates", total, v.lim.MaxPlates)...)
	}
}

// outcomeKind is what a data node holds as an outcome: real, binary (int, support binary), count
// (int, support nonnegative), integer (int, any support), or "" for what cannot be observed or is
// not known.
func outcomeKind(n *Node) string {
	switch n.Type {
	case "real":
		return "real"
	case "int":
		switch n.Support {
		case "binary":
			return "binary"
		case "nonnegative":
			return "count"
		case "any":
			return "integer"
		}
	case "group_index", "category", "time":
		return "other"
	}
	return ""
}

// outcomeNeed is what a likelihood's outcome must be: the catalogue's outcome of the distribution
// (real; binary; zero to N and counts, both a count or a binary), as the id of the finding's args and
// the kinds of data that fit.
func outcomeNeed(d bayescatalog.Distribution) (need string, fits []string) {
	switch d.Outcome {
	case bayescatalog.RealLine:
		return "real", []string{"real"}
	case bayescatalog.Binary:
		return "binary", []string{"binary"}
	case bayescatalog.ZeroToN, bayescatalog.Counts:
		return "count", []string{"count", "binary"}
	}
	return "", nil
}

// likelihoods: graph.likelihood. At least one; its observed node a data node observed once; its
// distribution one of the catalogue's likelihoods with exactly its arguments; the outcome fits the
// distribution; binomial_logit's N a nonnegative integer data node on the observed node's plate.
func (v *validation) likelihoods() {
	var ls []*Node
	for _, n := range v.nodeList() {
		if n.Kind == "likelihood" {
			ls = append(ls, n)
		}
	}
	if len(ls) == 0 {
		v.error("graph.likelihood", "", "", map[string]any{"reason": "none"})
		return
	}
	byObserved := map[string][]string{}
	for _, l := range ls {
		if o, ok := v.env.nodes[l.Observed]; ok && o.Kind == "data" {
			byObserved[o.ID] = append(byObserved[o.ID], l.ID)
		}
	}
	specs := v.specByKey()
	for _, l := range ls {
		obs, ok := v.env.nodes[l.Observed]
		switch {
		case l.Observed == "":
			v.error("graph.likelihood", l.ID, "observed", map[string]any{"reason": "missing"})
		case !ok:
			v.error("graph.likelihood", l.ID, "observed", map[string]any{"reason": "unknown_node", "observed": l.Observed})
		case obs.Kind != "data":
			v.error("graph.likelihood", l.ID, "observed", map[string]any{"reason": "not_data", "observed": l.Observed})
		case len(byObserved[obs.ID]) > 1:
			v.error("graph.likelihood", l.ID, "observed", map[string]any{"reason": "observed_twice", "observed": obs.ID, "count": len(byObserved[obs.ID])})
		}
		if !ok || obs.Kind != "data" {
			obs = nil
		}
		d, known := v.likelihoodDist(l)
		if !known {
			continue
		}
		if obs != nil {
			need, fits := outcomeNeed(d)
			kind := outcomeKind(obs)
			if kind != "" && !contains(fits, kind) {
				args := map[string]any{"reason": "outcome", "dist": d.ID, "need": need, "type": obs.Type}
				if obs.Type == "int" {
					args["support"] = obs.Support
				}
				v.error("graph.likelihood", l.ID, "observed", args)
			}
		}
		for _, p := range d.Params {
			if !p.Data {
				continue
			}
			if s, has := specs[SlotKey{l.ID, "args." + p.Name}]; has && s.ok {
				v.dataArgument(l, obs, d, p.Name, s)
			}
		}
	}
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

// likelihoodDist: the likelihood's distribution and arguments, reporting what is wrong with them;
// known is false when the distribution cannot be used.
func (v *validation) likelihoodDist(l *Node) (bayescatalog.Distribution, bool) {
	if l.Dist == "" {
		v.error("graph.likelihood", l.ID, "dist", map[string]any{"reason": "missing"})
		return bayescatalog.Distribution{}, false
	}
	d, found := bayescatalog.LookupDistribution(l.Dist)
	switch {
	case !found:
		v.error("graph.likelihood", l.ID, "dist", map[string]any{"reason": "unknown_dist", "dist": l.Dist})
		return d, false
	case !d.HasUse(bayescatalog.Likelihood):
		v.error("graph.likelihood", l.ID, "dist", map[string]any{"reason": "not_likelihood", "dist": d.ID})
		return d, false
	}
	v.arguments(l, "args.", d, l.Args, "graph.likelihood")
	return d, true
}

// arguments: a distribution's arguments are exactly its parameters, in the slots prefix + name.
func (v *validation) arguments(n *Node, prefix string, d bayescatalog.Distribution, args map[string]Slot, code string) bool {
	ok := true
	want := map[string]bool{}
	for _, p := range d.Params {
		want[p.Name] = true
		if _, has := args[p.Name]; !has {
			v.error(code, n.ID, prefix+p.Name, map[string]any{"reason": "missing_arg", "dist": d.ID, "arg": p.Name})
			ok = false
		}
	}
	for arg := range args {
		if !want[arg] {
			v.error(code, n.ID, prefix+arg, map[string]any{"reason": "unknown_arg", "dist": d.ID, "arg": arg})
			ok = false
		}
	}
	return ok
}

// dataArgument: an argument that is data (the binomial's N) is a data node, written by its name or a
// ref, an integer with a support of zero or more, on the observed node's plate.
func (v *validation) dataArgument(l, obs *Node, d bayescatalog.Distribution, arg string, s *slotSpec) {
	slot := "args." + arg
	id := ""
	switch {
	case s.slot.Ref != nil:
		id = *s.slot.Ref
	case s.tree != nil && s.tree.Kind == bayesexpr.KindName:
		sym, found := v.env.byName[s.tree.Name]
		if !found {
			return // an unknown name: expr.unknown_name says so
		}
		id = sym.ID
	}
	n, isNode := v.env.nodes[id]
	if !isNode || n.Kind != "data" {
		if _, known := v.env.lookupID(id); !known && s.slot.Ref != nil {
			return // graph.slot unknown_ref says so
		}
		v.error("graph.likelihood", l.ID, slot, map[string]any{"reason": "arg_not_data", "dist": d.ID, "arg": arg})
		return
	}
	if k := outcomeKind(n); k != "count" && k != "binary" {
		args := map[string]any{"reason": "arg_type", "dist": d.ID, "arg": arg, "type": n.Type}
		if n.Type == "int" {
			args["support"] = n.Support
		}
		v.error("graph.likelihood", l.ID, slot, args)
		return
	}
	if obs != nil && plateOf(n) != plateOf(obs) {
		v.error("graph.likelihood", l.ID, slot, map[string]any{"reason": "arg_plate", "dist": d.ID, "arg": arg,
			"plate": string(plateOf(n)), "observed": string(plateOf(obs))})
	}
}

// terms: graph.terms. A linear predictor's terms are a real or integer data node with a scalar
// parameter as its coefficient, or a category with a parameter on its levels plate; each data node
// once, on the predictor's plate.
func (v *validation) terms() {
	specs := v.specByKey()
	for _, n := range v.nodeList() {
		if n.Kind != "linear_predictor" {
			continue
		}
		count := map[string]int{}
		for _, t := range n.Terms {
			count[t.Data]++
		}
		for i, t := range n.Terms {
			dataSlot := "terms[" + strconv.Itoa(i) + "].data"
			coefSlot := "terms[" + strconv.Itoa(i) + "].coef"
			d, ok := v.env.nodes[t.Data]
			if !ok || d.Kind != "data" {
				v.error("graph.terms", n.ID, dataSlot, map[string]any{"reason": "not_data", "data": t.Data})
				continue
			}
			if count[t.Data] > 1 {
				v.error("graph.terms", n.ID, dataSlot, map[string]any{"reason": "duplicate", "data": t.Data, "count": count[t.Data]})
			}
			if d.Type == "group_index" || d.Type == "time" {
				v.error("graph.terms", n.ID, dataSlot, map[string]any{"reason": "data_type", "data": t.Data, "type": d.Type})
				continue
			}
			if plateOf(d) != plateOf(n) {
				v.error("graph.terms", n.ID, dataSlot, map[string]any{"reason": "plate", "data": t.Data,
					"plate": string(plateOf(d)), "expected": string(plateOf(n))})
			}
			if d.Type != "real" && d.Type != "int" && d.Type != "category" {
				continue
			}
			s, has := specs[SlotKey{n.ID, coefSlot}]
			if !has || !s.ok {
				continue
			}
			if _, typed := v.slots[s.key]; !typed {
				continue // its own finding says why
			}
			p := v.coefficient(s)
			if p == nil {
				v.error("graph.terms", n.ID, coefSlot, map[string]any{"reason": "coef_not_parameter", "data": t.Data})
				continue
			}
			need, plate := "scalar", ""
			if d.Type == "category" {
				need, plate = "levels", LevelsPlate(d.ID)
			}
			if string(plateOf(p)) != plate {
				v.error("graph.terms", n.ID, coefSlot, map[string]any{"reason": "coef_plate", "data": t.Data,
					"need": need, "plate": string(plateOf(p))})
			}
		}
	}
}

// coefficient is the parameter a coefficient slot names (a bare name or a ref), nil when the slot is
// anything else.
func (v *validation) coefficient(s *slotSpec) *Node {
	id := ""
	switch {
	case s.slot.Ref != nil:
		id = *s.slot.Ref
	case s.tree != nil && s.tree.Kind == bayesexpr.KindName:
		sym, ok := v.env.byName[s.tree.Name]
		if !ok {
			return nil
		}
		id = sym.ID
	}
	if n, ok := v.env.nodes[id]; ok && n.Kind == "parameter" {
		return n
	}
	return nil
}

// effects: graph.effects. A correlated-effects node has 2 to max_effect_components components and its
// correlation. (The components' names are optional: a component without one is written by its id,
// as a ref; their uniqueness and form are CheckStructure's; the scales and eta, the slots'.)
func (v *validation) effects() {
	for _, n := range v.nodeList() {
		if n.Kind != "correlated_effects" {
			continue
		}
		if k := len(n.Components); k < 2 || k > v.lim.MaxEffectComponents {
			v.error("graph.effects", n.ID, "components", map[string]any{"reason": "count", "got": k, "min": 2, "max": v.lim.MaxEffectComponents})
		}
		if n.Correlation == nil {
			v.error("graph.effects", n.ID, "correlation", map[string]any{"reason": "missing"})
		}
	}
}

// states: graph.state. A state is on the rows of a series, is a random walk, has a proper prior for
// its first value (init), a form the plates know; its scale is the slots'.
func (v *validation) states() {
	for _, n := range v.nodeList() {
		if n.Kind != "state" {
			continue
		}
		if ep, ok := v.env.plates[plateOf(n)]; ok && ep.kind == kindRows && !ep.series {
			v.error("graph.state", n.ID, "plate", map[string]any{"reason": "not_series", "plate": string(plateOf(n))})
		}
		if n.Process != "random_walk" {
			v.error("graph.state", n.ID, "process", map[string]any{"reason": "process", "process": n.Process})
		}
		if n.Form != "" && n.Form != "non_centred" && n.Form != "centred" {
			v.error("graph.state", n.ID, "form", map[string]any{"reason": "form", "form": n.Form})
		}
		v.stateInit(n)
	}
}

// stateInit: the state's init is a proper prior of the catalogue with exactly its arguments (LKJ
// and the internal distributions are not priors).
func (v *validation) stateInit(n *Node) {
	if n.Init == nil {
		v.error("graph.state", n.ID, "init", map[string]any{"reason": "missing"})
		return
	}
	d, found := bayescatalog.LookupDistribution(n.Init.Dist)
	switch {
	case !found:
		v.error("graph.state", n.ID, "init", map[string]any{"reason": "unknown_dist", "dist": n.Init.Dist})
		return
	case d.Support == bayescatalog.CholeskyCorrelation:
		v.error("graph.state", n.ID, "init", map[string]any{"reason": "effects_only", "dist": d.ID})
		return
	case !d.HasUse(bayescatalog.Prior):
		v.error("graph.state", n.ID, "init", map[string]any{"reason": "not_prior", "dist": d.ID})
		return
	}
	v.arguments(n, "init.", d, n.Init.Args, "graph.state")
}

// selfPredictors: graph.self_predictor. A likelihood's arguments read the data it observes, directly
// or through transformed and linear-predictor nodes, without lag (lag(y, 1) is the previous row). The
// finding is on the likelihood's slot, with the observed node and, when it is reached through another
// node, the node the slot reads it through (the smallest such id).
func (v *validation) selfPredictors() {
	plainOf := func(s *slotSpec) []string {
		var ids []string
		for _, id := range v.readSets(s).plain {
			ids = append(ids, ownerOf(id))
		}
		return ids
	}
	via := func(id string) bool {
		n, ok := v.env.nodes[id]
		return ok && (n.Kind == "transformed" || n.Kind == "linear_predictor")
	}
	for _, l := range v.nodeList() {
		o, ok := v.env.nodes[l.Observed]
		if l.Kind != "likelihood" || !ok || o.Kind != "data" {
			continue
		}
		reaches := func(start string) bool {
			seen, queue := map[string]bool{start: true}, []string{start}
			for len(queue) > 0 {
				cur := queue[0]
				queue = queue[1:]
				for _, r := range v.deps.plain[cur] {
					switch {
					case r == o.ID:
						return true
					case via(r) && !seen[r]:
						seen[r] = true
						queue = append(queue, r)
					}
				}
			}
			return false
		}
		for _, s := range v.specs {
			if s.owner != l {
				continue
			}
			direct, through := false, ""
			for _, r := range plainOf(s) {
				switch {
				case r == o.ID:
					direct = true
				case via(r) && reaches(r) && (through == "" || r < through):
					through = r
				}
			}
			switch {
			case direct:
				v.error("graph.self_predictor", l.ID, s.key.Slot, map[string]any{"observed": o.ID})
			case through != "":
				v.error("graph.self_predictor", l.ID, s.key.Slot, map[string]any{"observed": o.ID, "through": through})
			}
		}
	}
}

// unused: graph.unused_data (an error: every data node must be bound, so a data node nothing uses is
// a trap) and graph.unused_parameter (a warning: a parameter on no path to a likelihood has its prior
// for its posterior). A data node is used when a slot or a term reads it, a likelihood observes it,
// a group plate has it for its key or a series for its time. Without any likelihood every parameter
// would be unused: graph.likelihood says so once, and this says nothing.
func (v *validation) unused() {
	used := map[string]bool{}
	for _, e := range v.deps.edges {
		used[ownerOf(e.From)] = true
	}
	for _, p := range v.g.Plates {
		if p.Key != "" {
			used[p.Key] = true
		}
		if p.Series != nil {
			used[p.Series.Time] = true
		}
	}
	for _, n := range v.nodeList() {
		if n.Kind == "data" && !used[n.ID] {
			v.error("graph.unused_data", n.ID, "", nil)
		}
	}
	var seen []string
	reach := map[string]bool{}
	for _, n := range v.nodeList() {
		if n.Kind == "likelihood" {
			reach[n.ID] = true
			seen = append(seen, n.ID)
		}
	}
	if len(seen) == 0 {
		return
	}
	for len(seen) > 0 {
		cur := seen[len(seen)-1]
		seen = seen[:len(seen)-1]
		for _, w := range v.deps.reads[cur] {
			if !reach[w] {
				reach[w] = true
				seen = append(seen, w)
			}
		}
	}
	for _, n := range v.nodeList() {
		if n.Kind == "parameter" && !reach[n.ID] {
			v.add(Warning, "graph.unused_parameter", n.ID, "", nil)
		}
	}
}

// hasBounds: a parameter with a bound written.
func hasBounds(n *Node) bool {
	return n.Bounds != nil && (n.Bounds.Lower != nil || n.Bounds.Upper != nil)
}

// nonCentredForm: whether a group plate or a state asks for the non-centred form: its form is not
// "centred" (the default is non-centred).
func nonCentredForm(form string) bool { return form != "centred" }

// formCentred: graph.form_centred, the non-centred form that cannot be had, and the parameters that
// keep the form they can have: on a group plate whose form is non-centred, a parameter with bounds
// (Stan's affine transform does not combine with a bound) or a prior that is not location-scale; a
// state with a non-centred form whose init is not location-scale. Each stays centred, and the person
// is told. The parameters' NonCentred is set here.
func (v *validation) formCentred() {
	groupForm := map[string]string{}
	for _, p := range v.g.Plates {
		if p.Kind == kindGrp {
			if _, dup := groupForm[p.ID]; !dup {
				groupForm[p.ID] = p.Form
			}
		}
	}
	for _, n := range v.nodeList() {
		switch n.Kind {
		case "parameter":
			form, onGroup := groupForm[string(plateOf(n))]
			if !onGroup || !nonCentredForm(form) || n.Prior == nil {
				continue
			}
			bounded := hasBounds(n)
			if bounded {
				v.add(Warning, "graph.form_centred", n.ID, "bounds", map[string]any{"reason": "bounded", "plate": string(plateOf(n))})
			}
			d, found := bayescatalog.LookupDistribution(n.Prior.Dist)
			if !found || !d.HasUse(bayescatalog.Prior) || d.Support == bayescatalog.CholeskyCorrelation {
				continue
			}
			locScale := d.LocationScale != nil
			if !locScale {
				v.add(Warning, "graph.form_centred", n.ID, "prior", map[string]any{"reason": "not_location_scale", "dist": d.ID, "plate": string(plateOf(n))})
			}
			if p, ok := v.params[n.ID]; ok {
				p.NonCentred = locScale && !bounded
				v.params[n.ID] = p
			}
		case "state":
			if n.Init == nil || !nonCentredForm(n.Form) {
				continue
			}
			d, found := bayescatalog.LookupDistribution(n.Init.Dist)
			if !found || !d.HasUse(bayescatalog.Prior) || d.Support == bayescatalog.CholeskyCorrelation {
				continue
			}
			if d.LocationScale == nil {
				v.add(Warning, "graph.form_centred", n.ID, "init", map[string]any{"reason": "not_location_scale", "dist": d.ID})
			}
		}
	}
}

// plateEmpty: graph.plate_empty, a group plate with no node on it.
func (v *validation) plateEmpty() {
	on := map[string]bool{}
	for _, n := range v.g.Nodes {
		if n.Plate != nil {
			on[*n.Plate] = true
		}
	}
	var ids []string
	for _, p := range v.g.Plates {
		if p.Kind == kindGrp && !on[p.ID] {
			ids = append(ids, p.ID)
		}
	}
	sort.Strings(ids)
	for i, id := range ids {
		if i > 0 && ids[i-1] == id {
			continue
		}
		v.add(Warning, "graph.plate_empty", id, "", nil)
	}
}
