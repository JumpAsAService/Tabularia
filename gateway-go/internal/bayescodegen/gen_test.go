package bayescodegen

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"strconv"
	"strings"
)

// The generator of valid graphs the properties and the fuzzer share (docs/specs/bayes-stan-codegen.md,
// «Testing strategy»). Every choice it makes comes from a chooser: a seeded random source for the
// properties, the fuzzer's bytes for FuzzGenerate (so that the fuzzer's mutations move the choices
// one by one). It builds the model families of the editor (regressions with a linear predictor and
// every likelihood, hierarchies of one and two levels, correlated effects, time series with lags and
// latent states) with random sizes, names (absent and reserved ones included), ids and orders, and
// reaches the splitting of large vector expressions (lot S5) in every place its table lists.
//
// It does not promise a valid graph: the validator is the judge, and the properties count how many
// of the graphs it writes pass it.

// chooser gives the generator its choices.
type chooser interface {
	// pick returns a number in [0, n).
	pick(n int) int
}

type randChooser struct{ r *rand.Rand }

func (c randChooser) pick(n int) int { return c.r.IntN(n) }

// byteChooser reads the choices off a fuzzer's bytes; past their end every choice is the first.
type byteChooser struct {
	b []byte
	i int
}

func (c *byteChooser) pick(n int) int {
	if n <= 1 || c.i >= len(c.b) {
		return 0
	}
	v := int(c.b[c.i])
	c.i++
	return v % n
}

// reservedNames are names a person may give a node that the program must not use as they are.
var genReserved = []string{
	"normal", "model", "lower", "upper", "vector", "matrix", "array", "cauchy", "student_t", "uniform",
	"x_lpdf", "y_lcdf", "offset", "multiplier", "data", "parameters", "quantities", "real", "int",
	"log_prob", "size_t", "foo_impl", "get_x_sizedtypes", "std_normal", "exponential", "while", "if",
}

type gref struct{ id, name string }

// slot is the whole-slot reference to a node: its name as an expression, or its id when it has none.
func (r gref) slot() map[string]any {
	if r.name == "" {
		return map[string]any{"ref": r.id}
	}
	return map[string]any{"expr": r.name}
}

type gb struct {
	c            chooser
	nodes        []any
	plates       []any
	seq          int
	used         map[string]bool
	reserved     int            // how many reserved names were handed out
	series       map[string]any // the rows plate's series, nil for none
	effectSlopes int            // how many of the effects' components are slopes on a regressor
}

func (b *gb) pick(n int) int        { return b.c.pick(n) }
func (b *gb) yes(num, den int) bool { return b.c.pick(den) < num }

func (b *gb) id(prefix string) string {
	b.seq++
	s := prefix + strconv.FormatInt(int64(b.seq), 36)
	if b.yes(1, 4) {
		s += string(rune('a' + b.pick(26)))
	}
	return s
}

// name is a node's name: usually plain, now and then reserved, and (when the caller can refer to
// the node by its id alone) now and then none.
func (b *gb) name(base string, mayBeNone bool) string {
	switch {
	case mayBeNone && b.yes(1, 10):
		return ""
	case b.yes(1, 10) && b.reserved < len(genReserved):
		b.reserved++
		w := genReserved[(b.reserved*7+b.seq)%len(genReserved)]
		if !b.used[w] {
			b.used[w] = true
			return w
		}
	}
	for k := 0; ; k++ {
		s := fmt.Sprintf("%s%d", base, b.seq+k)
		if !b.used[s] {
			b.used[s] = true
			return s
		}
	}
}

func (b *gb) add(n map[string]any) { b.nodes = append(b.nodes, n) }

func expr(s string) map[string]any { return map[string]any{"expr": s} }

func prior(dist string, args map[string]any) map[string]any {
	return map[string]any{"dist": dist, "args": args}
}

func normalPrior(mu, sigma string) map[string]any {
	return prior("normal", map[string]any{"mu": expr(mu), "sigma": expr(sigma)})
}

// data adds a data node on a plate.
func (b *gb) data(base, typ, plate string, extra map[string]any, mayBeNone bool) gref {
	r := gref{id: b.id("d"), name: b.name(base, mayBeNone)}
	n := map[string]any{"id": r.id, "kind": "data", "label": "Data " + r.id, "plate": plate, "type": typ}
	if r.name != "" {
		n["name"] = r.name
	}
	for k, v := range extra {
		n[k] = v
	}
	b.add(n)
	return r
}

// param adds a parameter; bounds may be nil.
func (b *gb) param(base string, plate any, pr map[string]any, bounds map[string]any, mayBeNone bool) gref {
	r := gref{id: b.id("m"), name: b.name(base, mayBeNone)}
	n := map[string]any{"id": r.id, "kind": "parameter", "label": "Parameter " + r.id, "plate": plate, "prior": pr}
	if r.name != "" {
		n["name"] = r.name
	}
	if bounds != nil {
		n["bounds"] = bounds
	}
	b.add(n)
	return r
}

// scale adds a positive scalar parameter (a spread): an exponential, a half-normal (a bound) or a
// half student_t, the way the templates write them.
func (b *gb) scale(base string, mayBeNone bool) gref {
	switch b.pick(4) {
	case 0:
		return b.param(base, nil, normalPrior("0", fmt.Sprint(1+b.pick(300))), map[string]any{"lower": 0}, mayBeNone)
	case 1:
		return b.param(base, nil, prior("student_t", map[string]any{"nu": expr("4"), "mu": expr("0"), "sigma": expr(fmt.Sprint(1 + b.pick(50)))}),
			map[string]any{"lower": 0}, mayBeNone)
	case 2:
		return b.param(base, nil, prior("gamma", map[string]any{"alpha": expr("2"), "beta": expr(fmt.Sprint(1 + b.pick(5)))}), nil, mayBeNone)
	}
	return b.param(base, nil, prior("exponential", map[string]any{"beta": expr("1 / " + fmt.Sprint(1+b.pick(300)))}), nil, mayBeNone)
}

// location adds a real scalar parameter with a prior on the real line.
func (b *gb) location(base string, mayBeNone bool) gref {
	switch b.pick(5) {
	case 0:
		return b.param(base, nil, prior("cauchy", map[string]any{"mu": expr("0"), "sigma": expr("2.5")}), nil, mayBeNone)
	case 1:
		return b.param(base, nil, prior("student_t", map[string]any{"nu": expr("3"), "mu": expr(fmt.Sprint(b.pick(10))), "sigma": expr("2")}), nil, mayBeNone)
	}
	return b.param(base, nil, normalPrior(fmt.Sprint(b.pick(20)-5), fmt.Sprint(1+b.pick(10))), nil, mayBeNone)
}

// longTerms writes sums and differences of terms over vector atoms (vecs) and single-value atoms
// (scs): each term holds at least one vector, every atom appears. A term has 3 to 9 nodes of the
// tree; `terms` of them pass the 64-node bound of a part from about 10.
func (b *gb) longTerms(terms int, vecs, scs []string) string {
	if terms < len(vecs) {
		terms = len(vecs)
	}
	if terms < len(scs) {
		terms = len(scs)
	}
	var sb strings.Builder
	for i := 0; i < terms; i++ {
		x := vecs[i%len(vecs)]
		if i >= len(vecs) {
			x = vecs[b.pick(len(vecs))]
		}
		sc := func(k int) string {
			if len(scs) == 0 {
				return fmt.Sprint(1 + b.pick(9))
			}
			if k == 0 && i < len(scs) {
				return scs[i]
			}
			return scs[b.pick(len(scs))]
		}
		var t string
		switch b.pick(6) {
		case 0:
			t = sc(0) + " * " + x + " / (" + x + " + " + sc(1) + ")"
		case 1:
			t = sc(0) + " * " + x
		case 2:
			t = "square(" + x + " - " + sc(0) + ")"
		case 3:
			t = "exp(-" + sc(0) + " * " + x + ")"
		case 4:
			t = "inv_logit(" + sc(0) + " + " + sc(1) + " * " + x + ")"
		default:
			t = x + " / " + sc(0)
		}
		if i > 0 {
			if b.yes(1, 3) {
				sb.WriteString(" - ")
			} else {
				sb.WriteString(" + ")
			}
		}
		sb.WriteString(t)
		if sb.Len() > 700 {
			break
		}
	}
	return sb.String()
}

// size picks how many terms an expression has: mostly small, a good share large enough to be split.
func (b *gb) size() int {
	switch b.pick(6) {
	case 0, 1:
		return 1 + b.pick(2)
	case 2:
		return 3 + b.pick(5)
	}
	return 10 + b.pick(11)
}

func names(rs []gref) []string {
	var out []string
	for _, r := range rs {
		if r.name != "" {
			out = append(out, r.name)
		}
	}
	return out
}

// genGraph builds one graph's JSON from the chooser.
func genGraph(c chooser) []byte {
	b := &gb{c: c, used: map[string]bool{}}
	b.build()
	rows := map[string]any{"id": "rows", "kind": "rows", "label": "Rows"}
	if b.series != nil {
		rows["series"] = b.series
	}
	doc := map[string]any{"format_version": 1, "plates": append([]any{rows}, b.plates...), "nodes": b.nodes}
	data, err := json.Marshal(doc)
	if err != nil {
		panic(err)
	}
	return data
}

type glevel struct {
	plate, form string
	key         gref
}

func (b *gb) build() {
	series := b.yes(2, 5)
	groups := b.pick(3)
	if series {
		groups = b.pick(2)
	}
	family := "normal"
	if !series {
		family = []string{"normal", "normal", "normal", "poisson_log", "neg_binomial_2_log", "bernoulli_logit", "binomial_logit"}[b.pick(7)]
	}
	effects := groups > 0 && b.yes(1, 3)
	nested := groups == 2 && !effects && b.yes(1, 2)

	// the plates and their keys
	var levels []glevel
	for l := 0; l < groups; l++ {
		p := b.id("p")
		form := []string{"centred", "non_centred"}[b.pick(2)]
		var parent any
		if nested && l == 1 {
			parent = levels[0].plate
		}
		key := b.data("g", "group_index", "rows", map[string]any{"target": p}, false)
		b.plates = append(b.plates, map[string]any{"id": p, "kind": "group", "label": "Level " + p, "parent": parent, "key": key.id, "form": form})
		levels = append(levels, glevel{p, form, key})
	}

	// the outcome
	yType, yExtra := "real", map[string]any(nil)
	switch family {
	case "poisson_log", "neg_binomial_2_log", "binomial_logit":
		yType, yExtra = "int", map[string]any{"support": "nonnegative"}
	case "bernoulli_logit":
		yType, yExtra = "int", map[string]any{"support": "binary"}
	}
	y := b.data("y", yType, "rows", yExtra, false)
	var trials gref
	if family == "binomial_logit" {
		trials = b.data("trials", "int", "rows", map[string]any{"support": "nonnegative"}, false)
	}
	if series {
		t := b.data("t", "time", "rows", nil, true)
		var by any
		if groups > 0 && b.yes(1, 3) {
			by = levels[0].plate
		}
		b.series = map[string]any{"time": t.id, "by": by}
		if t.name == "" {
			b.series["time"] = t.id
		}
	}

	var pieces []string // the pieces of the mean, summed
	var scalars []string
	var rowVecs []string
	alpha := b.location("alpha", false)
	scalars = append(scalars, alpha.name)
	pieces = append(pieces, alpha.name)

	// group levels: intercepts, or correlated effects
	var lastIntercept string
	groupVecs := make([][]string, len(levels))
	slopes := []string{} // the names of the effects' slope components, with their key
	for l, lv := range levels {
		if effects && l == 0 {
			k := 2 + b.pick(3)
			if b.yes(1, 12) {
				k = 2 + b.pick(9)
			}
			var comps []any
			for ci := 0; ci < k; ci++ {
				cid := fmt.Sprintf("c%d", ci)
				mean := b.location("mu", false)
				sd := b.scale("tau", false)
				comp := map[string]any{"id": cid, "label": "Component " + cid, "mean": expr(mean.name), "scale": expr(sd.name)}
				if !b.yes(1, 8) {
					nm := b.name("eff", false)
					comp["name"] = nm
					if ci == 0 {
						pieces = append(pieces, nm+"["+lv.key.name+"]")
					} else {
						slopes = append(slopes, nm+"["+lv.key.name+"]")
					}
				}
				comps = append(comps, comp)
			}
			corr := map[string]any{"label": "Correlation", "eta": expr(fmt.Sprint(1 + b.pick(4)))}
			if b.yes(2, 3) {
				corr["name"] = b.name("rho", false)
			}
			b.add(map[string]any{"id": b.id("e"), "kind": "correlated_effects", "label": "Effects", "plate": lv.plate, "components": comps, "correlation": corr})
			continue
		}
		sd := b.scale("tau", false)
		mu := alpha.name
		if nested && l == 1 {
			mu = lastIntercept + "[" + levels[0].key.name + "]"
		}
		if b.yes(1, 2) {
			rate := b.param("rate", lv.plate, prior("exponential", map[string]any{"beta": expr("1")}), nil, false)
			groupVecs[l] = append(groupVecs[l], rate.name)
			if b.yes(2, 3) {
				mu = b.longTerms(b.size(), []string{rate.name}, scalars[:1])
			}
		}
		var bounds map[string]any
		if b.yes(1, 3) {
			bounds = map[string]any{"lower": -50 - b.pick(50)}
		}
		a := b.param("a", lv.plate, normalPrior(mu, sd.name), bounds, false)
		lastIntercept = a.name
		groupVecs[l] = append(groupVecs[l], a.name)
		if !(nested && l == 0) {
			pieces = append(pieces, a.name+"["+lv.key.name+"]")
		}
	}

	// a group-level datum and a value computed from it
	if groups > 0 && b.yes(1, 2) {
		u := b.data("u", "real", levels[0].plate, nil, false)
		vecs := []string{u.name}
		scn := []string(nil)
		if len(groupVecs[0]) > 0 && b.yes(1, 2) {
			vecs = append(vecs, groupVecs[0]...) // reads parameters: transformed parameters
			scn = scalars
		}
		ug := gref{id: b.id("t"), name: b.name("ug", false)}
		b.add(map[string]any{"id": ug.id, "kind": "transformed", "name": ug.name, "label": "Group value", "plate": levels[0].plate,
			"value": expr(b.longTerms(b.size(), vecs, scn))})
		pieces = append(pieces, ug.name+"["+levels[0].key.name+"]")
	}

	// regressors: a linear predictor, or the mean written as one expression
	nx := b.pick(4)
	useLP := b.yes(1, 2)
	longLP := useLP && b.yes(1, 4)
	if longLP {
		nx = 17 + b.pick(8)
	}
	if len(slopes) > 0 && nx == 0 {
		nx = 1
	}
	var xs, coefs []gref
	for i := 0; i < nx; i++ {
		x := b.data("x", "real", "rows", map[string]any{"standardize": b.yes(1, 2)}, false)
		xs = append(xs, x)
		rowVecs = append(rowVecs, x.name)
		coefs = append(coefs, b.location("b", useLP))
	}
	for i, s := range slopes {
		if len(xs) > 0 {
			pieces = append(pieces, s+" * "+xs[i%len(xs)].name)
		}
	}
	var lp gref
	if useLP {
		var terms []any
		for i, x := range xs {
			terms = append(terms, map[string]any{"data": x.id, "coef": coefs[i].slot()})
		}
		if !longLP && b.yes(1, 3) {
			cat := b.data("cat", "category", "rows", nil, false)
			bc := b.param("bc", "levels:"+cat.id, normalPrior("0", "1"), nil, false)
			terms = append(terms, map[string]any{"data": cat.id, "coef": bc.slot()})
		}
		if !longLP && b.yes(1, 3) {
			ik := b.data("k", "int", "rows", map[string]any{"support": []string{"any", "nonnegative", "binary"}[b.pick(3)]}, false)
			bk := b.location("bk", true)
			terms = append(terms, map[string]any{"data": ik.id, "coef": bk.slot()})
		}
		lp = gref{id: b.id("q"), name: b.name("eta", true)}
		n := map[string]any{"id": lp.id, "kind": "linear_predictor", "label": "Predictor", "plate": "rows", "terms": terms}
		n["intercept"] = expr(strings.Join(pieces, " + "))
		if lp.name != "" {
			n["name"] = lp.name
		}
		b.add(n)
		pieces = nil
		if lp.name != "" {
			pieces = []string{lp.name}
		}
	} else {
		for i, x := range xs {
			pieces = append(pieces, coefs[i].name+" * "+x.name)
			scalars = append(scalars, coefs[i].name)
		}
	}

	// time series: lags, latent states
	var states []gref
	if series {
		if b.yes(3, 5) {
			p := 1 + b.pick(4)
			if b.yes(1, 3) {
				p = 8 + b.pick(8)
			}
			var scn, vecs []string
			for k := 1; k <= p; k++ {
				if !b.yes(1, 6) {
					phi := b.param("phi", nil, normalPrior("0", "0.5"), nil, false)
					scn = append(scn, phi.name)
				}
				vecs = append(vecs, fmt.Sprintf("lag(%s, %d)", y.name, k))
			}
			text := b.longTerms(b.size(), vecs, scn)
			if b.yes(1, 2) {
				ar := gref{id: b.id("t"), name: b.name("ar", false)}
				b.add(map[string]any{"id": ar.id, "kind": "transformed", "name": ar.name, "label": "Autoregression", "plate": "rows", "value": expr(text)})
				pieces = append(pieces, ar.name)
			} else {
				pieces = append(pieces, text)
			}
		}
		for s := b.pick(3); s > 0; s-- {
			sd := b.scale("sl", false)
			form := []string{"centred", "non_centred"}[b.pick(2)]
			init := normalPrior(fmt.Sprint(b.pick(5)), fmt.Sprint(1+b.pick(20)))
			if form == "non_centred" && b.yes(1, 3) {
				init = prior("student_t", map[string]any{"nu": expr("5"), "mu": expr("0"), "sigma": expr("3")})
			}
			if len(states) > 0 && b.yes(1, 2) {
				// an init that reads another state: a vector, long now and then
				mu := states[len(states)-1].name
				if b.yes(1, 2) {
					mu = b.longTerms(b.size(), []string{mu}, nil)
				}
				init = normalPrior(mu, "1")
			}
			st := gref{id: b.id("s"), name: b.name("level", false)}
			b.add(map[string]any{"id": st.id, "kind": "state", "name": st.name, "label": "Level", "plate": "rows", "process": "random_walk",
				"form": form, "init": init, "scale": expr(sd.name)})
			states = append(states, st)
			pieces = append(pieces, st.name)
		}
	}

	// a long expression of the rows: a transformed node reading data alone (transformed data) or
	// the single values too (a value of the model)
	if len(rowVecs) > 0 && b.yes(1, 2) {
		scn := []string(nil)
		if b.yes(1, 2) {
			scn = scalars
		}
		m := gref{id: b.id("t"), name: b.name("curve", false)}
		b.add(map[string]any{"id": m.id, "kind": "transformed", "name": m.name, "label": "Curve", "plate": "rows",
			"value": expr(b.longTerms(b.size(), rowVecs, scn))})
		pieces = append(pieces, m.name)
	}

	// the likelihood
	var sigma gref
	if family == "normal" || family == "neg_binomial_2_log" {
		sigma = b.scale("sigma", true)
	}
	slotMu := expr(strings.Join(pieces, " + "))
	if len(pieces) == 0 {
		slotMu = lp.slot()
	}
	lik := map[string]any{"id": b.id("l"), "kind": "likelihood", "label": "Outcome", "observed": y.id, "dist": family}
	switch family {
	case "normal":
		lik["args"] = map[string]any{"mu": slotMu, "sigma": sigma.slot()}
	case "poisson_log", "bernoulli_logit":
		lik["args"] = map[string]any{"alpha": slotMu}
	case "binomial_logit":
		lik["args"] = map[string]any{"N": expr(trials.name), "alpha": slotMu}
	case "neg_binomial_2_log":
		lik["args"] = map[string]any{"eta": slotMu, "phi": sigma.slot()}
	}
	b.add(lik)
	r := rand.New(rand.NewPCG(uint64(b.seq), 7))
	r.Shuffle(len(b.nodes), func(i, j int) { b.nodes[i], b.nodes[j] = b.nodes[j], b.nodes[i] })
}
