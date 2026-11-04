package api

import "testing"

func TestDuration(t *testing.T) {
	for m, want := range map[int]string{0: "none — entries are kept until an administrator removes them", 1: "1 minute",
		59: "59 minutes", 60: "1 hour", 120: "2 hours", 1440: "1 day", 2880: "2 days", 90: "90 minutes"} {
		if got := duration(m); got != want {
			t.Errorf("%d: %q, want %q", m, got, want)
		}
	}
}

func TestLikePattern(t *testing.T) {
	if got := likePattern(`a_b%c\d`); got != `%a\_b\%c\\d%` {
		t.Error(got)
	}
}

func TestQuoteURL(t *testing.T) {
	for in, want := range map[string]string{
		"http://localhost:3000/auth/callback#error=sso_disabled": "http://localhost:3000/auth/callback#error=sso_disabled",
		"https://x.test/a b/è?q=1&r=~":                           "https://x.test/a%20b/%C3%A8?q=1&r=~",
	} {
		if got := quoteURL(in); got != want {
			t.Errorf("%q: %q", in, got)
		}
	}
}

func TestParseMemory(t *testing.T) {
	m, err := parseMemory("node_memory_MemTotal_bytes 3\nnode_memory_MemAvailable_bytes 2.0\n")
	if err != nil || m.UsedPercent != 33.3 || m.UsedBytes != 1 {
		t.Fatalf("%+v %v", m, err)
	}
	if _, err := parseMemory("node_memory_MemTotal_bytes 0\nnode_memory_MemAvailable_bytes 0\n"); err == nil {
		t.Error("a total of zero")
	}
	if m, _ := parseMemory("node_memory_MemTotal_bytes 10\nnode_memory_MemAvailable_bytes 20\n"); m.UsedBytes != 0 {
		t.Error("never below zero")
	}
}
