// Package dbtls is services/db_tls.py on the gateway's side: the encryption choice of a
// database connection (`extra.tls`: verify | trust | none, and `ca_cert` with verify), what the
// engine gets of it, and the port an empty one means. (option_error, the check when a
// connection is saved, belongs to the connections' routes.)
package dbtls

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// DBTypes is DB_TYPES: the connections that are databases.
var DBTypes = []string{"postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle", "sqlserver"}

// tlsPorts is TLS_PORTS: the encrypted protocol's port where it is not the plain one's.
var tlsPorts = map[string]int64{"clickhouse": 8443, "trino": 8443, "oracle": 2484}

func isDB(dbType string) bool {
	for _, t := range DBTypes {
		if t == dbType {
			return true
		}
	}
	return false
}

// data is _data(extra): the options as a dict, {} when they are not one.
func data(extra string) pyjson.Object {
	if extra == "" {
		extra = "{}"
	}
	v, err := pyjson.Loads(extra)
	if err != nil {
		return pyjson.Object{}
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return pyjson.Object{}
	}
	return o
}

func modeOf(dbType string, d pyjson.Object) string {
	if t, ok := d.Value("tls").(string); ok && (t == "verify" || t == "trust" || t == "none") {
		return t
	}
	if dbType == "sqlserver" {
		return "verify"
	}
	return ""
}

// Mode is mode(): verify | trust | none, or "" for a connection saved before the choice
// (SQL Server: verify when unset).
func Mode(dbType, extra string) string { return modeOf(dbType, data(extra)) }

// EngineOptions is engine_options(): the choice (absent = the legacy behaviour) and, with
// verify, the company's authority.
func EngineOptions(dbType, extra string) pyjson.Object {
	if !isDB(dbType) {
		return pyjson.Object{}
	}
	d := data(extra)
	tls := modeOf(dbType, d)
	out := pyjson.Object{}
	if tls != "" {
		out = append(out, pyjson.KV{Key: "tls", Value: tls})
	}
	if ca, ok := d.Value("ca_cert").(string); ok && tls == "verify" && dbType != "sqlserver" && pystr.Strip(ca) != "" {
		out = append(out, pyjson.KV{Key: "ca_cert", Value: pystr.Strip(ca)})
	}
	return out
}

// DefaultPort is default_port(): the encrypted protocol's port when the connection encrypts
// (ClickHouse's and Trino's HTTPS, Oracle's TCPS), otherwise `plain` (nil: None).
func DefaultPort(dbType, extra string, plain *int64) *int64 {
	if m := Mode(dbType, extra); m == "verify" || m == "trust" {
		if p, ok := tlsPorts[dbType]; ok {
			return &p
		}
	}
	return plain
}
