// Package statcatalog is the catalogue of the hypothesis tests as the gateway reads it: which
// operations of a flow are statistics (`stat_` + the test's id), in the catalogue's order.
//
// The tests are declared once, in the engine (backend/app/engine/stats/catalog.py);
// catalog.json is a copy of its snapshot, embedded so that the gateway is built from its own
// folder alone. .github/scripts/stat_catalog.py writes the copy again and checks it: a new
// test reaches every list here (the flow editor's quick menu, the dbt export's refusal)
// without a name written by hand.
package statcatalog

import (
	_ "embed"
	"encoding/json"
)

//go:embed catalog.json
var raw []byte

// Test is one hypothesis test of the catalogue.
type Test struct {
	ID        string `json:"id"`
	Operation string `json:"operation"`
	Family    string `json:"family"`
}

type catalogue struct {
	Prefix   string   `json:"prefix"`
	Families []string `json:"families"`
	Tests    []Test   `json:"tests"`
}

var data, byOperation = func() (catalogue, map[string]Test) {
	var c catalogue
	if err := json.Unmarshal(raw, &c); err != nil {
		panic("statcatalog: catalog.json: " + err.Error())
	}
	if c.Prefix == "" || len(c.Tests) == 0 {
		panic("statcatalog: catalog.json holds no test")
	}
	index := make(map[string]Test, len(c.Tests))
	for _, t := range c.Tests {
		if t.ID == "" || t.Operation != c.Prefix+t.ID {
			panic("statcatalog: catalog.json: the operation of «" + t.ID + "» is not the prefix and its id")
		}
		index[t.Operation] = t
	}
	return c, index
}()

// Operations are the statistics operations a flow may hold, in the catalogue's order.
func Operations() []string {
	out := make([]string, 0, len(data.Tests))
	for _, t := range data.Tests {
		out = append(out, t.Operation)
	}
	return out
}

// Is says whether an operation of a flow is a statistics one.
func Is(operation string) bool {
	_, ok := byOperation[operation]
	return ok
}

// Tests are the tests of the catalogue, in its order.
func Tests() []Test {
	return append([]Test(nil), data.Tests...)
}

// Families are the families the tests are shown by, in the palette's order.
func Families() []string {
	return append([]string(nil), data.Families...)
}
