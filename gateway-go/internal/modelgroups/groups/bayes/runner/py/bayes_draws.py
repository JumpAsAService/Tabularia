"""The writers of the draws, of the checks and of the replicated data (docs/specs/bayes-contract.md, «The draws parquet»): one parquet
file each, always long, appended while the chains sample, one row group a block of one chain, never a chain or a file in memory.

THE INTERFACE, as the follower (bayes_follow.py, lot K2) calls it:

    writer = DrawsWriter(path, layout.draws, layout.draw_columns, chains, draws, block_draws, info,
                         n_unchecked=layout.n_sampler)             # opens the file; writes its schema, no row
    for chain, first_draw, count in writer.schedule():            # the fixed order: block 1 of chains 1…C, block 2 of chains 1…C, …
        ...wait for the chain's `count` draw lines, parse them into `block`, a float64 matrix (count, layout.n_columns): one row a
        draw, one column a CSV column, in the CSV's order...
        writer.write_block(chain, first_draw, block)              # appends ONE row group, then forgets it; the caller frees `block`
    footer = writer.close()                                        # checks every chain is complete, adds the metadata, closes

    writer.abort()                                                 # a refusal or a crash: closes and deletes the file

`write_block` refuses (ValueError, a bug of the caller) a block that is not the one `schedule` says comes next: that is what makes the
file the same byte for byte whatever the chains' speeds. It raises the refusal `not_finite` (naming the quantity, the chain and the
draw) when a quantity of the program holds a value that is not finite; the sampler's fields (the first `n_unchecked` quantities) are
facts and are not checked; the program's internal columns are never in `meta`, hence never checked. The writer holds one block at a
time (the arrays of the row group being built) and a few numbers a row group (the footer's list): its memory does not grow with the
draws. The checks file is a DrawsWriter with the checks' meta and columns and n_unchecked = 0; the replicated data are a
ReplicatedWriter (chain, draw, row, outcome, value), called the same way with a block shaped (count, rows) or, with `outcomes`, the CSV's own block.

The columns, as `bayescontract`'s declaration has them: chain (Int32), draw (Int32), quantity (dictionary of String), node, unit (the
same, nullable), index, index_2 (Int32, nullable), value (Float64); within a row group the rows are quantity-major (all the block's
draws of one quantity, then the next). The metadata `tabularia.bayes` is added at the closing and lists every row group as
[chain, first draw, draws], so a reader finds a chain's rows from the footer alone.
"""
from __future__ import annotations

import json
import os
from itertools import islice
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from tab_protocol import Refusal, _fs, is_uri
from bayes_task import MAX_ROW_GROUP, Element

METADATA_KEY = "tabularia.bayes"
FORMAT_VERSION = 1
COPY_CHUNK = 8 * 1024 * 1024  # the chunk of a copy to the output (as garch's paths file)
_DICT = pa.dictionary(pa.int32(), pa.string())

DRAWS_SCHEMA = pa.schema([
    pa.field("chain", pa.int32(), nullable=False), pa.field("draw", pa.int32(), nullable=False),
    pa.field("quantity", _DICT, nullable=False), pa.field("node", _DICT), pa.field("unit", _DICT),
    pa.field("index", pa.int32()), pa.field("index_2", pa.int32()), pa.field("value", pa.float64(), nullable=False)])
REPLICATED_SCHEMA = pa.schema([
    pa.field("chain", pa.int32(), nullable=False), pa.field("draw", pa.int32(), nullable=False),
    pa.field("row", pa.int32(), nullable=False), pa.field("outcome", _DICT, nullable=False),
    pa.field("value", pa.float64(), nullable=False)])


def block_schedule(chains: int, draws: int, block_draws: int) -> Iterator[tuple[int, int, int]]:
    """The blocks in their fixed order: (chain, first draw, count) for block 1 of chain 1…C, then block 2 of chain 1…C, …; the last block
    of a chain is shorter when the block does not divide the draws. Draws are numbered from 1."""
    first = 1
    while first <= draws:
        count = min(block_draws, draws - first + 1)
        for chain in range(1, chains + 1):
            yield chain, first, count
        first += count


class _BlockWriter:
    """What the three writers share: the fixed order of the blocks, the file opened once, the metadata at the closing."""
    kind = ""
    schema: pa.Schema

    def __init__(self, path: str | Path, chains: int, draws: int, block_draws: int, info: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.chains, self.draws, self.block_draws = chains, draws, block_draws
        self.info = dict(info or {})
        self._plan = block_schedule(chains, draws, block_draws)
        self._next: tuple[int, int, int] | None = next(self._plan, None)
        self._row_groups: list[list[int]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = pq.ParquetWriter(str(self.path), self.schema, compression="zstd")
        self._closed = False

    def schedule(self) -> Iterator[tuple[int, int, int]]:
        """(chain, first draw, count) of every block still to write, in the order to write them."""
        yield from islice(block_schedule(self.chains, self.draws, self.block_draws), len(self._row_groups), None)

    def _expect(self, chain: int, first_draw: int, count: int) -> None:
        if self._closed:
            raise ValueError("the writer is closed")
        if self._next is None:
            raise ValueError("every block was written")
        if (chain, first_draw, count) != self._next:
            raise ValueError(f"the next block is (chain, first draw, draws) = {self._next}, not {(chain, first_draw, count)}: "
                             "blocks are written in block-major, chain-minor order")

    def _append(self, table: pa.Table, chain: int, first_draw: int, count: int) -> None:
        if table.num_rows > MAX_ROW_GROUP:
            raise ValueError(f"a block of {table.num_rows} rows passes a row group's {MAX_ROW_GROUP}")
        self._file.write_table(table, row_group_size=max(1, table.num_rows))
        self._row_groups.append([chain, first_draw, count])
        self._next = next(self._plan, None)

    def metadata(self) -> dict[str, Any]:
        return {"format": FORMAT_VERSION, "kind": self.kind, "chains": self.chains, "draws": self.draws, "block_draws": self.block_draws,
                **self.info, "row_groups": self._row_groups}

    def close(self) -> dict[str, Any]:
        """Closes the file when every chain is complete, with the metadata; returns the metadata."""
        if self._next is not None:
            self.abort()
            raise ValueError(f"the file is not complete: the block {self._next} is missing")
        meta = self.metadata()
        self._file.add_key_value_metadata({METADATA_KEY: json.dumps(meta, sort_keys=True, separators=(",", ":"))})
        self._file.close()
        self._closed = True
        return meta

    def abort(self) -> None:
        """A refusal or a crash: the file is closed and deleted («a refusal leaves no partial result»)."""
        if not self._closed:
            try:
                self._file.close()
            except Exception:  # noqa: BLE001 — it is being thrown away
                pass
            self._closed = True
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    @property
    def row_groups(self) -> int:
        return len(self._row_groups)


def _dictionary(values: Sequence[str | None]) -> tuple[pa.Array, np.ndarray, np.ndarray]:
    """(the dictionary, each value's index, the null mask) of a list of texts."""
    seen: dict[str, int] = {}
    codes = np.zeros(len(values), dtype=np.int32)
    mask = np.zeros(len(values), dtype=bool)
    for i, v in enumerate(values):
        if v is None:
            mask[i] = True
        else:
            codes[i] = seen.setdefault(v, len(seen))
    return pa.array(list(seen), pa.string()), codes, mask


class DrawsWriter(_BlockWriter):
    """The long draws (or checks) file. `meta` and `columns` are aligned: the i-th quantity is column columns[i] of the CSV's matrix."""
    kind = "draws"
    schema = DRAWS_SCHEMA

    def __init__(self, path: str | Path, meta: Sequence[Element], columns: np.ndarray, chains: int, draws: int, block_draws: int,
                 info: dict[str, Any] | None = None, n_unchecked: int = 0, kind: str = "draws") -> None:
        if len(meta) != len(columns):
            raise ValueError("a column for every quantity")
        if block_draws * max(1, len(meta)) > MAX_ROW_GROUP:
            raise ValueError("a block of draws of every quantity would pass a row group")
        self.kind = kind
        self.meta = list(meta)
        self.columns = np.asarray(columns, dtype=np.int64)
        self.n_unchecked = n_unchecked
        # what is the same in every row group is built once: the dictionaries and the quantities' own columns
        self._quantity, self._quantity_codes, _ = _dictionary([m.quantity for m in self.meta])
        self._node, self._node_codes, self._node_null = _dictionary([m.node for m in self.meta])
        self._unit, self._unit_codes, self._unit_null = _dictionary([m.unit for m in self.meta])
        self._index = np.array([m.index or 0 for m in self.meta], dtype=np.int32)
        self._index_null = np.array([m.index is None for m in self.meta], dtype=bool)
        self._index_2 = np.array([m.index_2 or 0 for m in self.meta], dtype=np.int32)
        self._index_2_null = np.array([m.index_2 is None for m in self.meta], dtype=bool)
        super().__init__(path, chains, draws, block_draws, {**(info or {}), "quantities": len(self.meta)})

    def _not_finite(self, block: np.ndarray, chain: int, first_draw: int) -> None:
        for p in range(self.n_unchecked, len(self.meta)):
            column = block[:, self.columns[p]]
            if not np.isfinite(column).all():
                draw = first_draw + int(np.argmax(~np.isfinite(column)))
                name = self.meta[p].quantity
                raise Refusal("not_finite", f"the draw {draw} of chain {chain} of {name} is not a finite number: the model has "
                                            "no finite value there", {"quantity": name, "chain": chain, "draw": draw})

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None:
        count = int(block.shape[0])
        self._expect(chain, first_draw, count)
        self._not_finite(block, chain, first_draw)
        p = len(self.meta)
        values = np.ascontiguousarray(block[:, self.columns].T, dtype=np.float64).reshape(-1)  # quantity-major
        n = values.shape[0]

        table = pa.table({
            "chain": pa.array(np.full(n, chain, dtype=np.int32)),
            "draw": pa.array(np.tile(np.arange(first_draw, first_draw + count, dtype=np.int32), p)),
            "quantity": pa.DictionaryArray.from_arrays(pa.array(np.repeat(self._quantity_codes, count), pa.int32()), self._quantity),
            "node": pa.DictionaryArray.from_arrays(pa.array(np.repeat(self._node_codes, count), pa.int32(),
                                                            mask=np.repeat(self._node_null, count)), self._node),
            "unit": pa.DictionaryArray.from_arrays(pa.array(np.repeat(self._unit_codes, count), pa.int32(),
                                                            mask=np.repeat(self._unit_null, count)), self._unit),
            "index": pa.array(np.repeat(self._index, count), pa.int32(), mask=np.repeat(self._index_null, count)),
            "index_2": pa.array(np.repeat(self._index_2, count), pa.int32(), mask=np.repeat(self._index_2_null, count)),
            "value": pa.array(values),
        }, schema=self.schema)
        self._append(table, chain, first_draw, count)


class ReplicatedWriter(_BlockWriter):
    """The replicated data (y_rep): chain, draw, row (1…rows, the kept rows in the input's order), outcome (the observed data's Stan
    identifier, dictionary-encoded), value. `rows` is the rows a draw holds in all. Without `outcomes` a block is (count, rows) of one
    outcome named `y`; with `outcomes` (a list of (identifier, the CSV's columns of that outcome's rows)) a block is the generate_quantities
    CSV's own block, and the writer takes each outcome's columns from it. A row group holds the block's outcomes one after the other, each
    draw by draw (the draw's rows in order): the draw number is the thinned draw's number in its chain (1…its draws)."""
    kind = "replicated"
    schema = REPLICATED_SCHEMA

    def __init__(self, path: str | Path, rows: int, chains: int, draws: int, block_draws: int, info: dict[str, Any] | None = None,
                 outcomes: Sequence[tuple[str, np.ndarray]] | None = None) -> None:
        if rows < 1 or block_draws * rows > MAX_ROW_GROUP:
            raise ValueError("a block of draws of every row would pass a row group")
        self.rows = rows
        self.outcomes = [(name, np.asarray(cols, dtype=np.int64)) for name, cols in outcomes] if outcomes is not None else None
        if self.outcomes is not None and sum(len(c) for _, c in self.outcomes) != rows:
            raise ValueError("the outcomes' columns are not the rows a draw holds")
        names = [n for n, _ in self.outcomes] if self.outcomes is not None else ["y"]
        self._names, self._codes, _ = _dictionary(names)
        super().__init__(path, chains, draws, block_draws, {**(info or {}), "rows": rows, "outcomes": names})

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None:
        count = int(block.shape[0])
        self._expect(chain, first_draw, count)
        parts = self.outcomes if self.outcomes is not None else [("y", np.arange(self.rows))]
        if self.outcomes is None and block.shape[1] != self.rows:
            raise ValueError(f"a block has {self.rows} columns, not {block.shape[1]}")
        taken = [block[:, cols] for _, cols in parts] if self.outcomes is not None else [block]
        for values in taken:
            if not np.isfinite(values).all():
                draw = first_draw + int(np.argmax((~np.isfinite(values)).any(axis=1)))
                raise Refusal("not_finite", f"the replicated data of draw {draw} of chain {chain} are not finite numbers",
                              {"quantity": "replicated", "chain": chain, "draw": draw})
        n = count * self.rows
        draw = np.concatenate([np.repeat(np.arange(first_draw, first_draw + count, dtype=np.int32), v.shape[1]) for v in taken])
        row = np.concatenate([np.tile(np.arange(1, v.shape[1] + 1, dtype=np.int32), count) for v in taken])
        outcome = np.concatenate([np.full(count * v.shape[1], k, dtype=np.int32) for k, v in enumerate(taken)])
        value = np.concatenate([np.ascontiguousarray(v, dtype=np.float64).reshape(-1) for v in taken])
        table = pa.table({
            "chain": pa.array(np.full(n, chain, dtype=np.int32)),
            "draw": pa.array(draw),
            "row": pa.array(row),
            "outcome": pa.DictionaryArray.from_arrays(pa.array(outcome, pa.int32()), self._names),
            "value": pa.array(value),
        }, schema=self.schema)
        self._append(table, chain, first_draw, count)


def block_draws_for_rows(block_values: int, rows: int) -> int:
    """The draws a block of the replicated data holds: floor(block_values / rows), at least 1."""
    return max(1, block_values // max(1, rows))


def read_metadata(path: str | Path) -> dict[str, Any]:
    """The metadata `tabularia.bayes` of a file, from its footer alone."""
    md = pq.ParquetFile(str(path)).metadata.metadata or {}
    return json.loads(md[METADATA_KEY.encode()].decode("utf-8"))


def copy_in_chunks(source: str | Path, target: str, chunk: int = COPY_CHUNK) -> int:
    """Copies a file to a path or an s3:// address, `chunk` bytes at a time (never the whole file); returns the bytes copied."""
    total = 0
    if is_uri(target):
        filesystem, where = _fs(target)
        out = filesystem.open_output_stream(where)
    else:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        out = open(target, "wb")
    with open(source, "rb") as src, out:
        while True:
            piece = src.read(chunk)
            if not piece:
                break
            out.write(piece)
            total += len(piece)
    return total


def file_size(path: str | Path) -> int:
    return os.stat(path).st_size
