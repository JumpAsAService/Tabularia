package pod

import (
	"fmt"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// OpenAPI is the data API's description, OpenAPI 3.1, for a key: one path per resource it
// may read, the filters as parameters with their types, the rows' schema from the columns
// that go out (typed once the data is prepared), the two ways to send the key.
func OpenAPI(m *dataapiwire.Manifest, key *KeyRule, tables map[string]*Table) map[string]any {
	paths := map[string]any{}
	schemas := map[string]any{
		"Problem": map[string]any{
			"type": "object", "required": []string{"detail", "code"},
			"properties": map[string]any{
				"detail": map[string]any{"type": "string", "description": "What went wrong, in a sentence"},
				"code":   map[string]any{"type": "string", "description": "The same, for a program: key_invalid, rate_limited, data_changed…"},
			},
		},
	}
	problem := func(text string) map[string]any {
		return map[string]any{"description": text,
			"content": map[string]any{"application/json": map[string]any{"schema": map[string]any{"$ref": "#/components/schemas/Problem"}}}}
	}
	for _, res := range m.Resources {
		if key != nil && !key.AllowsResource(res.Name) {
			continue
		}
		t := tables[res.Name]
		columnOf := func(name string) (Column, bool) {
			if t == nil {
				return Column{Name: name, Kind: KindOther}, false
			}
			return t.Column(name)
		}
		props := map[string]any{}
		for _, c := range res.Columns {
			col, typed := columnOf(c)
			s := outSchema(col)
			if !typed {
				s = map[string]any{"description": "typed once the data is loaded"}
			}
			props[c] = s
		}
		row := "Row_" + res.Name
		pageName := "Page_" + res.Name
		schemas[row] = map[string]any{"type": "object", "properties": props, "additionalProperties": false}
		schemas[pageName] = map[string]any{
			"type": "object", "required": []string{"data", "next", "version"},
			"properties": map[string]any{
				"data":    map[string]any{"type": "array", "items": map[string]any{"$ref": "#/components/schemas/" + row}},
				"next":    map[string]any{"type": []string{"string", "null"}, "description": "The _cursor of the next page; null on the last"},
				"version": map[string]any{"type": "string", "description": "The data's version: when it was written"},
				"count":   map[string]any{"type": "integer", "minimum": 0, "description": "With _count=true: the rows the filters match"},
			},
		}
		params := []any{}
		for _, f := range res.Filters {
			col, _ := columnOf(f.Column)
			for _, op := range f.Operators {
				name, schema, about := f.Column, inSchema(col), "equal to"
				p := map[string]any{"in": "query"}
				switch op {
				case "in":
					name, about = f.Column+"__in", "one of these (a comma-separated list)"
					schema = map[string]any{"type": "array", "items": inSchema(col), "maxItems": MaxInValues}
					p["style"], p["explode"] = "form", false
				case "null":
					name, about, schema = f.Column+"__null", "null (true) or not null (false)", map[string]any{"type": "boolean"}
				case "gt", "gte", "lt", "lte":
					name = f.Column + "__" + op
					about = map[string]string{"gt": "greater than", "gte": "at least", "lt": "less than", "lte": "at most"}[op]
				}
				p["name"], p["schema"], p["description"] = name, schema, fmt.Sprintf("Rows whose %s is %s", f.Column, about)
				params = append(params, p)
			}
		}
		sortable := []string{}
		for _, f := range res.Filters {
			sortable = append(sortable, f.Column, "-"+f.Column)
		}
		params = append(params,
			map[string]any{"name": "_columns", "in": "query", "style": "form", "explode": false,
				"description": "The columns to return (all by default)",
				"schema":      map[string]any{"type": "array", "items": map[string]any{"type": "string", "enum": res.Columns}}},
			map[string]any{"name": "_limit", "in": "query", "description": "Rows per page",
				"schema": map[string]any{"type": "integer", "minimum": 1, "maximum": res.MaxPage, "default": res.MaxPage}},
			map[string]any{"name": "_cursor", "in": "query",
				"description": "The `next` of the previous page; alone (or with _format): it carries the query",
				"schema":      map[string]any{"type": "string"}},
			map[string]any{"name": "_count", "in": "query", "description": "Add `count`, the rows the filters match",
				"schema": map[string]any{"type": "boolean", "default": false}},
			map[string]any{"name": "_format", "in": "query", "description": "json, or csv (next cursor, version and count in headers)",
				"schema": map[string]any{"type": "string", "enum": []string{"json", "csv"}, "default": "json"}},
		)
		if len(sortable) > 0 {
			params = append(params, map[string]any{"name": "_sort", "in": "query", "style": "form", "explode": false,
				"description": "The order: columns, a leading - for descending",
				"schema":      map[string]any{"type": "array", "maxItems": MaxSortKeys, "items": map[string]any{"type": "string", "enum": sortable}}})
		}
		paths["/data/"+m.Slug+"/"+res.Name] = map[string]any{"get": map[string]any{
			"operationId": "list_" + strings.ReplaceAll(res.Name, "-", "_"),
			"summary":     "Rows of " + res.Name,
			"parameters":  params,
			"responses": map[string]any{
				"200": map[string]any{"description": "A page of rows",
					"headers": map[string]any{
						"X-Next-Cursor":  map[string]any{"description": "CSV only: the next page's cursor", "schema": map[string]any{"type": "string"}},
						"X-Data-Version": map[string]any{"description": "CSV only: the data's version", "schema": map[string]any{"type": "string"}},
						"X-Total-Count":  map[string]any{"description": "CSV with _count=true", "schema": map[string]any{"type": "integer"}},
					},
					"content": map[string]any{
						"application/json": map[string]any{"schema": map[string]any{"$ref": "#/components/schemas/" + pageName}},
						"text/csv":         map[string]any{"schema": map[string]any{"type": "string"}},
					}},
				"400": problem("A parameter is wrong: the detail names it"),
				"401": problem("No key, or not a valid one"),
				"403": problem("The key may not read this resource, or not from this address; or the resource is not available"),
				"404": problem("No such resource"),
				"409": problem("The data changed since the cursor was given: ask the first page again"),
				"429": map[string]any{"description": "Past the key's limit",
					"headers": map[string]any{"Retry-After": map[string]any{"schema": map[string]any{"type": "integer"}}},
					"content": map[string]any{"application/json": map[string]any{"schema": map[string]any{"$ref": "#/components/schemas/Problem"}}}},
				"503": problem("The data is loading, or the data API is paused"),
			},
		}}
	}
	return map[string]any{
		"openapi": "3.1.0",
		"info": map[string]any{"title": m.Name, "version": m.At.UTC().Format("2006-01-02T15:04:05Z"),
			"description": "A read-only data API of Tabularia. Send the key as `Authorization: Bearer tdk_…` (or in `X-API-Key`), never in the address."},
		"servers":  []any{map[string]any{"url": "/"}},
		"security": []any{map[string]any{"bearer": []string{}}, map[string]any{"apiKey": []string{}}},
		"paths":    paths,
		"components": map[string]any{
			"schemas": schemas,
			"securitySchemes": map[string]any{
				"bearer": map[string]any{"type": "http", "scheme": "bearer", "description": "The key: tdk_ and 40 characters"},
				"apiKey": map[string]any{"type": "apiKey", "in": "header", "name": "X-API-Key"},
			},
		},
	}
}

// inSchema is a parameter's value of the column
func inSchema(c Column) map[string]any {
	switch c.Kind {
	case KindInt:
		return map[string]any{"type": "integer"}
	case KindFloat, KindDecimal:
		return map[string]any{"type": "number"}
	case KindBool:
		return map[string]any{"type": "boolean"}
	case KindDate:
		return map[string]any{"type": "string", "format": "date"}
	case KindTimestampTZ:
		return map[string]any{"type": "string", "format": "date-time"}
	case KindTimestamp:
		return map[string]any{"type": "string", "description": "a date and time, 2026-01-31T08:30:00, or a date"}
	case KindTime:
		return map[string]any{"type": "string", "description": "a time, 08:30:00"}
	case KindUUID:
		return map[string]any{"type": "string", "format": "uuid"}
	}
	return map[string]any{"type": "string"}
}

// outSchema is a value of the column in an answer, null included
func outSchema(c Column) map[string]any {
	switch c.Kind {
	case KindInt:
		return map[string]any{"type": []string{"integer", "null"}}
	case KindFloat:
		return map[string]any{"type": []string{"number", "string", "null"}, "description": "a number; NaN and Infinity as strings"}
	case KindDecimal:
		return map[string]any{"type": []string{"number", "string", "null"}, "description": "a number; a string when a float would not hold it exactly"}
	case KindBool:
		return map[string]any{"type": []string{"boolean", "null"}}
	case KindString:
		return map[string]any{"type": []string{"string", "null"}}
	case KindDate:
		return map[string]any{"type": []string{"string", "null"}, "format": "date"}
	case KindTimestampTZ:
		return map[string]any{"type": []string{"string", "null"}, "format": "date-time"}
	case KindTimestamp:
		return map[string]any{"type": []string{"string", "null"}, "description": "a date and time without a zone, ISO 8601"}
	case KindTime:
		return map[string]any{"type": []string{"string", "null"}, "description": "a time, ISO 8601"}
	case KindUUID:
		return map[string]any{"type": []string{"string", "null"}, "format": "uuid"}
	}
	return map[string]any{}
}
