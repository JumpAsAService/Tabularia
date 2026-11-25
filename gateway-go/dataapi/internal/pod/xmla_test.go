package pod

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// The XMLA route, piece 1 (docs/specs/xmla.md): the admission every route shares, Basic, and the
// two answers it gives (DISCOVER_PROPERTIES, and a fault for the rest).

const discoverProperties = `<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
<RequestType>DISCOVER_PROPERTIES</RequestType><Restrictions><RestrictionList/></Restrictions>
<Properties><PropertyList><Catalog>CRM</Catalog></PropertyList></Properties>
</Discover></soap:Body></soap:Envelope>`

const executeStatement = `<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>SELECT FROM [customers]</Statement></Command>
<Properties><PropertyList><Catalog>CRM</Catalog></PropertyList></Properties></Execute></soap:Body></soap:Envelope>`

// withXMLA turns the interface on in the fixture's manifest, with the caps an installation gives
func (f *fixture) withXMLA() {
	f.m.XMLA = &dataapiwire.XMLA{MaxCells: 1000, MaxTuples: 100, MaxMembers: 50}
	f.s.Apply(f.m)
}

// post sends an XMLA request; the key as the Bearer, or as the Basic password with `basic`
func (f *fixture) post(body, key string, edit ...func(*http.Request)) answer {
	f.t.Helper()
	r := httptest.NewRequest(http.MethodPost, "/data/crm/xmla", strings.NewReader(body))
	r.RemoteAddr = "198.51.100.7:4321"
	r.Header.Set("Content-Type", "text/xml")
	if secret, ok := f.keys[key]; ok {
		key = secret
	}
	if key != "" {
		r.Header.Set("Authorization", "Bearer "+key)
	}
	for _, e := range edit {
		e(r)
	}
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	a := answer{status: w.Code, header: w.Header(), body: w.Body.Bytes()}
	if strings.HasPrefix(w.Header().Get("Content-Type"), "application/json") {
		a.json = map[string]any{}
		_ = json.Unmarshal(a.body, &a.json)
	}
	return a
}

func basic(user, password string) func(*http.Request) {
	return func(r *http.Request) {
		r.Header.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(user+":"+password)))
	}
}

// behindTLS is a request that came through the trusted proxy, which ended its TLS
func behindTLS(r *http.Request) {
	r.RemoteAddr = "10.0.0.5:1"
	r.Header.Set("X-Forwarded-Proto", "https")
}

func TestXMLAIsOffUnlessTheManifestSaysSo(t *testing.T) {
	f := newFixture(t)
	f.s.Apply(f.m)
	// off: 404, after the key (a stranger learns nothing)
	f.want(f.post(discoverProperties, "main"), 404, "xmla_off")
	f.want(f.post(discoverProperties, ""), 401, "key_missing")
	f.withXMLA()
	if a := f.post(discoverProperties, "main"); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// the manifest after it takes it away again
	f.m.XMLA = nil
	f.s.Apply(f.m)
	f.want(f.post(discoverProperties, "main"), 404, "xmla_off")
}

func TestXMLAMethods(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	for _, m := range []string{http.MethodGet, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		r := httptest.NewRequest(m, "/data/crm/xmla", nil)
		r.Header.Set("Authorization", "Bearer "+f.keys["main"])
		w := httptest.NewRecorder()
		f.h.ServeHTTP(w, r)
		if w.Code != 405 || w.Header().Get("Allow") != "POST" {
			t.Errorf("%s: %d %v", m, w.Code, w.Header())
		}
	}
	// the other routes stay GET only: the exception is this route's
	r := httptest.NewRequest(http.MethodPost, "/data/crm/customers", strings.NewReader("{}"))
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	if w.Code != 405 || w.Header().Get("Allow") != "GET" {
		t.Errorf("POST on a resource: %d %v", w.Code, w.Header())
	}
	r = httptest.NewRequest(http.MethodPost, "/data/crm/openapi.json", strings.NewReader("{}"))
	w = httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	if w.Code != 405 {
		t.Errorf("POST on the description: %d", w.Code)
	}
}

func TestXMLAAsksForBasic(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	a := f.post(discoverProperties, "")
	f.want(a, 401, "key_missing")
	// the realm is the slug, which the caller knows from the address: the name of the data API is
	// not told to someone who has no key
	if got := a.header.Get("WWW-Authenticate"); got != `Basic realm="crm"` {
		t.Errorf("challenge: %q", got)
	}
	// a wrong key is challenged as well: the client asks the person again
	a = f.post(discoverProperties, "tdk_short")
	f.want(a, 401, "key_invalid")
	if got := a.header.Get("WWW-Authenticate"); got != `Basic realm="crm"` {
		t.Errorf("challenge: %q", got)
	}
	// the REST routes keep their Bearer challenge
	if got := f.get("/data/crm/customers?id=1", "").header.Get("WWW-Authenticate"); !strings.HasPrefix(got, "Bearer") {
		t.Errorf("REST challenge: %q", got)
	}
	// whatever the name is, it is not in the challenge
	f.m.Name = `The "CRM" \ data`
	f.s.Apply(f.m)
	if got := f.post(discoverProperties, "").header.Get("WWW-Authenticate"); got != `Basic realm="crm"` || strings.Contains(got, "CRM") {
		t.Errorf("the name in the realm: %q", got)
	}
}

func TestXMLABasicOnlyOverHTTPS(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	f.s.clientIP = clientIPFor([]string{"10.0.0.0/8"})
	// the password is the key, the user name whatever the client says
	a := f.post(discoverProperties, "", basic("anyone", f.keys["main"]), behindTLS)
	if a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	if a := f.post(discoverProperties, "", basic("", f.keys["main"]), behindTLS); a.status != 200 {
		t.Fatalf("empty user: %d %s", a.status, a.body)
	}
	// plain HTTP, or a forwarded protocol from a peer that is not a trusted proxy: refused
	a = f.post(discoverProperties, "", basic("x", f.keys["main"]))
	f.want(a, 403, "https_required")
	untrusted := func(r *http.Request) { r.Header.Set("X-Forwarded-Proto", "https") }
	f.want(f.post(discoverProperties, "", basic("x", f.keys["main"]), untrusted), 403, "https_required")
	// the Bearer key keeps working over HTTP (inside the cluster)
	if a := f.post(discoverProperties, "main"); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// a wrong password is a wrong key, a revoked one a revoked key
	f.want(f.post(discoverProperties, "", basic("x", "tdk_nope"), behindTLS), 401, "key_invalid")
	f.want(f.post(discoverProperties, "", basic("x", f.keys["revoked"]), behindTLS), 401, "key_revoked")
}

func TestXMLAKeepsTheRulesOfEveryKey(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	// the key's address
	f.want(f.post(discoverProperties, "cidr"), 403, "address_not_allowed")
	from := func(r *http.Request) { r.RemoteAddr = "203.0.113.9:1" }
	if a := f.post(discoverProperties, "cidr", from); a.status != 200 {
		t.Fatalf("%d %s", a.status, a.body)
	}
	// its rate: two requests, then the third waits
	if a := f.post(discoverProperties, "limited"); a.status != 200 {
		t.Fatal(a.status)
	}
	f.post(discoverProperties, "limited")
	a := f.post(discoverProperties, "limited")
	f.want(a, 429, "rate_limited")
	if a.header.Get("Retry-After") != "30" {
		t.Errorf("%v", a.header)
	}
	// its state
	f.want(f.post(discoverProperties, "expired"), 401, "key_expired")
	// the state of the data API
	f.m.State = "paused"
	f.s.Apply(f.m)
	f.want(f.post(discoverProperties, "main"), 503, "api_paused")
	f.m.State = "active"
	f.s.Apply(f.m)
	// another data API's address
	r := httptest.NewRequest(http.MethodPost, "/data/erp/xmla", strings.NewReader(discoverProperties))
	r.Header.Set("Authorization", "Bearer "+f.keys["main"])
	w := httptest.NewRecorder()
	f.h.ServeHTTP(w, r)
	if w.Code != 404 {
		t.Errorf("another API: %d", w.Code)
	}
	// every request is counted for its key, as the REST ones are
	if got := f.s.usage.Take(); len(got) == 0 {
		t.Error("no usage counted")
	}
}

func TestXMLADiscoverProperties(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	a := f.post(discoverProperties, "main")
	if a.status != 200 || !strings.HasPrefix(a.header.Get("Content-Type"), "text/xml") {
		t.Fatalf("%d %v %s", a.status, a.header, a.body)
	}
	body := string(a.body)
	for _, want := range []string{
		`<DiscoverResponse xmlns="urn:schemas-microsoft-com:xml-analysis">`,
		`<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset"`,
		`<PropertyName>ProviderName</PropertyName>`, `<PropertyName>ProviderVersion</PropertyName>`,
		`<PropertyName>MdpropMdxSubqueries</PropertyName>`, `<PropertyName>DbpropMsmdSubqueries</PropertyName>`,
		`<PropertyName>Catalog</PropertyName><PropertyDescription>The catalog: the data API&#39;s name</PropertyDescription>`,
		`<Value>CRM</Value>`, `sql:field="PropertyAccessType"`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %s in %s", want, body)
		}
	}
	// narrowed by its restriction
	narrowed := strings.Replace(discoverProperties, `<RestrictionList/>`,
		`<RestrictionList><PropertyName>ProviderName</PropertyName></RestrictionList>`, 1)
	body = string(f.post(narrowed, "main").body)
	if !strings.Contains(body, "<PropertyName>ProviderName</PropertyName>") || strings.Contains(body, "<PropertyName>Catalog</PropertyName>") {
		t.Errorf("not narrowed: %s", body)
	}
}

func TestXMLAFaultsNameWhatIsNotServed(t *testing.T) {
	f := newFixture(t)
	f.withXMLA()
	fault := func(body string, want string) {
		t.Helper()
		a := f.post(body, "main")
		if a.status != 500 || !strings.HasPrefix(a.header.Get("Content-Type"), "text/xml") {
			t.Fatalf("%d %v %s", a.status, a.header, a.body)
		}
		if !strings.Contains(string(a.body), "<soap:Fault>") || !strings.Contains(string(a.body), want) {
			t.Errorf("want %q in %s", want, a.body)
		}
	}
	fault(strings.Replace(discoverProperties, "DISCOVER_PROPERTIES", "DISCOVER_NOPE", 1), "The rowset «DISCOVER_NOPE» is not served by this interface")
	fault(executeStatement, "still loading")
	fault("<not xml", "not well-formed")
	fault(`<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body/></soap:Envelope>`, "SOAP 1.2")
	fault(`<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><Other/></soap:Body></soap:Envelope>`, "not a Discover or an Execute")
	// a request too large is refused before it is read
	big := strings.Repeat("x", maxXMLARequest+10)
	a := f.post(big, "main")
	f.want(a, 413, "request_too_large")
}

func TestKeyOfReadsBasic(t *testing.T) {
	key, _ := dataapiwire.Random(dataapiwire.KeyPrefix)
	of := func(header string) string {
		r := httptest.NewRequest(http.MethodPost, "/x", nil)
		if header != "" {
			r.Header.Set("Authorization", header)
		}
		return KeyOf(r)
	}
	enc := func(s string) string { return "Basic " + base64.StdEncoding.EncodeToString([]byte(s)) }
	for _, c := range []struct{ header, want string }{
		{"Bearer " + key, key},
		{"bearer " + key, key},
		{enc("name:" + key), key},
		{enc(":" + key), key},
		{"basic " + base64.StdEncoding.EncodeToString([]byte("a:"+key)), key},
		{enc("a:b:" + key), "b:" + key}, // the user name ends at the first colon
		{enc(key), ""},                  // no colon: no password
		{"Basic !!!not-base64", ""},
		{"Basic " + key, ""},
		{"Digest abc", ""},
		{"", ""},
	} {
		if got := of(c.header); got != c.want {
			t.Errorf("%q: got %q, want %q", c.header, got, c.want)
		}
	}
	r := httptest.NewRequest(http.MethodPost, "/x", nil)
	r.Header.Set("X-API-Key", " "+key+" ")
	if KeyOf(r) != key || UsesBasic(r) {
		t.Error("X-API-Key")
	}
	r.Header.Set("Authorization", enc("u:"+key))
	if !UsesBasic(r) {
		t.Error("UsesBasic")
	}
}
