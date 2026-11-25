package xmla

// Discover (docs/specs/xmla.md, piece 2): the rowsets a pivot table reads to list the catalogs,
// cubes, measures, dimensions, hierarchies, levels and members of a data API. The columns of each
// rowset, their order and their types are the ones the XMLA specification gives for it; a row
// leaves out a column it has nothing for (the schema says which may be left out).

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// Catalog is what a pivot table is connected to: a data API, with the cubes of the resources the
// key may read.
type Catalog struct {
	Name  string
	Cubes []*xmlamodel.Cube
}

// the rowsets
const (
	RowsetDatasources   = "DISCOVER_DATASOURCES"
	RowsetSchemaRowsets = "DISCOVER_SCHEMA_ROWSETS"
	RowsetEnumerators   = "DISCOVER_ENUMERATORS"
	RowsetKeywords      = "DISCOVER_KEYWORDS"
	RowsetLiterals      = "DISCOVER_LITERALS"
	RowsetCatalogs      = "DBSCHEMA_CATALOGS"
	RowsetCubes         = "MDSCHEMA_CUBES"
	RowsetMeasureGroups = "MDSCHEMA_MEASUREGROUPS"
	RowsetMeasures      = "MDSCHEMA_MEASURES"
	RowsetDimensions    = "MDSCHEMA_DIMENSIONS"
	RowsetHierarchies   = "MDSCHEMA_HIERARCHIES"
	RowsetLevels        = "MDSCHEMA_LEVELS"
	RowsetMembers       = "MDSCHEMA_MEMBERS"
	RowsetProps         = "MDSCHEMA_PROPERTIES"
	RowsetSets          = "MDSCHEMA_SETS"
	RowsetKPIs          = "MDSCHEMA_KPIS"
	RowsetMGDimensions  = "MDSCHEMA_MEASUREGROUP_DIMENSIONS"
	RowsetFunctions     = "MDSCHEMA_FUNCTIONS"
)

// Restriction is a restriction a rowset takes: its name and XML Schema type.
type Restriction struct{ Name, Type string }

type spec struct {
	name, guid, description string
	cols                    []Column
	restrictions            []Restriction
	// whether the rowset needs the cubes (and so the data to be loaded)
	cubes bool
}

func s(name string) Column         { return Column{Name: name, Type: Str} }
func sn(name string) Column        { return Column{Name: name, Type: Str, Nullable: true} }
func col(name, typ string) Column  { return Column{Name: name, Type: typ} }
func coln(name, typ string) Column { return Column{Name: name, Type: typ, Nullable: true} }
func rs(names ...string) []Restriction {
	out := make([]Restriction, len(names))
	for i, n := range names {
		out[i] = Restriction{n, Str}
	}
	return out
}

func with(r []Restriction, more ...Restriction) []Restriction {
	return append(r[:len(r):len(r)], more...)
}

// the restrictions every multidimensional schema rowset starts with
var mdBase = rs("CATALOG_NAME", "SCHEMA_NAME", "CUBE_NAME")

var specs = []*spec{
	{name: RowsetProperties, description: "The properties of the server", cols: PropertiesColumns, restrictions: rs("PropertyName")},
	{name: RowsetSchemaRowsets, description: "The rowsets this server answers, with their restrictions",
		cols: []Column{s("SchemaName"), coln("SchemaGuid", "uuid"), {Name: "Restrictions", Type: "", Nullable: true, Repeat: true,
			Inner: `<xsd:complexType><xsd:sequence><xsd:element sql:field="Name" name="Name" type="xsd:string"/><xsd:element sql:field="Type" name="Type" type="xsd:string"/></xsd:sequence></xsd:complexType>`},
			sn("Description"), col("RestrictionsMask", ULong)},
		restrictions: rs("SchemaName")},
	{name: RowsetDatasources, description: "The data sources of the server",
		cols: []Column{s("DataSourceName"), sn("DataSourceDescription"), sn("URL"), sn("DataSourceInfo"), s("ProviderName"),
			sn("ProviderType"), sn("AuthenticationMode")},
		restrictions: rs("DataSourceName", "URL", "ProviderName", "ProviderType", "AuthenticationMode")},
	{name: RowsetEnumerators, description: "The values the enumerated properties take",
		cols:         []Column{s("EnumName"), sn("EnumDescription"), sn("EnumType"), s("ElementName"), sn("ElementDescription"), sn("ElementValue")},
		restrictions: rs("EnumName")},
	{name: RowsetKeywords, description: "The reserved words of the MDX the server reads",
		cols: []Column{s("Keyword")}, restrictions: rs("Keyword")},
	{name: RowsetLiterals, description: "The literals of the MDX the server reads",
		cols:         []Column{s("LiteralName"), sn("LiteralValue"), sn("LiteralInvalidChars"), sn("LiteralInvalidStartingChars"), coln("LiteralMaxLength", Int)},
		restrictions: rs("LiteralName")},
	{name: RowsetCatalogs, guid: "C8B52211-5CF3-11CE-ADE5-00AA0044773D", description: "The catalogs: one, the data API",
		cols:         []Column{s("CATALOG_NAME"), sn("DESCRIPTION"), sn("ROLES"), coln("DATE_MODIFIED", DateTime)},
		restrictions: rs("CATALOG_NAME")},
	{name: RowsetCubes, guid: "C8B522D8-5CF3-11CE-ADE5-00AA0044773D", description: "The cubes: one for each resource the key may read", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("CUBE_TYPE"), sn("CUBE_GUID"), coln("CREATED_ON", DateTime),
			coln("LAST_SCHEMA_UPDATE", DateTime), sn("SCHEMA_UPDATED_BY"), coln("LAST_DATA_UPDATE", DateTime), sn("DATA_UPDATED_BY"),
			sn("DESCRIPTION"), col("IS_DRILLTHROUGH_ENABLED", Bool), col("IS_LINKABLE", Bool), col("IS_WRITE_ENABLED", Bool), col("IS_SQL_ENABLED", Bool),
			sn("CUBE_CAPTION"), sn("BASE_CUBE_NAME"), coln("CUBE_SOURCE", UShort)},
		restrictions: with(mdBase, Restriction{"CUBE_TYPE", Str}, Restriction{"BASE_CUBE_NAME", Str}, Restriction{"CUBE_SOURCE", UShort})},
	{name: RowsetMeasureGroups, description: "The measure groups: one for each cube", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("MEASUREGROUP_NAME"), sn("DESCRIPTION"), col("IS_WRITE_ENABLED", Bool),
			sn("MEASUREGROUP_CAPTION")},
		restrictions: with(mdBase, Restriction{"MEASUREGROUP_NAME", Str})},
	{name: RowsetMeasures, guid: "C8B522DC-5CF3-11CE-ADE5-00AA0044773D", description: "The measures of the cubes", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("MEASURE_NAME"), s("MEASURE_UNIQUE_NAME"), sn("MEASURE_CAPTION"),
			sn("MEASURE_GUID"), col("MEASURE_AGGREGATOR", Int), col("DATA_TYPE", UShort), coln("NUMERIC_PRECISION", UShort),
			coln("NUMERIC_SCALE", Short), sn("MEASURE_UNITS"), sn("DESCRIPTION"), sn("EXPRESSION"), col("MEASURE_IS_VISIBLE", Bool),
			sn("LEVELS_LIST"), sn("MEASURE_NAME_SQL_COLUMN_NAME"), sn("MEASURE_UNQUALIFIED_CAPTION"), sn("MEASUREGROUP_NAME"),
			sn("MEASURE_DISPLAY_FOLDER"), sn("DEFAULT_FORMAT_STRING")},
		restrictions: with(mdBase, Restriction{"MEASURE_NAME", Str}, Restriction{"MEASURE_UNIQUE_NAME", Str}, Restriction{"CUBE_SOURCE", UShort},
			Restriction{"MEASURE_VISIBILITY", UShort})},
	{name: RowsetDimensions, guid: "C8B522D9-5CF3-11CE-ADE5-00AA0044773D", description: "The dimensions of the cubes, the measures' among them", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("DIMENSION_NAME"), s("DIMENSION_UNIQUE_NAME"), sn("DIMENSION_GUID"),
			sn("DIMENSION_CAPTION"), col("DIMENSION_ORDINAL", UInt), col("DIMENSION_TYPE", Short), coln("DIMENSION_CARDINALITY", UInt),
			sn("DEFAULT_HIERARCHY"), sn("DESCRIPTION"), col("IS_VIRTUAL", Bool), col("IS_READWRITE", Bool), col("DIMENSION_UNIQUE_SETTINGS", Int),
			sn("DIMENSION_MASTER_UNIQUE_NAME"), col("DIMENSION_IS_VISIBLE", Bool)},
		restrictions: with(mdBase, Restriction{"DIMENSION_NAME", Str}, Restriction{"DIMENSION_UNIQUE_NAME", Str}, Restriction{"CUBE_SOURCE", UShort},
			Restriction{"DIMENSION_VISIBILITY", UShort})},
	{name: RowsetHierarchies, guid: "C8B522DA-5CF3-11CE-ADE5-00AA0044773D", description: "The hierarchies of the dimensions", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("DIMENSION_UNIQUE_NAME"), s("HIERARCHY_NAME"), s("HIERARCHY_UNIQUE_NAME"),
			sn("HIERARCHY_GUID"), sn("HIERARCHY_CAPTION"), col("DIMENSION_TYPE", Short), coln("HIERARCHY_CARDINALITY", UInt), sn("DEFAULT_MEMBER"),
			sn("ALL_MEMBER"), sn("DESCRIPTION"), col("STRUCTURE", Short), col("IS_VIRTUAL", Bool), col("IS_READWRITE", Bool),
			col("DIMENSION_UNIQUE_SETTINGS", Int), col("DIMENSION_IS_VISIBLE", Bool), col("HIERARCHY_ORDINAL", UInt), col("DIMENSION_IS_SHARED", Bool),
			col("HIERARCHY_IS_VISIBLE", Bool), col("HIERARCHY_ORIGIN", UShort), sn("HIERARCHY_DISPLAY_FOLDER"), col("INSTANCE_SELECTION", UShort)},
		restrictions: with(mdBase, Restriction{"DIMENSION_UNIQUE_NAME", Str}, Restriction{"HIERARCHY_NAME", Str}, Restriction{"HIERARCHY_UNIQUE_NAME", Str},
			Restriction{"HIERARCHY_ORIGIN", UShort}, Restriction{"CUBE_SOURCE", UShort}, Restriction{"HIERARCHY_VISIBILITY", UShort})},
	{name: RowsetLevels, guid: "C8B522DB-5CF3-11CE-ADE5-00AA0044773D", description: "The levels of the hierarchies", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("DIMENSION_UNIQUE_NAME"), s("HIERARCHY_UNIQUE_NAME"), s("LEVEL_NAME"),
			s("LEVEL_UNIQUE_NAME"), sn("LEVEL_GUID"), sn("LEVEL_CAPTION"), col("LEVEL_NUMBER", UInt), coln("LEVEL_CARDINALITY", UInt), col("LEVEL_TYPE", Int),
			col("CUSTOM_ROLLUP_SETTING", Int), col("LEVEL_UNIQUE_SETTINGS", Int), col("LEVEL_IS_VISIBLE", Bool), sn("DESCRIPTION"),
			col("LEVEL_ORIGIN", UShort)},
		restrictions: with(mdBase, Restriction{"DIMENSION_UNIQUE_NAME", Str}, Restriction{"HIERARCHY_UNIQUE_NAME", Str}, Restriction{"LEVEL_NAME", Str},
			Restriction{"LEVEL_UNIQUE_NAME", Str}, Restriction{"LEVEL_ORIGIN", UShort}, Restriction{"CUBE_SOURCE", UShort}, Restriction{"LEVEL_VISIBILITY", UShort})},
	{name: RowsetMembers, guid: "C8B522DE-5CF3-11CE-ADE5-00AA0044773D", description: "The members of the levels, read from the data", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("DIMENSION_UNIQUE_NAME"), s("HIERARCHY_UNIQUE_NAME"), s("LEVEL_UNIQUE_NAME"),
			col("LEVEL_NUMBER", UInt), col("MEMBER_ORDINAL", UInt), s("MEMBER_NAME"), s("MEMBER_UNIQUE_NAME"), col("MEMBER_TYPE", Int), sn("MEMBER_GUID"),
			sn("MEMBER_CAPTION"), col("CHILDREN_CARDINALITY", UInt), coln("PARENT_LEVEL", UInt), sn("PARENT_UNIQUE_NAME"), col("PARENT_COUNT", UInt),
			sn("DESCRIPTION"), sn("MEMBER_KEY")},
		restrictions: with(mdBase, Restriction{"DIMENSION_UNIQUE_NAME", Str}, Restriction{"HIERARCHY_UNIQUE_NAME", Str}, Restriction{"LEVEL_UNIQUE_NAME", Str},
			Restriction{"LEVEL_NUMBER", UInt}, Restriction{"MEMBER_NAME", Str}, Restriction{"MEMBER_UNIQUE_NAME", Str}, Restriction{"MEMBER_CAPTION", Str},
			Restriction{"MEMBER_TYPE", Int}, Restriction{"TREE_OP", Int}, Restriction{"CUBE_SOURCE", UShort})},
	{name: RowsetProps, guid: "C8B522DD-5CF3-11CE-ADE5-00AA0044773D", description: "The member properties: none beyond the intrinsic ones", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("DIMENSION_UNIQUE_NAME"), s("HIERARCHY_UNIQUE_NAME"), s("LEVEL_UNIQUE_NAME"),
			sn("MEMBER_UNIQUE_NAME"), col("PROPERTY_TYPE", Short), s("PROPERTY_NAME"), sn("PROPERTY_CAPTION"), col("DATA_TYPE", UShort),
			coln("PROPERTY_CONTENT_TYPE", Short), sn("DESCRIPTION")},
		restrictions: with(mdBase, Restriction{"DIMENSION_UNIQUE_NAME", Str}, Restriction{"HIERARCHY_UNIQUE_NAME", Str}, Restriction{"LEVEL_UNIQUE_NAME", Str},
			Restriction{"MEMBER_UNIQUE_NAME", Str}, Restriction{"PROPERTY_TYPE", Short}, Restriction{"PROPERTY_NAME", Str},
			Restriction{"PROPERTY_CONTENT_TYPE", Short}, Restriction{"PROPERTY_ORIGIN", UShort}, Restriction{"CUBE_SOURCE", UShort},
			Restriction{"PROPERTY_VISIBILITY", UShort})},
	{name: RowsetSets, guid: "A07CCD0B-8148-11D0-87BB-00C04FC33942", description: "The named sets: none", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("SET_NAME"), col("SCOPE", Int), sn("DESCRIPTION"), sn("EXPRESSION"),
			sn("DIMENSIONS"), sn("SET_CAPTION"), sn("SET_DISPLAY_FOLDER"), sn("SET_EVALUATION_CONTEXT")},
		restrictions: with(mdBase, Restriction{"SET_NAME", Str}, Restriction{"SCOPE", Int}, Restriction{"CUBE_SOURCE", UShort},
			Restriction{"HIERARCHY_UNIQUE_NAME", Str})},
	{name: RowsetKPIs, guid: "2AE44109-ED3D-4842-B16F-B694D1CB0E3F", description: "The KPIs: none", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), sn("MEASUREGROUP_NAME"), s("KPI_NAME"), sn("KPI_CAPTION"), sn("KPI_DESCRIPTION"),
			sn("KPI_DISPLAY_FOLDER"), sn("KPI_VALUE"), sn("KPI_GOAL"), sn("KPI_STATUS"), sn("KPI_TREND"), sn("KPI_STATUS_GRAPHIC"),
			sn("KPI_TREND_GRAPHIC"), sn("KPI_WEIGHT"), sn("KPI_CURRENT_TIME_MEMBER"), sn("KPI_PARENT_KPI_NAME"), col("SCOPE", Int)},
		restrictions: with(mdBase, Restriction{"KPI_NAME", Str}, Restriction{"CUBE_SOURCE", UShort})},
	{name: RowsetMGDimensions, guid: "A07CCD33-8148-11D0-87BB-00C04FC33942", description: "The dimensions each measure group is related to", cubes: true,
		cols: []Column{s("CATALOG_NAME"), sn("SCHEMA_NAME"), s("CUBE_NAME"), s("MEASUREGROUP_NAME"), sn("MEASUREGROUP_CARDINALITY"),
			s("DIMENSION_UNIQUE_NAME"), sn("DIMENSION_CARDINALITY"), col("DIMENSION_IS_VISIBLE", Bool), col("DIMENSION_IS_FACT_DIMENSION", Bool),
			{Name: "DIMENSION_PATH", Nullable: true, Inner: `<xsd:complexType><xsd:sequence><xsd:element name="MeasureGroupDimension" type="xsd:string" minOccurs="0" maxOccurs="unbounded"/></xsd:sequence></xsd:complexType>`},
			sn("DESCRIPTION")},
		restrictions: with(mdBase, Restriction{"MEASUREGROUP_NAME", Str}, Restriction{"DIMENSION_UNIQUE_NAME", Str})},
	{name: RowsetFunctions, guid: "A07CCD07-8148-11D0-87BB-00C04FC33942", description: "The MDX functions the server understands",
		cols: []Column{s("FUNCTION_NAME"), sn("DESCRIPTION"), sn("PARAMETER_LIST"), col("RETURN_TYPE", Int), col("ORIGIN", Int), sn("INTERFACE_NAME"),
			sn("LIBRARY_NAME"), sn("CAPTION")},
		restrictions: rs("LIBRARY_NAME", "INTERFACE_NAME", "FUNCTION_NAME", "ORIGIN")},
}

func specOf(name string) *spec {
	for _, sp := range specs {
		if strings.EqualFold(sp.name, name) {
			return sp
		}
	}
	return nil
}

// Served says whether a rowset is one this interface answers.
func Served(name string) bool { return specOf(name) != nil }

// Columns are the columns of a served rowset, in the order the standard gives them; nil for a
// rowset that is not served.
func Columns(name string) []Column {
	if sp := specOf(name); sp != nil {
		return sp.cols
	}
	return nil
}

// RestrictionsOf are the restrictions a served rowset takes.
func RestrictionsOf(name string) []Restriction {
	if sp := specOf(name); sp != nil {
		return sp.restrictions
	}
	return nil
}

// Rowsets are the names of the rowsets served, in the order DISCOVER_SCHEMA_ROWSETS lists them.
func Rowsets() []string {
	out := make([]string, len(specs))
	for i, sp := range specs {
		out[i] = sp.name
	}
	return out
}

// NeedsCubes says whether answering a rowset needs the cubes (and so the data to be loaded).
func NeedsCubes(name string) bool {
	sp := specOf(name)
	return sp != nil && sp.cubes
}

// Data is what Discover reads of the data: the members of the levels. The pod gives it over its
// DuckDB tables; the answers' order is the order of the values (a null last).
type Data interface {
	// Members calls fn for each member of a level, in order, with the number of its children. With
	// under set, only the members below that member (of a level above this one). A fn that returns
	// an error ends the read, and Members returns it.
	Members(ctx context.Context, cube *xmlamodel.Cube, level *xmlamodel.Level, under *xmlamodel.Member, fn func(m xmlamodel.Member, children int) error) error
	// Member looks one member up: whether the data has it, and how many children it has.
	Member(ctx context.Context, cube *xmlamodel.Cube, m xmlamodel.Member) (children int, found bool, err error)
}

// Limits are the administrator's caps a Discover answer meets.
type Limits struct {
	// the most members one answer holds (DATA_API__XMLA_MAX_MEMBERS)
	MaxMembers int
}

// Discover answers a Discover request: the whole envelope is written to w only if the answer can
// be made (otherwise a fault is returned and nothing is written). It returns how many rows it
// wrote. An unknown rowset is a fault that names it.
func Discover(ctx context.Context, w io.Writer, req *Request, cat *Catalog, data Data, lim Limits) (int, error) {
	sp := specOf(req.RequestType)
	if sp == nil {
		return 0, NewFault(CodeNotServed, "The rowset «"+Shorten(req.RequestType)+"» is not served by this interface")
	}
	if lim.MaxMembers <= 0 {
		lim.MaxMembers = 5_000
	}
	r := restrictions(req.Restrictions)
	// the answer is held until it is whole: a fault must not follow a byte (it is at most the cap of
	// members' rows, and the other rowsets are small)
	var buf bytes.Buffer
	rw := NewRowsetWriter(&buf, sp.cols)
	// the rowsets are written by one function each, which puts its rows in the writer
	var err error
	switch sp.name {
	case RowsetProperties:
		for _, row := range PropertiesRowset(cat.Name, req.Restrictions) {
			rw.Row(row...)
		}
	case RowsetSchemaRowsets:
		schemaRowsets(rw, r)
	case RowsetDatasources:
		datasources(rw, r)
	case RowsetEnumerators:
		enumerators(rw, r)
	case RowsetKeywords:
		keywords(rw, r)
	case RowsetLiterals:
		literals(rw, r)
	case RowsetCatalogs:
		catalogs(rw, r, cat)
	case RowsetFunctions:
		functions(rw, r)
	case RowsetMembers:
		err = members(ctx, rw, r, cat, data, lim)
	case RowsetCubes, RowsetMeasureGroups, RowsetMeasures, RowsetDimensions, RowsetHierarchies, RowsetLevels, RowsetMGDimensions:
		structure(rw, sp.name, r, cat)
	case RowsetProps, RowsetSets, RowsetKPIs:
		// nothing to say: the empty rowset has the columns and no row
	}
	if err != nil {
		return 0, err
	}
	if err := rw.Close(); err != nil {
		return 0, NewFault(CodeInternal, "The answer could not be written")
	}
	if _, err := w.Write(buf.Bytes()); err != nil {
		return 0, err
	}
	return rw.Rows(), nil
}

// shorten cuts a name a client sent before it is written in an answer or a log.
func Shorten(name string) string {
	if len(name) > 64 {
		name = name[:64] + "…"
	}
	return strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return -1
		}
		return r
	}, name)
}

// ── restrictions ──────────────────────────────────────────────────────────────

type restr map[string][]string

// restrictions drops the empty values: a restriction with no text is a restriction not given.
func restrictions(in map[string][]string) restr {
	out := restr{}
	for k, vs := range in {
		for _, v := range vs {
			if v != "" {
				out[strings.ToUpper(k)] = append(out[strings.ToUpper(k)], v)
			}
		}
	}
	return out
}

func (r restr) given(name string) bool { return len(r[name]) > 0 }

// is says whether the value meets the restriction of this name: true when there is none, else
// when it equals one of the values, without regard to case.
func (r restr) is(name, value string) bool {
	vs := r[name]
	if len(vs) == 0 {
		return true
	}
	for _, v := range vs {
		if strings.EqualFold(v, value) {
			return true
		}
	}
	return false
}

// flag says whether a restriction that is a set of bits (CUBE_SOURCE, …_VISIBILITY, TREE_OP, …)
// shares a bit with the value; true when there is none, and for a value that is not a number.
func (r restr) flag(name string, bits int) bool {
	vs := r[name]
	if len(vs) == 0 {
		return true
	}
	for _, v := range vs {
		n, err := strconv.Atoi(strings.TrimSpace(v))
		if err != nil || n&bits != 0 {
			return true
		}
	}
	return false
}

// number says whether a restriction holding a number equals this one.
func (r restr) number(name string, n int) bool {
	vs := r[name]
	if len(vs) == 0 {
		return true
	}
	for _, v := range vs {
		if x, err := strconv.Atoi(strings.TrimSpace(v)); err == nil && x == n {
			return true
		}
	}
	return false
}

// the CATALOG_NAME restriction names the one catalog
func (r restr) catalog(cat *Catalog) bool { return r.is("CATALOG_NAME", cat.Name) }

// ── small rowsets ─────────────────────────────────────────────────────────────

// put writes a row from the columns' values by name.
func put(rw *RowsetWriter, v map[string]string) {
	row := make([]string, len(rw.cols))
	for i, c := range rw.cols {
		row[i] = v[c.Name]
	}
	rw.Row(row...)
}

func boolText(b bool) string { return strconv.FormatBool(b) }

func schemaRowsets(rw *RowsetWriter, r restr) {
	for _, sp := range specs {
		if !r.is("SCHEMANAME", sp.name) {
			continue
		}
		var rs strings.Builder
		for _, x := range sp.restrictions {
			rs.WriteString("<Restrictions><Name>" + escape(x.Name) + "</Name><Type>" + escape(x.Type) + "</Type></Restrictions>")
		}
		mask := uint64(1)<<uint(len(sp.restrictions)) - 1
		put(rw, map[string]string{"SchemaName": sp.name, "SchemaGuid": sp.guid, "Restrictions": rs.String(), "Description": sp.description,
			"RestrictionsMask": strconv.FormatUint(mask, 10)})
	}
}

func datasources(rw *RowsetWriter, r restr) {
	if !r.is("DATASOURCENAME", ProviderName) || !r.is("PROVIDERNAME", ProviderName) || !r.is("PROVIDERTYPE", "MDP") ||
		!r.is("AUTHENTICATIONMODE", "Authenticated") || r.given("URL") {
		return
	}
	put(rw, map[string]string{"DataSourceName": ProviderName, "DataSourceDescription": "Tabularia data APIs", "ProviderName": ProviderName,
		"ProviderType": "MDP", "AuthenticationMode": "Authenticated"})
}

func catalogs(rw *RowsetWriter, r restr, cat *Catalog) {
	if !r.catalog(cat) {
		return
	}
	put(rw, map[string]string{"CATALOG_NAME": cat.Name})
}

var enumerations = []struct {
	name, description, typ string
	elements               [][2]string // name, value
}{
	{"Content", "What an answer holds", "string", [][2]string{{"None", "0"}, {"Schema", "1"}, {"Data", "2"}, {"SchemaData", "3"}}},
	{"Format", "The format of an answer", "string", [][2]string{{"Tabular", "1"}, {"Multidimensional", "2"}, {"Native", "3"}}},
	{"AxisFormat", "The format of the axes of a multidimensional answer", "string", [][2]string{{"TupleFormat", "1"}, {"ClusterFormat", "2"}, {"CustomFormat", "3"}}},
	{"ProviderType", "The kind of provider", "string", [][2]string{{"MDP", "1"}}},
	{"AuthenticationMode", "How a client is known", "string", [][2]string{{"Authenticated", "2"}}},
}

func enumerators(rw *RowsetWriter, r restr) {
	for _, e := range enumerations {
		if !r.is("ENUMNAME", e.name) {
			continue
		}
		for _, el := range e.elements {
			put(rw, map[string]string{"EnumName": e.name, "EnumDescription": e.description, "EnumType": e.typ, "ElementName": el[0], "ElementValue": el[1]})
		}
	}
}

// the words of the MDX subset a pivot table writes, and the ones the full language reserves that a
// name may not be
var keywordList = []string{"AND", "AS", "AXIS", "CELL", "CHAPTERS", "COLUMNS", "DIMENSION", "EMPTY", "FROM", "MEMBER", "NON", "NOT", "ON", "OR", "PAGES",
	"PROPERTIES", "ROWS", "SECTIONS", "SELECT", "SET", "WHERE", "WITH", "XOR"}

func keywords(rw *RowsetWriter, r restr) {
	for _, k := range keywordList {
		if r.is("KEYWORD", k) {
			put(rw, map[string]string{"Keyword": k})
		}
	}
}

func literals(rw *RowsetWriter, r restr) {
	for _, l := range [][3]string{
		{"DBLITERAL_QUOTE_PREFIX", "[", "-1"}, {"DBLITERAL_QUOTE_SUFFIX", "]", "-1"}, {"DBLITERAL_CATALOG_NAME", "", "128"},
		{"DBLITERAL_CUBE_NAME", "", "128"}, {"DBLITERAL_DIMENSION_NAME", "", "128"}, {"DBLITERAL_HIERARCHY_NAME", "", "128"},
		{"DBLITERAL_LEVEL_NAME", "", "128"}, {"DBLITERAL_MEMBER_NAME", "", "-1"}, {"DBLITERAL_PROPERTY_NAME", "", "128"},
	} {
		if !r.is("LITERALNAME", l[0]) {
			continue
		}
		put(rw, map[string]string{"LiteralName": l[0], "LiteralValue": l[1], "LiteralMaxLength": l[2]})
	}
}

// the functions the MDX parser reads (docs/specs/xmla.md, «The MDX that is understood»)
var functionList = []struct{ name, params, description string }{
	{"AddCalculatedMembers", "<Set>", "Returns the set it is given: the cubes have no calculated members"},
	{"BottomCount", "<Set>, <Count>[, <Numeric Expression>]", "The last members of a set by a measure"},
	{"CrossJoin", "<Set>, <Set>", "The tuples of two sets"},
	{"Distinct", "<Set>", "A set without repeated tuples"},
	{"DrilldownLevel", "<Set>[, <Level>]", "Adds the members of the level below"},
	{"DrilldownMember", "<Set>, <Set>", "Adds the children of the members of the second set"},
	{"Except", "<Set>, <Set>", "The first set without the tuples of the second"},
	{"Filter", "<Set>, <Condition>", "The tuples whose measure meets a comparison"},
	{"Head", "<Set>[, <Count>]", "The first tuples of a set"},
	{"Hierarchize", "<Set>", "Puts a set in the order of its hierarchy"},
	{"Order", "<Set>, <Numeric Expression>[, ASC | DESC | BASC | BDESC]", "A set sorted by a measure"},
	{"TopCount", "<Set>, <Count>[, <Numeric Expression>]", "The first members of a set by a measure"},
}

func functions(rw *RowsetWriter, r restr) {
	for _, f := range functionList {
		if !r.is("FUNCTION_NAME", f.name) || !r.is("LIBRARY_NAME", "MDX") || r.given("INTERFACE_NAME") || !r.number("ORIGIN", 1) {
			continue
		}
		put(rw, map[string]string{"FUNCTION_NAME": f.name, "DESCRIPTION": f.description, "PARAMETER_LIST": f.params, "RETURN_TYPE": "8", "ORIGIN": "1",
			"LIBRARY_NAME": "MDX", "CAPTION": f.name})
	}
}

// ── errors of a members answer ────────────────────────────────────────────────

var errMembersCap = errors.New("members cap")

func limitFault(max int) *Fault {
	return NewFault(CodeLimit, fmt.Sprintf("This answer holds more than %d members, the cap of members in a Discover answer (DATA_API__XMLA_MAX_MEMBERS). "+
		"Ask for one level, or for the children of one member (MEMBER_UNIQUE_NAME with TREE_OP 1)", max))
}
