"""Connettore SharePoint + Excel, provato contro un Graph finto (tests/fake_graph).

NON sostituisce la prova contro un tenant vero: fissa il contratto che usiamo e,
soprattutto, le scelte nostre — il glob, l'impilamento dei file, e il rifiuto di
ciò che non è machine readable.
"""
import io

import polars as pl
import pytest

from app.core.crypto import encrypt_secret
from app.ingest import sharepoint_source as sp
from tests.fake_graph import GRAPH, LOGIN, FakeGraph, xlsx
from tests.fakes import FakeStorage

GEN = xlsx([["id", "importo"], [1, 10.5], [2, 20]])
FEB = xlsx([["id", "importo"], [3, 30]])


def _conn(**kw):
    base = dict(tenant_id="T", client_id="C", client_secret_encrypted=encrypt_secret("s3gr3t0"),
                site_url="https://contoso.sharepoint.com/sites/Finance", graph_base=GRAPH, login_base=LOGIN)
    base.update(kw)
    return sp.SharePointConnectionSpec(**base)


def _client(graph, **kw):
    return sp.GraphClient(_conn(**kw), http=graph.request, sleep=lambda s: None)


def _ingest(graph, path, sheet="Dati", **kw):
    st = FakeStorage()
    res = sp.ingest_sharepoint_to_parquet(_conn(**kw), sp.SharePointSourceSpec(path=path, sheet=sheet), "b", "datasets/x.parquet", st, client=_client(graph, **kw))
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "datasets/x.parquet")]))


# ── glob ───────────────────────────────────────────────────────────────────────
TREE = {
    "Budget/2026/gen.xlsx": GEN, "Budget/2026/feb.xlsx": FEB, "Budget/2026/note.docx": b"x",
    "Budget/2026/~$gen.xlsx": b"lock", "Budget/2025/dic.xlsx": GEN, "Budget/2025/q4/chiusura.XLSX": GEN,
    "Altro/gen.xlsx": GEN,
}


def _paths(pattern, **kw):
    return [f.path for f in sp.find_files(_client(FakeGraph(TREE, **kw)), pattern)]


def test_a_single_file():
    assert _paths("Budget/2026/gen.xlsx") == ["Budget/2026/gen.xlsx"]


def test_a_star_takes_the_excel_files_of_one_folder_only():
    """Non il .docx, e NON il file di blocco `~$…` che Office crea mentre qualcuno
    ha il file aperto: un `*.xlsx` lo prenderebbe, ed è spazzatura."""
    assert _paths("Budget/2026/*.xlsx") == ["Budget/2026/feb.xlsx", "Budget/2026/gen.xlsx"]


def test_a_star_on_a_folder_segment():
    assert _paths("Budget/*/gen.xlsx") == ["Budget/2026/gen.xlsx"]


def test_double_star_descends_to_any_depth_including_zero():
    assert _paths("Budget/**/*.xlsx") == [
        "Budget/2025/dic.xlsx", "Budget/2025/q4/chiusura.XLSX", "Budget/2026/feb.xlsx", "Budget/2026/gen.xlsx"]


def test_matching_ignores_case_like_sharepoint():
    assert _paths("budget/2025/Q4/*.xlsx") == ["Budget/2025/q4/chiusura.XLSX"]


def test_a_fixed_prefix_is_walked_without_listing_it():
    g = FakeGraph(TREE)
    sp.find_files(_client(g), "Budget/2026/*.xlsx")
    listed = [u for _, u in g.calls if u.endswith("/children")]
    assert len(listed) == 1 and "Budget/2026" in listed[0]


def test_listing_follows_pagination():
    tree = {f"Mesi/m{i:02d}.xlsx": GEN for i in range(7)}
    assert len(sp.find_files(_client(FakeGraph(tree, page_size=3)), "Mesi/*.xlsx")) == 7


def test_a_runaway_glob_is_stopped(monkeypatch):
    monkeypatch.setattr(sp, "MAX_FILES", 3)
    with pytest.raises(sp.SharePointError, match="più di 3 file"):
        sp.find_files(_client(FakeGraph({f"M/m{i}.xlsx": GEN for i in range(5)})), "M/*.xlsx")


# ── import ─────────────────────────────────────────────────────────────────────
def test_one_file_becomes_a_table_with_its_provenance():
    res, df = _ingest(FakeGraph(TREE), "Budget/2026/gen.xlsx")
    assert res["rows_written"] == 2 and df.columns == ["id", "importo", "_file", "_file_modified_at"]
    assert df["_file"].unique().to_list() == ["Budget/2026/gen.xlsx"]
    assert df["_file_modified_at"].dtype == pl.Datetime and df["_file_modified_at"][0].year == 2026


def test_a_glob_stacks_the_files_and_every_row_says_where_it_came_from():
    res, df = _ingest(FakeGraph(TREE), "Budget/2026/*.xlsx")
    assert res["rows_written"] == 3 and [f["path"] for f in res["files"]] == ["Budget/2026/feb.xlsx", "Budget/2026/gen.xlsx"]
    assert df.group_by("_file").len().sort("_file").rows() == [("Budget/2026/feb.xlsx", 1), ("Budget/2026/gen.xlsx", 2)]


def test_files_with_different_columns_are_united_by_name_and_the_drift_is_reported():
    tree = {"M/a.xlsx": GEN, "M/b.xlsx": xlsx([["id", "importo", "filiale"], [9, 1.0, "MI"]])}
    res, df = _ingest(FakeGraph(tree), "M/*.xlsx")
    assert res["schema_drift"] == ["filiale"]
    assert df.columns == ["id", "importo", "filiale", "_file", "_file_modified_at"]  # provenienza in fondo
    assert df.filter(pl.col("_file") == "M/a.xlsx")["filiale"].to_list() == [None, None]


def test_nothing_matching_is_an_error_not_an_empty_table():
    with pytest.raises(sp.SharePointError, match="Nessun file Excel"):
        _ingest(FakeGraph(TREE), "Budget/2027/*.xlsx")


# ── machine readable, o niente ─────────────────────────────────────────────────
def test_a_title_above_the_header_is_refused():
    """La libreria, da sola, leggerebbe `REPORT BUDGET` come nome di colonna e
    inventerebbe `__UNNAMED__1`: il dato sporco passerebbe in silenzio."""
    tree = {"M/brutto.xlsx": xlsx([["REPORT BUDGET 2026"], [], ["id", "importo"], [1, 2]])}
    with pytest.raises(sp.SharePointError, match=r"M/brutto\.xlsx.*PRIMA riga"):
        _ingest(FakeGraph(tree), "M/*.xlsx")


def test_a_hole_in_the_header_is_refused_and_says_which_column():
    with pytest.raises(sp.SharePointError, match="colonne senza nome: 2"):
        _ingest(FakeGraph({"M/x.xlsx": xlsx([["id", "", "importo"], [1, 2, 3]])}), "M/x.xlsx")


def test_repeated_column_names_are_refused():
    """Altrimenti la seconda diventerebbe `id_1`, indistinguibile da una colonna vera."""
    with pytest.raises(sp.SharePointError, match="(?i)ripetuti.*id"):
        _ingest(FakeGraph({"M/x.xlsx": xlsx([["id", "ID", "importo"], [1, 2, 3]])}), "M/x.xlsx")


def test_a_missing_sheet_names_the_file():
    tree = {"M/a.xlsx": GEN, "M/b.xlsx": xlsx([["id"], [1]], sheet="Altro")}
    with pytest.raises(sp.SharePointError, match=r"M/b\.xlsx.*foglio 'Dati' non esiste"):
        _ingest(FakeGraph(tree), "M/*.xlsx")


def test_an_empty_sheet_is_refused():
    with pytest.raises(sp.SharePointError, match="è vuoto"):
        _ingest(FakeGraph({"M/x.xlsx": xlsx([])}), "M/x.xlsx")


def test_the_provenance_column_names_are_reserved():
    with pytest.raises(sp.SharePointError, match="riservato"):
        _ingest(FakeGraph({"M/x.xlsx": xlsx([["id", "_file"], [1, "a"]])}), "M/x.xlsx")


# ── accesso ────────────────────────────────────────────────────────────────────
def test_a_wrong_secret_says_so_without_the_trace_noise():
    with pytest.raises(sp.SharePointError, match="Invalid client secret") as e:
        _ingest(FakeGraph(TREE, secret="altro"), "Budget/2026/gen.xlsx")
    assert "Trace ID" not in str(e.value)


def test_no_grant_on_the_site_points_at_sites_selected():
    with pytest.raises(sp.SharePointError, match="Sites.Selected"):
        _ingest(FakeGraph(TREE, granted=False), "Budget/2026/gen.xlsx")


def test_an_unknown_site_is_reported_as_such():
    with pytest.raises(sp.SharePointError, match="Sito SharePoint: non trovato"):
        _ingest(FakeGraph(TREE), "Budget/2026/gen.xlsx", site_url="https://contoso.sharepoint.com/sites/Nope")


def test_a_named_library_is_resolved_and_a_wrong_one_lists_the_real_ones():
    g = FakeGraph(TREE, libraries=("Documenti", "Contabilita"))
    assert _client(g, library="contabilita").drive_id() == "DRV-Contabilita"
    with pytest.raises(sp.SharePointError, match="Disponibili: Contabilita, Documenti"):
        _client(g, library="Boh").drive_id()


def test_the_token_is_fetched_once_and_throttling_is_waited_out():
    g = FakeGraph(TREE); g.throttle_next = 2
    waits = []
    c = sp.GraphClient(_conn(), http=g.request, sleep=waits.append)
    assert len(sp.find_files(c, "Budget/2026/*.xlsx")) == 2
    assert g.tokens == 1 and waits == [1.0, 1.0]
