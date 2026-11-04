package dbtls

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const ca = `-----BEGIN CERTIFICATE-----
MIIBjTCCATOgAwIBAgIUJ4bHekTAr7OaWUOLAbekNwaTu7swCgYIKoZIzj0EAwIw
HDEaMBgGA1UEAwwRZGlmZmNoZWNrIHRlc3QgQ0EwHhcNMjYxMTA0MDY1MzU4WhcN
MzYxMTAxMDY1MzU4WjAcMRowGAYDVQQDDBFkaWZmY2hlY2sgdGVzdCBDQTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABMkatwNz/fe6P/XLlohz0OUkuBi7wVjHeZ8x
W9WJjcSq7Rlz+m+0voMjDAY/cXZRr9IRzpPCSy4YmTDimw4ZZuqjUzBRMB0GA1Ud
DgQWBBSdvLoERmVWZfYchfDTRDv+DcPNgDAfBgNVHSMEGDAWgBSdvLoERmVWZfYc
hfDTRDv+DcPNgDAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0gAMEUCIQCT
DlqN6zDokEBhJRgqx/ll0gl39x4YCUOvBV89lUIlRgIgE8QvfFDi3K0RxDTvkbNP
cQw+GvnTI1ZBvkxOUokwB7s=
-----END CERTIFICATE-----`

func TestOptionError(t *testing.T) {
	for _, c := range []struct {
		dbType string
		data   pyjson.Object
		want   string
	}{
		{"smtp", pyjson.Object{{Key: "tls", Value: "x"}}, ""},
		{"postgresql", pyjson.Object{{Key: "tls", Value: "x"}}, "The TLS option must be verify, trust or none"},
		{"postgresql", pyjson.Object{{Key: "tls", Value: false}}, "The TLS option must be verify, trust or none"},
		{"postgresql", pyjson.Object{{Key: "tls", Value: ""}, {Key: "ca_cert", Value: ""}}, ""},
		{"postgresql", pyjson.Object{{Key: "ca_cert", Value: 5}}, "The certificate authority must be text (PEM)"},
		{"sqlserver", pyjson.Object{{Key: "ca_cert", Value: "x"}}, "SQL Server takes no certificate authority here: its driver reads the system's store, so add the authority to the backend image (docs/sqlserver.md)"},
		{"mysql", pyjson.Object{{Key: "tls", Value: "trust"}, {Key: "ca_cert", Value: "x"}}, "The certificate authority is used only to verify: choose verify, or leave it empty"},
		{"mysql", pyjson.Object{{Key: "tls", Value: "verify"}, {Key: "ca_cert", Value: "-----BEGIN RSA PRIVATE KEY-----"}}, "That is a private key: paste only the authority's certificate (-----BEGIN CERTIFICATE-----)"},
		{"mysql", pyjson.Object{{Key: "tls", Value: "verify"}, {Key: "ca_cert", Value: "hello"}}, caUnreadable},
		{"mysql", pyjson.Object{{Key: "tls", Value: "verify"}, {Key: "ca_cert", Value: "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"}}, caUnreadable},
		{"mysql", pyjson.Object{{Key: "tls", Value: "verify"}, {Key: "ca_cert", Value: "text around\n" + ca + "\nmore"}}, ""},
		{"mysql", pyjson.Object{{Key: "tls", Value: "verify"}, {Key: "ca_cert", Value: ca + "\n-----BEGIN CERTIFICATE-----\n!!\n-----END CERTIFICATE-----"}}, caUnreadable},
	} {
		if got := OptionError(c.dbType, c.data); got != c.want {
			t.Errorf("%s %v: %q", c.dbType, c.data, got)
		}
	}
}
