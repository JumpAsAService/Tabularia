package main

import (
	"net/http"
	"time"

	"golang.org/x/crypto/bcrypt"
)

// bodyEdges are the requests every route with a body gets: a JSON that does not parse (from
// nobody too: FastAPI answers it before the authentication), no body, a body that is not JSON,
// JSON null, a list instead of an object.
func bodyEdges(method, path string, auth func(*http.Request)) []Step {
	return []Step{
		{Name: "broken JSON", Method: method, Path: path, Auth: auth, Raw: []byte(`{"name": `), ContentType: "application/json"},
		{Name: "broken JSON, nobody", Method: method, Path: path, Raw: []byte(`{"name": `), ContentType: "application/json"},
		{Name: "no body", Method: method, Path: path, Auth: auth},
		{Name: "a form", Method: method, Path: path, Auth: auth, Raw: []byte("name=x"), ContentType: "application/x-www-form-urlencoded"},
		{Name: "JSON null", Method: method, Path: path, Auth: auth, Raw: []byte("null"), ContentType: "application/json"},
		{Name: "a list", Method: method, Path: path, Auth: auth, JSON: []any{1}},
		{Name: "bytes that are not UTF-8", Method: method, Path: path, Auth: auth, Raw: []byte("{\"name\": \"\xff\"}"), ContentType: "application/json"},
	}
}

// Person adds a person of a suite, with a password (bcrypt, cost 4: the Python gateway reads
// it as its own) or none (""), and a session «s-<key>».
func (w *World) Person(key, email, password string, superuser, observer, service bool) int64 {
	var hash *string
	if password != "" {
		h, err := bcrypt.GenerateFromPassword([]byte(password), 4)
		if err != nil {
			fail("%v", err)
		}
		hash = ptr("$2b$" + string(h[4:]))
	}
	w.People[key] = w.Insert(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer,
		is_service, created_at) VALUES ($1, $2, $3, true, $4, $5, $6, '2026-01-01') RETURNING id`,
		email, "Person "+key, hash, superuser, observer, service)
	w.Session(key, "s-"+key, 5*time.Minute, time.Hour)
	return w.People[key]
}
