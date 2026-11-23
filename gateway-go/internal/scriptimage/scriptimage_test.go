package scriptimage

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

func TestAnRImage(t *testing.T) {
	d, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"zoo@1.9-1", "sandwich"}, Settings{CRANURL: " https://cran.example/ ", Jobs: 3})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM rocker/r-ver:4.6.0\n", `CRAN="https://cran.example/"`, "libcurl4-openssl-dev",
		"COPY <<'TABULARIA_PACKAGES' /opt/tabularia/packages.txt\narrow\nzoo@1.9-1\nsandwich\nTABULARIA_PACKAGES\n",
		"remotes::install_version", "RUN TABULARIA_JOBS=3 Rscript /opt/tabularia/install.R\n", "USER 10001\n"} {
		if !strings.Contains(d, want) {
			t.Errorf("no %q in\n%s", want, d)
		}
	}
	if _, err := Dockerfile("r", "x", nil, Settings{CRANURL: "ftp://cran"}); err == nil || err.Error() != "SCRIPTS__CRAN_URL is not an http(s) address" {
		t.Errorf("a CRAN that is not http: %v", err)
	}
}

func TestAPythonImage(t *testing.T) {
	d, err := Dockerfile("python", "python:3.12-slim", []string{"numpy==2.0"}, Settings{PyPIIndexURL: "https://pypi.example/simple", Jobs: 2})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM python:3.12-slim\n", `PIP_INDEX_URL="https://pypi.example/simple"`,
		"polars\npyarrow\nmarimo\nnumpy==2.0\n", "RUN MAKEFLAGS=-j2 pip install"} {
		if !strings.Contains(d, want) {
			t.Errorf("no %q in\n%s", want, d)
		}
	}
	if _, err := Dockerfile("python", "x", nil, Settings{PyPIIndexURL: "nope"}); err == nil {
		t.Error("an index that is not http")
	}
	if d, _ := Dockerfile("python", "x", nil, Settings{}); strings.Contains(d, "PIP_INDEX_URL") {
		t.Error("an index without one")
	}
}

// The images of the scripting environments are byte for byte what they were before managed images came (the R one by its
// sha256: its install script is long); hashes given to them change nothing.
func TestTheImagesThatWereStayAsTheyWere(t *testing.T) {
	for _, hashes := range []map[string][]string{nil, {"zoo@1.9-1": {sumA}}} {
		r, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"zoo@1.9-1", "sandwich"}, Settings{CRANURL: "https://cran.example/", Jobs: 3, Hashes: hashes})
		if err != nil {
			t.Fatal(err)
		}
		if sum := sha256.Sum256([]byte(r)); hex.EncodeToString(sum[:]) != "1cd14b912c40fe40243c4195db10d8f438b2158f9b7e0336f25cfbe96df2c80e" {
			t.Errorf("the R image changed (hashes %v):\n%s", hashes, r)
		}
	}
	p, err := Dockerfile("python", "python:3.12-slim", []string{"numpy==2.0"}, Settings{PyPIIndexURL: "https://pypi.example/simple", Jobs: 2})
	want := "FROM python:3.12-slim\nENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 " +
		"PIP_INDEX_URL=\"https://pypi.example/simple\"\nCOPY <<'TABULARIA_PACKAGES' /opt/tabularia/requirements.txt\npolars\npyarrow\nmarimo\n" +
		"numpy==2.0\nTABULARIA_PACKAGES\nRUN MAKEFLAGS=-j2 pip install -r /opt/tabularia/requirements.txt\n" +
		"RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tabularia\nUSER 10001\nWORKDIR /home/tabularia\n"
	if err != nil || p != want {
		t.Errorf("a scripting environment's Python image changed: %v\n%s", err, p)
	}
}

// An R image installs the system libraries its recipe names after the fixed ones, and only then: with none
// the Dockerfile is the one every R image had (the sha256 above).
func TestARecipeSystemLibrariesComeAfterTheFixedOnes(t *testing.T) {
	s := Settings{CRANURL: "https://cran.example/", Jobs: 3, Managed: true, Hashes: map[string][]string{"zoo@1.9-1": {sumA}, "numpy==2.0": {sumB}}}
	plain, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"zoo@1.9-1"}, s)
	if err != nil {
		t.Fatal(err)
	}
	s.SystemLibraries = []string{"libgsl-dev"}
	with, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"zoo@1.9-1"}, s)
	if err != nil {
		t.Fatal(err)
	}
	fixed := strings.Join(RSystemLibraries, " ")
	if !strings.Contains(with, "install -y --no-install-recommends "+fixed+" libgsl-dev && rm -rf /var/lib/apt/lists/*\n") {
		t.Errorf("libgsl-dev does not follow the fixed libraries:\n%s", with)
	}
	// after the apt install the build records the versions installed, for the card (and for nothing
	// else: without libraries the Dockerfile has no such step)
	record := "RUN mkdir -p /opt/tabularia && dpkg-query -W -f '${Package}@${Version}\\n' libgsl-dev > " + SystemLibrariesFile + "\n"
	if !strings.Contains(with, "rm -rf /var/lib/apt/lists/*\n"+record+"RUN printf") {
		t.Errorf("the versions installed are not recorded right after the install:\n%s", with)
	}
	if strings.Replace(strings.Replace(with, record, "", 1), " libgsl-dev", "", 1) != plain || strings.Contains(plain, "libgsl") ||
		strings.Contains(plain, "dpkg-query") {
		t.Error("the library and its record are the only difference")
	}
	for _, bad := range []string{"libgsl-dev; rm -rf /", "-y", "Libgsl", "a", "libgsl dev", ""} {
		s.SystemLibraries = []string{bad}
		if _, err := Dockerfile("r", "rocker/r-ver:4.6.0", nil, s); err == nil {
			t.Errorf("%q is not a Debian package's name", bad)
		}
	}
	// a Python image installs no Debian package: its Dockerfile is the same
	s.SystemLibraries = []string{"libgsl-dev"}
	a, _ := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.0"}, s)
	s.SystemLibraries = nil
	b, _ := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.0"}, s)
	if a != b {
		t.Error("a Python image takes no system library")
	}
}

const (
	sumA = "1111111111111111111111111111111111111111111111111111111111111111"
	sumB = "2222222222222222222222222222222222222222222222222222222222222222"
	sumC = "3333333333333333333333333333333333333333333333333333333333333333"
)

// pyInstall is the step of a managed Python image that installs its requirements: downloaded and checked, installed from the
// files and checked again, their sha256 recorded, then pip check.
const pyInstall = "RUN pip download --no-deps --only-binary=:all: --require-hashes -d /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
	" && pip install --no-deps --only-binary=:all: --require-hashes --no-index --find-links /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
	" && (cd /tmp/tabularia-wheels && sha256sum *.whl) >> /opt/tabularia/package-sha256.txt && rm -rf /tmp/tabularia-wheels && pip check\n"

// A managed Python image: the recipe's packages only, each with the sha256 of its wheels, installed with --require-hashes,
// without their dependencies, then pip check.
func TestAManagedPythonImage(t *testing.T) {
	d, err := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5", "pyarrow==26.0.1"},
		Settings{Jobs: 2, Managed: true, Hashes: map[string][]string{"numpy==2.5.5": {sumA, sumB}, "pyarrow==26.0.1": {sumC}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM python:3.14-slim\n", "/opt/tabularia/requirements.txt\n" +
		"numpy==2.5.5 --hash=sha256:" + sumA + " --hash=sha256:" + sumB + "\npyarrow==26.0.1 --hash=sha256:" + sumC + "\nTABULARIA_PACKAGES\n",
		pyInstall, "USER 10001\n"} {
		if !strings.Contains(d, want) {
			t.Errorf("no %q in\n%s", want, d)
		}
	}
	for _, not := range []string{"polars", "marimo", "MAKEFLAGS"} {
		if strings.Contains(d, not) {
			t.Errorf("%q in a managed image:\n%s", not, d)
		}
	}
}

// A managed Python image with wheels and files (docs/specs/foundation-forecast.md, «Changes to the machinery»): the wheels of both
// machines in one Dockerfile, each guarded by BuildKit's TARGETARCH and checked by its sha256, before the requirements; the files
// downloaded and checked after them; and, with neither, the very Dockerfile every managed Python image had.
func TestAManagedPythonImageWithWheelsAndFiles(t *testing.T) {
	const sum = "35103e180214793207f95d7e12e100ecba3ace6f5fb37e1fcefbb07e096ed180"
	const sum2 = "f56cd23a3bf003bfbed3da0645ba09b3ccc66c0a2f2ac726a9b98f34e0e4926f"
	hashes := map[string][]string{"numpy==2.5.5": {sumA}}
	plain, err := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5"}, Settings{Jobs: 2, Managed: true, Hashes: hashes})
	if err != nil {
		t.Fatal(err)
	}
	s := Settings{Jobs: 2, Managed: true, Hashes: hashes,
		Wheels: []Wheel{{"x86_64", "https://torch.example/whl/torch-2.14.1%2Bcpu-cp314-x86_64.whl", sum},
			{"aarch64", "https://torch.example/whl/torch-2.14.1%2Bcpu-cp314-aarch64.whl", sum2}},
		Files: []File{{"https://hub.example/org/m/resolve/abc/model.safetensors", sum, "/opt/tabularia/weights/28m/model.safetensors"},
			{"https://hub.example/org/m/resolve/abc/LICENSE", sum2, "/opt/tabularia/weights/28m/LICENSE"}}}
	d, err := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5"}, s)
	if err != nil {
		t.Fatal(err)
	}
	want := "FROM python:3.14-slim\n" + strings.SplitN(plain, "\n", 3)[1] + "\n" +
		"COPY <<'TABULARIA_PACKAGES' /opt/tabularia/requirements.txt\nnumpy==2.5.5 --hash=sha256:" + sumA + "\nTABULARIA_PACKAGES\n" +
		"ARG TARGETARCH\n" +
		"RUN if [ \"$TARGETARCH\" = \"amd64\" ]; then pip install --no-deps --only-binary=:all: \"https://torch.example/whl/torch-2.14.1%2Bcpu-cp314-x86_64.whl#sha256=" + sum + "\"" +
		" && echo \"" + sum + "  torch-2.14.1+cpu-cp314-x86_64.whl\" >> /opt/tabularia/package-sha256.txt; fi\n" +
		"RUN if [ \"$TARGETARCH\" = \"arm64\" ]; then pip install --no-deps --only-binary=:all: \"https://torch.example/whl/torch-2.14.1%2Bcpu-cp314-aarch64.whl#sha256=" + sum2 + "\"" +
		" && echo \"" + sum2 + "  torch-2.14.1+cpu-cp314-aarch64.whl\" >> /opt/tabularia/package-sha256.txt; fi\n" +
		pyInstall +
		"RUN mkdir -p /opt/tabularia/weights/28m\n" +
		"ADD --checksum=sha256:" + sum + " --chmod=0644 https://hub.example/org/m/resolve/abc/model.safetensors /opt/tabularia/weights/28m/model.safetensors\n" +
		"ADD --checksum=sha256:" + sum2 + " --chmod=0644 https://hub.example/org/m/resolve/abc/LICENSE /opt/tabularia/weights/28m/LICENSE\n" +
		"RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tabularia\nUSER 10001\nWORKDIR /home/tabularia\n"
	if d != want {
		t.Errorf("the image with wheels and files:\n%s\nwanted:\n%s", d, want)
	}
	// only the wheels, only the files: the steps of each, the other's absent
	onlyFiles := s
	onlyFiles.Wheels = nil
	if d, _ := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5"}, onlyFiles); strings.Contains(d, "TARGETARCH") || !strings.Contains(d, "ADD --checksum") {
		t.Errorf("files without wheels:\n%s", d)
	}
	// a script environment that is not managed takes neither: its Dockerfile is what it was
	notManaged := s
	notManaged.Managed = false
	a, _ := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5"}, notManaged)
	notManaged.Wheels, notManaged.Files = nil, nil
	b, _ := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5"}, notManaged)
	if a != b {
		t.Error("a scripting environment takes no wheel and no file")
	}
	// an R image takes neither
	r1, _ := Dockerfile("r", "rocker/r-ver:4.6.0", nil, Settings{CRANURL: "https://cran.example/", Jobs: 3, Managed: true, Wheels: s.Wheels, Files: s.Files})
	r2, _ := Dockerfile("r", "rocker/r-ver:4.6.0", nil, Settings{CRANURL: "https://cran.example/", Jobs: 3, Managed: true})
	if r1 != r2 {
		t.Error("an R image takes no wheel and no file")
	}
	for name, bad := range map[string]Settings{
		"a machine":                  {Managed: true, Wheels: []Wheel{{"riscv64", "https://x.example/a.whl", sum}}},
		"a wheel's address":          {Managed: true, Wheels: []Wheel{{"x86_64", "ftp://x.example/a.whl", sum}}},
		"an address with a fragment": {Managed: true, Wheels: []Wheel{{"x86_64", "https://x.example/a.whl#sha256=" + sum, sum}}},
		"a wheel's sha256":           {Managed: true, Wheels: []Wheel{{"x86_64", "https://x.example/a.whl", "ABC"}}},
		"not a wheel":                {Managed: true, Wheels: []Wheel{{"x86_64", "https://x.example/a.tar.gz", sum}}},
		"a file's address":           {Managed: true, Files: []File{{"https://x.example/a b", sum, "/opt/a"}}},
		"a file's sha256":            {Managed: true, Files: []File{{"https://x.example/a", "sha256:" + sum, "/opt/a"}}},
		"a path":                     {Managed: true, Files: []File{{"https://x.example/a", sum, "/opt/a; rm -rf /"}}},
		"a relative path":            {Managed: true, Files: []File{{"https://x.example/a", sum, "opt/a"}}},
	} {
		if _, err := Dockerfile("python", "python:3.14-slim", nil, bad); err == nil {
			t.Errorf("%s: no error", name)
		} else if _, ok := err.(ValueError); !ok {
			t.Errorf("%s: %v is not a ValueError", name, err)
		}
	}
}

// The sizes of a version's weights are its `# tabularia:weights` line; without one the build takes every size the recipe has.
func TestTheSizesOfTheWeightsAreTheirLine(t *testing.T) {
	for text, want := range map[string][]string{
		"numpy==2.5.5\n# tabularia:weights 5m,28m\n# tabularia:licence abc\n": {"5m", "28m"},
		"# tabularia:weights 28M\nnumpy==2.5.5\n":                             {"28m"},
		"# tabularia:weights   209m,123m,209m  \n":                            {"209m", "123m"},
		"# tabularia:weights ,\n":                                             nil,
	} {
		got, ok := WeightsSizes(text)
		if !ok || strings.Join(got, ",") != strings.Join(want, ",") {
			t.Errorf("%q: %v %v, wanted %v", text, got, ok, want)
		}
	}
	for _, text := range []string{"", "numpy==2.5.5\n", "# tabularia:licence 5m\n", "numpy # tabularia:weights 5m\n", "#tabularia:weights\n"} {
		if got, ok := WeightsSizes(text); ok {
			t.Errorf("%q: sizes %v from no line", text, got)
		}
	}
}

// A managed R image (a model group's): the recipe's packages only, each with the sha256 of its source tarball, installed by
// RInstallPinned (downloaded, checked, then R CMD INSTALL from the file), and arrow's own download checked by its checksums.
func TestAManagedRImage(t *testing.T) {
	d, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"cpp11@0.5.5", "arrow@26.0.1"},
		Settings{CRANURL: "https://cran.example/", Jobs: 3, Managed: true, Hashes: map[string][]string{"cpp11@0.5.5": {sumA}, "arrow@26.0.1": {sumB}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"FROM rocker/r-ver:4.6.0\nENV NOT_CRAN=true LIBARROW_MINIMAL=false CRAN=\"https://cran.example/\"\n",
		"COPY <<'TABULARIA_PACKAGES' /opt/tabularia/packages.txt\ncpp11@0.5.5 " + sumA + "\narrow@26.0.1 " + sumB + "\nTABULARIA_PACKAGES\n",
		"COPY <<'TABULARIA_INSTALL' /opt/tabularia/install.R\n" + RInstallPinned + "TABULARIA_INSTALL\n",
		"RUN TABULARIA_JOBS=3 Rscript /opt/tabularia/install.R\n", "USER 10001\n"} {
		if !strings.Contains(d, want) {
			t.Errorf("no %q in\n%s", want, d)
		}
	}
	// nothing the installer downloads by itself: no remotes, no install.packages, no package without its version
	for _, not := range []string{"remotes", "install.packages", "install_version", "\narrow\n"} {
		if strings.Contains(d, not) {
			t.Errorf("%q in a managed R image:\n%s", not, d)
		}
	}
	// the installer: every tarball checked before any is installed, from CRAN or its archive, then R CMD INSTALL of the file; the
	// sha256 recorded where the check Job reads them
	for _, want := range []string{`"/src/contrib/", file[i]`, `"/src/contrib/Archive/", name[i], "/", file[i]`, `system2("sha256sum"`,
		"TABULARIA_SHA256_MISMATCH", `c("CMD", "INSTALL", shQuote(path[i]))`, `"` + PackageHashesFile + `"`, `ARROW_R_ENFORCE_CHECKSUM = "true"`} {
		if !strings.Contains(RInstallPinned, want) {
			t.Errorf("the installer has no %s", want)
		}
	}
	if strings.Index(RInstallPinned, "TABULARIA_SHA256_MISMATCH") > strings.Index(RInstallPinned, `"CMD", "INSTALL"`) {
		t.Error("a package is installed before every tarball is checked")
	}
	// the layers before the packages are every R image's: a scripting environment's shares them
	plain, _ := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"cpp11@0.5.5", "arrow@26.0.1"}, Settings{CRANURL: "https://cran.example/", Jobs: 3})
	layers := func(d string) string { before, _, _ := strings.Cut(d, "COPY <<'TABULARIA_PACKAGES'"); return before }
	if layers(d) != layers(plain) {
		t.Errorf("the layers before the packages differ:\n%s\n----\n%s", layers(d), layers(plain))
	}
}

// A managed image installs no package without its hash: the Dockerfile is refused (the version fails), whatever the language.
func TestAManagedImageRefusesAPackageWithoutItsHash(t *testing.T) {
	r := Settings{CRANURL: "https://cran.example/", Jobs: 2, Managed: true}
	py := Settings{Jobs: 2, Managed: true}
	for name, c := range map[string]struct {
		language string
		packages []string
		s        Settings
		hashes   map[string][]string
	}{
		"an R package without its hash":        {"r", []string{"cpp11@0.5.5", "arrow@26.0.1"}, r, map[string][]string{"cpp11@0.5.5": {sumA}}},
		"an R package with two hashes":         {"r", []string{"cpp11@0.5.5"}, r, map[string][]string{"cpp11@0.5.5": {sumA, sumB}}},
		"an R hash that is not a sha256":       {"r", []string{"cpp11@0.5.5"}, r, map[string][]string{"cpp11@0.5.5": {"md5:abc"}}},
		"an R package without its version":     {"r", []string{"cpp11"}, r, map[string][]string{"cpp11": {sumA}}},
		"a Python package without its hash":    {"python", []string{"numpy==2.5.5", "six==1.17.0"}, py, map[string][]string{"numpy==2.5.5": {sumA}}},
		"a Python hash that is not a sha256":   {"python", []string{"numpy==2.5.5"}, py, map[string][]string{"numpy==2.5.5": {sumA, "ABC"}}},
		"a Python package without a version":   {"python", []string{"numpy>=2"}, py, map[string][]string{"numpy>=2": {sumA}}},
		"no hashes at all (a managed R image)": {"r", []string{"cpp11@0.5.5"}, r, nil},
	} {
		c.s.Hashes = c.hashes
		_, err := Dockerfile(c.language, "base", c.packages, c.s)
		if _, ok := err.(ValueError); !ok {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := Dockerfile("python", "base", []string{"six==1.17.0"}, Settings{Managed: true}); err == nil ||
		err.Error() != "the package «six==1.17.0» has no sha256: a managed image installs only packages pinned by their hash" {
		t.Errorf("the sentence: %v", err)
	}
}
