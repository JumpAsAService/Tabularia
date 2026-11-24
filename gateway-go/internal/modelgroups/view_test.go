package modelgroups

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
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

// The card schema is cut where it stands: for every group but `bayes` the view is the file as it was before the card's `bayes` object
// (its bytes and its sha256, recorded from the commit that came before lot Q1 of docs/specs/bayes-report.md), and the group `bayes` gets
// the file whole, with the object, the names in the enums and the rule that requires it.
func TestTheSchemaViewOfTheOtherGroupsIsTheSchemaBeforeTheBayesObject(t *testing.T) {
	const beforeBytes, beforeSum = 18625, "3d95d2856cd09cc3c5f6e154389350c91263678fd8cde0e5521d3f673f1ce127"
	whole := CardSchema()
	for _, id := range goldenGroups {
		view := schemaView(whole, id)
		sum := sha256.Sum256(view)
		if len(view) != beforeBytes || hex.EncodeToString(sum[:]) != beforeSum {
			t.Errorf("%s: the schema's view is %d bytes, sha256 %x: not the schema before the object (%d bytes, %s)", id, len(view), sum, beforeBytes, beforeSum)
		}
		if strings.Contains(string(view), "bayes") {
			t.Errorf("%s: the schema's view names the group bayes", id)
		}
	}
	if got := schemaView(whole, "bayes"); !bytes.Equal(got, whole) {
		t.Error("the group bayes did not receive the schema whole")
	}
	var parsed struct {
		Properties map[string]json.RawMessage `json:"properties"`
		AllOf      []json.RawMessage          `json:"allOf"`
	}
	if err := json.Unmarshal(whole, &parsed); err != nil {
		t.Fatal(err)
	}
	if _, ok := parsed.Properties["bayes"]; !ok {
		t.Error("the card schema has no `bayes` object")
	}
	for _, want := range []string{`"bayes_model"]},`, `"customer_lifetime", "bayes"]}`, `"const": "bayes_model"`} {
		if !strings.Contains(string(whole), want) {
			t.Errorf("the schema lacks %s", want)
		}
	}
}
