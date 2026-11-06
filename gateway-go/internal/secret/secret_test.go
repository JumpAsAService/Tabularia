package secret

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

// a test key, not a secret of anything
const key = "Vd3m7oIM2w3o2r6Y-ZbM6O1t3Qk3c7HnS0WBu6Mxx2E="

// Fernet(key).encrypt("s3cr3t pässword") and internal_token() with that key, by the Python
// gateway's own cryptography and hmac
const (
	pythonToken    = "gAAAAABq6kF26KEAT2iYfv4iZ49noIqJkOGy4d8iURuyUd2fnmevkBiLaSAfLVahSoUGh9gFr4cBYP_lL7-CflzSHVymgUrkC8teB_Kwj25TIvCk9r33CR8="
	pythonInternal = "af85627a92713e0c62bd3f3b06a114513d1f11348ad08db1d0303de3b311c71d"
)

func TestFernetReadsPythonsTokens(t *testing.T) {
	f, err := NewFernet(key)
	if err != nil {
		t.Fatal(err)
	}
	if back, err := f.Decrypt(pythonToken); err != nil || back != "s3cr3t pässword" {
		t.Fatalf("%q %v", back, err)
	}
}

func TestInternalTokenAsPythons(t *testing.T) {
	if got := InternalToken(key); got != pythonInternal {
		t.Errorf("%s, want %s", got, pythonInternal)
	}
}

// What is written here, cryptography reads: with Docker at hand (FERNET_PY=1).
func TestCryptographyReadsOurTokens(t *testing.T) {
	if os.Getenv("FERNET_PY") == "" {
		t.Skip("FERNET_PY=1 with Docker checks it against cryptography")
	}
	f, _ := NewFernet(key)
	tok, _ := f.Encrypt("dal go")
	out, err := exec.Command("docker", "run", "--rm", "tabularia-gateway:latest", "python", "-c",
		"import sys; from cryptography.fernet import Fernet; print(Fernet(sys.argv[1]).decrypt(sys.argv[2].encode()).decode())",
		key, tok).Output()
	if err != nil || strings.TrimSpace(string(out)) != "dal go" {
		t.Fatalf("%q %v", out, err)
	}
}

func TestRoundTripAndTampering(t *testing.T) {
	f, _ := NewFernet(key)
	tok, _ := f.Encrypt("x")
	if back, err := f.Decrypt(tok); err != nil || back != "x" {
		t.Fatal(back, err)
	}
	bad := []byte(tok)
	bad[len(bad)-5] ^= 1
	if _, err := f.Decrypt(string(bad)); err == nil {
		t.Error("a changed token was read")
	}
	if _, err := NewFernet("short"); err == nil {
		t.Error("a bad key was taken")
	}
}
