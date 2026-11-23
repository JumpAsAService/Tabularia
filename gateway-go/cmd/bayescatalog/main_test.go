package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

func TestRunWritesTheSnapshotTheGatewayEmbeds(t *testing.T) {
	out := filepath.Join(t.TempDir(), "catalog.json")
	var stdout bytes.Buffer
	if err := run([]string{"-o", out}, &stdout); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, bayescatalog.Raw()) {
		t.Fatal("the command's output is not the embedded snapshot: run it from gateway-go/")
	}
	if !strings.Contains(stdout.String(), "distributions") {
		t.Fatalf("stdout %q", stdout.String())
	}
}

func TestRunRefusesArgumentsAndUnwritablePaths(t *testing.T) {
	if err := run([]string{"extra"}, &bytes.Buffer{}); err == nil {
		t.Error("an argument is refused")
	}
	if err := run([]string{"-o", filepath.Join(t.TempDir(), "missing", "catalog.json")}, &bytes.Buffer{}); err == nil {
		t.Error("an unwritable path is an error")
	}
}
