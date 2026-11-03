package main

import "net/http"

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
