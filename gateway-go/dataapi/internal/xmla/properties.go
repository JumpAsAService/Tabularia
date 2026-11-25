package xmla

// DISCOVER_PROPERTIES: the properties of the server, as XMLA 1.1 describes them (the columns
// are the specification's).

// RowsetProperties is the name of the rowset.
const RowsetProperties = "DISCOVER_PROPERTIES"

// ProviderName and ProviderVersion are what the server says it is.
const (
	ProviderName    = "Tabularia"
	ProviderVersion = "1.0"
)

// PropertiesColumns are the columns of DISCOVER_PROPERTIES.
var PropertiesColumns = []Column{
	{Name: "PropertyName", Type: Str},
	{Name: "PropertyDescription", Type: Str, Nullable: true},
	{Name: "PropertyType", Type: Str, Nullable: true},
	{Name: "PropertyAccessType", Type: Str},
	{Name: "IsRequired", Type: Bool, Nullable: true},
	{Name: "Value", Type: Str, Nullable: true},
}

// PropertiesRowset is the rows of DISCOVER_PROPERTIES for the data API whose catalog is
// `catalog`, narrowed by the PropertyName restriction when the client sent one.
func PropertiesRowset(catalog string, restrictions map[string][]string) [][]string {
	all := [][]string{
		{"ProviderName", "The name of the provider", "string", "Read", "false", ProviderName},
		{"ProviderVersion", "The version of the provider", "string", "Read", "false", ProviderVersion},
		{"DataSourceInfo", "The data source's connection information", "string", "ReadWrite", "false", ""},
		{"Catalog", "The catalog: the data API's name", "string", "ReadWrite", "false", catalog},
		{"Content", "The content of the answer", "string", "ReadWrite", "false", "SchemaData"},
		{"Format", "The format of the answer", "string", "ReadWrite", "false", "Native"},
		{"AxisFormat", "The format of the axes of a multidimensional answer", "string", "ReadWrite", "false", "TupleFormat"},
		{"Timeout", "The longest a request may take, in seconds", "int", "ReadWrite", "false", ""},
		{"LocaleIdentifier", "The locale of the answer", "int", "ReadWrite", "false", "1033"},
		{"MdpropMdxSubqueries", "The levels of subselects the MDX accepts", "int", "Read", "false", "15"},
		{"DbpropMsmdSubqueries", "The subselects in the FROM clause", "int", "ReadWrite", "false", "2"},
		{"MdpropMdxDrillFunctions", "The drill functions the MDX accepts", "int", "Read", "false", "7"},
		{"MdpropMdxNamedSets", "The named sets the MDX accepts", "int", "Read", "false", "0"},
		{"ShowHiddenCubes", "Whether hidden cubes are listed", "boolean", "ReadWrite", "false", "false"},
	}
	names := restrictions["PropertyName"]
	if len(names) == 0 {
		return all
	}
	out := [][]string{}
	for _, row := range all {
		for _, n := range names {
			if row[0] == n {
				out = append(out, row)
				break
			}
		}
	}
	return out
}
