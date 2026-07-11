"""Test delle singole operazioni: ogni test è una frase sull'engine.

Le operazioni pure (senza storage) si testano direttamente su un LazyFrame.
Quelle che leggono altre sorgenti (join, foreach col driver) usano
l'OperationContext con il FakeStorage.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.engine.context import OperationContext
from app.engine.exceptions import EngineError
from app.engine.operations import get_operation
from tests.conftest import upload_df


def df_base() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "paese": ["IT", "FR", "DE", "IT"],
            "vendite": [100, 50, 250, 300],
            "data": ["2024-01-15", "2024-03-01", "2025-02-10", "2024-06-30"],
        }
    ).with_columns(pl.col("data").str.to_date())


def apply(op_type: str, params: dict, df: pl.DataFrame | None = None, ctx=None) -> pl.DataFrame:
    """Applica una singola operazione e materializza il risultato."""
    lf = (df if df is not None else df_base()).lazy()
    return get_operation(op_type)(lf, params, ctx).collect(engine="streaming")


# ── Colonne ──────────────────────────────────────────────────────────────────
def test_select_tiene_solo_le_colonne_indicate():
    out = apply("select", {"columns": ["paese"]})
    assert out.columns == ["paese"]


def test_drop_rimuove_le_colonne_indicate():
    out = apply("drop", {"columns": ["data"]})
    assert out.columns == ["paese", "vendite"]


def test_rename_rinomina_secondo_il_mapping():
    out = apply("rename", {"mapping": {"vendite": "ricavi"}})
    assert "ricavi" in out.columns and "vendite" not in out.columns


def test_cast_converte_il_tipo():
    out = apply("cast", {"columns": {"vendite": "float"}})
    assert out.schema["vendite"] == pl.Float64


# ── Filtri ───────────────────────────────────────────────────────────────────
def test_filter_eq_su_stringa():
    out = apply("filter", {"column": "paese", "operator": "eq", "value": "IT"})
    assert out.height == 2


def test_filter_gt_numerico():
    out = apply("filter", {"column": "vendite", "operator": "gt", "value": 100})
    assert set(out["paese"]) == {"DE", "IT"}


def test_filter_in_con_lista():
    out = apply("filter", {"column": "paese", "operator": "in", "value": ["IT", "FR"]})
    assert out.height == 3


def test_filter_between_numerico_estremi_inclusi():
    out = apply("filter", {"column": "vendite", "operator": "between", "value": [50, 250]})
    assert set(out["vendite"]) == {50, 100, 250}


def test_filter_between_su_date_accetta_stringhe_iso():
    out = apply("filter", {"column": "data", "operator": "between", "value": ["2024-01-01", "2024-12-31"]})
    assert out.height == 3  # esclude il 2025


def test_filter_gt_su_data_accetta_stringa_iso():
    out = apply("filter", {"column": "data", "operator": "gt", "value": "2024-06-01"})
    assert out.height == 2


def test_filter_between_su_colonna_stringa_non_scambia_i_valori_per_colonne():
    # regressione: is_between con stringhe nude le interpretava come NOMI colonna
    df = pl.DataFrame({"s": ["alfa", "beta", "zeta"]})
    out = apply("filter", {"column": "s", "operator": "between", "value": ["a", "c"]}, df=df)
    assert set(out["s"]) == {"alfa", "beta"}


def test_filter_data_formato_sbagliato_da_errore_chiaro():
    with pytest.raises(EngineError, match="formato ISO"):
        apply("filter", {"column": "data", "operator": "gt", "value": "15/01/2024"})


def test_filter_contains_su_testo():
    out = apply("filter", {"column": "paese", "operator": "contains", "value": "T"})
    assert set(out["paese"]) == {"IT"}


def test_filter_is_null_e_is_not_null():
    df = pl.DataFrame({"v": [1, None, 3]})
    assert apply("filter", {"column": "v", "operator": "is_null"}, df=df).height == 1
    assert apply("filter", {"column": "v", "operator": "is_not_null"}, df=df).height == 2


# ── Righe ────────────────────────────────────────────────────────────────────
def test_sort_discendente():
    out = apply("sort", {"by": "vendite", "descending": True})
    assert out["vendite"].to_list() == [300, 250, 100, 50]


def test_limit_taglia_le_righe():
    assert apply("limit", {"n": 2}).height == 2


def test_unique_deduplica_sul_sottoinsieme():
    out = apply("unique", {"subset": ["paese"]})
    assert out.height == 3  # IT compare una volta sola


def test_fill_null_per_colonna():
    df = pl.DataFrame({"v": [1, None], "s": ["a", None]})
    out = apply("fill_null", {"columns": {"v": 0, "s": "n/d"}}, df=df)
    assert out["v"].to_list() == [1, 0]
    assert out["s"].to_list() == ["a", "n/d"]


def test_drop_nulls_rimuove_le_righe_incomplete():
    df = pl.DataFrame({"v": [1, None, 3]})
    assert apply("drop_nulls", {}, df=df).height == 2


# ── Aggregazioni ─────────────────────────────────────────────────────────────
def test_group_by_somma_con_alias():
    out = apply(
        "group_by",
        {"by": ["paese"], "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}]},
    ).sort("paese")
    assert out.columns == ["paese", "tot"]
    assert out.filter(pl.col("paese") == "IT")["tot"].item() == 400


def test_group_by_aggregazioni_multiple():
    out = apply(
        "group_by",
        {
            "by": ["paese"],
            "aggregations": [
                {"column": "vendite", "func": "sum", "alias": "tot"},
                {"column": "vendite", "func": "max", "alias": "massimo"},
            ],
        },
    )
    assert {"tot", "massimo"} <= set(out.columns)


# ── Join (il lato destro passa dallo storage) ────────────────────────────────
def test_join_inner_con_sorgente_semplice(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        out = apply(
            "join",
            {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "on": ["paese"], "how": "inner"},
            ctx=ctx,
        )
        assert set(out["nome"]) == {"Italia", "Francia"}
        assert "DE" not in out["paese"].to_list()
    finally:
        ctx.cleanup()


def test_join_con_sotto_flow_sul_lato_destro(storage, anagrafica):
    # il ramo destro può essere una catena trasformata, non solo una sorgente
    ctx = OperationContext(storage)
    try:
        right = {
            "source": {"bucket": anagrafica.bucket, "key": anagrafica.key},
            "operations": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}],
        }
        out = apply("join", {"right": right, "on": ["paese"], "how": "inner"}, ctx=ctx)
        assert set(out["paese"]) == {"IT"}
    finally:
        ctx.cleanup()


def test_join_left_tiene_le_righe_senza_match(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        out = apply(
            "join",
            {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "on": ["paese"], "how": "left"},
            ctx=ctx,
        )
        assert out.height == 4  # DE resta, con nome null
        assert out.filter(pl.col("paese") == "DE")["nome"].item() is None
    finally:
        ctx.cleanup()


# ── Foreach (cicli con placeholder) ──────────────────────────────────────────
def test_foreach_items_statici_appende_le_iterazioni():
    out = apply(
        "foreach",
        {
            "items": [{"p": "IT"}, {"p": "FR"}],
            "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
        },
    )
    assert set(out["paese"]) == {"IT", "FR"}
    assert out.height == 3  # 2 righe IT + 1 FR


def test_foreach_placeholder_preserva_il_tipo():
    # "{{soglia}}" da solo → resta numero: il filtro gt funziona
    out = apply(
        "foreach",
        {
            "items": [{"soglia": 200}],
            "body": [{"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": "{{soglia}}"}}],
        },
    )
    assert set(out["vendite"]) == {250, 300}


def test_foreach_placeholder_tollera_spazi_e_nomi_con_spazi():
    df = pl.DataFrame({"Customer ID": [1, 2], "v": [10, 20]})
    out = apply(
        "foreach",
        {
            "items": [{"Customer ID": 2}],
            "body": [{"type": "filter", "params": {"column": "Customer ID", "operator": "eq", "value": "{{ Customer ID }}"}}],
        },
        df=df,
    )
    assert out["v"].to_list() == [20]


def test_foreach_placeholder_nelle_chiavi_dei_dict():
    # regressione: rename/cast/fill hanno la colonna come CHIAVE del mapping
    out = apply(
        "foreach",
        {
            "items": [{"col": "vendite"}],
            "body": [{"type": "rename", "params": {"mapping": {"{{col}}": "valore"}}}],
        },
    )
    assert "valore" in out.columns


def test_foreach_placeholder_immerso_nel_testo_diventa_stringa():
    out = apply(
        "foreach",
        {
            "items": [{"suff": "eu"}],
            "body": [{"type": "rename", "params": {"mapping": {"vendite": "vendite_{{suff}}"}}}],
        },
    )
    assert "vendite_eu" in out.columns


def test_foreach_add_keys_as_columns_etichetta_le_iterazioni():
    out = apply(
        "foreach",
        {
            "items": [{"p": "IT"}, {"p": "DE"}],
            "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
            "add_keys_as_columns": True,
        },
    )
    assert "p" in out.columns
    assert set(out["p"]) == {"IT", "DE"}


def test_foreach_driver_da_storage(storage, anagrafica):
    # il driver è un sotto-flow: ogni RIGA un'iterazione, le COLONNE i placeholder
    ctx = OperationContext(storage)
    try:
        out = apply(
            "foreach",
            {
                "driver": {"source": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "operations": []},
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{paese}}"}}],
            },
            ctx=ctx,
        )
        assert set(out["paese"]) == {"IT", "FR"}
    finally:
        ctx.cleanup()


def test_foreach_placeholder_mancante_da_errore_con_le_chiavi_disponibili():
    with pytest.raises(EngineError, match="non trovato.*disponibili"):
        apply(
            "foreach",
            {
                "items": [{"x": 1}],
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{manca}}"}}],
            },
        )


def test_foreach_senza_iterazioni_da_errore_chiaro():
    with pytest.raises(EngineError, match="senza iterazioni"):
        apply("foreach", {"body": [{"type": "limit", "params": {"n": 1}}]})


def test_foreach_corpo_vuoto_da_errore_chiaro():
    with pytest.raises(EngineError, match="corpo vuoto"):
        apply("foreach", {"items": [{"p": 1}], "body": []})


def test_foreach_errore_nel_corpo_indica_quale_iterazione():
    # il 2° item non ha la chiave usata dal corpo → l'errore dice "iterazione 2/2"
    with pytest.raises(EngineError, match=r"iterazione 2/2"):
        apply(
            "foreach",
            {
                "items": [{"p": "IT"}, {"altro": "FR"}],
                "body": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "{{p}}"}}],
            },
        )


# ── Reshape: pivot / unpivot ─────────────────────────────────────────────────
def _df_vendite_per_anno() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "paese": ["IT", "IT", "FR", "FR", "IT"],
            "anno": ["2023", "2024", "2023", "2024", "2023"],
            "vendite": [10, 20, 5, 7, 3],
        }
    )


def test_pivot_trasforma_le_righe_in_colonne_aggregando():
    out = apply(
        "pivot",
        {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"},
        _df_vendite_per_anno(),
    ).sort("paese")
    assert out.columns == ["paese", "2023", "2024"]  # colonne ordinate
    it = out.filter(pl.col("paese") == "IT")
    assert it["2023"].item() == 13  # 10 + 3: le righe duplicate si sommano
    assert it["2024"].item() == 20


def test_pivot_func_default_e_sum():
    out = apply("pivot", {"index": ["paese"], "on": "anno", "values": "vendite"}, _df_vendite_per_anno())
    assert out.filter(pl.col("paese") == "IT")["2023"].item() == 13


def test_pivot_combinazioni_mancanti_diventano_null():
    df = pl.DataFrame({"k": ["a", "b"], "on": ["x", "y"], "v": [1, 2]})
    out = apply("pivot", {"index": ["k"], "on": "on", "values": "v"}, df).sort("k")
    assert out.filter(pl.col("k") == "a")["y"].item() is None


def test_pivot_rifiuta_troppe_colonne(monkeypatch):
    import app.engine.operations as ops

    monkeypatch.setattr(ops, "MAX_PIVOT_COLUMNS", 2)
    df = pl.DataFrame({"k": ["a", "a", "a"], "on": ["x", "y", "z"], "v": [1, 2, 3]})
    with pytest.raises(EngineError, match="valori distinti"):
        apply("pivot", {"index": ["k"], "on": "on", "values": "v"}, df)


def test_pivot_senza_indice_da_errore_chiaro():
    with pytest.raises(EngineError, match="indice"):
        apply("pivot", {"index": [], "on": "anno", "values": "vendite"}, _df_vendite_per_anno())


def test_pivot_funzione_sconosciuta_da_errore_chiaro():
    with pytest.raises(EngineError, match="aggregazione"):
        apply(
            "pivot",
            {"index": ["paese"], "on": "anno", "values": "vendite", "func": "boh"},
            _df_vendite_per_anno(),
        )


def test_unpivot_trasforma_le_colonne_in_righe():
    df = pl.DataFrame({"id": [1, 2], "gen": [10, 20], "feb": [30, 40]})
    out = apply(
        "unpivot",
        {"index": ["id"], "on": ["gen", "feb"], "variable_name": "mese", "value_name": "valore"},
        df,
    )
    assert out.columns == ["id", "mese", "valore"]
    assert out.height == 4
    assert out.filter((pl.col("id") == 1) & (pl.col("mese") == "feb"))["valore"].item() == 30


def test_unpivot_senza_on_scioglie_tutte_le_altre_colonne():
    df = pl.DataFrame({"id": [1], "a": [10], "b": [20]})
    out = apply("unpivot", {"index": ["id"]}, df)
    assert out.height == 2
    assert set(out["variable"].to_list()) == {"a", "b"}


def test_pivot_poi_unpivot_riporta_ai_dati_di_partenza():
    # andata e ritorno: pivot e unpivot sono operazioni inverse
    pivoted = apply(
        "pivot",
        {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"},
        _df_vendite_per_anno(),
    )
    back = apply(
        "unpivot",
        {"index": ["paese"], "variable_name": "anno", "value_name": "vendite"},
        pivoted,
    ).sort(["paese", "anno"])
    atteso = (
        _df_vendite_per_anno()
        .group_by(["paese", "anno"])
        .agg(pl.col("vendite").sum())
        .sort(["paese", "anno"])
    )
    assert back.equals(atteso)


# ── Compute (colonne calcolate, espressioni SQL) ─────────────────────────────
def test_compute_espressione_aritmetica():
    out = apply("compute", {"columns": [{"name": "doppio", "expr": "vendite * 2"}]})
    assert out["doppio"].to_list() == [200, 100, 500, 600]


def test_compute_case_when_e_funzioni_stringa():
    out = apply(
        "compute",
        {"columns": [{"name": "fascia", "expr": "CASE WHEN vendite >= 250 THEN UPPER(paese) ELSE 'altro' END"}]},
    )
    assert out["fascia"].to_list() == ["altro", "altro", "DE", "IT"]


def test_compute_window_function_over_partition():
    out = apply(
        "compute",
        {"columns": [{"name": "tot_paese", "expr": "SUM(vendite) OVER (PARTITION BY paese)"}]},
    )
    per_riga = dict(zip(out["paese"].to_list(), out["tot_paese"].to_list()))
    assert per_riga["IT"] == 400 and per_riga["FR"] == 50 and per_riga["DE"] == 250


def test_compute_window_running_con_order_by():
    out = apply(
        "compute",
        {"columns": [{"name": "progressivo", "expr": "SUM(vendite) OVER (PARTITION BY paese ORDER BY data)"}]},
    )
    it = out.filter(pl.col("paese") == "IT").sort("data")
    assert it["progressivo"].to_list() == [100, 400]  # running sum, non totale


def test_compute_sequenziale_usa_le_colonne_precedenti():
    out = apply(
        "compute",
        {"columns": [
            {"name": "netto", "expr": "vendite * 0.8"},
            {"name": "netto_iva", "expr": "netto * 1.22"},  # usa la colonna appena creata
        ]},
    )
    assert out["netto_iva"][0] == pytest.approx(100 * 0.8 * 1.22)


def test_compute_sovrascrive_una_colonna_esistente():
    out = apply("compute", {"columns": [{"name": "vendite", "expr": "vendite + 1"}]})
    assert out["vendite"].to_list() == [101, 51, 251, 301]


def test_compute_espressione_malformata_da_errore_parlante():
    with pytest.raises(EngineError, match="non valida"):
        apply("compute", {"columns": [{"name": "x", "expr": "SUM(("}]})


def test_compute_nome_vuoto_rifiutato():
    with pytest.raises(EngineError, match="nome"):
        apply("compute", {"columns": [{"name": " ", "expr": "1 + 1"}]})


def test_compute_lista_vuota_rifiutata():
    with pytest.raises(EngineError, match="almeno una"):
        apply("compute", {"columns": []})


# ── Union (il ramo destro passa dallo storage, come il join) ────────────────
def test_union_relaxed_allinea_colonne_per_nome(storage):
    destra = upload_df(
        storage,
        pl.DataFrame({"paese": ["ES"], "vendite": [75], "canale": ["web"]}),
        "datasets/vendite_es.parquet",
    )
    ctx = OperationContext(storage)
    try:
        out = apply("union", {"right": {"bucket": destra.bucket, "key": destra.key}}, ctx=ctx)
        assert out.height == 5  # 4 righe base + 1 accodata
        assert "canale" in out.columns and "data" in out.columns
        es = out.filter(pl.col("paese") == "ES")
        assert es["canale"].item() == "web" and es["data"].item() is None
        assert out.filter(pl.col("paese") == "IT")["canale"].null_count() == 2
    finally:
        ctx.cleanup()


def test_union_strict_rifiuta_schemi_diversi(storage):
    destra = upload_df(
        storage,
        pl.DataFrame({"paese": ["ES"], "solo_qui": [1]}),
        "datasets/schema_diverso.parquet",
    )
    ctx = OperationContext(storage)
    try:
        with pytest.raises(Exception):  # errore Polars a runtime: schemi non identici
            apply("union", {"right": {"bucket": destra.bucket, "key": destra.key}, "strategy": "strict"}, ctx=ctx)
    finally:
        ctx.cleanup()


def test_union_con_sotto_flow_sul_lato_destro(storage, anagrafica):
    right = {
        "source": {"bucket": anagrafica.bucket, "key": anagrafica.key},
        "operations": [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}],
    }
    ctx = OperationContext(storage)
    try:
        out = apply("union", {"right": right}, ctx=ctx)
        assert out.height == 5
        assert out.filter(pl.col("nome") == "Italia").height == 1
    finally:
        ctx.cleanup()


def test_union_strategia_sconosciuta_rifiutata(storage, anagrafica):
    ctx = OperationContext(storage)
    try:
        with pytest.raises(EngineError, match="strategia"):
            apply("union", {"right": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "strategy": "boh"}, ctx=ctx)
    finally:
        ctx.cleanup()


# ── Guardie foreach (anti-DoS): cap per-livello sugli items + budget cumulativo ──
def test_foreach_items_statici_oltre_il_cap_rifiutati(monkeypatch):
    from app.engine import operations as ops
    monkeypatch.setattr(ops, "MAX_FOREACH_ITERATIONS", 5)
    with pytest.raises(EngineError, match="oltre il limite"):
        apply(
            "foreach",
            {
                "items": [{"p": "IT"}] * 6,  # 6 > 5
                "body": [{"type": "limit", "params": {"n": 1}}],
            },
        )


def test_foreach_annidato_supera_il_budget_totale(monkeypatch, storage):
    # due livelli 3×3 = 9 iterazioni totali; col budget a 5 deve tagliare
    from app.engine import operations as ops
    monkeypatch.setattr(ops, "MAX_FOREACH_TOTAL", 5)
    ctx = OperationContext(storage)
    try:
        with pytest.raises(EngineError, match="iterazioni totali"):
            apply(
                "foreach",
                {
                    "items": [{"a": 1}, {"a": 2}, {"a": 3}],
                    "body": [
                        {
                            "type": "foreach",
                            "params": {
                                "items": [{"b": 1}, {"b": 2}, {"b": 3}],
                                "body": [{"type": "limit", "params": {"n": 1}}],
                            },
                        }
                    ],
                },
                ctx=ctx,
            )
    finally:
        ctx.cleanup()


def test_foreach_budget_ok_sotto_il_limite(storage):
    # sanity: sotto il budget il foreach annidato funziona
    ctx = OperationContext(storage)
    try:
        out = apply(
            "foreach",
            {
                "items": [{"a": 1}, {"a": 2}],
                "body": [
                    {
                        "type": "foreach",
                        "params": {
                            "items": [{"b": 1}, {"b": 2}],
                            "body": [{"type": "limit", "params": {"n": 1}}],
                        },
                    }
                ],
            },
            ctx=ctx,
        )
        assert out.height == 4 * 1  # 2×2 iterazioni, ognuna 1 riga (limit 1) su df_base
    finally:
        ctx.cleanup()
