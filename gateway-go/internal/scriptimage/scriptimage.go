// Package scriptimage is the Dockerfile of a scripting environment's image: a base image, a
// language and a list of packages (script_builds.dockerfile). The build loop of the scripting
// environments (api/jobs_script_builds.go) hands it to BuildKit; cmd/modelgroup prints it
// for a model group's recipe, so that the image of a group can be built and self-tested on
// a laptop exactly as the cluster builds it (docs/specs/statistical-models.md).
package scriptimage

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
)

// ScriptUID is the user the scripts run as, in the image and in the pods.
const ScriptUID = 10001

var (
	// RSystemLibraries are the Debian packages an R image compiles its packages with.
	RSystemLibraries = []string{
		"build-essential", "gfortran", "cmake", "pkg-config", "libcurl4-openssl-dev", "libssl-dev", "libxml2-dev",
		"zlib1g-dev", "libbz2-dev", "liblzma-dev", "libzstd-dev", "libicu-dev", "libgit2-dev", "libuv1-dev", "libfontconfig1-dev",
		"libfreetype-dev", "libharfbuzz-dev", "libfribidi-dev", "libpng-dev", "libjpeg-dev", "libtiff-dev",
	}
	// BasePackages are installed in every image of the language, before its own.
	BasePackages = map[string][]string{"python": {"polars", "pyarrow", "marimo"}, "r": {"arrow"}}
	url          = regexp.MustCompile(`^https?://[^\s"'\\]+\n?$`)
	// a Debian package's name, as a recipe may name one
	debianPackage = regexp.MustCompile(`^[a-z0-9][a-z0-9+.-]+$`)
)

// ValueError is what script_builds raises for packages or settings that cannot become a
// Dockerfile: the version fails with it.
type ValueError string

func (e ValueError) Error() string { return string(e) }

// Settings are what the Dockerfile reads besides the version: the package indexes
// (SCRIPTS__PYPI_INDEX_URL, SCRIPTS__CRAN_URL), how many compilers run at once, and whether the
// environment is managed by Tabularia (a statistical model group's: docs/specs/
// statistical-models.md, «The runner in Python»).
type Settings struct {
	PyPIIndexURL, CRANURL string
	Jobs                  int64
	// Managed: a Python image installs only the packages given (no BasePackages: no polars, no
	// marimo), each one a wheel and none of their dependencies, since the list is the whole
	// closure pinned; `pip check` then fails the build when one is missing. An R image is the
	// same either way.
	Managed bool
	// SystemLibraries are Debian packages an R image installs besides RSystemLibraries: a
	// managed environment's own (a model group's recipe, `system_libraries`: the GNU Scientific
	// Library of the customer lifetime group). Never taken from a user's input. None: the
	// Dockerfile is the one every R image has had.
	SystemLibraries []string
}

func heredoc(path string, lines []string, mark string) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "COPY <<'%s' %s\n", mark, path)
	for _, x := range lines {
		sb.WriteString(x + "\n")
	}
	return sb.String() + mark + "\n"
}

// Dockerfile is script_builds.dockerfile: the image of `language` on `baseImage` with the
// language's base packages, then `packages` (already checked: one per element).
func Dockerfile(language, baseImage string, packages []string, s Settings) (string, error) {
	user := fmt.Sprintf("RUN useradd --uid %d --create-home --shell /usr/sbin/nologin tabularia\nUSER %d\nWORKDIR /home/tabularia\n",
		ScriptUID, ScriptUID)
	wanted := append(append([]string{}, BasePackages[language]...), packages...)
	if language == "python" && s.Managed {
		wanted = append([]string{}, packages...)
	}
	if language == "python" {
		index := pyd.Strip(s.PyPIIndexURL)
		if index != "" && !url.MatchString(index) {
			return "", ValueError("SCRIPTS__PYPI_INDEX_URL is not an http(s) address")
		}
		env := "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1"
		if index != "" {
			env += ` PIP_INDEX_URL="` + index + `"`
		}
		install := fmt.Sprintf("RUN MAKEFLAGS=-j%d pip install -r /opt/tabularia/requirements.txt\n", s.Jobs)
		if s.Managed {
			// wheels only (nothing compiles), from PyPI or its mirror, without what the wheels
			// ask for: the recipe is the closure, and pip check says what it lacks
			install = "RUN pip install --no-deps --only-binary=:all: -r /opt/tabularia/requirements.txt && pip check\n"
		}
		return "FROM " + baseImage + "\n" + env + "\n" +
			heredoc("/opt/tabularia/requirements.txt", wanted, "TABULARIA_PACKAGES") + install + user, nil
	}
	cran := pyd.Strip(s.CRANURL)
	if !url.MatchString(cran) {
		return "", ValueError("SCRIPTS__CRAN_URL is not an http(s) address")
	}
	libraries := RSystemLibraries
	if len(s.SystemLibraries) > 0 {
		libraries = append(append([]string{}, RSystemLibraries...), s.SystemLibraries...)
		for _, l := range s.SystemLibraries {
			if !debianPackage.MatchString(l) {
				return "", ValueError(fmt.Sprintf("«%s» is not the name of a Debian package", l))
			}
		}
	}
	// NOT_CRAN and LIBARROW_MINIMAL: arrow, built from source, gets S3 support. rocker's
	// images point R at a package manager of their own; CRAN replaces it
	return "FROM " + baseImage + "\nENV NOT_CRAN=true LIBARROW_MINIMAL=false CRAN=\"" + cran + "\"\n" +
		"RUN apt-get update && apt-get install -y --no-install-recommends " + strings.Join(libraries, " ") +
		" && rm -rf /var/lib/apt/lists/*\n" +
		`RUN printf 'options(repos = c(CRAN = "%s"))\n' "$CRAN" >> "$(R RHOME)/etc/Rprofile.site"` + "\n" +
		heredoc("/opt/tabularia/packages.txt", wanted, "TABULARIA_PACKAGES") +
		heredoc("/opt/tabularia/install.R", scriptnodes.SplitLines(RInstall), "TABULARIA_INSTALL") +
		fmt.Sprintf("RUN TABULARIA_JOBS=%d Rscript /opt/tabularia/install.R\n", s.Jobs) + user, nil
}

// RInstall is script_builds._R_INSTALL: how an R image installs its packages.
const RInstall = "lines <- trimws(readLines(\"/opt/tabularia/packages.txt\"))\nlines <- lines[nzchar(lines)]\nname <- sub(\"@.*$\", \"\", lines)\npinned <- grepl(\"@\", lines, fixed = TRUE)\n# CRAN has sources only: the packages compile. At most TABULARIA_JOBS compilers at\n# once, whatever the cores of the node (a C++ compiler of arrow takes over 1 GB): a\n# package alone gets them all, packages side by side one each\njobs <- max(1L, suppressWarnings(as.integer(Sys.getenv(\"TABULARIA_JOBS\", \"1\"))), na.rm = TRUE)\nSys.setenv(MAKEFLAGS = paste0(\"-j\", jobs))\n# the pinned ones first: installing the others afterwards does not replace them\nif (any(pinned)) {\n  install.packages(\"remotes\", Ncpus = 1L)\n  for (i in which(pinned)) {\n    remotes::install_version(name[i], version = sub(\"^[^@]*@\", \"\", lines[i]), upgrade = \"never\", Ncpus = 1L)\n  }\n}\n# arrow, the heaviest, alone\nif (!requireNamespace(\"arrow\", quietly = TRUE)) install.packages(\"arrow\", Ncpus = 1L)\nrest <- setdiff(unique(name[!pinned]), \"arrow\")\nSys.setenv(MAKEFLAGS = \"-j1\")\nif (length(rest)) install.packages(rest, Ncpus = jobs)\n# install.packages only warns about a package it could not install\nmissing <- name[!vapply(name, requireNamespace, logical(1), quietly = TRUE)]\nif (length(missing)) stop(\"not installed: \", paste(unique(missing), collapse = \", \"))\n"
