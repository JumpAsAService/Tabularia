package connpayload

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func dump(t *testing.T, o pyjson.Object) string {
	t.Helper()
	s, err := pyjson.Dumps(o, pyjson.Options{})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestPayloadOfEveryKind(t *testing.T) {
	port := int64(0)
	cases := []struct {
		conn Conn
		want string
	}{
		{Conn{DBType: "smtp", Host: "m", Port: &port, Username: "u", PasswordEncrypted: "E", Extra: `{"from_address": 5, "tls": ""}`},
			`{"db_type": "smtp", "host": "m", "port": 587, "username": "u", "password_encrypted": "E", "from_address": 5, "from_name": "", "tls": "starttls"}`},
		{Conn{DBType: "sharepoint", Host: " https://s ", Username: " c ", PasswordEncrypted: "E", Database: " t ", Extra: `{"library": 1.0}`},
			`{"tenant_id": "t", "client_id": "c", "client_secret_encrypted": "E", "site_url": "https://s", "library": "1.0"}`},
		{Conn{DBType: "sftp", Host: " h ", Username: " u ", PasswordEncrypted: "P", PrivateKeyEncrypted: "K", Extra: `{"auth": "key"}`},
			`{"db_type": "sftp", "host": "h", "port": 22, "username": "u", "auth": "key", "password_encrypted": "", "private_key_encrypted": "K", "host_key_type": "", "host_key_fingerprint": ""}`},
		{Conn{DBType: "api", Host: " https://a ", Username: " u ", PasswordEncrypted: "S",
			Extra: `{"auth": "basic", "key_in": "x", "headers": {" A ": 1, "A": "two"}, "scope": null}`},
			`{"db_type": "api", "base_url": "https://a", "auth": "basic", "username": "u", "secret_encrypted": "S", "key_name": "", "key_in": "header", "token_url": "", "scope": "", "client_auth": "body", "headers": {"A": "two"}, "test_path": ""}`},
		{Conn{DBType: "api", PasswordEncrypted: "S", Extra: `not json`},
			`{"db_type": "api", "base_url": "", "auth": "none", "username": "", "secret_encrypted": "", "key_name": "", "key_in": "header", "token_url": "", "scope": "", "client_auth": "body", "headers": {}, "test_path": ""}`},
		{Conn{DBType: "postgresql", Host: "h", Extra: `{"tls": "verify", "ca_cert": " C "}`},
			`{"db_type": "postgresql", "host": "h", "port": null, "username": "", "password_encrypted": "", "database": "", "db_schema": "", "extra": {"tls": "verify", "ca_cert": "C"}}`},
		{Conn{DBType: "gsheets", Host: "e", Database: "p", PasswordEncrypted: "K"},
			`{"db_type": "gsheets", "key_encrypted": "K", "client_email": "e", "project_id": "p"}`},
	}
	for _, c := range cases {
		if got := dump(t, Payload(&c.conn)); got != c.want {
			t.Errorf("%s:\n got %s\nwant %s", c.conn.DBType, got, c.want)
		}
	}
}

func TestDomainList(t *testing.T) {
	for _, c := range []struct {
		raw  any
		want []string
		ok   bool
	}{
		{nil, []string{}, true}, {"", []string{}, true}, {"A.test, @b.TEST,, ", []string{"a.test", "b.test"}, true},
		{[]any{"x", " "}, []string{"x"}, true}, {[]any{"x", 1}, nil, false}, {5, nil, false}, {false, nil, false},
	} {
		got, ok := DomainList(c.raw)
		if ok != c.ok || len(got) != len(c.want) {
			t.Errorf("%v: %v %v", c.raw, got, ok)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("%v: %v", c.raw, got)
			}
		}
	}
}
