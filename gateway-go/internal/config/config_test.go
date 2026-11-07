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
	c, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://gateway:8000", "SECURITY__FERNET_KEY": "k"}))
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
	c, err := Load(env(map[string]string{"gateway_go__upstream": "http://g:8000", "security__fernet_key": "k", "db__host": "pg", "DB__PASSWORD": "a@b/c% d", "DB__USER": "tab user"}))
	if err != nil {
		t.Fatal(err)
	}
	if c.DSN != "postgresql://tab%20user:a%40b%2Fc%25%20d@pg:5432/tabularia" {
		t.Errorf("dsn %q", c.DSN)
	}
	c, _ = Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__FERNET_KEY": "k", "DB__URL": "postgresql://x@y/z", "DB__HOST": "ignored"}))
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
	for _, name := range []string{"AUTH__SESSION_IDLE_MINUTES", "AUTH__API_TOKEN_MAX_DAYS", "AUTH__PERSONAL_TOKENS", "JWT__ALGORITHM"} {
		if !strings.Contains(err.Error(), name) {
			t.Errorf("%s not named in %q", name, err)
		}
	}
}

func TestBooleansAsPydanticReadsThem(t *testing.T) {
	for v, want := range map[string]bool{"1": true, "True": true, "yes": true, "on": true, "0": false, "FALSE": false, "no": false, "off": false} {
		c, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__FERNET_KEY": "k", "AUTH__PERSONAL_TOKENS": v}))
		if err != nil || c.PersonalTokens != want {
			t.Errorf("%q → %v (%v)", v, c, err)
		}
	}
}

// Settings._key_from_keys_dir: without the key, the one in SECURITY__KEYS_DIR, created once
func TestTheFernetKeyFromTheSharedDirectory(t *testing.T) {
	dir := t.TempDir()
	vars := map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__KEYS_DIR": dir}
	a, err := Load(env(vars))
	if err != nil || len(a.FernetKey) != 44 {
		t.Fatalf("%v %v", err, a != nil)
	}
	b, _ := Load(env(vars))
	if b.FernetKey != a.FernetKey {
		t.Error("a second start must read the same key")
	}
	if _, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000"})); err == nil || !strings.Contains(err.Error(), "SECURITY__FERNET_KEY") {
		t.Errorf("without a key the gateway must not start: %v", err)
	}
}

// GATEWAY_GO__AI_SERVICE: read as an address; cmd/gateway refuses to start without it
func TestTheAIServiceAddress(t *testing.T) {
	c, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__FERNET_KEY": "k", "GATEWAY_GO__AI_SERVICE": "http://ai:8000"}))
	if err != nil || c.AIService == nil || c.AIService.Host != "ai:8000" {
		t.Fatalf("%v %v", c, err)
	}
	if _, err := Load(env(map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__FERNET_KEY": "k", "GATEWAY_GO__AI_SERVICE": "ai"})); err == nil ||
		!strings.Contains(err.Error(), "GATEWAY_GO__AI_SERVICE") {
		t.Errorf("a bad address must be refused by name: %v", err)
	}
}

// GATEWAY_GO__UPSTREAM is optional: nothing is forwarded to the Python gateway any more; set,
// it must still be an address
func TestTheUpstreamIsOptional(t *testing.T) {
	c, err := Load(env(map[string]string{"SECURITY__FERNET_KEY": "k"}))
	if err != nil || c.Upstream != nil {
		t.Fatalf("without it: %v %v", c, err)
	}
	if _, err := Load(env(map[string]string{"SECURITY__FERNET_KEY": "k", "GATEWAY_GO__UPSTREAM": "gateway"})); err == nil ||
		!strings.Contains(err.Error(), "GATEWAY_GO__UPSTREAM") {
		t.Errorf("a bad address: %v", err)
	}
}

// JWT__ACCESS_TTL_MINUTES: any whole number, as pydantic's int (a bound min == max refused them all)
func TestAccessTTLMinutesUnbounded(t *testing.T) {
	c, err := Load(env(map[string]string{"SECURITY__FERNET_KEY": "k", "GATEWAY_GO__AI_SERVICE": "http://ai:8000", "JWT__ACCESS_TTL_MINUTES": "720"}))
	if err != nil || c.AccessTTLMinutes != 720 {
		t.Fatalf("got %v, %v", c, err)
	}
}

// The data APIs' public address (the chart's dataApi.publicHost): trimmed, an http(s) address.
func TestTheDataAPIsPublicAddress(t *testing.T) {
	base := map[string]string{"GATEWAY_GO__UPSTREAM": "http://g:8000", "SECURITY__FERNET_KEY": "k"}
	c, err := Load(env(base))
	if err != nil || c.DataAPI.Pods.PublicBaseURL != "" {
		t.Fatalf("%v %q", err, c.DataAPI.Pods.PublicBaseURL)
	}
	base["DATA_API__PUBLIC_BASE_URL"] = " https://api.example.com/ "
	base["DATA_API__INGRESS_HOST"], base["DATA_API__INGRESS_TLS_SECRET"] = "api.example.com", "api-tls"
	if c, err = Load(env(base)); err != nil || c.DataAPI.Pods.PublicBaseURL != "https://api.example.com" ||
		c.DataAPI.Pods.IngressHost != "api.example.com" || c.DataAPI.Pods.IngressTLSSecret != "api-tls" {
		t.Fatalf("%v %+v", err, c.DataAPI.Pods)
	}
	base["DATA_API__PUBLIC_BASE_URL"] = "api.example.com"
	if _, err := Load(env(base)); err == nil || !strings.Contains(err.Error(), "DATA_API__PUBLIC_BASE_URL") {
		t.Fatalf("%v", err)
	}
}
