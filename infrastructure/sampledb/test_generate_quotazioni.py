"""The raw materials' prices in the sample data generator (generate.py), without any database.

    python3 -m unittest infrastructure/sampledb/test_generate_quotazioni.py

It needs the generator's own libraries (numpy, pandas, Faker, psycopg2); without them the tests are
skipped, so a plain Python without them passes over them. The CI runs them in the sampledb image
with SAMPLEDB_REQUIRE_LIBS=1 (a missing library is then an error, not a skip):
    docker run --rm -e SAMPLEDB_REQUIRE_LIBS=1 --entrypoint python tabularia-sampledb:ci \
        -m unittest -v test_generate_quotazioni

What they pin (docs/specs/garch-risk.md, «Sample data»): the prices' generator is apart from the others (the
customers' and orders' tables are byte for byte what they were before it existed, whatever it draws), the
data is the same at every run and at every scale, the days are the trading days, and the series shows what
it was built to show — volatility that clusters (checked with a portmanteau written out here, not with the
library the volatility node uses), fat tails, an olive oil that starts agitated and calms down — within a
few hundred KiB of memory.
"""
import hashlib
import math
import os
import sys
import unittest
from datetime import date

try:
    import numpy as np
    import pandas as pd
    os.environ["SAMPLES_SCALE"] = "small"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import generate as g
    HAVE_LIBS = True
except ImportError:  # a Python without numpy: the tests below skip, unless they are required
    if os.environ.get("SAMPLEDB_REQUIRE_LIBS"):
        raise
    HAVE_LIBS = False

# The hash of the customers' lives, agents, customers and orders' skeleton at small: the one the experiments' test
# pins, taken before the experiments were added. The prices must not move it either.
EXISTING_TABLES_HASH = "98c0687f067651d9c51c018a569f12a3721d21f1fc3a1ea9d154d76cabafca0e"
CHI2_30_AT_1_PERCENT = 50.89     # the critical value of a chi-squared with 30 degrees of freedom, at 0.01


def returns_of(df: "pd.DataFrame", material: str) -> "np.ndarray":
    return np.diff(np.log(df[df.materia == material].prezzo.astype(float).to_numpy()))


class FakeCursor:
    def __init__(self, exists):
        self.exists, self.statements, self.copied = exists, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.statements.append(sql)

    def fetchone(self):
        return ("ref.quotazioni_materie_prime",) if self.exists else (None,)

    def copy_expert(self, sql, buf):
        self.copied.append(sql)


class FakePg:
    def __init__(self, exists):
        self.cur, self.commits = FakeCursor(exists), 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


@unittest.skipUnless(HAVE_LIBS, "numpy, pandas, Faker and psycopg2 are needed")
class Quotazioni(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = g.build_quotazioni()

    # ── apart from everything else ───────────────────────────────────────────
    @staticmethod
    def existing_tables_hash() -> str:
        g.rng = np.random.default_rng(g.SEED)
        g.Faker.seed(g.SEED)
        agents = g.gen_agents()
        intensity = g.build_day_intensity()
        life = g.simulate_lifecycles(agents, intensity)
        month = pd.to_datetime(np.datetime64(g.START_DATE) + np.arange(g.DAYS + 1).astype("timedelta64[D]")).month.to_numpy()
        cust, day = g.simulate_orders(life, intensity, month)
        customers = g.gen_customers(agents, life)
        h = hashlib.sha256()
        for df in (agents, customers):
            h.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
        for a in (intensity, cust, day, *(np.asarray(life[k]) for k in sorted(life))):
            h.update(np.ascontiguousarray(a).tobytes())
        return h.hexdigest()

    def test_the_existing_tables_are_what_they_were(self):
        self.assertEqual(self.existing_tables_hash(), EXISTING_TABLES_HASH)

    def test_the_prices_draw_nothing_from_the_other_generators(self):
        state = g.rng.bit_generator.state
        faker_state = g.fake.random.getstate()
        g.build_quotazioni()
        self.assertEqual(g.rng.bit_generator.state, state)
        self.assertEqual(g.fake.random.getstate(), faker_state)
        # and the other way round: moving the main generator changes nothing here
        g.rng = np.random.default_rng(12345)
        pd.testing.assert_frame_equal(g.build_quotazioni(), self.df)

    def test_the_data_is_the_same_at_every_run_and_at_every_scale(self):
        pd.testing.assert_frame_equal(g.build_quotazioni(), self.df)
        # the builder takes no scale: nothing in it reads the preset
        self.assertEqual(g.build_quotazioni.__code__.co_argcount, 0)

    def test_the_generator_peak_is_small(self):
        import tracemalloc
        tracemalloc.start()
        try:
            g.build_quotazioni()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024, f"the peak was {peak / 1024:.0f} KiB")

    # ── the table ────────────────────────────────────────────────────────────
    def test_the_columns_and_the_rows(self):
        self.assertEqual(list(self.df.columns), ["data", "materia", "prezzo"])
        self.assertEqual(sorted(self.df.materia.unique()), ["caffe_verde", "grano_duro", "olio_evo"])
        days = g.trading_days()
        self.assertEqual(len(self.df), 3 * len(days))
        self.assertTrue(2200 <= len(self.df) <= 2400)
        for _, d in self.df.groupby("materia"):
            self.assertEqual(list(d.data), days)
        self.assertEqual(len(self.df.drop_duplicates(["data", "materia"])), len(self.df))
        self.assertTrue((self.df.prezzo > 0).all())
        self.assertTrue(((self.df.prezzo * 10000).round(6) % 1 == 0).all(), "four decimals at most (numeric(12, 4))")
        self.assertTrue((self.df.prezzo < 10 ** 8).all())

    def test_the_prices_start_where_they_are_said_to(self):
        first = self.df.groupby("materia").prezzo.first()
        self.assertEqual(first["grano_duro"], 395.0)
        self.assertEqual(first["olio_evo"], 7.2)
        self.assertEqual(first["caffe_verde"], 4.1)

    def test_the_days_are_the_trading_days(self):
        days = g.trading_days()
        self.assertEqual((days[0], days[-1]), (date(2023, 7, 3), date(2026, 7, 15)))
        self.assertTrue(all(d.weekday() < 5 for d in days))
        closed = [date(2024, 1, 1), date(2024, 3, 29), date(2024, 4, 1), date(2024, 5, 1), date(2024, 12, 25), date(2024, 12, 26),
                  date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21), date(2025, 5, 1), date(2025, 12, 25), date(2025, 12, 26),
                  date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1)]
        for d in closed:
            self.assertNotIn(d, days, d)
        # Ferragosto and 2 June are working days of the market, as the specification says
        self.assertIn(date(2024, 8, 15), days)
        self.assertIn(date(2024, 6, 3), days)
        self.assertIn(date(2024, 3, 28), days)

    # ── the series ───────────────────────────────────────────────────────────
    def test_the_skewed_t_is_standardized(self):
        r = np.random.Generator(np.random.PCG64(7))
        for nu, xi in ((5.0, 0.92), (8.0, 0.95), (6.0, 0.9)):
            x = g._skew_t(r, 400_000, nu, xi)
            self.assertAlmostEqual(x.mean(), 0.0, delta=0.02)
            self.assertAlmostEqual(x.std(), 1.0, delta=0.02)
            self.assertLess(((x - x.mean()) ** 3).mean(), 0.0, "xi below 1: a longer left tail")

    def test_the_parameters_are_the_specified_ones(self):
        for m in g.QUOT_MATERIALS:
            persistence = m["alpha"] + m["gamma"] / 2 + m["beta"]
            self.assertTrue(0.96 <= persistence <= 0.99, m["materia"])
            self.assertGreater(m["gamma"], 0)
            self.assertTrue(5 <= m["nu"] <= 8)
        olive = next(m for m in g.QUOT_MATERIALS if m["materia"] == "olio_evo")
        self.assertGreater(olive["vol0"], 2 * olive["vol"])

    def test_the_volatility_clusters(self):
        """Box-Pierce on the squared returns, 10 lags a material, the three added: 30 degrees of freedom."""
        q = 0.0
        for m in ("grano_duro", "olio_evo", "caffe_verde"):
            r = returns_of(self.df, m)
            s = (r - r.mean()) ** 2
            s = s - s.mean()
            denom = (s * s).sum()
            q += len(r) * sum(((s[k:] * s[:-k]).sum() / denom) ** 2 for k in range(1, 11))
        self.assertGreater(q, CHI2_30_AT_1_PERCENT, f"Q = {q:.1f}")

    def test_the_returns_have_fat_tails(self):
        for m in ("grano_duro", "olio_evo", "caffe_verde"):
            r = returns_of(self.df, m)
            kurtosis = ((r - r.mean()) ** 4).mean() / r.var() ** 2
            self.assertGreater(kurtosis, 3.5, m)

    def test_the_olive_oil_starts_agitated_and_calms_down(self):
        r = returns_of(self.df, "olio_evo")
        self.assertGreater(r[:100].std(), 1.4 * r[-100:].std())

    def test_the_daily_volatility_is_a_few_percent_at_most(self):
        for m in ("grano_duro", "olio_evo", "caffe_verde"):
            self.assertTrue(0.005 < returns_of(self.df, m).std() < 0.03, m)

    def test_the_seeds_threshold_is_ten_percent_below_the_last_durum_wheat_price(self):
        import seed_tabularia as seed
        last = float(self.df[self.df.materia == "grano_duro"].prezzo.iloc[-1])
        self.assertAlmostEqual(seed.DURUM_THRESHOLD, 0.9 * last, delta=0.25)

    # ── the database ─────────────────────────────────────────────────────────
    def test_the_schema_creates_the_table_and_the_run_sees_it(self):
        sql = open(os.path.join(g.HERE, "schema_ref.sql"), encoding="utf-8").read()
        self.assertIn("DROP TABLE IF EXISTS ref.quotazioni_materie_prime", sql)
        self.assertIn("CREATE TABLE ref.quotazioni_materie_prime", sql)
        for col in ("data              date", "materia           text", "prezzo            numeric(12, 4)"):
            self.assertIn(col, sql)

    def test_ensure_creates_only_the_missing_table(self):
        pg = FakePg(exists=False)
        self.assertTrue(g.ensure_quotazioni(pg))
        text = "\n".join(pg.cur.statements)
        self.assertIn("CREATE TABLE ref.quotazioni_materie_prime", text)
        self.assertNotIn("calendario", text)
        self.assertNotIn("regioni", text)
        self.assertEqual(len(pg.cur.copied), 1)
        self.assertIn("ref.quotazioni_materie_prime", pg.cur.copied[0])
        self.assertEqual(pg.commits, 1)

    def test_ensure_leaves_an_existing_table_alone(self):
        pg = FakePg(exists=True)
        self.assertFalse(g.ensure_quotazioni(pg))
        self.assertEqual(len(pg.cur.statements), 1)     # the to_regclass question and nothing else
        self.assertEqual((pg.cur.copied, pg.commits), ([], 0))


if __name__ == "__main__":
    unittest.main()
