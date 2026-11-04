// Package apidocs is the API's documentation: GET /openapi.json (FastAPI's schema of the Python
// gateway, openapi.json, written by gen_openapi.py in the gateway's image) and core/api_docs.py's
// pages, /docs and /redoc, with pinned, hash-checked scripts and a Content-Security-Policy.
package apidocs

import (
	"crypto/sha256"
	_ "embed"
	"encoding/base64"
	"html"
	"net/http"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

//go:embed openapi.json
var schema string

// cdn and the scripts, as core/api_docs.py pins them.
const cdn = "https://cdn.jsdelivr.net/npm"

var (
	swaggerJS  = [2]string{cdn + "/swagger-ui-dist@5.33.0/swagger-ui-bundle.js", "sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf"}
	swaggerCSS = [2]string{cdn + "/swagger-ui-dist@5.33.0/swagger-ui.css", "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW"}
	redocJS    = [2]string{cdn + "/redoc@2.5.4/bundles/redoc.standalone.js", "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5"}
)

// Docs serves the pages for an application named `name` (APP__NAME), at `version`
// (APP__VERSION), behind `rootPath` (APP__ROOT_PATH as its validator writes it).
type Docs struct {
	title    string // FastAPI's title: «<name> — Gateway»
	rootPath string
	schema   []byte
}

// New builds the schema once: the placeholders filled, the root path in `servers` as
// FastAPI's openapi() puts it (a key added at the end).
func New(name, version, rootPath string) *Docs {
	s := strings.Replace(schema, "@@APP_NAME@@", jsonInner(name), 1)
	s = strings.Replace(s, "@@APP_VERSION@@", jsonInner(version), 1)
	root := strings.TrimRight(rootPath, "/")
	if root != "" {
		s = strings.TrimSuffix(s, "}") + `,"servers":[{"url":"` + jsonInner(root) + `"}]}`
	}
	return &Docs{title: name + " — Gateway", rootPath: root, schema: []byte(s)}
}

// jsonInner is a text as json.dumps(ensure_ascii=False) writes it, without its quotes.
func jsonInner(s string) string {
	out, _ := pyjson.Dumps(s, pyjson.Options{Compact: true})
	return out[1 : len(out)-1]
}

// OpenAPI is GET /openapi.json.
func (d *Docs) OpenAPI(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(d.schema)
}

// schemaURL is api_docs.schema_url.
func (d *Docs) schemaURL() string { return d.rootPath + "/openapi.json" }

func sha256Source(script string) string {
	sum := sha256.Sum256([]byte(script))
	return "'sha256-" + base64.StdEncoding.EncodeToString(sum[:]) + "'"
}

// csp is api_docs._csp.
func csp(inline []string) string {
	var hashes []string
	for _, s := range inline {
		hashes = append(hashes, sha256Source(s))
	}
	return strings.Join([]string{
		"default-src 'none'",
		strings.TrimSpace("script-src " + cdn + "/ " + strings.Join(hashes, " ")),
		"style-src " + cdn + "/ 'unsafe-inline'",
		"img-src 'self' data: https:",
		"font-src 'self' data: https:",
		"connect-src 'self'",
		"worker-src blob:",
		"frame-ancestors 'none'",
		"base-uri 'none'",
		"form-action 'none'",
	}, "; ")
}

// escape is Python's html.escape (quote=True).
func escape(s string) string {
	return strings.NewReplacer("&#34;", "&quot;", "&#39;", "&#x27;").Replace(html.EscapeString(s))
}

// page is api_docs._page: an HTMLResponse with its headers.
func page(w http.ResponseWriter, title, head, body string, inline []string) {
	var scripts strings.Builder
	for _, s := range inline {
		scripts.WriteString("<script>" + s + "</script>")
	}
	text := `<!doctype html><html><head><meta charset="utf-8"><title>` + escape(title) + `</title>` +
		`<meta name="viewport" content="width=device-width, initial-scale=1">` + head + `</head>` +
		`<body>` + body + scripts.String() + `</body></html>`
	h := w.Header()
	h.Set("Content-Security-Policy", csp(inline))
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(text))
}

func tagScript(s [2]string) string {
	return `<script src="` + s[0] + `" integrity="` + s[1] + `" crossorigin="anonymous"></script>`
}

// asciiJSON is json.dumps(s) (ensure_ascii=True).
func asciiJSON(s string) string {
	out, _ := pyjson.Dumps(s, pyjson.Options{EnsureASCII: true})
	return out
}

// Swagger is GET /docs.
func (d *Docs) Swagger(w http.ResponseWriter, _ *http.Request) {
	start := "window.ui = SwaggerUIBundle({url: " + asciiJSON(d.schemaURL()) +
		", dom_id: '#swagger-ui', deepLinking: true, presets: [SwaggerUIBundle.presets.apis," +
		" SwaggerUIBundle.SwaggerUIStandalonePreset], layout: 'BaseLayout'});"
	head := `<link rel="stylesheet" href="` + swaggerCSS[0] + `" integrity="` + swaggerCSS[1] + `" crossorigin="anonymous">`
	page(w, d.title+" — API", head, `<div id="swagger-ui"></div>`+tagScript(swaggerJS), []string{start})
}

// Redoc is GET /redoc.
func (d *Docs) Redoc(w http.ResponseWriter, _ *http.Request) {
	body := `<redoc spec-url="` + escape(d.schemaURL()) + `"></redoc>` + tagScript(redocJS)
	page(w, d.title+" — API", "", body, nil)
}
