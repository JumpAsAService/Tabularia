package api

import "testing"

func TestSmallPorts(t *testing.T) {
	if maskEmail("alice.rossi@esempio.it") != "a***@esempio.it" || maskEmail("@x") != "***@x" || maskEmail("bob") != "***" {
		t.Error("mask_email")
	}
	if pyRound(2.675, 2) != 2.67 || pyRound(0.125, 2) != 0.12 || pyRound(1.0/3, 2) != 0.33 {
		t.Error("round")
	}
	if l, r := parseRecipients("a@x.it; b@y.it,, "); r != nil || len(l) != 2 {
		t.Error("recipients")
	}
	if _, r := parseRecipients("x@"); r == nil || r.Detail != "Invalid address: x@" {
		t.Error("bad recipient")
	}
	for def, want := range map[string]bool{`{"nodes": [{"type": "output"}]}`: true, `{"nodes": [null, {"type": "x"}]}`: false, `nope`: false, ``: false} {
		if got, err := hasRunnableNodes(def); err != nil || got != want {
			t.Errorf("%s: %v %v", def, got, err)
		}
	}
	if _, err := hasRunnableNodes(`[1]`); err == nil {
		t.Error("a list is no definition")
	}
	if fromTimestamp(1.5) != "1970-01-01T00:00:01.500000+00:00" {
		t.Error(fromTimestamp(1.5))
	}
}
