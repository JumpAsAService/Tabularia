package siem

import (
	"net/netip"
	"testing"
)

// webhooks._forbidden_ip, as ipaddress's categories of CPython 3.12 say.
func TestForbidden(t *testing.T) {
	for ip, want := range map[string]bool{
		"127.0.0.1": true, "10.0.0.1": false, "172.18.0.2": false, "169.254.169.254": true, "224.0.0.1": true,
		"240.0.0.1": true, "255.255.255.255": true, "0.0.0.0": true, "100.100.100.200": true, "168.63.129.16": true,
		"8.8.8.8": false, "::1": true, "::": true, "fe80::1": true, "ff02::1": true, "::ffff:127.0.0.1": true,
		"2002:7f00:1::": true, "2002:808:808::": false, "2001:db8::1": false, "fd00:ec2::254": true, "fc00::1": false,
		"100::1": true, "fe80::1%eth0": true,
	} {
		if got := Forbidden(netip.MustParseAddr(ip)); got != want {
			t.Errorf("%s: %v", ip, got)
		}
	}
}

// send's reading of Splunk's answer.
func TestAnswer(t *testing.T) {
	for _, c := range []struct {
		status int
		body   string
		ok     bool
		msg    string
	}{
		{200, `{"text":"Success","code":0}`, true, "Success"},
		{200, `{}`, true, "Success"},
		{200, ``, true, "Success"},
		{200, `[1]`, true, "Success"},
		{200, `{"text":"Partial","code":6}`, false, "response 200: Partial (code 6)"},
		{403, `{"text":"Invalid token","code":4}`, false, "response 403: Invalid token (code 4)"},
		{400, `{"text":7}`, false, "response 400: 7 (code None)"},
		{503, `busy`, false, "response 503: busy"},
		{200, `{"code":false}`, true, "Success"},
	} {
		r := answer(c.status, []byte(c.body))
		if r.OK != c.ok || r.Message != c.msg || r.Code == nil || *r.Code != c.status {
			t.Errorf("%d %s: %+v", c.status, c.body, r)
		}
	}
}
