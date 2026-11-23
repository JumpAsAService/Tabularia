"""The dispatcher of the group «bayes» (docs/specs/bayes-contract.md, «The modes»): `run.py` hands over here.

`python -I -B /tabularia/code/runner/run.py` reads task.json beside the runner (the group's own reader, bayes_task: `tab_protocol.MODES`
stays the other groups'), and runs the mode it names; `--selftest` runs the image's self-test (lot B0). A refusal is written as
`artifacts/error.json` and the log's TABULARIA_ERROR line, exit 3, and `/tmp/run` is deleted: a refusal leaves no partial result. Any
other exception is a bug of the runner, exit 1, the log's tail says what.

The modes after the task reader are other lots' and are loaded when they exist (a missing module is the refusal `not_built`):

  sample, prior_predictive   bayes_run.begin(task)              lot R1: the allocator, the checks' plan, the refusal of what later lots build
                             bayes_run.run(task, prepared)      lot R1: CmdStan on the verified binary and prepared.data_path, the CSVs
                                                                followed into the draws and prior files, the projections, the second pass,
                                                                the results, the card, run.json, and the copies after the last check
  compile                    bayes_compile.run(task)            lot K3
  selftest                   bayes_selftest.main(code_dir)      lot B0

The binary is fetched, hashed and compared (bayes_binary) before `prepare_run` opens the input.

`prepare_run` is this lot's part of a sampling Job: pass A of the data, the expansion of the quantity manifest with the sizes it found
(and the refusal `too_many_quantities` before anything is sampled), pass B and data.json.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tab_protocol import ENV_INPUT, EXIT_OK, EXIT_REFUSED, Refusal, catalog, code_dir, not_built, variant_spec, write_error
import bayes_binary
import bayes_data
import bayes_selftest
import bayes_task
from bayes_task import Layout, Task

WORKDIR = Path("/tmp/run")


@dataclass
class Prepared:
    """What a sampling Job has after the data passes: everything the follower needs and nothing of the input."""
    task: Task
    workdir: Path
    facts: bayes_data.Facts
    layout: Layout
    data_path: Path
    binary_path: Path | None = None
    checks: Any = None          # the checks' plan (bayes_checks.Plan) pass A observed with


def prepare_run(task: Task, workdir: Path = WORKDIR, input_path: str | None = None, batch_rows: int = bayes_data.BATCH_ROWS,
                extension_sizes: dict[str, int] | None = None, extension_values: dict[str, Any] | None = None,
                checks: Any = None, run: str = "sample") -> Prepared:
    """Passes A and B over the input (TABULARIA_INPUT unless given), the layout of the CSV, and data.json under `workdir`."""
    path = input_path if input_path is not None else os.environ.get(ENV_INPUT, "")
    if not path:
        raise Refusal("task_value", "TABULARIA_INPUT is empty: the Job was not made by Tabularia", {"what": ENV_INPUT})
    workdir.mkdir(parents=True, exist_ok=True)
    facts, state = bayes_data.pass_a(task, path, batch_rows, extension_sizes, checks, run)
    layout = bayes_task.build_layout(task, facts.sizes, facts.units)
    most = task.limit("max_quantities")
    if layout.n_quantities > most:
        raise Refusal("too_many_quantities", f"the model has {layout.n_quantities} quantities; this installation allows {most}",
                      {"quantities": layout.n_quantities, "max_quantities": most})
    data_path = bayes_data.pass_b(task, facts, state, path, workdir, batch_rows, extension_values)
    return Prepared(task, workdir, facts, layout, data_path)


def results_declaration(task: Task) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The catalogue's declaration of the variant and its section shapes, for the results writer's own check; (None, None) while the
    catalogue has no `bayes_model` (lot K0b)."""
    try:
        spec = variant_spec(bayes_task.MODEL, task.variant)
        return (spec["sections"], catalog()["section_shapes"]) if spec else (None, None)
    except (OSError, KeyError, ValueError):
        return None, None


def _stage(name: str, what: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name != name:
            raise
        raise not_built(what) from e


def run_task(task: Task, here: Path, workdir: Path = WORKDIR) -> int:
    if task.mode == "selftest":
        return bayes_selftest.main(here)
    if task.mode == "compile":
        return _stage("bayes_compile", "compiling a Bayesian model").run(task)
    stage = _stage("bayes_run", "sampling a Bayesian model")
    try:
        plan, data_run = stage.begin(task)    # the allocator first; what a later lot builds is refused before anything is fetched
        binary = bayes_binary.fetch_binary(workdir, task.model["binary_sha256"])  # the binary first: a poisoned one is refused before a row is read
        prepared = prepare_run(task, workdir, checks=plan, run=data_run)
        prepared.binary_path = binary
        prepared.checks = plan
        return stage.run(task, prepared)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)  # a refusal or a crash leaves no partial result
        raise


def main(here: Path) -> int:
    """`here` is /tabularia/code (the folder that holds runner/, common/ and the reference cases)."""
    if "--selftest" in sys.argv[1:]:
        return bayes_selftest.main(here)
    try:
        task = bayes_task.read_task(code_dir() / "task.json")
        code = run_task(task, here)
        return EXIT_OK if code is None else code
    except Refusal as e:
        write_error(e)
        return EXIT_REFUSED
