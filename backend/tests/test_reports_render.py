"""A report from its definition to its files (app/reports/render.py): the PDF and the pages on
screen laid out by Typst in a child process, Excel, the email's MHTML; the limits of a
render; the task on the export queue and the engine's routes. The chart service is a fake
here (it answers a fixed SVG): the real one is Node (frontend/chart-renderer)."""
import email
import hashlib
import json
import re
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import exports
from app.api.routes import reports as report_routes
from app.api.routes import tasks
from app.reports import pdf, render
from app.reports.errors import ReportError
from app.tasks import jobs, report_jobs
from tests import report_samples as samples
from tests.fakes import FakeRedis
from tests.test_export_route import Result, Storage, _play

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
# The sample report's PDF, byte for byte. It changes with the template, the fonts, Typst's
# version (pinned in pyproject.toml) or the model: look at the new PDF, then update the hash.
GOLDEN_PDF_SHA256 = "97bcba85e5401bdaf99c211e6a2bf025bc37ec603058f332f0cc8ab0ee08be4c"


@pytest.fixture(scope="module")
def chart_service():
    """A chart service that answers every chart with a rectangle of its size."""
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — the name http.server calls
            spec = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append(spec)
            w, h = spec["width"], spec["height"]
            body = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
                    f'<rect x="0" y="0" width="{w}" height="{h}" fill="#3b5bdb"/></svg>').encode()
            self.send_response(200)
            self.send_header("content-type", "image/svg+xml")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", calls
    server.shutdown()


def run(tmp_path, chart_service, fmt="pdf", rows=300, definition=None, **kw):
    limits = render.Limits(chart_url=chart_service[0], **kw.pop("limits", {}))
    return render.render(definition or samples.definition(), samples.datasets(rows), {}, fmt, tmp_path, title="Vendite",
                         limits=limits, now=NOW, **kw)


# ── PDF ──────────────────────────────────────────────────────────────────────
def test_the_pdf_is_byte_identical_and_matches_its_golden(tmp_path, chart_service):
    a = run(tmp_path / "a", chart_service)
    b = run(tmp_path / "b", chart_service)
    first, second = a.path.read_bytes(), b.path.read_bytes()
    assert first.startswith(b"%PDF-") and first == second
    assert a.pages == 8 and a.warnings == []
    assert hashlib.sha256(first).hexdigest() == GOLDEN_PDF_SHA256


def test_hostile_names_are_text(tmp_path, chart_service):
    """Typst markup, HTML, a formula, a backslash in the data: the template receives them
    as JSON values, so they print and change nothing — no page of 1 mm, no panic."""
    rows = samples.orders(300)
    assert any(r["customer"] in samples.HOSTILE for r in rows)
    r = run(tmp_path, chart_service, "svg")
    pages = json.loads(r.path.read_text())["pages"]
    sizes = {re.search(r'viewBox="([^"]+)"', p).group(1) for p in pages}
    assert len(pages) == 8 and sizes == {"0 0 595.275590551 841.88976378"}  # A4, every page


def test_the_pages_on_screen_are_svg_with_where_to_click(tmp_path, chart_service):
    state = {"items": {"detail": {"sort": {"column": 4, "dir": "desc"}, "collapsed": [["Isole"]]}}}
    r = run(tmp_path, chart_service, "svg", state=state)
    out = json.loads(r.path.read_text())
    assert len(out["pages"]) == r.pages and all(p.startswith("<svg") for p in out["pages"])
    kinds = {(h["kind"], h["item"]) for h in out["hotspots"]}
    assert kinds == {("sort", "detail"), ("toggle", "detail")}
    toggles = [h for h in out["hotspots"] if h["kind"] == "toggle"]
    assert {"key": ["Isole"], "closed": True}.items() <= next(h for h in toggles if h["key"] == ["Isole"]).items()
    assert all(1 <= h["page"] <= r.pages and h["x"] > 0 and h["y"] > 0 for h in out["hotspots"])



def test_a_closed_group_ends_the_repetition_of_the_header_before_it(tmp_path):
    """The Sample margins with «Insaccati da cuocere» closed: «Insaccati cotti» ends at the foot
    of page 1, and page 2 opened again with its header — nothing under it — because a closed
    group was a plain cell, not a header ending the repetition of the one before."""
    data = json.loads((Path(__file__).parent / "report_margins.json").read_text())
    rows = [dict(zip([c["name"] for c in data["columns"]], r)) for r in data["rows"]]
    state = {"items": {"detail": {"collapsed": [["Insaccati da cuocere"]]}}}
    r = render.render(data["definition"], {"margins": {"rows": rows, "columns": data["columns"]}},
                      {"from_month": 202401}, "svg", tmp_path, state=state, limits=render.Limits(chart_url=""),
                      now=NOW)
    toggles = [h for h in json.loads(r.path.read_text())["hotspots"] if h["kind"] == "toggle"]
    first_page = {h["key"][0] for h in toggles if h["page"] == 1}
    second_page = [h["key"][0] for h in toggles if h["page"] == 2]
    assert "Insaccati cotti" in first_page and second_page[0] == "Insaccati da cuocere", second_page


# ── limits ───────────────────────────────────────────────────────────────────
def test_too_many_pages_stop_the_render(tmp_path, chart_service):
    with pytest.raises(ReportError, match="would have 8 pages, more than the 2"):
        run(tmp_path, chart_service, limits={"max_pages": 2})


def test_too_many_rows_stop_the_render_before_any_layout(tmp_path, chart_service):
    with pytest.raises(ReportError, match="«orders» has more than 100 rows"):
        run(tmp_path, chart_service, limits={"max_rows": 100})
    assert not (tmp_path / "typst").exists()


def test_the_layout_s_memory_is_limited(tmp_path, chart_service):
    with pytest.raises(ReportError, match="needs more than 150 MB") as e:
        run(tmp_path, chart_service, rows=3000, limits={"memory_mb": 150, "max_pages": 1000})
    assert e.value.tag == "unprocessable"


def test_a_child_that_allocates_too_much_is_a_memory_error(tmp_path):
    child = tmp_path / "greedy.py"
    child.write_text("import resource, sys\nlimit = int(sys.argv[1]) << 20\n"
                     "resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))\nb = bytearray(1 << 30)\n")
    with pytest.raises(ReportError, match="needs more than 64 MB"):
        pdf.run({"root": str(tmp_path)}, 64, 30, child=child)


def test_a_child_that_takes_too_long_is_killed(tmp_path):
    child = tmp_path / "slow.py"
    child.write_text("import os, time\nopen('pid', 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    with pytest.raises(ReportError, match="longer than 1 s") as e:
        pdf.run({"root": str(tmp_path)}, 256, 1, child=child)
    assert e.value.tag == "timeout"
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(pid, 0)


def test_the_child_does_not_outlive_its_parent():
    # started by a process that is not its parent (pid 1 is never ours): it leaves at once
    done = subprocess.run([sys.executable, "-I", str(pdf.CHILD), "256", "1"], input=b"{}", capture_output=True,
                          timeout=30)
    assert done.returncode == 1 and done.stdout == b""


def test_without_the_chart_service_a_report_with_charts_fails_clearly(tmp_path):
    with pytest.raises(ReportError, match="chart service is not answering") as e:
        render.render(samples.definition(), samples.datasets(50), {}, "pdf", tmp_path,
                      limits=render.Limits(chart_url="http://127.0.0.1:9"), now=NOW)
    assert e.value.tag == "unavailable"
    with pytest.raises(ReportError, match="no chart service is configured"):
        render.render(samples.definition(), samples.datasets(50), {}, "pdf", tmp_path, limits=render.Limits(), now=NOW)


def test_a_field_the_data_does_not_have_is_named_at_render_time(tmp_path, chart_service):
    data = samples.datasets(10)
    for row in data["orders"]["rows"]:
        row.pop("product")
    data["orders"]["columns"] = [c for c in samples.COLUMNS if c["name"] != "product"]
    with pytest.raises(ReportError, match=r"item «detail», columns\[2\].value: there is no field \[product\]"):
        render.render(samples.definition(), data, {}, "pdf", tmp_path, limits=render.Limits(chart_url=chart_service[0]),
                      now=NOW)


# ── Excel ────────────────────────────────────────────────────────────────────
def test_excel_keeps_values_groups_and_subtotal_formulas(tmp_path, chart_service):
    r = run(tmp_path, chart_service, "xlsx")
    z = zipfile.ZipFile(r.path)
    book = z.read("xl/workbook.xml").decode()
    assert re.findall(r'<sheet name="([^"]+)"', book) == ["detail", "Parametri"]
    sheet = z.read("xl/worksheets/sheet1.xml").decode()
    strings = z.read("xl/sharedStrings.xml").decode()
    # a text is never a formula: the customer called =HYPERLINK(…) is a shared string
    assert '=HYPERLINK("http://x")' in strings
    assert all(f.startswith("SUBTOTAL(9,") for f in re.findall(r"<f>([^<]+)</f>", sheet))
    cached = re.findall(r"<f>SUBTOTAL\(9,E\d+:E\d+\)</f><v>([^<]+)</v>", sheet)
    assert cached and float(cached[-1]) == pytest.approx(sum(o["amount"] for o in samples.orders(300)), abs=0.01)
    assert 'state="frozen"' in sheet and "<rowBreaks" in sheet
    assert 'outlineLevel="2"' in sheet and 'outlineLevel="1"' in sheet
    assert "Print_Titles" in book
    styles = z.read("xl/styles.xml").decode()
    assert "[Red]" in styles


# ── the email ────────────────────────────────────────────────────────────────
def _mhtml(path) -> tuple[str, list]:
    message = email.message_from_bytes(path.read_bytes())
    assert message.get_content_type() == "multipart/related" and message.get_param("type") == "text/html"
    html_part, *images = message.get_payload()
    return html_part.get_payload(decode=True).decode("utf-8"), images


def test_the_email_body_carries_its_charts_by_cid_and_stays_small(tmp_path, chart_service):
    r = run(tmp_path, chart_service, "html")
    assert r.path.suffix == ".mhtml"
    html, images = _mhtml(r.path)
    assert len(html.encode("utf-8")) < 102 * 1024
    cids = re.findall(r'src="cid:([^"]+)"', html)
    assert len(cids) == 2 and sorted(cids) == sorted(i["Content-ID"].strip("<>") for i in images)
    assert all(i.get_payload(decode=True).startswith(b"\x89PNG") for i in images)
    assert "Totale Regione: Centro" in html and "€ 346.161" in html


def test_names_in_the_email_are_escaped(tmp_path, chart_service):
    d = samples.definition()
    d["sections"]["body"]["items"][6]["groups"] = [{"by": "[customer]"}]
    html, _ = _mhtml(run(tmp_path, chart_service, "html", definition=d).path)
    assert "&lt;script&gt;alert(1)&lt;/script&gt; S.p.A." in html and "<script>" not in html
    assert "#panic(&quot;boom&quot;)" in html and "Back\\slash" in html


def test_a_long_summary_is_cut_under_gmail_s_limit(tmp_path, chart_service):
    d = samples.definition()
    d["sections"]["body"]["items"][6]["groups"] = [{"by": "[order]"}]
    d["sections"]["body"]["items"] = [it for it in d["sections"]["body"]["items"] if it["type"] in ("table", "text")]
    r = run(tmp_path, chart_service, "html", rows=3000, definition=d, limits={"max_pages": 5000})
    html, images = _mhtml(r.path)
    assert len(html.encode("utf-8")) < 102 * 1024 and images == []
    assert "il dettaglio completo è nel PDF" in html


# ── choices of a parameter ───────────────────────────────────────────────────
def test_the_choices_of_a_parameter_come_from_a_dataset():
    out = render.choices(samples.definition(), "orders", samples.datasets(300), {}, "[region]",
                         '=UPPER([region])', limit=3, now=NOW)
    assert out["truncated"] is True and len(out["choices"]) == 3
    assert out["choices"][0] == {"value": out["choices"][0]["value"], "label": out["choices"][0]["value"].upper()}
    days = render.choices(samples.definition(), "orders", samples.datasets(20), {}, "[day]", None, now=NOW)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["value"]) for c in days["choices"])
    with pytest.raises(ReportError, match="there is no field \\[regoin\\]"):
        render.choices(samples.definition(), "orders", samples.datasets(5), {}, "[regoin]", now=NOW)


def test_the_choices_task_answers_as_a_preview():
    ok = report_jobs.report_choices_task.run(samples.definition(), "orders", samples.datasets(5), {}, "[region]")
    assert ok["ok"] and ok["result"]["choices"]
    bad = report_jobs.report_choices_task.run(samples.definition(), "orders", {}, {}, "[region]")
    assert bad == {"ok": False, "error": "unprocessable",
                   "detail": "the dataset «orders» was not sent with the report: it cannot be read"}


# ── the task and the routes ──────────────────────────────────────────────────
@pytest.fixture
def worker(monkeypatch, chart_service):
    storage = Storage()
    results: dict[str, Result] = {}
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    monkeypatch.setattr(exports, "get_storage_service", lambda: storage)
    monkeypatch.setattr(exports, "_client", FakeRedis())
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0])))

    def send_task(name, kwargs, task_id):
        assert name == "app.tasks.report_jobs.render_report_task"
        results[task_id] = Result(report_jobs.render_report_task.apply(kwargs=kwargs, task_id=task_id).get())
        return results[task_id]

    def lookup(task_id, app=None):
        return results.get(task_id) or Result(None, state="PENDING")

    monkeypatch.setattr(report_routes.celery_app, "send_task", send_task)
    monkeypatch.setattr(tasks, "AsyncResult", lookup)
    monkeypatch.setattr(exports, "AsyncResult", lookup)
    return storage


def _start(**body):
    return report_routes.render(report_routes.ReportRender(**{"definition": samples.definition(),
                                                             "datasets": samples.datasets(60), "owner": "u1", **body}))


def test_a_report_is_followed_and_taken_as_an_export(worker):
    started = _start(format="pdf", filename="Vendite Nord")
    assert started.filename == "Vendite Nord.pdf"
    status = tasks.export_status(started.task_id, owner="u1")
    assert status["status"] == "ready" and status["filename"] == "Vendite Nord.pdf" and status["pages"] >= 1
    with pytest.raises(HTTPException) as e:
        tasks.export_file(started.task_id, owner="u2")
    assert e.value.status_code == 404
    headers, body = _play(tasks.export_file(started.task_id, owner="u1"))
    assert headers["content-type"] == "application/pdf" and body.startswith(b"%PDF-")
    assert worker.keys("exports/") == []


def test_the_email_and_the_screen_get_the_extension_of_what_they_hold(worker):
    assert _start(format="html", filename="Vendite.html").filename == "Vendite.mhtml"
    svg = _start(format="svg")
    assert svg.filename == "report.json"
    headers, body = _play(tasks.export_file(svg.task_id, owner="u1"))
    assert headers["content-type"] == "application/json" and json.loads(body)["pages"]


def test_a_render_that_cannot_go_on_is_a_failed_export_with_its_status(worker, monkeypatch):
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url="http://127.0.0.1:9")))
    task_id = _start().task_id
    status = tasks.export_status(task_id, owner="u1")
    assert status["status"] == "failed" and status["code"] == 503 and "chart service" in status["detail"]
    over = _start(params={"since": "not a date"}).task_id
    assert tasks.export_status(over, owner="u1")["code"] == 422


def test_a_broken_definition_is_refused_before_the_queue(worker):
    d = samples.definition()
    d["sections"]["body"]["items"][6]["columns"][0]["value"] = "[order"
    with pytest.raises(HTTPException) as e:
        _start(definition=d)
    assert e.value.status_code == 422 and "item «detail», columns[0].value" in e.value.detail


def test_check_names_every_mistake_and_lets_empty_images_be():
    d = samples.definition()
    d["sections"]["body"]["items"][6]["filter"] = "[day] >= PARAM(\"snice\")"
    d["sections"]["page_header"]["items"].append({"id": "logo", "type": "image", "x": 150, "y": 0, "w": 30, "h": 10,
                                                   "asset": ""})
    out = report_routes.check(report_routes.ReportCheck(definition=d))
    assert out["problems"] == [{"item": "detail", "field": "filter", "pos": 15, "length": 7,
                                "message": 'there is no parameter «snice»: did you mean "since"?'}]
