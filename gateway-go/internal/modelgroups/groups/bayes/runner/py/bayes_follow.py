"""The follower of CmdStan's CSV files while the chains write them (docs/specs/bayes-contract.md, «The owner's rule», «Following
the CSV files while the chains run»; lot K2).

CmdStan writes one CSV file a chain and its progress to standard output; the runner starts it and does not wait for it. This
module reads those files **while they grow**, from an offset kept per file, and hands the draws on **one block of one chain at a
time**, in a fixed order — block 1 of chains 1…C, then block 2 of chains 1…C, … — so that the draws parquet built from the blocks
is the same file, byte for byte, whatever the chains' speeds. Nothing here holds a whole file or a whole chain:

- a file is read with `os.pread` from its offset, at most `read_bytes` (1 MiB) at a time, and no byte is read twice (the
  offset only grows; `bytes_read` of every chain equals its file's size at the end);
- a line cut by a read (CmdStan's stream is flushed in pieces) is kept until its end arrives, never parsed half;
- the comments are parsed for the few numbers kept (the step size after the adaptation, the elapsed times) and dropped;
- a block's draw lines are parsed one by one into a preallocated float matrix (B × columns): its text is never held whole;
- a chain faster than the others waits **on disk**: only the chain whose turn it is is read, and a read is sized to the lines
  its block still needs (at least 64 KiB), so the lines in memory are the block being filled and one small read beyond it;
- after the last block the files' tails (the elapsed times) are read and CmdStan's exit is checked.

The block is handed to each sink (`write_block(chain, first_draw, block)`: the draws writer and the checks writer of bayes_draws,
which take what they need of the CSV's columns, check that the program's quantities are finite — `not_finite` — and append one row
group) and is not referenced here afterwards. Any refusal or fault stops CmdStan (terminate, kill after the grace) and calls every
sink's `abort()`, so that a refused step leaves no partial result; the caller closes the sinks after `follow` returns and deletes
/tmp/run either way.

The format followed is CmdStan 2.40.0's, measured on the image (lot K0 and this lot): one chain writes `out.csv`, C ≥ 2 chains
`out_1.csv` … `out_C.csv`; the header is the sampler's fields then the program's columns (`name.i.j`, column-major); a draw is
written with 8 significant digits, `nan`, `inf` and `-inf` as text; the comments after the header are «# Adaptation
terminated», «# Step size = …», «# Diagonal elements of inverse mass matrix:» and the diagonal, and at the end «#  Elapsed Time:
… seconds (Warm-up)», «#                … seconds (Sampling)», «… (Total)»; a chain that cannot start leaves its file with the
configuration's comments alone and CmdStan exits 1 with «Initialization failed.» on standard error; progress reads «Chain [k]
Iteration: i / n [ p%]  (Warmup|Sampling)», without «Chain [k] » with one chain.

`sample` starts CmdStan with the fixed argument list («The CSV files») and follows it; the sampling Job around it (the binary, the
sinks, the projections, the second pass, the results, the card, the copies) is bayes_run's (lot R1 of bayes-runner).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

import numpy as np
from tab_protocol import Refusal

# The sampler's fields CmdStan writes first on every draw line: NUTS, and fixed_param (the prior predictive).
SAMPLER_NUTS = ("lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__")
SAMPLER_FIXED_PARAM = ("lp__", "accept_stat__")

READ_BYTES = 1 << 20          # one read of a file, at most (the spec: «os.read, at most 1 MiB at a time»)
MIN_READ_BYTES = 1 << 16      # a read sized to the lines a block still needs is never smaller than this
POLL_SECONDS = 0.05           # the sleep when nothing new has arrived
STOP_GRACE_SECONDS = 5.0      # terminate, then kill after this
STDOUT_LINE_CAP = 1 << 16     # a progress line is short: a longer unfinished line of stdout is dropped, never grown
STDERR_TAIL_BYTES = 8192      # the end of stderr.txt read for a refusal's sentence
STDERR_TAIL_LINES = 20

_PROGRESS = re.compile(rb"^(?:Chain \[(\d+)\] )?Iteration:\s*(\d+)\s*/\s*(\d+)\s*\[\s*\d+%\]\s*\((Warmup|Sampling)\)")
_STEP_SIZE = re.compile(rb"^#\s*Step size\s*=\s*(\S+)")
_ELAPSED = re.compile(rb"^#\s*(?:Elapsed Time:\s*)?(\S+)\s+seconds\s+\((Warm-up|Sampling|Total)\)")
_INIT_FAILED = "Initialization failed"
# CmdStan's environment: PATH alone (bayes-image's contract, as bayes_cmdstan runs it)
CMDSTAN_PATH = "/usr/local/bin:/usr/bin:/bin"


class ManifestMismatch(RuntimeError):
    """The CSV's header differs from the one the manifest expects: a fault of Tabularia (the program and its manifest disagree),
    never a person's error — exit 1, not a refusal."""


class Sink(Protocol):
    """Where the blocks go (bayes_draws.DrawsWriter, ReplicatedWriter): `block` is draws × the CSV's columns, in the header's
    order, of chain `chain`, draws `first_draw` … `first_draw + len(block) - 1`; the sink appends what it needs as one row group
    and keeps no reference to it. `abort()` closes and deletes its file."""

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None: ...

    def abort(self) -> None: ...


@dataclass
class Progress:
    """One progress line of CmdStan: chain (1 with a single chain), iteration i of n, phase `warmup` or `sampling`."""
    chain: int
    iteration: int
    total: int
    phase: str


@dataclass
class BlockInfo:
    """What a block was, for the runner's projections (the disk after the first block of each chain): its chain, its number
    (1…), its draws, and the chain's draw lines so far — their count and their bytes (a draw line's size × the draws left is the
    rest of the chain's file)."""
    chain: int
    block: int
    first_draw: int
    rows: int
    chain_draws: int
    chain_draw_bytes: int


@dataclass
class ChainFacts:
    """What the follower read of one chain, besides its draws: the step size after the adaptation (None under fixed_param), the
    elapsed times CmdStan wrote at the end (None when absent), the draws, the bytes read (equal to the file's size at the end)."""
    chain: int
    path: str
    draws: int = 0
    step_size: float | None = None
    seconds_warmup: float | None = None
    seconds_sampling: float | None = None
    bytes_read: int = 0
    draw_bytes: int = 0


def csv_paths(out: str | Path, chains: int) -> list[Path]:
    """The files CmdStan writes for `output file=<out>`: `out` itself with one chain, `<stem>_<k><suffix>` for k = 1…C with
    more (measured on CmdStan 2.40.0)."""
    out = Path(out)
    if chains == 1:
        return [out]
    return [out.with_name(f"{out.stem}_{k}{out.suffix}") for k in range(1, chains + 1)]


def block_rows(block_values: int, columns: int) -> int:
    """B, the draws of a block: max(1, floor(block_values / columns)) («The block»)."""
    if block_values < 1 or columns < 1:
        raise ValueError("block_values and columns must be positive")
    return max(1, block_values // columns)


def sampler_failed(reason: str, chain: int | None, detail: str, tail: str = "") -> Refusal:
    params: dict[str, Any] = {"reason": reason, "chain": chain}
    if tail:
        params["log"] = tail
    where = f"chain {chain}" if chain is not None else "the sampler"
    return Refusal("sampler_failed", f"the sampler stopped before its draws were complete ({where}: {detail}); nothing was "
                   "kept. The log's last lines say what CmdStan reported", params)


class _ChainFile:
    """One chain's CSV file, read from its offset. Lines read beyond what the current block needs wait in `ready` (at most one
    read's worth); a line without its end waits in `rest`."""

    def __init__(self, chain: int, path: Path, check_header: Callable[[list[str]], None], draws: int, read_bytes: int):
        self.chain = chain
        self.path = path
        self.check_header = check_header
        self.draws_expected = draws
        self.read_bytes = read_bytes
        self.fd: int | None = None
        self.offset = 0
        self.rest = b""
        self.ready: deque[bytes] = deque()
        self.header_seen = False
        self.draws_read = 0          # draw lines accepted from the file so far
        self.facts = ChainFacts(chain=chain, path=str(path))

    def open(self) -> bool:
        if self.fd is None:
            try:
                self.fd = os.open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                return False
        return True

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read_more(self, lines_wanted: int = 0) -> bool:
        """Reads the next bytes from the offset and sorts their complete lines; False when nothing new has arrived (or the file
        does not exist yet). A read takes at most `read_bytes`, and no more than the `lines_wanted` draw lines the current block
        still needs are likely to take (their mean size so far, at least MIN_READ_BYTES): what is read beyond the block waits in
        memory until the chain's next turn, so it is kept to about one small read."""
        if not self.open():
            return False
        size = os.fstat(self.fd).st_size
        if size < self.offset:
            raise sampler_failed("truncated", self.chain, f"its file shrank from {self.offset} to {size} bytes while it was read")
        if size == self.offset:
            return False
        n = self.read_bytes
        if lines_wanted:
            line = -(-self.facts.draw_bytes // self.facts.draws) if self.facts.draws else 0
            n = min(n, max(MIN_READ_BYTES, lines_wanted * line))
        chunk = os.pread(self.fd, min(n, size - self.offset), self.offset)
        if not chunk:
            return False
        self.offset += len(chunk)
        self.facts.bytes_read += len(chunk)
        data = self.rest + chunk if self.rest else chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self.rest = data
            return True
        self.rest = data[cut + 1:]
        for line in data[:cut].split(b"\n"):
            self._sort(line)
        return True

    def _sort(self, line: bytes) -> None:
        if line.startswith(b"#"):
            self._comment(line)
            return
        if not self.header_seen:
            self.check_header(line.decode("utf-8", errors="replace").split(","))
            self.header_seen = True
            return
        self.draws_read += 1
        if self.draws_read > self.draws_expected:
            raise sampler_failed("malformed", self.chain, f"its file holds more than the {self.draws_expected} draws asked for")
        self.ready.append(line)

    def _comment(self, line: bytes) -> None:
        m = _STEP_SIZE.match(line)
        if m:
            self.facts.step_size = _number(m.group(1))
            return
        m = _ELAPSED.match(line)
        if m:
            seconds = _number(m.group(1))
            if m.group(2) == b"Warm-up":
                self.facts.seconds_warmup = seconds
            elif m.group(2) == b"Sampling":
                self.facts.seconds_sampling = seconds


def _number(text: bytes) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


class _Stdout:
    """CmdStan's standard output (a file), followed from its offset for the progress lines; anything else is dropped."""

    def __init__(self, path: Path | None, read_bytes: int):
        self.path = path
        self.read_bytes = read_bytes
        self.fd: int | None = None
        self.offset = 0
        self.rest = b""
        self.latest: dict[int, Progress] = {}

    def pump(self) -> list[Progress]:
        if self.path is None:
            return []
        if self.fd is None:
            try:
                self.fd = os.open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                return []
        out: list[Progress] = []
        while True:
            chunk = os.pread(self.fd, self.read_bytes, self.offset)
            if not chunk:
                return out
            self.offset += len(chunk)
            data = self.rest + chunk if self.rest else chunk
            cut = data.rfind(b"\n")
            if cut < 0:
                self.rest = data if len(data) <= STDOUT_LINE_CAP else b""
                continue
            self.rest = data[cut + 1:]
            if len(self.rest) > STDOUT_LINE_CAP:
                self.rest = b""
            for line in data[:cut].split(b"\n"):
                m = _PROGRESS.match(line)
                if m:
                    p = Progress(int(m.group(1) or 1), int(m.group(2)), int(m.group(3)),
                                 "warmup" if m.group(4) == b"Warmup" else "sampling")
                    self.latest[p.chain] = p
                    out.append(p)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def tail_lines(path: Path | None, max_bytes: int = STDERR_TAIL_BYTES, max_lines: int = STDERR_TAIL_LINES) -> str:
    """The last lines of a log file (CmdStan's own text, read from the end: never the whole file)."""
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
    except OSError:
        return ""
    lines = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def stop(process: Any, grace: float = STOP_GRACE_SECONDS) -> None:
    """Stops CmdStan: terminate, then kill after `grace` seconds; waits for it either way."""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except ProcessLookupError:
        pass


class Follower:
    """Follows the C CSV files of one CmdStan run (`paths`, from csv_paths) and gives their draws as blocks in the fixed order.

    `header` is the expected CSV header (the sampler's fields then the program's columns: bayes_task's Layout.header, as
    bayescontract.Header writes it); `check_header` compares a file's header with it (Layout.check_header, which raises its
    ManifestMismatch; by default an exact comparison raising this module's); `draws` the draws a chain must write;
    `block_values` the limit that sizes a block (B = block_rows(block_values, columns), the sinks' `block_draws`); `process` the
    CmdStan child (a Popen: poll, terminate, kill, wait); `stdout`/`stderr` the files CmdStan's two streams go to.
    `on_progress(Progress)` and `on_idle()` are the runner's checks (the time projection), `on_block(BlockInfo)` its disk
    projection; any of them may raise a Refusal, which stops CmdStan like any other. `on_line(chain, draw, line)` sees every draw
    line as CmdStan wrote it, without its newline (the thinned `fitted_<c>.csv` of predictive-checks keeps some: a line at a
    time, never a file)."""

    def __init__(self, *, paths: Sequence[str | Path], header: Sequence[str], draws: int, block_values: int, process: Any,
                 stdout: str | Path | None = None, stderr: str | Path | None = None,
                 check_header: Callable[[list[str]], None] | None = None, on_progress: Callable[[Progress], None] | None = None,
                 on_idle: Callable[[], None] | None = None, on_block: Callable[[BlockInfo], None] | None = None,
                 on_line: Callable[[int, int, bytes], None] | None = None,
                 poll_seconds: float = POLL_SECONDS, read_bytes: int = READ_BYTES, stop_grace: float = STOP_GRACE_SECONDS,
                 sleep: Callable[[float], None] = time.sleep):
        if draws < 1:
            raise ValueError("draws must be positive")
        self.header = list(header)
        self.columns = len(self.header)
        self.draws = draws
        self.rows = block_rows(block_values, self.columns)
        self.blocks_per_chain = -(-draws // self.rows)
        self.process = process
        self.stderr = Path(stderr) if stderr is not None else None
        self.on_progress = on_progress
        self.on_idle = on_idle
        self.on_block = on_block
        self.on_line = on_line
        self.poll_seconds = poll_seconds
        self.stop_grace = stop_grace
        self.sleep = sleep
        self.chains = [_ChainFile(k + 1, Path(p), check_header or self._check_header, draws, read_bytes) for k, p in enumerate(paths)]
        self.stdout = _Stdout(Path(stdout) if stdout is not None else None, read_bytes)
        self.exit_code: int | None = None

    @property
    def progress(self) -> dict[int, Progress]:
        """The latest progress line read of each chain."""
        return self.stdout.latest

    # -- the loop -------------------------------------------------------------------------------------------------------------

    def follow(self, *sinks: Sink) -> list[ChainFacts]:
        """Hands every block to each sink in the fixed order, then reads the files' tails and checks CmdStan's exit; returns the
        chains' facts. On any refusal or fault CmdStan is stopped, every sink's `abort()` is called, and the exception goes on."""
        try:
            for sink in sinks:
                block_draws = getattr(sink, "block_draws", self.rows)
                if block_draws != self.rows:
                    raise ValueError(f"a sink writes blocks of {block_draws} draws; the follower reads blocks of {self.rows}")
            for info, values in self._blocks():
                for sink in sinks:
                    sink.write_block(info.chain, info.first_draw, values)
                del values   # the block is the sinks' now; nothing here keeps it
                if self.on_block is not None:
                    self.on_block(info)
            self._finish()
            return [c.facts for c in self.chains]
        except BaseException:
            stop(self.process, self.stop_grace)
            for sink in sinks:
                abort = getattr(sink, "abort", None)
                if abort is not None:
                    abort()
            raise
        finally:
            for c in self.chains:
                c.close()
            self.stdout.close()

    def _blocks(self) -> Iterator[tuple[BlockInfo, np.ndarray]]:
        for b in range(self.blocks_per_chain):
            first = b * self.rows + 1
            rows = min(self.rows, self.draws - b * self.rows)
            for chain in self.chains:
                values = self._gather(chain, first, rows)
                yield BlockInfo(chain.chain, b + 1, first, rows, chain.facts.draws, chain.facts.draw_bytes), values
                del values   # before the next block is allocated: one block in memory at a time

    def _gather(self, chain: _ChainFile, first: int, rows: int) -> np.ndarray:
        """The next `rows` draws of `chain`, parsed line by line into a new matrix; waits (on disk) for lines not yet written."""
        values = np.empty((rows, self.columns), dtype=np.float64)
        i = 0
        while i < rows:
            if chain.ready:
                line = chain.ready.popleft()
                fields = line.split(b",")
                if len(fields) != self.columns:
                    raise sampler_failed("malformed", chain.chain, f"draw {first + i} has {len(fields)} fields where the header "
                                         f"has {self.columns}")
                try:
                    values[i] = list(map(float, fields))
                except ValueError:
                    raise sampler_failed("malformed", chain.chain, f"draw {first + i} holds a field that is not a number") from None
                if self.on_line is not None:
                    self.on_line(chain.chain, first + i, line)
                i += 1
                chain.facts.draws += 1
                chain.facts.draw_bytes += len(line) + 1
                continue
            self._pump()
            if chain.read_more(rows - i):
                continue
            if self._ended():
                # the process has exited: whatever it wrote before is on disk, so one more read sees it all
                if chain.read_more(rows - i):
                    continue
                self._refuse_ended(chain, first + i)
            self._idle()
        return values

    def _finish(self) -> None:
        """After the last block: waits for CmdStan to exit, reads each file's tail (the elapsed times: no draw line may be left)
        and refuses a non-zero exit."""
        while not self._ended():
            self._pump()
            self._idle()
        self._pump()
        if self.exit_code != 0:
            self._refuse_exit(None)
        for chain in self.chains:
            while chain.read_more():
                pass
            if chain.ready:
                raise sampler_failed("malformed", chain.chain, f"its file holds more than the {self.draws} draws asked for")
            if chain.rest:
                raise sampler_failed("malformed", chain.chain, "its file ends in the middle of a line")

    # -- helpers ----------------------------------------------------------------------------------------------------------------

    def _check_header(self, header: list[str]) -> None:
        if header != self.header:
            first = next((i for i, (a, b) in enumerate(zip(header, self.header)) if a != b), min(len(header), len(self.header)))
            raise ManifestMismatch(f"a program and its manifest disagree: the CSV's header has {len(header)} columns and the manifest "
                                   f"expects {len(self.header)}; the first difference is at column {first + 1}")

    def _pump(self) -> None:
        for p in self.stdout.pump():
            if self.on_progress is not None:
                self.on_progress(p)

    def _idle(self) -> None:
        if self.on_idle is not None:
            self.on_idle()
        self.sleep(self.poll_seconds)

    def _ended(self) -> bool:
        if self.exit_code is None:
            self.exit_code = self.process.poll()
        return self.exit_code is not None

    def _refuse_ended(self, chain: _ChainFile, draw: int) -> None:
        if self.exit_code != 0:
            self._refuse_exit(chain.chain)
        if not chain.header_seen:
            detail = "its file was never written" if chain.fd is None else "its file has no header"
            raise sampler_failed("chain_ended", chain.chain, detail, tail_lines(self.stderr))
        raise sampler_failed("chain_ended", chain.chain, f"it ended after {draw - 1} of its {self.draws} draws",
                             tail_lines(self.stderr))

    def _refuse_exit(self, chain: int | None) -> None:
        tail = tail_lines(self.stderr)
        if _INIT_FAILED in tail:
            raise Refusal("init_failed", "the sampler found no starting point where the model's density is finite: check the "
                          "bounds and the priors, or give initial values under Advanced", {"chain": chain, "log": tail})
        raise sampler_failed("exit", chain, f"CmdStan exited with code {self.exit_code}", tail)


# -- CmdStan, started and followed ---------------------------------------------------------------------------------------------

def use_system_allocator() -> None:
    """Arrow's allocations through the C library's allocator instead of pyarrow 26's default (mimalloc). Measured by lot K2 at the
    spec's size (2,000 quantities, 4 chains, blocks of 1,048,576 values): with mimalloc the peak resident memory of the same run
    varied by about 20 MB from one run to the next (103 to 125 MB above the start) and kept a few more MB the longer the run;
    with the system allocator it was 85 MB at 1,000 draws a chain and 88 MB at 10,000. Called once at the start of a sampling Job,
    before the writers are opened."""
    import pyarrow as pa
    pa.set_memory_pool(pa.system_memory_pool())



def _number_text(x: Any) -> str:
    """A number of the task as CmdStan's argument: an integer as an integer, a double as Python writes it back exactly."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TypeError(f"not a number: {x!r}")
    return str(x) if isinstance(x, int) else repr(float(x))


def cmdstan_args(binary: str | Path, sampler: dict[str, Any], algorithm: str, data: str | Path, out: str | Path, threads: int,
                 init: str | Path | None = None) -> list[str]:
    """The fixed argument list of a sampling run («The CSV files»): every value a number this function formats, no text of a
    person. `algorithm` is `nuts` or `fixed_param` (the prior predictive: no adaptation, no tree); `init` is the path of init.json
    when the initial values are an object, else the radius comes from `sampler["init"]` (null: CmdStan's default)."""
    args = [str(binary), "method=sample", f"num_samples={_number_text(int(sampler['draws']))}"]
    if algorithm == "fixed_param":
        args += ["num_warmup=0", "save_warmup=0", "algorithm=fixed_param"]
    elif algorithm == "nuts":
        args += [f"num_warmup={_number_text(int(sampler['warmup']))}", "save_warmup=0",
                 "adapt", f"delta={_number_text(float(sampler['adapt_delta']))}",
                 "algorithm=hmc", "engine=nuts", f"max_depth={_number_text(int(sampler['max_treedepth']))}"]
    else:
        raise ValueError(f"no algorithm {algorithm!r}")
    args += [f"num_chains={_number_text(int(sampler['chains']))}", "data", f"file={data}"]
    if init is not None:
        args.append(f"init={init}")
    elif sampler.get("init") is not None:
        args.append(f"init={_number_text(sampler['init'])}")
    args += ["random", f"seed={_number_text(int(sampler['seed']))}", "output", f"file={out}",
             f"refresh={_number_text(int(sampler['refresh']))}", f"num_threads={_number_text(int(threads))}"]
    return args


def write_init(sampler: dict[str, Any], workdir: Path) -> Path | None:
    """init.json when the initial values are an object of values per Stan identifier (checked by the task reader); None when
    they are a radius or CmdStan's default."""
    init = sampler.get("init")
    if not isinstance(init, dict):
        return None
    path = workdir / "init.json"
    path.write_text(json.dumps(init, allow_nan=False, sort_keys=True), encoding="utf-8")
    return path


def start(args: list[str], workdir: Path) -> subprocess.Popen:
    """Starts CmdStan without waiting for it: its standard output to stdout.txt, its errors to stderr.txt (both followed, never
    read whole), PATH its only variable."""
    with open(workdir / "stdout.txt", "wb") as out, open(workdir / "stderr.txt", "wb") as err:
        return subprocess.Popen(args, cwd=str(workdir), env={"PATH": CMDSTAN_PATH}, stdin=subprocess.DEVNULL, stdout=out, stderr=err)


def sample(binary: str | Path, sampler: dict[str, Any], algorithm: str, header: Sequence[str], block_values: int, workdir: Path,
           data: Path, threads: int, *sinks: Sink, check_header: Callable[[list[str]], None] | None = None,
           **hooks: Any) -> list[ChainFacts]:
    """One CmdStan run followed to its end: the arguments, the start, the follower handing every block to `sinks` (opened by the
    caller with `block_rows(block_values, len(header))` draws a block, closed by it afterwards). A refusal stops CmdStan and aborts
    the sinks (Follower.follow); the caller deletes `workdir`."""
    out = workdir / "out.csv"
    args = cmdstan_args(binary, sampler, algorithm, data, out, threads, write_init(sampler, workdir))
    process = start(args, workdir)
    follower = Follower(paths=csv_paths(out, int(sampler["chains"])), header=header, draws=int(sampler["draws"]),
                        block_values=block_values, process=process, stdout=workdir / "stdout.txt",
                        stderr=workdir / "stderr.txt", check_header=check_header, **hooks)
    return follower.follow(*sinks)
