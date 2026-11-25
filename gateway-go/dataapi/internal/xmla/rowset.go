package xmla

import (
	"bufio"
	"io"
	"strings"
)

// Column is a column of a rowset: its name, its XML Schema type (xsd:string, xsd:int,
// xsd:boolean, xsd:unsignedInt …) and whether a row may leave it out.
type Column struct {
	Name     string
	Type     string
	Nullable bool
	// an array column: Inner is the XML Schema of what one element holds, and the row's value is
	// XML already (escaped by whoever wrote it), written inside the column's element; with Repeat
	// the value is the elements themselves, as many as the array has (DISCOVER_SCHEMA_ROWSETS'
	// Restrictions)
	Inner  string
	Repeat bool
}

// Str, Int, Bool and UInt are the columns' types the rowsets of Discover use.
const (
	Str  = "xsd:string"
	Int  = "xsd:int"
	UInt = "xsd:unsignedInt"
	Bool = "xsd:boolean"
	// the types of the schema rowsets' numbers and dates
	UShort   = "xsd:unsignedShort"
	Short    = "xsd:short"
	ULong    = "xsd:unsignedLong"
	DateTime = "xsd:dateTime"
)

// RowsetWriter writes a Discover answer a row at a time, so that a large one (the members of a
// level) is never held whole.
type RowsetWriter struct {
	w      *bufio.Writer
	cols   []Column
	rows   int
	closed bool
	err    error
}

// NewRowsetWriter starts the envelope and the rowset's schema; Row adds a row and Close ends
// the answer (the envelope is not valid without it).
func NewRowsetWriter(w io.Writer, cols []Column) *RowsetWriter {
	rw := &RowsetWriter{w: bufio.NewWriter(w), cols: cols}
	rw.puts(envelopeHead + `<DiscoverResponse xmlns="` + NSXMLA + `"><return>` +
		`<root xmlns="` + NSRowset + `" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:EX="` + NSException + `">`)
	rw.puts(schema(cols))
	return rw
}

func (rw *RowsetWriter) puts(s string) {
	if rw.err == nil {
		_, rw.err = rw.w.WriteString(s)
	}
}

// Row writes a row: one value per column, in order. A nullable column with the empty value is
// left out.
func (rw *RowsetWriter) Row(values ...string) {
	rw.puts("<row>")
	for i, c := range rw.cols {
		v := ""
		if i < len(values) {
			v = values[i]
		}
		if v == "" && c.Nullable {
			continue
		}
		switch {
		case c.Inner != "" && c.Repeat:
			rw.puts(v)
		case c.Inner != "":
			rw.puts("<" + c.Name + ">" + v + "</" + c.Name + ">")
		default:
			rw.puts("<" + c.Name + ">" + escape(v) + "</" + c.Name + ">")
		}
	}
	rw.puts("</row>")
	rw.rows++
}

// Rows is how many rows were written.
func (rw *RowsetWriter) Rows() int { return rw.rows }

// Close ends the answer and flushes it.
func (rw *RowsetWriter) Close() error {
	if !rw.closed {
		rw.closed = true
		rw.puts(`</root></return></DiscoverResponse>` + envelopeTail)
		if rw.err == nil {
			rw.err = rw.w.Flush()
		}
	}
	return rw.err
}

// WriteRowset writes a whole rowset held in memory (the small ones).
func WriteRowset(w io.Writer, cols []Column, rows [][]string) error {
	rw := NewRowsetWriter(w, cols)
	for _, r := range rows {
		rw.Row(r...)
	}
	return rw.Close()
}

// schema is the XML Schema every rowset carries, which a client reads for the columns' types.
func schema(cols []Column) string {
	var b strings.Builder
	b.WriteString(`<xsd:schema targetNamespace="` + NSRowset + `" xmlns:sql="urn:schemas-microsoft-com:xml-sql" elementFormDefault="qualified">` +
		`<xsd:element name="root"><xsd:complexType><xsd:sequence minOccurs="0" maxOccurs="unbounded"><xsd:element name="row" type="row"/></xsd:sequence></xsd:complexType></xsd:element>` +
		`<xsd:simpleType name="uuid"><xsd:restriction base="xsd:string"><xsd:pattern value="[0-9a-zA-Z]{8}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{4}-[0-9a-zA-Z]{12}"/></xsd:restriction></xsd:simpleType>` +
		`<xsd:complexType name="xmlDocument"><xsd:sequence><xsd:any/></xsd:sequence></xsd:complexType>` +
		`<xsd:complexType name="row"><xsd:sequence>`)
	for _, c := range cols {
		b.WriteString(`<xsd:element sql:field="` + c.Name + `" name="` + c.Name + `"`)
		if c.Inner == "" {
			b.WriteString(` type="` + c.Type + `"`)
		}
		if c.Nullable || c.Inner != "" {
			b.WriteString(` minOccurs="0"`)
		}
		if c.Inner != "" && c.Repeat {
			b.WriteString(` maxOccurs="unbounded"`)
		}
		if c.Inner != "" {
			b.WriteString(`>` + c.Inner + `</xsd:element>`)
		} else {
			b.WriteString(`/>`)
		}
	}
	b.WriteString(`</xsd:sequence></xsd:complexType></xsd:schema>`)
	return b.String()
}
