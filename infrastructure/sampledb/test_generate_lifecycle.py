"""The customers' lifecycle in the sample data generator (generate.py), without any database.

    python3 -m unittest infrastructure/sampledb/test_generate_lifecycle.py

It needs the generator's own libraries (numpy, pandas, Faker, psycopg2); without them the
tests are skipped, so a plain Python without them passes over them. The CI runs them in the
sampledb image, built from requirements.txt's pinned versions, with SAMPLEDB_REQUIRE_LIBS=1 (a
missing library is then an error, not a skip):
    docker build -t tabularia-sampledb:ci infrastructure/sampledb
    docker run --rm -e SAMPLEDB_REQUIRE_LIBS=1 --entrypoint python tabularia-sampledb:ci \
        -m unittest -v test_generate_lifecycle

What they pin: the same data at every run, the orders adding up to the preset's, nothing
bought before the customer was acquired or after it churned (the first order apart), the
churn kept out of what the CRM shows, a spend that does not follow the frequency, and the
shapes the README promises (a channel's rate, acquisitions that fall in August); and, on the
orders as the databases get them, the observed average spend against the frequency: close to
0 inside each channel, while the channels differ in both.
"""
import os
import sys
import unittest
from unittest import mock

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


@unittest.skipUnless(HAVE_LIBS, "numpy, pandas, Faker and psycopg2 are needed")
class Lifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents, cls.intensity, cls.life, cls.cust, cls.day = cls._simulate()

    @staticmethod
    def _simulate():
        g.rng = np.random.default_rng(g.SEED)
        g.Faker.seed(g.SEED)
        agents = g.gen_agents()
        intensity = g.build_day_intensity()
        life = g.simulate_lifecycles(agents, intensity)
        month = pd.to_datetime(np.datetime64(g.START_DATE) + np.arange(g.DAYS + 1).astype("timedelta64[D]")).month.to_numpy()
        cust, day = g.simulate_orders(life, intensity, month)
        return agents, intensity, life, cust, day

    def test_the_data_is_the_same_at_every_run(self):
        _, _, life2, cust2, day2 = self._simulate()
        self.assertTrue(np.array_equal(self.cust, cust2) and np.array_equal(self.day, day2))
        self.assertTrue(np.array_equal(self.life["death"], life2["death"]))

    def test_the_orders_add_up_to_the_preset_and_come_in_order_of_time(self):
        self.assertEqual(len(self.cust), g.P["orders"])
        self.assertTrue(np.all(np.diff(self.day) >= 0))
        self.assertGreaterEqual(self.day.min(), 0)
        self.assertLessEqual(self.day.max(), g.DAYS)

    def test_nobody_buys_before_being_acquired_or_after_churning(self):
        life = self.life
        acq, first, death = life["acq"][self.cust], life["first"][self.cust], life["death"][self.cust]
        self.assertTrue(np.all(self.day >= np.maximum(acq, 0)))
        # the first order of a customer acquired in the window is its first
        new = ~life["initial"][self.cust]
        first_seen = pd.Series(self.day[new]).groupby(self.cust[new]).min()
        self.assertTrue(np.array_equal(first_seen.to_numpy(), life["first"][first_seen.index.to_numpy()]))
        # every other order is before the death
        later = self.day > first
        self.assertTrue(np.all(self.day[later] <= np.floor(death[later])))

    def test_a_third_of_the_customers_are_there_at_the_start_and_the_others_arrive_growing(self):
        life, C = self.life, g.P["customers"]
        self.assertAlmostEqual(life["initial"].mean(), g.INITIAL_SHARE, places=2)
        self.assertTrue(np.all(life["acq"][life["initial"]] < 0))
        arrivals = life["acq"][~life["initial"]]
        self.assertTrue(np.all((arrivals >= 0) & (arrivals <= g.DAYS)))
        per_year = [int(((arrivals >= y * 365) & (arrivals < (y + 1) * 365)).sum()) for y in range(3)]
        self.assertLess(per_year[0], per_year[2])

    def test_acquisitions_fall_in_august_and_cluster_at_the_start_of_the_year(self):
        month = pd.to_datetime(np.datetime64(g.START_DATE) + self.life["acq"][~self.life["initial"]].astype("timedelta64[D]")).month
        counts = pd.Series(month).value_counts()
        self.assertLess(counts[8], 0.5 * counts.mean())
        self.assertGreater(counts[1], counts.mean())

    def test_the_channel_sets_the_rate_and_the_churn(self):
        life = self.life
        rate = pd.Series(life["rate"]).groupby(np.array(g.CHANNELS)[life["channel_idx"]]).mean()
        self.assertGreater(rate["Supermercati"], rate["Negozi"])
        hazard = pd.Series(life["hazard"]).groupby(np.array(g.CHANNELS)[life["channel_idx"]]).mean()
        self.assertGreater(hazard["Online"], 2 * hazard["Supermercati"])

    def test_the_size_of_the_orders_does_not_follow_the_frequency(self):
        life = self.life
        rate = life["rate"] / pd.Series(life["rate"]).groupby(life["channel_idx"]).transform("mean").to_numpy()
        self.assertLess(abs(np.corrcoef(rate, life["spend"])[0, 1]), 0.08)

    def test_the_crm_status_is_what_a_crm_sees_and_never_the_churn(self):
        life, C = self.life, g.P["customers"]
        last = np.full(C, -1)
        np.maximum.at(last, self.cust, self.day)
        status = g.crm_status(life, last)
        self.assertEqual(list(status.columns), ["stato", "attivo"])
        self.assertEqual(set(status.stato), {"attivo", "inattivo", "nuovo"})
        stale = status.stato.eq("inattivo").to_numpy() & (last >= 0)
        self.assertTrue(np.all(g.DAYS - last[stale] > g.INACTIVE_AFTER_DAYS))
        self.assertTrue(np.all(g.DAYS - last[status.stato.eq("attivo").to_numpy()] <= g.INACTIVE_AFTER_DAYS))
        # inattivo is not «lost»: some inactive customers are still alive (slow ones)
        alive_but_inactive = status.stato.eq("inattivo").to_numpy() & (life["death"] > g.DAYS)
        self.assertGreater(alive_but_inactive.sum(), 0)
        # and the customers' table has no column that says it
        self.assertNotIn("death", g.gen_customers(self.agents, life).columns)


@unittest.skipUnless(HAVE_LIBS, "numpy, pandas, Faker and psycopg2 are needed")
class ObservedSpend(unittest.TestCase):
    """The spend and the frequency as a user measures them on the orders (docs/capabilities/models.md,
    the customer lifetime example): the customers acquired in the window, the orders that were not
    cancelled, one purchase per customer and day, the repeat purchases after the first day; x their
    count, m̄ their average amount. Gamma–Gamma assumes m̄ independent of x: inside a channel it is
    (each customer's order size is drawn apart from its rate), over the whole base it is not, because
    the channels differ both in the size of their orders and in how often they order. That is meant:
    the report's spend check sees it, and the page proposes a fit per channel."""

    @classmethod
    def setUpClass(cls):
        tables = {}
        with mock.patch.object(g, "copy_df", lambda cur, table, df: tables.setdefault(table, []).append(df)), \
                mock.patch.object(g, "log", lambda *a, **k: None):
            g.rng = np.random.default_rng(g.SEED)
            g.Faker.seed(g.SEED)
            agents = g.gen_agents()
            intensity = g.build_day_intensity()
            life = g.simulate_lifecycles(agents, intensity)
            cust = g.gen_customers(agents, life)
            prod = g.build_products()
            g.gen_orders_and_lines(None, prod, life, intensity, cust.attrs["agent_of"], cust.attrs["channel_of"], agents)
        orders = pd.concat(tables["vendite.ordini"])
        lines = pd.concat(tables["vendite.righe_ordine"])
        amount = (lines.quantita * lines.prezzo_unitario * (1 - lines.sconto_pct / 100)).groupby(lines.ordine_id).sum()
        orders["amount"] = orders.id.map(amount)
        kept = orders[(orders.stato != "annullato") & orders.cliente_id.isin(np.where(~life["initial"])[0] + 1)]
        day = kept.groupby(["cliente_id", "data_ordine", "canale"], as_index=False).amount.sum()
        repeat = day[day.data_ordine > day.groupby("cliente_id").data_ordine.transform("min")]
        cls.summary = repeat.groupby(["cliente_id", "canale"]).amount.agg(x="size", m="mean").reset_index()

    def test_inside_a_channel_the_average_spend_does_not_follow_the_frequency(self):
        for channel, q in self.summary.groupby("canale"):
            r = np.corrcoef(q.x, q.m)[0, 1]
            self.assertGreater(len(q), 100, channel)
            self.assertLess(abs(r), 0.12, f"{channel}: r = {r:.3f} on {len(q)} repeat customers")

    def test_the_channels_differ_in_both_the_size_of_their_orders_and_their_frequency(self):
        by = self.summary.groupby("canale").agg(x=("x", "mean"), m=("m", "mean"))
        # Supermercati and Ingrosso order in bulk; Supermercati often, Ingrosso rarely
        for bulk in ("Supermercati", "Ingrosso"):
            for small in ("Negozi", "Ristorazione", "Online"):
                self.assertGreater(by.m[bulk], 1.5 * by.m[small], (bulk, small))
        self.assertGreater(by.x["Supermercati"], 2 * by.x["Ingrosso"])
        self.assertGreater(by.x["Ristorazione"], 2 * by.x["Online"])


if __name__ == "__main__":
    unittest.main()
