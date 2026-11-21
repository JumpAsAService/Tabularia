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
tables as the databases get them: the observed average spend against the frequency (close to
0 inside each channel, while the channels differ in both), no fact of a customer before its
agent was hired, the agent of the customer's macro-area, orders in progress only at the
window's end, «nuovo» for a customer with nothing delivered yet, and an outcome «ordine» that
an order follows.
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


_TABLES = {}


def tables() -> tuple[dict, dict]:
    """Every table at `small` as the databases get them (copy_df and insert_df caught), and the
    lives: made once for the classes below."""
    if not _TABLES:
        out = {}

        class CH:
            def insert_df(self, table, df):
                out.setdefault(table, []).append(df.copy())

        with mock.patch.object(g, "copy_df", lambda cur, table, df: out.setdefault(table, []).append(df.copy())), \
                mock.patch.object(g, "log", lambda *a, **k: None):
            g.rng = np.random.default_rng(g.SEED)
            g.Faker.seed(g.SEED)
            agents = g.gen_agents()
            intensity = g.build_day_intensity()
            life = g.simulate_lifecycles(agents, intensity)
            cust = g.gen_customers(agents, life)
            prod = g.build_products()
            agent_of = cust.attrs["agent_of"]
            orders = g.gen_orders_and_lines(None, prod, life, intensity, agent_of, cust.attrs["channel_of"], agents)
            cust = pd.concat([cust, g.crm_status(life, orders["last_order"], orders["delivered"])], axis=1)
            g.gen_activities(CH(), agent_of, life, intensity, orders)
        out = {k: pd.concat(v, ignore_index=True) for k, v in out.items()}
        out["analytics.agenti"] = agents
        out["analytics.clienti"] = cust
        _TABLES.update(tables=out, life=life)
    return _TABLES["tables"], _TABLES["life"]


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
        t, life = tables()
        orders = t["vendite.ordini"].copy()
        lines = t["vendite.righe_ordine"]
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


@unittest.skipUnless(HAVE_LIBS, "numpy, pandas, Faker and psycopg2 are needed")
class Consistency(unittest.TestCase):
    """The tables agree with each other as a real ERP and CRM would."""

    @classmethod
    def setUpClass(cls):
        t, cls.life = tables()
        cls.agents = t["analytics.agenti"].assign(data_assunzione=lambda a: pd.to_datetime(a.data_assunzione))
        cls.customers = t["analytics.clienti"].assign(data_acquisizione=lambda c: pd.to_datetime(c.data_acquisizione))
        cls.orders = t["vendite.ordini"].assign(data_ordine=lambda o: pd.to_datetime(o.data_ordine))
        cls.activities = t["analytics.attivita_crm"].assign(data=lambda a: pd.to_datetime(a.data))
        cls.end = pd.Timestamp(g.END_DATE)

    def test_nothing_of_a_customer_comes_before_its_agent_was_hired(self):
        hired = self.agents.set_index("id").data_assunzione
        self.assertTrue((self.customers.data_acquisizione >= self.customers.agente_id.map(hired)).all())
        self.assertTrue((self.orders.data_ordine >= self.orders.agente_id.map(hired)).all())
        self.assertTrue((self.activities.data >= self.activities.agente_id.map(hired)).all())
        # the facts carry the customer's agent
        agent = self.customers.set_index("id").agente_id
        self.assertTrue((self.orders.agente_id == self.orders.cliente_id.map(agent)).all())
        # agents are still hired over the years, some inside the window
        agents = self.agents[self.agents.ruolo == "agente"]
        self.assertGreater(int((agents.data_assunzione > pd.Timestamp(g.START_DATE)).sum()), 0)

    def test_a_customer_s_agent_is_of_its_macro_area_and_every_area_has_its_network(self):
        macro = {r[0]: r[1] for r in g.REGIONS}
        area = self.agents.set_index("id").area
        self.assertTrue((self.customers.regione.map(macro) == self.customers.agente_id.map(area)).all())
        self.assertEqual(set(self.agents[self.agents.ruolo == "capo_area"].area), set(g.AREAS))
        boss = self.agents.set_index("id")
        under = self.agents[self.agents.ruolo != "capo_area"]
        self.assertTrue((under.area.to_numpy() == under.responsabile_id.map(boss.area).to_numpy()).all())

    def test_orders_in_progress_are_the_window_s_last_ones(self):
        state = self.orders.stato.value_counts()
        self.assertEqual(set(state.index), {"evaso", "annullato", "in_lavorazione"})
        age = (self.end - self.orders.data_ordine[self.orders.stato == "in_lavorazione"]).dt.days
        self.assertTrue((age <= g.IN_PROGRESS_DAYS).all())
        recent = self.orders[(self.end - self.orders.data_ordine).dt.days <= 3]
        self.assertGreater((recent.stato == "in_lavorazione").mean(), 0.3)

    def test_nuovo_is_a_customer_acquired_lately_with_nothing_delivered(self):
        c = self.customers
        delivered = c.id.isin(self.orders[self.orders.stato == "evaso"].cliente_id)
        recent = (self.end - c.data_acquisizione).dt.days <= g.INACTIVE_AFTER_DAYS
        self.assertTrue(((c.stato == "nuovo") == (recent & ~delivered)).all())
        # among them some have orders, still in progress: they are not «attivo»
        waiting = recent & ~delivered & c.id.isin(self.orders[self.orders.stato == "in_lavorazione"].cliente_id)
        self.assertGreater(int(waiting.sum()), 0)

    def test_an_outcome_ordine_is_followed_by_an_order_and_never_ends_a_complaint(self):
        kept = self.orders[self.orders.stato != "annullato"]
        days = kept.groupby("cliente_id").data_ordine.apply(lambda s: np.sort(s.to_numpy()))
        won = self.activities[self.activities.esito == "ordine"]
        self.assertGreater(len(won), 1000)
        self.assertFalse((won.tipo == "reclamo").any())
        day = won.data.dt.normalize().to_numpy()
        nxt = [d_[np.searchsorted(d_, x)] if np.searchsorted(d_, x) < len(d_) else None
               for d_, x in zip(won.cliente_id.map(days), day)]
        gap = pd.Series([(n - x) / np.timedelta64(1, "D") if n is not None else np.inf for n, x in zip(nxt, day)])
        self.assertTrue((gap <= g.ORDER_OUTCOME_DAYS).all(), gap.max())


if __name__ == "__main__":
    unittest.main()
