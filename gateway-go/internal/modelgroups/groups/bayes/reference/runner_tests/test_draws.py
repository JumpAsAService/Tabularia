"""The writers of the draws, the checks and the replicated data (lot K1): the schema, the order of the row groups, the rows quantity-major,
the metadata at the closing, the refusals and the chunked copy, on a few hundred values."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_draws  # noqa: E402
from bayes_task import Element  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

META = [Element("lp__", None, None, None, None), Element("accept_stat__", None, None, None, None),
        Element("mu", "mu", None, None, None), Element("a[x]", "a", "x", 1, None), Element("a[y]", "a", "y", 2, None),
        Element("rho[b,a]", "rho", "b,a", 2, 1)]
# a CSV of 8 columns: 0, 1 sampler, 2 internal (dropped), 3 mu, 4 a.1, 5 a.2, 6 internal, 7 rho.2.1
COLUMNS = np.array([0, 1, 3, 4, 5, 7])
CHAINS, DRAWS, BLOCK = 3, 7, 3


def chain_matrix(chain, seed=0):
    return np.random.default_rng(1000 * seed + chain).normal(size=(DRAWS, 8))


def write(path, order=None, **kw):
    w = bayes_draws.DrawsWriter(path, META, COLUMNS, CHAINS, DRAWS, BLOCK, {"seed": 7, "program_sha256": "ab" * 32}, n_unchecked=2, **kw)
    data = {c: chain_matrix(c) for c in range(1, CHAINS + 1)}
    for chain, first, count in w.schedule():
        w.write_block(chain, first, data[chain][first - 1:first - 1 + count])
    return w, data


class TestDrawsWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_schema_is_the_one_the_go_side_declares(self):
        declared = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))["output_columns"]["draws"]
        types = {"Int32": pa.int32(), "Float64": pa.float64()}
        schema = bayes_draws.DRAWS_SCHEMA
        self.assertEqual([f.name for f in schema], [c["name"] for c in declared])
        for field, c in zip(schema, declared):
            self.assertEqual(field.nullable, c["nullable"], c["name"])
            if c.get("dictionary"):
                self.assertTrue(pa.types.is_dictionary(field.type) and field.type.value_type == pa.string(), c["name"])
            else:
                self.assertEqual(field.type, types[c["type"]], c["name"])
        replicated = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))["output_columns"]["replicated"]
        self.assertEqual([(f.name, f.nullable) for f in bayes_draws.REPLICATED_SCHEMA], [(c["name"], c["nullable"]) for c in replicated])

    def test_the_row_groups_are_block_major_chain_minor_and_quantity_major(self):
        w, data = write(self.dir / "draws.parquet")
        meta = w.close()
        f = pq.ParquetFile(self.dir / "draws.parquet")
        self.assertEqual(f.schema_arrow, bayes_draws.DRAWS_SCHEMA)
        expected = [(c, first, n) for first, n in ((1, 3), (4, 3), (7, 1)) for c in (1, 2, 3)]
        self.assertEqual(f.num_row_groups, len(expected))
        self.assertEqual([tuple(x) for x in meta["row_groups"]], expected)
        self.assertEqual(f.metadata.num_rows, CHAINS * DRAWS * len(META))
        for i, (chain, first, count) in enumerate(expected):
            t = f.read_row_group(i).to_pydict()
            self.assertEqual(len(t["value"]), count * len(META))
            self.assertEqual(set(t["chain"]), {chain})
            rows = iter(zip(t["draw"], t["quantity"], t["node"], t["unit"], t["index"], t["index_2"], t["value"]))
            for p, element in enumerate(META):
                for b in range(count):
                    draw, quantity, node, unit, index, index_2, value = next(rows)
                    self.assertEqual((draw, quantity, node, unit, index, index_2), (first + b, element.quantity, element.node, element.unit,
                                                                                  element.index, element.index_2))
                    self.assertEqual(value, data[chain][first - 1 + b, COLUMNS[p]])
        # every draw of every chain and quantity once
        full = pq.read_table(self.dir / "draws.parquet").to_pydict()
        self.assertEqual(len({(c, d, q) for c, d, q in zip(full["chain"], full["draw"], full["quantity"])}), CHAINS * DRAWS * len(META))

    def test_the_metadata_is_written_at_the_closing_and_a_footer_finds_a_chains_row_groups(self):
        w, _ = write(self.dir / "draws.parquet")
        meta = w.close()
        read = bayes_draws.read_metadata(self.dir / "draws.parquet")
        self.assertEqual(read, meta)
        self.assertEqual((read["format"], read["kind"], read["chains"], read["draws"], read["block_draws"], read["quantities"]),
                         (1, "draws", 3, 7, 3, 6))
        self.assertEqual((read["seed"], read["program_sha256"]), (7, "ab" * 32))
        chain2 = [i for i, (c, _, _) in enumerate(read["row_groups"]) if c == 2]
        self.assertEqual(chain2, [1, 4, 7])
        f = pq.ParquetFile(self.dir / "draws.parquet")
        self.assertEqual(set(f.read_row_group(chain2[1]).column("chain").to_pylist()), {2})

    def test_the_same_blocks_make_the_same_file_byte_for_byte(self):
        digests = set()
        for run in range(3):
            w, _ = write(self.dir / f"d{run}.parquet")
            w.close()
            digests.add(hashlib.sha256((self.dir / f"d{run}.parquet").read_bytes()).hexdigest())
        self.assertEqual(len(digests), 1)

    def test_blocks_out_of_order_are_refused_and_never_written(self):
        w = bayes_draws.DrawsWriter(self.dir / "d.parquet", META, COLUMNS, CHAINS, DRAWS, BLOCK)
        block = chain_matrix(1)[:3]
        with self.assertRaises(ValueError):
            w.write_block(2, 1, block)           # chain 2 before chain 1
        with self.assertRaises(ValueError):
            w.write_block(1, 2, block)           # the wrong first draw
        with self.assertRaises(ValueError):
            w.write_block(1, 1, block[:2])       # the wrong count
        self.assertEqual(w.row_groups, 0)
        w.write_block(1, 1, block)
        self.assertEqual(w.row_groups, 1)
        w.abort()

    def test_an_incomplete_file_is_not_closed_and_an_abort_deletes_it(self):
        w = bayes_draws.DrawsWriter(self.dir / "d.parquet", META, COLUMNS, CHAINS, DRAWS, BLOCK)
        w.write_block(1, 1, chain_matrix(1)[:3])
        with self.assertRaises(ValueError):
            w.close()
        self.assertFalse((self.dir / "d.parquet").exists())
        w = bayes_draws.DrawsWriter(self.dir / "e.parquet", META, COLUMNS, CHAINS, DRAWS, BLOCK)
        w.write_block(1, 1, chain_matrix(1)[:3])
        w.abort()
        self.assertFalse((self.dir / "e.parquet").exists())

    def test_a_value_that_is_not_finite_is_named_and_the_sampler_fields_are_facts(self):
        w = bayes_draws.DrawsWriter(self.dir / "d.parquet", META, COLUMNS, CHAINS, DRAWS, BLOCK, n_unchecked=2)
        block = chain_matrix(1)[:3].copy()
        block[:, 0] = np.inf          # lp__: a fact, not checked
        block[1, 2] = np.inf          # the dropped internal column: never checked
        w.write_block(1, 1, block)
        block = chain_matrix(2)[:3].copy()
        block[2, 4] = np.nan          # a[x] of the third draw
        with self.assertRaises(Refusal) as e:
            w.write_block(2, 1, block)
        self.assertEqual(e.exception.code, "not_finite")
        self.assertEqual(e.exception.params, {"quantity": "a[x]", "chain": 2, "draw": 3})
        self.assertEqual(w.row_groups, 1)
        w.abort()

    def test_one_block_is_all_the_writer_holds(self):
        w, _ = write(self.dir / "d.parquet")
        held = {k: v for k, v in vars(w).items() if isinstance(v, (list, np.ndarray)) and k not in ("meta",)}
        # the writer's lists are the footer's row groups and the quantities' own small arrays: nothing grows with the draws
        self.assertLessEqual(len(w._row_groups), 9)
        for k, v in held.items():
            self.assertLessEqual(len(v), max(len(META), 9), k)
        w.close()

    def test_the_checks_file_is_the_same_writer_with_its_own_meta(self):
        meta = [Element("Ppc_first_rep", "first", None, None, None)]
        w = bayes_draws.DrawsWriter(self.dir / "c.parquet", meta, np.array([2]), 2, 4, 4, kind="checks")
        for chain, first, count in w.schedule():
            w.write_block(chain, first, chain_matrix(chain)[:count])
        self.assertEqual(w.close()["kind"], "checks")
        t = pq.read_table(self.dir / "c.parquet")
        self.assertEqual(t.schema, bayes_draws.DRAWS_SCHEMA)
        self.assertEqual(t.num_rows, 8)

    def test_a_block_that_would_pass_a_row_group_is_refused_at_the_opening(self):
        with self.assertRaises(ValueError):
            bayes_draws.DrawsWriter(self.dir / "d.parquet", META, COLUMNS, 1, 10, 10 ** 6)

    def test_the_copy_in_chunks_is_the_file(self):
        w, _ = write(self.dir / "d.parquet")
        w.close()
        n = bayes_draws.copy_in_chunks(self.dir / "d.parquet", str(self.dir / "out" / "copy.parquet"), chunk=100)
        self.assertEqual(n, (self.dir / "d.parquet").stat().st_size)
        self.assertEqual((self.dir / "out" / "copy.parquet").read_bytes(), (self.dir / "d.parquet").read_bytes())


class TestReplicatedWriter(unittest.TestCase):
    def test_rows_of_a_thinned_draw_are_written_in_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rep.parquet"
            w = bayes_draws.ReplicatedWriter(path, 5, 2, 5, 2, {"seed": 1})
            data = {c: np.random.default_rng(c).normal(size=(5, 5)) for c in (1, 2)}
            for chain, first, count in w.schedule():
                w.write_block(chain, first, data[chain][first - 1:first - 1 + count])
            meta = w.close()
            self.assertEqual((meta["kind"], meta["rows"], len(meta["row_groups"])), ("replicated", 5, 6))
            f = pq.ParquetFile(path)
            self.assertEqual(f.schema_arrow, bayes_draws.REPLICATED_SCHEMA)
            t = f.read_row_group(1).to_pydict()                     # block 1 of chain 2
            self.assertEqual((set(t["chain"]), t["draw"][:5], t["row"][:5]), ({2}, [1] * 5, [1, 2, 3, 4, 5]))
            self.assertEqual(t["value"][:5], list(data[2][0]))
            self.assertEqual(bayes_draws.block_draws_for_rows(1_048_576, 1000), 1048)
            with self.assertRaises(ValueError):
                bayes_draws.ReplicatedWriter(Path(tmp) / "x.parquet", 5, 2, 5, 10 ** 6)

    def test_a_replicated_draw_with_a_value_that_is_not_finite_is_left_out_whole_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rep.parquet"
            w = bayes_draws.ReplicatedWriter(path, 3, 1, 3, 3)
            block = np.ones((3, 3))
            block[1, 1] = np.nan
            w.write_block(1, 1, block)
            meta = w.close()
            self.assertEqual((w.dropped, w.kept, meta["dropped_draws"]), (1, 2, 1))
            self.assertEqual(meta["row_groups"], [[1, 1, 3]])            # the schedule's draws, not the kept ones
            t = pq.read_table(path).to_pydict()
            self.assertEqual(sorted(set(t["draw"])), [1, 3])
            self.assertEqual(len(t["value"]), 6)

    def test_a_block_of_replicated_draws_that_all_threw_leaves_an_empty_row_group_and_the_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rep.parquet"
            w = bayes_draws.ReplicatedWriter(path, 2, 1, 2, 2)
            w.write_block(1, 1, np.full((2, 2), np.nan))
            w.close()
            self.assertEqual((w.dropped, w.kept), (2, 0))
            self.assertEqual(pq.ParquetFile(path).metadata.num_row_groups, 1)


class TestSchedule(unittest.TestCase):
    def test_the_fixed_order(self):
        self.assertEqual(list(bayes_draws.block_schedule(2, 5, 2)),
                         [(1, 1, 2), (2, 1, 2), (1, 3, 2), (2, 3, 2), (1, 5, 1), (2, 5, 1)])
        self.assertEqual(list(bayes_draws.block_schedule(1, 4, 4)), [(1, 1, 4)])


if __name__ == "__main__":
    unittest.main()
