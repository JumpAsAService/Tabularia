"""A dashboard's cache (app/reports/tile_cache.py; docs/specs/dashboards.md, «The cache»), on a real
Valkey: its expiries and its locks are the point. The gateway's side is gateway/tests/
test_dashboards_cache.py: there, rights are checked and only what the reader may read comes here.

The rules these tests hold: a hundred people run one query, counted, whichever tiles and states they
ask for; a tile on a dataset refused looks at nothing and says nothing, cache full or empty, in either
order; anything that makes the source or the tile different makes another key, a value of a parameter
never makes the same one; numbers from kept rows are those from the source; those waiting get an
answer or a clear error, never a second query; a lock whose holder died ends and someone takes over;
a refresh goes round everything, at most every 10 seconds for everyone; whatever is too large is not
kept, and is counted; without Valkey or the bucket the numbers still come.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import polars as pl
import pytest
import redis
from fastapi import HTTPException

from app.api.routes import reports as routes
from app.api.routes import tasks
from app.core import panel_settings
from app.reports import data as rdata
from app.reports import frames, tile_cache, tiles
from app.reports.errors import ReportError
from app.tasks.celery_app import celery_app
from app.tasks import report_jobs  # noqa: F401 — its tasks, by name
from tests.fakes import BrokenRedis, FakeStorage

UTC = timezone.utc
ORDERS = {"bucket": "b", "input_key": "datasets/1/one.parquet", "operations": [], "engine": "duckdb"}
PAY = {"bucket": "b", "input_key": "datasets/2/pay.parquet", "operations": [], "engine": "duckdb"}
SECRET_NUMBER = 987654.0


def frame(amounts=(10.0, 20.0, 30.0)) -> pl.DataFrame:
    return pl.DataFrame({"region": [f"R{i}" for i in range(len(amounts))], "amount": list(amounts)})


class Source:
    """The rows behind every spec, counted: what would be a query on a database."""

    def __init__(self):
        self.reads: list[str] = []
        self.frames: dict[str, pl.DataFrame] = {}
        self.delay = 0.0
        self.failure: Exception | None = None
        self.over: set[str] = set()
        self.lock = threading.Lock()

    def read(self, name, spec, cap):
        if "rows" in spec:
            return {c["name"]: c["type"] for c in spec["columns"]}, spec["rows"], False
        key = spec.get("input_key") or spec.get("sql")
        with self.lock:
            self.reads.append(key)
        time.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        df = self.frames.get(key, frame()).clone()
        return {n: str(t) for n, t in df.schema.items()}, df, key in self.over


@pytest.fixture
def valkey(monkeypatch):
    url = os.getenv("VALKEY_TEST_URL")
    if not url:
        pytest.skip("needs a throwaway Valkey (VALKEY_TEST_URL)")
    client = redis.Redis.from_url(url, db=9, decode_responses=True)
    client.flushdb()
    monkeypatch.setattr(tile_cache, "_client", client)
    monkeypatch.setattr(tile_cache, "_down_until", 0.0)
    monkeypatch.setattr(tile_cache, "_bucket_down_until", 0.0)
    monkeypatch.setattr(tile_cache, "_memory_seen", (0.0, False))
    yield client
    client.flushdb()


@pytest.fixture
def bucket(monkeypatch) -> FakeStorage:
    storage = FakeStorage()
    monkeypatch.setattr(tile_cache, "_storage", lambda: storage)
    return storage


@pytest.fixture
def source(monkeypatch) -> Source:
    s = Source()
    monkeypatch.setattr(rdata, "_read_one", s.read)
    return s


@pytest.fixture
def worker(monkeypatch) -> list:
    """The preview worker, in this process, counting how many times it is asked."""
    asked: list = []

    def on_preview_worker(task, kwargs, slot, seconds=None, watch_worker=False):
        asked.append({"task": task, "kwargs": kwargs, "slot": slot, "watch_worker": watch_worker})
        return celery_app.tasks[task].run(**kwargs)

    monkeypatch.setattr(tasks, "on_preview_worker", on_preview_worker)
    return asked


@pytest.fixture
def world(valkey, bucket, source, worker):
    return dict(valkey=valkey, bucket=bucket, source=source, worker=worker)


def item(tile_id="k", value="SUM([amount])", dataset="orders", **props) -> dict:
    return {"id": tile_id, "type": "kpi", "dataset": dataset, "x": 0, "y": 0, "w": 100, "h": 10, "value": value, **props}


def rows_table(tile_id="t", dataset="orders", **props) -> dict:
    return {"id": tile_id, "type": "table", "dataset": dataset, "x": 0, "y": 0, "w": 100, "h": 10,
            "columns": [{"header": "Region", "value": "[region]"}, {"header": "Amount", "value": "[amount]"}], **props}


def definition(*items, names=("orders",), parameters=(), zone="UTC", locale="en", calculated=()) -> dict:
    return {"v": 1, "locale": locale, "timezone": zone, "page": {"size": {"w": 2000, "h": 2000}, "margins": [0, 0, 0, 0]},
            "parameters": list(parameters),
            "datasets": [{"name": n, "source": {"kind": "sql", "sql": "SELECT 1"}, "calculated": list(calculated)} for n in names],
            "sections": {"body": {"items": list(items) or [item()]}}}


def request(d=None, datasets=None, cache=None, **kw) -> routes.DashboardTiles:
    return routes.DashboardTiles(definition=d or definition(), datasets={"orders": ORDERS} if datasets is None else datasets,
                                 cache=cache or {}, **kw)


def ask(*a, **kw) -> dict:
    return asyncio.run(routes.tiles(request(*a, **kw)))


def everyone(n: int, make) -> list:
    async def go():
        return await asyncio.gather(*[routes.tiles(make(i)) for i in range(n)])
    return asyncio.run(go())


NO_ACCESS = {"error": "You cannot read the dataset «pay» this tile needs: ask whoever manages its data", "code": "no_access"}


# ── one query ────────────────────────────────────────────────────────────────
def test_fifty_requests_at_once_make_one_query_and_one_computation(world):
    world["source"].delay = 0.3
    answers = everyone(50, lambda i: request())
    assert len(world["source"].reads) == 1 and len(world["worker"]) == 1
    assert {a["tiles"]["k"]["value"] for a in answers} == {60.0}
    assert {a["tiles"]["k"]["computed_at"] for a in answers} and sum(a["tiles"]["k"]["cached"] is False for a in answers) == 1


def test_fifty_people_asking_for_different_tiles_and_pages_still_read_once(world):
    """Each their own numbers to work out, the rows for all of them read once."""
    d = definition(item("a"), item("b", "COUNT()"), rows_table("t"))
    world["source"].delay = 0.2
    everyone(50, lambda i: request(d, tiles=[["a"], ["b"], ["t"]][i % 3], pages={"t": i % 3}))
    assert len(world["source"].reads) == 1


def test_reading_a_full_cache_queues_nothing(world):
    ask()
    asked, read = len(world["worker"]), len(world["source"].reads)
    for _ in range(5):
        assert ask()["tiles"]["k"]["cached"] is True
    assert (len(world["worker"]), len(world["source"].reads)) == (asked, read)


def test_a_new_computation_is_asked_with_no_slot_that_could_knock_down_who_waits(world):
    ask()
    assert world["worker"][0]["slot"] is None and world["worker"][0]["watch_worker"] is True


def test_a_dataset_read_by_many_tiles_is_read_once_and_a_new_tile_finds_the_rows(world):
    d = definition(item("a"), item("b", "COUNT()"))
    ask(d, tiles=["a"])
    t = ask(d, tiles=["b"])["tiles"]["b"]
    assert world["source"].reads == [ORDERS["input_key"]] and t["value"] == 3 and t["cached"] is True


def test_a_parameter_that_leads_to_the_same_source_reuses_the_rows(world):
    """The value is not in the source: the numbers are worked out again, the data is not read."""
    parameters = [{"name": "p", "type": "text"}]
    d = definition(item("k", "SUM([amount]) + LEN(PARAM(\"p\"))"), parameters=parameters)
    a, b = ask(d, params={"p": "x"}), ask(d, params={"p": "xxx"})
    assert a["tiles"]["k"]["value"] == 61.0 and b["tiles"]["k"]["value"] == 63.0
    assert len(world["source"].reads) == 1 and len(world["worker"]) == 2


def test_other_locales_and_zones_read_the_same_rows_and_say_their_own_numbers(world):
    d = definition(item("k", format="#,##0.00"))
    en = ask(d, locale="en")["tiles"]["k"]
    it = ask(definition(item("k", format="#,##0.00"), zone="Europe/Rome"), locale="it")["tiles"]["k"]
    world["source"].frames[ORDERS["input_key"]] = frame((1000.5,))
    assert en["text"] == "60.00" and it["text"] == "60,00"
    assert len(world["source"].reads) == 1 and len(world["worker"]) == 2


# ── who reads what ───────────────────────────────────────────────────────────
def _refused_tile(d, **kw) -> dict:
    return ask(d, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS}, **kw)


@pytest.mark.parametrize("first", ["who_may", "who_may_not"])
def test_who_may_not_read_a_dataset_gets_only_the_refusal_cache_full_or_empty(world, first):
    """Both orders: what fills the cache does not change a word of what B is told, and B asking first
    leaves nothing A would find."""
    d = definition(item("pay_tile", dataset="pay"), item("mine"), names=("orders", "pay"))
    world["source"].frames[PAY["input_key"]] = frame((SECRET_NUMBER,))
    world["source"].over.add(PAY["input_key"])  # that it is large is not for B to know either

    def a():
        return ask(d, datasets={"orders": ORDERS, "pay": PAY})

    def b():
        return _refused_tile(d)

    answers = {"a": a(), "b": b()} if first == "who_may" else {"b": b(), "a": a()}
    said = answers["b"]
    assert said["tiles"]["pay_tile"] == NO_ACCESS and said["truncated"] == {"orders": False}
    assert said["tiles"]["mine"]["value"] == 60.0
    assert str(SECRET_NUMBER) not in json.dumps(said) and answers["a"]["truncated"]["pay"] is True
    assert answers["a"]["tiles"]["pay_tile"]["code"] == "truncated"


def test_a_tile_refused_touches_nothing_of_the_cache(world, monkeypatch):
    d = definition(item("pay_tile", dataset="pay"), names=("orders", "pay"))
    ask(d, datasets={"pay": PAY})
    looked: list = []
    monkeypatch.setattr(tile_cache, "_values", lambda hashes: looked.append(hashes) or {})
    monkeypatch.setattr(tile_cache, "_take", lambda *a: looked.append(a) or True)
    answer = ask(d, datasets={}, refused={"pay": NO_ACCESS})
    assert answer == {"tiles": {"pay_tile": NO_ACCESS}, "truncated": {}} and all(not x for x in looked)


def test_a_tile_that_reads_a_refused_dataset_through_a_lookup_or_a_field_is_refused_too(world):
    through_scope = item("via_scope", 'SUM([amount], "pay")')
    calculated = {"name": "x", "expr": 'LOOKUP([region], [region], [amount], "pay")'}
    full = definition(through_scope, item("via_field", "MAX([x])"), item("clean", dataset="pay"), names=("orders", "pay"), calculated=[calculated])
    ask(full, datasets={"orders": ORDERS, "pay": PAY})
    answer = ask(full, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS})
    assert all(t == NO_ACCESS for t in answer["tiles"].values()) and answer["truncated"] == {}


def test_two_people_with_different_rights_share_what_both_may_read(world):
    d = definition(item("mine"), item("pay_tile", dataset="pay"), names=("orders", "pay"))
    ask(d, datasets={"orders": ORDERS, "pay": PAY})
    asked = len(world["worker"])
    answer = ask(d, datasets={"orders": ORDERS}, refused={"pay": NO_ACCESS})
    assert answer["tiles"]["mine"]["cached"] is True and len(world["worker"]) == asked


def test_two_readers_who_read_two_files_under_one_name_share_nothing(world):
    """A SQL on the catalog: the same text, each reader's tables (gateway: sql_scripts.resolve). The
    gateway's side shows the specs differ exactly so."""
    def chain(key):
        return {"bucket": "b", "input_key": key, "engine": "duckdb", "operations": [{"type": "sql", "params": {
            "query": 'SELECT * FROM "t0"', "tables": {"t0": {"source": {"bucket": "b", "key": key}}}}}]}

    world["source"].frames["datasets/mine.parquet"] = frame((1.0,))
    world["source"].frames["datasets/theirs.parquet"] = frame((1000.0,))
    mine = ask(datasets={"orders": chain("datasets/mine.parquet")})["tiles"]["k"]
    theirs = ask(datasets={"orders": chain("datasets/theirs.parquet")})["tiles"]["k"]
    assert (mine["value"], theirs["value"]) == (1.0, 1000.0) and theirs["cached"] is False
    assert ask(datasets={"orders": chain("datasets/mine.parquet")})["tiles"]["k"]["value"] == 1.0
    assert len(world["source"].reads) == 2


def test_two_connections_with_one_sql_are_two_sources(world):
    """The same query on another server or as another user is not the same data."""
    def spec(host):
        return {"connection": {"db_type": "postgresql", "host": host, "username": "u", "password_encrypted": "e"}, "sql": "SELECT 1"}

    assert tile_cache.rows_hash(spec("a"), 10) != tile_cache.rows_hash(spec("b"), 10)
    other_user = spec("a")
    other_user["connection"]["username"] = "v"
    assert tile_cache.rows_hash(spec("a"), 10) != tile_cache.rows_hash(other_user, 10)


def test_nothing_in_the_keys_names_a_person_and_no_answer_is_kept_with_one(world):
    """Two requests with nothing in common but the source: what is kept has no reader in it, so that
    what depends on a reader can only be in the source, which is the key."""
    ask()
    for key in world["valkey"].keys("dataprep:dashboards:value:*"):
        kept = json.loads(world["valkey"].get(key))
        assert set(kept) == {"tile", "truncated"} and "user" not in json.dumps(kept)


# ── what makes another key ───────────────────────────────────────────────────
def _tile_hash(**over) -> str:
    base = dict(item={"id": "k", "type": "kpi", "value": "SUM([a])", "x": 0, "y": 0}, datasets={"d": {"reads": "r", "generation": ""}},
                params={"p": 1}, parameters=[], page=0, state=None, locale="en", zone="UTC", title="T", cap=10, seconds=300)
    return tile_cache.tile_hash(**{**base, **over})


@pytest.mark.parametrize("change", [
    {"item": {"id": "k", "type": "kpi", "value": "SUM([b])"}}, {"datasets": {"d": {"reads": "other", "generation": ""}}},
    {"datasets": {"d": {"reads": "r", "generation": "new"}}}, {"params": {"p": 2}}, {"page": 1},
    {"state": {"sort": {"column": 1, "dir": "desc"}}}, {"state": {"collapsed": [["North"]]}}, {"locale": "it"}, {"zone": "Europe/Rome"},
    {"title": "Other"}, {"cap": 11}, {"seconds": 600}, {"clicks": [{"tile": "c", "values": ["North"]}]},
])
def test_anything_the_numbers_depend_on_makes_another_key(change):
    assert _tile_hash(**change) != _tile_hash()


def test_a_sort_a_closed_group_and_a_page_are_answered_as_asked_not_as_someone_else_asked(world):
    n = 120
    world["source"].frames[ORDERS["input_key"]] = pl.DataFrame({"region": [f"R{i % 2}" for i in range(n)], "amount": [float(i) for i in range(n)]})
    d = definition(rows_table("t", interactive={"sort": True, "toggle": True}))
    first = ask(d)["tiles"]["t"]["rows"][0]["cells"][1]["value"]
    down = {"items": {"t": {"sort": {"column": 1, "dir": "desc"}}}}
    assert ask(d, state=down)["tiles"]["t"]["rows"][0]["cells"][1]["value"] == n - 1 != first
    assert ask(d)["tiles"]["t"]["rows"][0]["cells"][1]["value"] == first
    assert ask(d, pages={"t": 1})["tiles"]["t"]["rows"][0]["cells"][1]["value"] == 50.0
    grouped = definition(rows_table("t", groups=[{"by": "[region]"}], interactive={"toggle": True}))
    closed = {"items": {"t": {"collapsed": [["R0"]]}}}
    open_rows, closed_rows = ask(grouped)["tiles"]["t"], ask(grouped, state=closed)["tiles"]["t"]
    assert open_rows["total_rows"] != closed_rows["total_rows"] and closed_rows["rows"][0]["closed"] is True
    assert ask(definition(rows_table("other", interactive={"sort": True})), state=down)["tiles"]["other"]["rows"][0]["cells"][1]["value"] == first  # another's state


def test_a_title_a_locale_and_a_calculated_field_are_answered_as_asked(world):
    plain, doubled = definition(item("k", "SUM([x])"), calculated=[{"name": "x", "expr": "[amount]"}]), definition(item("k", "SUM([x])"), calculated=[{"name": "x", "expr": "[amount] * 2"}])
    assert ask(plain)["tiles"]["k"]["value"] == 60.0 and ask(doubled)["tiles"]["k"]["value"] == 120.0
    text = definition({"id": "t", "type": "text", "dataset": "orders", "x": 0, "y": 0, "w": 100, "h": 10, "value": '=REPORT() & ""'})
    assert ask(text, title="One")["tiles"]["t"]["text"] == "One" and ask(text, title="Two")["tiles"]["t"]["text"] == "Two"
    assert len(world["source"].reads) == 1


def test_where_a_tile_sits_on_the_grid_makes_no_other_key():
    assert _tile_hash(item={"id": "k", "type": "kpi", "value": "SUM([a])", "x": 9, "y": 99, "w": 1, "h": 1}) == _tile_hash()


def test_a_new_snapshot_is_never_served_the_old_numbers(world):
    world["source"].frames["datasets/1/older.parquet"] = frame((1.0,))
    world["source"].frames["datasets/1/newer.parquet"] = frame((2.0,))
    older = {**ORDERS, "input_key": "datasets/1/older.parquet"}
    assert ask(datasets={"orders": older})["tiles"]["k"]["value"] == 1.0
    newer = ask(datasets={"orders": {**older, "input_key": "datasets/1/newer.parquet"}})["tiles"]["k"]
    assert newer["value"] == 2.0 and newer["cached"] is False


@pytest.mark.parametrize("one, other", [
    ({"p": 'a","q":"b'}, {"p": "a", "q": "b"}), ({"p": 1}, {"p": "1"}), ({"p": ["a", "b"]}, {"p": "a,b"}),
    ({"p": None}, {"p": "null"}), ({"p": "\u00e9"}, {"p": "e\u0301"}), ({"p": "a:b"}, {"p:a": "b"}),
])
def test_two_values_never_pass_for_one(one, other):
    assert _tile_hash(params=one) != _tile_hash(params=other)


def test_hostile_values_reach_valkey_only_as_a_hash(world):
    parameters = [{"name": "p", "type": "text"}]
    d = definition(item("k", "SUM([amount]) + LEN(PARAM(\"p\"))"), parameters=parameters)
    for value in ("dataprep:dashboards:lock:*", "\r\nFLUSHALL\r\n", "x" * 100_000, "' OR 1=1 --", "\u0000"):
        ask(d, params={"p": value})
    keys = world["valkey"].keys("*")
    assert world["valkey"].keys("dataprep:dashboards:value:*")
    assert all(re.fullmatch(r"dataprep:dashboards:(value|rows|gen|lock):[0-9a-f]{64}|dataprep:metrics:dashboard_cache|dataprep:stepcache:dashboards.*", k) for k in keys), keys


# ── the numbers from kept rows ───────────────────────────────────────────────
def test_the_same_numbers_from_the_rows_kept_and_from_the_source_both_ways(world, monkeypatch):
    df = pl.DataFrame({
        "region": ["Nord", "Città «Sud»", None, "Nord"], "amount": [1.5, None, -2.25, 4.0], "qty": [1, 2, None, 4], "ok": [True, False, None, True],
        "price": pl.Series([Decimal("1.10"), Decimal("2.20"), None, Decimal("3.30")], dtype=pl.Decimal(10, 2)),
        "day": [date(2026, 1, 1), date(2026, 2, 1), None, date(2026, 3, 1)],
        "at": pl.Series([datetime(2026, 1, 1, 22, 30), None, datetime(2026, 2, 1), datetime(2026, 3, 1)], dtype=pl.Datetime("us")),
        "at_utc": pl.Series([datetime(2026, 1, 1, 23, 30, tzinfo=UTC)] * 4, dtype=pl.Datetime("us", "UTC")),
    })
    world["source"].frames[ORDERS["input_key"]] = df
    d = definition(item("sum", "SUM([amount])"), item("n", "COUNT([qty])"), item("money", "SUM([price])", format="#,##0.00"),
                   item("last", "MAX([day])"), item("first", 'FORMAT(MIN([at]), "yyyy-MM-dd HH:mm")'), item("zoned", 'FORMAT(MIN([at_utc]), "yyyy-MM-dd HH:mm")'),
                   item("yes", "COUNT([ok])"),
                   {"id": "c", "type": "chart", "x": 0, "y": 0, "w": 100, "h": 10, "dataset": "orders", "kind": "column", "category": "[region]",
                    "series": [{"value": "SUM([price])", "name": "P"}]},
                   rows_table("t", columns=[{"header": h, "value": f"[{h}]"} for h in df.columns]), zone="Europe/Rome")

    def clean(answer):
        return {k: {f: v for f, v in t.items() if f not in ("computed_at", "cached")} for k, t in answer["tiles"].items()}

    for columns in (None, False):
        monkeypatch.setattr(frames, "COLUMNS", columns)
        world["valkey"].flushdb()
        world["source"].reads.clear()
        cold = tiles.compute(d, {"orders": ORDERS}, read=tile_cache.reader(300))
        kept = tiles.compute(d, {"orders": ORDERS}, read=tile_cache.reader(300))
        assert world["source"].reads == [ORDERS["input_key"]]
        assert clean(cold) == clean(kept) and "error" not in json.dumps(clean(cold))
        assert kept["tiles"]["sum"]["cached"] is True and cold["tiles"]["sum"]["cached"] is False


# ── computed_at, cached ──────────────────────────────────────────────────────
def test_computed_at_says_when_the_data_was_read_and_cached_whether_this_answer_went_to_it(world):
    d = definition(item("a"), rows_table("t"))
    first = ask(d, tiles=["a"])["tiles"]["a"]
    assert first["cached"] is False
    time.sleep(0.05)
    again = ask(d, tiles=["a"])["tiles"]["a"]
    assert again["cached"] is True and again["computed_at"] == first["computed_at"]
    worked_later = ask(d, tiles=["t"])["tiles"]["t"]  # new numbers, the same rows: not newer than they are
    assert worked_later["cached"] is True and worked_later["computed_at"] == first["computed_at"]
    assert datetime.fromisoformat(first["computed_at"]).tzinfo is not None


def test_numbers_worked_out_late_end_with_the_rows_they_come_from(world, monkeypatch):
    """Without this, a click at the end of the rows' life would show data twice as old as promised."""
    tile = {"computed_at": datetime.fromtimestamp(time.time() - 100, UTC).isoformat()}
    assert tile_cache._keep_for(tile, 300) == pytest.approx(200, abs=2)
    assert tile_cache._keep_for({"computed_at": datetime.fromtimestamp(time.time() - 1000, UTC).isoformat()}, 300) == 1.0
    assert tile_cache._keep_for({"error": "x", "code": "truncated"}, 300) == 300
    assert tile_cache._keep_for({"error": "x", "code": "error"}, 300) == tile_cache.FAILED_SECONDS


def test_a_dashboard_with_a_shorter_life_does_not_read_rows_older_than_it(world):
    ask(definition(item("a")), cache={"refresh_seconds": 3600})
    key = next(iter(world["valkey"].keys("dataprep:dashboards:rows:*")))
    entry = json.loads(world["valkey"].get(key))
    entry["at"] = datetime.fromtimestamp(time.time() - 1000, UTC).isoformat()
    world["valkey"].set(key, json.dumps(entry))
    ask(definition(item("b", "COUNT()")))  # 300 seconds
    assert len(world["source"].reads) == 2
    ask(definition(item("c", "MAX([amount])")), cache={"refresh_seconds": 3600})
    assert len(world["source"].reads) == 2


def test_the_life_is_the_setting_and_at_least_the_dashboard_s_interval(world):
    assert tile_cache.lifetime(0) == 300 and tile_cache.lifetime(60) == 300 and tile_cache.lifetime(3600) == 3600
    panel_settings.write({"dashboards.cache_seconds": 900})
    panel_settings.forget()
    assert tile_cache.lifetime(0) == 900


def test_what_is_kept_ends_with_its_life(world):
    ask()
    ttls = [world["valkey"].ttl(k) for k in world["valkey"].keys("dataprep:dashboards:value:*")] + [
        world["valkey"].ttl(k) for k in world["valkey"].keys("dataprep:dashboards:rows:*")]
    assert ttls and all(0 < t <= 300 for t in ttls)


# ── locks ────────────────────────────────────────────────────────────────────
def _tile_hashes(monkeypatch, world, *a, **kw) -> list[str]:
    """The hashes of a request, without leaving anything behind."""
    seen: list[str] = []
    real = tile_cache._take

    def spy(h, token):
        seen.append(h)
        return real(h, token)

    monkeypatch.setattr(tile_cache, "_take", spy)
    ask(*a, **kw)
    monkeypatch.setattr(tile_cache, "_take", real)
    world["valkey"].flushdb()
    world["worker"].clear()
    world["source"].reads.clear()
    return seen


def test_an_orphan_lock_ends_and_one_of_those_waiting_takes_its_place(world, monkeypatch):
    h = _tile_hashes(monkeypatch, world)[0]
    world["valkey"].set(tile_cache._lock_key(h), "a process that died", ex=1)
    started = time.monotonic()
    answers = everyone(10, lambda i: request())
    assert time.monotonic() - started >= 0.8
    assert len(world["worker"]) == 1 and {a["tiles"]["k"]["value"] for a in answers} == {60.0}


def test_a_lock_taken_ends_by_itself_if_nobody_keeps_it(world):
    """What makes an orphan end: whoever took it may die in the next instant."""
    assert tile_cache._take("a" * 64, "me") and not tile_cache._take("a" * 64, "another")
    assert 0 < world["valkey"].ttl(tile_cache._lock_key("a" * 64)) <= tile_cache.LOCK_SECONDS


def test_an_orphan_lock_on_the_rows_ends_too(world, monkeypatch):
    h = tile_cache.rows_hash(ORDERS, tiles.max_rows())
    world["valkey"].set(tile_cache._lock_key(h), "a worker that died", ex=1)
    started = time.monotonic()
    ds = tile_cache.read_rows("orders", ORDERS, tiles.max_rows(), datetime.now().astimezone().tzinfo, 300)
    assert time.monotonic() - started >= 0.8 and ds.columns and len(world["source"].reads) == 1


def test_past_the_wait_who_waits_says_so_and_does_not_query(world, monkeypatch):
    h = _tile_hashes(monkeypatch, world)[0]
    world["valkey"].set(tile_cache._lock_key(h), "someone slow", ex=60)
    monkeypatch.setattr(tile_cache, "WAIT_SECONDS", 0.2)
    t = ask()["tiles"]["k"]
    assert t == {"error": tile_cache.BUSY, "code": "error"} and world["worker"] == [] and world["source"].reads == []


def test_on_the_rows_too_past_the_wait_it_is_an_error_of_that_tile_and_no_second_query(world, monkeypatch):
    h = tile_cache.rows_hash(ORDERS, tiles.max_rows())
    world["valkey"].set(tile_cache._lock_key(h), "someone slow", ex=60)
    monkeypatch.setattr(tile_cache, "WAIT_SECONDS", 0.2)
    t = ask()["tiles"]["k"]
    assert t["code"] == "error" and "still being read" in t["error"] and world["source"].reads == []


def test_a_slow_computation_keeps_its_lock_past_the_lock_s_life(world, monkeypatch):
    """A lock lasts seconds, a query minutes: as long as the holder lives, nobody takes over."""
    monkeypatch.setattr(tile_cache, "LOCK_SECONDS", 1)
    world["source"].delay = 2.5
    answers = everyone(5, lambda i: request())
    assert len(world["source"].reads) == 1 and len(world["worker"]) == 1 and {a["tiles"]["k"]["value"] for a in answers} == {60.0}


def test_those_waiting_get_the_failure_of_who_was_computing_and_ask_again_only_later(world, monkeypatch):
    monkeypatch.setattr(tile_cache, "FAILED_SECONDS", 1)
    calls = []

    def fails(task, kwargs, slot, seconds=None, watch_worker=False):
        calls.append(1)
        time.sleep(0.3)
        raise HTTPException(status_code=503, detail="The worker computing this was lost: try again")

    monkeypatch.setattr(tasks, "on_preview_worker", fails)

    async def go():
        return await asyncio.gather(*[routes.tiles(request()) for _ in range(10)], return_exceptions=True)

    results = asyncio.run(go())
    failures = [r for r in results if isinstance(r, HTTPException)]
    waited = [r for r in results if not isinstance(r, Exception)]
    assert len(calls) == 1 and len(failures) == 1 and failures[0].status_code == 503
    assert len(waited) == 9 and all(r["tiles"]["k"] == {"error": "The worker computing this was lost: try again", "code": "error"} for r in waited)
    time.sleep(1.1)
    monkeypatch.setattr(tasks, "on_preview_worker", lambda task, kwargs, slot, seconds=None, watch_worker=False: celery_app.tasks[task].run(**kwargs))
    assert ask()["tiles"]["k"]["value"] == 60.0


def test_a_source_that_fails_is_asked_once_by_those_waiting_and_recovers(world, monkeypatch):
    monkeypatch.setattr(tile_cache, "FAILED_SECONDS", 1)
    world["source"].failure = ReportError("dataset «orders»: the server is down")
    world["source"].delay = 0.1
    answers = everyone(10, lambda i: request())
    assert len(world["source"].reads) == 1 and all(a["tiles"]["k"]["code"] == "error" and "down" in a["tiles"]["k"]["error"] for a in answers)
    world["source"].failure = None
    time.sleep(1.1)
    assert ask()["tiles"]["k"]["value"] == 60.0


def test_what_a_failure_says_is_not_read_under_another_dataset_s_name(world, monkeypatch):
    """Its message names a dataset of someone else's dashboard, whose source is the same."""
    world["source"].failure = ReportError("dataset «private_name»: down")
    with pytest.raises(ReportError):
        tile_cache.read_rows("private_name", ORDERS, 10, UTC, 300)
    world["source"].failure = ReportError("dataset «mine»: down")
    with pytest.raises(ReportError) as e:
        tile_cache.read_rows("mine", ORDERS, 10, UTC, 300)
    assert "private_name" not in e.value.message


def test_a_dataset_over_the_cap_is_remembered_with_no_number_and_no_new_query(world):
    world["source"].over.add(ORDERS["input_key"])
    d = definition(item("a"), item("b", "COUNT()"))
    first = ask(d, tiles=["a"])
    second = ask(d, tiles=["b"])
    assert first["tiles"]["a"]["code"] == second["tiles"]["b"]["code"] == "truncated"
    assert "value" not in second["tiles"]["b"] and first["truncated"] == second["truncated"] == {"orders": True}
    assert len(world["source"].reads) == 1


def test_many_workers_at_once_read_the_rows_once(world):
    world["source"].delay = 0.3
    results: list = []

    def read():
        results.append(tile_cache.read_rows("orders", ORDERS, 100, UTC, 300))

    threads = [threading.Thread(target=read) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(world["source"].reads) == 1 and len(results) == 8 and sum(d.from_cache for d in results) == 7


# ── a refresh ────────────────────────────────────────────────────────────────
def test_a_refresh_goes_round_both_levels_once_and_everyone_sees_it(world):
    world["source"].frames[ORDERS["input_key"]] = frame((1.0,))
    d = definition(item("a"), item("b", "COUNT()"))
    before = ask(d, tiles=["a"])["tiles"]["a"]
    world["source"].frames[ORDERS["input_key"]] = frame((2.0, 2.0))
    assert ask(d, tiles=["a"])["tiles"]["a"]["value"] == 1.0  # what is kept
    token = tile_cache.claim_refresh(7)
    time.sleep(0.01)
    fresh = ask(d, tiles=["a"], cache={"fresh": token})["tiles"]["a"]
    assert fresh["value"] == 4.0 and fresh["cached"] is False and fresh["computed_at"] > before["computed_at"]
    assert len(world["source"].reads) == 2
    # someone else, no refresh of theirs: the new numbers, of the tile asked and of another
    other = ask(d, tiles=["a"])["tiles"]["a"]
    assert other["value"] == 4.0 and other["cached"] is True
    assert ask(d, tiles=["b"])["tiles"]["b"]["value"] == 2
    assert len(world["source"].reads) == 2


def test_a_refresh_at_most_once_every_ten_seconds_for_everyone(world):
    assert tile_cache.REFRESH_SECONDS == 10
    first = tile_cache.claim_refresh(7)
    assert first and tile_cache.claim_refresh(7) is None and tile_cache.claim_refresh(8)
    assert 0 < world["valkey"].ttl("dataprep:dashboards:refresh:7") <= 10
    world["valkey"].delete("dataprep:dashboards:refresh:7")
    assert tile_cache.claim_refresh(7) not in (None, first)


def test_a_hundred_refreshes_at_once_are_granted_once_and_read_once(world):
    world["source"].delay = 0.2
    tokens = [tile_cache.claim_refresh(7) for _ in range(100)]
    granted = [t for t in tokens if t]
    assert len(granted) == 1
    ask()
    reads = len(world["source"].reads)
    answers = everyone(100, lambda i: request(cache={"fresh": tokens[i]} if tokens[i] else {}))
    assert all(a["tiles"]["k"]["value"] == 60.0 for a in answers)
    assert len(world["source"].reads) == reads + 1


def test_the_route_of_a_refresh(world):
    assert routes.tiles_refresh(routes.TilesRefresh(dashboard=3))["fresh"]
    assert routes.tiles_refresh(routes.TilesRefresh(dashboard=3)) == {"fresh": None}


def test_a_refresh_of_who_may_read_gives_who_may_not_nothing(world):
    d = definition(item("pay_tile", dataset="pay"), names=("orders", "pay"))
    world["source"].frames[PAY["input_key"]] = frame((SECRET_NUMBER,))
    ask(d, datasets={"pay": PAY}, cache={"fresh": tile_cache.claim_refresh(1)})
    answer = ask(d, datasets={}, refused={"pay": NO_ACCESS})
    assert answer == {"tiles": {"pay_tile": NO_ACCESS}, "truncated": {}}


def test_a_refresh_by_who_may_not_read_refreshes_nothing_of_it(world):
    d = definition(item("pay_tile", dataset="pay"), names=("orders", "pay"))
    ask(d, datasets={"pay": PAY})
    reads = len(world["source"].reads)
    ask(d, datasets={}, refused={"pay": NO_ACCESS}, cache={"fresh": tile_cache.claim_refresh(1)})
    assert ask(d, datasets={"pay": PAY})["tiles"]["pay_tile"]["cached"] is True and len(world["source"].reads) == reads


# ── sizes ────────────────────────────────────────────────────────────────────
def test_a_tile_too_large_for_valkey_is_not_kept_and_is_counted(world):
    panel_settings.write({"dashboards.cache_tile_bytes": 1024})
    panel_settings.forget()
    world["source"].frames[ORDERS["input_key"]] = pl.DataFrame({"region": [f"R{i}" * 30 for i in range(200)], "amount": [1.0] * 200})
    t = ask(definition(rows_table("t")))["tiles"]["t"]
    assert t["total_rows"] == 200 and world["valkey"].keys("dataprep:dashboards:value:*") == []
    assert tile_cache.stats()["tile_too_large"] == 1
    assert ask(definition(rows_table("t")))["tiles"]["t"]["total_rows"] == 200
    assert len(world["source"].reads) == 1  # worked out again, from the rows


def test_a_million_rows_do_not_go_in_valkey(world):
    n = 1_000_000
    world["source"].frames[ORDERS["input_key"]] = pl.DataFrame({"region": ["R"] * n, "amount": [1.5] * n})
    d = definition(item("k"), rows_table("t"))
    answer = ask(d)
    assert answer["tiles"]["k"]["value"] == 1.5 * n and answer["tiles"]["t"]["total_rows"] == n
    kept = sum(len(world["valkey"].get(k)) for k in world["valkey"].keys("dataprep:dashboards:*") if world["valkey"].type(k) == "string")
    assert kept < 50_000
    assert sum(len(v) for v in world["bucket"].blobs.values()) < 5_000_000


def test_past_the_memory_given_valkey_keeps_no_number(world, monkeypatch):
    monkeypatch.setattr(world["valkey"], "info", lambda section: {"used_memory": 10 * 1024**3}, raising=False)
    assert ask()["tiles"]["k"]["value"] == 60.0
    assert world["valkey"].keys("dataprep:dashboards:value:*") == [] and tile_cache.stats()["valkey_full"] >= 1


def test_rows_too_large_for_the_room_are_not_kept_and_are_counted(world):
    panel_settings.write({"dashboards.cache_bucket_bytes": 16})
    panel_settings.forget()
    assert ask()["tiles"]["k"]["value"] == 60.0
    assert world["bucket"].blobs == {} and tile_cache.stats()["rows_too_large"] == 1
    assert world["valkey"].keys("dataprep:dashboards:rows:*") == []


def test_a_room_of_zero_keeps_no_rows_and_the_numbers_still_come(world):
    panel_settings.write({"dashboards.cache_bucket_bytes": 0})
    panel_settings.forget()
    assert ask()["tiles"]["k"]["value"] == 60.0 and world["bucket"].blobs == {}


def test_who_ends_first_leaves_first_when_the_room_is_short(world):
    index = tile_cache._index()
    for h, ends in (("a" * 64, 100.0), ("b" * 64, 200.0)):
        world["bucket"].blobs[(index.bucket, index.object_key(h))] = b"x" * 100
        index.keep(h, 100, time.time() + ends)
    index.make_room(150, 300)
    assert sorted(k for _, k in world["bucket"].blobs) == [index.object_key("b" * 64)]
    assert world["valkey"].hkeys(index.sizes) == ["b" * 64]


def test_the_rows_go_under_cache_and_a_sweep_takes_those_that_ended(world):
    ask()
    index = tile_cache._index()
    assert [k for _, k in world["bucket"].blobs] and all(k.startswith("cache/") and k.endswith(".parquet") for _, k in world["bucket"].blobs)
    assert tile_cache.sweep() == 0 and world["bucket"].blobs
    h = next(iter(world["valkey"].smembers(index.index_set)))
    world["valkey"].zadd(index.atime_zset, {h: time.time() - 1})
    assert tile_cache.sweep() == 1
    assert world["bucket"].blobs == {} and world["valkey"].hkeys(index.sizes) == [] and world["valkey"].smembers(index.index_set) == set()


def test_the_beat_s_sweep_of_the_cache_includes_the_dashboards(world, monkeypatch):
    from app.tasks import jobs

    class Engine:
        class cache:
            @staticmethod
            def evict_expired(ttl):
                return 0

    monkeypatch.setattr(jobs, "get_engine", lambda *a: Engine)
    monkeypatch.setattr("app.engine._BIGQUERY_AVAILABLE", False)
    monkeypatch.setattr(tile_cache, "sweep", lambda: 4)
    assert jobs.evict_cache_task()["removed"] == 4


def test_the_step_cache_s_sweep_knows_nothing_of_these_rows(world):
    """Same prefix, an index of its own: a step that ended does not take a dashboard's rows."""
    from app.engine.cache import StepCache

    ask()
    assert StepCache(world["bucket"], redis_client=world["valkey"]).evict_expired(0) == 0 and world["bucket"].blobs


# ── a courtesy ───────────────────────────────────────────────────────────────
def test_without_valkey_the_numbers_still_come_read_each_time(world, monkeypatch):
    monkeypatch.setattr(tile_cache, "_client", BrokenRedis())
    monkeypatch.setattr(tile_cache, "_broker_is_valkey", lambda: False)  # a broker of its own
    assert ask()["tiles"]["k"]["value"] == 60.0
    assert ask()["tiles"]["k"]["cached"] is False
    assert len(world["worker"]) == 2 and world["bucket"].blobs == {}
    assert tile_cache.claim_refresh(1)  # nothing to go round, and no reason to refuse


def test_when_valkey_is_the_broker_too_the_numbers_are_worked_out_here(world, monkeypatch):
    monkeypatch.setattr(tile_cache, "_client", BrokenRedis())
    monkeypatch.setattr(tile_cache, "_broker_is_valkey", lambda: True)
    tile_cache._safely(lambda r: r.get("x"))  # found out
    assert ask()["tiles"]["k"]["value"] == 60.0
    assert world["worker"] == [] and len(world["source"].reads) == 1


def test_a_broker_that_does_not_answer_means_working_here(world, monkeypatch):
    from kombu.exceptions import OperationalError

    def refuses(*a, **k):
        raise OperationalError("no broker")

    monkeypatch.setattr(tasks, "on_preview_worker", refuses)
    assert ask()["tiles"]["k"]["value"] == 60.0 and len(world["source"].reads) == 1


def test_valkey_that_hangs_is_asked_once_and_left_alone_for_a_while(world, monkeypatch):
    calls = []

    class Hangs:
        def __getattr__(self, name):
            def fail(*a, **k):
                calls.append(name)
                raise redis.TimeoutError("hangs")
            return fail

    monkeypatch.setattr(tile_cache, "_client", Hangs())
    assert tile_cache._safely(lambda r: r.get("x"), "no") == "no" and tile_cache._safely(lambda r: r.get("y"), "no") == "no"
    assert len(calls) == 1


def test_a_bucket_that_hangs_is_said_in_seconds_and_tried_once():
    """boto3's own defaults: a minute to connect, and five attempts at a download."""
    from app.utils import StorageService

    quick = StorageService(quick=True)
    config = quick.client.meta.config
    assert (config.connect_timeout, config.read_timeout, config.retries["total_max_attempts"]) == (2, 15, 1)
    assert quick._download_config.num_download_attempts == 1
    assert StorageService()._download_config is None and StorageService().client.meta.config.connect_timeout == 60


class BrokenBucket(FakeStorage):
    def upload_file(self, *a):
        raise ConnectionError("no bucket")

    def download_file(self, *a):
        raise ConnectionError("no bucket")


def test_a_bucket_that_does_not_answer_leaves_the_numbers_as_they_are(world, monkeypatch):
    monkeypatch.setattr(tile_cache, "_storage", lambda: BrokenBucket())
    assert ask()["tiles"]["k"]["value"] == 60.0
    assert world["valkey"].keys("dataprep:dashboards:rows:*") == []
    assert ask(definition(item("b", "COUNT()")))["tiles"]["b"]["value"] == 3  # no rows kept: read again


def test_a_bucket_lost_after_the_rows_were_kept_means_reading_them_again(world, monkeypatch):
    ask(definition(item("a")))
    monkeypatch.setattr(tile_cache, "_storage", lambda: BrokenBucket())
    assert ask(definition(item("b", "COUNT()")))["tiles"]["b"]["value"] == 3
    assert len(world["source"].reads) == 2


def test_a_file_that_is_gone_means_reading_again_not_an_error(world):
    ask(definition(item("a")))
    world["bucket"].blobs.clear()
    t = ask(definition(item("b", "COUNT()")))["tiles"]["b"]
    assert t["value"] == 3 and t["cached"] is False and len(world["source"].reads) == 2


def test_a_column_a_parquet_cannot_hold_means_no_rows_kept_and_the_numbers_as_they_are(world):
    world["source"].frames[ORDERS["input_key"]] = pl.DataFrame({"region": ["a"], "amount": [1.0], "o": pl.Series([object()], dtype=pl.Object)})
    assert ask()["tiles"]["k"]["value"] == 1.0 and world["bucket"].blobs == {}


def test_rows_given_as_they_are_are_not_kept(world):
    t = ask(datasets={"orders": {"rows": [{"region": "x", "amount": 5.0}], "columns": [{"name": "region", "type": "String"}, {"name": "amount", "type": "Float64"}]}})
    assert t["tiles"]["k"]["value"] == 5.0 and world["bucket"].blobs == {}


def test_a_definition_that_cannot_be_read_is_said_by_the_worker_as_ever(world):
    with pytest.raises(HTTPException) as e:
        ask({"v": 99})
    assert e.value.status_code == 422 and len(world["worker"]) == 1


def test_without_cache_nothing_changes(world, monkeypatch):
    """A definition being edited: its slot, and nothing read or kept."""
    answer = asyncio.run(routes.tiles(routes.DashboardTiles(definition=definition(), datasets={"orders": ORDERS}, slot="u1:dashboard:preview")))
    assert answer["tiles"]["k"]["value"] == 60.0 and world["worker"][0]["slot"] == "u1:dashboard:preview"
    assert "cache_seconds" not in world["worker"][0]["kwargs"] and world["valkey"].keys("dataprep:dashboards:*") == [] and world["bucket"].blobs == {}


# ── a parameter's list ───────────────────────────────────────────────────────
def _choices(cache=None, spec=None, value="[region]", **kw) -> dict:
    return asyncio.run(routes.choices(routes.ReportChoices(
        definition=definition(), dataset="orders", datasets={"orders": spec or ORDERS}, value=value, cache=cache, **kw)))


def test_a_list_is_read_once_for_everyone(world):
    world["source"].delay = 0.2

    async def go():
        return await asyncio.gather(*[routes.choices(routes.ReportChoices(
            definition=definition(), dataset="orders", datasets={"orders": ORDERS}, value="[region]", cache={})) for _ in range(30)])

    answers = asyncio.run(go())
    assert len(world["source"].reads) == 1 and len(world["worker"]) == 1
    assert all([c["value"] for c in a["choices"]] == ["R0", "R1", "R2"] for a in answers)


def test_a_list_goes_round_with_a_refresh(world):
    _choices(cache={})
    world["source"].frames[ORDERS["input_key"]] = frame((1.0, 2.0, 3.0, 4.0))
    assert len(_choices(cache={})["choices"]) == 3
    assert len(_choices(cache={"fresh": "t1"})["choices"]) == 4
    assert len(world["source"].reads) == 2


def test_a_refresh_with_the_same_token_reads_a_source_once_for_a_list_and_a_tile(world):
    token = tile_cache.claim_refresh(1)
    _choices(cache={"fresh": token}, value='[region] & ""')  # reads the whole dataset
    ask(cache={"fresh": token})
    assert len(world["source"].reads) == 1


def test_a_list_that_fails_keeps_its_status_for_those_waiting(world):
    world["source"].over.add(ORDERS["input_key"])
    for _ in range(2):
        with pytest.raises(HTTPException) as e:
            _choices(cache={}, value='[region] & ""')
        assert e.value.status_code == 422 and "more than" in e.value.detail


def test_a_list_without_cache_is_a_report_s_as_ever(world):
    assert [c["value"] for c in _choices()["choices"]] == ["R0", "R1", "R2"]
    assert world["worker"][0]["slot"] is None and "cache_seconds" not in world["worker"][0]["kwargs"]
    assert world["valkey"].keys("dataprep:dashboards:*") == []


# ── a worker gone ────────────────────────────────────────────────────────────
def test_who_others_wait_for_finds_out_a_worker_is_gone_without_waiting_to_the_end(monkeypatch):
    from celery.exceptions import TimeoutError as CeleryTimeout

    class Result:
        state = "STARTED"
        info = {"hostname": "gone", "since": 1.0}

        def get(self, timeout=None):
            time.sleep(0.01)
            raise CeleryTimeout()

        def forget(self):
            pass

        def revoke(self, terminate=False):
            pass

    monkeypatch.setattr(tasks.celery_app, "send_task", lambda *a, **k: Result())
    monkeypatch.setattr(tasks, "_WORKER_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(tasks.worker_pool, "lost", lambda task_id, meta: "The worker was lost")
    with pytest.raises(HTTPException) as e:
        tasks.on_preview_worker("x", {}, None, seconds=5, watch_worker=True)
    assert e.value.status_code == 503
    # without being told to, as before: it waits to its end
    with pytest.raises(HTTPException) as e:
        tasks.on_preview_worker("x", {}, None, seconds=0.05)
    assert e.value.status_code == 504
