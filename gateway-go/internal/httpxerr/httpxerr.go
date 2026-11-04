// Package httpxerr names a Go HTTP client's failure as the httpx exception the Python code
// would have caught, with its str() where it is known (measured in the gateway's image): a
// connection refused or a name not resolved (ConnectError «All connection attempts failed»),
// a timeout, a server that closes without answering, TLS spoken to a plain-HTTP server, a
// certificate not trusted. Other texts come from OpenSSL or the OS in Python and cannot be the
// same bytes here.
package httpxerr

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"os"
	"strings"
)

// Of is (type(e).__name__, str(e)) of the httpx exception for err.
func Of(err error) (string, string) {
	var cert *tls.CertificateVerificationError
	var hostErr x509.HostnameError
	var unknown x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	var record tls.RecordHeaderError
	var op *net.OpError
	var dns *net.DNSError
	var ne net.Error
	text := err.Error()
	switch {
	case strings.Contains(text, "unsupported protocol scheme"), strings.Contains(text, "no Host in request URL"):
		return "UnsupportedProtocol", "Request URL is missing an 'http://' or 'https://' protocol."
	case strings.Contains(text, "stopped after") && strings.Contains(text, "redirects"),
		strings.Contains(text, "Exceeded maximum allowed redirects"):
		return "TooManyRedirects", "Exceeded maximum allowed redirects."
	case errors.As(err, &record), strings.Contains(text, "server gave HTTP response to HTTPS client"):
		return "ConnectError", "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1010)"
	case errors.As(err, &cert), errors.As(err, &hostErr), errors.As(err, &unknown), errors.As(err, &invalid):
		return "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: " + err.Error()
	case errors.As(err, &dns):
		return "ConnectError", "[Errno -2] Name or service not known"
	case errors.As(err, &op) && op.Op == "dial":
		if op.Timeout() {
			return "ConnectTimeout", ""
		}
		return "ConnectError", "All connection attempts failed"
	case errors.As(err, &ne) && ne.Timeout():
		if errors.Is(err, os.ErrDeadlineExceeded) && strings.Contains(text, "TLS handshake") {
			return "ConnectTimeout", "_ssl.c:983: The handshake operation timed out"
		}
		return "ReadTimeout", ""
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return "RemoteProtocolError", "Server disconnected without sending a response."
	}
	return "ConnectError", text
}
