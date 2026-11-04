package api

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtls"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/connections.py: creating, editing and testing before saving a connection, with the
// checks of each kind's options.

func init() {
	register("connections_create", func(d *Deps) []server.Route {
		b := func(route string, h authedFunc) http.Handler { return withBody(server.Authed(d.Auth, route, h)) }
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/connections", Handler: b("/projects/{project_id}/connections", createConnection(d))},
			{Method: http.MethodPost, Pattern: "/projects/{project_id}/connections/test",
				Handler: b("/projects/{project_id}/connections/test", testDraftConnection(d))},
			{Method: http.MethodPatch, Pattern: "/connections/{conn_id}", Handler: b("/connections/{conn_id}", updateConnection(d))},
		}
	})
}

// supportedDBTypes is SUPPORTED_DB_TYPES, sorted as the refusal lists it.
var supportedDBTypes = []string{"api", "clickhouse", "gsheets", "mariadb", "mysql", "oracle", "postgresql", "s3", "sftp",
	"sharepoint", "smtp", "sqlserver", "trino"}

func supported(dbType string) bool {
	for _, t := range supportedDBTypes {
		if t == dbType {
			return true
		}
	}
	return false
}

func unsupportedDBType() *auth.Refusal {
	quoted := make([]string, len(supportedDBTypes))
	for i, t := range supportedDBTypes {
		quoted[i] = "'" + t + "'"
	}
	return unprocessable("Unsupported db_type: use one of [" + strings.Join(quoted, ", ") + "]")
}

// ── the options' checks ──────────────────────────────────────────────────────

// validExtra is _valid_extra: the kind's options must be a JSON object, and those the engine
// reads are checked. Returns the text stored (stripped).
func validExtra(extra *string, dbType string) (string, *auth.Refusal) {
	raw := "{}"
	if extra != nil && *extra != "" {
		raw = *extra
	}
	raw = pystr.Strip(raw)
	if raw == "" {
		raw = "{}"
	}
	v, err := pyjson.Loads(raw)
	if err != nil {
		return "", unprocessable("The connection options are not valid JSON")
	}
	data, ok := v.(pyjson.Object)
	if !ok {
		return "", unprocessable("The connection options must be a JSON object")
	}
	if e := dbtls.OptionError(dbType, data); e != "" {
		return "", unprocessable(e)
	}
	switch dbType {
	case "smtp":
		if errs := smtpOptionErrors(data); len(errs) > 0 {
			return "", unprocessable("SMTP options: " + strings.Join(errs, "; "))
		}
	case "sftp":
		if a := data.Value("auth"); a != nil && a != "" && a != "password" && a != "key" {
			return "", unprocessable("SFTP: auth must be password or key")
		}
	case "api":
		if e := apiOptionError(data); e != "" {
			return "", unprocessable("API: " + e)
		}
	}
	return raw, nil
}

// smtpOptionErrors is _smtp_option_errors.
func smtpOptionErrors(data pyjson.Object) []string {
	var errs []string
	if t := data.Value("tls"); t != nil && t != "" && t != "starttls" && t != "ssl" && t != "none" {
		errs = append(errs, "tls must be starttls, ssl or none")
	}
	for _, key := range []string{"from_address", "from_name"} {
		if v := data.Value(key); v != nil {
			if _, ok := v.(string); !ok {
				errs = append(errs, key+" must be text")
			}
		}
	}
	if _, ok := connpayload.DomainList(data.Value("allowed_domains")); !ok {
		errs = append(errs, "allowed_domains must be a list of domains or a comma-separated text")
	}
	return errs
}

// headerName is _HEADER_NAME; Python's `$` also matches before a final newline.
var headerName = regexp.MustCompile("^[!#$%&'*+.^_`|~0-9A-Za-z-]+\n?$")

// urlError is _url_error: what is wrong with an API address ("" = nothing); err is
// urlsplit's ValueError (an exception nobody catches).
func urlError(url, what string) (string, error) {
	p, err := pyurl.URLSplitNFKC(pystr.Strip(url))
	if err != nil {
		return "", err
	}
	if _, has := p.Hostname(); (p.Scheme != "http" && p.Scheme != "https") || !has {
		return fmt.Sprintf("invalid %s '%s': expected http(s)://host[:port][/path]", what, url), nil
	}
	if user, password := p.UserInfo(); user != "" || password != "" {
		return fmt.Sprintf("the %s cannot hold a user or a password: use the authentication", what), nil
	}
	if p.Query != "" || p.Fragment != "" {
		return fmt.Sprintf("the %s cannot hold a query (?…) or a fragment (#…): put the parameters in the datasource "+
			"and the key in the authentication", what), nil
	}
	return "", nil
}

// errURL is urlsplit's ValueError met by a check: the route's 500.
type errURL struct{ error }

// apiOptionError is _api_option_error ("" = nothing wrong). A ValueError of urlsplit panics
// as errURL, recovered by the route (a 500).
func apiOptionError(data pyjson.Object) string {
	auth := pyjson.Or(data.Value("auth"), "none")
	if !connpaylodIn(auth, connpayload.APIAuth...) {
		return "auth must be none, api_key, bearer, basic or oauth2"
	}
	keyIn, hasKeyIn := data.Get("key_in")
	if !hasKeyIn {
		keyIn = "header"
	}
	if auth == "api_key" {
		name := pystr.Strip(pyjson.Str(pyjson.Or(data.Value("key_name"), "")))
		if name == "" {
			return "the API key needs the name of its header or query parameter"
		}
		if keyIn != "header" && keyIn != "query" {
			return "the API key goes in a header or query parameter"
		}
		if keyIn == "header" && !headerName.MatchString(name) {
			return fmt.Sprintf("invalid name for the API key header '%s'", name)
		}
	}
	if auth == "oauth2" {
		if pystr.Strip(pyjson.Str(pyjson.Or(data.Value("token_url"), ""))) == "" {
			return "OAuth2 needs the token URL"
		}
		e, err := urlError(pyjson.Str(data.Value("token_url")), "token URL")
		if err != nil {
			panic(errURL{err})
		}
		if e != "" {
			return e
		}
		ca, has := data.Get("client_auth")
		if !has {
			ca = "body"
		}
		if ca != "body" && ca != "basic" {
			return "the OAuth2 client authentication is body or basic"
		}
	}
	headers, ok := pyjson.Or(data.Value("headers"), pyjson.Object{}).(pyjson.Object)
	if ok {
		for _, kv := range headers {
			if _, isStr := kv.Value.(string); !isStr {
				ok = false
			}
		}
	}
	if !ok {
		return "the default headers must be an object of names and values (text)"
	}
	for _, kv := range headers {
		k := pystr.Strip(kv.Key)
		if k == "" {
			k = "-"
		}
		if !headerName.MatchString(k) {
			return fmt.Sprintf("invalid header name '%s'", kv.Key)
		}
		if v := kv.Value.(string); strings.ContainsAny(v, "\r\n") {
			return fmt.Sprintf("the header '%s' cannot hold a line break", kv.Key)
		}
	}
	testPath := pyjson.Str(pyjson.Or(data.Value("test_path"), ""))
	if strings.Contains(testPath, "://") || strings.HasPrefix(testPath, "//") {
		return "the test path must be relative to the base URL (e.g. /status)"
	}
	return ""
}

func connpaylodIn(v any, options ...string) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	for _, o := range options {
		if o == s {
			return true
		}
	}
	return false
}

// checkAPI is _check_api.
func checkAPI(host, username, extra string, hasSecret bool) *auth.Refusal {
	e, err := urlError(host, "base URL")
	if err != nil {
		panic(errURL{err})
	}
	opts := connpayload.APIOptions(extra)
	a := pyjson.Or(opts.Value("auth"), "none")
	if e == "" {
		e = apiOptionError(opts)
	}
	if e == "" && (a == "basic" || a == "oauth2") && pystr.Strip(username) == "" {
		if a == "oauth2" {
			e = "OAuth2 needs the client id"
		} else {
			e = "basic authentication needs the user"
		}
	}
	if e == "" && a != "none" && !hasSecret {
		e = "enter the secret of the authentication (the API key, the token, the password or the client secret)"
	}
	if e != "" {
		return unprocessable("API: " + e)
	}
	return nil
}

// apiOrigin is _api_origin: scheme, host and port of an address (the default port of its
// scheme when none, or a bad one).
func apiOrigin(url string) (string, string, int64, error) {
	p, err := pyurl.URLSplitNFKC(pystr.Strip(url))
	if err != nil {
		return "", "", 0, err
	}
	host, _ := p.Hostname()
	scheme := strings.ToLower(p.Scheme)
	port := splitPort(p)
	if port == 0 {
		port = 80
		if scheme == "https" {
			port = 443
		}
	}
	return scheme, strings.ToLower(host), port, nil
}

// splitPort is SplitResult.port, 0 for None or a ValueError (_api_origin catches it).
func splitPort(p pyurl.Split) int64 {
	_, hostinfo, _ := strings.Cut(p.Netloc, "@")
	if !strings.Contains(p.Netloc, "@") {
		hostinfo = p.Netloc
	} else {
		hostinfo = p.Netloc[strings.LastIndex(p.Netloc, "@")+1:]
	}
	var port string
	if _, bracketed, open := strings.Cut(hostinfo, "["); open {
		_, port, _ = strings.Cut(bracketed, "]")
		_, port, _ = strings.Cut(port, ":")
	} else {
		_, port, _ = strings.Cut(hostinfo, ":")
	}
	if port == "" {
		return 0
	}
	for _, c := range port {
		if c < '0' || c > '9' {
			return 0
		}
	}
	n, err := strconv.ParseInt(port, 10, 64)
	if err != nil || n > 65535 {
		return 0
	}
	return n
}

// checkPrivateKey is _check_private_key.
func checkPrivateKey(text string) *auth.Refusal {
	t := pystr.Strip(text)
	if !strings.HasPrefix(t, "-----BEGIN") || !strings.Contains(t, "PRIVATE KEY-----") {
		return unprocessable("This is not a private key: paste the whole PEM or OpenSSH private key, from -----BEGIN to -----END")
	}
	return nil
}

// checkSFTP is _check_sftp. port: nil (None), int64 or *big.Int.
func checkSFTP(host string, port any, username, auth string, hasPassword, hasKey bool) *auth.Refusal {
	h := pystr.Strip(host)
	if h == "" || strings.Contains(h, "://") || strings.Contains(h, "/") || strings.Contains(h, " ") {
		return unprocessable("SFTP: enter the host name or IP address only (no sftp://, no path)")
	}
	if port != nil {
		n, ok := port.(int64)
		if !ok || n <= 0 || n >= 65536 {
			return unprocessable("SFTP: the port must be between 1 and 65535")
		}
	}
	if pystr.Strip(username) == "" {
		return unprocessable("SFTP: the user name is required")
	}
	if auth == "password" && !hasPassword {
		return unprocessable("SFTP: enter the password, or choose the private key method")
	}
	if auth == "key" && !hasKey {
		return unprocessable("SFTP: paste the private key, or choose the password method")
	}
	return nil
}

// checkSharePoint is _check_sharepoint.
func checkSharePoint(host, username, database string) *auth.Refusal {
	if !strings.HasPrefix(pystr.Lower(pystr.Strip(host)), "https://") {
		return unprocessable("Invalid SharePoint site URL: expected https://<company>.sharepoint.com/sites/<name>")
	}
	if pystr.Strip(username) == "" || pystr.Strip(database) == "" {
		return unprocessable("The tenant id and the client id of the app registered in Entra ID are required")
	}
	return nil
}

// gsheetsKeyMax is _GSHEETS_KEY_MAX.
const gsheetsKeyMax = 20_000

// gsheetsIdentity is gsheets_identity: the account's email and project from its JSON key.
func gsheetsIdentity(keyText string) (string, string, *auth.Refusal) {
	text := pystr.Strip(keyText)
	if text == "" {
		return "", "", unprocessable("Google Sheets: paste the service account's JSON key, or choose its file")
	}
	if pystr.Len(text) > gsheetsKeyMax {
		return "", "", unprocessable("Google Sheets: this is too long for a service account's JSON key")
	}
	v, err := pyjson.Loads(text)
	if err != nil {
		return "", "", unprocessable("Google Sheets: this is not JSON. Paste the whole key file, from { to }, as Google downloaded it")
	}
	key, isDict := v.(pyjson.Object)
	if isDict {
		_, installed := key.Get("installed")
		_, web := key.Get("web")
		if installed || web {
			return "", "", unprocessable("Google Sheets: this is an OAuth client's file, not a service account's key. In the Google " +
				"Cloud console: IAM & Admin → Service accounts → the account → Keys → Add key → JSON")
		}
	}
	if !isDict || key.Value("type") != "service_account" {
		return "", "", unprocessable("Google Sheets: this is not a service account's key (its \"type\" must be \"service_account\"). " +
			"Download a JSON key from IAM & Admin → Service accounts → the account → Keys")
	}
	email := pystr.Strip(pyjson.Str(pyjson.Or(key.Value("client_email"), "")))
	privateKey := pyjson.Str(pyjson.Or(key.Value("private_key"), ""))
	if email == "" || !strings.Contains(email, "@") || !strings.HasPrefix(strings.TrimLeftFunc(privateKey, pystr.IsSpace), "-----BEGIN") ||
		!strings.Contains(privateKey, "PRIVATE KEY-----") {
		return "", "", unprocessable("Google Sheets: the key has no client_email or no private_key: download a new JSON key of the service account")
	}
	return email, pystr.Strip(pyjson.Str(pyjson.Or(key.Value("project_id"), ""))), nil
}

// errEncrypt: a text Fernet cannot take (Python's encode() raising on a lone surrogate), or
// no key: an exception nobody catches.
var errEncrypt = errors.New("the secret cannot be encrypted")

// encryptSecret is encrypt_secret.
func encryptSecret(d *Deps, plain string) (string, error) {
	if d.Fernet == nil || !utf8.ValidString(plain) {
		return "", errEncrypt
	}
	return d.Fernet.Encrypt(plain)
}

// keyBlob is _key_blob: the private key and its passphrase, encrypted together.
func keyBlob(d *Deps, privateKey, passphrase string) (string, error) {
	text, err := pyjson.Dumps(pyjson.Object{{Key: "private_key", Value: pystr.Strip(privateKey) + "\n"},
		{Key: "passphrase", Value: passphrase}}, pyjson.Options{EnsureASCII: true})
	if err != nil {
		return "", err
	}
	return encryptSecret(d, text)
}

// portParam is a validated `port`: nil, int64 or *big.Int as the JSON value the engine gets.
func portParam(v any) any {
	if b, ok := v.(*big.Int); ok {
		return pyjson.Int(b.String())
	}
	return v
}

// portColumn is the port as the integer column takes it: ok false when PostgreSQL refuses it
// (the commit's 500).
func portColumn(v any) (*int64, bool) {
	switch x := v.(type) {
	case nil:
		return nil, true
	case int64:
		return &x, x >= -1<<31 && x <= 1<<31-1
	}
	return nil, false
}

// recoverURL turns urlsplit's ValueError met in a check into the route's 500.
func recoverURL(d *Deps, w http.ResponseWriter) {
	if r := recover(); r != nil {
		if e, ok := r.(errURL); ok {
			d.Log.Error("connections: urlsplit", "err", e.error)
			server.InternalError(w)
			return
		}
		panic(r)
	}
}

// ── the routes ───────────────────────────────────────────────────────────────

// connectionFolder validates the folder's id and the body, then the 404 and CONNECT.
func connectionFolder(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, m *pyd.Model) (int64, *pyd.ModelValue, bool) {
	var ps pyd.Params
	pv := ps.Path("project_id", r.PathValue("project_id"), pyd.Int())
	body := ps.Body(bodyOf(r), m)
	if !validated(w, &ps) {
		return 0, nil, true
	}
	id, ok := rowID(pv)
	found := false
	if ok {
		var err error
		if found, err = exists(r.Context(), d.DB, "projects", id); err != nil {
			fail500(d, w, "connections", err)
			return 0, nil, true
		}
	}
	if !found {
		server.Refuse(w, notFound("Folder not found"))
		return 0, nil, true
	}
	if ref, err := perm.For(d.DB, &p.User).Ensure(r.Context(), id, perm.Connect); refuse(d, w, ref, err) {
		return 0, nil, true
	}
	return id, body, false
}

// connectionNameTaken is _name_taken.
func connectionNameTaken(ctx context.Context, d *Deps, project int64, name string, exclude int64) (bool, error) {
	var one int
	err := d.DB.QueryRow(ctx, `SELECT 1 FROM connections WHERE project_id = $1 AND name = $2 AND id <> $3 LIMIT 1`,
		project, name, exclude).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

// createConnection is POST /projects/{project_id}/connections.
func createConnection(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		defer recoverURL(d, w)
		project, body, done := connectionFolder(d, w, r, p, gatewaymodels.ConnectionCreate)
		if done {
			return
		}
		ctx := r.Context()
		dbType, host, username := strField(body, "db_type"), strField(body, "host"), strField(body, "username")
		database, dbSchema, extra := strField(body, "database"), strField(body, "db_schema"), strField(body, "extra")
		password, privateKey := strField(body, "password"), strField(body, "private_key")
		port := body.Get("port")
		if !supported(dbType) {
			server.Refuse(w, unsupportedDBType())
			return
		}
		if dbType == "sharepoint" {
			if ref := checkSharePoint(host, username, database); ref != nil {
				server.Refuse(w, ref)
				return
			}
		}
		if _, ref := validExtra(&extra, dbType); ref != nil {
			server.Refuse(w, ref)
			return
		}
		if privateKey != "" && dbType != "sftp" {
			server.Refuse(w, unprocessable("Only SFTP connections take a private key"))
			return
		}
		if dbType == "sftp" {
			if privateKey != "" {
				if ref := checkPrivateKey(privateKey); ref != nil {
					server.Refuse(w, ref)
					return
				}
			}
			if ref := checkSFTP(host, port, username, connpayload.SFTPAuth(extra), password != "", privateKey != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
		}
		if dbType == "api" {
			if ref := checkAPI(host, username, extra, password != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
		}
		if dbType == "gsheets" {
			email, gproject, ref := gsheetsIdentity(password)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			host, database = email, gproject
			port, username, dbSchema, extra = nil, "", "", "{}"
		}
		name := pystr.Strip(strField(body, "name"))
		if name == "" {
			server.Refuse(w, unprocessable("The connection needs a name"))
			return
		}
		if taken, err := connectionNameTaken(ctx, d, project, name, 0); err != nil {
			fail500(d, w, "connections", err)
			return
		} else if taken {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a connection '%s' in this folder", name)})
			return
		}
		secret, blob := "", ""
		var err error
		if password != "" {
			if secret, err = encryptSecret(d, password); err != nil {
				fail500(d, w, "connections", err)
				return
			}
		}
		stored, _ := validExtra(&extra, dbType)
		if privateKey != "" {
			if blob, err = keyBlob(d, privateKey, strField(body, "private_key_passphrase")); err != nil {
				fail500(d, w, "connections", err)
				return
			}
		}
		portCol, ok := portColumn(port)
		if !ok {
			fail500(d, w, "connections", errors.New("port out of the column's range"))
			return
		}
		now := time.Now().UTC()
		var id int64
		err = d.DB.QueryRow(ctx, `INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
			password_encrypted, database, db_schema, extra, private_key_encrypted, host_key_type, host_key_fingerprint, created_at, updated_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, '', '', $14, $14) RETURNING id`,
			name, strField(body, "description"), project, p.User.ID, dbType, host, portCol, username, secret, database, dbSchema,
			stored, blob, now).Scan(&id)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		kind := "connection"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "connection.create", TargetType: &kind, TargetID: &id,
			TargetLabel: &name, Detail: pyjson.Object{{Key: "project_id", Value: project}, {Key: "db_type", Value: dbType},
				{Key: "host", Value: host}}})
		answerConnection(d, w, ctx, id, http.StatusCreated)
	}
}

func answerConnection(d *Deps, w http.ResponseWriter, ctx context.Context, id int64, status int) {
	out, err := scanConnection(d.DB.QueryRow(ctx, `SELECT `+connectionColumns+` FROM connections WHERE id = $1`, id))
	if err != nil {
		fail500(d, w, "connections", err)
		return
	}
	server.JSON(w, status, out)
}

// testDraftConnection is POST /projects/{project_id}/connections/test: a connection tested
// before it is saved, with only the secret written in the form. It stores nothing.
func testDraftConnection(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		defer recoverURL(d, w)
		_, body, done := connectionFolder(d, w, r, p, gatewaymodels.ConnectionTest)
		if done {
			return
		}
		ctx, lang := r.Context(), requestLanguage(r)
		dbType, host, username := strField(body, "db_type"), strField(body, "host"), strField(body, "username")
		database, dbSchema, extra := strField(body, "database"), strField(body, "db_schema"), strField(body, "extra")
		password, privateKey := strField(body, "password"), strField(body, "private_key")
		port := body.Get("port")
		if !supported(dbType) {
			server.Refuse(w, unsupportedDBType())
			return
		}
		secret := ""
		if password != "" {
			var err error
			if secret, err = encryptSecret(d, password); err != nil {
				fail500(d, w, "connections", err)
				return
			}
		}
		withPort := func(o pyjson.Object) pyjson.Object { return o.Set("port", portParam(port)) }
		var payload pyjson.Object
		switch dbType {
		case "sftp":
			if privateKey != "" {
				if ref := checkPrivateKey(privateKey); ref != nil {
					server.Refuse(w, ref)
					return
				}
			}
			if ref := checkSFTP(host, port, username, connpayload.SFTPAuth(extra), password != "", privateKey != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
			blob := ""
			if privateKey != "" {
				var err error
				if blob, err = keyBlob(d, privateKey, strField(body, "private_key_passphrase")); err != nil {
					fail500(d, w, "connections", err)
					return
				}
			}
			payload = connpayload.SFTP(host, nil, username, secret, blob, extra, "", "")
			if port != nil && port != int64(0) {
				payload = withPort(payload)
			}
			expected := pystr.Strip(strField(body, "expected_fingerprint"))
			raw, ref, err := engineInspect(ctx, d, payload, "test", "", lang, pyjson.Object{{Key: "expected_fingerprint", Value: expected},
				{Key: "expected_type", Value: pystr.Strip(strField(body, "expected_type"))}})
			if err != nil || ref != nil {
				inspectAnswer(d, w, raw, ref, err)
				return
			}
			result, ok := sftpResult(w, raw)
			if !ok {
				return
			}
			portText := "22"
			if port != nil && port != int64(0) {
				portText = pyjson.Str(portParam(port))
			}
			if ref, ok := refuseUnofferedKey(result, expected, pystr.Strip(host)+":"+portText, lang); !ok {
				server.InternalError(w)
				return
			} else if ref != nil {
				server.Refuse(w, ref)
				return
			}
			engineJSON(w, raw)
			return
		case "api":
			if _, ref := validExtra(&extra, "api"); ref != nil {
				server.Refuse(w, ref)
				return
			}
			if ref := checkAPI(host, username, extra, password != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
			raw, ref, err := engineInspect(ctx, d, connpayload.API(host, username, secret, extra), "test", "", "en", nil)
			inspectAnswer(d, w, raw, ref, err)
			return
		case "gsheets":
			email, gproject, ref := gsheetsIdentity(password)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			raw, ref, err := engineInspect(ctx, d, connpayload.GSheets(secret, email, gproject), "test", "", "en", nil)
			inspectAnswer(d, w, raw, ref, err)
			return
		case "sharepoint":
			if ref := checkSharePoint(host, username, database); ref != nil {
				server.Refuse(w, ref)
				return
			}
			payload = connpayload.SharePoint(host, username, secret, database, extra)
		case "s3":
			payload = connpayload.S3(host, username, secret, database, dbSchema)
		case "smtp":
			raw, ref := validExtra(&extra, "smtp")
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			payload = connpayload.SMTP(host, nil, username, secret, connpayload.SMTPOptions(raw))
			if port != nil && port != int64(0) {
				payload = withPort(payload)
			}
		default:
			if dbtls.IsDB(dbType) {
				if _, ref := validExtra(&extra, dbType); ref != nil {
					server.Refuse(w, ref)
					return
				}
			}
			payload = withPort(connpayload.Database(dbType, host, nil, username, secret, database, dbSchema, extra))
		}
		raw, ref, err := engineInspect(ctx, d, payload, "test", "", lang, nil)
		inspectAnswer(d, w, raw, ref, err)
	}
}

// updateConnection is PATCH /connections/{conn_id}: edit, move; a stored secret never
// follows the connection to another server.
func updateConnection(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		defer recoverURL(d, w)
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.ConnectionUpdate, false)
		if done {
			return
		}
		ctx := r.Context()
		c := perm.For(d.DB, &p.User)
		target := conn.ProjectID
		if pv := body.Get("project_id"); pv != nil {
			dest, ok := rowID(pv)
			if !ok || dest != conn.ProjectID {
				found := false
				if ok {
					var err error
					if found, err = exists(ctx, d.DB, "projects", dest); err != nil {
						fail500(d, w, "connections", err)
						return
					}
				}
				if !found {
					server.Refuse(w, notFound("Destination folder not found"))
					return
				}
				if ref, err := c.Ensure(ctx, dest, perm.Connect); refuse(d, w, ref, err) {
					return
				}
				target = dest
			}
		}
		name := conn.Name
		if s := pyStr(body.Get("name")); s != nil {
			name = pystr.Strip(*s)
		}
		if name == "" {
			server.Refuse(w, unprocessable("The connection needs a name"))
			return
		}
		if taken, err := connectionNameTaken(ctx, d, target, name, conn.ID); err != nil {
			fail500(d, w, "connections", err)
			return
		} else if taken {
			server.Refuse(w, &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("There is already a connection '%s' in this folder", name)})
			return
		}
		password, privateKey, passphrase := pyStr(body.Get("password")), pyStr(body.Get("private_key")), pyStr(body.Get("private_key_passphrase"))
		truthy := func(s *string) bool { return s != nil && *s != "" }
		if (truthy(privateKey) || truthy(passphrase)) && conn.DBType != "sftp" {
			server.Refuse(w, unprocessable("Only SFTP connections take a private key"))
			return
		}
		if truthy(privateKey) {
			if ref := checkPrivateKey(*privateKey); ref != nil {
				server.Refuse(w, ref)
				return
			}
		}
		host, username, extra := pyStr(body.Get("host")), pyStr(body.Get("username")), pyStr(body.Get("extra"))
		database, dbSchema := pyStr(body.Get("database")), pyStr(body.Get("db_schema"))
		port := body.Get("port")
		var connPort any
		if conn.Port != nil {
			connPort = *conn.Port
		}
		hostBefore, portBefore := conn.Host, connPort
		moved := false
		if conn.DBType == "sftp" {
			moved = (host != nil && *host != conn.Host) || (port != nil && !pyjson.Equal(portParam(port), connPort)) ||
				(username != nil && *username != conn.Username)
			if moved && !(truthy(password) || truthy(privateKey)) {
				server.Refuse(w, unprocessable("SFTP: a new host, port or user needs the password (or the private key) typed again"))
				return
			}
		}
		if conn.DBType == "gsheets" {
			port, username, dbSchema, extra = nil, nil, nil, nil
			if password != nil {
				email, gproject, ref := gsheetsIdentity(*password)
				if ref != nil {
					server.Refuse(w, ref)
					return
				}
				host, database = &email, &gproject
			} else {
				host, database = nil, nil
			}
		}
		if conn.DBType == "api" {
			beforeHost := conn.Host
			beforeToken := pyjson.Or(connpayload.APIOptions(conn.Extra).Value("token_url"), "")
			newHost := conn.Host
			if host != nil {
				newHost = *host
			}
			newOpts := connpayload.APIOptions(conn.Extra)
			if extra != nil {
				newOpts = connpayload.APIOptions(*extra)
			}
			newToken := pyjson.Str(pyjson.Or(newOpts.Value("token_url"), ""))
			s1, h1, p1, err := apiOrigin(newHost)
			if err != nil {
				panic(errURL{err})
			}
			s2, h2, p2, err := apiOrigin(beforeHost)
			if err != nil {
				panic(errURL{err})
			}
			apiMoved := s1 != s2 || h1 != h2 || p1 != p2 || (newToken != "" && pystr.Strip(newToken) != pystr.Strip(pyjson.Str(beforeToken)))
			if apiMoved && pyjson.Or(newOpts.Value("auth"), "none") == "none" {
				apiMoved = false
				if password == nil {
					empty := ""
					password = &empty
				}
			}
			if apiMoved && !truthy(password) && conn.PasswordEncrypted != "" {
				server.Refuse(w, unprocessable("API: a new host (or OAuth2 token URL) needs the secret typed again"))
				return
			}
		}
		// the edit, as the Python code applies it to the row
		next := *conn
		next.Name, next.ProjectID = name, target
		if s := pyStr(body.Get("description")); s != nil {
			next.Description = *s
		}
		if host != nil {
			next.Host = *host
		}
		nextPort := connPort
		if port != nil {
			nextPort = port
		}
		if username != nil {
			next.Username = *username
		}
		if database != nil {
			next.Database = *database
		}
		if dbSchema != nil {
			next.DBSchema = *dbSchema
		}
		if extra != nil {
			raw, ref := validExtra(extra, conn.DBType)
			if ref != nil {
				server.Refuse(w, ref)
				return
			}
			next.Extra = raw
		}
		var err error
		if password != nil {
			next.PasswordEncrypted = ""
			if *password != "" {
				if next.PasswordEncrypted, err = encryptSecret(d, *password); err != nil {
					fail500(d, w, "connections", err)
					return
				}
			}
		}
		if privateKey != nil {
			next.PrivateKeyEncrypted = ""
			if *privateKey != "" {
				pp := ""
				if passphrase != nil {
					pp = *passphrase
				}
				if next.PrivateKeyEncrypted, err = keyBlob(d, *privateKey, pp); err != nil {
					fail500(d, w, "connections", err)
					return
				}
			}
		} else if passphrase != nil && conn.PrivateKeyEncrypted != "" {
			// a passphrase alone: the stored key stays, with the new passphrase
			stored, err := storedKey(d, conn.PrivateKeyEncrypted)
			if err != nil {
				fail500(d, w, "connections", err)
				return
			}
			if next.PrivateKeyEncrypted, err = keyBlob(d, stored, *passphrase); err != nil {
				fail500(d, w, "connections", err)
				return
			}
		}
		if moved {
			if !truthy(password) {
				next.PasswordEncrypted = ""
			}
			if !truthy(privateKey) {
				next.PrivateKeyEncrypted = ""
			}
		}
		if conn.DBType == "api" {
			if ref := checkAPI(next.Host, next.Username, next.Extra, next.PasswordEncrypted != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
		}
		if conn.DBType == "sftp" {
			if ref := checkSFTP(next.Host, portParamBack(nextPort), next.Username, connpayload.SFTPAuth(next.Extra),
				next.PasswordEncrypted != "", next.PrivateKeyEncrypted != ""); ref != nil {
				server.Refuse(w, ref)
				return
			}
			if next.Host != hostBefore || !pyjson.Equal(portParam(nextPort), portBefore) {
				next.HostKeyType, next.HostKeyFingerprint = "", ""
			}
		}
		portCol, ok := portColumn(portParamBack(nextPort))
		if !ok {
			fail500(d, w, "connections", errors.New("port out of the column's range"))
			return
		}
		_, err = d.DB.Exec(ctx, `UPDATE connections SET name = $1, project_id = $2, description = $3, host = $4, port = $5,
			username = $6, database = $7, db_schema = $8, extra = $9, password_encrypted = $10, private_key_encrypted = $11,
			host_key_type = $12, host_key_fingerprint = $13, updated_at = $14 WHERE id = $15`,
			next.Name, next.ProjectID, next.Description, next.Host, portCol, next.Username, next.Database, next.DBSchema, next.Extra,
			next.PasswordEncrypted, next.PrivateKeyEncrypted, next.HostKeyType, next.HostKeyFingerprint, time.Now().UTC(), conn.ID)
		if err != nil {
			fail500(d, w, "connections", err)
			return
		}
		kind := "connection"
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "connection.update", TargetType: &kind, TargetID: &conn.ID,
			TargetLabel: &next.Name})
		answerConnection(d, w, ctx, conn.ID, http.StatusOK)
	}
}

// portParamBack is a port as checkSFTP and portColumn take it (int64 or *big.Int).
func portParamBack(v any) any {
	if i, ok := v.(pyjson.Int); ok {
		b, _ := new(big.Int).SetString(string(i), 10)
		return b
	}
	return v
}

// storedKey is json.loads(decrypt_secret(conn.private_key_encrypted)).get("private_key", "").
func storedKey(d *Deps, encrypted string) (string, error) {
	if d.Fernet == nil {
		return "", errEncrypt
	}
	plain, err := d.Fernet.Decrypt(encrypted)
	if err != nil {
		return "", err
	}
	v, err := pyjson.Loads(plain)
	if err != nil {
		return "", err
	}
	o, ok := v.(pyjson.Object)
	if !ok {
		return "", errors.New("the stored key is not a dict")
	}
	k, has := o.Get("private_key")
	if !has {
		return "", nil
	}
	s, ok := k.(string)
	if !ok {
		return "", errors.New("the stored key is not text") // .strip() on something else
	}
	return s, nil
}
