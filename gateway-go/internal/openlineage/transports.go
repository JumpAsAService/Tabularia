package openlineage

import (
	"context"
	"crypto/md5"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The library's transports (openlineage-python 1.53.0, openlineage/client/transport), built from
// a configuration as its DefaultTransportFactory builds them. The gateway's image has none of
// the optional modules (confluent_kafka, boto3, google-cloud-datacatalog-lineage, fsspec): the
// transports that need them fail there with the library's own words, and so they fail here.

// ltransport is a Transport.
type ltransport interface {
	emit(ctx context.Context, ev pyjson.Object) error
	class() string // the class's name, for str(transport)
	kind() string
	meta() *transportMeta
}

type transportMeta struct {
	name     any
	priority int64
}

func (m *transportMeta) meta() *transportMeta { return m }

// str is Transport.__str__.
func str_(t ltransport) string {
	name := "None"
	if n := t.meta().name; n != nil {
		name = pyjson.Str(n)
	}
	return fmt.Sprintf("<%s(name=%s, kind=%s, priority=%d)>", t.class(), name, t.kind(), t.meta().priority)
}

// pyErr is a Python exception as str(e) shows it.
type pyErr struct{ msg string }

func (e *pyErr) Error() string { return e.msg }

func perr(format string, a ...any) error { return &pyErr{fmt.Sprintf(format, a...)} }

// keyError is str(KeyError(msg)): the message in quotes.
func keyError(msg string) error { return &pyErr{"'" + msg + "'"} }

// the library's own classes, by the dotted path a configuration may name instead of a kind
var builtinClasses = map[string]string{
	"openlineage.client.transport.composite.CompositeTransport":            "composite",
	"openlineage.client.transport.http.HttpTransport":                      "http",
	"openlineage.client.transport.async_http.AsyncHttpTransport":           "async_http",
	"openlineage.client.transport.kafka.KafkaTransport":                    "kafka",
	"openlineage.client.transport.msk_iam.MSKIAMTransport":                 "msk-iam",
	"openlineage.client.transport.console.ConsoleTransport":                "console",
	"openlineage.client.transport.noop.NoopTransport":                      "noop",
	"openlineage.client.transport.file.FileTransport":                      "file",
	"openlineage.client.transport.transform.transform.TransformTransport":  "transform",
	"openlineage.client.transport.amazon_datazone.AmazonDataZoneTransport": "amazon_datazone_api",
	"openlineage.client.transport.datadog.DatadogTransport":                "datadog",
	"openlineage.client.transport.gcplineage.GCPLineageTransport":          "gcplineage",
	"openlineage.client.transport.CompositeTransport":                      "composite",
	"openlineage.client.transport.HttpTransport":                           "http",
	"openlineage.client.transport.AsyncHttpTransport":                      "async_http",
	"openlineage.client.transport.KafkaTransport":                          "kafka",
	"openlineage.client.transport.MSKIAMTransport":                         "msk-iam",
	"openlineage.client.transport.ConsoleTransport":                        "console",
	"openlineage.client.transport.NoopTransport":                           "noop",
	"openlineage.client.transport.FileTransport":                           "file",
	"openlineage.client.transport.TransformTransport":                      "transform",
	"openlineage.client.transport.AmazonDataZoneTransport":                 "amazon_datazone_api",
	"openlineage.client.transport.DatadogTransport":                        "datadog",
	"openlineage.client.transport.GCPLineageTransport":                     "gcplineage",
}

type factory struct {
	env environ
	log *slog.Logger
}

func (f *factory) disabled() bool {
	v, _ := f.env.get("OPENLINEAGE_DISABLED")
	return strings.ToLower(strings.TrimSpace(v)) == "true"
}

// create is DefaultTransportFactory.create.
func (f *factory) create(config any) (ltransport, error) {
	if f.disabled() {
		f.log.Info("OpenLineage is disabled. No events will be emitted.")
		return &noopTransport{}, nil
	}
	if !pyjson.Truthy(config) {
		f.log.Warn("Couldn't initialize OpenLineage transport; will print events to console.")
		return &consoleTransport{log: f.log}, nil
	}
	cfg, ok := config.(pyjson.Object)
	if !ok {
		return nil, perr("string indices must be integers, not 'str'")
	}
	typ, has := cfg.Get("type")
	if !has {
		return nil, keyError("type")
	}
	m := transportMeta{}
	rest := pyjson.Object{}
	var prio any = int64(0)
	for _, kv := range cfg {
		switch kv.Key {
		case "name":
			m.name = kv.Value
		case "priority":
			prio = kv.Value
		default:
			rest = append(rest, kv)
		}
	}
	switch p := prio.(type) {
	case int64:
		m.priority = p
	case float64:
		m.priority = int64(p)
	case bool:
		if p {
			m.priority = 1
		}
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(strings.ReplaceAll(p, "_", "")), 10, 64)
		if err != nil {
			return nil, perr("Error casting priority `%s` to int for transport `%s`", p, pyjson.Str(typ))
		}
		m.priority = n
	}
	kind, isText := typ.(string)
	if !isText {
		return nil, perr("Transport named %s: %s has to be class, and subclass of Transport", pyStrOrNone(m.name), pyjson.Str(typ))
	}
	if k, ok := builtinClasses[kind]; ok {
		kind = k
	}
	t, err := f.build(kind, rest)
	if err != nil {
		return nil, err
	}
	if pyjson.Truthy(m.name) && t.meta().name == nil {
		t.meta().name = m.name
	}
	if m.priority != 0 && t.meta().priority == 0 {
		t.meta().priority = m.priority
	}
	return t, nil
}

func pyStrOrNone(v any) string {
	if v == nil {
		return "None"
	}
	return pyjson.Str(v)
}

func (f *factory) build(kind string, cfg pyjson.Object) (ltransport, error) {
	switch kind {
	case "http":
		c, err := httpConfigFrom(cfg, "HttpConfig", f.env)
		if err != nil {
			return nil, err
		}
		if err := c.validURL(false); err != nil {
			return nil, err
		}
		return &httpTransport{cfg: c, log: f.log}, nil
	case "async_http":
		c, err := httpConfigFrom(cfg, "AsyncHttpConfig", f.env)
		if err != nil {
			return nil, err
		}
		if err := c.validURL(true); err != nil {
			return nil, err
		}
		return newAsyncHTTP(c, f.log), nil
	case "file":
		return fileTransportFrom(cfg)
	case "console":
		return &consoleTransport{log: f.log}, nil
	case "noop":
		return &noopTransport{}, nil
	case "composite":
		return f.composite(cfg)
	case "datadog":
		return f.datadog(cfg)
	case "gcplineage":
		if _, ok := cfg.Get("project_id"); !ok {
			return nil, perr("project_id is required for GCPLineageTransport. Provide it in config")
		}
		return &gcpTransport{log: f.log}, nil
	case "kafka", "msk-iam":
		if mk, ok := cfg.Get("message_key"); ok && pyjson.Truthy(mk) && !pyjson.Truthy(cfg.Value("messageKey")) {
			cfg = cfg.Set("messageKey", mk)
		}
		c, ok := cfg.Get("config")
		if !ok {
			return nil, perr("kafka `config` not passed to KafkaConfig")
		}
		if _, isDict := c.(pyjson.Object); !isDict {
			return nil, perr("`config` passed to KafkaConfig must be dict")
		}
		if _, ok := cfg.Get("topic"); !ok {
			if kind == "kafka" {
				return nil, perr("KafkaConfig.__init__() missing 1 required positional argument: 'topic'")
			}
			return nil, perr("MSKIAMConfig.__init__() missing 1 required positional argument: 'topic'")
		}
		if kind == "msk-iam" && cfg.Value("region") == nil {
			return nil, perr("No module named 'boto3'")
		}
		return nil, perr("No module named 'confluent_kafka'")
	case "amazon_datazone_api":
		if _, ok := cfg.Get("domainId"); !ok {
			return nil, perr("`domainId` key not passed to AmazonDataZoneConfig")
		}
		return nil, perr("No module named 'boto3'")
	case "transform":
		if _, ok := cfg.Get("transport"); !ok {
			return nil, perr("'transport' not passed to TransformConfig")
		}
		tc, ok := cfg.Get("transformer_class")
		if !ok {
			return nil, perr("'transformer_class' not passed to TransformConfig")
		}
		// the transformer is a Python class: none is importable from here
		return nil, perr("Failed to import %s", pyjson.Str(tc))
	}
	// a class named by its path: the library imports it, and no Python class is here
	return nil, perr("Failed to import %s", kind)
}

// ── noop, console ───────────────────────────────────────────────────────────

type noopTransport struct{ transportMeta }

func (*noopTransport) emit(context.Context, pyjson.Object) error { return nil }
func (*noopTransport) class() string                             { return "NoopTransport" }
func (*noopTransport) kind() string                              { return "noop" }

type consoleTransport struct {
	transportMeta
	log *slog.Logger
}

func (t *consoleTransport) emit(_ context.Context, ev pyjson.Object) error {
	body, err := pyjson.Dumps(ev, pyjson.Options{SortKeys: true, EnsureASCII: true})
	if err != nil {
		return err
	}
	t.log.Info(body)
	return nil
}
func (*consoleTransport) class() string { return "ConsoleTransport" }
func (*consoleTransport) kind() string  { return "console" }

// ── gcplineage: the module is not in the image ─────────────────────────────

type gcpTransport struct {
	transportMeta
	log *slog.Logger
}

func (t *gcpTransport) emit(_ context.Context, ev pyjson.Object) error {
	if _, ok := ev.Get("eventType"); !ok {
		t.log.Warn("GCP Lineage only supports RunEvent")
		return nil
	}
	t.log.Error("OpenLineage client did not find google-cloud-datacatalog-lineage module. Installing it is required for GCPLineage to work. " +
		"You can also get it via `pip install google-cloud-datacatalog-lineage`")
	return perr("No module named 'google'")
}
func (*gcpTransport) class() string { return "GCPLineageTransport" }
func (*gcpTransport) kind() string  { return "gcplineage" }

// ── composite ───────────────────────────────────────────────────────────────

type compositeTransport struct {
	transportMeta
	children             []ltransport
	onFailure, onSuccess bool
	log                  *slog.Logger
}

func (f *factory) composite(cfg pyjson.Object) (ltransport, error) {
	ts, ok := cfg.Get("transports")
	if !ok {
		return nil, perr("composite `transports` not passed to CompositeConfig")
	}
	t := &compositeTransport{onFailure: true, onSuccess: true, log: f.log}
	if v, ok := cfg.Get("continue_on_failure"); ok {
		t.onFailure = pyjson.Truthy(v)
	}
	if v, ok := cfg.Get("continue_on_success"); ok {
		t.onSuccess = pyjson.Truthy(v)
	}
	var list []any
	switch x := ts.(type) {
	case []any:
		list = x
	case pyjson.Object:
		for _, kv := range x {
			if !pyjson.Truthy(kv.Value) {
				continue
			}
			c, isDict := kv.Value.(pyjson.Object)
			if !isDict {
				return nil, perr("'%s' object is not a mapping", pyTypeName(kv.Value))
			}
			list = append(list, append(append(pyjson.Object{}, c...), pyjson.KV{Key: "name", Value: kv.Key}))
		}
	}
	for _, c := range list {
		child, err := f.create(c)
		if err != nil {
			return nil, err
		}
		t.children = append(t.children, child)
	}
	if pyjson.Truthy(cfg.Value("sort_transports")) {
		sorted := append([]ltransport{}, t.children...)
		for i := 1; i < len(sorted); i++ { // stable, by priority, highest first
			for j := i; j > 0 && sorted[j].meta().priority > sorted[j-1].meta().priority; j-- {
				sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
			}
		}
		t.children = sorted
	}
	if len(t.children) == 0 {
		return nil, perr("CompositeTransport initialization failed: No transports found")
	}
	return t, nil
}

func (t *compositeTransport) emit(ctx context.Context, ev pyjson.Object) error {
	ok := 0
	for _, c := range t.children {
		if err := c.emit(ctx, ev); err != nil {
			if !t.onFailure {
				return perr("Transport %s failed to emit event", str_(c))
			}
			t.log.Warn(fmt.Sprintf("Transport %s failed to emit event with error: %v", str_(c), err))
			continue
		}
		ok++
		if !t.onSuccess {
			return nil
		}
	}
	if ok == 0 {
		names := make([]string, len(t.children))
		for i, c := range t.children {
			names[i] = "'" + str_(c) + "'"
		}
		return perr("None of the transports successfully emitted the event: [%s]", strings.Join(names, ", "))
	}
	return nil
}
func (*compositeTransport) class() string { return "CompositeTransport" }
func (*compositeTransport) kind() string  { return "composite" }

// ── datadog ─────────────────────────────────────────────────────────────────

var datadogSites = []string{"datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu", "ddog-gov.com", "us2.ddog-gov.com",
	"ap1.datadoghq.com", "ap2.datadoghq.com", "uk1.datadoghq.com", "datad0g.com"}

func validHTTPURL(s string) bool {
	u, err := url.Parse(s)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

type datadogTransport struct {
	transportMeta
	sync  *httpTransport
	async *asyncHTTP
	acfg  *httpConfig
	rules pyjson.Object
	mu    sync.Mutex
	log   *slog.Logger
}

func (f *factory) datadog(cfg pyjson.Object) (ltransport, error) {
	apiKey := cfg.Value("apiKey")
	if !pyjson.Truthy(apiKey) {
		apiKey = cfg.Value("apikey")
	}
	if !pyjson.Truthy(apiKey) {
		if v, _ := f.env.get("DD_API_KEY"); v != "" {
			apiKey = v
		}
	}
	if !pyjson.Truthy(apiKey) {
		return nil, perr("apiKey is required for DatadogTransport. Provide it in config or set DD_API_KEY environment variable.")
	}
	site := cfg.Value("site")
	if !pyjson.Truthy(site) {
		if v, ok := f.env.get("DD_SITE"); ok {
			site = v
		} else {
			site = "datadoghq.com"
		}
	}
	siteText := pyjson.Str(site)
	intake := ""
	for _, s := range datadogSites {
		if s == siteText {
			intake = "https://data-obs-intake." + s
		}
	}
	if validHTTPURL(siteText) {
		intake = siteText
	} else if intake == "" {
		quoted := make([]string, len(datadogSites))
		for i, s := range datadogSites {
			quoted[i] = "'" + s + "'"
		}
		return nil, perr("Invalid site '%s'. Must be one of: [%s] or a valid URL", siteText, strings.Join(quoted, ", "))
	}
	retry := cfg.Value("retry")
	if retry == nil {
		retry = pyjson.Object{{Key: "total", Value: int64(5)}, {Key: "read", Value: int64(5)}, {Key: "connect", Value: int64(5)},
			{Key: "backoff_factor", Value: 0.3}, {Key: "status_forcelist", Value: []any{int64(500), int64(502), int64(503), int64(504)}},
			{Key: "allowed_methods", Value: []any{"HEAD", "POST"}}}
	}
	timeout := cfg.Value("timeout")
	if timeout == nil {
		timeout = 5.0
	}
	shared := pyjson.Object{{Key: "url", Value: intake}, {Key: "timeout", Value: timeout}, {Key: "retry", Value: retry},
		{Key: "compression", Value: "gzip"}, {Key: "auth", Value: pyjson.Object{{Key: "type", Value: "api_key"}, {Key: "apiKey", Value: apiKey}}}}
	hc, err := httpConfigFrom(shared, "HttpConfig", f.env)
	if err != nil {
		return nil, err
	}
	ac, err := httpConfigFrom(shared, "AsyncHttpConfig", f.env)
	if err != nil {
		return nil, err
	}
	rules, _ := cfg.Value("async_transport_rules").(pyjson.Object)
	if _, ok := cfg.Get("async_transport_rules"); !ok {
		rules = pyjson.Object{{Key: "dbt", Value: pyjson.Object{{Key: "*", Value: true}}}}
	}
	return &datadogTransport{sync: &httpTransport{cfg: hc, log: f.log}, acfg: ac, rules: rules, log: f.log}, nil
}

// useAsync is _should_use_async_transport.
func useAsync(rules pyjson.Object, ev pyjson.Object) bool {
	if w, ok := rules.Value("*").(pyjson.Object); ok && pyjson.Truthy(w.Value("*")) {
		return true
	}
	job, _ := ev.Value("job").(pyjson.Object)
	facets, _ := job.Value("facets").(pyjson.Object)
	jt, _ := facets.Value("jobType").(pyjson.Object)
	if jt == nil {
		return false
	}
	integration := strings.ToLower(text(jt.Value("integration")))
	jobType := strings.ToLower(text(jt.Value("jobType")))
	if integration == "" {
		return false
	}
	for _, r := range rules {
		if r.Key == "*" || strings.ToLower(r.Key) == integration {
			sub, _ := r.Value.(pyjson.Object)
			for _, jr := range sub {
				if jr.Key == "*" || strings.ToLower(jr.Key) == jobType {
					return pyjson.Truthy(jr.Value)
				}
			}
		}
	}
	return false
}

func (t *datadogTransport) emit(ctx context.Context, ev pyjson.Object) error {
	if useAsync(t.rules, ev) {
		t.mu.Lock()
		if t.async == nil {
			t.async = newAsyncHTTP(t.acfg, t.log)
		}
		a := t.async
		t.mu.Unlock()
		return a.emit(ctx, ev)
	}
	return t.sync.emit(ctx, ev)
}
func (*datadogTransport) class() string { return "DatadogTransport" }
func (*datadogTransport) kind() string  { return "datadog" }

// ── file ────────────────────────────────────────────────────────────────────

type fileTransport struct {
	transportMeta
	path          string
	append, debug bool
}

func fileTransportFrom(cfg pyjson.Object) (ltransport, error) {
	p, ok := cfg.Get("log_file_path")
	if !ok {
		return nil, perr("`log_file_path` key not passed to FileConfig")
	}
	t := &fileTransport{path: pyjson.Str(p), append: pyjson.Truthy(cfg.Value("append")), debug: pyjson.Truthy(cfg.Value("debug_mode"))}
	if pyjson.Truthy(cfg.Value("filesystem")) || (strings.Contains(t.path, "://") && !strings.HasPrefix(t.path, "file://")) {
		return nil, perr("An explicit filesystem was requested or remote protocol detected but fsspec is not available. " +
			"Please install fsspec to use remote filesystems.")
	}
	return t, nil
}

// localPath is LocalFileHandler._to_local_path.
func localPath(p string) string {
	u, err := url.Parse(p)
	if err != nil || u.Scheme != "file" {
		return p
	}
	if u.Host != "" && u.Host != "localhost" {
		return "//" + u.Host + u.Path
	}
	return u.Path
}

// filePath is _get_file_path: the file itself when appending, otherwise one file per event.
func (t *fileTransport) filePath(ev pyjson.Object) string {
	if t.append {
		return t.path
	}
	now := time.Now()
	if !t.debug {
		return fmt.Sprintf("%s-%s.%06d.json", t.path, now.Format("20060102-150405"), now.Nanosecond()/1000)
	}
	stamp := fmt.Sprintf("%s.%06d", now.Format("060102T150405"), now.Nanosecond()/1000)
	jobName := "unknown_job_name"
	if job, ok := ev.Value("job").(pyjson.Object); ok {
		jobName = text(job.Value("name"))
	} else if ds, ok := ev.Value("dataset").(pyjson.Object); ok {
		jobName = text(ds.Value("name"))
	}
	eventType := "unknown_event_type"
	if et, ok := ev.Get("eventType"); ok {
		eventType = pyjson.Str(et)
	}
	var safe strings.Builder
	for _, r := range jobName {
		if unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' || r == '.' || r == '-' {
			safe.WriteRune(r)
		} else {
			safe.WriteString("__")
		}
	}
	return fmt.Sprintf("%s-%s-%s-%s.json", t.path, stamp, safe.String(), eventType)
}

// pyOSError is str(OSError): «[Errno N] Message: 'path'», as C's strerror words it.
func pyOSError(err error, path string) string {
	var errno interface{ Errno() uintptr }
	var pe *os.PathError
	msg := err.Error()
	if errors.As(err, &pe) {
		msg = pe.Err.Error()
		err = pe.Err
	}
	if len(msg) > 0 {
		msg = strings.ToUpper(msg[:1]) + msg[1:]
	}
	if n, ok := errnoOf(err); ok {
		return fmt.Sprintf("[Errno %d] %s: '%s'", n, msg, path)
	}
	_ = errno
	return msg
}

func (t *fileTransport) emit(_ context.Context, ev pyjson.Object) error {
	path := t.filePath(ev)
	var body string
	var err error
	if t.debug {
		body, err = dumpsIndent(ev)
	} else {
		body, err = pyjson.Dumps(ev, pyjson.Options{SortKeys: true, EnsureASCII: true})
	}
	if err != nil {
		return err
	}
	flags := os.O_CREATE | os.O_WRONLY | os.O_TRUNC
	if t.append {
		flags = os.O_CREATE | os.O_WRONLY | os.O_APPEND
	}
	f, err := os.OpenFile(localPath(path), flags, 0o666)
	if err == nil {
		_, err = f.WriteString(body + "\n")
		if cerr := f.Close(); err == nil {
			err = cerr
		}
	}
	if err != nil {
		if errors.Is(err, os.ErrPermission) {
			return perr("Log file `%s` is not writeable", path)
		}
		return perr("Failed to write to log file `%s`: %s", path, pyOSError(err, localPath(path)))
	}
	return nil
}
func (*fileTransport) class() string { return "FileTransport" }
func (*fileTransport) kind() string  { return "file" }

// dumpsIndent is json.dumps(…, sort_keys=True, indent=2).
func dumpsIndent(v any) (string, error) {
	var b strings.Builder
	if err := writeIndent(&b, v, 0); err != nil {
		return "", err
	}
	return b.String(), nil
}

func writeIndent(b *strings.Builder, v any, level int) error {
	pad := func(n int) string { return "\n" + strings.Repeat("  ", n) }
	switch x := v.(type) {
	case pyjson.Object:
		if len(x) == 0 {
			b.WriteString("{}")
			return nil
		}
		keys := make([]string, len(x))
		for i, kv := range x {
			keys[i] = kv.Key
		}
		sortStrings(keys)
		b.WriteString("{")
		for i, k := range keys {
			if i > 0 {
				b.WriteString(",")
			}
			b.WriteString(pad(level + 1))
			ks, _ := pyjson.Dumps(k, pyjson.Options{EnsureASCII: true})
			b.WriteString(ks + ": ")
			if err := writeIndent(b, x.Value(k), level+1); err != nil {
				return err
			}
		}
		b.WriteString(pad(level) + "}")
		return nil
	case []any:
		if len(x) == 0 {
			b.WriteString("[]")
			return nil
		}
		b.WriteString("[")
		for i, e := range x {
			if i > 0 {
				b.WriteString(",")
			}
			b.WriteString(pad(level + 1))
			if err := writeIndent(b, e, level+1); err != nil {
				return err
			}
		}
		b.WriteString(pad(level) + "]")
		return nil
	}
	s, err := pyjson.Dumps(v, pyjson.Options{EnsureASCII: true})
	b.WriteString(s)
	return err
}

// ── async_http ──────────────────────────────────────────────────────────────

// asyncHTTP is AsyncHttpTransport: emit queues and returns; a goroutine sends, a START before
// the events that close its run, with the retries on the configured statuses and on errors.
type asyncHTTP struct {
	transportMeta
	cfg *httpConfig
	log *slog.Logger
	ch  chan asyncReq
}

type asyncReq struct {
	id, body string
	gz       []byte
}

func newAsyncHTTP(c *httpConfig, log *slog.Logger) *asyncHTTP {
	size := c.maxQueue * 2
	if size <= 0 {
		size = 20000
	}
	a := &asyncHTTP{cfg: c, log: log, ch: make(chan asyncReq, size)}
	go a.loop()
	return a
}

func (a *asyncHTTP) emit(_ context.Context, ev pyjson.Object) error {
	body, err := pyjson.Dumps(ev, pyjson.Options{SortKeys: true, EnsureASCII: true})
	if err != nil {
		return err
	}
	id := ""
	if et, ok := ev.Get("eventType"); ok {
		run, _ := ev.Value("run").(pyjson.Object)
		id = text(run.Value("runId")) + "-" + pyjson.Str(et)
	} else {
		sum := md5.Sum([]byte(body))
		id = hex.EncodeToString(sum[:])
	}
	a.ch <- asyncReq{id: id, body: body}
	return nil
}

func (a *asyncHTTP) loop() {
	for r := range a.ch {
		total := a.cfg.retry.totalOr(5)
		for attempt := 0; attempt <= total; attempt++ {
			status, err := a.cfg.postOnce(context.Background(), r.body)
			if err == nil && status >= 200 && status < 300 {
				break
			}
			if err == nil && (!a.cfg.retry.forcelist[status] || attempt == total) {
				a.log.Error(fmt.Sprintf("Failed to emit event (event_id) %s to %s, status code: %d", r.id, a.cfg.url, status))
				break
			}
			if err != nil && attempt == total {
				a.log.Error(fmt.Sprintf("Failed to emit event (event_id) %s to %s due to exception: %v", r.id, a.cfg.url, err))
				break
			}
			time.Sleep(time.Duration(a.cfg.retry.backoff * math.Pow(2, float64(attempt)) * float64(time.Second)))
		}
	}
}
func (*asyncHTTP) class() string { return "AsyncHttpTransport" }
func (*asyncHTTP) kind() string  { return "async_http" }
