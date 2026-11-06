package mcp

import (
	"strings"
	"testing"
)

func TestTheSnapshotReads(t *testing.T) {
	if len(Tools("read", false)) == 0 || len(Tools("write", false)) <= len(Tools("read", false)) ||
		len(Tools("write", true)) >= len(Tools("write", false)) {
		t.Fatal("tools.json")
	}
	if !strings.Contains(Instructions("read", false, false), "This token reads only") ||
		strings.Contains(Instructions("write", false, false), "This token reads only") {
		t.Error("instructions")
	}
}
