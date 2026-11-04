package masking

import "testing"

func TestMaskEmail(t *testing.T) {
	for in, want := range map[string]string{
		"alice.rossi@esempio.it": "a***@esempio.it", "@x.it": "***@x.it", "nobody": "***", "": "",
		"  b@y  ": "b***@y", "a@b@c": "a***@c", "èlan@x": "è***@x",
	} {
		if got := Email(in); got != want {
			t.Errorf("%q: got %q, want %q", in, got, want)
		}
	}
}
