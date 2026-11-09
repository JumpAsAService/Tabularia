package dataapiwire

// The data API's description, OpenAPI 3.1, written once for both sides: the pods serve it at
// /data/{api}/openapi.json (typed by the data they prepared, for the resources a key may read),
// the gateway gives it to the API's page, Documentation, with no key (typed by the datasources'
// columns). The same definition and the same types make the same document.

import (
	"fmt"
	"strings"
)

// The kinds of column a data API knows: how a filter's value is read and bound, how a value is
// written in an answer, how the description types it. Anything else (lists, structs, maps,
// blobs, intervals) goes out as JSON but cannot be filtered on or sorted by.
const (
	KindInt         = "integer"
	KindFloat       = "float"
	KindDecimal     = "decimal"
	KindBool        = "boolean"
	KindString      = "string"
	KindDate        = "date"
	KindTimestamp   = "timestamp"
	KindTimestampTZ = "timestamptz"
	KindTime        = "time"
	KindUUID        = "uuid"
	KindOther       = "other"
)

// The limits of a request the pods apply and the description states.
const (
	MaxInValues = 500 // the values of one col__in=
	MaxSortKeys = 5   // the columns of _sort
)

// KindOfPolars is the kind of a datasource's column from its type as the datasource lists it
// (Polars' names: Int64, Decimal(precision=12, scale=2), Datetime(time_unit='us',
// time_zone='UTC')…): the kind the pods give the same column once they read its parquet.
func KindOfPolars(dtype string) string {
	t := strings.TrimSpace(dtype)
	name, args, _ := strings.Cut(t, "(")
	switch name {
	// a duration is written as its count of time units; a 128-bit integer as bytes
	case "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64", "Duration":
		return KindInt
	case "Float32", "Float64":
		return KindFloat
	case "Decimal":
		return KindDecimal
	case "Boolean":
		return KindBool
	case "String", "Utf8", "Categorical", "Enum":
		return KindString
	case "Date":
		return KindDate
	case "Datetime":
		// a zone makes the parquet's timestamp adjusted to UTC, which the pods read with its zone
		if strings.Contains(args, "time_zone=") && !strings.Contains(args, "time_zone=None") {
			return KindTimestampTZ
		}
		return KindTimestamp
	case "Time":
		return KindTime
	}
	return KindOther
}

// Doc is what a description is made of: the data API's name, address and definition's version,
// and its resources.
type Doc struct {
	Name, Slug string
	// the version of the definition the description is of
	Version   string
	Resources []Resource
}

// KindOf is the kind of a resource's column; false when it is not known (the data not yet
// prepared, a column no longer in the datasource): the description says it is typed later.
type KindOf func(resource, column string) (string, bool)

// ErrorCode is an error a data API answers, as {"detail": "…", "code": "…"}.
type ErrorCode struct {
	Status  int    `json:"status"`
	Code    string `json:"code"`
	Meaning string `json:"meaning"`
}

// ErrorCodes are every error the pods answer (a test of the pods checks every code they write
// is here), in the order of the documentation.
var ErrorCodes = []ErrorCode{
	{400, "bad_query", "The query string cannot be read"},
	{400, "key_in_query", "A key was sent in the address: send it in a header, and revoke that key"},
	{400, "repeated_parameter", "The same parameter twice"},
	{400, "unknown_parameter", "A parameter that is not a filter of this resource nor one of _columns, _sort, _limit, _cursor, _count, _format"},
	{400, "not_filterable", "A filter on a column one may not filter on"},
	{400, "operator_not_allowed", "An operator this column does not allow"},
	{400, "bad_value", "A value that is not of its column's type"},
	{400, "too_many_values", "More than 500 values in one __in"},
	{400, "unknown_column", "_columns names a column that does not go out"},
	{400, "not_sortable", "_sort names a column one may not filter on, or too many"},
	{400, "bad_limit", "_limit is not a number from 1 to the largest page"},
	{400, "bad_count", "_count is not true or false"},
	{400, "bad_format", "_format is not json or csv"},
	{400, "cursor_alone", "_cursor comes alone (or with _format): it carries its query"},
	{400, "bad_cursor", "Not a cursor of this resource"},
	{401, "key_missing", "No key: send Authorization: Bearer tdk_… or X-API-Key"},
	{401, "key_invalid", "Not a key of this data API"},
	{401, "key_expired", "The key has expired"},
	{401, "key_revoked", "The key was revoked"},
	{401, "key_replaced", "The key was renewed and its grace period is over"},
	{403, "key_not_allowed", "The key may not read this resource"},
	{403, "address_not_allowed", "The key may not be used from this address"},
	{403, "resource_unavailable", "The resource's datasource may no longer be read by the data API's creator"},
	{404, "not_found", "No such data API, or nothing at this path"},
	{404, "unknown_resource", "The data API has no such resource"},
	{405, "method_not_allowed", "A data API is read-only: GET only"},
	{409, "data_changed", "The data changed since the cursor was given: ask the first page again"},
	{429, "rate_limited", "Past the key's requests per minute: wait the Retry-After seconds"},
	{500, "internal", "The data API could not answer: try again"},
	{503, "loading", "The data is still loading: try again after Retry-After"},
	{503, "no_data", "The datasource the resource publishes has no data file: never written, or missing from the storage"},
	{503, "api_paused", "The data API is paused"},
	{503, "api_suspended", "The data API's creator may no longer publish it"},
	{503, "manifest_stale", "The pod has been out of touch with Tabularia too long, and refuses every key"},
	{503, "query_timeout", "The request took too long"},
	{503, "cancelled", "The request was cancelled"},
}

// OpenAPI is the description, OpenAPI 3.1: one path per resource `allow` lets through (nil:
// all), the filters as parameters with their types, the rows' schema from the columns that go
// out, the two ways to send the key; `server` is the address the paths are under ("/": the
// one the description was asked at).
func OpenAPI(doc Doc, allow func(resource string) bool, kindOf KindOf, server string) map[string]any {
	codes := make([]string, 0, len(ErrorCodes))
	for _, e := range ErrorCodes {
		codes = append(codes, e.Code)
	}
	paths := map[string]any{}
	schemas := map[string]any{
		"Problem": map[string]any{
			"type": "object", "required": []string{"detail", "code"},
			"properties": map[string]any{
				"detail": map[string]any{"type": "string", "description": "What went wrong, in a sentence"},
				"code":   map[string]any{"type": "string", "enum": codes, "description": "The same, for a program"},
			},
		},
	}
	problem := func(text string) map[string]any {
		return map[string]any{"description": text,
			"content": map[string]any{"application/json": map[string]any{"schema": map[string]any{"$ref": "#/components/schemas/Problem"}}}}
	}
	for _, res := range doc.Resources {
		if allow != nil && !allow(res.Name) {
			continue
		}
		kind := func(col string) (string, bool) {
			if kindOf == nil {
				return KindOther, false
			}
			k, ok := kindOf(res.Name, col)
			if !ok || k == "" {
				return KindOther, false
			}
			return k, true
		}
		props := map[string]any{}
		for _, c := range res.Columns {
			k, typed := kind(c)
			s := outSchema(k)
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
			k, _ := kind(f.Column)
			for _, op := range f.Operators {
				name, schema, about := f.Column, inSchema(k), "equal to"
				p := map[string]any{"in": "query"}
				switch op {
				case "in":
					name, about = f.Column+"__in", "one of these (a comma-separated list)"
					schema = map[string]any{"type": "array", "items": inSchema(k), "maxItems": MaxInValues}
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
		paths["/data/"+doc.Slug+"/"+res.Name] = map[string]any{"get": map[string]any{
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
	if server == "" {
		server = "/"
	}
	return map[string]any{
		"openapi": "3.1.0",
		"info": map[string]any{"title": doc.Name, "version": doc.Version,
			"description": "A read-only data API of Tabularia. Send the key as `Authorization: Bearer tdk_…` (or in `X-API-Key`), never in the address."},
		"servers":  []any{map[string]any{"url": server}},
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

// inSchema is a parameter's value of a column of this kind
func inSchema(kind string) map[string]any {
	switch kind {
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

// outSchema is a value of a column of this kind in an answer, null included
func outSchema(kind string) map[string]any {
	switch kind {
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
