package main

// A fake OpenID Connect provider at /idp/ (the SSO suite: OIDC__ISSUER=http://siem.test:8000/idp,
// each stack's own fake engine): discovery, its keys (an RSA key made at every start), the
// token endpoint and userinfo. The authorization code is the base64url of a JSON that says
// which tokens to give: {"nonce", "challenge", "claims", "status", …}, written by the suite
// with the nonce and the PKCE challenge of the side's own sign-in.

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"math/big"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

var idpKey, idpOtherKey = mustKey(), mustKey()

func mustKey() *rsa.PrivateKey {
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		panic(err)
	}
	return k
}

// idpSpec is what the code asks of the token endpoint.
type idpSpec struct {
	Nonce     string         `json:"nonce"`
	Challenge string         `json:"challenge"`
	Claims    map[string]any `json:"claims"` // on top of the defaults; null removes one
	Status    int            `json:"status"` // the token endpoint's answer
	Raw       string         `json:"raw"`    // a body that is not JSON
	NoIDToken bool           `json:"no_id"`  // no id_token
	Kid       string         `json:"kid"`    // another kid (not in the keys)
	OtherKey  bool           `json:"other"`  // signed by a key the provider does not publish
	Access    map[string]any `json:"access"` // the access token: a JWT with these claims
	Opaque    map[string]any `json:"opaque"` // or an opaque one whose userinfo is this
	ExpIn     *int64         `json:"exp_in"` // the id_token's exp, seconds from now
}

func idpBase(r *http.Request) string { return "http://" + r.Host + "/idp" }

// idpRecord is the token request as recorded: the code and the verifier are random on each
// side, the PKCE check is what is compared.
func idpRecord(raw []byte) any {
	form, err := url.ParseQuery(string(raw))
	if err != nil {
		return string(raw)
	}
	out := map[string]any{}
	for k := range form {
		out[k] = form.Get(k)
	}
	if code, ok := out["code"].(string); ok {
		out["code"] = "<code>"
		out["pkce"] = "bad"
		if spec, err := decodeSpec(code); err == nil {
			sum := sha256.Sum256([]byte(form.Get("code_verifier")))
			if base64.RawURLEncoding.EncodeToString(sum[:]) == spec.Challenge {
				out["pkce"] = "ok"
			}
		} else {
			out["code"] = code
		}
	}
	delete(out, "code_verifier")
	return out
}

func decodeSpec(code string) (*idpSpec, error) {
	raw, err := base64.RawURLEncoding.DecodeString(code)
	if err != nil {
		return nil, err
	}
	var s idpSpec
	return &s, json.Unmarshal(raw, &s)
}

func sign(r *http.Request, claims map[string]any, kid string, key *rsa.PrivateKey) string {
	t := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims(claims))
	t.Header["kid"] = kid
	s, _ := t.SignedString(key)
	return s
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// idp answers the provider's paths; false for any other.
func idp(w http.ResponseWriter, r *http.Request, raw []byte) bool {
	switch r.URL.Path {
	case "/idp/.well-known/openid-configuration":
		b := idpBase(r)
		writeJSON(w, 200, map[string]any{"issuer": b, "authorization_endpoint": b + "/auth", "token_endpoint": b + "/token",
			"jwks_uri": b + "/jwks", "userinfo_endpoint": b + "/userinfo", "id_token_signing_alg_values_supported": []string{"RS256", "HS256"}})
	case "/idp/jwks":
		pub := idpKey.PublicKey
		writeJSON(w, 200, map[string]any{"keys": []any{map[string]any{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
			"n": base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
			"e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes())}}})
	case "/idp/token":
		form, _ := url.ParseQuery(string(raw))
		spec, err := decodeSpec(form.Get("code"))
		if err != nil {
			writeJSON(w, 400, map[string]any{"error": "invalid_grant"})
			return true
		}
		if spec.Status != 0 && spec.Status != 200 {
			writeJSON(w, spec.Status, map[string]any{"error": "invalid_grant"})
			return true
		}
		if spec.Raw != "" {
			w.WriteHeader(200)
			_, _ = w.Write([]byte(spec.Raw))
			return true
		}
		now := time.Now().Unix()
		exp := now + 300
		if spec.ExpIn != nil {
			exp = now + *spec.ExpIn
		}
		claims := map[string]any{"iss": idpBase(r), "aud": form.Get("client_id"), "sub": "sub-1", "email": "new.person@example.test",
			"iat": now, "exp": exp, "nonce": spec.Nonce}
		for k, v := range spec.Claims {
			if v == nil {
				delete(claims, k)
			} else {
				claims[k] = v
			}
		}
		kid, key := "k1", idpKey
		if spec.Kid != "" {
			kid = spec.Kid
		}
		if spec.OtherKey {
			key = idpOtherKey
		}
		out := map[string]any{"token_type": "Bearer"}
		if !spec.NoIDToken {
			out["id_token"] = sign(r, claims, kid, key)
		}
		switch {
		case spec.Access != nil:
			at := map[string]any{"iss": idpBase(r), "sub": claims["sub"], "iat": now, "exp": now + 300}
			for k, v := range spec.Access {
				at[k] = v
			}
			out["access_token"] = sign(r, at, "k1", idpKey)
		case spec.Opaque != nil:
			b, _ := json.Marshal(spec.Opaque)
			out["access_token"] = "opaque." + base64.RawURLEncoding.EncodeToString(b)
		}
		writeJSON(w, 200, out)
	case "/idp/userinfo":
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		raw, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(token, "opaque."))
		var claims map[string]any
		if err != nil || json.Unmarshal(raw, &claims) != nil {
			writeJSON(w, 401, map[string]any{"error": "invalid_token"})
			return true
		}
		writeJSON(w, 200, claims)
	default:
		return false
	}
	return true
}
