// Package xmla is the XMLA interface of a data API's pod (docs/specs/xmla.md): the SOAP 1.1
// envelope a pivot table speaks, read here into a Request and written back as a rowset or a
// fault. Pure Go (no DuckDB): the pod's route and, later, the model, the MDX and the dataset
// writer sit on it.
package xmla

import (
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"strings"
)

// The namespaces of the protocol.
const (
	NSEnvelope  = "http://schemas.xmlsoap.org/soap/envelope/"
	NSEnvelope2 = "http://www.w3.org/2003/05/soap-envelope" // SOAP 1.2: not spoken
	NSXMLA      = "urn:schemas-microsoft-com:xml-analysis"
	NSRowset    = "urn:schemas-microsoft-com:xml-analysis:rowset"
	NSException = "urn:schemas-microsoft-com:xml-analysis:exception"
)

// The two methods of the protocol.
const (
	MethodDiscover = "Discover"
	MethodExecute  = "Execute"
)

// Request is what a client sent: one Discover or Execute, with its properties.
type Request struct {
	Method string // MethodDiscover or MethodExecute
	// Discover: the rowset asked for, and the restrictions on it (a name may hold several values)
	RequestType  string
	Restrictions map[string][]string
	// Execute: the statement (the MDX), as sent
	Statement string
	// the property list: Catalog, Format, Content, Timeout, …
	Properties map[string]string
	// the session headers a client sends around its queries
	BeginSession bool
	EndSession   bool
	SessionID    string
}

// Property is a property of the request, or "" when it did not send it.
func (r *Request) Property(name string) string { return r.Properties[name] }

// ReadRequest reads a SOAP 1.1 envelope holding a Discover or an Execute. The errors are
// Faults: what the client is told. The input should be capped by the caller.
func ReadRequest(in io.Reader) (*Request, error) {
	dec := xml.NewDecoder(in)
	dec.Strict = true
	req := &Request{Restrictions: map[string][]string{}, Properties: map[string]string{}}
	// the path of open elements, by local name
	var path []string
	sawEnvelope, sawBody := false, false
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, NewFault(CodeBadRequest, "The request is not well-formed XML: "+err.Error())
		}
		start, ok := tok.(xml.StartElement)
		if !ok {
			if _, end := tok.(xml.EndElement); end && len(path) > 0 {
				path = path[:len(path)-1]
			}
			continue
		}
		name := start.Name.Local
		switch {
		case len(path) == 0:
			if name != "Envelope" {
				return nil, NewFault(CodeBadRequest, "Not a SOAP envelope: the root element is «"+name+"»")
			}
			if start.Name.Space == NSEnvelope2 {
				return nil, NewFault(CodeBadRequest, "SOAP 1.2 is not spoken: send a SOAP 1.1 envelope")
			}
			if start.Name.Space != NSEnvelope {
				return nil, NewFault(CodeBadRequest, "Not a SOAP 1.1 envelope")
			}
			sawEnvelope = true
		case len(path) == 1 && name == "Body":
			sawBody = true
		case len(path) == 1 && name == "Header":
		case len(path) == 2 && path[1] == "Header":
			// the session headers: <BeginSession/>, <EndSession SessionId=…/>, <Session SessionId=…/>
			switch name {
			case "BeginSession":
				req.BeginSession = true
			case "EndSession":
				req.EndSession = true
			}
			for _, a := range start.Attr {
				if a.Name.Local == "SessionId" {
					req.SessionID = a.Value
				}
			}
		case len(path) == 2 && path[1] == "Body":
			if start.Name.Space != NSXMLA || (name != MethodDiscover && name != MethodExecute) {
				return nil, NewFault(CodeBadRequest, "The body holds «"+name+"», not a Discover or an Execute of XMLA")
			}
			if req.Method != "" {
				return nil, NewFault(CodeBadRequest, "One Discover or Execute per request")
			}
			req.Method = name
			if err := readMethod(dec, req); err != nil {
				return nil, err
			}
			// readMethod consumed the method's end element
			continue
		}
		path = append(path, name)
	}
	if !sawEnvelope || !sawBody || req.Method == "" {
		return nil, NewFault(CodeBadRequest, "The envelope holds no Discover or Execute")
	}
	if req.Method == MethodDiscover && req.RequestType == "" {
		return nil, NewFault(CodeBadRequest, "A Discover needs its RequestType")
	}
	// an Execute with no statement is what a client sends to open, keep or end a session: the
	// caller decides what to answer
	return req, nil
}

// readMethod reads the children of a Discover or Execute up to its end element.
func readMethod(dec *xml.Decoder, req *Request) error {
	for {
		tok, err := dec.Token()
		if err != nil {
			return NewFault(CodeBadRequest, "The request is not well-formed XML: "+err.Error())
		}
		switch t := tok.(type) {
		case xml.EndElement:
			return nil // the method's own
		case xml.StartElement:
			var err error
			switch t.Name.Local {
			case "RequestType":
				req.RequestType, err = text(dec)
				req.RequestType = strings.TrimSpace(req.RequestType)
			case "Restrictions":
				err = readList(dec, "RestrictionList", func(name, value string) {
					req.Restrictions[name] = append(req.Restrictions[name], value)
				})
			case "Properties":
				err = readList(dec, "PropertyList", func(name, value string) { req.Properties[name] = value })
			case "Command":
				err = readCommand(dec, req)
			default:
				err = dec.Skip()
			}
			if err != nil {
				return asFault(err)
			}
		}
	}
}

// readCommand reads <Command><Statement>…</Statement></Command>.
func readCommand(dec *xml.Decoder, req *Request) error {
	for {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		switch t := tok.(type) {
		case xml.EndElement:
			return nil
		case xml.StartElement:
			if t.Name.Local == "Statement" {
				if req.Statement, err = text(dec); err != nil {
					return err
				}
			} else if err := dec.Skip(); err != nil {
				return err
			}
		}
	}
}

// readList reads <Restrictions><RestrictionList>…</RestrictionList></Restrictions> (or the
// Properties twin): each child element is a name and its text; a child holding <Value>
// elements gives one value per element. The end element of the wrapper closes it.
func readList(dec *xml.Decoder, list string, add func(name, value string)) error {
	for {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		switch t := tok.(type) {
		case xml.EndElement:
			return nil // the wrapper's own
		case xml.StartElement:
			if t.Name.Local != list {
				if err := dec.Skip(); err != nil {
					return err
				}
				continue
			}
			for {
				tok, err := dec.Token()
				if err != nil {
					return err
				}
				if _, end := tok.(xml.EndElement); end {
					break
				}
				child, ok := tok.(xml.StartElement)
				if !ok {
					continue
				}
				values, err := valuesOf(dec)
				if err != nil {
					return err
				}
				for _, v := range values {
					add(child.Name.Local, v)
				}
			}
		}
	}
}

// valuesOf reads an element's content: its text, or the text of each <Value> child. It stops
// after the element's end.
func valuesOf(dec *xml.Decoder) ([]string, error) {
	var buf strings.Builder
	var values []string
	depth := 0
	inValue := false
	var value strings.Builder
	sawValue := false
	for {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		switch t := tok.(type) {
		case xml.CharData:
			if inValue {
				value.Write(t)
			} else if depth == 0 {
				buf.Write(t)
			}
		case xml.StartElement:
			if depth == 0 && t.Name.Local == "Value" {
				inValue, sawValue = true, true
				value.Reset()
			}
			depth++
		case xml.EndElement:
			if depth == 0 {
				if sawValue {
					return values, nil
				}
				return []string{strings.TrimSpace(buf.String())}, nil
			}
			depth--
			if depth == 0 && inValue {
				inValue = false
				values = append(values, strings.TrimSpace(value.String()))
			}
		}
	}
}

// text reads an element's character data up to its end element.
func text(dec *xml.Decoder) (string, error) {
	var b strings.Builder
	depth := 0
	for {
		tok, err := dec.Token()
		if err != nil {
			return "", err
		}
		switch t := tok.(type) {
		case xml.CharData:
			b.Write(t)
		case xml.StartElement:
			depth++
		case xml.EndElement:
			if depth == 0 {
				return b.String(), nil
			}
			depth--
		}
	}
}

func asFault(err error) error {
	var f *Fault
	if errors.As(err, &f) {
		return err
	}
	return NewFault(CodeBadRequest, "The request is not well-formed XML: "+err.Error())
}

// ── answers ───────────────────────────────────────────────────────────────────

// Codes of the faults, written as the error code of the exception element (the server's own;
// a client shows the description).
const (
	CodeBadRequest = 0xC10E0001 // the request is not a request this interface reads
	CodeNotServed  = 0xC10E0002 // a request it reads and does not serve (yet)
	CodeLimit      = 0xC10E0003 // an administrator's cap was reached
	CodeInternal   = 0xC10E0004
)

// Fault is a SOAP fault, and the error a reader or a handler gives for one.
type Fault struct {
	Code    uint32
	Message string
}

// NewFault is a fault with this code and description.
func NewFault(code uint32, message string) *Fault { return &Fault{Code: code, Message: message} }

func (f *Fault) Error() string { return f.Message }

const envelopeHead = `<?xml version="1.0" encoding="utf-8"?>` +
	`<soap:Envelope xmlns:soap="` + NSEnvelope + `" soap:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><soap:Body>`
const envelopeTail = `</soap:Body></soap:Envelope>`

// WriteFault writes the fault as a SOAP 1.1 envelope (send it with HTTP 500).
func WriteFault(w io.Writer, f *Fault) error {
	var b bytes.Buffer
	b.WriteString(envelopeHead)
	b.WriteString(`<soap:Fault><faultcode>XMLAnalysisError.0x` + fmt.Sprintf("%08x", f.Code) + `</faultcode><faultstring>`)
	b.WriteString(escape(f.Message))
	b.WriteString(`</faultstring><faultactor>Server</faultactor><detail><Error xmlns="` + NSException + `" ErrorCode="` +
		fmt.Sprint(f.Code) + `" Description="` + escape(f.Message) + `" Source="Tabularia" HelpFile=""/></detail></soap:Fault>`)
	b.WriteString(envelopeTail)
	_, err := w.Write(b.Bytes())
	return err
}

// escape is XML text and attribute escaping, with the characters XML cannot hold dropped.
func escape(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(strings.Map(func(r rune) rune {
		if r == 0x9 || r == 0xA || r == 0xD || (r >= 0x20 && r <= 0xD7FF) || (r >= 0xE000 && r <= 0xFFFD) || r >= 0x10000 {
			return r
		}
		return -1
	}, s)))
	return b.String()
}
