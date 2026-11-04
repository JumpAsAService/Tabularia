// Package connpayload is what routes/connections.py hands the engine for a connection: the
// `connection` payload of every kind (engine_connection_payload, the secret still encrypted),
// and the tolerant readings of a connection's options (`extra`) that the runs use too (the
// SMTP options and their allowed domains, the SFTP method, the API options). The routes of the
// connections, of the datasources and of the runs (and the background work) build on it.
package connpayload

import (
	"context"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtls"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// NotDatabases is NOT_DATABASES: the connection types with no table to read, none to write.
var NotDatabases = map[string]bool{"smtp": true, "sharepoint": true, "sftp": true, "api": true, "gsheets": true}

// Conn is a `connections` row.
type Conn struct {
	ID                  int64
	Name                string
	Description         string
	ProjectID           int64
	OwnerID             *int64
	DBType              string
	Host                string
	Port                *int64
	Username            string
	PasswordEncrypted   string
	Database            string
	DBSchema            string
	Extra               string
	PrivateKeyEncrypted string
	HostKeyType         string
	HostKeyFingerprint  string
}

// Columns are the columns Scan reads, in its order.
const Columns = `id, name, description, project_id, owner_id, db_type, host, port, username, password_encrypted, database,
	db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint`

// Scan reads a row of Columns.
func Scan(row pgx.Row) (*Conn, error) {
	var c Conn
	err := row.Scan(&c.ID, &c.Name, &c.Description, &c.ProjectID, &c.OwnerID, &c.DBType, &c.Host, &c.Port, &c.Username,
		&c.PasswordEncrypted, &c.Database, &c.DBSchema, &c.Extra, &c.PrivateKeyEncrypted, &c.HostKeyType, &c.HostKeyFingerprint)
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// Querier is what Load reads with (a pool or a transaction).
type Querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Load is session.get(Connection, id): nil when there is none.
func Load(ctx context.Context, q Querier, id int64) (*Conn, error) {
	c, err := Scan(q.QueryRow(ctx, `SELECT `+Columns+` FROM connections WHERE id = $1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return c, err
}

// options is json.loads(extra or "{}") as a dict, {} when it is broken or not a dict (the
// tolerant readings: smtp_options, api_options, sftp_auth, _sharepoint_payload).
func options(extra string) pyjson.Object {
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

// SMTPOptions is smtp_options.
func SMTPOptions(extra string) pyjson.Object { return options(extra) }

// APIOptions is api_options.
func APIOptions(extra string) pyjson.Object { return options(extra) }

// DomainList is _domain_list: the allowed domains as a list; ok false when the value is not
// a list of text nor a comma-separated text.
func DomainList(raw any) ([]string, bool) {
	var items []any
	switch x := raw.(type) {
	case nil:
		return []string{}, true
	case string:
		if x == "" {
			return []string{}, true
		}
		for _, p := range strings.Split(x, ",") {
			items = append(items, p)
		}
	case []any:
		items = x
	default:
		return nil, false
	}
	out := []string{}
	for _, it := range items {
		s, ok := it.(string)
		if !ok {
			return nil, false
		}
		if pystr.Strip(s) != "" {
			out = append(out, strings.TrimLeft(pystr.Lower(pystr.Strip(s)), "@"))
		}
	}
	return out, true
}

// AllowedEmailDomains is allowed_email_domains: the domains an SMTP connection may send to
// (empty = no limit); ok false when the restriction is malformed (no email leaves: the caller
// refuses with MalformedDomains).
func AllowedEmailDomains(c *Conn) ([]string, bool) {
	return DomainList(SMTPOptions(c.Extra).Value("allowed_domains"))
}

// MalformedDomains is the 422 detail of allowed_email_domains.
func MalformedDomains(c *Conn) string {
	return "The allowed domains of the SMTP connection '" + c.Name + "' are malformed: fix the connection"
}

// SFTPAuth is sftp_auth: password (the default) or key.
func SFTPAuth(extra string) string {
	if a, ok := options(extra).Value("auth").(string); ok && (a == "password" || a == "key") {
		return a
	}
	return "password"
}

// APIAuth is API_AUTH.
var APIAuth = []string{"none", "api_key", "bearer", "basic", "oauth2"}

func in(v any, options ...string) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	for _, o := range options {
		if s == o {
			return true
		}
	}
	return false
}

// SMTP is _smtp_payload.
func SMTP(host string, port *int64, username, secret string, opts pyjson.Object) pyjson.Object {
	var p any = int64(587)
	if port != nil && *port != 0 {
		p = *port
	}
	return pyjson.Object{{Key: "db_type", Value: "smtp"}, {Key: "host", Value: host}, {Key: "port", Value: p},
		{Key: "username", Value: username}, {Key: "password_encrypted", Value: secret},
		{Key: "from_address", Value: pyjson.Or(opts.Value("from_address"), "")},
		{Key: "from_name", Value: pyjson.Or(opts.Value("from_name"), "")},
		{Key: "tls", Value: pyjson.Or(opts.Value("tls"), "starttls")}}
}

// SharePoint is _sharepoint_payload: the Microsoft endpoints are never taken from here.
func SharePoint(host, username, secret, database, extra string) pyjson.Object {
	library := ""
	if v := options(extra).Value("library"); pyjson.Truthy(v) {
		library = pystr.Strip(pyjson.Str(v))
	}
	return pyjson.Object{{Key: "tenant_id", Value: pystr.Strip(database)}, {Key: "client_id", Value: pystr.Strip(username)},
		{Key: "client_secret_encrypted", Value: secret}, {Key: "site_url", Value: pystr.Strip(host)}, {Key: "library", Value: library}}
}

// GSheets is _gsheets_payload: the key still encrypted; the email and the project only for
// the messages.
func GSheets(keyEncrypted, email, project string) pyjson.Object {
	return pyjson.Object{{Key: "db_type", Value: "gsheets"}, {Key: "key_encrypted", Value: keyEncrypted},
		{Key: "client_email", Value: email}, {Key: "project_id", Value: project}}
}

// API is _api_payload: only what the chosen authentication uses travels.
func API(host, username, secret, extra string) pyjson.Object {
	opts := APIOptions(extra)
	auth := "none"
	if a := opts.Value("auth"); in(a, APIAuth...) {
		auth = a.(string)
	}
	headers := pyjson.Object{}
	if h, ok := opts.Value("headers").(pyjson.Object); ok {
		for _, kv := range h {
			headers = headers.Set(pystr.Strip(kv.Key), pyjson.Str(kv.Value))
		}
	}
	user := ""
	if auth == "basic" || auth == "oauth2" {
		user = pystr.Strip(username)
	}
	sec := ""
	if auth != "none" {
		sec = secret
	}
	keyIn, clientAuth := "header", "body"
	if v := opts.Value("key_in"); in(v, "header", "query") {
		keyIn = v.(string)
	}
	if v := opts.Value("client_auth"); in(v, "body", "basic") {
		clientAuth = v.(string)
	}
	text := func(k string) string { return pystr.Strip(pyjson.Str(pyjson.Or(opts.Value(k), ""))) }
	return pyjson.Object{{Key: "db_type", Value: "api"}, {Key: "base_url", Value: pystr.Strip(host)}, {Key: "auth", Value: auth},
		{Key: "username", Value: user}, {Key: "secret_encrypted", Value: sec}, {Key: "key_name", Value: text("key_name")},
		{Key: "key_in", Value: keyIn}, {Key: "token_url", Value: text("token_url")}, {Key: "scope", Value: text("scope")},
		{Key: "client_auth", Value: clientAuth}, {Key: "headers", Value: headers}, {Key: "test_path", Value: text("test_path")}}
}

// SFTP is _sftp_payload: only the secret of the chosen method travels.
func SFTP(host string, port *int64, username, passwordEnc, keyEnc, extra, hostKeyType, hostKeyFingerprint string) pyjson.Object {
	auth := SFTPAuth(extra)
	var p any = int64(22)
	if port != nil && *port != 0 {
		p = *port
	}
	pw, key := "", ""
	if auth == "password" {
		pw = passwordEnc
	}
	if auth == "key" {
		key = keyEnc
	}
	return pyjson.Object{{Key: "db_type", Value: "sftp"}, {Key: "host", Value: pystr.Strip(host)}, {Key: "port", Value: p},
		{Key: "username", Value: pystr.Strip(username)}, {Key: "auth", Value: auth}, {Key: "password_encrypted", Value: pw},
		{Key: "private_key_encrypted", Value: key}, {Key: "host_key_type", Value: hostKeyType},
		{Key: "host_key_fingerprint", Value: hostKeyFingerprint}}
}

// S3 is _s3_payload: the columns mapped to the S3 fields.
func S3(host, username, secret, database, dbSchema string) pyjson.Object {
	return pyjson.Object{{Key: "db_type", Value: "s3"}, {Key: "endpoint_url", Value: host}, {Key: "access_key", Value: username},
		{Key: "secret_key_encrypted", Value: secret}, {Key: "bucket", Value: database}, {Key: "region", Value: dbSchema}}
}

// Database is the payload of a database connection.
func Database(dbType, host string, port *int64, username, secret, database, dbSchema, extra string) pyjson.Object {
	var p any
	if port != nil {
		p = *port
	}
	return pyjson.Object{{Key: "db_type", Value: dbType}, {Key: "host", Value: host}, {Key: "port", Value: p},
		{Key: "username", Value: username}, {Key: "password_encrypted", Value: secret}, {Key: "database", Value: database},
		{Key: "db_schema", Value: dbSchema}, {Key: "extra", Value: dbtls.EngineOptions(dbType, extra)}}
}

// Payload is engine_connection_payload: the engine's `connection`, the secret still ENCRYPTED.
func Payload(c *Conn) pyjson.Object {
	switch c.DBType {
	case "sharepoint":
		return SharePoint(c.Host, c.Username, c.PasswordEncrypted, c.Database, c.Extra)
	case "smtp":
		return SMTP(c.Host, c.Port, c.Username, c.PasswordEncrypted, SMTPOptions(c.Extra))
	case "sftp":
		return SFTP(c.Host, c.Port, c.Username, c.PasswordEncrypted, c.PrivateKeyEncrypted, c.Extra, c.HostKeyType, c.HostKeyFingerprint)
	case "s3":
		return S3(c.Host, c.Username, c.PasswordEncrypted, c.Database, c.DBSchema)
	case "api":
		return API(c.Host, c.Username, c.PasswordEncrypted, c.Extra)
	case "gsheets":
		return GSheets(c.PasswordEncrypted, c.Host, c.Database)
	}
	return Database(c.DBType, c.Host, c.Port, c.Username, c.PasswordEncrypted, c.Database, c.DBSchema, c.Extra)
}
