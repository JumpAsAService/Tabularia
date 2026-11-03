package clientip

import (
	"net/http/httptest"
	"testing"
)

// uvicorn 0.50's ProxyHeadersMiddleware, case by case
func TestAsUvicornReadsXForwardedFor(t *testing.T) {
	cases := []struct {
		trusted []string
		remote  string
		xff     []string
		want    string
	}{
		{[]string{"127.0.0.1"}, "10.0.0.9:5000", []string{"1.2.3.4"}, "10.0.0.9"},                               // an untrusted hop: its header ignored
		{[]string{"127.0.0.1"}, "127.0.0.1:5000", nil, "127.0.0.1"},                                             // no header
		{[]string{"127.0.0.1"}, "127.0.0.1:5000", []string{"1.2.3.4"}, "1.2.3.4"},                               // a trusted proxy
		{[]string{"*"}, "10.0.0.9:5000", []string{"6.6.6.6, 1.2.3.4"}, "6.6.6.6"},                               // «*»: the leftmost
		{[]string{"127.0.0.1", "10.0.0.0/8"}, "127.0.0.1:1", []string{"6.6.6.6, 1.2.3.4, 10.1.2.3"}, "1.2.3.4"}, // from the right
		{[]string{"127.0.0.1", "10.0.0.0/8"}, "127.0.0.1:1", []string{"10.1.1.1, 10.2.2.2"}, "10.1.1.1"},        // all trusted: the leftmost
		{[]string{"127.0.0.1"}, "127.0.0.1:1", []string{"1.2.3.4:5678"}, "1.2.3.4"},                             // a port is dropped
		{[]string{"127.0.0.1"}, "127.0.0.1:1", []string{"[2001:db8::1]:80"}, "2001:db8::1"},
		{[]string{"127.0.0.1"}, "127.0.0.1:1", []string{"1.2.3.4:x"}, "1.2.3.4:x"},        // not a port: as written
		{[]string{"127.0.0.1"}, "127.0.0.1:1", []string{""}, "127.0.0.1"},                 // empty: the connection's
		{[]string{"127.0.0.1"}, "127.0.0.1:1", []string{"1.1.1.1", "2.2.2.2"}, "2.2.2.2"}, // two headers joined
		{[]string{"::1"}, "[::1]:1", []string{"9.9.9.9"}, "9.9.9.9"},
		{[]string{"0:0:0:0:0:0:0:1"}, "[::1]:1", []string{"9.9.9.9"}, "9.9.9.9"}, // IPv6 compared as an address
	}
	for _, c := range cases {
		r := httptest.NewRequest("GET", "/", nil)
		r.RemoteAddr = c.remote
		for _, v := range c.xff {
			r.Header.Add("X-Forwarded-For", v)
		}
		if got := New(c.trusted).Of(r); got != c.want {
			t.Errorf("trusted %v from %s xff %q: %q, want %q", c.trusted, c.remote, c.xff, got, c.want)
		}
	}
}
