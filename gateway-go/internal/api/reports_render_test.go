package api

import "testing"

// routes/proxy.py ascii_fallback's answers, asked of the gateway's image
func TestASCIIFallbackAsPython(t *testing.T) {
	for in, want := range map[string]string{
		"attachment; filename*=utf-8''Vendite%20%C3%A0%20Nord.pdf": "attachment; filename=\"Vendite a Nord.pdf\"; filename*=utf-8''Vendite%20%C3%A0%20Nord.pdf",
		"attachment; filename=\"x.pdf\"":                           "attachment; filename=\"x.pdf\"",
		"attachment; filename*=UTF-8''%E2%82%AC.xlsx":              "attachment; filename=\"report.xlsx\"; filename*=UTF-8''%E2%82%AC.xlsx",
		"inline; filename*=utf-8''r%C3%A9sum%C3%A9; size=3":        "inline; filename=\"resume\"; filename*=utf-8''r%C3%A9sum%C3%A9; size=3",
		"attachment; filename*=utf-8''noext":                       "attachment; filename=\"noext\"; filename*=utf-8''noext",
		"attachment; filename*=utf-8''a%22b.p%C3%B6f":              "attachment; filename=\"a b.pf\"; filename*=utf-8''a%22b.p%C3%B6f",
	} {
		if got := asciiFallback(in); got != want {
			t.Errorf("%s\n got  %s\n want %s", in, got, want)
		}
	}
}
