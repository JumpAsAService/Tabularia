// Package bayestemplates holds the ten templates of the Bayesian editor: the models of the
// first release's families drawn once as graphs of the model graph's format, with their default
// priors written in the open on the nodes (docs/specs/bayes-templates.md).
//
// templates.json is the one source, embedded; the browser reads a byte-for-byte copy
// (frontend/assets/bayes-templates.json, copied and checked by .github/scripts/bayes_templates.py).
// Opening a template (the outcome's numbers, the AR order) is done in the browser
// (frontend/composables/bayesTemplates.ts); the opened graphs the tests fix are in
// testdata/opened/.
package bayestemplates

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
)

//go:embed templates.json
var raw []byte

// Raw is the file, byte for byte.
func Raw() []byte { return append([]byte(nil), raw...) }

// FormatVersion is the file's format.
const FormatVersion = 1

// Scaled is a slot the outcome's numbers rewrite: Slot is "prior.<arg>" or "init.<arg>" of Node;
// As is "center" (the typical value), "spread" (Times, 1 when absent, times the typical spread)
// or "rate" (1 over the spread).
type Scaled struct {
	Node  string   `json:"node"`
	Slot  string   `json:"slot"`
	As    string   `json:"as"`
	Times *float64 `json:"times,omitempty"`
}

// Order describes the AR template's order: the coefficient node that is copied for each lag,
// and the likelihood's slot that reads them. The copies stand in columns of PerColumn from the
// first one's place down (RowHeight apart), the columns ColumnWidth apart going right; whatever
// stands right of the first one moves right by the columns added, so the canvas still reads left
// to right.
type Order struct {
	Default     int    `json:"default"`
	Node        string `json:"node"`
	IDPrefix    string `json:"id_prefix"`
	NamePrefix  string `json:"name_prefix"`
	Label       string `json:"label"`
	Likelihood  string `json:"likelihood"`
	Slot        string `json:"slot"`
	Outcome     string `json:"outcome"`
	Mean        string `json:"mean"`
	PerColumn   int    `json:"per_column"`
	ColumnWidth int    `json:"column_width"`
	RowHeight   int    `json:"row_height"`
}

// Template is one model. Graph is a document of the model graph's format at the default
// numbers (centre 0, spread 1, the AR order Order.Default): a valid model as it stands.
type Template struct {
	ID      string            `json:"id"`
	Version int               `json:"version"`
	Family  string            `json:"family"`
	Scale   *string           `json:"scale"`
	Scaled  []Scaled          `json:"scaled"`
	Sources map[string]string `json:"sources"`
	Order   *Order            `json:"order,omitempty"`
	Graph   json.RawMessage   `json:"graph"`
}

// Source is where a default prior comes from.
type Source struct {
	Title string `json:"title"`
	URL   string `json:"url"`
}

// File is the document.
type File struct {
	FormatVersion int               `json:"format_version"`
	Templates     []Template        `json:"templates"`
	Sources       map[string]Source `json:"sources"`
}

// Decode reads a file strictly (no unknown key).
func Decode(data []byte) (*File, error) {
	var f File
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&f); err != nil {
		return nil, err
	}
	if f.FormatVersion != FormatVersion {
		return nil, fmt.Errorf("format_version %d, this gateway reads %d", f.FormatVersion, FormatVersion)
	}
	return &f, nil
}

var file = func() *File {
	f, err := Decode(raw)
	if err != nil {
		panic("bayestemplates: templates.json: " + err.Error())
	}
	return f
}()

// List is the templates in the file's order.
func List() []Template { return append([]Template(nil), file.Templates...) }

// Get is the template with this id.
func Get(id string) (Template, bool) {
	for _, t := range file.Templates {
		if t.ID == id {
			return t, true
		}
	}
	return Template{}, false
}

// Sources is the file's sources by key.
func Sources() map[string]Source {
	out := make(map[string]Source, len(file.Sources))
	for k, v := range file.Sources {
		out[k] = v
	}
	return out
}
