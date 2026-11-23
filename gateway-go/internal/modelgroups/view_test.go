package modelgroups

import (
	"strings"
	"testing"
)

// A catalogue with a made-up entry of the group `bayes` (lot Q0): the other groups' ConfigMaps hold nothing of it, the group `bayes`'s
// holds it whole, and the machinery takes the view from the same place for the run, the self-test and the hash.
func TestEveryGroupsFilesAreItsView(t *testing.T) {
	oldCat, oldSchema := catalogueView, schemaView
	defer func() { catalogueView, schemaView = oldCat, oldSchema }()
	catalogueView = func(group string) []byte {
		if group == "bayes" {
			return []byte(`{"bayes_model":1}`)
		}
		return []byte(`{}`)
	}
	schemaView = func(b []byte, group string) []byte {
		if group == "bayes" {
			return []byte(`{"bayes":1}`)
		}
		return []byte(`{"title":"card"}`)
	}
	for _, id := range append([]string{"bayes"}, goldenGroups...) {
		for _, reference := range []bool{false, true} {
			code := mustCode(t, id, nil, reference)
			cat, schema := string(mustFile(t, code, "catalog.json")), string(mustFile(t, code, "card.schema.json"))
			if id == "bayes" {
				if cat != `{"bayes_model":1}` || schema != `{"bayes":1}` {
					t.Errorf("bayes: its catalogue and schema must be whole: %s %s", cat, schema)
				}
				continue
			}
			if strings.Contains(cat, "bayes") || strings.Contains(schema, "bayes") {
				t.Errorf("%s: a file holds something of the group bayes: %s %s", id, cat, schema)
			}
		}
	}
}
