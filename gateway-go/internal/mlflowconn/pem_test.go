package mlflowconn

import (
	"encoding/pem"
	"net/http/httptest"
)

// pemOf is the certificate a TLS test server presents, in PEM.
func pemOf(srv *httptest.Server) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
}
