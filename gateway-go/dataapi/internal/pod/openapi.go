package pod

import (
	"strconv"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// DocOf is what the description of the manifest's data API is made of.
func DocOf(m *dataapiwire.Manifest) dataapiwire.Doc {
	version := m.At.UTC().Format("2006-01-02T15:04:05Z") // a gateway of before the definition's version
	if m.DefinitionVersion > 0 {
		version = strconv.FormatInt(m.DefinitionVersion, 10)
	}
	return dataapiwire.Doc{Name: m.Name, Slug: m.Slug, Version: version, Resources: m.Resources}
}

// OpenAPI is the data API's description for a key (dataapiwire.OpenAPI, which the gateway's
// Documentation uses too): the resources it may read, the columns typed by the data prepared.
func OpenAPI(m *dataapiwire.Manifest, key *KeyRule, tables map[string]*Table) map[string]any {
	allow := func(name string) bool { return key == nil || key.AllowsResource(name) }
	kindOf := func(resource, column string) (string, bool) {
		t := tables[resource]
		if t == nil {
			return "", false
		}
		c, ok := t.Column(column)
		return c.Kind, ok
	}
	return dataapiwire.OpenAPI(DocOf(m), allow, kindOf, "/")
}
