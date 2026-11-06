package pyurl

import (
	"errors"
	"testing"
)

// str.lower as CPython 3.12 answers it (python3 -c 'print(repr(s.lower()))').
func TestLowerIsPythons(t *testing.T) {
	for in, want := range map[string]string{
		"HOST.Test": "host.test", "BÜCHER": "bücher", "İstanbul": "i̇stanbul", "ΟΔΟΣ": "οδος", "ΟΔΟΣ.test": "οδοσ.test",
		"ΑΣ.ΣΑ": "ασ.σα", "Σ": "σ", "ΑΣ'": "ας'", "ΑΣ'Α": "ασ'α", "ǅ": "ǆ", "ΑΣ́": "ας́",
	} {
		if got := Lower(in); got != want {
			t.Errorf("Lower(%q) = %q, want %q", in, got, want)
		}
	}
}

// httpx's encoding of a host that is not ASCII is the encoder's; without one, ErrNotASCII.
func TestAHostThatIsNotASCIIIsEncodedByTheEncoder(t *testing.T) {
	if _, err := HTTPXURL("http://ü.test/"); !errors.Is(err, ErrNotASCII) {
		t.Fatalf("no encoder: %v", err)
	}
	UseIDNA(func(host string) (string, bool, error) {
		switch host {
		case "Ü.test":
			return "xn--tda.test", true, nil
		case "☃":
			return "", false, nil
		}
		return "", false, errors.New("down")
	})
	defer UseIDNA(nil)
	if u, err := HTTPXURL("http://Ü.test:81/x"); err != nil || u.Host != "xn--tda.test" || u.Port.Int64() != 81 {
		t.Errorf("encoded: %+v %v", u, err)
	}
	if _, err := HTTPXURL("http://☃/"); err == nil || err.Error() != "Invalid IDNA hostname: '☃'" {
		t.Errorf("refused: %v", err)
	}
	if _, err := HTTPXURL("http://é/"); !errors.Is(err, ErrIDNAUnavailable) {
		t.Errorf("not asked: %v", err)
	}
	if s, err := URLSplit("http://ΟΔΟΣ.test/"); err != nil || s.HostnameOrNone() != "οδοσ.test" {
		t.Errorf("urlsplit: %+v %v", s, err)
	}
}
