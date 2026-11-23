"""CmdStan as the image holds it (docs/specs/bayes-image.md, «The contract of the image»): the compile of a Stan program into /tmp
and the run of its executable, both with an empty environment but PATH, so that no CXXFLAGS, STANCFLAGS, CPPFLAGS or LDFLAGS of the
Pod reaches make, and no variable on make's command line but PRECOMPILED_HEADERS=false (make then never tries to rebuild the
header in the read-only /opt/cmdstan; GCC still finds it and uses it). The flags are the image's alone: STAN_THREADS from
make/local, CmdStan's default -O3.

Every compile and every run here is a child process waited for with os.wait4, which gives its time and the peak resident memory
of its largest process (ru_maxrss): facts for the log, never compared with a threshold.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CMDSTAN = Path("/opt/cmdstan")
# the only variable of a compile's and of a run's environment
PATH = "/usr/local/bin:/usr/bin:/bin"
# the one variable make is given (the contract): make leaves the image's precompiled header alone
MAKE_VARIABLES = ("PRECOMPILED_HEADERS=false",)


@dataclass
class Outcome:
    """A child process waited for: its exit status, its output (stdout and stderr together), its wall time in seconds and the peak
    resident memory of its largest process in MiB; `missing` when it could not start, with the reason."""
    status: int
    output: str
    seconds: float
    peak_mib: float
    missing: str = ""


def _wait(args: list[str], cwd: Path, log: Path, timeout: float) -> Outcome:
    t0 = time.monotonic()
    with open(log, "wb") as out:
        try:
            proc = subprocess.Popen(args, cwd=str(cwd), env={"PATH": PATH}, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        except OSError as e:
            return Outcome(-1, "", 0.0, 0.0, missing=f"{type(e).__name__}: {e}")
        while True:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if time.monotonic() - t0 > timeout:
                proc.kill()
                pid, status, usage = os.wait4(proc.pid, 0)
                break
            time.sleep(0.05)
        proc.returncode = os.waitstatus_to_exitcode(status)
    seconds = time.monotonic() - t0
    text = log.read_text(encoding="utf-8", errors="replace")
    return Outcome(proc.returncode, text, seconds, usage.ru_maxrss / 1024.0)


def compile_program(source: str, name: str, workdir: Path, extra: tuple[str, ...] = (), timeout: float = 600) -> tuple[Path, Outcome]:
    """Compiles the program `source` as <workdir>/<name>: `make -C /opt/cmdstan <workdir>/<name> PRECOMPILED_HEADERS=false`, with the
    program at <workdir>/<name>.stan and an environment of PATH alone. `extra` are more of make's variables: the self-test's
    -H (check 2) only."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / f"{name}.stan").write_text(source, encoding="utf-8")
    exe = workdir / name
    outcome = _wait(["make", "-C", str(CMDSTAN), str(exe), *MAKE_VARIABLES, *extra], workdir, workdir / f"{name}.make.log", timeout)
    return exe, outcome


def run(exe: Path, args: list[str], workdir: Path, log_name: str, timeout: float = 600) -> Outcome:
    """Runs a compiled program from its folder in /tmp, with an environment of PATH alone."""
    return _wait([str(exe), *args], workdir, workdir / log_name, timeout)


def read_csv(path: Path) -> tuple[list[str], list[list[float]]]:
    """A CmdStan output CSV: its header (the column names) and its rows of numbers; the comment lines (#) left out."""
    header: list[str] = []
    rows: list[list[float]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            if not header:
                header = line.strip().split(",")
                continue
            rows.append([float(v) for v in line.strip().split(",")])
    return header, rows


def draws_lines(path: Path) -> bytes:
    """The lines of a CSV that are not comments (the header and the draws), as bytes: what the same seed must give again."""
    with open(path, "rb") as f:
        return b"".join(line for line in f if line.strip() and not line.startswith(b"#"))
