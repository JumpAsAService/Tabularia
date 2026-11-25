package xmla

import (
	"bytes"
	"encoding/xml"
	"errors"
	"slices"
	"strings"
	"testing"
)

// Envelopes as a pivot table sends them (written from the protocol's documentation: the real
// ones join the fixtures from the first trials).

const discoverCubes = `<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Header>
    <Session soapenv:mustUnderstand="1" SessionId="A1B2" xmlns="urn:schemas-microsoft-com:xml-analysis" xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"/>
  </soap:Header>
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_CUBES</RequestType>
      <Restrictions>
        <RestrictionList>
          <CATALOG_NAME>CRM</CATALOG_NAME>
          <CUBE_NAME>customers</CUBE_NAME>
          <CUBE_NAME><Value>orders</Value><Value>items</Value></CUBE_NAME>
        </RestrictionList>
      </Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>CRM</Catalog>
          <Content>SchemaData</Content>
          <Format>Tabular</Format>
          <LocaleIdentifier>1033</LocaleIdentifier>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>`

const executeMDX = `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Header><BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis" soapenv:mustUnderstand="1" xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"/></soap:Header>
<soap:Body><Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
<Command><Statement>SELECT NON EMPTY { [Measures].[Sum of score] } ON COLUMNS,
 NON EMPTY { [customers].[region].[region].AllMembers } ON ROWS FROM [customers] WHERE ([customers].[active].&amp;[true])</Statement></Command>
<Properties><PropertyList><Catalog>CRM</Catalog><Format>Multidimensional</Format><AxisFormat>TupleFormat</AxisFormat><Content>SchemaData</Content></PropertyList></Properties>
</Execute></soap:Body></soap:Envelope>`

func TestReadsADiscover(t *testing.T) {
	req, err := ReadRequest(strings.NewReader(discoverCubes))
	if err != nil {
		t.Fatal(err)
	}
	if req.Method != MethodDiscover || req.RequestType != "MDSCHEMA_CUBES" {
		t.Fatalf("%+v", req)
	}
	if got := req.Restrictions["CATALOG_NAME"]; !slices.Equal(got, []string{"CRM"}) {
		t.Errorf("catalog: %v", got)
	}
	if got := req.Restrictions["CUBE_NAME"]; !slices.Equal(got, []string{"customers", "orders", "items"}) {
		t.Errorf("cubes: %v", got)
	}
	want := map[string]string{"Catalog": "CRM", "Content": "SchemaData", "Format": "Tabular", "LocaleIdentifier": "1033"}
	for k, v := range want {
		if req.Property(k) != v {
			t.Errorf("%s: %q", k, req.Property(k))
		}
	}
	if req.Property("Timeout") != "" || req.SessionID != "A1B2" || req.BeginSession || req.EndSession {
		t.Errorf("%+v", req)
	}
}

func TestReadsAnExecute(t *testing.T) {
	req, err := ReadRequest(strings.NewReader(executeMDX))
	if err != nil {
		t.Fatal(err)
	}
	if req.Method != MethodExecute || !req.BeginSession {
		t.Fatalf("%+v", req)
	}
	if !strings.HasPrefix(req.Statement, "SELECT NON EMPTY") || !strings.Contains(req.Statement, "&[true])") ||
		!strings.Contains(req.Statement, "\n") {
		t.Errorf("the statement as it was sent: %q", req.Statement)
	}
	if req.Property("Format") != "Multidimensional" || req.Property("AxisFormat") != "TupleFormat" {
		t.Errorf("%v", req.Properties)
	}
}

func TestReadsTheSessionStatements(t *testing.T) {
	const end = `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Header>
<EndSession xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="S9"/></soap:Header><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement/></Command><Properties/></Execute></soap:Body></soap:Envelope>`
	req, err := ReadRequest(strings.NewReader(end))
	if err != nil {
		t.Fatal(err)
	}
	if req.Method != MethodExecute || !req.EndSession || req.SessionID != "S9" || strings.TrimSpace(req.Statement) != "" {
		t.Errorf("%+v", req)
	}
	// an Execute with no command at all, as a client probing its session sends
	const bare = `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"/></soap:Body></soap:Envelope>`
	if req, err := ReadRequest(strings.NewReader(bare)); err != nil || req.Method != MethodExecute || req.Statement != "" {
		t.Errorf("%+v %v", req, err)
	}
}

func TestRefusesWhatIsNotAnXMLARequest(t *testing.T) {
	soap := func(body string) string {
		return `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>` + body + `</soap:Body></soap:Envelope>`
	}
	for name, c := range map[string]struct{ in, why string }{
		"empty":           {"", "no Discover or Execute"},
		"not xml":         {"SELECT 1", "no Discover or Execute"},
		"malformed":       {"<not xml", "not well-formed"},
		"truncated":       {soap(`<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>X`), "not well-formed"},
		"not an envelope": {`<html/>`, "Not a SOAP envelope"},
		"soap 1.2":        {`<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"><e:Body/></e:Envelope>`, "SOAP 1.2"},
		"no namespace":    {`<Envelope><Body/></Envelope>`, "Not a SOAP 1.1"},
		"empty body":      {soap(""), "no Discover or Execute"},
		"another body":    {soap(`<GetThing xmlns="urn:x"/>`), "not a Discover or an Execute"},
		"wrong xmla ns":   {soap(`<Discover xmlns="urn:other"><RequestType>X</RequestType></Discover>`), "not a Discover or an Execute"},
		"no type":         {soap(`<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"/>`), "needs its RequestType"},
		"two methods": {soap(`<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>A</RequestType></Discover>` +
			`<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"/>`), "One Discover or Execute"},
	} {
		_, err := ReadRequest(strings.NewReader(c.in))
		var f *Fault
		if !errors.As(err, &f) || f.Code != CodeBadRequest || !strings.Contains(f.Message, c.why) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestWritesAFault(t *testing.T) {
	var b bytes.Buffer
	if err := WriteFault(&b, NewFault(CodeNotServed, `Discover of "X" <is> not served & more`)); err != nil {
		t.Fatal(err)
	}
	out := b.String()
	for _, want := range []string{`<soap:Fault>`, `<faultcode>XMLAnalysisError.0xc10e0002</faultcode>`,
		`Discover of &#34;X&#34; &lt;is&gt; not served &amp; more`, `ErrorCode="3238920194"`, `Source="Tabularia"`} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %s in %s", want, out)
		}
	}
	wellFormed(t, out)
	// a character XML cannot hold is dropped, not written
	b.Reset()
	_ = WriteFault(&b, NewFault(CodeInternal, "bad \x00 \x01 byte"))
	wellFormed(t, b.String())
	if strings.Contains(b.String(), "\x00") {
		t.Error("a NUL in the answer")
	}
}

func TestWritesARowset(t *testing.T) {
	cols := []Column{{Name: "Name", Type: Str}, {Name: "Count", Type: Int}, {Name: "Note", Type: Str, Nullable: true},
		{Name: "On", Type: Bool}}
	var b bytes.Buffer
	if err := WriteRowset(&b, cols, [][]string{{"a<b", "3", "", "true"}, {"c&d", "4", "kept", "false"}}); err != nil {
		t.Fatal(err)
	}
	out := b.String()
	wellFormed(t, out)
	for _, want := range []string{
		`<DiscoverResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return><root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset"`,
		`<xsd:element sql:field="Name" name="Name" type="xsd:string"/>`,
		`<xsd:element sql:field="Note" name="Note" type="xsd:string" minOccurs="0"/>`,
		`<xsd:element sql:field="Count" name="Count" type="xsd:int"/>`,
		`<row><Name>a&lt;b</Name><Count>3</Count><On>true</On></row>`, // the nullable, empty Note is left out
		`<row><Name>c&amp;d</Name><Count>4</Count><Note>kept</Note><On>false</On></row>`,
		`</root></return></DiscoverResponse></soap:Body></soap:Envelope>`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %s in %s", want, out)
		}
	}
	// no rows is a rowset all the same
	b.Reset()
	if err := WriteRowset(&b, cols, nil); err != nil {
		t.Fatal(err)
	}
	wellFormed(t, b.String())
	if strings.Contains(b.String(), "<row>") {
		t.Error("a row")
	}
}

func TestARowsetIsWrittenARowAtATime(t *testing.T) {
	var b bytes.Buffer
	rw := NewRowsetWriter(&b, PropertiesColumns)
	for i := 0; i < 3; i++ {
		rw.Row("P", "", "string", "Read", "false", "v")
	}
	if rw.Rows() != 3 {
		t.Error(rw.Rows())
	}
	if err := rw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := rw.Close(); err != nil { // closing twice writes nothing more
		t.Fatal(err)
	}
	wellFormed(t, b.String())
	if strings.Count(b.String(), "<row>") != 3 || strings.Count(b.String(), "</soap:Envelope>") != 1 {
		t.Error(b.String())
	}
}

func TestPropertiesRowset(t *testing.T) {
	all := PropertiesRowset("Sales", nil)
	names := map[string][]string{}
	for _, r := range all {
		if len(r) != len(PropertiesColumns) {
			t.Fatalf("a row of %d for %d columns: %v", len(r), len(PropertiesColumns), r)
		}
		names[r[0]] = r
	}
	for _, n := range []string{"ProviderName", "ProviderVersion", "Catalog", "MdpropMdxSubqueries", "DbpropMsmdSubqueries"} {
		if names[n] == nil {
			t.Errorf("no %s", n)
		}
	}
	if names["Catalog"][5] != "Sales" || names["ProviderName"][5] != ProviderName || names["MdpropMdxSubqueries"][5] != "15" {
		t.Errorf("%v", names)
	}
	if len(names) != len(all) {
		t.Error("a property twice")
	}
	one := PropertiesRowset("Sales", map[string][]string{"PropertyName": {"Catalog", "Nope"}})
	if len(one) != 1 || one[0][0] != "Catalog" {
		t.Errorf("%v", one)
	}
	if got := PropertiesRowset("Sales", map[string][]string{"PropertyName": {"Nope"}}); len(got) != 0 {
		t.Errorf("%v", got)
	}
}

// wellFormed checks that every token of the document reads.
func wellFormed(t *testing.T, doc string) {
	t.Helper()
	dec := xml.NewDecoder(strings.NewReader(doc))
	for {
		if _, err := dec.Token(); err != nil {
			if err.Error() != "EOF" {
				t.Fatalf("not well-formed: %v\n%s", err, doc)
			}
			return
		}
	}
}
