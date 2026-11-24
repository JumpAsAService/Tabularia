"""A fake CmdStan for the follower's tests (docs/specs/bayes-contract.md, «Testing strategy»; lot K2): a process that writes the CSV
files and the progress lines as CmdStan 2.40.0 of the image does — measured by lots K0 and K2 — slowly, chains at different speeds,
each file in random pieces cut anywhere (in the middle of a line, of a number), so that a follower sees what it would see while real
chains sample.

    python -I fake_cmdstan.py <spec.json>
    python -I fake_cmdstan.py --cmdstan <spec.json> <CmdStan's arguments…>

The second form is the binary of a whole sampling Job's test (lot R1 of bayes-runner): a shell script fetched and hashed as the compiled
model is, which hands CmdStan's own argument list over (`num_samples=`, `num_warmup=`, `num_chains=`, `algorithm=fixed_param`, the data
and output `file=`, `seed=`, `refresh=`); the spec file gives what a real binary knows by itself (the program's columns, their means).

The spec (JSON), every key optional but `out`, `chains`, `draws`:

    out          CmdStan's `output file=`: one chain writes it as it is, C ≥ 2 chains write <stem>_1.csv … <stem>_C.csv
    chains, draws, warmup (default 10), refresh (default 100), seed (default 1)
    header       the program's columns (after the sampler's fields), or `quantities`: P, named q.1 … q.P
    fixed_param  true: the sampler's fields are lp__ and accept_stat__ (both 0), no adaptation comments (the prior predictive)
    speeds       iterations a tick for each chain (default 1 each; fractions allowed); `start` the tick each chain starts at
    tick         seconds a tick sleeps (default 0.001); `piece_seed` the seed of the cuts (default 0); `max_piece` the longest piece
    special      [[chain, draw, column (1-based among the program's columns), "nan" | "inf" | "-inf"], …]
    means        a mean for each of the program's columns (default 0): the values are normal around it, sd 1
    pool         draw lines are taken in turn from a pool of this many lines a chain (the memory test: fast, still distinct lines)
    init_fail    true: each chain writes its configuration's comments only, «Initialization failed.» goes to stderr, exit 1
    die          {"chain", "after", "exit"}: once that chain has written `after` draws, exit with `exit` (default 70), stderr says so
    malformed    {"chain", "draw"}: that draw line loses its last field
    extra        {"chain"}: that chain writes one draw more than asked

The values are drawn from numpy's generator seeded by (seed, chain) and written with 8 significant digits («%.8g», CmdStan's
default `sig_figs = 8`): the files' bytes depend on the spec's draws, never on its speeds or cuts.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLER_NUTS = ("lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__")
SAMPLER_FIXED_PARAM = ("lp__", "accept_stat__")
STEP_SIZE = 0.025693225


def csv_paths(out: str, chains: int) -> list[Path]:
    p = Path(out)
    return [p] if chains == 1 else [p.with_name(f"{p.stem}_{k}{p.suffix}") for k in range(1, chains + 1)]


def program_columns(spec: dict) -> list[str]:
    if "header" in spec:
        return list(spec["header"])
    return [f"q.{i}" for i in range(1, int(spec.get("quantities", 3)) + 1)]


def header(spec: dict) -> list[str]:
    """The whole CSV header: the sampler's fields, then the program's columns."""
    return list(SAMPLER_FIXED_PARAM if spec.get("fixed_param") else SAMPLER_NUTS) + program_columns(spec)


def fmt(x: float) -> str:
    """A number as CmdStan writes it with sig_figs = 8: «%.8g», and nan, inf, -inf as text."""
    if x != x:
        return "nan"
    if x in (float("inf"), float("-inf")):
        return "inf" if x > 0 else "-inf"
    return "%.8g" % x


def config_comments(spec: dict, chain: int) -> str:
    """The configuration CmdStan 2.40.0 writes at the top of each file (measured on the image), with this run's values."""
    fixed = bool(spec.get("fixed_param"))
    warmup = int(spec.get("warmup", 10))
    lines = ["stan_version_major = 2", "stan_version_minor = 40", "stan_version_patch = 0", "model = model_model",
             "start_datetime = 2026-11-23 19:33:40 UTC", "method = sample (Default)", "  sample",
             f"    num_samples = {spec['draws']}", f"    num_warmup = {warmup}", "    save_warmup = false (Default)",
             "    thin = 1 (Default)", "    adapt", "      engaged = true (Default)", "      gamma = 0.05 (Default)",
             "      delta = 0.8 (Default)", "      kappa = 0.75 (Default)", "      t0 = 10 (Default)",
             "      init_buffer = 75 (Default)", "      term_buffer = 50 (Default)", "      window = 25 (Default)",
             "      save_metric = false (Default)"]
    if fixed:
        lines += ["    algorithm = fixed_param"]
    else:
        lines += ["    algorithm = hmc (Default)", "      hmc", "        engine = nuts (Default)", "          nuts",
                  "            max_depth = 10 (Default)", "        metric = diag_e (Default)", "        metric_file =  (Default)",
                  "        stepsize = 1 (Default)", "        stepsize_jitter = 0 (Default)"]
    lines += [f"    num_chains = {spec['chains']}", "id = 1 (Default)", "data", "  file = /tmp/run/data.json", "init = 2 (Default)",
              "random", f"  seed = {spec.get('seed', 1)}", "output", f"  file = {spec['out']}", "  diagnostic_file =  (Default)",
              f"  refresh = {spec.get('refresh', 100)}", "  sig_figs = 8 (Default)", "  profile_file = profile.csv (Default)",
              "  save_cmdstan_config = false (Default)", "num_threads = 2", "stanc_version = stanc3 v2.40.0", "stancflags = "]
    return "".join(f"# {line}\n" for line in lines)


def adaptation_comments(spec: dict) -> str:
    n = len(program_columns(spec))
    return ("# Adaptation terminated\n# Step size = 0.0256932\n# Diagonal elements of inverse mass matrix:\n# "
            + ", ".join("1" for _ in range(max(1, n))) + "\n")


def timing_comments(chain: int) -> str:
    return (f"# \n#  Elapsed Time: {0.1 * chain:.3g} seconds (Warm-up)\n#                {0.2 * chain:.3g} seconds (Sampling)\n"
            f"#                {0.3 * chain:.3g} seconds (Total)\n# \n")


class Draws:
    """The draw lines of one chain, in order (the file's bytes depend on the spec alone)."""

    def __init__(self, spec: dict, chain: int):
        self.fixed = bool(spec.get("fixed_param"))
        self.p = len(program_columns(spec))
        self.rng = np.random.default_rng([int(spec.get("seed", 1)), chain])
        self.special = {(int(d), int(c)): v for k, d, c, v in spec.get("special", []) if int(k) == chain}
        malformed = spec.get("malformed") or {}
        self.malformed = int(malformed["draw"]) if int(malformed.get("chain", 0)) == chain else None
        self.means = np.asarray(spec.get("means") or [0.0] * self.p, dtype=np.float64)
        pool = int(spec.get("pool", 0))
        self.pool = [self._make(d) for d in range(1, pool + 1)] if pool else None

    def _make(self, draw: int) -> str:
        values = self.rng.normal(size=self.p) + self.means
        if self.fixed:
            fields = ["0", "0"]
        else:
            lp = -40.0 + self.rng.normal()
            fields = [fmt(lp), fmt(self.rng.uniform(0.6, 1.0)), fmt(STEP_SIZE), str(int(self.rng.integers(1, 4))),
                      str(int(self.rng.integers(3, 8))), "0", fmt(-lp + self.rng.normal())]
        program = [fmt(v) for v in values]
        for (d, c), v in self.special.items():
            if d == draw:
                program[c - 1] = v
        line = fields + program
        if self.malformed == draw:
            line = line[:-1]
        return ",".join(line) + "\n"

    def line(self, draw: int) -> str:
        if self.pool is not None and not any(d == draw for d, _ in self.special) and self.malformed != draw:
            return self.pool[(draw - 1) % len(self.pool)]
        return self._make(draw)


def progress(chains: int, chain: int, i: int, n: int, phase: str) -> str:
    """CmdStan's progress line: «Chain [k] Iteration: i / n [ p%]  (Warmup)», without «Chain [k] » with one chain."""
    text = f"Iteration: {i:{len(str(n))}d} / {n} [{int(100 * i / n):3d}%]  ({phase})"
    return text if chains == 1 else f"Chain [{chain}] {text}"


def chain_steps(spec: dict, chain: int) -> Iterator[tuple[str, str]]:
    """One chain's run as (bytes for its CSV, a progress line or ""), one item an iteration, the configuration and the header
    first, the elapsed times last."""
    chains, draws, warmup = int(spec["chains"]), int(spec["draws"]), int(spec.get("warmup", 10))
    if spec.get("fixed_param"):
        warmup = 0
    refresh = int(spec.get("refresh", 100))
    total = warmup + draws
    yield config_comments(spec, chain) + ",".join(header(spec)) + "\n", ""
    generator = Draws(spec, chain)
    for i in range(1, total + 1):
        phase = "Warmup" if i <= warmup else "Sampling"
        text = ""
        if i == warmup + 1 and warmup > 0 and not spec.get("fixed_param"):
            text += adaptation_comments(spec)
        if i > warmup:
            text += generator.line(i - warmup)
        shown = i == 1 or i % refresh == 0 or i == warmup + 1 or i == total
        yield text, progress(chains, chain, i, total, phase) if shown else ""
    extra = spec.get("extra") or {}
    if int(extra.get("chain", 0)) == chain:
        yield generator.line(draws + 1), ""
    yield timing_comments(chain), ""


def _write_pieces(fd: int, data: bytes, cuts: random.Random, max_piece: int) -> bytes:
    """Writes `data` in random pieces of at most `max_piece` bytes, leaving a random part of the last piece for later (often a line
    cut in two, sometimes nothing written at all); returns what is left."""
    view = memoryview(data)
    pos = 0
    while len(data) - pos > max_piece:
        pos += os.write(fd, view[pos:pos + cuts.randint(1, max_piece)])
    n = cuts.randint(0, len(data) - pos)
    if n:
        pos += os.write(fd, view[pos:pos + n])
    return data[pos:]


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    chains = int(spec["chains"])
    paths = csv_paths(spec["out"], chains)
    out = sys.stdout
    out.write("method = sample (Default)\n  sample\n    num_samples = %d\n\n" % int(spec["draws"]))
    out.write("Gradient evaluation took 3.2e-05 seconds\n1000 transitions using 10 leapfrog steps per transition would take "
              "0.32 seconds.\nAdjust your expectations accordingly!\n\n")
    out.flush()
    if spec.get("init_fail"):
        for k, p in enumerate(paths, start=1):
            p.write_text(config_comments(spec, k))
            out.write("Rejecting initial value:\n  Log probability evaluates to log(0), i.e. negative infinity.\n"
                      "  Stan can't start sampling from this initial value.\n")
        out.flush()
        sys.stderr.write("Initialization between (-2, 2) failed after 100 attempts. \n Try specifying initial values, reducing "
                         "ranges of constrained values, or reparameterizing the model.\nInitialization failed.\n")
        sys.stderr.flush()
        return 1
    speeds = [float(x) for x in spec.get("speeds", [1] * chains)]
    start = [int(x) for x in spec.get("start", [0] * chains)]
    tick = float(spec.get("tick", 0.001))
    cuts = random.Random(int(spec.get("piece_seed", 0)))
    max_piece = int(spec.get("max_piece", 4096))
    die = spec.get("die") or {}
    steps = [chain_steps(spec, k) for k in range(1, chains + 1)]
    pending = [b""] * chains
    credit = [0.0] * chains
    fds: list[int | None] = [None] * chains
    done = [False] * chains
    drawn = [0] * chains
    t = 0
    while not all(done):
        for c in range(chains):
            if done[c] or t < start[c]:
                continue
            if fds[c] is None:
                fds[c] = os.open(paths[c], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            credit[c] += speeds[c]
            finished = False
            while credit[c] >= 1:
                credit[c] -= 1
                try:
                    text, line = next(steps[c])
                except StopIteration:
                    finished = True
                    break
                if text:
                    drawn[c] += sum(1 for x in text.split("\n") if x and not x.startswith("#") and not x.startswith("lp__"))
                    pending[c] += text.encode()
                if line:
                    out.write(line + "\n")
            if not finished:
                pending[c] = _write_pieces(fds[c], pending[c], cuts, max_piece)
            if finished:
                while pending[c]:
                    n = os.write(fds[c], pending[c])
                    pending[c] = pending[c][n:]
                os.close(fds[c])
                fds[c] = None
                done[c] = True
                out.write("\n Elapsed Time: 0 seconds (Warm-up)\n               0 seconds (Sampling)\n"
                          "               0 seconds (Total)\n\n")
            if die and int(die["chain"]) == c + 1 and drawn[c] >= int(die["after"]):
                while pending[c]:
                    n = os.write(fds[c], pending[c])
                    pending[c] = pending[c][n:]
                out.flush()
                sys.stderr.write(f"Exception: the fake sampler stops chain {c + 1} here\n")
                sys.stderr.flush()
                return int(die.get("exit", 70))
        out.flush()
        t += 1
        if tick:
            time.sleep(tick)
    return 0


def cmdstan_spec(spec_path: str, args: list[str]) -> str:
    """The spec of a run from CmdStan's argument list and the binary's own spec file; written beside the output, its path returned."""
    spec = json.loads(Path(spec_path).read_text())
    section = ""
    for arg in args:
        if "=" not in arg:
            section = arg
            continue
        key, value = arg.split("=", 1)
        if key == "num_samples":
            spec["draws"] = int(value)
        elif key == "num_warmup":
            spec["warmup"] = int(value)
        elif key == "num_chains":
            spec["chains"] = int(value)
        elif key == "algorithm" and value == "fixed_param":
            spec["fixed_param"] = True
        elif key == "seed":
            spec["seed"] = int(value)
        elif key == "refresh":
            spec["refresh"] = int(value)
        elif key == "file" and section == "data":
            json.loads(Path(value).read_text())          # a real binary reads its data first: a broken file fails here
        elif key == "file" and section == "output":
            spec["out"] = value
    spec.setdefault("chains", 1)
    path = Path(spec["out"]).with_name("fake_spec.json")
    path.write_text(json.dumps(spec))
    return str(path)


if __name__ == "__main__":
    if sys.argv[1] == "--cmdstan":
        sys.exit(main(cmdstan_spec(sys.argv[2], sys.argv[3:])))
    sys.exit(main(sys.argv[1]))
