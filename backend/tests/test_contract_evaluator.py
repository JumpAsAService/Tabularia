"""Il valutatore dei data contracts: ogni regola, quando regge e quando no.

Un referto sbagliato qui è il difetto più caro di tutta la funzione: un «passa»
di troppo pubblica dati rotti, un «non passa» di troppo ferma dati buoni.
"""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.contracts.evaluator import evaluate, family, profile, propose

ADESSO = datetime(2026, 10, 6, 12, 0, 0)


def dati() -> pl.LazyFrame:
    return pl.LazyFrame({
        "id": [1, 2, 3, 4, 5],
        "riga": [1, 2, 3, 1, 1],
        "ordine": [10, 10, 10, 11, 12],
        "stato": ["aperto", "spedito", "aperto", None, "perso"],
        "quantita": [1.0, 2.5, -3.0, None, 40.0],
        "sku": ["ABC-1", "ABC-22", "abc-3", "XYZ-9", None],
        "creato": [datetime(2026, 10, 1), datetime(2026, 10, 2), datetime(2026, 10, 3), datetime(2026, 10, 4), datetime(2026, 10, 5, 9)],
        "spedito": [datetime(2026, 10, 2), datetime(2026, 10, 1), None, datetime(2026, 10, 6), datetime(2026, 10, 5, 10)],
        "giorno": [date(2026, 10, 1)] * 5,
        "attivo": [True, False, True, True, False],
    })


def uno(regola: dict, lf: pl.LazyFrame | None = None, **kw) -> dict:
    referto = evaluate(lf if lf is not None else dati(), {"rules": [{"id": "r", "severity": "error", **regola}]}, now=ADESSO, **kw)
    return referto["rules"][0]


# ── le regole, una per una ───────────────────────────────────────────────────
@pytest.mark.parametrize("regola, passa", [
    ({"kind": "column", "column": "id"}, True),
    ({"kind": "column", "column": "non_esiste"}, False),
    ({"kind": "column", "column": "id", "dtype": "integer"}, True),
    ({"kind": "column", "column": "id", "dtype": "number"}, True),      # un intero è anche un numero
    ({"kind": "column", "column": "quantita", "dtype": "integer"}, False),
    ({"kind": "column", "column": "quantita", "dtype": "number"}, True),
    ({"kind": "column", "column": "stato", "dtype": "string"}, True),
    ({"kind": "column", "column": "stato", "dtype": "integer"}, False),
    ({"kind": "column", "column": "creato", "dtype": "datetime"}, True),
    ({"kind": "column", "column": "giorno", "dtype": "date"}, True),
    ({"kind": "column", "column": "giorno", "dtype": "datetime"}, False),
    ({"kind": "column", "column": "attivo", "dtype": "boolean"}, True),
    ({"kind": "not_null", "column": "id"}, True),
    ({"kind": "not_null", "column": "stato"}, False),
    ({"kind": "unique", "columns": ["id"]}, True),
    ({"kind": "unique", "column": "id"}, True),                          # forma breve
    ({"kind": "unique", "columns": ["ordine"]}, False),
    ({"kind": "unique", "columns": ["ordine", "riga"]}, True),           # chiave composta
    ({"kind": "unique", "columns": ["ordine", "attivo"]}, False),
    ({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito", "perso"]}, True),   # il null non è un valore sbagliato
    ({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito"]}, False),
    ({"kind": "accepted_values", "column": "riga", "values": [1, 2, 3]}, True),
    ({"kind": "accepted_values", "column": "riga", "values": ["1", "2", "3"]}, True),   # scritto come testo, colonna numerica
    ({"kind": "accepted_values", "column": "riga", "values": [1]}, False),
    ({"kind": "accepted_values", "column": "attivo", "values": [True, False]}, True),      # booleani: il difetto trovato dal vivo
    ({"kind": "accepted_values", "column": "attivo", "values": ["true", "FALSE"]}, True),
    ({"kind": "accepted_values", "column": "attivo", "values": [True]}, False),
    ({"kind": "accepted_values", "column": "quantita", "values": [1, 2.5, -3, "40"]}, True),   # 1 combacia con 1.0, «40» con 40.0
    ({"kind": "accepted_values", "column": "quantita", "values": [1, 2.5]}, False),
    ({"kind": "accepted_values", "column": "giorno", "values": ["2026-10-01"]}, True),
    ({"kind": "range", "column": "quantita", "min": -3}, True),
    ({"kind": "range", "column": "quantita", "min": 0}, False),
    ({"kind": "range", "column": "quantita", "max": 40}, True),
    ({"kind": "range", "column": "quantita", "min": 0, "max": 10}, False),
    ({"kind": "range", "column": "creato", "min": "2026-10-01"}, True),
    ({"kind": "range", "column": "creato", "min": "2026-10-02T00:00:00"}, False),
    ({"kind": "range", "column": "giorno", "max": "2026-10-01"}, True),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Za-z]{3}-\\d+"}, True),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Z]{3}-\\d+"}, False),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Z]{3}"}, False),   # per INTERO, non «comincia con»
    ({"kind": "row_count", "min": 1}, True),
    ({"kind": "row_count", "min": 6}, False),
    ({"kind": "row_count", "max": 5}, True),
    ({"kind": "row_count", "min": 1, "max": 4}, False),
    ({"kind": "expression", "sql": "id > 0"}, True),
    ({"kind": "expression", "sql": "quantita >= 0"}, False),              # -3, e un null
    ({"kind": "expression", "sql": "spedito >= creato"}, False),
    ({"kind": "expression", "sql": "ordine * 2 >= riga"}, True),
])
def test_ogni_regola_quando_regge_e_quando_no(regola, passa):
    assert uno(regola)["passed"] is passa, uno(regola)


def test_un_conteggio_dice_quante_righe_violano_e_ne_mostra_qualcuna():
    r = uno({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito"]})
    assert (r["passed"], r["violations"], r["sample"]) == (False, 1, ["perso"])
    r = uno({"kind": "unique", "columns": ["ordine"]})
    assert r["violations"] == 3 and r["sample"] == [10]
    r = uno({"kind": "range", "column": "quantita", "min": 0})
    assert r["violations"] == 1 and r["sample"] == [-3.0]   # il null non è fuori intervallo
    r = uno({"kind": "not_null", "column": "stato"})
    assert r["violations"] == 1 and "sample" not in r        # di un null non c'è niente da mostrare


def test_in_un_espressione_il_non_lo_so_non_e_un_si():
    """`spedito >= creato` con `spedito` null dà null: quella riga viola."""
    r = uno({"kind": "expression", "sql": "spedito >= creato"})
    assert r["violations"] == 2   # una data prima, e un null


def test_gli_esempi_sono_pochi_e_tagliati():
    lungo = pl.LazyFrame({"t": [f"{'x' * 200}{i}" for i in range(50)]})
    r = uno({"kind": "pattern", "column": "t", "regex": "y+"}, lungo)
    assert r["violations"] == 50 and len(r["sample"]) == 5 and all(len(s) <= 80 for s in r["sample"])
    assert "sample" not in evaluate(lungo, {"rules": [{"id": "r", "severity": "error", "kind": "pattern", "column": "t", "regex": "y+"}]}, samples=False)["rules"][0]


# ── una regola che non si può valutare NON regge ─────────────────────────────
@pytest.mark.parametrize("regola, perche", [
    ({"kind": "not_null", "column": "non_esiste"}, "does not exist"),
    ({"kind": "not_null"}, "names no column"),
    ({"kind": "unique", "columns": ["id", "boh"]}, "does not exist"),
    ({"kind": "range", "column": "stato", "min": 0}, "needs a number or a date"),
    ({"kind": "range", "column": "quantita"}, "neither a minimum nor a maximum"),
    ({"kind": "range", "column": "creato", "min": "ieri"}, "not a valid datetime"),
    ({"kind": "pattern", "column": "sku", "regex": "([A-Z"}, "invalid pattern"),
    ({"kind": "accepted_values", "column": "stato", "values": []}, "empty"),
    ({"kind": "accepted_values", "column": "attivo", "values": ["forse"]}, "not a boolean"),
    ({"kind": "row_count"}, "neither a minimum nor a maximum"),
    ({"kind": "expression", "sql": "questa non è sql ((("}, "invalid expression"),
    ({"kind": "expression", "sql": "colonna_che_non_ce > 1"}, ""),
    ({"kind": "freshness", "max_age_hours": 0}, "positive number"),
    ({"kind": "freshness", "max_age_hours": 24, "column": "stato"}, "needs a date"),
    ({"kind": "regola_inventata"}, "unknown rule kind"),
])
def test_una_regola_che_non_si_puo_valutare_non_regge_e_dice_perche(regola, perche):
    r = uno(regola)
    assert r["passed"] is False and r.get("error"), r
    assert perche in r["error"], r["error"]


def test_una_regola_rotta_non_impedisce_di_valutare_le_altre():
    referto = evaluate(dati(), {"rules": [
        {"id": "a", "kind": "expression", "sql": "boh > 1", "severity": "warning"},
        {"id": "b", "kind": "not_null", "column": "id", "severity": "error"},
        {"id": "c", "kind": "not_null", "column": "stato", "severity": "warning"},
    ]})
    assert [(x["id"], x["passed"]) for x in referto["rules"]] == [("a", False), ("b", True), ("c", False)]
    assert referto["rows"] == 5


# ── l'esito viene dalle severità ─────────────────────────────────────────────
def _esito(*regole) -> str:
    return evaluate(dati(), {"rules": [{"id": f"r{i}", **r} for i, r in enumerate(regole)]})["outcome"]


def test_due_severita_tre_esiti():
    regge = {"kind": "not_null", "column": "id"}
    cade = {"kind": "not_null", "column": "stato"}
    assert _esito({**regge, "severity": "error"}, {**regge, "severity": "warning"}) == "passed"
    assert _esito({**regge, "severity": "error"}, {**cade, "severity": "warning"}) == "warning"
    assert _esito({**cade, "severity": "error"}, {**regge, "severity": "warning"}) == "failed"
    assert _esito({**cade, "severity": "error"}, {**cade, "severity": "warning"}) == "failed"
    assert _esito() == "passed"   # un contratto vuoto non promette niente


def test_il_referto_conta_errori_e_avvisi():
    r = evaluate(dati(), {"rules": [
        {"id": "a", "kind": "not_null", "column": "stato", "severity": "error"},
        {"id": "b", "kind": "not_null", "column": "sku", "severity": "warning"},
        {"id": "c", "kind": "not_null", "column": "quantita", "severity": "warning"},
        {"id": "d", "kind": "not_null", "column": "id", "severity": "error"},
    ]})
    assert (r["outcome"], r["errors"], r["warnings"], r["rows"]) == ("failed", 1, 2, 5)


@pytest.mark.parametrize("severita", [None, "info", "blocker", "", 3])
def test_una_severita_che_non_e_avviso_ne_errore_non_passa_in_silenzio(severita):
    r = evaluate(dati(), {"rules": [{"id": "a", "kind": "not_null", "column": "id", "severity": severita}]})
    assert r["rules"][0]["passed"] is False and "severity" in r["rules"][0]["error"] and r["outcome"] == "failed"


# ── freschezza ───────────────────────────────────────────────────────────────
def test_la_freschezza_dello_snapshot_guarda_quando_i_dati_sono_stati_prodotti():
    assert uno({"kind": "freshness", "max_age_hours": 24})["passed"] is True          # appena scritti
    r = uno({"kind": "freshness", "max_age_hours": 24}, snapshot_at=ADESSO - timedelta(hours=30))
    assert (r["passed"], r["observed"], r["expected"]) == (False, 30.0, 24)
    assert uno({"kind": "freshness", "max_age_hours": 48}, snapshot_at=ADESSO - timedelta(hours=30))["passed"] is True


def test_la_freschezza_di_una_colonna_guarda_il_valore_piu_recente():
    r = uno({"kind": "freshness", "max_age_hours": 30, "column": "creato"})       # 5 ottobre 09:00 → 27 ore prima
    assert (r["passed"], r["observed"]) == (True, 27.0)
    assert uno({"kind": "freshness", "max_age_hours": 24, "column": "creato"})["passed"] is False
    assert uno({"kind": "freshness", "max_age_hours": 24 * 6, "column": "giorno"})["passed"] is True
    vuota = dati().filter(pl.col("id") < 0)
    assert uno({"kind": "freshness", "max_age_hours": 24, "column": "creato"}, vuota)["passed"] is False


# ── casi limite ──────────────────────────────────────────────────────────────
def test_una_tabella_vuota():
    vuota = dati().filter(pl.col("id") < 0)
    assert uno({"kind": "not_null", "column": "stato"}, vuota)["passed"] is True
    assert uno({"kind": "unique", "columns": ["id"]}, vuota)["passed"] is True
    assert uno({"kind": "row_count", "min": 1}, vuota) == {"id": "r", "kind": "row_count", "severity": "error", "passed": False, "observed": 0, "expected": {"min": 1, "max": None}}
    r = uno({"kind": "not_null", "column": "stato"})
    assert r["column"] == "stato"                       # il referto dice su che cosa era la regola
    assert uno({"kind": "unique", "columns": ["ordine", "riga"]})["columns"] == ["ordine", "riga"]
    assert evaluate(dati(), {"version": 7, "rules": []})["version"] == 7   # e di quale versione del contratto
    assert uno({"kind": "column", "column": "id", "dtype": "integer"}, vuota)["passed"] is True


def test_le_famiglie_dei_tipi():
    casi = {pl.Int8: "integer", pl.UInt64: "integer", pl.Float32: "number", pl.Decimal(10, 2): "number", pl.String: "string",
            pl.Boolean: "boolean", pl.Date: "date", pl.Datetime("ms"): "datetime", pl.Datetime("us", "UTC"): "datetime", pl.List(pl.Int8): "other"}
    for tipo, attesa in casi.items():
        assert family(tipo) == attesa, tipo


def test_due_milioni_di_righe_in_pochi_secondi(tmp_path):
    import time

    n = 2_000_000
    pl.DataFrame({"id": range(n), "stato": ["a", "b", "c", "d"] * (n // 4), "v": [float(i % 1000) for i in range(n)]}).write_parquet(tmp_path / "g.parquet")
    t = time.monotonic()
    r = evaluate(pl.scan_parquet(tmp_path / "g.parquet"), {"rules": [
        {"id": "a", "kind": "unique", "columns": ["id"], "severity": "error"},
        {"id": "b", "kind": "not_null", "column": "stato", "severity": "error"},
        {"id": "c", "kind": "accepted_values", "column": "stato", "values": ["a", "b", "c"], "severity": "warning"},
        {"id": "d", "kind": "range", "column": "v", "min": 0, "max": 999, "severity": "error"},
        {"id": "e", "kind": "expression", "sql": "v >= 0 AND id >= 0", "severity": "error"},
    ]})
    assert time.monotonic() - t < 15
    assert (r["outcome"], r["rows"], r["rules"][2]["violations"], r["rules"][2]["sample"]) == ("warning", n, n // 4, ["d"])


# ── profilo e proposta ───────────────────────────────────────────────────────
def test_il_profilo_dice_che_cosa_c_e_nei_dati():
    p = profile(dati())
    per = {c["name"]: c for c in p["columns"]}
    assert p["rows"] == 5
    assert (per["id"]["family"], per["id"]["nulls"], per["id"]["distinct"], per["id"]["min"], per["id"]["max"]) == ("integer", 0, 5, 1, 5)
    assert (per["stato"]["nulls"], per["stato"]["family"]) == (1, "string")
    assert per["creato"]["min"] == "2026-10-01T00:00:00"


def test_la_proposta_e_prudente_e_il_contratto_proposto_regge_sui_dati_da_cui_nasce():
    lf = pl.LazyFrame({"id": list(range(40)), "canale": ["web", "negozio"] * 20, "note": [None] * 40, "importo": [1.5] * 40})
    contratto = propose(profile(lf))
    # con i tipi che si incontrano davvero: date, booleani, decimali, testo con pochi valori e null
    vero = pl.LazyFrame({"giorno": [date(2026, 1, 1 + i % 28) for i in range(60)], "festivo": [True, False, False] * 20,
                         "nome": [None, "Natale", "Pasqua"] * 20, "anno": pl.Series([2026] * 60, dtype=pl.Int32),
                         "prezzo": pl.Series(["1.50"] * 60).cast(pl.Decimal(5, 2))})
    proposto = propose(profile(vero))
    assert evaluate(vero, proposto)["outcome"] == "passed", [r for r in evaluate(vero, proposto)["rules"] if not r["passed"]]
    assert not any(r["kind"] == "accepted_values" and r["column"] == "festivo" for r in proposto["rules"])
    per_tipo = {}
    for r in contratto["rules"]:
        per_tipo.setdefault(r["kind"], []).append(r)
    # strutturale = errore; vero oggi = avviso
    assert all(r["severity"] == "error" for r in per_tipo["column"]) and len(per_tipo["column"]) == 4
    assert {r["column"] for r in per_tipo["not_null"]} == {"id", "canale", "importo"} and all(r["severity"] == "warning" for r in per_tipo["not_null"])
    assert [r["columns"] for r in per_tipo["unique"]] == [["id"]]
    assert [(r["column"], r["values"]) for r in per_tipo["accepted_values"]] == [("canale", ["negozio", "web"])]
    assert per_tipo["row_count"][0]["min"] == 1
    assert len({r["id"] for r in contratto["rules"]}) == len(contratto["rules"])
    assert evaluate(lf, contratto)["outcome"] == "passed"
