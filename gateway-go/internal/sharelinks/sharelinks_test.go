package sharelinks

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestATokenIs256RandomBitsKeptOnlyAsItsHash(t *testing.T) {
	a, err := NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	b, _ := NewSecret()
	if len(a) != 43 || a == b || !WellFormed(a) {
		t.Fatalf("tokens %q %q", a, b)
	}
	if h := Hash(a); len(h) != 64 || h == a || strings.Contains(h, a) {
		t.Errorf("hash %q", h)
	}
	for _, bad := range []string{"", "short", strings.Repeat("a", 42), strings.Repeat("a", 44), strings.Repeat("a", 42) + "/",
		strings.Repeat("a", 42) + "=", "../" + strings.Repeat("a", 40)} {
		if WellFormed(bad) {
			t.Errorf("%q is not a token", bad)
		}
	}
}

func TestTheOriginsOfAnEmbeddableLink(t *testing.T) {
	got, err := Origins([]string{"https://Intranet.Example.com", "https://intranet.example.com:443/", "http://portal:8080",
		"https://[::1]:8443"})
	want := []string{"https://intranet.example.com", "http://portal:8080", "https://[::1]:8443"}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Errorf("%v %v", got, err)
	}
	for _, bad := range [][]string{nil, {""}, {"intranet.example.com"}, {"https://*.example.com"}, {"https://a.example.com/page"},
		{"ftp://a.example.com"}, {"https://user@a.example.com"}, {"https://a.example.com?x=1"}, {"'self'"}, {"https://a.com https://b.com"},
		{"https://a.example.com;script-src *"}, {"javascript:alert(1)"},
		{"https://a1.com", "https://a2.com", "https://a3.com", "https://a4.com", "https://a5.com", "https://a6.com", "https://a7.com",
			"https://a8.com", "https://a9.com", "https://a10.com", "https://a11.com"}} {
		if _, err := Origins(bad); err == nil {
			t.Errorf("%v accepted", bad)
		}
	}
}

func TestFrameAncestorsAreNoneUnlessEmbeddable(t *testing.T) {
	if got := FrameAncestors(nil); got != "'none'" {
		t.Error(got)
	}
	if got := FrameAncestors(&Link{Origins: []string{"https://a.com"}}); got != "'none'" {
		t.Error("a link with a password is never framed:", got)
	}
	if got := FrameAncestors(&Link{Embeddable: true, Origins: []string{"https://a.com", "https://b.com"}}); got != "https://a.com https://b.com" {
		t.Error(got)
	}
}

func TestAnExportHandleOpensOnlyItsOwnLinksExport(t *testing.T) {
	secret := []byte("a test secret")
	task := "0b6e7c1a-2f4e-4c3e-9d5e-1a2b3c4d5e6f"
	h := ExportHandle(secret, 7, task)
	if got := ExportTask(secret, 7, h); got != task {
		t.Errorf("own link: %q", got)
	}
	for _, c := range []struct {
		link   int64
		handle string
	}{{8, h}, {7, task}, {7, task + ".x"}, {7, "." + h}, {7, ""}, {7, "other" + h[len(task):]}} {
		if got := ExportTask(secret, c.link, c.handle); got != "" {
			t.Errorf("link %d handle %q: %q", c.link, c.handle, got)
		}
	}
	if got := ExportTask([]byte("another secret"), 7, h); got != "" {
		t.Error("another installation's handle")
	}
}

func TestTheStateOfALink(t *testing.T) {
	now := time.Date(2026, 11, 7, 10, 0, 0, 0, time.UTC)
	later, earlier := now.Add(time.Hour), now.Add(-time.Hour)
	cases := []struct {
		l    Link
		want string
		live bool
	}{
		{Link{ExpiresAt: later, PasswordTemporary: true}, "waiting", true},
		{Link{ExpiresAt: later}, "active", true},
		{Link{ExpiresAt: later, Embeddable: true, PasswordTemporary: true}, "active", true},
		{Link{ExpiresAt: earlier}, "expired", false},
		{Link{ExpiresAt: now}, "expired", false},
		{Link{ExpiresAt: later, RevokedAt: &earlier}, "revoked", false},
		{Link{ExpiresAt: later, Failures: LockAfter}, "locked", false},
		{Link{ExpiresAt: later, Failures: LockAfter - 1}, "active", true},
	}
	for i, c := range cases {
		if got := State(&c.l, now); got != c.want {
			t.Errorf("%d: %s, want %s", i, got, c.want)
		}
		if got := Live(&c.l, now); got != c.live {
			t.Errorf("%d: live %v", i, got)
		}
	}
}
