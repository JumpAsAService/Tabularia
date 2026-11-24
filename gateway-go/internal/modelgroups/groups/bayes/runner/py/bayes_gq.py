"""The `generate_quantities` runs of a sampling Job (docs/specs/bayes-runner.md, «The runs of CmdStan»; lot R2): the posterior predictive checks'
statistics and, with the output `replicated`, the replicated data, both from a second CmdStan run on a thinned set of the sampling run's draws.

THE PARTS, each streaming (nothing here holds a file, a chain or a draw):

* `kept_draws(draws, kept)` and `ThinnedLines`: while the sampling run's chains are followed, `on_line(chain, draw, line)` hands every draw line
  as CmdStan wrote it; the lines of the kept draws (⌈i·D/k⌉ for i = 1…k: evenly spaced, the last included) are appended, under the sampling
  run's header line, to `fitted_<c>.csv` (the checks' run) and `fitted_rep_<c>.csv` (the replicated run), one file a chain as CmdStan opens
  them for `fitted_params=<stem>.csv num_chains=C`. A line is never parsed again.
* `gq_layout(task, sizes, units, outcomes)`: the columns of the run's CSV, expanded from the quantity manifest's entries whose mapping `block` is
  `generated quantities`, in the manifest's order, with that run's sizes (`Ppc_on`, the bin slots, the replicated rows…): where the checks'
  columns are and, for each outcome, the replicated rows'. A header the program writes that differs is `ManifestMismatch` (exit 1).
* `write_data_file`: the data file of a run is the bytes of `data.json` before the first entry of the checks' extension (copied 8 MiB at a time,
  never parsed), then that run's own extension entries (bayes_checks.Plan.extension), then `}`.
* `plan_runs`: the runs a Job will make, built BEFORE sampling: the extension's values (a refusal of the data costs no sampling), the layouts,
  the draws a chain keeps; and `generate`, which starts CmdStan on `fitted_params` and follows its output into the sinks.

The rows of a run's output join the thinned draws by position (lot P0 measured it): row i of chain c is the i-th kept draw of chain c.
CmdStan writes NaN for every generated quantity of a draw whose generator throws: the checks' writer counts those and drops them (lot P3b's
`DrawsWriter(..., n_unchecked=len(meta), kind="checks")`), never `not_finite`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Sequence

import numpy as np

import bayes_data
import bayes_follow
from bayes_checks import EXTENSION_KINDS, Plan
from bayes_task import Element, ManifestMismatch, Task, _elements, _mapping_by_stan, task_value
from tab_protocol import Refusal

GENERATED = "generated quantities"
DEFAULT_PPC_DRAWS = 1000
COPY_CHUNK = 8 * 1024 * 1024
CHECKS, REPLICATED = "checks", "replicated"
FITTED = {CHECKS: "fitted", REPLICATED: "fitted_rep"}       # the stems of the thinned files: fitted_<c>.csv, fitted_rep_<c>.csv
OUTPUT = {CHECKS: "gq", REPLICATED: "rep"}                  # the stems of the runs' outputs: gq_<c>.csv, rep_<c>.csv
DATA_FILE = {CHECKS: "data_gq.json", REPLICATED: "data_rep.json"}


# ── the thinned draws ─────────────────────────────────────────────────────────
def kept_draws(draws: int, kept: int) -> list[int]:
    """The draws (1…`draws`) a chain keeps for `kept` of them: ⌈(i·draws)/kept⌉ for i = 1…kept, evenly spaced, the last included."""
    if not 1 <= kept <= draws:
        raise ValueError(f"cannot keep {kept} of {draws} draws")
    return [-(-(i * draws) // kept) for i in range(1, kept + 1)]


def checks_per_chain(task: Task) -> int:
    """The draws of a chain the checks' run keeps: `ppc_draws` shared over the chains (1,000 over 4: 250), at most the chain's draws."""
    s = task.sampler or {}
    chains, draws = int(s["chains"]), int(s["draws"])
    checks = task.checks if isinstance(task.checks, dict) else {}
    total = int(checks.get("ppc_draws") or DEFAULT_PPC_DRAWS)
    return max(1, min(draws, total // chains))


def replicated_per_chain(task: Task) -> int:
    return int((task.replicated or {})["draws_per_chain"])


class ThinnedLines:
    """The thinned draw lines of the sampling run, written as the follower reads them: a sink with no blocks (the follower calls
    `write_block` and `abort`) and the follower's `on_line` hook. `wanted` maps each file's stem (`fitted`, `fitted_rep`) to the draws a chain
    keeps in it; its files are `csv_paths(<workdir>/<stem>.csv, chains)`, each starting with the sampling run's header line."""

    def __init__(self, workdir: Path, header: Sequence[str], chains: int, draws: int, wanted: dict[str, int]) -> None:
        self.chains = chains
        self.keep = {stem: frozenset(kept_draws(draws, k)) for stem, k in wanted.items()}
        self.paths = {stem: bayes_follow.csv_paths(workdir / f"{stem}.csv", chains) for stem in wanted}
        self.files: dict[tuple[str, int], BinaryIO] = {}
        self.lines = {(stem, c): 0 for stem in wanted for c in range(1, chains + 1)}
        head = (",".join(header) + "\n").encode("utf-8")
        try:
            for stem, paths in self.paths.items():
                for c, path in enumerate(paths, start=1):
                    f = open(path, "wb")
                    self.files[(stem, c)] = f
                    f.write(head)
        except BaseException:
            self.abort()
            raise

    def on_line(self, chain: int, draw: int, line: bytes) -> None:
        for stem, keep in self.keep.items():
            if draw in keep:
                self.files[(stem, chain)].write(line + b"\n")
                self.lines[(stem, chain)] += 1

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None:
        """The blocks go to the other sinks: this one keeps the lines."""

    def close(self) -> None:
        for f in self.files.values():
            f.close()
        for (stem, c), n in self.lines.items():
            if n != len(self.keep[stem]):
                raise ValueError(f"{stem}_{c} holds {n} draws, not {len(self.keep[stem])}")

    def abort(self) -> None:
        for f in self.files.values():
            try:
                f.close()
            except OSError:
                pass
        for paths in self.paths.values():
            for path in paths:
                path.unlink(missing_ok=True)

    def remove(self, stem: str) -> None:
        """The files of one run, once it has ended."""
        for path in self.paths[stem]:
            path.unlink(missing_ok=True)


# ── the run's columns ─────────────────────────────────────────────────────────
@dataclass
class GqLayout:
    """The CSV of a `generate_quantities` run: its header, the checks' columns (the statistics, the bin counts, the unit sums) and, for each
    outcome, the columns of its replicated rows."""
    header: list[str]
    check_columns: np.ndarray
    checks: list[Element]
    replicated: list[tuple[str, np.ndarray]] = field(default_factory=list)   # (the outcome's Stan identifier, the CSV's columns of its rows)

    @property
    def n_columns(self) -> int:
        return len(self.header)

    @property
    def replicated_rows(self) -> int:
        return sum(len(c) for _, c in self.replicated)

    def check_header(self, header: list[str]) -> None:
        """The CSV's own header against the expansion, name for name and in order."""
        if list(header) != self.header:
            first = next((i for i, (a, b) in enumerate(zip(header, self.header)) if a != b), min(len(header), len(self.header)))
            raise ManifestMismatch(
                f"a program and its manifest disagree: the generate_quantities CSV's header has {len(header)} columns and the manifest "
                f"expects {len(self.header)}; the first difference is at column {first + 1} "
                f"(«{header[first] if first < len(header) else ''}» against «{self.header[first] if first < len(self.header) else ''}»)")


def gq_layout(task: Task, sizes: dict[str, int], units: dict[str, list[str]], outcomes: dict[str, str]) -> GqLayout:
    """Expands the quantity manifest's generated quantities with the sizes of one run. `outcomes` maps a checked outcome's data node to its
    Stan identifier (the `outcome` column of the replicated output). The replicated rows' columns are the check `replicated` of each
    outcome; the other checks go to the checks file."""
    mapping = _mapping_by_stan(task)
    header: list[str] = []
    check_cols: list[int] = []
    checks: list[Element] = []
    replicated: list[tuple[str, np.ndarray]] = []
    for q in task.quantities:
        if mapping.get(q["stan"], {}).get("block") != GENERATED:
            continue
        elements = _elements(q, sizes, units, task)
        first = len(header)
        header.extend(name for name, _ in elements)
        if q.get("role") != "check":
            continue
        if q.get("check") == "replicated":
            if elements:
                node = q.get("of")
                if node not in outcomes:
                    raise task_value(f"the replicated rows {q['stan']} name no checked outcome")
                replicated.append((outcomes[node], np.arange(first, first + len(elements), dtype=np.int64)))
            continue
        for col, (_, element) in zip(range(first, first + len(elements)), elements):
            if element is not None:
                check_cols.append(col)
                checks.append(element)
    return GqLayout(header, np.asarray(check_cols, dtype=np.int64), checks, replicated)


# ── the data file ─────────────────────────────────────────────────────────────
def write_data_file(source: Path, offset: int, entries: Sequence[dict[str, Any]], values: dict[str, Any], target: Path) -> int:
    """The data file of a run: the bytes of `source` (the sampling run's data.json) before `offset`, which end with the separator that precedes
    the first entry of the extension, then each entry of `entries` with its value in `values` (a number, a list, or a bayes_checks.Stream
    written in pieces), then `}`. Returns the bytes written."""
    total = 0
    with open(source, "rb") as src, open(target, "wb") as out:
        left = offset
        while left > 0:
            chunk = src.read(min(COPY_CHUNK, left))
            if not chunk:
                raise ValueError("the data file is shorter than the offset of the checks' data")
            out.write(chunk)
            total += len(chunk)
            left -= len(chunk)
        for i, e in enumerate(entries):
            stan = e["stan"]
            head = (b",\n" if i else b"") + f'"{stan}": '.encode("ascii")
            out.write(head)
            total += len(head)
            v = values[stan]
            if hasattr(v, "pieces"):
                out.write(b"[")
                total += 1
                for piece in v.pieces({}):
                    out.write(piece)
                    total += len(piece)
                out.write(b"]")
                total += 1
            else:
                text = (bayes_data._array_json(v) if isinstance(v, (list, tuple, np.ndarray)) else bayes_data._scalar_json(v)).encode("ascii")
                out.write(text)
                total += len(text)
        out.write(b"}\n")
        total += 2
    return total


# ── the runs of a Job ─────────────────────────────────────────────────────────
@dataclass
class GqRun:
    """One `generate_quantities` run, known before sampling starts."""
    name: str                       # CHECKS or REPLICATED
    per_chain: int                  # the thinned draws a chain keeps, and the run's rows a chain
    layout: GqLayout
    values: dict[str, Any]          # the extension's data of the run (bayes_checks.Plan.extension)
    entries: list[dict[str, Any]]   # the extension's entries, in the manifest's order

    @property
    def fitted_stem(self) -> str:
        return FITTED[self.name]

    @property
    def output_stem(self) -> str:
        return OUTPUT[self.name]

    @property
    def data_name(self) -> str:
        return DATA_FILE[self.name]


def plan_runs(task: Task, plan: Plan, facts: bayes_data.Facts) -> list[GqRun]:
    """The `generate_quantities` runs of a sampling Job, in order: the checks' (the program holds the checks and the node turned them on)
    then the replicated (the output). A version without the extension has none; asking it for replicated data is the gateway's fault. A
    refusal here (`data_shape`: fewer than two rows to check) costs no sampling."""
    if not plan.has_extension:
        if task.output == "replicated":
            raise task_value("the replicated output needs a program with the predictive checks' extension")
        return []
    wanted = []
    if plan.observing:
        wanted.append((CHECKS, checks_per_chain(task)))
    if task.output == "replicated":
        wanted.append((REPLICATED, replicated_per_chain(task)))
    if not wanted:
        return []
    # the draws are read back by CmdStan from its CSV, written to 8 significant digits, and a constrained type of the program
    # (the correlated effects' cholesky_factor_corr) checks its value then: the manifest says so, and the run is refused before
    # the sampling (the owner's decision of 2026-11-24; the gateway keeps the checks off and refuses the replicated output)
    for q in task.quantities:
        if q.get("gq_reread") is False:
            kind = str(q.get("gq_reread_type") or "constrained type")
            what = "the replicated data" if name_of(wanted) == REPLICATED else "the predictive checks"
            raise Refusal("gq_reread", f"{what} cannot be made for this model: it has correlated effects, and Stan reads the draws back with a check "
                          f"on their correlations ({kind}) that the 8 digits the sampler writes cannot meet; run it without them",
                          {"type": kind, "what": what})
    extension = [e for e in task.data if e.get("kind") in EXTENSION_KINDS]
    first = next(i for i, e in enumerate(task.data) if e.get("kind") in EXTENSION_KINDS)
    if any(e.get("kind") not in EXTENSION_KINDS for e in task.data[first:]):
        raise task_value("the checks' data are not the last entries of the data manifest")
    outcomes = {o.node: o.stan for o in plan.outcomes}
    most = task.limit("max_check_columns")
    runs = []
    for name, per_chain in wanted:
        run_sizes, values = plan.extension(name, facts)
        layout = gq_layout(task, {**facts.sizes, **run_sizes}, facts.units, outcomes)
        if len(layout.checks) > most:
            raise task_value(f"the checks hold {len(layout.checks)} columns, over the {most} allowed")
        if name == REPLICATED and not layout.replicated:
            raise task_value("the program writes no replicated rows")
        runs.append(GqRun(name, per_chain, layout, values, extension))
    return runs


def name_of(wanted: Sequence[tuple[str, int]]) -> str:
    """The run the refusal names: the replicated data when it is wanted, else the checks."""
    return REPLICATED if any(n == REPLICATED for n, _ in wanted) else CHECKS


def thinned_wanted(runs: Sequence[GqRun]) -> dict[str, int]:
    """What the thinned-lines sink keeps: each run's file stem and the draws a chain keeps in it."""
    return {r.fitted_stem: r.per_chain for r in runs}


def generate(binary: str | Path, sampler: dict[str, Any], run: GqRun, workdir: Path, data: Path, block_values: int, *sinks: Any,
             **hooks: Any) -> list[bayes_follow.ChainFacts]:
    """One run followed to its end: CmdStan on `<workdir>/<fitted stem>.csv` (`num_chains` = the sampling run's), its output
    `<workdir>/<output stem>.csv` read as it is written, every block handed to `sinks` (opened by the caller with
    `bayes_follow.block_rows(block_values, run.layout.n_columns)` draws a block, closed by it afterwards). A refusal stops CmdStan and aborts
    the sinks (Follower.follow)."""
    chains = int(sampler["chains"])
    out = workdir / f"{run.output_stem}.csv"
    args = bayes_follow.cmdstan_args(binary, sampler, "generate_quantities", data, out, 1, fitted=workdir / f"{run.fitted_stem}.csv")
    process = bayes_follow.start(args, workdir)
    follower = bayes_follow.Follower(paths=bayes_follow.csv_paths(out, chains), header=run.layout.header, draws=run.per_chain,
                                     block_values=block_values, process=process, stdout=workdir / "stdout.txt",
                                     stderr=workdir / "stderr.txt", check_header=run.layout.check_header, **hooks)
    return follower.follow(*sinks)


def remove_files(workdir: Path, run: GqRun, chains: int) -> None:
    """What a run leaves under the workdir once its rows are written: its data file and its output files."""
    (workdir / run.data_name).unlink(missing_ok=True)
    for path in bayes_follow.csv_paths(workdir / f"{run.output_stem}.csv", chains):
        path.unlink(missing_ok=True)

