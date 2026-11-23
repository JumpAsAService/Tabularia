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
// likelihoods. Plates (S1), correlated effects, states, lags, transformed data and the extension
// point (S2) are refused with ErrUnsupported until their lots are built.
package bayescodegen

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesgraph"
)

// CodegenVersion is the version of the generator, stored with each program (model-store). It is
// raised with every change of the output: a golden program that changes while it does not fails
// the suite. A saved version always runs with its stored program; a new CodegenVersion never
// rewrites one.
const CodegenVersion = 1

var (
	// ErrInvalidModel: the model has an error finding (or is nil); no program is written from it.
	ErrInvalidModel = errors.New("bayescodegen: the model is not valid")
	// ErrUnsupported: the model uses what this generator does not write yet (a later lot).
	ErrUnsupported = errors.New("bayescodegen: not written by this generator yet")
	// ErrBug: the generator produced, or was about to produce, what the output's rules refuse. It is
	// a fault of the codegen, never of the person's model.
	ErrBug = errors.New("bayescodegen: bug")
)

func bug(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrBug, fmt.Sprintf(format, a...))
}

func unsupported(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrUnsupported, fmt.Sprintf(format, a...))
}

// Extension is what predictive-checks adds to a program (generated.go, lot S2). Until that lot,
// Generate accepts only a nil Extension.
type Extension interface {
	extension()
}

// Program is a generated program with what is stored and shown beside it.
type Program struct {
	Text           []byte
	SHA256         string // hex of Text's sha256
	CodegenVersion int
	Mapping        []MappingEntry
	Data           []DataEntry
	Quantities     []Quantity
}

// Generate writes the program of a validated model. A model with an error finding gives
// ErrInvalidModel, a model this lot does not write ErrUnsupported; any other error is a bug of the
// codegen (ErrBug or Emit's). A returned program has passed CheckProgram and CheckIdentifiers.
func Generate(m *bayesgraph.Model, ext Extension) (*Program, error) {
	if m == nil || m.Graph == nil || !m.Valid() {
		return nil, ErrInvalidModel
	}
	if ext != nil {
		return nil, unsupported("the extension point (lot S2)")
	}
	g, err := newGen(m)
	if err != nil {
		return nil, err
	}
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
	blockData        = "data"
	blockParameters  = "parameters"
	blockTransformed = "transformed parameters"
	blockModel       = "model"
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

	w          writer
	mapping    []MappingEntry
	data       []DataEntry
	quantities []Quantity
}

func newGen(m *bayesgraph.Model) (*gen, error) {
	g := &gen{m: m, nodes: map[string]*bayesgraph.Node{}, ident: map[string]string{},
		renamed: map[string][2]string{}, bindings: map[string]bayesgraph.Binding{}}
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
	return g, nil
}

// supported refuses what lot S0 does not write, naming the lot that will.
func (g *gen) supported() error {
	for _, p := range g.m.Plates[1:] {
		switch p.Kind {
		case bayesgraph.PlateGroup:
			return unsupported("the group plate %q (lot S1)", p.ID)
		case bayesgraph.PlateLevels:
			return unsupported("the category %q (lot S1)", p.Category)
		default:
			return bug("the plate %q of kind %q", p.ID, p.Kind)
		}
	}
	if g.rows.Series != nil {
		return unsupported("a series on the rows (lot S2)")
	}
	if len(g.m.Lifts) > 0 {
		return unsupported("a lifted index (lot S1)")
	}
	if len(g.m.Lags) > 0 {
		return unsupported("lag (lot S2)")
	}
	for _, id := range g.m.Order {
		n := g.nodes[id]
		if n == nil {
			continue
		}
		switch n.Kind {
		case "state":
			return unsupported("the latent state %q (lot S2)", id)
		case "correlated_effects":
			return unsupported("the correlated effects %q (lot S2)", id)
		case "data":
			switch n.Type {
			case "group_index", "category":
				return unsupported("the %s data node %q (lot S1)", n.Type, id)
			}
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
	if err := g.writeParameters(); err != nil {
		return err
	}
	if err := g.writeTransformedParameters(); err != nil {
		return err
	}
	return g.writeModel()
}

// node is the node with the id, which the model holds.
func (g *gen) node(id string) *bayesgraph.Node { return g.nodes[id] }

// entry is the mapping table's entry of a node's identifier, declared in block.
func (g *gen) entry(n *bayesgraph.Node, block, role string) MappingEntry {
	e := MappingEntry{Stan: g.ident[n.ID], Block: block, NodeID: strp(n.ID), Label: strp(n.Label),
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
