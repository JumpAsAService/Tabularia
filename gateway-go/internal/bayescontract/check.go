package bayescontract

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// This file checks the manifests' invariants and expands the quantity manifest into the CSV header CmdStan writes
// («The quantity manifest»: the runner checks the header against this list, name for name and in order).

var (
	stanName      = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]*$`)
	extensionName = regexp.MustCompile(`^(Ppc|Prior)_[a-z0-9]+(_[a-z0-9]+)*$`)
	extensionKind = regexp.MustCompile(`^[a-z][a-z_]*[a-z]$`)
)

// coreKinds are the data kinds of the program itself; an extension's data (predictive-checks) has a kind of its own.
var coreKinds = map[string]bool{DataSize: true, DataReal: true, DataInt: true, DataGroupIndex: true, DataCategory: true,
	DataLift: true, DataMode: true, DataRowsSubset: true, DataLag: true, DataFirst: true, DataNext: true, DataPrevious: true}

// DataReadsColumn says whether a data entry of this kind reads an input column (flow-node fills its Column): the values on a
// plate, the group indices and the categories. Every other kind is derived by the runner from these and from the rows' order.
func DataReadsColumn(kind string) bool {
	return kind == DataReal || kind == DataInt || kind == DataGroupIndex || kind == DataCategory
}

// RowsLag is the series' rows that have every lag (DataRowsSubset): a lagged likelihood's statement runs on them.
const RowsLag = "Rows_lag"

// seriesArrays are the arrays of a series a size may be `of`.
var seriesArrays = map[string]string{RowsLag: DataRowsSubset, "Rows_first": DataFirst, "Rows_next": DataNext}

// CheckManifests checks that the plates, the mapping and the two manifests hold together: names well formed and unique, kinds
// and roles known with the fields each needs, every plate a dimension or an entry names sized by a data entry and listed in
// plates, every size dimension a data integer, every component dimension the components of an effect, every written
// quantity in the mapping with its node. A failure is a fault of Tabularia (stan-codegen or flow-node), never a person's.
func CheckManifests(plates []Plate, mapping []MappingEntry, data []DataEntry, quantities []Quantity) error {
	plateKind := map[string]string{}
	for _, p := range plates {
		ok := (p.Kind == PlateRows && p.ID == PlateRows) || (p.Kind == PlateGroup && stanName.MatchString(p.ID) && p.ID != PlateRows) ||
			(p.Kind == PlateLevels && strings.HasPrefix(p.ID, LevelsPrefix) && stanName.MatchString(strings.TrimPrefix(p.ID, LevelsPrefix)))
		if !ok || plateKind[p.ID] != "" {
			return fmt.Errorf("bayes.plates: %q (%s) is not a plate, or is listed twice", p.ID, p.Kind)
		}
		plateKind[p.ID] = p.Kind
	}
	if plateKind[PlateRows] == "" {
		return errors.New("bayes.plates: no rows plate")
	}
	names := map[string]string{} // Stan name → data kind ("" for a written variable)
	sized := map[string]bool{}   // what a size counts: a plate, or an array of a series
	modes := 0
	for i, d := range data {
		at := fmt.Sprintf("bayes.data[%d] %s", i, d.Stan)
		if !stanName.MatchString(d.Stan) {
			return fmt.Errorf("%s: not an identifier", at)
		}
		if _, dup := names[d.Stan]; dup {
			return fmt.Errorf("%s: twice", at)
		}
		names[d.Stan] = d.Kind
		if err := checkDataEntry(d, plateKind); err != nil {
			return fmt.Errorf("%s: %w", at, err)
		}
		switch d.Kind {
		case DataSize:
			if sized[d.Of] {
				return fmt.Errorf("%s: a second size of %q", at, d.Of)
			}
			sized[d.Of] = true
		case DataMode:
			modes++
		}
	}
	if modes != 1 || len(data) == 0 || data[0] != (DataEntry{Stan: "N", Kind: DataSize, Of: PlateRows}) {
		return errors.New("bayes.data: N (the rows) first, and Mode once")
	}
	for i, d := range data {
		if d.Kind == DataSize && plateKind[d.Of] == "" {
			if kind, ok := seriesArrays[d.Of]; !ok || names[d.Of] != kind {
				return fmt.Errorf("bayes.data[%d] %s: the size of %q, which is neither a plate nor an array of the data", i, d.Stan, d.Of)
			}
		}
		if !coreKinds[d.Kind] && d.Of == RowsLag && names[RowsLag] != DataRowsSubset {
			return fmt.Errorf("bayes.data[%d] %s: positions among %s, which the data does not hold", i, d.Stan, RowsLag)
		}
		if seriesArrays[d.Stan] == d.Kind && d.Kind != "" && !sized[d.Stan] {
			return fmt.Errorf("bayes.data[%d] %s: no size of it", i, d.Stan)
		}
	}
	for id := range plateKind {
		if !sized[id] {
			return fmt.Errorf("bayes.plates: %q has no size in the data", id)
		}
	}
	effects := map[string]int{} // effect id → its components
	for _, q := range quantities {
		if eff, comp, ok := strings.Cut(q.Node, "."); ok && comp != "correlation" && q.Role == RoleQuantity {
			effects[eff]++
		}
	}
	nodes := map[string]bool{}
	for _, q := range quantities {
		if q.Role == RoleQuantity {
			nodes[q.Node] = true
		}
	}
	// the data nodes the data manifest reads (an outcome a check names, a node an extension's data names)
	dataNodes := map[string]bool{}
	for _, d := range data {
		if DataReadsColumn(d.Kind) {
			dataNodes[d.Node] = true
		}
	}
	for i, d := range data {
		if !coreKinds[d.Kind] && d.Node != "" && !dataNodes[d.Node] {
			return fmt.Errorf("bayes.data[%d] %s: the node %q is not a data node the data reads", i, d.Stan, d.Node)
		}
	}
	for i, q := range quantities {
		at := fmt.Sprintf("bayes.quantities[%d] %s", i, q.Stan)
		if !stanName.MatchString(q.Stan) {
			return fmt.Errorf("%s: not an identifier", at)
		}
		if _, dup := names[q.Stan]; dup {
			return fmt.Errorf("%s: twice", at)
		}
		names[q.Stan] = ""
		if q.Dims == nil {
			return fmt.Errorf("%s: dims is null ([] for a single value)", at)
		}
		if q.Role == RoleCheck && q.Of != "" && !dataNodes[q.Of] {
			return fmt.Errorf("%s: the outcome %q is not a data node the data reads", at, q.Of)
		}
		if err := checkQuantity(q, nodes); err != nil {
			return fmt.Errorf("%s: %w", at, err)
		}
		for _, dim := range q.Dims {
			set := 0
			for _, s := range []string{dim.Plate, dim.Size, dim.Component} {
				if s != "" {
					set++
				}
			}
			switch {
			case set != 1:
				return fmt.Errorf("%s: a dimension with one of plate, size, component", at)
			case dim.Plate != "" && plateKind[dim.Plate] == "":
				return fmt.Errorf("%s: the plate %q is not listed", at, dim.Plate)
			case dim.Size != "" && !sizeName(dim.Size, data):
				return fmt.Errorf("%s: the size %q is not an integer of the data", at, dim.Size)
			case dim.Component != "" && effects[dim.Component] < 2:
				return fmt.Errorf("%s: %q has no components", at, dim.Component)
			}
		}
	}
	return checkMapping(mapping, data, quantities)
}

func checkDataEntry(d DataEntry, plateKind map[string]string) error {
	onRows := d.Plate == PlateRows
	none := func(fields ...string) bool {
		for _, f := range fields {
			if f != "" {
				return false
			}
		}
		return true
	}
	if d.Lag != 0 && d.Kind != DataLag {
		return errors.New("a lag on another kind than lag")
	}
	if d.Support != "" && d.Kind != DataInt {
		return errors.New("a support on another kind than int")
	}
	if d.Standardize && d.Kind != DataReal && d.Kind != DataInt {
		return errors.New("standardized, and not a number")
	}
	if d.Column != "" && !DataReadsColumn(d.Kind) {
		return errors.New("a column on a kind that reads none")
	}
	switch d.Kind {
	case DataSize:
		if d.Of == "" || !none(d.Plate, d.Node, d.Target, d.Column) {
			return errors.New("a size counts one thing (of)")
		}
	case DataReal, DataInt:
		if d.Node == "" || (plateKind[d.Plate] != PlateRows && plateKind[d.Plate] != PlateGroup) || !none(d.Of, d.Target) {
			return errors.New("a column's values: a node, on the rows or a group plate")
		}
		if d.Kind == DataInt && d.Support != SupportAny && d.Support != SupportNonnegative && d.Support != SupportBinary {
			return fmt.Errorf("the support %q", d.Support)
		}
	case DataGroupIndex:
		if d.Node == "" || !onRows || plateKind[d.Target] != PlateGroup || d.Of != "" {
			return errors.New("a group index: a node, on the rows, its target a group plate")
		}
	case DataCategory:
		if d.Node == "" || !onRows || plateKind[LevelsPrefix+d.Node] != PlateLevels || !none(d.Of, d.Target) {
			return errors.New("a category: a node on the rows, with its levels plate")
		}
	case DataLift:
		if d.Node == "" || plateKind[d.Plate] != PlateGroup || plateKind[d.Target] != PlateGroup || d.Plate == d.Target || d.Of != "" {
			return errors.New("a lift: an index node, from a group plate to another")
		}
	case DataMode:
		if d.Stan != "Mode" || !none(d.Of, d.Plate, d.Node, d.Target) {
			return errors.New("the mode flag is Mode, alone")
		}
	case DataRowsSubset, DataFirst, DataNext, DataPrevious, DataLag:
		if !onRows || !none(d.Of, d.Node, d.Target) {
			return errors.New("an array of a series: on the rows, nothing else")
		}
		if (d.Kind == DataLag) != (d.Lag >= 1) {
			return errors.New("a lag of k ≥ 1 steps")
		}
	default:
		// an extension's datum may name a data node (its outcome, a group index): checked against the data in CheckManifests;
		// positions among a statement's rows name them in of, the rows plate or Rows_lag (Correction of 2026-11-24 from P3a)
		if !extensionKind.MatchString(d.Kind) || !extensionName.MatchString(d.Stan) ||
			!none(d.Plate, d.Target, d.Column) || (d.Of != "" && d.Of != PlateRows && d.Of != RowsLag) {
			return fmt.Errorf("the kind %q", d.Kind)
		}
	}
	return nil
}

func checkQuantity(q Quantity, nodes map[string]bool) error {
	switch q.Role {
	case RoleQuantity:
		if q.Node == "" || q.Check != "" || q.Of != "" {
			return errors.New("a quantity: its node, nothing else")
		}
	case RoleInternal:
		if q.Node != "" || q.Check != "" || q.Of != "" || q.Triangle != "" {
			return errors.New("an internal variable has no node")
		}
	case RoleCheck:
		if q.Check == "" || q.Node != "" || !extensionName.MatchString(q.Stan) {
			return errors.New("a check: a statistic, no node")
		}
	case RolePriorDraw:
		if q.Of == "" || !nodes[q.Of] || q.Node != "" || q.Check != "" || !extensionName.MatchString(q.Stan) {
			return errors.New("a prior draw: of a quantity of the program")
		}
	case RolePriorMarginal:
		if q.Of == "" || !nodes[q.Of] || q.Node != "" || q.Check != "" || len(q.Dims) != 0 || !extensionName.MatchString(q.Stan) {
			return errors.New("a prior marginal: a single value, of a quantity of the program")
		}
	default:
		return fmt.Errorf("the role %q", q.Role)
	}
	if q.Triangle != "" && (q.Triangle != TriangleStrictLower || len(q.Dims) != 2 || q.Dims[0].Component == "" ||
		q.Dims[0] != q.Dims[1]) {
		return errors.New("a triangle: strict_lower, of a correlation over one effect's components")
	}
	return nil
}

// sizeName says whether name is an integer of the data a dimension may be sized by: a size, or an extension's datum.
func sizeName(name string, data []DataEntry) bool {
	for _, d := range data {
		if d.Stan == name {
			return d.Kind == DataSize || !coreKinds[d.Kind]
		}
	}
	return false
}

// checkMapping checks that every variable of the data block and every written variable has its entry in the mapping, the
// quantities with their node.
func checkMapping(mapping []MappingEntry, data []DataEntry, quantities []Quantity) error {
	by := map[string]MappingEntry{}
	for _, e := range mapping {
		if _, dup := by[e.Stan]; dup {
			return fmt.Errorf("bayes.mapping: %s twice", e.Stan)
		}
		by[e.Stan] = e
	}
	for _, d := range data {
		if e, ok := by[d.Stan]; !ok || e.Block != "data" {
			return fmt.Errorf("bayes.mapping: no data entry for %s", d.Stan)
		}
	}
	for _, q := range quantities {
		e, ok := by[q.Stan]
		if !ok || e.Role == nil || *e.Role != q.Role {
			return fmt.Errorf("bayes.mapping: no entry of the role %s for %s", q.Role, q.Stan)
		}
		if q.Role != RoleQuantity {
			continue
		}
		node, comp, _ := strings.Cut(q.Node, ".")
		want := e.NodeID != nil && *e.NodeID == node
		switch {
		case comp == "correlation":
			want = want && e.Kind == KindCorrelation
		case comp != "":
			want = want && e.Kind == KindComponent && e.Component != nil && *e.Component == comp
		}
		if !want {
			return fmt.Errorf("bayes.mapping: %s is not the entry of the node %s", q.Stan, q.Node)
		}
	}
	return nil
}

// Sizes are the numbers the runner learns from the data, by what they count: "rows" (N, the kept rows), a group plate's id
// (its units), "levels:<category>" (its levels K, the reference included), and the name of any other integer of the data (the
// series' N_lag, N_first, N_next, an extension's integers).
type Sizes map[string]int

// SamplerColumns are the sampler's fields that start every line of CmdStan's CSV: seven under NUTS, two under fixed_param
// (measured on CmdStan 2.40.0 of the image, lot K0).
var (
	SamplerColumnsNUTS       = []string{"lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__"}
	SamplerColumnsFixedParam = []string{"lp__", "accept_stat__"}
)

// Header is the CSV header CmdStan writes for these quantities in this mode: the sampler's fields, then each variable's
// elements as name.i.j, the first index varying fastest (column-major, for matrices and for arrays of vectors alike: measured
// on CmdStan 2.40.0 of the image, lot K0). A plate dimension has the plate's units, a levels plate K - 1 (the levels after the
// reference); a component dimension the effect's components; a prior draw with a dimension has none in the sample mode (its
// size is Mode == 1 ? s : 0).
func Header(mode string, quantities []Quantity, sizes Sizes) ([]string, error) {
	var out []string
	switch mode {
	case ModeSample:
		out = append(out, SamplerColumnsNUTS...)
	case ModePriorPredictive:
		out = append(out, SamplerColumnsFixedParam...)
	default:
		return nil, fmt.Errorf("no CSV in the mode %q", mode)
	}
	components := map[string]int{}
	for _, q := range quantities {
		if eff, comp, ok := strings.Cut(q.Node, "."); ok && comp != "correlation" && q.Role == RoleQuantity {
			components[eff]++
		}
	}
	for _, q := range quantities {
		lengths := make([]int, len(q.Dims))
		for i, d := range q.Dims {
			n, err := dimLength(d, sizes, components)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", q.Stan, err)
			}
			if q.Role == RolePriorDraw && mode == ModeSample {
				n = 0
			}
			lengths[i] = n
		}
		out = append(out, elementNames(q.Stan, lengths)...)
	}
	return out, nil
}

func dimLength(d Dim, sizes Sizes, components map[string]int) (int, error) {
	var n int
	var ok bool
	switch {
	case d.Component != "":
		n, ok = components[d.Component], components[d.Component] > 0
	case strings.HasPrefix(d.Plate, LevelsPrefix):
		n, ok = sizes[d.Plate]
		n--
	case d.Plate != "":
		n, ok = sizes[d.Plate]
	default:
		n, ok = sizes[d.Size]
	}
	if !ok || n < 0 {
		return 0, fmt.Errorf("no size for %+v", d)
	}
	return n, nil
}

// elementNames are a variable's elements as CmdStan names them, the first index fastest; a single value is its name.
func elementNames(name string, lengths []int) []string {
	total := 1
	for _, n := range lengths {
		total *= n
	}
	if len(lengths) == 0 {
		return []string{name}
	}
	out := make([]string, 0, total)
	index := make([]int, len(lengths))
	for k := 0; k < total; k++ {
		var b strings.Builder
		b.WriteString(name)
		for _, i := range index {
			b.WriteByte('.')
			b.WriteString(strconv.Itoa(i + 1))
		}
		out = append(out, b.String())
		for j := range index {
			index[j]++
			if index[j] < lengths[j] {
				break
			}
			index[j] = 0
		}
	}
	return out
}
