package main

// core/api_docs.py and main.py: the API's documentation pages, the OpenAPI schema, the root.
func init() {
	register(Suite{
		Module: "api_docs",
		Steps: func(w *World) []Step {
			var s []Step
			for _, path := range []string{"/docs", "/redoc", "/openapi.json", "/"} {
				s = append(s, Step{Name: "read " + path, Method: "GET", Path: path},
					Step{Name: "read " + path + " signed in", Method: "GET", Path: path + "?x=1", Auth: w.As("admin")})
			}
			return s
		},
	})
}
