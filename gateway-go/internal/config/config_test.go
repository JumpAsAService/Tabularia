package config

import (
	"strings"
	"testing"
)

func env(vars map[string]string) Env {
	up := map[string]string{}
	for k, v := range vars {
		up[strings.ToUpper(k)] = v
	}
	return func(name string) (string, bool) { v, ok := up[strings.ToUpper(name)]; return v, ok }
}

func TestDefaultsAreThePythonGateways(t *testing.T) {
	c, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://gateway:8000"}))
	if err != nil {
		t.Fatal(err)
	}
	if c.DSN != "postgresql://tabularia:tabularia@localhost:5432/tabularia" {
		t.Errorf("dsn %q", c.DSN)
	}
	if c.JWTAlgorithm != "HS256" || string(c.JWTSecret) != "change-me-in-production" {
		t.Errorf("jwt %q %q", c.JWTAlgorithm, c.JWTSecret)
	}
	if c.SessionIdleMinutes != 0 || !c.PersonalTokens || c.APITokenMaxDays != 365 || !c.SettingsPanel {
		t.Errorf("auth %+v", c)
	}
	if c.Listen != ":8000" || len(c.ForwardedAllowIPs) != 1 || c.ForwardedAllowIPs[0] != "127.0.0.1" {
		t.Errorf("listen %q trusted %v", c.Listen, c.ForwardedAllowIPs)
	}
}

func TestTheDatabaseAddressEscapesUserAndPassword(t *testing.T) {
	c, err := Load(env(map[string]string{"gateway_go__upstream": "http://g:8000", "db__host": "pg", "DB__PASSWORD": "a@b/c% d", "DB__USER": "tab user"}))
	if err != nil {
		t.Fatal(err)
	}
	if c.DSN != "postgresql://tab%20user:a%40b%2Fc%25%20d@pg:5432/tabularia" {
		t.Errorf("dsn %q", c.DSN)
	}
	c, _ = Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "DB__URL": "postgresql://x@y/z", "DB__HOST": "ignored"}))
	if c.DSN != "postgresql://x@y/z" {
		t.Errorf("DB__URL must win: %q", c.DSN)
	}
}

func TestBadValuesAreRefusedByName(t *testing.T) {
	_, err := Load(env(map[string]string{
		"AUTH__SESSION_IDLE_MINUTES": "-1", "AUTH__API_TOKEN_MAX_DAYS": "9999",
		"AUTH__PERSONAL_TOKENS": "maybe", "JWT__ALGORITHM": "none",
	}))
	if err == nil {
		t.Fatal("expected an error")
	}
	for _, name := range []string{"GATEWAY_GO__UPSTREAM", "AUTH__SESSION_IDLE_MINUTES", "AUTH__API_TOKEN_MAX_DAYS", "AUTH__PERSONAL_TOKENS", "JWT__ALGORITHM"} {
		if !strings.Contains(err.Error(), name) {
			t.Errorf("%s not named in %q", name, err)
		}
	}
}

func TestBooleansAsPydanticReadsThem(t *testing.T) {
	for v, want := range map[string]bool{"1": true, "True": true, "yes": true, "on": true, "0": false, "FALSE": false, "no": false, "off": false} {
		c, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "AUTH__PERSONAL_TOKENS": v}))
		if err != nil || c.PersonalTokens != want {
			t.Errorf("%q → %v (%v)", v, c, err)
		}
	}
}
