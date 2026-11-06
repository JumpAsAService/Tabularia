package openlineage

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The values below are PyYAML's and the library's, read in the gateway's image.
func TestYAMLIsPyYAMLs(t *testing.T) {
	doc := "a: yes\nb: No\nc: 0777\nd: 0x1F\ne: 1_000\nf: 1:30\ng: 1e5\nh: 1.5e+3\ni: ~\nj: \"yes\"\nk: !!str 12\nl: .inf\n" +
		"m: [1, two, 3.0]\nn: &x {p: 1}\no: *x\np:\n  <<: *x\n  q: 2\n"
	v, err := yamlSafeLoad(doc)
	if err != nil {
		t.Fatal(err)
	}
	o := v.(pyjson.Object)
	want := map[string]any{"a": true, "b": false, "c": int64(511), "d": int64(31), "e": int64(1000), "f": int64(90), "g": "1e5",
		"h": 1500.0, "i": nil, "j": "yes", "k": "12"}
	for k, w := range want {
		if got := o.Value(k); got != w {
			t.Errorf("%s: %#v, want %#v", k, got, w)
		}
	}
	if !math.IsInf(o.Value("l").(float64), 1) {
		t.Error("l")
	}
	if s, _ := pyjson.Dumps(o.Value("p"), pyjson.Options{SortKeys: true}); s != `{"p": 1, "q": 2}` {
		t.Error(s)
	}
	if _, err := yamlSafeLoad("a: 1\n---\nb: 2\n"); err == nil {
		t.Error("two documents")
	}
}

func TestTrimmersAreTheLibrarys(t *testing.T) {
	r := newReducer(&libConfig{}, quiet())
	for name, want := range map[string]string{"/data/t/day=2024-01-01": "/data/t", "/tmp/20250722T0901Z": "/tmp", "/tmp/2025-07-22": "/tmp",
		"/20250721": "/20250721", "/a/2025/7": "/a", "/a/b/2025/07/31": "/a/b", "/x/202513": "/x/202513", "/x/2024-02-30": "/x/2024-02-30",
		"/a/b=c=d": "/a/b=c=d", "/p/01.02.2025": "/p"} {
		if got, _ := r.trimName(name); got != want {
			t.Errorf("%s: %s, want %s", name, got, want)
		}
	}
}

func TestEnvironmentConfig(t *testing.T) {
	e := environ{vals: map[string]string{}}
	for _, kv := range [][2]string{{"OPENLINEAGE__TRANSPORT__URL", "http://x"}, {"OPENLINEAGE__TRANSPORT", `{"type": "http"}`},
		{"OPENLINEAGE__TAGS__JOB__TEAM", "data"}, {"OPENLINEAGE__TAGS__RUN__TIER", "2"}, {"OPENLINEAGE_URL", "http://u"}, {"OPENLINEAGE_API_KEY", "k"}} {
		e.set(kv[0], kv[1])
	}
	aliasEnvVars(&e)
	cfg, err := envConfig(e, quiet())
	if err != nil {
		t.Fatal(err)
	}
	s, _ := pyjson.Dumps(cfg, pyjson.Options{SortKeys: true})
	// OPENLINEAGE__TRANSPORT, a shorter name, is written last: the whole transport is its value
	if s != `{"tags": {"job": {"team": "data"}, "run": {"tier": 2}}, "transport": {"type": "http"}}` {
		t.Fatal(s)
	}
	e2 := environ{vals: map[string]string{}}
	e2.set("OPENLINEAGE_URL", "http://u")
	e2.set("OPENLINEAGE_API_KEY", "k")
	aliasEnvVars(&e2)
	cfg, _ = envConfig(e2, quiet())
	if s, _ := pyjson.Dumps(cfg, pyjson.Options{SortKeys: true}); s !=
		`{"transport": {"transports": {"default_http": {"auth": {"apiKey": "k", "type": "api_key"}, "type": "http", "url": "http://u"}}}}` {
		t.Fatal(s)
	}
}

func TestTheFileWinsOverTheVariables(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "openlineage.yml")
	os.WriteFile(path, []byte("transport:\n  type: console\ntags:\n  job: {a: file}\n"), 0o644)
	e := environ{vals: map[string]string{}}
	e.set("OPENLINEAGE_CONFIG", path)
	e.set("OPENLINEAGE__TRANSPORT__TYPE", "http")
	e.set("OPENLINEAGE__TAGS__JOB__B", "env")
	c, err := loadLibConfig(e, quiet())
	if err != nil {
		t.Fatal(err)
	}
	if c.transport.(pyjson.Object).Value("type") != "console" || len(c.jobTags) != 2 {
		t.Fatal(c.transport, c.jobTags)
	}
	e.set("OPENLINEAGE__FACETS__NOPE", "1")
	if _, err := loadLibConfig(e, quiet()); err == nil || err.Error() != "Failed to parse OpenLineage config." {
		t.Fatal(err)
	}
}

func TestTransportsFailAsInTheImage(t *testing.T) {
	f := &factory{env: environ{vals: map[string]string{}}, log: quiet()}
	for _, c := range []struct {
		cfg  string
		want string
	}{
		{`{"type": "kafka", "config": {}, "topic": "t"}`, "No module named 'confluent_kafka'"},
		{`{"type": "kafka", "config": {}}`, "KafkaConfig.__init__() missing 1 required positional argument: 'topic'"},
		{`{"type": "msk-iam", "config": {}, "topic": "t"}`, "No module named 'boto3'"},
		{`{"type": "amazon_datazone_api", "domainId": "d"}`, "No module named 'boto3'"},
		{`{"type": "transform", "transport": {"type": "console"}, "transformer_class": "a.B"}`, "Failed to import a.B"},
		{`{"type": "foo"}`, "Failed to import foo"},
		{`{"name": "x"}`, "'type'"},
		{`{"type": "http"}`, "`url` key not passed to HttpConfig"},
		{`{"type": "http", "url": "h"}`, "Need valid url for OpenLineageClient, passed h"},
		{`{"type": "http", "url": "http://h", "compression": "zip"}`, "'zip' is not a valid HttpCompression"},
		{`{"type": "http", "url": "http://h", "auth": {"type": "api_key"}}`, "'apiKey is required for HTTP Transport when auth type is `api_key`.'"},
		{`{"type": "http", "url": "http://h", "priority": "x"}`, "Error casting priority `x` to int for transport `http`"},
		{`{"type": "composite", "transports": []}`, "CompositeTransport initialization failed: No transports found"},
		{`{"type": "file", "log_file_path": "s3://b/x"}`, "An explicit filesystem was requested or remote protocol detected but fsspec is not available. Please install fsspec to use remote filesystems."},
		{`{"type": "gcplineage"}`, "project_id is required for GCPLineageTransport. Provide it in config"},
		{`{"type": "datadog", "apiKey": "k", "site": "nope"}`, "Invalid site 'nope'. Must be one of: ['datadoghq.com', 'us3.datadoghq.com', " +
			"'us5.datadoghq.com', 'datadoghq.eu', 'ddog-gov.com', 'us2.ddog-gov.com', 'ap1.datadoghq.com', 'ap2.datadoghq.com', 'uk1.datadoghq.com', " +
			"'datad0g.com'] or a valid URL"},
	} {
		v, _ := pyjson.Loads(c.cfg)
		if _, err := f.create(v); err == nil || err.Error() != c.want {
			t.Errorf("%s: %v", c.cfg, err)
		}
	}
	v, _ := pyjson.Loads(`{"type": "composite", "transports": {"one": {"type": "http", "url": "http://h", "priority": 2.7}, "two": {"type": "console"}},
		"sort_transports": true}`)
	tr, err := f.create(v)
	if err != nil {
		t.Fatal(err)
	}
	if got := str_(tr.(*compositeTransport).children[0]); got != "<HttpTransport(name=one, kind=http, priority=2)>" {
		t.Fatal(got)
	}
}

func TestAFileForEachEvent(t *testing.T) {
	dir := t.TempDir()
	ft, _ := fileTransportFrom(pyjson.Object{{Key: "log_file_path", Value: filepath.Join(dir, "ev")}, {Key: "debug_mode", Value: true}})
	ev := pyjson.Object{{Key: "eventType", Value: "START"}, {Key: "job", Value: pyjson.Object{{Key: "name", Value: "a/b c"}}}}
	if err := ft.emit(nil, ev); err != nil {
		t.Fatal(err)
	}
	files, _ := os.ReadDir(dir)
	if len(files) != 1 || !strings.HasSuffix(files[0].Name(), "-a__b__c-START.json") {
		t.Fatal(files)
	}
	raw, _ := os.ReadFile(filepath.Join(dir, files[0].Name()))
	if string(raw) != "{\n  \"eventType\": \"START\",\n  \"job\": {\n    \"name\": \"a/b c\"\n  }\n}\n" {
		t.Fatal(string(raw))
	}
	err := (&fileTransport{path: "/nonexistent/x", append: true}).emit(nil, ev)
	if err == nil || err.Error() != "Failed to write to log file `/nonexistent/x`: [Errno 2] No such file or directory: '/nonexistent/x'" {
		t.Fatal(err)
	}
}
