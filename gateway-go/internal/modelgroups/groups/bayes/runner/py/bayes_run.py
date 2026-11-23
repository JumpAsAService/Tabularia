"""The sampling Job of a Bayesian model, assembled (docs/specs/bayes-runner.md, «The sampling Job, in order»; lot R1): the mode `sample`
from the verified binary to the last copy.

    plan, data_run = bayes_run.begin(task)      # the allocator, the checks' plan, the refusal of what a later lot builds
    ...bayes_main fetches the binary (bayes_binary) and runs the data passes (prepare_run, with the plan)...
    bayes_run.run(task, prepared)               # the rest, below

THE ORDER, and the owner's rule that nothing large is ever held («never everything in RAM»):

1. the disk projected with the measured constants (10.3 bytes a value of CSV text, 7.75 of parquet) and the time with what the later
   stages will take, **before CmdStan starts**: the first refusal costs no sampling;
2. CmdStan (NUTS, the node's chains, warm-up, draws, seed, `adapt_delta`, `max_treedepth`, initial values, `OMP_NUM_THREADS` threads,
   `PATH` its only variable) on the binary bayes_main verified, followed WHILE the chains write their CSVs: every block of every chain
   goes to three sinks — the draws writer (`/tmp/run/draws.parquet`, one row group a block), the prior writer (`/tmp/run/prior.parquet`,
   the `prior_marginal` columns, the draws' long schema and order) and the sampler's accumulators (bayes_facts) — and is dropped. The
   time is projected at every progress line and every idle tick, the disk again after the first block of each chain with the chain's
   own bytes a draw; a projection past its limit is `too_slow` / `too_much_disk`, CmdStan is stopped and every sink aborted;
3. the CSVs deleted once CmdStan has exited and the follower has read them to their end;
4. the time projected again before the second pass, which re-projects itself after its first block and refuses before any row;
5. the second pass (bayes_pass2) by blocks of quantities: the summary file, the `summary`, `quantity` and chart rows, the contraction
   from the prior's draws (`prior.parquet`: one quantity's column read back at a time for the density chart, the variances in one blocked
   read; `bayes.finite_variance` says which priors have one — absent, every contraction is NULL with its note);
6. the remaining rows (`model`, `data`, `sampler`, the notes), the card (never a draw: bayes_card) and run.json;
7. only now the copies: `results.parquet` to TABULARIA_RESULTS, the summary or the draws file to TABULARIA_OUTPUT when the node writes an
   output, the card, MLmodel and run.json to TABULARIA_ARTIFACTS, each 8 MiB at a time. `/tmp/run` is deleted.

A refusal or a fault before step 7 leaves nothing at any address (bayes_main deletes `/tmp/run`); a failure during the copies is not a
refusal (exit 1: the gateway deletes the step's files by key).

Not here yet: the posterior checks' and the replicated `generate_quantities` runs (lot R2), the prior predictive mode (R3), the
original scale (R4). `begin` refuses them as not built, before anything is fetched.
"""
from __future__ import annotations

import math
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pyarrow.parquet as pq

import bayes_card
import bayes_checks
import bayes_draws
import bayes_facts
import bayes_follow
import bayes_main
import bayes_pass2
import bayes_rows
import tab_card
from bayes_task import Layout, ManifestMismatch, Task, report_order
from tab_card import versions
from tab_protocol import ENV_ARTIFACTS, ENV_OUTPUT, ENV_RESULTS, PROTOCOL, Refusal, artifact, not_built, threads, to_json

# Measured constants («Time and disk»): the bytes of a value of 8-digit CSV text (bayes-image) and of the long parquet (the contract, 2026-11-23,
# full doubles); the second pass's seconds a value (X5: 74 s for 80 million values); the follower's seconds a converted value (K2: 895 MB of
# CSV, about 87 million values, followed and written in 28 s, the fake sampler's own writing included).
CSV_BYTES_A_VALUE = 10.3
PARQUET_BYTES_A_VALUE = 7.75
PASS2_SECONDS_A_VALUE = 0.93e-6
CONVERT_SECONDS_A_VALUE = 0.35e-6
# Assumed until lot R6 measures them on the image: the copy's rate to the bucket, a CSV file's comments (the configuration, the adaptation's
# step size and the mass matrix's diagonal, the elapsed times), and the summary's and the results' bytes.
COPY_BYTES_A_SECOND = 50e6
CSV_HEAD_BYTES = 8192
CSV_HEAD_BYTES_A_COLUMN = 48
SUMMARY_BYTES_A_QUANTITY = 256
RESULT_BYTES_A_ROW = 128

NO_CHECKS_NOTE = "This version was saved before the predictive checks: saving it again adds them."


# ── the start ─────────────────────────────────────────────────────────────────
def begin(task: Task) -> tuple[Any, str]:
    """The start of every sampling mode: the C library's allocator for Arrow (bayes_follow.use_system_allocator, once, before any writer),
    the checks' plan, and the run whose extension values pass B writes. What a later lot builds is refused as not built here, before the
    binary is fetched or a row is read: never half run."""
    bayes_follow.use_system_allocator()
    plan = bayes_checks.plan(task)
    if task.mode == "prior_predictive":
        raise not_built("the prior predictive check")
    if task.output == "replicated":
        raise not_built("the replicated output")
    if plan.has_extension and plan.on:
        raise not_built("the posterior predictive checks' run")
    return plan, "sample"


def run(task: Task, prepared: Any) -> int:
    """The sampling Job after the data passes (bayes_main.run_task): everything from CmdStan's start to the last copy."""
    return SampleJob(task, prepared).run()


# ── the clock and the projections ─────────────────────────────────────────────
class Clock:
    """The step's clock: it started at `started_unix` (the gateway's, when the Job was made; 0 in a test: now), the budget is
    `time_budget_seconds`."""

    def __init__(self, limits: dict[str, int], now: Callable[[], float] = time.time) -> None:
        self.now = now
        started = int(limits.get("started_unix") or 0)
        self.start = float(started) if started > 0 else now()
        self.budget = float(limits["time_budget_seconds"])

    def elapsed(self) -> float:
        return max(0.0, self.now() - self.start)

    def left(self) -> float:
        return self.budget - self.elapsed()


def _minutes(seconds: float) -> int:
    return max(1, int(math.ceil(seconds / 60.0)))


def _size(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{max(n, 0) / 1e6:.1f} MB"


def tree_bytes(path: Path) -> int:
    """The bytes the files under `path` hold now (what /tmp/run already holds: the binary, data.json)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except FileNotFoundError:
                pass
    return total


class _ChainClock:
    """One chain's progress lines: the first and the latest of each phase, with the clock's time they were read."""

    def __init__(self) -> None:
        self.phase: dict[str, list[float]] = {}    # phase → [first iteration, its time, latest iteration, its time]
        self.latest: tuple[int, int, float] | None = None   # (iteration, total, time)

    def add(self, iteration: int, total: int, phase: str, t: float) -> None:
        seen = self.phase.get(phase)
        if seen is None:
            self.phase[phase] = [iteration, t, iteration, t]
        else:
            seen[2], seen[3] = iteration, t
        self.latest = (iteration, total, t)

    def rate(self, phase: str) -> float | None:
        """Seconds an iteration in `phase`, between its first and its latest line; None before two lines."""
        seen = self.phase.get(phase)
        if seen is None or seen[2] <= seen[0]:
            return None
        return (seen[3] - seen[1]) / (seen[2] - seen[0])


class Watch:
    """The projections of a sampling run (the follower's hooks): the time left at every progress line and idle tick, the disk before
    sampling and after the first block of each chain. Arithmetic on what was seen; a projection past its limit is a refusal."""

    def __init__(self, job: "SampleJob") -> None:
        self.job = job
        self.clock = job.clock
        s = job.task.sampler or {}
        self.chains, self.warmup, self.draws = int(s["chains"]), int(s["warmup"]), int(s["draws"])
        self.threads = job.threads
        self.columns = job.layout.n_columns
        self.chain_clock = {c: _ChainClock() for c in range(1, self.chains + 1)}
        self.csv_draw_bytes: dict[int, float] = {}
        self.converted_draws = 0
        self.used = 0
        self.last_line: float | None = None      # time.monotonic() of the latest progress line: the sampler's last iteration at the end

    # the time
    def sampling_seconds(self) -> float:
        """The sampling still to come: for each chain that has two progress lines of a phase, the iterations left at its own rate
        (warm-up and sampling apart once both are seen), less the time since its latest line; the slowest chain. Chains not yet started
        when the threads are fewer than the chains run in later waves at the slowest rate seen."""
        now = self.clock.now()
        rates_s = [r for c in self.chain_clock.values() if (r := c.rate("sampling")) is not None]
        any_sampling = max(rates_s) if rates_s else None
        worst, slowest_rate, seen = 0.0, 0.0, 0
        for c in self.chain_clock.values():
            if c.latest is None:
                continue
            seen += 1
            iteration, total, t = c.latest
            rate_w, rate_s = c.rate("warmup"), c.rate("sampling") or any_sampling
            if "sampling" in c.phase:
                rate = rate_s if rate_s is not None else rate_w
                if rate is None:
                    continue
                left = (total - iteration) * rate
            else:
                if rate_w is None:
                    continue
                warm_total = total - self.draws
                left = max(0, warm_total - iteration) * rate_w + self.draws * (rate_s if rate_s is not None else rate_w)
                rate = rate_w
            slowest_rate = max(slowest_rate, rate)
            worst = max(worst, left - (now - t))
        unseen = self.chains - seen
        if unseen > 0 and seen > 0 and self.threads < self.chains and slowest_rate > 0:
            worst += math.ceil(unseen / seen) * (self.warmup + self.draws) * slowest_rate
        return max(0.0, worst)

    def tail_seconds(self) -> float:
        """The conversion's tail after CmdStan exits: at most one block a chain still to parse and write."""
        rows = self.job.block_rows
        return self.chains * rows * self.columns * CONVERT_SECONDS_A_VALUE

    def check_time(self) -> None:
        left = self.clock.left()
        later = self.job.later_seconds()
        ahead = self.sampling_seconds() + self.tail_seconds() + later
        if left <= 0 or ahead > left:
            self.job.too_slow(self.clock.elapsed() + ahead)

    def on_progress(self, p: bayes_follow.Progress) -> None:
        self.last_line = time.monotonic()
        clock = self.chain_clock.get(p.chain)
        if clock is not None:
            clock.add(p.iteration, p.total, p.phase, self.clock.now())
        self.check_time()

    def on_idle(self) -> None:
        self.check_time()

    # the disk
    def disk_bytes(self) -> float:
        layout = self.job.layout
        c, d = self.chains, self.draws
        line = self.columns * CSV_BYTES_A_VALUE
        csv = sum(d * self.csv_draw_bytes.get(k, line) for k in range(1, c + 1)) + c * (CSV_HEAD_BYTES + self.columns * CSV_HEAD_BYTES_A_COLUMN)
        parquet = c * d * (len(layout.draws) + len(layout.marginals)) * PARQUET_BYTES_A_VALUE
        summary = layout.n_quantities * SUMMARY_BYTES_A_QUANTITY
        results = self.job.task.limit("max_result_rows") * RESULT_BYTES_A_ROW + self.job.task.limit("max_card_bytes")
        return self.used + csv + parquet + summary + results

    def check_disk(self) -> None:
        most = self.job.task.limit("tmp_bytes")
        need = self.disk_bytes()
        if need > most:
            raise Refusal("too_much_disk", f"the draws would write about {_size(need)}; this Job's disk holds {_size(most)}: fewer draws or "
                                           "chains", {"projected_bytes": int(need), "tmp_bytes": most})

    def on_block(self, info: bayes_follow.BlockInfo) -> None:
        self.converted_draws += info.rows
        if info.block == 1 and info.chain_draws:
            self.csv_draw_bytes[info.chain] = info.chain_draw_bytes / info.chain_draws
            self.check_disk()


# ── the prior's draws ─────────────────────────────────────────────────────────
class PriorDraws:
    """`/tmp/run/prior.parquet` read back for the second pass: a quantity's prior marginal (its node's: a plate's quantities share it), one
    column at a time for the density chart (at most S doubles), and every marginal's variance computed once, by blocks of marginals within
    `pass_bytes`. `finite` is the task's `bayes.finite_variance` (None: not decided, every contraction NULL with its note)."""

    def __init__(self, path: Path | None, layout: Layout, finite: frozenset[str] | None, budget: bayes_pass2.Budget) -> None:
        self.path = path
        self.layout = layout
        self.finite = finite
        self.entry_of = bayes_pass2._entry_index(layout)
        self.index = {m.node: i for i, m in enumerate(layout.marginals)}
        self.variances: dict[str, float | str] = {}
        self.groups: list[tuple[int, int, int]] = []
        if path is None or not layout.marginals:
            return
        meta = bayes_draws.read_metadata(path)
        self.chains, self.draws = int(meta["chains"]), int(meta["draws"])
        self.groups = [tuple(int(v) for v in g) for g in meta["row_groups"]]
        self.file = pq.ParquetFile(str(path))
        if finite is None:
            return
        m_all = len(layout.marginals)
        per_block = max(1, budget.pass_bytes // (8 * self.chains * self.draws))
        for m0 in range(0, m_all, per_block):
            m1 = min(m_all, m0 + per_block)
            block = self._read(m0, m1)
            for j in range(m1 - m0):
                of = layout.marginals[m0 + j].node
                self.variances[of] = bayes_pass2.prior_variance_of(block[j].reshape(-1), of in finite)
            del block

    def _read(self, m0: int, m1: int) -> np.ndarray:
        m_all = len(self.layout.marginals)
        out = np.empty((m1 - m0, self.chains, self.draws))
        for i, (chain, first_draw, count) in enumerate(self.groups):
            values = self.file.read_row_group(i, columns=["value"]).column(0).to_numpy()
            if values.shape[0] != m_all * count:
                raise ManifestMismatch(f"the prior's row group {i + 1} holds {values.shape[0]} values, not {m_all} × {count}")
            out[:, chain - 1, first_draw - 1:first_draw - 1 + count] = values[m0 * count:m1 * count].reshape(m1 - m0, count)
            del values
        return out

    def _of(self, position: int) -> str | None:
        k = int(self.entry_of[position])
        return self.layout.entries[k].node if k >= 0 else None

    def draws_at(self, position: int) -> np.ndarray | None:
        """The prior marginal's finite draws of the quantity at `position` of `layout.draws` (None: the run has none)."""
        of = self._of(position)
        i = self.index.get(of) if of is not None else None
        if i is None or not self.groups:
            return None
        x = self._read(i, i + 1).reshape(-1)
        return x[np.isfinite(x)]

    def variance_at(self, position: int) -> float | str:
        if self.finite is None:
            return bayes_pass2.NOT_DECIDED
        of = self._of(position)
        if of is None or of not in self.finite:
            return bayes_pass2.NO_FINITE_VARIANCE
        return self.variances.get(of, bayes_pass2.NO_PRIOR_DRAWS)


# ── the Job ───────────────────────────────────────────────────────────────────
class SampleJob:
    def __init__(self, task: Task, prepared: Any, clock: Clock | None = None) -> None:
        self.task = task
        self.prepared = prepared
        self.layout: Layout = prepared.layout
        self.facts = prepared.facts
        self.workdir = Path(prepared.workdir)
        self.clock = clock or Clock(task.limits)
        self.threads = threads(task.raw)
        s = task.sampler or {}
        self.chains, self.draws = int(s["chains"]), int(s["draws"])
        self.block_rows = bayes_follow.block_rows(task.limit("block_values"), self.layout.n_columns)
        self.seconds: dict[str, float] = {}

    # the stages after sampling, as projected before they start
    def pass2_seconds(self) -> float:
        return self.chains * self.draws * self.layout.n_quantities * PASS2_SECONDS_A_VALUE

    def copy_seconds(self) -> float:
        out = 0.0
        if self.task.write_output and self.task.output == "draws":
            out += self.chains * self.draws * len(self.layout.draws) * PARQUET_BYTES_A_VALUE
        elif self.task.write_output and self.task.output == "summary":
            out += self.layout.n_quantities * SUMMARY_BYTES_A_QUANTITY
        out += self.task.limit("max_result_rows") * RESULT_BYTES_A_ROW
        return out / COPY_BYTES_A_SECOND

    def later_seconds(self) -> float:
        return self.pass2_seconds() + self.copy_seconds()

    def too_slow(self, projected: float) -> None:
        s = self.task.sampler or {}
        budget = self.clock.budget
        raise Refusal("too_slow", f"about {_minutes(projected)} minutes for {s['chains']} chains of {int(s['warmup']) + int(s['draws'])} "
                                  f"iterations; the node's timeout is {_minutes(budget)} minutes: fewer draws, or a longer timeout",
                      {"projected_seconds": int(projected), "time_budget_seconds": int(budget)})

    def run(self) -> int:
        t_job = time.monotonic()
        task, layout, work = self.task, self.layout, self.workdir
        results_to = os.environ.get(ENV_RESULTS, "")
        if not results_to:
            raise Refusal("task_value", f"{ENV_RESULTS} is empty: the Job was not made by Tabularia", {"what": ENV_RESULTS})
        output_to = os.environ.get(ENV_OUTPUT, "")
        writes_output = task.write_output and task.output in ("summary", "draws")
        if writes_output and not output_to:
            raise Refusal("task_value", f"{ENV_OUTPUT} is empty: the Job was not made by Tabularia", {"what": ENV_OUTPUT})

        # 1. the projections before CmdStan starts
        watch = Watch(self)
        watch.used = tree_bytes(work)
        watch.check_disk()
        watch.check_time()

        # 2. the sampling run, followed into its sinks
        s = task.sampler or {}
        info = {"program_sha256": task.model.get("program_sha256"), "semantic_hash": task.model.get("semantic_hash"),
                "seed": s.get("seed"), "sampler": {k: s.get(k) for k in ("chains", "warmup", "draws", "adapt_delta", "max_treedepth")}}
        draws_path, prior_path = work / "draws.parquet", work / "prior.parquet"
        draws_w = bayes_draws.DrawsWriter(draws_path, layout.draws, layout.draw_columns, self.chains, self.draws, self.block_rows, info,
                                          n_unchecked=layout.n_sampler)
        sinks: list[Any] = [draws_w]
        prior_w = None
        if layout.marginals:
            # a prior marginal is a generated quantity, not a sampled one: a draw that is not finite is left out of its variance and its
            # density, never a refusal of the run (not_finite is for the program's sampled quantities)
            prior_w = bayes_draws.DrawsWriter(prior_path, layout.marginals, layout.marginal_columns, self.chains, self.draws, self.block_rows,
                                              info, n_unchecked=len(layout.marginals), kind="prior")
            sinks.append(prior_w)
        acc = bayes_facts.FactsAccumulator(layout.header, self.chains, s.get("max_treedepth"))
        sinks.append(acc)
        chain_facts = bayes_follow.sample(self.prepared.binary_path, s, task.algorithm, layout.header, task.limit("block_values"), work,
                                          self.prepared.data_path, self.threads, *sinks, check_header=layout.check_header,
                                          on_progress=watch.on_progress, on_idle=watch.on_idle, on_block=watch.on_block)
        t_followed = time.monotonic()
        draws_w.close()
        if prior_w is not None:
            prior_w.close()
        # 3. CmdStan has exited and every file was read to its end: the CSVs go before the second pass
        for path in bayes_follow.csv_paths(work / "out.csv", self.chains):
            path.unlink(missing_ok=True)
        warm = [c.seconds_warmup for c in chain_facts if c.seconds_warmup is not None]
        samp = [c.seconds_sampling for c in chain_facts if c.seconds_sampling is not None]
        self.seconds["warmup"] = max(warm) if warm else 0.0
        self.seconds["sampling"] = max(samp) if samp else 0.0
        # the conversion's tail: from the sampler's last progress line to the last block written and the files closed (the rest of the
        # conversion ran while the chains sampled)
        self.seconds["conversion"] = max(0.0, time.monotonic() - (watch.last_line if watch.last_line is not None else t_followed))

        # 4. the time before the second pass
        left = self.clock.left()
        if left <= 0 or self.later_seconds() > left:
            self.too_slow(self.clock.elapsed() + self.later_seconds())

        # 5. the second pass, with the prior's draws
        decl, shapes = bayes_main.results_declaration(task)
        results_path = work / "results.parquet"
        w = bayes_rows.ResultsWriter(str(results_path), max_rows=task.limit("max_result_rows"), declaration=decl, shapes=shapes)
        try:
            entries = bayes_rows.quantity_section(w, task, layout)
            budget = bayes_pass2.Budget.of(task.limits)
            prior = PriorDraws(prior_path if prior_w is not None else None, layout, task.finite_variance, budget)
            summary_path = work / "summary.parquet"
            report = bayes_pass2.second_pass(str(draws_path), layout, task.limits, w, summary_path=str(summary_path), budget=budget,
                                             diagnostics=True, prior=prior.draws_at, seed=int(s.get("seed") or 0),
                                             seconds_left=self.clock.left() - self.copy_seconds(), quantity_entries=entries,
                                             prior_variance=prior.variance_at)
            self.seconds["pass_2"] = report.seconds
            if prior_w is not None:
                prior_path.unlink(missing_ok=True)

            # 6. the rows that remain, the card, run.json
            bayes_rows.model_section(w, task, self.facts, layout, self.threads, self.seconds)
            plan = getattr(self.prepared, "checks", None)
            if plan is None or not plan.has_extension:
                w.note("no_checks", NO_CHECKS_NOTE)
            bayes_rows.data_section(w, task, self.facts)
            sampler_facts = acc.facts({c.chain: c.step_size for c in chain_facts})
            bayes_pass2.sampler_section(w, sampler_facts, {c.chain: (c.seconds_warmup, c.seconds_sampling) for c in chain_facts})
            every = sampler_facts["all"]
            card = bayes_card.build(task, self.facts, layout,
                                    {"divergences": every.get("divergences") or 0, "treedepth_hits": every.get("treedepth_hits") or 0,
                                     "e_bfmi_min": every.get("e_bfmi")},
                                    card_summary(summary_path, layout, task.limit("bayes_max_summary_quantities")),
                                    int(task.model.get("program_bytes") or 0), str(task.model.get("image") or ""))
            card_text = bayes_card.fit(task, card, w)
            rows = w.close()
        except BaseException:
            w.abort()
            raise
        artifacts = work / "artifacts"
        artifacts.mkdir(exist_ok=True)
        if card_text is not None:
            (artifacts / "model.json").write_text(card_text, encoding="utf-8")
            if task.raw.get("mlflow"):
                (artifacts / "MLmodel").write_text(tab_card.mlmodel(card), encoding="utf-8")
        self.seconds["data"] = self.facts.seconds_pass_a + self.facts.seconds_pass_b
        self.seconds["job"] = time.monotonic() - t_job
        run_json = {"protocol": PROTOCOL, "group": "bayes", "model": task.raw.get("model"), "mode": task.mode, "exit": 0,
                    "software": versions(("numpy", "pyarrow")), "seconds": round(self.seconds["job"], 3),
                    "phases": {k: round(v, 3) for k, v in self.seconds.items() if k != "job"}, "rows": rows,
                    "draws_bytes": draws_path.stat().st_size, "warnings": []}
        (artifacts / "run.json").write_text(to_json(run_json), encoding="utf-8")

        # 7. the copies, only now
        bayes_draws.copy_in_chunks(results_path, results_to)
        if writes_output:
            bayes_draws.copy_in_chunks(summary_path if task.output == "summary" else draws_path, output_to)
        if os.environ.get(ENV_ARTIFACTS, ""):
            for name in ("model.json", "MLmodel", "run.json"):
                if (artifacts / name).exists():
                    bayes_draws.copy_in_chunks(artifacts / name, artifact(name))
        shutil.rmtree(work, ignore_errors=True)
        return 0


def card_summary(summary_path: Path, layout: Layout, cap: int) -> list[dict[str, Any]]:
    """The card's summary: the scalar quantities' rows (the report's first ones, at most `cap`), read back from the summary file a
    row group at a time (bayes_card.fit cuts them first when the card is too large)."""
    wanted: dict[str, int] = {}
    for e in report_order(layout.entries):
        if not e.scalar:
            continue
        wanted[layout.draws[e.first].quantity] = len(wanted)
        if len(wanted) >= cap:
            break
    if not wanted:
        return []
    found: dict[str, dict[str, Any]] = {}
    names = ("quantity", "mean", "sd", "q5", "q50", "q95", "rhat", "ess_bulk", "ess_tail")
    f = pq.ParquetFile(str(summary_path))
    for i in range(f.num_row_groups):
        t = f.read_row_group(i, columns=list(names)).to_pydict()
        for j, q in enumerate(t["quantity"]):
            if q in wanted:
                found[q] = {n: t[n][j] for n in names}
    return [found[q] for q in sorted(found, key=wanted.__getitem__)]
