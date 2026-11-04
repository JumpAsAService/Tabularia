package dbtls

import (
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// caMaxBytes is CA_MAX_BYTES.
const caMaxBytes = 64 * 1024

const caUnreadable = "The certificate authority cannot be read: paste it in PEM (-----BEGIN CERTIFICATE-----)"

// OptionError is option_error: what is wrong with the encryption options of a database
// connection, "" when nothing is (or the connection is no database).
func OptionError(dbType string, data pyjson.Object) string {
	if !isDB(dbType) {
		return ""
	}
	tls := data.Value("tls")
	if tls != nil {
		if s, ok := tls.(string); !ok || (s != "" && s != "verify" && s != "trust" && s != "none") {
			return "The TLS option must be verify, trust or none"
		}
	}
	ca := data.Value("ca_cert")
	if ca == nil || ca == "" {
		return ""
	}
	text, ok := ca.(string)
	if !ok {
		return "The certificate authority must be text (PEM)"
	}
	if dbType == "sqlserver" {
		return "SQL Server takes no certificate authority here: its driver reads the system's store, " +
			"so add the authority to the backend image (docs/sqlserver.md)"
	}
	if tls != "verify" {
		return "The certificate authority is used only to verify: choose verify, or leave it empty"
	}
	if len(text) > caMaxBytes {
		return fmt.Sprintf("The certificate authority is too long (over %d KiB)", caMaxBytes/1024)
	}
	if strings.Contains(text, "PRIVATE KEY") {
		return "That is a private key: paste only the authority's certificate (-----BEGIN CERTIFICATE-----)"
	}
	if !strings.Contains(text, "-----BEGIN CERTIFICATE-----") || !readable(text) {
		return caUnreadable
	}
	return ""
}

// readable is ssl.create_default_context(cadata=ca) not raising: OpenSSL reads every PEM
// certificate in the text (what is around them is skipped); one that does not parse, or none
// at all, is an error.
func readable(text string) bool {
	rest, found := []byte(text), 0
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			break
		}
		switch block.Type {
		case "CERTIFICATE", "X509 CERTIFICATE", "TRUSTED CERTIFICATE":
			if _, err := x509.ParseCertificate(block.Bytes); err != nil {
				return false
			}
			found++
		}
	}
	// a block Go skips (its base64 broken) is one OpenSSL refuses
	return found > 0 && found >= strings.Count(text, "-----BEGIN CERTIFICATE-----")
}
