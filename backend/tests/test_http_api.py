"""The «API call» datasource: an HTTP API read into a table at each refresh.

Against an HTTP server that runs in the test process (tests/fake_api), so the
client speaks real HTTP. Fixed here: every authentication (OAuth2 included) and
the rule that credentials never reach another host, the pagination types, the
politeness (retries honouring Retry-After, the minimum interval), the caps, the
flattening of JSON into columns and its types, the list variables, the refresh
modes (snapshot, append with keys) and the «Try the call» answer — with secrets
masked everywhere a person can read.
"""
from __future__ import annotations

import base64
import io
import json
import time
from datetime import datetime, timezone

import polars as pl
import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.ingest import http_api as api
from tests.fake_api import FakeApi
from tests.fakes import FakeStorage

SECRET = "s3cr3t-K3Y-0123"
NOW = datetime(2026, 10, 17, 22, 30, 5, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────────────
@pytest.fixture
def srv():
    with FakeApi() as s:
        yield s


@pytest.fixture(autouse=True)
def no_real_waits(monkeypatch):
    """Retries and the minimum interval sleep through api._sleep: recorded, not slept."""
    waits: list[float] = []
    monkeypatch.setattr(api, "_sleep", lambda s: waits.append(s))
    api.forget_tokens()
    return waits


def conn(srv, auth="none", **kw) -> api.ApiConnectionSpec:
    base = dict(base_url=srv.url("/v1"), auth=auth, secret_encrypted=encrypt_secret(SECRET) if auth != "none" else "",
                timeout_seconds=5)
    base.update(kw)
    return api.ApiConnectionSpec(**base)


def source(**kw) -> api.ApiSourceSpec:
    return api.ApiSourceSpec(**kw)


def ingest(c, s, previous=None, last_refresh=None, storage=None, **kw):
    st = storage or FakeStorage()
    res = api.ingest_api_to_parquet(c, s, "b", "datasets/new.parquet", st, previous=previous,
                                    last_refresh=last_refresh, now=NOW, **kw)
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "datasets/new.parquet")]))


def items(*rows):
    return lambda req: (200, {}, list(rows))


def no_secret_anywhere(srv, *texts):
    for t in texts:
        assert SECRET not in json.dumps(t, default=str)


# ── authentication ─────────────────────────────────────────────────────────────
def test_no_auth_reads_a_list_of_records(srv):
    srv.route("GET", "/v1/items", items({"id": 1, "name": "a"}, {"id": 2, "name": "b"}))
    res, df = ingest(conn(srv), source(path="/items"))
    assert df.to_dicts() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert res["rows_written"] == 2 and res["calls"] == 1 and res["pages"] == 1
    assert "authorization" not in srv.log[0].headers


def test_api_key_in_a_header_with_its_own_name(srv):
    srv.route("GET", "/v1/items", items({"id": 1}))
    ingest(conn(srv, "api_key", key_name="X-Api-Key", key_in="header"), source(path="items"))
    assert srv.log[0].headers["x-api-key"] == SECRET


def test_api_key_in_the_query(srv):
    srv.route("GET", "/v1/items", items({"id": 1}))
    ingest(conn(srv, "api_key", key_name="apikey", key_in="query"), source(path="/items", query=[{"name": "a", "value": "1"}]))
    assert srv.log[0].query == [("a", "1"), ("apikey", SECRET)]


def test_bearer_and_basic(srv):
    srv.route("GET", "/v1/items", items({"id": 1}))
    ingest(conn(srv, "bearer"), source(path="/items"))
    ingest(conn(srv, "basic", username="tab"), source(path="/items"))
    assert srv.log[0].headers["authorization"] == f"Bearer {SECRET}"
    assert srv.log[1].headers["authorization"] == "Basic " + base64.b64encode(f"tab:{SECRET}".encode()).decode()


def _oauth(srv, expires_in=3600):
    issued = []

    def token(req):
        form = dict(req.query) if req.method == "GET" else dict(__import__("urllib.parse").parse.parse_qsl(req.body.decode()))
        issued.append((form, req.headers.get("authorization")))
        return 200, {}, {"access_token": f"tok{len(issued)}", "token_type": "Bearer", "expires_in": expires_in}

    srv.route("POST", "/oauth/token", token)
    return issued


def test_oauth2_client_credentials_cached_until_expiry(srv):
    issued = _oauth(srv)
    srv.route("GET", "/v1/items", items({"id": 1}))
    c = conn(srv, "oauth2", username="client-1", token_url=srv.url("/oauth/token"), scope="read")
    ingest(c, source(path="/items"))
    ingest(c, source(path="/items"))
    assert len(issued) == 1
    form, auth = issued[0]
    assert form == {"grant_type": "client_credentials", "client_id": "client-1", "client_secret": SECRET, "scope": "read"}
    assert auth is None
    assert [r.headers["authorization"] for r in srv.requests_to("/v1/items")] == ["Bearer tok1", "Bearer tok1"]


def test_oauth2_with_the_client_in_basic_auth(srv):
    issued = _oauth(srv)
    srv.route("GET", "/v1/items", items({"id": 1}))
    ingest(conn(srv, "oauth2", username="client-1", token_url=srv.url("/oauth/token"), client_auth="basic"), source(path="/items"))
    form, auth = issued[0]
    assert "client_secret" not in form and auth == "Basic " + base64.b64encode(f"client-1:{SECRET}".encode()).decode()


def test_oauth2_refreshes_the_token_once_on_401(srv):
    issued = _oauth(srv)

    def guarded(req):
        return (200, {}, [{"id": 1}]) if req.headers.get("authorization") == "Bearer tok2" else (401, {}, {"error": "expired"})

    srv.route("GET", "/v1/items", guarded)
    _, df = ingest(conn(srv, "oauth2", username="c", token_url=srv.url("/oauth/token")), source(path="/items"))
    assert len(issued) == 2 and df.height == 1


def test_oauth2_a_second_401_is_an_error_not_a_loop(srv):
    _oauth(srv)
    srv.route("GET", "/v1/items", lambda req: (401, {}, {"error": "no"}))
    with pytest.raises(api.ApiError, match="401"):
        ingest(conn(srv, "oauth2", username="c", token_url=srv.url("/oauth/token")), source(path="/items"))
    assert len(srv.requests_to("/v1/items")) == 2


def test_oauth2_refused_credentials(srv):
    srv.route("POST", "/oauth/token", lambda req: (401, {}, {"error": "invalid_client", "secret_echo": SECRET}))
    with pytest.raises(api.ApiError) as e:
        ingest(conn(srv, "oauth2", username="c", token_url=srv.url("/oauth/token")), source(path="/items"))
    assert "token" in str(e.value) and "invalid_client" in str(e.value) and SECRET not in str(e.value)


# ── redirects: credentials never go to another host ────────────────────────────
def test_a_redirect_on_the_same_host_keeps_the_credentials(srv):
    srv.route("GET", "/v1/old", lambda req: (302, {"Location": "/v1/new"}, b""))
    srv.route("GET", "/v1/new", items({"id": 1}))
    ingest(conn(srv, "bearer"), source(path="/old"))
    assert srv.requests_to("/v1/new")[0].headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize("auth,kw", [("bearer", {}), ("api_key", {"key_name": "X-Key"}),
                                     ("api_key", {"key_name": "key", "key_in": "query"}), ("basic", {"username": "u"})])
def test_a_redirect_to_another_host_carries_no_credential(srv, auth, kw):
    srv.route("GET", "/v1/data", lambda req: (302, {"Location": srv.other_host_url("/cdn/data")}, b""))
    srv.route("GET", "/cdn/data", items({"id": 1}))
    c = conn(srv, auth, headers={"X-Tenant": "acme"}, **kw)
    _, df = ingest(c, source(path="/data", headers=[{"name": "X-Call", "value": "1"}]))
    (cdn,) = srv.requests_to("/cdn/data")
    assert df.height == 1 and cdn.host.startswith("localhost")
    assert SECRET not in json.dumps([cdn.headers, cdn.query])
    assert "authorization" not in cdn.headers and "x-key" not in cdn.headers and "x-tenant" not in cdn.headers


def test_a_303_turns_a_post_into_a_get(srv):
    srv.route("POST", "/v1/search", lambda req: (303, {"Location": "/v1/results"}, b""))
    srv.route("GET", "/v1/results", items({"id": 1}))
    ingest(conn(srv), source(method="POST", path="/search", body='{"q": "x"}'))
    assert srv.requests_to("/v1/results")[0].method == "GET"


def test_too_many_redirects(srv):
    srv.route("GET", "/v1/loop", lambda req: (302, {"Location": "/v1/loop"}, b""))
    with pytest.raises(api.ApiError, match="redirect"):
        ingest(conn(srv), source(path="/loop"))


# ── headers, query, body and placeholders ──────────────────────────────────────
def test_placeholders_resolved_in_the_time_zone_everywhere(srv):
    srv.route("POST", "/v1/q/2026-10-18", items({"id": 1}))
    s = source(method="POST", path="/q/{today}", timezone="Asia/Tokyo",
               query=[{"name": "from", "value": "{today-7d:%Y%m%d}"}],
               headers=[{"name": "X-Day", "value": "{yesterday}"}],
               body='{"since": "{now:epoch}", "label": "day {today}", "literal": "{{x}}"}')
    ingest(conn(srv), s)
    (req,) = srv.log
    assert req.query == [("from", "20261011")] and req.headers["x-day"] == "2026-10-17"
    # a JSON string that is a single epoch placeholder becomes a number
    assert req.json() == {"since": int(NOW.timestamp()), "label": "day 2026-10-18", "literal": "{x}"}


def test_default_headers_of_the_connection_and_of_the_call(srv):
    srv.route("GET", "/v1/items", items({"id": 1}))
    ingest(conn(srv, headers={"Accept": "application/json", "X-Tenant": "acme"}),
           source(path="/items", headers=[{"name": "X-Tenant", "value": "override"}]))
    h = srv.log[0].headers
    assert h["accept"] == "application/json" and h["x-tenant"] == "override"


def test_last_refresh_is_sent_as_given(srv):
    srv.route("GET", "/v1/changes", items({"id": 1}))
    s = source(path="/changes", query=[{"name": "since", "value": "{last_refresh}"}], last_refresh_initial="today-365")
    ingest(conn(srv), s)
    ingest(conn(srv), s, last_refresh="2026-10-16T06:00:00Z")
    assert [r.q("since") for r in srv.log] == ["2025-10-17T00:00:00+00:00", "2026-10-16T06:00:00+00:00"]


def test_path_values_are_url_encoded(srv):
    srv.route("GET", "/v1/quote/A%2FB", items({"id": 1}))
    _, df = ingest(conn(srv), source(path="/quote/{symbol}", variables=[{"name": "symbol", "values": ["A/B"]}]))
    assert srv.log[0].raw_path.startswith("/v1/quote/A%2FB") and df.height == 1


# ── list variables ─────────────────────────────────────────────────────────────
def test_one_call_per_value_with_the_value_in_a_column(srv):
    srv.route("GET", "/v1/quote", lambda req: (200, {}, [{"price": len(req.q("s"))}]))
    res, df = ingest(conn(srv), source(path="/quote", query=[{"name": "s", "value": "{symbol}"}],
                                       variables=[{"name": "symbol", "values": ["AAPL", "MSFT", "ENI.MI"]}]))
    assert [r.q("s") for r in srv.log] == ["AAPL", "MSFT", "ENI.MI"]
    assert df.to_dicts() == [{"price": 4, "_param_symbol": "AAPL"}, {"price": 4, "_param_symbol": "MSFT"},
                             {"price": 6, "_param_symbol": "ENI.MI"}]
    assert res["calls"] == 3


def test_two_lists_make_every_combination(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {}, [{"n": 1}]))
    _, df = ingest(conn(srv), source(path="/x", query=[{"name": "a", "value": "{a}"}, {"name": "b", "value": "{b}"}],
                                     variables=[{"name": "a", "values": ["1", "2"]}, {"name": "b", "values": ["x", "y", "z"]}]))
    assert df.height == 6 and df.select("_param_a", "_param_b").rows()[:2] == [("1", "x"), ("1", "y")]


def test_too_many_combinations_are_refused_before_any_call(srv, monkeypatch):
    monkeypatch.setattr(get_settings().api, "max_calls", 5)
    s = source(path="/x", query=[{"name": "a", "value": "{a}"}, {"name": "b", "value": "{b}"}],
               variables=[{"name": "a", "values": ["1", "2", "3"]}, {"name": "b", "values": ["x", "y"]}])
    with pytest.raises(api.ApiError, match="6 calls"):
        ingest(conn(srv), s)
    assert srv.log == []


# ── pagination ─────────────────────────────────────────────────────────────────
def test_page_number_until_an_empty_page(srv):
    data = {"1": [{"id": 1}, {"id": 2}], "2": [{"id": 3}]}
    srv.route("GET", "/v1/p", lambda req: (200, {}, {"data": data.get(req.q("page"), [])}))
    res, df = ingest(conn(srv), source(path="/p", response={"records_path": "data"},
                                       pagination={"type": "page", "param": "page", "start": 1}))
    assert df["id"].to_list() == [1, 2, 3] and [r.q("page") for r in srv.log] == ["1", "2", "3"] and res["pages"] == 3


def test_offset_and_limit_until_an_empty_page(srv):
    rows = [{"id": i} for i in range(7)]

    def h(req):
        o, n = int(req.q("offset")), int(req.q("limit"))
        return 200, {}, rows[o:o + n]

    srv.route("GET", "/v1/o", h)
    _, df = ingest(conn(srv), source(path="/o", pagination={"type": "offset", "param": "offset", "limit_param": "limit", "limit": 3}))
    # a short page is not the end (an API may cap the page size): only an empty one is
    assert df["id"].to_list() == list(range(7)) and [r.q("offset") for r in srv.log] == ["0", "3", "6", "7"]


def test_cursor(srv):
    pages = {None: ({"items": [{"id": 1}], "meta": {"next": "c2"}}), "c2": {"items": [{"id": 2}], "meta": {"next": None}}}
    srv.route("GET", "/v1/c", lambda req: (200, {}, pages[req.q("cursor")]))
    _, df = ingest(conn(srv), source(path="/c", response={"records_path": "items"},
                                     pagination={"type": "cursor", "param": "cursor", "cursor_path": "meta.next"}))
    assert df["id"].to_list() == [1, 2] and [r.q("cursor") for r in srv.log] == [None, "c2"]


def test_next_link_in_the_link_header(srv):
    srv.route("GET", "/v1/l", lambda req: (200, {"Link": f'<{srv.url("/v1/l2")}?page=2>; rel="next"'}, [{"id": 1}]))
    srv.route("GET", "/v1/l2", lambda req: (200, {}, [{"id": 2}]))
    _, df = ingest(conn(srv, "bearer"), source(path="/l", pagination={"type": "next_link"}))
    assert df["id"].to_list() == [1, 2] and srv.log[1].headers["authorization"] == f"Bearer {SECRET}"


def test_next_link_in_the_json_relative_and_on_another_host_without_credentials(srv):
    srv.route("GET", "/v1/n", lambda req: (200, {}, {"results": [{"id": 1}], "next": "/v1/n2"}))
    srv.route("GET", "/v1/n2", lambda req: (200, {}, {"results": [{"id": 2}], "next": srv.other_host_url("/v1/n3")}))
    srv.route("GET", "/v1/n3", lambda req: (200, {}, {"results": [{"id": 3}], "next": None}))
    _, df = ingest(conn(srv, "api_key", key_name="key", key_in="query"),
                   source(path="/n", response={"records_path": "results"}, pagination={"type": "next_link", "next_path": "next"}))
    assert df["id"].to_list() == [1, 2, 3]
    assert srv.log[1].q("key") == SECRET and srv.log[2].q("key") is None


def test_more_pages_than_allowed_is_an_error_not_a_silent_cut(srv):
    srv.route("GET", "/v1/p", lambda req: (200, {}, [{"id": int(req.q("page"))}]))
    with pytest.raises(api.ApiError, match="more than 3 pages"):
        ingest(conn(srv), source(path="/p", pagination={"type": "page", "param": "page"}, limits={"max_pages": 3}))


# ── politeness and limits ──────────────────────────────────────────────────────
def test_429_with_retry_after_is_waited_and_retried(srv, no_real_waits):
    calls = []

    def h(req):
        calls.append(1)
        return (429, {"Retry-After": "7"}, {"error": "slow down"}) if len(calls) == 1 else (200, {}, [{"id": 1}])

    srv.route("GET", "/v1/x", h)
    _, df = ingest(conn(srv), source(path="/x"))
    assert df.height == 1 and 7 in no_real_waits


def test_a_5xx_then_success(srv, no_real_waits):
    calls = []
    srv.route("GET", "/v1/x", lambda req: (calls.append(1) or (503, {}, b"down")) if len(calls) < 2 else (200, {}, [{"id": 1}]))
    _, df = ingest(conn(srv), source(path="/x"))
    assert df.height == 1 and len(no_real_waits) == 2


def test_a_5xx_that_does_not_end(srv):
    srv.route("GET", "/v1/x", lambda req: (500, {}, b"boom"))
    with pytest.raises(api.ApiError, match="500"):
        ingest(conn(srv), source(path="/x"))
    assert len(srv.log) == get_settings().api.max_retries + 1


def test_a_retry_after_longer_than_allowed(srv):
    srv.route("GET", "/v1/x", lambda req: (429, {"Retry-After": "86400"}, b""))
    with pytest.raises(api.ApiError, match="86400"):
        ingest(conn(srv), source(path="/x"))


def test_connection_errors_are_retried_and_then_explained():
    c = api.ApiConnectionSpec(base_url="http://127.0.0.1:1/v1", auth="api_key", key_name="k", key_in="query",
                              secret_encrypted=encrypt_secret(SECRET), timeout_seconds=2)
    with pytest.raises(api.ApiError) as e:
        ingest(c, source(path="/x"))
    assert "127.0.0.1:1" in str(e.value) and SECRET not in str(e.value)


def test_the_minimum_interval_between_requests(srv, no_real_waits):
    srv.route("GET", "/v1/p", lambda req: (200, {}, [{"id": 1}] if req.q("page") in ("1", "2") else []))
    ingest(conn(srv), source(path="/p", pagination={"type": "page", "param": "page"}, limits={"min_interval_seconds": 2}))
    assert len(srv.log) == 3 and sum(1 for w in no_real_waits if 0 < w <= 2) == 2


def test_a_response_over_the_cap(srv, monkeypatch):
    monkeypatch.setattr(get_settings().api, "max_response_bytes", 1000)
    srv.route("GET", "/v1/big", lambda req: (200, {}, [{"x": "a" * 2000}]))
    with pytest.raises(api.ApiError, match="API__MAX_RESPONSE_BYTES"):
        ingest(conn(srv), source(path="/big"))


def test_all_responses_over_the_total_cap(srv, monkeypatch):
    monkeypatch.setattr(get_settings().api, "max_total_bytes", 1500)
    srv.route("GET", "/v1/p", lambda req: (200, {}, [{"x": "a" * 600}] if int(req.q("page")) < 5 else []))
    with pytest.raises(api.ApiError, match="API__MAX_TOTAL_BYTES"):
        ingest(conn(srv), source(path="/p", pagination={"type": "page", "param": "page"}))


def test_more_rows_than_allowed(srv):
    srv.route("GET", "/v1/x", items(*[{"id": i} for i in range(5)]))
    with pytest.raises(api.ApiError, match="more than 3 rows"):
        ingest(conn(srv), source(path="/x", limits={"max_rows": 3}))


def test_a_datasource_cannot_raise_a_limit_above_the_deployment(srv, monkeypatch):
    monkeypatch.setattr(get_settings().api, "max_rows", 2)
    srv.route("GET", "/v1/x", items(*[{"id": i} for i in range(5)]))
    with pytest.raises(api.ApiError, match="more than 2 rows"):
        ingest(conn(srv), source(path="/x", limits={"max_rows": 1000}))


def test_an_http_error_explains_without_the_secret(srv):
    srv.route("GET", "/v1/x", lambda req: (403, {}, {"error": f"key {SECRET} has no access"}))
    with pytest.raises(api.ApiError) as e:
        ingest(conn(srv, "api_key", key_name="key", key_in="query"), source(path="/x"))
    msg = str(e.value)
    assert msg.startswith("API: ") and "403" in msg and "has no access" in msg and SECRET not in msg and "***" in msg


# ── the response: records, flattening, types ───────────────────────────────────
def test_records_path_and_nested_objects_as_dotted_columns(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {}, {"data": {"items": [
        {"id": 1, "owner": {"name": "a", "geo": {"lat": 1.5}}, "tags": ["x", "y"], "lines": [{"sku": "A"}]},
    ]}}))
    _, df = ingest(conn(srv), source(path="/x", response={"records_path": "data.items"}))
    assert df.columns == ["id", "owner.name", "owner.geo.lat", "tags", "lines"]
    row = df.row(0, named=True)
    assert row["owner.geo.lat"] == 1.5 and json.loads(row["tags"]) == ["x", "y"] and json.loads(row["lines"]) == [{"sku": "A"}]


def test_a_list_of_objects_exploded_to_rows(srv):
    srv.route("GET", "/v1/x", items({"id": 1, "lines": [{"sku": "A", "q": 1}, {"sku": "B", "q": 2}]}, {"id": 2, "lines": []}))
    _, df = ingest(conn(srv), source(path="/x", response={"explode": "lines"}))
    assert df.to_dicts() == [{"id": 1, "lines.sku": "A", "lines.q": 1}, {"id": 1, "lines.sku": "B", "lines.q": 2},
                             {"id": 2, "lines.sku": None, "lines.q": None}]


def test_an_object_keyed_by_date_one_row_per_key(srv):
    srv.route("GET", "/v1/fx", lambda req: (200, {}, {"base": "EUR", "rates": {
        "2026-10-01": {"USD": 1.13, "CHF": 0.94}, "2026-10-02": {"USD": 1.12, "CHF": 0.93}}}))
    _, df = ingest(conn(srv), source(path="/fx", response={"records_path": "rates", "keyed": True}))
    assert df.columns == ["_key", "USD", "CHF"]
    assert df.rows() == [("2026-10-01", 1.13, 0.94), ("2026-10-02", 1.12, 0.93)]


def test_keyed_values_that_are_numbers(srv):
    srv.route("GET", "/v1/fx", lambda req: (200, {}, {"rates": {"USD": 1.1, "CHF": 0.9}}))
    _, df = ingest(conn(srv), source(path="/fx", response={"records_path": "rates", "keyed": True}))
    assert df.rows() == [("USD", 1.1), ("CHF", 0.9)] and df.columns == ["_key", "value"]


def test_a_jmespath_expression_first(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {}, {"res": [{"a": {"id": 1}}, {"a": {"id": 2}}]}))
    _, df = ingest(conn(srv), source(path="/x", response={"jmespath": "res[].a"}))
    assert df["id"].to_list() == [1, 2]


def test_ndjson_and_csv(srv):
    srv.route("GET", "/v1/nd", lambda req: (200, {"Content-Type": "application/x-ndjson"}, b'{"id": 1}\n{"id": 2, "x": "b"}\n'))
    srv.route("GET", "/v1/csv", lambda req: (200, {"Content-Type": "text/csv"}, b"id;name\n1;a\n2;b\n"))
    _, df = ingest(conn(srv), source(path="/nd", response={"format": "ndjson"}))
    assert df.to_dicts() == [{"id": 1, "x": None}, {"id": 2, "x": "b"}]
    _, df = ingest(conn(srv), source(path="/csv", response={"format": "csv", "csv_separator": ";"}))
    assert df.to_dicts() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_types_are_inferred_and_iso_dates_stay_text(srv):
    srv.route("GET", "/v1/x", items({"i": 1, "f": 1.5, "b": True, "s": "x", "d": "2026-10-17", "n": None}))
    _, df = ingest(conn(srv), source(path="/x"))
    assert dict(df.schema) == {"i": pl.Int64, "f": pl.Float64, "b": pl.Boolean, "s": pl.String, "d": pl.String, "n": pl.String}


def test_a_schema_that_grows_between_pages_adds_columns(srv):
    pages = {"1": [{"id": 1}], "2": [{"id": 2, "extra": "x"}], "3": []}
    srv.route("GET", "/v1/p", lambda req: (200, {}, pages[req.q("page")]))
    _, df = ingest(conn(srv), source(path="/p", pagination={"type": "page", "param": "page"}))
    assert df.to_dicts() == [{"id": 1, "extra": None}, {"id": 2, "extra": "x"}]


def test_a_type_conflict_makes_the_column_text_and_is_reported(srv):
    pages = {"1": [{"v": 1, "w": 1}], "2": [{"v": "N/A", "w": 2.5}, {"v": True, "w": 3}], "3": []}
    srv.route("GET", "/v1/p", lambda req: (200, {}, pages[req.q("page")]))
    res, df = ingest(conn(srv), source(path="/p", pagination={"type": "page", "param": "page"}))
    assert df.schema["v"] == pl.String and df["v"].to_list() == ["1", "N/A", "true"]
    # integers and decimals are both numbers: the column is a decimal one, not text
    assert df.schema["w"] == pl.Float64
    assert res["type_changes"] == ["v"]


def test_not_json_is_explained_with_an_excerpt(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {"Content-Type": "text/html"}, b"<html>login page</html>"))
    with pytest.raises(api.ApiError, match="not valid JSON.*login page"):
        ingest(conn(srv), source(path="/x"))


def test_a_records_path_that_is_not_there(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {}, {"result": []}))
    with pytest.raises(api.ApiError, match="'data' is not in the response.*result"):
        ingest(conn(srv), source(path="/x", response={"records_path": "data"}))


def test_no_records_at_all_keeps_the_previous_snapshot(srv):
    srv.route("GET", "/v1/x", items())
    with pytest.raises(api.ApiError, match="no records"):
        ingest(conn(srv), source(path="/x"))


def test_a_field_named_like_a_provenance_column(srv):
    srv.route("GET", "/v1/x", lambda req: (200, {}, [{"_param_symbol": "x"}]))
    with pytest.raises(api.ApiError, match="_param_symbol"):
        ingest(conn(srv), source(path="/x", query=[{"name": "s", "value": "{symbol}"}], variables=[{"name": "symbol", "values": ["A"]}]))


# ── refresh modes ──────────────────────────────────────────────────────────────
def _previous(storage, df):
    path = io.BytesIO()
    df.write_parquet(path)
    storage.blobs[("b", "datasets/old.parquet")] = path.getvalue()
    return {"bucket": "b", "key": "datasets/old.parquet"}


def test_append_replaces_existing_keys_and_adds_new_ones(srv):
    srv.route("GET", "/v1/fx", lambda req: (200, {}, {"rates": {"2026-10-02": {"USD": 1.5}, "2026-10-03": {"USD": 1.3}}}))
    st = FakeStorage()
    prev = _previous(st, pl.DataFrame({"_key": ["2026-10-01", "2026-10-02"], "USD": [1.1, 1.2]}))
    s = source(path="/fx", response={"records_path": "rates", "keyed": True}, mode="append", keys=["_key"])
    res, df = ingest(conn(srv), s, previous=prev, storage=st)
    assert df.sort("_key").rows() == [("2026-10-01", 1.1), ("2026-10-02", 1.5), ("2026-10-03", 1.3)]
    assert (res["rows_written"], res["rows_fetched"], res["rows_replaced"]) == (3, 2, 1)


def test_append_twice_never_duplicates(srv):
    srv.route("GET", "/v1/q", lambda req: (200, {}, [{"date": "2026-10-01", "px": 1}, {"date": "2026-10-02", "px": 2}]))
    s = source(path="/q", query=[{"name": "s", "value": "{symbol}"}], variables=[{"name": "symbol", "values": ["A", "B"]}],
               mode="append", keys=["date", "_param_symbol"])
    st = FakeStorage()
    _, first = ingest(conn(srv), s, storage=st)
    prev = _previous(st, first)
    _, second = ingest(conn(srv), s, previous=prev, storage=st)
    assert first.height == second.height == 4
    assert second.select("date", "_param_symbol").is_duplicated().sum() == 0


def test_append_with_a_key_that_is_not_a_column(srv):
    srv.route("GET", "/v1/q", items({"id": 1}))
    with pytest.raises(api.ApiError, match="key column 'date'"):
        ingest(conn(srv), source(path="/q", mode="append", keys=["date"]))


def test_append_with_no_new_rows_keeps_the_previous_table(srv):
    srv.route("GET", "/v1/q", items())
    st = FakeStorage()
    prev = _previous(st, pl.DataFrame({"id": [1, 2]}))
    res, df = ingest(conn(srv), source(path="/q", mode="append", keys=["id"]), previous=prev, storage=st)
    assert df["id"].to_list() == [1, 2] and res["rows_fetched"] == 0


def test_append_aligns_types_with_the_previous_snapshot(srv):
    srv.route("GET", "/v1/q", items({"id": 3, "v": 2.5, "note": None}))
    st = FakeStorage()
    prev = _previous(st, pl.DataFrame({"id": [1], "v": [1], "note": ["x"]}))
    _, df = ingest(conn(srv), source(path="/q", mode="append", keys=["id"]), previous=prev, storage=st)
    assert dict(df.schema) == {"id": pl.Int64, "v": pl.Float64, "note": pl.String}


# ── progress ───────────────────────────────────────────────────────────────────
def test_progress_counts_calls_pages_and_rows(srv):
    srv.route("GET", "/v1/q", lambda req: (200, {}, [{"n": 1}, {"n": 2}]))
    seen = []
    ingest(conn(srv), source(path="/q", query=[{"name": "s", "value": "{s}"}], variables=[{"name": "s", "values": ["a", "b"]}]),
           progress=lambda phase, **c: seen.append((phase, c)))
    assert seen[0] == ("calls", {"call": 1, "calls": 2, "pages": 0, "rows": 0})
    assert ("calls", {"call": 2, "calls": 2, "pages": 1, "rows": 2}) in seen and seen[-1][0] == "saving"


# ── «Try the call», test and validation ────────────────────────────────────────
def test_try_shows_the_resolved_request_masked_and_the_first_rows(srv):
    srv.route("GET", "/v1/q/AAPL", lambda req: (200, {}, {"data": [{"id": i, "o": {"p": 1}} for i in range(30)], "next": "c"}))
    c = conn(srv, "api_key", key_name="apikey", key_in="query", headers={"X-Tenant": "acme"})
    out = api.try_call(c, source(path="/q/{symbol}", query=[{"name": "d", "value": "{today}"}],
                                 variables=[{"name": "symbol", "values": ["AAPL", "MSFT"]}],
                                 response={"records_path": "data"},
                                 pagination={"type": "cursor", "param": "cursor", "cursor_path": "next"}), now=NOW)
    assert out["ok"] and out["status"] == 200 and out["calls"] == 2
    assert out["request"]["url"] == srv.url("/v1/q/AAPL") + "?d=2026-10-17&apikey=***"
    assert out["request"]["headers"]["X-Tenant"] == "acme"
    assert out["columns"] == ["id", "o.p", "_param_symbol"] and len(out["rows"]) == 20 and out["records"] == 30
    assert '"data"' in out["excerpt"] and out["next"] == "c"
    no_secret_anywhere(srv, out)


def test_try_masks_auth_headers(srv):
    srv.route("GET", "/v1/q", items({"id": 1}))
    out = api.try_call(conn(srv, "bearer"), source(path="/q"), now=NOW)
    assert out["request"]["headers"]["Authorization"] == "***"
    no_secret_anywhere(srv, out)


def test_try_an_http_error_shows_the_answer(srv):
    srv.route("GET", "/v1/q", lambda req: (404, {}, {"message": "no such symbol"}))
    out = api.try_call(conn(srv), source(path="/q"), now=NOW)
    assert not out["ok"] and out["status"] == 404 and "no such symbol" in out["excerpt"] and "404" in out["error"]


def test_try_a_shape_error_still_shows_the_answer(srv):
    srv.route("GET", "/v1/q", lambda req: (200, {}, {"result": [1]}))
    out = api.try_call(conn(srv), source(path="/q", response={"records_path": "data"}), now=NOW)
    assert not out["ok"] and "result" in out["excerpt"] and "'data'" in out["error"]


def test_test_connection_calls_the_base_url_or_the_test_path(srv):
    srv.route("GET", "/v1", lambda req: (200, {}, {"ok": True}))
    srv.route("GET", "/v1/ping", lambda req: (200, {}, "pong"))
    assert api.test_connection(conn(srv, "bearer"))["status"] == 200
    assert api.test_connection(conn(srv, "bearer", test_path="/ping"))["url"] == srv.url("/v1/ping")
    srv.route("GET", "/v1/denied", lambda req: (401, {}, {"error": "bad key"}))
    with pytest.raises(api.ApiError, match="401"):
        api.test_connection(conn(srv, "bearer", test_path="denied"))


@pytest.mark.parametrize("kw,message", [
    (dict(path="/q/{tomorrow}"), "unknown placeholder"),
    (dict(path="/q?x=1"), "Query"),
    (dict(path="https://evil.example.com/x"), "relative"),
    (dict(path="/q", body='{"a": 1}'), "GET"),
    (dict(method="POST", path="/q", body="{not json"), "JSON"),
    (dict(path="/q", variables=[{"name": "s", "values": ["a"]}]), "not used"),
    (dict(path="/q/{s}", variables=[{"name": "s", "values": ["a"]}, {"name": "s", "values": ["b"]}]), "twice"),
    (dict(path="/q/{today}", variables=[{"name": "today", "values": ["a"]}]), "reserved"),
    (dict(path="/q", query=[{"name": "since", "value": "{last_refresh}"}]), "initial"),
    (dict(path="/q", response={"jmespath": "a[?"}), "JMESPath"),
    (dict(path="/q", response={"records_path": "a..b"}), "records path"),
    (dict(path="/q", pagination={"type": "cursor", "param": "c"}), "cursor"),
    (dict(path="/q", pagination={"type": "page"}), "parameter"),
    (dict(path="/q", mode="append"), "key"),
    (dict(path="/q", timezone="Mars/Base"), "time zone"),
    (dict(path="/q", headers=[{"name": "X-A", "value": "a\r\nInjected: 1"}]), "header"),
])
def test_validation_refuses_what_cannot_work(kw, message):
    c = api.ApiConnectionSpec(base_url="https://api.example.com/v1")
    with pytest.raises(api.ApiError, match=message):
        api.validate_source(c, source(**kw), now=NOW)


def test_validation_counts_the_calls_and_resolves_the_first_request():
    c = api.ApiConnectionSpec(base_url="https://api.example.com/v1", auth="api_key", key_name="k", key_in="query",
                              secret_encrypted=encrypt_secret(SECRET))
    out = api.validate_source(c, source(path="/q/{s}", variables=[{"name": "s", "values": ["a", "b"]}]), now=NOW)
    assert out["calls"] == 2 and out["request"]["url"] == "https://api.example.com/v1/q/a?k=***"


@pytest.mark.parametrize("url", ["ftp://x/y", "https://user:pw@api.example.com", "https://api.example.com/v1?key=1",
                                 "https://api.example.com/#x", "api.example.com", "https:///x"])
def test_a_base_url_that_cannot_be_one(url):
    with pytest.raises(api.ApiError, match="base URL"):
        api.validate_source(api.ApiConnectionSpec(base_url=url), source(path="/q"), now=NOW)


def test_secrets_are_masked_in_logged_urls():
    c = api.ApiConnectionSpec(base_url="https://api.example.com", auth="api_key", key_name="k", key_in="query",
                              secret_encrypted=encrypt_secret(SECRET))
    assert api.Redactor(c).text(f"GET https://api.example.com/x?k={SECRET}&a=1") == "GET https://api.example.com/x?k=***&a=1"


def test_the_traceback_of_a_network_error_holds_no_secret():
    """The run keeps the traceback (Run.error_detail, shown in Runs): the exception
    of the HTTP library names the URL, query string included — it must not be chained."""
    import traceback

    c = api.ApiConnectionSpec(base_url="http://127.0.0.1:1/v1", auth="api_key", key_name="k", key_in="query",
                              secret_encrypted=encrypt_secret(SECRET), timeout_seconds=2)
    with pytest.raises(api.ApiError) as e:
        ingest(c, source(path="/x"))
    assert SECRET not in "".join(traceback.format_exception(e.value))


def test_the_traceback_of_a_tls_error_holds_no_secret(monkeypatch):
    import traceback

    import requests

    def boom(*a, **k):
        raise requests.exceptions.SSLError(f"HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: /x?k={SECRET}")

    monkeypatch.setattr(requests.Session, "request", boom)
    c = api.ApiConnectionSpec(base_url="https://api.example.com", auth="api_key", key_name="k", key_in="query",
                              secret_encrypted=encrypt_secret(SECRET))
    with pytest.raises(api.ApiError, match="TLS") as e:
        ingest(c, source(path="/x"))
    assert SECRET not in "".join(traceback.format_exception(e.value))


def test_a_connection_lost_while_reading_is_retried_and_holds_no_secret(srv, monkeypatch, no_real_waits):
    import traceback

    import requests

    srv.route("GET", "/v1/x", items({"id": 1}))
    import urllib3

    # the body is read with urllib3's read1 (so a deadline holds): its errors are urllib3's
    real = urllib3.response.HTTPResponse.read1
    calls = []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise urllib3.exceptions.ProtocolError(f"Connection broken while reading /v1/x?key={SECRET}")
        return real(self, *a, **k)

    monkeypatch.setattr(urllib3.response.HTTPResponse, "read1", flaky)
    _, df = ingest(conn(srv, "api_key", key_name="key", key_in="query"), source(path="/x"))
    assert df.height == 1 and len(no_real_waits) == 1

    calls.clear()
    monkeypatch.setattr(urllib3.response.HTTPResponse, "read1",
                        lambda self, *a, **k: (_ for _ in ()).throw(urllib3.exceptions.ProtocolError(f"broken ?key={SECRET}")))
    with pytest.raises(api.ApiError) as e:
        ingest(conn(srv, "api_key", key_name="key", key_in="query"), source(path="/x"))
    assert SECRET not in "".join(traceback.format_exception(e.value))


def test_a_netrc_never_adds_credentials(srv, tmp_path, monkeypatch):
    """requests reads ~/.netrc when a request has no auth of its own: a host listed
    there would get its password even on a hop to another host."""
    (tmp_path / ".netrc").write_text("machine localhost login netrcuser password netrc-pw\nmachine 127.0.0.1 login netrcuser password netrc-pw\n")
    (tmp_path / ".netrc").chmod(0o600)
    monkeypatch.setenv("NETRC", str(tmp_path / ".netrc"))
    monkeypatch.setenv("HOME", str(tmp_path))
    srv.route("GET", "/v1/x", lambda req: (302, {"Location": srv.other_host_url("/cdn")}, b""))
    srv.route("GET", "/cdn", items({"id": 1}))
    ingest(conn(srv), source(path="/x"))
    assert all("authorization" not in r.headers for r in srv.log)


# ── review 2026-10-18: the key never reaches another host, whatever the server copies ──
def _other_host_requests(srv):
    return [r for r in srv.log if r.host.startswith("localhost")]


def test_a_301_that_copies_the_whole_request_uri_to_another_host_loses_the_key(srv):
    srv.route("GET", "/v1/data", lambda req: (301, {"Location": srv.other_host_url(req.raw_path)}, b""))
    srv.route("GET", "/v1/data", lambda req: (200, {}, [{"id": 1}]) if req.host.startswith("localhost")
              else (301, {"Location": srv.other_host_url(req.raw_path)}, b""))
    _, df = ingest(conn(srv, "api_key", key_name="apikey", key_in="query"), source(path="/data", query=[{"name": "a", "value": "1"}]))
    (other,) = _other_host_requests(srv)
    assert df.height == 1 and other.query == [("a", "1")] and SECRET not in other.raw_path


def test_a_redirect_with_the_key_in_the_path_is_not_followed(srv):
    srv.route("GET", "/v1/data", lambda req: (302, {"Location": srv.other_host_url(f"/cdn/{req.q('apikey')}/x")}, b""))
    with pytest.raises(api.ApiError, match="holds the key") as e:
        ingest(conn(srv, "api_key", key_name="apikey", key_in="query"), source(path="/data"))
    assert _other_host_requests(srv) == [] and SECRET not in str(e.value)


def test_a_redirect_with_the_secret_under_another_name_or_encoded(srv):
    from urllib.parse import quote as q

    srv.route("GET", "/v1/a", lambda req: (302, {"Location": srv.other_host_url(f"/cdn?token={q(SECRET, safe='')}&b=2")}, b""))
    srv.route("GET", "/cdn", items({"id": 1}))
    _, df = ingest(conn(srv, "bearer"), source(path="/a"))
    (other,) = _other_host_requests(srv)
    assert df.height == 1 and other.query == [("b", "2")]
    srv.route("GET", "/v1/b", lambda req: (302, {"Location": srv.other_host_url(f"/cdn/{q(SECRET, safe='')}")}, b""))
    with pytest.raises(api.ApiError, match="holds the key"):
        ingest(conn(srv, "bearer"), source(path="/b"))


def test_a_json_next_link_to_another_host_that_copies_the_query_loses_the_key(srv):
    def page(req):
        if req.host.startswith("localhost"):
            return 200, {}, {"rows": [{"id": 2}], "next": None}
        return 200, {}, {"rows": [{"id": 1}], "next": srv.other_host_url(req.raw_path)}

    srv.route("GET", "/v1/n", page)
    _, df = ingest(conn(srv, "api_key", key_name="apikey", key_in="query"),
                   source(path="/n", response={"records_path": "rows"}, pagination={"type": "next_link", "next_path": "next"}))
    (other,) = _other_host_requests(srv)
    assert df["id"].to_list() == [1, 2] and SECRET not in other.raw_path


def test_a_link_header_to_another_host_that_copies_the_query_loses_the_key(srv):
    def page(req):
        if req.host.startswith("localhost"):
            return 200, {}, [{"id": 2}]
        return 200, {"Link": f'<{srv.other_host_url(req.raw_path)}>; rel="next"'}, [{"id": 1}]

    srv.route("GET", "/v1/l", page)
    _, df = ingest(conn(srv, "api_key", key_name="apikey", key_in="query"), source(path="/l", pagination={"type": "next_link"}))
    (other,) = _other_host_requests(srv)
    assert df["id"].to_list() == [1, 2] and SECRET not in other.raw_path


# ── «Try the call» rows and JSON-escaped secrets are redacted ──────────────────
def _echo(req):
    return 200, {}, {"items": [{"auth": req.headers.get("authorization"), "key": req.headers.get("x-key"),
                                req.headers.get("x-key") or "k": 1}]}


@pytest.mark.parametrize("auth,kw", [("bearer", {}), ("basic", {"username": "u"}), ("api_key", {"key_name": "X-Key"})])
def test_try_rows_and_column_names_are_redacted(srv, auth, kw):
    srv.route("GET", "/v1/echo", _echo)
    out = api.try_call(conn(srv, auth, **kw), source(path="/echo", response={"records_path": "items"}), now=NOW)
    assert out["ok"] and "***" in json.dumps(out["rows"])
    assert SECRET not in json.dumps(out) and base64.b64encode(f"u:{SECRET}".encode()).decode() not in json.dumps(out)


def test_try_rows_redact_the_oauth2_access_token(srv):
    srv.route("POST", "/oauth/token", lambda req: (200, {}, {"access_token": "acc-TOKEN-4242", "expires_in": 3600}))
    srv.route("GET", "/v1/echo", _echo)
    out = api.try_call(conn(srv, "oauth2", username="c", token_url=srv.url("/oauth/token")),
                       source(path="/echo", response={"records_path": "items"}), now=NOW)
    assert out["ok"] and "acc-TOKEN-4242" not in json.dumps(out) and "***" in json.dumps(out["rows"])


ODD = 'zz"odd\\sè/cret-77'


@pytest.mark.parametrize("ensure_ascii,slash", [(True, False), (False, False), (True, True)])
def test_a_secret_with_quotes_backslash_accents_and_slash_is_masked_in_json(srv, caplog, ensure_ascii, slash):
    import logging
    import traceback

    def echo(req, status=200):
        text = json.dumps({"items": [{"auth": req.headers.get("authorization")}]}, ensure_ascii=ensure_ascii)
        return status, {"Content-Type": "application/json"}, (text.replace("/", "\\/") if slash else text).encode()

    srv.route("GET", "/v1/echo", echo)
    srv.route("GET", "/v1/err", lambda req: echo(req, 400))
    c = api.ApiConnectionSpec(base_url=srv.url("/v1"), auth="bearer", secret_encrypted=encrypt_secret(ODD), timeout_seconds=5)
    out = api.try_call(c, source(path="/echo"), now=NOW)
    forms = [ODD, json.dumps(ODD)[1:-1], json.dumps(ODD, ensure_ascii=False)[1:-1], json.dumps(ODD)[1:-1].replace("/", "\\/")]
    blob = json.dumps(out, ensure_ascii=False)
    assert "***" in out["excerpt"] and not any(f in blob or f in out["excerpt"] for f in forms)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(api.ApiError) as e:
        ingest(c, source(path="/err"))
    text = str(e.value) + "".join(traceback.format_exception(e.value)) + caplog.text
    assert "400" in str(e.value) and not any(f in text for f in forms)


# ── pagination that cannot cut or loop ─────────────────────────────────────────
def test_offset_goes_on_when_the_api_caps_the_page_size(srv):
    def capped(req):
        o, n = int(req.q("offset")), min(int(req.q("limit")), 50)
        return 200, {}, [{"n": i} for i in range(o, min(o + n, 120))]

    srv.route("GET", "/v1/o", capped)
    _, df = ingest(conn(srv), source(path="/o", pagination={"type": "offset", "param": "offset", "limit_param": "limit", "limit": 100}))
    assert df["n"].to_list() == list(range(120)) and [r.q("offset") for r in srv.log] == ["0", "50", "100", "120"]


def test_a_next_link_back_to_a_page_already_read_is_an_error(srv):
    srv.route("GET", "/v1/self", lambda req: (200, {}, {"rows": [{"id": 1}], "next": srv.url(req.raw_path)}))
    with pytest.raises(api.ApiError, match="already read"):
        ingest(conn(srv), source(path="/self", response={"records_path": "rows"}, pagination={"type": "next_link", "next_path": "next"}))
    srv.route("GET", "/v1/loop", lambda req: (200, {}, {"rows": [{"p": req.q("p")}], "next": "/v1/loop?p=" + ("2" if req.q("p", "1") == "1" else "1")}))
    with pytest.raises(api.ApiError, match="already read"):
        ingest(conn(srv), source(path="/loop", query=[{"name": "p", "value": "1"}], response={"records_path": "rows"},
                                 pagination={"type": "next_link", "next_path": "next"}))


def test_a_cursor_already_seen_is_an_error(srv):
    nxt = {None: "A", "A": "B", "B": "A"}
    srv.route("GET", "/v1/c", lambda req: (200, {}, {"items": [{"c": req.q("cursor")}], "meta": {"next": nxt[req.q("cursor")]}}))
    with pytest.raises(api.ApiError, match="already read"):
        ingest(conn(srv), source(path="/c", response={"records_path": "items"},
                                 pagination={"type": "cursor", "param": "cursor", "cursor_path": "meta.next"}))
    assert len(srv.log) == 3


def test_keyed_values_cannot_hold_a_key_field(srv):
    srv.route("GET", "/v1/fx", lambda req: (200, {}, {"rates": {"2024-01-01": {"_key": "x", "v": 1}}}))
    with pytest.raises(api.ApiError, match="_key"):
        ingest(conn(srv), source(path="/fx", response={"records_path": "rates", "keyed": True}))


# ── a wall-clock limit per request, and POST retried only before it was sent ──
def test_a_server_that_drips_its_answer_is_stopped_by_the_request_limit(srv, monkeypatch):
    import time as _time

    monkeypatch.setattr(get_settings().api, "max_request_seconds", 1.0)

    def drip():
        yield b"["
        for _ in range(40):
            _time.sleep(0.1)
            yield b" "
        yield b"]"

    srv.route("GET", "/v1/slow", lambda req: (200, {"Content-Type": "application/json"}, drip()))
    started = _time.monotonic()
    with pytest.raises(api.ApiError, match="API__MAX_REQUEST_SECONDS"):
        ingest(conn(srv, timeout_seconds=3), source(path="/slow"))
    assert _time.monotonic() - started < 3


def test_a_post_is_not_retried_after_it_was_sent(srv, no_real_waits):
    srv.route("POST", "/v1/p", lambda req: (500, {}, b"boom"))
    with pytest.raises(api.ApiError, match="500"):
        ingest(conn(srv), source(method="POST", path="/p", body='{"a": 1}'))
    assert len(srv.log) == 1

    import time as _time

    srv.route("POST", "/v1/slow", lambda req: (_time.sleep(1.5), (200, {}, [{"id": 1}]))[1])
    with pytest.raises(api.ApiError):
        ingest(conn(srv, timeout_seconds=0.5), source(method="POST", path="/slow", body='{"a": 1}'))
    assert len(srv.requests_to("/v1/slow")) == 1


def test_a_post_is_retried_on_429_and_on_errors_before_sending(srv, no_real_waits):
    calls = []
    srv.route("POST", "/v1/p", lambda req: (calls.append(1) or (429, {"Retry-After": "1"}, b"")) if len(calls) < 1 else (200, {}, [{"id": 1}]))
    _, df = ingest(conn(srv), source(method="POST", path="/p", body='{"a": 1}'))
    assert df.height == 1 and len(srv.log) == 2
    c = api.ApiConnectionSpec(base_url="http://127.0.0.1:1/v1", timeout_seconds=1)
    with pytest.raises(api.ApiError, match="attempts"):
        ingest(c, source(method="POST", path="/x", body='{"a": 1}'))


def test_a_server_that_drips_its_headers_is_stopped_by_the_request_limit(monkeypatch):
    """The wall clock covers the whole request: a status line, then header bytes one
    at a time — each resets the read timeout, only a watchdog stops it."""
    import socket
    import threading
    import time as _time

    monkeypatch.setattr(get_settings().api, "max_request_seconds", 1.0)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve():
        conn_, _ = listener.accept()
        conn_.recv(65536)
        try:
            conn_.sendall(b"HTTP/1.1 200 OK\r\n")
            for b in b"X-Drip: " + b"a" * 200:
                if stop.is_set():
                    break
                conn_.sendall(bytes([b]))
                _time.sleep(0.1)
        except OSError:
            pass
        finally:
            conn_.close()

    threading.Thread(target=serve, daemon=True).start()
    c = api.ApiConnectionSpec(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}/v1", timeout_seconds=3)
    started = _time.monotonic()
    try:
        with pytest.raises(api.ApiError, match="API__MAX_REQUEST_SECONDS"):
            ingest(c, source(path="/x"))
        assert _time.monotonic() - started < 3
    finally:
        stop.set()
        listener.close()


def test_a_secret_escaped_with_uppercase_hex_is_masked(srv):
    """Java's Jackson writes è as \\u00E8, Python as \\u00e8."""
    def echo(req):
        text = json.dumps({"items": [{"auth": req.headers.get("authorization")}]})
        return 200, {"Content-Type": "application/json"}, re.sub(r"\\u([0-9a-f]{4})", lambda m: "\\u" + m.group(1).upper(), text).encode()

    import re

    srv.route("GET", "/v1/echo", echo)
    c = api.ApiConnectionSpec(base_url=srv.url("/v1"), auth="bearer", secret_encrypted=encrypt_secret(ODD), timeout_seconds=5)
    out = api.try_call(c, source(path="/echo"), now=NOW)
    assert "\\u00E8" not in out["excerpt"] and "***" in out["excerpt"] and "odd" not in json.dumps(out)
