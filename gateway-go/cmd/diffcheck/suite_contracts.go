package main

import "fmt"

// routes/contracts.py: a datasource's data contract.
func init() {
	report := map[string]any{"outcome": "warning", "errors": 0, "warnings": "2", "rows": 10, "rules": []any{map[string]any{"id": "r1", "ok": 1.0}}}
	register(Suite{
		Module: "contracts",
		Seed:   seedData,
		Engine: []EngineRule{
			{Method: "POST", Path: "^/contracts/evaluate$", Body: report},
			{Method: "POST", Path: "^/contracts/profile$", Body: map[string]any{"proposal": map[string]any{"rules": []any{}, "z": 1, "a": 2.0}, "profile": []any{1}}},
		},
		Steps: func(w *World) []Step {
			ds := func(k string) string { return "/datasources/" + fmt.Sprint(Data["ds:"+k]) + "/contract" }
			s := forEveryone(w, "the contract", "GET", ds("root"), nil)
			s = append(s, forEveryone(w, "the history", "GET", ds("root")+"/history", nil)...)
			s = append(s, forEveryone(w, "check", "POST", ds("root")+"/check", nil)...)
			s = append(s, forEveryone(w, "propose", "POST", ds("root")+"/proposal", nil)...)
			as := func(person, name, method, path string, rules []EngineRule) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, Auth: w.As(person), Engine: rules})
			}
			for _, k := range []string{"ready", "half", "hidden", "child", "run", "broken", "nulls"} {
				as("writer", "the contract of "+k, "GET", ds(k), nil)
				as("admin", "the contract of "+k, "GET", ds(k), nil)
			}
			for _, id := range []string{"999999", "x", "99999999999"} {
				as("editor", "the contract of "+id, "GET", "/datasources/"+id+"/contract", nil)
			}
			for _, q := range []string{"?limit=0", "?limit=-5", "?limit=1", "?limit=500", "?limit=x", "?limit=99999999999999999999",
				"?limit=-99999999999999999999", "?with_report=true", "?with_report=yes", "?with_report=maybe", "?with_report=1&limit=2"} {
				as("editor", "the history "+q, "GET", ds("root")+"/history"+q, nil)
			}
			as("editor", "a bad id and a bad query", "GET", "/datasources/x/contract/history?limit=x&with_report=maybe", nil)
			s = append(s, Step{Name: "a bad query, nobody", Method: "GET", Path: ds("root") + "/history?limit=x"})
			as("writer", "a bad query, without VIEW? no: writer reads", "GET", ds("hidden")+"/history?limit=x", nil)
			as("editor", "the history of a datasource without one", "GET", ds("child")+"/history", nil)
			// checking
			check := func(name, k string, rule EngineRule) {
				as("admin", "check: "+name, "POST", ds(k)+"/check", []EngineRule{rule})
			}
			check("the engine refuses with a detail", "root", EngineRule{Path: ".", Status: 422, Body: map[string]any{"detail": "too large"}})
			check("a dict detail", "root", EngineRule{Path: ".", Status: 500, Body: map[string]any{"detail": map[string]any{"a": 1, "b": []any{true, nil}}}})
			check("an empty detail", "root", EngineRule{Path: ".", Status: 500, Body: map[string]any{"detail": ""}})
			check("a list error", "root", EngineRule{Path: ".", Status: 500, Body: []any{1}})
			check("a text error", "root", EngineRule{Path: ".", Status: 503, Text: "down"})
			check("not JSON", "root", EngineRule{Path: ".", Status: 200, Text: "ok"})
			check("errors not a number", "root", EngineRule{Path: ".", Body: map[string]any{"outcome": "x", "errors": "many"}})
			check("errors a float", "root", EngineRule{Path: ".", Body: map[string]any{"outcome": "failed", "errors": 2.7, "warnings": true}})
			check("an empty document is not the one asked", "run", EngineRule{Path: ".", Body: report})
			check("no contract", "child", EngineRule{Path: ".", Body: report})
			check("a document that is a list", "broken", EngineRule{Path: ".", Body: report})
			check("a blocked one", "ready", EngineRule{Path: ".", Body: report})
			as("writer", "check without EDIT", "POST", ds("root")+"/check", nil)
			as("editor", "check with EDIT", "POST", ds("root")+"/check", nil)
			// proposing
			propose := func(name string, rule EngineRule) {
				as("editor", "propose: "+name, "POST", ds("child")+"/proposal", []EngineRule{rule})
			}
			propose("no proposal", EngineRule{Path: ".", Body: map[string]any{"proposal": nil}})
			propose("refused", EngineRule{Path: ".", Status: 404, Body: map[string]any{"detail": "no data"}})
			propose("refused with nothing", EngineRule{Path: ".", Status: 500, Text: ""})
			propose("a list", EngineRule{Path: ".", Body: []any{}})
			as("editor", "propose on a hidden datasource", "POST", ds("hidden")+"/proposal", nil)
			// deleting
			as("writer", "delete without EDIT", "DELETE", ds("half"), nil)
			as("editor", "delete", "DELETE", ds("half"), nil)
			as("editor", "delete again", "DELETE", ds("half"), nil)
			as("editor", "delete where there is none", "DELETE", ds("child"), nil)
			as("editor", "delete an odd id", "DELETE", "/datasources/x/contract", nil)
			return s
		},
	})
}
