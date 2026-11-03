package flowaccess

import (
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func TestWidens(t *testing.T) {
	before := `{"nodes": [{"type": "source", "data": {"datasourceId": 3, "key": "datasets/a.parquet"}}]}`
	if n := len(Widens(before, before)); n != 0 {
		t.Fatalf("the same definition widens by %d", n)
	}
	after := `{"nodes": [{"type": "source", "data": {"datasourceId": "3", "key": "datasets/a.parquet"}},
		{"type": "output", "data": {"connectionId": 7, "flowId": true}}]}`
	got := Widens(before, after)
	if len(got) != 1 || got[0] != (Ref{"connection", "7"}) {
		t.Fatalf("widens: %v", got)
	}
	script := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "x"}]}}]}`
	changed := `{"nodes": [{"id": "s", "type": "script", "data": {"files": [{"path": "a.py", "content": "y"}]}}]}`
	if len(Widens(script, changed)) != 1 {
		t.Fatal("new code does not widen")
	}
}

func TestDigits(t *testing.T) {
	for _, c := range []struct {
		v  any
		ok bool
	}{{int64(4), true}, {"12", true}, {"-1", false}, {int64(-1), false}, {true, false}, {"1.0", false}, {"", false}, {1.0, false}} {
		if _, ok := Digits(c.v); ok != c.ok {
			t.Errorf("Digits(%#v) = %v", c.v, ok)
		}
	}
}

func TestCheckFiles(t *testing.T) {
	node := func(files string, entry string) pyjson.Object {
		v, _ := pyjson.Loads([]byte(`{"id": "n", "data": {"label": "L", "entrypoint": "` + entry + `", "files": ` + files + `}}`))
		return v.(pyjson.Object)
	}
	cases := []struct {
		files, entry, lang, want string
	}{
		{`[{"path": "main.py", "content": "print(1)"}]`, "main.py", "python", ""},
		{`[{"path": "main.py", "content": 1}]`, "main.py", "", "Script «L»: the file «main.py» has no text"},
		{`[{"path": "../x.py", "content": ""}]`, "", "", "Script «L»: «../x.py» is not a file name: letters, digits, . _ - and / between folders"},
		{`[{"path": "a.py", "content": ""}, {"path": "A.py", "content": ""}]`, "", "", "Script «L»: two files are called «A.py»"},
		{`[{"path": "a.py", "content": "read s3://Bucket/x"}]`, "", "", "Script «L»: a.py, line 1 names the scripts bucket: read the input from TABULARIA_INPUT and write the output to TABULARIA_OUTPUT"},
		{`[{"path": "main.R", "content": ""}]`, "main.R", "python", "Script «L»: the file to run is a Python file (.py)"},
		{`[{"path": "main.py", "content": ""}]`, "other.py", "", "Script «L»: the file to run («other.py») is not among its files"},
	}
	for _, c := range cases {
		got := ""
		if r := checkFiles(node(c.files, c.entry), c.lang, "bucket", false); r != nil {
			got = r.Detail
		}
		if got != c.want {
			t.Errorf("%s: %q, want %q", c.files, got, c.want)
		}
	}
}

func TestSplitLines(t *testing.T) {
	got := pySplitLines("a\r\nb\rc d\n")
	if len(got) != 4 || got[3] != "d" {
		t.Fatalf("%q", got)
	}
}
