package bayescodegen

// This file is the data block and the data manifest (docs/specs/bayes-stan-codegen.md, «Data»;
// docs/specs/bayes-contract.md, «The data manifest»): sizes first, then the data nodes in Order,
// then Mode. The manifest lists the same variables in the same order.

// DataEntry is one variable of the program's data block, as the runner writes it. Column is the
// input column the flow node binds (answer 6): the codegen does not know it and leaves it empty;
// flow-node fills it from the binding of Node when it writes the task.
type DataEntry struct {
	Stan        string `json:"stan"`
	Kind        string `json:"kind"`
	Of          string `json:"of,omitempty"`
	Plate       string `json:"plate,omitempty"`
	Column      string `json:"column,omitempty"`
	Node        string `json:"node,omitempty"`
	Standardize bool   `json:"standardize,omitempty"`
	Target      string `json:"target,omitempty"`
	Support     string `json:"support,omitempty"`
}

// Kinds of a data entry (bayes-contract).
const (
	DataSize = "size"
	DataReal = "real"
	DataInt  = "int"
	DataMode = "mode"
)

// Identifiers the program declares for itself.
const (
	identRows = "N"
	identMode = "Mode"
)

// intDecl is an integer data array's type by its support.
var intDecl = map[string]string{
	"any":         "array[N] int",
	"nonnegative": "array[N] int<lower=0>",
	"binary":      "array[N] int<lower=0, upper=1>",
}

func (g *gen) writeData() error {
	w := &g.w
	w.open(blockData)
	w.line("int<lower=0> " + identRows + ";")
	g.mapping = append(g.mapping, MappingEntry{Stan: identRows, Block: blockData, Kind: KindSize, Plate: strp(g.rows.ID)})
	g.data = append(g.data, DataEntry{Stan: identRows, Kind: DataSize, Of: g.rows.ID})
	for _, id := range g.m.Order {
		n := g.node(id)
		if n.Kind != "data" {
			continue
		}
		b, ok := g.bindings[id]
		if !ok {
			return bug("the data node %q has no binding", id)
		}
		if b.Plate != g.rows.ID {
			return unsupported("the data node %q on the plate %q (lot S1)", id, b.Plate)
		}
		x := g.ident[id]
		entry := DataEntry{Stan: x, Plate: g.rows.ID, Node: id, Standardize: b.Standardize}
		switch b.Type {
		case "real":
			w.line("vector[N] " + x + ";")
			entry.Kind = DataReal
		case "int":
			decl, ok := intDecl[b.Support]
			if !ok {
				return bug("the integer data node %q has the support %q", id, b.Support)
			}
			w.line(decl + " " + x + ";")
			entry.Kind, entry.Support = DataInt, b.Support
		case "time":
			continue // the runner sorts by it; the program does not read it (a series: lot S2)
		default:
			return bug("the data node %q has the type %q", id, b.Type)
		}
		g.mapping = append(g.mapping, g.entry(n, blockData, ""))
		g.data = append(g.data, entry)
	}
	w.line("int<lower=0, upper=1> " + identMode + ";")
	g.mapping = append(g.mapping, MappingEntry{Stan: identMode, Block: blockData, Kind: KindMode})
	g.data = append(g.data, DataEntry{Stan: identMode, Kind: DataMode})
	w.close()
	return nil
}
