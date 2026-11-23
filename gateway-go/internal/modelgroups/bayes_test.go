package modelgroups

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
)

// The group `bayes` (docs/specs/bayes-image.md): its recipe loads, every package has its hashes, the toolchain, the two CmdStan
// tarballs and the switches are the spec's; it is shipped ahead of the models' catalogue, with no model yet.
func TestTheBayesRecipe(t *testing.T) {
	g, ok := Get("bayes")
	if !ok || g.Recipe.Language != "python" || g.Recipe.Base != "3.14" || len(g.Models) != 0 {
		t.Fatalf("the group: %+v", g)
	}
	r := g.Recipe
	if err := r.CheckHashes("bayes"); err != nil {
		t.Fatal(err)
	}
	if strings.Join(r.Packages, " ") != "numpy==2.5.5 pyarrow==26.0.1" || strings.Join(r.Toolchain, " ") != "g++ make" {
		t.Errorf("packages %v, toolchain %v", r.Packages, r.Toolchain)
	}
	want := map[string][2]string{
		"x86_64":  {"https://github.com/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz", "26d21c9737b35728e926f9b7a5d7362b38dc23e2e7d5e166c890e1fa74dac90c"},
		"aarch64": {"https://github.com/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0-linux-arm64.tar.gz", "c31e9f45a701c47f547381080ee804bf7e2428aace68eff68d4262b5c406aa63"},
	}
	if len(r.Sources) != 2 {
		t.Fatalf("sources: %+v", r.Sources)
	}
	for _, s := range r.Sources {
		if w := want[s.Machine]; s.Name != "cmdstan" || s.Version != "2.40.0" || s.URL != w[0] || s.SHA256 != w[1] {
			t.Errorf("the source: %+v", s)
		}
	}
	if c := r.CmdStan; c == nil || !c.Threads || !c.PrecompiledHeader || c.Jobs != 1 {
		t.Errorf("cmdstan: %+v", r.CmdStan)
	}
	if r.Resources.CPU != "4" || r.Resources.Memory != "2Gi" || r.Resources.Tmp != "2Gi" || r.MaxRows != 1000000 || r.MaxSeconds != 3600 {
		t.Errorf("resources %+v, max_rows %d, max_seconds %d", r.Resources, r.MaxRows, r.MaxSeconds)
	}
	if c := r.Compile; c == nil || *c != (CompileLimits{CPU: "1", Memory: "3Gi", Seconds: 300, Tmp: "512Mi"}) || r.Cache == nil || r.Cache.MaxBytes != 2<<30 {
		t.Errorf("compile %+v, cache %+v", r.Compile, r.Cache)
	}
	// the reference's CmdStan is the recipe's, machine by machine (check 1 compares the image's lock with it)
	raw, err := files.ReadFile("groups/bayes/reference/expected.json")
	if err != nil {
		t.Fatal(err)
	}
	var expected struct {
		CmdStan struct {
			Version string            `json:"version"`
			SHA256  map[string]string `json:"sha256"`
		} `json:"cmdstan"`
	}
	if err := json.Unmarshal(raw, &expected); err != nil {
		t.Fatal(err)
	}
	for _, s := range r.Sources {
		if expected.CmdStan.Version != s.Version || expected.CmdStan.SHA256[s.Machine] != s.SHA256 {
			t.Errorf("reference/expected.json's CmdStan %+v is not the recipe's %+v", expected.CmdStan, s)
		}
	}
	if len(expected.CmdStan.SHA256) != len(r.Sources) {
		t.Errorf("reference/expected.json names %d machines, the recipe %d", len(expected.CmdStan.SHA256), len(r.Sources))
	}
	// the self-test's code: the runner, the Python library, the programs and the data, gzipped
	code := mustCode(t, "bayes", nil, true)
	paths := map[string]bool{}
	for _, f := range code {
		paths[f.Path] = true
	}
	for _, p := range []string{"runner/run.py", "runner/py/bayes_selftest.py", "runner/py/bayes_cmdstan.py", "runner/py/bayes_stats.py",
		"common/py/tab_protocol.py", "reference/expected.json.gz", "reference/programs/ar1.stan.gz", "reference/programs/normal_known_sigma.stan.gz",
		"reference/programs/catalog_names.stan.gz", "reference/programs/catalog_points.stan.gz", "reference/programs/lkj_marginal.stan.gz",
		"reference/data/normal.json.gz", "reference/data/points.json.gz", "reference/data/catalog_names.json.gz", "reference/data/catalog_points.json.gz"} {
		if !paths[p] {
			t.Errorf("the self-test's code has no %s", p)
		}
	}
}

// The Dockerfile of the group `bayes` (docs/specs/bayes-image.md, «The Dockerfile the machinery generates»): the toolchain first,
// its versions recorded; the packages by hash; one stage a machine for CmdStan's tarball, checked by BuildKit and mounted, never
// copied into a layer; the fixed CmdStan step with -j1, STAN_THREADS, the two dependency files and the lock's line; the mirror.
func TestTheBayesDockerfile(t *testing.T) {
	d, err := Dockerfile("bayes", scriptcfg.DefaultBaseImages, scriptimage.Settings{Jobs: 2})
	if err != nil {
		t.Fatal(err)
	}
	want := "FROM python:3.14-slim AS tabularia-image\n" +
		"ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1\n" +
		"RUN apt-get update && apt-get install -y --no-install-recommends g++ make && rm -rf /var/lib/apt/lists/* \\\n" +
		" && mkdir -p /opt/tabularia && dpkg-query -W -f '${Package}@${Version}\\n' g++ g++-14 make > /opt/tabularia/system-libraries.txt\n" +
		"COPY <<'TABULARIA_PACKAGES' /opt/tabularia/requirements.txt\n" +
		"numpy==2.5.5 --hash=sha256:c48960d420e90cf305cb1fde0d4e6082cc90512cda87a9acf0b6e04e91cf4abe --hash=sha256:cc6eb7502facce90349b4097a26821af224aaa0ab36e78304711cdb544352882\n" +
		"pyarrow==26.0.1 --hash=sha256:0044cb9303c3ae082cc52a2493312109c955bcfa84e87be768736f7d6d8f6257 --hash=sha256:7ae48ab53610b85e004b03bccff593fbdf22186d0ab48ed42c90797915dac814\n" +
		"TABULARIA_PACKAGES\n" +
		"RUN pip download --no-deps --only-binary=:all: --require-hashes -d /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
		" && pip install --no-deps --only-binary=:all: --require-hashes --no-index --find-links /tmp/tabularia-wheels -r /opt/tabularia/requirements.txt" +
		" && (cd /tmp/tabularia-wheels && sha256sum *.whl) >> /opt/tabularia/package-sha256.txt && rm -rf /tmp/tabularia-wheels && pip check\n" +
		"FROM scratch AS cmdstan-amd64\n" +
		"ADD --checksum=sha256:26d21c9737b35728e926f9b7a5d7362b38dc23e2e7d5e166c890e1fa74dac90c https://github.com/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0.tar.gz /cmdstan.tar.gz\n" +
		"FROM scratch AS cmdstan-arm64\n" +
		"ADD --checksum=sha256:c31e9f45a701c47f547381080ee804bf7e2428aace68eff68d4262b5c406aa63 https://github.com/stan-dev/cmdstan/releases/download/v2.40.0/cmdstan-2.40.0-linux-arm64.tar.gz /cmdstan.tar.gz\n" +
		"FROM cmdstan-${TARGETARCH} AS cmdstan-source\n" +
		"FROM tabularia-image\n" +
		"RUN --mount=type=bind,from=cmdstan-source,target=/tabularia-src \\\n" +
		"    tar -xzf /tabularia-src/cmdstan.tar.gz -C /opt && mv /opt/cmdstan-2.40.0 /opt/cmdstan && cd /opt/cmdstan \\\n" +
		" && rm -f bin/mac-stanc bin/windows-stanc \\\n" +
		" && printf 'STAN_THREADS=true\\n' > make/local \\\n" +
		" && make -j1 build \\\n" +
		" && make src/cmdstan/main_threads.d \\\n" +
		" && for h in stan/src/stan/model/model_header.hpp.gch/*.hpp.gch; do make \"${h%.hpp.gch}.d\"; done \\\n" +
		" && echo \"$(sha256sum /tabularia-src/cmdstan.tar.gz | cut -d' ' -f1)  cmdstan@2.40.0\" >> /opt/tabularia/package-sha256.txt\n" +
		"RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tabularia\nUSER 10001\nWORKDIR /home/tabularia\n"
	if d != want {
		t.Errorf("the Dockerfile:\n%s\nwanted:\n%s", d, want)
	}
	// a mirror replaces GitHub's host and keeps the path and the sha256
	m, err := DockerfileFor("bayes", scriptcfg.DefaultBaseImages, scriptimage.Settings{Jobs: 2}, Build{SourcesBaseURL: "https://mirror.example/gh/"})
	if err != nil {
		t.Fatal(err)
	}
	if m != strings.ReplaceAll(d, "https://github.com/", "https://mirror.example/gh/") || strings.Contains(m, "github.com") {
		t.Errorf("the mirror:\n%s", m)
	}
	// the jobs of a build (SCRIPTS__BUILD_MEMORY) never reach CmdStan's step: the recipe's -j1 stays
	if j, _ := Dockerfile("bayes", scriptcfg.DefaultBaseImages, scriptimage.Settings{Jobs: 8}); j != d {
		t.Error("the build's jobs changed the Dockerfile")
	}
	// the other groups' Dockerfiles have none of it
	for _, g := range Groups() {
		if g.ID == "bayes" {
			continue
		}
		o, err := Dockerfile(g.ID, scriptcfg.DefaultBaseImages, scriptimage.Settings{CRANURL: "https://cran.r-project.org", Jobs: 2})
		if err != nil {
			t.Fatal(err)
		}
		for _, not := range []string{"cmdstan", "FROM scratch", " AS ", "g++-14", "--mount"} {
			if strings.Contains(o, not) {
				t.Errorf("%s: %q in its Dockerfile", g.ID, not)
			}
		}
	}
}

// What load refuses in a recipe's build (docs/specs/bayes-image.md, «Changes to the machinery»), one sentence each.
func TestABayesRecipeThatBuildsIsChecked(t *testing.T) {
	clone := func() Recipe {
		raw, err := files.ReadFile("groups/bayes/recipe.json")
		if err != nil {
			t.Fatal(err)
		}
		var r Recipe
		if err := json.Unmarshal(raw, &r); err != nil {
			t.Fatal(err)
		}
		return r
	}
	if err := clone().checkBuild("bayes"); err != nil {
		t.Fatal(err)
	}
	for what, c := range map[string]struct {
		mutate func(r *Recipe)
		says   string
	}{
		"an R image":                 {func(r *Recipe) { r.Language = "r" }, "are for a Python image"},
		"a toolchain outside":        {func(r *Recipe) { r.Toolchain = append(r.Toolchain, "curl") }, "«curl» is not a package of the toolchain"},
		"a toolchain twice":          {func(r *Recipe) { r.Toolchain = append(r.Toolchain, "make") }, "or it is named twice"},
		"no g++":                     {func(r *Recipe) { r.Toolchain = []string{"make"} }, "the toolchain's g++ and make"},
		"a source without a sha256":  {func(r *Recipe) { r.Sources[0].SHA256 = "" }, "is not 64 hexadecimal digits"},
		"an upper-case sha256":       {func(r *Recipe) { r.Sources[0].SHA256 = strings.ToUpper(r.Sources[0].SHA256) }, "is not 64 hexadecimal digits"},
		"a bad machine":              {func(r *Recipe) { r.Sources[1].Machine = "riscv64" }, "machine «riscv64»"},
		"two for a machine":          {func(r *Recipe) { r.Sources[1].Machine = "x86_64" }, "is given twice for x86_64"},
		"two versions":               {func(r *Recipe) { r.Sources[1].Version = "2.39.0" }, "or at two versions"},
		"an http address":            {func(r *Recipe) { r.Sources[0].URL = strings.Replace(r.Sources[0].URL, "https://", "http://", 1) }, "has no https address"},
		"an address with a fragment": {func(r *Recipe) { r.Sources[0].URL += "#x" }, "has no https address"},
		"not a tarball":              {func(r *Recipe) { r.Sources[0].URL = strings.TrimSuffix(r.Sources[0].URL, ".tar.gz") + ".zip" }, "has no https address"},
		"no x86_64":                  {func(r *Recipe) { r.Sources = r.Sources[1:] }, "has no tarball for x86_64"},
		"a source of another name":   {func(r *Recipe) { r.Sources[0].Name, r.Sources[1].Name = "stan", "stan" }, "a source named «cmdstan»"},
		"sources without cmdstan":    {func(r *Recipe) { r.CmdStan = nil }, "the one needs the other"},
		"cmdstan without sources":    {func(r *Recipe) { r.Sources = nil }, "the one needs the other"},
		"-j3":                        {func(r *Recipe) { r.CmdStan.Jobs = 3 }, "make runs -j1 or -j2"},
		"-j0":                        {func(r *Recipe) { r.CmdStan.Jobs = 0 }, "make runs -j1 or -j2"},
		"a compile's memory":         {func(r *Recipe) { r.Compile.Memory = "3G" }, "compile needs"},
		"a compile's seconds":        {func(r *Recipe) { r.Compile.Seconds = 0 }, "compile needs"},
		"a cache of nothing":         {func(r *Recipe) { r.Cache.MaxBytes = 0 }, "max_bytes is not positive"},
		"a run's tmp":                {func(r *Recipe) { r.Resources.Tmp = "2 GB" }, "is not a quantity"},
	} {
		r := clone()
		c.mutate(&r)
		if err := r.checkBuild("bayes"); err == nil || !strings.Contains(err.Error(), c.says) || !strings.HasPrefix(err.Error(), "bayes/recipe.json: ") {
			t.Errorf("%s: %v", what, err)
		}
	}
	// the Dockerfile refuses what does not have its shape, whatever the recipe's checks let through
	base := scriptimage.Settings{Managed: true, Hashes: map[string][]string{}, Toolchain: []string{"g++", "make"},
		Sources: []scriptimage.Source{{Name: "cmdstan", Version: "2.40.0", Machine: "x86_64", URL: "https://x.example/c.tar.gz", SHA256: strings.Repeat("a", 64)}},
		CmdStan: &scriptimage.CmdStan{Threads: true, PrecompiledHeader: true, Jobs: 1}}
	if _, err := scriptimage.Dockerfile("python", "python:3.14-slim", nil, base); err != nil {
		t.Fatal(err)
	}
	for what, mutate := range map[string]func(s *scriptimage.Settings){
		"a toolchain outside":     func(s *scriptimage.Settings) { s.Toolchain = []string{"g++", "make", "curl"} },
		"no toolchain":            func(s *scriptimage.Settings) { s.Toolchain = nil },
		"a stage's name":          func(s *scriptimage.Settings) { s.Sources[0].Name = "cmdstan AS x" },
		"a shell in the address":  func(s *scriptimage.Settings) { s.Sources[0].URL = "https://x.example/$(id).tar.gz" },
		"a version with a slash":  func(s *scriptimage.Settings) { s.Sources[0].Version = "2.40/../x" },
		"no source named cmdstan": func(s *scriptimage.Settings) { s.Sources[0].Name = "stan" },
		"sources without the step": func(s *scriptimage.Settings) {
			s.CmdStan = nil
		},
		"-j4": func(s *scriptimage.Settings) { s.CmdStan = &scriptimage.CmdStan{Jobs: 4} },
	} {
		s := base
		s.Sources = append([]scriptimage.Source(nil), base.Sources...)
		mutate(&s)
		if _, err := scriptimage.Dockerfile("python", "python:3.14-slim", nil, s); err == nil {
			t.Errorf("%s: a Dockerfile", what)
		} else if _, ok := err.(scriptimage.ValueError); !ok {
			t.Errorf("%s: %v is not a ValueError", what, err)
		}
	}
}

// The licences of CmdStan and of the libraries it bundles ship with the group as the tarball holds them (docs/specs/bayes-image.md,
// «Licences»): each file of the sha256 the spec cites, and the spec's verbatim quotes are the files' own words.
func TestTheBayesLicencesAreTheTarballsAndTheSpecQuotesThem(t *testing.T) {
	cited := map[string]string{
		"cmdstan-2.40.0/LICENSE":                                             "a48b9091992a0fffa8f42395a6b146e1bf29e97fb4bc5d521b9b38327a712143",
		"cmdstan-2.40.0/stan/LICENSE.md":                                     "aa2930f0a281e429c72f79a4189a188fcf20b0f2bbd4c7d030f1d5060ea649dc",
		"cmdstan-2.40.0/stan/lib/stan_math/LICENSE.md":                       "9c9bf5ffa4941872658b9765bc4effdfdcde44b76d2ccadbfd1bdbf54b6b2af3",
		"cmdstan-2.40.0/stan/lib/stan_math/lib/boost_1.87.0/LICENSE_1_0.txt": "c9bff75738922193e67fa726fa225535870d2aa1059f91452c411736284ad566",
		"cmdstan-2.40.0/stan/lib/stan_math/lib/eigen_5.0.1/COPYING.README":   "db640ff2bd90c6abd6a4d3fbb351e0ee4d555417cf840492054d1cbb2ea85644",
		"cmdstan-2.40.0/stan/lib/stan_math/lib/eigen_5.0.1/LICENSE":          "1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5",
		"cmdstan-2.40.0/stan/lib/stan_math/lib/sundials_6.1.1/LICENSE":       "7650025e01bf24dd1a9e6bc1d259edb9a5ed2e44dc749038f728134921a32487",
		"cmdstan-2.40.0/stan/lib/stan_math/lib/tbb_2020.3/LICENSE":           "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
	}
	for p, want := range cited {
		b, err := files.ReadFile("groups/bayes/licence/" + p)
		if err != nil {
			t.Errorf("%s: %v", p, err)
			continue
		}
		if s := sha256.Sum256(b); hex.EncodeToString(s[:]) != want {
			t.Errorf("%s is not the file of sha256 %s the spec cites", p, want)
		}
	}
	dir, _ := os.Getwd()
	var spec []byte
	for d := dir; ; d = filepath.Dir(d) {
		if b, err := os.ReadFile(filepath.Join(d, "docs", "specs", "bayes-image.md")); err == nil {
			spec = b
			break
		}
		if filepath.Dir(d) == d {
			t.Skip("the spec is not beside the gateway here")
		}
	}
	for p, sha := range cited {
		if !strings.Contains(string(spec), sha) {
			t.Errorf("the spec does not cite %s's sha256", p)
		}
	}
	// the spec's ```text blocks quoted «verbatim»: each is, line for line, the start of one of the files shipped
	words := func(s string) string { return strings.Join(strings.Fields(s), " ") }
	var shipped []string
	for _, p := range append(mapKeys(cited), "stanc3-v2.40.0/LICENSE.txt") {
		b, _ := files.ReadFile("groups/bayes/licence/" + p)
		shipped = append(shipped, words(string(b)))
	}
	block := regexp.MustCompile("(?s)```text\n(.*?)```")
	licences := string(spec[strings.Index(string(spec), "## Licences"):strings.Index(string(spec), "## Code style")])
	quotes := block.FindAllStringSubmatch(licences, -1)
	if len(quotes) < 7 {
		t.Fatalf("%d quotes in the spec's «Licences»", len(quotes))
	}
	for _, q := range quotes {
		found := false
		for _, s := range shipped {
			found = found || strings.HasPrefix(s, words(q[1]))
		}
		if !found {
			t.Errorf("the spec quotes a text no licence file shipped starts with:\n%s", q[1])
		}
	}
}

func mapKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
