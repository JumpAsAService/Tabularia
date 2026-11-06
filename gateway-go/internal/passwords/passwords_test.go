package passwords

import (
	"strings"
	"testing"
)

// A hash written by Python's bcrypt (5.0, gensalt(4)) for «correct horse battery».
const fromPython = "$2b$04$7og827wiP3wHWOE6B.bndO5kB6MM6sy6VEpdVNQWXIRGbqnpTINKi"

func TestReadsPython(t *testing.T) {
	if !Verify("correct horse battery", fromPython) || Verify("correct horse batter", fromPython) {
		t.Fatal("the Python hash")
	}
}

func TestRoundTrip(t *testing.T) {
	h, err := Hash("a password long enough")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(h, "$2b$12$") || !Verify("a password long enough", h) || Verify("another", h) {
		t.Fatalf("hash %q", h)
	}
	if Verify(strings.Repeat("a", 80), h) || Verify("x", "not a hash") {
		t.Fatal("a refusal expected")
	}
}
