package xmla

// The answer of an Execute: a dataset of the «mddataset» namespace, axes in TupleFormat, written
// as it is read from the tables of the Result.

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/xmlamodel"
)

// NSDataset is the namespace of the multidimensional dataset.
const (
	NSDataset = "urn:schemas-microsoft-com:xml-analysis:mddataset"
	NSEmpty   = "urn:schemas-microsoft-com:xml-analysis:empty"
	nsEngine  = "http://schemas.microsoft.com/analysisservices/2003/engine"
)

const envelopeOpen = `<?xml version="1.0" encoding="utf-8"?>` +
	`<soap:Envelope xmlns:soap="` + NSEnvelope + `" soap:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">`

// the header that answers a session request
func sessionHeader(id string) string {
	if id == "" {
		return ""
	}
	return `<soap:Header><Session xmlns="` + NSXMLA + `" SessionId="` + escape(id) + `"/></soap:Header>`
}

// WriteEmptyExecute answers an Execute that has no statement, which is what a client sends to
// open (with the id of the session it is given), keep or end a session.
func WriteEmptyExecute(w io.Writer, sessionID string) error {
	_, err := io.WriteString(w, envelopeOpen+sessionHeader(sessionID)+`<soap:Body><ExecuteResponse xmlns="`+NSXMLA+`"><return>`+
		`<root xmlns="`+NSEmpty+`"/></return></ExecuteResponse></soap:Body></soap:Envelope>`)
	return err
}

// the schema of the dataset, which the client reads for the shape of what follows
const datasetSchema = `<xsd:schema targetNamespace="` + NSDataset + `" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns="` + NSDataset + `" elementFormDefault="qualified">` +
	`<xsd:complexType name="MemberType"><xsd:sequence><xsd:element name="UName" type="xsd:string"/><xsd:element name="Caption" type="xsd:string"/>` +
	`<xsd:element name="LName" type="xsd:string"/><xsd:element name="LNum" type="xsd:unsignedInt"/><xsd:element name="DisplayInfo" type="xsd:unsignedInt"/>` +
	`<xsd:sequence maxOccurs="unbounded" minOccurs="0"><xsd:any processContents="lax" maxOccurs="unbounded"/></xsd:sequence></xsd:sequence>` +
	`<xsd:attribute name="Hierarchy" type="xsd:string" use="optional"/></xsd:complexType>` +
	`<xsd:complexType name="PropType"><xsd:attribute name="name" type="xsd:string"/></xsd:complexType>` +
	`<xsd:complexType name="TupleType"><xsd:sequence maxOccurs="unbounded"><xsd:element name="Member" type="MemberType"/></xsd:sequence></xsd:complexType>` +
	`<xsd:complexType name="MembersType"><xsd:sequence maxOccurs="unbounded"><xsd:element name="Member" type="MemberType"/></xsd:sequence><xsd:attribute name="Hierarchy" type="xsd:string"/></xsd:complexType>` +
	`<xsd:complexType name="TuplesType"><xsd:sequence maxOccurs="unbounded"><xsd:element name="Tuple" type="TupleType"/></xsd:sequence></xsd:complexType>` +
	`<xsd:complexType name="CrossProductType"><xsd:sequence><xsd:choice minOccurs="0" maxOccurs="unbounded"><xsd:element name="Members" type="MembersType"/><xsd:element name="Tuples" type="TuplesType"/></xsd:choice></xsd:sequence><xsd:attribute name="Size" type="xsd:unsignedInt"/></xsd:complexType>` +
	`<xsd:complexType name="OlapInfo"><xsd:sequence>` +
	`<xsd:element name="CubeInfo"><xsd:complexType><xsd:sequence><xsd:element name="Cube" maxOccurs="unbounded"><xsd:complexType><xsd:sequence><xsd:element name="CubeName" type="xsd:string"/><xsd:any processContents="lax" minOccurs="0" maxOccurs="unbounded"/></xsd:sequence></xsd:complexType></xsd:element></xsd:sequence></xsd:complexType></xsd:element>` +
	`<xsd:element name="AxesInfo"><xsd:complexType><xsd:sequence><xsd:element name="AxisInfo" maxOccurs="unbounded"><xsd:complexType><xsd:sequence><xsd:element name="HierarchyInfo" minOccurs="0" maxOccurs="unbounded"><xsd:complexType><xsd:sequence><xsd:any processContents="lax" maxOccurs="unbounded"/></xsd:sequence><xsd:attribute name="name" type="xsd:string" use="required"/></xsd:complexType></xsd:element></xsd:sequence><xsd:attribute name="name" type="xsd:string"/></xsd:complexType></xsd:element></xsd:sequence></xsd:complexType></xsd:element>` +
	`<xsd:element name="CellInfo"><xsd:complexType><xsd:sequence><xsd:any processContents="lax" minOccurs="0" maxOccurs="unbounded"/></xsd:sequence></xsd:complexType></xsd:element>` +
	`</xsd:sequence></xsd:complexType>` +
	`<xsd:complexType name="Axes"><xsd:sequence maxOccurs="unbounded"><xsd:element name="Axis"><xsd:complexType><xsd:choice minOccurs="0" maxOccurs="unbounded"><xsd:element name="CrossProduct" type="CrossProductType"/><xsd:element name="Tuples" type="TuplesType"/><xsd:element name="Members" type="MembersType"/></xsd:choice><xsd:attribute name="name" type="xsd:string"/></xsd:complexType></xsd:element></xsd:sequence></xsd:complexType>` +
	`<xsd:complexType name="CellData"><xsd:sequence><xsd:element name="Cell" minOccurs="0" maxOccurs="unbounded"><xsd:complexType><xsd:sequence maxOccurs="unbounded"><xsd:any processContents="lax" maxOccurs="unbounded"/></xsd:sequence><xsd:attribute name="CellOrdinal" type="xsd:unsignedInt" use="required"/></xsd:complexType></xsd:element></xsd:sequence></xsd:complexType>` +
	`<xsd:element name="root"><xsd:complexType><xsd:sequence maxOccurs="unbounded"><xsd:element name="OlapInfo" type="OlapInfo"/><xsd:element name="Axes" type="Axes"/><xsd:element name="CellData" type="CellData"/></xsd:sequence></xsd:complexType></xsd:element>` +
	`</xsd:schema>`

// ErrWrite is a write that failed because the client is gone, as opposed to a read of the engine.
var ErrWrite = errors.New("the answer could not be written")

// writer remembers the first error of a long write.
type writer struct {
	w   *bufio.Writer
	err error
}

func (w *writer) puts(s string) {
	if w.err == nil {
		_, w.err = w.w.WriteString(s)
	}
}

func (w *writer) text(s string) { w.puts(escape(s)) }

// the properties of a member that the standard wants in every answer
var intrinsic = map[string]bool{"MEMBER_UNIQUE_NAME": true, "MEMBER_CAPTION": true, "LEVEL_UNIQUE_NAME": true, "LEVEL_NUMBER": true, "DISPLAY_INFO": true}

// the type of the properties that are not text
func propType(name string) string {
	if name == "CHILDREN_CARDINALITY" || name == "PARENT_LEVEL" {
		return "xsd:unsignedInt"
	}
	return "xsd:string"
}

func hierarchyName(pt *part) string {
	if pt.h == nil {
		return xmlamodel.Bracket(xmlamodel.MeasuresName)
	}
	return pt.h.UniqueName
}

// Write writes the answer, reading the axes and the cells from the tables. Nothing it can fail on
// is left to this point but the connection and the engine going away.
func (r *Result) Write(ctx context.Context, out io.Writer, sessionID string) error {
	w := &writer{w: bufio.NewWriterSize(out, 64<<10)}
	w.puts(envelopeOpen + sessionHeader(sessionID) + `<soap:Body><ExecuteResponse xmlns="` + NSXMLA + `"><return>`)
	w.puts(`<root xmlns="` + NSDataset + `" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">`)
	if r.props.content == "SchemaData" || r.props.content == "Schema" {
		w.puts(datasetSchema)
	}
	if r.props.content == "SchemaData" || r.props.content == "Data" {
		r.olapInfo(w)
		w.puts("<Axes>")
		for a, ax := range r.plan.axes {
			if err := r.axis(ctx, w, a, ax); err != nil {
				return err
			}
		}
		r.slicerAxis(w)
		w.puts("</Axes>")
		w.puts("<CellData>")
		if err := r.cellData(ctx, w); err != nil {
			return err
		}
		w.puts("</CellData>")
	}
	w.puts(`</root></return></ExecuteResponse></soap:Body></soap:Envelope>`)
	if w.err == nil {
		w.err = w.w.Flush()
	}
	if w.err != nil {
		return fmt.Errorf("%w: %v", ErrWrite, w.err)
	}
	return nil
}

func (r *Result) cellPropNames() (value, formatted, format bool) {
	if len(r.plan.cellProps) == 0 {
		return true, true, false
	}
	for _, p := range r.plan.cellProps {
		switch p {
		case "VALUE":
			value = true
		case "FORMATTED_VALUE":
			formatted = true
		case "FORMAT_STRING":
			format = true
		}
	}
	return
}

func (r *Result) olapInfo(w *writer) {
	w.puts("<OlapInfo><CubeInfo><Cube><CubeName>")
	w.text(r.plan.cube.Name)
	w.puts("</CubeName>")
	if u := r.plan.cube.Updated; u != "" {
		w.puts(`<LastDataUpdate xmlns="` + nsEngine + `">` + escape(u) + `</LastDataUpdate>`)
	}
	w.puts("</Cube></CubeInfo><AxesInfo>")
	for a, ax := range r.plan.axes {
		w.puts(`<AxisInfo name="Axis` + strconv.Itoa(a) + `">`)
		for _, pt := range ax.parts {
			hierarchyInfo(w, hierarchyName(pt), ax.props)
		}
		w.puts("</AxisInfo>")
	}
	w.puts(`<AxisInfo name="SlicerAxis">`)
	for _, pt := range r.plan.slicerAxis {
		hierarchyInfo(w, hierarchyName(pt), nil)
	}
	hierarchyInfo(w, xmlamodel.Bracket(xmlamodel.MeasuresName), nil)
	w.puts("</AxisInfo></AxesInfo><CellInfo>")
	value, formatted, format := r.cellPropNames()
	if value {
		w.puts(`<Value name="VALUE"/>`)
	}
	if formatted {
		w.puts(`<FormattedValue name="FORMATTED_VALUE"/>`)
	}
	if format {
		w.puts(`<FormatString name="FORMAT_STRING" type="xsd:string"/>`)
	}
	w.puts("</CellInfo></OlapInfo>")
}

func hierarchyInfo(w *writer, name string, props []string) {
	q := func(prop string) string { return escape(name + "." + xmlamodel.Bracket(prop)) }
	w.puts(`<HierarchyInfo name="` + escape(name) + `">`)
	w.puts(`<UName name="` + q("MEMBER_UNIQUE_NAME") + `" type="xsd:string"/><Caption name="` + q("MEMBER_CAPTION") + `" type="xsd:string"/>` +
		`<LName name="` + q("LEVEL_UNIQUE_NAME") + `" type="xsd:string"/><LNum name="` + q("LEVEL_NUMBER") + `" type="xsd:int"/>` +
		`<DisplayInfo name="` + q("DISPLAY_INFO") + `" type="xsd:unsignedInt"/>`)
	for _, p := range props {
		if !intrinsic[p] {
			w.puts(`<` + p + ` name="` + q(p) + `" type="` + propType(p) + `"/>`)
		}
	}
	w.puts("</HierarchyInfo>")
}

// member writes one member of a tuple, with the properties its axis asks for.
func member(w *writer, pt *part, it *item, props []string) {
	var un, caption, lname, lnum string
	display := 0
	if pt.h == nil {
		un, caption = it.measure.UniqueName, it.measure.Name
		lname, lnum = xmlamodel.Bracket(xmlamodel.MeasuresName)+"."+xmlamodel.Bracket(xmlamodel.MeasuresLevel), "0"
	} else {
		un, caption = it.m.UniqueName(), it.m.Caption()
		lname, lnum = it.m.Level.UniqueName, strconv.Itoa(it.m.Level.Number)
		display = min(max(it.children, 0), 0xFFFF)
		if it.drilled {
			display |= 0x10000
		}
		if it.sameParent {
			display |= 0x20000
		}
	}
	w.puts(`<Member Hierarchy="` + escape(hierarchyName(pt)) + `"><UName>`)
	w.text(un)
	w.puts("</UName><Caption>")
	w.text(caption)
	w.puts("</Caption><LName>")
	w.text(lname)
	w.puts("</LName><LNum>" + lnum + "</LNum><DisplayInfo>" + strconv.Itoa(display) + "</DisplayInfo>")
	for _, p := range props {
		if intrinsic[p] {
			continue
		}
		var v string
		have := true
		switch p {
		case "HIERARCHY_UNIQUE_NAME":
			v = hierarchyName(pt)
		case "DIMENSION_UNIQUE_NAME":
			v = xmlamodel.Bracket(xmlamodel.MeasuresName)
			if pt.h != nil {
				v = pt.h.Dimension.UniqueName
			}
		case "CHILDREN_CARDINALITY":
			v = strconv.Itoa(min(max(it.children, 0), math.MaxInt32))
		case "PARENT_UNIQUE_NAME", "PARENT_LEVEL":
			par, ok := xmlamodel.Member{}, false
			if pt.h != nil {
				par, ok = it.m.Parent()
			}
			have = ok
			if ok && p == "PARENT_UNIQUE_NAME" {
				v = par.UniqueName()
			} else if ok {
				v = strconv.Itoa(par.Level.Number)
			}
		case "MEMBER_NAME":
			v = caption
		case "MEMBER_KEY", "MEMBER_VALUE":
			v = caption
			if pt.h != nil {
				v = it.m.Key
				if it.m.Kind == xmlamodel.MemberAll {
					v = xmlamodel.AllMemberName
				}
			}
		}
		if have {
			w.puts("<" + p + ">")
			w.text(v)
			w.puts("</" + p + ">")
		}
	}
	w.puts("</Member>")
}

// decode splits the ordinal of a tuple into the place of each of its parts.
func decode(parts []*part, pre int64, idx []int) {
	for i := len(parts) - 1; i >= 0; i-- {
		n := int64(len(parts[i].items))
		idx[i] = int(pre % n)
		pre /= n
	}
}

func (r *Result) axis(ctx context.Context, w *writer, a int, ax *axisPlan) error {
	w.puts(`<Axis name="Axis` + strconv.Itoa(a) + `"><Tuples>`)
	idx := make([]int, len(ax.parts))
	tuple := func(pre int64) {
		decode(ax.parts, pre, idx)
		w.puts("<Tuple>")
		for i, pt := range ax.parts {
			member(w, pt, &pt.items[idx[i]], ax.props)
		}
		w.puts("</Tuple>")
	}
	if t := r.axisTable[a]; t != "" {
		rows, err := r.run.QueryContext(ctx, "SELECT pre FROM "+t+" ORDER BY o")
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var pre int64
			if err := rows.Scan(&pre); err != nil {
				return err
			}
			tuple(pre)
			if w.err != nil {
				return fmt.Errorf("%w: %v", ErrWrite, w.err)
			}
		}
		if err := rows.Err(); err != nil {
			return err
		}
	} else {
		for pre := int64(0); pre < r.counts[a]; pre++ {
			tuple(pre)
			if w.err != nil {
				return fmt.Errorf("%w: %v", ErrWrite, w.err)
			}
		}
	}
	w.puts("</Tuples></Axis>")
	return nil
}

// slicerAxis writes the members the slicer holds, and the measure the cells are of when no axis
// has the measures.
func (r *Result) slicerAxis(w *writer) {
	pl := r.plan
	parts := append([]*part(nil), pl.slicerAxis...)
	m := pl.cube.Measures[0]
	if pl.measureAxis < 0 && len(pl.measures) > 0 {
		m = pl.measures[0]
	}
	measures := &part{items: []item{{measure: m}}}
	parts = append(parts, measures)
	w.puts(`<Axis name="SlicerAxis"><Tuples>`)
	idx := make([]int, len(parts))
	total := int64(1)
	for _, pt := range parts {
		total *= int64(len(pt.items))
	}
	for pre := int64(0); pre < total; pre++ {
		decode(parts, pre, idx)
		w.puts("<Tuple>")
		for i, pt := range parts {
			member(w, pt, &pt.items[idx[i]], nil)
		}
		w.puts("</Tuple>")
	}
	w.puts("</Tuples></Axis>")
}

func (r *Result) cellData(ctx context.Context, w *writer) error {
	if r.cellTable == "" {
		return nil // an axis with nothing on it
	}
	value, formatted, format := r.cellPropNames()
	rows, err := r.run.QueryContext(ctx, "SELECT ord, mi, v FROM "+r.cellTable+" ORDER BY ord")
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var ord int64
		var mi int
		var v string
		if err := rows.Scan(&ord, &mi, &v); err != nil {
			return err
		}
		m := r.plan.measures[mi]
		typ, text := valueOf(m, v)
		w.puts(`<Cell CellOrdinal="` + strconv.FormatInt(ord, 10) + `">`)
		if value {
			w.puts(`<Value xsi:type="` + typ + `">` + escape(text) + `</Value>`)
		}
		if formatted {
			w.puts("<FormattedValue>" + escape(formatNumber(m, text)) + "</FormattedValue>")
		}
		if format {
			w.puts("<FormatString>" + escape(m.FormatString()) + "</FormatString>")
		}
		w.puts("</Cell>")
		if w.err != nil {
			return fmt.Errorf("%w: %v", ErrWrite, w.err)
		}
	}
	return rows.Err()
}

// valueOf is the XML Schema type and the text of a cell's value, from the text the engine gave.
func valueOf(m *xmlamodel.Measure, text string) (string, string) {
	switch m.Result() {
	case dataapiwire.KindInt:
		n, err := strconv.ParseInt(text, 10, 64)
		switch {
		case err != nil:
			return "xsd:decimal", text // wider than 64 bits
		case n >= math.MinInt32 && n <= math.MaxInt32:
			return "xsd:int", text
		}
		return "xsd:long", text
	case dataapiwire.KindDecimal:
		return "xsd:decimal", text
	}
	switch strings.ToLower(text) {
	case "nan":
		text = "NaN"
	case "inf":
		text = "INF"
	case "-inf":
		text = "-INF"
	}
	return "xsd:double", text
}

// formatNumber writes a value as the format string of its measure says: thousands separators and
// the decimals of the format.
func formatNumber(m *xmlamodel.Measure, text string) string {
	if m.Result() == dataapiwire.KindFloat {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return text
		}
		text = strconv.FormatFloat(f, 'f', 2, 64)
	}
	sign := ""
	if strings.HasPrefix(text, "-") {
		sign, text = "-", text[1:]
	}
	whole, frac, _ := strings.Cut(text, ".")
	var b strings.Builder
	for i, c := range whole {
		if i > 0 && (len(whole)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(c)
	}
	out := sign + b.String()
	if frac != "" {
		out += "." + frac
	}
	return out
}
