package api

import "testing"

func TestValidExtra(t *testing.T) {
	s := func(v string) *string { return &v }
	for _, c := range []struct {
		extra  *string
		dbType string
		raw    string
		detail string
	}{
		{nil, "postgresql", "{}", ""}, {s("  "), "x", "{}", ""}, {s(" {\"a\": 1} "), "x", `{"a": 1}`, ""},
		{s("nope"), "x", "", "The connection options are not valid JSON"}, {s("[1]"), "x", "", "The connection options must be a JSON object"},
		{s(`{"tls": "x", "allowed_domains": 5}`), "smtp", "", "SMTP options: tls must be starttls, ssl or none; allowed_domains must be a list of domains or a comma-separated text"},
		{s(`{"auth": "x"}`), "sftp", "", "SFTP: auth must be password or key"},
		{s(`{"auth": "api_key", "key_name": "a b"}`), "api", "", "API: invalid name for the API key header 'a b'"},
		{s(`{"auth": "oauth2", "token_url": "https://t/x#f"}`), "api", "",
			"API: the token URL cannot hold a query (?…) or a fragment (#…): put the parameters in the datasource and the key in the authentication"},
		{s(`{"headers": {"": "x"}}`), "api", `{"headers": {"": "x"}}`, ""},
	} {
		raw, ref := validExtra(c.extra, c.dbType)
		detail := ""
		if ref != nil {
			detail = ref.Detail
		}
		if raw != c.raw || detail != c.detail {
			t.Errorf("%v %s: %q %q", c.extra, c.dbType, raw, detail)
		}
	}
}

func TestURLErrorAndOrigin(t *testing.T) {
	for url, want := range map[string]string{
		"https://a.test/x": "", " http://a.test ": "", "ftp://a": "invalid base URL 'ftp://a': expected http(s)://host[:port][/path]",
		"https://u@a": "the base URL cannot hold a user or a password: use the authentication", "https://": "invalid base URL 'https://': expected http(s)://host[:port][/path]",
	} {
		if got, err := urlError(url, "base URL"); got != want || err != nil {
			t.Errorf("%q: %q %v", url, got, err)
		}
	}
	if _, err := urlError("https://[::1", "base URL"); err == nil {
		t.Error("an invalid IPv6 address is urlsplit's ValueError")
	}
	for url, port := range map[string]int64{"https://a": 443, "http://a": 80, "http://a:8080/x": 8080, "http://a:x": 80, "https://a:99999": 443, "https://[::1]:81": 81} {
		if _, _, p, err := apiOrigin(url); p != port || err != nil {
			t.Errorf("%q: %d %v", url, p, err)
		}
	}
}

func TestGsheetsIdentity(t *testing.T) {
	key := `{"type": "service_account", "client_email": " a@b ", "project_id": 5, "private_key": "  -----BEGIN PRIVATE KEY-----x"}`
	if e, p, ref := gsheetsIdentity(key); e != "a@b" || p != "5" || ref != nil {
		t.Errorf("%q %q %v", e, p, ref)
	}
	for _, bad := range []string{"", "x", `{"web": 1}`, `{"type": "user"}`, `{"type": "service_account", "client_email": "a"}`} {
		if _, _, ref := gsheetsIdentity(bad); ref == nil || ref.Status != 422 {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestCheckSFTP(t *testing.T) {
	if r := checkSFTP("h", int64(22), "u", "password", true, false); r != nil {
		t.Error(r)
	}
	for _, c := range []struct {
		host string
		port any
		user string
	}{{" ", nil, "u"}, {"a/b", nil, "u"}, {"h", int64(0), "u"}, {"h", int64(65536), "u"}, {"h", nil, " "}} {
		if r := checkSFTP(c.host, c.port, c.user, "password", true, true); r == nil {
			t.Errorf("%v accepted", c)
		}
	}
}
