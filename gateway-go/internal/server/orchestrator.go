package server

import "net/http"

// NotFound is what FastAPI answers for a path no route declares: 404 {"detail": "Not Found"}.
// A process with APP__ROLE=orchestrator declares no API routes (app/main.py): everything but
// its own few paths gets it, nothing is forwarded.
func NotFound() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, http.StatusNotFound, map[string]string{"detail": "Not Found"})
	})
}

// MethodNotAllowed is Starlette's 405 for a declared path asked with another method; `allow`
// is the route's methods as its Allow header lists them.
func MethodNotAllowed(allow string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Allow", allow)
		JSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	})
}

// Root is GET / (app/main.py root): the gateway is running.
func Root(appName string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, http.StatusOK, map[string]string{"message": appName + " gateway is running"})
	})
}

// Orchestrator is the whole HTTP surface of an APP__ROLE=orchestrator process: /health and
// /, nothing else (the API docs' pages are not served by the Go gateway there).
func Orchestrator(appName string) http.Handler {
	paths := map[string]http.Handler{"/health": Health(), "/": Root(appName)}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h, ok := paths[r.URL.Path]
		switch {
		case !ok:
			NotFound().ServeHTTP(w, r)
		case r.Method != http.MethodGet:
			MethodNotAllowed("GET").ServeHTTP(w, r)
		default:
			h.ServeHTTP(w, r)
		}
	})
}
