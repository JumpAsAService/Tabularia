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

// The images of the scripting environments, and the R images of the model groups, are byte for
// byte what they were before managed Python images came (the R one by its sha256: its install
// script is long).
func TestTheImagesThatWereStayAsTheyWere(t *testing.T) {
	for _, managed := range []bool{false, true} {
		r, err := Dockerfile("r", "rocker/r-ver:4.6.0", []string{"zoo@1.9-1", "sandwich"}, Settings{CRANURL: "https://cran.example/", Jobs: 3, Managed: managed})
		if err != nil {
			t.Fatal(err)
		}
		if sum := sha256.Sum256([]byte(r)); hex.EncodeToString(sum[:]) != "1cd14b912c40fe40243c4195db10d8f438b2158f9b7e0336f25cfbe96df2c80e" {
			t.Errorf("the R image changed (managed %v):\n%s", managed, r)
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

// A managed Python image: the recipe's packages only, wheels without their dependencies, then pip check.
func TestAManagedPythonImage(t *testing.T) {
	d, err := Dockerfile("python", "python:3.14-slim", []string{"numpy==2.5.5", "pyarrow==26.0.1"}, Settings{Jobs: 2, Managed: true})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM python:3.14-slim\n", "/opt/tabularia/requirements.txt\nnumpy==2.5.5\npyarrow==26.0.1\nTABULARIA_PACKAGES\n",
		"RUN pip install --no-deps --only-binary=:all: -r /opt/tabularia/requirements.txt && pip check\n", "USER 10001\n"} {
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
