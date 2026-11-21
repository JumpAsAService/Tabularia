"""The web shop's experiments in the sample data generator (generate.py), without any database.

    python3 -m unittest infrastructure/sampledb/test_generate_experiments.py

It needs the generator's own libraries (numpy, pandas, Faker, psycopg2); without them the tests are
skipped, so a plain Python without them passes over them. The CI runs them in the sampledb image
with SAMPLEDB_REQUIRE_LIBS=1 (a missing library is then an error, not a skip):
    docker build -t tabularia-sampledb:ci infrastructure/sampledb
    docker run --rm -e SAMPLEDB_REQUIRE_LIBS=1 --entrypoint python tabularia-sampledb:ci \
        -m unittest -v test_generate_experiments

What they pin: the experiments' generator is apart from the others (the customers' and orders'
tables are byte for byte what they were before it existed, whatever it draws), the data is the same
at every run, and each experiment shows what it was built to show, checked here with formulas written
out in plain Python and not with the library the A/B test node uses:
  · checkout_2026: B converts more than A, the difference is significant, a converter's revenue is not
    different, nothing is wrong with the split;
  · banner_home: the clicks per impression of B above A's, C's below, four variants split evenly;
  · spedizione_gratuita: a sample ratio mismatch (p < 0.001), caused by a filter on B alone, from day 12,
    on the visitors who engage least;
  · the 28-day covariate: correlated with the revenue (about 0.5), empty for the new visitors;
  · the ids: 64 bits, some above 2^31; a few checkout visitors in both variants;
  · the days: inside each experiment, more at the weekend; the history before the first one; the
    heavy tail of the revenue; the daily sums equal to the visitors'.
"""
import hashlib
import math
import os
import sys
import unittest

try:
    import numpy as np
    import pandas as pd
    os.environ["SAMPLES_SCALE"] = "small"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import generate as g
    import scales
    HAVE_LIBS = True
except ImportError:  # a Python without numpy: the tests below skip, unless they are required
    if os.environ.get("SAMPLEDB_REQUIRE_LIBS"):
        raise
    HAVE_LIBS = False

# The hash of the customers' lives, agents, customers and orders' skeleton at small, taken BEFORE the
# experiments were added (the simulation of test_generate_lifecycle, seed 42). It must not move.
EXISTING_TABLES_HASH = "98c0687f067651d9c51c018a569f12a3721d21f1fc3a1ea9d154d76cabafca0e"


def chi2_p(x: float, df: int) -> float:
    """The upper tail of a chi-squared, written out for the degrees of freedom the splits have
    (an odd number: 1 for two variants, 3 for four) — no library."""
    if df == 1:
        return math.erfc(math.sqrt(x / 2))
    if df == 3:
        return math.erfc(math.sqrt(x / 2)) + math.sqrt(2 * x / math.pi) * math.exp(-x / 2)
    raise ValueError(df)


def srm_p(counts) -> float:
    n, k = sum(counts), len(counts)
    return chi2_p(sum((c - n / k) ** 2 / (n / k) for c in counts), k - 1)


def two_proportions(a, b):
    """z and its two-sided p for B - A, unpooled (the plain Wald test)."""
    pa, pb = a.mean(), b.mean()
    z = (pb - pa) / math.sqrt(pa * (1 - pa) / len(a) + pb * (1 - pb) / len(b))
    return pb - pa, z, math.erfc(abs(z) / math.sqrt(2))


@unittest.skipUnless(HAVE_LIBS, "numpy, pandas, Faker and psycopg2 are needed")
class Experiments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = g.build_experiments("small")
        cls.users = cls.tables["esperimenti_utenti"]
        cls.history = cls.tables["esperimenti_storico"]
        cls.daily = cls.tables["esperimenti_giornaliero"]
        cls.experiments = cls.tables["esperimenti"]

    def of(self, experiment: str) -> "pd.DataFrame":
        return self.users[self.users.esperimento_id == experiment]

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

    def test_the_experiments_draw_nothing_from_the_other_generators(self):
        state = g.rng.bit_generator.state
        faker_state = g.fake.random.getstate()
        g.build_experiments("small")
        self.assertEqual(g.rng.bit_generator.state, state)
        self.assertEqual(g.fake.random.getstate(), faker_state)
        # and the other way round: moving the main generator changes nothing here
        g.rng = np.random.default_rng(12345)
        again = g.build_experiments("small")["esperimenti_utenti"]
        pd.testing.assert_frame_equal(again, self.users)

    def test_the_data_is_the_same_at_every_run(self):
        again = g.build_experiments("small")
        for name, df in self.tables.items():
            pd.testing.assert_frame_equal(again[name], df)

    # ── the tables ───────────────────────────────────────────────────────────
    def test_the_tables_have_the_columns_of_the_specification(self):
        self.assertEqual(list(self.experiments.columns), ["id", "nome", "descrizione", "inizio", "fine", "varianti", "quote_previste"])
        self.assertEqual(list(self.users.columns), ["esperimento_id", "utente_id", "variante", "data_ingresso", "sessioni", "impression",
                                                    "click", "convertito", "ricavo", "ricavo_pre_28g", "sessioni_pre_28g"])
        self.assertEqual(list(self.history.columns), ["utente_id", "data_prima_visita", "sessioni", "impression", "click", "convertito",
                                                      "ricavo", "ricavo_pre_28g"])
        self.assertEqual(list(self.daily.columns), ["esperimento_id", "variante", "giorno", "utenti", "conversioni", "ricavo_totale",
                                                    "ricavo_quadrati"])

    def test_three_experiments_and_the_sizes_of_the_scale(self):
        self.assertEqual(list(self.experiments.id), ["checkout_2026", "banner_home", "spedizione_gratuita"])
        self.assertEqual(list(self.experiments.varianti), ["A,B", "A,B,C,D", "A,B"])
        self.assertEqual(list(self.experiments.quote_previste), ["0.5,0.5", "0.25,0.25,0.25,0.25", "0.5,0.5"])
        self.assertEqual(len(self.history), scales.EXPERIMENTS["small"]["history"])
        # the planned visitors, plus the checkout's crossovers, minus what the bot filter took
        planned = scales.EXPERIMENTS["small"]["users"]
        crossover = len(self.of("checkout_2026")) - round(planned * 30 / 60)
        dropped = planned - len(self.users) + crossover
        self.assertGreater(crossover, 0)
        self.assertGreater(dropped, 0)
        self.assertAlmostEqual(len(self.users), planned, delta=planned * 0.02)

    def test_the_sizes_of_the_other_scales_are_the_specifications(self):
        self.assertEqual(scales.EXPERIMENTS["medium"], dict(users=600_000, history=400_000))
        self.assertEqual(scales.EXPERIMENTS["robust"], dict(users=3_000_000, history=2_000_000))

    def test_the_visitors_are_64_bit_ids_some_above_2_pow_31(self):
        for df in (self.users, self.history):
            self.assertEqual(str(df.utente_id.dtype), "int64")
            self.assertGreater((df.utente_id > 2**31).sum(), 0.03 * len(df))
            self.assertGreater(df.utente_id.max(), 2**32)
            self.assertGreater(df.utente_id.min(), 0)
        both = pd.concat([self.users.utente_id, self.history.utente_id])
        # nobody is in the history and in an experiment; within the experiments only the crossovers repeat
        self.assertEqual(len(set(self.users.utente_id) & set(self.history.utente_id)), 0)
        self.assertEqual(self.history.utente_id.nunique(), len(self.history))
        repeated = self.users[self.users.duplicated(["esperimento_id", "utente_id"], keep=False)]
        self.assertEqual(len(self.users) - self.users.utente_id.nunique(), len(repeated) // 2)
        self.assertGreater(len(both), 0)

    def test_a_few_checkout_visitors_are_in_both_variants_and_no_one_else(self):
        checkout = self.of("checkout_2026")
        seen = checkout.groupby("utente_id").variante.nunique()
        crossing = int((seen > 1).sum())
        self.assertGreaterEqual(crossing, 0.0005 * checkout.utente_id.nunique())
        self.assertLessEqual(crossing, 0.0015 * checkout.utente_id.nunique())
        self.assertTrue((self.users.groupby(["esperimento_id", "utente_id"]).size() <= 2).all())
        for other in ("banner_home", "spedizione_gratuita"):
            self.assertFalse(self.of(other).utente_id.duplicated().any())

    def test_the_dates_are_inside_each_experiment_and_the_weekend_is_busier(self):
        for _, e in self.experiments.iterrows():
            d = self.of(e.id).data_ingresso
            self.assertGreaterEqual(d.min(), e.inizio)
            self.assertLessEqual(d.max(), e.fine)
            self.assertEqual((e.fine - e.inizio).days + 1, {"checkout_2026": 42, "banner_home": 28, "spedizione_gratuita": 35}[e.id])
            self.assertEqual(d.nunique(), (e.fine - e.inizio).days + 1)    # every day has visitors
        checkout = self.of("checkout_2026")
        per_day = checkout.groupby("data_ingresso").size()
        weekend = per_day[per_day.index.dayofweek >= 5].mean()
        weekdays = per_day[per_day.index.dayofweek < 5].mean()
        self.assertGreater(weekend, 1.1 * weekdays)

    def test_the_history_is_the_eight_weeks_before_the_first_experiment(self):
        first = self.experiments.inizio.min()
        self.assertEqual(self.history.data_prima_visita.min(), first - pd.Timedelta(days=56))
        self.assertEqual(self.history.data_prima_visita.max(), first - pd.Timedelta(days=1))

    # ── what each visitor did ────────────────────────────────────────────────
    def test_the_numbers_are_coherent(self):
        for df in (self.users, self.history):
            self.assertTrue((df.sessioni >= 1).all())
            self.assertTrue((df.click <= df.impression).all())
            self.assertTrue((df.impression >= 1).all())
            self.assertTrue((df.ricavo[~df.convertito] == 0).all())
            self.assertTrue((df.ricavo[df.convertito] > 0).all())
        # the covariates are known together or not at all
        self.assertTrue((self.users.ricavo_pre_28g.isna() == self.users.sessioni_pre_28g.isna()).all())

    def test_the_new_visitors_have_no_covariate(self):
        for df in (self.users, self.history):
            share = df.ricavo_pre_28g.isna().mean()
            self.assertAlmostEqual(share, 0.40, delta=0.02)
        self.assertTrue((self.users.ricavo_pre_28g.dropna() >= 0).all())
        # a visitor of the checkout seen twice is the same visitor: new or not in both rows
        checkout = self.of("checkout_2026")
        twice = checkout[checkout.duplicated("utente_id", keep=False)]
        self.assertEqual(twice.groupby("utente_id").ricavo_pre_28g.apply(lambda s: s.isna().nunique()).max(), 1)

    def test_the_covariate_is_correlated_with_the_revenue(self):
        for e in ("checkout_2026", "spedizione_gratuita"):
            known = self.of(e).dropna(subset=["ricavo_pre_28g"])
            rho = np.corrcoef(known.ricavo, known.ricavo_pre_28g)[0, 1]
            self.assertGreater(rho, 0.4, e)
            self.assertLess(rho, 0.62, e)

    def test_the_revenue_has_a_heavy_tail(self):
        paid = self.of("checkout_2026").ricavo[lambda s: s > 0]
        self.assertGreater(paid.max(), 15 * paid.median())
        self.assertGreater(paid.quantile(0.99), 3 * paid.median())
        self.assertGreater(((paid - paid.mean()) ** 4).mean() / paid.var(ddof=0) ** 2, 10)     # the kurtosis of a lognormal with bulk orders

    # ── the three experiments ────────────────────────────────────────────────
    def test_checkout_B_converts_more_than_A_and_it_is_significant(self):
        c = self.of("checkout_2026")
        a, b = c[c.variante == "A"].convertito.astype(float), c[c.variante == "B"].convertito.astype(float)
        diff, z, p = two_proportions(a, b)
        self.assertGreater(diff, 0)
        self.assertLess(p, 0.001)
        lift = b.mean() / a.mean() - 1
        self.assertGreater(lift, 0.03)     # built-in: +5% relative
        self.assertLess(lift, 0.12)

    def test_checkout_a_converters_revenue_is_the_same_in_both_variants(self):
        c = self.of("checkout_2026")
        paid = c[c.convertito]
        a, b = paid[paid.variante == "A"].ricavo, paid[paid.variante == "B"].ricavo
        # Welch's t on the difference of means: nowhere near significant (the tail is heavy: a normal tail is a good enough witness)
        t = (b.mean() - a.mean()) / math.sqrt(a.var() / len(a) + b.var() / len(b))
        self.assertLess(abs(t), 2.0)

    def test_the_checkout_and_the_banner_are_split_as_planned(self):
        self.assertGreater(srm_p(self.of("checkout_2026").variante.value_counts().tolist()), 0.01)
        banner = self.of("banner_home").variante.value_counts()
        self.assertEqual(sorted(banner.index), ["A", "B", "C", "D"])
        self.assertGreater(srm_p(banner.tolist()), 0.01)

    def test_banner_ratios_B_above_A_C_below(self):
        b = self.of("banner_home")
        ratio = b.groupby("variante").click.sum() / b.groupby("variante").impression.sum()
        self.assertGreater(ratio["B"] / ratio["A"], 1.02)    # built-in: +6%
        self.assertLess(ratio["C"], ratio["A"])              # built-in: -4%
        self.assertLess(abs(ratio["D"] / ratio["A"] - 1), 0.05)

    def test_shipping_has_a_sample_ratio_mismatch_far_below_0_001(self):
        counts = self.of("spedizione_gratuita").variante.value_counts()
        self.assertGreater(counts["A"], counts["B"])
        self.assertLess(srm_p([counts["A"], counts["B"]]), 0.0005)

    def test_the_mismatch_comes_from_a_filter_on_B_alone_from_day_12_on_the_least_engaged(self):
        s = self.of("spedizione_gratuita")
        day = (s.data_ingresso - s.data_ingresso.min()).dt.days + 1
        early, late = day < g.EXP_BOT_FROM_DAY, day >= g.EXP_BOT_FROM_DAY
        count = lambda v, rows: int(((s.variante == v) & rows).sum())
        # before day 12 the split is the planned one; after it B is short, and A is not
        self.assertGreater(srm_p([count("A", early), count("B", early)]), 0.01)
        self.assertLess(srm_p([count("A", late), count("B", late)]), 0.0001)
        self.assertGreater(count("A", late), count("B", late))
        # those who are missing engage least: B's sessions after day 12 are above A's
        later = s[late]
        self.assertGreater(later[later.variante == "B"].sessioni.mean(), later[later.variante == "A"].sessioni.mean())

    def test_B_looks_better_than_it_is(self):
        s = self.of("spedizione_gratuita")
        a, b = s[s.variante == "A"].ricavo.mean(), s[s.variante == "B"].ricavo.mean()
        self.assertGreater(b / a - 1, 0.03)    # the built-in +3%, and what the filter adds

    # ── the aggregated form ──────────────────────────────────────────────────
    def test_the_daily_table_is_the_visitors_summed(self):
        self.assertEqual(len(self.daily), 42 * 2 + 28 * 4 + 35 * 2)
        by = self.users.assign(q=self.users.ricavo ** 2, c=self.users.convertito.astype(int)).groupby(["esperimento_id", "variante"])
        for (e, v), rows in by:
            d = self.daily[(self.daily.esperimento_id == e) & (self.daily.variante == v)]
            self.assertEqual(d.utenti.sum(), len(rows))
            self.assertEqual(d.conversioni.sum(), rows.c.sum())
            self.assertAlmostEqual(d.ricavo_totale.sum(), rows.ricavo.sum(), places=4)
            self.assertAlmostEqual(d.ricavo_quadrati.sum(), rows.q.sum(), delta=1e-6 * rows.q.sum())

    # ── what ClickHouse takes ────────────────────────────────────────────────
    def test_the_tables_are_ready_for_clickhouse(self):
        users = g._exp_table(self.users)
        self.assertEqual(str(users.utente_id.dtype), "int64")
        self.assertEqual(str(users.sessioni.dtype), "uint32")
        self.assertEqual(users.convertito.dtype, bool)
        self.assertEqual(str(users.sessioni_pre_28g.dtype), "UInt32")
        self.assertTrue(users.ricavo.map(lambda v: v.as_tuple().exponent == -2).all())
        nulls = users.ricavo_pre_28g.isna()
        self.assertEqual(int(nulls.sum()), int(self.users.ricavo_pre_28g.isna().sum()))
        self.assertTrue(users.sessioni_pre_28g.isna().equals(nulls))

    def test_the_schema_creates_the_four_tables_and_keeps_the_ids_64_bit(self):
        with open(os.path.join(g.HERE, "schema_experiments.sql"), encoding="utf-8") as f:
            sql = f.read()
        for name in ("esperimenti", "esperimenti_utenti", "esperimenti_storico", "esperimenti_giornaliero"):
            self.assertIn(f"CREATE TABLE analytics.{name} ", sql)
        self.assertEqual(sql.count("utente_id        Int64") + sql.count("utente_id          Int64"), 2)


if __name__ == "__main__":
    unittest.main()
