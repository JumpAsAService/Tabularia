package notebookpod

import (
	"os"
	"testing"
)

// The copy is the Python gateway's file (gateway/app/notebook_pod/tabularia.py): copy it again
// when that one changes.
func TestTheSameAsPythons(t *testing.T) {
	want, err := os.ReadFile("../../../gateway/app/notebook_pod/tabularia.py")
	if err != nil {
		t.Skip("the Python gateway's source is not here")
	}
	if string(want) != Module {
		t.Fatal("internal/notebookpod/tabularia.py differs from gateway/app/notebook_pod/tabularia.py: copy it again")
	}
}
