package personalfolders

import "testing"

func TestFolderName(t *testing.T) {
	for in, want := range map[string]string{"leonardtriv@esempio.it": "leonardtriv", " a @b": "a", "@b": "@b", "  ": "personal", "nobody": "nobody"} {
		if got := FolderName(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
}
