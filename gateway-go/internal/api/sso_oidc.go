package api

// routes/sso.py and services/sso.py with single sign-on configured (OIDC__ISSUER and
// OIDC__CLIENT_ID): the authorization code flow with PKCE, state and nonce in a signed
// cookie, the ID token checked against the provider's keys, the person provisioned and their
// groups synchronized, then the same internal session as the local sign-in.

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/personalfolders"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/reqinfo"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/sessions"
)

const (
	txTokenType = "sso_tx"
	txCookie    = "tab_sso_tx"
	clockSkew   = 60 * time.Second // core/security.py CLOCK_SKEW_SECONDS
)

// allowedAlgs is ALLOWED_ALGS: asymmetric only.
var allowedAlgs = []string{"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}

func init() {
	register("sso_oidc", func(d *Deps) []server.Route {
		if !d.Config.SSOEnabled() {
			return nil
		}
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/auth/sso/config", Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				cfg := oidcSettings(r.Context(), d)
				server.JSON(w, http.StatusOK, SSOConfigOut{Enabled: true, ButtonLabel: cfg.ButtonLabel})
			})},
			{Method: http.MethodGet, Pattern: "/auth/sso/login", Handler: anonymous(d, ssoLogin(d))},
			{Method: http.MethodGet, Pattern: "/auth/sso/callback", Handler: anonymous(d, ssoCallback(d))},
		}
	})
}

// ── the settings ────────────────────────────────────────────────────────────

// oidcCfg is OidcSettings: the environment's, with the panel's values on top.
type oidcCfg struct {
	Issuer, ClientID, ClientSecret, RedirectURI, Scopes    string
	GroupsClaim, GroupAllowlist, SuperuserGroup            string
	AllowedEmailDomains, PostLoginURL, ButtonLabel, CAFile string
	AutoCreateGroups, Authoritative, RequireAllowlisted    bool
	VerifyTLS                                              bool
	DiscoveryTTL, LoginTxTTL                               int64
	Timeout                                                float64
}

func (c *oidcCfg) allowlist() map[string]bool {
	out := map[string]bool{}
	for _, g := range strings.Split(c.GroupAllowlist, ",") {
		if g = strings.TrimSpace(g); g != "" {
			out[g] = true
		}
	}
	return out
}

func (c *oidcCfg) allowedDomains() map[string]bool {
	out := map[string]bool{}
	for _, x := range strings.Split(c.AllowedEmailDomains, ",") {
		if strings.TrimSpace(x) != "" {
			out[strings.TrimLeft(strings.ToLower(strings.TrimSpace(x)), "@")] = true
		}
	}
	return out
}

func (c *oidcCfg) discoveryURL() string {
	return strings.TrimRight(c.Issuer, "/") + "/.well-known/openid-configuration"
}

// oidcEnv is OidcSettings from the environment.
func oidcEnv() oidcCfg {
	env := config.FromOS()
	str := func(n, def string) string {
		if v, ok := env(n); ok {
			return v
		}
		return def
	}
	boolean := func(n string, def bool) bool {
		if v, ok := env(n); ok {
			switch strings.ToLower(strings.TrimSpace(v)) {
			case "1", "true", "yes", "on", "t", "y":
				return true
			case "0", "false", "no", "off", "f", "n":
				return false
			}
		}
		return def
	}
	integer := func(n string, def int64) int64 {
		if v, ok := env(n); ok {
			if i, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64); err == nil {
				return i
			}
		}
		return def
	}
	timeout := 10.0
	if v, ok := env("OIDC__TIMEOUT_SECONDS"); ok {
		if f, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
			timeout = f
		}
	}
	return oidcCfg{Issuer: str("OIDC__ISSUER", ""), ClientID: str("OIDC__CLIENT_ID", ""), ClientSecret: str("OIDC__CLIENT_SECRET", ""),
		RedirectURI: str("OIDC__REDIRECT_URI", ""), Scopes: str("OIDC__SCOPES", "openid profile email"),
		GroupsClaim: str("OIDC__GROUPS_CLAIM", "groups"), GroupAllowlist: str("OIDC__GROUP_ALLOWLIST", ""),
		AutoCreateGroups: boolean("OIDC__AUTO_CREATE_GROUPS", false), Authoritative: boolean("OIDC__AUTHORITATIVE", true),
		SuperuserGroup: str("OIDC__SUPERUSER_GROUP", ""), AllowedEmailDomains: str("OIDC__ALLOWED_EMAIL_DOMAINS", ""),
		RequireAllowlisted: boolean("OIDC__REQUIRE_ALLOWLISTED_GROUP", false),
		PostLoginURL:       str("OIDC__POST_LOGIN_URL", "http://localhost:3000/auth/callback"),
		ButtonLabel:        str("OIDC__BUTTON_LABEL", "Single sign-on"), DiscoveryTTL: integer("OIDC__DISCOVERY_TTL_SECONDS", 3600),
		Timeout: timeout, CAFile: str("OIDC__CA_FILE", ""), VerifyTLS: boolean("OIDC__VERIFY_TLS", true),
		LoginTxTTL: integer("OIDC__LOGIN_TX_TTL_SECONDS", 600)}
}

// oidcPanelItem is an oidc.* item of overrides.ITEMS: its kind and range.
type oidcPanelItem struct {
	kind     string
	max      int64 // max_length of a text, the maximum of a number
	min      int64
	required bool
}

var oidcPanelItems = map[string]oidcPanelItem{
	"oidc.groups_claim": {kind: "str", max: 500, required: true}, "oidc.group_allowlist": {kind: "str", max: 4000},
	"oidc.auto_create_groups": {kind: "bool"}, "oidc.authoritative": {kind: "bool"},
	"oidc.allowed_email_domains": {kind: "str", max: 4000}, "oidc.require_allowlisted_group": {kind: "bool"},
	"oidc.button_label": {kind: "str", max: 60}, "oidc.discovery_ttl_seconds": {kind: "int", min: 60, max: 86400},
	"oidc.timeout_seconds": {kind: "float", min: 1, max: 60}, "oidc.login_tx_ttl_seconds": {kind: "int", min: 120, max: 3600},
}

// oidcSettings is get_settings().oidc: the panel's oidc.* values (overrides.check_one, and the
// constraint between require_allowlisted_group and the allowlist) on the environment's.
func oidcSettings(ctx context.Context, d *Deps) oidcCfg {
	cfg := oidcEnv()
	if !d.Panel.FromPanel() {
		return cfg
	}
	rows, err := d.DB.Query(ctx, `SELECT key, value FROM gateway_settings WHERE key LIKE 'oidc.%'`)
	if err != nil {
		return cfg
	}
	good := map[string]any{}
	for rows.Next() {
		var key, raw string
		if rows.Scan(&key, &raw) != nil {
			continue
		}
		it, ok := oidcPanelItems[key]
		if !ok {
			continue
		}
		v, err := pyjson.Loads(raw)
		if err != nil {
			continue
		}
		switch it.kind {
		case "bool":
			if b, ok := v.(bool); ok {
				good[key] = b
			}
		case "int":
			if n, ok := v.(int64); ok && n >= it.min && n <= it.max {
				good[key] = n
			}
		case "float":
			var f float64
			switch x := v.(type) {
			case int64:
				f = float64(x)
			case float64:
				f = x
			default:
				continue
			}
			if f >= float64(it.min) && f <= float64(it.max) {
				good[key] = f
			}
		case "str":
			s, ok := v.(string)
			s = strings.TrimSpace(s)
			if ok && int64(len([]rune(s))) <= it.max && !(it.required && s == "") {
				good[key] = s
			}
		}
	}
	rows.Close()
	// overrides.check: single sign-on that admits only the groups taken needs a list of them
	req, okReq := good["oidc.require_allowlisted_group"].(bool)
	if !okReq {
		req = cfg.RequireAllowlisted
	}
	allow, okAllow := good["oidc.group_allowlist"].(string)
	if !okAllow {
		allow = cfg.GroupAllowlist
	}
	if req && strings.TrimSpace(allow) == "" && strings.TrimSpace(cfg.SuperuserGroup) == "" {
		if okReq {
			delete(good, "oidc.require_allowlisted_group")
		} else if okAllow {
			delete(good, "oidc.group_allowlist")
		}
	}
	for k, v := range good {
		switch k {
		case "oidc.groups_claim":
			cfg.GroupsClaim = v.(string)
		case "oidc.group_allowlist":
			cfg.GroupAllowlist = v.(string)
		case "oidc.auto_create_groups":
			cfg.AutoCreateGroups = v.(bool)
		case "oidc.authoritative":
			cfg.Authoritative = v.(bool)
		case "oidc.allowed_email_domains":
			cfg.AllowedEmailDomains = v.(string)
		case "oidc.require_allowlisted_group":
			cfg.RequireAllowlisted = v.(bool)
		case "oidc.button_label":
			cfg.ButtonLabel = v.(string)
		case "oidc.discovery_ttl_seconds":
			cfg.DiscoveryTTL = v.(int64)
		case "oidc.timeout_seconds":
			cfg.Timeout = v.(float64)
		case "oidc.login_tx_ttl_seconds":
			cfg.LoginTxTTL = v.(int64)
		}
	}
	return cfg
}

// ── the provider ────────────────────────────────────────────────────────────

// ssoError is SsoError: a message for the log, a code for the person.
type ssoError struct{ message, code string }

func (e *ssoError) Error() string { return e.message }

func ssoErr(code, format string, a ...any) *ssoError {
	return &ssoError{fmt.Sprintf(format, a...), code}
}

// oidcClient is the provider's HTTP client: its certificate checked as sso.tls says.
func oidcClient(cfg oidcCfg) *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	switch {
	case !cfg.VerifyTLS:
		t.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} //nolint:gosec — the installer's choice
	case cfg.CAFile != "":
		pool, err := x509.SystemCertPool()
		if err != nil {
			pool = x509.NewCertPool()
		}
		if pem, err := os.ReadFile(cfg.CAFile); err == nil {
			pool.AppendCertsFromPEM(pem)
		}
		t.TLSClientConfig = &tls.Config{RootCAs: pool}
	}
	return &http.Client{Transport: t, Timeout: time.Duration(cfg.Timeout * float64(time.Second)),
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func untrusted(err error) bool {
	var cert *tls.CertificateVerificationError
	var unknown x509.UnknownAuthorityError
	return errors.As(err, &cert) || errors.As(err, &unknown)
}

// unreachable is sso.unreachable.
func unreachable(what, target string, err error) *ssoError {
	if untrusted(err) {
		host := target
		if strings.Contains(target, "://") {
			host = strings.Split(target, "/")[2]
		}
		return ssoErr("idp_untrusted", "%s: the certificate of %s is not signed by an authority the gateway trusts (%v)", what, host, err)
	}
	return ssoErr("idp_unreachable", "%s on %s: %v", what, target, err)
}

var discoveryCache = struct {
	sync.Mutex
	docs map[string]struct {
		at  time.Time
		doc pyjson.Object
	}
}{docs: map[string]struct {
	at  time.Time
	doc pyjson.Object
}{}}

// oidcDiscovery is sso.discovery: the provider's document, cached for discovery_ttl_seconds.
func oidcDiscovery(ctx context.Context, cfg oidcCfg) (pyjson.Object, error) {
	u := cfg.discoveryURL()
	discoveryCache.Lock()
	cached, ok := discoveryCache.docs[u]
	discoveryCache.Unlock()
	if ok && time.Since(cached.at) < time.Duration(cfg.DiscoveryTTL)*time.Second {
		return cached.doc, nil
	}
	body, err := oidcGet(ctx, cfg, u, "")
	if err != nil {
		return nil, unreachable("OIDC discovery failed", u, err)
	}
	v, err := pyjson.Loads(string(body))
	if err != nil {
		return nil, unreachable("OIDC discovery failed", u, err)
	}
	doc, isObj := v.(pyjson.Object)
	if !isObj {
		return nil, errors.New("sso: the discovery document is not a dict")
	}
	for _, f := range []string{"authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"} {
		if !pyjson.Truthy(doc.Value(f)) {
			return nil, ssoErr("idp_invalid", "discovery document without '%s'", f)
		}
	}
	discoveryCache.Lock()
	discoveryCache.docs[u] = struct {
		at  time.Time
		doc pyjson.Object
	}{time.Now(), doc}
	discoveryCache.Unlock()
	return doc, nil
}

// oidcGet is httpx.get(…).raise_for_status(): a non-2xx answer is an error.
func oidcGet(ctx context.Context, cfg oidcCfg, target, bearer string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	res, err := oidcClient(cfg).Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 {
		return nil, fmt.Errorf("%s for url '%s'", res.Status, target)
	}
	return body, nil
}

// jwks is PyJWKClient: the provider's keys, kept five minutes, read again for an unknown kid.
type jwksEntry struct {
	at   time.Time
	keys map[string]any // kid → public key, the signing ones
}

var jwksCache = struct {
	sync.Mutex
	sets map[string]*jwksEntry
}{sets: map[string]*jwksEntry{}}

func fetchJWKS(ctx context.Context, cfg oidcCfg, uri string) (*jwksEntry, error) {
	body, err := oidcGet(ctx, cfg, uri, "")
	if err != nil {
		return nil, err
	}
	var set struct {
		Keys []json.RawMessage `json:"keys"`
	}
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	e := &jwksEntry{at: time.Now(), keys: map[string]any{}}
	for _, raw := range set.Keys {
		var k struct {
			Kid string `json:"kid"`
			Use string `json:"use"`
		}
		if json.Unmarshal(raw, &k) != nil || k.Kid == "" || (k.Use != "" && k.Use != "sig") {
			continue
		}
		if pub, err := publicKeyOf(raw); err == nil {
			e.keys[k.Kid] = pub
		}
	}
	if len(e.keys) == 0 {
		return nil, errors.New("The JWKS endpoint did not contain any signing keys")
	}
	return e, nil
}

// signingKey is get_signing_key_from_jwt: the key the token's header names.
func signingKey(ctx context.Context, cfg oidcCfg, uri, token string) (any, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("Not enough segments")
	}
	rawHeader, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[0], "="))
	if err != nil {
		return nil, errors.New("Invalid header padding")
	}
	var header struct {
		Kid string `json:"kid"`
	}
	if err := json.Unmarshal(rawHeader, &header); err != nil {
		return nil, errors.New("Invalid header string")
	}
	jwksCache.Lock()
	defer jwksCache.Unlock()
	key := uri + "|" + strconv.FormatFloat(cfg.Timeout, 'g', -1, 64)
	e := jwksCache.sets[key]
	if e == nil || time.Since(e.at) > 300*time.Second {
		if e, err = fetchJWKS(ctx, cfg, uri); err != nil {
			return nil, err
		}
		jwksCache.sets[key] = e
	}
	if k, ok := e.keys[header.Kid]; ok {
		return k, nil
	}
	if e, err = fetchJWKS(ctx, cfg, uri); err != nil {
		return nil, err
	}
	jwksCache.sets[key] = e
	if k, ok := e.keys[header.Kid]; ok {
		return k, nil
	}
	return nil, fmt.Errorf("Unable to find a signing key that matches: %q", header.Kid)
}

// publicKeyOf reads a JWK (RSA or EC).
func publicKeyOf(raw json.RawMessage) (any, error) {
	var k struct {
		Kty, N, E, Crv, X, Y string
	}
	if err := json.Unmarshal(raw, &k); err != nil {
		return nil, err
	}
	switch k.Kty {
	case "RSA":
		return rsaKey(k.N, k.E)
	case "EC":
		return ecKey(k.Crv, k.X, k.Y)
	}
	return nil, fmt.Errorf("unsupported key type %q", k.Kty)
}

// algsOf is the algorithms the document offers among ALLOWED_ALGS, or all of them.
func algsOf(doc pyjson.Object) []string {
	offered, _ := doc.Value("id_token_signing_alg_values_supported").([]any)
	var out []string
	for _, a := range offered {
		for _, ok := range allowedAlgs {
			if a == ok {
				out = append(out, ok)
			}
		}
	}
	if out == nil {
		out = allowedAlgs
	}
	return out
}

// decodeJWT is jwt.decode with the key, the algorithms, the issuer and, given one, the audience.
func decodeJWT(token string, key any, algs []string, issuer, audience string, require []string) (jwt.MapClaims, error) {
	opts := []jwt.ParserOption{jwt.WithValidMethods(algs), jwt.WithLeeway(clockSkew), jwt.WithIssuer(issuer), jwt.WithIssuedAt()}
	if audience != "" {
		opts = append(opts, jwt.WithAudience(audience))
	}
	claims := jwt.MapClaims{}
	if _, err := jwt.NewParser(opts...).ParseWithClaims(token, claims, func(*jwt.Token) (any, error) { return key, nil }); err != nil {
		return nil, err
	}
	for _, r := range require {
		if _, ok := claims[r]; !ok {
			return nil, fmt.Errorf("Token is missing the %q claim", r)
		}
	}
	return claims, nil
}

// ── the person ──────────────────────────────────────────────────────────────

func claimStr(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	b, _ := json.Marshal(v)
	return pyStrOfJSON(v, string(b))
}

// pyStrOfJSON is str() of a decoded JSON value, for the few that are not texts.
func pyStrOfJSON(v any, fallback string) string {
	switch x := v.(type) {
	case bool:
		if x {
			return "True"
		}
		return "False"
	case float64:
		if x == float64(int64(x)) && x < 1e16 && x > -1e16 {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	}
	return fallback
}

// claimAt is sso.claim_at: the value at a dotted path, a key holding dots matched first.
func claimAt(claims any, path string) any {
	m, ok := claims.(map[string]any)
	if !ok {
		return nil
	}
	if v, ok := m[path]; ok {
		return v
	}
	parts := strings.Split(path, ".")
	for i := len(parts) - 1; i > 0; i-- {
		head := strings.Join(parts[:i], ".")
		if v, ok := m[head]; ok {
			if found := claimAt(v, strings.Join(parts[i:], ".")); found != nil {
				return found
			}
		}
	}
	return nil
}

func groupSources(cfg oidcCfg) []string {
	var out []string
	for _, s := range strings.Split(cfg.GroupsClaim, ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// normalizeGroups is sso.normalize_groups.
func normalizeGroups(claims map[string]any, cfg oidcCfg, more []map[string]any) map[string]bool {
	names := map[string]bool{}
	for _, source := range groupSources(cfg) {
		raw := claimAt(claims, source)
		for _, other := range more {
			if raw != nil {
				break
			}
			raw = claimAt(other, source)
		}
		if s, ok := raw.(string); ok {
			raw = []any{s}
		}
		list, ok := raw.([]any)
		if !ok {
			continue
		}
		for _, g := range list {
			names[strings.TrimSpace(strings.TrimLeft(strings.TrimSpace(claimStr(g)), "/"))] = true
		}
	}
	delete(names, "")
	return names
}

func groupsClaimFound(claims map[string]any, cfg oidcCfg, more []map[string]any) bool {
	for _, source := range groupSources(cfg) {
		for _, c := range append([]map[string]any{claims}, more...) {
			if claimAt(c, source) != nil {
				return true
			}
		}
	}
	return false
}

// claimIsTrue is sso.claim_is_true.
func claimIsTrue(v any) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	switch strings.ToLower(strings.TrimSpace(claimStr(v))) {
	case "true", "1", "yes":
		return true
	}
	return false
}

func b64int(s string) (*big.Int, error) {
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "="))
	if err != nil {
		return nil, err
	}
	return new(big.Int).SetBytes(raw), nil
}

func rsaKey(n, e string) (any, error) {
	nn, err := b64int(n)
	if err != nil {
		return nil, err
	}
	ee, err := b64int(e)
	if err != nil {
		return nil, err
	}
	return &rsa.PublicKey{N: nn, E: int(ee.Int64())}, nil
}

func ecKey(crv, x, y string) (any, error) {
	curves := map[string]elliptic.Curve{"P-256": elliptic.P256(), "P-384": elliptic.P384(), "P-521": elliptic.P521()}
	c, ok := curves[crv]
	if !ok {
		return nil, fmt.Errorf("unsupported curve %q", crv)
	}
	xx, err := b64int(x)
	if err != nil {
		return nil, err
	}
	yy, err := b64int(y)
	if err != nil {
		return nil, err
	}
	return &ecdsa.PublicKey{Curve: c, X: xx, Y: yy}, nil
}

func ssoSortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ── the routes ──────────────────────────────────────────────────────────────

func txCookieName(state string) string {
	sum := sha256.Sum256([]byte(state))
	return txCookie + "_" + hex.EncodeToString(sum[:])[:16]
}

func ssoCookiePath() string { return scriptcfg.Get().App.RootPath + "/auth/sso" }

// deleteCookie is Starlette's delete_cookie(name, path): no HttpOnly, no Secure.
func ssoDeleteCookie(w http.ResponseWriter, name string) {
	w.Header().Add("Set-Cookie", name+`=""; expires=`+time.Now().UTC().Format("Mon, 02 Jan 2006 15:04:05 GMT")+
		"; Max-Age=0; Path="+ssoCookiePath()+"; SameSite=lax")
}

func b64url(raw []byte) string { return base64.RawURLEncoding.EncodeToString(raw) }

// quotePlusSSO is urllib's quote_plus, as urlencode writes the authorize request's parameters.
func quotePlusSSO(s string) string { return url.QueryEscape(s) }

// ssoLogin is GET /auth/sso/login (sso_login): state, nonce and PKCE in a signed cookie, the
// person sent to the provider.
func ssoLogin(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		cfg := oidcSettings(ctx, d)
		state, nonce := sessions.TokenURLSafe(32), sessions.TokenURLSafe(32)
		raw := make([]byte, 48)
		_, _ = rand.Read(raw)
		verifier := b64url(raw)
		sum := sha256.Sum256([]byte(verifier))
		challenge := b64url(sum[:])
		doc, err := oidcDiscovery(ctx, cfg)
		var se *ssoError
		if errors.As(err, &se) {
			d.Log.Warn("SSO: authorize cannot be built", "code", se.code, "err", se.message)
			redirectWith(w, cfg.PostLoginURL, "error="+se.code)
			return
		}
		if err != nil {
			fail(d, w, "sso: the discovery", err)
			return
		}
		var scopes []string
		for _, s := range strings.Fields(cfg.Scopes) {
			scopes = append(scopes, s)
		}
		params := [][2]string{{"response_type", "code"}, {"client_id", cfg.ClientID}, {"redirect_uri", cfg.RedirectURI},
			{"scope", strings.Join(scopes, " ")}, {"state", state}, {"nonce", nonce}, {"code_challenge", challenge},
			{"code_challenge_method", "S256"}}
		var q []string
		for _, p := range params {
			q = append(q, quotePlusSSO(p[0])+"="+quotePlusSSO(p[1]))
		}
		endpoint := pyjson.Str(doc.Value("authorization_endpoint"))
		sep := "?"
		if strings.Contains(endpoint, "?") {
			sep = "&"
		}
		now := time.Now()
		tx, err := jwt.NewWithClaims(jwt.GetSigningMethod(d.Config.JWTAlgorithm), jwt.MapClaims{"typ": txTokenType, "state": state,
			"nonce": nonce, "cv": verifier, "next": "", "iat": now.Unix(), "exp": now.Add(time.Duration(cfg.LoginTxTTL) * time.Second).Unix()}).
			SignedString(d.Config.JWTSecret)
		if err != nil {
			fail(d, w, "sso: the transaction", err)
			return
		}
		cookie := fmt.Sprintf("%s=%s; HttpOnly; Max-Age=%d; Path=%s; SameSite=lax", txCookieName(state), tx, cfg.LoginTxTTL, ssoCookiePath())
		if auth.InHTTPS(r) {
			cookie += "; Secure"
		}
		w.Header().Add("Set-Cookie", cookie)
		redirect(w, endpoint+sep+strings.Join(q, "&"), http.StatusSeeOther)
	}
}

// ssoFail is _fail: the whole story in the log, a code to the person.
func ssoFail(d *Deps, w http.ResponseWriter, r *http.Request, cfg oidcCfg, err *ssoError, email, state string) {
	d.Log.Warn("SSO failed", "code", err.code, "err", err.message)
	label := email
	if label == "" {
		label = "sso"
	}
	audit.Record(r.Context(), d.DB, d.Log, audit.Event{ActorLabel: label, Action: "auth.sso_login_failed", Outcome: "failure",
		Detail: pyjson.Object{{Key: "reason", Value: err.code}}})
	if state != "" {
		ssoDeleteCookie(w, txCookieName(state))
	}
	redirectWith(w, cfg.PostLoginURL, "error="+err.code)
}

// ssoCallback is GET /auth/sso/callback (sso_callback): checks, provisions, synchronizes the
// groups and opens an internal session.
func ssoCallback(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		cfg := oidcSettings(ctx, d)
		var ps pyd.Params
		q := pyd.ParseQuery(r.URL.RawQuery)
		code, _ := ps.Query(q, "code", pyd.Str(), "").(string)
		state, _ := ps.Query(q, "state", pyd.Str(), "").(string)
		idpError, _ := ps.Query(q, "error", pyd.Str(), "").(string)
		description, _ := ps.Query(q, "error_description", pyd.Str(), "").(string)
		if !validated(w, &ps) {
			return
		}
		if idpError != "" {
			ssoFail(d, w, r, cfg, ssoErr("idp_denied", "IdP: %s %s", idpError, description), "", state)
			return
		}
		if code == "" {
			ssoFail(d, w, r, cfg, ssoErr("no_code", "callback without authorization code"), "", state)
			return
		}
		var txText string
		if state != "" {
			if c, err := r.Cookie(txCookieName(state)); err == nil {
				txText = c.Value
			}
		}
		if txText == "" {
			if c, err := r.Cookie(txCookie); err == nil {
				txText = c.Value
			}
		}
		if txText == "" {
			ssoFail(d, w, r, cfg, ssoErr("tx_missing", "no transaction cookie came back with the callback"), "", state)
			return
		}
		user, detail, err := ssoLoginUser(ctx, d, r, cfg, code, txText, state)
		if err != nil {
			var se *ssoError
			if !errors.As(err, &se) {
				d.Log.Error("SSO: unexpected error in the callback", "err", err)
				se = &ssoError{err.Error(), "sso_failed"}
			}
			ssoFail(d, w, r, cfg, se, "", state)
			return
		}
		var kept pyjson.Object
		for _, kv := range detail {
			switch v := kv.Value.(type) {
			case []any:
				if len(v) == 0 {
					continue
				}
			case bool:
				if !v {
					continue
				}
			}
			kept = append(kept, kv)
		}
		audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: user, Action: "auth.sso_login", Detail: kept})
		if err := closeTheCookiesSession(d, r); err != nil {
			fail(d, w, "sso: closing the previous session", err)
			return
		}
		s, token, err := d.opener().Open(ctx, user.ID, reqinfo.From(ctx).ClientIP, r.Header.Get("User-Agent"), "sso", nil)
		if errors.Is(err, sessions.ErrSuperseded) {
			ssoFail(d, w, r, cfg, ssoErr("sso_failed", "user disabled during the sign-in"), user.Email, state)
			return
		}
		if err != nil {
			fail(d, w, "sso: opening the session", err)
			return
		}
		w.Header().Add("Set-Cookie", sessions.CookieOf(r, d.Config.CookieDomain, token, s.ExpiresAt))
		ssoDeleteCookie(w, txCookieName(state))
		ssoDeleteCookie(w, txCookie)
		redirect(w, cfg.PostLoginURL, http.StatusSeeOther)
	}
}

// readTx is read_tx_token: the transaction cookie checked, and the state.
func readTx(d *Deps, text, state string) (jwt.MapClaims, error) {
	claims := jwt.MapClaims{}
	_, err := jwt.NewParser(jwt.WithValidMethods([]string{d.Config.JWTAlgorithm}), jwt.WithLeeway(clockSkew), jwt.WithIssuedAt()).
		ParseWithClaims(text, claims, func(*jwt.Token) (any, error) { return d.Config.JWTSecret, nil })
	if err != nil {
		return nil, ssoErr("tx_expired", "transaction cookie invalid or expired: %v", err)
	}
	if claims["typ"] != txTokenType {
		return nil, ssoErr("tx_invalid", "transaction cookie of the wrong type")
	}
	if subtle.ConstantTimeCompare([]byte(claimStr(orEmpty(claims["state"]))), []byte(state)) != 1 {
		return nil, ssoErr("state_mismatch", "state does not match (possible CSRF on the callback)")
	}
	return claims, nil
}

func orEmpty(v any) any {
	if v == nil {
		return ""
	}
	return v
}

// ssoLoginUser is sso.login_user: code → token → validation → the person synchronized.
func ssoLoginUser(ctx context.Context, d *Deps, r *http.Request, cfg oidcCfg, code, txText, state string) (*auth.User, pyjson.Object, error) {
	tx, err := readTx(d, txText, state)
	if err != nil {
		return nil, nil, err
	}
	doc, err := oidcDiscovery(ctx, cfg)
	if err != nil {
		return nil, nil, err
	}
	// exchange_code: the client secret travels only here, server to server
	tokenEndpoint := pyjson.Str(doc.Value("token_endpoint"))
	form := [][2]string{{"grant_type", "authorization_code"}, {"code", code}, {"redirect_uri", cfg.RedirectURI},
		{"client_id", cfg.ClientID}, {"client_secret", cfg.ClientSecret}, {"code_verifier", claimStr(tx["cv"])}}
	var parts []string
	for _, p := range form {
		parts = append(parts, quotePlusSSO(p[0])+"="+quotePlusSSO(p[1]))
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenEndpoint, strings.NewReader(strings.Join(parts, "&")))
	if err != nil {
		return nil, nil, unreachable("token endpoint unreachable", tokenEndpoint, err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := oidcClient(cfg).Do(req)
	if err != nil {
		return nil, nil, unreachable("token endpoint unreachable", tokenEndpoint, err)
	}
	body, err := io.ReadAll(res.Body)
	res.Body.Close()
	if err != nil {
		return nil, nil, unreachable("token endpoint unreachable", tokenEndpoint, err)
	}
	if res.StatusCode != 200 {
		return nil, nil, ssoErr("code_rejected", "token exchange refused (%d)", res.StatusCode)
	}
	var tokens map[string]any
	if err := json.Unmarshal(body, &tokens); err != nil {
		return nil, nil, err // a ValueError in Python: sso_failed
	}
	idToken, _ := tokens["id_token"].(string)
	if !pyjson.Truthy(tokens["id_token"]) {
		return nil, nil, ssoErr("idp_invalid", "token endpoint response without id_token")
	}
	// validate_id_token
	jwksURI := pyjson.Str(doc.Value("jwks_uri"))
	key, err := signingKey(ctx, cfg, jwksURI, idToken)
	if err != nil {
		if untrusted(err) {
			return nil, nil, unreachable("signing keys not readable", jwksURI, err)
		}
		return nil, nil, ssoErr("jwks_error", "signing key not retrievable from the JWKS: %v", err)
	}
	issuer := pyjson.Str(doc.Value("issuer"))
	claims, err := decodeJWT(idToken, key, algsOf(doc), issuer, cfg.ClientID, []string{"exp", "iat", "iss", "aud", "sub"})
	if err != nil {
		return nil, nil, ssoErr("token_invalid", "invalid id_token: %v", err)
	}
	if subtle.ConstantTimeCompare([]byte(claimStr(orEmpty(claims["nonce"]))), []byte(claimStr(tx["nonce"]))) != 1 {
		return nil, nil, ssoErr("nonce_mismatch", "nonce does not match in the id_token")
	}
	if b, ok := claims["email_verified"].(bool); ok && !b {
		return nil, nil, ssoErr("email_unverified", "email not verified by the IdP")
	}
	more := moreGroupClaims(ctx, cfg, doc, claims, tokens)
	return provisionAndSync(ctx, d, cfg, claims, normalizeGroups(claims, cfg, more), groupsClaimFound(claims, cfg, more))
}

// moreGroupClaims is sso.more_group_claims: the access token's claims, then userinfo.
func moreGroupClaims(ctx context.Context, cfg oidcCfg, doc pyjson.Object, claims map[string]any, tokens map[string]any) []map[string]any {
	var missing []string
	for _, s := range groupSources(cfg) {
		if claimAt(claims, s) == nil {
			missing = append(missing, s)
		}
	}
	access, _ := tokens["access_token"].(string)
	if len(missing) == 0 || !pyjson.Truthy(tokens["access_token"]) {
		return nil
	}
	var found []map[string]any
	subject := claimStr(claims["sub"])
	if key, err := signingKey(ctx, cfg, pyjson.Str(doc.Value("jwks_uri")), access); err == nil {
		if at, err := decodeJWT(access, key, algsOf(doc), pyjson.Str(doc.Value("issuer")), "", []string{"exp", "iss", "sub"}); err == nil &&
			claimStr(at["sub"]) == subject {
			found = append(found, at)
		}
	}
	needInfo := false
	for _, s := range missing {
		all := true
		for _, c := range found {
			if claimAt(c, s) != nil {
				all = false
			}
		}
		if all {
			needInfo = true
		}
	}
	if needInfo {
		if endpoint := doc.Value("userinfo_endpoint"); pyjson.Truthy(endpoint) {
			if body, err := oidcGet(ctx, cfg, pyjson.Str(endpoint), access); err == nil {
				var info map[string]any
				if json.Unmarshal(body, &info) == nil && info != nil && claimStr(info["sub"]) == subject {
					found = append(found, info)
				}
			}
		}
	}
	return found
}

type ssoGroup struct {
	id              int64
	name            string
	admin, observer bool
}

// provisionAndSync is sso.provision_and_sync.
func provisionAndSync(ctx context.Context, d *Deps, cfg oidcCfg, claims map[string]any, asserted map[string]bool,
	groupsKnown bool) (*auth.User, pyjson.Object, error) {
	issuer, subject := strings.TrimSpace(claimStr(claims["iss"])), strings.TrimSpace(claimStr(claims["sub"]))
	if issuer == "" || subject == "" {
		return nil, nil, ssoErr("no_identity", "the id_token contains no iss/sub")
	}
	email := ""
	for _, f := range []string{"email", "preferred_username", "upn"} {
		if v := strings.TrimSpace(claimStr(claims[f])); strings.Contains(v, "@") {
			email = strings.ToLower(v)
			break
		}
	}
	if email == "" {
		return nil, nil, ssoErr("no_email", "the id_token contains no email (nor UPN)")
	}
	// ensure_admitted
	if domains := cfg.allowedDomains(); len(domains) > 0 {
		domain := strings.ToLower(email[strings.LastIndex(email, "@")+1:])
		if !domains[domain] {
			return nil, nil, ssoErr("not_allowed", "domain %s is not allowed to sign in", domain)
		}
	}
	allow := cfg.allowlist()
	if cfg.RequireAllowlisted {
		allowed := map[string]bool{}
		for k := range allow {
			allowed[k] = true
		}
		if cfg.SuperuserGroup != "" {
			allowed[cfg.SuperuserGroup] = true
		}
		if len(allowed) == 0 {
			return nil, nil, ssoErr("config_error", "OIDC__REQUIRE_ALLOWLISTED_GROUP is on but OIDC__GROUP_ALLOWLIST is empty")
		}
		in := false
		for g := range asserted {
			if allowed[g] {
				in = true
			}
		}
		if !in {
			return nil, nil, ssoErr("not_allowed", "%s does not belong to any allowed group", email)
		}
	}
	tx, err := d.DB.Begin(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	type urow struct {
		id                         int64
		email, fullName            string
		active, superuser, service bool
		subject                    *string
	}
	scan := func(row pgx.Row) (*urow, error) {
		var u urow
		err := row.Scan(&u.id, &u.email, &u.fullName, &u.active, &u.superuser, &u.service, &u.subject)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return &u, err
	}
	const cols = `id, email, full_name, is_active, is_superuser, is_service, oidc_subject`
	u, err := scan(tx.QueryRow(ctx, `SELECT `+cols+` FROM users WHERE oidc_issuer = $1 AND oidc_subject = $2 LIMIT 1`, issuer, subject))
	if err != nil {
		return nil, nil, err
	}
	created, linked := false, false
	if u == nil {
		same, err := scan(tx.QueryRow(ctx, `SELECT `+cols+` FROM users WHERE email = $1 LIMIT 1`, email))
		if err != nil {
			return nil, nil, err
		}
		switch {
		case same == nil:
			name := claimStr(claims["name"])
			if !pyjson.Truthy(claims["name"]) {
				name = claimStr(claims["given_name"])
				if !pyjson.Truthy(claims["given_name"]) {
					name = ""
				}
			}
			if u, err = scan(tx.QueryRow(ctx, `INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer,
				is_service, created_at, oidc_issuer, oidc_subject) VALUES ($1, $2, NULL, true, false, false, false, $3, $4, $5) RETURNING `+cols,
				email, strings.TrimSpace(name), nowNaive(), issuer, subject)); err != nil {
				return nil, nil, err
			}
			created = true
		case !same.active:
			return nil, nil, ssoErr("user_disabled", "user %s is deactivated in Tabularia", email)
		case same.service:
			return nil, nil, ssoErr("identity_conflict", "%s is a service account", email)
		case same.subject != nil && *same.subject != "":
			return nil, nil, ssoErr("identity_conflict", "the account %s is already linked to another IdP identity", email)
		case !claimIsTrue(claims["email_verified"]):
			return nil, nil, ssoErr("email_unverified", "the IdP does not declare the email %s verified", email)
		default:
			if _, err := tx.Exec(ctx, `UPDATE users SET oidc_issuer = $1, oidc_subject = $2 WHERE id = $3`, issuer, subject, same.id); err != nil {
				return nil, nil, err
			}
			u, linked = same, true
		}
	}
	if !u.active {
		return nil, nil, ssoErr("user_disabled", "user %s is deactivated in Tabularia", u.email)
	}
	renamed := pyjson.Object{}
	if u.email != email {
		var other int64
		err := tx.QueryRow(ctx, `SELECT id FROM users WHERE email = $1 AND id != $2 LIMIT 1`, email, u.id).Scan(&other)
		if errors.Is(err, pgx.ErrNoRows) {
			renamed = append(renamed, pyjson.KV{Key: "email", Value: pyjson.Object{{Key: "from", Value: u.email}, {Key: "to", Value: email}}})
			u.email = email
		} else if err != nil {
			return nil, nil, err
		}
	}
	if pyjson.Truthy(claims["name"]) && u.fullName == "" {
		name := strings.TrimSpace(claimStr(claims["name"]))
		if name != u.fullName && !created {
			renamed = append(renamed, pyjson.KV{Key: "full_name", Value: pyjson.Object{{Key: "from", Value: u.fullName}, {Key: "to", Value: name}}})
		}
		u.fullName = name
	}
	idpGroups := map[string]bool{}
	for g := range asserted {
		idpGroups[g] = true
	}
	superuserBefore := u.superuser
	if !groupsKnown {
		d.Log.Warn("SSO: none of the group claims is in the token: read as no groups", "claims", cfg.GroupsClaim, "email", email)
	}
	if cfg.SuperuserGroup != "" {
		u.superuser = idpGroups[cfg.SuperuserGroup]
		delete(idpGroups, cfg.SuperuserGroup)
	}
	if len(allow) > 0 {
		for g := range idpGroups {
			if !allow[g] {
				delete(idpGroups, g)
			}
		}
	}
	if _, err := tx.Exec(ctx, `UPDATE users SET email = $1, full_name = $2, is_superuser = $3 WHERE id = $4`,
		u.email, u.fullName, u.superuser, u.id); err != nil {
		return nil, nil, err
	}
	// _group_ids
	wanted := map[int64]bool{}
	var groupsCreated []ssoGroup
	if len(idpGroups) > 0 {
		names := ssoSortedKeys(idpGroups)
		rows, err := tx.Query(ctx, `SELECT id, name FROM groups WHERE name = ANY($1)`, names)
		if err != nil {
			return nil, nil, err
		}
		existing := map[string]bool{}
		for rows.Next() {
			var id int64
			var name string
			if err := rows.Scan(&id, &name); err != nil {
				rows.Close()
				return nil, nil, err
			}
			wanted[id], existing[name] = true, true
		}
		rows.Close()
		if cfg.AutoCreateGroups {
			for _, name := range names {
				if existing[name] {
					continue
				}
				var id int64
				if err := tx.QueryRow(ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at)
					VALUES ($1, 'created by SSO at the first login', false, false, $2) RETURNING id`, name, nowNaive()).Scan(&id); err != nil {
					return nil, nil, err
				}
				wanted[id] = true
				groupsCreated = append(groupsCreated, ssoGroup{id: id, name: name})
			}
		}
	}
	managed := map[int64]bool{}
	rows, err := tx.Query(ctx, `SELECT id, name, is_admin FROM groups WHERE is_admin OR is_observer`)
	if err != nil {
		return nil, nil, err
	}
	for rows.Next() {
		var id int64
		var name string
		var admin bool
		if err := rows.Scan(&id, &name, &admin); err != nil {
			rows.Close()
			return nil, nil, err
		}
		if admin || !allow[name] {
			managed[id] = true
		}
	}
	rows.Close()
	for id := range managed {
		delete(wanted, id)
	}
	current := map[int64]bool{}
	rows, err = tx.Query(ctx, `SELECT group_id FROM user_groups WHERE user_id = $1`, u.id)
	if err != nil {
		return nil, nil, err
	}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, nil, err
		}
		if !managed[id] {
			current[id] = true
		}
	}
	rows.Close()
	added, removed := map[int64]bool{}, map[int64]bool{}
	for id := range wanted {
		if !current[id] {
			added[id] = true
			if _, err := tx.Exec(ctx, `INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, u.id, id); err != nil {
				return nil, nil, err
			}
		}
	}
	if cfg.Authoritative {
		for id := range current {
			if !wanted[id] {
				removed[id] = true
				if _, err := tx.Exec(ctx, `DELETE FROM user_groups WHERE user_id = $1 AND group_id = $2`, u.id, id); err != nil {
					return nil, nil, err
				}
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, nil, err
	}
	actor, err := ssoActor(ctx, d, u.id)
	if err != nil {
		return nil, nil, err
	}
	// _record_changes: the sign-in's changes with the panel's actions, "via": "sso"
	via := pyjson.KV{Key: "via", Value: "sso"}
	userType, groupType := "user", "group"
	write := func(action string, detail pyjson.Object, g *ssoGroup) {
		e := audit.Event{Actor: actor, Action: action, Detail: append(detail, via)}
		if g != nil {
			e.TargetType, e.TargetID, e.TargetLabel = &groupType, &g.id, &g.name
		} else {
			e.TargetType, e.TargetID, e.TargetLabel = &userType, &actor.ID, &actor.Email
		}
		audit.Record(ctx, d.DB, d.Log, e)
	}
	if created {
		write("user.create", pyjson.Object{}, nil)
	}
	if len(renamed) > 0 {
		write("user.update", renamed, nil)
	}
	for i := range groupsCreated {
		write("group.create", pyjson.Object{}, &groupsCreated[i])
	}
	for _, change := range []struct {
		action string
		ids    map[int64]bool
	}{{"group.member_add", added}, {"group.member_remove", removed}} {
		if len(change.ids) == 0 {
			continue
		}
		groups, err := ssoGroupsByName(ctx, d, change.ids)
		if err != nil {
			return nil, nil, err
		}
		for _, g := range groups {
			// the target is the person (Python's write() without a group)
			write(change.action, membershipDetail(g.name, g.id, g.admin, g.observer), nil)
		}
	}
	if actor.IsSuperuser != superuserBefore {
		action, detail := "user.demote", pyjson.Object{}
		if actor.IsSuperuser {
			action = "user.promote"
		}
		if created {
			detail = pyjson.Object{{Key: "at_creation", Value: true}}
		}
		write(action, detail, nil)
	}
	personalfolders.Ensure(ctx, d.DB, d.Log, actor.ID, actor, "", "sso")
	names := func(ids map[int64]bool) ([]any, error) {
		out := []any{}
		if len(ids) == 0 {
			return out, nil
		}
		groups, err := ssoGroupsByName(ctx, d, ids)
		for _, g := range groups {
			out = append(out, g.name)
		}
		return out, err
	}
	addedNames, err := names(added)
	if err != nil {
		return nil, nil, err
	}
	removedNames, err := names(removed)
	if err != nil {
		return nil, nil, err
	}
	sortedGroups := []any{}
	for _, g := range ssoSortedKeys(idpGroups) {
		sortedGroups = append(sortedGroups, g)
	}
	detail := pyjson.Object{{Key: "created", Value: created}, {Key: "linked", Value: linked}, {Key: "groups_added", Value: addedNames},
		{Key: "groups_removed", Value: removedNames}, {Key: "groups", Value: sortedGroups}, {Key: "is_superuser", Value: actor.IsSuperuser}}
	if actor.IsSuperuser != superuserBefore {
		detail = append(detail, pyjson.KV{Key: "superuser_changed", Value: true})
	}
	if !groupsKnown {
		detail = append(detail, pyjson.KV{Key: "groups_claim_missing", Value: cfg.GroupsClaim})
	}
	return actor, detail, nil
}

// ssoGroupsByName reads the groups, by name (Python's order_by(Group.name)).
func ssoGroupsByName(ctx context.Context, d *Deps, ids map[int64]bool) ([]ssoGroup, error) {
	list := make([]int64, 0, len(ids))
	for id := range ids {
		list = append(list, id)
	}
	rows, err := d.DB.Query(ctx, `SELECT id, name, is_admin, is_observer FROM groups WHERE id = ANY($1) ORDER BY name`, list)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ssoGroup
	for rows.Next() {
		var g ssoGroup
		if err := rows.Scan(&g.id, &g.name, &g.admin, &g.observer); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// ssoActor is the person as the audit takes them.
func ssoActor(ctx context.Context, d *Deps, id int64) (*auth.User, error) {
	u, err := scanUserRow(d.DB.QueryRow(ctx, `SELECT `+userRowColumns+` FROM users WHERE id = $1`, id))
	if err != nil {
		return nil, err
	}
	return &u.User, nil
}
