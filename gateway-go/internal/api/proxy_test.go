package api

import "testing"

func TestQuery(t *testing.T) {
	got := encodeQuery(parseQSL("b=2&a=x+y&c=%C3%A8&d&&e=%zz&f=a%2Fb"))
	if want := "b=2&a=x+y&c=%C3%A8&d=&e=%25zz&f=a%2Fb"; got != want {
		t.Fatalf("%s, want %s", got, want)
	}
}

func TestASCIIFallback(t *testing.T) {
	for in, want := range map[string]string{
		"attachment; filename*=UTF-8''vendite%20citt%C3%A0.xlsx":    `attachment; filename="vendite citta.xlsx"; filename*=UTF-8''vendite%20citt%C3%A0.xlsx`,
		`attachment; filename="a.csv"; filename*=UTF-8''%C3%A8.csv`: `attachment; filename="a.csv"; filename*=UTF-8''%C3%A8.csv`,
		"inline; filename*=utf-8''%E2%98%83":                        `inline; filename="report"; filename*=utf-8''%E2%98%83`,
		"attachment":                                                "attachment",
	} {
		if got := asciiFallback(in); got != want {
			t.Errorf("%s\n got  %s\n want %s", in, got, want)
		}
	}
}

func TestDescribeBytes(t *testing.T) {
	if describeBytes(1<<30) != "1 GB" || describeBytes(3<<20) != "3 MB" || describeBytes(1000) != "1000 bytes" {
		t.Error("describe_bytes")
	}
}
