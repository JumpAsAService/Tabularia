package api

import (
	"context"
	"net/http"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The `mlflow` kind of connection (docs/specs/statistical-models.md, «MLflow» → «The
// connection»): a tracking server outside Tabularia. Host = the tracking URI, username =
// the user of the basic authentication, password = the basic password or the bearer token
// (encrypted, never read back), extra = {auth, verify_tls, ca_cert, ui_url}. The gateway
// tests it itself (package mlflowconn): read-only calls with the connection's credentials.

// mlflowOptionError is validExtra's check of the kind's options.
func mlflowOptionError(data pyjson.Object) string {
	if e := mlflowconn.OptionError(data); e != "" {
		return e
	}
	if u := pystr.Strip(pyjson.Str(pyjson.Or(data.Value("ui_url"), ""))); u != "" {
		e, err := urlError(u, "UI address")
		if err != nil {
			panic(errURL{err})
		}
		return e
	}
	return ""
}

// checkMLflow is the check of a connection's fields (the _check_api of this kind).
func checkMLflow(host, username, extra string, hasSecret bool) *auth.Refusal {
	e, err := urlError(host, "tracking URI")
	if err != nil {
		panic(errURL{err})
	}
	a := mlflowconn.ParseOptions(extra).Auth
	if e == "" && a == "basic" && pystr.Strip(username) == "" {
		e = "basic authentication needs the user"
	}
	if e == "" && a != "none" && !hasSecret {
		e = "enter the secret of the authentication (the password or the token)"
	}
	if e != "" {
		return unprocessable("MLflow: " + e)
	}
	return nil
}

// mlflowMoved says whether an edit points the stored secret somewhere it was not given for:
// another server, another kind of authentication, another user. The secret then has to be
// typed again, as for the API connection.
func mlflowMoved(before *connpayload.Conn, host, username, extra string) bool {
	b := mlflowconn.ParseOptions(before.Extra)
	n := mlflowconn.ParseOptions(extra)
	s1, h1, p1, err := apiOrigin(host)
	if err != nil {
		panic(errURL{err})
	}
	s2, h2, p2, err := apiOrigin(before.Host)
	if err != nil {
		panic(errURL{err})
	}
	return s1 != s2 || h1 != h2 || p1 != p2 || b.Auth != n.Auth || (n.Auth == "basic" && pystr.Strip(username) != pystr.Strip(before.Username))
}

// mlflowConfig is the test's configuration from the connection's fields and the secret in clear.
func mlflowConfig(host, username, secret, extra string) mlflowconn.Config {
	o := mlflowconn.ParseOptions(extra)
	cfg := mlflowconn.Config{URI: pystr.Strip(host), Auth: o.Auth, VerifyTLS: o.VerifyTLS, CACert: o.CACert}
	if o.Auth != "none" {
		cfg.Secret = secret
	}
	if o.Auth == "basic" {
		cfg.User = pystr.Strip(username)
	}
	return cfg
}

// answerMLflowTest runs the test and answers its Result (200 even when a check fails: the
// codes are what the interface words in the person's language).
func answerMLflowTest(w http.ResponseWriter, r *http.Request, cfg mlflowconn.Config) {
	ctx, cancel := context.WithTimeout(r.Context(), 40*time.Second)
	defer cancel()
	server.JSON(w, http.StatusOK, mlflowconn.Test(ctx, cfg))
}

// testSavedMLflow tests a saved connection: its secret decrypted here, the options of the
// form (a change not saved yet) when sent.
func testSavedMLflow(d *Deps, w http.ResponseWriter, r *http.Request, conn *connpayload.Conn, extra *string) {
	defer recoverURL(d, w)
	use := conn.Extra
	if extra != nil {
		raw, ref := validExtra(extra, "mlflow")
		if ref != nil {
			server.Refuse(w, ref)
			return
		}
		use = raw
	}
	secret := ""
	if conn.PasswordEncrypted != "" {
		if d.Fernet == nil {
			fail500(d, w, "connections", errEncrypt)
			return
		}
		var err error
		if secret, err = d.Fernet.Decrypt(conn.PasswordEncrypted); err != nil {
			fail500(d, w, "connections", err)
			return
		}
	}
	if ref := checkMLflow(conn.Host, conn.Username, use, secret != ""); ref != nil {
		server.Refuse(w, ref)
		return
	}
	answerMLflowTest(w, r, mlflowConfig(conn.Host, conn.Username, secret, use))
}

// mlflowAuditDetail marks in the audit a connection that does not verify the server's
// certificate.
func mlflowAuditDetail(d pyjson.Object, extra string) pyjson.Object {
	if o := mlflowconn.ParseOptions(extra); !o.VerifyTLS {
		d = append(d, pyjson.KV{Key: "tls_verification", Value: "off"})
	}
	return d
}
