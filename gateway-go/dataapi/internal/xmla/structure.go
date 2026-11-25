package xmla

import (
	"strconv"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// measuresLevelUnique is the unique name of the level of the measures, as the servers this
// protocol comes from write it; the three-segment form is read as well.
var measuresLevelUnique = xmlamodel.Bracket(xmlamodel.MeasuresName) + "." + xmlamodel.Bracket(xmlamodel.MeasuresLevel)

// the DBTYPE codes of a measure's data type
const (
	dbR8      = 5   // double
	dbI8      = 20  // 64-bit integer
	dbNumeric = 131 // exact decimal
)

// the numbers the standard gives to the aggregations of a measure
func aggregator(a xmlamodel.Agg) string {
	switch a {
	case xmlamodel.AggSum:
		return "1"
	case xmlamodel.AggMin:
		return "3"
	case xmlamodel.AggMax:
		return "4"
	case xmlamodel.AggAverage:
		return "5"
	}
	return "2" // Rows counts
}

func dataType(m *xmlamodel.Measure) string {
	switch m.Result() {
	case dataapiwire.KindInt:
		return strconv.Itoa(dbI8)
	case dataapiwire.KindDecimal:
		return strconv.Itoa(dbNumeric)
	}
	return strconv.Itoa(dbR8)
}

// structure writes the rowsets that describe the cubes' structure: all but the members.
func structure(rw *RowsetWriter, name string, r restr, cat *Catalog) {
	if !r.catalog(cat) || r.given("SCHEMA_NAME") {
		return // there is one catalog and no schema
	}
	// every cube is a cube (CUBE_SOURCE 1); every name is visible
	if !r.flag("CUBE_SOURCE", 1) || !r.flag("DIMENSION_VISIBILITY", 1) || !r.flag("HIERARCHY_VISIBILITY", 1) || !r.flag("LEVEL_VISIBILITY", 1) ||
		!r.flag("MEASURE_VISIBILITY", 1) {
		return
	}
	for _, c := range cat.Cubes {
		if !r.is("CUBE_NAME", c.Name) {
			continue
		}
		base := map[string]string{"CATALOG_NAME": cat.Name, "CUBE_NAME": c.Name}
		row := func(more map[string]string) {
			for k, v := range base {
				if _, set := more[k]; !set {
					more[k] = v
				}
			}
			put(rw, more)
		}
		switch name {
		case RowsetCubes:
			if !r.is("CUBE_TYPE", "CUBE") || r.given("BASE_CUBE_NAME") {
				continue
			}
			row(map[string]string{"CUBE_TYPE": "CUBE", "LAST_DATA_UPDATE": dateTime(c.Updated), "IS_DRILLTHROUGH_ENABLED": "false", "IS_LINKABLE": "false",
				"IS_WRITE_ENABLED": "false", "IS_SQL_ENABLED": "false", "CUBE_CAPTION": c.Name, "CUBE_SOURCE": "1"})
		case RowsetMeasureGroups:
			if r.is("MEASUREGROUP_NAME", c.Name) {
				row(map[string]string{"MEASUREGROUP_NAME": c.Name, "IS_WRITE_ENABLED": "false", "MEASUREGROUP_CAPTION": c.Name})
			}
		case RowsetMeasures:
			for _, m := range c.Measures {
				if !r.is("MEASURE_NAME", m.Name) || !r.is("MEASURE_UNIQUE_NAME", m.UniqueName) {
					continue
				}
				v := map[string]string{"MEASURE_NAME": m.Name, "MEASURE_UNIQUE_NAME": m.UniqueName, "MEASURE_CAPTION": m.Name,
					"MEASURE_AGGREGATOR": aggregator(m.Agg), "DATA_TYPE": dataType(m), "DESCRIPTION": m.Description, "MEASURE_IS_VISIBLE": "true",
					"MEASURE_NAME_SQL_COLUMN_NAME": m.Name, "MEASURE_UNQUALIFIED_CAPTION": m.Name, "MEASUREGROUP_NAME": c.Name,
					"MEASURE_DISPLAY_FOLDER": m.Folder, "DEFAULT_FORMAT_STRING": m.FormatString()}
				if m.Result() == dataapiwire.KindDecimal && m.Width > 0 {
					v["NUMERIC_PRECISION"], v["NUMERIC_SCALE"] = strconv.Itoa(m.Width), strconv.Itoa(m.Scale)
				}
				row(v)
			}
		case RowsetDimensions:
			if r.is("DIMENSION_NAME", xmlamodel.MeasuresName) && r.is("DIMENSION_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) {
				row(map[string]string{"DIMENSION_NAME": xmlamodel.MeasuresName, "DIMENSION_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName),
					"DIMENSION_CAPTION": xmlamodel.MeasuresName, "DIMENSION_ORDINAL": "0", "DIMENSION_TYPE": "2",
					"DIMENSION_CARDINALITY": strconv.Itoa(len(c.Measures)), "DEFAULT_HIERARCHY": xmlamodel.Bracket(xmlamodel.MeasuresName),
					"IS_VIRTUAL": "false", "IS_READWRITE": "false", "DIMENSION_UNIQUE_SETTINGS": "0", "DIMENSION_IS_VISIBLE": "true"})
			}
			for _, d := range c.Dimensions {
				if !r.is("DIMENSION_NAME", d.Name) || !r.is("DIMENSION_UNIQUE_NAME", d.UniqueName) {
					continue
				}
				row(map[string]string{"DIMENSION_NAME": d.Name, "DIMENSION_UNIQUE_NAME": d.UniqueName, "DIMENSION_CAPTION": d.Name,
					"DIMENSION_ORDINAL": strconv.Itoa(d.Ordinal), "DIMENSION_TYPE": dimensionType(d), "DEFAULT_HIERARCHY": d.Default().UniqueName,
					"DESCRIPTION": d.Description, "IS_VIRTUAL": "false", "IS_READWRITE": "false", "DIMENSION_UNIQUE_SETTINGS": "0", "DIMENSION_IS_VISIBLE": "true"})
			}
		case RowsetHierarchies:
			if r.is("DIMENSION_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) && r.is("HIERARCHY_NAME", xmlamodel.MeasuresName) &&
				r.is("HIERARCHY_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) && r.flag("HIERARCHY_ORIGIN", 1) {
				row(map[string]string{"DIMENSION_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName), "HIERARCHY_NAME": xmlamodel.MeasuresName,
					"HIERARCHY_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName), "HIERARCHY_CAPTION": xmlamodel.MeasuresName, "DIMENSION_TYPE": "2",
					"HIERARCHY_CARDINALITY": strconv.Itoa(len(c.Measures)), "DEFAULT_MEMBER": c.Measures[0].UniqueName, "STRUCTURE": "0",
					"IS_VIRTUAL": "false", "IS_READWRITE": "false", "DIMENSION_UNIQUE_SETTINGS": "0", "DIMENSION_IS_VISIBLE": "true",
					"HIERARCHY_ORDINAL": "0", "DIMENSION_IS_SHARED": "true", "HIERARCHY_IS_VISIBLE": "true", "HIERARCHY_ORIGIN": "1", "INSTANCE_SELECTION": "0"})
			}
			for _, d := range c.Dimensions {
				for _, h := range d.Hierarchies {
					origin := hierarchyOrigin(h)
					if !r.is("DIMENSION_UNIQUE_NAME", d.UniqueName) || !r.is("HIERARCHY_NAME", h.Name) || !r.is("HIERARCHY_UNIQUE_NAME", h.UniqueName) ||
						!r.flag("HIERARCHY_ORIGIN", origin) {
						continue
					}
					row(map[string]string{"DIMENSION_UNIQUE_NAME": d.UniqueName, "HIERARCHY_NAME": h.Name, "HIERARCHY_UNIQUE_NAME": h.UniqueName,
						"HIERARCHY_CAPTION": h.Name, "DIMENSION_TYPE": dimensionType(d), "DEFAULT_MEMBER": h.AllName(), "ALL_MEMBER": h.AllName(),
						"DESCRIPTION": d.Description, "STRUCTURE": "0", "IS_VIRTUAL": "false", "IS_READWRITE": "false", "DIMENSION_UNIQUE_SETTINGS": "0",
						"DIMENSION_IS_VISIBLE": "true", "HIERARCHY_ORDINAL": strconv.Itoa(h.Ordinal), "DIMENSION_IS_SHARED": "true",
						"HIERARCHY_IS_VISIBLE": "true", "HIERARCHY_ORIGIN": strconv.Itoa(origin), "INSTANCE_SELECTION": "0"})
				}
			}
		case RowsetLevels:
			if r.is("DIMENSION_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) && r.is("HIERARCHY_UNIQUE_NAME", xmlamodel.Bracket(xmlamodel.MeasuresName)) &&
				r.is("LEVEL_NAME", xmlamodel.MeasuresLevel) && (r.is("LEVEL_UNIQUE_NAME", measuresLevelUnique) || r.is("LEVEL_UNIQUE_NAME", measuresLevelUnique3())) &&
				r.flag("LEVEL_ORIGIN", 1) {
				row(map[string]string{"DIMENSION_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName), "HIERARCHY_UNIQUE_NAME": xmlamodel.Bracket(xmlamodel.MeasuresName),
					"LEVEL_NAME": xmlamodel.MeasuresLevel, "LEVEL_UNIQUE_NAME": measuresLevelUnique, "LEVEL_CAPTION": xmlamodel.MeasuresLevel,
					"LEVEL_NUMBER": "0", "LEVEL_CARDINALITY": strconv.Itoa(len(c.Measures)), "LEVEL_TYPE": "0", "CUSTOM_ROLLUP_SETTING": "0",
					"LEVEL_UNIQUE_SETTINGS": "0", "LEVEL_IS_VISIBLE": "true", "LEVEL_ORIGIN": "1"})
			}
			for _, d := range c.Dimensions {
				for _, h := range d.Hierarchies {
					for _, l := range h.Levels {
						origin := hierarchyOrigin(h)
						if !r.is("DIMENSION_UNIQUE_NAME", d.UniqueName) || !r.is("HIERARCHY_UNIQUE_NAME", h.UniqueName) || !r.is("LEVEL_NAME", l.Name) ||
							!r.is("LEVEL_UNIQUE_NAME", l.UniqueName) || !r.flag("LEVEL_ORIGIN", origin) {
							continue
						}
						row(map[string]string{"DIMENSION_UNIQUE_NAME": d.UniqueName, "HIERARCHY_UNIQUE_NAME": h.UniqueName, "LEVEL_NAME": l.Name,
							"LEVEL_UNIQUE_NAME": l.UniqueName, "LEVEL_CAPTION": l.Name, "LEVEL_NUMBER": strconv.Itoa(l.Number),
							"LEVEL_TYPE": strconv.Itoa(l.Type()), "CUSTOM_ROLLUP_SETTING": "0", "LEVEL_UNIQUE_SETTINGS": "0", "LEVEL_IS_VISIBLE": "true",
							"DESCRIPTION": d.Description, "LEVEL_ORIGIN": strconv.Itoa(origin)})
					}
				}
			}
		case RowsetMGDimensions:
			if !r.is("MEASUREGROUP_NAME", c.Name) {
				continue
			}
			for _, d := range c.Dimensions {
				if !r.is("DIMENSION_UNIQUE_NAME", d.UniqueName) {
					continue
				}
				row(map[string]string{"MEASUREGROUP_NAME": c.Name, "MEASUREGROUP_CARDINALITY": "MANY", "DIMENSION_UNIQUE_NAME": d.UniqueName,
					"DIMENSION_CARDINALITY": "MANY", "DIMENSION_IS_VISIBLE": "true", "DIMENSION_IS_FACT_DIMENSION": "false",
					"DIMENSION_PATH": "<MeasureGroupDimension>" + escape(d.UniqueName) + "</MeasureGroupDimension>"})
			}
		}
	}
}

func measuresLevelUnique3() string {
	return xmlamodel.Bracket(xmlamodel.MeasuresName) + "." + xmlamodel.Bracket(xmlamodel.MeasuresName) + "." + xmlamodel.Bracket(xmlamodel.MeasuresLevel)
}

// dimensionType: 1 for time, 3 for the rest (2 is the measures')
func dimensionType(d *xmlamodel.Dimension) string {
	if d.Time {
		return "1"
	}
	return "3"
}

// hierarchyOrigin: 1 for a hierarchy of several levels (user defined), 2 for the one of a column
// (an attribute): the standard's bits.
func hierarchyOrigin(h *xmlamodel.Hierarchy) int {
	if len(h.Levels) > 2 {
		return 1
	}
	return 2
}

// dateTime writes an RFC 3339 time as XML Schema wants it; "" when it is not one.
func dateTime(rfc string) string {
	t, err := time.Parse(time.RFC3339, rfc)
	if err != nil {
		return ""
	}
	return t.UTC().Format("2006-01-02T15:04:05Z")
}
