// Package pypkg is services/script_packages.py: the search for packages while an
// administrator defines a scripting environment, in the indexes the builds install from (PyPI
// or its mirror, CRAN or its mirror), with PEP 440 versions (packaging.version) and the wheels'
// tags (packaging.utils.parse_wheel_filename).
package pypkg

import (
	"context"
	"errors"
	"fmt"
	"html"
	"io"
	"mime"
	"net"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/httpxerr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

const (
	// PyPI is PYPI.
	PyPI           = "https://pypi.org/simple/"
	refresh        = 24 * time.Hour
	maxIndexBytes  = 256 * 1024 * 1024
	maxResults     = 20
	readTimeout    = 60 * time.Second
	connectTimeout = 10 * time.Second
)

// IndexError is PackageIndexError: the index could not be read.
type IndexError struct{ Message string }

func (e *IndexError) Error() string { return e.Message }

// Settings are the indexes: SCRIPTS__PYPI_INDEX_URL, SCRIPTS__CRAN_URL.
type Settings struct{ PyPIIndexURL, CRANURL string }

// PythonIndex is python_index.
func (s Settings) PythonIndex() string {
	u := strings.TrimSpace(s.PyPIIndexURL)
	if u == "" {
		u = PyPI
	}
	if !strings.HasSuffix(u, "/") {
		u += "/"
	}
	return u
}

// CRAN is cran.
func (s Settings) CRAN() string { return strings.TrimRight(strings.TrimSpace(s.CRANURL), "/") }

type page struct {
	status      int
	contentType string
	body        []byte
}

// text is httpx's Response.text: the charset of Content-Type, UTF-8 otherwise, errors replaced.
func (p *page) text() string {
	_, params, _ := mime.ParseMediaType(p.contentType)
	switch strings.ToLower(params["charset"]) {
	case "iso-8859-1", "latin-1", "latin1":
		r := make([]rune, len(p.body))
		for i, b := range p.body {
			r[i] = rune(b)
		}
		return string(r)
	}
	return strings.ToValidUTF8(string(p.body), "�")
}

func client() *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DialContext = (&dialer{}).dial
	t.TLSHandshakeTimeout = readTimeout
	t.ResponseHeaderTimeout = readTimeout
	return &http.Client{Transport: t, CheckRedirect: func(_ *http.Request, via []*http.Request) error {
		if len(via) > 20 {
			return errors.New("Exceeded maximum allowed redirects")
		}
		return nil
	}}
}

type dialer struct{}

// dial is httpx.Timeout(60.0, connect=10.0): ten seconds to connect, sixty for each read and
// write.
func (dialer) dial(ctx context.Context, network, address string) (net.Conn, error) {
	c, err := (&net.Dialer{Timeout: connectTimeout}).DialContext(ctx, network, address)
	if err != nil {
		return nil, err
	}
	return &deadlineConn{c, readTimeout}, nil
}

type deadlineConn struct {
	net.Conn
	timeout time.Duration
}

func (c *deadlineConn) Read(b []byte) (int, error) {
	_ = c.Conn.SetReadDeadline(time.Now().Add(c.timeout))
	return c.Conn.Read(b)
}

func (c *deadlineConn) Write(b []byte) (int, error) {
	_ = c.Conn.SetWriteDeadline(time.Now().Add(c.timeout))
	return c.Conn.Write(b)
}

// get is _get: the page at `url`; a 404 is returned, any other failure is an IndexError.
func get(ctx context.Context, url, accept string) (*page, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err == nil {
		req.Header.Set("User-Agent", "Tabularia (scripting environments)")
		req.Header.Set("Accept", accept)
	}
	var res *http.Response
	if err == nil {
		res, err = client().Do(req)
	}
	if err != nil {
		name, _ := httpxerr.Of(err)
		return nil, &IndexError{fmt.Sprintf("%s is not reachable (%s)", url, name)}
	}
	defer res.Body.Close()
	body, err := io.ReadAll(io.LimitReader(res.Body, maxIndexBytes+1))
	if err != nil {
		name, _ := httpxerr.Of(err)
		return nil, &IndexError{fmt.Sprintf("%s is not reachable (%s)", url, name)}
	}
	if res.StatusCode >= 400 && res.StatusCode != 404 {
		return nil, &IndexError{fmt.Sprintf("%s answered %d", url, res.StatusCode)}
	}
	if len(body) > maxIndexBytes {
		return nil, &IndexError{fmt.Sprintf("%s is larger than %d MB", url, maxIndexBytes>>20)}
	}
	return &page{status: res.StatusCode, contentType: res.Header.Get("Content-Type"), body: body}, nil
}

var hrefName = regexp.MustCompile(`(?i)<a\s[^>]*href="[^"]*?/([^/"]+)/?"[^>]*>`)

// CanonicalizeName is packaging.utils.canonicalize_name.
func CanonicalizeName(name string) string {
	v := strings.ReplaceAll(strings.ReplaceAll(pyLower(name), "_", "-"), ".", "-")
	for strings.Contains(v, "--") {
		v = strings.ReplaceAll(v, "--", "-")
	}
	return v
}

// pyLower is str.lower (Go's ToLower, which maps the same letters but for a few specials).
func pyLower(s string) string { return strings.ToLower(s) }

// parsePythonIndex is parse_python_index: the index's names, normalized, sorted.
func parsePythonIndex(text string) string {
	set := map[string]bool{}
	for _, m := range hrefName.FindAllStringSubmatch(text, -1) {
		set[CanonicalizeName(html.UnescapeString(m[1]))] = true
	}
	names := make([]string, 0, len(set))
	for n := range set {
		names = append(names, n)
	}
	sort.Strings(names)
	return strings.Join(names, "\n")
}

// splitLines is str.splitlines.
func splitLines(text string) []string {
	var out []string
	start := 0
	runes := []rune(text)
	for i := 0; i < len(runes); i++ {
		switch runes[i] {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ':
			out = append(out, string(runes[start:i]))
			start = i + 1
		case '\r':
			out = append(out, string(runes[start:i]))
			if i+1 < len(runes) && runes[i+1] == '\n' {
				i++
			}
			start = i + 1
		}
	}
	if start < len(runes) {
		out = append(out, string(runes[start:]))
	}
	return out
}

// parseCRANPackages is parse_cran_packages: «name\tversion» lines, sorted without case.
func parseCRANPackages(text string) string {
	var rows []string
	name, has := "", false
	for _, line := range splitLines(text) {
		if strings.HasPrefix(line, "Package:") {
			_, v, _ := strings.Cut(line, ":")
			name, has = strings.TrimFunc(v, unicode.IsSpace), true
		} else if strings.HasPrefix(line, "Version:") && has && name != "" {
			_, v, _ := strings.Cut(line, ":")
			rows = append(rows, name+"\t"+strings.TrimFunc(v, unicode.IsSpace))
			name, has = "", false
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return pyLower(rows[i]) < pyLower(rows[j]) })
	return strings.Join(rows, "\n")
}

type index struct {
	mu        sync.Mutex
	fetchedAt time.Time
	blob      string
}

var (
	indexesMu sync.Mutex
	indexes   = map[[2]string]*index{}
)

// Index is _index: the language's list of names, read once a day per process.
func Index(ctx context.Context, s Settings, language string) (string, error) {
	url := s.CRAN() + "/src/contrib/PACKAGES"
	accept := "text/plain"
	if language == "python" {
		url, accept = s.PythonIndex(), "text/html"
	}
	indexesMu.Lock()
	entry := indexes[[2]string{language, url}]
	if entry == nil {
		entry = &index{}
		indexes[[2]string{language, url}] = entry
	}
	indexesMu.Unlock()
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if entry.blob != "" && time.Since(entry.fetchedAt) < refresh {
		return entry.blob, nil
	}
	p, err := get(ctx, url, accept)
	if err == nil && p.status == 404 {
		err = &IndexError{url + " answered 404"}
	}
	if err != nil {
		var ie *IndexError
		if errors.As(err, &ie) && entry.blob != "" { // yesterday's list is better than none
			entry.fetchedAt = time.Now().Add(-refresh + 600*time.Second)
			return entry.blob, nil
		}
		return "", err
	}
	var blob string
	if language == "python" {
		blob = parsePythonIndex(p.text())
	} else {
		blob = parseCRANPackages(p.text())
	}
	entry.blob, entry.fetchedAt = blob, time.Now()
	return blob, nil
}

// Hit is one result of Search.
type Hit struct {
	Name    string
	Version *string
}

// Search is search: the names that hold `query`, the exact one first, then those it starts,
// then the others; shorter first.
func Search(ctx context.Context, s Settings, language, query string) ([]Hit, error) {
	q := strings.TrimFunc(query, unicode.IsSpace)
	if language == "python" {
		q = CanonicalizeName(q)
	}
	if utf8.RuneCountInString(q) < 2 {
		return []Hit{}, nil
	}
	blob, err := Index(ctx, s, language)
	if err != nil {
		return nil, err
	}
	var hits []string
	if language == "r" {
		re := regexp.MustCompile(`(?im)^[^\n]*` + regexp.QuoteMeta(q) + `[^\n]*$`)
		hits = re.FindAllString(blob, -1)
	} else {
		for _, line := range strings.Split(blob, "\n") {
			if strings.Contains(line, q) {
				hits = append(hits, line)
			}
		}
	}
	key := pyLower(q)
	type ranked struct {
		line, low string
		exact     bool
		starts    bool
		n         int
	}
	rs := make([]ranked, len(hits))
	for i, line := range hits {
		name, _, _ := strings.Cut(line, "\t")
		low := pyLower(name)
		rs[i] = ranked{line, low, low == key, strings.HasPrefix(low, key), utf8.RuneCountInString(name)}
	}
	sort.SliceStable(rs, func(i, j int) bool {
		a, b := rs[i], rs[j]
		if a.exact != b.exact {
			return a.exact
		}
		if a.starts != b.starts {
			return a.starts
		}
		if a.n != b.n {
			return a.n < b.n
		}
		return a.low < b.low
	})
	out := []Hit{}
	for i, r := range rs {
		if i >= maxResults {
			break
		}
		name, version, _ := strings.Cut(r.line, "\t")
		h := Hit{Name: name}
		if version != "" {
			v := version
			h.Version = &v
		}
		out = append(out, h)
	}
	return out, nil
}

// VersionOut is one of python_versions' and r_versions' rows.
type VersionOut struct {
	Version string
	Pre     bool
	Wheel   *bool
}

var pythonTag = regexp.MustCompile(`^([0-9]+)\.([0-9]+)$`)

type file struct {
	filename any
	yanked   bool
}

var fileRe = regexp.MustCompile(`(?i)<a\s([^>]*)>([^<]+)</a>`)

// pythonFiles is _python_files: the files of a project's page, JSON (PEP 691) or HTML.
func pythonFiles(p *page) ([]file, error) {
	if strings.Contains(p.contentType, "json") {
		v, err := pyjson.Loads(p.text())
		if err != nil {
			return nil, err
		}
		o, ok := v.(pyjson.Object)
		if !ok {
			return nil, errors.New("pypkg: the page is not a dict")
		}
		fv, _ := o.Get("files")
		var out []file
		if !pyjson.Truthy(fv) {
			return out, nil
		}
		list, ok := fv.([]any)
		if !ok {
			return nil, errors.New("pypkg: files is not a list")
		}
		for _, f := range list {
			fo, ok := f.(pyjson.Object)
			if !ok {
				return nil, errors.New("pypkg: a file is not a dict")
			}
			name, has := fo.Get("filename")
			if !has {
				return nil, errors.New("pypkg: a file without its filename")
			}
			y, _ := fo.Get("yanked")
			out = append(out, file{name, pyjson.Truthy(y)})
		}
		return out, nil
	}
	var out []file
	for _, m := range fileRe.FindAllStringSubmatch(p.text(), -1) {
		out = append(out, file{html.UnescapeString(strings.TrimFunc(m[2], unicode.IsSpace)), strings.Contains(m[1], "data-yanked")})
	}
	return out, nil
}

var sdistRe = regexp.MustCompile(`^(.+?)-([0-9][^-]*)$`)

// fileVersion is _file_version.
func fileVersion(name, filename string) string {
	if strings.HasSuffix(filename, ".whl") {
		w, err := ParseWheelFilename(filename)
		if err != nil {
			return ""
		}
		return w.Version.String()
	}
	for _, ext := range []string{".tar.gz", ".zip", ".tar.bz2", ".tgz"} {
		if strings.HasSuffix(filename, ext) {
			stem := filename[:len(filename)-len(ext)]
			if m := sdistRe.FindStringSubmatch(stem); m != nil && CanonicalizeName(m[1]) == name {
				return m[2]
			}
		}
	}
	return ""
}

// PythonVersions is python_versions: the project's versions, newest first; with a Python
// version, whether each has a wheel for it.
func PythonVersions(ctx context.Context, s Settings, name, pythonVersion string) ([]VersionOut, error) {
	name = CanonicalizeName(name)
	p, err := get(ctx, s.PythonIndex()+name+"/", "application/vnd.pypi.simple.v1+json, text/html;q=0.1")
	if err != nil {
		return nil, err
	}
	if p.status == 404 {
		return nil, nil
	}
	var python *[2]int
	if m := pythonTag.FindStringSubmatch(pythonVersion); m != nil {
		var a, b int
		fmt.Sscan(m[1], &a)
		fmt.Sscan(m[2], &b)
		python = &[2]int{a, b}
	}
	files, err := pythonFiles(p)
	if err != nil {
		return nil, err
	}
	type row struct {
		v      *Version
		out    VersionOut
		yanked bool
	}
	var order []string
	found := map[string]*row{}
	for _, f := range files {
		fn, ok := f.filename.(string)
		if !ok {
			return nil, errors.New("pypkg: a filename is not a text")
		}
		vs := fileVersion(name, fn)
		if vs == "" {
			continue
		}
		parsed, err := Parse(vs)
		if err != nil {
			continue
		}
		key := parsed.String()
		r := found[key]
		if r == nil {
			r = &row{v: parsed, out: VersionOut{Version: key, Pre: parsed.IsPrerelease()}, yanked: true}
			if python != nil {
				no := false
				r.out.Wheel = &no
			}
			found[key] = r
			order = append(order, key)
		}
		r.yanked = r.yanked && f.yanked
		if python != nil && !f.yanked && strings.HasSuffix(fn, ".whl") {
			fits, err := WheelFits(fn, *python)
			if err != nil {
				return nil, err
			}
			if fits {
				yes := true
				r.out.Wheel = &yes
			}
		}
	}
	var rows []*row
	for _, k := range order {
		if !found[k].yanked {
			rows = append(rows, found[k])
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return Compare(rows[i].v, rows[j].v) > 0 })
	out := []VersionOut{}
	for _, r := range rows {
		out = append(out, r.out)
	}
	return out, nil
}

var rKeySplit = regexp.MustCompile(`[.-]`)

func rKey(v string) []num {
	var out []num
	for _, p := range rKeySplit.Split(v, -1) {
		if isUnicodeDigits(p) {
			out = append(out, newNum(p))
		}
	}
	return out
}

func isUnicodeDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if !unicode.IsDigit(c) {
			return false
		}
	}
	return true
}

func cmpKeys(a, b []num) int {
	for i := 0; i < len(a) && i < len(b); i++ {
		if c := a[i].cmp(b[i]); c != 0 {
			return c
		}
	}
	return cmpInt(len(a), len(b))
}

// RVersions is r_versions: the current version from PACKAGES, the older ones from the archive.
func RVersions(ctx context.Context, s Settings, name string) ([]VersionOut, error) {
	blob, err := Index(ctx, s, "r")
	if err != nil {
		return nil, err
	}
	m := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(name) + `\t([^\n]+)$`).FindStringSubmatch(blob)
	if m == nil {
		return nil, nil
	}
	set := map[string]bool{m[1]: true}
	p, err := get(ctx, s.CRAN()+"/src/contrib/Archive/"+name+"/", "text/html")
	if err != nil {
		return nil, err
	}
	if p.status != 404 {
		re := regexp.MustCompile(`href="` + regexp.QuoteMeta(name) + `_([0-9][0-9.\-]*)\.tar\.gz"`)
		for _, x := range re.FindAllStringSubmatch(p.text(), -1) {
			set[x[1]] = true
		}
	}
	versions := make([]string, 0, len(set))
	for v := range set {
		versions = append(versions, v)
	}
	// a set's order before the sort: ties (1.0 and 1-0) are rare; text order stands in
	sort.Strings(versions)
	sort.SliceStable(versions, func(i, j int) bool { return cmpKeys(rKey(versions[i]), rKey(versions[j])) > 0 })
	out := []VersionOut{}
	for _, v := range versions {
		out = append(out, VersionOut{Version: v})
	}
	return out, nil
}
