package api

import (
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestMLflowIsASupportedKindWithNoTables(t *testing.T) {
	if !supported("mlflow") || !connpayload.NotDatabases["mlflow"] {
		t.Fatal("mlflow must be supported and have no tables")
	}
	if !strings.Contains(unsupportedDBType().Detail, "'mlflow'") {
		t.Fatal(unsupportedDBType().Detail)
	}
}

func TestMLflowValidExtra(t *testing.T) {
	s := func(v string) *string { return &v }
	for extra, want := range map[string]string{
		`{}`: "", `{"auth":"bearer","ui_url":"https://mlflow.example.com"}`: "",
		`{"auth":"oauth2"}`:              "MLflow: auth must be none, basic or bearer",
		`{"verify_tls":false}`:           "",
		`{"ui_url":"nope"}`:              "MLflow: invalid UI address 'nope': expected http(s)://host[:port][/path]",
		`{"ui_url":"https://u:p@host/"}`: "MLflow: the UI address cannot hold a user or a password: use the authentication",
		`{"ca_cert":"x"}`:                "MLflow: The certificate authority cannot be read: paste it in PEM (-----BEGIN CERTIFICATE-----)",
	} {
		_, ref := validExtra(s(extra), "mlflow")
		got := ""
		if ref != nil {
			got = ref.Detail
		}
		if got != want {
			t.Errorf("%s: %q", extra, got)
		}
	}
}

func TestCheckMLflow(t *testing.T) {
	for _, c := range []struct {
		host, user, extra string
		secret            bool
		want              string
	}{
		{"https://mlflow.example.com", "", `{}`, false, ""},
		{"https://mlflow.example.com/prefix", "", `{"auth":"bearer"}`, true, ""},
		{"https://mlflow.example.com", "", `{"auth":"bearer"}`, false, "MLflow: enter the secret of the authentication (the password or the token)"},
		{"https://mlflow.example.com", "", `{"auth":"basic"}`, true, "MLflow: basic authentication needs the user"},
		{"mlflow.example.com", "", `{}`, false, "MLflow: invalid tracking URI 'mlflow.example.com': expected http(s)://host[:port][/path]"},
		{"https://mlflow.example.com?x=1", "", `{}`, false, ""},
	} {
		ref := checkMLflow(c.host, c.user, c.extra, c.secret)
		got := ""
		if ref != nil {
			got = ref.Detail
		}
		if c.host == "https://mlflow.example.com?x=1" {
			if got == "" {
				t.Errorf("a query in the tracking URI must be refused")
			}
			continue
		}
		if got != c.want {
			t.Errorf("%+v: %q", c, got)
		}
	}
}

func TestMLflowMovedNeedsTheSecretAgain(t *testing.T) {
	before := &connpayload.Conn{Host: "https://mlflow.example.com", Username: "ann", Extra: `{"auth":"basic"}`}
	for _, c := range []struct {
		host, user, extra string
		moved             bool
	}{
		{"https://mlflow.example.com/", "ann", `{"auth":"basic"}`, false},
		{"https://mlflow.example.com:443", "ann", `{"auth":"basic","verify_tls":false}`, false},
		{"https://other.example.com", "ann", `{"auth":"basic"}`, true},
		{"http://mlflow.example.com", "ann", `{"auth":"basic"}`, true},
		{"https://mlflow.example.com", "bob", `{"auth":"basic"}`, true},
		{"https://mlflow.example.com", "ann", `{"auth":"bearer"}`, true},
	} {
		if got := mlflowMoved(before, c.host, c.user, c.extra); got != c.moved {
			t.Errorf("%+v: %v", c, got)
		}
	}
}

func TestMLflowPayloadCarriesOnlyWhatTheAuthUses(t *testing.T) {
	p := connpayload.MLflow(" https://m.example.com ", "ann", "ENC", `{"auth":"bearer","ca_cert":" PEM "}`)
	if p.Value("username") != "" || p.Value("secret_encrypted") != "ENC" || p.Value("tracking_uri") != "https://m.example.com" || p.Value("ca_cert") != "PEM" {
		t.Fatalf("%v", p)
	}
	n := connpayload.MLflow("https://m", "ann", "ENC", `{"verify_tls":false,"ca_cert":"PEM"}`)
	if n.Value("secret_encrypted") != "" || n.Value("auth") != "none" || n.Value("ca_cert") != "" || n.Value("verify_tls") != false {
		t.Fatalf("%v", n)
	}
	var _ pyjson.Object = n
	if got := connpayload.Payload(&connpayload.Conn{DBType: "mlflow", Host: "https://m"}).Value("db_type"); got != "mlflow" {
		t.Fatal(got)
	}
}

func TestMLflowConfigIgnoresWhatTheAuthDoesNotUse(t *testing.T) {
	if c := mlflowConfig("https://m", "ann", "s", `{"auth":"bearer"}`); c.User != "" || c.Secret != "s" || !c.VerifyTLS {
		t.Fatalf("%+v", c)
	}
	if c := mlflowConfig("https://m", "ann", "s", `{}`); c.Secret != "" || c.Auth != "none" {
		t.Fatalf("%+v", c)
	}
}
