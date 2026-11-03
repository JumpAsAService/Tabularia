// Package webhooks is services/webhooks.py for the gateway's routes: the events, the
// address's check at saving, the secret, the deliveries queued (a ping, a resend). The
// sending loop belongs to the orchestrators and stays in Python for now.
package webhooks

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"math/big"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
)

// Events is EVENTS, in order.
var Events = []string{
	"flow.run.succeeded",
	"flow.run.failed",
	"datasource.refresh.succeeded",
	"datasource.refresh.failed",
}

// Ping is PING.
const Ping = "ping"

// ErrNotASCII: an address only Python judges (pyurl.ErrNotASCII).
var ErrNotASCII = pyurl.ErrNotASCII

// ValidateURL is validate_url: the address's form at saving, stripped; the error's text
// is the ValueError's. pyurl.ErrNotASCII for a host only Python can judge.
func ValidateURL(url string) (string, error) {
	url = strings.TrimFunc(url, unicode.IsSpace)
	parts, err := pyurl.URLSplit(url)
	if err != nil {
		return "", err
	}
	if _, ok := parts.Hostname(); (parts.Scheme != "http" && parts.Scheme != "https") || !ok {
		return "", errors.New("The address must be http:// or https:// with a host")
	}
	if utf8.RuneCountInString(url) > 2000 {
		return "", errors.New("Address too long (at most 2000 characters)")
	}
	if err := readHost(url); err != nil {
		if errors.Is(err, pyurl.ErrNotASCII) {
			return "", err
		}
		return "", fmt.Errorf("Invalid address: %s", err.Error())
	}
	return url, nil
}

// readHost is httpx.URL(url) then _read_host: the host as httpx connects to it, checked as
// the resolver checks it, and the port.
func readHost(url string) error {
	u, err := pyurl.HTTPXURL(url)
	if err != nil {
		return err
	}
	if _, ok := pyurl.IPAddress(strings.Trim(u.Host, "[]")); !ok {
		if err := pyurl.IDNALabels(u.Host); err != nil {
			return err
		}
	}
	if u.Port != nil && !(u.Port.Sign() > 0 && u.Port.Cmp(big.NewInt(65536)) < 0) {
		return fmt.Errorf("port out of range: %s", u.Port.String())
	}
	return nil
}

// EventsOf is events_of: the JSON list of names, what is not a string dropped, a broken
// text none.
func EventsOf(text string) []string {
	if text == "" {
		text = "[]"
	}
	v, err := pyjson.Loads([]byte(text))
	out := []string{}
	if err != nil {
		return out
	}
	list, ok := v.([]any)
	if !ok {
		// a dict iterates over its keys, a string over its characters
		switch x := v.(type) {
		case pyjson.Object:
			for _, kv := range x {
				out = append(out, kv.Key)
			}
		case string:
			for _, r := range x {
				out = append(out, string(r))
			}
		}
		return out
	}
	for _, e := range list {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// NewSecret is new_secret: "whsec_" and 32 random bytes, URL-safe base64 without padding
// (secrets.token_urlsafe(32)).
func NewSecret() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return "whsec_" + base64.RawURLEncoding.EncodeToString(b)
}
