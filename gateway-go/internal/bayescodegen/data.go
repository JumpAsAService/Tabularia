package bayescodegen

import (
	"fmt"
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesexpr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// This file is the data block and the data manifest (docs/specs/bayes-stan-codegen.md, «Data»;
// docs/specs/bayes-contract.md, «The data manifest»): the sizes first, then the data nodes in Order,
// the lifted indices, the arrays of a series (its lags, its states' first and next rows), then
// Mode, then the extension's data. The manifest lists the same variables in the same order.

// Identifiers the program declares for itself.
const (
	identRows = "N"
	identMode = "Mode"
)

// intType is an integer data array's element type by its support.
var intType = map[string]string{
	"any":         "int",
	"nonnegative": "int<lower=0>",
	"binary":      "int<lower=0, upper=1>",
}

// liftKey is the key of a lifted index: the index's id and the plate it is lifted to.
func liftKey(index string, context bayesexpr.Plate) string { return index + "\x00" + string(context) }

// indexPlates names what the plates need: each plate's size (N, J_<plate>, K_<category>), each
// lifted index's array (Up_<index>_<plate>), and Full_<id> for each parameter on a category's
// levels that a value per row reads (through an index, or as a linear predictor's term).
func (g *gen) indexPlates() error {
	for _, p := range g.m.Plates {
		if _, dup := g.plates[p.ID]; dup {
			return bug("the plate %q twice in a valid model", p.ID)
		}
		g.plates[p.ID] = p
		var s string
		var err error
		switch p.Kind {
		case bayesgraph.PlateRows:
			s = identRows
		case bayesgraph.PlateGroup:
			s, err = generated("J", p.ID)
		case bayesgraph.PlateLevels:
			if c := g.node(p.Category); c == nil || c.Kind != "data" || c.Type != "category" {
				return bug("the levels plate %q of %q, not a category", p.ID, p.Category)
			}
			s, err = generated("K", p.Category)
		default:
			return bug("the plate %q of kind %q", p.ID, p.Kind)
		}
		if err != nil {
			return err
		}
		g.size[p.ID] = s
	}
	for _, l := range g.m.Lifts {
		ctx, ok := g.plates[string(l.Context)]
		idx := g.node(l.ID)
		if !ok || ctx.Kind != bayesgraph.PlateGroup || idx == nil || idx.Kind != "data" || idx.Type != "group_index" {
			return bug("the lift of %q to %q", l.ID, l.Context)
		}
		if _, ok := g.plates[idx.Target]; !ok {
			return bug("the index %q targets %q, not a plate", l.ID, idx.Target)
		}
		s, err := generated("Up", l.ID, string(l.Context))
		if err != nil {
			return err
		}
		g.lift[liftKey(l.ID, l.Context)] = s
	}
	perRow := map[string]bool{}
	for _, t := range g.m.Slots {
		for _, r := range t.Refs {
			if p, ok := g.levelsOf(r); ok && string(t.Context) != p.ID {
				perRow[r] = true
			}
		}
	}
	for _, id := range g.m.Order {
		n := g.node(id)
		if n.Kind != "linear_predictor" {
			continue
		}
		for i, term := range n.Terms {
			if d := g.node(term.Data); d != nil && d.Type == "category" {
				b, err := g.coefficient(bayesgraph.SlotKey{Node: n.ID, Slot: fmt.Sprintf("terms[%d].coef", i)})
				if err != nil {
					return err
				}
				if _, ok := g.levelsOf(b); !ok {
					return bug("the coefficient %q of the category %q is not on its levels", b, d.ID)
				}
				perRow[b] = true
			}
		}
	}
	for _, id := range g.m.Order {
		if perRow[id] {
			s, err := generated("Full", id)
			if err != nil {
				return err
			}
			g.full[id] = s
		}
	}
	return nil
}

// levelsOf is the levels plate of a parameter on a category's levels.
func (g *gen) levelsOf(id string) (bayesgraph.ModelPlate, bool) {
	n := g.node(id)
	if n == nil || n.Kind != "parameter" || n.Plate == nil {
		return bayesgraph.ModelPlate{}, false
	}
	p, ok := g.plates[*n.Plate]
	return p, ok && p.Kind == bayesgraph.PlateLevels
}

// coefficient is the parameter a linear predictor's coefficient slot names (a bare name or a ref).
func (g *gen) coefficient(key bayesgraph.SlotKey) (string, error) {
	t, ok := g.m.Slots[key]
	switch {
	case !ok:
		return "", bug("the slot %s.%s did not type in a valid model", key.Node, key.Slot)
	case t.Ref != "":
		return t.Ref, nil
	case t.Expr != nil && t.Expr.Tree != nil && t.Expr.Tree.Node != nil && t.Expr.Tree.Node.Kind == bayesexpr.KindName && t.Expr.Tree.Sym != nil:
		return t.Expr.Tree.Sym.ID, nil
	}
	return "", bug("the coefficient %s.%s is not a parameter's name", key.Node, key.Slot)
}

// sizeEntry is the declaration, the mapping entry and the manifest entry of a plate's size.
func (g *gen) sizeEntry(p bayesgraph.ModelPlate) {
	s := g.size[p.ID]
	if p.Kind == bayesgraph.PlateRows {
		g.w.line("int<lower=0> " + s + ";")
	} else {
		g.w.line("int<lower=1> " + s + ";")
	}
	g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: s, Block: blockData, Kind: bayescontract.KindSize, Plate: strp(p.ID)})
	g.data = append(g.data, bayescontract.DataEntry{Stan: s, Kind: bayescontract.DataSize, Of: p.ID})
}

// The data block: the sizes (N, then the group plates' J and the categories' K in Model.Plates'
// order), the data nodes in Order, the lifted indices in Model.Lifts' order, then Mode.
func (g *gen) writeData() error {
	w := g.w
	w.open(blockData)
	for _, p := range g.m.Plates {
		g.sizeEntry(p)
	}
	for _, id := range g.m.Order {
		n := g.node(id)
		if n.Kind != "data" {
			continue
		}
		b, ok := g.bindings[id]
		if !ok {
			return bug("the data node %q has no binding", id)
		}
		plate, ok := g.plates[b.Plate]
		if !ok || plate.Kind == bayesgraph.PlateLevels {
			return bug("the data node %q on the plate %q", id, b.Plate)
		}
		size := g.size[plate.ID]
		x := g.ident[id]
		entry := bayescontract.DataEntry{Stan: x, Plate: plate.ID, Node: id, Standardize: b.Standardize}
		switch b.Type {
		case "real":
			w.line("vector[" + size + "] " + x + ";")
			entry.Kind = bayescontract.DataReal
		case "int":
			typ, ok := intType[b.Support]
			if !ok {
				return bug("the integer data node %q has the support %q", id, b.Support)
			}
			w.line("array[" + size + "] " + typ + " " + x + ";")
			entry.Kind, entry.Support = bayescontract.DataInt, b.Support
		case "group_index":
			target, ok := g.plates[b.Target]
			if !ok || target.Kind != bayesgraph.PlateGroup {
				return bug("the group index %q targets %q", id, b.Target)
			}
			w.line("array[" + size + "] int<lower=1, upper=" + g.size[target.ID] + "> " + x + ";")
			entry.Kind, entry.Target = bayescontract.DataGroupIndex, target.ID
		case "category":
			if plate.Kind != bayesgraph.PlateRows {
				// graph.data_type: a category is on the rows only.
				return bug("the category %q on the group plate %q", id, plate.ID)
			}
			levels, ok := g.plates[bayesgraph.LevelsPlate(id)]
			if !ok {
				return bug("the category %q has no levels plate", id)
			}
			w.line("array[" + size + "] int<lower=1, upper=" + g.size[levels.ID] + "> " + x + ";")
			entry.Kind = bayescontract.DataCategory
		case "time":
			continue // the runner sorts the series by it; the program does not read it
		default:
			return bug("the data node %q has the type %q", id, b.Type)
		}
		g.mapping = append(g.mapping, g.entry(n, blockData, ""))
		g.data = append(g.data, entry)
	}
	for _, l := range g.m.Lifts {
		x := g.lift[liftKey(l.ID, l.Context)]
		target := g.node(l.ID).Target
		w.line("array[" + g.size[string(l.Context)] + "] int<lower=1, upper=" + g.size[target] + "> " + x + ";")
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: x, Block: blockData, NodeID: strp(l.ID), Kind: bayescontract.KindLift,
			Plate: strp(string(l.Context))})
		g.data = append(g.data, bayescontract.DataEntry{Stan: x, Kind: bayescontract.DataLift, Plate: string(l.Context), Node: l.ID, Target: target})
	}
	g.writeSeriesData()
	w.line("int<lower=0, upper=1> " + identMode + ";")
	g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: identMode, Block: blockData, Kind: bayescontract.KindMode})
	g.data = append(g.data, bayescontract.DataEntry{Stan: identMode, Kind: bayescontract.DataMode})
	if err := g.writeExtensionData(); err != nil {
		return err
	}
	w.close()
	return nil
}

// writeSeriesData declares the arrays of a series the runner builds from the sorted rows, within
// each unit of the series: N_lag, Rows_lag and Lag<k> for each lag the model uses; N_first,
// Rows_first, N_next, Rows_next and Prev when it has a latent state.
func (g *gen) writeSeriesData() {
	rows := g.rows.ID
	size := func(s, of string) {
		g.w.line("int<lower=0> " + s + ";")
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: s, Block: blockData, Kind: bayescontract.KindSize, Plate: strp(rows)})
		g.data = append(g.data, bayescontract.DataEntry{Stan: s, Kind: bayescontract.DataSize, Of: of})
	}
	array := func(s, size, kind string, lag int) {
		g.w.line("array[" + size + "] int<lower=1, upper=" + identRows + "> " + s + ";")
		g.mapping = append(g.mapping, bayescontract.MappingEntry{Stan: s, Block: blockData, Kind: bayescontract.KindSeries, Plate: strp(rows)})
		g.data = append(g.data, bayescontract.DataEntry{Stan: s, Kind: kind, Plate: rows, Lag: lag})
	}
	if len(g.m.Lags) > 0 {
		size(identNLag, identRowsLag)
		array(identRowsLag, identNLag, bayescontract.DataRowsSubset, 0)
		for _, k := range g.m.Lags {
			array(identLagPrefix+strconv.Itoa(k), identNLag, bayescontract.DataLag, k)
		}
	}
	if len(g.states) > 0 {
		size(identNFirst, identRowsFirst)
		array(identRowsFirst, identNFirst, bayescontract.DataFirst, 0)
		size(identNNext, identRowsNext)
		array(identRowsNext, identNNext, bayescontract.DataNext, 0)
		array(identPrev, identNNext, bayescontract.DataPrevious, 0)
	}
}
