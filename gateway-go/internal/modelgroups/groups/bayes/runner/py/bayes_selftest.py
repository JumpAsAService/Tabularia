"""The self-test of the group «bayes» (docs/specs/bayes-image.md, «The self-test»): its eight checks of the image, in the check
Job's conditions (uid 10001, a read-only root, /tmp the only writable path, no network). It prints one line a check,
«TABULARIA_SELFTEST ok|failed|skipped <name>: <detail>», the facts of each compile and run (times, peak memory: printed, never
compared with a threshold), then «TABULARIA_SELFTEST_REPORT {…}» last, as every group's self-test (common/py/tab_selftest.py),
and exits 0 when nothing failed, 4 otherwise.

The image holds numpy and pyarrow only: the common library's own tests, which read their table with pandas, run in the other
Python groups' self-tests, not here.

The tolerances and the alpha are the self-test's, declared here and in the spec; they test the software, never a user's model:
  check 4: the posterior mean and standard deviation within four Monte Carlo standard errors of the closed form;
  check 7: Stan's log-density (or log-mass) within a relative 1e-10 of the witness's (an absolute 1e-12 when the witness's value
           is within 1e-12 of zero), its log-CDF within a relative 1e-8 (the same absolute rule); a case of points.json that writes
           `"stan": {"logpdf_rel": …, "logcdf_rel": …, "reason": "…"}` uses its own, wider ones;
  check 8: a Kolmogorov–Smirnov test of each case's probability integral transform against the uniform, failed below alpha 0.001.

reference/expected.json:
  {"format_version": 1,
   "cmdstan": {"version": "2.40.0", "sha256": {"x86_64": "<hex>", "aarch64": "<hex>"}},   the recipe's sources (a Go test)
   "seed": 20261123,
   "draws_sha256": {"x86_64": ["<hex of out_1.csv's header and draws>", …]}}               check 5, computed on x86_64
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

import numpy as np

import bayes_cmdstan as cs
import bayes_stats
from tab_columns import read_ref_text
from tab_protocol import EXIT_OK, EXIT_SELFTEST_FAILED, SELFTEST_THREADS, to_json

# the sampler's columns, in CmdStan's order, before the model's
SAMPLER_COLUMNS = ["lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__"]
MCSE_TIMES = 4.0
LOGPDF_REL, LOGCDF_REL, NEAR_ZERO = 1e-10, 1e-8, 1e-12
ALPHA = 0.001
# check 8: the dimensions and the shapes of LKJ, 4,000 draws each
LKJ_CASES = [(k, eta) for k in (2, 3, 5) for eta in (0.5, 1.0, 2.0, 10.0)]
LKJ_DRAWS = 4000


class Failed(Exception):
    """A check that did not pass, with the sentence that says why."""


def fact(text: str) -> None:
    print("TABULARIA_SELFTEST_FACT " + text, flush=True)


def _mib(x: float) -> str:
    return f"{x:,.0f} MiB"


class SelfTest:
    def __init__(self, code: Path, work: Path, cmdstan: Path = cs.CMDSTAN, lock: Path = Path("/opt/tabularia/package-sha256.txt")):
        self.code = code
        self.work = work
        self.cmdstan = cmdstan
        self.lock = lock
        self.expected = json.loads(read_ref_text(code / "reference" / "expected.json"))
        self.machine = platform.machine()
        self.binaries: dict[str, Path] = {}
        self.csv: list[Path] = []

    # ── the files of the reference cases (gzipped in the check Job's ConfigMap) ──
    def reference(self, rel: str) -> str:
        return read_ref_text(self.code / "reference" / rel)

    def compile(self, name: str, folder: str, extra: tuple[str, ...] = ()) -> tuple[Path, cs.Outcome]:
        exe, out = cs.compile_program(self.reference(f"programs/{name}.stan"), name, self.work / folder, extra)
        fact(f"compile {name} ({folder}): {out.seconds:.1f} s, largest process {_mib(out.peak_mib)}, exit {out.status}")
        if out.missing:
            raise Failed(f"make could not start: {out.missing}")
        if out.status != 0 or not exe.exists():
            raise Failed(f"the compile of {name} failed (exit {out.status}): " + _tail(out.output))
        if "Read-only file system" in out.output:
            raise Failed(f"the compile of {name} tried to write in the read-only root: " + _lines(out.output, "Read-only file system"))
        fact(f"binary {name}: {exe.stat().st_size:,} bytes")
        return exe, out

    def sample(self, exe: Path, args: list[str], log: str) -> cs.Outcome:
        out = cs.run(exe, args, exe.parent, log)
        fact(f"run {exe.name} {log}: {out.seconds:.2f} s, largest process {_mib(out.peak_mib)}, exit {out.status}")
        if out.missing:
            raise Failed(f"{exe.name} could not be run: {out.missing}")
        if out.status != 0:
            raise Failed(f"{exe.name} failed (exit {out.status}): " + _tail(out.output))
        return out

    # ── 1. the image is the recipe's ──
    def check_image(self) -> str:
        want = self.expected["cmdstan"]
        version = want["version"]
        out = cs.run(self.cmdstan / "bin" / "stanc", ["--version"], self.work, "stanc-version.log")
        said = out.output.strip()
        if out.status != 0 or said != f"stanc3 v{version} (Unix)":
            raise Failed(f"bin/stanc --version says «{said or out.missing}», the recipe pins {version}")
        sha = (want.get("sha256") or {}).get(self.machine)
        if not sha:
            raise Failed(f"the recipe pins no CmdStan tarball for {self.machine}")
        lines = self.lock.read_text(encoding="utf-8").splitlines() if self.lock.exists() else []
        line = f"{sha}  cmdstan@{version}"
        if line not in lines:
            found = [x for x in lines if x.endswith(f"cmdstan@{version}")]
            raise Failed(f"the image's {self.lock} records CmdStan as {found or 'nothing'}, the recipe pins {sha} for {self.machine}")
        local = (self.cmdstan / "make" / "local").read_text(encoding="utf-8")
        if "STAN_THREADS=true" not in local.split():
            raise Failed(f"make/local says «{local.strip()}», not STAN_THREADS=true")
        headers = sorted((self.cmdstan / "stan/src/stan/model/model_header.hpp.gch").glob("*.hpp.gch"))
        if len(headers) != 1:
            raise Failed(f"the image holds {len(headers)} precompiled headers, not one")
        header_d = headers[0].with_suffix("").with_suffix(".d")
        main_d = self.cmdstan / "src/cmdstan/main_threads.d"
        for f in (header_d, main_d):
            if not f.exists():
                raise Failed(f"{f.relative_to(self.cmdstan)} is missing: every compile would try to write it in the read-only root")
        return f"stanc3 v{version}, sha256 {sha[:12]}… for {self.machine}, STAN_THREADS, {headers[0].name} and its dependency files"

    # ── 2. a compile in the Job's conditions, the precompiled header used ──
    def check_compile(self) -> str:
        exe, plain = self.compile("ar1", "ar1")
        self.binaries["ar1"] = exe
        _, h = self.compile("ar1", "ar1-h", ("CXXFLAGS_PROGRAM=-H -Wno-ignored-attributes",))
        used = [x for x in h.output.splitlines() if x.startswith("! ") and "model_header" in x and x.endswith(".hpp.gch")]
        if not used:
            raise Failed("the compile with -H shows no «! …model_header…hpp.gch» line: the precompiled header was not used")
        return f"{plain.seconds:.1f} s, {_mib(plain.peak_mib)}; the header used: {used[0][2:].split('/')[-1]}"

    # ── 3. the binary runs from /tmp ──
    def check_binary_runs(self) -> str:
        exe = self.binaries.get("ar1")
        if exe is None:
            raise Failed("no binary: the compile of check 2 failed")
        out = cs.run(exe, ["info"], exe.parent, "info.log")
        if out.missing:
            if "Permission denied" in out.missing or "PermissionError" in out.missing:
                raise Failed(f"the binary in {exe.parent} cannot be executed ({out.missing}): /tmp is mounted noexec, and a cluster "
                             "whose policy mounts emptyDir volumes noexec cannot run this group")
            raise Failed(f"the binary could not start: {out.missing}")
        major = re.search(r"stan_version_major = (\d+)", out.output)
        minor = re.search(r"stan_version_minor = (\d+)", out.output)
        version = self.expected["cmdstan"]["version"].split(".")
        if out.status != 0 or not major or not minor or [major.group(1), minor.group(1)] != version[:2]:
            raise Failed(f"`info` (exit {out.status}) says {_tail(out.output)}, not Stan {'.'.join(version[:2])}")
        return f"stan_version_major = {major.group(1)}, stan_version_minor = {minor.group(1)}"

    # ── 4. NUTS gives the known answer ──
    def check_nuts(self) -> str:
        exe, _ = self.compile("normal_known_sigma", "normal")
        data = json.loads(self.reference("data/normal.json"))
        (exe.parent / "normal.json").write_text(json.dumps(data), encoding="utf-8")
        self.sample(exe, ["sample", "num_chains=4", "num_samples=1000", "num_warmup=1000",
                          "data", f"file={exe.parent / 'normal.json'}", "output", f"file={exe.parent / 'out.csv'}",
                          "random", f"seed={self.expected['seed']}", f"num_threads={SELFTEST_THREADS}"], "sample.log")
        self.csv = [exe.parent / f"out_{c}.csv" for c in range(1, 5)]
        chains = []
        for path in self.csv:
            header, rows = cs.read_csv(path)
            if header[:len(SAMPLER_COLUMNS)] != SAMPLER_COLUMNS or header[len(SAMPLER_COLUMNS):] != ["mu"]:
                raise Failed(f"{path.name}'s header is {header}, not the sampler's columns then mu")
            if len(rows) != 1000:
                raise Failed(f"{path.name} holds {len(rows)} draws, not 1,000")
            chains.append([r[header.index("mu")] for r in rows])
        y = np.asarray(data["y"], dtype=float)
        precision = 1 / data["tau0"] ** 2 + len(y) / data["sigma"] ** 2
        mean = (data["mu0"] / data["tau0"] ** 2 + y.sum() / data["sigma"] ** 2) / precision
        sd = 1 / math.sqrt(precision)
        got = bayes_stats.mean_and_sd(np.asarray(chains))
        said = (f"mean {got['mean']:.5f} (closed form {mean:.5f}, MCSE {got['mcse_mean']:.5f}), sd {got['sd']:.5f} "
                f"(closed form {sd:.5f}, MCSE {got['mcse_sd']:.5f}), ESS {got['ess']:.0f}")
        if abs(got["mean"] - mean) > MCSE_TIMES * got["mcse_mean"] or abs(got["sd"] - sd) > MCSE_TIMES * got["mcse_sd"]:
            raise Failed(f"farther than {MCSE_TIMES:g} Monte Carlo standard errors from the closed form: {said}")
        return said

    # ── 5. the same seed gives the same draws ──
    def check_same_draws(self) -> str | None:
        import hashlib

        if len(self.csv) != 4 or not all(p.exists() for p in self.csv):
            raise Failed("no draws: check 4 did not sample")
        got = [hashlib.sha256(cs.draws_lines(p)).hexdigest() for p in self.csv]
        want = (self.expected.get("draws_sha256") or {}).get(self.machine)
        if self.machine != "x86_64":
            return None  # skipped: the reference is computed on x86_64, floating point differs elsewhere
        if want != got:
            raise Failed(f"the draws' sha256 are {got}, the reference ones {want}")
        return "the four chains' draws are the reference's, byte for byte"

    # ── 6. the catalogue's names exist in the pinned Stan ──
    def check_names(self) -> str:
        folder = self.work / "names"
        folder.mkdir(parents=True, exist_ok=True)
        program = folder / "catalog_names.stan"
        program.write_text(self.reference("programs/catalog_names.stan"), encoding="utf-8")
        out = cs.run(self.cmdstan / "bin" / "stanc", ["--info", str(program)], folder, "info.log")
        if out.status != 0:
            raise Failed("stanc refuses the program of the catalogue's names: " + _tail(out.output))
        info = json.loads(out.output)
        want = json.loads(self.reference("data/catalog_names.json"))
        problems = []
        for key in ("functions", "distributions"):
            missing = sorted(set(want[key]) - set(info.get(key, [])))
            extra = sorted(set(info.get(key, [])) - set(want[key]))
            if missing:
                problems.append(f"stanc does not report the {key} {missing}")
            if extra:
                problems.append(f"stanc reports {key} the catalogue does not name: {extra}")
        if problems:
            raise Failed("; ".join(problems))
        return f"{len(want['functions'])} functions and {len(want['distributions'])} distributions, as stanc --info reports them"

    # ── 7. the catalogue's points against Stan's own functions ──
    def check_points(self) -> str:
        points = json.loads(self.reference("data/points.json"))
        layout = json.loads(self.reference("data/catalog_points.json"))["distributions"]
        data, cases, left = points_data(points, layout)
        exe, _ = self.compile("catalog_points", "points")
        (exe.parent / "points.data.json").write_text(json.dumps(data), encoding="utf-8")
        self.sample(exe, ["sample", "algorithm=fixed_param", "num_samples=1", "num_warmup=0", "data", f"file={exe.parent / 'points.data.json'}",
                          "output", f"file={exe.parent / 'points.csv'}", "sig_figs=18", "random", "seed=1"], "points.log")
        header, rows = cs.read_csv(exe.parent / "points.csv")
        values = dict(zip(header, rows[0]))
        checked, failures = compare_points(cases, layout, values)
        if failures:
            more = f" … and {len(failures) - 20} more" if len(failures) > 20 else ""
            raise Failed(f"{len(failures)} of {checked} values differ from the witness's: " + "; ".join(failures[:20]) + more)
        return f"{checked} values of {len(cases)} cases within the tolerances; {left} cases of kind marginal left to check 8 and the catalogue's suite"

    # ── 8. LKJ's marginal against Stan's generator ──
    def check_lkj(self) -> str:
        exe, _ = self.compile("lkj_marginal", "lkj")
        data = {"C": len(LKJ_CASES), "K": [k for k, _ in LKJ_CASES], "eta": [e for _, e in LKJ_CASES]}
        (exe.parent / "lkj.data.json").write_text(json.dumps(data), encoding="utf-8")
        self.sample(exe, ["sample", "algorithm=fixed_param", f"num_samples={LKJ_DRAWS}", "num_warmup=0", "data", f"file={exe.parent / 'lkj.data.json'}",
                          "output", f"file={exe.parent / 'lkj.csv'}", "sig_figs=18", "random", f"seed={self.expected['seed']}"], "lkj.log")
        header, rows = cs.read_csv(exe.parent / "lkj.csv")
        table = np.asarray(rows)
        said, bad = [], []
        for c, (k, eta) in enumerate(LKJ_CASES, start=1):
            for element, column in (("Omega[2,1]", f"u_21.{c}"), (f"Omega[{k},1]", f"u_k1.{c}")):
                if column.startswith("u_k1") and k == 2:
                    continue  # the same element as Omega[2,1]
                u = table[:, header.index(column)]
                d, p = bayes_stats.ks_uniform(u)
                said.append(f"K={k} eta={eta:g} {element}: D={d:.4f} p={p:.4f}")
                if p < ALPHA:
                    bad.append(said[-1])
        fact("LKJ marginal p-values: " + "; ".join(said))
        if bad:
            raise Failed(f"below alpha {ALPHA:g}: " + "; ".join(bad))
        return f"{len(said)} Kolmogorov–Smirnov tests of {LKJ_DRAWS:,} draws, every p-value at or above alpha {ALPHA:g} (lowest {min(float(x.split('p=')[1]) for x in said):.4f})"


def points_data(points: dict[str, Any], layout: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """catalog_points.stan's data from points.json (format 1, make_points.py's header): for each distribution of the layout, its
    cases of kind density or mass, their real arguments, their integer ones and their points. The cases of kind marginal are left
    out and counted."""
    if points.get("format_version") != 1:
        raise Failed(f"points.json is of format {points.get('format_version')}, this runner reads format 1")
    data: dict[str, Any] = {}
    cases: list[dict[str, Any]] = []
    left = 0
    by: dict[str, list[dict[str, Any]]] = {d: [] for d in layout}
    for case in points["cases"]:
        if case["kind"] == "marginal":
            left += 1
            continue
        if case["kind"] not in ("density", "mass") or case["dist"] not in layout:
            raise Failed(f"a case of points.json ({case['dist']}, {case['kind']}) is not one catalog_points.stan evaluates")
        by[case["dist"]].append(case)
    for dist, spec in layout.items():
        own = by[dist]
        data[f"n_{dist}"] = len(own)
        if spec["real"]:
            data[f"a_{dist}"] = [[float(c["args"][i]) for i in spec["real"]] for c in own]
        if spec["integer"]:
            data[f"i_{dist}"] = [[int(c["args"][i]) for i in spec["integer"]] for c in own]
        data[f"x_{dist}"] = [int(c["x"]) if spec["integer_outcome"] else float(c["x"]) for c in own]
        for i, c in enumerate(own, start=1):
            cases.append({**c, "_index": i})
    return data, cases, left


def _within(got: float, want: float, rel: float) -> bool:
    if not math.isfinite(got):
        return False
    if abs(want) <= NEAR_ZERO:
        return abs(got - want) <= NEAR_ZERO
    return abs(got - want) <= rel * abs(want)


def compare_points(cases: list[dict[str, Any]], layout: dict[str, Any], values: dict[str, float]) -> tuple[int, list[str]]:
    """Stan's values (the CSV's columns lp_<dist>.<i>, lcdf_<dist>.<i>) against the witness's, within the declared tolerances:
    the number of values compared, and a sentence for each that differs (the distribution, the arguments, the point, both values
    and the difference)."""
    checked, failures = 0, []
    for c in cases:
        dist, i = c["dist"], c["_index"]
        own = c.get("stan") or {}
        pairs = [("lp", "logpmf" if c["kind"] == "mass" else "logpdf", float(own.get("logpdf_rel", LOGPDF_REL)))]
        if layout[dist]["lcdf"] and "logcdf" in c:
            pairs.append(("lcdf", "logcdf", float(own.get("logcdf_rel", LOGCDF_REL))))
        for column, key, rel in pairs:
            name = f"{column}_{dist}.{i}"
            if name not in values:
                failures.append(f"{dist}{tuple(c['args'])} at {c['x']!r}: no column {name}")
                continue
            got, want = values[name], float(c[key])
            checked += 1
            if not _within(got, want, rel):
                failures.append(f"{dist}{tuple(c['args'])} at {c['x']!r}: Stan's {key} {got!r}, the witness's {want!r}, difference "
                                f"{got - want:.3g} (relative {abs(got - want) / max(abs(want), NEAR_ZERO):.3g}, tolerance {rel:g})")
    return checked, failures


def _tail(text: str, n: int = 6) -> str:
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return " | ".join(lines[-n:]) if lines else "(no output)"


def _lines(text: str, needle: str) -> str:
    return " | ".join(x.strip() for x in text.splitlines() if needle in x)[:600]


def _versions(t: SelfTest) -> dict[str, str]:
    from importlib import metadata

    out = {"python": platform.python_version(), "cmdstan": t.expected["cmdstan"]["version"]}
    for name in ("numpy", "pyarrow"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    record = Path("/opt/tabularia/system-libraries.txt")
    if record.exists():
        for line in record.read_text(encoding="utf-8").split():
            pkg, _, version = line.partition("@")
            out[pkg] = version
    return out


def main(code: Path) -> int:
    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail})
        print(f"TABULARIA_SELFTEST {status} {name}" + (f": {detail}" if detail else ""), flush=True)

    work = Path(tempfile.mkdtemp(prefix="tabularia-bayes-selftest-", dir=os.environ.get("TMPDIR") or "/tmp"))
    t = SelfTest(code, work)

    def run(name: str, f: Callable[[], str | None]) -> None:
        try:
            said = f()
            if said is None:
                record(name, "skipped", f"not compared on {t.machine}: the reference is computed on x86_64, floating point differs")
            else:
                record(name, "ok", said)
        except Failed as e:
            record(name, "failed", str(e))
        except Exception as e:  # noqa: BLE001 — every failure is a line of the report
            record(name, "failed", f"{type(e).__name__}: {e}")
            traceback.print_exc()

    fact(f"machine {t.machine}, uid {os.getuid()}, work folder {work}")
    run("1 the image is the recipe's", t.check_image)
    run("2 a compile in the Job's conditions", t.check_compile)
    run("3 the binary runs from /tmp", t.check_binary_runs)
    run("4 NUTS gives the known answer", t.check_nuts)
    run("5 the same seed gives the same draws", t.check_same_draws)
    run("6 the catalogue's names exist in Stan", t.check_names)
    run("7 the catalogue's points against Stan", t.check_points)
    run("8 LKJ's marginal against Stan's generator", t.check_lkj)
    counts = {s: sum(c["status"] == s for c in checks) for s in ("ok", "failed", "skipped")}
    report = {"group": "bayes", **counts, "software": _versions(t), "checks": checks}
    shutil.rmtree(work, ignore_errors=True)
    print("TABULARIA_SELFTEST_REPORT " + to_json(report), flush=True)
    sys.stdout.flush()
    return EXIT_SELFTEST_FAILED if counts["failed"] else EXIT_OK
