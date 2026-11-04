package api

// services/script_builds.py build_loop: on Kubernetes, the images of the scripting
// environments built (a Job running BuildKit in the build namespace), then checked (a Job
// running the new image as a script would), one round every SCRIPTS__POLL_SECONDS. It
// returns at once where the scripting environments are off (or not configured).
//
// One round at a time across the processes: the transaction's advisory lock 7402131, asked
// once, as the Python loop asks it. A version's step is recorded in that transaction; a
// Job's name is fixed by its environment and version, so a process that dies halfway is
// replaced by another that picks up the same Job.

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/jobs"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/kube"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

const (
	sbLock         = 7_402_131 // LOCK
	sbDigestMark   = "TABULARIA_IMAGE_DIGEST="
	sbArchMark     = "TABULARIA_IMAGE_ARCH="
	sbMaxPackages  = 200
	sbScriptUID    = 10001
	sbLabelEnv     = "tabularia.io/environment"
	sbCompilerSize = 3 * (1 << 29) // COMPILER_BYTES: 1.5 GiB
	sbNoPodSeconds = 10            // NO_POD_SECONDS
	sbPullSeconds  = 30            // PULL_FAILURE_SECONDS
	sbCAPath       = "/home/user/registry-ca/ca.crt"
	sbCompletions  = "TABULARIA_COMPLETIONS "
)

var (
	// `uname -m` of the build's node → the kubernetes.io/arch label of the nodes
	sbArches = map[string]string{"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
		"armv7l": "arm", "ppc64le": "ppc64le", "s390x": "s390x"}
	sbRSystemLibraries = []string{
		"build-essential", "gfortran", "cmake", "pkg-config", "libcurl4-openssl-dev", "libssl-dev", "libxml2-dev",
		"zlib1g-dev", "libbz2-dev", "liblzma-dev", "libzstd-dev", "libicu-dev", "libgit2-dev", "libfontconfig1-dev",
		"libfreetype-dev", "libharfbuzz-dev", "libfribidi-dev", "libpng-dev", "libjpeg-dev", "libtiff-dev",
	}
	sbBasePackages = map[string][]string{"python": {"polars", "pyarrow", "marimo"}, "r": {"arrow"}}
	sbURL          = regexp.MustCompile(`^https?://[^\s"'\\]+\n?$`)
	sbRegistry     = regexp.MustCompile(`^[a-z0-9]([a-z0-9.:_/-]*[a-z0-9])?\n?$`)
	sbMemoryParts  = regexp.MustCompile(`^([0-9]+(?:\.[0-9]+)?)([A-Za-z]*)$`)
	sbBytes        = map[string]float64{"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
		"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40}
	sbLockLine = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(==|@)\S+$`)
	sbDigest   = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	// why a push failed, and a compiler the kernel killed for memory
	sbUnknownAuthority = regexp.MustCompile(`x509: certificate signed by unknown authority|certificate is not trusted`)
	sbPlainHTTP        = regexp.MustCompile(`server gave HTTP response to HTTPS client`)
	sbPushDenied       = regexp.MustCompile(`(?s)failed to push.*(401 Unauthorized|403 Forbidden|insufficient_scope|authorization failed)`)
	sbOutOfMemory      = regexp.MustCompile(`Killed signal terminated program|internal compiler error: Killed|OOMKilled`)
)

func init() {
	RegisterJob("script_builds_loop", func(d *Deps) jobs.Job {
		b := &scriptBuilds{db: d.DB, log: d.Log, panel: d.Panel, cfg: d.Config.Jobs.Kube, s: scriptcfg.Load(config.FromOS())}
		return jobs.Job{Order: 10, Loop: b.loop, Ticks: map[string]jobs.Tick{
			// the loop's guard, then one_round: how many versions changed state
			"script_builds": func(ctx context.Context) (any, error) {
				if b.s.DisabledReason() != "" {
					return nil, nil
				}
				return b.oneRound(ctx)
			},
		}}
	})
}

type scriptBuilds struct {
	db    *pgxpool.Pool
	log   *slog.Logger
	panel *settings.Panel
	cfg   config.KubeJobsConfig
	s     *scriptcfg.Settings
}

// sbVersion is a row of script_environment_versions, what a round reads and writes.
type sbVersion struct {
	ID, EnvironmentID, Number int64
	BaseImage, Packages       string
	Status                    string
	JobName, ImageRef, Arch   *string
	Lock, Completions         *string
	BuildLog, Error           *string
	StartedAt, FinishedAt     *time.Time
}

// sbEnvironment is the part of a script_environments row a round reads.
type sbEnvironment struct {
	ID                                            int64
	Language                                      string
	CurrentVersionID                              *int64
	CPURequest, CPULimit, MemoryRequest, MemLimit string
}

func sbStr(s string) *string { return &s }

func (b *scriptBuilds) kubeAPI() scriptcfg.Kubernetes {
	k := b.s.Kubernetes
	k.TimeoutSeconds = b.panel.KubernetesTimeout()
	return k
}

// buildNamespace is script_builds.build_namespace.
func (b *scriptBuilds) buildNamespace() string {
	if b.s.Scripts.BuildNamespace != "" {
		return b.s.Scripts.BuildNamespace
	}
	return b.s.Scripts.Namespace
}

// repository is script_builds.repository: a ValueError (an sbValueError) when the registry
// is no registry path.
func (b *scriptBuilds) repository(environmentID int64) (string, error) {
	registry := strings.TrimRight(pyd.Strip(b.s.Scripts.Registry), "/")
	if !sbRegistry.MatchString(registry) {
		return "", sbValueError("SCRIPTS__REGISTRY is not a registry path (host[:port]/path)")
	}
	return fmt.Sprintf("%s/tabularia-env-%d", registry, environmentID), nil
}

// sbValueError is a ValueError of script_builds: what build_job raises for packages or
// settings that cannot become a Dockerfile.
type sbValueError string

func (e sbValueError) Error() string { return string(e) }

// parsePackages is script_builds.parse_packages: the packages of the text, one per line.
func sbParsePackages(language, text string) ([]string, error) {
	pattern := scRPackage
	if language == "python" {
		pattern = scPyRequirement
	}
	var out []string
	for i, raw := range scriptnodes.SplitLines(text) {
		line, _, _ := strings.Cut(raw, "#")
		line = pyd.Strip(line)
		if line == "" {
			continue
		}
		// a line equal to a heredoc's end mark would close the list in the Dockerfile
		if !pattern.MatchString(line) || strings.HasPrefix(strings.ToUpper(line), "TABULARIA_") {
			return nil, sbValueError(fmt.Sprintf("line %d: «%s» is not a package", i+1, line))
		}
		out = append(out, line)
	}
	if len(out) > sbMaxPackages {
		return nil, sbValueError(fmt.Sprintf("at most %d packages", sbMaxPackages))
	}
	return out, nil
}

// sbCores is script_builds._cores.
func sbCores(cpu string) float64 {
	if strings.HasSuffix(cpu, "m") {
		f, _ := strconv.ParseFloat(cpu[:len(cpu)-1], 64)
		return f / 1000
	}
	f, _ := strconv.ParseFloat(cpu, 64)
	return f
}

// buildJobs is script_builds.build_jobs: how many compilers a build runs at once.
func (b *scriptBuilds) buildJobs() int64 {
	m := sbMemoryParts.FindStringSubmatch(b.panel.BuildMemory())
	size, _ := strconv.ParseFloat(m[1], 64)
	compilers := size * sbBytes[m[2]] / sbCompilerSize
	return max(1, int64(min(sbCores(b.panel.BuildCPU()), compilers)))
}

func sbHeredoc(path string, lines []string, mark string) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "COPY <<'%s' %s\n", mark, path)
	for _, x := range lines {
		sb.WriteString(x + "\n")
	}
	return sb.String() + mark + "\n"
}

// dockerfile is script_builds.dockerfile.
func (b *scriptBuilds) dockerfile(language, baseImage string, packages []string) (string, error) {
	user := fmt.Sprintf("RUN useradd --uid %d --create-home --shell /usr/sbin/nologin tabularia\nUSER %d\nWORKDIR /home/tabularia\n",
		sbScriptUID, sbScriptUID)
	wanted := append(append([]string{}, sbBasePackages[language]...), packages...)
	if language == "python" {
		index := pyd.Strip(b.s.Scripts.PyPIIndexURL)
		if index != "" && !sbURL.MatchString(index) {
			return "", sbValueError("SCRIPTS__PYPI_INDEX_URL is not an http(s) address")
		}
		env := "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1"
		if index != "" {
			env += ` PIP_INDEX_URL="` + index + `"`
		}
		return "FROM " + baseImage + "\n" + env + "\n" +
			sbHeredoc("/opt/tabularia/requirements.txt", wanted, "TABULARIA_PACKAGES") +
			fmt.Sprintf("RUN MAKEFLAGS=-j%d pip install -r /opt/tabularia/requirements.txt\n", b.buildJobs()) + user, nil
	}
	cran := pyd.Strip(b.s.Scripts.CRANURL)
	if !sbURL.MatchString(cran) {
		return "", sbValueError("SCRIPTS__CRAN_URL is not an http(s) address")
	}
	// NOT_CRAN and LIBARROW_MINIMAL: arrow, built from source, gets S3 support. rocker's
	// images point R at a package manager of their own; CRAN replaces it
	return "FROM " + baseImage + "\nENV NOT_CRAN=true LIBARROW_MINIMAL=false CRAN=\"" + cran + "\"\n" +
		"RUN apt-get update && apt-get install -y --no-install-recommends " + strings.Join(sbRSystemLibraries, " ") +
		" && rm -rf /var/lib/apt/lists/*\n" +
		`RUN printf 'options(repos = c(CRAN = "%s"))\n' "$CRAN" >> "$(R RHOME)/etc/Rprofile.site"` + "\n" +
		sbHeredoc("/opt/tabularia/packages.txt", wanted, "TABULARIA_PACKAGES") +
		sbHeredoc("/opt/tabularia/install.R", scriptnodes.SplitLines(sbRInstall), "TABULARIA_INSTALL") +
		fmt.Sprintf("RUN TABULARIA_JOBS=%d Rscript /opt/tabularia/install.R\n", b.buildJobs()) + user, nil
}

// sbShellQuote is shlex.quote.
func sbShellQuote(s string) string {
	if s == "" {
		return "''"
	}
	safe := true
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("_@%+=:,./-", c)) {
			safe = false
			break
		}
	}
	if safe {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

func sbCheckCommand(language string) []any {
	if language == "python" {
		return []any{"python", "-c", sbPyCheck}
	}
	return []any{"Rscript", "-e", sbRCheck}
}

func sbJobName(stage string, v *sbVersion) string {
	return fmt.Sprintf("tabularia-%s-e%d-v%d", stage, v.EnvironmentID, v.Number)
}

func sbLabels(stage string, v *sbVersion) pyjson.Object {
	return pyjson.Object{{Key: "app.kubernetes.io/managed-by", Value: "tabularia"}, {Key: "app.kubernetes.io/component", Value: "scripting"},
		{Key: sbLabelEnv, Value: strconv.FormatInt(v.EnvironmentID, 10)}, {Key: "tabularia.io/version", Value: strconv.FormatInt(v.Number, 10)},
		{Key: "tabularia.io/stage", Value: stage}}
}

// job is script_builds._job.
func (b *scriptBuilds) job(name string, labels pyjson.Object, deadline int64, pod pyjson.Object) pyjson.Object {
	spec := pyjson.Object{{Key: "restartPolicy", Value: "Never"}, {Key: "automountServiceAccountToken", Value: false}}
	spec = append(spec, pod...)
	return pyjson.Object{
		{Key: "apiVersion", Value: "batch/v1"}, {Key: "kind", Value: "Job"},
		{Key: "metadata", Value: pyjson.Object{{Key: "name", Value: name}, {Key: "labels", Value: labels}}},
		{Key: "spec", Value: pyjson.Object{
			{Key: "backoffLimit", Value: int64(0)}, {Key: "activeDeadlineSeconds", Value: deadline},
			{Key: "ttlSecondsAfterFinished", Value: b.panel.ScriptsJobTTL()},
			{Key: "template", Value: pyjson.Object{{Key: "metadata", Value: pyjson.Object{{Key: "labels", Value: labels}}}, {Key: "spec", Value: spec}}},
		}},
	}
}

// buildJob is script_builds.build_job.
func (b *scriptBuilds) buildJob(v *sbVersion, language string) (pyjson.Object, error) {
	repo, err := b.repository(v.EnvironmentID)
	if err != nil {
		return nil, err
	}
	tag := fmt.Sprintf("%s:v%d", repo, v.Number)
	output := "type=image,name=" + tag + ",push=true"
	if b.cfg.RegistryInsecure {
		output += ",registry.insecure=true"
	}
	trust := ""
	if b.cfg.RegistryCAConfigMap != "" {
		// the registry's certificate is signed by an authority of the company's: BuildKit
		// verifies the push with it
		host, _, _ := strings.Cut(pyd.Strip(b.s.Scripts.Registry), "/")
		trust = "mkdir -p /home/user/.config/buildkit\n" +
			`printf '[registry."%s"]\n  ca = ["` + sbCAPath + `"]\n' ` + sbShellQuote(host) + " " +
			"> /home/user/.config/buildkit/buildkitd.toml\n" +
			`export BUILDKITD_FLAGS="$BUILDKITD_FLAGS --config /home/user/.config/buildkit/buildkitd.toml"` + "\n"
	}
	script := "set -e\n" + trust +
		"mkdir -p /home/user/ctx\n" +
		`printf "%s" "$TABULARIA_DOCKERFILE" > /home/user/ctx/Dockerfile` + "\n" +
		"buildctl-daemonless.sh build --frontend dockerfile.v0 --local context=/home/user/ctx " +
		"--local dockerfile=/home/user/ctx --output " + sbShellQuote(output) + " --metadata-file /home/user/meta.json\n" +
		// the manifest's digest: meta.json also holds the config's, which is no image
		`digest=$(grep -o '"containerimage.digest": *"sha256:[0-9a-f]\{64\}"' /home/user/meta.json ` +
		`| grep -o 'sha256:[0-9a-f]\{64\}')` + "\n" +
		`test -n "$digest"` + "\n" +
		`echo; echo "` + sbDigestMark + `$digest"` + "\n" +
		// BuildKit builds for the platform of the node it runs on
		`echo "` + sbArchMark + `$(uname -m)"` + "\n"
	mounts := []any{pyjson.Object{{Key: "name", Value: "buildkit"}, {Key: "mountPath", Value: "/home/user/.local/share/buildkit"}}}
	volumes := []any{pyjson.Object{{Key: "name", Value: "buildkit"}, {Key: "emptyDir", Value: pyjson.Object{}}}}
	if b.cfg.RegistryCAConfigMap != "" {
		mounts = append(mounts, pyjson.Object{{Key: "name", Value: "registry-ca"}, {Key: "mountPath", Value: sbCAPath[:strings.LastIndex(sbCAPath, "/")]},
			{Key: "readOnly", Value: true}})
		volumes = append(volumes, pyjson.Object{{Key: "name", Value: "registry-ca"},
			{Key: "configMap", Value: pyjson.Object{{Key: "name", Value: b.cfg.RegistryCAConfigMap}}}})
	}
	if b.cfg.RegistryPushSecret != "" {
		mounts = append(mounts, pyjson.Object{{Key: "name", Value: "registry"}, {Key: "mountPath", Value: "/home/user/.docker"}, {Key: "readOnly", Value: true}})
		volumes = append(volumes, pyjson.Object{{Key: "name", Value: "registry"}, {Key: "secret", Value: pyjson.Object{
			{Key: "secretName", Value: b.cfg.RegistryPushSecret},
			{Key: "items", Value: []any{pyjson.Object{{Key: "key", Value: ".dockerconfigjson"}, {Key: "path", Value: "config.json"}}}}}}})
	}
	packages, err := sbParsePackages(language, v.Packages)
	if err != nil {
		return nil, err
	}
	dockerfile, err := b.dockerfile(language, v.BaseImage, packages)
	if err != nil {
		return nil, err
	}
	cpu, memory := b.panel.BuildCPU(), b.panel.BuildMemory()
	request := cpu
	if sbCores(cpu) >= 1 {
		request = "1"
	}
	container := pyjson.Object{
		{Key: "name", Value: "build"}, {Key: "image", Value: b.cfg.BuilderImage}, {Key: "command", Value: []any{"sh", "-c", script}},
		{Key: "env", Value: []any{
			pyjson.Object{{Key: "name", Value: "BUILDKITD_FLAGS"}, {Key: "value", Value: "--oci-worker-no-process-sandbox"}},
			pyjson.Object{{Key: "name", Value: "TABULARIA_DOCKERFILE"}, {Key: "value", Value: dockerfile}}}},
		// rootless BuildKit: its own user namespace, which the default profiles forbid
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsUser", Value: int64(1000)}, {Key: "runAsGroup", Value: int64(1000)},
			{Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "Unconfined"}}},
			{Key: "appArmorProfile", Value: pyjson.Object{{Key: "type", Value: "Unconfined"}}}}},
		{Key: "volumeMounts", Value: mounts},
		{Key: "resources", Value: pyjson.Object{
			{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: request}, {Key: "memory", Value: memory}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: cpu}, {Key: "memory", Value: memory}}}}},
	}
	return b.job(sbJobName("build", v), sbLabels("build", v), b.panel.BuildTimeout(),
		pyjson.Object{{Key: "containers", Value: []any{container}}, {Key: "volumes", Value: volumes}}), nil
}

// nodeSelector is script_builds.node_selector: the nodes of the architecture the image
// was built for.
func sbNodeSelector(v *sbVersion) pyjson.Object {
	if v.Arch != nil && *v.Arch != "" {
		return pyjson.Object{{Key: "nodeSelector", Value: pyjson.Object{{Key: "kubernetes.io/arch", Value: *v.Arch}}}}
	}
	return nil
}

// checkJob is script_builds.check_job.
func (b *scriptBuilds) checkJob(v *sbVersion, env *sbEnvironment) pyjson.Object {
	var image any
	if v.ImageRef != nil {
		image = *v.ImageRef
	}
	container := pyjson.Object{
		{Key: "name", Value: "check"}, {Key: "image", Value: image}, {Key: "command", Value: sbCheckCommand(env.Language)},
		{Key: "env", Value: []any{pyjson.Object{{Key: "name", Value: "HOME"}, {Key: "value", Value: "/tmp"}},
			pyjson.Object{{Key: "name", Value: "TMPDIR"}, {Key: "value", Value: "/tmp"}}}},
		{Key: "resources", Value: pyjson.Object{
			{Key: "requests", Value: pyjson.Object{{Key: "cpu", Value: env.CPURequest}, {Key: "memory", Value: env.MemoryRequest}}},
			{Key: "limits", Value: pyjson.Object{{Key: "cpu", Value: env.CPULimit}, {Key: "memory", Value: env.MemLimit}}}}},
		{Key: "securityContext", Value: pyjson.Object{{Key: "allowPrivilegeEscalation", Value: false}, {Key: "readOnlyRootFilesystem", Value: true},
			{Key: "capabilities", Value: pyjson.Object{{Key: "drop", Value: []any{"ALL"}}}}}},
		{Key: "volumeMounts", Value: []any{pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "mountPath", Value: "/tmp"}}}},
	}
	pod := pyjson.Object{
		{Key: "securityContext", Value: pyjson.Object{{Key: "runAsNonRoot", Value: true}, {Key: "runAsUser", Value: int64(sbScriptUID)},
			{Key: "runAsGroup", Value: int64(sbScriptUID)}, {Key: "seccompProfile", Value: pyjson.Object{{Key: "type", Value: "RuntimeDefault"}}}}},
		{Key: "containers", Value: []any{container}},
		{Key: "volumes", Value: []any{pyjson.Object{{Key: "name", Value: "tmp"}, {Key: "emptyDir", Value: pyjson.Object{}}}}},
	}
	pod = append(pod, sbNodeSelector(v)...)
	if b.cfg.RegistryPullSecret != "" {
		pod = append(pod, pyjson.KV{Key: "imagePullSecrets", Value: []any{pyjson.Object{{Key: "name", Value: b.cfg.RegistryPullSecret}}}})
	}
	return b.job(sbJobName("check", v), sbLabels("check", v), b.panel.CheckTimeout(), pod)
}

// sbSplitCheckOutput is script_builds.split_check_output: the lock (one package per line)
// and the completions JSON.
func sbSplitCheckOutput(log string) (string, *string) {
	var lock []string
	var completions *string
	for _, line := range scriptnodes.SplitLines(log) {
		if strings.HasPrefix(line, sbCompletions) {
			completions = nil
			if parsed, err := pyjson.Loads(line[len(sbCompletions):]); err == nil {
				if _, isDict := parsed.(pyjson.Object); isDict {
					if s, err := pyjson.Dumps(parsed, pyjson.Options{EnsureASCII: true}); err == nil {
						completions = &s
					}
				}
			}
		} else if stripped := pystr.Strip(line); sbLockLine.MatchString(stripped) { // not what an import printed
			lock = append(lock, stripped)
		}
	}
	return strings.Join(lock, "\n"), completions
}

// sbDigestFromLog is script_builds.digest_from_log.
func sbDigestFromLog(log string) string {
	lines := scriptnodes.SplitLines(log)
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.HasPrefix(lines[i], sbDigestMark) {
			d := pystr.Strip(lines[i][len(sbDigestMark):])
			if sbDigest.MatchString(d) {
				return d
			}
			return ""
		}
	}
	return ""
}

// sbArchFromLog is script_builds.arch_from_log (nil: none known).
func sbArchFromLog(log string) *string {
	lines := scriptnodes.SplitLines(log)
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.HasPrefix(lines[i], sbArchMark) {
			if a, ok := sbArches[pystr.Strip(lines[i][len(sbArchMark):])]; ok {
				return &a
			}
			return nil
		}
	}
	return nil
}

// sbStripDigest is script_builds._strip_digest.
func sbStripDigest(log string) string {
	var keep []string
	for _, x := range scriptnodes.SplitLines(log) {
		if !strings.HasPrefix(x, sbDigestMark) && !strings.HasPrefix(x, sbArchMark) {
			keep = append(keep, x)
		}
	}
	return strings.TrimRightFunc(strings.Join(keep, "\n"), pystr.IsSpace)
}

// sbAgeSeconds is script_builds._age_seconds: how long ago the Job was created (0 when it
// does not say).
func sbAgeSeconds(job any) float64 {
	created := kube.Dict(kube.Dict(job).Value("metadata")).Value("creationTimestamp")
	if !pyjson.Truthy(created) {
		return 0
	}
	raw := strings.ReplaceAll(pyjson.Str(created), "Z", "+00:00")
	for _, layout := range []string{"2006-01-02T15:04:05.999999999-07:00", "2006-01-02 15:04:05.999999999-07:00"} {
		if t, err := time.Parse(layout, raw); err == nil {
			return jobs.Now().Sub(t).Seconds()
		}
	}
	return 0
}

// fail is script_builds._fail.
func (b *scriptBuilds) fail(v *sbVersion, message string, log *string) {
	now := jobs.NaiveNow()
	v.Status, v.Error, v.FinishedAt, v.JobName = "failed", &message, &now, nil
	if log != nil {
		v.BuildLog = log
	}
	b.log.Info(fmt.Sprintf("scripting: environment %d version %d failed: %s", v.EnvironmentID, v.Number, message))
}

func sbEnvironmentOf(ctx context.Context, tx pgx.Tx, id int64) (*sbEnvironment, error) {
	var e sbEnvironment
	err := tx.QueryRow(ctx, `SELECT id, language, current_version_id, cpu_request, cpu_limit, memory_request, memory_limit
		FROM script_environments WHERE id = $1`, id).Scan(&e.ID, &e.Language, &e.CurrentVersionID, &e.CPURequest, &e.CPULimit,
		&e.MemoryRequest, &e.MemLimit)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &e, err
}

// advance is script_builds.advance: one step of a version, if its Job has moved; the
// caller writes the version back.
func (b *scriptBuilds) advance(ctx context.Context, tx pgx.Tx, v *sbVersion) error {
	k := b.kubeAPI()
	env, err := sbEnvironmentOf(ctx, tx, v.EnvironmentID)
	if err != nil {
		return err
	}
	if env == nil {
		b.fail(v, "The environment no longer exists", nil)
		return nil
	}
	if v.Status == "queued" {
		manifest, err := b.buildJob(v, env.Language)
		var bad sbValueError
		if errors.As(err, &bad) { // packages or settings that cannot become a Dockerfile
			b.fail(v, bad.Error(), nil)
			return nil
		} else if err != nil {
			return err
		}
		if _, err := kube.CreateJob(ctx, k, b.buildNamespace(), manifest); err != nil {
			return err
		}
		now := jobs.NaiveNow()
		v.Status, v.JobName, v.StartedAt = "building", sbStr(sbJobName("build", v)), &now
		return nil
	}
	building := v.Status == "building"
	stageNS, stageJob, stage := b.s.Scripts.Namespace, "check", "check of the new image"
	if building {
		stageNS, stageJob, stage = b.buildNamespace(), "build", "build"
	}
	name := sbJobName(stageJob, v)
	if v.JobName != nil && *v.JobName != "" {
		name = *v.JobName
	}
	job, err := kube.GetJob(ctx, k, stageNS, name)
	if err != nil {
		return err
	}
	if job == nil {
		b.fail(v, fmt.Sprintf("The Job %s of the %s is no longer in the cluster (deleted, or removed once finished "+
			"while no orchestrator was running): build the environment again", name, stage), nil)
		return nil
	}
	outcome := kube.JobOutcome(job)
	if outcome == "" {
		// a Pod the cluster refuses, or an image the nodes cannot download, would otherwise
		// wait for the whole timeout and then say nothing of it
		why := ""
		if !kube.HasPod(job) && sbAgeSeconds(job) >= sbNoPodSeconds {
			if why, err = kube.Refused(ctx, k, stageNS, name); err != nil {
				return err
			}
		}
		if why == "" {
			if why, err = kube.PullFailure(ctx, k, stageNS, name); err != nil {
				return err
			}
			if why == "" || sbAgeSeconds(job) < sbPullSeconds {
				why = ""
			}
		}
		if why != "" {
			if err := kube.DeleteJob(ctx, k, stageNS, name); err != nil {
				return err
			}
			b.fail(v, fmt.Sprintf("The %s cannot start: %s", stage, why), nil)
		}
		return nil
	}
	// the check prints every package and what each exports: room for all of it
	maxBytes := 8 << 20
	if building {
		maxBytes = int(b.panel.ScriptsLogMaxBytes())
	}
	log, exitCode, ended, err := kube.JobResult(ctx, k, stageNS, name, maxBytes)
	var unavailable *kube.LogUnavailable
	if errors.As(err, &unavailable) {
		log, exitCode, ended = fmt.Sprintf("(the cluster did not give the log: %s)", unavailable.Kube.Message), unavailable.ExitCode, unavailable.Reason
	} else if err != nil {
		return err
	}
	digest := ""
	if building && outcome != "failed" {
		digest = sbDigestFromLog(log)
	}
	if digest != "" {
		repo, err := b.repository(v.EnvironmentID)
		if err != nil {
			return err
		}
		v.ImageRef, v.BuildLog = sbStr(repo+"@"+digest), sbStr(sbStripDigest(log))
		v.Arch = sbArchFromLog(log)
		manifest := b.checkJob(v, env)
		// the check's Job first, then the build's goes: when the API server fails in
		// between, the build's Job is still there and the next round reads it again
		if _, err := kube.CreateJob(ctx, k, b.s.Scripts.Namespace, manifest); err != nil {
			return err
		}
		if err := kube.DeleteJob(ctx, k, stageNS, name); err != nil {
			return err
		}
		v.Status, v.JobName = "checking", sbStr(sbJobName("check", v))
		return nil
	}
	if err := kube.DeleteJob(ctx, k, stageNS, name); err != nil { // what it had to say is in `log`
		return err
	}
	if building {
		if outcome == "failed" {
			reason := kube.JobFailure(job)
			switch {
			case sbUnknownAuthority.MatchString(log):
				b.fail(v, "The image was built but not pushed: the registry's certificate is signed by an authority "+
					"the build does not trust. Give the chart your authority's certificate "+
					"(scripts.registry.caCert), or for a test registry set scripts.registry.insecure", &log)
			case sbPlainHTTP.MatchString(log):
				b.fail(v, "The image was built but not pushed: the registry answers in plain HTTP. Set "+
					"scripts.registry.insecure (the nodes must allow that registry over HTTP too)", &log)
			case sbPushDenied.MatchString(log):
				b.fail(v, "The image was built but not pushed: the registry refused the credentials. Check the "+
					"Secret of scripts.registry.pushSecret and its rights on the repository", &log)
			case sbOutOfMemory.MatchString(log) || ended == "OOMKilled" || isExit137(exitCode):
				// the compiler's own line, or (cgroup v2) only the exit code
				b.fail(v, fmt.Sprintf("The image build ran out of memory: a compiler was killed. Raise SCRIPTS__BUILD_MEMORY "+
					"(now %s), or lower SCRIPTS__BUILD_CPU to run fewer compilers at once", b.panel.BuildMemory()), &log)
			case strings.Contains(reason, "DeadlineExceeded"):
				b.fail(v, fmt.Sprintf("The image build took longer than SCRIPTS__BUILD_TIMEOUT_SECONDS (%d s): "+
					"raise it, or give the build more CPU (SCRIPTS__BUILD_CPU)", b.panel.BuildTimeout()), &log)
			default:
				why := ""
				if reason != "" {
					why = " (" + reason + ")"
				}
				b.fail(v, "The image build failed"+why+": the end of the build log says why", &log)
			}
			return nil
		}
		b.fail(v, "The build ended without the image's digest", &log)
		return nil
	}
	// checking
	if outcome == "failed" {
		reason := kube.JobFailure(job)
		if reason == "" {
			reason = "it ended with an error"
		}
		b.fail(v, strings.TrimRightFunc(fmt.Sprintf("The image was built but does not start as a script would: %s\n%s", reason, log),
			pystr.IsSpace), nil)
		return nil
	}
	lock, completions := sbSplitCheckOutput(log)
	now := jobs.NaiveNow()
	v.Status, v.Lock, v.Completions, v.FinishedAt, v.JobName, v.Error = "ready", &lock, completions, &now, nil, nil
	var currentNumber *int64
	if env.CurrentVersionID != nil && *env.CurrentVersionID != 0 {
		var n int64
		err := tx.QueryRow(ctx, `SELECT number FROM script_environment_versions WHERE id = $1`, *env.CurrentVersionID).Scan(&n)
		if err == nil {
			currentNumber = &n
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
	}
	if currentNumber == nil || *currentNumber < v.Number {
		if _, err := tx.Exec(ctx, `UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, v.ID, env.ID); err != nil {
			return err
		}
	}
	ref := ""
	if v.ImageRef != nil {
		ref = *v.ImageRef
	}
	b.log.Info(fmt.Sprintf("scripting: environment %d version %d ready (%s)", env.ID, v.Number, ref))
	return nil
}

// isExit137 is `exit_code == 137`.
func isExit137(v any) bool {
	switch x := v.(type) {
	case int64:
		return x == 137
	case float64:
		return x == 137
	}
	return false
}

// oneRound is script_builds.one_round: every version in progress moved forward; how many
// changed state.
func (b *scriptBuilds) oneRound(ctx context.Context) (int64, error) {
	tx, err := b.db.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx)
	var got bool
	if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1)`, int64(sbLock)).Scan(&got); err != nil {
		return 0, err
	}
	if !got {
		return 0, nil
	}
	rows, err := tx.Query(ctx, `SELECT id, environment_id, number, base_image, packages, status, job_name, image_ref, arch, lock,
		completions, build_log, error, started_at, finished_at FROM script_environment_versions
		WHERE status IN ('queued', 'building', 'checking') ORDER BY id`)
	if err != nil {
		return 0, err
	}
	var versions []*sbVersion
	for rows.Next() {
		var v sbVersion
		if err := rows.Scan(&v.ID, &v.EnvironmentID, &v.Number, &v.BaseImage, &v.Packages, &v.Status, &v.JobName, &v.ImageRef, &v.Arch,
			&v.Lock, &v.Completions, &v.BuildLog, &v.Error, &v.StartedAt, &v.FinishedAt); err != nil {
			rows.Close()
			return 0, err
		}
		versions = append(versions, &v)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	var changed int64
	for _, v := range versions {
		before := v.Status
		err := b.advance(ctx, tx, v)
		var ke *kube.Error
		switch {
		case errors.As(err, &ke) && (ke.Status == 0 || ke.Status == 429 || ke.Status >= 500):
			b.log.Warn(fmt.Sprintf("scripting: version %d of environment %d waits (%s)", v.Number, v.EnvironmentID, ke.Message))
			// the API server is restarting: at the next round (what changed is kept)
		case errors.As(err, &ke):
			b.fail(v, ke.Message, nil)
		case err != nil:
			return 0, err
		}
		if _, err := tx.Exec(ctx, `UPDATE script_environment_versions SET status = $2, job_name = $3, image_ref = $4, arch = $5,
			lock = $6, completions = $7, build_log = $8, error = $9, started_at = $10, finished_at = $11 WHERE id = $1`,
			v.ID, v.Status, v.JobName, v.ImageRef, v.Arch, v.Lock, v.Completions, v.BuildLog, v.Error, v.StartedAt, v.FinishedAt); err != nil {
			return 0, err
		}
		if errors.As(err, &ke) && (ke.Status == 0 || ke.Status == 429 || ke.Status >= 500) {
			continue
		}
		if v.Status != before {
			changed++
		}
	}
	return changed, tx.Commit(ctx) // releases the lock
}

// loop is build_loop.
func (b *scriptBuilds) loop(ctx context.Context) {
	if b.s.DisabledReason() != "" {
		return
	}
	for ctx.Err() == nil {
		every := b.panel.ScriptsPollSeconds() // changes from the panel at the next round
		if _, err := b.oneRound(ctx); err != nil && !errors.Is(err, context.Canceled) {
			// database or cluster down: at the next round
			b.log.Warn(fmt.Sprintf("scripting: builds not checked (%v), again in %ss", err, pyjson.FloatRepr(every)))
		}
		if !jobs.Sleep(ctx, jobs.Seconds(every)) {
			break
		}
	}
}

// sbRInstall is script_builds._R_INSTALL.
const sbRInstall = "lines <- trimws(readLines(\"/opt/tabularia/packages.txt\"))\nlines <- lines[nzchar(lines)]\nname <- sub(\"@.*$\", \"\", lines)\npinned <- grepl(\"@\", lines, fixed = TRUE)\n# CRAN has sources only: the packages compile. At most TABULARIA_JOBS compilers at\n# once, whatever the cores of the node (a C++ compiler of arrow takes over 1 GB): a\n# package alone gets them all, packages side by side one each\njobs <- max(1L, suppressWarnings(as.integer(Sys.getenv(\"TABULARIA_JOBS\", \"1\"))), na.rm = TRUE)\nSys.setenv(MAKEFLAGS = paste0(\"-j\", jobs))\n# the pinned ones first: installing the others afterwards does not replace them\nif (any(pinned)) {\n  install.packages(\"remotes\", Ncpus = 1L)\n  for (i in which(pinned)) {\n    remotes::install_version(name[i], version = sub(\"^[^@]*@\", \"\", lines[i]), upgrade = \"never\", Ncpus = 1L)\n  }\n}\n# arrow, the heaviest, alone\nif (!requireNamespace(\"arrow\", quietly = TRUE)) install.packages(\"arrow\", Ncpus = 1L)\nrest <- setdiff(unique(name[!pinned]), \"arrow\")\nSys.setenv(MAKEFLAGS = \"-j1\")\nif (length(rest)) install.packages(rest, Ncpus = jobs)\n# install.packages only warns about a package it could not install\nmissing <- name[!vapply(name, requireNamespace, logical(1), quietly = TRUE)]\nif (length(missing)) stop(\"not installed: \", paste(unique(missing), collapse = \", \"))\n"

// sbPyCheck is script_builds._PY_CHECK.
const sbPyCheck = "import importlib, importlib.metadata as m, json, signal\nimport polars, pyarrow\nnames = {f\"{d.metadata['Name']}=={d.version}\" for d in m.distributions() if d.metadata['Name']}\nprint('\\n'.join(sorted(names, key=str.lower)))\ndef too_long(*_):\n    raise TimeoutError\nsignal.signal(signal.SIGALRM, too_long)\napi = {}\nfor mod in sorted(x for x in m.packages_distributions() if x.isidentifier() and not x.startswith('_')):\n    signal.alarm(15)  # a module that takes longer to import is left out of the completions\n    api[mod] = []  # listed even when it cannot be imported here: the editor knows it is installed\n    try:\n        api[mod] = sorted(n for n in dir(importlib.import_module(mod)) if not n.startswith('_'))[:400]\n    except BaseException:\n        pass\n    finally:\n        signal.alarm(0)\nprint('TABULARIA_COMPLETIONS ' + json.dumps(api))\n"

// sbRCheck is script_builds._R_CHECK.
const sbRCheck = "library(arrow)\nif (!arrow::arrow_with_s3()) stop('the arrow package of this image has no S3 support')\nip <- installed.packages()[, c('Package', 'Version'), drop = FALSE]\nwriteLines(sort(unique(paste0(ip[, 1], '@', ip[, 2]))))\nq <- function(x) paste0('\"', gsub('\"', '\\\\\"', gsub('\\\\', '\\\\\\\\', x, fixed = TRUE), fixed = TRUE), '\"')\nexports <- function(p) tryCatch(head(sort(getNamespaceExports(p)), 400), error = function(e) character())\npk <- sort(unique(ip[, 1]))\nparts <- vapply(pk, function(p) paste0(q(p), ':[', paste(q(exports(p)), collapse = ','), ']'), character(1))\ncat('TABULARIA_COMPLETIONS ', '{', paste(parts, collapse = ','), '}', '\\n', sep = '')\n"
