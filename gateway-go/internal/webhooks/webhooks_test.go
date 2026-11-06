package webhooks

import (
	"encoding/json"
	"errors"
	"os"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyurl"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/siem"
)

// The Python gateway's own answers (testdata/urls.py, run in its image): validate_url,
// siem.endpoint and urlsplit's scheme and hostname, address by address. httpx's IDNA encoding
// of a host that is not ASCII (the AI service's, in the gateway) is the one Python recorded.
func TestTheAddressesAsPythonJudgesThem(t *testing.T) {
	raw, err := os.ReadFile("testdata/urls.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		URL        string             `json:"url"`
		Valid      *string            `json:"valid"`
		Error      *string            `json:"error"`
		Scheme     string             `json:"scheme"`
		Hostname   *string            `json:"hostname"`
		SplitError *string            `json:"split_error"`
		Endpoint   *string            `json:"endpoint"`
		IDNA       map[string]*string `json:"idna"`
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	encoded := map[string]*string{}
	for _, c := range cases {
		for h, a := range c.IDNA {
			encoded[h] = a
		}
	}
	pyurl.UseIDNA(func(host string) (string, bool, error) {
		a, met := encoded[host]
		if !met {
			t.Errorf("an IDNA encoding Python never asked: %q", host)
			return "", false, errors.New("unknown")
		}
		if a == nil {
			return "", false, nil
		}
		return *a, true, nil
	})
	defer pyurl.UseIDNA(nil)
	for _, c := range cases {
		got, err := ValidateURL(c.URL)
		switch {
		case c.Valid != nil && (err != nil || got != *c.Valid):
			t.Errorf("%q: want %q, got %q %v", c.URL, *c.Valid, got, err)
		case c.Error != nil && (err == nil || err.Error() != *c.Error):
			t.Errorf("%q: want the error %q, got %q %v", c.URL, *c.Error, got, err)
		}
		parts, err := pyurl.URLSplit(c.URL)
		switch {
		case c.SplitError != nil:
			if err == nil || err.Error() != *c.SplitError {
				t.Errorf("%q: urlsplit should fail with %q: %v", c.URL, *c.SplitError, err)
			}
		case err != nil:
			t.Errorf("%q: urlsplit %v", c.URL, err)
		default:
			h, ok := parts.Hostname()
			if parts.Scheme != c.Scheme || ok != (c.Hostname != nil) || (ok && h != *c.Hostname) {
				t.Errorf("%q: urlsplit %q %q %v", c.URL, parts.Scheme, h, ok)
			}
		}
		if c.Endpoint != nil {
			if e, err := siem.Endpoint(c.URL); err != nil || e != *c.Endpoint {
				t.Errorf("%q: endpoint %q %v", c.URL, e, err)
			}
		}
	}
}

func TestEventsOf(t *testing.T) {
	for text, want := range map[string]int{"": 0, "[]": 0, `["a", 1, "b"]`: 2, "nope": 0, `{"x": 1}`: 1} {
		if got := EventsOf(text); len(got) != want {
			t.Errorf("%q: %v", text, got)
		}
	}
}
