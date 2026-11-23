package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func tarball(t *testing.T, name, description string) []byte {
	t.Helper()
	var b bytes.Buffer
	gz := gzip.NewWriter(&b)
	tw := tar.NewWriter(gz)
	for path, body := range map[string]string{name + "/DESCRIPTION": description, name + "/R/" + name + ".R": "f <- function() 1\n"} {
		if err := tw.WriteHeader(&tar.Header{Name: path, Mode: 0o644, Size: int64(len(body))}); err != nil {
			t.Fatal(err)
		}
		_, _ = io.WriteString(tw, body)
	}
	_ = tw.Close()
	_ = gz.Close()
	return b.Bytes()
}

func sum(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// a CRAN and a PyPI of a few files: what `pin` downloads
func fakeIndexes(t *testing.T) (*httptest.Server, map[string][]byte) {
	files := map[string][]byte{
		// a current package and an archived one that needs it (on a continuation line), and one that needs an R package of R's own
		"/src/contrib/aaa_1.0-1.tar.gz":                                              tarball(t, "aaa", "Package: aaa\nVersion: 1.0-1\nImports: methods, stats\n"),
		"/src/contrib/Archive/bbb/bbb_2.0.tar.gz":                                    tarball(t, "bbb", "Package: bbb\nVersion: 2.0\nDepends: R (>= 4.1)\nImports: utils,\n    aaa (>= 1.0)\nLinkingTo: aaa\n"),
		"/files/xx-1.0-cp314-cp314-manylinux_2_28_x86_64.whl":                        []byte("wheel x86_64"),
		"/files/xx-1.0-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl": []byte("wheel aarch64"),
		"/files/xx-1.0-cp313-cp313-manylinux_2_28_x86_64.whl":                        []byte("another Python"),
		"/files/xx-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl":                       []byte("free-threaded"),
		"/files/xx-1.0-cp314-cp314-musllinux_1_2_x86_64.whl":                         []byte("musl"),
		"/files/xx-1.0-cp314-cp314-win_amd64.whl":                                    []byte("windows"),
		"/files/xx-1.0.tar.gz":                                                       []byte("sources"),
		"/files/yy-3.0-py3-none-any.whl":                                             []byte("pure"),
		"/files/zz-0.8-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":    []byte("abi3 x86_64"),
		"/files/zz-0.8-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl":  []byte("abi3 aarch64"),
	}
	release := func(names ...string) []byte {
		var rel pyRelease
		for _, n := range names {
			kind := "bdist_wheel"
			if strings.HasSuffix(n, ".tar.gz") {
				kind = "sdist"
			}
			rel.URLs = append(rel.URLs, struct {
				Filename    string            `json:"filename"`
				URL         string            `json:"url"`
				PackageType string            `json:"packagetype"`
				Digests     map[string]string `json:"digests"`
			}{n, "", kind, map[string]string{"sha256": sum(files["/files/"+n])}})
		}
		b, _ := json.Marshal(rel)
		return b
	}
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pypi/xx/1.0/json":
			var rel pyRelease
			_ = json.Unmarshal(release("xx-1.0-cp314-cp314-manylinux_2_28_x86_64.whl", "xx-1.0-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",
				"xx-1.0-cp313-cp313-manylinux_2_28_x86_64.whl", "xx-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl", "xx-1.0-cp314-cp314-musllinux_1_2_x86_64.whl",
				"xx-1.0-cp314-cp314-win_amd64.whl", "xx-1.0.tar.gz"), &rel)
			for i := range rel.URLs {
				rel.URLs[i].URL = srv.URL + "/files/" + rel.URLs[i].Filename
			}
			_ = json.NewEncoder(w).Encode(rel)
		case "/pypi/yy/3.0/json", "/pypi/zz/0.8/json", "/pypi/bad/1.0/json":
			var rel pyRelease
			switch r.URL.Path {
			case "/pypi/yy/3.0/json":
				_ = json.Unmarshal(release("yy-3.0-py3-none-any.whl"), &rel)
			case "/pypi/zz/0.8/json":
				_ = json.Unmarshal(release("zz-0.8-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
					"zz-0.8-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"), &rel)
			default: // the index says another digest than the file's
				_ = json.Unmarshal(release("yy-3.0-py3-none-any.whl"), &rel)
				rel.URLs[0].Digests["sha256"] = strings.Repeat("0", 64)
			}
			for i := range rel.URLs {
				rel.URLs[i].URL = srv.URL + "/files/" + rel.URLs[i].Filename
			}
			_ = json.NewEncoder(w).Encode(rel)
		default:
			b, ok := files[r.URL.Path]
			if !ok {
				http.NotFound(w, r)
				return
			}
			_, _ = w.Write(b)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, files
}

// `pin` writes the sha256 of the files it downloaded, the same bytes every time, and leaves the rest of the recipe as it was.
func TestPinIsDeterministic(t *testing.T) {
	srv, files := fakeIndexes(t)
	p := &pinner{client: srv.Client(), cran: srv.URL, pypi: srv.URL, log: io.Discard}
	r := []byte(`{
  "language": "r",
  "base": "4.6",
  "pinned_on": "2026-11-23",
  "packages": [
    "aaa@1.0-1",
    "bbb@2.0"
  ],
  "resources": {
    "cpu": "1",
    "memory": "2Gi"
  },
  "max_rows": 10,
  "max_seconds": 60
}
`)
	once, err := p.pin("g", r)
	if err != nil {
		t.Fatal(err)
	}
	want := strings.Replace(string(r), "  ],\n  \"resources\"", "  ],\n  \"hashes\": {\n"+
		"    \"aaa@1.0-1\": [\""+sum(files["/src/contrib/aaa_1.0-1.tar.gz"])+"\"],\n"+
		"    \"bbb@2.0\": [\""+sum(files["/src/contrib/Archive/bbb/bbb_2.0.tar.gz"])+"\"]\n  },\n  \"resources\"", 1)
	if string(once) != want {
		t.Errorf("the recipe pinned:\n%s\nwanted:\n%s", once, want)
	}
	for i := 0; i < 2; i++ { // again from the same recipe, and from the one written: the same bytes
		from := r
		if i == 1 {
			from = once
		}
		again, err := p.pin("g", from)
		if err != nil || !bytes.Equal(again, once) {
			t.Errorf("pinned again (%d): %v\n%s", i, err, again)
		}
	}

	py := []byte(`{
  "language": "python",
  "base": "3.14",
  "pinned_on": "2026-11-23",
  "packages": [
    "xx==1.0",
    "yy==3.0",
    "zz==0.8"
  ],
  "hashes": {
    "xx==1.0": ["` + strings.Repeat("0", 64) + `"]
  },
  "max_rows": 10,
  "max_seconds": 60
}
`)
	got, err := p.pin("g", py)
	if err != nil {
		t.Fatal(err)
	}
	var back struct {
		Hashes map[string][]string `json:"hashes"`
	}
	if err := json.Unmarshal(got, &back); err != nil {
		t.Fatal(err)
	}
	x := []string{sum(files["/files/xx-1.0-cp314-cp314-manylinux_2_28_x86_64.whl"]), sum(files["/files/xx-1.0-cp314-cp314-manylinux2014_aarch64.manylinux_2_17_aarch64.whl"])}
	if x[0] > x[1] {
		x[0], x[1] = x[1], x[0]
	}
	z := []string{sum(files["/files/zz-0.8-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"]),
		sum(files["/files/zz-0.8-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"])}
	if z[0] > z[1] {
		z[0], z[1] = z[1], z[0]
	}
	if !reflect.DeepEqual(back.Hashes, map[string][]string{"xx==1.0": x, "yy==3.0": {sum(files["/files/yy-3.0-py3-none-any.whl"])}, "zz==0.8": z}) {
		t.Errorf("the wheels hashed: %v", back.Hashes)
	}
	if again, _ := p.pin("g", got); !bytes.Equal(again, got) {
		t.Error("a Python recipe pinned twice differs")
	}
}

// What `pin` refuses: a package before one it needs, a package that is in no index, a file that is not the index's digest, a
// version without a wheel for x86_64.
func TestPinRefuses(t *testing.T) {
	srv, _ := fakeIndexes(t)
	p := &pinner{client: srv.Client(), cran: srv.URL, pypi: srv.URL, log: io.Discard}
	for name, c := range map[string]struct{ recipe, says string }{
		"out of order":     {`{"language": "r", "base": "4.6", "packages": ["bbb@2.0", "aaa@1.0-1"]}`, "bbb@2.0 needs aaa (Imports), which is not before it in the recipe"},
		"not in the index": {`{"language": "r", "base": "4.6", "packages": ["ccc@1.0"]}`, "ccc@1.0: "},
		"another digest":   {`{"language": "python", "base": "3.14", "packages": ["bad==1.0"]}`, "PyPI says 0000"},
		"no wheel":         {`{"language": "python", "base": "3.12", "packages": ["zz==0.8", "xx==1.0"]}`, "xx==1.0 has no wheel for cp312 on Linux x86_64"},
		"an unknown key":   {`{"language": "r", "base": "4.6", "packages": [], "extra": 1}`, "unknown field"},
	} {
		_, err := p.pin("g", []byte(c.recipe))
		if err == nil || !strings.Contains(err.Error(), c.says) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestTheWheelsALinuxBuildMayPick(t *testing.T) {
	for file, want := range map[string][]string{
		"a-1-cp314-cp314-manylinux_2_28_x86_64.whl":                         {"x86_64"},
		"a-1-cp314-cp314-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl": {"aarch64"},
		"a-1-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl":     {"x86_64"},
		"a-1-py3-none-manylinux_2_28_aarch64.whl":                           {"aarch64"},
		"a-1-py2.py3-none-any.whl":                                          {"x86_64", "aarch64"},
		"a-1-1build-py3-none-any.whl":                                       {"x86_64", "aarch64"},
		"a-1-cp313-cp313-manylinux_2_28_x86_64.whl":                         nil,
		"a-1-cp314-cp314t-manylinux_2_28_x86_64.whl":                        nil,
		"a-1-cp315-abi3-manylinux_2_28_x86_64.whl":                          nil,
		"a-1-cp314-cp314-musllinux_1_2_x86_64.whl":                          nil,
		"a-1-cp314-cp314-macosx_11_0_arm64.whl":                             nil,
		"a-1-cp314-cp314-win_amd64.whl":                                     nil,
		"a-1.tar.gz":                                                        nil,
		"a-1-py2-none-any.whl":                                              nil,
	} {
		if got := wheelMachines(file, "cp314"); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: %v, wanted %v", file, got, want)
		}
	}
}

// `pin` learns the sources (docs/specs/bayes-image.md, «The recipe»): each tarball downloaded and hashed here, refused unless its
// sha256 is the digest the GitHub release publishes for that asset; the same files give the same recipe, byte for byte.
func TestPinHashesTheSourcesAgainstTheReleasesDigest(t *testing.T) {
	tarballs := map[string][]byte{"/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz": []byte("the x86-64 tarball"),
		"/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0-linux-arm64.tar.gz": []byte("the arm64 tarball")}
	digest := func(path string) string { return "sha256:" + sum(tarballs[path]) }
	wrong := false
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/repos/stan-dev/cmdstan/releases/tags/v2.40.0":
			arm := digest("/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0-linux-arm64.tar.gz")
			if wrong {
				arm = "sha256:" + strings.Repeat("0", 64)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"assets": []map[string]string{
				{"name": "cmdstan-2.40.0.tar.gz", "digest": digest("/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz")},
				{"name": "cmdstan-2.40.0-linux-arm64.tar.gz", "digest": arm},
				{"name": "cmdstan-2.40.0-no-digest.tar.gz"}}})
		default:
			if _, _ = w.Write(tarballs[r.URL.Path]); tarballs[r.URL.Path] == nil {
				http.NotFound(w, r)
			}
		}
	}))
	t.Cleanup(srv.Close)
	p := &pinner{client: srv.Client(), pypi: srv.URL, github: srv.URL + "/api", log: io.Discard}
	recipe := func(arm string) []byte {
		return []byte(`{
  "language": "python",
  "base": "3.14",
  "packages": [],
  "hashes": {},
  "sources": [
    {
      "name": "cmdstan",
      "version": "2.40.0",
      "machine": "x86_64",
      "url": "` + srv.URL + `/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz",
      "sha256": ""
    },
    {
      "name": "cmdstan",
      "version": "2.40.0",
      "machine": "aarch64",
      "url": "` + srv.URL + `/stan-dev/cmdstan/releases/download/v2.40.0/` + arm + `",
      "sha256": "` + strings.Repeat("a", 64) + `"
    }
  ],
  "max_rows": 10,
  "max_seconds": 60
}
`)
	}
	r := recipe("cmdstan-2.40.0-linux-arm64.tar.gz")
	once, err := p.pin("bayes", r)
	if err != nil {
		t.Fatal(err)
	}
	want := strings.Replace(strings.Replace(string(r), `"sha256": ""`, `"sha256": "`+sum([]byte("the x86-64 tarball"))+`"`, 1),
		strings.Repeat("a", 64), sum([]byte("the arm64 tarball")), 1)
	if string(once) != want {
		t.Errorf("the recipe pinned:\n%s\nwanted:\n%s", once, want)
	}
	if again, err := p.pin("bayes", once); err != nil || !bytes.Equal(again, once) {
		t.Errorf("pinned again: %v\n%s", err, again)
	}
	// a digest that is not the file's, an asset without a digest, an address that is no release's asset: refused
	wrong = true
	if _, err := p.pin("bayes", r); err == nil || !strings.Contains(err.Error(), "the release says 0000") {
		t.Errorf("another digest: %v", err)
	}
	wrong = false
	if _, err := p.pin("bayes", recipe("cmdstan-2.40.0-no-digest.tar.gz")); err == nil || !strings.Contains(err.Error(), "publishes no sha256 digest") {
		t.Errorf("no digest: %v", err)
	}
	other := bytes.Replace(r, []byte("/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz"), []byte("/archive/cmdstan-2.40.0.tar.gz"), 1)
	if _, err := p.pin("bayes", other); err == nil || !strings.Contains(err.Error(), "is not the address of a GitHub release's asset") {
		t.Errorf("not an asset: %v", err)
	}
}
