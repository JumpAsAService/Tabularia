package pod

// POST /data/{api}/xmla: the XMLA interface (docs/specs/xmla.md), a second way a data API with
// it switched on answers, for pivot tables. The route, its admission and the envelope are piece
// 1, the Discover rowsets piece 2 (xmla_discover.go); Execute is answered with a fault that says
// it is not served yet.

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/dataapi/internal/xmla"
)

// the most a request's envelope may weigh: an MDX statement of a pivot is a few KB
const maxXMLARequest = 1 << 20

// isXMLAPath says whether the path is /data/{api}/xmla.
func isXMLAPath(path string) bool {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	return len(parts) == 3 && parts[0] == "data" && parts[2] == "xmla"
}

func (s *Server) xmla(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		s.fail(w, r, &Problem{Status: 405, Code: "method_not_allowed", Detail: "XMLA is SOAP over POST"})
		return
	}
	snap, key, ok := s.admit(w, r)
	if !ok {
		return
	}
	if snap.m.XMLA == nil {
		s.fail(w, r, &Problem{Status: 404, Code: "xmla_off", Detail: "This data API does not answer XMLA"})
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxXMLARequest))
	if err != nil {
		s.fail(w, r, &Problem{Status: 413, Code: "request_too_large", Detail: "An XMLA request is at most 1 MB"})
		return
	}
	req, err := xmla.ReadRequest(bytes.NewReader(raw))
	if err != nil {
		s.fault(w, r, err)
		return
	}
	if req.Method == xmla.MethodDiscover {
		s.discover(w, r, snap, key, req)
		return
	}
	recordOf(r).code = "xmla_not_served"
	s.fault(w, r, xmla.NewFault(xmla.CodeNotServed, "Execute is not served yet by this interface"))
}

// fault answers a SOAP fault with HTTP 500, as SOAP 1.1 says.
func (s *Server) fault(w http.ResponseWriter, r *http.Request, err error) {
	var f *xmla.Fault
	if !errors.As(err, &f) {
		f = xmla.NewFault(xmla.CodeInternal, fmt.Sprint(err))
	}
	if recordOf(r).code == "" {
		recordOf(r).code = "xmla_fault"
	}
	var out bytes.Buffer
	_ = xmla.WriteFault(&out, f)
	writeXML(w, http.StatusInternalServerError, out.Bytes())
}

func writeXML(w http.ResponseWriter, status int, body []byte) {
	w.Header().Set("Content-Type", "text/xml; charset=utf-8")
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
