package aiprompts

import (
	"strings"
	"testing"
	"time"
)

func TestTheContextIsFilledIn(t *testing.T) {
	c := ExampleContext("assistant", "it-IT", false, 77, time.Date(2026, 11, 4, 0, 0, 0, 0, time.UTC))
	if !strings.Contains(c, "Today is 2026-11-04.") || !strings.Contains(c, "capped at 77 rows") || !strings.Contains(c, "Italian") {
		t.Error(c)
	}
	if _, ok := Default("nope"); ok || len(Agents()) != 7 {
		t.Error("agents")
	}
}
