package api

import "testing"

func TestTheLanguageVersionsSortAsNumbers(t *testing.T) {
	if less, ok := scVersionLess("3.9", "3.12"); !ok || !less {
		t.Error("3.9 comes before 3.12")
	}
	if _, ok := scVersionLess("3.x", "3.1"); ok {
		t.Error("a word against a number cannot be ordered (a TypeError in Python)")
	}
}

func TestURLSplit(t *testing.T) {
	for in, want := range map[string][2]string{
		"https://S3.Example.com:443/x": {"https", "s3.example.com"},
		"http://:9000/":                {"http", ""},
		"minio":                        {"", ""},
		"http://u:p@[::1]:9/":          {"http", "::1"},
	} {
		if s, h := scURLSplit(in); s != want[0] || h != want[1] {
			t.Errorf("%s: %s %s", in, s, h)
		}
	}
}
