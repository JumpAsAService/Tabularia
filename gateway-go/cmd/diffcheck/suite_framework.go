package main

// What the framework answers around the routes (uvicorn, Starlette, FastAPI: internal/server
// router.go): a path no route declares, a method a route does not take, HEAD, the trailing
// slash's redirect, the path decoded as uvicorn decodes it («%2F», «..», «//», a final newline),
// the CORS middleware, a WebSocket handshake where no WebSocket route is or a bad one.
func init() {
	register(Suite{
		Module: "framework",
		Steps: func(w *World) []Step {
			editor := w.As("editor")
			ui := "http://localhost:3000" // APP__CORS_ORIGINS' default
			ws := func(extra map[string]string) map[string]string {
				h := map[string]string{"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
					"Sec-WebSocket-Version": "13"}
				for k, v := range extra {
					if v == "" {
						delete(h, k)
					} else {
						h[k] = v
					}
				}
				return h
			}
			return []Step{
				// no route
				{Name: "a path nobody declares", Method: "GET", Path: "/nothing"},
				{Name: "a path nobody declares, POST", Method: "POST", Path: "/nothing", JSON: map[string]any{"a": 1}},
				{Name: "the root", Method: "GET", Path: "/"},
				{Name: "an unknown method on nothing", Method: "PROPFIND", Path: "/nothing"},
				// another method
				{Name: "POST on a GET", Method: "POST", Path: "/health"},
				{Name: "DELETE on /auth/me", Method: "DELETE", Path: "/auth/me", Auth: editor},
				{Name: "an unknown method on a route", Method: "PROPFIND", Path: "/auth/me", Auth: editor},
				{Name: "OPTIONS without CORS", Method: "OPTIONS", Path: "/auth/me"},
				{Name: "POST on a flow: the first route's Allow", Method: "POST", Path: "/flows/1", Auth: editor},
				{Name: "PUT on the docs", Method: "PUT", Path: "/docs"},
				// HEAD
				{Name: "HEAD on a GET of FastAPI's", Method: "HEAD", Path: "/health"},
				{Name: "HEAD on /auth/me", Method: "HEAD", Path: "/auth/me", Auth: editor},
				{Name: "HEAD on the schema (a Starlette route)", Method: "HEAD", Path: "/openapi.json"},
				{Name: "HEAD on nothing", Method: "HEAD", Path: "/nothing"},
				{Name: "HEAD with a slash too many", Method: "HEAD", Path: "/health/"},
				// the trailing slash
				{Name: "a slash too many", Method: "GET", Path: "/projects/", Auth: editor},
				{Name: "a slash too many, with a query", Method: "GET", Path: "/projects/?a=1&b=%20x%2F&c", Auth: editor},
				{Name: "slashes too many", Method: "GET", Path: "/health///"},
				{Name: "a slash missing", Method: "GET", Path: "/flows/1/runs/", Auth: editor},
				{Name: "a slash too many on a POST", Method: "POST", Path: "/auth/login/", JSON: map[string]any{"email": "x", "password": "y"}},
				{Name: "a slash too many on another method", Method: "DELETE", Path: "/docs/"},
				{Name: "a slash too many, behind an https proxy", Method: "GET", Path: "/health/",
					Header: map[string]string{"X-Forwarded-Proto": "https", "Host": "gw.test"}},
				{Name: "a slash too many, a proxy's odd scheme", Method: "GET", Path: "/health/",
					Header: map[string]string{"X-Forwarded-Proto": " ftp ", "Host": "gw.test:8443"}},
				{Name: "a slash too many, an encoded path", Method: "GET", Path: "/flows/%C3%A8/", Auth: editor, Header: map[string]string{"Host": "gw.test"}},
				// the path as uvicorn decodes it
				{Name: "an escaped slash splits the segment", Method: "GET", Path: "/flows/1%2Fruns", Auth: editor},
				{Name: "an escaped slash at the end", Method: "GET", Path: "/flows/1%2F", Auth: editor, Header: map[string]string{"Host": "gw.test"}},
				{Name: "an escaped slash in a path parameter", Method: "PUT", Path: "/ai/models/a%2Fb", Auth: editor},
				{Name: "an escaped slash, GET", Method: "GET", Path: "/ai/models/a%2Fb", Auth: editor},
				{Name: "dot segments are not cleaned", Method: "GET", Path: "/projects/../auth/me", Auth: editor},
				{Name: "dot dot as a parameter", Method: "GET", Path: "/flows/..", Auth: editor},
				{Name: "a double slash", Method: "GET", Path: "//auth/me", Auth: editor},
				{Name: "a double slash inside", Method: "GET", Path: "/flows//runs", Auth: editor},
				{Name: "a final newline", Method: "GET", Path: "/health%0A"},
				{Name: "a final newline in a parameter", Method: "GET", Path: "/flows/1%0A", Auth: editor},
				{Name: "two final newlines", Method: "GET", Path: "/health%0A%0A"},
				{Name: "an accented parameter", Method: "GET", Path: "/flows/%C3%A8", Auth: editor},
				{Name: "bytes that are not UTF-8", Method: "GET", Path: "/flows/%FF%E2%82", Auth: editor},
				{Name: "a space", Method: "GET", Path: "/flows/1%20", Auth: editor},
				{Name: "an escaped letter", Method: "GET", Path: "/he%61lth"},
				// CORS (APP__CORS_ORIGINS' default)
				{Name: "CORS: an allowed origin", Method: "GET", Path: "/auth/me", Auth: editor, Header: map[string]string{"Origin": ui}},
				{Name: "CORS: another origin", Method: "GET", Path: "/auth/me", Auth: editor, Header: map[string]string{"Origin": "http://evil.test"}},
				{Name: "CORS: an empty origin", Method: "GET", Path: "/health", Header: map[string]string{"Origin": ""}},
				{Name: "CORS: on a 404", Method: "GET", Path: "/nothing", Header: map[string]string{"Origin": ui}},
				{Name: "CORS: on a 405", Method: "PATCH", Path: "/health", Header: map[string]string{"Origin": ui}},
				{Name: "CORS: on a redirect", Method: "GET", Path: "/health/", Header: map[string]string{"Origin": ui}},
				{Name: "CORS: on a 401", Method: "GET", Path: "/auth/me", Header: map[string]string{"Origin": ui}},
				{Name: "CORS: preflight", Method: "OPTIONS", Path: "/auth/me", Header: map[string]string{"Origin": ui,
					"Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": "X-Tabularia-CSRF, content-type"}},
				{Name: "CORS: preflight on nothing", Method: "OPTIONS", Path: "/nothing", Header: map[string]string{"Origin": ui,
					"Access-Control-Request-Method": "GET"}},
				{Name: "CORS: preflight from another origin", Method: "OPTIONS", Path: "/auth/me", Header: map[string]string{"Origin": "http://evil.test",
					"Access-Control-Request-Method": "TRACE"}},
				{Name: "CORS: preflight, a private network", Method: "OPTIONS", Path: "/auth/me", Header: map[string]string{"Origin": ui,
					"Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true"}},
				{Name: "CORS: preflight, an empty method", Method: "OPTIONS", Path: "/auth/me", Header: map[string]string{"Origin": ui,
					"Access-Control-Request-Method": ""}},
				{Name: "CORS: OPTIONS without a requested method", Method: "OPTIONS", Path: "/auth/me", Header: map[string]string{"Origin": ui}},
				// WebSocket
				{Name: "a WebSocket where none is", Method: "GET", Path: "/health", Header: ws(nil)},
				{Name: "a WebSocket on nothing", Method: "GET", Path: "/nothing", Header: ws(nil)},
				{Name: "a WebSocket POST", Method: "POST", Path: "/health", Header: ws(nil)},
				{Name: "a WebSocket without its key", Method: "GET", Path: "/health", Header: ws(map[string]string{"Sec-WebSocket-Key": ""})},
				{Name: "a WebSocket with a short key", Method: "GET", Path: "/health", Header: ws(map[string]string{"Sec-WebSocket-Key": "c2hvcnQ="})},
				{Name: "a WebSocket without its version", Method: "GET", Path: "/health", Header: ws(map[string]string{"Sec-WebSocket-Version": ""})},
				{Name: "a WebSocket of another version", Method: "GET", Path: "/health", Header: ws(map[string]string{"Sec-WebSocket-Version": "8"})},
				{Name: "a WebSocket on a notebook", Method: "GET", Path: "/notebooks/1/app/ws", Header: ws(nil)},
				{Name: "a WebSocket on a notebook, a bad key", Method: "GET", Path: "/notebooks/1/app/ws",
					Header: ws(map[string]string{"Sec-WebSocket-Key": "c2hvcnQ="})},
				{Name: "Upgrade without Connection: plain HTTP", Method: "GET", Path: "/health", Header: map[string]string{"Upgrade": "websocket"}},
				{Name: "Upgrade to something else", Method: "GET", Path: "/health", Header: map[string]string{"Upgrade": "h2c", "Connection": "Upgrade"}},
			}
		},
	})
}
