package scriptimage

import (
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
