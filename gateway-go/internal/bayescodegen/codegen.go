// Package bayescodegen writes the Stan program of a validated model of the Bayesian editor
// (docs/specs/bayes-stan-codegen.md): the only producer of a program. The program is written from
// the validated model alone (bayesgraph.Model), deterministically, with no text of the person in
// it: identifiers are the person's names when they pass the rule or generated from ids, every
// expression comes through bayesexpr.Emit, every function and distribution from the catalogue,
// every number re-printed from its double. Beside the program it writes the mapping table
// (identifier ↔ node ↔ label) and the two manifests of bayes-contract (the data the runner writes,
// the quantities the program writes).
//
// Lot S0 writes the generalized linear models: the rows plate, scalar parameters with their bounds,
// priors and truncation, transformed nodes and linear predictors on the rows or single, the five
// likelihoods. Lot S1 adds the plates: group plates and their sizes, the indices and their lifts,
// the non-centred forms (affine and explicit) and the centred one, the categories' levels with the
// reference at 0, and the truncation loop over a plate. Lot S2 adds the correlated effects (LKJ),
// the latent states, lag on a series (the statements on the rows that have every lag), transformed
// data, and the extension point of predictive-checks. Lot S5 splits a large vector expression into
// a chain of locals (split.go). Lot P1 of predictive-checks gives the extension values on the rows
// it names (SlotOn, ValueOn) and its arithmetic (Add, Mul).
package bayescodegen

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// CodegenVersion is the version of the generator, stored with each program (model-store). It is
// raised with every change of the output: a golden program that changes while it does not fails
// the suite. A saved version always runs with its stored program; a new CodegenVersion never
// rewrites one.
//
// 2 (lot P1 of predictive-checks): the extension point gains SlotOn, ValueOn, Add and Mul, the
// catalogue's internal functions grow (so a person's name such as mean, sd, min or max is now
// renamed), and the programs a save writes hold predictive-checks' extension (lot P2). The programs
// of the golden corpus without an extension are unchanged.
//
// 3 (bayes-contract's correction of 2026-11-24 from P3a): an extension's datum may say what its
// positions are among (Decl.RowsOf): predictive-checks' post_rows and prior_rows name in the data
// manifest's `of` the rows plate, or Rows_lag for a lagged likelihood. The programs' bytes are
// unchanged; the manifests a save stores change, so a save of a version stored before writes them.
const CodegenVersion = 3

var (
	// ErrInvalidModel: the model has an error finding (or is nil); no program is written from it.
	ErrInvalidModel = errors.New("bayescodegen: the model is not valid")
	// ErrUnsupported: the model uses what this generator does not write yet (a later lot).
	ErrUnsupported = errors.New("bayescodegen: not written by this generator yet")
	// ErrBug: the generator produced, or was about to produce, what the output's rules refuse. It is
	// a fault of the codegen, never of the person's model.
	ErrBug = errors.New("bayescodegen: bug")
	// ErrExtension: an extension declared or wrote what the extension point refuses (a name outside
	// its prefixes, a call outside the catalogue, an assignment to the program's own variables…).
	ErrExtension = errors.New("bayescodegen: extension")
)

func bug(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrBug, fmt.Sprintf(format, a...))
}

func unsupported(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrUnsupported, fmt.Sprintf(format, a...))
}

// Program is a generated program with what is stored and shown beside it.
type Program struct {
	Text           []byte
	SHA256         string // hex of Text's sha256
	CodegenVersion int
	Mapping        []bayescontract.MappingEntry
	Data           []bayescontract.DataEntry
	Quantities     []bayescontract.Quantity
}

// Generate writes the program of a validated model, with what ext adds (nil: nothing). A model
// with an error finding gives ErrInvalidModel, a model this generator does not write
// ErrUnsupported; an extension's refused declaration or statement ErrExtension; any other error is
// a bug of the codegen (ErrBug or Emit's). A returned program has passed CheckProgram and
// CheckIdentifiers.
func Generate(m *bayesgraph.Model, ext Extension) (*Program, error) {
	if m == nil || m.Graph == nil || !m.Valid() {
		return nil, ErrInvalidModel
	}
	g, err := newGen(m)
	if err != nil {
		return nil, err
	}
	g.ext = ext
	if err := g.write(); err != nil {
		return nil, err
	}
	text, err := g.w.text()
	if err != nil {
		return nil, err
	}
	if err := CheckProgram(text); err != nil {
		return nil, err
	}
	if err := CheckIdentifiers(g.mapping); err != nil {
		return nil, err
	}
	sum := sha256.Sum256([]byte(text))
	return &Program{
		Text:           []byte(text),
		SHA256:         hex.EncodeToString(sum[:]),
		CodegenVersion: CodegenVersion,
		Mapping:        g.mapping,
		Data:           g.data,
		Quantities:     g.quantities,
	}, nil
}

// Block names, as the program and the mapping table write them.
const (
	blockData            = "data"
	blockTransformedData = "transformed data"
	blockParameters      = "parameters"
	blockTransformed     = "transformed parameters"
	blockModel           = "model"
	blockGenerated       = "generated quantities"
)

// gen is one generation: the model, its nodes by id, the identifiers, and what has been written.
type gen struct {
	m     *bayesgraph.Model
	nodes map[string]*bayesgraph.Node
	rows  bayesgraph.ModelPlate
	// ident is every node's identifier (those that have none are missing: a likelihood).
	ident    map[string]string
	renamed  map[string][2]string // id → (renamed, word)
	bindings map[string]bayesgraph.Binding
	// reads is what each node's slots read (ids), for whether a value depends on a parameter.
	reads map[string][]string
	// plates are the model's plates by id; size is the identifier of each plate's number of units
	// (N, J_<plate>, K_<category>).
	plates map[string]bayesgraph.ModelPlate
	size   map[string]string
	// lift is the identifier of each lifted index (Up_<index>_<plate>), by liftKey.
	lift map[string]string
	// full is the identifier Full_<id> of each parameter on a category's levels that is read per
	// row: its coefficients with the reference level's 0 in front.
	full map[string]string
	// explicit is the parameters written in the explicit non-centred form: their identifier is a
	// transformed parameter, their standard normals Z_<id> the parameter.
	explicit map[string]string
	// slotKeys are each node's slots (its components' and correlation's included), sorted.
	slotKeys map[string][]bayesgraph.SlotKey
	// data is the transformed nodes that depend on no parameter: transformed data.
	tdata map[string]bool
	// lagged is the values per row and the likelihoods computed on the rows that have every lag
	// (Rows_lag): those with lag in a slot, and those that read one of them.
	lagged map[string]bool
	// effects and states by node id; comps the components of correlated effects by
	// "<node>.<component>" (their identifiers are in ident under the same key).
	effects map[string]*effect
	states  map[string]*state
	comps   map[string]*bayesgraph.Component

	// splitNames are the locals of each split expression (lot S5), by the slot or the linear
	// predictor's sum they hold, in the order they are written; splitCount is the number of locals
	// each node has, the last k of its E_<id>_<k>.
	splitNames map[string][]string
	splitCount map[string]int

	ext Extension
	// extNames are the identifiers the extension declared, by name: what Scope.Declared answers.
	extNames map[string]Decl
	// extPre are the locals of the extension's split values already written.
	extPre map[string]bool
	// extLoops counts the extension's loops; extLoop is the loop each local declared in one is
	// declared in.
	extLoops int
	extLoop  map[string]int

	w          *writer
	mapping    []bayescontract.MappingEntry
	data       []bayescontract.DataEntry
	quantities []bayescontract.Quantity
}

func newGen(m *bayesgraph.Model) (*gen, error) {
	g := &gen{m: m, nodes: map[string]*bayesgraph.Node{}, ident: map[string]string{},
		renamed: map[string][2]string{}, bindings: map[string]bayesgraph.Binding{},
		plates: map[string]bayesgraph.ModelPlate{}, size: map[string]string{}, lift: map[string]string{},
		full: map[string]string{}, explicit: map[string]string{}, tdata: map[string]bool{}, lagged: map[string]bool{},
		effects: map[string]*effect{}, states: map[string]*state{}, comps: map[string]*bayesgraph.Component{},
		splitNames: map[string][]string{}, splitCount: map[string]int{},
		extNames: map[string]Decl{}, extPre: map[string]bool{}, extLoop: map[string]int{}, w: &writer{}}
	for i := range m.Graph.Nodes {
		n := &m.Graph.Nodes[i]
		if _, dup := g.nodes[n.ID]; dup {
			return nil, bug("the node id %q twice in a valid model", n.ID)
		}
		g.nodes[n.ID] = n
	}
	if len(m.Plates) == 0 || m.Plates[0].Kind != bayesgraph.PlateRows {
		return nil, bug("a valid model without its rows plate first")
	}
	g.rows = m.Plates[0]
	if err := g.supported(); err != nil {
		return nil, err
	}
	for _, b := range m.Bindings {
		g.bindings[b.Node] = b
	}
	g.indexReads()
	for _, id := range m.Order {
		n, ok := g.nodes[id]
		if !ok {
			return nil, bug("the order names %q, not a node", id)
		}
		if n.Kind == "likelihood" {
			continue // a likelihood declares nothing
		}
		s, renamed, word, err := nodeIdentifier(n.ID, n.Name)
		if err != nil {
			return nil, err
		}
		g.ident[n.ID] = s
		if renamed != "" {
			g.renamed[n.ID] = [2]string{renamed, word}
		}
	}
	if err := g.indexPlates(); err != nil {
		return nil, err
	}
	if err := g.indexEffectsAndStates(); err != nil {
		return nil, err
	}
	g.indexTransformedData()
	if err := g.indexForms(); err != nil {
		return nil, err
	}
	if err := g.indexLags(); err != nil {
		return nil, err
	}
	return g, nil
}

// supported checks what a valid model holds and the generator relies on: the plates' kinds, and a
// series for lag and states.
func (g *gen) supported() error {
	for _, p := range g.m.Plates[1:] {
		switch p.Kind {
		case bayesgraph.PlateGroup, bayesgraph.PlateLevels:
		default:
			return bug("the plate %q of kind %q", p.ID, p.Kind)
		}
	}
	if len(g.m.Lags) > 0 && g.rows.Series == nil {
		return bug("lag on rows that are not a series")
	}
	for _, k := range g.m.Lags {
		if k < 1 || k > bayescatalog.GetLimits().MaxLag {
			return bug("the lag %d outside 1…max_lag", k)
		}
	}
	return nil
}

// write writes the blocks in their order (a block with nothing to hold left out, except data,
// parameters and model).
func (g *gen) write() error {
	if err := g.writeData(); err != nil {
		return err
	}
	if err := g.optionalBlock(blockTransformedData, g.writeTransformedData); err != nil {
		return err
	}
	if err := g.writeParameters(); err != nil {
		return err
	}
	if err := g.optionalBlock(blockTransformed, g.writeTransformedParameters); err != nil {
		return err
	}
	if err := g.writeModel(); err != nil {
		return err
	}
	return g.optionalBlock(blockGenerated, g.writeGenerated)
}

// optionalBlock writes "head { … }" with what body writes, and nothing when body writes nothing.
func (g *gen) optionalBlock(head string, body func() error) error {
	outer, inner := g.w, &writer{depth: 1, base: 1}
	g.w = inner
	err := body()
	g.w = outer
	if err != nil {
		return err
	}
	text, err := inner.text()
	if err != nil {
		return err
	}
	if text == "" {
		return nil
	}
	outer.open(head)
	outer.raw(text)
	outer.close()
	return nil
}

// node is the node with the id, which the model holds.
func (g *gen) node(id string) *bayesgraph.Node { return g.nodes[id] }

// entry is the mapping table's entry of a node's identifier, declared in block.
func (g *gen) entry(n *bayesgraph.Node, block, role string) bayescontract.MappingEntry {
	e := bayescontract.MappingEntry{Stan: g.ident[n.ID], Block: block, NodeID: strp(n.ID), Label: strp(n.Label),
		Kind: n.Kind, Role: optp(role)}
	if n.Plate != nil {
		e.Plate = strp(*n.Plate)
	}
	if r, ok := g.renamed[n.ID]; ok {
		e.Renamed = strp(r[0])
		e.Word = r[1]
	}
	if n.Name != nil {
		e.Name = strp(*n.Name)
	}
	return e
}
