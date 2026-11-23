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
	// PackageHashesFile is where a managed image records the sha256 of every file it installed a package from, one
	// `<sha256>  <what>` line each (sha256sum's format): R `name@version`, Python the wheel's file name. The check Job adds them to
	// the lock's lines, which the Administration card shows.
	PackageHashesFile = "/opt/tabularia/package-sha256.txt"

	// SystemLibrariesFile is where an R image records the versions of the Debian libraries its recipe
	// names (`system_libraries`), one `package@version` line each; absent from every other image.
	SystemLibrariesFile = "/opt/tabularia/system-libraries.txt"

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
	sha256Hex     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// the file name of a wheel, as an address names it ("+" decoded)
	wheelFile = regexp.MustCompile(`^[A-Za-z0-9_.+-]+\.whl$`)
	// a package of a managed image, as the hashes key it
	pinnedR  = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9.]*@[0-9][0-9.-]*$`)
	pinnedPy = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+$`)
	// where an image keeps the files it downloads: an absolute path of plain names (nothing a shell or a Dockerfile reads)
	filePath = regexp.MustCompile(`^/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$`)
	// the line of a managed environment's packages that says which sizes of a pre-trained model its image holds
	weightsLine = regexp.MustCompile(`(?m)^#[ \t]*tabularia:weights[ \t]+(\S+)[ \t]*$`)
	// the architectures of BuildKit (TARGETARCH) and the machines (uname -m) a wheel is built for
	machines = map[string]string{"x86_64": "amd64", "aarch64": "arm64"}
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
	// closure pinned; `pip check` then fails the build when one is missing. An R image installs
	// only the packages given too (no unpinned arrow), each from its source tarball checked by its
	// sha256 (RInstallPinned), on the same layers as every R image. Both need Hashes for every package.
	Managed bool
	// SystemLibraries are Debian packages an R image installs besides RSystemLibraries: a
	// managed environment's own (a model group's recipe, `system_libraries`: the GNU Scientific
	// Library of the customer lifetime group). Never taken from a user's input. None: the
	// Dockerfile is the one every R image has had.
	SystemLibraries []string
	// Wheels: wheels a managed Python image installs by their address, before the requirements, each checked by its sha256
	// (`pip install "<url>#sha256=<hex>"`); one RUN per machine, guarded by BuildKit's TARGETARCH, so that one Dockerfile
	// serves x86_64 and aarch64. A recipe's `wheels`, the mirror setting already applied. None: the Dockerfile is the one
	// every managed Python image has had.
	Wheels []Wheel
	// Hashes: the sha256 of each package of a managed image (a recipe's `hashes`, keyed by the package's line). Required there for
	// every package: a managed Python image installs with `pip --require-hashes` (every wheel checked, none taken that the list
	// does not pin), a managed R image downloads every source tarball, checks it and installs it from the file with
	// `R CMD INSTALL` (nothing else is downloaded by the installer). Ignored for every other image.
	Hashes map[string][]string
	// Files: files a managed Python image downloads (`ADD --checksum=sha256:<hex> <url> <path>`), after the requirements: the
	// weights of a pre-trained model. The build fails on a different byte. A recipe's `weights` of the sizes chosen.
	Files []File
}

// Wheel is a wheel of a recipe, for one machine (`uname -m`: x86_64 or aarch64).
type Wheel struct {
	Machine, URL, SHA256 string
}

// File is a file an image downloads while it is built: its address, its sha256 (hex) and the absolute path it lands at, readable
// by the user the scripts run as.
type File struct {
	URL, SHA256, Path string
}

// WeightsSizes are the sizes a managed environment's packages text asks for in its `# tabularia:weights 5m,28m` line (docs/specs/
// foundation-forecast.md, «Where the acceptance is kept, without a migration»); false when it has no such line, which the build
// reads as every size the recipe holds. The package parser drops everything after a `#`, so the line changes no other build. The
// sizes are as written, lower-cased and without repeats; the recipe says which of them exist.
func WeightsSizes(packages string) ([]string, bool) {
	m := weightsLine.FindStringSubmatch(packages)
	if m == nil {
		return nil, false
	}
	var out []string
	seen := map[string]bool{}
	for _, size := range strings.Split(strings.ToLower(m[1]), ",") {
		if size = strings.TrimSpace(size); size != "" && !seen[size] {
			seen[size] = true
			out = append(out, size)
		}
	}
	return out, true
}

func heredoc(path string, lines []string, mark string) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "COPY <<'%s' %s\n", mark, path)
	for _, x := range lines {
		sb.WriteString(x + "\n")
	}
	return sb.String() + mark + "\n"
}

// wheelSteps are the RUNs that install the recipe's wheels: one a machine, guarded by `ARG TARGETARCH` (BuildKit's: amd64, arm64)
// so that the one Dockerfile builds for both; pip checks each file against its sha256 and takes none of its dependencies (the
// recipe's packages are the closure, and `pip check` after them says what is missing).
func wheelSteps(wheels []Wheel) (string, error) {
	if len(wheels) == 0 {
		return "", nil
	}
	var sb strings.Builder
	sb.WriteString("ARG TARGETARCH\n")
	for _, w := range wheels {
		arch, ok := machines[w.Machine]
		if !ok {
			return "", ValueError(fmt.Sprintf("«%s» is not a machine a wheel is built for (x86_64 or aarch64)", w.Machine))
		}
		if !url.MatchString(w.URL) || strings.ContainsAny(w.URL, "#`$") {
			return "", ValueError(fmt.Sprintf("«%s» is not the address of a wheel", w.URL))
		}
		if !sha256Hex.MatchString(w.SHA256) {
			return "", ValueError(fmt.Sprintf("the sha256 of «%s» is not 64 hexadecimal digits", w.URL))
		}
		address := strings.TrimSpace(w.URL)
		file := strings.ReplaceAll(address[strings.LastIndex(address, "/")+1:], "%2B", "+")
		if !wheelFile.MatchString(file) {
			return "", ValueError(fmt.Sprintf("«%s» does not name a wheel", w.URL))
		}
		fmt.Fprintf(&sb, "RUN if [ \"$TARGETARCH\" = \"%s\" ]; then pip install --no-deps --only-binary=:all: \"%s#sha256=%s\" && "+
			"echo \"%s  %s\" >> %s; fi\n", arch, address, w.SHA256, w.SHA256, file, PackageHashesFile)
	}
	return sb.String(), nil
}

// fileSteps are the steps that download the recipe's files: the folders first (`mkdir -p`: an ADD with --chmod gives the folders it
// creates the file's mode too, which has no x bit, and the user the scripts run as could not enter them), then one ADD a file, where
// BuildKit fails the build on a byte that is not the sha256's, and the file is readable by that user (an ADD of an address is
// owner-only by default).
func fileSteps(files []File) (string, error) {
	var dirs []string
	var adds strings.Builder
	seen := map[string]bool{}
	for _, f := range files {
		if !url.MatchString(f.URL) || strings.ContainsAny(f.URL, " `$") {
			return "", ValueError(fmt.Sprintf("«%s» is not the address of a file", f.URL))
		}
		if !sha256Hex.MatchString(f.SHA256) {
			return "", ValueError(fmt.Sprintf("the sha256 of «%s» is not 64 hexadecimal digits", f.URL))
		}
		if !filePath.MatchString(f.Path) {
			return "", ValueError(fmt.Sprintf("«%s» is not a path an image can keep a file at", f.Path))
		}
		if dir := f.Path[:strings.LastIndex(f.Path, "/")]; dir != "" && !seen[dir] {
			seen[dir] = true
			dirs = append(dirs, dir)
		}
		fmt.Fprintf(&adds, "ADD --checksum=sha256:%s --chmod=0644 %s %s\n", f.SHA256, strings.TrimSpace(f.URL), f.Path)
	}
	if len(dirs) == 0 {
		return "", nil
	}
	return "RUN mkdir -p " + strings.Join(dirs, " ") + "\n" + adds.String(), nil
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
		before, after := "", ""
		if s.Managed {
			// wheels only (nothing compiles), from PyPI or its mirror, without what the wheels ask for: the recipe is the
			// closure, and pip check says what it lacks. Every wheel is checked against the recipe's sha256 when it is
			// downloaded and again when it is installed (--require-hashes: a requirement without a hash is an error), and the
			// sha256 of the files installed are recorded for the lock
			var err error
			if wanted, err = hashedRequirements(packages, s.Hashes); err != nil {
				return "", err
			}
			install = "RUN pip download --no-deps --only-binary=:all: --require-hashes -d /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
				" && pip install --no-deps --only-binary=:all: --require-hashes --no-index --find-links /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
				" && (cd /tmp/tabularia-wheels && sha256sum *.whl) >> " + PackageHashesFile + " && rm -rf /tmp/tabularia-wheels && pip check\n"
			if before, err = wheelSteps(s.Wheels); err != nil {
				return "", err
			}
			if after, err = fileSteps(s.Files); err != nil {
				return "", err
			}
		}
		return "FROM " + baseImage + "\n" + env + "\n" +
			heredoc("/opt/tabularia/requirements.txt", wanted, "TABULARIA_PACKAGES") + before + install + after + user, nil
	}
	cran := pyd.Strip(s.CRANURL)
	if !url.MatchString(cran) {
		return "", ValueError("SCRIPTS__CRAN_URL is not an http(s) address")
	}
	libraries := RSystemLibraries
	record := ""
	if len(s.SystemLibraries) > 0 {
		libraries = append(append([]string{}, RSystemLibraries...), s.SystemLibraries...)
		for _, l := range s.SystemLibraries {
			if !debianPackage.MatchString(l) {
				return "", ValueError(fmt.Sprintf("«%s» is not the name of a Debian package", l))
			}
		}
		// what apt installed, as `package@version` lines the version's check adds to its lock: the
		// Administration card says the version from here. Only an image that names libraries has it
		record = "RUN mkdir -p /opt/tabularia && dpkg-query -W -f '${Package}@${Version}\\n' " + strings.Join(s.SystemLibraries, " ") +
			" > " + SystemLibrariesFile + "\n"
	}
	install := RInstall
	if s.Managed {
		// the recipe's packages only, each with its sha256 (arrow is one of them)
		var err error
		if wanted, err = hashedRPackages(packages, s.Hashes); err != nil {
			return "", err
		}
		install = RInstallPinned
	}
	// NOT_CRAN and LIBARROW_MINIMAL: arrow, built from source, gets S3 support. rocker's
	// images point R at a package manager of their own; CRAN replaces it
	return "FROM " + baseImage + "\nENV NOT_CRAN=true LIBARROW_MINIMAL=false CRAN=\"" + cran + "\"\n" +
		"RUN apt-get update && apt-get install -y --no-install-recommends " + strings.Join(libraries, " ") +
		" && rm -rf /var/lib/apt/lists/*\n" + record +
		`RUN printf 'options(repos = c(CRAN = "%s"))\n' "$CRAN" >> "$(R RHOME)/etc/Rprofile.site"` + "\n" +
		heredoc("/opt/tabularia/packages.txt", wanted, "TABULARIA_PACKAGES") +
		heredoc("/opt/tabularia/install.R", scriptnodes.SplitLines(install), "TABULARIA_INSTALL") +
		fmt.Sprintf("RUN TABULARIA_JOBS=%d Rscript /opt/tabularia/install.R\n", s.Jobs) + user, nil
}

// hashedRequirements are a managed Python image's requirements: `name==version --hash=sha256:<hex> …`, every package with the
// sha256 of its wheels.
func hashedRequirements(packages []string, hashes map[string][]string) ([]string, error) {
	out := make([]string, 0, len(packages))
	for _, p := range packages {
		if !pinnedPy.MatchString(p) {
			return nil, ValueError(fmt.Sprintf("«%s» is not a package pinned to one version (name==version)", p))
		}
		line := p
		for _, sum := range hashes[p] {
			if !sha256Hex.MatchString(sum) {
				return nil, ValueError(fmt.Sprintf("a sha256 of «%s» is not 64 hexadecimal digits", p))
			}
			line += " --hash=sha256:" + sum
		}
		if line == p {
			return nil, ValueError(fmt.Sprintf("the package «%s» has no sha256: a managed image installs only packages pinned by their hash", p))
		}
		out = append(out, line)
	}
	return out, nil
}

// hashedRPackages are a managed R image's packages: `name@version <sha256>`, every package with the sha256 of its source tarball.
func hashedRPackages(packages []string, hashes map[string][]string) ([]string, error) {
	out := make([]string, 0, len(packages))
	for _, p := range packages {
		if !pinnedR.MatchString(p) {
			return nil, ValueError(fmt.Sprintf("«%s» is not a package pinned to one version (name@version)", p))
		}
		sums := hashes[p]
		if len(sums) != 1 || !sha256Hex.MatchString(sums[0]) {
			return nil, ValueError(fmt.Sprintf("the package «%s» has no sha256 (one, of its source tarball): a managed image installs only packages pinned by their hash", p))
		}
		out = append(out, p+" "+sums[0])
	}
	return out, nil
}

// RInstall is script_builds._R_INSTALL: how an R image installs its packages.
const RInstall = "lines <- trimws(readLines(\"/opt/tabularia/packages.txt\"))\nlines <- lines[nzchar(lines)]\nname <- sub(\"@.*$\", \"\", lines)\npinned <- grepl(\"@\", lines, fixed = TRUE)\n# CRAN has sources only: the packages compile. At most TABULARIA_JOBS compilers at\n# once, whatever the cores of the node (a C++ compiler of arrow takes over 1 GB): a\n# package alone gets them all, packages side by side one each\njobs <- max(1L, suppressWarnings(as.integer(Sys.getenv(\"TABULARIA_JOBS\", \"1\"))), na.rm = TRUE)\nSys.setenv(MAKEFLAGS = paste0(\"-j\", jobs))\n# the pinned ones first: installing the others afterwards does not replace them\nif (any(pinned)) {\n  install.packages(\"remotes\", Ncpus = 1L)\n  for (i in which(pinned)) {\n    remotes::install_version(name[i], version = sub(\"^[^@]*@\", \"\", lines[i]), upgrade = \"never\", Ncpus = 1L)\n  }\n}\n# arrow, the heaviest, alone\nif (!requireNamespace(\"arrow\", quietly = TRUE)) install.packages(\"arrow\", Ncpus = 1L)\nrest <- setdiff(unique(name[!pinned]), \"arrow\")\nSys.setenv(MAKEFLAGS = \"-j1\")\nif (length(rest)) install.packages(rest, Ncpus = jobs)\n# install.packages only warns about a package it could not install\nmissing <- name[!vapply(name, requireNamespace, logical(1), quietly = TRUE)]\nif (length(missing)) stop(\"not installed: \", paste(unique(missing), collapse = \", \"))\n"

// RInstallPinned is how a managed R image (a model group's) installs its packages: every line of packages.txt is
// `name@version <sha256>`, in the order of the recipe (each package after the ones it needs). First every source tarball is
// downloaded from CRAN (SCRIPTS__CRAN_URL), the current one or the archived one, and checked against its sha256: a byte that
// is not the recipe's stops the build before anything compiles, with a line that starts with TABULARIA_SHA256_MISMATCH. Then
// each is installed from its file with `R CMD INSTALL`, which downloads nothing and fails when a package it needs is not
// installed yet. Last, the installed versions are compared with the recipe's, and the sha256 recorded for the lock.
const RInstallPinned = `lines <- trimws(readLines("/opt/tabularia/packages.txt"))
lines <- lines[nzchar(lines)]
pkg <- sub(" .*$", "", lines)
want <- sub("^.* ", "", lines)
name <- sub("@.*$", "", pkg)
version <- sub("^[^@]*@", "", pkg)
file <- paste0(name, "_", version, ".tar.gz")
cran <- sub("/+$", "", Sys.getenv("CRAN"))
src <- "/tmp/tabularia-src"
dir.create(src, showWarnings = FALSE)
path <- file.path(src, file)
fetch <- function(url, dest) tryCatch(download.file(url, dest, mode = "wb", quiet = TRUE) == 0,
  error = function(e) FALSE, warning = function(w) FALSE)
for (i in seq_along(pkg)) {
  urls <- c(paste0(cran, "/src/contrib/", file[i]), paste0(cran, "/src/contrib/Archive/", name[i], "/", file[i]))
  got <- FALSE
  for (u in urls) if (!got) got <- fetch(u, path[i])
  if (!got) stop("cannot download ", file[i], " from ", cran, " (src/contrib, then src/contrib/Archive/", name[i], ")")
  sum <- sub(" .*$", "", system2("sha256sum", shQuote(path[i]), stdout = TRUE))
  if (!identical(sum, want[i])) {
    message("TABULARIA_SHA256_MISMATCH ", pkg[i], ": the file ", file[i], " downloaded from ", cran, " has sha256 ", sum,
      ", the recipe pins ", want[i], ". The repository serves other bytes than the ones the recipe was pinned on")
    quit(status = 1)
  }
}
# at most TABULARIA_JOBS compilers at once, whatever the cores of the node (a C++ compiler of arrow takes over 1 GB).
# arrow's installer downloads libarrow, and checks it against the SHA-512 its tarball (checked above) ships: always
jobs <- max(1L, suppressWarnings(as.integer(Sys.getenv("TABULARIA_JOBS", "1"))), na.rm = TRUE)
Sys.setenv(MAKEFLAGS = paste0("-j", jobs), ARROW_R_ENFORCE_CHECKSUM = "true")
r <- file.path(R.home("bin"), "R")
for (i in seq_along(pkg)) {
  if (system2(r, c("CMD", "INSTALL", shQuote(path[i]))) != 0) stop("R CMD INSTALL ", file[i], " failed")
}
installed <- vapply(name, function(p) tryCatch(utils::packageDescription(p)$Version, error = function(e) NA_character_), "")
bad <- is.na(installed) | installed != version
if (any(bad)) stop("not installed at the version pinned: ", paste(pkg[bad], collapse = ", "))
unlink(src, recursive = TRUE)
dir.create("/opt/tabularia", showWarnings = FALSE)
cat(paste0(want, "  ", pkg, "\n"), file = "/opt/tabularia/package-sha256.txt", sep = "")
`
