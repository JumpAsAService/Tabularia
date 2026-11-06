package auth

import (
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

var secret = []byte("a-secret-for-the-tests-only-32-bytes!")

func sign(t *testing.T, alg jwt.SigningMethod, key []byte, claims jwt.MapClaims) string {
	t.Helper()
	s, err := jwt.NewWithClaims(alg, claims).SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func at(now time.Time) *Authenticator {
	return &Authenticator{Secret: secret, Algorithm: "HS256", Now: func() time.Time { return now }}
}

// what core/security.py signs: sub as a string, sid, iat, exp
func TestATokenOfTheGatewayIsRead(t *testing.T) {
	now := time.Now()
	tok := sign(t, jwt.SigningMethodHS256, secret, jwt.MapClaims{"sub": "7", "sid": "abc", "iat": now.Unix(), "exp": now.Add(time.Hour).Unix()})
	id, sid, ok := at(now).parseJWT(tok)
	if !ok || id != 7 || sid != "abc" {
		t.Fatalf("%d %q %v", id, sid, ok)
	}
}

func TestRefused(t *testing.T) {
	now := time.Now()
	base := func() jwt.MapClaims {
		return jwt.MapClaims{"sub": "7", "sid": "abc", "iat": now.Unix(), "exp": now.Add(time.Hour).Unix()}
	}
	cases := map[string]string{}
	c := base()
	delete(c, "sid")
	cases["without a session (before the sessions)"] = sign(t, jwt.SigningMethodHS256, secret, c)
	c = base()
	c["exp"] = now.Add(-2 * time.Minute).Unix()
	cases["expired past the minute of leeway"] = sign(t, jwt.SigningMethodHS256, secret, c)
	c = base()
	c["iat"] = now.Add(2 * time.Minute).Unix()
	cases["issued in the future past the leeway"] = sign(t, jwt.SigningMethodHS256, secret, c)
	c = base()
	c["sub"] = 7
	cases["sub a number (PyJWT wants a string)"] = sign(t, jwt.SigningMethodHS256, secret, c)
	c = base()
	c["sub"] = "seven"
	cases["sub not a whole number"] = sign(t, jwt.SigningMethodHS256, secret, c)
	cases["another key"] = sign(t, jwt.SigningMethodHS256, []byte("another-secret-another-secret-0000"), base())
	cases["another algorithm"] = sign(t, jwt.SigningMethodHS512, secret, base())
	cases["not a token"] = "abc.def.ghi"
	for name, tok := range cases {
		if _, _, ok := at(now).parseJWT(tok); ok {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestAMinuteOfLeewayHolds(t *testing.T) {
	now := time.Now()
	tok := sign(t, jwt.SigningMethodHS256, secret, jwt.MapClaims{"sub": "7", "sid": "abc", "iat": now.Add(30 * time.Second).Unix(), "exp": now.Add(-30 * time.Second).Unix()})
	if _, _, ok := at(now).parseJWT(tok); !ok {
		t.Error("a clock 30 s apart must hold")
	}
}

func TestSubAsPythonsInt(t *testing.T) {
	for in, want := range map[string]int64{"7": 7, " 7 ": 7, "+7": 7, "1_000": 1000} {
		if got, err := pythonInt(in); err != nil || got != want {
			t.Errorf("%q → %d %v", in, got, err)
		}
	}
	for _, in := range []string{"", "_1", "1_", "1__0", "7.0", "0x7"} {
		if _, err := pythonInt(in); err == nil {
			t.Errorf("%q accepted", in)
		}
	}
}
