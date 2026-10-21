"""The images of the scripting environments: built once, then checked.

Saving an environment's language version or packages adds a version in state
`queued` (models/scripting.py). The orchestrators move it forward, one round every
`SCRIPTS__POLL_SECONDS`, one process at a time (an advisory lock):

1. `queued` → `building`: a Kubernetes Job runs BuildKit, rootless, in the build
   namespace. It writes the Dockerfile this module generates, builds it and pushes
   `<registry>/tabularia-env-<id>:v<n>`; the last line of its log is the digest.
2. `building` → `checking`: a second Job runs the new image, by digest, in the
   scripts namespace, with the restrictions a script will have (non-root, read-only
   root filesystem). It proves the image starts and that the packages a script
   needs to read its input load, and prints every installed package: the version's
   lock.
3. `checking` → `ready`, and it becomes the environment's current version unless a
   newer one already is. Any failure → `failed`, with the end of the log; the
   current version stays as it was.

Every step is recorded before the next round, and a Job's name is fixed by its
environment and version, so an orchestrator that dies halfway is replaced by
another that picks up the same Job.

The packages are checked line by line before they reach a Dockerfile: a name and a
version, nothing a shell or Dockerfile would read as a command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from typing import Optional

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import ScriptEnvironment, ScriptEnvironmentVersion
from app.models.scripting import IN_PROGRESS, now_utc
from app.services import kube_jobs
from app.services.kube_scaling import KubeError

logger = logging.getLogger(__name__)

# the advisory lock of the builds (pg_advisory_xact_lock), apart from the others
LUCCHETTO = 7_402_131
DIGEST_MARK = "TABULARIA_IMAGE_DIGEST="
MAX_PACKAGES = 200
# CRAN publishes sources only: what the usual packages need to compile (arrow, curl,
# openssl, xml2, the graphics stack, git2r…), installed in every R image
R_SYSTEM_LIBRARIES = (
    "build-essential", "gfortran", "cmake", "pkg-config", "libcurl4-openssl-dev", "libssl-dev", "libxml2-dev",
    "zlib1g-dev", "libbz2-dev", "liblzma-dev", "libzstd-dev", "libicu-dev", "libgit2-dev", "libfontconfig1-dev",
    "libfreetype-dev", "libharfbuzz-dev", "libfribidi-dev", "libpng-dev", "libjpeg-dev", "libtiff-dev",
)
# always installed: what a script needs to read its input from S3 and write its output
BASE_PACKAGES = {"python": ["polars", "pyarrow"], "r": ["arrow"]}
# the user the scripts run as, in every image
SCRIPT_UID = 10001
LABEL_ENV = "tabularia.io/environment"

_PY_SPEC = r"(==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+"
_PY_REQUIREMENT = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?\s*({_PY_SPEC}(\s*,\s*{_PY_SPEC})*)?$")
_R_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*(@[0-9][0-9.-]*)?$")
_URL = re.compile(r"^https?://[^\s\"'\\]+$")
_REGISTRY = re.compile(r"^[a-z0-9]([a-z0-9.:_/-]*[a-z0-9])?$")


class PackageError(ValueError):
    """A line of packages that cannot be installed as written; `line` is 1-based."""

    def __init__(self, line: int, text: str):
        super().__init__(f"line {line}: «{text}» is not a package")
        self.line = line
        self.text = text


def parse_packages(language: str, text: str) -> list[str]:
    """The packages of the text, one per line; blank lines and `#` comments skipped."""
    pattern = _PY_REQUIREMENT if language == "python" else _R_PACKAGE
    out: list[str] = []
    for n, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # a line equal to a heredoc's end mark would close the list in the Dockerfile
        if not pattern.match(line) or line.upper().startswith("TABULARIA_"):
            raise PackageError(n, line)
        out.append(line)
    if len(out) > MAX_PACKAGES:
        raise ValueError(f"at most {MAX_PACKAGES} packages")
    return out


def disabled_reason() -> Optional[str]:
    """Why the scripting environments cannot be built here, as a code the panel
    translates; None when they can."""
    s = get_settings().scripts
    if not s.enabled:
        return "kubernetes"
    if not s.namespace or not s.registry:
        return "configuration"
    return None


def build_namespace() -> str:
    s = get_settings().scripts
    return s.build_namespace or s.namespace


def repository(environment_id: int) -> str:
    registry = get_settings().scripts.registry.strip().rstrip("/")
    if not _REGISTRY.match(registry):
        raise ValueError("SCRIPTS__REGISTRY is not a registry path (host[:port]/path)")
    return f"{registry}/tabularia-env-{environment_id}"


def _heredoc(path: str, lines: list[str], mark: str) -> str:
    return f"COPY <<'{mark}' {path}\n" + "".join(f"{x}\n" for x in lines) + f"{mark}\n"


_R_INSTALL = """\
lines <- trimws(readLines("/opt/tabularia/packages.txt"))
lines <- lines[nzchar(lines)]
name <- sub("@.*$", "", lines)
pinned <- grepl("@", lines, fixed = TRUE)
# CRAN has sources only: the packages compile. At most TABULARIA_JOBS compilers at
# once, whatever the cores of the node (a C++ compiler of arrow takes over 1 GB): a
# package alone gets them all, packages side by side one each
jobs <- max(1L, suppressWarnings(as.integer(Sys.getenv("TABULARIA_JOBS", "1"))), na.rm = TRUE)
Sys.setenv(MAKEFLAGS = paste0("-j", jobs))
# the pinned ones first: installing the others afterwards does not replace them
if (any(pinned)) {
  install.packages("remotes", Ncpus = 1L)
  for (i in which(pinned)) {
    remotes::install_version(name[i], version = sub("^[^@]*@", "", lines[i]), upgrade = "never", Ncpus = 1L)
  }
}
# arrow, the heaviest, alone
if (!requireNamespace("arrow", quietly = TRUE)) install.packages("arrow", Ncpus = 1L)
rest <- setdiff(unique(name[!pinned]), "arrow")
Sys.setenv(MAKEFLAGS = "-j1")
if (length(rest)) install.packages(rest, Ncpus = jobs)
# install.packages only warns about a package it could not install
missing <- name[!vapply(name, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) stop("not installed: ", paste(unique(missing), collapse = ", "))
"""

_BYTES = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
          "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
# what one C++ compiler of a heavy package (arrow's bindings) may take
COMPILER_BYTES = 3 * 2**29  # 1.5 GiB


def build_jobs() -> int:
    """How many compilers a build runs at once: one per 1.5 GiB of SCRIPTS__BUILD_MEMORY,
    at most one per core of SCRIPTS__BUILD_CPU. The node's cores say nothing: a Pod sees
    them all, its limits are what it may use."""
    s = get_settings().scripts
    memory = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Za-z]*)", s.build_memory)
    compilers = float(memory[1]) * _BYTES[memory[2]] / COMPILER_BYTES
    return max(1, int(min(_cores(s.build_cpu), compilers)))


def dockerfile(language: str, base_image: str, packages: list[str]) -> str:
    s = get_settings().scripts
    user = (f"RUN useradd --uid {SCRIPT_UID} --create-home --shell /usr/sbin/nologin tabularia\n"
            f"USER {SCRIPT_UID}\nWORKDIR /home/tabularia\n")
    wanted = BASE_PACKAGES[language] + packages
    if language == "python":
        index = s.pypi_index_url.strip()
        if index and not _URL.match(index):
            raise ValueError("SCRIPTS__PYPI_INDEX_URL is not an http(s) address")
        env = "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1"
        if index:
            env += f' PIP_INDEX_URL="{index}"'
        return (f"FROM {base_image}\n{env}\n"
                + _heredoc("/opt/tabularia/requirements.txt", wanted, "TABULARIA_PACKAGES")
                + f"RUN MAKEFLAGS=-j{build_jobs()} pip install -r /opt/tabularia/requirements.txt\n" + user)
    cran = s.cran_url.strip()
    if not _URL.match(cran):
        raise ValueError("SCRIPTS__CRAN_URL is not an http(s) address")
    # NOT_CRAN and LIBARROW_MINIMAL: arrow, built from source, gets S3 support.
    # rocker's images point R at a package manager of their own; CRAN replaces it, for
    # the build and for the scripts
    return (f'FROM {base_image}\nENV NOT_CRAN=true LIBARROW_MINIMAL=false CRAN="{cran}"\n'
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(R_SYSTEM_LIBRARIES) + " && rm -rf /var/lib/apt/lists/*\n"
            + 'RUN printf \'options(repos = c(CRAN = "%s"))\\n\' "$CRAN" >> "$(R RHOME)/etc/Rprofile.site"\n'
            + _heredoc("/opt/tabularia/packages.txt", wanted, "TABULARIA_PACKAGES")
            + _heredoc("/opt/tabularia/install.R", _R_INSTALL.splitlines(), "TABULARIA_INSTALL")
            + f"RUN TABULARIA_JOBS={build_jobs()} Rscript /opt/tabularia/install.R\n" + user)


# what the check prints: every installed package, one per line (the version's lock),
# then one line with what each module exports (the code editor's completions)
COMPLETIONS_MARK = "TABULARIA_COMPLETIONS "
MAX_NAMES_PER_MODULE = 400
_PY_CHECK = f"""\
import importlib, importlib.metadata as m, json, signal
import polars, pyarrow
names = {{f"{{d.metadata['Name']}}=={{d.version}}" for d in m.distributions() if d.metadata['Name']}}
print('\\n'.join(sorted(names, key=str.lower)))
def too_long(*_):
    raise TimeoutError
signal.signal(signal.SIGALRM, too_long)
api = {{}}
for mod in sorted(x for x in m.packages_distributions() if x.isidentifier() and not x.startswith('_')):
    signal.alarm(15)  # a module that takes longer to import is left out of the completions
    api[mod] = []  # listed even when it cannot be imported here: the editor knows it is installed
    try:
        api[mod] = sorted(n for n in dir(importlib.import_module(mod)) if not n.startswith('_'))[:{MAX_NAMES_PER_MODULE}]
    except BaseException:
        pass
    finally:
        signal.alarm(0)
print({COMPLETIONS_MARK!r} + json.dumps(api))
"""
_R_CHECK = f"""\
library(arrow)
if (!arrow::arrow_with_s3()) stop('the arrow package of this image has no S3 support')
ip <- installed.packages()[, c('Package', 'Version'), drop = FALSE]
writeLines(sort(unique(paste0(ip[, 1], '@', ip[, 2]))))
q <- function(x) paste0('"', gsub('"', '\\\\"', gsub('\\\\', '\\\\\\\\', x, fixed = TRUE), fixed = TRUE), '"')
exports <- function(p) tryCatch(head(sort(getNamespaceExports(p)), {MAX_NAMES_PER_MODULE}), error = function(e) character())
pk <- sort(unique(ip[, 1]))
parts <- vapply(pk, function(p) paste0(q(p), ':[', paste(q(exports(p)), collapse = ','), ']'), character(1))
cat({COMPLETIONS_MARK!r}, '{{', paste(parts, collapse = ','), '}}', '\\n', sep = '')
"""


def check_command(language: str) -> list[str]:
    return ["python", "-c", _PY_CHECK] if language == "python" else ["Rscript", "-e", _R_CHECK]


def job_name(stage: str, v: ScriptEnvironmentVersion) -> str:
    return f"tabularia-{stage}-e{v.environment_id}-v{v.number}"


def _labels(stage: str, v: ScriptEnvironmentVersion) -> dict:
    return {"app.kubernetes.io/managed-by": "tabularia", "app.kubernetes.io/component": "scripting",
            LABEL_ENV: str(v.environment_id), "tabularia.io/version": str(v.number), "tabularia.io/stage": stage}


def _job(name: str, labels: dict, deadline: int, pod: dict) -> dict:
    s = get_settings().scripts
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": deadline, "ttlSecondsAfterFinished": s.job_ttl_seconds,
            "template": {"metadata": {"labels": labels},
                         "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, **pod}},
        },
    }


# a compiler the kernel killed for memory (gcc, g++, clang), or the whole build
_OUT_OF_MEMORY = re.compile(r"Killed signal terminated program|internal compiler error: Killed|OOMKilled")


def _cores(cpu: str) -> float:
    return float(cpu[:-1]) / 1000 if cpu.endswith("m") else float(cpu)


def build_job(v: ScriptEnvironmentVersion, language: str) -> dict:
    s = get_settings().scripts
    tag = f"{repository(v.environment_id)}:v{v.number}"
    output = f"type=image,name={tag},push=true" + (",registry.insecure=true" if s.registry_insecure else "")
    script = (
        "set -e\n"
        "mkdir -p /home/user/ctx\n"
        'printf "%s" "$TABULARIA_DOCKERFILE" > /home/user/ctx/Dockerfile\n'
        "buildctl-daemonless.sh build --frontend dockerfile.v0 --local context=/home/user/ctx "
        f"--local dockerfile=/home/user/ctx --output {shlex.quote(output)} --metadata-file /home/user/meta.json\n"
        # the manifest's digest: meta.json also holds the config's, which is no image
        "digest=$(grep -o '\"containerimage.digest\": *\"sha256:[0-9a-f]\\{64\\}\"' /home/user/meta.json "
        "| grep -o 'sha256:[0-9a-f]\\{64\\}')\n"
        'test -n "$digest"\n'
        f'echo; echo "{DIGEST_MARK}$digest"\n'
    )
    mounts = [{"name": "buildkit", "mountPath": "/home/user/.local/share/buildkit"}]
    volumes: list[dict] = [{"name": "buildkit", "emptyDir": {}}]
    if s.registry_push_secret:
        mounts.append({"name": "registry", "mountPath": "/home/user/.docker", "readOnly": True})
        volumes.append({"name": "registry", "secret": {
            "secretName": s.registry_push_secret, "items": [{"key": ".dockerconfigjson", "path": "config.json"}]}})
    container = {
        "name": "build", "image": s.builder_image, "command": ["sh", "-c", script],
        "env": [{"name": "BUILDKITD_FLAGS", "value": "--oci-worker-no-process-sandbox"},
                {"name": "TABULARIA_DOCKERFILE", "value": dockerfile(language, v.base_image,
                                                                     parse_packages(language, v.packages))}],
        # rootless BuildKit: its own user namespace, which the default profiles forbid
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000,
                            "seccompProfile": {"type": "Unconfined"}, "appArmorProfile": {"type": "Unconfined"}},
        "volumeMounts": mounts,
        # the memory requested whole: a build lands only on a node that has it, and one
        # that needs more fails alone (its compiler killed) instead of starving the node
        "resources": {"requests": {"cpu": s.build_cpu if _cores(s.build_cpu) < 1 else "1", "memory": s.build_memory},
                      "limits": {"cpu": s.build_cpu, "memory": s.build_memory}},
    }
    return _job(job_name("build", v), _labels("build", v), s.build_timeout_seconds,
                {"containers": [container], "volumes": volumes})


def check_job(v: ScriptEnvironmentVersion, env: ScriptEnvironment) -> dict:
    s = get_settings().scripts
    container = {
        "name": "check", "image": v.image_ref, "command": check_command(env.language),
        "env": [{"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"}],
        "resources": {"requests": {"cpu": env.cpu_request, "memory": env.memory_request},
                      "limits": {"cpu": env.cpu_limit, "memory": env.memory_limit}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
    }
    pod: dict = {
        "securityContext": {"runAsNonRoot": True, "runAsUser": SCRIPT_UID, "runAsGroup": SCRIPT_UID,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container], "volumes": [{"name": "tmp", "emptyDir": {}}],
    }
    if s.registry_pull_secret:
        pod["imagePullSecrets"] = [{"name": s.registry_pull_secret}]
    return _job(job_name("check", v), _labels("check", v), s.check_timeout_seconds, pod)


_LOCK_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(==|@)\S+$")


def split_check_output(log: str) -> tuple[str, Optional[str]]:
    """The check's output: the lock (one package per line) and the completions JSON."""
    lock, completions = [], None
    for line in log.splitlines():
        if line.startswith(COMPLETIONS_MARK):
            try:
                parsed = json.loads(line[len(COMPLETIONS_MARK):])
                completions = json.dumps(parsed) if isinstance(parsed, dict) else None
            except ValueError:
                completions = None
        elif _LOCK_LINE.match(line.strip()):  # not what an import printed along the way
            lock.append(line.strip())
    return "\n".join(lock), completions


def digest_from_log(log: str) -> Optional[str]:
    for line in reversed(log.splitlines()):
        if line.startswith(DIGEST_MARK):
            d = line[len(DIGEST_MARK):].strip()
            return d if re.fullmatch(r"sha256:[0-9a-f]{64}", d) else None
    return None


def _strip_digest(log: str) -> str:
    return "\n".join(x for x in log.splitlines() if not x.startswith(DIGEST_MARK)).rstrip()


def _fail(v: ScriptEnvironmentVersion, message: str, log: Optional[str] = None) -> None:
    v.status, v.error, v.finished_at, v.job_name = "failed", message, now_utc(), None
    if log is not None:
        v.build_log = log
    logger.info("scripting: environment %s version %s failed: %s", v.environment_id, v.number, message)


async def avanza(session: Session, v: ScriptEnvironmentVersion) -> None:
    """One step of a version, if its Job has moved; the caller commits."""
    s = get_settings().scripts
    env = session.get(ScriptEnvironment, v.environment_id)
    if env is None:
        _fail(v, "The environment no longer exists")
        return
    if v.status == "queued":
        try:
            manifest = build_job(v, env.language)
        except ValueError as e:  # packages or settings that cannot become a Dockerfile
            _fail(v, str(e))
            return
        await kube_jobs.create_job(build_namespace(), manifest)
        v.status, v.job_name, v.started_at = "building", manifest["metadata"]["name"], now_utc()
        return
    stage_ns = build_namespace() if v.status == "building" else s.namespace
    name = v.job_name or job_name("build" if v.status == "building" else "check", v)
    job = await kube_jobs.get_job(stage_ns, name)
    if job is None:
        _fail(v, f"The Job {name} is gone from the cluster before it ended")
        return
    outcome = kube_jobs.job_outcome(job)
    if outcome is None:
        return
    # the check prints every package and what each exports: room for all of it
    log = await kube_jobs.job_log(stage_ns, name, s.log_max_bytes if v.status == "building" else 8 * 2**20)
    await kube_jobs.delete_job(stage_ns, name)  # what it had to say is in `log`
    if v.status == "building":
        if outcome == "failed":
            reason = kube_jobs.job_failure(job)
            if _OUT_OF_MEMORY.search(log or ""):
                _fail(v, f"The image build ran out of memory: a compiler was killed. Raise SCRIPTS__BUILD_MEMORY "
                         f"(now {s.build_memory}), or lower SCRIPTS__BUILD_CPU to run fewer compilers at once", log)
                return
            _fail(v, f"The image build failed{f' ({reason})' if reason else ''}: the end of the build log says why", log)
            return
        digest = digest_from_log(log)
        if not digest:
            _fail(v, "The build ended without the image's digest", log)
            return
        v.image_ref, v.build_log = f"{repository(v.environment_id)}@{digest}", _strip_digest(log)
        manifest = check_job(v, env)
        await kube_jobs.create_job(s.namespace, manifest)
        v.status, v.job_name = "checking", manifest["metadata"]["name"]
        return
    # checking
    if outcome == "failed":
        reason = kube_jobs.job_failure(job) or "it ended with an error"
        _fail(v, f"The image was built but does not start as a script would: {reason}\n{log}".rstrip())
        return
    lock, completions = split_check_output(log)
    v.status, v.lock, v.completions, v.finished_at, v.job_name, v.error = "ready", lock, completions, now_utc(), None, None
    current = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if current is None or current.number < v.number:
        env.current_version_id = v.id
        session.add(env)
    logger.info("scripting: environment %s version %s ready (%s)", env.id, v.number, v.image_ref)


async def un_giro(bind: Engine) -> int:
    """Moves forward every version in progress; returns how many changed state."""
    from app.services.regolazioni_motore import blocca

    changed = 0
    with Session(bind) as session:
        if not await blocca(session, attesa=0, chiave=LUCCHETTO):
            return 0
        versions = session.exec(select(ScriptEnvironmentVersion)
                                .where(ScriptEnvironmentVersion.status.in_(IN_PROGRESS))  # type: ignore[attr-defined]
                                .order_by(ScriptEnvironmentVersion.id)).all()
        for v in versions:
            before = v.status
            try:
                await avanza(session, v)
            except KubeError as e:
                if e.status in (0, 429) or e.status >= 500:
                    logger.warning("scripting: version %s of environment %s waits (%s)", v.number, v.environment_id, e)
                    continue  # the API server is restarting: at the next round
                _fail(v, str(e))
            session.add(v)
            changed += v.status != before
        session.commit()  # releases the lock
    return changed


async def cancel_environment(environment_id: int) -> None:
    """Stops the builds and checks of an environment being deleted."""
    selector = f"{LABEL_ENV}={environment_id}"
    for ns in {build_namespace(), get_settings().scripts.namespace}:
        await kube_jobs.delete_jobs(ns, selector)


async def build_loop(stop: asyncio.Event) -> None:
    if disabled_reason():
        return
    from app.db.session import engine

    every = get_settings().scripts.poll_seconds
    while not stop.is_set():
        try:
            await un_giro(engine)
        except Exception as e:  # noqa: BLE001 — database or cluster down: at the next round
            logger.warning("scripting: builds not checked (%s), again in %ss", e, every)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass
