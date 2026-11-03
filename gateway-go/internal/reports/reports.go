// Package reports is services/reports.py: the reports, their definitions and how a render is
// prepared with the reader's rights.
package reports

import (
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/versioned"
)

// Kind is the reports' tables (models/report.py).
var Kind = &versioned.Kind{Label: "report", Table: "reports", Versions: "report_versions", FK: "report_id",
	Views: "report_views", NotFound: "Report not found", Sizes: []string{"datasets", "parameters"},
	Empty: func() pyjson.Object { return Empty("en", "UTC") }}

// Pushmail is where a Pushmail cell names a report.
var Pushmail = versioned.Pushmail{List: "reports", IDKey: "reportId"}

// Locales are report_definition.LOCALES.
var Locales = []string{"en", "it", "de", "es", "fr"}

// HasLocale is `locale in LOCALES`.
func HasLocale(s string) bool {
	for _, x := range Locales {
		if x == s {
			return true
		}
	}
	return false
}

// Empty is report_definition.empty: a new report's definition, an A4 page and no data yet.
func Empty(locale, timezone string) pyjson.Object {
	if !HasLocale(locale) {
		locale = "en"
	}
	if timezone == "" {
		timezone = "UTC"
	}
	return pyjson.Object{
		{Key: "v", Value: int64(1)},
		{Key: "page", Value: pyjson.Object{{Key: "size", Value: "A4"}, {Key: "orientation", Value: "portrait"},
			{Key: "margins", Value: []any{int64(15), int64(12), int64(15), int64(12)}}}},
		{Key: "locale", Value: locale},
		{Key: "timezone", Value: timezone},
		{Key: "theme", Value: pyjson.Object{{Key: "font", Value: "IBM Plex Sans"}, {Key: "size", Value: int64(9)},
			{Key: "color", Value: "#1f2933"}, {Key: "accent", Value: "#3b5bdb"}}},
		{Key: "parameters", Value: []any{}},
		{Key: "datasets", Value: []any{}},
		{Key: "sections", Value: pyjson.Object{
			{Key: "page_header", Value: pyjson.Object{{Key: "height", Value: int64(18)}, {Key: "on_first_page", Value: true},
				{Key: "items", Value: []any{}}}},
			{Key: "body", Value: pyjson.Object{{Key: "items", Value: []any{}}}},
			{Key: "page_footer", Value: pyjson.Object{{Key: "height", Value: int64(10)}, {Key: "on_last_page", Value: true},
				{Key: "items", Value: []any{}}}},
		}},
	}
}
